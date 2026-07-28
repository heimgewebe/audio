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
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import wave
from array import array
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


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        destination.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    expected_bytes = source["expected_bytes"]
    expected_sha256 = source["expected_sha256"]
    if total != expected_bytes:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"whale source snapshot byte-size mismatch: {source['file']}: "
            f"{total} != {expected_bytes}"
        )
    if digest.hexdigest() != expected_sha256:
        destination.unlink(missing_ok=True)
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


def _rename_exchange(first: pathlib.Path, second: pathlib.Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("atomic directory exchange is unavailable on this host")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_exchange = 2
    result = renameat2(
        at_fdcwd,
        os.fsencode(first),
        at_fdcwd,
        os.fsencode(second),
        rename_exchange,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _atomic_replace_directory(staging: pathlib.Path, output_root: pathlib.Path) -> None:
    if output_root.exists():
        _rename_exchange(staging, output_root)
        # After the exchange, staging names the previous complete bank.
        shutil.rmtree(staging)
        return
    os.replace(staging, output_root)


def build(source_catalog: pathlib.Path, output_root: pathlib.Path) -> dict[str, Any]:
    source_root, sources = load_catalog(source_catalog)
    protected_paths = (
        source_catalog.resolve(strict=True),
        *(pathlib.Path(source["_path"]) for source in sources),
    )
    output_root = validate_output_root(source_root, output_root, protected_paths)
    staging = pathlib.Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}-staging-", dir=source_root)
    )
    intermediate_root = staging / ".intermediate"
    intermediate_root.mkdir(mode=0o700)
    source_snapshot_root = staging / ".sources"
    source_snapshot_root.mkdir(mode=0o700)
    try:
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
                            round(0.12 * SAMPLE_RATE), round(frame_count * 0.08)
                        ),
                    }
                )
        shutil.rmtree(intermediate_root)
        shutil.rmtree(source_snapshot_root)

        slots: list[dict[str, Any]] = []
        for category in ("low", "song", "high"):
            category_clips = [clip for clip in clips if clip["category"] == category]
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
            "source_catalog_sha256": sha256_file(source_catalog.resolve(strict=True)),
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
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        expected_files = {clip["file"] for clip in clips} | {"manifest.json"}
        actual_files = {path.name for path in staging.iterdir() if path.is_file()}
        if actual_files != expected_files:
            raise RuntimeError("staged whale bank file set does not match manifest")
        validate_output_root(source_root, output_root, protected_paths)
        _atomic_replace_directory(staging, output_root)
        return manifest
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


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
