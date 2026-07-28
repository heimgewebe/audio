#!/usr/bin/env python3
"""Build a deterministic, atomic humpback-whale sample bank."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import wave
from array import array
from collections import Counter
from dataclasses import dataclass
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "assets" / "whale-sources"
SOURCE_CATALOG = SOURCE_ROOT / "SOURCES.json"
PROCESSED_ROOT = SOURCE_ROOT / "processed"
SAMPLE_RATE = 48_000
TARGET_PEAK = 25_500
WINDOW_SECONDS = 0.25
HOP_SECONDS = 0.125
MIN_PEAK_DISTANCE_SECONDS = 3.5
CLIP_SECONDS = {"low": 6.0, "song": 7.0, "high": 5.0}
ROOT_RANGES = {"low": (24, 48), "song": (42, 90), "high": (84, 108)}
SOURCE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ALLOWED_LICENSES = {"CC0-1.0", "Public-Domain-US-NPS", "CC-BY-2.5"}
CATALOG_SCHEMA_VERSION = 2
FileIdentity = tuple[int, int, int]


class _RecoveryRequiredError(RuntimeError):
    """Report an ambiguous replace without allowing generic staging cleanup."""


def _file_identity(metadata: os.stat_result) -> FileIdentity:
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _entry_identity(directory_fd: int, name: str) -> FileIdentity | None:
    try:
        return _file_identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _open_nofollow(directory_fd: int, name: str, *, directory: bool) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None:
        raise RuntimeError(
            "descriptor-bound no-follow directory operations are unavailable"
        )
    flags = os.O_RDONLY | os.O_CLOEXEC | no_follow
    if directory:
        flags |= directory_flag
    return os.open(name, flags, dir_fd=directory_fd)


def _require_identity(
    actual: FileIdentity | None, expected: FileIdentity, label: str
) -> None:
    if actual != expected:
        raise RuntimeError(
            f"whale bank protected identity changed for {label}: "
            f"expected={expected!r}; actual={actual!r}"
        )


@dataclass
class _ProtectedBindings:
    source_root: pathlib.Path
    source_root_fd: int
    source_root_identity: FileIdentity
    raw_fd: int
    raw_identity: FileIdentity
    catalog_name: str
    catalog_fd: int
    catalog_identity: FileIdentity
    source_fds: tuple[tuple[str, int, FileIdentity], ...]
    output_name: str
    output_fd: int | None
    output_identity: FileIdentity | None

    @property
    def protected_identities(self) -> frozenset[FileIdentity]:
        return frozenset(
            (
                self.source_root_identity,
                self.raw_identity,
                self.catalog_identity,
                *(identity for _name, _fd, identity in self.source_fds),
            )
        )

    def verify(self) -> None:
        _require_identity(
            _file_identity(os.fstat(self.source_root_fd)),
            self.source_root_identity,
            "source root descriptor",
        )
        _require_identity(
            _file_identity(os.stat(self.source_root, follow_symlinks=False)),
            self.source_root_identity,
            "source root path",
        )
        _require_identity(
            _file_identity(os.fstat(self.raw_fd)),
            self.raw_identity,
            "raw descriptor",
        )
        _require_identity(
            _entry_identity(self.source_root_fd, "raw"),
            self.raw_identity,
            "raw",
        )
        _require_identity(
            _file_identity(os.fstat(self.catalog_fd)),
            self.catalog_identity,
            "catalog descriptor",
        )
        _require_identity(
            _entry_identity(self.source_root_fd, self.catalog_name),
            self.catalog_identity,
            "catalog",
        )
        for name, descriptor, identity in self.source_fds:
            _require_identity(
                _file_identity(os.fstat(descriptor)),
                identity,
                f"raw/{name} descriptor",
            )
            _require_identity(
                _entry_identity(self.raw_fd, name),
                identity,
                f"raw/{name}",
            )

    def close(self) -> None:
        for _name, descriptor, _identity in self.source_fds:
            os.close(descriptor)
        if self.output_fd is not None:
            os.close(self.output_fd)
        os.close(self.catalog_fd)
        os.close(self.raw_fd)
        os.close(self.source_root_fd)


def _bind_protected_inputs(
    source_root: pathlib.Path,
    source_catalog: pathlib.Path,
    sources: list[dict[str, Any]],
    output_root: pathlib.Path,
) -> _ProtectedBindings:
    source_root = source_root.resolve(strict=True)
    source_catalog = source_catalog.resolve(strict=True)
    source_root_fd = -1
    raw_fd = -1
    catalog_fd = -1
    output_fd: int | None = None
    source_fds: list[tuple[str, int, FileIdentity]] = []
    try:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory_flag is None:
            raise RuntimeError(
                "descriptor-bound no-follow directory operations are unavailable"
            )
        source_root_fd = os.open(
            source_root,
            os.O_RDONLY | os.O_CLOEXEC | no_follow | directory_flag,
        )
        source_root_identity = _file_identity(os.fstat(source_root_fd))
        raw_fd = _open_nofollow(source_root_fd, "raw", directory=True)
        raw_identity = _file_identity(os.fstat(raw_fd))
        if raw_identity[2] != stat.S_IFDIR:
            raise RuntimeError("bound whale raw input is not a directory")

        if source_catalog.parent != source_root:
            raise RuntimeError("whale source catalog moved outside its source root")
        catalog_fd = _open_nofollow(
            source_root_fd, source_catalog.name, directory=False
        )
        catalog_identity = _file_identity(os.fstat(catalog_fd))
        if catalog_identity[2] != stat.S_IFREG:
            raise RuntimeError("bound whale source catalog is not a regular file")

        for source in sources:
            source_path = pathlib.Path(source["_path"])
            if source_path.parent != source_root / "raw":
                raise RuntimeError("catalog-bound whale source moved outside raw")
            descriptor = _open_nofollow(raw_fd, source_path.name, directory=False)
            identity = _file_identity(os.fstat(descriptor))
            if identity[2] != stat.S_IFREG:
                os.close(descriptor)
                raise RuntimeError(
                    f"catalog-bound whale source is not regular: {source_path}"
                )
            source_fds.append((source_path.name, descriptor, identity))

        output_identity = _entry_identity(source_root_fd, output_root.name)
        if output_identity is not None:
            if output_identity[2] != stat.S_IFDIR:
                raise RuntimeError("bound whale bank output is not a directory")
            output_fd = _open_nofollow(source_root_fd, output_root.name, directory=True)
            _require_identity(
                _file_identity(os.fstat(output_fd)),
                output_identity,
                "initial output",
            )

        bindings = _ProtectedBindings(
            source_root=source_root,
            source_root_fd=source_root_fd,
            source_root_identity=source_root_identity,
            raw_fd=raw_fd,
            raw_identity=raw_identity,
            catalog_name=source_catalog.name,
            catalog_fd=catalog_fd,
            catalog_identity=catalog_identity,
            source_fds=tuple(source_fds),
            output_name=output_root.name,
            output_fd=output_fd,
            output_identity=output_identity,
        )
        bindings.verify()
        if output_fd is not None:
            _verify_tree_excludes_identities(
                output_fd, bindings.protected_identities, "initial output"
            )
        return bindings
    except BaseException:
        for _name, descriptor, _identity in source_fds:
            os.close(descriptor)
        if catalog_fd >= 0:
            os.close(catalog_fd)
        if output_fd is not None:
            os.close(output_fd)
        if raw_fd >= 0:
            os.close(raw_fd)
        if source_root_fd >= 0:
            os.close(source_root_fd)
        raise


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def _reject_symlink_chain(path: pathlib.Path, root: pathlib.Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise RuntimeError(f"symlink is not allowed in whale-bank path: {current}")
        if current == root:
            return
        if root not in current.parents:
            raise RuntimeError(f"path escapes whale source root: {path}")
        current = current.parent


def _source_path(source_root: pathlib.Path, value: object) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError("whale source file must be a non-empty relative path")
    relative = pathlib.PurePosixPath(value)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise RuntimeError(f"unsafe whale source path: {value!r}")
    if len(relative.parts) != 2 or relative.parts[0] != "raw":
        raise RuntimeError(f"whale source must be directly under raw/: {value!r}")
    if relative.suffix.lower() not in {".ogg", ".oga"}:
        raise RuntimeError(f"unsupported whale source extension: {value!r}")
    candidate = source_root.joinpath(*relative.parts)
    _reject_symlink_chain(candidate, source_root)
    if not candidate.exists() or not candidate.is_file():
        raise RuntimeError(f"missing source audio: {candidate}")
    resolved = candidate.resolve(strict=True)
    if resolved.parent != (source_root / "raw").resolve(strict=True):
        raise RuntimeError(f"whale source escapes raw directory: {value!r}")
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise RuntimeError(f"whale source is not a regular file: {candidate}")
    return resolved


def require_json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} root must be an object")
    return value


def _nonempty_text(source: dict[str, Any], field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"whale source {field} must be non-empty")
    return value


def load_catalog(
    source_catalog: pathlib.Path,
) -> tuple[pathlib.Path, list[dict[str, Any]]]:
    if source_catalog.is_symlink() or not source_catalog.is_file():
        raise RuntimeError("whale source catalog must be a regular non-symlink file")
    source_catalog = source_catalog.resolve(strict=True)
    source_root = source_catalog.parent
    _reject_symlink_chain(source_catalog, source_root)
    catalog = require_json_object(
        json.loads(source_catalog.read_text(encoding="utf-8")),
        "whale source catalog",
    )
    sources = catalog.get("sources")
    if (
        catalog.get("schema_version") != CATALOG_SCHEMA_VERSION
        or catalog.get("kind") != "humpback_whale_source_catalog"
        or not isinstance(sources, list)
        or not sources
    ):
        raise RuntimeError("whale source catalog has the wrong schema")

    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw_source in sources:
        if not isinstance(raw_source, dict):
            raise RuntimeError("whale source catalog entries must be objects")
        source = dict(raw_source)
        source_id = _nonempty_text(source, "id")
        if not SOURCE_ID_RE.fullmatch(source_id):
            raise RuntimeError(f"unsafe whale source id: {source_id!r}")
        if source_id in seen_ids:
            raise RuntimeError(f"duplicate whale source id: {source_id}")
        seen_ids.add(source_id)

        file_value = _nonempty_text(source, "file")
        if file_value in seen_files:
            raise RuntimeError(f"duplicate whale source file: {file_value}")
        seen_files.add(file_value)
        path = _source_path(source_root, file_value)

        category = _nonempty_text(source, "category")
        if category not in CLIP_SECONDS:
            raise RuntimeError(f"unknown source category: {category}")
        license_id = _nonempty_text(source, "license")
        if license_id not in ALLOWED_LICENSES:
            raise RuntimeError(f"unsupported whale source license: {license_id}")
        for field in ("license_url", "title", "attribution", "source_page", "changes"):
            value = _nonempty_text(source, field)
            if field in {"license_url", "source_page"} and not value.startswith(
                "https://"
            ):
                raise RuntimeError(f"whale source {field} must use https")
        creators = source.get("creators")
        if (
            not isinstance(creators, list)
            or not creators
            or not all(isinstance(item, str) and item.strip() for item in creators)
        ):
            raise RuntimeError("whale source creators must be a non-empty string list")
        expected_sha256 = _nonempty_text(source, "expected_sha256")
        if not SHA256_RE.fullmatch(expected_sha256):
            raise RuntimeError(f"invalid expected SHA-256 for {source_id}")
        expected_bytes = source.get("expected_bytes")
        if (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes <= 0
        ):
            raise RuntimeError(f"invalid expected byte size for {source_id}")
        clip_count = source.get("clip_count")
        if (
            not isinstance(clip_count, int)
            or isinstance(clip_count, bool)
            or not 1 <= clip_count <= 8
        ):
            raise RuntimeError(f"invalid clip count for {source_id}")
        actual_bytes = path.stat().st_size
        if actual_bytes != expected_bytes:
            raise RuntimeError(
                f"whale source byte-size mismatch: {file_value}: {actual_bytes} != {expected_bytes}"
            )
        actual_sha256 = sha256_file(path)
        if actual_sha256 != expected_sha256:
            raise RuntimeError(f"whale source SHA-256 mismatch: {file_value}")
        source["_path"] = path
        validated.append(source)

    if {source["category"] for source in validated} != set(CLIP_SECONDS):
        raise RuntimeError(
            "whale source catalog must cover low, song and high categories"
        )

    raw_root = source_root / "raw"
    _reject_symlink_chain(raw_root, source_root)
    raw_entries = list(raw_root.iterdir())
    unsafe_entries = [
        path.name for path in raw_entries if path.is_symlink() or not path.is_file()
    ]
    if unsafe_entries:
        raise RuntimeError(f"unsafe raw whale source entries: {sorted(unsafe_entries)}")
    actual_files = {path.relative_to(source_root).as_posix() for path in raw_entries}
    if actual_files != seen_files:
        missing = sorted(seen_files - actual_files)
        extra = sorted(actual_files - seen_files)
        raise RuntimeError(
            f"raw whale source set mismatch; missing={missing}; extra={extra}"
        )
    return source_root, validated


def _resolved_path_key(path: pathlib.Path) -> pathlib.Path:
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"cannot resolve whale-bank path safely: {path}") from error
    return pathlib.Path(os.path.normcase(os.fspath(resolved)))


def _paths_overlap(first: pathlib.Path, second: pathlib.Path) -> bool:
    first_key = _resolved_path_key(first)
    second_key = _resolved_path_key(second)
    if (
        first_key == second_key
        or first_key in second_key.parents
        or second_key in first_key.parents
    ):
        return True
    if not first.exists() or not second.exists():
        return False
    try:
        return os.path.samefile(first, second)
    except OSError as error:
        raise RuntimeError(
            f"cannot verify whale-bank path identity: {first}"
        ) from error


def _reject_protected_output_aliases(
    output_root: pathlib.Path, protected_paths: tuple[pathlib.Path, ...]
) -> None:
    paths_to_check = [output_root]
    while paths_to_check:
        candidate = paths_to_check.pop()
        if any(_paths_overlap(candidate, protected) for protected in protected_paths):
            raise RuntimeError(
                "whale bank output overlaps a protected catalog or raw source input"
            )
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            with os.scandir(candidate) as entries:
                for entry in entries:
                    path = pathlib.Path(entry.path)
                    paths_to_check.append(path)
        except OSError as error:
            raise RuntimeError(
                f"cannot inspect whale bank output safely: {candidate}"
            ) from error


def validate_output_root(
    source_root: pathlib.Path,
    output_root: pathlib.Path,
    protected_paths: tuple[pathlib.Path, ...] = (),
) -> pathlib.Path:
    candidate = output_root.expanduser()
    if not candidate.is_absolute():
        candidate = pathlib.Path.cwd() / candidate
    try:
        parent = candidate.parent.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("whale bank output parent must already exist") from error
    source_root = source_root.resolve(strict=True)
    if parent != source_root:
        raise RuntimeError(
            "whale bank output must be a direct child of the source root"
        )
    if not SOURCE_ID_RE.fullmatch(candidate.name):
        raise RuntimeError("unsafe whale bank output directory name")
    candidate = parent / candidate.name
    if candidate.is_symlink():
        raise RuntimeError("whale bank output must not be a symlink")
    if candidate.exists() and not candidate.is_dir():
        raise RuntimeError("whale bank output must be a directory")
    protected_paths = (
        (source_root / "raw").resolve(strict=True),
        *(path.resolve(strict=True) for path in protected_paths),
    )
    _reject_protected_output_aliases(candidate, protected_paths)
    return candidate


def snapshot_verified_source(
    source: dict[str, Any], destination: pathlib.Path
) -> pathlib.Path:
    source_path = pathlib.Path(source["_path"])
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(
            f"refusing to overwrite staged source snapshot: {destination}"
        )
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("O_NOFOLLOW is required for verified whale source snapshots")
    flags = os.O_RDONLY | os.O_CLOEXEC | no_follow
    descriptor = os.open(source_path, flags)
    digest = hashlib.sha256()
    total = 0
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"whale source snapshot is not regular: {source_path}")
        with os.fdopen(descriptor, "rb") as source_handle:
            descriptor = -1
            with destination.open("xb") as destination_handle:
                while True:
                    chunk = source_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    destination_handle.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
    except BaseException:
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    expected_bytes = source["expected_bytes"]
    expected_sha256 = source["expected_sha256"]
    if total != expected_bytes:
        raise RuntimeError(
            f"whale source snapshot byte-size mismatch: {source['file']}: "
            f"{total} != {expected_bytes}"
        )
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError(f"whale source snapshot SHA-256 mismatch: {source['file']}")
    return destination


def run_ffmpeg(source: pathlib.Path, output: pathlib.Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to build the whale sample bank")
    if output.exists() or output.is_symlink():
        raise RuntimeError(f"refusing to overwrite staged audio: {output}")
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-sample_fmt",
            "s16",
            "-af",
            "highpass=f=22,lowpass=f=7000,acompressor=threshold=-28dB:ratio=2:attack=20:release=250",
            str(output),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffmpeg failed for {source}")


def read_wav(path: pathlib.Path) -> array:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise RuntimeError(f"unexpected intermediate WAV format: {path}")
        if handle.getframerate() != SAMPLE_RATE:
            raise RuntimeError(f"unexpected intermediate sample rate: {path}")
        frames = handle.readframes(handle.getnframes())
    samples = array("h")
    samples.frombytes(frames)
    if struct.pack("=H", 1) != struct.pack("<H", 1):
        samples.byteswap()
    return samples


def candidate_centers(samples: array, count: int, clip_frames: int) -> list[int]:
    window = max(1, round(WINDOW_SECONDS * SAMPLE_RATE))
    hop = max(1, round(HOP_SECONDS * SAMPLE_RATE))
    half_clip = clip_frames // 2
    scored: list[tuple[int, int]] = []
    for start in range(0, max(1, len(samples) - window), hop):
        center = start + window // 2
        if center < half_clip or center + half_clip >= len(samples):
            continue
        segment = samples[start : start + window]
        rms = math.isqrt(sum(sample * sample for sample in segment) // len(segment))
        scored.append((rms, center))
    if not scored:
        return [len(samples) // 2]
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected: list[int] = []
    minimum_distance = round(MIN_PEAK_DISTANCE_SECONDS * SAMPLE_RATE)
    for rms, center in scored:
        if rms <= 0:
            continue
        if all(abs(center - previous) >= minimum_distance for previous in selected):
            selected.append(center)
        if len(selected) == count:
            break
    if len(selected) < count:
        evenly = [
            round((index + 1) * len(samples) / (count + 1)) for index in range(count)
        ]
        for center in evenly:
            center = min(max(center, half_clip), len(samples) - half_clip - 1)
            if all(abs(center - previous) >= window for previous in selected):
                selected.append(center)
            if len(selected) == count:
                break
    if len(selected) != count:
        raise RuntimeError("could not select the requested number of whale clips")
    return sorted(selected)


def normalize_and_fade(samples: array) -> array:
    if not samples:
        raise RuntimeError("cannot normalize an empty clip")
    mean = sum(samples) / len(samples)
    corrected = array(
        "h", (int(max(-32768, min(32767, sample - mean))) for sample in samples)
    )
    peak = max(abs(sample) for sample in corrected) or 1
    scale = min(8.0, TARGET_PEAK / peak)
    fade_frames = min(round(0.04 * SAMPLE_RATE), len(corrected) // 4)
    result = array("h")
    for index, sample in enumerate(corrected):
        gain = 1.0
        if index < fade_frames:
            gain *= index / max(1, fade_frames)
        tail = len(corrected) - 1 - index
        if tail < fade_frames:
            gain *= tail / max(1, fade_frames)
        value = round(sample * scale * gain)
        result.append(max(-32768, min(32767, value)))
    return result


def write_wav(path: pathlib.Path, samples: array) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"refusing to overwrite staged WAV: {path}")
    payload = array("h", samples)
    if struct.pack("=H", 1) != struct.pack("<H", 1):
        payload.byteswap()
    with path.open("xb") as raw_handle:
        with wave.open(raw_handle, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(payload.tobytes())


def _source_manifest_record(source: dict[str, Any]) -> dict[str, Any]:
    record = {
        key: value
        for key, value in source.items()
        if key not in {"_path", "clip_count", "expected_sha256", "expected_bytes"}
    }
    record["sha256"] = source["expected_sha256"]
    record["bytes"] = source["expected_bytes"]
    return record


def _renameat2(
    directory_fd: int, first_name: str, second_name: str, flags: int
) -> None:
    _renameat2_between(
        directory_fd,
        first_name,
        directory_fd,
        second_name,
        flags,
    )


def _renameat2_between(
    first_directory_fd: int,
    first_name: str,
    second_directory_fd: int,
    second_name: str,
    flags: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("descriptor-bound renameat2 is unavailable on this host")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        first_directory_fd,
        os.fsencode(first_name),
        second_directory_fd,
        os.fsencode(second_name),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _rename_exchange(directory_fd: int, first_name: str, second_name: str) -> None:
    _renameat2(directory_fd, first_name, second_name, 2)


def _rename_noreplace(directory_fd: int, first_name: str, second_name: str) -> None:
    _renameat2(directory_fd, first_name, second_name, 1)


def _rename_exchange_between(
    first_directory_fd: int,
    first_name: str,
    second_directory_fd: int,
    second_name: str,
) -> None:
    _renameat2_between(
        first_directory_fd,
        first_name,
        second_directory_fd,
        second_name,
        2,
    )


def _require_secure_replace_platform() -> None:
    if (
        getattr(os, "O_PATH", None) is None
        or getattr(os, "O_NOFOLLOW", None) is None
        or getattr(os, "O_DIRECTORY", None) is None
        or getattr(ctypes.CDLL(None), "renameat2", None) is None
    ):
        raise RuntimeError(
            "descriptor-bound exchange and cleanup are unavailable on this host"
        )


def _verify_tree_excludes_identities(
    directory_fd: int,
    protected_identities: frozenset[FileIdentity],
    label: str,
) -> None:
    for name in os.listdir(directory_fd):
        identity = _entry_identity(directory_fd, name)
        if identity is None:
            raise RuntimeError(f"whale bank tree changed while inspecting {label}")
        if identity in protected_identities:
            raise RuntimeError(
                f"whale bank {label} contains a protected input identity"
            )
        if identity[2] != stat.S_IFDIR:
            continue
        child_fd = _open_nofollow(directory_fd, name, directory=True)
        try:
            _require_identity(
                _file_identity(os.fstat(child_fd)),
                identity,
                f"{label}/{name}",
            )
            _verify_tree_excludes_identities(
                child_fd, protected_identities, f"{label}/{name}"
            )
        finally:
            os.close(child_fd)


@dataclass
class _ReplaceState:
    staging: pathlib.Path
    output_root: pathlib.Path
    staging_name: str
    output_name: str
    staging_fd: int
    staging_identity: FileIdentity
    output_fd: int | None
    output_identity: FileIdentity | None

    def close(self) -> None:
        if self.output_fd is not None:
            os.close(self.output_fd)
        os.close(self.staging_fd)


@dataclass
class _BoundDirectory:
    parent_fd: int
    parent_identity: FileIdentity
    name: str
    descriptor: int
    identity: FileIdentity
    label: str

    def verify(self) -> None:
        _require_identity(
            _file_identity(os.fstat(self.parent_fd)),
            self.parent_identity,
            f"{self.label} parent descriptor",
        )
        _require_identity(
            _file_identity(os.fstat(self.descriptor)),
            self.identity,
            f"{self.label} descriptor",
        )
        _require_identity(
            _entry_identity(self.parent_fd, self.name),
            self.identity,
            self.label,
        )

    def close(self) -> None:
        os.close(self.descriptor)


def _create_bound_directory(
    parent_fd: int,
    parent_identity: FileIdentity,
    name: str,
    label: str,
) -> _BoundDirectory:
    _require_identity(
        _file_identity(os.fstat(parent_fd)),
        parent_identity,
        f"{label} parent descriptor",
    )
    if _entry_identity(parent_fd, name) is not None:
        raise RuntimeError(f"refusing to reuse existing {label}")
    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    identity = _entry_identity(parent_fd, name)
    if identity is None or identity[2] != stat.S_IFDIR:
        raise _RecoveryRequiredError(
            f"{label} identity could not be established after creation; "
            "no recursive deletion will be attempted"
        )
    descriptor = -1
    try:
        descriptor = _open_nofollow(parent_fd, name, directory=True)
        bound = _BoundDirectory(
            parent_fd=parent_fd,
            parent_identity=parent_identity,
            name=name,
            descriptor=descriptor,
            identity=identity,
            label=label,
        )
        bound.verify()
        return bound
    except BaseException as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise _RecoveryRequiredError(
            f"{label} identity could not be bound after creation; "
            f"no recursive deletion will be attempted; reason={error}"
        ) from error


def _capture_replace_state(
    staging: pathlib.Path,
    output_root: pathlib.Path,
    bindings: _ProtectedBindings,
    bound_staging_fd: int,
    bound_staging_identity: FileIdentity,
) -> _ReplaceState:
    if (
        staging.parent != bindings.source_root
        or output_root.parent != bindings.source_root
    ):
        raise RuntimeError(
            "replace paths are not direct children of the bound source root"
        )
    staging_fd = os.dup(bound_staging_fd)
    try:
        output_fd = (
            os.dup(bindings.output_fd) if bindings.output_fd is not None else None
        )
        state = _ReplaceState(
            staging=staging,
            output_root=output_root,
            staging_name=staging.name,
            output_name=output_root.name,
            staging_fd=staging_fd,
            staging_identity=bound_staging_identity,
            output_fd=output_fd,
            output_identity=bindings.output_identity,
        )
        _verify_tree_excludes_identities(
            staging_fd, bindings.protected_identities, "staging"
        )
        if output_fd is not None:
            _verify_tree_excludes_identities(
                output_fd, bindings.protected_identities, "existing output"
            )
        return state
    except BaseException:
        if "output_fd" in locals() and output_fd is not None:
            os.close(output_fd)
        os.close(staging_fd)
        raise


def _verify_original_layout(bindings: _ProtectedBindings, state: _ReplaceState) -> None:
    bindings.verify()
    _require_identity(
        _file_identity(os.fstat(state.staging_fd)),
        state.staging_identity,
        "staging descriptor",
    )
    _require_identity(
        _entry_identity(bindings.source_root_fd, state.staging_name),
        state.staging_identity,
        "staging",
    )
    actual_output = _entry_identity(bindings.source_root_fd, state.output_name)
    if actual_output != state.output_identity:
        raise RuntimeError(
            "whale bank output identity changed: "
            f"expected={state.output_identity!r}; actual={actual_output!r}"
        )
    if state.output_fd is not None and state.output_identity is not None:
        _require_identity(
            _file_identity(os.fstat(state.output_fd)),
            state.output_identity,
            "existing output descriptor",
        )
    _verify_tree_excludes_identities(
        state.staging_fd, bindings.protected_identities, "staging"
    )
    if state.output_fd is not None:
        _verify_tree_excludes_identities(
            state.output_fd, bindings.protected_identities, "existing output"
        )


def _verify_installed_layout(
    bindings: _ProtectedBindings, state: _ReplaceState
) -> None:
    bindings.verify()
    _require_identity(
        _file_identity(os.fstat(state.staging_fd)),
        state.staging_identity,
        "new output descriptor",
    )
    _require_identity(
        _entry_identity(bindings.source_root_fd, state.output_name),
        state.staging_identity,
        "new output",
    )
    backup_identity = _entry_identity(bindings.source_root_fd, state.staging_name)
    if backup_identity != state.output_identity:
        raise RuntimeError(
            "whale bank backup identity changed: "
            f"expected={state.output_identity!r}; actual={backup_identity!r}"
        )
    if backup_identity in bindings.protected_identities:
        raise RuntimeError("whale bank backup is a protected input identity")
    if state.output_fd is not None and state.output_identity is not None:
        _require_identity(
            _file_identity(os.fstat(state.output_fd)),
            state.output_identity,
            "output backup descriptor",
        )
    _verify_tree_excludes_identities(
        state.staging_fd, bindings.protected_identities, "new output"
    )
    if state.output_fd is not None:
        _verify_tree_excludes_identities(
            state.output_fd, bindings.protected_identities, "output backup"
        )


def _directory_layout(
    bindings: _ProtectedBindings, state: _ReplaceState
) -> dict[str, FileIdentity | None]:
    return {
        "raw": _entry_identity(bindings.source_root_fd, "raw"),
        state.output_name: _entry_identity(bindings.source_root_fd, state.output_name),
        state.staging_name: _entry_identity(
            bindings.source_root_fd, state.staging_name
        ),
    }


def _restore_directory_layout(
    bindings: _ProtectedBindings, state: _ReplaceState
) -> None:
    expected = {
        "raw": bindings.raw_identity,
        state.output_name: state.output_identity,
        state.staging_name: state.staging_identity,
    }
    current = _directory_layout(bindings, state)
    if Counter(current.values()) != Counter(expected.values()):
        raise RuntimeError(
            f"cannot safely map changed whale bank directories: {current!r}"
        )

    for target_name in ("raw", state.staging_name, state.output_name):
        wanted = expected[target_name]
        current = _directory_layout(bindings, state)
        if current[target_name] == wanted:
            continue
        source_name = next(
            (name for name, identity in current.items() if identity == wanted),
            None,
        )
        if source_name is None:
            raise RuntimeError(
                f"cannot locate expected whale bank identity for {target_name}"
            )
        if wanted is None:
            raise RuntimeError("cannot exchange a missing directory identity")
        if current[target_name] is None:
            _rename_noreplace(bindings.source_root_fd, source_name, target_name)
        else:
            _rename_exchange(bindings.source_root_fd, target_name, source_name)

    _verify_original_layout(bindings, state)


def _before_quarantine_rename(
    _directory_fd: int,
    _name: str,
    _quarantine_name: str,
    _expected: FileIdentity,
    _label: str,
) -> None:
    """Test seam immediately before the namespace entry is quarantined."""


def _quarantine_verified_entry(
    bindings: _ProtectedBindings,
    directory_fd: int,
    name: str,
    expected: FileIdentity,
    label: str,
) -> None:
    if expected in bindings.protected_identities:
        raise RuntimeError(f"refusing to quarantine protected identity from {label}")
    path_flag = getattr(os, "O_PATH", None)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if path_flag is None or no_follow is None:
        raise RuntimeError(
            "descriptor-bound quarantine cleanup is unavailable on this host"
        )

    quarantine_name = ""
    for _attempt in range(16):
        bindings.verify()
        _require_identity(
            _entry_identity(directory_fd, name),
            expected,
            label,
        )
        candidate = f".whale-cleanup-quarantine-{secrets.token_hex(16)}"
        if _entry_identity(directory_fd, candidate) is not None:
            continue
        _before_quarantine_rename(
            directory_fd,
            name,
            candidate,
            expected,
            label,
        )
        try:
            _rename_noreplace(directory_fd, name, candidate)
        except FileExistsError:
            continue
        except BaseException as error:
            raise _RecoveryRequiredError(
                f"could not quarantine {label}; nothing was deleted; "
                f"reason={error}"
            ) from error
        quarantine_name = candidate
        break
    if not quarantine_name:
        raise _RecoveryRequiredError(
            f"could not reserve a unique quarantine name for {label}; "
            "nothing was deleted"
        )

    quarantine_fd = -1
    try:
        flags = path_flag | os.O_CLOEXEC | no_follow
        if expected[2] == stat.S_IFDIR:
            quarantine_fd = _open_nofollow(
                directory_fd, quarantine_name, directory=True
            )
        else:
            quarantine_fd = os.open(
                quarantine_name,
                flags,
                dir_fd=directory_fd,
            )
        actual_descriptor = _file_identity(os.fstat(quarantine_fd))
        actual_entry = _entry_identity(directory_fd, quarantine_name)
        if actual_descriptor != expected or actual_entry != expected:
            raise _RecoveryRequiredError(
                f"quarantined {label} identity is ambiguous; expected={expected!r}; "
                f"descriptor={actual_descriptor!r}; entry={actual_entry!r}; "
                "nothing was deleted"
            )
        bindings.verify()
        if actual_descriptor in bindings.protected_identities:
            raise _RecoveryRequiredError(
                f"quarantined {label} is a protected identity; nothing was deleted"
            )
        _require_identity(
            _entry_identity(directory_fd, quarantine_name),
            actual_descriptor,
            f"quarantined {label}",
        )
        if expected[2] == stat.S_IFDIR:
            os.rmdir(quarantine_name, dir_fd=directory_fd)
        else:
            os.unlink(quarantine_name, dir_fd=directory_fd)
        if _entry_identity(directory_fd, quarantine_name) is not None:
            raise _RecoveryRequiredError(
                f"quarantined {label} still exists after cleanup"
            )
        _require_identity(
            _file_identity(os.fstat(quarantine_fd)),
            actual_descriptor,
            f"removed {label} descriptor",
        )
    except _RecoveryRequiredError:
        raise
    except BaseException as error:
        raise _RecoveryRequiredError(
            f"quarantined {label} could not be removed safely; "
            f"preserved_name={quarantine_name!r}; reason={error}"
        ) from error
    finally:
        if quarantine_fd >= 0:
            os.close(quarantine_fd)


def _remove_verified_directory(
    bindings: _ProtectedBindings,
    state: _ReplaceState,
    *,
    installed: bool,
) -> None:
    if installed:
        _verify_installed_layout(bindings, state)
        expected = state.output_identity
        descriptor = state.output_fd
        label = "output backup"
    else:
        _verify_original_layout(bindings, state)
        expected = state.staging_identity
        descriptor = state.staging_fd
        label = "staging"
    if expected is None or descriptor is None:
        return
    if expected in bindings.protected_identities:
        raise RuntimeError(f"refusing to recursively remove protected {label}")
    _require_identity(
        _file_identity(os.fstat(descriptor)),
        expected,
        f"{label} descriptor",
    )
    _require_identity(
        _entry_identity(bindings.source_root_fd, state.staging_name),
        expected,
        label,
    )
    _verify_tree_excludes_identities(descriptor, bindings.protected_identities, label)
    _clear_bound_directory(bindings, descriptor, label)
    _quarantine_verified_entry(
        bindings,
        bindings.source_root_fd,
        state.staging_name,
        expected,
        label,
    )


def _clear_bound_directory(
    bindings: _ProtectedBindings, directory_fd: int, label: str
) -> None:
    path_flag = getattr(os, "O_PATH", None)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if path_flag is None or no_follow is None:
        raise RuntimeError(
            "descriptor-bound recursive cleanup is unavailable on this host"
        )
    for name in os.listdir(directory_fd):
        bindings.verify()
        identity = _entry_identity(directory_fd, name)
        if identity is None:
            raise RuntimeError(f"whale bank {label} changed during cleanup")
        if identity in bindings.protected_identities:
            raise RuntimeError(f"refusing to remove protected identity from {label}")
        if identity[2] == stat.S_IFDIR:
            child_fd = _open_nofollow(directory_fd, name, directory=True)
            try:
                _require_identity(
                    _file_identity(os.fstat(child_fd)),
                    identity,
                    f"{label}/{name}",
                )
                _clear_bound_directory(bindings, child_fd, f"{label}/{name}")
                _quarantine_verified_entry(
                    bindings,
                    directory_fd,
                    name,
                    identity,
                    f"{label}/{name}",
                )
            finally:
                os.close(child_fd)
            continue

        identity_fd = os.open(
            name,
            path_flag | os.O_CLOEXEC | no_follow,
            dir_fd=directory_fd,
        )
        try:
            _require_identity(
                _file_identity(os.fstat(identity_fd)),
                identity,
                f"{label}/{name}",
            )
            _quarantine_verified_entry(
                bindings,
                directory_fd,
                name,
                identity,
                f"{label}/{name}",
            )
        finally:
            os.close(identity_fd)


def _remove_bound_directory(
    bindings: _ProtectedBindings,
    bound: _BoundDirectory,
) -> None:
    bindings.verify()
    bound.verify()
    if bound.identity in bindings.protected_identities:
        raise RuntimeError(f"refusing to recursively remove protected {bound.label}")
    _verify_tree_excludes_identities(
        bound.descriptor,
        bindings.protected_identities,
        bound.label,
    )
    _clear_bound_directory(bindings, bound.descriptor, bound.label)
    bound.verify()
    _quarantine_verified_entry(
        bindings,
        bound.parent_fd,
        bound.name,
        bound.identity,
        bound.label,
    )


def _remove_failed_staging(
    bindings: _ProtectedBindings,
    staging: pathlib.Path,
    staging_fd: int,
    staging_identity: FileIdentity,
) -> None:
    bindings.verify()
    _require_identity(
        _file_identity(os.fstat(staging_fd)),
        staging_identity,
        "failed staging descriptor",
    )
    actual = _entry_identity(bindings.source_root_fd, staging.name)
    if actual is None:
        return
    _require_identity(actual, staging_identity, "failed staging")
    if staging_identity in bindings.protected_identities:
        raise RuntimeError("refusing to clean a protected failed staging identity")
    _verify_tree_excludes_identities(
        staging_fd, bindings.protected_identities, "failed staging"
    )
    _clear_bound_directory(bindings, staging_fd, "failed staging")
    _quarantine_verified_entry(
        bindings,
        bindings.source_root_fd,
        staging.name,
        staging_identity,
        "failed staging",
    )


def _before_temporary_cleanup(
    _bindings: _ProtectedBindings,
    _bound: _BoundDirectory,
) -> None:
    """Test seam immediately before a bound temporary directory is removed."""


def _after_exchange_before_cleanup(
    _bindings: _ProtectedBindings, _state: _ReplaceState
) -> None:
    """Test seam for deterministic namespace-race coverage."""


def _manual_recovery_error(
    bindings: _ProtectedBindings,
    state: _ReplaceState | None,
    reason: BaseException,
    rollback_error: BaseException | None = None,
) -> _RecoveryRequiredError:
    paths = [bindings.source_root / "raw"]
    if state is not None:
        paths.extend((state.output_root, state.staging))
    detail = f"; rollback_error={rollback_error}" if rollback_error else ""
    return _RecoveryRequiredError(
        "whale bank replace outcome is unknown; no further recursive deletion "
        f"will be attempted; manual recovery required; preserved_paths={paths!r}; "
        f"reason={reason}{detail}"
    )


def _atomic_replace_directory(
    staging: pathlib.Path,
    output_root: pathlib.Path,
    bindings: _ProtectedBindings,
    staging_fd: int,
    staging_identity: FileIdentity,
) -> None:
    state: _ReplaceState | None = None
    try:
        state = _capture_replace_state(
            staging,
            output_root,
            bindings,
            staging_fd,
            staging_identity,
        )
        try:
            _require_secure_replace_platform()
            # This is the replace operation's own bound precondition,
            # immediately adjacent to the descriptor-relative rename.
            _verify_original_layout(bindings, state)
            if state.output_identity is None:
                _rename_noreplace(
                    bindings.source_root_fd, state.staging_name, state.output_name
                )
            else:
                _rename_exchange(
                    bindings.source_root_fd, state.staging_name, state.output_name
                )
        except BaseException as exchange_error:
            try:
                _restore_directory_layout(bindings, state)
                _remove_verified_directory(bindings, state, installed=False)
            except BaseException as rollback_error:
                raise _manual_recovery_error(
                    bindings, state, exchange_error, rollback_error
                ) from exchange_error
            raise RuntimeError(
                f"{exchange_error}; original directory layout restored"
            ) from exchange_error

        try:
            _after_exchange_before_cleanup(bindings, state)
            _verify_installed_layout(bindings, state)
        except BaseException as validation_error:
            try:
                _restore_directory_layout(bindings, state)
                _remove_verified_directory(bindings, state, installed=False)
            except BaseException as rollback_error:
                raise _manual_recovery_error(
                    bindings, state, validation_error, rollback_error
                ) from validation_error
            raise RuntimeError(
                f"{validation_error}; original directory layout restored"
            ) from validation_error

        try:
            _remove_verified_directory(bindings, state, installed=True)
        except BaseException as cleanup_error:
            raise _manual_recovery_error(
                bindings, state, cleanup_error
            ) from cleanup_error
    except _RecoveryRequiredError:
        raise
    except BaseException as error:
        if state is None:
            raise _manual_recovery_error(bindings, state, error) from error
        raise
    finally:
        if state is not None:
            state.close()


def build(source_catalog: pathlib.Path, output_root: pathlib.Path) -> dict[str, Any]:
    source_root, sources = load_catalog(source_catalog)
    protected_paths = (
        source_catalog.resolve(strict=True),
        *(pathlib.Path(source["_path"]) for source in sources),
    )
    output_root = validate_output_root(source_root, output_root, protected_paths)
    bindings = _bind_protected_inputs(source_root, source_catalog, sources, output_root)
    staging_fd = -1
    intermediate_binding: _BoundDirectory | None = None
    source_snapshot_binding: _BoundDirectory | None = None
    try:
        staging = pathlib.Path(
            tempfile.mkdtemp(prefix=f".{output_root.name}-staging-", dir=source_root)
        )
        try:
            staging_fd = _open_nofollow(
                bindings.source_root_fd, staging.name, directory=True
            )
            staging_identity = _file_identity(os.fstat(staging_fd))
            bindings.verify()
            _require_identity(
                _entry_identity(bindings.source_root_fd, staging.name),
                staging_identity,
                "created staging",
            )
        except BaseException as staging_error:
            raise _RecoveryRequiredError(
                "whale bank staging identity could not be bound; no recursive "
                "deletion will be attempted; manual recovery required; "
                f"preserved_path={staging!r}; reason={staging_error}"
            ) from staging_error
        try:
            intermediate_binding = _create_bound_directory(
                staging_fd,
                staging_identity,
                ".intermediate",
                "intermediate workspace",
            )
            source_snapshot_binding = _create_bound_directory(
                staging_fd,
                staging_identity,
                ".sources",
                "source snapshot workspace",
            )
            intermediate_root = staging / intermediate_binding.name
            source_snapshot_root = staging / source_snapshot_binding.name
            clips: list[dict[str, Any]] = []
            source_records: list[dict[str, Any]] = []
            for source in sources:
                source_id = source["id"]
                category = source["category"]
                source_path = pathlib.Path(source["_path"])
                snapshot_path = (
                    source_snapshot_root / f"{source_id}{source_path.suffix.lower()}"
                )
                snapshot_verified_source(source, snapshot_path)
                intermediate = intermediate_root / f"{source_id}.wav"
                run_ffmpeg(snapshot_path, intermediate)
                samples = read_wav(intermediate)
                clip_frames = round(CLIP_SECONDS[category] * SAMPLE_RATE)
                count = source["clip_count"]
                centers = candidate_centers(samples, count, clip_frames)
                source_records.append(_source_manifest_record(source))
                for clip_index, center in enumerate(centers, start=1):
                    start = max(0, center - clip_frames // 2)
                    end = min(len(samples), start + clip_frames)
                    start = max(0, end - clip_frames)
                    clip_samples = normalize_and_fade(samples[start:end])
                    clip_id = f"{source_id}-{clip_index:02d}"
                    filename = f"{clip_id}.wav"
                    destination = staging / filename
                    write_wav(destination, clip_samples)
                    frame_count = len(clip_samples)
                    clips.append(
                        {
                            "id": clip_id,
                            "source_id": source_id,
                            "category": category,
                            "file": filename,
                            "frames": frame_count,
                            "sha256": sha256_file(destination),
                            "loop_start_frame": round(frame_count * 0.22),
                            "loop_end_frame": round(frame_count * 0.78),
                            "loop_crossfade_frames": min(
                                round(0.12 * SAMPLE_RATE),
                                round(frame_count * 0.08),
                            ),
                        }
                    )
            _before_temporary_cleanup(bindings, intermediate_binding)
            _remove_bound_directory(bindings, intermediate_binding)
            _before_temporary_cleanup(bindings, source_snapshot_binding)
            _remove_bound_directory(bindings, source_snapshot_binding)

            slots: list[dict[str, Any]] = []
            for category in ("low", "song", "high"):
                category_clips = [
                    clip for clip in clips if clip["category"] == category
                ]
                lower, upper = ROOT_RANGES[category]
                roots = list(range(lower, upper + 1, 4))
                if roots[-1] != upper:
                    roots.append(upper)
                for index, root_note in enumerate(roots):
                    clip_index = (
                        0
                        if len(roots) == 1
                        else round(index * (len(category_clips) - 1) / (len(roots) - 1))
                    )
                    clip = category_clips[clip_index]
                    slots.append(
                        {
                            "clip_id": clip["id"],
                            "root_note": root_note,
                            "minimum_note": max(21, root_note - 3),
                            "maximum_note": min(108, root_note + 3),
                        }
                    )
            manifest = {
                "schema_version": 2,
                "kind": "humpback_whale_sample_bank",
                "sample_rate_hz": SAMPLE_RATE,
                "channels": 1,
                "sample_width_bytes": 2,
                "source_catalog_schema_version": CATALOG_SCHEMA_VERSION,
                "source_catalog_sha256": _sha256_descriptor(bindings.catalog_fd),
                "sources": source_records,
                "clips": clips,
                "slots": sorted(
                    slots, key=lambda slot: (slot["root_note"], slot["clip_id"])
                ),
                "render_contract": {
                    "maximum_total_pitch_shift_semitones": 4,
                    "pitch_bend_included": True,
                    "monophonic": True,
                    "looping": "equal-power-crossfade",
                    "default_voice_mode": "realistic",
                },
            }
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            expected_files = {clip["file"] for clip in clips} | {"manifest.json"}
            actual_files = {path.name for path in staging.iterdir() if path.is_file()}
            if actual_files != expected_files:
                raise RuntimeError("staged whale bank file set does not match manifest")
            validate_output_root(source_root, output_root, protected_paths)
            _atomic_replace_directory(
                staging,
                output_root,
                bindings,
                staging_fd,
                staging_identity,
            )
            return manifest
        except _RecoveryRequiredError:
            raise
        except BaseException as build_error:
            try:
                _remove_failed_staging(bindings, staging, staging_fd, staging_identity)
            except BaseException as cleanup_error:
                raise _RecoveryRequiredError(
                    "whale bank failed staging outcome is unknown; no further "
                    "recursive deletion will be attempted; manual recovery "
                    f"required; preserved_path={staging!r}; "
                    f"reason={build_error}; cleanup_error={cleanup_error}"
                ) from build_error
            raise
    finally:
        if source_snapshot_binding is not None:
            source_snapshot_binding.close()
        if intermediate_binding is not None:
            intermediate_binding.close()
        if staging_fd >= 0:
            os.close(staging_fd)
        bindings.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=pathlib.Path, default=SOURCE_CATALOG)
    parser.add_argument("--output", type=pathlib.Path, default=PROCESSED_ROOT)
    try:
        args = parser.parse_args(argv)
        manifest = build(args.catalog, args.output)
        print(
            json.dumps(
                {
                    "state": "built",
                    "manifest": str(
                        validate_output_root(
                            args.catalog.resolve().parent,
                            args.output,
                            (args.catalog,),
                        )
                        / "manifest.json"
                    ),
                    "source_count": len(manifest["sources"]),
                    "clip_count": len(manifest["clips"]),
                    "slot_count": len(manifest["slots"]),
                },
                sort_keys=True,
            )
        )
        return 0
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as error:
        print(
            json.dumps({"state": "blocked", "error": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
