#!/usr/bin/env python3
"""Capture a bounded MOTU-bound WAV for the T001 voice reference gate."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAB_PATH = ROOT / "scripts" / "laboratory_gate.py"
LEVEL_PATH = ROOT / "scripts" / "level_analyzer.py"
SYSTEM_TRUTH_PATH = ROOT / "scripts" / "system_truth.py"
MOTU_CAPTURE_IDENTITY_PATH = ROOT / "scripts" / "motu_capture_identity.py"
PARECORD_PATH = pathlib.Path("/usr/bin/parecord")
MAX_CAPTURE_SECONDS = 20
MIN_PASS_SECONDS = 8
MAX_STDERR_BYTES = 65_536
MAX_WAV_BYTES = 32_000_000
def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LAB = load_module("laboratory_gate_for_voice_capture", LAB_PATH)
LEVEL = load_module("level_analyzer_for_voice_capture", LEVEL_PATH)
SYSTEM_TRUTH = load_module("system_truth_for_voice_capture", SYSTEM_TRUTH_PATH)
MOTU_CAPTURE_IDENTITY = load_module(
    "motu_capture_identity_for_voice_capture", MOTU_CAPTURE_IDENTITY_PATH
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def monotonic_now() -> float:
    return time.monotonic()


def sleep_for(seconds: float) -> None:
    time.sleep(seconds)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8", errors="strict"))


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    total = 0
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("voice capture file is not regular")
        if metadata.st_size < 1 or metadata.st_size > MAX_WAV_BYTES:
            raise ValueError("voice capture file size is outside the bound")
        while chunk := os.read(descriptor, 1_048_576):
            total += len(chunk)
            if total > MAX_WAV_BYTES:
                raise ValueError("voice capture file grew beyond the bound")
            digest.update(chunk)
        if total != metadata.st_size:
            raise ValueError("voice capture file size changed while hashing")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _source_volume_values(source: dict[str, Any]) -> list[int]:
    """Delegate the legacy helper surface to the shared MOTU identity contract."""
    return MOTU_CAPTURE_IDENTITY.source_volume_values(source)

def _source_identity(source: dict[str, Any]) -> dict[str, Any] | None:
    return MOTU_CAPTURE_IDENTITY.source_identity(source)


def source_snapshot() -> dict[str, Any]:
    argv = LAB.VOICE_PACTL_SOURCES_ARGV
    SYSTEM_TRUTH.assert_read_only_commands((argv,))
    result = SYSTEM_TRUTH.run_read_only(argv)
    query = {
        "argv": list(argv),
        "argv_sha256": LAB.canonical_value_sha256(list(argv)),
        "returncode": result.returncode,
        "complete": (
            result.error is None
            and result.returncode == 0
            and not result.stdout_truncated
            and not result.stderr_truncated
        ),
        "stdout_sha256": result.stdout_sha256,
        "stdout_total_bytes": result.stdout_total_bytes,
        "stderr_sha256": result.stderr_sha256,
    }
    matches: list[dict[str, Any]] = []
    errors: list[str] = []
    if query["complete"]:
        try:
            payload = json.loads(result.stdout)
            if not isinstance(payload, list):
                raise ValueError("pactl source list is not an array")
            for source in payload:
                if not isinstance(source, dict):
                    raise ValueError("pactl source item is not an object")
                try:
                    identity = _source_identity(source)
                except ValueError as exc:
                    errors.append(str(exc))
                    continue
                if identity is not None:
                    matches.append(identity)
        except (json.JSONDecodeError, ValueError):
            errors.append("pactl-source-json-invalid")
    else:
        errors.append("pactl-source-query-failed")
    complete = not errors and len(matches) == 1
    snapshot = {
        "schema_version": 1,
        "kind": "audio_voice_source_snapshot",
        "observed_at": utc_now().isoformat(),
        "complete": complete,
        "present": complete,
        "match_count": len(matches),
        "ambiguous": len(matches) > 1,
        "errors": sorted(set(errors)),
        "identity": matches[0] if complete else None,
        "query": query,
    }
    snapshot["observation_sha256"] = LAB.canonical_value_sha256(snapshot)
    return snapshot


def _source_name_from_live_query() -> str:
    argv = LAB.VOICE_PACTL_SOURCES_ARGV
    SYSTEM_TRUTH.assert_read_only_commands((argv,))
    result = SYSTEM_TRUTH.run_read_only(argv)
    if (
        result.error is not None
        or result.returncode != 0
        or result.stdout_truncated
        or result.stderr_truncated
    ):
        raise ValueError("MOTU source query failed before capture")
    payload = json.loads(result.stdout)
    names: list[str] = []
    for source in payload:
        if not isinstance(source, dict):
            continue
        identity = _source_identity(source)
        if identity is not None:
            name = source.get("name")
            if isinstance(name, str):
                names.append(name)
    if len(names) != 1:
        raise ValueError("MOTU capture source is missing or ambiguous")
    return names[0]


def capture_command_contract(source_name: str) -> dict[str, Any]:
    fixed_arguments = [
        "--record",
        "--rate=48000",
        "--format=s32le",
        "--channels=2",
        "--no-remix",
        "--file-format=wav",
        "--client-name=audio-voice-reference",
        "--stream-name=voice-reference",
    ]
    contract = {
        "executable": str(PARECORD_PATH),
        "fixed_arguments": fixed_arguments,
        "device_name_sha256": sha256_text(source_name),
        "output_role": "private-temporary-wav",
    }
    contract["contract_sha256"] = LAB.canonical_value_sha256(contract)
    return contract


def _run_parecord(source_name: str, output: pathlib.Path, duration_seconds: int) -> dict[str, Any]:
    if not PARECORD_PATH.is_file() or not os.access(PARECORD_PATH, os.X_OK):
        raise ValueError("parecord executable is unavailable")
    contract = capture_command_contract(source_name)
    argv = [
        str(PARECORD_PATH),
        "--record",
        f"--device={source_name}",
        "--rate=48000",
        "--format=s32le",
        "--channels=2",
        "--no-remix",
        "--file-format=wav",
        "--client-name=audio-voice-reference",
        "--stream-name=voice-reference",
        str(output),
    ]
    started_at = utc_now()
    started_monotonic = monotonic_now()
    environment = {
        key: os.environ[key]
        for key in ("HOME", "XDG_RUNTIME_DIR", "PULSE_SERVER", "PULSE_COOKIE")
        if key in os.environ
    }
    environment.update({"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"})
    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            start_new_session=True,
            close_fds=True,
            env=environment,
        )
        ready_deadline = monotonic_now() + LAB.VOICE_STARTUP_TIMEOUT_SECONDS
        stream_ready_at: dt.datetime | None = None
        stream_ready_monotonic: float | None = None
        while monotonic_now() < ready_deadline:
            if process.poll() is not None:
                break
            try:
                if output.stat().st_size > 44:
                    stream_ready_at = utc_now()
                    stream_ready_monotonic = monotonic_now()
                    break
            except FileNotFoundError:
                pass
            sleep_for(0.05)
        stream_ready = (
            stream_ready_at is not None and stream_ready_monotonic is not None
        )
        if stream_ready:
            sleep_for(duration_seconds)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        try:
            returncode = process.wait(timeout=5)
            forced_kill = False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            returncode = process.wait(timeout=5)
            forced_kill = True
        ended_at = utc_now()
        duration = max(0.0, monotonic_now() - started_monotonic)
        stderr_file.seek(0)
        stderr = stderr_file.read(MAX_STDERR_BYTES + 1)
    stderr_truncated = len(stderr) > MAX_STDERR_BYTES
    if stderr_truncated:
        stderr = stderr[:MAX_STDERR_BYTES]
    return {
        "method": LAB.VOICE_CAPTURE_METHOD,
        "requested_duration_seconds": duration_seconds,
        "capture_started_at": started_at.isoformat(),
        "capture_ended_at": ended_at.isoformat(),
        "duration_seconds": round(duration, 3),
        "stream_ready": stream_ready,
        "stream_ready_at": (
            stream_ready_at.isoformat() if stream_ready_at is not None else None
        ),
        "startup_seconds": round(
            max(0.0, (stream_ready_monotonic or monotonic_now()) - started_monotonic), 3
        ),
        "command": contract,
        "returncode": returncode,
        "accepted_returncodes": [0, -signal.SIGINT],
        "forced_kill": forced_kill,
        "stderr_bytes": len(stderr),
        "stderr_sha256": sha256_bytes(stderr),
        "stderr_truncated": stderr_truncated,
        "complete": (
            returncode in {0, -signal.SIGINT}
            and not forced_kill
            and not stderr_truncated
            and stream_ready
            and output.is_file()
        ),
    }


def _copy_private_binary(source: pathlib.Path, destination: pathlib.Path) -> dict[str, Any]:
    absolute = SYSTEM_TRUTH.absolute_without_resolution(destination)
    _, parent_fd = SYSTEM_TRUTH.open_directory_chain(absolute.parent, create=True)
    temporary_name = f".{absolute.name}.{secrets.token_hex(12)}.tmp"
    source_fd = os.open(
        source,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    target_fd = -1
    digest = hashlib.sha256()
    total = 0
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("captured WAV is not regular")
        if source_stat.st_size < 1 or source_stat.st_size > MAX_WAV_BYTES:
            raise ValueError("captured WAV size is outside the bound")
        try:
            existing = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError("WAV output must be absent or regular")
        target_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        while chunk := os.read(source_fd, 1_048_576):
            total += len(chunk)
            if total > MAX_WAV_BYTES:
                raise ValueError("captured WAV grew beyond the bound")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                if written <= 0:
                    raise OSError("short WAV output write")
                view = view[written:]
        if total != source_stat.st_size:
            raise ValueError("captured WAV size changed during copy")
        os.fsync(target_fd)
        os.fchmod(target_fd, 0o600)
        os.close(target_fd)
        target_fd = -1
        os.replace(
            temporary_name,
            absolute.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)
    return {"name": absolute.name, "sha256": digest.hexdigest(), "bytes": total}


def _implementation_binding() -> dict[str, str]:
    return {
        "voice_capture_observer_sha256": hashlib.sha256(
            pathlib.Path(__file__).read_bytes()
        ).hexdigest(),
        "laboratory_gate_sha256": hashlib.sha256(LAB_PATH.read_bytes()).hexdigest(),
        "level_analyzer_sha256": hashlib.sha256(LEVEL_PATH.read_bytes()).hexdigest(),
        "system_truth_sha256": hashlib.sha256(SYSTEM_TRUTH_PATH.read_bytes()).hexdigest(),
    }


def _source_readiness_blockers(snapshot: dict[str, Any]) -> list[str]:
    if snapshot.get("complete") is not True:
        return ["motu-source-not-uniquely-ready"]
    identity = snapshot.get("identity")
    if not isinstance(identity, dict):
        return ["motu-source-identity-missing"]
    blockers: list[str] = []
    if identity.get("sample_format") != "s32le":
        blockers.append("motu-source-format-mismatch")
    if identity.get("sample_rate_hz") != 48_000:
        blockers.append("motu-source-rate-mismatch")
    if identity.get("channels") != 2:
        blockers.append("motu-source-channel-mismatch")
    if identity.get("muted") is not False:
        blockers.append("motu-source-muted")
    if identity.get("unity_volume") is not True:
        blockers.append("motu-source-volume-not-unity")
    return blockers


def capture_voice_evidence(
    duration_seconds: int,
    physical_state: pathlib.Path,
    wav_output: pathlib.Path,
) -> dict[str, Any]:
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise ValueError("voice capture duration must be an integer")
    if duration_seconds < 1 or duration_seconds > MAX_CAPTURE_SECONDS:
        raise ValueError(
            f"voice capture duration must be between 1 and {MAX_CAPTURE_SECONDS} seconds"
        )
    before = source_snapshot()
    blockers = _source_readiness_blockers(before)
    source_name: str | None = None
    process: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    source_wav: dict[str, Any] | None = None
    after = before
    if not blockers:
        try:
            source_name = _source_name_from_live_query()
        except (OSError, ValueError, json.JSONDecodeError):
            blockers.append("motu-source-query-race")
        if source_name is not None and sha256_text(source_name) != (
            before["identity"]["node_name_sha256"]
        ):
            blockers.append("motu-source-changed-before-capture")
    with tempfile.TemporaryDirectory(prefix="audio-voice-capture-") as directory:
        private_dir = pathlib.Path(directory)
        private_dir.chmod(0o700)
        captured = private_dir / "voice-reference.wav"
        if not blockers and source_name is not None:
            try:
                process = _run_parecord(source_name, captured, duration_seconds)
            except (OSError, ValueError, subprocess.SubprocessError):
                blockers.append("voice-capture-process-failed")
                process = None
            if process is None:
                blockers.append("voice-capture-process-incomplete")
            elif not process.get("complete"):
                blockers.append("voice-capture-process-incomplete")
                if process.get("stream_ready") is not True:
                    blockers.append("voice-capture-stream-not-ready")
            after = source_snapshot()
            if not after.get("complete"):
                blockers.append("motu-source-not-ready-after-capture")
            elif (
                before["identity"]["fingerprint"]
                != after["identity"]["fingerprint"]
            ):
                blockers.append("motu-source-identity-changed")
            if captured.is_file() and captured.stat().st_size > 0:
                try:
                    analysis = LEVEL.analyze(captured)
                    source_wav = _copy_private_binary(captured, wav_output)
                except (OSError, ValueError):
                    blockers.append("captured-wav-invalid")
            else:
                blockers.append("captured-wav-missing")
    if duration_seconds < MIN_PASS_SECONDS:
        blockers.append("voice-capture-too-short")
    if analysis is not None:
        if analysis.get("sample_rate_hz") != 48_000:
            blockers.append("captured-wav-rate-mismatch")
        if analysis.get("channels") != 2 or analysis.get("bit_depth") != 32:
            blockers.append("captured-wav-format-mismatch")
        if float(analysis.get("duration_seconds", 0.0)) < duration_seconds - 1.0:
            blockers.append("captured-wav-duration-too-short")
        channels = analysis.get("channels_analysis", [])
        if not channels or any(
            not isinstance(item, dict) or item.get("clipped_samples") != 0
            for item in channels
        ):
            blockers.append("voice-capture-clipped")
        if analysis.get("voice_target", {}).get("status") != "in-range":
            blockers.append("voice-peak-outside-target")
    blockers = sorted(set(blockers))
    evidence = {
        "schema_version": 1,
        "kind": "audio_level_measurement_evidence",
        "gate": "voice-level-measurement",
        "result": "pass" if not blockers else "fail",
        "measured_at": utc_now().isoformat(),
        "physical_state_sha256": LAB.current_physical_sha256(physical_state),
        "source_wav": source_wav,
        "analysis": analysis,
        "capture_observation": {
            "method": LAB.VOICE_CAPTURE_METHOD,
            "before": before,
            "after": after,
            "process": process,
            "stable_source_identity": (
                before.get("complete") is True
                and after.get("complete") is True
                and before.get("identity", {}).get("fingerprint")
                == after.get("identity", {}).get("fingerprint")
            ),
        },
        "implementation": _implementation_binding(),
        "criteria": {
            "peak_dbfs_range": [-12.0, -6.0],
            "maximum_clipped_samples_per_channel": 0,
            "minimum_capture_duration_seconds": MIN_PASS_SECONDS,
            "maximum_capture_duration_seconds": MAX_CAPTURE_SECONDS,
            "maximum_startup_seconds": LAB.VOICE_STARTUP_TIMEOUT_SECONDS,
            "required_sample_rate_hz": 48_000,
            "required_channels": 2,
            "required_bit_depth": 32,
            "requires_motu_serial_identity": True,
            "requires_unity_capture_volume": True,
            "requires_stable_source_identity": True,
        },
        "blockers": blockers,
        "does_not_establish": [
            "subjective vocal quality",
            "safe monitoring level",
            "analog gain position beyond the physical-state binding",
            "speech content or speaker identity",
        ],
    }
    return evidence
