#!/usr/bin/env python3
"""Safe, immutable file ingest for Zoom H2essential recording sessions.

The H2 is treated as a removable source medium, never as an audio-routing
profile. A recording session is the H2 scene directory and may contain FRONT,
REAR and MIX 32-bit-float WAV masters. Import copies every observed master
byte-identically, verifies source and destination SHA-256, and publishes an
immutable manifest plus separate mutable annotations.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import struct
import sys
import tempfile
from typing import Any, BinaryIO, Iterator

SCHEMA_VERSION = 1
SOURCE_SENTINEL = "ZOOM_H2essential.SYS"
SOURCE_ORIGINATOR = "ZOOM H2essential"
SCENE_RE = re.compile(r"^[0-9]{6}_[0-9]{6}$")
MATERIAL_ID_RE = re.compile(r"^[0-9a-f]{24}$")
ROLE_RE = re.compile(
    r"^(?P<scene>[0-9]{6}_[0-9]{6})_(?P<role>FRONT|REAR|MIX)"
    r"(?:_(?P<segment>[0-9]{3}))?\.WAV$"
)
ROLE_ORDER = {"FRONT": 0, "REAR": 1, "MIX": 2}
ROLE_TRACK = {"FRONT": "1", "REAR": "3", "MIX": "5"}
ALLOWED_SAMPLE_RATES = frozenset({44_100, 48_000, 96_000})
COPY_CHUNK_BYTES = 1024 * 1024
MAX_BEXT_BYTES = 128 * 1024
MAX_SESSION_FILES = 192
DEFAULT_SOURCE_ROOT = pathlib.Path(
    os.environ.get(
        "AUDIO_H2_SOURCE_ROOT",
        pathlib.Path("/media") / os.environ.get("USER", "user") / "ZOOM_H2E",
    )
)
PRIMARY_LIBRARY_ROOT = pathlib.Path.home() / "Music" / "Audio-Aufnahmen" / "H2-Material"
LEGACY_LIBRARY_ROOT = pathlib.Path.home() / "Music" / "Audio-Material" / "H2"


def _library_root_has_material(root: pathlib.Path) -> bool:
    if not root.is_dir():
        return False
    try:
        with os.scandir(root) as entries:
            return any(
                entry.is_dir(follow_symlinks=False)
                and re.fullmatch(r"[0-9a-f]{24}", entry.name) is not None
                for entry in entries
            )
    except OSError:
        return True


def _select_library_root(
    primary: pathlib.Path,
    legacy: pathlib.Path,
) -> pathlib.Path:
    primary = primary.expanduser()
    legacy = legacy.expanduser()
    primary_present = primary.exists() or primary.is_symlink()
    legacy_present = legacy.exists() or legacy.is_symlink()
    primary_has_material = (
        _library_root_has_material(primary) if primary_present else False
    )
    legacy_has_material = (
        _library_root_has_material(legacy) if legacy_present else False
    )
    if primary_has_material and legacy_has_material:
        raise RuntimeError(
            "H2-Bibliothek besitzt Material in Primär- und Legacy-Root; "
            "automatische Rootwahl ist verboten."
        )
    if legacy_has_material and not primary_has_material:
        return legacy
    return primary


def _default_library_root(
    material_root_override: str | None,
    primary: pathlib.Path = PRIMARY_LIBRARY_ROOT,
    legacy: pathlib.Path = LEGACY_LIBRARY_ROOT,
) -> pathlib.Path:
    if material_root_override:
        # Preserve the preceding release contract: AUDIO_MATERIAL_ROOT names
        # the material parent and H2 remains its child.
        return pathlib.Path(material_root_override).expanduser() / "H2"
    return _select_library_root(primary, legacy)


DEFAULT_LIBRARY_ROOT = _default_library_root(os.environ.get("AUDIO_MATERIAL_ROOT"))


class H2IngestError(RuntimeError):
    """Expected fail-closed H2 ingest error."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _material_id_for_master_set(master_set_sha256: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", master_set_sha256) is None:
        raise H2IngestError("Master-Set-Hash besitzt kein gültiges SHA-256-Format.")
    return hashlib.sha256(
        b"zoom-h2essential-import-v1\0" + bytes.fromhex(master_set_sha256)
    ).hexdigest()[:24]


def _sha256_path(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _lstat_regular(path: pathlib.Path, description: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise H2IngestError(f"{description} ist nicht lesbar.") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise H2IngestError(f"{description} muss eine normale Datei ohne Symlink sein.")
    return metadata


def _lstat_directory(path: pathlib.Path, description: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise H2IngestError(f"{description} ist nicht lesbar.") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise H2IngestError(f"{description} muss ein Verzeichnis ohne Symlink sein.")
    return metadata


def _resolve_source_root(path: pathlib.Path) -> pathlib.Path:
    source = path.expanduser()
    if not source.is_absolute():
        raise H2IngestError("H2-Quellpfad muss absolut sein.")
    _lstat_directory(source, "H2-Quellpfad")
    sentinel = source / SOURCE_SENTINEL
    _lstat_regular(sentinel, "H2-Kennung")
    return source


def _resolve_library_root(path: pathlib.Path, source_root: pathlib.Path) -> pathlib.Path:
    library = path.expanduser()
    if not library.is_absolute():
        raise H2IngestError("Materialbibliothek muss absolut sein.")
    source_real = source_root.resolve(strict=True)
    library_real_parent = library.parent.resolve(strict=False)
    try:
        common = pathlib.Path(os.path.commonpath([source_real, library_real_parent]))
    except ValueError as exc:
        raise H2IngestError("Quell- und Zielpfad sind nicht vergleichbar.") from exc
    if common == source_real:
        raise H2IngestError("Materialbibliothek darf niemals auf der H2-Karte liegen.")
    if library.exists() or library.is_symlink():
        metadata = _lstat_directory(library, "Materialbibliothek")
        if metadata.st_uid != os.getuid():
            raise H2IngestError("Materialbibliothek gehört nicht dem aktuellen Benutzer.")
    else:
        library.mkdir(parents=True, mode=0o700, exist_ok=False)
    try:
        os.chmod(library, 0o700)
    except OSError as exc:
        raise H2IngestError("Materialbibliothek kann nicht privat gesetzt werden.") from exc
    return library


def _decode_ascii(value: bytes) -> str:
    return value.split(b"\0", 1)[0].decode("ascii", "replace").strip()


def _parse_kv_lines(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in value.replace("\r", "").split("\n"):
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        if key:
            result[key] = raw
    return result


def _parse_coding_history(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        key = key.strip()
        if key:
            result[key] = raw.strip()
    return result


def _iter_wave_chunks(handle: BinaryIO, file_size: int) -> Iterator[tuple[str, int, int]]:
    header = handle.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise H2IngestError("H2-Master ist kein unterstütztes RIFF/WAVE.")
    cursor = 12
    while cursor + 8 <= file_size:
        handle.seek(cursor)
        chunk_header = handle.read(8)
        if len(chunk_header) != 8:
            raise H2IngestError("WAV-Chunkheader ist abgeschnitten.")
        raw_id, raw_size = chunk_header[:4], chunk_header[4:]
        size = struct.unpack("<I", raw_size)[0]
        data_offset = cursor + 8
        end = data_offset + size
        if end > file_size:
            raise H2IngestError("WAV-Chunk überschreitet die Dateigrenze.")
        yield raw_id.decode("latin1"), size, data_offset
        cursor = end + (size & 1)
    if cursor not in {file_size, file_size + 1}:
        raise H2IngestError("WAV-Struktur endet nicht an einer gültigen Chunkgrenze.")


def _inspect_wav_handle(
    handle: BinaryIO,
    file_size: int,
    file_name: str,
    expected_scene: str,
    expected_role: str,
) -> dict[str, Any]:
    fmt: dict[str, int] | None = None
    bext: dict[str, Any] | None = None
    data_bytes: int | None = None
    chunk_ids: list[str] = []
    for chunk_id, size, offset in _iter_wave_chunks(handle, file_size):
        chunk_ids.append(chunk_id)
        if chunk_id == "fmt ":
            if size < 16:
                raise H2IngestError("WAV-fmt-Chunk ist zu kurz.")
            handle.seek(offset)
            payload = handle.read(16)
            audio_format, channels, rate, byte_rate, block_align, bits = struct.unpack(
                "<HHIIHH", payload
            )
            fmt = {
                "audio_format": audio_format,
                "channels": channels,
                "sample_rate_hz": rate,
                "byte_rate": byte_rate,
                "block_align": block_align,
                "bits_per_sample": bits,
            }
        elif chunk_id == "bext":
            if size < 602 or size > MAX_BEXT_BYTES:
                raise H2IngestError("BWF-bext-Chunk liegt außerhalb des erlaubten Bereichs.")
            handle.seek(offset)
            payload = handle.read(size)
            description = _decode_ascii(payload[:256])
            originator = _decode_ascii(payload[256:288])
            originator_reference = _decode_ascii(payload[288:320])
            recorded_date = payload[320:330].decode("ascii", "replace").strip("\0 ")
            recorded_time = payload[330:338].decode("ascii", "replace").strip("\0 ")
            time_reference = struct.unpack("<Q", payload[338:346])[0]
            version = struct.unpack("<H", payload[346:348])[0]
            coding_history = _decode_ascii(payload[602:])
            bext = {
                "description": description,
                "description_fields": _parse_kv_lines(description),
                "originator": originator,
                "originator_reference": originator_reference,
                "recorded_date": recorded_date,
                "recorded_time": recorded_time,
                "time_reference_samples": time_reference,
                "version": version,
                "coding_history": coding_history,
                "coding_fields": _parse_coding_history(coding_history),
            }
        elif chunk_id == "data":
            data_bytes = size

    if fmt is None or bext is None or data_bytes is None:
        raise H2IngestError("H2-WAV benötigt fmt-, bext- und data-Chunks.")
    if (
        fmt["audio_format"] != 3
        or fmt["channels"] != 2
        or fmt["sample_rate_hz"] not in ALLOWED_SAMPLE_RATES
        or fmt["bits_per_sample"] != 32
        or fmt["block_align"] != 8
        or fmt["byte_rate"] != fmt["sample_rate_hz"] * fmt["block_align"]
    ):
        raise H2IngestError("H2-WAV entspricht nicht dem erwarteten 32-Bit-Float-Stereoformat.")
    if bext["originator"] != SOURCE_ORIGINATOR:
        raise H2IngestError("BWF-Originator ist nicht der erwartete Zoom H2essential.")
    fields = bext["description_fields"]
    if fields.get("zSCENE") != expected_scene:
        raise H2IngestError("BWF-Szene stimmt nicht mit dem H2-Sessionordner überein.")
    coding = bext["coding_fields"]
    track = coding.get("TRK")
    expected_track = ROLE_TRACK[expected_role]
    if track is not None and track != expected_track:
        raise H2IngestError("BWF-Spurrolle stimmt nicht mit dem Dateinamen überein.")
    if data_bytes <= 0 or data_bytes % fmt["block_align"] != 0:
        raise H2IngestError("H2-WAV besitzt keine vollständigen Audioframes.")
    frames = data_bytes // fmt["block_align"]
    return {
        "name": file_name,
        "role": expected_role.lower(),
        "bytes": file_size,
        "audio": {
            "codec": "pcm_f32le",
            "sample_rate_hz": fmt["sample_rate_hz"],
            "channels": fmt["channels"],
            "bits_per_sample": fmt["bits_per_sample"],
            "frames": frames,
            "duration_seconds": round(frames / fmt["sample_rate_hz"], 9),
        },
        "bwf": bext,
        "chunk_ids": chunk_ids,
        "marker_chunks_observed": [
            item for item in chunk_ids if item in {"cue ", "LIST", "iXML", "axml"}
        ],
    }


def _open_source_generation(path: pathlib.Path) -> tuple[int, os.stat_result]:
    lexical = _lstat_regular(path, "H2-WAV")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise H2IngestError("H2-WAV kann nicht generationstreu geöffnet werden.") from exc
    opened = os.fstat(fd)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_dev != lexical.st_dev
        or opened.st_ino != lexical.st_ino
        or opened.st_size != lexical.st_size
        or opened.st_mtime_ns != lexical.st_mtime_ns
    ):
        os.close(fd)
        raise H2IngestError("H2-WAV änderte seine Identität beim Öffnen.")
    return fd, opened


def _inspect_source_generation(
    path: pathlib.Path,
    expected_scene: str,
    expected_role: str,
    *,
    hash_content: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    fd, opened = _open_source_generation(path)
    digest = hashlib.sha256() if hash_content else None
    hashed = 0
    try:
        with os.fdopen(fd, "rb", closefd=True) as handle:
            inspected = _inspect_wav_handle(
                handle,
                opened.st_size,
                path.name,
                expected_scene,
                expected_role,
            )
            if digest is not None:
                handle.seek(0)
                while True:
                    chunk = handle.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    hashed += len(chunk)
            finished = os.fstat(handle.fileno())
            if (
                finished.st_dev != opened.st_dev
                or finished.st_ino != opened.st_ino
                or finished.st_size != opened.st_size
                or finished.st_mtime_ns != opened.st_mtime_ns
            ):
                raise H2IngestError("H2-WAV änderte sich während Prüfung oder Vorhash.")
    except Exception:
        raise
    if digest is None:
        return inspected, None
    if hashed != opened.st_size:
        raise H2IngestError("H2-WAV wurde beim Vorhash nicht vollständig gelesen.")
    return inspected, {
        "sha256": digest.hexdigest(),
        "bytes": hashed,
        "st_dev": opened.st_dev,
        "st_ino": opened.st_ino,
        "st_mtime_ns": opened.st_mtime_ns,
    }


def inspect_wav(path: pathlib.Path, expected_scene: str, expected_role: str) -> dict[str, Any]:
    inspected, _receipt = _inspect_source_generation(
        path,
        expected_scene,
        expected_role,
        hash_content=False,
    )
    return inspected


def _inspect_and_hash_source_master(
    path: pathlib.Path,
    expected_scene: str,
    expected_role: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    inspected, receipt = _inspect_source_generation(
        path,
        expected_scene,
        expected_role,
        hash_content=True,
    )
    if receipt is None:
        raise H2IngestError("H2-Vorhash lieferte keinen Generationbeleg.")
    return inspected, receipt


def inspect_scene(source_root: pathlib.Path, scene: str) -> dict[str, Any]:
    if not SCENE_RE.fullmatch(scene):
        raise H2IngestError("Ungültige H2-Szene.")
    session_dir = source_root / scene
    _lstat_directory(session_dir, "H2-Session")
    files: list[dict[str, Any]] = []
    seen_segments: set[tuple[str, int]] = set()
    with os.scandir(session_dir) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if not entry.is_file(follow_symlinks=False):
                raise H2IngestError("H2-Session enthält einen unerwarteten Nicht-Datei-Eintrag.")
            match = ROLE_RE.fullmatch(entry.name)
            if match is None or match.group("scene") != scene:
                raise H2IngestError("H2-Session enthält einen unerwarteten Dateinamen.")
            role = match.group("role")
            raw_segment = match.group("segment")
            if raw_segment == "000":
                raise H2IngestError("H2-Folgesegment besitzt eine ungültige Segmentnummer.")
            segment_index = int(raw_segment or "0")
            segment_key = (role, segment_index)
            if segment_key in seen_segments:
                raise H2IngestError("H2-Session enthält ein doppeltes Spursegment.")
            seen_segments.add(segment_key)
            item = inspect_wav(pathlib.Path(entry.path), scene, role)
            item["segment_index"] = segment_index
            files.append(item)
            if len(files) > MAX_SESSION_FILES:
                raise H2IngestError("H2-Session überschreitet das Dateilimit.")
    if not files:
        raise H2IngestError("H2-Session enthält keine WAV-Master.")

    files.sort(
        key=lambda item: (
            ROLE_ORDER[item["role"].upper()],
            item["segment_index"],
        )
    )
    rates = {item["audio"]["sample_rate_hz"] for item in files}
    if len(rates) != 1:
        raise H2IngestError("H2-Session wechselt unerwartet die Sample-Rate.")

    roles = sorted(
        {item["role"] for item in files},
        key=lambda role: ROLE_ORDER[role.upper()],
    )
    role_segments: dict[str, tuple[int, ...]] = {}
    role_frames: dict[str, int] = {}
    for role in roles:
        role_items = [item for item in files if item["role"] == role]
        indexes = tuple(item["segment_index"] for item in role_items)
        if indexes != tuple(range(len(indexes))):
            raise H2IngestError("H2-Session besitzt keine lückenlose Segmentfolge.")
        role_segments[role] = indexes
        role_frames[role] = sum(item["audio"]["frames"] for item in role_items)

    if len(set(role_segments.values())) != 1:
        raise H2IngestError("H2-Session ist zwischen ihren Spursegmenten nicht konsistent.")

    segment_indexes = next(iter(role_segments.values()))
    for segment_index in segment_indexes:
        segment_items = [
            item for item in files if item["segment_index"] == segment_index
        ]
        dates = {item["bwf"]["recorded_date"] for item in segment_items}
        times = {item["bwf"]["recorded_time"] for item in segment_items}
        takes = {
            item["bwf"]["description_fields"].get("zTAKE") for item in segment_items
        }
        frames = {item["audio"]["frames"] for item in segment_items}
        if (
            len(dates) != 1
            or len(times) != 1
            or len(takes) != 1
            or len(frames) != 1
        ):
            raise H2IngestError(
                "H2-Segment ist zwischen seinen Spurrollen nicht konsistent."
            )

    sample_rate = next(iter(rates))
    reference_role = roles[0]
    reference_items = [item for item in files if item["role"] == reference_role]
    total_frames = sum(item["audio"]["frames"] for item in reference_items)
    segment_count = len(segment_indexes)
    base_segment = next(
        item
        for item in reference_items
        if item["segment_index"] == 0
    )
    return {
        "scene": scene,
        "take": base_segment["bwf"]["description_fields"].get("zTAKE"),
        "recorded_date": base_segment["bwf"]["recorded_date"],
        "recorded_time": base_segment["bwf"]["recorded_time"],
        "sample_rate_hz": sample_rate,
        "duration_seconds": round(total_frames / sample_rate, 9),
        "roles": roles,
        "segment_count": segment_count,
        "files": files,
        "marker_chunks_observed": sorted(
            {chunk for item in files for chunk in item["marker_chunks_observed"]}
        ),
    }


def scan(source_root: pathlib.Path = DEFAULT_SOURCE_ROOT) -> dict[str, Any]:
    source = _resolve_source_root(source_root)
    sessions: list[dict[str, Any]] = []
    skipped: list[str] = []
    with os.scandir(source) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not SCENE_RE.fullmatch(entry.name):
                continue
            try:
                sessions.append(inspect_scene(source, entry.name))
            except H2IngestError:
                skipped.append(entry.name)
    sessions.sort(key=lambda item: (item["recorded_date"], item["recorded_time"], item["scene"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_h2_source_scan",
        "device": {
            "model": SOURCE_ORIGINATOR,
            "transport": "file-transfer",
            "volume_hint": source.name,
        },
        "sessions": sessions,
        "count": len(sessions),
        "skipped_invalid_sessions": skipped,
        "read_only": True,
        "source_mutated": False,
    }


def _assert_source_receipt_current(source: pathlib.Path, receipt: dict[str, Any]) -> None:
    current = _lstat_regular(source, "H2-Master")
    if (
        current.st_dev != receipt["st_dev"]
        or current.st_ino != receipt["st_ino"]
        or current.st_size != receipt["bytes"]
        or current.st_mtime_ns != receipt["st_mtime_ns"]
    ):
        raise H2IngestError("H2-Master änderte sich nach dem Vorhash.")


def _copy_master(
    source: pathlib.Path,
    destination: pathlib.Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
) -> dict[str, Any]:
    source_meta = _lstat_regular(source, "H2-Master")
    digest = hashlib.sha256()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        target_fd = os.open(destination, flags, 0o440)
    except OSError as exc:
        raise H2IngestError("Ziel-Master kann nicht exklusiv angelegt werden.") from exc
    copied = 0
    try:
        with source.open("rb") as src, os.fdopen(target_fd, "wb", closefd=True) as dst:
            opened = os.fstat(src.fileno())
            if (
                opened.st_dev != source_meta.st_dev
                or opened.st_ino != source_meta.st_ino
                or opened.st_size != source_meta.st_size
            ):
                raise H2IngestError("H2-Master änderte seine Identität vor dem Kopieren.")
            while True:
                chunk = src.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                dst.write(chunk)
                copied += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
            finished = os.fstat(src.fileno())
            if (
                finished.st_dev != opened.st_dev
                or finished.st_ino != opened.st_ino
                or finished.st_size != opened.st_size
                or finished.st_mtime_ns != opened.st_mtime_ns
                or copied != opened.st_size
            ):
                raise H2IngestError("H2-Master änderte sich während des Imports.")
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    source_sha = digest.hexdigest()
    if copied != expected_bytes or source_sha != expected_sha256:
        try:
            destination.unlink()
        except OSError:
            pass
        raise H2IngestError("H2-Master änderte sich zwischen Vorhash und Kopieren.")
    destination_sha = _sha256_path(destination)
    if destination_sha != source_sha:
        try:
            destination.unlink()
        except OSError:
            pass
        raise H2IngestError("Kopierter Master stimmt nicht bytegenau mit der H2-Quelle überein.")
    os.chmod(destination, 0o440)
    return {"sha256": source_sha, "bytes": copied}


def _write_json_new(path: pathlib.Path, value: dict[str, Any], mode: int) -> None:
    payload = _canonical_bytes(value) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, mode)
    except OSError as exc:
        raise H2IngestError("Metadatendatei kann nicht exklusiv angelegt werden.") from exc
    with os.fdopen(fd, "wb", closefd=True) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, mode)


def _read_json_regular(path: pathlib.Path) -> dict[str, Any]:
    _lstat_regular(path, "Metadatendatei")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise H2IngestError("Metadatendatei ist nicht sicher lesbar.") from exc
    if not isinstance(value, dict):
        raise H2IngestError("Metadatendatei besitzt kein Objektformat.")
    return value


def _library_item(
    manifest: dict[str, Any],
    annotations: dict[str, Any],
    material_id: str,
) -> dict[str, Any]:
    source = manifest.get("source")
    imported_at = manifest.get("imported_at")
    master_set_sha256 = manifest.get("master_set_sha256")
    masters = manifest.get("masters")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("kind") != "audio_imported_material_manifest"
        or manifest.get("material_id") != material_id
        or not isinstance(source, dict)
        or source.get("kind") != "zoom-h2essential-file-transfer"
        or not isinstance(source.get("recorder_model"), str)
        or not isinstance(source.get("scene"), str)
        or not (source.get("take") is None or isinstance(source.get("take"), str))
        or not isinstance(source.get("recorded_date"), str)
        or not isinstance(source.get("recorded_time"), str)
        or not isinstance(imported_at, str)
        or not isinstance(master_set_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", master_set_sha256) is None
        or not isinstance(masters, list)
        or not masters
    ):
        raise H2IngestError("Bibliotheksobjekt ist strukturell ungültig.")

    if (
        annotations.get("schema_version") != SCHEMA_VERSION
        or annotations.get("kind") != "audio_material_annotations"
        or annotations.get("material_id") != material_id
        or not isinstance(annotations.get("title"), str)
        or not isinstance(annotations.get("note"), str)
        or not isinstance(annotations.get("tags"), list)
        or not all(isinstance(item, str) for item in annotations["tags"])
        or not isinstance(annotations.get("markers"), list)
        or not (
            annotations.get("updated_at") is None
            or isinstance(annotations.get("updated_at"), str)
        )
    ):
        raise H2IngestError("Materialannotation ist strukturell ungültig.")

    projected_masters: list[dict[str, Any]] = []
    for item in masters:
        if not isinstance(item, dict):
            raise H2IngestError("Bibliotheksobjekt ist strukturell ungültig.")
        name = item.get("name")
        role = item.get("role")
        size = item.get("bytes")
        audio = item.get("audio")
        segment_index = item.get("segment_index", 0)
        if (
            not isinstance(name, str)
            or pathlib.Path(name).name != name
            or role not in {"front", "rear", "mix"}
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(audio, dict)
            or not isinstance(segment_index, int)
            or isinstance(segment_index, bool)
            or segment_index < 0
        ):
            raise H2IngestError("Bibliotheksobjekt ist strukturell ungültig.")
        projected_masters.append(
            {
                "name": name,
                "role": role,
                "bytes": size,
                "audio": audio,
                "segment_index": segment_index,
            }
        )

    return {
        "material_id": material_id,
        "source": source,
        "imported_at": imported_at,
        "master_set_sha256": master_set_sha256,
        "masters": projected_masters,
        "annotations": annotations,
        "current_bytes_verified": False,
    }


def import_scene(
    scene: str,
    *,
    source_root: pathlib.Path = DEFAULT_SOURCE_ROOT,
    library_root: pathlib.Path = DEFAULT_LIBRARY_ROOT,
) -> dict[str, Any]:
    source = _resolve_source_root(source_root)
    session = inspect_scene(source, scene)
    library = _resolve_library_root(library_root, source)

    source_receipts: dict[str, dict[str, Any]] = {}
    master_identity: list[dict[str, Any]] = []
    for item in session["files"]:
        source_path = source / scene / item["name"]
        inspected, receipt = _inspect_and_hash_source_master(
            source_path,
            scene,
            item["role"].upper(),
        )
        inspected["segment_index"] = item["segment_index"]
        if inspected != item:
            raise H2IngestError(
                "H2-Master änderte sich zwischen Sessionprüfung und Vorhash."
            )
        source_receipts[item["name"]] = receipt
        master_identity.append(
            {
                "name": item["name"],
                "role": item["role"],
                "sha256": receipt["sha256"],
                "bytes": receipt["bytes"],
            }
        )
    for item in session["files"]:
        _assert_source_receipt_current(
            source / scene / item["name"],
            source_receipts[item["name"]],
        )

    master_set_sha256 = hashlib.sha256(_canonical_bytes(master_identity)).hexdigest()
    material_id = _material_id_for_master_set(master_set_sha256)
    final_dir = library / material_id
    if final_dir.exists() or final_dir.is_symlink():
        _lstat_directory(final_dir, "Vorhandenes Materialobjekt")
        existing = _read_json_regular(final_dir / "manifest.json")
        if (
            existing.get("material_id") == material_id
            and existing.get("master_set_sha256") == master_set_sha256
        ):
            verification = verify_material(material_id, library_root=library)
            if verification["master_set_sha256"] != master_set_sha256:
                raise H2IngestError(
                    "Vorhandener Import stimmt nicht mit dem aktuellen H2-Masterset überein."
                )
            return {
                "schema_version": SCHEMA_VERSION,
                "kind": "audio_h2_import_result",
                "status": "already-imported",
                "material_id": material_id,
                "master_set_sha256": master_set_sha256,
                "master_count": len(master_identity),
                "verified_current": True,
                "source_mutated": False,
            }
        raise H2IngestError("Material-ID kollidiert mit einem anderen Bibliotheksobjekt.")

    if inspect_scene(source, scene) != session:
        raise H2IngestError("H2-Session änderte sich zwischen Prüfung und Import.")

    staging = pathlib.Path(tempfile.mkdtemp(prefix=".h2-staging-", dir=library))
    os.chmod(staging, 0o700)
    try:
        master_dir = staging / "master"
        master_dir.mkdir(mode=0o700)
        manifest_files: list[dict[str, Any]] = []
        for item in session["files"]:
            source_path = source / scene / item["name"]
            destination = master_dir / item["name"]
            expected = source_receipts[item["name"]]
            receipt = _copy_master(
                source_path,
                destination,
                expected_sha256=expected["sha256"],
                expected_bytes=expected["bytes"],
            )
            manifest_files.append(
                {
                    "name": item["name"],
                    "role": item["role"],
                    "segment_index": item["segment_index"],
                    "sha256": receipt["sha256"],
                    "bytes": receipt["bytes"],
                    "audio": item["audio"],
                    "bwf": item["bwf"],
                    "chunk_ids": item["chunk_ids"],
                    "marker_chunks_observed": item["marker_chunks_observed"],
                }
            )

        imported_at = dt.datetime.now(dt.timezone.utc).isoformat()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": "audio_imported_material_manifest",
            "material_id": material_id,
            "source": {
                "kind": "zoom-h2essential-file-transfer",
                "recorder_model": SOURCE_ORIGINATOR,
                "scene": session["scene"],
                "take": session["take"],
                "recorded_date": session["recorded_date"],
                "recorded_time": session["recorded_time"],
                "segment_count": session["segment_count"],
            },
            "imported_at": imported_at,
            "master_set_sha256": master_set_sha256,
            "masters": manifest_files,
            "integrity": {
                "source_mutation": "forbidden",
                "master_mutation": "forbidden",
                "copy_verification": "prehash-source-and-destination-sha256",
            },
        }
        annotations = {
            "schema_version": SCHEMA_VERSION,
            "kind": "audio_material_annotations",
            "material_id": material_id,
            "title": "",
            "note": "",
            "tags": [],
            "markers": [],
            "updated_at": None,
        }
        _write_json_new(staging / "manifest.json", manifest, 0o440)
        _write_json_new(staging / "annotations.json", annotations, 0o600)
        os.chmod(master_dir, 0o550)
        os.rename(staging, final_dir)
        parent_fd = os.open(
            library,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "audio_h2_import_result",
            "status": "imported",
            "material_id": material_id,
            "master_set_sha256": master_set_sha256,
            "master_count": len(manifest_files),
            "recorded_date": session["recorded_date"],
            "recorded_time": session["recorded_time"],
            "roles": session["roles"],
            "segment_count": session["segment_count"],
            "source_mutated": False,
        }
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
def library(library_root: pathlib.Path = DEFAULT_LIBRARY_ROOT) -> dict[str, Any]:
    root = library_root.expanduser()
    if not root.exists() and not root.is_symlink():
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "audio_material_library",
            "items": [],
            "count": 0,
            "read_only": True,
        }
    _lstat_directory(root, "Materialbibliothek")
    items: list[dict[str, Any]] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or not MATERIAL_ID_RE.fullmatch(entry.name):
                continue
            directory = pathlib.Path(entry.path)
            manifest = _read_json_regular(directory / "manifest.json")
            annotations = _read_json_regular(directory / "annotations.json")
            items.append(_library_item(manifest, annotations, entry.name))
    items.sort(key=lambda item: item["imported_at"], reverse=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_material_library",
        "items": items,
        "count": len(items),
        "read_only": True,
    }


def verify_material(
    material_id: str, *, library_root: pathlib.Path = DEFAULT_LIBRARY_ROOT
) -> dict[str, Any]:
    if not MATERIAL_ID_RE.fullmatch(material_id):
        raise H2IngestError("Ungültige Material-ID.")
    directory = library_root.expanduser() / material_id
    _lstat_directory(directory, "Materialobjekt")
    manifest = _read_json_regular(directory / "manifest.json")
    if manifest.get("material_id") != material_id:
        raise H2IngestError("Materialmanifest gehört nicht zur angeforderten Material-ID.")
    masters = manifest.get("masters")
    if not isinstance(masters, list) or not masters:
        raise H2IngestError("Materialmanifest enthält keine gültigen Master.")
    verified: list[dict[str, Any]] = []
    identity: list[dict[str, Any]] = []
    for item in masters:
        if not isinstance(item, dict):
            raise H2IngestError("Materialmanifest enthält einen ungültigen Master.")
        name = item.get("name")
        if not isinstance(name, str) or pathlib.Path(name).name != name:
            raise H2IngestError("Materialmanifest enthält einen ungültigen Dateinamen.")
        path = directory / "master" / name
        metadata = _lstat_regular(path, "Archivierter H2-Master")
        digest = _sha256_path(path)
        if metadata.st_size != item.get("bytes") or digest != item.get("sha256"):
            raise H2IngestError("Archivierter H2-Master weicht vom Importbeleg ab.")
        row = {
            "name": name,
            "role": item.get("role"),
            "sha256": digest,
            "bytes": metadata.st_size,
        }
        verified.append(row)
        identity.append(row)
    expected_set = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
    if expected_set != manifest.get("master_set_sha256"):
        raise H2IngestError("Master-Set stimmt nicht mit dem Importmanifest überein.")
    if _material_id_for_master_set(expected_set) != material_id:
        raise H2IngestError("Material-ID stimmt nicht mit dem verifizierten Master-Set überein.")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_material_verification",
        "material_id": material_id,
        "master_set_sha256": expected_set,
        "masters": verified,
        "verified_current": True,
    }



MAX_TITLE_CHARS = 160
MAX_NOTE_CHARS = 2000
MAX_TAGS = 16
MAX_TAG_CHARS = 48
_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _validated_annotation_text(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise H2IngestError(f"{label} muss Text sein.")
    normalized = value.strip()
    if len(normalized) > maximum or _TEXT_CONTROL_RE.search(normalized):
        raise H2IngestError(f"{label} ist zu lang oder enthält Steuerzeichen.")
    return normalized


def _validated_tags(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_TAGS:
        raise H2IngestError("Tags sind ungültig.")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        tag = _validated_annotation_text(raw, label="Tag", maximum=MAX_TAG_CHARS)
        if not tag:
            continue
        folded = tag.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(tag)
    return result


def _write_json_replace(path: pathlib.Path, value: dict[str, Any], mode: int) -> None:
    directory = path.parent
    _lstat_directory(directory, "Metadatenverzeichnis")
    _lstat_regular(path, "Metadatendatei")
    payload = _canonical_bytes(value) + b"\n"
    fd, temporary_name = tempfile.mkstemp(prefix=".metadata-", dir=directory)
    temporary = pathlib.Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def annotate_material(
    material_id: str,
    *,
    title: Any,
    note: Any,
    tags: Any,
    library_root: pathlib.Path = DEFAULT_LIBRARY_ROOT,
) -> dict[str, Any]:
    if not MATERIAL_ID_RE.fullmatch(material_id):
        raise H2IngestError("Ungültige Material-ID.")
    directory = library_root.expanduser() / material_id
    _lstat_directory(directory, "Materialobjekt")
    manifest = _read_json_regular(directory / "manifest.json")
    annotations_path = directory / "annotations.json"
    current = _read_json_regular(annotations_path)
    _library_item(manifest, current, material_id)
    updated = dict(current)
    updated["title"] = _validated_annotation_text(
        title, label="Titel", maximum=MAX_TITLE_CHARS
    )
    updated["note"] = _validated_annotation_text(
        note, label="Notiz", maximum=MAX_NOTE_CHARS
    )
    updated["tags"] = _validated_tags(tags)
    changed = (
        updated["title"] != current["title"]
        or updated["note"] != current["note"]
        or updated["tags"] != current["tags"]
    )
    if changed:
        updated["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        _write_json_replace(annotations_path, updated, 0o600)
        observed = _read_json_regular(annotations_path)
        _library_item(manifest, observed, material_id)
        if observed != updated:
            raise H2IngestError("Materialannotation wurde nicht exakt zurückgelesen.")
    else:
        observed = current
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_material_annotation_result",
        "material_id": material_id,
        "changed": changed,
        "annotations": observed,
    }


def _preferred_session_files(session: dict[str, Any]) -> list[dict[str, Any]]:
    files = session["files"]
    for role in ("mix", "front", "rear"):
        selected = [item for item in files if item["role"] == role]
        if selected:
            return sorted(selected, key=lambda item: item["segment_index"])
    raise H2IngestError("H2-Session besitzt keine abspielbare Spur.")


def source_media(
    scene: str,
    segment_index: int,
    *,
    source_root: pathlib.Path = DEFAULT_SOURCE_ROOT,
) -> dict[str, Any]:
    if isinstance(segment_index, bool) or not isinstance(segment_index, int) or segment_index < 0:
        raise H2IngestError("Ungültiger H2-Vorschausegmentindex.")
    source = _resolve_source_root(source_root)
    session = inspect_scene(source, scene)
    selected = _preferred_session_files(session)
    if segment_index >= len(selected):
        raise H2IngestError("H2-Vorschausegment existiert nicht.")
    item = selected[segment_index]
    path = source / scene / item["name"]
    inspected, receipt = _inspect_and_hash_source_master(
        path, scene, item["role"].upper()
    )
    inspected["segment_index"] = item["segment_index"]
    if inspected != item:
        raise H2IngestError("H2-Vorschau änderte sich zwischen Prüfung und Medienbindung.")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_h2_source_media_binding",
        "scene": scene,
        "role": item["role"],
        "segment_index": segment_index,
        "segment_count": len(selected),
        "path": str(path),
        "sha256": receipt["sha256"],
        "bytes": receipt["bytes"],
        "device": receipt["st_dev"],
        "inode": receipt["st_ino"],
        "mtime_ns": receipt["st_mtime_ns"],
        "duration_seconds": item["audio"]["duration_seconds"],
        "verified_current": True,
    }


def material_media(
    material_id: str,
    segment_index: int,
    *,
    library_root: pathlib.Path = DEFAULT_LIBRARY_ROOT,
) -> dict[str, Any]:
    if not MATERIAL_ID_RE.fullmatch(material_id):
        raise H2IngestError("Ungültige Material-ID.")
    if isinstance(segment_index, bool) or not isinstance(segment_index, int) or segment_index < 0:
        raise H2IngestError("Ungültiger Materialsegmentindex.")
    directory = library_root.expanduser() / material_id
    _lstat_directory(directory, "Materialobjekt")
    manifest = _read_json_regular(directory / "manifest.json")
    annotations = _read_json_regular(directory / "annotations.json")
    item = _library_item(manifest, annotations, material_id)
    verification = verify_material(material_id, library_root=library_root)
    masters = item["masters"]
    for role in ("mix", "front", "rear"):
        selected = sorted(
            [entry for entry in masters if entry["role"] == role],
            key=lambda entry: entry["segment_index"],
        )
        if selected:
            break
    else:
        raise H2IngestError("Materialobjekt besitzt keine abspielbare Spur.")
    if segment_index >= len(selected):
        raise H2IngestError("Materialsegment existiert nicht.")
    selected_item = selected[segment_index]
    verified_by_name = {entry["name"]: entry for entry in verification["masters"]}
    verified = verified_by_name.get(selected_item["name"])
    if verified is None:
        raise H2IngestError("Materialsegment ist nicht im Integritätsbeleg enthalten.")
    path = directory / "master" / selected_item["name"]
    metadata = _lstat_regular(path, "Archivierter H2-Master")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_h2_material_media_binding",
        "material_id": material_id,
        "role": selected_item["role"],
        "segment_index": segment_index,
        "segment_count": len(selected),
        "path": str(path),
        "sha256": verified["sha256"],
        "bytes": verified["bytes"],
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mtime_ns": metadata.st_mtime_ns,
        "duration_seconds": selected_item["audio"].get("duration_seconds"),
        "verified_current": True,
    }

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("--source-root", type=pathlib.Path, default=DEFAULT_SOURCE_ROOT)

    import_parser = sub.add_parser("import")
    import_parser.add_argument("scene")
    import_parser.add_argument("--source-root", type=pathlib.Path, default=DEFAULT_SOURCE_ROOT)
    import_parser.add_argument("--library-root", type=pathlib.Path, default=DEFAULT_LIBRARY_ROOT)

    library_parser = sub.add_parser("library")
    library_parser.add_argument("--library-root", type=pathlib.Path, default=DEFAULT_LIBRARY_ROOT)

    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("material_id")
    verify_parser.add_argument("--library-root", type=pathlib.Path, default=DEFAULT_LIBRARY_ROOT)

    annotate_parser = sub.add_parser("annotate")
    annotate_parser.add_argument("material_id")
    annotate_parser.add_argument("--title", required=True)
    annotate_parser.add_argument("--note", required=True)
    annotate_parser.add_argument("--tags-json", required=True)
    annotate_parser.add_argument("--library-root", type=pathlib.Path, default=DEFAULT_LIBRARY_ROOT)

    source_media_parser = sub.add_parser("source-media")
    source_media_parser.add_argument("scene")
    source_media_parser.add_argument("segment_index", type=int)
    source_media_parser.add_argument("--source-root", type=pathlib.Path, default=DEFAULT_SOURCE_ROOT)

    material_media_parser = sub.add_parser("material-media")
    material_media_parser.add_argument("material_id")
    material_media_parser.add_argument("segment_index", type=int)
    material_media_parser.add_argument("--library-root", type=pathlib.Path, default=DEFAULT_LIBRARY_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "scan":
            result = scan(args.source_root)
        elif args.command == "import":
            result = import_scene(
                args.scene,
                source_root=args.source_root,
                library_root=args.library_root,
            )
        elif args.command == "library":
            result = library(args.library_root)
        elif args.command == "verify":
            result = verify_material(args.material_id, library_root=args.library_root)
        elif args.command == "annotate":
            try:
                tags = json.loads(args.tags_json)
            except json.JSONDecodeError as exc:
                raise H2IngestError("Tags sind kein gültiges JSON-Array.") from exc
            result = annotate_material(
                args.material_id,
                title=args.title,
                note=args.note,
                tags=tags,
                library_root=args.library_root,
            )
        elif args.command == "source-media":
            result = source_media(
                args.scene,
                args.segment_index,
                source_root=args.source_root,
            )
        else:
            result = material_media(
                args.material_id,
                args.segment_index,
                library_root=args.library_root,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (H2IngestError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "audio_h2_ingest_error",
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
