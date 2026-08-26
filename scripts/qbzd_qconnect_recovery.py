#!/usr/bin/env python3
"""Fail-closed self-heal for a QBZD QConnect session stuck in reconnect."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import pathlib
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

QBZD_HOST = "127.0.0.1"
QBZD_PORT = 8182
QBZD_STATUS_PATH = "/api/status"
QBZD_UNIT = "qbzd.service"
EXPECTED_DEVICE = "front:CARD=M2,DEV=0"
POLL_SECONDS = 30.0
HEALTHY_STATUS_FALLBACK_SECONDS = 300.0
QCONNECT_CYCLE_STUCK_SECONDS = 90.0
STUCK_SECONDS = 300.0
QCONNECT_READBACK_ATTEMPTS = 10
QCONNECT_READBACK_INTERVAL_SECONDS = 2.0
QCONNECT_FAILURE_BACKOFF_BASE_SECONDS = 120.0
QCONNECT_FAILURE_BACKOFF_MAX_SECONDS = 900.0
STABILIZATION_SECONDS = 2.0
READBACK_ATTEMPTS = 30
READBACK_INTERVAL_SECONDS = 2.0
FAILURE_BACKOFF_BASE_SECONDS = 900.0
FAILURE_BACKOFF_MAX_SECONDS = 3600.0
SUCCESS_COOLDOWN_SECONDS = 900.0
HTTP_TIMEOUT_SECONDS = 1.5
COMMAND_TIMEOUT_SECONDS = 5.0
MAX_STATUS_BYTES = 65_536
MAX_COMMAND_OUTPUT_BYTES = 65_536
MAX_PROC_BYTES = 16_384
MAX_PCM_STATUS_FILES = 128
MAX_JOURNAL_CURSOR_BYTES = 2_048
JOURNAL_EXECUTABLE = pathlib.Path("/usr/bin/journalctl")
JOURNAL_CURSOR_PREFIX = "-- cursor: "
QCONNECT_JOURNAL_TRIGGERS = (
    "[QConnect] Lifecycle -> Reconnecting",
    "Cloud rejected session:",
    "Reconnect scheduled:",
    "Max reconnect attempts exceeded",
    "[QConnect] Reconnect exhausted",
)
QCONNECT_NETWORK_REACHABILITY_MARKERS = (
    "WebSocket connected",
    "Authenticated with JWT",
    "Cloud rejected session:",
)
NETWORK_REACHABILITY_EVIDENCE_SECONDS = 90.0
QCONNECT_NETWORK_SEQUENCE_SECONDS = 30.0
NETWORK_REACHABILITY_FUTURE_SKEW_SECONDS = 5.0
STATE_SCHEMA_VERSION = 3
LEGACY_STATE_SCHEMA_VERSION = 2


class RecoveryError(RuntimeError):
    """A bounded observation or recovery gate failed closed."""


@dataclass(frozen=True)
class QbzdStatus:
    api_version: int
    version: str
    auth_state: str
    network_online: bool
    qconnect_state: str
    session_active: bool
    audio_backend: str
    configured_device: str
    device_present: bool
    device_open: bool
    playback_state: str | None
    playback_track_id: int | None
    playback_position: float | None
    uptime_secs: int
    qconnect_enabled: bool | None = None


@dataclass(frozen=True)
class QbzdService:
    pid: int
    start_ticks: int
    cgroup: str
    executable: pathlib.Path = pathlib.Path("/usr/bin/qbzd")


@dataclass(frozen=True)
class JournalEvent:
    realtime_usec: int
    message: str


@dataclass(frozen=True)
class JournalDelta:
    cursor: str
    text: str
    events: tuple[JournalEvent, ...] = ()


StatusReader = Callable[[], QbzdStatus]
Runner = Callable[[tuple[str, ...]], str]
Sleeper = Callable[[float], None]
ServiceReader = Callable[[], QbzdService]
PcmIdleChecker = Callable[[QbzdService], None]
PcmOwnedChecker = Callable[[QbzdService], None]
Clock = Callable[[], float]
BootIdReader = Callable[[], str]
JournalReader = Callable[[str | None], JournalDelta]
QconnectActionRunner = Callable[[QbzdService, str], str]
Reconciler = Callable[[pathlib.Path], str]


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecoveryError(f"status-invalid:{label}")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _strict_json_loads(payload: str) -> Any:
    return json.loads(payload, parse_constant=_reject_json_constant)


def classify_status_payload(payload: Any) -> QbzdStatus:
    root = _require_dict(payload, "root")
    api_version = root.get("api_version")
    if (
        isinstance(api_version, bool)
        or not isinstance(api_version, int)
        or api_version != 1
    ):
        raise RecoveryError("status-invalid:api-version")
    version = root.get("version")
    uptime = root.get("uptime_secs")
    if not isinstance(version, str) or not version or len(version.encode("utf-8")) > 128:
        raise RecoveryError("status-invalid:version")
    if isinstance(uptime, bool) or not isinstance(uptime, int) or uptime < 0:
        raise RecoveryError("status-invalid:uptime")

    auth = _require_dict(root.get("auth"), "auth")
    network = _require_dict(root.get("network"), "network")
    qconnect = _require_dict(root.get("qconnect"), "qconnect")
    audio = _require_dict(root.get("audio"), "audio")
    raw_playback = root.get("playback")
    playback = None if raw_playback is None else _require_dict(raw_playback, "playback")
    fields: tuple[tuple[str, Any, type], ...] = (
        ("auth.state", auth.get("state"), str),
        ("network.online", network.get("online"), bool),
        ("qconnect.state", qconnect.get("state"), str),
        ("qconnect.session_active", qconnect.get("session_active"), bool),
        ("audio.backend", audio.get("backend"), str),
        ("audio.configured_device", audio.get("configured_device"), str),
        ("audio.device_present", audio.get("device_present"), bool),
        ("audio.device_open", audio.get("device_open"), bool),
    )
    for label, value, expected in fields:
        if not isinstance(value, expected):
            raise RecoveryError(f"status-invalid:{label}")

    raw_qconnect_enabled = qconnect.get("enabled")
    if raw_qconnect_enabled is not None and not isinstance(raw_qconnect_enabled, bool):
        raise RecoveryError("status-invalid:qconnect.enabled")

    playback_state: str | None = None
    playback_track_id: int | None = None
    playback_position: float | None = None
    if playback is not None:
        raw_playback_state = playback.get("state")
        if raw_playback_state is not None:
            if not isinstance(raw_playback_state, str):
                raise RecoveryError("status-invalid:playback.state")
            if not raw_playback_state or len(raw_playback_state.encode("utf-8")) > 64:
                raise RecoveryError("status-invalid:playback.state")
            playback_state = raw_playback_state

        playback_track_id = playback.get("track_id")
        if playback_track_id is not None and (
            isinstance(playback_track_id, bool)
            or not isinstance(playback_track_id, int)
            or playback_track_id <= 0
        ):
            raise RecoveryError("status-invalid:playback.track_id")
        raw_position = playback.get("position")
        if raw_position is not None:
            if isinstance(raw_position, bool) or not isinstance(raw_position, (int, float)):
                raise RecoveryError("status-invalid:playback.position")
            try:
                playback_position = float(raw_position)
            except (OverflowError, ValueError) as exc:
                raise RecoveryError("status-invalid:playback.position") from exc
            if not math.isfinite(playback_position) or playback_position < 0:
                raise RecoveryError("status-invalid:playback.position")

    return QbzdStatus(
        api_version=1,
        version=version,
        auth_state=auth["state"],
        network_online=network["online"],
        qconnect_state=qconnect["state"],
        session_active=qconnect["session_active"],
        audio_backend=audio["backend"],
        configured_device=audio["configured_device"],
        device_present=audio["device_present"],
        device_open=audio["device_open"],
        playback_state=playback_state,
        playback_track_id=playback_track_id,
        playback_position=playback_position,
        uptime_secs=uptime,
        qconnect_enabled=raw_qconnect_enabled,
    )


def read_status() -> QbzdStatus:
    connection = http.client.HTTPConnection(QBZD_HOST, QBZD_PORT, timeout=HTTP_TIMEOUT_SECONDS)
    try:
        connection.request("GET", QBZD_STATUS_PATH, headers={"Host": QBZD_HOST})
        response = connection.getresponse()
        payload = response.read(MAX_STATUS_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise RecoveryError("status-unavailable") from exc
    finally:
        connection.close()
    if response.status != 200:
        raise RecoveryError("status-http-error")
    if len(payload) > MAX_STATUS_BYTES:
        raise RecoveryError("status-too-large")
    try:
        decoded = _strict_json_loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RecoveryError("status-invalid:json") from exc
    return classify_status_payload(decoded)


def _validate_command(argv: tuple[str, ...]) -> None:
    if argv == (
        "systemctl",
        "--user",
        "show",
        QBZD_UNIT,
        "--property=ActiveState",
        "--property=MainPID",
    ):
        return
    if argv == ("systemctl", "--user", "try-restart", QBZD_UNIT):
        return
    raise RecoveryError("command-not-allowed")


def run_command(argv: tuple[str, ...]) -> str:
    _validate_command(argv)
    executable = pathlib.Path("/usr/bin") / argv[0]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed allowlisted argv
            (str(executable), *argv[1:]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError(f"command-unavailable:{argv[0]}") from exc
    if len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES:
        raise RecoveryError(f"command-output-limit:{argv[0]}")
    if completed.returncode != 0:
        raise RecoveryError(f"command-failed:{argv[0]}")
    try:
        return completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RecoveryError(f"command-output-invalid:{argv[0]}") from exc


def run_qconnect_action(
    service: QbzdService,
    action: str,
    *,
    proc_root: pathlib.Path = pathlib.Path("/proc"),
) -> str:
    """Run one supported QConnect verb through a pinned, reverified QBZD image."""
    if action not in {"disable", "enable"}:
        raise RecoveryError("qconnect-action-not-allowed")
    if service.pid <= 0 or service.start_ticks <= 0 or service.executable.name != "qbzd":
        raise RecoveryError("qbzd-process-unverified")

    try:
        executable_fd = os.open(service.executable, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as exc:
        raise RecoveryError("qbzd-process-unverified") from exc
    try:
        try:
            pinned_metadata = os.fstat(executable_fd)
        except OSError as exc:
            raise RecoveryError("qbzd-process-unverified") from exc
        if not stat.S_ISREG(pinned_metadata.st_mode):
            raise RecoveryError("qbzd-process-unverified")

        process_root = proc_root / str(service.pid)
        if _read_bounded_text(process_root / "comm", "qbzd-process-unverified") != "qbzd":
            raise RecoveryError("qbzd-process-unverified")
        current_start_ticks = _parse_start_ticks(
            _read_bounded_text(process_root / "stat", "qbzd-process-unverified")
        )
        current_cgroup = _parse_unified_cgroup(
            _read_bounded_text(process_root / "cgroup", "qbzd-process-unverified"),
            "qbzd-process-unverified",
        )
        if current_start_ticks != service.start_ticks or current_cgroup != service.cgroup:
            raise RecoveryError("qbzd-process-unverified")
        try:
            proc_executable_metadata = (process_root / "exe").stat(follow_symlinks=True)
        except OSError as exc:
            raise RecoveryError("qbzd-process-unverified") from exc
        if (
            not stat.S_ISREG(proc_executable_metadata.st_mode)
            or proc_executable_metadata.st_dev != pinned_metadata.st_dev
            or proc_executable_metadata.st_ino != pinned_metadata.st_ino
        ):
            raise RecoveryError("qbzd-process-unverified")

        pinned_executable = f"/proc/self/fd/{executable_fd}"
        try:
            completed = subprocess.run(  # noqa: S603 - executable is the verified pinned fd
                (pinned_executable, "qconnect", action),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
                env={**os.environ, "LC_ALL": "C.UTF-8"},
                pass_fds=(executable_fd,),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RecoveryError(f"qconnect-command-unavailable:{action}") from exc
    finally:
        os.close(executable_fd)

    if len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES:
        raise RecoveryError(f"qconnect-command-output-limit:{action}")
    if completed.returncode != 0:
        raise RecoveryError(f"qconnect-command-failed:{action}")
    try:
        return completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RecoveryError(f"qconnect-command-output-invalid:{action}") from exc


def _validate_journal_cursor(value: str) -> str:
    if not isinstance(value, str):
        raise RecoveryError("journal-cursor-invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RecoveryError("journal-cursor-invalid") from exc
    if not (1 <= len(encoded) <= MAX_JOURNAL_CURSOR_BYTES):
        raise RecoveryError("journal-cursor-invalid")
    if not value.startswith("s="):
        raise RecoveryError("journal-cursor-invalid")
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise RecoveryError("journal-cursor-invalid")
    return value


def parse_journal_output(payload: str) -> JournalDelta:
    if not isinstance(payload, str):
        raise RecoveryError("journal-output-invalid")
    if len(payload.encode("utf-8")) > MAX_COMMAND_OUTPUT_BYTES:
        raise RecoveryError("journal-output-limit")
    lines = payload.splitlines()
    cursor_indexes = [
        index for index, line in enumerate(lines) if line.startswith(JOURNAL_CURSOR_PREFIX)
    ]
    if cursor_indexes != [len(lines) - 1]:
        raise RecoveryError("journal-cursor-invalid")
    cursor = _validate_journal_cursor(lines[-1][len(JOURNAL_CURSOR_PREFIX) :])
    events: list[JournalEvent] = []
    for line in lines[:-1]:
        if not line:
            continue
        try:
            entry = _strict_json_loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RecoveryError("journal-output-invalid") from exc
        if not isinstance(entry, dict):
            raise RecoveryError("journal-output-invalid")
        message = entry.get("MESSAGE")
        realtime = entry.get("__REALTIME_TIMESTAMP")
        if not isinstance(message, str):
            raise RecoveryError("journal-output-invalid")
        if isinstance(realtime, bool):
            raise RecoveryError("journal-output-invalid")
        if isinstance(realtime, int):
            realtime_usec = realtime
        elif isinstance(realtime, str) and realtime.isascii() and realtime.isdigit():
            realtime_usec = int(realtime)
        else:
            raise RecoveryError("journal-output-invalid")
        if realtime_usec <= 0:
            raise RecoveryError("journal-output-invalid")
        events.append(JournalEvent(realtime_usec=realtime_usec, message=message))
    return JournalDelta(
        cursor=cursor,
        text="\n".join(event.message for event in events),
        events=tuple(events),
    )


def _drain_journal_stream(stream: Any, result: dict[str, Any]) -> None:
    kept = bytearray()
    truncated = False
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            remaining = max(0, MAX_COMMAND_OUTPUT_BYTES - len(kept))
            if remaining:
                kept.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
    finally:
        stream.close()
    result["bytes"] = bytes(kept)
    result["truncated"] = truncated


def read_journal_delta(cursor: str | None = None) -> JournalDelta:
    argv = [
        str(JOURNAL_EXECUTABLE),
        "--user",
        "--unit",
        QBZD_UNIT,
        "--no-pager",
        "--output=json",
        "--show-cursor",
    ]
    if cursor is None:
        argv.append("--lines=0")
    else:
        argv.extend(("--after-cursor", _validate_journal_cursor(cursor)))
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed executable and bounded argv
            tuple(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except OSError as exc:
        raise RecoveryError("journal-unavailable") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_result: dict[str, Any] = {}
    stderr_result: dict[str, Any] = {}
    readers = (
        threading.Thread(
            target=_drain_journal_stream, args=(process.stdout, stdout_result), daemon=True
        ),
        threading.Thread(
            target=_drain_journal_stream, args=(process.stderr, stderr_result), daemon=True
        ),
    )
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            process.kill()
        except ProcessLookupError:
            pass
        returncode = process.wait()
    for reader in readers:
        reader.join()

    if timed_out:
        raise RecoveryError("journal-unavailable")
    if stdout_result.get("truncated") is True or stderr_result.get("truncated") is True:
        raise RecoveryError("journal-output-limit")
    if returncode != 0:
        raise RecoveryError("journal-unavailable")
    stdout_bytes = stdout_result.get("bytes", b"")
    stderr_bytes = stderr_result.get("bytes", b"")
    if not isinstance(stdout_bytes, bytes) or not isinstance(stderr_bytes, bytes):
        raise RecoveryError("journal-output-invalid")
    try:
        payload = stdout_bytes.decode("utf-8", errors="strict")
        stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RecoveryError("journal-output-invalid") from exc
    return parse_journal_output(payload)


def journal_requires_status(text: str) -> bool:
    return any(trigger in text for trigger in QCONNECT_JOURNAL_TRIGGERS)


def journal_qconnect_network_reachability_timestamp(
    delta: JournalDelta,
) -> float | None:
    connected_at: float | None = None
    authenticated_at: float | None = None
    proof_at: float | None = None
    connected_marker, authenticated_marker, rejected_marker = (
        QCONNECT_NETWORK_REACHABILITY_MARKERS
    )
    for event in delta.events:
        event_at = event.realtime_usec / 1_000_000.0
        message = event.message
        if connected_marker in message:
            connected_at = event_at
            authenticated_at = None
        if authenticated_marker in message:
            if (
                connected_at is not None
                and 0.0 <= event_at - connected_at <= QCONNECT_NETWORK_SEQUENCE_SECONDS
            ):
                authenticated_at = event_at
            else:
                connected_at = None
                authenticated_at = None
        if rejected_marker in message:
            if (
                connected_at is not None
                and authenticated_at is not None
                and connected_at <= authenticated_at <= event_at
                and event_at - connected_at <= QCONNECT_NETWORK_SEQUENCE_SECONDS
            ):
                proof_at = event_at
            connected_at = None
            authenticated_at = None
    return proof_at


def journal_proves_qconnect_network_reachable(
    delta: JournalDelta, *, now_wall: float
) -> bool:
    proof_at = journal_qconnect_network_reachability_timestamp(delta)
    if proof_at is None or not math.isfinite(now_wall) or now_wall < 0.0:
        return False
    age = now_wall - proof_at
    return (
        -NETWORK_REACHABILITY_FUTURE_SKEW_SECONDS
        <= age
        <= NETWORK_REACHABILITY_EVIDENCE_SECONDS
    )


def _read_bounded_text(path: pathlib.Path, error_code: str) -> str:
    try:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise RecoveryError(error_code)
        with path.open("rb") as handle:
            payload = handle.read(MAX_PROC_BYTES + 1)
    except OSError as exc:
        raise RecoveryError(error_code) from exc
    if len(payload) > MAX_PROC_BYTES:
        raise RecoveryError(error_code)
    try:
        return payload.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise RecoveryError(error_code) from exc


def _parse_start_ticks(stat_text: str) -> int:
    closing = stat_text.rfind(")")
    if closing < 1 or closing + 2 > len(stat_text):
        raise RecoveryError("qbzd-process-unverified")
    fields = stat_text[closing + 2 :].split()
    if len(fields) <= 19 or not fields[19].isdigit() or int(fields[19]) <= 0:
        raise RecoveryError("qbzd-process-unverified")
    return int(fields[19])


def _parse_unified_cgroup(text: str, error_code: str) -> str:
    lines = [line for line in text.splitlines() if line]
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        raise RecoveryError(error_code)
    cgroup = lines[0][3:]
    if not cgroup.startswith("/") or len(cgroup.encode("utf-8")) > 4096:
        raise RecoveryError(error_code)
    return cgroup


def read_qbzd_service(
    *, runner: Runner = run_command, proc_root: pathlib.Path = pathlib.Path("/proc")
) -> QbzdService:
    output = runner(
        (
            "systemctl",
            "--user",
            "show",
            QBZD_UNIT,
            "--property=ActiveState",
            "--property=MainPID",
        )
    )
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if values.get("ActiveState") != "active":
        raise RecoveryError("qbzd-service-not-active")
    raw_pid = values.get("MainPID", "")
    if not raw_pid.isdigit() or int(raw_pid) <= 0:
        raise RecoveryError("qbzd-service-pid-invalid")
    pid = int(raw_pid)
    process_root = proc_root / str(pid)
    if _read_bounded_text(process_root / "comm", "qbzd-process-unverified") != "qbzd":
        raise RecoveryError("qbzd-process-unverified")
    start_ticks = _parse_start_ticks(
        _read_bounded_text(process_root / "stat", "qbzd-process-unverified")
    )
    cgroup = _parse_unified_cgroup(
        _read_bounded_text(process_root / "cgroup", "qbzd-process-unverified"),
        "qbzd-process-unverified",
    )
    try:
        executable_text = os.readlink(process_root / "exe")
    except OSError as exc:
        raise RecoveryError("qbzd-process-unverified") from exc
    if (
        not executable_text
        or len(executable_text.encode("utf-8", errors="strict")) > 4096
        or executable_text.endswith(" (deleted)")
    ):
        raise RecoveryError("qbzd-process-unverified")
    executable = pathlib.Path(executable_text)
    if not executable.is_absolute() or executable.name != "qbzd":
        raise RecoveryError("qbzd-process-unverified")
    try:
        executable_metadata = executable.stat(follow_symlinks=True)
    except OSError as exc:
        raise RecoveryError("qbzd-process-unverified") from exc
    if not stat.S_ISREG(executable_metadata.st_mode):
        raise RecoveryError("qbzd-process-unverified")
    return QbzdService(
        pid=pid, start_ticks=start_ticks, cgroup=cgroup, executable=executable
    )


def _owner_process_identity(owner_tid: int, proc_root: pathlib.Path) -> tuple[int, str]:
    if isinstance(owner_tid, bool) or not isinstance(owner_tid, int) or owner_tid <= 0:
        raise RecoveryError("alsa-owner-unreadable")
    task_root = proc_root / str(owner_tid)
    status_text = _read_bounded_text(task_root / "status", "alsa-owner-unreadable")
    tgid: int | None = None
    for line in status_text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "Tgid":
            candidate = value.strip()
            if not candidate.isdigit() or int(candidate) <= 0:
                raise RecoveryError("alsa-owner-unreadable")
            tgid = int(candidate)
            break
    if tgid is None:
        raise RecoveryError("alsa-owner-unreadable")
    cgroup = _parse_unified_cgroup(
        _read_bounded_text(task_root / "cgroup", "alsa-owner-unreadable"),
        "alsa-owner-unreadable",
    )
    return tgid, cgroup


def require_qbzd_pcm_idle(
    service: QbzdService,
    *,
    asound_root: pathlib.Path = pathlib.Path("/proc/asound"),
    proc_root: pathlib.Path = pathlib.Path("/proc"),
) -> None:
    if service.pid <= 0 or service.start_ticks <= 0 or not service.cgroup.startswith("/"):
        raise RecoveryError("qbzd-pcm-owner-invalid")
    try:
        status_paths = sorted(asound_root.glob("card*/pcm*/sub*/status"))
    except OSError as exc:
        raise RecoveryError("alsa-status-unavailable") from exc
    if not status_paths or len(status_paths) > MAX_PCM_STATUS_FILES:
        raise RecoveryError("alsa-status-unavailable")
    for status_path in status_paths:
        text = _read_bounded_text(status_path, "alsa-status-unavailable")
        if text == "closed":
            continue
        owner_tid: int | None = None
        for line in text.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() == "owner_pid":
                candidate = value.strip()
                if not candidate.isdigit() or int(candidate) <= 0:
                    raise RecoveryError("alsa-owner-unreadable")
                owner_tid = int(candidate)
                break
        if owner_tid is None:
            raise RecoveryError("alsa-owner-unreadable")
        tgid, owner_cgroup = _owner_process_identity(owner_tid, proc_root)
        if tgid == service.pid or owner_cgroup == service.cgroup:
            raise RecoveryError("qbzd-pcm-open")


def require_qbzd_pcm_owned(
    service: QbzdService,
    *,
    asound_root: pathlib.Path = pathlib.Path("/proc/asound"),
    proc_root: pathlib.Path = pathlib.Path("/proc"),
) -> None:
    """Require the target MOTU playback PCM to be owned by exact live QBZD.

    The paused-open QConnect exception is intentionally bound to the configured
    ``front:CARD=M2,DEV=0`` playback endpoint. An unrelated QBZD-owned PCM must
    never satisfy this gate, and any open target substream with foreign or
    merely same-cgroup-helper ownership fails closed. Daemon restart continues
    to use :func:`require_qbzd_pcm_idle` instead.
    """
    if service.pid <= 0 or service.start_ticks <= 0 or not service.cgroup.startswith("/"):
        raise RecoveryError("qbzd-pcm-owner-invalid")
    process_root = proc_root / str(service.pid)
    current_start = _parse_start_ticks(
        _read_bounded_text(process_root / "stat", "qbzd-process-unverified")
    )
    current_cgroup = _parse_unified_cgroup(
        _read_bounded_text(process_root / "cgroup", "qbzd-process-unverified"),
        "qbzd-process-unverified",
    )
    if current_start != service.start_ticks or current_cgroup != service.cgroup:
        raise RecoveryError("qbzd-process-unverified")

    try:
        card_id_paths = sorted(asound_root.glob("card*/id"))
    except OSError as exc:
        raise RecoveryError("alsa-card-id-unavailable") from exc
    if not card_id_paths or len(card_id_paths) > MAX_PCM_STATUS_FILES:
        raise RecoveryError("alsa-card-id-unavailable")
    target_cards: list[pathlib.Path] = []
    for card_id_path in card_id_paths:
        if _read_bounded_text(card_id_path, "alsa-card-id-unavailable") == "M2":
            target_cards.append(card_id_path.parent)
    if len(target_cards) != 1:
        raise RecoveryError("alsa-target-card-ambiguous")

    try:
        status_paths = sorted(target_cards[0].glob("pcm0p/sub*/status"))
    except OSError as exc:
        raise RecoveryError("alsa-target-status-unavailable") from exc
    if not status_paths or len(status_paths) > MAX_PCM_STATUS_FILES:
        raise RecoveryError("alsa-target-status-unavailable")

    exact_owner_found = False
    for status_path in status_paths:
        text = _read_bounded_text(status_path, "alsa-target-status-unavailable")
        if text == "closed":
            continue
        owner_tid: int | None = None
        for line in text.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() == "owner_pid":
                candidate = value.strip()
                if not candidate.isdigit() or int(candidate) <= 0:
                    raise RecoveryError("alsa-owner-unreadable")
                owner_tid = int(candidate)
                break
        if owner_tid is None:
            raise RecoveryError("alsa-owner-unreadable")
        tgid, owner_cgroup = _owner_process_identity(owner_tid, proc_root)
        if tgid != service.pid or owner_cgroup != service.cgroup:
            raise RecoveryError("qbzd-target-pcm-owner-mismatch")
        exact_owner_found = True
    if not exact_owner_found:
        raise RecoveryError("qbzd-target-pcm-owner-not-found")


def require_qbzd_pcm_paused(
    service: QbzdService,
    *,
    asound_root: pathlib.Path = pathlib.Path("/proc/asound"),
    proc_root: pathlib.Path = pathlib.Path("/proc"),
) -> None:
    """Require independent kernel proof that QBZD's exact MOTU PCM is paused.

    QBZD's playback snapshot is diagnostic and can remain ``paused`` while the
    ALSA stream is already RUNNING. The paused-open recovery exception may
    therefore reach an effect only when the kernel reports ``state: PAUSED``
    for every open target playback substream and the exact QBZD owner binding
    remains valid. RUNNING, missing state, closed-only or ambiguous input fails
    closed.
    """
    require_qbzd_pcm_owned(service, asound_root=asound_root, proc_root=proc_root)

    try:
        card_id_paths = sorted(asound_root.glob("card*/id"))
    except OSError as exc:
        raise RecoveryError("alsa-card-id-unavailable") from exc
    if not card_id_paths or len(card_id_paths) > MAX_PCM_STATUS_FILES:
        raise RecoveryError("alsa-card-id-unavailable")
    target_cards = [
        card_id_path.parent
        for card_id_path in card_id_paths
        if _read_bounded_text(card_id_path, "alsa-card-id-unavailable") == "M2"
    ]
    if len(target_cards) != 1:
        raise RecoveryError("alsa-target-card-ambiguous")
    try:
        status_paths = sorted(target_cards[0].glob("pcm0p/sub*/status"))
    except OSError as exc:
        raise RecoveryError("alsa-target-status-unavailable") from exc
    if not status_paths or len(status_paths) > MAX_PCM_STATUS_FILES:
        raise RecoveryError("alsa-target-status-unavailable")

    paused_open_found = False
    for status_path in status_paths:
        text = _read_bounded_text(status_path, "alsa-target-status-unavailable")
        if text == "closed":
            continue
        pcm_state: str | None = None
        for line in text.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() == "state":
                pcm_state = value.strip()
                break
        if pcm_state != "PAUSED":
            raise RecoveryError("qbzd-target-pcm-not-paused")
        paused_open_found = True
    if not paused_open_found:
        raise RecoveryError("qbzd-target-pcm-owner-not-found")

    # Re-prove exact ownership after the kernel-state scan. The later effect-
    # edge call in reconcile_once repeats this whole gate once more after the
    # final QBZD status/process observation.
    require_qbzd_pcm_owned(service, asound_root=asound_root, proc_root=proc_root)


def _paused_playback_fingerprint(status: QbzdStatus) -> tuple[int, float] | None:
    if (
        status.device_open is not True
        or status.playback_state != "paused"
        or status.playback_track_id is None
        or status.playback_position is None
    ):
        return None
    return status.playback_track_id, status.playback_position


def _same_paused_playback(first: QbzdStatus, later: QbzdStatus) -> bool:
    first_fingerprint = _paused_playback_fingerprint(first)
    return first_fingerprint is not None and first_fingerprint == _paused_playback_fingerprint(later)


def read_boot_id(*, proc_root: pathlib.Path = pathlib.Path("/proc")) -> str:
    raw = _read_bounded_text(
        proc_root / "sys" / "kernel" / "random" / "boot_id",
        "boot-id-unavailable",
    )
    try:
        parsed = str(uuid.UUID(raw))
    except ValueError as exc:
        raise RecoveryError("boot-id-invalid") from exc
    if parsed != raw.casefold():
        raise RecoveryError("boot-id-invalid")
    return parsed


def _finite_nonnegative(value: Any, label: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecoveryError(f"state-invalid:{label}")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise RecoveryError(f"state-invalid:{label}")
    return normalized


def _optional_positive_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RecoveryError(f"state-invalid:{label}")
    return value


def _optional_boot_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RecoveryError("state-invalid:boot-id")
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise RecoveryError("state-invalid:boot-id") from exc
    if normalized != value.casefold():
        raise RecoveryError("state-invalid:boot-id")
    return normalized


def _default_state(boot_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "boot_id": boot_id,
        "candidate_pid": None,
        "candidate_start_ticks": None,
        "retry_since_monotonic": None,
        "qconnect_armed_monotonic": None,
        "qconnect_armed_pid": None,
        "qconnect_armed_start_ticks": None,
        "qconnect_armed_executable": None,
        "qconnect_reenable_required": False,
        "qconnect_failures": 0,
        "qconnect_next_attempt_monotonic": 0.0,
        "failures": 0,
        "next_attempt_monotonic": 0.0,
        "last_recovered_at_unix": None,
        "restart_armed_monotonic": None,
        "restart_armed_pid": None,
        "restart_armed_start_ticks": None,
    }


def _state_file_from_environment() -> pathlib.Path:
    raw = os.environ.get("STATE_DIRECTORY", "")
    if not raw or ":" in raw:
        raise RecoveryError("state-directory-unavailable")
    directory = pathlib.Path(raw)
    if not directory.is_absolute():
        raise RecoveryError("state-directory-invalid")
    return directory / "state.json"


def _optional_absolute_executable(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise RecoveryError("state-invalid:qconnect-armed-executable")
    path = pathlib.Path(value)
    if not path.is_absolute() or path.name != "qbzd" or value.endswith(" (deleted)"):
        raise RecoveryError("state-invalid:qconnect-armed-executable")
    return value


def _load_state(path: pathlib.Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _default_state()
    except OSError as exc:
        raise RecoveryError("state-unreadable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RecoveryError("state-invalid")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RecoveryError("state-unreadable") from exc
    if len(payload) > 16_384:
        raise RecoveryError("state-invalid")
    try:
        state = _strict_json_loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RecoveryError("state-invalid") from exc
    if not isinstance(state, dict):
        raise RecoveryError("state-invalid")
    schema_version = state.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise RecoveryError("state-invalid")
    if schema_version not in {LEGACY_STATE_SCHEMA_VERSION, STATE_SCHEMA_VERSION}:
        raise RecoveryError("state-invalid")

    boot_id = _optional_boot_id(state.get("boot_id"))
    candidate_pid = _optional_positive_int(state.get("candidate_pid"), "candidate-pid")
    candidate_start = _optional_positive_int(
        state.get("candidate_start_ticks"), "candidate-start-ticks"
    )
    retry_since = _finite_nonnegative(
        state.get("retry_since_monotonic"), "retry-since-monotonic", allow_none=True
    )
    failures = state.get("failures")
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise RecoveryError("state-invalid:failures")
    next_attempt = _finite_nonnegative(
        state.get("next_attempt_monotonic"), "next-attempt-monotonic"
    )
    last_recovered = _finite_nonnegative(
        state.get("last_recovered_at_unix"), "last-recovered-at-unix", allow_none=True
    )
    restart_armed = _finite_nonnegative(
        state.get("restart_armed_monotonic"), "restart-armed-monotonic", allow_none=True
    )
    restart_pid = _optional_positive_int(state.get("restart_armed_pid"), "restart-armed-pid")
    restart_start = _optional_positive_int(
        state.get("restart_armed_start_ticks"), "restart-armed-start-ticks"
    )

    if schema_version == LEGACY_STATE_SCHEMA_VERSION:
        qconnect_armed = None
        qconnect_pid = None
        qconnect_start = None
        qconnect_executable = None
        qconnect_reenable_required = False
        qconnect_failures = 0
        qconnect_next_attempt = 0.0
    else:
        qconnect_armed = _finite_nonnegative(
            state.get("qconnect_armed_monotonic"),
            "qconnect-armed-monotonic",
            allow_none=True,
        )
        qconnect_pid = _optional_positive_int(
            state.get("qconnect_armed_pid"), "qconnect-armed-pid"
        )
        qconnect_start = _optional_positive_int(
            state.get("qconnect_armed_start_ticks"), "qconnect-armed-start-ticks"
        )
        qconnect_executable = _optional_absolute_executable(
            state.get("qconnect_armed_executable")
        )
        qconnect_reenable_required = state.get("qconnect_reenable_required")
        if not isinstance(qconnect_reenable_required, bool):
            raise RecoveryError("state-invalid:qconnect-reenable-required")
        qconnect_failures = state.get("qconnect_failures")
        if (
            isinstance(qconnect_failures, bool)
            or not isinstance(qconnect_failures, int)
            or qconnect_failures < 0
        ):
            raise RecoveryError("state-invalid:qconnect-failures")
        qconnect_next_attempt = _finite_nonnegative(
            state.get("qconnect_next_attempt_monotonic"),
            "qconnect-next-attempt-monotonic",
        )

    candidate_values = (candidate_pid, candidate_start, retry_since)
    if any(value is None for value in candidate_values) != all(
        value is None for value in candidate_values
    ):
        raise RecoveryError("state-invalid:candidate-binding")
    restart_values = (restart_armed, restart_pid, restart_start)
    if any(value is None for value in restart_values) != all(
        value is None for value in restart_values
    ):
        raise RecoveryError("state-invalid:restart-binding")
    qconnect_values = (
        qconnect_armed,
        qconnect_pid,
        qconnect_start,
        qconnect_executable,
    )
    if any(value is None for value in qconnect_values) != all(
        value is None for value in qconnect_values
    ):
        raise RecoveryError("state-invalid:qconnect-binding")
    if (
        retry_since is not None
        or restart_armed is not None
        or qconnect_armed is not None
    ) and boot_id is None:
        raise RecoveryError("state-invalid:boot-binding")

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "boot_id": boot_id,
        "candidate_pid": candidate_pid,
        "candidate_start_ticks": candidate_start,
        "retry_since_monotonic": retry_since,
        "qconnect_armed_monotonic": qconnect_armed,
        "qconnect_armed_pid": qconnect_pid,
        "qconnect_armed_start_ticks": qconnect_start,
        "qconnect_armed_executable": qconnect_executable,
        "qconnect_reenable_required": qconnect_reenable_required,
        "qconnect_failures": qconnect_failures,
        "qconnect_next_attempt_monotonic": qconnect_next_attempt,
        "failures": failures,
        "next_attempt_monotonic": next_attempt,
        "last_recovered_at_unix": last_recovered,
        "restart_armed_monotonic": restart_armed,
        "restart_armed_pid": restart_pid,
        "restart_armed_start_ticks": restart_start,
    }


def _store_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_metadata = path.parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise RecoveryError("state-parent-invalid")
    payload = (
        json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = pathlib.Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _clock_value(clock: Clock, label: str) -> float:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecoveryError(f"clock-invalid:{label}")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise RecoveryError(f"clock-invalid:{label}")
    return normalized


def _network_reachability_evidence_active(
    evidence_realtime: float | None, *, now_wall: float
) -> bool:
    if evidence_realtime is None:
        return False
    if isinstance(evidence_realtime, bool) or not isinstance(
        evidence_realtime, (int, float)
    ):
        raise RecoveryError("network-attestation-time-invalid")
    observed = float(evidence_realtime)
    if not math.isfinite(observed) or observed < 0:
        raise RecoveryError("network-attestation-time-invalid")
    age = now_wall - observed
    return (
        age >= -NETWORK_REACHABILITY_FUTURE_SKEW_SECONDS
        and age <= NETWORK_REACHABILITY_EVIDENCE_SECONDS
    )


def _is_healthy(status: QbzdStatus) -> bool:
    return status.qconnect_state == "connected" and status.session_active


def _is_recovery_candidate(
    status: QbzdStatus, *, allow_network_offline: bool = False
) -> bool:
    qconnect_stuck = status.qconnect_state in {
        "retrying",
        "reconnecting",
        "exhausted",
    }
    audio_effect_candidate = (
        status.device_open is False
        or _paused_playback_fingerprint(status) is not None
    )
    return (
        status.auth_state == "logged_in"
        and (status.network_online is True or allow_network_offline)
        and qconnect_stuck
        and status.session_active is False
        and status.audio_backend.casefold() == "alsa"
        and status.configured_device == EXPECTED_DEVICE
        and status.device_present is True
        and audio_effect_candidate
    )


def _is_daemon_restart_candidate(
    status: QbzdStatus, *, allow_network_offline: bool = False
) -> bool:
    return status.device_open is False and _is_recovery_candidate(
        status, allow_network_offline=allow_network_offline
    )


def _candidate_binding_matches(
    state: dict[str, Any], boot_id: str, service: QbzdService
) -> bool:
    return (
        state["boot_id"] == boot_id
        and state["candidate_pid"] == service.pid
        and state["candidate_start_ticks"] == service.start_ticks
        and state["retry_since_monotonic"] is not None
    )


def _arm_candidate(
    state: dict[str, Any], boot_id: str, service: QbzdService, now_monotonic: float
) -> dict[str, Any]:
    return {
        **state,
        "boot_id": boot_id,
        "candidate_pid": service.pid,
        "candidate_start_ticks": service.start_ticks,
        "retry_since_monotonic": now_monotonic,
    }


def _clear_candidate(
    state: dict[str, Any],
    *,
    healthy: bool,
    boot_id: str,
    now_monotonic: float,
    now_unix: float,
) -> dict[str, Any]:
    if healthy and state["restart_armed_monotonic"] is not None:
        return _success_state(boot_id, now_monotonic, now_unix)
    updated = {
        **state,
        "boot_id": boot_id,
        "candidate_pid": None,
        "candidate_start_ticks": None,
        "retry_since_monotonic": None,
        "qconnect_armed_monotonic": None,
        "qconnect_armed_pid": None,
        "qconnect_armed_start_ticks": None,
        "qconnect_armed_executable": None,
        "qconnect_reenable_required": False,
        "qconnect_failures": 0 if healthy else state["qconnect_failures"],
        "qconnect_next_attempt_monotonic": (
            max(
                float(state["qconnect_next_attempt_monotonic"]),
                now_monotonic + SUCCESS_COOLDOWN_SECONDS,
            )
            if healthy and state["qconnect_armed_monotonic"] is not None
            else (0.0 if healthy else state["qconnect_next_attempt_monotonic"])
        ),
    }
    if healthy:
        updated["failures"] = 0
        updated["restart_armed_monotonic"] = None
        updated["restart_armed_pid"] = None
        updated["restart_armed_start_ticks"] = None
        if (
            updated["last_recovered_at_unix"] is None
            or now_monotonic >= float(updated["next_attempt_monotonic"])
        ):
            updated["next_attempt_monotonic"] = 0.0
    return updated


def _qconnect_effect_armed_state(
    state: dict[str, Any], service: QbzdService, now_monotonic: float
) -> dict[str, Any]:
    return {
        **state,
        "qconnect_armed_monotonic": now_monotonic,
        "qconnect_armed_pid": service.pid,
        "qconnect_armed_start_ticks": service.start_ticks,
        "qconnect_armed_executable": str(service.executable),
        "qconnect_reenable_required": True,
        "qconnect_next_attempt_monotonic": max(
            float(state["qconnect_next_attempt_monotonic"]),
            now_monotonic + QCONNECT_FAILURE_BACKOFF_BASE_SECONDS,
        ),
    }


def _qconnect_failure_state(
    state: dict[str, Any], now_monotonic: float
) -> dict[str, Any]:
    failures = int(state["qconnect_failures"]) + 1
    exponent = min(failures - 1, 6)
    delay = min(
        QCONNECT_FAILURE_BACKOFF_BASE_SECONDS * (2**exponent),
        QCONNECT_FAILURE_BACKOFF_MAX_SECONDS,
    )
    return {
        **state,
        "qconnect_failures": failures,
        "qconnect_next_attempt_monotonic": max(
            float(state["qconnect_next_attempt_monotonic"]), now_monotonic + delay
        ),
    }


def _effect_armed_state(
    state: dict[str, Any], service: QbzdService, now_monotonic: float
) -> dict[str, Any]:
    return {
        **state,
        "restart_armed_monotonic": now_monotonic,
        "restart_armed_pid": service.pid,
        "restart_armed_start_ticks": service.start_ticks,
        "next_attempt_monotonic": max(
            float(state["next_attempt_monotonic"]),
            now_monotonic + FAILURE_BACKOFF_BASE_SECONDS,
        ),
    }


def _failure_state(state: dict[str, Any], now_monotonic: float) -> dict[str, Any]:
    failures = int(state["failures"]) + 1
    exponent = min(failures - 1, 8)
    delay = min(FAILURE_BACKOFF_BASE_SECONDS * (2**exponent), FAILURE_BACKOFF_MAX_SECONDS)
    return {
        **state,
        "failures": failures,
        "next_attempt_monotonic": max(
            float(state["next_attempt_monotonic"]), now_monotonic + delay
        ),
    }


def _success_state(
    boot_id: str, now_monotonic: float, now_unix: float
) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "boot_id": boot_id,
        "candidate_pid": None,
        "candidate_start_ticks": None,
        "retry_since_monotonic": None,
        "qconnect_armed_monotonic": None,
        "qconnect_armed_pid": None,
        "qconnect_armed_start_ticks": None,
        "qconnect_armed_executable": None,
        "qconnect_reenable_required": False,
        "qconnect_failures": 0,
        "qconnect_next_attempt_monotonic": now_monotonic + SUCCESS_COOLDOWN_SECONDS,
        "failures": 0,
        "next_attempt_monotonic": now_monotonic + SUCCESS_COOLDOWN_SECONDS,
        "last_recovered_at_unix": now_unix,
        "restart_armed_monotonic": None,
        "restart_armed_pid": None,
        "restart_armed_start_ticks": None,
    }


def _qconnect_control_enabled(status: QbzdStatus) -> bool:
    # QBZD 2.0.2 exposes the control setting independently from its lifecycle.
    # Prefer that explicit bit whenever present. Legacy/synthetic snapshots may
    # omit it; their lifecycle fallback is only consumed after a successful,
    # idempotent `qconnect enable` command and never clears a pre-existing
    # durable re-enable obligation by itself.
    if status.qconnect_enabled is not None:
        return status.qconnect_enabled
    return status.qconnect_state in {"connected", "retrying", "reconnecting", "exhausted"}


def _clear_qconnect_effect_obligation(state: dict[str, Any]) -> dict[str, Any]:
    return {
        **state,
        "qconnect_armed_monotonic": None,
        "qconnect_armed_pid": None,
        "qconnect_armed_start_ticks": None,
        "qconnect_armed_executable": None,
        "qconnect_reenable_required": False,
    }


def _legacy_v2_state_projection(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": LEGACY_STATE_SCHEMA_VERSION,
        "boot_id": state["boot_id"],
        "candidate_pid": state["candidate_pid"],
        "candidate_start_ticks": state["candidate_start_ticks"],
        "retry_since_monotonic": state["retry_since_monotonic"],
        "failures": state["failures"],
        "next_attempt_monotonic": state["next_attempt_monotonic"],
        "last_recovered_at_unix": state["last_recovered_at_unix"],
        "restart_armed_monotonic": state["restart_armed_monotonic"],
        "restart_armed_pid": state["restart_armed_pid"],
        "restart_armed_start_ticks": state["restart_armed_start_ticks"],
    }


def prepare_rollback_state(
    *,
    state_path: pathlib.Path,
    status_reader: StatusReader = read_status,
    service_reader: ServiceReader | None = None,
    qconnect_action_runner: QconnectActionRunner | None = None,
    proc_root: pathlib.Path = pathlib.Path("/proc"),
    sleeper: Sleeper = time.sleep,
) -> str:
    """Resolve a pending enable obligation, then persist schema v2 for rollback."""
    try:
        state = _load_state(state_path)
        if state["qconnect_reenable_required"]:
            # Never clear this durable obligation from a lifecycle snapshot.
            # A prior enable may have returned an unknown outcome while status
            # still reports a stale exhausted/retrying state. Re-issuing enable
            # is intentionally idempotent and is the narrowest safe repair.
            status_reader()
            service_probe = service_reader or (
                lambda: read_qbzd_service(runner=run_command, proc_root=proc_root)
            )
            qconnect_effect = qconnect_action_runner or (
                lambda service, action: run_qconnect_action(
                    service, action, proc_root=proc_root
                )
            )
            service = service_probe()
            qconnect_effect(service, "enable")
            restored = False
            for attempt in range(QCONNECT_READBACK_ATTEMPTS):
                try:
                    if _qconnect_control_enabled(status_reader()):
                        restored = True
                        break
                except RecoveryError:
                    pass
                if attempt + 1 < QCONNECT_READBACK_ATTEMPTS:
                    sleeper(QCONNECT_READBACK_INTERVAL_SECONDS)
            if not restored:
                return "blocked:qconnect-reenable-readback"
            state = _clear_qconnect_effect_obligation(state)

        _store_state(state_path, _legacy_v2_state_projection(state))
        return "rollback-state:v2-ready"
    except RecoveryError as exc:
        return f"blocked:{exc}"


def reconcile_once(
    *,
    state_path: pathlib.Path,
    status_reader: StatusReader = read_status,
    service_reader: ServiceReader | None = None,
    runner: Runner = run_command,
    qconnect_action_runner: QconnectActionRunner | None = None,
    proc_root: pathlib.Path = pathlib.Path("/proc"),
    pcm_idle_checker: PcmIdleChecker | None = None,
    pcm_owned_checker: PcmOwnedChecker | None = None,
    sleeper: Sleeper = time.sleep,
    monotonic_clock: Clock = time.monotonic,
    wall_clock: Clock = time.time,
    boot_id_reader: BootIdReader | None = None,
    network_reachability_evidence_realtime: float | None = None,
) -> str:
    try:
        state = _load_state(state_path)
        first = status_reader()
        boot_probe = boot_id_reader or (lambda: read_boot_id(proc_root=proc_root))
        boot_id = boot_probe()
        now_monotonic = _clock_value(monotonic_clock, "monotonic")
        now_unix = _clock_value(wall_clock, "wall")
        if state["boot_id"] not in {None, boot_id}:
            reenable_required = bool(state["qconnect_reenable_required"])
            state = _default_state(boot_id)
            if reenable_required:
                state["qconnect_reenable_required"] = True
            _store_state(state_path, state)
        elif state["boot_id"] is None:
            state = {**state, "boot_id": boot_id}

        service_probe = service_reader or (
            lambda: read_qbzd_service(runner=runner, proc_root=proc_root)
        )
        qconnect_effect = qconnect_action_runner or (
            lambda service, action: run_qconnect_action(
                service, action, proc_root=proc_root
            )
        )

        if state["qconnect_reenable_required"] and not _is_healthy(first):
            # The obligation itself is stronger than any lifecycle snapshot.
            # It must never be cleared by stale `exhausted`/retry lifecycle
            # metadata, but repeated idempotent enable attempts still honor the
            # durable QConnect failure backoff.
            if now_monotonic < float(state["qconnect_next_attempt_monotonic"]):
                return "noop:qconnect-backoff"
            restore_service = service_probe()
            try:
                qconnect_effect(restore_service, "enable")
            except RecoveryError as exc:
                failed_at = _clock_value(monotonic_clock, "monotonic")
                state = _qconnect_failure_state(state, failed_at)
                _store_state(state_path, state)
                return f"blocked:qconnect-reenable:{exc}"

            restored_status: QbzdStatus | None = None
            for attempt in range(QCONNECT_READBACK_ATTEMPTS):
                observed = status_reader()
                if _qconnect_control_enabled(observed):
                    restored_status = observed
                    break
                if attempt + 1 < QCONNECT_READBACK_ATTEMPTS:
                    sleeper(QCONNECT_READBACK_INTERVAL_SECONDS)
            restored_at = _clock_value(monotonic_clock, "monotonic")
            restored_unix = _clock_value(wall_clock, "wall")
            if restored_status is None:
                state = _qconnect_failure_state(state, restored_at)
                _store_state(state_path, state)
                return "blocked:qconnect-reenable-readback"
            if _is_healthy(restored_status):
                _store_state(
                    state_path, _success_state(boot_id, restored_at, restored_unix)
                )
                return "recovered:qconnect-reenabled"
            state = _clear_qconnect_effect_obligation(state)
            _store_state(state_path, state)
            return "restored:qconnect-enabled"

        if _is_healthy(first):
            cleared = _clear_candidate(
                state,
                healthy=True,
                boot_id=boot_id,
                now_monotonic=now_monotonic,
                now_unix=now_unix,
            )
            if cleared != state:
                _store_state(state_path, cleared)
            return "noop:connected"
        if not _is_recovery_candidate(
            first,
            allow_network_offline=_network_reachability_evidence_active(
                network_reachability_evidence_realtime, now_wall=now_unix
            ),
        ):
            if state["retry_since_monotonic"] is not None:
                updated = _clear_candidate(
                    state,
                    healthy=False,
                    boot_id=boot_id,
                    now_monotonic=now_monotonic,
                    now_unix=now_unix,
                )
                _store_state(state_path, updated)
            elif state["boot_id"] != boot_id:
                _store_state(state_path, {**state, "boot_id": boot_id})
            return "noop:not-candidate"

        pcm_idle = pcm_idle_checker or (
            lambda service: require_qbzd_pcm_idle(service, proc_root=proc_root)
        )
        pcm_owned = pcm_owned_checker or (
            lambda service: require_qbzd_pcm_paused(service, proc_root=proc_root)
        )
        service = service_probe()
        retry_since = state["retry_since_monotonic"]
        if (
            not _candidate_binding_matches(state, boot_id, service)
            or retry_since is None
            or now_monotonic < float(retry_since)
        ):
            armed = _arm_candidate(state, boot_id, service, now_monotonic)
            _store_state(state_path, armed)
            return "armed"

        stuck_age = now_monotonic - float(retry_since)
        if (
            first.qconnect_state != "exhausted"
            and stuck_age < QCONNECT_CYCLE_STUCK_SECONDS
        ):
            return "noop:stabilizing"

        qconnect_due = now_monotonic >= float(state["qconnect_next_attempt_monotonic"])
        restart_now = now_monotonic
        if qconnect_due:
            paused_open_cycle = _paused_playback_fingerprint(first) is not None
            if paused_open_cycle:
                pcm_owned(service)
            else:
                pcm_idle(service)
            sleeper(STABILIZATION_SECONDS)
            second = status_reader()
            second_monotonic = _clock_value(monotonic_clock, "monotonic")
            second_unix = _clock_value(wall_clock, "wall")
            if _is_healthy(second):
                _store_state(
                    state_path,
                    _clear_candidate(
                        state,
                        healthy=True,
                        boot_id=boot_id,
                        now_monotonic=second_monotonic,
                        now_unix=second_unix,
                    ),
                )
                return "noop:recovered-naturally"
            if not _is_recovery_candidate(
                second,
                allow_network_offline=_network_reachability_evidence_active(
                    network_reachability_evidence_realtime, now_wall=second_unix
                ),
            ):
                _store_state(
                    state_path,
                    _clear_candidate(
                        state,
                        healthy=False,
                        boot_id=boot_id,
                        now_monotonic=second_monotonic,
                        now_unix=second_unix,
                    ),
                )
                return "noop:changed"
            if boot_probe() != boot_id:
                _store_state(state_path, _default_state(boot_probe()))
                return "noop:boot-changed"
            second_service = service_probe()
            if second_service != service:
                _store_state(
                    state_path,
                    _arm_candidate(state, boot_id, second_service, second_monotonic),
                )
                return "noop:qbzd-restarted"
            if paused_open_cycle:
                if not _same_paused_playback(first, second):
                    return "blocked:playback-not-stably-paused"
                pcm_owned(second_service)
            else:
                if second.device_open is not False:
                    return "blocked:audio-open-state-changed"
                pcm_idle(second_service)

            final_status = status_reader()
            final_monotonic = _clock_value(monotonic_clock, "monotonic")
            final_unix = _clock_value(wall_clock, "wall")
            if _is_healthy(final_status):
                _store_state(
                    state_path,
                    _clear_candidate(
                        state,
                        healthy=True,
                        boot_id=boot_id,
                        now_monotonic=final_monotonic,
                        now_unix=final_unix,
                    ),
                )
                return "noop:recovered-naturally"
            if not _is_recovery_candidate(
                final_status,
                allow_network_offline=_network_reachability_evidence_active(
                    network_reachability_evidence_realtime, now_wall=final_unix
                ),
            ):
                _store_state(
                    state_path,
                    _clear_candidate(
                        state,
                        healthy=False,
                        boot_id=boot_id,
                        now_monotonic=final_monotonic,
                        now_unix=final_unix,
                    ),
                )
                return "noop:changed"
            if boot_probe() != boot_id:
                _store_state(state_path, _default_state(boot_probe()))
                return "noop:boot-changed"
            final_service = service_probe()
            if final_service != service:
                _store_state(
                    state_path,
                    _arm_candidate(state, boot_id, final_service, final_monotonic),
                )
                return "noop:qbzd-restarted"
            if paused_open_cycle:
                if not _same_paused_playback(first, final_status):
                    return "blocked:playback-not-stably-paused"
                pcm_owned(final_service)
                edge_status = status_reader()
                edge_unix = _clock_value(wall_clock, "wall")
                if not _is_recovery_candidate(
                    edge_status,
                    allow_network_offline=_network_reachability_evidence_active(
                        network_reachability_evidence_realtime, now_wall=edge_unix
                    ),
                ):
                    return "blocked:playback-changed-at-effect-edge"
                if not _same_paused_playback(first, edge_status):
                    return "blocked:playback-changed-at-effect-edge"
                if service_probe() != final_service:
                    return "blocked:qbzd-process-changed-at-effect-edge"
                # QBZD playback metadata is not authoritative here. Production
                # therefore takes one final independent kernel PAUSED/owner
                # observation after the last QBZD status and process read. Test
                # seams may inject their own equivalent checker.
                if pcm_owned_checker is None:
                    pcm_owned(final_service)
                final_status = edge_status
            else:
                if final_status.device_open is not False:
                    return "blocked:audio-open-state-changed"
                pcm_idle(final_service)

            effect_monotonic = _clock_value(monotonic_clock, "monotonic")
            if final_status.network_online is False:
                effect_unix = _clock_value(wall_clock, "wall")
                if not _network_reachability_evidence_active(
                    network_reachability_evidence_realtime, now_wall=effect_unix
                ):
                    return "blocked:network-attestation-expired"
            qconnect_armed = _qconnect_effect_armed_state(
                state, final_service, effect_monotonic
            )
            _store_state(state_path, qconnect_armed)
            qconnect_error: RecoveryError | None = None
            try:
                qconnect_effect(final_service, "disable")
                mid_service = service_probe()
                if mid_service != final_service:
                    raise RecoveryError("qbzd-process-changed-during-qconnect-cycle")
                qconnect_effect(mid_service, "enable")
            except RecoveryError as exc:
                qconnect_error = exc

            qconnect_recovered = False
            qconnect_enabled_observed = False
            if qconnect_error is None:
                for attempt in range(QCONNECT_READBACK_ATTEMPTS):
                    try:
                        after = status_reader()
                        if _qconnect_control_enabled(after):
                            qconnect_enabled_observed = True
                        if _is_healthy(after):
                            if service_probe() != final_service:
                                raise RecoveryError("qbzd-process-changed-after-qconnect-cycle")
                            qconnect_recovered = True
                            break
                    except RecoveryError:
                        pass
                    if attempt + 1 < QCONNECT_READBACK_ATTEMPTS:
                        sleeper(QCONNECT_READBACK_INTERVAL_SECONDS)
            if qconnect_enabled_observed:
                qconnect_armed = _clear_qconnect_effect_obligation(qconnect_armed)
            completion_monotonic = _clock_value(monotonic_clock, "monotonic")
            completion_unix = _clock_value(wall_clock, "wall")
            restart_now = completion_monotonic
            if qconnect_recovered:
                _store_state(
                    state_path,
                    _success_state(boot_id, completion_monotonic, completion_unix),
                )
                return "recovered:qconnect"

            state = _qconnect_failure_state(qconnect_armed, completion_monotonic)
            _store_state(state_path, state)
            if state["qconnect_reenable_required"]:
                if qconnect_error is not None:
                    return f"blocked:{qconnect_error}"
                return "blocked:qconnect-reenable-required"
            if stuck_age < STUCK_SECONDS:
                if qconnect_error is not None:
                    return f"blocked:{qconnect_error}"
                return "blocked:qconnect-readback"
        elif stuck_age < STUCK_SECONDS:
            return "noop:qconnect-backoff"

        if stuck_age < STUCK_SECONDS:
            return "noop:stabilizing"
        if restart_now < float(state["next_attempt_monotonic"]):
            return "noop:backoff"

        if not _is_daemon_restart_candidate(
            first,
            allow_network_offline=_network_reachability_evidence_active(
                network_reachability_evidence_realtime, now_wall=now_unix
            ),
        ):
            return "blocked:audio-open-for-daemon-restart"
        pcm_idle(service)
        sleeper(STABILIZATION_SECONDS)
        second = status_reader()
        second_monotonic = _clock_value(monotonic_clock, "monotonic")
        second_unix = _clock_value(wall_clock, "wall")
        if _is_healthy(second):
            _store_state(
                state_path,
                _clear_candidate(
                    state,
                    healthy=True,
                    boot_id=boot_id,
                    now_monotonic=second_monotonic,
                    now_unix=second_unix,
                ),
            )
            return "noop:recovered-naturally"
        if not _is_daemon_restart_candidate(
            second,
            allow_network_offline=_network_reachability_evidence_active(
                network_reachability_evidence_realtime, now_wall=second_unix
            ),
        ):
            _store_state(
                state_path,
                _clear_candidate(
                    state,
                    healthy=False,
                    boot_id=boot_id,
                    now_monotonic=second_monotonic,
                    now_unix=second_unix,
                ),
            )
            return "noop:changed"
        if boot_probe() != boot_id:
            _store_state(state_path, _default_state(boot_probe()))
            return "noop:boot-changed"
        second_service = service_probe()
        if second_service != service:
            _store_state(
                state_path,
                _arm_candidate(state, boot_id, second_service, second_monotonic),
            )
            return "noop:qbzd-restarted"
        pcm_idle(second_service)

        final_status = status_reader()
        final_monotonic = _clock_value(monotonic_clock, "monotonic")
        final_unix = _clock_value(wall_clock, "wall")
        if _is_healthy(final_status):
            _store_state(
                state_path,
                _clear_candidate(
                    state,
                    healthy=True,
                    boot_id=boot_id,
                    now_monotonic=final_monotonic,
                    now_unix=final_unix,
                ),
            )
            return "noop:recovered-naturally"
        if not _is_daemon_restart_candidate(
            final_status,
            allow_network_offline=_network_reachability_evidence_active(
                network_reachability_evidence_realtime, now_wall=final_unix
            ),
        ):
            _store_state(
                state_path,
                _clear_candidate(
                    state,
                    healthy=False,
                    boot_id=boot_id,
                    now_monotonic=final_monotonic,
                    now_unix=final_unix,
                ),
            )
            return "noop:changed"
        if boot_probe() != boot_id:
            _store_state(state_path, _default_state(boot_probe()))
            return "noop:boot-changed"
        final_service = service_probe()
        if final_service != service:
            _store_state(
                state_path,
                _arm_candidate(state, boot_id, final_service, final_monotonic),
            )
            return "noop:qbzd-restarted"
        pcm_idle(final_service)

        effect_monotonic = _clock_value(monotonic_clock, "monotonic")
        if final_status.network_online is False:
            effect_unix = _clock_value(wall_clock, "wall")
            if not _network_reachability_evidence_active(
                network_reachability_evidence_realtime, now_wall=effect_unix
            ):
                return "blocked:network-attestation-expired"
        armed_effect = _effect_armed_state(state, final_service, effect_monotonic)
        _store_state(state_path, armed_effect)
        runner(("systemctl", "--user", "try-restart", QBZD_UNIT))

        recovered = False
        for attempt in range(READBACK_ATTEMPTS):
            try:
                after = status_reader()
                if _is_healthy(after):
                    service_probe()
                    recovered = True
                    break
            except RecoveryError:
                pass
            if attempt + 1 < READBACK_ATTEMPTS:
                sleeper(READBACK_INTERVAL_SECONDS)
        completion_monotonic = _clock_value(monotonic_clock, "monotonic")
        completion_unix = _clock_value(wall_clock, "wall")
        if recovered:
            _store_state(
                state_path,
                _success_state(boot_id, completion_monotonic, completion_unix),
            )
            return "recovered"
        _store_state(
            state_path, _failure_state(armed_effect, completion_monotonic)
        )
        return "blocked:restart-readback"
    except RecoveryError as exc:
        return f"blocked:{exc}"


def _fast_followup_required(result: str) -> bool:
    return result not in {
        "noop:connected",
        "noop:recovered-naturally",
        "recovered",
        "recovered:qconnect",
    }


def adaptive_poll_reason(
    *,
    fast_followup: bool,
    journal_available: bool,
    journal_text: str,
    last_status_monotonic: float | None,
    now_monotonic: float,
) -> str | None:
    if fast_followup:
        return "fast-followup"
    if not journal_available:
        return "journal-fallback"
    if journal_requires_status(journal_text):
        return "journal-trigger"
    if last_status_monotonic is None:
        return "startup"
    if now_monotonic < last_status_monotonic:
        return "monotonic-reset"
    if now_monotonic - last_status_monotonic >= HEALTHY_STATUS_FALLBACK_SECONDS:
        return "safety-fallback"
    return None


def check_contract() -> None:
    _validate_command(
        (
            "systemctl",
            "--user",
            "show",
            QBZD_UNIT,
            "--property=ActiveState",
            "--property=MainPID",
        )
    )
    _validate_command(("systemctl", "--user", "try-restart", QBZD_UNIT))
    if QBZD_HOST != "127.0.0.1" or QBZD_PORT != 8182 or QBZD_STATUS_PATH != "/api/status":
        raise RecoveryError("status-endpoint-contract-drift")
    if EXPECTED_DEVICE != "front:CARD=M2,DEV=0":
        raise RecoveryError("device-contract-drift")
    if JOURNAL_EXECUTABLE != pathlib.Path("/usr/bin/journalctl"):
        raise RecoveryError("journal-executable-contract-drift")
    if HEALTHY_STATUS_FALLBACK_SECONDS < POLL_SECONDS:
        raise RecoveryError("healthy-fallback-contract-drift")
    if not QCONNECT_JOURNAL_TRIGGERS or any(
        not isinstance(trigger, str) or not trigger for trigger in QCONNECT_JOURNAL_TRIGGERS
    ):
        raise RecoveryError("journal-trigger-contract-drift")


def run_loop(
    state_path: pathlib.Path,
    *,
    reconciler: Reconciler | None = None,
    journal_reader: JournalReader = read_journal_delta,
    sleeper: Sleeper = time.sleep,
    monotonic_clock: Clock = time.monotonic,
    wall_clock: Clock = time.time,
) -> None:
    previous: str | None = None
    cursor: str | None = None
    last_status_monotonic: float | None = None
    fast_followup = True
    network_evidence_realtime: float | None = None

    try:
        cursor = journal_reader(None).cursor
    except RecoveryError:
        cursor = None

    while True:
        journal_text = ""
        journal_delta: JournalDelta | None = None
        journal_available = cursor is not None
        if cursor is not None:
            try:
                journal_delta = journal_reader(cursor)
                cursor = journal_delta.cursor
                journal_text = journal_delta.text
                journal_available = True
            except RecoveryError:
                cursor = None
                journal_available = False

        if cursor is None:
            try:
                cursor = journal_reader(None).cursor
            except RecoveryError:
                cursor = None

        now_monotonic = _clock_value(monotonic_clock, "loop-monotonic")
        now_wall = _clock_value(wall_clock, "loop-wall")
        if (
            journal_delta is not None
            and journal_proves_qconnect_network_reachable(
                journal_delta, now_wall=now_wall
            )
        ):
            network_evidence_realtime = (
                journal_qconnect_network_reachability_timestamp(journal_delta)
            )
        if network_evidence_realtime is not None:
            evidence_age = now_wall - network_evidence_realtime
            if (
                evidence_age < -NETWORK_REACHABILITY_FUTURE_SKEW_SECONDS
                or evidence_age > NETWORK_REACHABILITY_EVIDENCE_SECONDS
            ):
                network_evidence_realtime = None
        reason = adaptive_poll_reason(
            fast_followup=fast_followup,
            journal_available=journal_available,
            journal_text=journal_text,
            last_status_monotonic=last_status_monotonic,
            now_monotonic=now_monotonic,
        )
        if reason is not None:
            if reconciler is None:
                result = reconcile_once(
                    state_path=state_path,
                    network_reachability_evidence_realtime=network_evidence_realtime,
                )
            else:
                result = reconciler(state_path)
            last_status_monotonic = _clock_value(
                monotonic_clock, "loop-status-monotonic"
            )
            fast_followup = _fast_followup_required(result)
            if not fast_followup:
                network_evidence_realtime = None
            if result != previous and not result.startswith("noop:"):
                print(json.dumps({"qbzd_qconnect_recovery": result}), flush=True)
            previous = result

        sleeper(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--state-file", type=pathlib.Path, required=True)
    rollback_parser = subparsers.add_parser("prepare-rollback")
    rollback_parser.add_argument("--state-file", type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.command == "check":
        check_contract()
        return 0
    if args.command == "prepare-rollback":
        try:
            state_file = args.state_file or _state_file_from_environment()
        except RecoveryError as exc:
            result = f"blocked:{exc}"
        else:
            result = prepare_rollback_state(state_path=state_file)
        print(json.dumps({"qbzd_qconnect_recovery": result}), flush=True)
        return 0 if result == "rollback-state:v2-ready" else 1
    run_loop(args.state_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
