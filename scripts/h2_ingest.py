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
import heapq
import datetime as dt
import fcntl
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
import time
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
MAX_CONTROL_SCAN_SESSIONS = 2048
MAX_CONTROL_LIBRARY_ITEMS = 80
MAX_METADATA_JSON_BYTES = 2 * 1024 * 1024
MANIFEST_METADATA_ENVELOPE_RESERVE_BYTES = 64 * 1024
MAX_MANIFEST_MASTER_METADATA_BYTES = (
    MAX_METADATA_JSON_BYTES - MANIFEST_METADATA_ENVELOPE_RESERVE_BYTES
)
MAX_WAVE_CHUNK_IDS_JSON_BYTES = MAX_MANIFEST_MASTER_METADATA_BYTES
MIN_CANONICAL_WAVE_CHUNK_ID_JSON_BYTES = 6
MAX_WAVE_CHUNK_COUNT = max(
    0,
    (MAX_WAVE_CHUNK_IDS_JSON_BYTES - 1)
    // (MIN_CANONICAL_WAVE_CHUNK_ID_JSON_BYTES + 1),
)
MAX_WAVE_CHUNK_HEADER_READ_BYTES = MAX_WAVE_CHUNK_COUNT * 8
MAX_LEGACY_MANIFEST_JSON_BYTES = 144 * 1024 * 1024
MAX_LEGACY_ANNOTATIONS_JSON_BYTES = 64 * 1024 * 1024
LEGACY_MANIFEST_CONTROL_NAME = "manifest.control-v1.json"
LEGACY_ANNOTATIONS_CONTROL_NAME = "annotations.control-v1.json"
LEGACY_MIGRATION_RECEIPT_NAME = ".h2-legacy-migration-v1.json"
LEGACY_MIGRATION_WORKER_MEMORY_MAX_BYTES = 512 * 1024 * 1024
LEGACY_MIGRATION_MIN_IO_BYTES_PER_SECOND = 512 * 1024
LEGACY_MIGRATION_IO_PASSES = 2
LEGACY_MIGRATION_BASE_TIMEOUT_SECONDS = 60
LEGACY_MIGRATION_PER_MATERIAL_SECONDS = 1
LEGACY_MIGRATION_RUNTIME_MARGIN_SECONDS = 5 * 60
RELEASE_MARKER_NAME = ".audio-control-release.json"
CONTROL_SCAN_METADATA_BUDGET_BYTES_PER_FILE = (
    12
    + 16
    + MAX_BEXT_BYTES
    + MAX_WAVE_CHUNK_HEADER_READ_BYTES
    + 4096
)
CONTROL_SCAN_PROJECTION = "control-v1"
CONTROL_SCAN_BUDGET_PROJECTION = "control-budget-v1"
CONTROL_LIBRARY_PROJECTION = "control-v1"
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


def _bounded_canonical_size(value: Any, maximum: int) -> int:
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        raise H2IngestError("Metadatenbudget ist ungültig.")
    total = 0
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    for chunk in encoder.iterencode(value):
        total += len(chunk.encode("utf-8"))
        if total > maximum:
            raise H2IngestError(
                "H2-Materialmanifest überschreitet das sichere Metadatenbudget."
            )
    return total


def _manifest_master_metadata_projection(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item["name"],
        "role": item["role"],
        "segment_index": item["segment_index"],
        "sha256": "0" * 64,
        "bytes": item["bytes"],
        "audio": item["audio"],
        "bwf": item["bwf"],
        "chunk_ids": item["chunk_ids"],
        "marker_chunks_observed": item["marker_chunks_observed"],
    }


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
    chunk_ids_json_bytes = 2  # JSON array brackets.
    for chunk_id, size, offset in _iter_wave_chunks(handle, file_size):
        encoded_chunk_id_bytes = len(_canonical_bytes(chunk_id))
        additional_chunk_id_bytes = encoded_chunk_id_bytes + (1 if chunk_ids else 0)
        if (
            chunk_ids_json_bytes + additional_chunk_id_bytes
            > MAX_WAVE_CHUNK_IDS_JSON_BYTES
        ):
            raise H2IngestError(
                "H2-WAV überschreitet das sichere Chunk-Metadatenbudget."
            )
        chunk_ids_json_bytes += additional_chunk_id_bytes
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
    manifest_master_metadata_bytes = 2  # JSON list brackets.
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
            if len(files) >= MAX_SESSION_FILES:
                raise H2IngestError("H2-Session überschreitet das Dateilimit.")
            separator_bytes = 1 if files else 0
            remaining_metadata_bytes = (
                MAX_MANIFEST_MASTER_METADATA_BYTES
                - manifest_master_metadata_bytes
                - separator_bytes
            )
            if remaining_metadata_bytes < 0:
                raise H2IngestError(
                    "H2-Materialmanifest überschreitet das sichere Metadatenbudget."
                )
            item_metadata_bytes = _bounded_canonical_size(
                _manifest_master_metadata_projection(item),
                remaining_metadata_bytes,
            )
            manifest_master_metadata_bytes += separator_bytes + item_metadata_bytes
            files.append(item)
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


def _control_scan_session(session: dict[str, Any]) -> dict[str, Any]:
    files = session.get("files")
    if not isinstance(files, list) or not files:
        raise H2IngestError("H2-Control-Scan besitzt keine Mastergrößen.")
    sizes = [
        item.get("bytes")
        for item in files
        if isinstance(item, dict)
    ]
    if (
        len(sizes) != len(files)
        or any(
            isinstance(size, bool) or not isinstance(size, int) or size <= 0
            for size in sizes
        )
    ):
        raise H2IngestError("H2-Control-Scan besitzt ungültige Mastergrößen.")
    return {
        "scene": session["scene"],
        "recorded_date": session["recorded_date"],
        "recorded_time": session["recorded_time"],
        "sample_rate_hz": session["sample_rate_hz"],
        "duration_seconds": session["duration_seconds"],
        "roles": session["roles"],
        "segment_count": session["segment_count"],
        "total_bytes": sum(sizes),
        "max_file_bytes": max(sizes),
    }


def _control_scan_budget(source: pathlib.Path) -> dict[str, Any]:
    matching_session_count = 0
    candidate_file_count = 0
    total_candidate_bytes = 0
    with os.scandir(source) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not SCENE_RE.fullmatch(entry.name):
                continue
            matching_session_count += 1
            if matching_session_count > MAX_CONTROL_SCAN_SESSIONS:
                raise H2IngestError(
                    "H2-Control-Scan überschreitet das Session-Limit."
                )
            session_candidate_count = 0
            with os.scandir(entry.path) as session_entries:
                for candidate in session_entries:
                    if candidate.name.startswith("."):
                        continue
                    match = ROLE_RE.fullmatch(candidate.name)
                    if match is None or match.group("scene") != entry.name:
                        continue
                    if not candidate.is_file(follow_symlinks=False):
                        continue
                    session_candidate_count += 1
                    if session_candidate_count > MAX_SESSION_FILES:
                        raise H2IngestError(
                            "H2-Control-Scan überschreitet das Dateilimit pro Session."
                        )
                    candidate_file_count += 1
                    total_candidate_bytes += CONTROL_SCAN_METADATA_BUDGET_BYTES_PER_FILE
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_h2_source_scan_budget",
        "projection": CONTROL_SCAN_BUDGET_PROJECTION,
        "matching_session_count": matching_session_count,
        "candidate_file_count": candidate_file_count,
        "total_candidate_bytes": total_candidate_bytes,
        "read_only": True,
        "source_mutated": False,
    }


def scan(
    source_root: pathlib.Path = DEFAULT_SOURCE_ROOT,
    *,
    projection: str = "full",
) -> dict[str, Any]:
    if projection not in {"full", "control", "budget"}:
        raise H2IngestError("Unbekannte H2-Scan-Projektion.")
    source = _resolve_source_root(source_root)
    if projection == "budget":
        return _control_scan_budget(source)
    sessions: list[dict[str, Any]] = []
    skipped: list[str] = []
    observed_scenes = 0
    with os.scandir(source) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not SCENE_RE.fullmatch(entry.name):
                continue
            observed_scenes += 1
            if projection == "control" and observed_scenes > MAX_CONTROL_SCAN_SESSIONS:
                raise H2IngestError(
                    "H2-Control-Scan überschreitet das Session-Limit."
                )
            try:
                session = inspect_scene(source, entry.name)
            except H2IngestError:
                skipped.append(entry.name)
                continue
            sessions.append(
                _control_scan_session(session)
                if projection == "control"
                else session
            )
    sessions.sort(key=lambda item: (item["recorded_date"], item["recorded_time"], item["scene"]))
    result = {
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
    if projection == "control":
        result["projection"] = CONTROL_SCAN_PROJECTION
    return result


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
    if len(payload) > MAX_METADATA_JSON_BYTES:
        raise H2IngestError("Metadatendatei überschreitet das sichere Größenlimit.")
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


def _write_json_atomic_publish(
    path: pathlib.Path,
    value: dict[str, Any],
    mode: int,
) -> None:
    directory = path.parent
    _lstat_directory(directory, "Metadatenverzeichnis")
    if path.exists() or path.is_symlink():
        _lstat_regular(path, "Metadatendatei")
    payload = _canonical_bytes(value) + b"\n"
    if len(payload) > MAX_METADATA_JSON_BYTES:
        raise H2IngestError("Metadatendatei überschreitet das sichere Größenlimit.")
    fd: int | None = None
    temporary: pathlib.Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=".metadata-", dir=directory)
        temporary = pathlib.Path(temporary_name)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        parent_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise H2IngestError(
            "Metadatendatei kann nicht atomar veröffentlicht werden."
        ) from exc
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise


def _read_json_regular(
    path: pathlib.Path,
    *,
    max_bytes: int = MAX_METADATA_JSON_BYTES,
) -> dict[str, Any]:
    metadata = _lstat_regular(path, "Metadatendatei")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
        or metadata.st_size > max_bytes
    ):
        raise H2IngestError("Metadatendatei überschreitet das sichere Größenlimit.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise H2IngestError("Metadatendatei ist nicht sicher lesbar.") from exc
    if not isinstance(value, dict):
        raise H2IngestError("Metadatendatei besitzt kein Objektformat.")
    return value


_JSON_NUMBER_RE = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
)
_JSON_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_LEGACY_ANNOTATION_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "material_id",
        "title",
        "note",
        "tags",
        "markers",
        "updated_at",
    }
)
_LEGACY_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "material_id",
        "source",
        "imported_at",
        "master_set_sha256",
        "masters",
    }
)
_LEGACY_MANIFEST_SOURCE_FIELDS = frozenset(
    {
        "kind",
        "recorder_model",
        "scene",
        "take",
        "recorded_date",
        "recorded_time",
        "segment_count",
    }
)
_LEGACY_MANIFEST_MASTER_FIELDS = frozenset(
    {"name", "role", "bytes", "audio", "segment_index", "sha256"}
)
_LEGACY_MANIFEST_AUDIO_FIELDS = frozenset(
    {
        "codec",
        "sample_rate_hz",
        "channels",
        "bits_per_sample",
        "frames",
        "duration_seconds",
    }
)
MAX_LEGACY_PROJECTION_SCALAR_JSON_BYTES = 64 * 1024


def _json_skip_whitespace(value: str, index: int) -> int:
    while index < len(value) and value[index] in " \t\r\n":
        index += 1
    return index


def _json_scan_string(value: str, index: int) -> int:
    if index >= len(value) or value[index] != '"':
        raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
    index += 1
    while index < len(value):
        character = value[index]
        if character == '"':
            return index + 1
        if ord(character) < 0x20:
            raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
        if character != "\\":
            index += 1
            continue
        index += 1
        if index >= len(value):
            raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
        escape = value[index]
        if escape == "u":
            if (
                index + 5 > len(value)
                or any(
                    character not in _JSON_HEX_DIGITS
                    for character in value[index + 1 : index + 5]
                )
            ):
                raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
            index += 5
            continue
        if escape not in '"\\/bfnrt':
            raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
        index += 1
    raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")


def _json_skip_value(value: str, index: int, depth: int = 0) -> int:
    if depth > 512:
        raise H2IngestError("Legacy-Materialannotation ist zu tief verschachtelt.")
    index = _json_skip_whitespace(value, index)
    if index >= len(value):
        raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
    token = value[index]
    if token == '"':
        return _json_scan_string(value, index)
    if token == "{":
        index = _json_skip_whitespace(value, index + 1)
        if index < len(value) and value[index] == "}":
            return index + 1
        while True:
            key_end = _json_scan_string(value, index)
            index = _json_skip_whitespace(value, key_end)
            if index >= len(value) or value[index] != ":":
                raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
            index = _json_skip_value(value, index + 1, depth + 1)
            index = _json_skip_whitespace(value, index)
            if index >= len(value):
                raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
            if value[index] == "}":
                return index + 1
            if value[index] != ",":
                raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
            index = _json_skip_whitespace(value, index + 1)
    if token == "[":
        index = _json_skip_whitespace(value, index + 1)
        if index < len(value) and value[index] == "]":
            return index + 1
        while True:
            index = _json_skip_value(value, index, depth + 1)
            index = _json_skip_whitespace(value, index)
            if index >= len(value):
                raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
            if value[index] == "]":
                return index + 1
            if value[index] != ",":
                raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
            index = _json_skip_whitespace(value, index + 1)
    for literal in ("true", "false", "null", "NaN", "Infinity", "-Infinity"):
        if value.startswith(literal, index):
            return index + len(literal)
    match = _JSON_NUMBER_RE.match(value, index)
    if match is not None:
        return match.end()
    raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")


def _legacy_annotation_field_ranges(value: str) -> dict[str, tuple[int, int]]:
    index = _json_skip_whitespace(value, 0)
    if index >= len(value) or value[index] != "{":
        raise H2IngestError("Legacy-Materialannotation besitzt kein Objektformat.")
    index = _json_skip_whitespace(value, index + 1)
    fields: dict[str, tuple[int, int]] = {}
    if index < len(value) and value[index] == "}":
        index += 1
    else:
        while True:
            key_start = index
            key_end = _json_scan_string(value, key_start)
            key: str | None = None
            if key_end - key_start <= 128:
                try:
                    decoded_key = json.loads(value[key_start:key_end])
                except json.JSONDecodeError as exc:
                    raise H2IngestError(
                        "Legacy-Materialannotation enthält ungültiges JSON."
                    ) from exc
                if isinstance(decoded_key, str):
                    key = decoded_key
            index = _json_skip_whitespace(value, key_end)
            if index >= len(value) or value[index] != ":":
                raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
            value_start = _json_skip_whitespace(value, index + 1)
            try:
                value_end = _json_skip_value(value, value_start)
            except RecursionError as exc:
                raise H2IngestError(
                    "Legacy-Materialannotation ist zu tief verschachtelt."
                ) from exc
            if key in _LEGACY_ANNOTATION_FIELDS:
                fields[key] = (value_start, value_end)
            index = _json_skip_whitespace(value, value_end)
            if index >= len(value):
                raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
            if value[index] == "}":
                index += 1
                break
            if value[index] != ",":
                raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
            index = _json_skip_whitespace(value, index + 1)
    if _json_skip_whitespace(value, index) != len(value):
        raise H2IngestError("Legacy-Materialannotation enthält ungültiges JSON.")
    return fields


def _legacy_json_string(value: str, bounds: tuple[int, int] | None) -> str | None:
    if bounds is None:
        return None
    start, end = bounds
    if start >= end or value[start] != '"':
        return None
    try:
        decoded = json.loads(value[start:end])
        if not isinstance(decoded, str):
            return None
        decoded.encode("utf-8")
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise H2IngestError("Legacy-Materialannotation enthält ungültigen Text.") from exc
    if len(_canonical_bytes(decoded)) > MAX_METADATA_JSON_BYTES:
        raise H2IngestError(
            "Legacy-Materialannotation überschreitet das sichere Projektionsbudget."
        )
    return decoded


def _legacy_json_tags(value: str, bounds: tuple[int, int] | None) -> list[str] | None:
    if bounds is None:
        return None
    start, end = bounds
    if start >= end or value[start] != "[":
        return None
    index = _json_skip_whitespace(value, start + 1)
    items: list[str] = []
    canonical_bytes = 2
    if index < end and value[index] == "]":
        return items
    while index < end:
        if value[index] != '"':
            return None
        item_end = _json_scan_string(value, index)
        if item_end > end:
            return None
        try:
            item = json.loads(value[index:item_end])
            if not isinstance(item, str):
                return None
            item.encode("utf-8")
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise H2IngestError("Legacy-Materialannotation enthält ungültigen Tagtext.") from exc
        item_bytes = len(_canonical_bytes(item))
        additional = item_bytes + (1 if items else 0)
        if canonical_bytes + additional > MAX_METADATA_JSON_BYTES:
            raise H2IngestError(
                "Legacy-Materialannotation überschreitet das sichere Projektionsbudget."
            )
        canonical_bytes += additional
        items.append(item)
        index = _json_skip_whitespace(value, item_end)
        if index >= end:
            return None
        if value[index] == "]":
            return items if index + 1 == end else None
        if value[index] != ",":
            return None
        index = _json_skip_whitespace(value, index + 1)
    return None


def _legacy_json_schema_version(
    value: str, bounds: tuple[int, int] | None
) -> int | None:
    if bounds is None:
        return None
    start, end = bounds
    token = value[start:end]
    if len(token) > 32 or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", token):
        return None
    try:
        decoded = json.loads(token)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, int) and not isinstance(decoded, bool) else None


def _legacy_json_object_field_ranges(
    value: str,
    bounds: tuple[int, int] | None,
    recognized_fields: frozenset[str],
) -> dict[str, tuple[int, int]] | None:
    if bounds is None:
        return None
    start, end = bounds
    if start >= end or value[start] != "{":
        return None
    index = _json_skip_whitespace(value, start + 1)
    fields: dict[str, tuple[int, int]] = {}
    if index < end and value[index] == "}":
        return fields if index + 1 == end else None
    while index < end:
        key_start = index
        key_end = _json_scan_string(value, key_start)
        if key_end > end:
            return None
        key: str | None = None
        if key_end - key_start <= 128:
            try:
                decoded_key = json.loads(value[key_start:key_end])
            except json.JSONDecodeError as exc:
                raise H2IngestError(
                    "Legacy-Materialmanifest enthält ungültiges JSON."
                ) from exc
            if isinstance(decoded_key, str):
                key = decoded_key
        index = _json_skip_whitespace(value, key_end)
        if index >= end or value[index] != ":":
            raise H2IngestError("Legacy-Materialmanifest enthält ungültiges JSON.")
        value_start = _json_skip_whitespace(value, index + 1)
        try:
            value_end = _json_skip_value(value, value_start)
        except RecursionError as exc:
            raise H2IngestError(
                "Legacy-Materialmanifest ist zu tief verschachtelt."
            ) from exc
        if value_end > end:
            raise H2IngestError("Legacy-Materialmanifest enthält ungültiges JSON.")
        if key in recognized_fields:
            fields[key] = (value_start, value_end)
        index = _json_skip_whitespace(value, value_end)
        if index >= end:
            raise H2IngestError("Legacy-Materialmanifest enthält ungültiges JSON.")
        if value[index] == "}":
            return fields if index + 1 == end else None
        if value[index] != ",":
            raise H2IngestError("Legacy-Materialmanifest enthält ungültiges JSON.")
        index = _json_skip_whitespace(value, index + 1)
    raise H2IngestError("Legacy-Materialmanifest enthält ungültiges JSON.")


def _legacy_manifest_field_ranges(value: str) -> dict[str, tuple[int, int]]:
    start = _json_skip_whitespace(value, 0)
    if start >= len(value) or value[start] != "{":
        raise H2IngestError("Legacy-Materialmanifest besitzt kein Objektformat.")
    try:
        end = _json_skip_value(value, start)
    except RecursionError as exc:
        raise H2IngestError(
            "Legacy-Materialmanifest ist zu tief verschachtelt."
        ) from exc
    if _json_skip_whitespace(value, end) != len(value):
        raise H2IngestError("Legacy-Materialmanifest enthält ungültiges JSON.")
    fields = _legacy_json_object_field_ranges(
        value,
        (start, end),
        _LEGACY_MANIFEST_FIELDS,
    )
    if fields is None:
        raise H2IngestError("Legacy-Materialmanifest besitzt kein Objektformat.")
    return fields


def _legacy_manifest_json_string(
    value: str,
    bounds: tuple[int, int] | None,
) -> str | None:
    if bounds is None:
        return None
    start, end = bounds
    if end - start > MAX_LEGACY_PROJECTION_SCALAR_JSON_BYTES:
        return None
    return _legacy_json_string(value, bounds)


def _legacy_json_integer(
    value: str,
    bounds: tuple[int, int] | None,
) -> int | None:
    if bounds is None:
        return None
    start, end = bounds
    token = value[start:end]
    if len(token) > 64 or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", token):
        return None
    try:
        decoded = json.loads(token)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, int) and not isinstance(decoded, bool) else None


def _legacy_json_finite_number(
    value: str,
    bounds: tuple[int, int] | None,
) -> int | float | None:
    if bounds is None:
        return None
    start, end = bounds
    token = value[start:end]
    if len(token) > 128:
        return None
    try:
        decoded = json.loads(token)
    except json.JSONDecodeError:
        return None
    if (
        isinstance(decoded, bool)
        or not isinstance(decoded, (int, float))
        or not math.isfinite(decoded)
    ):
        return None
    return decoded


def _legacy_manifest_source_projection(
    value: str,
    bounds: tuple[int, int] | None,
) -> dict[str, Any] | None:
    fields = _legacy_json_object_field_ranges(
        value,
        bounds,
        _LEGACY_MANIFEST_SOURCE_FIELDS,
    )
    if fields is None:
        return None
    take_bounds = fields.get("take")
    if take_bounds is None:
        take: str | None | int = None
    elif value[take_bounds[0] : take_bounds[1]] == "null":
        take = None
    else:
        decoded_take = _legacy_manifest_json_string(value, take_bounds)
        take = decoded_take if decoded_take is not None else 0
    return {
        "kind": _legacy_manifest_json_string(value, fields.get("kind")),
        "recorder_model": _legacy_manifest_json_string(
            value, fields.get("recorder_model")
        ),
        "scene": _legacy_manifest_json_string(value, fields.get("scene")),
        "take": take,
        "recorded_date": _legacy_manifest_json_string(
            value, fields.get("recorded_date")
        ),
        "recorded_time": _legacy_manifest_json_string(
            value, fields.get("recorded_time")
        ),
        "segment_count": _legacy_json_integer(
            value, fields.get("segment_count")
        ),
    }


def _legacy_manifest_audio_projection(
    value: str,
    bounds: tuple[int, int] | None,
) -> dict[str, Any] | None:
    fields = _legacy_json_object_field_ranges(
        value,
        bounds,
        _LEGACY_MANIFEST_AUDIO_FIELDS,
    )
    if fields is None:
        return None
    projected: dict[str, Any] = {}
    codec = _legacy_manifest_json_string(value, fields.get("codec"))
    if codec is not None:
        projected["codec"] = codec
    for name in ("sample_rate_hz", "channels", "bits_per_sample", "frames"):
        decoded = _legacy_json_integer(value, fields.get(name))
        if decoded is not None:
            projected[name] = decoded
    duration = _legacy_json_finite_number(value, fields.get("duration_seconds"))
    if duration is not None:
        projected["duration_seconds"] = duration
    return projected


def _legacy_manifest_masters_projection(
    value: str,
    bounds: tuple[int, int] | None,
) -> list[dict[str, Any]] | None:
    if bounds is None:
        return None
    start, end = bounds
    if start >= end or value[start] != "[":
        return None
    index = _json_skip_whitespace(value, start + 1)
    masters: list[dict[str, Any]] = []
    if index < end and value[index] == "]":
        return masters if index + 1 == end else None
    while index < end:
        if len(masters) >= MAX_SESSION_FILES:
            raise H2IngestError(
                "Legacy-Materialmanifest überschreitet das sichere Master-Limit."
            )
        item_start = index
        item_end = _json_skip_value(value, item_start)
        fields = _legacy_json_object_field_ranges(
            value,
            (item_start, item_end),
            _LEGACY_MANIFEST_MASTER_FIELDS,
        )
        if fields is None:
            return None
        segment_bounds = fields.get("segment_index")
        segment_index = (
            0
            if segment_bounds is None
            else _legacy_json_integer(value, segment_bounds)
        )
        masters.append(
            {
                "name": _legacy_manifest_json_string(value, fields.get("name")),
                "role": _legacy_manifest_json_string(value, fields.get("role")),
                "bytes": _legacy_json_integer(value, fields.get("bytes")),
                "audio": _legacy_manifest_audio_projection(
                    value, fields.get("audio")
                ),
                "segment_index": segment_index,
                "sha256": _legacy_manifest_json_string(
                    value, fields.get("sha256")
                ),
            }
        )
        index = _json_skip_whitespace(value, item_end)
        if index >= end:
            return None
        if value[index] == "]":
            return masters if index + 1 == end else None
        if value[index] != ",":
            raise H2IngestError("Legacy-Materialmanifest enthält ungültiges JSON.")
        index = _json_skip_whitespace(value, index + 1)
    return None


def _read_legacy_manifest_projection(
    path: pathlib.Path,
) -> dict[str, Any]:
    metadata = _lstat_regular(path, "Legacy-Materialmanifest")
    if (
        metadata.st_size <= MAX_METADATA_JSON_BYTES
        or metadata.st_size > MAX_LEGACY_MANIFEST_JSON_BYTES
    ):
        raise H2IngestError(
            "Legacy-Materialmanifest liegt außerhalb des Migrationslimits."
        )
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise H2IngestError("Legacy-Materialmanifest ist nicht sicher lesbar.") from exc
    fields = _legacy_manifest_field_ranges(value)
    return {
        "schema_version": _legacy_json_schema_version(
            value, fields.get("schema_version")
        ),
        "kind": _legacy_manifest_json_string(value, fields.get("kind")),
        "material_id": _legacy_manifest_json_string(
            value, fields.get("material_id")
        ),
        "source": _legacy_manifest_source_projection(
            value, fields.get("source")
        ),
        "imported_at": _legacy_manifest_json_string(
            value, fields.get("imported_at")
        ),
        "master_set_sha256": _legacy_manifest_json_string(
            value, fields.get("master_set_sha256")
        ),
        "masters": _legacy_manifest_masters_projection(
            value, fields.get("masters")
        ),
    }


def _read_legacy_annotations_projection(
    path: pathlib.Path,
) -> dict[str, Any]:
    metadata = _lstat_regular(path, "Legacy-Materialannotation")
    if (
        metadata.st_size <= MAX_METADATA_JSON_BYTES
        or metadata.st_size > MAX_LEGACY_ANNOTATIONS_JSON_BYTES
    ):
        raise H2IngestError("Legacy-Materialannotation liegt außerhalb des Migrationslimits.")
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise H2IngestError("Legacy-Materialannotation ist nicht sicher lesbar.") from exc
    fields = _legacy_annotation_field_ranges(value)

    updated_at_bounds = fields.get("updated_at")
    updated_at: str | None | int
    if updated_at_bounds is None:
        updated_at = None
    elif value[updated_at_bounds[0] : updated_at_bounds[1]] == "null":
        updated_at = None
    else:
        decoded_updated_at = _legacy_json_string(value, updated_at_bounds)
        updated_at = decoded_updated_at if decoded_updated_at is not None else 0

    markers_bounds = fields.get("markers")
    markers_is_list = (
        markers_bounds is not None
        and markers_bounds[0] < markers_bounds[1]
        and value[markers_bounds[0]] == "["
    )
    projection: dict[str, Any] = {
        "schema_version": _legacy_json_schema_version(
            value, fields.get("schema_version")
        ),
        "kind": _legacy_json_string(value, fields.get("kind")),
        "material_id": _legacy_json_string(value, fields.get("material_id")),
        "title": _legacy_json_string(value, fields.get("title")),
        "note": _legacy_json_string(value, fields.get("note")),
        "tags": _legacy_json_tags(value, fields.get("tags")),
        "markers": [] if markers_is_list else None,
        "updated_at": updated_at,
    }
    return projection


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



def _legacy_manifest_control_projection(
    manifest: dict[str, Any],
    material_id: str,
    *,
    legacy_sha256: str,
    legacy_bytes: int,
    legacy_mtime_ns: int,
    legacy_device: int,
    legacy_inode: int,
) -> dict[str, Any]:
    empty_annotations = {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_material_annotations",
        "material_id": material_id,
        "title": "",
        "note": "",
        "tags": [],
        "markers": [],
        "updated_at": None,
    }
    projected = _library_item(manifest, empty_annotations, material_id)
    source = projected["source"]
    compact_source = {
        "kind": source.get("kind"),
        "recorder_model": source.get("recorder_model"),
        "scene": source.get("scene"),
        "take": source.get("take"),
        "recorded_date": source.get("recorded_date"),
        "recorded_time": source.get("recorded_time"),
        "segment_count": source.get("segment_count"),
    }
    original_masters = manifest.get("masters")
    if not isinstance(original_masters, list) or len(original_masters) != len(projected["masters"]):
        raise H2IngestError("Legacy-Materialmanifest besitzt keine eindeutige Masterprojektion.")
    compact_masters: list[dict[str, Any]] = []
    for original, compact in zip(original_masters, projected["masters"], strict=True):
        digest = original.get("sha256") if isinstance(original, dict) else None
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise H2IngestError("Legacy-Materialmanifest besitzt keinen gültigen Masterhash.")
        compact_masters.append({**compact, "sha256": digest})
    binding = {
        "sha256": legacy_sha256,
        "bytes": legacy_bytes,
        "mtime_ns": legacy_mtime_ns,
        "device": legacy_device,
        "inode": legacy_inode,
    }
    if (
        re.fullmatch(r"[0-9a-f]{64}", legacy_sha256) is None
        or isinstance(legacy_bytes, bool)
        or not isinstance(legacy_bytes, int)
        or legacy_bytes <= MAX_METADATA_JSON_BYTES
        or isinstance(legacy_mtime_ns, bool)
        or not isinstance(legacy_mtime_ns, int)
        or legacy_mtime_ns < 0
        or isinstance(legacy_device, bool)
        or not isinstance(legacy_device, int)
        or legacy_device < 0
        or isinstance(legacy_inode, bool)
        or not isinstance(legacy_inode, int)
        or legacy_inode <= 0
    ):
        raise H2IngestError("Legacy-Materialmanifest besitzt keinen gültigen Sidecar-Beleg.")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_imported_material_manifest_control",
        "material_id": material_id,
        "source": compact_source,
        "imported_at": projected["imported_at"],
        "master_set_sha256": projected["master_set_sha256"],
        "masters": compact_masters,
        "legacy_manifest": binding,
        "read_only": True,
    }


def _manifest_from_legacy_control(
    control: dict[str, Any],
    material_id: str,
    *,
    manifest_metadata: os.stat_result,
) -> dict[str, Any]:
    legacy = control.get("legacy_manifest")
    masters = control.get("masters")
    source = control.get("source")
    if (
        control.get("schema_version") != SCHEMA_VERSION
        or control.get("kind") != "audio_imported_material_manifest_control"
        or control.get("material_id") != material_id
        or control.get("read_only") is not True
        or not isinstance(legacy, dict)
        or legacy.get("bytes") != manifest_metadata.st_size
        or legacy.get("mtime_ns") != manifest_metadata.st_mtime_ns
        or legacy.get("device") != manifest_metadata.st_dev
        or legacy.get("inode") != manifest_metadata.st_ino
        or not isinstance(legacy.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", legacy["sha256"]) is None
        or not isinstance(source, dict)
        or not isinstance(masters, list)
        or not masters
        or len(masters) > MAX_SESSION_FILES
    ):
        raise H2IngestError(
            "Legacy-Materialmanifest benötigt eine aktuelle, gebundene Control-Sidecar."
        )
    synthetic = {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_imported_material_manifest",
        "material_id": material_id,
        "source": source,
        "imported_at": control.get("imported_at"),
        "master_set_sha256": control.get("master_set_sha256"),
        "masters": masters,
        "integrity": {
            "legacy_manifest_sha256": legacy["sha256"],
            "legacy_control_projection": True,
        },
    }
    empty_annotations = {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_material_annotations",
        "material_id": material_id,
        "title": "",
        "note": "",
        "tags": [],
        "markers": [],
        "updated_at": None,
    }
    _library_item(synthetic, empty_annotations, material_id)
    for master in masters:
        digest = master.get("sha256") if isinstance(master, dict) else None
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise H2IngestError("Legacy-Control-Sidecar besitzt einen ungültigen Masterhash.")
    return synthetic


def _read_manifest(directory: pathlib.Path, material_id: str) -> dict[str, Any]:
    manifest_path = directory / "manifest.json"
    metadata = _lstat_regular(manifest_path, "Materialmanifest")
    if metadata.st_size <= MAX_METADATA_JSON_BYTES:
        return _read_json_regular(manifest_path)
    control = _read_json_regular(directory / LEGACY_MANIFEST_CONTROL_NAME)
    return _manifest_from_legacy_control(
        control,
        material_id,
        manifest_metadata=metadata,
    )


def _legacy_annotations_control_projection(
    annotations: dict[str, Any],
    material_id: str,
    *,
    legacy_sha256: str,
    legacy_metadata: os.stat_result,
) -> dict[str, Any]:
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
        raise H2IngestError("Legacy-Materialannotation ist strukturell ungültig.")
    if (
        re.fullmatch(r"[0-9a-f]{64}", legacy_sha256) is None
        or legacy_metadata.st_size <= MAX_METADATA_JSON_BYTES
        or legacy_metadata.st_size > MAX_LEGACY_ANNOTATIONS_JSON_BYTES
    ):
        raise H2IngestError("Legacy-Materialannotation besitzt keinen gültigen Sidecar-Beleg.")
    binding = {
        "sha256": legacy_sha256,
        "bytes": legacy_metadata.st_size,
        "mtime_ns": legacy_metadata.st_mtime_ns,
        "device": legacy_metadata.st_dev,
        "inode": legacy_metadata.st_ino,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_material_annotations_control",
        "material_id": material_id,
        "title": annotations["title"],
        "note": annotations["note"],
        "tags": annotations["tags"],
        "updated_at": annotations.get("updated_at"),
        "legacy_annotations": binding,
        "legacy_markers_preserved": True,
    }


def _annotations_from_legacy_control(
    control: dict[str, Any],
    material_id: str,
    *,
    annotations_metadata: os.stat_result,
) -> dict[str, Any]:
    legacy = control.get("legacy_annotations")
    tags = control.get("tags")
    if (
        control.get("schema_version") != SCHEMA_VERSION
        or control.get("kind") != "audio_material_annotations_control"
        or control.get("material_id") != material_id
        or control.get("legacy_markers_preserved") is not True
        or not isinstance(control.get("title"), str)
        or not isinstance(control.get("note"), str)
        or not isinstance(tags, list)
        or not all(isinstance(item, str) for item in tags)
        or not (
            control.get("updated_at") is None
            or isinstance(control.get("updated_at"), str)
        )
        or not isinstance(legacy, dict)
        or legacy.get("bytes") != annotations_metadata.st_size
        or legacy.get("mtime_ns") != annotations_metadata.st_mtime_ns
        or legacy.get("device") != annotations_metadata.st_dev
        or legacy.get("inode") != annotations_metadata.st_ino
        or not isinstance(legacy.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", legacy["sha256"]) is None
        or annotations_metadata.st_size <= MAX_METADATA_JSON_BYTES
        or annotations_metadata.st_size > MAX_LEGACY_ANNOTATIONS_JSON_BYTES
    ):
        raise H2IngestError(
            "Legacy-Materialannotation benötigt eine aktuelle, gebundene Control-Sidecar."
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_material_annotations",
        "material_id": material_id,
        "title": control["title"],
        "note": control["note"],
        "tags": tags,
        "markers": [],
        "updated_at": control.get("updated_at"),
    }


def _read_annotations(directory: pathlib.Path, material_id: str) -> dict[str, Any]:
    annotations_path = directory / "annotations.json"
    metadata = _lstat_regular(annotations_path, "Materialannotation")
    if metadata.st_size <= MAX_METADATA_JSON_BYTES:
        return _read_json_regular(annotations_path)
    control = _read_json_regular(directory / LEGACY_ANNOTATIONS_CONTROL_NAME)
    return _annotations_from_legacy_control(
        control,
        material_id,
        annotations_metadata=metadata,
    )


def migrate_legacy_manifests(
    library_root: pathlib.Path = DEFAULT_LIBRARY_ROOT,
) -> dict[str, Any]:
    root = library_root.expanduser()
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_h2_legacy_manifest_migration",
        "library_root": str(root),
        "migrated": 0,
        "already_bound": 0,
        "compact": 0,
        "manifest_migrated": 0,
        "manifest_already_bound": 0,
        "annotations_migrated": 0,
        "annotations_already_bound": 0,
        "annotations_compact": 0,
        "read_only_originals": True,
    }
    if not root.exists() and not root.is_symlink():
        return result
    _lstat_directory(root, "Materialbibliothek")
    names: list[str] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and MATERIAL_ID_RE.fullmatch(entry.name):
                names.append(entry.name)
    for material_id in sorted(names):
        directory = root / material_id

        manifest_path = directory / "manifest.json"
        manifest_metadata = _lstat_regular(manifest_path, "Materialmanifest")
        if manifest_metadata.st_size <= MAX_METADATA_JSON_BYTES:
            result["compact"] += 1
        else:
            if manifest_metadata.st_size > MAX_LEGACY_MANIFEST_JSON_BYTES:
                raise H2IngestError(
                    "Legacy-Materialmanifest überschreitet das sichere Migrationslimit."
                )
            control_path = directory / LEGACY_MANIFEST_CONTROL_NAME
            manifest_bound = False
            if control_path.exists() or control_path.is_symlink():
                try:
                    control = _read_json_regular(control_path)
                    _manifest_from_legacy_control(
                        control,
                        material_id,
                        manifest_metadata=manifest_metadata,
                    )
                except H2IngestError:
                    pass
                else:
                    manifest_bound = True
                    result["already_bound"] += 1
                    result["manifest_already_bound"] += 1
            if not manifest_bound:
                manifest = _read_legacy_manifest_projection(manifest_path)
                digest = _sha256_path(manifest_path)
                control = _legacy_manifest_control_projection(
                    manifest,
                    material_id,
                    legacy_sha256=digest,
                    legacy_bytes=manifest_metadata.st_size,
                    legacy_mtime_ns=manifest_metadata.st_mtime_ns,
                    legacy_device=manifest_metadata.st_dev,
                    legacy_inode=manifest_metadata.st_ino,
                )
                if control_path.exists() or control_path.is_symlink():
                    _write_json_replace(control_path, control, 0o440)
                else:
                    _write_json_new(control_path, control, 0o440)
                observed = _read_json_regular(control_path)
                _manifest_from_legacy_control(
                    observed,
                    material_id,
                    manifest_metadata=manifest_metadata,
                )
                result["migrated"] += 1
                result["manifest_migrated"] += 1

        annotations_path = directory / "annotations.json"
        annotations_metadata = _lstat_regular(annotations_path, "Materialannotation")
        if annotations_metadata.st_size <= MAX_METADATA_JSON_BYTES:
            result["annotations_compact"] += 1
            continue
        if annotations_metadata.st_size > MAX_LEGACY_ANNOTATIONS_JSON_BYTES:
            raise H2IngestError(
                "Legacy-Materialannotation überschreitet das sichere Migrationslimit."
            )
        annotations_control_path = directory / LEGACY_ANNOTATIONS_CONTROL_NAME
        annotations_bound = False
        if annotations_control_path.exists() or annotations_control_path.is_symlink():
            try:
                annotations_control = _read_json_regular(annotations_control_path)
            except H2IngestError as exc:
                if not isinstance(exc.__cause__, (UnicodeError, json.JSONDecodeError)):
                    raise
            else:
                _annotations_from_legacy_control(
                    annotations_control,
                    material_id,
                    annotations_metadata=annotations_metadata,
                )
                binding = annotations_control["legacy_annotations"]
                if _sha256_path(annotations_path) != binding["sha256"]:
                    raise H2IngestError(
                        "Legacy-Materialannotation weicht vom gebundenen Migrationsbeleg ab."
                    )
                annotations_bound = True
                result["annotations_already_bound"] += 1
        if annotations_bound:
            os.chmod(annotations_path, 0o440)
            continue

        legacy_annotations = _read_legacy_annotations_projection(
            annotations_path,
        )
        annotations_digest = _sha256_path(annotations_path)
        annotations_control = _legacy_annotations_control_projection(
            legacy_annotations,
            material_id,
            legacy_sha256=annotations_digest,
            legacy_metadata=annotations_metadata,
        )
        if annotations_control_path.exists() or annotations_control_path.is_symlink():
            _write_json_replace(annotations_control_path, annotations_control, 0o600)
        else:
            _write_json_new(annotations_control_path, annotations_control, 0o600)
        observed_control = _read_json_regular(annotations_control_path)
        _annotations_from_legacy_control(
            observed_control,
            material_id,
            annotations_metadata=annotations_metadata,
        )
        os.chmod(annotations_path, 0o440)
        result["annotations_migrated"] += 1
    return result


def _control_library_material_count(root: pathlib.Path) -> int:
    count = 0
    with os.scandir(root) as entries:
        for entry in entries:
            if (
                entry.is_dir(follow_symlinks=False)
                and MATERIAL_ID_RE.fullmatch(entry.name) is not None
            ):
                count += 1
    return count


def _open_library_import_lock(library: pathlib.Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(library / ".h2-import.lock", flags, 0o600)
    except OSError as error:
        raise H2IngestError("H2-Import-Lock kann nicht geöffnet werden.") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise H2IngestError("H2-Import-Lock ist nicht vertrauenswürdig.")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _legacy_migration_inventory(library_root: pathlib.Path) -> dict[str, Any]:
    root = library_root.expanduser()
    result: dict[str, Any] = {
        "library_root": str(root),
        "material_count": 0,
        "candidate_file_count": 0,
        "candidate_bytes": 0,
        "candidate_metadata_sha256": hashlib.sha256(b"").hexdigest(),
    }
    if not root.exists() and not root.is_symlink():
        return result
    metadata = _lstat_directory(root, "Materialbibliothek")
    if metadata.st_uid != os.getuid():
        raise H2IngestError("Materialbibliothek gehört nicht dem aktuellen Benutzer.")

    records: list[dict[str, Any]] = []
    with os.scandir(root) as entries:
        names = sorted(
            entry.name
            for entry in entries
            if entry.is_dir(follow_symlinks=False)
            and MATERIAL_ID_RE.fullmatch(entry.name) is not None
        )
    result["material_count"] = len(names)
    for material_id in names:
        directory = root / material_id
        for name, maximum in (
            ("manifest.json", MAX_LEGACY_MANIFEST_JSON_BYTES),
            ("annotations.json", MAX_LEGACY_ANNOTATIONS_JSON_BYTES),
        ):
            item = directory / name
            item_metadata = _lstat_regular(item, f"Legacy-{name}")
            if item_metadata.st_size > maximum:
                raise H2IngestError(
                    f"Legacy-{name} überschreitet das sichere Migrationslimit."
                )
            if item_metadata.st_size <= MAX_METADATA_JSON_BYTES:
                continue
            result["candidate_file_count"] += 1
            result["candidate_bytes"] += item_metadata.st_size
            records.append(
                {
                    "material_id": material_id,
                    "name": name,
                    "bytes": item_metadata.st_size,
                    "mtime_ns": item_metadata.st_mtime_ns,
                    "device": item_metadata.st_dev,
                    "inode": item_metadata.st_ino,
                }
            )
    digest = hashlib.sha256()
    for record in records:
        digest.update(_canonical_bytes(record))
        digest.update(b"\n")
    result["candidate_metadata_sha256"] = digest.hexdigest()
    return result


def _legacy_migration_timeout_seconds(inventory: dict[str, Any]) -> int:
    material_count = inventory.get("material_count")
    candidate_file_count = inventory.get("candidate_file_count")
    candidate_bytes = inventory.get("candidate_bytes")
    if (
        isinstance(material_count, bool)
        or not isinstance(material_count, int)
        or material_count < 0
        or isinstance(candidate_file_count, bool)
        or not isinstance(candidate_file_count, int)
        or candidate_file_count < 0
        or isinstance(candidate_bytes, bool)
        or not isinstance(candidate_bytes, int)
        or candidate_bytes < 0
        or (candidate_file_count == 0) != (candidate_bytes == 0)
    ):
        raise H2IngestError("Legacy-Migrationsbudget ist ungültig.")
    io_seconds = (
        candidate_bytes * LEGACY_MIGRATION_IO_PASSES
        + LEGACY_MIGRATION_MIN_IO_BYTES_PER_SECOND
        - 1
    ) // LEGACY_MIGRATION_MIN_IO_BYTES_PER_SECOND
    return (
        LEGACY_MIGRATION_BASE_TIMEOUT_SECONDS
        + material_count * LEGACY_MIGRATION_PER_MATERIAL_SECONDS
        + io_seconds
    )


def _durable_migration_release_commit() -> str | None:
    marker = pathlib.Path(__file__).resolve().parents[1] / RELEASE_MARKER_NAME
    if not marker.exists() and not marker.is_symlink():
        return None
    payload = _read_json_regular(marker)
    commit = payload.get("commit")
    if (
        payload.get("kind") != "audio_control_release"
        or not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    ):
        raise H2IngestError("Release-Bindung der Legacy-Migration ist ungültig.")
    return commit


def _legacy_migration_receipt_path(root: pathlib.Path) -> pathlib.Path:
    return root / LEGACY_MIGRATION_RECEIPT_NAME


def _legacy_migration_postcondition_sha256(
    root: pathlib.Path,
    expected_inventory: dict[str, Any],
) -> str:
    current = _legacy_migration_inventory(root)
    for key in (
        "material_count",
        "candidate_file_count",
        "candidate_bytes",
        "candidate_metadata_sha256",
    ):
        if current.get(key) != expected_inventory.get(key):
            raise H2IngestError(
                "Legacy-Migrationsbestand änderte sich während der Migration."
            )
    digest = hashlib.sha256()
    with os.scandir(root) as entries:
        names = sorted(
            entry.name
            for entry in entries
            if entry.is_dir(follow_symlinks=False)
            and MATERIAL_ID_RE.fullmatch(entry.name) is not None
        )
    for material_id in names:
        directory = root / material_id
        for name, sidecar_name in (
            ("manifest.json", LEGACY_MANIFEST_CONTROL_NAME),
            ("annotations.json", LEGACY_ANNOTATIONS_CONTROL_NAME),
        ):
            metadata = _lstat_regular(directory / name, f"Legacy-{name}")
            if metadata.st_size <= MAX_METADATA_JSON_BYTES:
                continue
            sidecar = directory / sidecar_name
            sidecar_metadata = _lstat_regular(
                sidecar, "Legacy-Migrations-Control-Sidecar"
            )
            if sidecar_metadata.st_size > MAX_METADATA_JSON_BYTES:
                raise H2IngestError(
                    "Legacy-Migrations-Control-Sidecar überschreitet das Größenlimit."
                )
            if name == "annotations.json":
                control = _read_json_regular(sidecar)
                immutable_control = {
                    "schema_version": control.get("schema_version"),
                    "kind": control.get("kind"),
                    "material_id": control.get("material_id"),
                    "legacy_annotations": control.get("legacy_annotations"),
                    "legacy_markers_preserved": control.get(
                        "legacy_markers_preserved"
                    ),
                }
                sidecar_binding_sha256 = hashlib.sha256(
                    _canonical_bytes(immutable_control)
                ).hexdigest()
            else:
                sidecar_binding_sha256 = _sha256_path(sidecar)
            record = {
                "material_id": material_id,
                "name": name,
                "bytes": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "sidecar_binding_sha256": sidecar_binding_sha256,
            }
            digest.update(_canonical_bytes(record))
            digest.update(b"\n")
    return digest.hexdigest()


def _read_legacy_migration_receipt(root: pathlib.Path) -> dict[str, Any] | None:
    path = _legacy_migration_receipt_path(root)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return _read_json_regular(path)
    except H2IngestError as exc:
        if isinstance(exc.__cause__, (UnicodeError, json.JSONDecodeError)):
            return None
        raise


def _write_legacy_migration_receipt(root: pathlib.Path, value: dict[str, Any]) -> None:
    _write_json_atomic_publish(
        _legacy_migration_receipt_path(root),
        value,
        0o600,
    )


def _durable_migration_receipt_result(
    root: pathlib.Path,
    *,
    release_commit: str,
    inventory: dict[str, Any],
) -> dict[str, Any] | None:
    receipt = _read_legacy_migration_receipt(root)
    if receipt is None:
        return None
    receipt_commit = receipt.get("release_commit")
    if isinstance(receipt_commit, str) and receipt_commit != release_commit:
        return None
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("kind") != "audio_h2_legacy_migration_receipt"
        or receipt_commit != release_commit
        or receipt.get("library_root") != str(root)
        or receipt.get("candidate_metadata_sha256")
        != inventory.get("candidate_metadata_sha256")
        or receipt.get("candidate_file_count") != inventory.get("candidate_file_count")
        or receipt.get("candidate_bytes") != inventory.get("candidate_bytes")
        or receipt.get("status") != "success"
    ):
        raise H2IngestError("Legacy-Migrationsbeleg ist ungültig.")
    observed_postcondition = _legacy_migration_postcondition_sha256(root, inventory)
    if receipt.get("postcondition_sha256") != observed_postcondition:
        return None
    result = receipt.get("result")
    if (
        not isinstance(result, dict)
        or result.get("kind") != "audio_h2_legacy_manifest_migration"
        or result.get("library_root") != str(root)
    ):
        raise H2IngestError("Legacy-Migrationsbeleg enthält kein gültiges Ergebnis.")
    reused = dict(result)
    reused["durable_worker"] = True
    reused["durable_receipt_reused"] = True
    reused["release_commit"] = release_commit
    return reused


def _legacy_migration_worker_unit(root: pathlib.Path) -> str:
    token = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return f"audio-h2-legacy-migrate-v1-{token}.service"


def _legacy_migration_systemd_state(unit: str) -> dict[str, str]:
    try:
        completed = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=Result",
                "--property=ExecMainStatus",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
            check=False,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise H2IngestError("Legacy-Migrationsworker-Zustand ist nicht lesbar.") from exc
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if not values:
        values["LoadState"] = "not-found"
        values["ActiveState"] = "inactive"
        values["SubState"] = "dead"
    return values


def _launch_legacy_migration_worker(
    root: pathlib.Path,
    inventory: dict[str, Any],
) -> str:
    root = root.expanduser()
    if not root.is_absolute():
        raise H2IngestError("Durable Legacy-Migration benötigt einen absoluten Bibliothekspfad.")
    script = pathlib.Path(__file__).resolve()
    release = script.parents[1]
    unit = _legacy_migration_worker_unit(root)
    unit_name = unit.removesuffix(".service")
    runtime_seconds = (
        _legacy_migration_timeout_seconds(inventory)
        + LEGACY_MIGRATION_RUNTIME_MARGIN_SECONDS
    )
    argv = [
        "systemd-run",
        "--user",
        "--collect",
        "--no-block",
        "--quiet",
        "--unit",
        unit_name,
        "--service-type=exec",
        f"--property=RuntimeMaxSec={runtime_seconds}s",
        "--property=TimeoutStopSec=10s",
        "--property=KillMode=control-group",
        "--property=LimitCORE=0",
        "--property=NoNewPrivileges=yes",
        "--property=PrivateTmp=yes",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=read-only",
        f"--property=ReadWritePaths={root}",
        "--property=ProtectControlGroups=yes",
        "--property=ProtectKernelTunables=yes",
        "--property=LockPersonality=yes",
        "--property=RestrictSUIDSGID=yes",
        "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=UMask=0077",
        f"--property=MemoryMax={LEGACY_MIGRATION_WORKER_MEMORY_MAX_BYTES}",
        "--property=CPUQuota=100%",
        "--property=TasksMax=16",
        f"--working-directory={release}",
        "--setenv=LC_ALL=C.UTF-8",
        "--",
        sys.executable,
        str(script),
        "_migrate-legacy-manifests-worker",
        "--library-root",
        str(root),
    ]
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise H2IngestError("Legacy-Migrationsworker konnte nicht gestartet werden.") from exc
    if completed.returncode != 0:
        state = _legacy_migration_systemd_state(unit)
        if state.get("ActiveState") not in {"active", "activating", "reloading"}:
            detail = completed.stderr.strip()
            if len(detail) > 300:
                detail = detail[:300] + "…"
            raise H2IngestError(
                "Legacy-Migrationsworker konnte nicht gestartet werden"
                + (f": {detail}" if detail else ".")
            )
    return unit


def _run_legacy_migration_worker(
    library_root: pathlib.Path,
) -> dict[str, Any]:
    root = library_root.expanduser()
    release_commit = _durable_migration_release_commit()
    if release_commit is None:
        raise H2IngestError("Durable Legacy-Migration benötigt einen Releasebeleg.")
    _lstat_directory(root, "Materialbibliothek")
    descriptor = _open_library_import_lock(root)
    try:
        inventory = _legacy_migration_inventory(root)
        result = migrate_legacy_manifests(root)
        postcondition = _legacy_migration_postcondition_sha256(root, inventory)
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "kind": "audio_h2_legacy_migration_receipt",
            "status": "success",
            "release_commit": release_commit,
            "library_root": str(root),
            "material_count": inventory["material_count"],
            "candidate_file_count": inventory["candidate_file_count"],
            "candidate_bytes": inventory["candidate_bytes"],
            "candidate_metadata_sha256": inventory["candidate_metadata_sha256"],
            "postcondition_sha256": postcondition,
            "result": result,
        }
        _write_legacy_migration_receipt(root, receipt)
    finally:
        os.close(descriptor)
    completed = dict(result)
    completed["durable_worker"] = True
    completed["durable_receipt_reused"] = False
    completed["release_commit"] = release_commit
    return completed


def launch_legacy_manifests_durable(
    library_root: pathlib.Path = DEFAULT_LIBRARY_ROOT,
) -> dict[str, Any]:
    """Schedule oversized legacy migration without waiting for worker completion."""
    root = library_root.expanduser()
    release_commit = _durable_migration_release_commit()
    if release_commit is None:
        result = migrate_legacy_manifests(root)
        result["durable_worker"] = False
        result["launch_only"] = True
        return result

    inventory = _legacy_migration_inventory(root)
    if inventory["candidate_file_count"] == 0:
        result = migrate_legacy_manifests(root)
        result["durable_worker"] = False
        result["launch_only"] = True
        result["release_commit"] = release_commit
        return result

    receipt_result = _durable_migration_receipt_result(
        root,
        release_commit=release_commit,
        inventory=inventory,
    )
    if receipt_result is not None:
        receipt_result["launch_only"] = True
        return receipt_result

    unit = _launch_legacy_migration_worker(root, inventory)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_h2_legacy_migration_launch",
        "status": "scheduled",
        "library_root": str(root),
        "durable_worker": True,
        "launch_only": True,
        "release_commit": release_commit,
        "unit": unit,
        "candidate_file_count": inventory["candidate_file_count"],
        "candidate_bytes": inventory["candidate_bytes"],
        "candidate_metadata_sha256": inventory["candidate_metadata_sha256"],
    }


def migrate_legacy_manifests_durable(
    library_root: pathlib.Path = DEFAULT_LIBRARY_ROOT,
) -> dict[str, Any]:
    root = library_root.expanduser()
    release_commit = _durable_migration_release_commit()
    if release_commit is None:
        return migrate_legacy_manifests(root)

    inventory = _legacy_migration_inventory(root)
    if inventory["candidate_file_count"] == 0:
        return migrate_legacy_manifests(root)

    receipt_result = _durable_migration_receipt_result(
        root,
        release_commit=release_commit,
        inventory=inventory,
    )
    if receipt_result is not None:
        return receipt_result

    unit = _legacy_migration_worker_unit(root)
    state = _legacy_migration_systemd_state(unit)
    launched_for_current_release = False
    if state.get("ActiveState") not in {"active", "activating", "reloading"}:
        _launch_legacy_migration_worker(root, inventory)
        launched_for_current_release = True

    while True:
        receipt_result = _durable_migration_receipt_result(
            root,
            release_commit=release_commit,
            inventory=inventory,
        )
        if receipt_result is not None:
            return receipt_result
        state = _legacy_migration_systemd_state(unit)
        if state.get("ActiveState") in {"active", "activating", "reloading"}:
            time.sleep(0.25)
            continue
        if not launched_for_current_release:
            _launch_legacy_migration_worker(root, inventory)
            launched_for_current_release = True
            time.sleep(0.05)
            continue
        receipt_result = _durable_migration_receipt_result(
            root,
            release_commit=release_commit,
            inventory=inventory,
        )
        if receipt_result is not None:
            return receipt_result
        raise H2IngestError(
            "Legacy-Migrationsworker endete ohne gültigen, releasegebundenen Abschlussbeleg."
        )


def import_scene(
    scene: str,
    *,
    source_root: pathlib.Path = DEFAULT_SOURCE_ROOT,
    library_root: pathlib.Path = DEFAULT_LIBRARY_ROOT,
) -> dict[str, Any]:
    source = _resolve_source_root(source_root)
    library = _resolve_library_root(library_root, source)
    descriptor = _open_library_import_lock(library)
    try:
        return _import_scene_locked(
            scene,
            source_root=source,
            library_root=library,
        )
    finally:
        os.close(descriptor)


def _import_scene_locked(
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
        existing = _read_manifest(final_dir, material_id)
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

    if _control_library_material_count(library) >= MAX_CONTROL_LIBRARY_ITEMS:
        raise H2IngestError(
            "H2-Control-Bibliothek hat ihr Material-Limit erreicht; "
            "neuer Import wird vor der Veröffentlichung abgewiesen."
        )

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


def _control_library_item(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("source")
    annotations = item.get("annotations")
    masters = item.get("masters")
    imported_at = item.get("imported_at")
    if (
        not isinstance(source, dict)
        or not isinstance(annotations, dict)
        or not isinstance(masters, list)
        or not masters
        or len(masters) > MAX_SESSION_FILES
        or not isinstance(imported_at, str)
        or len(imported_at) > 64
    ):
        raise H2IngestError("H2-Control-Bibliothek enthält ein ungültiges Objekt.")

    scene = source.get("scene")
    recorded_date = source.get("recorded_date")
    recorded_time = source.get("recorded_time")
    if (
        not isinstance(scene, str)
        or SCENE_RE.fullmatch(scene) is None
        or not isinstance(recorded_date, str)
        or len(recorded_date) > 10
        or not isinstance(recorded_time, str)
        or len(recorded_time) > 8
    ):
        raise H2IngestError("H2-Control-Bibliothek enthält ungültige Herkunftsdaten.")

    title = annotations.get("title")
    note = annotations.get("note")
    tags = annotations.get("tags")
    normalized_title = _validated_annotation_text(
        title, label="Titel", maximum=MAX_TITLE_CHARS
    )
    normalized_note = _validated_annotation_text(
        note, label="Notiz", maximum=MAX_NOTE_CHARS
    )
    normalized_tags = _validated_tags(tags)
    if (
        normalized_title != title
        or normalized_note != note
        or normalized_tags != tags
    ):
        raise H2IngestError("H2-Control-Bibliothek enthält nicht-kanonische Annotationen.")

    sizes: list[int] = []
    roles: set[str] = set()
    segment_indexes: list[int] = []
    for master in masters:
        if not isinstance(master, dict):
            raise H2IngestError("H2-Control-Bibliothek enthält ungültige Master.")
        size = master.get("bytes")
        role = master.get("role")
        segment_index = master.get("segment_index", 0)
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or size > (2**63 - 1)
            or role not in {"front", "rear", "mix"}
            or isinstance(segment_index, bool)
            or not isinstance(segment_index, int)
            or segment_index < 0
            or segment_index >= MAX_SESSION_FILES
        ):
            raise H2IngestError("H2-Control-Bibliothek enthält ungültige Master.")
        sizes.append(size)
        roles.add(role)
        segment_indexes.append(segment_index)

    return {
        "material_id": item["material_id"],
        "source": {
            "scene": scene,
            "recorded_date": recorded_date,
            "recorded_time": recorded_time,
        },
        "imported_at": imported_at,
        "annotations": {
            "title": title,
            "note": note,
            "tags": tags,
        },
        "roles": sorted(roles, key=lambda role: ROLE_ORDER[role.upper()]),
        "segment_count": max(segment_indexes) + 1,
        "total_bytes": sum(sizes),
        "max_file_bytes": max(sizes),
    }


def library(
    library_root: pathlib.Path = DEFAULT_LIBRARY_ROOT,
    *,
    projection: str = "full",
) -> dict[str, Any]:
    if projection not in {"full", "control"}:
        raise H2IngestError("Unbekannte H2-Bibliotheksprojektion.")
    root = library_root.expanduser()
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_material_library",
        "items": [],
        "count": 0,
        "read_only": True,
    }
    if projection == "control":
        result.update(
            {
                "projection": CONTROL_LIBRARY_PROJECTION,
                "total_count": 0,
                "truncated": False,
            }
        )
    if not root.exists() and not root.is_symlink():
        return result

    _lstat_directory(root, "Materialbibliothek")
    items: list[dict[str, Any]] = []
    if projection == "control":
        recent_candidates: list[tuple[int, str]] = []
        observed_items = 0
        with os.scandir(root) as entries:
            for entry in entries:
                if (
                    not entry.is_dir(follow_symlinks=False)
                    or not MATERIAL_ID_RE.fullmatch(entry.name)
                ):
                    continue
                observed_items += 1
                # The manifest is immutable after publication, so its mtime is
                # a bounded import-recency key without parsing every legacy manifest.
                manifest_metadata = _lstat_regular(
                    pathlib.Path(entry.path) / "manifest.json",
                    "Materialmanifest",
                )
                candidate = (manifest_metadata.st_mtime_ns, entry.name)
                if len(recent_candidates) < MAX_CONTROL_LIBRARY_ITEMS:
                    heapq.heappush(recent_candidates, candidate)
                elif candidate > recent_candidates[0]:
                    heapq.heapreplace(recent_candidates, candidate)
        for _manifest_mtime_ns, name in recent_candidates:
            directory = root / name
            manifest = _read_manifest(directory, name)
            annotations = _read_annotations(directory, name)
            item = _library_item(manifest, annotations, name)
            items.append(_control_library_item(item))
        result["total_count"] = observed_items
        result["truncated"] = observed_items > len(items)
    else:
        with os.scandir(root) as entries:
            for entry in entries:
                if (
                    not entry.is_dir(follow_symlinks=False)
                    or not MATERIAL_ID_RE.fullmatch(entry.name)
                ):
                    continue
                directory = pathlib.Path(entry.path)
                manifest = _read_manifest(directory, entry.name)
                annotations = _read_annotations(directory, entry.name)
                items.append(_library_item(manifest, annotations, entry.name))

    items.sort(key=lambda item: item["imported_at"], reverse=True)
    result["items"] = items
    result["count"] = len(items)
    return result


def verify_material(
    material_id: str, *, library_root: pathlib.Path = DEFAULT_LIBRARY_ROOT
) -> dict[str, Any]:
    if not MATERIAL_ID_RE.fullmatch(material_id):
        raise H2IngestError("Ungültige Material-ID.")
    directory = library_root.expanduser() / material_id
    _lstat_directory(directory, "Materialobjekt")
    manifest = _read_manifest(directory, material_id)
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
    if len(payload) > MAX_METADATA_JSON_BYTES:
        raise H2IngestError("Metadatendatei überschreitet das sichere Größenlimit.")
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
    manifest = _read_manifest(directory, material_id)
    annotations_path = directory / "annotations.json"
    annotations_metadata = _lstat_regular(annotations_path, "Materialannotation")
    current = _read_annotations(directory, material_id)
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
        if annotations_metadata.st_size <= MAX_METADATA_JSON_BYTES:
            _write_json_replace(annotations_path, updated, 0o600)
        else:
            control_path = directory / LEGACY_ANNOTATIONS_CONTROL_NAME
            control = _read_json_regular(control_path)
            _annotations_from_legacy_control(
                control,
                material_id,
                annotations_metadata=annotations_metadata,
            )
            control["title"] = updated["title"]
            control["note"] = updated["note"]
            control["tags"] = updated["tags"]
            control["updated_at"] = updated["updated_at"]
            _write_json_replace(control_path, control, 0o600)
        observed = _read_annotations(directory, material_id)
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
    manifest = _read_manifest(directory, material_id)
    annotations = _read_annotations(directory, material_id)
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
    scan_parser.add_argument(
        "--projection",
        choices=("full", "control", "budget"),
        default="full",
    )

    import_parser = sub.add_parser("import")
    import_parser.add_argument("scene")
    import_parser.add_argument("--source-root", type=pathlib.Path, default=DEFAULT_SOURCE_ROOT)
    import_parser.add_argument("--library-root", type=pathlib.Path, default=DEFAULT_LIBRARY_ROOT)

    library_parser = sub.add_parser("library")
    library_parser.add_argument("--library-root", type=pathlib.Path, default=DEFAULT_LIBRARY_ROOT)
    library_parser.add_argument(
        "--projection",
        choices=("full", "control"),
        default="full",
    )

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

    migrate_parser = sub.add_parser("migrate-legacy-manifests")
    migrate_parser.add_argument(
        "--library-root",
        type=pathlib.Path,
        default=DEFAULT_LIBRARY_ROOT,
    )
    migrate_parser.add_argument(
        "--launch-only",
        action="store_true",
        help="schedule a durable migration worker without waiting for completion",
    )
    worker_parser = sub.add_parser(
        "_migrate-legacy-manifests-worker",
        help=argparse.SUPPRESS,
    )
    worker_parser.add_argument(
        "--library-root",
        type=pathlib.Path,
        required=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "scan":
            result = scan(args.source_root, projection=args.projection)
        elif args.command == "import":
            result = import_scene(
                args.scene,
                source_root=args.source_root,
                library_root=args.library_root,
            )
        elif args.command == "library":
            result = library(args.library_root, projection=args.projection)
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
        elif args.command == "material-media":
            result = material_media(
                args.material_id,
                args.segment_index,
                library_root=args.library_root,
            )
        elif args.command == "migrate-legacy-manifests":
            if args.launch_only:
                result = launch_legacy_manifests_durable(args.library_root)
            else:
                result = migrate_legacy_manifests_durable(args.library_root)
        else:
            result = _run_legacy_migration_worker(args.library_root)
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
