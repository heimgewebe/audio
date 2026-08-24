#!/usr/bin/env python3
"""Plan and manage fail-closed, bounded audio recording sessions."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import resource
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import wave
from typing import Any, Callable, Iterator

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "profiles" / "recording-sessions.v1.json"
PROFILE_PATH = ROOT / "profiles" / "audio-profiles.v1.json"
PHYSICAL_PATH = ROOT / "scripts" / "physical_verification.py"
LAB_PATH = ROOT / "scripts" / "laboratory_gate.py"
VOICE_PATH = ROOT / "scripts" / "voice_capture_observer.py"
RATE_PATH = ROOT / "scripts" / "rate_policy_observer.py"
PRODUCTION_MIX_PATH = ROOT / "scripts" / "production_mix.py"
PRODUCTION_MIX_WRAPPER_PATH = ROOT / "scripts" / "audio-production-mix"
PRODUCTION_MIX_CONTRACT_PATH = ROOT / "profiles" / "production-mix-graph.v1.json"
MIDI_CAPTURE_PATH = ROOT / "scripts" / "roland_midi_capture.py"
WRAPPER_PATH = ROOT / "scripts" / "audio-record"
PARECORD_PATH = pathlib.Path("/usr/bin/parecord")
ARECORDMIDI_PATH = pathlib.Path("/usr/bin/arecordmidi")
FFMPEG_PATH = pathlib.Path("/usr/bin/ffmpeg")
MAX_AUDIO_SPAWN_SPREAD_NS = 5_000_000
MAX_AUDIO_FRAME_DIFFERENCE_FRAMES = 4_800  # 100 ms at 48 kHz
MAX_JSON_BYTES = 524_288
MAX_BINDING_BYTES = 64_000_000
PARECORD_WAV_FSIZE_FLOOR_BYTES = 64 * 1024 * 1024
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
SESSION_ID_RE = re.compile(r"[0-9a-f]{24}")
SOURCE_SPEC_RE = re.compile(
    r"^(?P<format>[A-Za-z0-9_-]+) (?P<channels>[0-9]+)ch (?P<rate>[0-9]+)Hz$"
)
SESSION_TYPES = (
    "voice-recording",
    "piano-vocal-performance",
    "roland-audio-recording",
    "production-mix-recording",
)
ARTIFACT_DETAIL_FIELDS = frozenset(
    {"channels", "bit_depth_container", "sample_rate_hz", "frames", "duration_seconds"}
)
DEFAULT_OUTPUT_ROOT = pathlib.Path(
    os.environ.get(
        "AUDIO_RECORDING_ROOT", pathlib.Path.home() / "Music" / "Audio-Aufnahmen"
    )
)
DEFAULT_STATE_ROOT = (
    pathlib.Path(os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state"))
    / "audio"
    / "recordings-v1"
)


class RecordingError(RuntimeError):
    """A fail-closed recording contract error."""


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RecordingError(f"module cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PHYSICAL = load_module("physical_verification_for_recording", PHYSICAL_PATH)
LAB = load_module("laboratory_gate_for_recording", LAB_PATH)
VOICE = load_module("voice_capture_observer_for_recording", VOICE_PATH)
RATE = load_module("rate_policy_observer_for_recording", RATE_PATH)
_PRODUCTION_MIX_MODULE: Any | None = None
MIDI = load_module("roland_midi_capture_for_recording", MIDI_CAPTURE_PATH)


def _production_mix_module():
    global _PRODUCTION_MIX_MODULE
    if _PRODUCTION_MIX_MODULE is None:
        _PRODUCTION_MIX_MODULE = load_module(
            "production_mix_for_recording", PRODUCTION_MIX_PATH
        )
    return _PRODUCTION_MIX_MODULE


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def lexical_absolute(path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.path.expanduser(str(path))))


def _plain_wav_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 120
        or CONTROL_RE.search(value)
        or pathlib.PurePath(value).name != value
        or value.startswith(".")
        or not value.casefold().endswith(".wav")
        or value.casefold() == "active.wav"
    ):
        raise RecordingError(
            "recording name must be one visible plain filename ending in .wav"
        )
    return value


def _read_bound_regular(
    path: pathlib.Path,
    *,
    maximum_bytes: int,
    require_private: bool,
    capture_content: bool,
    include_identity: bool = False,
) -> tuple[dict[str, Any], bytes | None]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RecordingError(f"regular file cannot be opened safely: {path}") from exc
    digest = hashlib.sha256()
    total = 0
    chunks: list[bytes] | None = [] if capture_content else None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RecordingError(f"path is not a regular file: {path}")
        if before.st_size < 1 or before.st_size > maximum_bytes:
            raise RecordingError(f"file size is outside its bound: {path}")
        if require_private and stat.S_IMODE(before.st_mode) != 0o600:
            raise RecordingError(f"private file must have mode 0600: {path}")
        while chunk := os.read(descriptor, 1_048_576):
            total += len(chunk)
            if total > maximum_bytes:
                raise RecordingError(f"file grew beyond its bound: {path}")
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, item) != getattr(after, item) for item in stable):
            raise RecordingError(f"file changed while it was read: {path}")
        if total != before.st_size:
            raise RecordingError(f"file size changed while it was read: {path}")
        binding = {
            "path": str(path),
            "sha256": digest.hexdigest(),
            "bytes": total,
            "mode": f"{stat.S_IMODE(before.st_mode):04o}",
        }
        if include_identity:
            binding.update({"device": before.st_dev, "inode": before.st_ino})
        return binding, (b"".join(chunks) if chunks is not None else None)
    finally:
        os.close(descriptor)


def _safe_regular_binding(
    path: pathlib.Path,
    *,
    maximum_bytes: int = MAX_BINDING_BYTES,
    require_private: bool = False,
    include_identity: bool = False,
) -> dict[str, Any]:
    binding, _content = _read_bound_regular(
        path,
        maximum_bytes=maximum_bytes,
        require_private=require_private,
        capture_content=False,
        include_identity=include_identity,
    )
    return binding


def _safe_json_read_with_binding(
    path: pathlib.Path,
    *,
    maximum_bytes: int = MAX_JSON_BYTES,
    require_private: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    binding, content = _read_bound_regular(
        path,
        maximum_bytes=maximum_bytes,
        require_private=require_private,
        capture_content=True,
    )
    if content is None:
        raise RecordingError(f"JSON snapshot was not captured: {path}")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecordingError(f"invalid UTF-8 JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise RecordingError(f"expected JSON object: {path}")
    return value, binding


def _safe_json_read(
    path: pathlib.Path,
    *,
    maximum_bytes: int = MAX_JSON_BYTES,
    require_private: bool = False,
) -> dict[str, Any]:
    value, _binding = _safe_json_read_with_binding(
        path,
        maximum_bytes=maximum_bytes,
        require_private=require_private,
    )
    return value


def _atomic_private_json(
    path: pathlib.Path, payload: dict[str, Any], *, create_only: bool = False
) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise RecordingError("recording state exceeds its byte limit")
    ensure_private_directory(path.parent)
    if path.is_symlink():
        raise RecordingError(f"private state path must not be a symbolic link: {path}")
    if create_only and path.exists():
        raise RecordingError(f"private state already exists: {path}")
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = pathlib.Path(handle.name)
        temporary.chmod(0o600)
        if create_only:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise RecordingError(f"private state already exists: {path}") from exc
            temporary.unlink()
            temporary = None
        else:
            temporary.replace(path)
            temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _check_directory_chain(
    path: pathlib.Path, *, allow_missing_leaf: bool = False
) -> None:
    absolute = lexical_absolute(path)
    current = pathlib.Path(absolute.anchor)
    parts = absolute.parts[1:]
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and index == len(parts) - 1:
                return
            raise RecordingError(f"directory path does not exist: {current}")
        if stat.S_ISLNK(metadata.st_mode):
            raise RecordingError(
                f"symbolic directory component is forbidden: {current}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise RecordingError(f"path component is not a directory: {current}")


def ensure_private_directory(path: pathlib.Path) -> pathlib.Path:
    absolute = lexical_absolute(path)
    current = pathlib.Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        created = False
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            metadata = current.lstat()
            created = True
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RecordingError(f"unsafe directory component: {current}")
        if created and metadata.st_uid != os.getuid():
            raise RecordingError(f"created directory has the wrong owner: {current}")
    final_metadata = absolute.lstat()
    if final_metadata.st_uid != os.getuid():
        raise RecordingError(
            f"private directory is not owned by the current user: {absolute}"
        )
    absolute.chmod(0o700)
    return absolute


def _validate_output_root(path: pathlib.Path) -> pathlib.Path:
    root = lexical_absolute(path)
    _check_directory_chain(root)
    metadata = root.stat()
    if metadata.st_uid != os.getuid():
        raise RecordingError("recording root is not owned by the current user")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o700:
        raise RecordingError("recording root must have private mode 0700")
    return root


def _validate_session_contract(session_type: str, session: Any) -> None:
    required = {
        "purpose",
        "profile",
        "required_physical_facts",
        "required_laboratory_gates",
        "advisory_laboratory_gates",
        "capture",
        "source",
        "monitoring",
        "process",
    }
    if not isinstance(session, dict) or set(session) != required:
        raise RecordingError(f"recording contract fields are invalid: {session_type}")
    if (
        not isinstance(session.get("purpose"), str)
        or not session["purpose"]
        or not isinstance(session.get("profile"), str)
        or not session["profile"]
        or not isinstance(session.get("required_physical_facts"), dict)
        or not isinstance(session.get("required_laboratory_gates"), list)
        or not isinstance(session.get("advisory_laboratory_gates"), list)
        or any(
            not isinstance(gate, str) or not gate or CONTROL_RE.search(gate)
            for gate in (
                session["required_laboratory_gates"]
                + session["advisory_laboratory_gates"]
            )
        )
        or len(set(session["required_laboratory_gates"]))
        != len(session["required_laboratory_gates"])
        or len(set(session["advisory_laboratory_gates"]))
        != len(session["advisory_laboratory_gates"])
        or set(session["required_laboratory_gates"])
        & set(session["advisory_laboratory_gates"])
    ):
        raise RecordingError(f"recording contract metadata is invalid: {session_type}")
    capture = session.get("capture")
    capture_fields = {
        "sample_rate_hz",
        "sample_format",
        "channels",
        "channel_map",
        "container",
        "minimum_duration_seconds",
        "maximum_duration_seconds",
        "startup_timeout_seconds",
        "stop_grace_seconds",
        "bytes_per_sample",
        "header_and_metadata_allowance_bytes",
        "free_space_reserve_bytes",
        "maximum_stderr_bytes",
    }
    if (
        not isinstance(capture, dict)
        or set(capture) != capture_fields
        or capture.get("sample_rate_hz") != 48_000
        or capture.get("sample_format") != "s32le"
        or capture.get("channels") != 2
        or capture.get("channel_map") != "front-left,front-right"
        or capture.get("container") != "wav"
        or capture.get("bytes_per_sample") != 4
        or capture.get("minimum_duration_seconds") != 1
        or capture.get("maximum_duration_seconds") != 14_400
        or not _positive_integer(capture.get("startup_timeout_seconds"))
        or not _positive_integer(capture.get("stop_grace_seconds"))
        or not _positive_integer(capture.get("header_and_metadata_allowance_bytes"))
        or not _positive_integer(capture.get("free_space_reserve_bytes"))
        or not _positive_integer(capture.get("maximum_stderr_bytes"))
    ):
        raise RecordingError(f"recording capture contract is invalid: {session_type}")
    source = session.get("source")
    if not isinstance(source, dict):
        raise RecordingError(f"recording source contract is invalid: {session_type}")
    kind = source.get("kind")
    if kind == "motu-voice-with-roland-audio-and-midi":
        expected_fields = {"kind", "audio", "roland_audio", "midi"}
        audio = source.get("audio")
        roland_audio = source.get("roland_audio")
        midi = source.get("midi")
        if (
            set(source) != expected_fields
            or session_type != "piano-vocal-performance"
            or not isinstance(audio, dict)
            or set(audio)
            != {
                "kind",
                "vendor_id",
                "product_id",
                "serial_prefix",
                "node_name_prefix",
                "required_sample_formats",
                "required_sample_rate_hz",
                "required_channels",
                "requires_unmuted",
                "requires_unity_volume",
            }
            or audio.get("kind") != "motu-voice"
            or not isinstance(roland_audio, dict)
            or set(roland_audio)
            != {
                "kind",
                "device",
                "vendor_id",
                "product_id",
                "node_name_prefix",
                "required_sample_formats",
                "required_sample_rate_hz",
                "required_channels",
                "requires_unmuted",
                "requires_unity_volume",
            }
            or roland_audio.get("kind") != "usb-audio"
            or roland_audio.get("device") != "roland_fp_30x"
            or roland_audio.get("vendor_id") != "0582"
            or roland_audio.get("product_id") != "01b1"
            or roland_audio.get("required_sample_rate_hz") != 44_100
            or sorted(roland_audio.get("required_sample_formats", [])) != ["s24le", "s32le"]
            or roland_audio.get("required_channels") != 2
            or roland_audio.get("requires_unmuted") is not True
            or roland_audio.get("requires_unity_volume") is not True
            or not isinstance(midi, dict)
            or set(midi)
            != {
                "kind",
                "manufacturer",
                "model",
                "usb_vendor_id",
                "usb_product_id",
                "capture",
                "port_inventory",
                "timing",
            }
            or midi.get("kind") != "alsa-sequencer-midi"
            or midi.get("manufacturer") != "Roland"
            or midi.get("model") != "FP-30X"
            or midi.get("usb_vendor_id") != "0582"
            or midi.get("usb_product_id") != "01b1"
            or midi.get("capture") != "arecordmidi-standard-midi-file"
            or midi.get("port_inventory")
            != "kernel-sequencer-plus-arecordmidi-list"
            or midi.get("timing")
            != {
                "basis": "SMPTE",
                "fps": 25,
                "ticks_per_frame": 40,
                "nominal_resolution_ms": 1,
            }
        ):
            raise RecordingError("performance recording source contract is inconsistent")
    else:
        audio = None
        roland_audio = None
        midi = None
    source_fields = {
        "motu-voice": {
            "kind",
            "vendor_id",
            "product_id",
            "serial_prefix",
            "node_name_prefix",
            "required_sample_formats",
            "required_sample_rate_hz",
            "required_channels",
            "requires_unmuted",
            "requires_unity_volume",
        },
        "usb-audio": {
            "kind",
            "device",
            "vendor_id",
            "product_id",
            "node_name_prefix",
            "required_sample_formats",
            "required_sample_rate_hz",
            "required_channels",
            "requires_unmuted",
            "requires_unity_volume",
        },
        "named-pipewire-source": {
            "kind",
            "node_name",
            "upstream_roles",
            "required_sample_formats",
            "required_sample_rate_hz",
            "required_channels",
            "requires_unmuted",
            "requires_unity_volume",
        },
    }
    if kind != "motu-voice-with-roland-audio-and-midi" and (
        kind not in source_fields or set(source) != source_fields[kind]
    ):
        raise RecordingError(f"recording source fields are invalid: {session_type}")
    format_source = audio if kind == "motu-voice-with-roland-audio-and-midi" else source
    assert isinstance(format_source, dict)
    formats = format_source.get("required_sample_formats")
    if (
        not isinstance(formats, list)
        or not formats
        or any(not isinstance(item, str) or not item for item in formats)
        or not _positive_integer(format_source.get("required_sample_rate_hz"))
        or not _positive_integer(format_source.get("required_channels"))
        or format_source.get("requires_unmuted") is not True
        or format_source.get("requires_unity_volume") is not True
    ):
        raise RecordingError(
            f"recording source format contract is invalid: {session_type}"
        )
    monitoring = session.get("monitoring")
    if (
        not isinstance(monitoring, dict)
        or set(monitoring) != {"mode", "endpoint", "software_loopback", "level_claim"}
        or monitoring.get("mode") not in {"hardware-direct", "software-monitoring"}
        or not isinstance(monitoring.get("endpoint"), str)
        or not monitoring["endpoint"]
        or monitoring.get("software_loopback") is not False
        or not isinstance(monitoring.get("level_claim"), str)
        or not monitoring["level_claim"]
    ):
        raise RecordingError(
            f"recording monitoring contract is invalid: {session_type}"
        )
    process = session.get("process")
    process_fields = {
        "stdin",
        "stdout",
        "stderr",
        "new_session",
        "core_dumps",
        "maximum_open_files",
        "partial_file_policy",
        "publication_policy",
        "client_name",
        "stream_name_prefix",
    }
    if (
        not isinstance(process, dict)
        or set(process) != process_fields
        or process.get("stdin") != "devnull"
        or process.get("stdout") != "devnull"
        or process.get("stderr") != "bounded-private-temporary-file"
        or process.get("new_session") is not True
        or process.get("core_dumps") is not False
        or process.get("maximum_open_files") != 64
        or process.get("partial_file_policy") != "preserve-on-failure"
        or process.get("publication_policy")
        != (
            "manifest-last-hardlink-no-replace"
            if session_type == "piano-vocal-performance"
            else "same-directory-hardlink-no-replace"
        )
        or not isinstance(process.get("client_name"), str)
        or not process["client_name"]
        or not isinstance(process.get("stream_name_prefix"), str)
        or not process["stream_name_prefix"]
    ):
        raise RecordingError(f"recording process contract is invalid: {session_type}")
    expected = {
        "voice-recording": (
            "motu-voice",
            [],
            ["voice-level-measurement"],
        ),
        "piano-vocal-performance": (
            "motu-voice-with-roland-audio-and-midi",
            ["resampling-decision"],
            ["voice-level-measurement"],
        ),
        "roland-audio-recording": ("usb-audio", ["resampling-decision"], []),
        "production-mix-recording": ("named-pipewire-source", [], []),
    }
    if (
        kind,
        session["required_laboratory_gates"],
        session["advisory_laboratory_gates"],
    ) != expected[session_type]:
        raise RecordingError(
            f"recording session specialization is invalid: {session_type}"
        )
    if session_type == "voice-recording" and (
        source.get("vendor_id") != "07fd"
        or source.get("product_id") != "0008"
        or source.get("required_sample_rate_hz") != 48_000
        or formats != ["s32le"]
    ):
        raise RecordingError("voice recording source contract is inconsistent")
    if session_type == "piano-vocal-performance" and (
        not isinstance(audio, dict)
        or audio.get("vendor_id") != "07fd"
        or audio.get("product_id") != "0008"
        or audio.get("required_sample_rate_hz") != 48_000
        or formats != ["s32le"]
        or not isinstance(roland_audio, dict)
        or roland_audio.get("vendor_id") != "0582"
        or roland_audio.get("product_id") != "01b1"
        or roland_audio.get("required_sample_rate_hz") != 44_100
        or sorted(roland_audio.get("required_sample_formats", [])) != ["s24le", "s32le"]
        or session["required_physical_facts"]
        != {
            "rode_nt1a_connected": True,
            "rode_nt1a_motu_input": ["input-1", "input-2"],
            "motu_phantom_48v": "on",
            "motu_input_gain_reference": "non-empty-string",
        }
    ):
        raise RecordingError("performance recording gates are inconsistent")
    if session_type == "roland-audio-recording" and (
        source.get("device") != "roland_fp_30x"
        or source.get("vendor_id") != "0582"
        or source.get("product_id") != "01b1"
        or source.get("required_sample_rate_hz") != 44_100
        or sorted(formats) != ["s24le", "s32le"]
    ):
        raise RecordingError("Roland recording source contract is inconsistent")
    if session_type == "production-mix-recording" and (
        source.get("node_name") != "audio-production-mix"
        or source.get("required_sample_rate_hz") != 48_000
        or formats != ["s32le"]
        or source.get("upstream_roles") != ["voice", "roland", "software-instrument"]
    ):
        raise RecordingError("production recording source contract is inconsistent")


def load_catalog(session_type: str = "voice-recording") -> dict[str, Any]:
    payload = _safe_json_read(CONTRACT_PATH, maximum_bytes=262_144)
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "audio_recording_session_catalog"
        or set(payload) != {"schema_version", "kind", "sessions", "rules"}
    ):
        raise RecordingError("recording-session catalog schema is invalid")
    sessions = payload.get("sessions")
    if not isinstance(sessions, dict) or set(sessions) != set(SESSION_TYPES):
        raise RecordingError("recording-session catalog has an invalid session set")
    for name in SESSION_TYPES:
        _validate_session_contract(name, sessions[name])
    profile_catalog = _safe_json_read(PROFILE_PATH, maximum_bytes=262_144)
    profiles = profile_catalog.get("profiles")
    if (
        profile_catalog.get("schema_version") != 1
        or profile_catalog.get("kind") != "audio_profile_catalog"
        or set(profile_catalog) != {"schema_version", "kind", "profiles"}
        or not isinstance(profiles, dict)
        or any(not isinstance(name, str) or not name for name in profiles)
    ):
        raise RecordingError("audio profile catalog schema is invalid")
    missing_profiles = sorted(
        name
        for name in {session["profile"] for session in sessions.values()}
        if name not in profiles
    )
    if missing_profiles:
        raise RecordingError(
            "recording sessions reference unknown audio profiles: "
            + ", ".join(missing_profiles)
        )
    if session_type not in sessions:
        raise RecordingError(f"recording session type is unsupported: {session_type}")
    return sessions[session_type]


def contract_bindings() -> list[dict[str, Any]]:
    paths = (
        CONTRACT_PATH,
        pathlib.Path(__file__).resolve(),
        WRAPPER_PATH,
        PHYSICAL_PATH,
        ROOT / "inventory" / "physical-facts.v1.json",
        ROOT / "inventory" / "physical-verification.v1.json",
        LAB_PATH,
        ROOT / "inventory" / "laboratory-gates.v1.json",
        PROFILE_PATH,
        VOICE_PATH,
        RATE_PATH,
        PRODUCTION_MIX_PATH,
        PRODUCTION_MIX_WRAPPER_PATH,
        PRODUCTION_MIX_CONTRACT_PATH,
        MIDI_CAPTURE_PATH,
    )
    return [
        _safe_regular_binding(path, maximum_bytes=MAX_BINDING_BYTES) for path in paths
    ]


def parecord_binding(path: pathlib.Path = PARECORD_PATH) -> dict[str, Any]:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RecordingError(f"recording executable is unavailable: {path}")
    metadata = path.lstat()
    link_target: str | None = None
    resolved = path
    if stat.S_ISLNK(metadata.st_mode):
        link_target = os.readlink(path)
        resolved = path.resolve(strict=True)
    binding = _safe_regular_binding(resolved, maximum_bytes=MAX_BINDING_BYTES)
    report = {
        "launcher": str(path),
        "launcher_symlink_target": link_target,
        "resolved": binding,
    }
    return report


def arecordmidi_binding(path: pathlib.Path = ARECORDMIDI_PATH) -> dict[str, Any]:
    """Bind the exact locally verified arecordmidi launcher and binary digest."""

    return parecord_binding(path)


def ffmpeg_binding(path: pathlib.Path = FFMPEG_PATH) -> dict[str, Any]:
    """Bind the offline mixer executable used by modern performance takes."""

    return parecord_binding(path)


def _read_optional_state(
    path: pathlib.Path,
    *,
    maximum_bytes: int,
    empty_factory: Callable[[], dict[str, Any]],
    validator: Callable[[pathlib.Path, dict[str, Any]], None],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not path.exists() and not path.is_symlink():
        return empty_factory(), None
    state, binding = _safe_json_read_with_binding(
        path, maximum_bytes=maximum_bytes, require_private=True
    )
    validator(path, state)
    return state, binding


def _require_sha256_provenance(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX64_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase hexadecimal SHA-256")
    return value


def _read_required_physical_state(
    path: pathlib.Path, requirements: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not path.exists() and not path.is_symlink():
        return PHYSICAL.empty_state(), None
    state, binding = _safe_json_read_with_binding(
        path, maximum_bytes=PHYSICAL.MAX_STATE_BYTES, require_private=True
    )
    if (
        state.get("schema_version") != 1
        or state.get("kind") != "physical_audio_observation"
    ):
        raise ValueError("physical observation state has the wrong schema or kind")
    _require_sha256_provenance(
        state.get("catalog_sha256"), "physical fact catalog provenance"
    )
    if state.get("template_sha256") != PHYSICAL.sha256_file(PHYSICAL.TEMPLATE_PATH):
        raise ValueError(
            "physical verification template changed; review observations before reuse"
        )
    facts = state.get("facts")
    if not isinstance(facts, dict):
        raise ValueError("physical observation state has no facts object")
    catalog_payload = PHYSICAL.load_json(PHYSICAL.CATALOG_PATH)
    catalog = catalog_payload.get("facts", {})
    if not isinstance(catalog, dict):
        raise RecordingError("physical fact catalog has no facts object")
    unknown_required = sorted(set(requirements) - set(catalog))
    if unknown_required:
        raise RecordingError(
            "recording contract references unknown physical facts: "
            + ", ".join(unknown_required)
        )
    observed_times: list[dt.datetime] = []
    for key in requirements:
        item = facts.get(key)
        if item is None:
            continue
        if not isinstance(item, dict):
            raise ValueError(f"stored required fact is not an object: {key}")
        spec = catalog[key]
        evidence = item.get("evidence")
        if evidence not in spec.get("allowed_evidence", []):
            raise ValueError(f"stored required fact has invalid evidence: {key}")
        if item.get("authority") != "explicit-human-observation":
            raise ValueError(f"stored required fact has invalid authority: {key}")
        observed_times.append(
            PHYSICAL.parse_timestamp(
                item.get("observed_at"), f"stored required fact timestamp: {key}"
            )
        )
        PHYSICAL.validate_stored_value(spec, item.get("value"))
    updated_at = state.get("updated_at")
    if updated_at is None:
        if observed_times:
            raise ValueError("physical observation state has required facts but no updated_at")
    else:
        updated = PHYSICAL.parse_timestamp(updated_at, "physical observation updated_at")
        if observed_times and updated < max(observed_times):
            raise ValueError(
                "physical observation updated_at predates a required stored fact"
            )
    return state, binding


def _physical_projection(
    path: pathlib.Path, requirements: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    try:
        state, binding = _read_required_physical_state(path, requirements)
    except (OSError, RecordingError, ValueError) as exc:
        return {
            "state_path": str(path),
            "state_sha256": None,
            "facts": {},
            "error": str(exc),
        }, ["physical-state-invalid"]
    facts = state.get("facts", {})
    resolved = {
        key: item.get("value")
        for key, item in facts.items()
        if key in requirements and isinstance(item, dict)
    }
    for key, expected in requirements.items():
        value = resolved.get(key)
        if expected == "non-empty-string":
            valid = isinstance(value, str) and bool(value.strip())
        elif isinstance(expected, list):
            valid = value in expected
        else:
            valid = value == expected
        if not valid:
            blockers.append(f"physical-fact:{key}")
    return {
        "state_path": str(path),
        "state_sha256": binding["sha256"] if binding is not None else None,
        "facts": {key: resolved.get(key) for key in sorted(requirements)},
        "error": None,
    }, blockers


def _read_required_laboratory_state(
    path: pathlib.Path, required: list[str]
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, dict[str, Any]]]:
    if not path.exists() and not path.is_symlink():
        return LAB.empty_state(), None, {}
    state, binding = _safe_json_read_with_binding(
        path, maximum_bytes=LAB.MAX_STATE_BYTES, require_private=True
    )
    if (
        state.get("schema_version") != 1
        or state.get("kind") != "audio_laboratory_gate_state"
    ):
        raise ValueError("laboratory gate state has the wrong schema or kind")
    _require_sha256_provenance(
        state.get("catalog_sha256"), "laboratory gate catalog provenance"
    )
    _require_sha256_provenance(
        state.get("profile_catalog_sha256"), "audio profile catalog provenance"
    )
    gates = state.get("gates")
    if not isinstance(gates, dict):
        raise ValueError("laboratory gate state has no gates object")
    catalog = LAB.load_catalog()
    unknown_required = sorted(set(required) - set(catalog))
    if unknown_required:
        raise RecordingError(
            "recording contract references unknown laboratory gates: "
            + ", ".join(unknown_required)
        )
    selected: dict[str, dict[str, Any]] = {}
    recorded_times: list[dt.datetime] = []
    for gate in required:
        receipt = gates.get(gate)
        if receipt is None:
            continue
        if not isinstance(receipt, dict):
            raise ValueError(f"required laboratory receipt is not an object: {gate}")
        if receipt.get("status") != "passed":
            raise ValueError(f"required laboratory receipt is not passed: {gate}")
        evidence = receipt.get("evidence")
        if not isinstance(evidence, dict):
            raise ValueError(f"required laboratory receipt has no evidence: {gate}")
        LAB.validate_evidence(
            gate,
            evidence,
            allow_legacy_voice=True,
            allow_legacy_policy=True,
            allow_stale_policy=True,
            allow_legacy_xrun=True,
            allow_legacy_plugin_host=True,
            allow_legacy_qobuz=True,
        )
        if receipt.get("evidence_sha256") != LAB.canonical_sha256(evidence):
            raise ValueError(f"required laboratory evidence digest mismatch: {gate}")
        recorded_times.append(
            LAB.parse_timestamp(
                receipt.get("recorded_at"), f"recorded_at: required gate {gate}"
            )
        )
        expected_binding = catalog[gate].get("binds_physical_state") is True
        physical_binding = receipt.get("physical_state_sha256")
        if expected_binding:
            _require_sha256_provenance(
                physical_binding, f"physical-state binding: required gate {gate}"
            )
        elif physical_binding is not None:
            raise ValueError(
                f"unbound required gate unexpectedly stores physical-state binding: {gate}"
            )
        selected[gate] = receipt
    updated_at = state.get("updated_at")
    if updated_at is None:
        if recorded_times:
            raise ValueError("laboratory state has required receipts but no updated_at")
    else:
        updated = LAB.parse_timestamp(updated_at, "laboratory updated_at")
        if recorded_times and updated < max(recorded_times):
            raise ValueError("laboratory updated_at predates a required receipt")
    return state, binding, selected


def _resolve_required_laboratory_gates(
    receipts: dict[str, dict[str, Any]],
    required: list[str],
    physical_state_sha256: str | None,
) -> tuple[set[str], dict[str, str]]:
    resolved: set[str] = set()
    invalidated: dict[str, str] = {}
    catalog = LAB.load_catalog()
    for gate in required:
        receipt = receipts.get(gate)
        if receipt is None:
            invalidated[gate] = "missing"
            continue
        evidence = receipt["evidence"]
        if gate == "voice-level-measurement" and not LAB.has_bound_voice_capture(evidence):
            invalidated[gate] = "legacy-unbound-voice-evidence"
            continue
        if gate in LAB.RATE_POLICY_DECISIONS:
            if not LAB.has_bound_policy_decision(evidence):
                invalidated[gate] = "legacy-unbound-policy-evidence"
                continue
            if not LAB.policy_decision_binding_current(evidence):
                invalidated[gate] = "policy-binding-changed"
                continue
        if gate == "xrun-stability-test" and not LAB.has_bound_xrun_observation(evidence):
            invalidated[gate] = "legacy-unbound-xrun-evidence"
            continue
        if gate == "managed-plugin-host-proof" and not LAB.has_bound_plugin_host_observation(evidence):
            invalidated[gate] = "legacy-unbound-plugin-host-evidence"
            continue
        if gate == "qobuz-rate-proof" and not LAB.has_bound_qobuz_observation(evidence):
            invalidated[gate] = "legacy-unbound-qobuz-evidence"
            continue
        if catalog[gate].get("binds_physical_state") is True:
            if physical_state_sha256 is None:
                invalidated[gate] = "physical-state-missing"
                continue
            if receipt.get("physical_state_sha256") != physical_state_sha256:
                invalidated[gate] = "physical-state-changed"
                continue
        resolved.add(gate)
    return resolved, invalidated


def _laboratory_projection(
    path: pathlib.Path, physical: dict[str, Any], required: list[str]
) -> tuple[dict[str, Any], list[str]]:
    try:
        state, binding, receipts = _read_required_laboratory_state(path, required)
        resolved_all, invalidated_all = _resolve_required_laboratory_gates(
            receipts, required, physical.get("state_sha256")
        )
    except (KeyError, OSError, RecordingError, ValueError) as exc:
        return {
            "state_path": str(path),
            "state_sha256": None,
            "resolved": [],
            "invalidated": {},
            "receipt_sha256": {},
            "error": str(exc),
        }, ["laboratory-state-invalid"]
    resolved = {gate for gate in required if gate in resolved_all}
    invalidated = {
        gate: invalidated_all.get(gate, "missing")
        for gate in required
        if gate not in resolved
    }
    blockers = [f"laboratory-gate:{gate}" for gate in required if gate not in resolved]
    receipt_sha = {
        gate: canonical_sha256(receipts[gate])
        for gate in required
        if isinstance(receipts.get(gate), dict)
    }
    return {
        "state_path": str(path),
        "state_sha256": binding["sha256"] if binding is not None else None,
        "resolved": sorted(resolved),
        "invalidated": invalidated,
        "receipt_sha256": receipt_sha,
        "error": None,
    }, blockers


def _normalize_usb_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold().removeprefix("0x")
    return normalized if re.fullmatch(r"[0-9a-f]{4}", normalized) else None


def _source_label(contract: dict[str, Any]) -> str:
    return {
        "motu-voice": "motu",
        "usb-audio": "roland",
        "named-pipewire-source": "production-mix",
        "motu-voice-with-roland-audio-and-midi": "performance",
    }.get(str(contract.get("kind")), "recording")


def _pactl_source_identity(
    item: Any, contract: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(item, dict):
        raise ValueError("pactl source item is not an object")
    properties = item.get("properties")
    if not isinstance(properties, dict):
        return None, None
    if (
        item.get("monitor_source") not in {None, ""}
        or properties.get("device.class") == "monitor"
        or properties.get("media.class") not in {None, "Audio/Source"}
    ):
        return None, None
    source_name = item.get("name")
    if (
        not isinstance(source_name, str)
        or not source_name
        or CONTROL_RE.search(source_name)
    ):
        return None, None
    kind = contract.get("kind")
    vendor_id = _normalize_usb_id(properties.get("device.vendor.id"))
    product_id = _normalize_usb_id(properties.get("device.product.id"))
    if kind == "usb-audio":
        if (
            vendor_id != contract.get("vendor_id")
            or product_id != contract.get("product_id")
            or not source_name.startswith(str(contract.get("node_name_prefix")))
        ):
            return None, None
    elif kind == "named-pipewire-source":
        if source_name != contract.get("node_name"):
            return None, None
    else:
        raise ValueError("generic source parser received an unsupported source kind")
    match = SOURCE_SPEC_RE.fullmatch(str(item.get("sample_specification", "")))
    if match is None:
        raise ValueError(
            f"{_source_label(contract)} source sample specification is invalid"
        )
    serial = properties.get("device.serial")
    bus_path = properties.get("device.bus_path")
    object_serial = properties.get("object.serial")
    if kind == "usb-audio" and (
        not isinstance(serial, str)
        or not serial
        or not isinstance(bus_path, str)
        or not bus_path
    ):
        raise ValueError("Roland source lacks its serial- and bus-bound identity")
    if kind == "named-pipewire-source" and object_serial in {None, ""}:
        raise ValueError("production mix source lacks its PipeWire object identity")
    volume_values = VOICE._source_volume_values(item)
    unity_volume = bool(volume_values) and all(
        value == 65_536 for value in volume_values
    )
    identity: dict[str, Any] = {
        "source_kind": kind,
        "node_name_sha256": hashlib.sha256(source_name.encode("utf-8")).hexdigest(),
        "sample_format": match.group("format"),
        "sample_rate_hz": int(match.group("rate")),
        "channels": int(match.group("channels")),
        "muted": item.get("mute"),
        "unity_volume": unity_volume,
    }
    if kind == "usb-audio":
        identity.update(
            {
                "vendor_id": vendor_id,
                "product_id": product_id,
                "serial_sha256": hashlib.sha256(serial.encode("utf-8")).hexdigest(),
                "bus_path_sha256": (
                    hashlib.sha256(bus_path.encode("utf-8")).hexdigest()
                    if isinstance(bus_path, str) and bus_path
                    else None
                ),
            }
        )
    else:
        identity.update(
            {
                "declared_upstream_roles": list(contract["upstream_roles"]),
                "object_serial_sha256": hashlib.sha256(
                    str(object_serial).encode("utf-8")
                ).hexdigest(),
            }
        )
    identity["fingerprint"] = canonical_sha256(identity)
    return identity, source_name


def _managed_production_mix_snapshot() -> dict[str, Any]:
    try:
        module = _production_mix_module()
        status = module.graph_status()
    except (OSError, RecordingError, RuntimeError, ValueError) as exc:
        return {
            "complete": False,
            "binding": None,
            "binding_sha256": None,
            "error": str(exc),
        }
    service = status.get("service")
    topology = status.get("topology")
    mix = (
        topology.get("endpoints", {}).get("mix") if isinstance(topology, dict) else None
    )
    binding = {
        "schema_version": 1,
        "kind": "audio_production_mix_runtime_binding",
        "session_id": status.get("session_id"),
        "plan_sha256": status.get("plan_sha256"),
        "service_identity_sha256": (
            canonical_sha256(service.get("identity"))
            if isinstance(service, dict) and isinstance(service.get("identity"), dict)
            else None
        ),
        "topology_sha256": (
            topology.get("topology_sha256") if isinstance(topology, dict) else None
        ),
        "virtual_sink": "audio-production-bus",
        "virtual_source": "audio-production-mix",
        "sample_format": mix.get("sample_format") if isinstance(mix, dict) else None,
        "sample_rate_hz": mix.get("sample_rate_hz") if isinstance(mix, dict) else None,
        "channels": mix.get("channels") if isinstance(mix, dict) else None,
        "channel_map": mix.get("channel_map") if isinstance(mix, dict) else None,
    }
    complete = (
        status.get("status") == "ready"
        and status.get("service_identity_exact") is True
        and status.get("child_identities_exact") is True
        and isinstance(topology, dict)
        and topology.get("complete") is True
        and isinstance(binding["session_id"], str)
        and SESSION_ID_RE.fullmatch(binding["session_id"]) is not None
        and isinstance(binding["plan_sha256"], str)
        and HEX64_RE.fullmatch(binding["plan_sha256"]) is not None
        and isinstance(binding["service_identity_sha256"], str)
        and HEX64_RE.fullmatch(binding["service_identity_sha256"]) is not None
        and isinstance(binding["topology_sha256"], str)
        and HEX64_RE.fullmatch(binding["topology_sha256"]) is not None
        and binding["sample_format"] == "s32le"
        and binding["sample_rate_hz"] == 48_000
        and binding["channels"] == 2
        and binding["channel_map"] == "front-left,front-right"
    )
    return {
        "complete": complete,
        "binding": binding if complete else None,
        "binding_sha256": canonical_sha256(binding) if complete else None,
        "error": None
        if complete
        else "managed production-mix graph is not exactly ready",
    }


def _pactl_source_snapshot(
    contract: dict[str, Any],
    managed_graph_snapshot_fn: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload, query = RATE._run_query(LAB.RATE_POLICY_PACTL_SOURCES_ARGV)
    matches: list[tuple[dict[str, Any], str]] = []
    errors: list[str] = []
    for item in payload:
        try:
            identity, source_name = _pactl_source_identity(item, contract)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if identity is not None and source_name is not None:
            matches.append((identity, source_name))
    source_complete = not errors and len(matches) == 1
    managed_graph: dict[str, Any] | None = None
    if contract.get("kind") == "named-pipewire-source":
        try:
            managed_graph = (
                managed_graph_snapshot_fn or _managed_production_mix_snapshot
            )()
        except (OSError, RecordingError, RuntimeError, ValueError) as exc:
            managed_graph = {
                "complete": False,
                "binding": None,
                "binding_sha256": None,
                "error": str(exc),
            }
    complete = source_complete and (
        managed_graph is None or managed_graph.get("complete") is True
    )
    return {
        "schema_version": 1,
        "kind": "audio_recording_source_snapshot",
        "complete": complete,
        "source_complete": source_complete,
        "match_count": len(matches),
        "ambiguous": len(matches) > 1,
        "errors": sorted(set(errors)),
        "identity": matches[0][0] if source_complete else None,
        "source_name": matches[0][1] if source_complete else None,
        "managed_graph": managed_graph,
        "query": query,
    }


def _source_snapshot_for_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if contract.get("kind") == "motu-voice-with-roland-audio-and-midi":
        audio = VOICE.source_snapshot()
        roland_audio = _pactl_source_snapshot(contract["roland_audio"])
        midi = _roland_midi_source_snapshot()
        complete = (
            audio.get("complete") is True
            and roland_audio.get("complete") is True
            and midi.get("complete") is True
        )
        identity = None
        if complete:
            identity = {
                "audio": audio.get("identity"),
                "roland_audio": roland_audio.get("identity"),
                "midi": midi.get("identity"),
            }
            identity["fingerprint"] = canonical_sha256(identity)
        return {
            "complete": complete,
            "source_complete": complete,
            "identity": identity,
            "audio": audio,
            "roland_audio": roland_audio,
            "midi": midi,
        }
    if contract.get("kind") == "motu-voice":
        return VOICE.source_snapshot()
    return _pactl_source_snapshot(contract)


def _source_name_for_contract(contract: dict[str, Any]) -> str:
    if contract.get("kind") == "motu-voice-with-roland-audio-and-midi":
        return VOICE._source_name_from_live_query()
    if contract.get("kind") == "motu-voice":
        return VOICE._source_name_from_live_query()
    snapshot = _pactl_source_snapshot(contract)
    source_name = snapshot.get("source_name")
    if snapshot.get("complete") is not True or not isinstance(source_name, str):
        raise RecordingError(
            f"{_source_label(contract)} capture source is missing, invalid or ambiguous"
        )
    return source_name


def _roland_audio_source_name_for_contract(contract: dict[str, Any]) -> str:
    if contract.get("kind") != "motu-voice-with-roland-audio-and-midi":
        raise RecordingError("Roland audio source requested for a non-performance contract")
    snapshot = _pactl_source_snapshot(contract["roland_audio"])
    source_name = snapshot.get("source_name")
    if snapshot.get("complete") is not True or not isinstance(source_name, str):
        raise RecordingError("Roland audio capture source is missing, invalid or ambiguous")
    return source_name


def _roland_midi_source_snapshot(
    *, arecordmidi_path: pathlib.Path = ARECORDMIDI_PATH
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(arecordmidi_path), "-l"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_restricted_environment(),
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"complete": False, "identity": None, "address": None, "error": str(exc)}
    if completed.returncode != 0 or len(completed.stdout) > 262_144:
        return {
            "complete": False,
            "identity": None,
            "address": None,
            "error": "arecordmidi port listing failed or exceeded its bound",
        }
    try:
        match = MIDI.discover_unique_roland_port(
            arecordmidi_listing=completed.stdout.decode("utf-8")
        )
    except (UnicodeDecodeError, MIDI.MidiCaptureError) as exc:
        return {"complete": False, "identity": None, "address": None, "error": str(exc)}
    return {
        "complete": True,
        "identity": match["identity"],
        "address": match["address"],
        "error": None,
    }


def _midi_address_for_contract() -> str:
    snapshot = _roland_midi_source_snapshot()
    address = snapshot.get("address")
    if snapshot.get("complete") is not True or not isinstance(address, str):
        raise RecordingError("Roland MIDI capture source is missing or ambiguous")
    return address


def _source_projection(
    contract: dict[str, Any],
    snapshot_fn: Callable[[], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    label = _source_label(contract)
    try:
        snapshot = (snapshot_fn or (lambda: _source_snapshot_for_contract(contract)))()
    except (OSError, ValueError, RecordingError, json.JSONDecodeError) as exc:
        fields = {"identity": None, "error": str(exc)}
        if contract.get("kind") == "named-pipewire-source":
            fields["managed_graph"] = None
        return fields, [f"{label}-source-query-failed"]
    identity = snapshot.get("identity")
    blockers: list[str] = []
    source_complete = snapshot.get("source_complete", snapshot.get("complete"))
    kind = contract.get("kind")
    if source_complete is not True or not isinstance(identity, dict):
        if kind == "motu-voice-with-roland-audio-and-midi":
            audio = snapshot.get("audio")
            roland_audio = snapshot.get("roland_audio")
            midi = snapshot.get("midi")
            if not isinstance(audio, dict) or audio.get("complete") is not True:
                blockers.append("motu-source-not-unique")
            if not isinstance(roland_audio, dict) or roland_audio.get("complete") is not True:
                blockers.append("roland-audio-source-not-unique")
            if not isinstance(midi, dict) or midi.get("complete") is not True:
                blockers.append("roland-midi-source-not-unique")
        else:
            blockers.append(f"{label}-source-not-unique")
        identity = None
    if kind == "motu-voice-with-roland-audio-and-midi":
        if identity is not None:
            audio_identity = identity.get("audio")
            roland_audio_identity = identity.get("roland_audio")
            midi_identity = identity.get("midi")
            audio_contract = contract["audio"]
            roland_contract = contract["roland_audio"]
            if (
                not isinstance(audio_identity, dict)
                or not isinstance(roland_audio_identity, dict)
                or not isinstance(midi_identity, dict)
            ):
                blockers.append("performance-source-invalid")
            else:
                for field, value in {
                    "sample_rate_hz": audio_contract["required_sample_rate_hz"],
                    "channels": audio_contract["required_channels"],
                    "muted": False,
                    "unity_volume": True,
                    "vendor_id": audio_contract["vendor_id"],
                    "product_id": audio_contract["product_id"],
                }.items():
                    if audio_identity.get(field) != value:
                        blockers.append(f"motu-source:{field}")
                if audio_identity.get("sample_format") not in audio_contract["required_sample_formats"]:
                    blockers.append("motu-source:sample_format")
                for field, value in {
                    "sample_rate_hz": roland_contract["required_sample_rate_hz"],
                    "channels": roland_contract["required_channels"],
                    "muted": False,
                    "unity_volume": True,
                    "vendor_id": roland_contract["vendor_id"],
                    "product_id": roland_contract["product_id"],
                }.items():
                    if roland_audio_identity.get(field) != value:
                        blockers.append(f"roland-audio-source:{field}")
                if roland_audio_identity.get("sample_format") not in roland_contract["required_sample_formats"]:
                    blockers.append("roland-audio-source:sample_format")
                if MIDI.ADDRESS_RE.fullmatch(str(midi_identity.get("address", ""))) is None:
                    blockers.append("roland-midi-source:address")
        return {
            "identity": identity,
            "identity_sha256": canonical_sha256(identity) if identity is not None else None,
            "error": None,
        }, sorted(set(blockers))
    if identity is not None:
        formats = contract["required_sample_formats"]
        expected = {
            "sample_rate_hz": contract["required_sample_rate_hz"],
            "channels": contract["required_channels"],
            "muted": False,
            "unity_volume": True,
        }
        for field, value in expected.items():
            if identity.get(field) != value:
                blockers.append(f"{label}-source:{field}")
        if identity.get("sample_format") not in formats:
            blockers.append(f"{label}-source:sample_format")
        if kind in {"motu-voice", "usb-audio"}:
            for field in ("vendor_id", "product_id"):
                if identity.get(field) != contract.get(field):
                    blockers.append(f"{label}-source:{field}")
        if kind == "named-pipewire-source" and identity.get("declared_upstream_roles") != contract.get("upstream_roles"):
            blockers.append(f"{label}-source:upstream_roles")
    result = {
        "identity": identity,
        "identity_sha256": canonical_sha256(identity) if identity is not None else None,
        "error": None,
    }
    if kind == "named-pipewire-source":
        managed_graph = snapshot.get("managed_graph")
        if (
            not isinstance(managed_graph, dict)
            or managed_graph.get("complete") is not True
            or not isinstance(managed_graph.get("binding"), dict)
            or not isinstance(managed_graph.get("binding_sha256"), str)
            or HEX64_RE.fullmatch(managed_graph["binding_sha256"]) is None
            or canonical_sha256(managed_graph["binding"]) != managed_graph["binding_sha256"]
        ):
            blockers.append("production-mix-graph-not-ready")
            result["managed_graph"] = None
        else:
            result["managed_graph"] = {
                "binding": managed_graph["binding"],
                "binding_sha256": managed_graph["binding_sha256"],
            }
    return result, sorted(set(blockers))


def maximum_file_bytes(capture: dict[str, Any], maximum_seconds: int) -> int:
    return (
        capture["sample_rate_hz"]
        * capture["channels"]
        * capture["bytes_per_sample"]
        * maximum_seconds
        + capture["header_and_metadata_allowance_bytes"]
    )


def _readiness_check(check_id: str, blockers: list[str]) -> dict[str, Any]:
    unique = sorted(set(blockers))
    return {
        "id": check_id,
        "status": "blocked" if unique else "ready",
        "blockers": unique,
    }


def _readiness_advisory(advisory_id: str, notices: list[str]) -> dict[str, Any]:
    unique = sorted(set(notices))
    return {
        "id": advisory_id,
        "status": "attention" if unique else "ready",
        "notices": unique,
    }


def _empty_laboratory_projection(path: pathlib.Path) -> dict[str, Any]:
    return {
        "state_path": str(path),
        "state_sha256": None,
        "resolved": [],
        "invalidated": {},
        "receipt_sha256": {},
        "error": None,
    }


def _hard_laboratory_projection(
    path: pathlib.Path, physical: dict[str, Any], required: list[str]
) -> tuple[dict[str, Any], list[str]]:
    if not required:
        return _empty_laboratory_projection(path), []
    return _laboratory_projection(path, physical, required)


def build_plan(
    name: str,
    maximum_seconds: int,
    *,
    session_type: str = "voice-recording",
    output_root: pathlib.Path = DEFAULT_OUTPUT_ROOT,
    state_root: pathlib.Path = DEFAULT_STATE_ROOT,
    physical_state: pathlib.Path = PHYSICAL.DEFAULT_STATE,
    laboratory_state: pathlib.Path = LAB.DEFAULT_STATE,
    source_snapshot_fn: Callable[[], dict[str, Any]] | None = None,
    disk_usage_fn: Callable[[pathlib.Path], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    name = _plain_wav_name(name)
    contract = load_catalog(session_type)
    capture = contract["capture"]
    if isinstance(maximum_seconds, bool) or not isinstance(maximum_seconds, int):
        raise RecordingError("maximum recording duration must be an integer")
    if not (
        capture["minimum_duration_seconds"]
        <= maximum_seconds
        <= capture["maximum_duration_seconds"]
    ):
        raise RecordingError(
            "maximum recording duration is outside the catalogued range"
        )
    root = lexical_absolute(output_root)
    state_root = lexical_absolute(state_root)
    output_blockers: list[str] = []
    tool_blockers: list[str] = []
    storage_blockers: list[str] = []
    session_blockers: list[str] = []
    output_path = root / name
    midi_output_path = output_path.with_suffix(".mid")
    manifest_output_path = output_path.with_suffix(".take.json")
    root_ready = False
    try:
        root = _validate_output_root(root)
        root_ready = True
    except RecordingError:
        output_blockers.append("output-root-not-ready")
    if output_path.exists() or output_path.is_symlink():
        output_blockers.append("output-already-exists")
    if session_type == "piano-vocal-performance":
        if midi_output_path.exists() or midi_output_path.is_symlink():
            output_blockers.append("midi-output-already-exists")
        if manifest_output_path.exists() or manifest_output_path.is_symlink():
            output_blockers.append("manifest-output-already-exists")
    physical, physical_blockers = _physical_projection(
        lexical_absolute(physical_state), contract["required_physical_facts"]
    )
    laboratory_path = lexical_absolute(laboratory_state)
    laboratory, laboratory_blockers = _hard_laboratory_projection(
        laboratory_path,
        physical,
        contract["required_laboratory_gates"],
    )
    if contract["advisory_laboratory_gates"]:
        advisory_laboratory, advisory_laboratory_notices = _laboratory_projection(
            laboratory_path,
            physical,
            contract["advisory_laboratory_gates"],
        )
    else:
        advisory_laboratory = _empty_laboratory_projection(laboratory_path)
        advisory_laboratory_notices = []
    source, source_blockers = _source_projection(contract["source"], source_snapshot_fn)
    try:
        recorder = parecord_binding()
    except RecordingError:
        recorder = None
        tool_blockers.append("parecord-unavailable")
    midi_recorder: dict[str, Any] | None = None
    mixer: dict[str, Any] | None = None
    if session_type == "piano-vocal-performance":
        try:
            midi_recorder = arecordmidi_binding()
        except RecordingError:
            tool_blockers.append("arecordmidi-unavailable")
        try:
            mixer = ffmpeg_binding()
        except RecordingError:
            tool_blockers.append("ffmpeg-unavailable")
    required_bytes = maximum_file_bytes(capture, maximum_seconds)
    required_session_bytes = required_bytes + (
        3 * required_bytes + MIDI.MAX_MIDI_BYTES + MAX_JSON_BYTES
        if session_type == "piano-vocal-performance"
        else 0
    )
    free_bytes: int | None = None
    if root_ready:
        try:
            free_bytes = int(disk_usage_fn(root).free)
        except (OSError, ValueError, TypeError):
            storage_blockers.append("free-space-unknown")
        else:
            if free_bytes < required_session_bytes + capture["free_space_reserve_bytes"]:
                storage_blockers.append("free-space-insufficient")
    active_path = state_root / "active.json"
    if active_path.exists() or active_path.is_symlink():
        session_blockers.append("active-session-requires-status-or-recovery")
    identity = {
        "schema_version": 1,
        "kind": "audio_recording_plan_identity",
        "session_type": session_type,
        "profile": contract["profile"],
        "output": {
            "root": str(root),
            "name": name,
            "path": str(output_path),
            "mode": "0600",
            "overwrite": False,
        },
        "capture": {
            "sample_rate_hz": capture["sample_rate_hz"],
            "sample_format": capture["sample_format"],
            "channels": capture["channels"],
            "channel_map": capture["channel_map"],
            "container": capture["container"],
            "maximum_duration_seconds": maximum_seconds,
            "maximum_file_bytes": required_bytes,
            "startup_timeout_seconds": capture["startup_timeout_seconds"],
            "stop_grace_seconds": capture["stop_grace_seconds"],
            "free_space_reserve_bytes": capture["free_space_reserve_bytes"],
        },
        "physical": physical,
        "laboratory": laboratory,
        "advisory_laboratory": advisory_laboratory,
        "source": source,
        "monitoring": contract["monitoring"],
        "contracts": contract_bindings(),
        "parecord": recorder,
        "process": contract["process"],
        "state_root": str(state_root),
    }
    if session_type == "piano-vocal-performance":
        identity["performance"] = {
            "midi_output": {
                "name": midi_output_path.name,
                "path": str(midi_output_path),
                "mode": "0600",
                "overwrite": False,
                "maximum_file_bytes": MIDI.MAX_MIDI_BYTES,
            },
            "manifest_output": {
                "name": manifest_output_path.name,
                "path": str(manifest_output_path),
                "mode": "0600",
                "overwrite": False,
            },
            "timing": {
                "basis": "SMPTE",
                "fps": MIDI.SMPTE_FPS,
                "ticks_per_frame": MIDI.SMPTE_TICKS_PER_FRAME,
                "nominal_resolution_ms": 1,
            },
            "arecordmidi": midi_recorder,
            "ffmpeg": mixer,
            "capture_argv": ["-p", "<plan-bound-client:port>", "-f", "25", "-t", "40", "<private-partial.mid>"],
            "audio_capture": {
                "sample_rate_hz": 48_000,
                "sources": ["motu-voice", "roland-fp-30x-usb-audio"],
                "maximum_spawn_spread_ns": MAX_AUDIO_SPAWN_SPREAD_NS,
                "maximum_frame_difference": MAX_AUDIO_FRAME_DIFFERENCE_FRAMES,
                "stems": "private-temporary-not-published",
            },
            "mix": {
                "method": "offline-ffmpeg-amix",
                "inputs": ["motu-voice", "roland-fp-30x-usb-audio"],
                "duration": "shortest",
                "normalize": True,
                "sample_rate_hz": 48_000,
                "sample_format": "s32le",
                "channels": 2,
            },
            "synchronization_boundary": "bounded audio process-spawn alignment; not sample-accurate WAV/MIDI synchronization",
        }
    plan_sha = canonical_sha256(identity)
    readiness_checks = [
        _readiness_check("output", output_blockers),
        _readiness_check("physical", physical_blockers),
        _readiness_check("laboratory", laboratory_blockers),
        _readiness_check("source", source_blockers),
        _readiness_check("tools", tool_blockers),
        _readiness_check("storage", storage_blockers),
        _readiness_check("session", session_blockers),
    ]
    blockers = sorted(
        {
            blocker
            for check in readiness_checks
            for blocker in check["blockers"]
        }
    )
    advisories = [
        _readiness_advisory("voice-level", advisory_laboratory_notices)
    ] if contract["advisory_laboratory_gates"] else []
    return {
        "schema_version": 1,
        "kind": "audio_recording_plan",
        "ready": not blockers,
        "plan_sha256": plan_sha,
        "identity": identity,
        "readiness": {
            "blockers": blockers,
            "checks": readiness_checks,
            "advisories": advisories,
            "free_bytes": free_bytes,
            "required_file_bytes": required_session_bytes,
            "required_free_bytes": required_session_bytes + capture["free_space_reserve_bytes"],
        },
        "does_not_establish": [
            "safe-monitoring-level",
            "subjective-recording-quality",
            "physical-source-state-beyond-bound-observation",
            "declared-production-upstream-roles-are-currently-connected",
            "successful-future-capture",
        ],
    }


@contextlib.contextmanager
def state_lock(state_root: pathlib.Path) -> Iterator[None]:
    root = ensure_private_directory(state_root)
    lock_path = root / "lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise RecordingError("recording lock cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RecordingError("recording lock is not a current-user regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _session_paths(
    state_root: pathlib.Path, session_id: str
) -> dict[str, pathlib.Path]:
    if not re.fullmatch(r"[0-9a-f]{24}", session_id):
        raise RecordingError("recording session id is invalid")
    root = lexical_absolute(state_root)
    return {
        "spec": root / f"{session_id}.spec.json",
        "state": root / f"{session_id}.state.json",
        "ready": root / f"{session_id}.ready.json",
        "result": root / f"{session_id}.result.json",
        "active": root / "active.json",
    }


def _read_active(state_root: pathlib.Path) -> str:
    active = lexical_absolute(state_root) / "active.json"
    payload = _safe_json_read(active, require_private=True)
    if set(payload) != {"schema_version", "kind", "session_id", "spec_sha256"}:
        raise RecordingError("active recording pointer schema is invalid")
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "audio_recording_active"
    ):
        raise RecordingError("active recording pointer kind is invalid")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        raise RecordingError("active recording pointer has no session id")
    paths = _session_paths(state_root, session_id)
    spec_binding = _safe_regular_binding(paths["spec"], require_private=True)
    if payload.get("spec_sha256") != spec_binding["sha256"]:
        raise RecordingError("active recording pointer does not bind the session spec")
    return session_id


def _resolve_session_id(state_root: pathlib.Path, session_id: str | None) -> str:
    return session_id if session_id is not None else _read_active(state_root)


def _proc_identity(pid: int) -> dict[str, Any] | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 2:
        return None
    proc = pathlib.Path("/proc") / str(pid)
    try:
        stat_text = (proc / "stat").read_text()
        rest = stat_text.rsplit(")", 1)[1].strip().split()
        if rest[0] == "Z":
            return None
        start_ticks = int(rest[19])
        executable = os.readlink(proc / "exe")
        command_line = (proc / "cmdline").read_bytes()
        process_group = os.getpgid(pid)
    except (
        FileNotFoundError,
        ProcessLookupError,
        PermissionError,
        OSError,
        ValueError,
        IndexError,
    ):
        return None
    return {
        "pid": pid,
        "start_ticks": start_ticks,
        "executable": executable,
        "cmdline_sha256": hashlib.sha256(command_line).hexdigest(),
        "process_group": process_group,
    }


def _identity_matches(expected: dict[str, Any]) -> bool:
    observed = _proc_identity(expected.get("pid"))
    return observed == expected


def _terminate_exact_process(expected: dict[str, Any], *, grace_seconds: float) -> bool:
    if grace_seconds < 0:
        raise RecordingError("process termination grace must not be negative")
    if not _identity_matches(expected):
        return True
    try:
        os.kill(expected["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and _identity_matches(expected):
        time.sleep(0.05)
    if _identity_matches(expected):
        try:
            if expected.get("process_group") == expected.get("pid"):
                os.killpg(expected["process_group"], signal.SIGKILL)
            else:
                os.kill(expected["pid"], signal.SIGKILL)
        except ProcessLookupError:
            return True
        for _ in range(100):
            if not _identity_matches(expected):
                return True
            time.sleep(0.02)
    return not _identity_matches(expected)


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value or CONTROL_RE.search(value):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _validate_binding_shape(
    value: Any,
    *,
    expected_path: pathlib.Path | None = None,
    require_identity: bool = False,
    detail_fields: frozenset[str] = frozenset(),
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise RecordingError("recording artifact binding is not an object")
    if set(value) == {"path", "error"}:
        if (
            not isinstance(value.get("path"), str)
            or CONTROL_RE.search(value["path"])
            or not isinstance(value.get("error"), str)
            or not value["error"]
            or CONTROL_RE.search(value["error"])
        ):
            raise RecordingError("recording artifact error binding is invalid")
        if expected_path is not None and pathlib.Path(value["path"]) != expected_path:
            raise RecordingError("recording artifact path does not match the session")
        return
    required = {"path", "sha256", "bytes", "mode"}
    if require_identity:
        required |= {"device", "inode"}
    if set(value) != required | detail_fields:
        raise RecordingError("recording artifact binding fields are invalid")
    path_value = value.get("path")
    if not isinstance(path_value, str) or CONTROL_RE.search(path_value):
        raise RecordingError("recording artifact path is invalid")
    if expected_path is not None and pathlib.Path(path_value) != expected_path:
        raise RecordingError("recording artifact path does not match the session")
    if not isinstance(value.get("sha256"), str) or not HEX64_RE.fullmatch(
        value["sha256"]
    ):
        raise RecordingError("recording artifact digest is invalid")
    if (
        isinstance(value.get("bytes"), bool)
        or not isinstance(value.get("bytes"), int)
        or value["bytes"] < 1
        or value.get("mode") != "0600"
    ):
        raise RecordingError("recording artifact identity is invalid")
    if require_identity and (
        isinstance(value.get("device"), bool)
        or not isinstance(value.get("device"), int)
        or value["device"] < 0
        or isinstance(value.get("inode"), bool)
        or not isinstance(value.get("inode"), int)
        or value["inode"] < 1
    ):
        raise RecordingError("recording artifact filesystem identity is invalid")


def _validate_process_identity(value: Any) -> None:
    required = {
        "pid",
        "start_ticks",
        "executable",
        "cmdline_sha256",
        "process_group",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RecordingError("recording process identity fields are invalid")
    if any(
        isinstance(value.get(key), bool)
        or not isinstance(value.get(key), int)
        or value[key] < minimum
        for key, minimum in (("pid", 2), ("start_ticks", 1), ("process_group", 1))
    ):
        raise RecordingError("recording process numeric identity is invalid")
    executable = value.get("executable")
    if (
        not isinstance(executable, str)
        or not pathlib.Path(executable).is_absolute()
        or CONTROL_RE.search(executable)
    ):
        raise RecordingError("recording process executable is invalid")
    digest = value.get("cmdline_sha256")
    if not isinstance(digest, str) or not HEX64_RE.fullmatch(digest):
        raise RecordingError("recording process command identity is invalid")


def _positive_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _non_negative_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _is_modern_performance_plan(plan: Any) -> bool:
    performance = plan.get("performance") if isinstance(plan, dict) else None
    return isinstance(performance, dict) and {
        "ffmpeg",
        "audio_capture",
        "mix",
    }.issubset(performance)


def _performance_audio_capture_generation(plan: Any) -> str | None:
    """Classify persisted modern performance capture policy without rewriting history."""

    performance = plan.get("performance") if isinstance(plan, dict) else None
    capture = performance.get("audio_capture") if isinstance(performance, dict) else None
    if not isinstance(capture, dict):
        return None
    current = {
        "sample_rate_hz": 48_000,
        "sources": ["motu-voice", "roland-fp-30x-usb-audio"],
        "maximum_spawn_spread_ns": MAX_AUDIO_SPAWN_SPREAD_NS,
        "maximum_frame_difference": MAX_AUDIO_FRAME_DIFFERENCE_FRAMES,
        "stems": "private-temporary-not-published",
    }
    if capture == current:
        return "bounded-tail-v1"
    historical = dict(current)
    historical.pop("maximum_frame_difference")
    if capture == historical:
        return "pre-bounded-tail-v1"
    return None


def _validate_planned_source_projection(source: Any, contract: dict[str, Any]) -> None:
    source_contract = contract["source"] if "source" in contract else contract
    kind = source_contract["kind"]
    projection_fields = {"identity", "identity_sha256", "error"}
    if kind == "named-pipewire-source":
        projection_fields.add("managed_graph")
    if not isinstance(source, dict) or set(source) != projection_fields:
        raise RecordingError("recording source projection fields are invalid")
    identity = source.get("identity")
    digest = source.get("identity_sha256")
    if (
        source.get("error") is not None
        or not isinstance(identity, dict)
        or not isinstance(digest, str)
        or not HEX64_RE.fullmatch(digest)
        or canonical_sha256(identity) != digest
    ):
        raise RecordingError("recording source projection is not bound")
    if kind in {
        "motu-voice-with-roland-audio-and-midi",
        "motu-voice-with-roland-midi",
    }:
        modern_performance = kind == "motu-voice-with-roland-audio-and-midi"
        modern = set(identity) == {"audio", "roland_audio", "midi", "fingerprint"}
        legacy = set(identity) == {"audio", "midi", "fingerprint"}
        if (modern_performance and not modern and not legacy) or (
            not modern_performance and not legacy
        ):
            raise RecordingError("performance source identity fields are invalid")
        unbound = {"audio": identity.get("audio"), "midi": identity.get("midi")}
        if modern:
            unbound["roland_audio"] = identity.get("roland_audio")
        if identity.get("fingerprint") != canonical_sha256(unbound):
            raise RecordingError("performance source fingerprint is invalid")
        audio_identity = identity.get("audio")
        midi_identity = identity.get("midi")
        _validate_planned_source_projection(
            {
                "identity": audio_identity,
                "identity_sha256": canonical_sha256(audio_identity),
                "error": None,
            },
            source_contract["audio"],
        )
        if modern:
            roland_audio_identity = identity.get("roland_audio")
            roland_contract = source_contract.get("roland_audio")
            if not isinstance(roland_contract, dict):
                raise RecordingError("Roland audio source contract is invalid")
            _validate_planned_source_projection(
                {
                    "identity": roland_audio_identity,
                    "identity_sha256": canonical_sha256(roland_audio_identity),
                    "error": None,
                },
                roland_contract,
            )
        if (
            not isinstance(midi_identity, dict)
            or set(midi_identity)
            != {
                "address",
                "client",
                "port",
                "kernel_card",
                "kernel_client_label_sha256",
                "kernel_port_label_sha256",
                "arecordmidi_client_label_sha256",
                "arecordmidi_port_label_sha256",
                "usb",
                "fingerprint",
            }
            or MIDI.ADDRESS_RE.fullmatch(str(midi_identity.get("address", ""))) is None
            or midi_identity.get("address")
            != f"{midi_identity.get('client')}:{midi_identity.get('port')}"
            or any(
                isinstance(midi_identity.get(field), bool)
                or not isinstance(midi_identity.get(field), int)
                or midi_identity[field] < 0
                for field in ("client", "port", "kernel_card")
            )
            or any(
                not isinstance(midi_identity.get(field), str)
                or HEX64_RE.fullmatch(midi_identity[field]) is None
                for field in (
                    "kernel_client_label_sha256",
                    "kernel_port_label_sha256",
                    "arecordmidi_client_label_sha256",
                    "arecordmidi_port_label_sha256",
                )
            )
            or not isinstance(midi_identity.get("usb"), dict)
            or set(midi_identity.get("usb", {}))
            != {
                "vendor_id",
                "product_id",
                "identity_strength",
                "bus_number",
                "port_path",
                "fingerprint",
            }
            or midi_identity["usb"].get("vendor_id") != "0582"
            or midi_identity["usb"].get("product_id") != "01b1"
            or midi_identity["usb"].get("identity_strength") != "model-usb-port"
        ):
            raise RecordingError("Roland MIDI source binding is invalid")
        usb_unbound = dict(midi_identity["usb"])
        usb_fingerprint = usb_unbound.pop("fingerprint")
        if usb_fingerprint != canonical_sha256(usb_unbound):
            raise RecordingError("Roland USB source fingerprint is invalid")
        midi_unbound = dict(midi_identity)
        fingerprint = midi_unbound.pop("fingerprint", None)
        if fingerprint != canonical_sha256(midi_unbound):
            raise RecordingError("Roland MIDI source fingerprint is invalid")
        return
    common = {
        "node_name_sha256",
        "sample_format",
        "sample_rate_hz",
        "channels",
        "muted",
        "unity_volume",
        "fingerprint",
    }
    expected_fields = {
        "motu-voice": common
        | {"vendor_id", "product_id", "serial_sha256", "bus_path_sha256"},
        "usb-audio": common
        | {
            "source_kind",
            "vendor_id",
            "product_id",
            "serial_sha256",
            "bus_path_sha256",
        },
        "named-pipewire-source": common
        | {"source_kind", "declared_upstream_roles", "object_serial_sha256"},
    }[kind]
    if set(identity) != expected_fields:
        raise RecordingError("recording source identity fields are invalid")
    fingerprint = identity.get("fingerprint")
    unbound = dict(identity)
    unbound.pop("fingerprint", None)
    if (
        not isinstance(fingerprint, str)
        or not HEX64_RE.fullmatch(fingerprint)
        or canonical_sha256(unbound) != fingerprint
        or not isinstance(identity.get("node_name_sha256"), str)
        or not HEX64_RE.fullmatch(identity["node_name_sha256"])
        or identity.get("sample_format")
        not in source_contract["required_sample_formats"]
        or identity.get("sample_rate_hz") != source_contract["required_sample_rate_hz"]
        or identity.get("channels") != source_contract["required_channels"]
        or identity.get("muted") is not False
        or identity.get("unity_volume") is not True
    ):
        raise RecordingError("recording source identity does not match its contract")
    if kind in {"motu-voice", "usb-audio"}:
        for field in ("vendor_id", "product_id"):
            if identity.get(field) != source_contract[field]:
                raise RecordingError(
                    "recording USB source identity does not match its contract"
                )
        for field in ("serial_sha256", "bus_path_sha256"):
            value = identity.get(field)
            if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
                raise RecordingError("recording USB source binding is invalid")
    if kind == "usb-audio" and identity.get("source_kind") != kind:
        raise RecordingError("Roland source kind is invalid")
    if kind == "named-pipewire-source" and (
        identity.get("source_kind") != kind
        or identity.get("declared_upstream_roles") != source_contract["upstream_roles"]
        or not isinstance(identity.get("object_serial_sha256"), str)
        or not HEX64_RE.fullmatch(identity["object_serial_sha256"])
    ):
        raise RecordingError("production mix source identity is invalid")
    if kind == "named-pipewire-source":
        managed = source.get("managed_graph")
        if not isinstance(managed, dict) or set(managed) != {
            "binding",
            "binding_sha256",
        }:
            raise RecordingError("managed production-mix binding fields are invalid")
        binding = managed.get("binding")
        binding_sha = managed.get("binding_sha256")
        expected_binding_fields = {
            "schema_version",
            "kind",
            "session_id",
            "plan_sha256",
            "service_identity_sha256",
            "topology_sha256",
            "virtual_sink",
            "virtual_source",
            "sample_format",
            "sample_rate_hz",
            "channels",
            "channel_map",
        }
        if (
            not isinstance(binding, dict)
            or set(binding) != expected_binding_fields
            or not isinstance(binding_sha, str)
            or HEX64_RE.fullmatch(binding_sha) is None
            or canonical_sha256(binding) != binding_sha
            or binding.get("schema_version") != 1
            or binding.get("kind") != "audio_production_mix_runtime_binding"
            or not isinstance(binding.get("session_id"), str)
            or SESSION_ID_RE.fullmatch(binding["session_id"]) is None
            or any(
                not isinstance(binding.get(field), str)
                or HEX64_RE.fullmatch(binding[field]) is None
                for field in (
                    "plan_sha256",
                    "service_identity_sha256",
                    "topology_sha256",
                )
            )
            or binding.get("virtual_sink") != "audio-production-bus"
            or binding.get("virtual_source") != source_contract["node_name"]
            or binding.get("sample_format") != "s32le"
            or binding.get("sample_rate_hz") != 48_000
            or binding.get("channels") != 2
            or binding.get("channel_map") != "front-left,front-right"
        ):
            raise RecordingError("managed production-mix binding is invalid")


def _validate_persisted_spec(
    spec: dict[str, Any], *, state_root: pathlib.Path | None = None
) -> None:
    base_required = {
        "schema_version",
        "kind",
        "session_id",
        "created_at",
        "plan_sha256",
        "plan_identity",
        "paths",
    }
    session_id = spec.get("session_id")
    plan_sha = spec.get("plan_sha256")
    plan = spec.get("plan_identity")
    if (
        spec.get("schema_version") != 1
        or spec.get("kind") != "audio_recording_session_spec"
        or not isinstance(session_id, str)
        or not SESSION_ID_RE.fullmatch(session_id)
        or not _valid_timestamp(spec.get("created_at"))
        or not isinstance(plan_sha, str)
        or not HEX64_RE.fullmatch(plan_sha)
        or not isinstance(plan, dict)
        or canonical_sha256(plan) != plan_sha
    ):
        raise RecordingError("recording worker spec schema is invalid")
    expected_plan_fields = {
        "schema_version",
        "kind",
        "session_type",
        "profile",
        "output",
        "capture",
        "physical",
        "laboratory",
        "advisory_laboratory",
        "source",
        "monitoring",
        "contracts",
        "parecord",
        "process",
        "state_root",
    }
    session_type = plan.get("session_type") if isinstance(plan, dict) else None
    modern_performance = (
        session_type == "piano-vocal-performance"
        and _is_modern_performance_plan(plan)
    )
    required = base_required | ({"source_names", "midi_source"} if modern_performance else {"source_name"}) | (
        {"midi_source"}
        if session_type == "piano-vocal-performance" and not modern_performance
        else set()
    )
    if set(spec) != required:
        raise RecordingError("recording worker spec fields are invalid")
    expected_plan_fields = expected_plan_fields | (
        {"performance"} if session_type == "piano-vocal-performance" else set()
    )
    legacy_plan_fields = expected_plan_fields - {"advisory_laboratory"}
    if (
        frozenset(plan) not in {frozenset(expected_plan_fields), frozenset(legacy_plan_fields)}
        or plan.get("schema_version") != 1
        or plan.get("kind") != "audio_recording_plan_identity"
        or session_type not in SESSION_TYPES
    ):
        raise RecordingError("recording plan identity fields are invalid")
    contract = load_catalog(session_type)
    if (
        plan.get("profile") != contract["profile"]
        or plan.get("monitoring") != contract["monitoring"]
        or plan.get("process") != contract["process"]
    ):
        raise RecordingError("recording plan no longer matches its session contract")
    _validate_planned_source_projection(plan.get("source"), contract)
    if modern_performance:
        source_identity = plan.get("source", {}).get("identity")
        if not isinstance(source_identity, dict) or set(source_identity) != {
            "audio",
            "roland_audio",
            "midi",
            "fingerprint",
        }:
            raise RecordingError("modern performance recording source identity is invalid")
    if modern_performance:
        source_names = spec.get("source_names")
        if (
            not isinstance(source_names, dict)
            or set(source_names) != {"voice", "roland"}
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 4096
                or CONTROL_RE.search(value)
                for value in source_names.values()
            )
        ):
            raise RecordingError("performance recording source names are invalid")
    else:
        source_name = spec.get("source_name")
        if (
            not isinstance(source_name, str)
            or not source_name
            or len(source_name) > 4096
            or CONTROL_RE.search(source_name)
        ):
            raise RecordingError("recording source name is invalid")
    if session_type == "piano-vocal-performance" and MIDI.ADDRESS_RE.fullmatch(
        str(spec.get("midi_source", ""))
    ) is None:
        raise RecordingError("recording MIDI source is invalid")
    paths = spec.get("paths")
    expected_paths = {"partial", "final", "result"}
    if session_type == "piano-vocal-performance":
        expected_paths |= {
            "midi_partial",
            "midi_final",
            "manifest_partial",
            "manifest_final",
        }
        if modern_performance:
            expected_paths |= {"voice_partial", "roland_partial", "mix_raw_partial"}
    if not isinstance(paths, dict):
        raise RecordingError("recording worker paths are invalid")
    accepted_path_sets = [expected_paths]
    if modern_performance:
        accepted_path_sets.append(expected_paths - {"mix_raw_partial"})
    if set(paths) not in accepted_path_sets:
        raise RecordingError("recording worker paths are invalid")
    resolved: dict[str, pathlib.Path] = {}
    for key, raw_path in paths.items():
        if not isinstance(raw_path, str) or CONTROL_RE.search(raw_path):
            raise RecordingError("recording worker path is invalid")
        candidate = pathlib.Path(raw_path)
        if not candidate.is_absolute() or lexical_absolute(candidate) != candidate:
            raise RecordingError("recording worker path is not canonical absolute")
        resolved[key] = candidate
    output = plan.get("output")
    if (
        not isinstance(output, dict)
        or set(output) != {"root", "name", "path", "mode", "overwrite"}
        or output.get("mode") != "0600"
        or output.get("overwrite") is not False
    ):
        raise RecordingError("recording worker output plan is invalid")
    capture = plan.get("capture")
    expected_capture_fields = {
        "sample_rate_hz",
        "sample_format",
        "channels",
        "channel_map",
        "container",
        "maximum_duration_seconds",
        "maximum_file_bytes",
        "startup_timeout_seconds",
        "stop_grace_seconds",
        "free_space_reserve_bytes",
    }
    maximum_duration = (
        capture.get("maximum_duration_seconds") if isinstance(capture, dict) else None
    )
    maximum_bytes = (
        capture.get("maximum_file_bytes") if isinstance(capture, dict) else None
    )
    if (
        not isinstance(capture, dict)
        or set(capture) != expected_capture_fields
        or capture.get("sample_rate_hz") != 48_000
        or capture.get("sample_format") != "s32le"
        or capture.get("channels") != 2
        or capture.get("channel_map") != "front-left,front-right"
        or capture.get("container") != "wav"
        or not _positive_integer(maximum_duration)
        or maximum_duration > 14_400
        or not _positive_integer(maximum_bytes)
        or maximum_bytes != 48_000 * 2 * 4 * maximum_duration + 1_048_576
        or not _positive_integer(capture.get("startup_timeout_seconds"))
        or not _positive_integer(capture.get("stop_grace_seconds"))
        or not _non_negative_integer(capture.get("free_space_reserve_bytes"))
        or not isinstance(plan.get("physical"), dict)
        or not isinstance(plan.get("laboratory"), dict)
        or not isinstance(plan.get("source"), dict)
        or not isinstance(plan.get("monitoring"), dict)
        or not isinstance(plan.get("contracts"), list)
        or not plan["contracts"]
        or not isinstance(plan.get("parecord"), dict)
        or not isinstance(plan.get("process"), dict)
    ):
        raise RecordingError("recording capture plan is invalid")
    final = resolved["final"]
    partial = resolved["partial"]
    output_root = output.get("root")
    output_name = output.get("name")
    output_path = output.get("path")
    if (
        not isinstance(output_root, str)
        or not isinstance(output_name, str)
        or not isinstance(output_path, str)
        or CONTROL_RE.search(output_root)
        or CONTROL_RE.search(output_name)
        or CONTROL_RE.search(output_path)
        or pathlib.Path(output_root) != final.parent
        or pathlib.Path(output_path) != final
        or output_name != final.name
        or partial.parent != final.parent
        or partial.name != f".{final.stem}.{session_id}.partial.wav"
    ):
        raise RecordingError("recording worker paths do not match the plan")
    if session_type == "piano-vocal-performance":
        performance = plan.get("performance")
        legacy_performance_fields = {
            "midi_output",
            "manifest_output",
            "timing",
            "arecordmidi",
            "capture_argv",
            "synchronization_boundary",
        }
        modern_performance_fields = legacy_performance_fields | {
            "ffmpeg",
            "audio_capture",
            "mix",
        }
        valid_common = (
            isinstance(performance, dict)
            and set(performance)
            == (modern_performance_fields if modern_performance else legacy_performance_fields)
            and isinstance(performance.get("arecordmidi"), dict)
            and performance.get("timing")
            == {
                "basis": "SMPTE",
                "fps": 25,
                "ticks_per_frame": 40,
                "nominal_resolution_ms": 1,
            }
            and performance.get("capture_argv")
            == [
                "-p",
                "<plan-bound-client:port>",
                "-f",
                "25",
                "-t",
                "40",
                "<private-partial.mid>",
            ]
            and pathlib.Path(performance.get("midi_output", {}).get("path", ""))
            == resolved["midi_final"]
            and pathlib.Path(performance.get("manifest_output", {}).get("path", ""))
            == resolved["manifest_final"]
            and resolved["midi_partial"].parent == final.parent
            and resolved["manifest_partial"].parent == final.parent
            and resolved["midi_partial"].name
            == f".{final.stem}.{session_id}.partial.mid"
            and resolved["manifest_partial"].name
            == f".{final.stem}.{session_id}.partial.take.json"
        )
        if not valid_common:
            raise RecordingError("performance recording plan or paths are invalid")
        if modern_performance:
            if (
                performance.get("synchronization_boundary")
                != "bounded audio process-spawn alignment; not sample-accurate WAV/MIDI synchronization"
                or not isinstance(performance.get("ffmpeg"), dict)
                or _performance_audio_capture_generation(plan) is None
                or performance.get("mix")
                != {
                    "method": "offline-ffmpeg-amix",
                    "inputs": ["motu-voice", "roland-fp-30x-usb-audio"],
                    "duration": "shortest",
                    "normalize": True,
                    "sample_rate_hz": 48_000,
                    "sample_format": "s32le",
                    "channels": 2,
                }
                or resolved["voice_partial"].parent != final.parent
                or resolved["roland_partial"].parent != final.parent
                or resolved["voice_partial"].name
                != f".{final.stem}.{session_id}.voice.partial.wav"
                or resolved["roland_partial"].name
                != f".{final.stem}.{session_id}.roland.partial.wav"
                or (
                    "mix_raw_partial" in resolved
                    and (
                        resolved["mix_raw_partial"].parent != final.parent
                        or resolved["mix_raw_partial"].name
                        != f".{final.stem}.{session_id}.mix.partial.s32le"
                    )
                )
            ):
                raise RecordingError("modern performance recording plan or paths are invalid")
        elif (
            performance.get("synchronization_boundary")
            != "process-start alignment only; not sample-accurate WAV/MIDI synchronization"
        ):
            raise RecordingError("legacy performance recording plan is invalid")
    raw_state_root = plan.get("state_root")
    if not isinstance(raw_state_root, str) or CONTROL_RE.search(raw_state_root):
        raise RecordingError("recording state root is invalid")
    effective_root = pathlib.Path(raw_state_root)
    if (
        not effective_root.is_absolute()
        or lexical_absolute(effective_root) != effective_root
        or (state_root is not None and lexical_absolute(state_root) != effective_root)
    ):
        raise RecordingError("recording state root does not match the session")
    if resolved["result"] != _session_paths(effective_root, session_id)["result"]:
        raise RecordingError("recording result path does not match the session")


def _validate_session_state(
    state: dict[str, Any], *, session_id: str, spec_sha256: str
) -> None:
    required = {
        "schema_version",
        "kind",
        "session_id",
        "spec_sha256",
        "started_at",
        "process",
    }
    fields = frozenset(state)
    if fields not in {frozenset(required), frozenset(required | {"phase"})}:
        raise RecordingError("recording session state fields are invalid")
    process = state.get("process")
    phase = state.get("phase")
    if (
        state.get("schema_version") != 1
        or state.get("kind") != "audio_recording_session_state"
        or state.get("session_id") != session_id
        or state.get("spec_sha256") != spec_sha256
        or not _valid_timestamp(state.get("started_at"))
        or phase not in {None, "starting", "running"}
    ):
        raise RecordingError("recording session state schema is invalid")
    if phase == "starting":
        if process is not None:
            raise RecordingError("starting recording state must not bind a process")
    else:
        _validate_process_identity(process)


def _artifact_binding_fields(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in ("path", "sha256", "bytes", "mode", "device", "inode")
    }


def _assert_artifact_binding_current(
    value: Any,
    *,
    expected_path: pathlib.Path,
    maximum_bytes: int,
    detail_fields: frozenset[str] = frozenset(),
) -> None:
    _validate_binding_shape(
        value,
        expected_path=expected_path,
        require_identity=True,
        detail_fields=detail_fields,
    )
    if value is None or set(value) == {"path", "error"}:
        return
    try:
        observed = _safe_regular_binding(
            expected_path,
            maximum_bytes=maximum_bytes,
            require_private=True,
            include_identity=True,
        )
    except RecordingError as exc:
        raise RecordingError(
            "recording artifact no longer matches its receipt"
        ) from exc
    if observed != _artifact_binding_fields(value):
        raise RecordingError("recording artifact no longer matches its receipt")


def _validate_performance_manifest(
    path: pathlib.Path,
    spec: dict[str, Any],
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    manifest = _safe_json_read(path, maximum_bytes=MAX_JSON_BYTES, require_private=True)
    modern = _is_modern_performance_plan(spec["plan_identity"])
    expected_fields = {
        "schema_version",
        "kind",
        "session_id",
        "plan_sha256",
        "worker_capture_epoch_monotonic_ns",
        "capture_timeline_offsets_ns",
        "midi_timing",
        "synchronization_boundary",
        "artifacts",
        "midi_event_counts",
        "midi_note_velocity",
        "does_not_establish",
    }
    timeline = manifest.get("capture_timeline_offsets_ns")
    legacy_timeline_fields = {
        "midi_spawn_requested_offset_ns",
        "midi_running_observed_offset_ns",
        "audio_spawn_requested_offset_ns",
        "audio_running_observed_offset_ns",
        "session_ready_offset_ns",
    }
    modern_timeline_fields = {
        "midi_spawn_requested_offset_ns",
        "midi_running_observed_offset_ns",
        "voice_spawn_requested_offset_ns",
        "roland_spawn_requested_offset_ns",
        "audio_spawn_spread_ns",
        "voice_running_observed_offset_ns",
        "roland_running_observed_offset_ns",
        "session_ready_offset_ns",
    }
    midi_count_fields = {
        "note_on",
        "note_off",
        "control_change",
        "sustain_cc64",
        "pitch_bend",
        "poly_aftertouch",
        "program_change",
        "channel_aftertouch",
        "sysex",
        "meta",
    }
    if (
        set(manifest) != expected_fields
        or manifest.get("schema_version") != 1
        or manifest.get("kind") != "piano_vocal_performance_take_manifest"
        or manifest.get("session_id") != spec["session_id"]
        or manifest.get("plan_sha256") != spec["plan_sha256"]
        or not _non_negative_integer(manifest.get("worker_capture_epoch_monotonic_ns"))
        or not isinstance(timeline, dict)
        or set(timeline) != (modern_timeline_fields if modern else legacy_timeline_fields)
        or any(
            not _non_negative_integer(timeline.get(field))
            for field in (modern_timeline_fields if modern else legacy_timeline_fields)
        )
        or manifest.get("midi_timing")
        != {
            "basis": "SMPTE",
            "fps": 25,
            "ticks_per_frame": 40,
            "nominal_resolution_ms": 1,
        }
        or manifest.get("synchronization_boundary")
        != (
            "bounded audio process-spawn alignment; not sample-accurate WAV/MIDI synchronization"
            if modern
            else "process-start alignment only; not sample-accurate WAV/MIDI synchronization"
        )
        or manifest.get("artifacts")
        != (
            {
                "mix_wav": artifacts.get("mix_wav"),
                "roland_midi_smf": artifacts.get("roland_midi_smf"),
            }
            if modern
            else {
                "vocal_wav": artifacts.get("vocal_wav"),
                "roland_midi_smf": artifacts.get("roland_midi_smf"),
            }
        )
        or not isinstance(manifest.get("midi_event_counts"), dict)
        or set(manifest.get("midi_event_counts", {})) != midi_count_fields
        or any(
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in manifest.get("midi_event_counts", {}).items()
        )
        or not isinstance(manifest.get("midi_note_velocity"), dict)
        or set(manifest.get("midi_note_velocity", {})) != {"minimum", "maximum"}
        or any(
            value is not None
            and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 127)
            for value in manifest.get("midi_note_velocity", {}).values()
        )
        or (
            manifest.get("midi_note_velocity", {}).get("minimum") is not None
            and manifest.get("midi_note_velocity", {}).get("maximum") is not None
            and manifest["midi_note_velocity"]["minimum"]
            > manifest["midi_note_velocity"]["maximum"]
        )
        or manifest.get("does_not_establish") != artifacts.get("does_not_establish")
    ):
        raise RecordingError("performance take manifest is invalid")
    if modern:
        ordered = [
            timeline["midi_spawn_requested_offset_ns"],
            timeline["midi_running_observed_offset_ns"],
            timeline["voice_spawn_requested_offset_ns"],
            timeline["roland_spawn_requested_offset_ns"],
            timeline["voice_running_observed_offset_ns"],
            timeline["roland_running_observed_offset_ns"],
            timeline["session_ready_offset_ns"],
        ]
        if (
            ordered != sorted(ordered)
            or timeline["audio_spawn_spread_ns"]
            != timeline["roland_spawn_requested_offset_ns"]
            - timeline["voice_spawn_requested_offset_ns"]
            or timeline["audio_spawn_spread_ns"] > MAX_AUDIO_SPAWN_SPREAD_NS
        ):
            raise RecordingError("performance audio spawn receipt is invalid")
    elif [
        timeline.get("midi_spawn_requested_offset_ns"),
        timeline.get("midi_running_observed_offset_ns"),
        timeline.get("audio_spawn_requested_offset_ns"),
        timeline.get("audio_running_observed_offset_ns"),
        timeline.get("session_ready_offset_ns"),
    ] != sorted(timeline.values()):
        raise RecordingError("legacy performance timeline is invalid")
    return manifest


def _validate_result(result: dict[str, Any], spec: dict[str, Any]) -> None:
    common = {
        "schema_version",
        "kind",
        "session_id",
        "status",
        "reason",
        "plan_sha256",
        "does_not_establish",
    }
    if (
        result.get("schema_version") != 1
        or result.get("kind") != "audio_recording_result"
        or result.get("session_id") != spec.get("session_id")
        or result.get("plan_sha256") != spec.get("plan_sha256")
        or not isinstance(result.get("reason"), str)
        or not result["reason"]
        or CONTROL_RE.search(result["reason"])
        or not isinstance(result.get("does_not_establish"), list)
        or not result["does_not_establish"]
        or any(
            not isinstance(item, str) or not item or CONTROL_RE.search(item)
            for item in result["does_not_establish"]
        )
    ):
        raise RecordingError("recording result common schema is invalid")
    status = result.get("status")
    final_path = pathlib.Path(spec["paths"]["final"])
    partial_path = pathlib.Path(spec["paths"]["partial"])
    capture = spec["plan_identity"]["capture"]
    maximum_bytes = int(capture["maximum_file_bytes"])
    maximum_duration = int(capture["maximum_duration_seconds"])
    if status == "completed":
        if spec["plan_identity"]["session_type"] == "piano-vocal-performance":
            modern = _is_modern_performance_plan(spec["plan_identity"])
            expected = common | {
                "started_at",
                "completed_at",
                "processes",
                "artifacts",
                "midi_event_counts",
            }
            if set(result) != expected:
                raise RecordingError("completed performance result fields are invalid")
            if (
                result.get("reason") not in {"requested-stop", "maximum-duration"}
                or not _valid_timestamp(result.get("started_at"))
                or not _valid_timestamp(result.get("completed_at"))
                or dt.datetime.fromisoformat(result["completed_at"])
                < dt.datetime.fromisoformat(result["started_at"])
            ):
                raise RecordingError("completed performance timeline is invalid")
            processes = result.get("processes")
            if (
                not isinstance(processes, dict)
                or set(processes)
                != (
                    {"voice", "roland", "midi", "mix", "forced_kill"}
                    if modern
                    else {"audio", "midi", "forced_kill"}
                )
                or processes.get("forced_kill") is not False
            ):
                raise RecordingError("completed performance process receipt is invalid")
            child_specs = (
                (
                    ("voice", {0, -signal.SIGINT}),
                    ("roland", {0, -signal.SIGINT}),
                    ("midi", {0}),
                    ("mix", {0}),
                )
                if modern
                else (("audio", {0, -signal.SIGINT}), ("midi", {0}))
            )
            for name, accepted in child_specs:
                child = processes.get(name)
                if (
                    not isinstance(child, dict)
                    or set(child) != {"returncode", "stderr_bytes", "stderr_sha256"}
                    or child.get("returncode") not in accepted
                    or isinstance(child.get("stderr_bytes"), bool)
                    or not isinstance(child.get("stderr_bytes"), int)
                    or child["stderr_bytes"] < 0
                    or not isinstance(child.get("stderr_sha256"), str)
                    or HEX64_RE.fullmatch(child["stderr_sha256"]) is None
                ):
                    raise RecordingError("completed performance child receipt is invalid")
            artifacts = result.get("artifacts")
            artifact_keys = (
                {"mix_wav", "roland_midi_smf", "take_manifest"}
                if modern
                else {"vocal_wav", "roland_midi_smf", "take_manifest"}
            )
            if not isinstance(artifacts, dict) or set(artifacts) != artifact_keys:
                raise RecordingError("completed performance artifacts are invalid")
            _assert_artifact_binding_current(
                artifacts["mix_wav"] if modern else artifacts["vocal_wav"],
                expected_path=pathlib.Path(spec["paths"]["final"]),
                maximum_bytes=maximum_bytes,
                detail_fields=ARTIFACT_DETAIL_FIELDS,
            )
            _assert_artifact_binding_current(
                artifacts["roland_midi_smf"],
                expected_path=pathlib.Path(spec["paths"]["midi_final"]),
                maximum_bytes=MIDI.MAX_MIDI_BYTES,
            )
            _assert_artifact_binding_current(
                artifacts["take_manifest"],
                expected_path=pathlib.Path(spec["paths"]["manifest_final"]),
                maximum_bytes=MAX_JSON_BYTES,
            )
            manifest_artifacts = dict(artifacts)
            manifest_artifacts["does_not_establish"] = result["does_not_establish"]
            manifest = _validate_performance_manifest(
                pathlib.Path(spec["paths"]["manifest_final"]), spec, manifest_artifacts
            )
            counts = result.get("midi_event_counts")
            if (
                not isinstance(counts, dict)
                or any(
                    not isinstance(key, str)
                    or isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    for key, value in counts.items()
                )
                or counts != manifest["midi_event_counts"]
            ):
                raise RecordingError("completed performance MIDI receipt is invalid")
            return
        expected = common | {"started_at", "completed_at", "process", "artifact"}
        if set(result) != expected:
            raise RecordingError("completed recording result fields are invalid")
        started_at = result.get("started_at")
        completed_at = result.get("completed_at")
        if (
            result.get("reason") not in {"requested-stop", "maximum-duration"}
            or not _valid_timestamp(started_at)
            or not _valid_timestamp(completed_at)
            or dt.datetime.fromisoformat(completed_at)
            < dt.datetime.fromisoformat(started_at)
        ):
            raise RecordingError("completed recording result timeline is invalid")
        process = result.get("process")
        if (
            not isinstance(process, dict)
            or set(process)
            != {
                "returncode",
                "forced_kill",
                "stderr_bytes",
                "stderr_sha256",
                "stderr_truncated",
            }
            or isinstance(process.get("returncode"), bool)
            or not isinstance(process.get("returncode"), int)
            or process.get("returncode") not in {0, -signal.SIGINT}
            or process.get("forced_kill") is not False
            or isinstance(process.get("stderr_bytes"), bool)
            or not isinstance(process.get("stderr_bytes"), int)
            or process["stderr_bytes"] < 0
            or not isinstance(process.get("stderr_sha256"), str)
            or not HEX64_RE.fullmatch(process["stderr_sha256"])
            or process.get("stderr_truncated") is not False
        ):
            raise RecordingError("completed recording process receipt is invalid")
        artifact = result.get("artifact")
        _assert_artifact_binding_current(
            artifact,
            expected_path=final_path,
            maximum_bytes=maximum_bytes,
            detail_fields=ARTIFACT_DETAIL_FIELDS,
        )
        if (
            not isinstance(artifact, dict)
            or artifact.get("channels") != capture.get("channels")
            or artifact.get("bit_depth_container") != 32
            or artifact.get("sample_rate_hz") != capture.get("sample_rate_hz")
            or isinstance(artifact.get("frames"), bool)
            or not isinstance(artifact.get("frames"), int)
            or artifact["frames"] < 1
            or isinstance(artifact.get("duration_seconds"), bool)
            or not isinstance(artifact.get("duration_seconds"), (int, float))
            or artifact["duration_seconds"] <= 0
            or artifact["duration_seconds"] > maximum_duration + 2
        ):
            raise RecordingError("completed recording artifact receipt is invalid")
        return
    if status != "failed-preserved":
        raise RecordingError("recording result status is invalid")
    performance_failure_fields = (
        {"performance_artifacts"}
        if spec["plan_identity"]["session_type"] == "piano-vocal-performance"
        else set()
    )
    recovery_fields = common | {"recovered_at", "partial", "final"} | performance_failure_fields
    worker_fields = common | {"failed_at", "error", "partial"} | performance_failure_fields
    fields = set(result)
    if fields == recovery_fields:
        if not _valid_timestamp(result.get("recovered_at")):
            raise RecordingError("recovered recording timestamp is invalid")
        _assert_artifact_binding_current(
            result.get("final"), expected_path=final_path, maximum_bytes=maximum_bytes
        )
    elif fields == worker_fields:
        if (
            not _valid_timestamp(result.get("failed_at"))
            or not isinstance(result.get("error"), str)
            or not result["error"]
            or CONTROL_RE.search(result["error"])
        ):
            raise RecordingError("failed recording result detail is invalid")
    else:
        raise RecordingError("failed recording result fields are invalid")
    if performance_failure_fields:
        inventory = result.get("performance_artifacts")
        modern = _is_modern_performance_plan(spec["plan_identity"])
        expected_inventory = (
            ({
                "mix_partial",
                "mix_final",
                "voice_stem_partial",
                "roland_stem_partial",
                "midi_partial",
                "midi_final",
                "manifest_partial",
                "manifest_final",
            } | ({"mix_raw_partial"} if "mix_raw_partial" in spec["paths"] else set()))
            if modern
            else {
                "wav_partial",
                "wav_final",
                "midi_partial",
                "midi_final",
                "manifest_partial",
                "manifest_final",
            }
        )
        if (
            not isinstance(inventory, dict)
            or set(inventory) != expected_inventory
            or inventory != _performance_path_inventory(spec)
        ):
            raise RecordingError("failed performance artifact inventory is invalid")
    _assert_artifact_binding_current(
        result.get("partial"), expected_path=partial_path, maximum_bytes=maximum_bytes
    )


def _read_session(
    state_root: pathlib.Path, session_id: str
) -> tuple[dict[str, pathlib.Path], dict[str, Any], dict[str, Any]]:
    paths = _session_paths(state_root, session_id)
    spec, spec_binding = _safe_json_read_with_binding(
        paths["spec"], require_private=True
    )
    _validate_persisted_spec(spec, state_root=state_root)
    state = _safe_json_read(paths["state"], require_private=True)
    _validate_session_state(
        state, session_id=session_id, spec_sha256=spec_binding["sha256"]
    )
    return paths, spec, state


def _bounded_path_binding(
    path: pathlib.Path, maximum_bytes: int
) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return _safe_regular_binding(
            path,
            maximum_bytes=maximum_bytes,
            require_private=True,
            include_identity=True,
        )
    except RecordingError as exc:
        return {"path": str(path), "error": str(exc)}


def _performance_path_inventory(spec: dict[str, Any]) -> dict[str, Any]:
    capture_maximum = int(spec["plan_identity"]["capture"]["maximum_file_bytes"])
    modern = _is_modern_performance_plan(spec["plan_identity"])
    limits = {
        "wav_partial": ("partial", capture_maximum),
        "wav_final": ("final", capture_maximum),
        "midi_partial": ("midi_partial", MIDI.MAX_MIDI_BYTES),
        "midi_final": ("midi_final", MIDI.MAX_MIDI_BYTES),
        "manifest_partial": ("manifest_partial", MAX_JSON_BYTES),
        "manifest_final": ("manifest_final", MAX_JSON_BYTES),
    }
    if modern:
        limits = {
            "mix_partial": ("partial", capture_maximum),
            "mix_final": ("final", capture_maximum),
            "voice_stem_partial": ("voice_partial", capture_maximum),
            "roland_stem_partial": ("roland_partial", capture_maximum),
            "midi_partial": ("midi_partial", MIDI.MAX_MIDI_BYTES),
            "midi_final": ("midi_final", MIDI.MAX_MIDI_BYTES),
            "manifest_partial": ("manifest_partial", MAX_JSON_BYTES),
            "manifest_final": ("manifest_final", MAX_JSON_BYTES),
        }
        if "mix_raw_partial" in spec["paths"]:
            limits["mix_raw_partial"] = ("mix_raw_partial", capture_maximum)
    return {
        label: _bounded_path_binding(pathlib.Path(spec["paths"][key]), maximum)
        for label, (key, maximum) in limits.items()
    }


def session_status(
    *, state_root: pathlib.Path = DEFAULT_STATE_ROOT, session_id: str | None = None
) -> dict[str, Any]:
    root = lexical_absolute(state_root)
    resolved_id = _resolve_session_id(root, session_id)
    paths, spec, state = _read_session(root, resolved_id)
    result: dict[str, Any] | None = None
    if paths["result"].exists() or paths["result"].is_symlink():
        result = _safe_json_read(paths["result"], require_private=True)
        _validate_result(result, spec)
    process = state.get("process")
    exact_alive = isinstance(process, dict) and _identity_matches(process)
    pid_alive = (
        isinstance(process, dict) and _proc_identity(process.get("pid")) is not None
    )
    if result is not None:
        status = result.get("status", "terminal")
        recovery_required = False
    elif exact_alive:
        status = "running"
        recovery_required = False
    elif pid_alive:
        status = "identity-mismatch"
        recovery_required = True
    else:
        status = "recovery-required"
        recovery_required = True
    maximum = int(spec["plan_identity"]["capture"]["maximum_file_bytes"])
    partial = pathlib.Path(spec["paths"]["partial"])
    final = pathlib.Path(spec["paths"]["final"])
    report = {
        "schema_version": 1,
        "kind": "audio_recording_status",
        "session_id": resolved_id,
        "session_type": spec["plan_identity"]["session_type"],
        "status": status,
        "recovery_required": recovery_required,
        "process_identity_exact": exact_alive,
        "plan_sha256": spec.get("plan_sha256"),
        "partial": _bounded_path_binding(partial, maximum),
        "final": _bounded_path_binding(final, maximum),
        "result": result,
    }
    if spec["plan_identity"]["session_type"] == "piano-vocal-performance":
        report["midi_final"] = _bounded_path_binding(
            pathlib.Path(spec["paths"]["midi_final"]), MIDI.MAX_MIDI_BYTES
        )
        report["manifest_final"] = _bounded_path_binding(
            pathlib.Path(spec["paths"]["manifest_final"]), MAX_JSON_BYTES
        )
    return report


def _restricted_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("HOME", "XDG_RUNTIME_DIR", "PULSE_SERVER", "PULSE_COOKIE")
        if key in os.environ
    }
    environment.update({"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"})
    return environment


def _capture_partial_started(path: pathlib.Path) -> bool:
    """Recognize a regular capture file that has progressed beyond a WAV header."""

    try:
        observed = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(observed.st_mode) and observed.st_size > 44


def _recording_startup_ready(
    spec: dict[str, Any], *, worker_ready_path: pathlib.Path
) -> bool:
    """Use the plan's actual live capture artifacts for bounded start readiness."""

    plan = spec["plan_identity"]
    paths = spec["paths"]
    if plan["session_type"] != "piano-vocal-performance":
        return _capture_partial_started(pathlib.Path(paths["partial"]))
    if _is_modern_performance_plan(plan):
        audio_paths = (paths["voice_partial"], paths["roland_partial"])
    else:
        audio_paths = (paths["partial"],)
    return worker_ready_path.is_file() and all(
        _capture_partial_started(pathlib.Path(path)) for path in audio_paths
    )


def start_session(
    name: str,
    maximum_seconds: int,
    expected_plan_sha256: str,
    *,
    session_type: str = "voice-recording",
    output_root: pathlib.Path = DEFAULT_OUTPUT_ROOT,
    state_root: pathlib.Path = DEFAULT_STATE_ROOT,
    physical_state: pathlib.Path = PHYSICAL.DEFAULT_STATE,
    laboratory_state: pathlib.Path = LAB.DEFAULT_STATE,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256):
        raise RecordingError("expected plan digest is invalid")
    state_root = lexical_absolute(state_root)
    with state_lock(state_root):
        plan = build_plan(
            name,
            maximum_seconds,
            session_type=session_type,
            output_root=output_root,
            state_root=state_root,
            physical_state=physical_state,
            laboratory_state=laboratory_state,
        )
        if plan["plan_sha256"] != expected_plan_sha256:
            raise RecordingError(
                "recording plan changed; review the new plan before start"
            )
        if plan["ready"] is not True:
            raise RecordingError(
                "recording plan is blocked: " + ", ".join(plan["readiness"]["blockers"])
            )
        contract = load_catalog(session_type)
        source_name = _source_name_for_contract(contract["source"])
        identity = plan["identity"]["source"]["identity"]
        audio_identity = (
            identity.get("audio")
            if session_type == "piano-vocal-performance"
            else identity
        )
        if hashlib.sha256(source_name.encode("utf-8")).hexdigest() != audio_identity.get(
            "node_name_sha256"
        ):
            raise RecordingError("recording source changed between plan and start")
        midi_source: str | None = None
        roland_source_name: str | None = None
        if session_type == "piano-vocal-performance":
            roland_source_name = _roland_audio_source_name_for_contract(contract["source"])
            roland_identity = identity.get("roland_audio", {})
            if hashlib.sha256(roland_source_name.encode("utf-8")).hexdigest() != roland_identity.get(
                "node_name_sha256"
            ):
                raise RecordingError("Roland audio source changed between plan and start")
            midi_source = _midi_address_for_contract()
            if midi_source != identity.get("midi", {}).get("address"):
                raise RecordingError("Roland MIDI source changed between plan and start")
        session_id = secrets.token_hex(12)
        paths = _session_paths(state_root, session_id)
        output_path = pathlib.Path(plan["identity"]["output"]["path"])
        partial_name = f".{output_path.stem}.{session_id}.partial.wav"
        partial_path = output_path.parent / partial_name
        performance = plan["identity"].get("performance")
        midi_final = pathlib.Path(performance["midi_output"]["path"]) if performance else None
        manifest_final = pathlib.Path(performance["manifest_output"]["path"]) if performance else None
        midi_partial = (
            output_path.parent / f".{output_path.stem}.{session_id}.partial.mid"
            if performance
            else None
        )
        manifest_partial = (
            output_path.parent / f".{output_path.stem}.{session_id}.partial.take.json"
            if performance
            else None
        )
        voice_partial = (
            output_path.parent / f".{output_path.stem}.{session_id}.voice.partial.wav"
            if performance
            else None
        )
        roland_partial = (
            output_path.parent / f".{output_path.stem}.{session_id}.roland.partial.wav"
            if performance
            else None
        )
        mix_raw_partial = (
            output_path.parent / f".{output_path.stem}.{session_id}.mix.partial.s32le"
            if performance
            else None
        )
        candidates = [partial_path, output_path]
        candidates.extend(
            path
            for path in (
                voice_partial,
                roland_partial,
                mix_raw_partial,
                midi_partial,
                midi_final,
                manifest_partial,
                manifest_final,
            )
            if path is not None
        )
        if any(path.exists() or path.is_symlink() for path in candidates):
            raise RecordingError("recording partial path already exists")
        spec = {
            "schema_version": 1,
            "kind": "audio_recording_session_spec",
            "session_id": session_id,
            "created_at": utc_now(),
            "plan_sha256": plan["plan_sha256"],
            "plan_identity": plan["identity"],
            "paths": {
                "partial": str(partial_path),
                "final": str(output_path),
                "result": str(paths["result"]),
            },
        }
        if performance:
            spec["midi_source"] = midi_source
            spec["source_names"] = {
                "voice": source_name,
                "roland": roland_source_name,
            }
            spec["paths"].update(
                {
                    "voice_partial": str(voice_partial),
                    "roland_partial": str(roland_partial),
                    "mix_raw_partial": str(mix_raw_partial),
                    "midi_partial": str(midi_partial),
                    "midi_final": str(midi_final),
                    "manifest_partial": str(manifest_partial),
                    "manifest_final": str(manifest_final),
                }
            )
        else:
            spec["source_name"] = source_name
        _atomic_private_json(paths["spec"], spec, create_only=True)
        spec_binding = _safe_regular_binding(paths["spec"], require_private=True)
        state = {
            "schema_version": 1,
            "kind": "audio_recording_session_state",
            "session_id": session_id,
            "spec_sha256": spec_binding["sha256"],
            "started_at": utc_now(),
            "phase": "starting",
            "process": None,
        }
        _atomic_private_json(paths["state"], state, create_only=True)
        active = {
            "schema_version": 1,
            "kind": "audio_recording_active",
            "session_id": session_id,
            "spec_sha256": spec_binding["sha256"],
        }
        _atomic_private_json(paths["active"], active, create_only=True)
        argv = [
            sys.executable,
            str(pathlib.Path(__file__).resolve()),
            "_worker",
            "--spec",
            str(paths["spec"]),
            "--expected-spec-sha256",
            spec_binding["sha256"],
        ]
        process: subprocess.Popen[bytes] | None = None
        process_identity: dict[str, Any] | None = None
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(ROOT),
                env=_restricted_environment(),
                start_new_session=True,
                close_fds=True,
            )
            for _ in range(100):
                process_identity = _proc_identity(process.pid)
                if process_identity is not None:
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            if process_identity is None:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                raise RecordingError(
                    "recording worker identity could not be established"
                )
            state["phase"] = "running"
            state["process"] = process_identity
            _atomic_private_json(paths["state"], state)
        except Exception as exc:
            terminated = True
            if process_identity is not None:
                terminated = _terminate_exact_process(
                    process_identity,
                    grace_seconds=float(
                        plan["identity"]["capture"]["stop_grace_seconds"]
                    ),
                )
            elif process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    terminated = False
            if not terminated:
                raise RecordingError(
                    "recording bootstrap failed and the exact worker could not be terminated"
                ) from exc
            raise RecordingError(
                f"recording bootstrap failed; recover session {session_id}: {exc}"
            ) from exc
    startup_windows = 2 if session_type == "piano-vocal-performance" else 1
    deadline = time.monotonic() + (
        plan["identity"]["capture"]["startup_timeout_seconds"] * startup_windows
    )
    while time.monotonic() < deadline:
        if _recording_startup_ready(spec, worker_ready_path=paths["ready"]):
            return {
                "schema_version": 1,
                "kind": "audio_recording_start_receipt",
                "session_id": session_id,
                "session_type": session_type,
                "status": "running",
                "plan_sha256": plan["plan_sha256"],
                "output": str(output_path),
                "maximum_duration_seconds": maximum_seconds,
            }
        if not _identity_matches(process_identity):
            break
        time.sleep(0.05)
    if process_identity is not None and not _terminate_exact_process(
        process_identity,
        grace_seconds=float(plan["identity"]["capture"]["stop_grace_seconds"]),
    ):
        raise RecordingError(
            f"recording worker did not become ready and could not be terminated; "
            f"recover session {session_id}"
        )
    raise RecordingError(
        f"recording worker did not become ready; recover session {session_id}"
    )


def _clear_active_if_matches(state_root: pathlib.Path, session_id: str) -> None:
    active = lexical_absolute(state_root) / "active.json"
    if not active.exists() and not active.is_symlink():
        return
    current = _safe_json_read(active, require_private=True)
    if current.get("session_id") != session_id:
        raise RecordingError("active pointer belongs to another recording session")
    active.unlink()
    directory_fd = os.open(active.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def stop_session(
    *, state_root: pathlib.Path = DEFAULT_STATE_ROOT, session_id: str | None = None
) -> dict[str, Any]:
    root = lexical_absolute(state_root)
    with state_lock(root):
        resolved_id = _resolve_session_id(root, session_id)
        paths, spec, state = _read_session(root, resolved_id)
        if paths["result"].exists():
            result = session_status(state_root=root, session_id=resolved_id)
            _clear_active_if_matches(root, resolved_id)
            return result
        process = state.get("process")
        if not isinstance(process, dict) or not _identity_matches(process):
            raise RecordingError(
                "recording process identity is not exact; use recovery"
            )
        terminated = _terminate_exact_process(
            process,
            grace_seconds=float(spec["plan_identity"]["capture"]["stop_grace_seconds"])
            + 5.0,
        )
        if not terminated:
            raise RecordingError(
                "recording process did not terminate after bounded stop"
            )
        status = session_status(state_root=root, session_id=resolved_id)
        if status["recovery_required"] is False:
            _clear_active_if_matches(root, resolved_id)
        return status


def recover_session(
    *, state_root: pathlib.Path = DEFAULT_STATE_ROOT, session_id: str | None = None
) -> dict[str, Any]:
    root = lexical_absolute(state_root)
    with state_lock(root):
        resolved_id = _resolve_session_id(root, session_id)
        paths, spec, state = _read_session(root, resolved_id)
        if paths["result"].exists() or paths["result"].is_symlink():
            result = session_status(state_root=root, session_id=resolved_id)
            _clear_active_if_matches(root, resolved_id)
            return result
        process = state.get("process")
        if isinstance(process, dict) and _identity_matches(process):
            return session_status(state_root=root, session_id=resolved_id)
        if isinstance(process, dict) and _proc_identity(process.get("pid")) is not None:
            raise RecordingError(
                "PID was reused or changed; recovery remains fail-closed"
            )
        maximum = int(spec["plan_identity"]["capture"]["maximum_file_bytes"])
        partial = pathlib.Path(spec["paths"]["partial"])
        final = pathlib.Path(spec["paths"]["final"])
        result = {
            "schema_version": 1,
            "kind": "audio_recording_result",
            "session_id": resolved_id,
            "status": "failed-preserved",
            "reason": "worker-exited-without-terminal-receipt",
            "recovered_at": utc_now(),
            "plan_sha256": spec["plan_sha256"],
            "partial": _bounded_path_binding(partial, maximum),
            "final": _bounded_path_binding(final, maximum),
            "does_not_establish": [
                "valid-finalized-wav",
                "safe-reuse-of-a-partial-file",
                "successful-recording",
            ],
        }
        if spec["plan_identity"]["session_type"] == "piano-vocal-performance":
            result["performance_artifacts"] = _performance_path_inventory(spec)
        _atomic_private_json(paths["result"], result, create_only=True)
        _clear_active_if_matches(root, resolved_id)
        return session_status(state_root=root, session_id=resolved_id)


def _validate_spec(spec: dict[str, Any]) -> None:
    _validate_persisted_spec(spec)
    if (
        _is_modern_performance_plan(spec["plan_identity"])
        and "mix_raw_partial" not in spec["paths"]
    ):
        raise RecordingError("pre-raw modern performance spec is recovery-only")
    if (
        _is_modern_performance_plan(spec["plan_identity"])
        and _performance_audio_capture_generation(spec["plan_identity"])
        != "bounded-tail-v1"
    ):
        raise RecordingError("pre-bounded-tail modern performance spec is recovery-only")
    if spec["plan_identity"].get("contracts") != contract_bindings():
        raise RecordingError(
            "recording implementation contracts changed after planning"
        )
    if spec["plan_identity"].get("parecord") != parecord_binding():
        raise RecordingError("parecord changed after planning")
    performance_mode = (
        spec["plan_identity"].get("session_type") == "piano-vocal-performance"
    )
    if performance_mode:
        if not _is_modern_performance_plan(spec["plan_identity"]):
            raise RecordingError("legacy performance takes are readable but cannot be captured")
        if spec["plan_identity"]["performance"].get("arecordmidi") != arecordmidi_binding():
            raise RecordingError("arecordmidi changed after planning")
        if spec["plan_identity"]["performance"].get("ffmpeg") != ffmpeg_binding():
            raise RecordingError("ffmpeg changed after planning")
        if (
            spec.get("midi_source")
            != spec["plan_identity"]["source"]["identity"]["midi"]["address"]
        ):
            raise RecordingError("private Roland MIDI source does not match the plan")
    identity = spec["plan_identity"].get("source", {}).get("identity")
    if performance_mode:
        if not isinstance(identity, dict):
            raise RecordingError("private performance sources do not match the plan")
        for role, identity_key in (("voice", "audio"), ("roland", "roland_audio")):
            source_name = spec["source_names"].get(role)
            observed_identity = identity.get(identity_key)
            if (
                not isinstance(source_name, str)
                or not isinstance(observed_identity, dict)
                or hashlib.sha256(source_name.encode("utf-8")).hexdigest()
                != observed_identity.get("node_name_sha256")
            ):
                raise RecordingError("private performance sources do not match the plan")
    else:
        source_name = spec["source_name"]
        if not isinstance(identity, dict) or hashlib.sha256(
            source_name.encode("utf-8")
        ).hexdigest() != identity.get("node_name_sha256"):
            raise RecordingError("private recording source does not match the plan")


def _validate_recorded_wave(
    path: pathlib.Path, capture: dict[str, Any]
) -> dict[str, Any]:
    maximum_bytes = int(capture["maximum_file_bytes"])
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RecordingError("recorded WAV cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RecordingError("recorded WAV is not a regular file")
        if before.st_size < 1 or before.st_size > maximum_bytes:
            raise RecordingError("recorded WAV size is outside its bound")
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise RecordingError("recorded WAV must have private mode 0600")
        try:
            with os.fdopen(os.dup(descriptor), "rb", closefd=True) as stream:
                with wave.open(stream, "rb") as handle:
                    channels = handle.getnchannels()
                    sample_width = handle.getsampwidth()
                    rate = handle.getframerate()
                    frames = handle.getnframes()
                    compression = handle.getcomptype()
        except (wave.Error, EOFError, OSError) as exc:
            raise RecordingError("recorded file is not a readable PCM WAV") from exc
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 1_048_576):
            total += len(chunk)
            if total > maximum_bytes:
                raise RecordingError("recorded WAV grew beyond its bound")
            digest.update(chunk)
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, item) != getattr(after, item) for item in stable):
            raise RecordingError("recorded WAV changed while it was validated")
        if total != before.st_size:
            raise RecordingError("recorded WAV size changed while it was validated")
    finally:
        os.close(descriptor)
    if (
        channels != capture["channels"]
        or sample_width != 4
        or rate != capture["sample_rate_hz"]
        or compression != "NONE"
        or frames < 1
    ):
        raise RecordingError("recorded WAV format does not match the plan")
    duration = frames / rate
    if duration > capture["maximum_duration_seconds"] + 2:
        raise RecordingError("recorded WAV exceeds the planned duration")
    return {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "bytes": total,
        "mode": f"{stat.S_IMODE(before.st_mode):04o}",
        "device": before.st_dev,
        "inode": before.st_ino,
        "channels": channels,
        "bit_depth_container": sample_width * 8,
        "sample_rate_hz": rate,
        "frames": frames,
        "duration_seconds": round(duration, 6),
    }


def _publish_no_replace(
    partial: pathlib.Path, final: pathlib.Path, expected_binding: dict[str, Any]
) -> None:
    if partial.parent != final.parent:
        raise RecordingError("partial and final recording must share one directory")
    root = _validate_output_root(partial.parent)
    if final.exists() or final.is_symlink():
        raise RecordingError("final recording path appeared during capture")
    binding_fields = {
        key: expected_binding[key]
        for key in ("path", "sha256", "bytes", "mode", "device", "inode")
    }
    try:
        observed_partial = _safe_regular_binding(
            partial,
            maximum_bytes=max(1, int(binding_fields["bytes"])),
            require_private=True,
            include_identity=True,
        )
    except RecordingError as exc:
        raise RecordingError("recording partial changed after WAV validation") from exc
    if observed_partial != binding_fields:
        raise RecordingError("recording partial changed after WAV validation")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.link(
            partial.name,
            final.name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
            follow_symlinks=False,
        )
        os.fsync(descriptor)
        final_binding = _safe_regular_binding(
            final,
            maximum_bytes=max(1, int(binding_fields["bytes"])),
            require_private=True,
            include_identity=True,
        )
        expected_final = dict(binding_fields)
        expected_final["path"] = str(final)
        if final_binding != expected_final:
            os.unlink(final.name, dir_fd=descriptor)
            os.fsync(descriptor)
            raise RecordingError("published recording does not match the validated WAV")
        os.unlink(partial.name, dir_fd=descriptor)
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise RecordingError(
            "final recording path appeared during publication"
        ) from exc
    finally:
        os.close(descriptor)


def _worker_result_path(spec: dict[str, Any]) -> pathlib.Path:
    return lexical_absolute(pathlib.Path(spec["paths"]["result"]))


def _validate_live_preconditions(spec: dict[str, Any]) -> None:
    plan = spec["plan_identity"]
    contract = load_catalog(plan["session_type"])
    physical, physical_blockers = _physical_projection(
        pathlib.Path(plan["physical"]["state_path"]),
        contract["required_physical_facts"],
    )
    laboratory, laboratory_blockers = _hard_laboratory_projection(
        pathlib.Path(plan["laboratory"]["state_path"]),
        physical,
        contract["required_laboratory_gates"],
    )
    source, source_blockers = _source_projection(contract["source"])
    blockers = sorted(set(physical_blockers + laboratory_blockers + source_blockers))
    if blockers:
        raise RecordingError(
            "recording preconditions changed before capture: " + ", ".join(blockers)
        )
    if physical != plan.get("physical"):
        raise RecordingError("physical recording state changed before capture")
    if laboratory != plan.get("laboratory"):
        raise RecordingError("laboratory recording state changed before capture")
    if source != plan.get("source"):
        raise RecordingError("recording source identity changed before capture")
    output = plan["output"]
    final = pathlib.Path(output["path"])
    root = _validate_output_root(pathlib.Path(output["root"]))
    if final.parent != root or final.exists() or final.is_symlink():
        raise RecordingError("recording output changed before capture")
    performance = plan.get("performance")
    if performance:
        for output_contract in (
            performance["midi_output"],
            performance["manifest_output"],
        ):
            candidate = pathlib.Path(output_contract["path"])
            if candidate.parent != root or candidate.exists() or candidate.is_symlink():
                raise RecordingError("performance output changed before capture")
    capture = plan["capture"]
    free_bytes = int(shutil.disk_usage(root).free)
    required = int(capture["maximum_file_bytes"]) + int(
        capture["free_space_reserve_bytes"]
    )
    if performance:
        required += 3 * int(capture["maximum_file_bytes"]) + MIDI.MAX_MIDI_BYTES + MAX_JSON_BYTES
    if free_bytes < required:
        raise RecordingError("free space fell below the recording budget")


def _parecord_argv(
    spec: dict[str, Any],
    parecord_path: pathlib.Path,
    partial: pathlib.Path,
    *,
    source_name: str | None = None,
    stream_role: str | None = None,
) -> list[str]:
    plan = spec["plan_identity"]
    capture = plan["capture"]
    process = plan["process"]
    session_id = spec["session_id"]
    if source_name is None:
        source_names = spec.get("source_names")
        source_name = (
            source_names.get("voice")
            if isinstance(source_names, dict)
            else spec.get("source_name")
        )
    if not isinstance(source_name, str) or not source_name:
        raise RecordingError("recording capture source name is invalid")
    stream_name = f"{process['stream_name_prefix']}-{session_id}"
    if stream_role is not None:
        if stream_role not in {"voice", "roland"}:
            raise RecordingError("performance capture stream role is invalid")
        stream_name = f"{stream_name}-{stream_role}"
    return [
        str(parecord_path),
        "--record",
        f"--device={source_name}",
        f"--rate={capture['sample_rate_hz']}",
        f"--format={capture['sample_format']}",
        f"--channels={capture['channels']}",
        f"--channel-map={capture['channel_map']}",
        "--no-remix",
        "--no-remap",
        "--file-format=wav",
        f"--client-name={process['client_name']}",
        f"--stream-name={stream_name}",
        str(partial),
    ]


def _link_no_replace_keep_partial(
    partial: pathlib.Path,
    final: pathlib.Path,
    expected_binding: dict[str, Any],
    *,
    maximum_bytes: int,
) -> dict[str, Any]:
    if partial.parent != final.parent:
        raise RecordingError("performance partial and final must share one directory")
    root = _validate_output_root(partial.parent)
    if final.exists() or final.is_symlink():
        raise RecordingError("performance final path appeared during capture")
    observed = _safe_regular_binding(
        partial,
        maximum_bytes=maximum_bytes,
        require_private=True,
        include_identity=True,
    )
    if observed != _artifact_binding_fields(expected_binding):
        raise RecordingError("performance partial changed after validation")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.link(
            partial.name,
            final.name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
            follow_symlinks=False,
        )
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise RecordingError("performance final path appeared during publication") from exc
    finally:
        os.close(descriptor)
    final_binding = _safe_regular_binding(
        final,
        maximum_bytes=maximum_bytes,
        require_private=True,
        include_identity=True,
    )
    expected_final = dict(observed)
    expected_final["path"] = str(final)
    if final_binding != expected_final:
        raise RecordingError("published performance artifact changed")
    return final_binding


def _child_preexec(parent_pid: int) -> None:
    """Arrange hard worker-death cleanup before the capture binary execs."""

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        os._exit(126)
    if os.getppid() != parent_pid:
        os._exit(126)


def _spawn_capture_child(argv: list[str], stderr: Any) -> subprocess.Popen[bytes]:
    parent_pid = os.getpid()
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        env=_restricted_environment(),
        close_fds=True,
        start_new_session=True,
        preexec_fn=lambda: _child_preexec(parent_pid),
    )


def _signal_owned_child_group(child: subprocess.Popen[bytes], signum: int) -> None:
    if child.poll() is not None:
        return
    try:
        process_group = os.getpgid(child.pid)
    except ProcessLookupError:
        return
    if process_group != child.pid:
        raise RecordingError("capture child is not leader of its bound process group")
    os.killpg(process_group, signum)


def _stop_capture_children(
    children: list[subprocess.Popen[bytes]], grace_seconds: int
) -> tuple[list[int], bool]:
    for child in children:
        _signal_owned_child_group(child, signal.SIGINT)
    returncodes: list[int] = []
    forced_kill = False
    for child in children:
        try:
            returncodes.append(child.wait(timeout=grace_seconds))
        except subprocess.TimeoutExpired:
            _signal_owned_child_group(child, signal.SIGKILL)
            returncodes.append(child.wait(timeout=5))
            forced_kill = True
    return returncodes, forced_kill


def _performance_child_exit_codes_clean(returncodes: list[int]) -> bool:
    """Require two audio children and arecordmidi to finalise cleanly.

    The two-child form remains accepted for regression checks of the retired
    legacy worker receipt. New workers always pass three children.
    """

    if len(returncodes) == 2:
        return returncodes[0] in {0, -signal.SIGINT} and returncodes[1] == 0
    return (
        len(returncodes) == 3
        and returncodes[0] in {0, -signal.SIGINT}
        and returncodes[1] in {0, -signal.SIGINT}
        and returncodes[2] == 0
    )


def _wav_compatible_fsize_limit(maximum_file_bytes: int) -> int:
    """Keep libsndfile WAV startup viable while retaining a hard file-size ceiling."""

    return max(maximum_file_bytes, PARECORD_WAV_FSIZE_FLOOR_BYTES)


def _midi_capture_process_ready(
    child: subprocess.Popen[bytes], partial: pathlib.Path
) -> bool:
    """Observe arecordmidi startup without requiring buffered SMF bytes."""

    if child.poll() is not None:
        return False
    try:
        observed = partial.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_uid == os.geteuid()
        and observed.st_nlink == 1
        and stat.S_IMODE(observed.st_mode) == 0o600
        and observed.st_size <= MIDI.MAX_MIDI_BYTES
    )


def _performance_mix_frame_count(voice_frames: int, roland_frames: int) -> int:
    """Return the shortest valid stem length within the bounded tail tolerance."""

    if (
        isinstance(voice_frames, bool)
        or not isinstance(voice_frames, int)
        or voice_frames < 1
        or isinstance(roland_frames, bool)
        or not isinstance(roland_frames, int)
        or roland_frames < 1
    ):
        raise RecordingError("parallel audio stem frame counts are invalid")
    difference = abs(voice_frames - roland_frames)
    if difference > MAX_AUDIO_FRAME_DIFFERENCE_FRAMES:
        raise RecordingError(
            "parallel audio stems exceed the bounded frame difference "
            f"({difference}>{MAX_AUDIO_FRAME_DIFFERENCE_FRAMES})"
        )
    return min(voice_frames, roland_frames)


def _performance_worker_run(
    spec: dict[str, Any],
    parecord_path: pathlib.Path,
    arecordmidi_path: pathlib.Path,
    ffmpeg_path: pathlib.Path,
) -> dict[str, Any]:
    plan = spec["plan_identity"]
    capture = plan["capture"]
    paths = {
        key: lexical_absolute(pathlib.Path(value))
        for key, value in spec["paths"].items()
    }
    ready_path = _session_paths(
        paths["result"].parent, spec["session_id"]
    )["ready"]
    if ready_path.exists() or ready_path.is_symlink():
        raise RecordingError("performance ready receipt already exists")
    output_paths = [
        paths["partial"],
        paths["final"],
        paths["voice_partial"],
        paths["roland_partial"],
        paths["mix_raw_partial"],
        paths["midi_partial"],
        paths["midi_final"],
        paths["manifest_partial"],
        paths["manifest_final"],
    ]
    if len({path.parent for path in output_paths}) != 1:
        raise RecordingError("performance paths do not share an output root")
    _validate_output_root(output_paths[0].parent)
    if any(path.exists() or path.is_symlink() for path in output_paths):
        raise RecordingError("performance output or partial path already exists")
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    maximum_artifact = max(
        _wav_compatible_fsize_limit(int(capture["maximum_file_bytes"])),
        MIDI.MAX_MIDI_BYTES,
    )
    resource.setrlimit(resource.RLIMIT_FSIZE, (maximum_artifact, maximum_artifact))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    epoch_ns = time.monotonic_ns()
    started_at = utc_now()
    maximum_stderr = int(load_catalog("piano-vocal-performance")["capture"]["maximum_stderr_bytes"])
    children: list[subprocess.Popen[bytes]] = []
    midi_process: subprocess.Popen[bytes] | None = None
    voice_process: subprocess.Popen[bytes] | None = None
    roland_process: subprocess.Popen[bytes] | None = None
    timeline: dict[str, int] = {}
    ready = False
    stop_reason = "startup-failed"
    returncodes: list[int] = []
    forced_kill = False
    with (
        tempfile.TemporaryFile() as voice_stderr,
        tempfile.TemporaryFile() as roland_stderr,
        tempfile.TemporaryFile() as midi_stderr,
    ):
        try:
            timeline["midi_spawn_requested_offset_ns"] = time.monotonic_ns() - epoch_ns
            midi_process = _spawn_capture_child(
                MIDI.arecordmidi_capture_argv(
                    arecordmidi_path, spec["midi_source"], paths["midi_partial"]
                ),
                midi_stderr,
            )
            children.append(midi_process)
            midi_deadline = time.monotonic() + int(capture["startup_timeout_seconds"])
            while time.monotonic() < midi_deadline:
                if midi_process.poll() is not None:
                    stop_reason = "midi-child-exited"
                    break
                midi_started = _midi_capture_process_ready(
                    midi_process, paths["midi_partial"]
                )
                if midi_started:
                    timeline["midi_running_observed_offset_ns"] = time.monotonic_ns() - epoch_ns
                    break
                time.sleep(0.02)
            else:
                midi_started = False
            if midi_started and midi_process.poll() is None:
                timeline["voice_spawn_requested_offset_ns"] = time.monotonic_ns() - epoch_ns
                voice_process = _spawn_capture_child(
                    _parecord_argv(
                        spec,
                        parecord_path,
                        paths["voice_partial"],
                        source_name=spec["source_names"]["voice"],
                        stream_role="voice",
                    ),
                    voice_stderr,
                )
                # Keep every successfully spawned child in the bounded cleanup
                # set before attempting the next spawn.  In particular, a
                # Roland-spawn failure must not leave the voice capture alive.
                children.append(voice_process)
                timeline["roland_spawn_requested_offset_ns"] = time.monotonic_ns() - epoch_ns
                timeline["audio_spawn_spread_ns"] = (
                    timeline["roland_spawn_requested_offset_ns"]
                    - timeline["voice_spawn_requested_offset_ns"]
                )
                if timeline["audio_spawn_spread_ns"] > MAX_AUDIO_SPAWN_SPREAD_NS:
                    raise RecordingError("parallel audio spawn spread exceeded its bound")
                roland_process = _spawn_capture_child(
                    _parecord_argv(
                        spec,
                        parecord_path,
                        paths["roland_partial"],
                        source_name=spec["source_names"]["roland"],
                        stream_role="roland",
                    ),
                    roland_stderr,
                )
                children.append(roland_process)
                children = [voice_process, roland_process, midi_process]
                audio_deadline = time.monotonic() + int(capture["startup_timeout_seconds"])
                voice_started = False
                roland_started = False
                while time.monotonic() < audio_deadline:
                    if (
                        voice_process.poll() is not None
                        or roland_process.poll() is not None
                        or midi_process.poll() is not None
                    ):
                        stop_reason = "capture-child-exited"
                        break
                    try:
                        voice_started = paths["voice_partial"].stat().st_size > 44
                    except FileNotFoundError:
                        voice_started = False
                    if voice_started and "voice_running_observed_offset_ns" not in timeline:
                        timeline["voice_running_observed_offset_ns"] = time.monotonic_ns() - epoch_ns
                    try:
                        roland_started = paths["roland_partial"].stat().st_size > 44
                    except FileNotFoundError:
                        roland_started = False
                    if roland_started and "roland_running_observed_offset_ns" not in timeline:
                        timeline["roland_running_observed_offset_ns"] = time.monotonic_ns() - epoch_ns
                    ready = voice_started and roland_started
                    if ready:
                        timeline["session_ready_offset_ns"] = time.monotonic_ns() - epoch_ns
                        _atomic_private_json(
                            ready_path,
                            {
                                "schema_version": 1,
                                "kind": "audio_recording_worker_ready",
                                "session_id": spec["session_id"],
                                "plan_sha256": spec["plan_sha256"],
                                "capture_timeline_offsets_ns": timeline,
                            },
                            create_only=True,
                        )
                        break
                    time.sleep(0.02)
            if ready:
                stop_reason = "maximum-duration"
                deadline = time.monotonic() + int(capture["maximum_duration_seconds"])
                while time.monotonic() < deadline:
                    if stop_requested:
                        stop_reason = "requested-stop"
                        break
                    if (
                        voice_process.poll() is not None
                        or roland_process.poll() is not None
                        or midi_process.poll() is not None
                    ):
                        stop_reason = "capture-child-exited"
                        break
                    time.sleep(0.02)
        finally:
            if children:
                returncodes, forced_kill = _stop_capture_children(
                    children, int(capture["stop_grace_seconds"])
                )
        voice_stderr.seek(0)
        voice_error = voice_stderr.read(maximum_stderr + 1)
        roland_stderr.seek(0)
        roland_error = roland_stderr.read(maximum_stderr + 1)
        midi_stderr.seek(0)
        midi_error = midi_stderr.read(maximum_stderr + 1)
    if (
        not ready
        or stop_reason not in {"requested-stop", "maximum-duration"}
        or not _performance_child_exit_codes_clean(returncodes)
        or forced_kill
        or len(voice_error) > maximum_stderr
        or len(roland_error) > maximum_stderr
        or len(midi_error) > maximum_stderr
    ):
        raise RecordingError(
            "parallel audio/MIDI capture children did not terminate through the bounded clean path "
            f"(ready={str(ready).lower()}, stop_reason={stop_reason}, "
            f"returncodes={returncodes}, forced_kill={str(forced_kill).lower()}, "
            f"voice_stderr_bytes={len(voice_error)}, roland_stderr_bytes={len(roland_error)}, "
            f"midi_stderr_bytes={len(midi_error)})"
        )
    paths["voice_partial"].chmod(0o600)
    paths["roland_partial"].chmod(0o600)
    paths["midi_partial"].chmod(0o600)
    for partial in (paths["voice_partial"], paths["roland_partial"], paths["midi_partial"]):
        descriptor = os.open(partial, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    voice_artifact = _validate_recorded_wave(paths["voice_partial"], capture)
    roland_artifact = _validate_recorded_wave(paths["roland_partial"], capture)
    mix_frames = _performance_mix_frame_count(
        int(voice_artifact["frames"]), int(roland_artifact["frames"])
    )
    try:
        midi_meta = MIDI.validate_smf(paths["midi_partial"])
    except MIDI.MidiCaptureError as exc:
        raise RecordingError(f"captured SMF is invalid: {exc}") from exc
    midi_artifact = _safe_regular_binding(
        paths["midi_partial"],
        maximum_bytes=MIDI.MAX_MIDI_BYTES,
        require_private=True,
        include_identity=True,
    )
    mix_argv = [
        str(ffmpeg_path),
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-n",
        "-i",
        str(paths["voice_partial"]),
        "-i",
        str(paths["roland_partial"]),
        "-filter_complex",
        "[0:a][1:a]amix=inputs=2:duration=shortest:dropout_transition=0:normalize=1,aformat=sample_fmts=s32:sample_rates=48000:channel_layouts=stereo[mix]",
        "-map",
        "[mix]",
        "-c:a",
        "pcm_s32le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-f",
        "s32le",
        str(paths["mix_raw_partial"]),
    ]
    with tempfile.TemporaryFile() as mix_stderr:
        mix_process = _spawn_capture_child(mix_argv, mix_stderr)
        mix_timeout = max(30, int(capture["maximum_duration_seconds"]) * 2)
        mix_forced_kill = False
        try:
            mix_returncode = mix_process.wait(timeout=mix_timeout)
        except subprocess.TimeoutExpired:
            _signal_owned_child_group(mix_process, signal.SIGKILL)
            mix_returncode = mix_process.wait(timeout=5)
            mix_forced_kill = True
        mix_stderr.seek(0)
        mix_error = mix_stderr.read(maximum_stderr + 1)
    if mix_returncode != 0 or mix_forced_kill or len(mix_error) > maximum_stderr:
        raise RecordingError("offline ffmpeg mix did not complete through its bounded clean path")
    paths["mix_raw_partial"].chmod(0o600)
    raw_artifact = _safe_regular_binding(
        paths["mix_raw_partial"],
        maximum_bytes=int(capture["maximum_file_bytes"]),
        require_private=True,
        include_identity=True,
    )
    expected_raw_bytes = mix_frames * 2 * 4
    if raw_artifact["bytes"] != expected_raw_bytes:
        raise RecordingError("offline raw mix size does not match parallel audio stems")
    raw_descriptor = os.open(
        paths["mix_raw_partial"], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    wave_descriptor = os.open(
        paths["partial"],
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(raw_descriptor, "rb", closefd=True) as raw_stream:
            raw_descriptor = -1
            with os.fdopen(wave_descriptor, "wb", closefd=True) as wave_stream:
                wave_descriptor = -1
                with wave.open(wave_stream, "wb") as writer:
                    writer.setnchannels(2)
                    writer.setsampwidth(4)
                    writer.setframerate(48_000)
                    remaining = expected_raw_bytes
                    while remaining:
                        chunk = raw_stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise RecordingError("offline raw mix ended before expected frame count")
                        writer.writeframesraw(chunk)
                        remaining -= len(chunk)
                    if raw_stream.read(1):
                        raise RecordingError("offline raw mix exceeded expected frame count")
                wave_stream.flush()
                os.fsync(wave_stream.fileno())
    finally:
        if raw_descriptor >= 0:
            os.close(raw_descriptor)
        if wave_descriptor >= 0:
            os.close(wave_descriptor)
    mix_artifact = _validate_recorded_wave(paths["partial"], capture)
    if mix_artifact["frames"] != mix_frames:
        raise RecordingError("offline mix frame count does not match the bounded shortest stem")
    mix_final = _link_no_replace_keep_partial(
        paths["partial"], paths["final"], mix_artifact, maximum_bytes=int(capture["maximum_file_bytes"])
    ) | {key: mix_artifact[key] for key in ARTIFACT_DETAIL_FIELDS}
    midi_final = _link_no_replace_keep_partial(
        paths["midi_partial"], paths["midi_final"], midi_artifact, maximum_bytes=MIDI.MAX_MIDI_BYTES
    )
    manifest = {
        "schema_version": 1,
        "kind": "piano_vocal_performance_take_manifest",
        "session_id": spec["session_id"],
        "plan_sha256": spec["plan_sha256"],
        "worker_capture_epoch_monotonic_ns": epoch_ns,
        "capture_timeline_offsets_ns": timeline,
        "midi_timing": plan["performance"]["timing"],
        "synchronization_boundary": plan["performance"]["synchronization_boundary"],
        "artifacts": {"mix_wav": mix_final, "roland_midi_smf": midi_final},
        "midi_event_counts": midi_meta["event_counts"],
        "midi_note_velocity": midi_meta["note_velocity"],
        "does_not_establish": [
            "musical-tempo",
            "sample-accurate-wav-midi-synchronization",
            "software-monitoring-or-production-mix-activation",
            "hardware-or-ipad-verification",
            "subjective-recording-quality",
        ],
    }
    _atomic_private_json(paths["manifest_partial"], manifest, create_only=True)
    observed_manifest = _safe_json_read(
        paths["manifest_partial"], maximum_bytes=MAX_JSON_BYTES, require_private=True
    )
    if observed_manifest != manifest:
        raise RecordingError("take manifest changed before commit publication")
    manifest_artifact = _safe_regular_binding(
        paths["manifest_partial"], maximum_bytes=MAX_JSON_BYTES, require_private=True, include_identity=True
    )
    manifest_final = _link_no_replace_keep_partial(
        paths["manifest_partial"], paths["manifest_final"], manifest_artifact, maximum_bytes=MAX_JSON_BYTES
    )
    # The durable manifest link above is the commit point.  Cleanup after it is
    # best-effort and must not downgrade a committed take to failed-preserved.
    for partial in (
        paths["partial"],
        paths["voice_partial"],
        paths["roland_partial"],
        paths["mix_raw_partial"],
        paths["midi_partial"],
        paths["manifest_partial"],
    ):
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
    return {
        "schema_version": 1,
        "kind": "audio_recording_result",
        "session_id": spec["session_id"],
        "status": "completed",
        "reason": stop_reason,
        "started_at": started_at,
        "completed_at": utc_now(),
        "plan_sha256": spec["plan_sha256"],
        "processes": {
            "voice": {"returncode": returncodes[0], "stderr_bytes": len(voice_error), "stderr_sha256": hashlib.sha256(voice_error).hexdigest()},
            "roland": {"returncode": returncodes[1], "stderr_bytes": len(roland_error), "stderr_sha256": hashlib.sha256(roland_error).hexdigest()},
            "midi": {"returncode": returncodes[2], "stderr_bytes": len(midi_error), "stderr_sha256": hashlib.sha256(midi_error).hexdigest()},
            "mix": {"returncode": mix_returncode, "stderr_bytes": len(mix_error), "stderr_sha256": hashlib.sha256(mix_error).hexdigest()},
            "forced_kill": forced_kill,
        },
        "artifacts": {"mix_wav": mix_final, "roland_midi_smf": midi_final, "take_manifest": manifest_final},
        "midi_event_counts": midi_meta["event_counts"],
        "does_not_establish": manifest["does_not_establish"],
    }


def worker_run(
    spec: dict[str, Any],
    *,
    parecord_path: pathlib.Path | None = None,
    validate_spec: bool = True,
) -> dict[str, Any]:
    if validate_spec:
        _validate_spec(spec)
        _validate_live_preconditions(spec)
    capture = spec["plan_identity"]["capture"]
    if parecord_path is None:
        parecord_path = pathlib.Path(
            spec["plan_identity"]["parecord"]["resolved"]["path"]
        )
    if spec["plan_identity"]["session_type"] == "piano-vocal-performance":
        arecordmidi_path = pathlib.Path(
            spec["plan_identity"]["performance"]["arecordmidi"]["resolved"]["path"]
        )
        ffmpeg_path = pathlib.Path(
            spec["plan_identity"]["performance"]["ffmpeg"]["resolved"]["path"]
        )
        return _performance_worker_run(
            spec, parecord_path, arecordmidi_path, ffmpeg_path
        )
    partial = lexical_absolute(pathlib.Path(spec["paths"]["partial"]))
    final = lexical_absolute(pathlib.Path(spec["paths"]["final"]))
    if partial.parent != final.parent:
        raise RecordingError("recording paths do not share an output root")
    _validate_output_root(partial.parent)
    if partial.exists() or partial.is_symlink() or final.exists() or final.is_symlink():
        raise RecordingError("recording output or partial path already exists")
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    file_size_limit = _wav_compatible_fsize_limit(int(capture["maximum_file_bytes"]))
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (file_size_limit, file_size_limit),
    )
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    session_id = spec["session_id"]
    session_type = spec["plan_identity"]["session_type"]
    argv = _parecord_argv(spec, parecord_path, partial)
    started_at = utc_now()
    started_monotonic = time.monotonic()
    maximum_stderr = int(load_catalog(session_type)["capture"]["maximum_stderr_bytes"])
    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            env=_restricted_environment(),
            close_fds=True,
        )
        ready = False
        ready_deadline = time.monotonic() + int(capture["startup_timeout_seconds"])
        while time.monotonic() < ready_deadline:
            if process.poll() is not None:
                break
            try:
                if partial.stat().st_size > 44:
                    ready = True
                    break
            except FileNotFoundError:
                pass
            time.sleep(0.05)
        stop_reason = "startup-failed"
        if ready:
            stop_reason = "maximum-duration"
            deadline = started_monotonic + int(capture["maximum_duration_seconds"])
            while time.monotonic() < deadline:
                if stop_requested:
                    stop_reason = "requested-stop"
                    break
                if process.poll() is not None:
                    stop_reason = "capture-process-exited"
                    break
                time.sleep(0.05)
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        forced_kill = False
        try:
            returncode = process.wait(timeout=int(capture["stop_grace_seconds"]))
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=5)
            forced_kill = True
        stderr_file.seek(0)
        stderr = stderr_file.read(maximum_stderr + 1)
    stderr_truncated = len(stderr) > maximum_stderr
    if stderr_truncated:
        stderr = stderr[:maximum_stderr]
    complete_process = (
        ready
        and stop_reason in {"requested-stop", "maximum-duration"}
        and returncode in {0, -signal.SIGINT}
        and not forced_kill
        and not stderr_truncated
    )
    if not complete_process:
        raise RecordingError(
            "capture process did not terminate through the bounded clean path"
        )
    partial.chmod(0o600)
    descriptor = os.open(partial, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    artifact = _validate_recorded_wave(partial, capture)
    _publish_no_replace(partial, final, artifact)
    artifact = _safe_regular_binding(
        final,
        maximum_bytes=int(capture["maximum_file_bytes"]),
        require_private=True,
        include_identity=True,
    ) | {
        key: artifact[key]
        for key in (
            "channels",
            "bit_depth_container",
            "sample_rate_hz",
            "frames",
            "duration_seconds",
        )
    }
    return {
        "schema_version": 1,
        "kind": "audio_recording_result",
        "session_id": session_id,
        "status": "completed",
        "reason": stop_reason,
        "started_at": started_at,
        "completed_at": utc_now(),
        "plan_sha256": spec["plan_sha256"],
        "process": {
            "returncode": returncode,
            "forced_kill": forced_kill,
            "stderr_bytes": len(stderr),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "stderr_truncated": stderr_truncated,
        },
        "artifact": artifact,
        "does_not_establish": [
            "subjective-recording-quality",
            "safe-monitoring-level",
            "24-effective-bits-in-the-32-bit-container",
            "physical-source-placement-or-upstream-mix-correctness",
            "absence-of-resampling-outside-the-declared-source-contract",
        ],
    }


def worker_entry(spec_path: pathlib.Path, expected_spec_sha256: str) -> int:
    result_path: pathlib.Path | None = None
    spec: dict[str, Any] | None = None
    try:
        spec, binding = _safe_json_read_with_binding(spec_path, require_private=True)
        if binding["sha256"] != expected_spec_sha256:
            raise RecordingError("recording worker spec digest changed")
        _validate_persisted_spec(spec, state_root=spec_path.parent)
        result_path = _worker_result_path(spec)
        result = worker_run(spec)
        _atomic_private_json(result_path, result, create_only=True)
        return 0
    except Exception as exc:
        if spec is not None and result_path is not None and not result_path.exists():
            maximum = int(
                spec.get("plan_identity", {})
                .get("capture", {})
                .get("maximum_file_bytes", MAX_BINDING_BYTES)
            )
            partial = pathlib.Path(spec.get("paths", {}).get("partial", "/nonexistent"))
            failure = {
                "schema_version": 1,
                "kind": "audio_recording_result",
                "session_id": spec.get("session_id"),
                "status": "failed-preserved",
                "reason": type(exc).__name__,
                "error": str(exc)[:500],
                "failed_at": utc_now(),
                "plan_sha256": spec.get("plan_sha256"),
                "partial": _bounded_path_binding(partial, maximum),
                "does_not_establish": [
                    "valid-finalized-wav",
                    "safe-reuse-of-a-partial-file",
                    "successful-recording",
                ],
            }
            if (
                spec.get("plan_identity", {}).get("session_type")
                == "piano-vocal-performance"
            ):
                failure["performance_artifacts"] = _performance_path_inventory(spec)
            try:
                _atomic_private_json(result_path, failure, create_only=True)
            except Exception:
                pass
        return 1


def init_roots(output_root: pathlib.Path, state_root: pathlib.Path) -> dict[str, Any]:
    output = ensure_private_directory(output_root)
    state = ensure_private_directory(state_root)
    return {
        "schema_version": 1,
        "kind": "audio_recording_initialization_receipt",
        "output_root": str(output),
        "state_root": str(state),
        "mode": "0700",
        "audio_effect": False,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--root", type=pathlib.Path, default=DEFAULT_OUTPUT_ROOT)
    init.add_argument("--state-root", type=pathlib.Path, default=DEFAULT_STATE_ROOT)

    for command in ("plan", "start"):
        item = sub.add_parser(command)
        item.add_argument("name")
        item.add_argument(
            "--session-type", choices=SESSION_TYPES, default="voice-recording"
        )
        item.add_argument("--maximum-seconds", type=int, default=3600)
        item.add_argument("--root", type=pathlib.Path, default=DEFAULT_OUTPUT_ROOT)
        item.add_argument("--state-root", type=pathlib.Path, default=DEFAULT_STATE_ROOT)
        item.add_argument(
            "--physical-state", type=pathlib.Path, default=PHYSICAL.DEFAULT_STATE
        )
        item.add_argument(
            "--laboratory-state", type=pathlib.Path, default=LAB.DEFAULT_STATE
        )
        if command == "start":
            item.add_argument("--expected-plan-sha256", required=True)

    for command in ("status", "stop", "recover"):
        item = sub.add_parser(command)
        item.add_argument("--state-root", type=pathlib.Path, default=DEFAULT_STATE_ROOT)
        item.add_argument("--session-id")

    worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--spec", type=pathlib.Path, required=True)
    worker.add_argument("--expected-spec-sha256", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "_worker":
        return worker_entry(args.spec, args.expected_spec_sha256)
    try:
        if args.command == "init":
            result = init_roots(args.root, args.state_root)
        elif args.command == "plan":
            result = build_plan(
                args.name,
                args.maximum_seconds,
                session_type=args.session_type,
                output_root=args.root,
                state_root=args.state_root,
                physical_state=args.physical_state,
                laboratory_state=args.laboratory_state,
            )
        elif args.command == "start":
            result = start_session(
                args.name,
                args.maximum_seconds,
                args.expected_plan_sha256,
                session_type=args.session_type,
                output_root=args.root,
                state_root=args.state_root,
                physical_state=args.physical_state,
                laboratory_state=args.laboratory_state,
            )
        elif args.command == "status":
            result = session_status(
                state_root=args.state_root, session_id=args.session_id
            )
        elif args.command == "stop":
            result = stop_session(
                state_root=args.state_root, session_id=args.session_id
            )
        else:
            result = recover_session(
                state_root=args.state_root, session_id=args.session_id
            )
    except (OSError, RecordingError, ValueError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_recording_error",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
