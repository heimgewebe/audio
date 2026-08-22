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
STUCK_SECONDS = 300.0
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
STATE_SCHEMA_VERSION = 2


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
    uptime_secs: int


@dataclass(frozen=True)
class QbzdService:
    pid: int
    start_ticks: int
    cgroup: str


StatusReader = Callable[[], QbzdStatus]
Runner = Callable[[tuple[str, ...]], str]
Sleeper = Callable[[float], None]
ServiceReader = Callable[[], QbzdService]
PcmIdleChecker = Callable[[QbzdService], None]
Clock = Callable[[], float]
BootIdReader = Callable[[], str]


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
        uptime_secs=uptime,
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
    return QbzdService(pid=pid, start_ticks=start_ticks, cgroup=cgroup)


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
        "failures": 0,
        "next_attempt_monotonic": 0.0,
        "last_recovered_at_unix": None,
        "restart_armed_monotonic": None,
        "restart_armed_pid": None,
        "restart_armed_start_ticks": None,
    }


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
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != STATE_SCHEMA_VERSION
    ):
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
    if (retry_since is not None or restart_armed is not None) and boot_id is None:
        raise RecoveryError("state-invalid:boot-binding")

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "boot_id": boot_id,
        "candidate_pid": candidate_pid,
        "candidate_start_ticks": candidate_start,
        "retry_since_monotonic": retry_since,
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


def _is_healthy(status: QbzdStatus) -> bool:
    return status.qconnect_state == "connected" and status.session_active


def _is_recovery_candidate(status: QbzdStatus) -> bool:
    return (
        status.auth_state == "logged_in"
        and status.network_online is True
        and status.qconnect_state in {"retrying", "reconnecting"}
        and status.session_active is False
        and status.audio_backend.casefold() == "alsa"
        and status.configured_device == EXPECTED_DEVICE
        and status.device_present is True
        and status.device_open is False
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
        "failures": 0,
        "next_attempt_monotonic": now_monotonic + SUCCESS_COOLDOWN_SECONDS,
        "last_recovered_at_unix": now_unix,
        "restart_armed_monotonic": None,
        "restart_armed_pid": None,
        "restart_armed_start_ticks": None,
    }


def reconcile_once(
    *,
    state_path: pathlib.Path,
    status_reader: StatusReader = read_status,
    service_reader: ServiceReader | None = None,
    runner: Runner = run_command,
    proc_root: pathlib.Path = pathlib.Path("/proc"),
    pcm_idle_checker: PcmIdleChecker | None = None,
    sleeper: Sleeper = time.sleep,
    monotonic_clock: Clock = time.monotonic,
    wall_clock: Clock = time.time,
    boot_id_reader: BootIdReader | None = None,
) -> str:
    try:
        state = _load_state(state_path)
        first = status_reader()
        boot_probe = boot_id_reader or (lambda: read_boot_id(proc_root=proc_root))
        boot_id = boot_probe()
        now_monotonic = _clock_value(monotonic_clock, "monotonic")
        now_unix = _clock_value(wall_clock, "wall")
        if state["boot_id"] not in {None, boot_id}:
            state = _default_state(boot_id)
            _store_state(state_path, state)
        elif state["boot_id"] is None:
            state = {**state, "boot_id": boot_id}

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
        if not _is_recovery_candidate(first):
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

        service_probe = service_reader or (
            lambda: read_qbzd_service(runner=runner, proc_root=proc_root)
        )
        pcm_idle = pcm_idle_checker or (
            lambda service: require_qbzd_pcm_idle(
                service, proc_root=proc_root
            )
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
        if now_monotonic - float(retry_since) < STUCK_SECONDS:
            return "noop:stabilizing"
        if now_monotonic < float(state["next_attempt_monotonic"]):
            return "noop:backoff"

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
        if not _is_recovery_candidate(second):
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

        # Re-read both QConnect and service identity at the restart edge. This
        # narrows, but cannot atomically eliminate, the interval in which an
        # uncooperating client could open ALSA after the final observation.
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
        if not _is_recovery_candidate(final_status):
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
        armed_effect = _effect_armed_state(state, final_service, effect_monotonic)
        # Persist the attempt and minimum backoff before the only mutation. If
        # systemctl loses its reply or the process dies after this fsync, the
        # next observer run cannot immediately repeat the restart.
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


def run_loop(state_path: pathlib.Path) -> None:
    previous: str | None = None
    while True:
        result = reconcile_once(state_path=state_path)
        if result != previous and not result.startswith("noop:"):
            print(json.dumps({"qbzd_qconnect_recovery": result}), flush=True)
        previous = result
        time.sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--state-file", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "check":
        check_contract()
        return 0
    run_loop(args.state_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
