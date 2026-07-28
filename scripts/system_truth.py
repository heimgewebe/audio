#!/usr/bin/env python3
"""Build and compare one hash-bound, read-only Heim-PC audio truth report."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
MAX_COMMAND_BYTES = 262_144
COMMAND_TIMEOUT_SECONDS = 12.0
MAX_TREE_ENTRIES = 5_000


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DOCTOR = load_module("audio_doctor_for_system_truth", ROOT / "scripts/audio_doctor.py")
PHYSICAL = load_module(
    "physical_verification_for_system_truth", ROOT / "scripts/physical_verification.py"
)

CONTRACT_PATHS: dict[str, pathlib.Path] = {
    "signal_path": ROOT / "inventory/signal-path.v1.json",
    "physical_fact_catalog": ROOT / "inventory/physical-facts.v1.json",
    "physical_template": ROOT / "inventory/physical-verification.v1.json",
    "laboratory_gates": ROOT / "inventory/laboratory-gates.v1.json",
    "system_truth_contract": ROOT / "inventory/system-truth.v1.json",
    "audio_profiles": ROOT / "profiles/audio-profiles.v1.json",
    "reference_levels": ROOT / "profiles/reference-levels.v1.json",
}

READ_ONLY_COMMANDS: tuple[tuple[str, ...], ...] = (
    (
        "systemctl",
        "--user",
        "is-active",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "mopidy",
        "easyeffects",
    ),
    (
        "systemctl",
        "--user",
        "show",
        "pipewire.service",
        "pipewire-pulse.service",
        "wireplumber.service",
        "mopidy.service",
        "easyeffects.service",
        "--property=Id,LoadState,ActiveState,SubState,NRestarts,MemoryCurrent,TasksCurrent,LimitNOFILE",
        "--no-pager",
    ),
    ("ps", "-eo", "pid,ppid,stat,etimes,%cpu,%mem,comm,args"),
    ("df", "-B1", "/"),
    ("journalctl", "--user", "--disk-usage", "--no-pager"),
    (
        "journalctl",
        "--user",
        "-u",
        "pipewire",
        "-u",
        "pipewire-pulse",
        "-u",
        "wireplumber",
        "-u",
        "mopidy",
        "-u",
        "easyeffects",
        "--since",
        "1 hour ago",
        "--no-pager",
        "-n",
        "500",
    ),
    ("uname", "-r"),
    ("pipewire", "--version"),
    ("wireplumber", "--version"),
    ("mopidy", "--version"),
)

FORBIDDEN_MUTATION_TOKENS = frozenset(
    {
        "start",
        "stop",
        "restart",
        "reload",
        "enable",
        "disable",
        "mask",
        "unmask",
        "set-default-sink",
        "set-default-source",
        "pw-link",
        "rm",
        "mv",
        "cp",
        "install",
        "remove",
        "purge",
        "truncate",
        "tee",
        "kill",
    }
)
PROCESS_COMMANDS: dict[str, frozenset[str]] = {
    "recorder": frozenset({"pw-record", "arecord", "jack_capture", "ardour"}),
    "plugin-host": frozenset(
        {"carla", "easyeffects", "sfizz", "sfizz_jack", "fluidsynth", "qsynth"}
    ),
    "playback": frozenset({"mopidy", "qobuz"}),
}
CREATIVE_NAME = re.compile(r"(?:whale|buckelwal|dauersong|animal)", re.I)
XRUN_PATTERN = re.compile(r"\b(xrun|underrun|overrun|dropout)\b", re.I)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    error: str | None = None
    duration_ms: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def file_binding(path: pathlib.Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
    }


def assert_read_only_commands(commands: Iterable[tuple[str, ...]] = READ_ONLY_COMMANDS) -> None:
    violations: list[str] = []
    for command in commands:
        hit = sorted({token.casefold() for token in command} & FORBIDDEN_MUTATION_TOKENS)
        if hit:
            violations.append(f"{shlex.join(command)}: {', '.join(hit)}")
    if violations:
        raise RuntimeError("mutation-capable truth command: " + "; ".join(violations))


def _bounded_text(value: bytes) -> tuple[str, bool]:
    return value[:MAX_COMMAND_BYTES].decode("utf-8", "replace"), len(value) > MAX_COMMAND_BYTES


def run_read_only(argv: tuple[str, ...]) -> CommandResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env={**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"},
        )
    except FileNotFoundError:
        return CommandResult(argv, 127, "", "", "command-not-found")
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = _bounded_text(exc.stdout or b"")
        stderr, stderr_truncated = _bounded_text(exc.stderr or b"")
        return CommandResult(
            argv,
            124,
            DOCTOR.redact(stdout),
            DOCTOR.redact(stderr),
            "timeout",
            round((time.monotonic() - started) * 1000),
            stdout_truncated,
            stderr_truncated,
        )
    stdout, stdout_truncated = _bounded_text(completed.stdout)
    stderr, stderr_truncated = _bounded_text(completed.stderr)
    return CommandResult(
        argv,
        completed.returncode,
        DOCTOR.redact(stdout),
        DOCTOR.redact(stderr),
        None,
        round((time.monotonic() - started) * 1000),
        stdout_truncated,
        stderr_truncated,
    )


def command_by_prefix(
    results: Iterable[CommandResult], prefix: tuple[str, ...]
) -> CommandResult | None:
    for result in results:
        if result.argv[: len(prefix)] == prefix:
            return result
    return None


def command_record(result: CommandResult) -> dict[str, Any]:
    return {
        "argv": list(result.argv),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
    }


def parse_services(result: CommandResult | None) -> dict[str, str]:
    names = ("pipewire", "pipewire-pulse", "wireplumber", "mopidy", "easyeffects")
    lines = result.stdout.splitlines() if result else []
    return {
        name: lines[index].strip() if index < len(lines) and lines[index].strip() else "unknown"
        for index, name in enumerate(names)
    }


def parse_systemd_show(result: CommandResult | None) -> dict[str, dict[str, Any]]:
    if result is None:
        return {}
    units: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] = {}
    for line in [*result.stdout.splitlines(), ""]:
        if not line:
            unit_id = current.get("Id")
            if isinstance(unit_id, str) and unit_id:
                units[unit_id] = dict(sorted(current.items()))
            current = {}
        elif "=" in line:
            key, value = line.split("=", 1)
            if key in {"NRestarts", "MemoryCurrent", "TasksCurrent", "LimitNOFILE"}:
                current[key] = int(value) if value.isdigit() else None
            else:
                current[key] = value or None
    return dict(sorted(units.items()))


def classify_process(command: str, arguments: str) -> str | None:
    executable = pathlib.Path(command).name.casefold()
    for classification, commands in PROCESS_COMMANDS.items():
        if executable in commands:
            return classification
    try:
        tokens = shlex.split(arguments)
    except ValueError:
        tokens = arguments.split()
    script_names = [pathlib.Path(token).name for token in tokens[:4] if not token.startswith("-")]
    if any(CREATIVE_NAME.search(name) for name in script_names):
        return "creative-runtime"
    return None


def parse_processes(result: CommandResult | None) -> list[dict[str, Any]]:
    if result is None:
        return []
    records: list[dict[str, Any]] = []
    for raw in result.stdout.splitlines()[1:]:
        parts = raw.strip().split(None, 7)
        if len(parts) < 8:
            continue
        pid, ppid, state, elapsed, cpu, memory, command, arguments = parts
        classification = classify_process(command, arguments)
        if classification is None:
            continue
        records.append(
            {
                "classification": classification,
                "pid": int(pid) if pid.isdigit() else None,
                "ppid": int(ppid) if ppid.isdigit() else None,
                "state": state,
                "elapsed_seconds": int(elapsed) if elapsed.isdigit() else None,
                "cpu_percent": float(cpu) if re.fullmatch(r"\d+(?:\.\d+)?", cpu) else None,
                "memory_percent": (
                    float(memory)
                    if re.fullmatch(r"\d+(?:\.\d+)?", memory)
                    else None
                ),
                "command": command,
                "arguments": DOCTOR.redact(arguments)[:1000],
            }
        )
    return records[:200]


def process_fingerprint(processes: list[dict[str, Any]]) -> str:
    stable = sorted(
        {
            (
                str(item.get("classification")),
                str(item.get("command")),
                re.sub(r"\b\d+\b", "<n>", str(item.get("arguments", ""))),
            )
            for item in processes
        }
    )
    return sha256_json(stable)


def parse_df(result: CommandResult | None) -> dict[str, int | None]:
    values: dict[str, int | None] = {
        "total_bytes": None,
        "used_bytes": None,
        "available_bytes": None,
        "used_percent": None,
    }
    if result is None or len(result.stdout.splitlines()) < 2:
        return values
    parts = result.stdout.splitlines()[-1].split()
    if len(parts) < 5:
        return values
    values.update(
        {
            "total_bytes": int(parts[1]) if parts[1].isdigit() else None,
            "used_bytes": int(parts[2]) if parts[2].isdigit() else None,
            "available_bytes": int(parts[3]) if parts[3].isdigit() else None,
        }
    )
    match = re.fullmatch(r"(\d+)%", parts[4])
    values["used_percent"] = int(match.group(1)) if match else None
    return values


def bounded_tree_usage(path: pathlib.Path, limit: int = MAX_TREE_ENTRIES) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": DOCTOR.redact(str(path)),
        "present": path.exists(),
        "bytes": 0,
        "files": 0,
        "directories": 0,
        "truncated": False,
        "errors": 0,
    }
    if not path.exists() or path.is_symlink():
        return result
    stack = [path]
    inspected = 0
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            result["errors"] += 1
            continue
        for entry in entries:
            inspected += 1
            if inspected > limit:
                result["truncated"] = True
                return result
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    result["directories"] += 1
                    stack.append(pathlib.Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    result["files"] += 1
                    result["bytes"] += entry.stat(follow_symlinks=False).st_size
            except OSError:
                result["errors"] += 1
    return result


def version_projection(results: Iterable[CommandResult]) -> dict[str, str | None]:
    projection: dict[str, str | None] = {}
    for prefix, label in (
        (("uname", "-r"), "kernel"),
        (("pipewire", "--version"), "pipewire"),
        (("wireplumber", "--version"), "wireplumber"),
        (("mopidy", "--version"), "mopidy"),
    ):
        result = command_by_prefix(results, prefix)
        lines = (result.stdout if result else "").strip().splitlines()
        projection[label] = "; ".join(lines[:3])[:400] if lines else None
    return projection


def doctor_graph_core(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "hardware": report.get("hardware"),
        "graph": report.get("graph"),
        "external_endpoints": report.get("external_endpoints"),
        "profiles": report.get("profiles"),
    }


def graph_fingerprint(report: dict[str, Any]) -> str:
    return sha256_json(doctor_graph_core(report))


def physical_projection(state_path: pathlib.Path) -> dict[str, Any]:
    state = PHYSICAL.read_state(state_path)
    status = PHYSICAL.status_payload(state, state_path)
    return {
        "state_sha256": sha256_json(state),
        "catalog_sha256": state.get("catalog_sha256"),
        "template_sha256": state.get("template_sha256"),
        "resolved_count": status["resolved_count"],
        "total_count": status["total_count"],
        "complete": status["complete"],
        "unresolved": status["unresolved"],
        "authority": "explicit-human-observation-only",
    }


def contract_projection() -> dict[str, Any]:
    bindings = {name: file_binding(path) for name, path in CONTRACT_PATHS.items()}
    return {"bindings": bindings, "aggregate_sha256": sha256_json(bindings)}


def _gate(
    status: str,
    detail: str,
    *,
    blockers: list[str] | None = None,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "detail": detail,
        "blockers": blockers or [],
        "evidence": evidence or [],
    }


def build_gate_status(
    doctor: dict[str, Any],
    physical: dict[str, Any],
    runtime: dict[str, Any],
    contracts: dict[str, Any],
) -> dict[str, Any]:
    unresolved = set(physical["unresolved"])
    safe_required = {
        "motu_output_to_lake_people",
        "lake_people_gain_setting",
        "lake_people_volume_reference",
        "focal_connected_output",
        "pioneer_pc_connection",
        "pioneer_selected_input",
        "pioneer_listening_mode",
        "pioneer_reference_volume",
    }
    voice_required = {
        "rode_nt1a_connected",
        "rode_nt1a_motu_input",
        "motu_phantom_48v",
        "motu_input_gain_reference",
    }
    runtime_unhealthy = [
        item["command"]
        for item in runtime["command_health"]
        if not item["available"] or item["returncode"] not in {0, 3, 4}
    ]
    return {
        "single-truth-model": _gate(
            "pass",
            "Contracts, doctor and private physical-state digest are bound in one report.",
            evidence=[
                contracts["aggregate_sha256"],
                graph_fingerprint(doctor),
                physical["state_sha256"],
            ],
        ),
        "host-read-boundary": _gate(
            "pass",
            "Every command is argv-only and mutation-denylist checked.",
            evidence=[sha256_json([list(command) for command in READ_ONLY_COMMANDS])],
        ),
        "safe-listening-calibration": _gate(
            "blocked" if unresolved & safe_required else "measurement-required",
            "Physical chain facts and a low-level calibration are required.",
            blockers=sorted(unresolved & safe_required)
            or ["safe-listening calibration evidence absent"],
        ),
        "voice-reference": _gate(
            "blocked" if unresolved & voice_required else "measurement-required",
            "Rode/MOTU facts and a real loudest-performance level recording are required.",
            blockers=sorted(unresolved & voice_required)
            or ["voice-level evidence absent"],
        ),
        "latency-xrun-baseline": _gate(
            "measurement-required",
            "Buffer period is observable; physical round-trip and bounded XRun evidence are not inferred.",
            blockers=[
                "physical loopback latency evidence absent",
                "bounded xrun-stability evidence absent",
            ],
            evidence=[runtime["graph_fingerprint"]],
        ),
        "device-loss-baseline": _gate(
            "exercise-required",
            "Current presence is observed; unplug/replug and identity change require a later exercise.",
            blockers=["MOTU loss/recovery exercise absent", "Roland loss/recovery exercise absent"],
        ),
        "runtime-storage-observation": _gate(
            "pass" if not runtime_unhealthy else "degraded",
            "Services, relevant processes, recent logs, limits and bounded state usage are observed.",
            blockers=runtime_unhealthy,
            evidence=[runtime["process_fingerprint"]],
        ),
        "drift-contract": _gate(
            "pass",
            "Stable report digests support field-level comparison.",
            evidence=[runtime["graph_fingerprint"], contracts["aggregate_sha256"]],
        ),
    }


def build_runtime_projection(results: list[CommandResult], doctor: dict[str, Any]) -> dict[str, Any]:
    processes = parse_processes(command_by_prefix(results, ("ps", "-eo")))
    log_result = command_by_prefix(results, ("journalctl", "--user", "-u"))
    log_text = log_result.stdout if log_result else ""
    state_home = pathlib.Path(
        os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state")
    )
    cache_home = pathlib.Path(
        os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")
    )
    disk_result = command_by_prefix(results, ("journalctl", "--user", "--disk-usage"))
    return {
        "graph_fingerprint": graph_fingerprint(doctor),
        "services": parse_services(
            command_by_prefix(results, ("systemctl", "--user", "is-active"))
        ),
        "service_limits": parse_systemd_show(
            command_by_prefix(results, ("systemctl", "--user", "show"))
        ),
        "processes": processes,
        "process_fingerprint": process_fingerprint(processes),
        "filesystem": parse_df(command_by_prefix(results, ("df", "-B1"))),
        "bounded_state_usage": [
            bounded_tree_usage(state_home / "audio"),
            bounded_tree_usage(state_home / "hauski-audio"),
            bounded_tree_usage(cache_home / "audio"),
        ],
        "journal": {
            "audio_lines": len(log_text.splitlines()),
            "xrun_like_lines": len(XRUN_PATTERN.findall(log_text)),
            "disk_usage": disk_result.stdout.strip() if disk_result else None,
        },
        "versions": version_projection(results),
        "command_health": [
            {
                "command": shlex.join(result.argv),
                "available": result.error is None,
                "returncode": result.returncode,
                "timed_out": result.error == "timeout",
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
            }
            for result in results
        ],
    }


def compute_truth_chain(
    contracts: dict[str, Any], runtime: dict[str, Any], physical: dict[str, Any]
) -> str:
    return sha256_json(
        {
            "contracts": contracts["aggregate_sha256"],
            "doctor": runtime["graph_fingerprint"],
            "physical": physical["state_sha256"],
            "runtime": sha256_json(
                {
                    "services": runtime["services"],
                    "service_limits": runtime["service_limits"],
                    "process_fingerprint": runtime["process_fingerprint"],
                    "versions": runtime["versions"],
                }
            ),
        }
    )


def report_digest_core(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in report.items()
        if key not in {"generated_at", "report_sha256"}
    }


def build_report(
    doctor_results: Iterable[Any] | None = None,
    runtime_results: list[CommandResult] | None = None,
    *,
    physical_state: pathlib.Path = PHYSICAL.DEFAULT_STATE,
    generated_at: str | None = None,
) -> dict[str, Any]:
    assert_read_only_commands()
    if doctor_results is None:
        doctor_results = [DOCTOR.run_read_only(command) for command in DOCTOR.READ_ONLY_COMMANDS]
    doctor_report = DOCTOR.build_report(doctor_results, DOCTOR.read_eld_text())
    runtime_results = runtime_results or [run_read_only(command) for command in READ_ONLY_COMMANDS]
    physical = physical_projection(physical_state)
    contracts = contract_projection()
    runtime = build_runtime_projection(runtime_results, doctor_report)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_system_truth_report",
        "generated_at": generated_at or dt.datetime.now(dt.timezone.utc).isoformat(),
        "read_only_contract": True,
        "contracts": contracts,
        "doctor": doctor_report,
        "physical": physical,
        "runtime": runtime,
        "gates": build_gate_status(doctor_report, physical, runtime, contracts),
        "commands": [command_record(result) for result in runtime_results],
        "does_not_establish": [
            "physical controls not explicitly observed",
            "safe listening level without calibration",
            "voice gain or phantom state without human observation",
            "round-trip latency from buffer duration",
            "xrun-free operation without bounded testing",
            "bit-perfect Qobuz playback",
            "profile apply or runtime mutation authority",
        ],
    }
    report["truth_chain_sha256"] = compute_truth_chain(contracts, runtime, physical)
    report["report_sha256"] = sha256_json(report_digest_core(report))
    return report


def verify_report(report: dict[str, Any]) -> None:
    if report.get("schema_version") != SCHEMA_VERSION or report.get("kind") != "audio_system_truth_report":
        raise ValueError("truth report schema or kind is unsupported")
    truth_chain = report.get("truth_chain_sha256")
    if not isinstance(truth_chain, str) or not re.fullmatch(r"[0-9a-f]{64}", truth_chain):
        raise ValueError("truth chain digest is missing or invalid")
    expected_chain = compute_truth_chain(
        report.get("contracts", {}), report.get("runtime", {}), report.get("physical", {})
    )
    if truth_chain != expected_chain:
        raise ValueError("truth chain digest mismatch")
    expected = report.get("report_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("truth report digest is missing or invalid")
    if sha256_json(report_digest_core(report)) != expected:
        raise ValueError("truth report digest mismatch")


def _value(report: dict[str, Any], *path: str) -> Any:
    current: Any = report
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def build_drift_report(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    verify_report(before)
    verify_report(after)
    fields = {
        "contract_aggregate": ("contracts", "aggregate_sha256"),
        "physical_state": ("physical", "state_sha256"),
        "graph_fingerprint": ("runtime", "graph_fingerprint"),
        "motu_present": ("doctor", "hardware", "motu_m2"),
        "roland_present": ("doctor", "hardware", "roland_fp_30x"),
        "default_sink": ("doctor", "graph", "default_sink"),
        "default_source": ("doctor", "graph", "default_source"),
        "rate_hz": ("doctor", "graph", "force_rate_hz"),
        "quantum_frames": ("doctor", "graph", "force_quantum_frames"),
        "services": ("runtime", "services"),
        "versions": ("runtime", "versions"),
        "process_fingerprint": ("runtime", "process_fingerprint"),
    }
    changes = [
        {"field": name, "before": _value(before, *path), "after": _value(after, *path)}
        for name, path in fields.items()
        if _value(before, *path) != _value(after, *path)
    ]
    changed_fields = {item["field"] for item in changes}
    material_fields = set(fields) - {"process_fingerprint"}
    remeasure: list[str] = []
    if changed_fields & {"rate_hz", "quantum_frames", "graph_fingerprint", "versions"}:
        remeasure.extend(["loopback-latency", "xrun-stability"])
    if changed_fields & {"default_sink", "motu_present", "physical_state"}:
        remeasure.append("safe-listening-calibration")
    if changed_fields & {"default_source", "motu_present", "physical_state"}:
        remeasure.append("voice-level")
    return {
        "schema_version": 1,
        "kind": "audio_system_truth_drift_report",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "before_report_sha256": before["report_sha256"],
        "after_report_sha256": after["report_sha256"],
        "changed": bool(changes),
        "material": bool(changed_fields & material_fields),
        "changes": changes,
        "required_remeasurements": sorted(set(remeasure)),
        "does_not_establish": [
            "root cause for drift",
            "safe automatic profile application",
            "physical topology change without human observation",
        ],
    }


def atomic_write_private(path: pathlib.Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("truth report output must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = pathlib.Path(handle.name)
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_report(path: pathlib.Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"truth report is not a regular file: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("truth report root must be an object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--output", type=pathlib.Path, required=True)
    capture.add_argument("--physical-state", type=pathlib.Path, default=PHYSICAL.DEFAULT_STATE)
    compare = sub.add_parser("compare")
    compare.add_argument("before", type=pathlib.Path)
    compare.add_argument("after", type=pathlib.Path)
    compare.add_argument("--output", type=pathlib.Path)
    verify = sub.add_parser("verify")
    verify.add_argument("report", type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.command == "capture":
        report = build_report(physical_state=args.physical_state)
        atomic_write_private(args.output, report)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_system_truth_capture_receipt",
                    "output": str(args.output),
                    "report_sha256": report["report_sha256"],
                    "truth_chain_sha256": report["truth_chain_sha256"],
                    "gates": {
                        name: gate["status"] for name, gate in report["gates"].items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "verify":
        report = load_report(args.report)
        verify_report(report)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_system_truth_verification_receipt",
                    "valid": True,
                    "report_sha256": report["report_sha256"],
                    "truth_chain_sha256": report["truth_chain_sha256"],
                },
                indent=2,
            )
        )
        return 0
    before = load_report(args.before)
    after = load_report(args.after)
    drift = build_drift_report(before, after)
    if args.output:
        atomic_write_private(args.output, drift)
    print(json.dumps(drift, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
