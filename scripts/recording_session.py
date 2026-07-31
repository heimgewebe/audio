#!/usr/bin/env python3
"""Plan and manage one fail-closed, bounded voice recording session."""

from __future__ import annotations

import argparse
import contextlib
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
PHYSICAL_PATH = ROOT / "scripts" / "physical_verification.py"
LAB_PATH = ROOT / "scripts" / "laboratory_gate.py"
VOICE_PATH = ROOT / "scripts" / "voice_capture_observer.py"
WRAPPER_PATH = ROOT / "scripts" / "audio-record"
PARECORD_PATH = pathlib.Path("/usr/bin/parecord")
MAX_JSON_BYTES = 524_288
MAX_BINDING_BYTES = 64_000_000
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
SESSION_ID_RE = re.compile(r"[0-9a-f]{24}")
ARTIFACT_DETAIL_FIELDS = frozenset(
    {"channels", "bit_depth_container", "sample_rate_hz", "frames", "duration_seconds"}
)
DEFAULT_OUTPUT_ROOT = pathlib.Path(
    os.environ.get("AUDIO_RECORDING_ROOT", pathlib.Path.home() / "Music" / "Audio-Aufnahmen")
)
DEFAULT_STATE_ROOT = pathlib.Path(
    os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state")
) / "audio" / "recordings-v1"


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


def _check_directory_chain(path: pathlib.Path, *, allow_missing_leaf: bool = False) -> None:
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
            raise RecordingError(f"symbolic directory component is forbidden: {current}")
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
        raise RecordingError(f"private directory is not owned by the current user: {absolute}")
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


def load_catalog() -> dict[str, Any]:
    payload = _safe_json_read(CONTRACT_PATH, maximum_bytes=262_144)
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "audio_recording_session_catalog"
        or set(payload) != {"schema_version", "kind", "sessions", "rules"}
    ):
        raise RecordingError("recording-session catalog schema is invalid")
    sessions = payload.get("sessions")
    if not isinstance(sessions, dict) or set(sessions) != {"voice-recording"}:
        raise RecordingError("recording-session catalog has an invalid session set")
    session = sessions["voice-recording"]
    if not isinstance(session, dict):
        raise RecordingError("voice recording contract is not an object")
    required = {
        "purpose",
        "required_physical_facts",
        "required_laboratory_gates",
        "capture",
        "source",
        "process",
    }
    if set(session) != required:
        raise RecordingError("voice recording contract fields are invalid")
    capture = session.get("capture")
    source = session.get("source")
    if not isinstance(capture, dict) or not isinstance(source, dict):
        raise RecordingError("voice recording capture or source contract is invalid")
    if session.get("required_laboratory_gates") != ["voice-level-measurement"]:
        raise RecordingError("voice recording requires an unsupported laboratory gate set")
    if (
        capture.get("sample_rate_hz") != 48_000
        or capture.get("sample_format") != "s32le"
        or capture.get("channels") != 2
        or capture.get("container") != "wav"
        or capture.get("bytes_per_sample") != 4
        or source.get("required_sample_rate_hz") != 48_000
        or source.get("required_sample_format") != "s32le"
        or source.get("required_channels") != 2
    ):
        raise RecordingError("voice recording format contract is inconsistent")
    return session


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
        ROOT / "profiles" / "audio-profiles.v1.json",
        VOICE_PATH,
    )
    return [_safe_regular_binding(path, maximum_bytes=MAX_BINDING_BYTES) for path in paths]


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
    return {
        "launcher": str(path),
        "launcher_symlink_target": link_target,
        "resolved": binding,
    }


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


def _physical_projection(
    path: pathlib.Path, requirements: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    try:
        state, binding = _read_optional_state(
            path,
            maximum_bytes=PHYSICAL.MAX_STATE_BYTES,
            empty_factory=PHYSICAL.empty_state,
            validator=PHYSICAL.validate_state,
        )
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
        if isinstance(key, str) and isinstance(item, dict)
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


def _laboratory_projection(
    path: pathlib.Path, physical: dict[str, Any], required: list[str]
) -> tuple[dict[str, Any], list[str]]:
    try:
        state, binding = _read_optional_state(
            path,
            maximum_bytes=LAB.MAX_STATE_BYTES,
            empty_factory=LAB.empty_state,
            validator=LAB.validate_state,
        )
        catalog = LAB.load_catalog()
    except (OSError, RecordingError, ValueError) as exc:
        return {
            "state_path": str(path),
            "state_sha256": None,
            "resolved": [],
            "invalidated": {},
            "receipt_sha256": {},
            "error": str(exc),
        }, ["laboratory-state-invalid"]
    receipts = state.get("gates", {})
    resolved: set[str] = set()
    invalidated: dict[str, str] = {}
    physical_sha = physical.get("state_sha256")
    for gate in required:
        receipt = receipts.get(gate) if isinstance(receipts, dict) else None
        if not isinstance(receipt, dict):
            invalidated[gate] = "missing"
            continue
        if gate != "voice-level-measurement":
            invalidated[gate] = "unsupported-required-gate"
            continue
        evidence = receipt.get("evidence")
        if not isinstance(evidence, dict) or not LAB.has_bound_voice_capture(evidence):
            invalidated[gate] = "legacy-unbound-voice-evidence"
            continue
        if catalog.get(gate, {}).get("binds_physical_state") is True:
            if physical_sha is None:
                invalidated[gate] = "physical-state-missing"
                continue
            if receipt.get("physical_state_sha256") != physical_sha:
                invalidated[gate] = "physical-state-changed"
                continue
        resolved.add(gate)
    blockers = [f"laboratory-gate:{gate}" for gate in required if gate not in resolved]
    receipt_sha = {
        gate: canonical_sha256(receipts[gate])
        for gate in required
        if isinstance(receipts, dict) and isinstance(receipts.get(gate), dict)
    }
    return {
        "state_path": str(path),
        "state_sha256": binding["sha256"] if binding is not None else None,
        "resolved": sorted(resolved),
        "invalidated": invalidated,
        "receipt_sha256": receipt_sha,
        "error": None,
    }, blockers

def _source_projection(
    contract: dict[str, Any], snapshot_fn: Callable[[], dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    try:
        snapshot = snapshot_fn()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"identity": None, "error": str(exc)}, ["motu-source-query-failed"]
    identity = snapshot.get("identity")
    blockers: list[str] = []
    if snapshot.get("complete") is not True or not isinstance(identity, dict):
        blockers.append("motu-source-not-unique")
        identity = None
    if identity is not None:
        expected = {
            "vendor_id": contract["vendor_id"],
            "product_id": contract["product_id"],
            "sample_format": contract["required_sample_format"],
            "sample_rate_hz": contract["required_sample_rate_hz"],
            "channels": contract["required_channels"],
            "muted": False,
            "unity_volume": True,
        }
        for field, value in expected.items():
            if identity.get(field) != value:
                blockers.append(f"motu-source:{field}")
    return {
        "identity": identity,
        "identity_sha256": (canonical_sha256(identity) if identity is not None else None),
        "error": None,
    }, blockers


def maximum_file_bytes(capture: dict[str, Any], maximum_seconds: int) -> int:
    return (
        capture["sample_rate_hz"]
        * capture["channels"]
        * capture["bytes_per_sample"]
        * maximum_seconds
        + capture["header_and_metadata_allowance_bytes"]
    )


def build_plan(
    name: str,
    maximum_seconds: int,
    *,
    output_root: pathlib.Path = DEFAULT_OUTPUT_ROOT,
    state_root: pathlib.Path = DEFAULT_STATE_ROOT,
    physical_state: pathlib.Path = PHYSICAL.DEFAULT_STATE,
    laboratory_state: pathlib.Path = LAB.DEFAULT_STATE,
    source_snapshot_fn: Callable[[], dict[str, Any]] | None = None,
    disk_usage_fn: Callable[[pathlib.Path], Any] = shutil.disk_usage,
) -> dict[str, Any]:
    name = _plain_wav_name(name)
    contract = load_catalog()
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
    blockers: list[str] = []
    output_path = root / name
    root_ready = False
    try:
        root = _validate_output_root(root)
        root_ready = True
    except RecordingError:
        blockers.append("output-root-not-ready")
    if output_path.exists() or output_path.is_symlink():
        blockers.append("output-already-exists")
    physical, physical_blockers = _physical_projection(
        lexical_absolute(physical_state), contract["required_physical_facts"]
    )
    laboratory, laboratory_blockers = _laboratory_projection(
        lexical_absolute(laboratory_state),
        physical,
        contract["required_laboratory_gates"],
    )
    source, source_blockers = _source_projection(
        contract["source"], source_snapshot_fn or VOICE.source_snapshot
    )
    blockers.extend(physical_blockers)
    blockers.extend(laboratory_blockers)
    blockers.extend(source_blockers)
    try:
        recorder = parecord_binding()
    except RecordingError:
        recorder = None
        blockers.append("parecord-unavailable")
    required_bytes = maximum_file_bytes(capture, maximum_seconds)
    free_bytes: int | None = None
    if root_ready:
        try:
            free_bytes = int(disk_usage_fn(root).free)
        except (OSError, ValueError, TypeError):
            blockers.append("free-space-unknown")
        else:
            if free_bytes < required_bytes + capture["free_space_reserve_bytes"]:
                blockers.append("free-space-insufficient")
    active_path = state_root / "active.json"
    if active_path.exists() or active_path.is_symlink():
        blockers.append("active-session-requires-status-or-recovery")
    identity = {
        "schema_version": 1,
        "kind": "audio_recording_plan_identity",
        "session_type": "voice-recording",
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
        "source": source,
        "contracts": contract_bindings(),
        "parecord": recorder,
        "process": contract["process"],
        "state_root": str(state_root),
    }
    plan_sha = canonical_sha256(identity)
    blockers = sorted(set(blockers))
    return {
        "schema_version": 1,
        "kind": "audio_recording_plan",
        "ready": not blockers,
        "plan_sha256": plan_sha,
        "identity": identity,
        "readiness": {
            "blockers": blockers,
            "free_bytes": free_bytes,
            "required_file_bytes": required_bytes,
            "required_free_bytes": required_bytes
            + capture["free_space_reserve_bytes"],
        },
        "does_not_establish": [
            "safe-monitoring-level",
            "subjective-recording-quality",
            "microphone-or-phantom-power-state-beyond-bound-human-observation",
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
            os.O_RDWR
            | os.O_CREAT
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
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


def _session_paths(state_root: pathlib.Path, session_id: str) -> dict[str, pathlib.Path]:
    if not re.fullmatch(r"[0-9a-f]{24}", session_id):
        raise RecordingError("recording session id is invalid")
    root = lexical_absolute(state_root)
    return {
        "spec": root / f"{session_id}.spec.json",
        "state": root / f"{session_id}.state.json",
        "result": root / f"{session_id}.result.json",
        "active": root / "active.json",
    }


def _read_active(state_root: pathlib.Path) -> str:
    active = lexical_absolute(state_root) / "active.json"
    payload = _safe_json_read(active, require_private=True)
    if set(payload) != {"schema_version", "kind", "session_id", "spec_sha256"}:
        raise RecordingError("active recording pointer schema is invalid")
    if payload.get("schema_version") != 1 or payload.get("kind") != "audio_recording_active":
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
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError, IndexError):
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


def _terminate_exact_process(
    expected: dict[str, Any], *, grace_seconds: float
) -> bool:
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


def _validate_persisted_spec(
    spec: dict[str, Any], *, state_root: pathlib.Path | None = None
) -> None:
    required = {
        "schema_version",
        "kind",
        "session_id",
        "created_at",
        "plan_sha256",
        "plan_identity",
        "source_name",
        "paths",
    }
    if set(spec) != required:
        raise RecordingError("recording worker spec fields are invalid")
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
        "output",
        "capture",
        "physical",
        "laboratory",
        "source",
        "contracts",
        "parecord",
        "process",
        "state_root",
    }
    if (
        set(plan) != expected_plan_fields
        or plan.get("schema_version") != 1
        or plan.get("kind") != "audio_recording_plan_identity"
        or plan.get("session_type") != "voice-recording"
    ):
        raise RecordingError("recording plan identity fields are invalid")
    source_name = spec.get("source_name")
    if (
        not isinstance(source_name, str)
        or not source_name
        or len(source_name) > 4096
        or CONTROL_RE.search(source_name)
    ):
        raise RecordingError("recording source name is invalid")
    paths = spec.get("paths")
    if not isinstance(paths, dict) or set(paths) != {"partial", "final", "result"}:
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
    maximum_bytes = capture.get("maximum_file_bytes") if isinstance(capture, dict) else None
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
        raise RecordingError("recording artifact no longer matches its receipt") from exc
    if observed != _artifact_binding_fields(value):
        raise RecordingError("recording artifact no longer matches its receipt")


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
    recovery_fields = common | {"recovered_at", "partial", "final"}
    worker_fields = common | {"failed_at", "error", "partial"}
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

def _bounded_path_binding(path: pathlib.Path, maximum_bytes: int) -> dict[str, Any] | None:
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
    pid_alive = isinstance(process, dict) and _proc_identity(process.get("pid")) is not None
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
    return {
        "schema_version": 1,
        "kind": "audio_recording_status",
        "session_id": resolved_id,
        "status": status,
        "recovery_required": recovery_required,
        "process_identity_exact": exact_alive,
        "plan_sha256": spec.get("plan_sha256"),
        "partial": _bounded_path_binding(partial, maximum),
        "final": _bounded_path_binding(final, maximum),
        "result": result,
    }


def _restricted_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("HOME", "XDG_RUNTIME_DIR", "PULSE_SERVER", "PULSE_COOKIE")
        if key in os.environ
    }
    environment.update({"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"})
    return environment


def start_session(
    name: str,
    maximum_seconds: int,
    expected_plan_sha256: str,
    *,
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
            output_root=output_root,
            state_root=state_root,
            physical_state=physical_state,
            laboratory_state=laboratory_state,
        )
        if plan["plan_sha256"] != expected_plan_sha256:
            raise RecordingError("recording plan changed; review the new plan before start")
        if plan["ready"] is not True:
            raise RecordingError(
                "recording plan is blocked: "
                + ", ".join(plan["readiness"]["blockers"])
            )
        source_name = VOICE._source_name_from_live_query()
        identity = plan["identity"]["source"]["identity"]
        if hashlib.sha256(source_name.encode("utf-8")).hexdigest() != identity.get(
            "node_name_sha256"
        ):
            raise RecordingError("MOTU source changed between plan and start")
        session_id = secrets.token_hex(12)
        paths = _session_paths(state_root, session_id)
        output_path = pathlib.Path(plan["identity"]["output"]["path"])
        partial_name = f".{output_path.stem}.{session_id}.partial.wav"
        partial_path = output_path.parent / partial_name
        if partial_path.exists() or partial_path.is_symlink():
            raise RecordingError("recording partial path already exists")
        spec = {
            "schema_version": 1,
            "kind": "audio_recording_session_spec",
            "session_id": session_id,
            "created_at": utc_now(),
            "plan_sha256": plan["plan_sha256"],
            "plan_identity": plan["identity"],
            "source_name": source_name,
            "paths": {
                "partial": str(partial_path),
                "final": str(output_path),
                "result": str(paths["result"]),
            },
        }
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
                raise RecordingError("recording worker identity could not be established")
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
    deadline = time.monotonic() + plan["identity"]["capture"][
        "startup_timeout_seconds"
    ]
    while time.monotonic() < deadline:
        if partial_path.is_file() and partial_path.stat().st_size > 44:
            return {
                "schema_version": 1,
                "kind": "audio_recording_start_receipt",
                "session_id": session_id,
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
        grace_seconds=float(
            plan["identity"]["capture"]["stop_grace_seconds"]
        ),
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
            raise RecordingError("recording process identity is not exact; use recovery")
        terminated = _terminate_exact_process(
            process,
            grace_seconds=float(
                spec["plan_identity"]["capture"]["stop_grace_seconds"]
            )
            + 5.0,
        )
        if not terminated:
            raise RecordingError("recording process did not terminate after bounded stop")
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
            raise RecordingError("PID was reused or changed; recovery remains fail-closed")
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
        _atomic_private_json(paths["result"], result, create_only=True)
        _clear_active_if_matches(root, resolved_id)
        return session_status(state_root=root, session_id=resolved_id)


def _validate_spec(spec: dict[str, Any]) -> None:
    _validate_persisted_spec(spec)
    if spec["plan_identity"].get("contracts") != contract_bindings():
        raise RecordingError("recording implementation contracts changed after planning")
    if spec["plan_identity"].get("parecord") != parecord_binding():
        raise RecordingError("parecord changed after planning")
    source_name = spec["source_name"]
    identity = spec["plan_identity"].get("source", {}).get("identity")
    if (
        not isinstance(identity, dict)
        or hashlib.sha256(source_name.encode("utf-8")).hexdigest()
        != identity.get("node_name_sha256")
    ):
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
        raise RecordingError(
            "recording partial changed after WAV validation"
        ) from exc
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
        raise RecordingError("final recording path appeared during publication") from exc
    finally:
        os.close(descriptor)


def _worker_result_path(spec: dict[str, Any]) -> pathlib.Path:
    return lexical_absolute(pathlib.Path(spec["paths"]["result"]))


def _validate_live_preconditions(spec: dict[str, Any]) -> None:
    plan = spec["plan_identity"]
    contract = load_catalog()
    physical, physical_blockers = _physical_projection(
        pathlib.Path(plan["physical"]["state_path"]),
        contract["required_physical_facts"],
    )
    laboratory, laboratory_blockers = _laboratory_projection(
        pathlib.Path(plan["laboratory"]["state_path"]),
        physical,
        contract["required_laboratory_gates"],
    )
    source, source_blockers = _source_projection(
        contract["source"], VOICE.source_snapshot
    )
    blockers = sorted(
        set(physical_blockers + laboratory_blockers + source_blockers)
    )
    if blockers:
        raise RecordingError(
            "recording preconditions changed before capture: " + ", ".join(blockers)
        )
    if physical != plan.get("physical"):
        raise RecordingError("physical recording state changed before capture")
    if laboratory != plan.get("laboratory"):
        raise RecordingError("laboratory recording state changed before capture")
    if source != plan.get("source"):
        raise RecordingError("MOTU source identity changed before capture")
    output = plan["output"]
    final = pathlib.Path(output["path"])
    root = _validate_output_root(pathlib.Path(output["root"]))
    if final.parent != root or final.exists() or final.is_symlink():
        raise RecordingError("recording output changed before capture")
    capture = plan["capture"]
    free_bytes = int(shutil.disk_usage(root).free)
    required = int(capture["maximum_file_bytes"]) + int(
        capture["free_space_reserve_bytes"]
    )
    if free_bytes < required:
        raise RecordingError("free space fell below the recording budget")


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
    partial = lexical_absolute(pathlib.Path(spec["paths"]["partial"]))
    final = lexical_absolute(pathlib.Path(spec["paths"]["final"]))
    if partial.parent != final.parent:
        raise RecordingError("recording paths do not share an output root")
    _validate_output_root(partial.parent)
    if partial.exists() or partial.is_symlink() or final.exists() or final.is_symlink():
        raise RecordingError("recording output or partial path already exists")
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (int(capture["maximum_file_bytes"]), int(capture["maximum_file_bytes"])),
    )
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    session_id = spec["session_id"]
    argv = [
        str(parecord_path),
        "--record",
        f"--device={spec['source_name']}",
        "--rate=48000",
        "--format=s32le",
        "--channels=2",
        "--channel-map=front-left,front-right",
        "--no-remix",
        "--no-remap",
        "--file-format=wav",
        "--client-name=audio-voice-recording",
        f"--stream-name=voice-recording-{session_id}",
        str(partial),
    ]
    started_at = utc_now()
    started_monotonic = time.monotonic()
    maximum_stderr = int(load_catalog()["capture"]["maximum_stderr_bytes"])
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
            "correct-microphone-placement",
        ],
    }


def worker_entry(spec_path: pathlib.Path, expected_spec_sha256: str) -> int:
    result_path: pathlib.Path | None = None
    spec: dict[str, Any] | None = None
    try:
        spec, binding = _safe_json_read_with_binding(
            spec_path, require_private=True
        )
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
