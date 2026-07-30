#!/usr/bin/env python3
"""Create a bounded, read-only managed plugin-host observation."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import stat
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAB_PATH = ROOT / "scripts" / "laboratory_gate.py"
SYSTEM_TRUTH_PATH = ROOT / "scripts" / "system_truth.py"
PROC_ROOT = pathlib.Path("/proc")
PS_ARGV = ("ps", "-eo", "pid=,ppid=,etimes=,comm=,args=")
SYSTEMD_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "ControlGroup",
    "MemoryCurrent",
    "MemoryMax",
    "TasksCurrent",
    "TasksMax",
    "LimitNOFILE",
    "StandardOutput",
    "StandardError",
    "LogRateLimitIntervalUSec",
    "LogRateLimitBurst",
    "NRestarts",
)
SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
MAX_PROC_BYTES = 65_536


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LAB = load_module("laboratory_gate_for_plugin_host_observer", LAB_PATH)
SYSTEM_TRUTH = load_module("system_truth_for_plugin_host_observer", SYSTEM_TRUTH_PATH)
PLUGIN_HOST_EXECUTABLES = frozenset(SYSTEM_TRUTH.PROCESS_COMMANDS["plugin-host"])


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def monotonic_now() -> float:
    return time.monotonic()


def sleep_for(seconds: int) -> None:
    time.sleep(seconds)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _run_read_only(argv: tuple[str, ...]):
    SYSTEM_TRUTH.assert_read_only_commands((argv,))
    result = SYSTEM_TRUTH.run_read_only(argv)
    if result.argv != argv:
        raise ValueError("read-only result is bound to another command")
    if result.error is not None or result.returncode != 0:
        raise ValueError(f"read-only command failed: {argv[0]}")
    if result.stdout_truncated or result.stderr_truncated:
        raise ValueError(f"read-only command output is truncated: {argv[0]}")
    return result


def _read_proc_text(pid: int, name: str) -> str:
    if name not in {"stat", "cgroup"}:
        raise ValueError("unsupported proc identity file")
    path = PROC_ROOT / str(pid) / name
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"proc identity is not a regular file: {name}")
        chunks: list[bytes] = []
        remaining = MAX_PROC_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_PROC_BYTES:
            raise ValueError(f"proc identity exceeds {MAX_PROC_BYTES} bytes: {name}")
        return payload.decode("utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read stable proc identity for PID {pid}: {name}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _process_start_ticks(stat_text: str) -> int:
    closing = stat_text.rfind(")")
    if closing < 0:
        raise ValueError("proc stat has no command boundary")
    fields = stat_text[closing + 2 :].split()
    if len(fields) <= 19:
        raise ValueError("proc stat has no start-time field")
    try:
        value = int(fields[19])
    except ValueError as exc:
        raise ValueError("proc start-time field is not numeric") from exc
    return _positive_int(value, "process_start_ticks")


def _service_from_cgroup(cgroup_text: str) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    for raw in cgroup_text.splitlines():
        parts = raw.split(":", 2)
        if len(parts) != 3:
            continue
        cgroup = parts[2].strip()
        if "/user.slice/" not in cgroup or "/app.slice/" not in cgroup:
            continue
        unit = pathlib.PurePosixPath(cgroup).name
        if SERVICE_RE.fullmatch(unit):
            candidates.append((unit, cgroup))
    if len(candidates) != 1:
        raise ValueError("plugin-host process is not bound to one user service")
    return candidates[0]


def _proc_identity(pid: int) -> dict[str, Any]:
    stat_before = _read_proc_text(pid, "stat")
    start_before = _process_start_ticks(stat_before)
    cgroup = _read_proc_text(pid, "cgroup")
    unit, cgroup_path = _service_from_cgroup(cgroup)
    stat_after = _read_proc_text(pid, "stat")
    start_after = _process_start_ticks(stat_after)
    if start_before != start_after:
        raise ValueError("plugin-host process identity changed during proc read")
    return {
        "process_start_ticks": start_before,
        "unit": unit,
        "cgroup": cgroup_path,
        "cgroup_sha256": _sha256_text(cgroup_path),
    }


def _parse_properties(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in SYSTEMD_PROPERTIES:
            result[key] = value
    missing = sorted(set(SYSTEMD_PROPERTIES) - set(result))
    if missing:
        raise ValueError(f"plugin-host service properties are incomplete: {missing}")
    return result


def _parse_limit(value: str, label: str) -> int | None:
    if value == "infinity":
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not numeric or infinity") from exc
    return _positive_int(parsed, label)


def _parse_optional_counter(value: str, label: str) -> int | None:
    if value in {"", "[not set]"}:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not numeric") from exc
    return _nonnegative_int(parsed, label)


def _service_properties(unit: str) -> dict[str, Any]:
    if SERVICE_RE.fullmatch(unit) is None:
        raise ValueError("plugin-host unit name is invalid")
    argv = (
        "systemctl",
        "--user",
        "show",
        unit,
        "--no-pager",
        "--property=" + ",".join(SYSTEMD_PROPERTIES),
    )
    result = _run_read_only(argv)
    raw = _parse_properties(result.stdout)
    return {
        "unit": raw["Id"],
        "load_state": raw["LoadState"],
        "active_state": raw["ActiveState"],
        "sub_state": raw["SubState"],
        "control_group": raw["ControlGroup"],
        "control_group_sha256": _sha256_text(raw["ControlGroup"]),
        "memory_current_bytes": _parse_optional_counter(
            raw["MemoryCurrent"], "MemoryCurrent"
        ),
        "memory_max_bytes": _parse_limit(raw["MemoryMax"], "MemoryMax"),
        "tasks_current": _parse_optional_counter(raw["TasksCurrent"], "TasksCurrent"),
        "tasks_max": _parse_limit(raw["TasksMax"], "TasksMax"),
        "limit_nofile": _parse_limit(raw["LimitNOFILE"], "LimitNOFILE"),
        "standard_output": raw["StandardOutput"],
        "standard_error": raw["StandardError"],
        "log_rate_limit_interval_usec": _nonnegative_int(
            int(raw["LogRateLimitIntervalUSec"]), "LogRateLimitIntervalUSec"
        ),
        "log_rate_limit_burst": _nonnegative_int(
            int(raw["LogRateLimitBurst"]), "LogRateLimitBurst"
        ),
        "restart_count": _nonnegative_int(int(raw["NRestarts"]), "NRestarts"),
        "query_argv_sha256": LAB.canonical_value_sha256(list(argv)),
        "query_stdout_sha256": result.stdout_sha256,
    }


def _parse_processes(text: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line in text.splitlines():
        fields = line.strip().split(None, 4)
        if len(fields) < 4:
            continue
        try:
            pid = int(fields[0])
            ppid = int(fields[1])
            elapsed = int(fields[2])
        except ValueError:
            continue
        executable = pathlib.PurePath(fields[3]).name
        if executable not in PLUGIN_HOST_EXECUTABLES:
            continue
        arguments = fields[4] if len(fields) == 5 else executable
        record: dict[str, Any] = {
            "pid": _positive_int(pid, "pid"),
            "ppid": _nonnegative_int(ppid, "ppid"),
            "elapsed_seconds": _nonnegative_int(elapsed, "elapsed_seconds"),
            "executable": executable,
            "command_sha256": _sha256_text(arguments),
        }
        try:
            identity = _proc_identity(pid)
            properties = _service_properties(identity["unit"])
            record.update(identity)
            record["service"] = properties
        except ValueError as exc:
            record.update(
                {
                    "process_start_ticks": None,
                    "unit": None,
                    "cgroup": None,
                    "cgroup_sha256": None,
                    "service": None,
                    "observation_error": str(exc)[:500],
                }
            )
        result.append(record)
    return sorted(result, key=lambda item: (str(item.get("unit")), item["pid"]))


def process_snapshot() -> dict[str, Any]:
    result = _run_read_only(PS_ARGV)
    processes = _parse_processes(result.stdout)
    return {
        "processes": processes,
        "process_count": len(processes),
        "query_argv_sha256": LAB.canonical_value_sha256(list(PS_ARGV)),
        "query_stdout_sha256": result.stdout_sha256,
    }


def _truth_binding() -> dict[str, str]:
    report = SYSTEM_TRUTH.build_report()
    SYSTEM_TRUTH.verify_report(report)
    runtime = report.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("system-truth report has no runtime projection")
    return {
        "report_sha256": str(report["report_sha256"]),
        "truth_chain_sha256": str(report["truth_chain_sha256"]),
        "process_fingerprint": str(runtime["process_fingerprint"]),
    }


def _implementation_binding() -> dict[str, str]:
    return {
        "plugin_host_observer_sha256": hashlib.sha256(
            pathlib.Path(__file__).read_bytes()
        ).hexdigest(),
        "laboratory_gate_sha256": hashlib.sha256(LAB_PATH.read_bytes()).hexdigest(),
        "system_truth_sha256": hashlib.sha256(SYSTEM_TRUTH_PATH.read_bytes()).hexdigest(),
    }


def _identity(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record["pid"],
        record["executable"],
        record["command_sha256"],
        record.get("process_start_ticks"),
        record.get("unit"),
        record.get("observation_error"),
    )


def _service_blockers(record: dict[str, Any]) -> list[str]:
    if record.get("observation_error"):
        return [f"{record['executable']}:unmanaged-or-unreadable"]
    service = record["service"]
    unit = record["unit"]
    blockers: list[str] = []
    if service["unit"] != unit:
        blockers.append(f"{unit}:service-id-mismatch")
    if service["load_state"] != "loaded":
        blockers.append(f"{unit}:not-loaded")
    if service["active_state"] != "active" or service["sub_state"] != "running":
        blockers.append(f"{unit}:not-running")
    if service["control_group"] != record["cgroup"]:
        blockers.append(f"{unit}:control-group-mismatch")
    memory_max = service["memory_max_bytes"]
    if memory_max is None:
        blockers.append(f"{unit}:memory-max-unbounded")
    elif memory_max > LAB.MAX_PLUGIN_HOST_MEMORY_BYTES:
        blockers.append(f"{unit}:memory-max-too-large")
    memory_current = service["memory_current_bytes"]
    if memory_current is None:
        blockers.append(f"{unit}:memory-current-unknown")
    elif memory_max is not None and memory_current > memory_max:
        blockers.append(f"{unit}:memory-current-exceeds-max")
    tasks_max = service["tasks_max"]
    if tasks_max is None:
        blockers.append(f"{unit}:tasks-max-unbounded")
    elif tasks_max > LAB.MAX_PLUGIN_HOST_TASKS:
        blockers.append(f"{unit}:tasks-max-too-large")
    tasks_current = service["tasks_current"]
    if tasks_current is None:
        blockers.append(f"{unit}:tasks-current-unknown")
    elif tasks_max is not None and tasks_current > tasks_max:
        blockers.append(f"{unit}:tasks-current-exceeds-max")
    nofile = service["limit_nofile"]
    if nofile is None or nofile > LAB.MAX_PLUGIN_HOST_NOFILE:
        blockers.append(f"{unit}:nofile-limit-too-large")
    if service["standard_output"] not in {"journal", "journal-or-kmsg"}:
        blockers.append(f"{unit}:stdout-not-journal")
    if service["standard_error"] not in {"inherit", "journal", "journal-or-kmsg"}:
        blockers.append(f"{unit}:stderr-not-journal")
    if service["log_rate_limit_interval_usec"] <= 0:
        blockers.append(f"{unit}:log-rate-interval-unbounded")
    if service["log_rate_limit_burst"] <= 0:
        blockers.append(f"{unit}:log-rate-burst-unbounded")
    return blockers


def managed_plugin_host_evidence(duration_seconds: int) -> dict[str, Any]:
    duration_seconds = _positive_int(duration_seconds, "duration_seconds")
    if duration_seconds < 60 or duration_seconds > 86_400:
        raise ValueError("plugin-host observation must cover 60 to 86400 seconds")

    truth_before = _truth_binding()
    before = process_snapshot()
    started_at = utc_now()
    started_monotonic = monotonic_now()
    sleep_for(duration_seconds)
    ended_monotonic = monotonic_now()
    ended_at = utc_now()
    actual_duration = ended_monotonic - started_monotonic
    if actual_duration < duration_seconds:
        raise ValueError("plugin-host observation ended before the requested duration")
    after = process_snapshot()
    truth_after = _truth_binding()

    blockers: list[str] = []
    before_processes = before["processes"]
    after_processes = after["processes"]
    if not before_processes:
        blockers.append("no-active-plugin-host")
    if [_identity(item) for item in before_processes] != [
        _identity(item) for item in after_processes
    ]:
        blockers.append("plugin-host-process-set-changed")
    if any(item["executable"] == "sfizz_jack" for item in before_processes + after_processes):
        blockers.append("standalone-sfizz-jack-active")

    for phase, processes in (("before", before_processes), ("after", after_processes)):
        for record in processes:
            blockers.extend(f"{phase}:{item}" for item in _service_blockers(record))
    before_restarts = {
        item["unit"]: item["service"]["restart_count"]
        for item in before_processes
        if item.get("unit") and isinstance(item.get("service"), dict)
    }
    after_restarts = {
        item["unit"]: item["service"]["restart_count"]
        for item in after_processes
        if item.get("unit") and isinstance(item.get("service"), dict)
    }
    if before_restarts != after_restarts:
        blockers.append("plugin-host-restart-count-changed")

    started_text = started_at.isoformat()
    ended_text = ended_at.isoformat()
    units = sorted(
        {
            item["unit"]
            for item in before_processes + after_processes
            if isinstance(item.get("unit"), str)
        }
    )
    journal: dict[str, Any] | None = None
    if units:
        argv = LAB.plugin_host_journal_argv(units, started_text, ended_text)
        journal_result = _run_read_only(argv)
        lines = journal_result.stdout.splitlines()
        if len(lines) > LAB.MAX_PLUGIN_HOST_JOURNAL_LINES:
            blockers.append("plugin-host-journal-line-limit-exceeded")
        journal = {
            "source": "journalctl-user-plugin-host-units",
            "units": units,
            "query_argv": list(argv),
            "query_argv_sha256": LAB.canonical_value_sha256(list(argv)),
            "returncode": journal_result.returncode,
            "stdout_sha256": journal_result.stdout_sha256,
            "stdout_total_bytes": journal_result.stdout_total_bytes,
            "stdout_truncated": journal_result.stdout_truncated,
            "line_count": len(lines),
            "max_lines": LAB.MAX_PLUGIN_HOST_JOURNAL_LINES,
            "complete": True,
        }

    blockers = sorted(set(blockers))
    managed_process = bool(before_processes) and not any(
        "not-loaded" in item
        or "not-running" in item
        or "service-id-mismatch" in item
        or "control-group-mismatch" in item
        or "unmanaged-or-unreadable" in item
        or item == "plugin-host-process-set-changed"
        for item in blockers
    )
    bounded_resources = bool(before_processes) and not any(
        any(
            token in item
            for token in (
                "memory-",
                "tasks-",
                "nofile-",
            )
        )
        for item in blockers
    )
    bounded_logs = bool(before_processes) and journal is not None and not any(
        "stdout-not-journal" in item
        or "stderr-not-journal" in item
        or "log-rate-" in item
        or "journal-" in item
        for item in blockers
    )
    standalone_sfizz_jack = any(
        item["executable"] == "sfizz_jack" for item in before_processes + after_processes
    )

    payload = {
        "schema_version": 1,
        "kind": "managed_plugin_host_validation",
        "gate": "managed-plugin-host-proof",
        "result": "pass" if not blockers else "fail",
        "measured_at": ended_text,
        "physical_state_sha256": None,
        "requested_duration_seconds": duration_seconds,
        "runtime_seconds": round(actual_duration, 3),
        "observation_started_at": started_text,
        "observation_ended_at": ended_text,
        "managed_process": managed_process,
        "bounded_resources": bounded_resources,
        "bounded_logs": bounded_logs,
        "standalone_sfizz_jack": standalone_sfizz_jack,
        "process_count": len(before_processes),
        "blockers": blockers,
        "implementation": _implementation_binding(),
        "truth_before": truth_before,
        "truth_after": truth_after,
        "processes_before": before_processes,
        "processes_after": after_processes,
        "journal": journal,
        "criteria": {
            "minimum_runtime_seconds": 60,
            "maximum_memory_bytes": LAB.MAX_PLUGIN_HOST_MEMORY_BYTES,
            "maximum_tasks": LAB.MAX_PLUGIN_HOST_TASKS,
            "maximum_nofile": LAB.MAX_PLUGIN_HOST_NOFILE,
            "requires_service_log_rate_limit": True,
            "standalone_sfizz_jack_forbidden": True,
        },
        "does_not_establish": [
            "plugin audio quality",
            "stability outside the bounded observation window",
            "permission to modify or restart plugin-host services",
        ],
    }
    if payload["result"] == "pass":
        LAB.validate_evidence("managed-plugin-host-proof", payload)
    return payload
