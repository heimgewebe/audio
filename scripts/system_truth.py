#!/usr/bin/env python3
"""Build and compare one bounded, read-only Heim-PC audio truth report.

The report is locally integrity-checked, but not signed. Authenticity requires an
external pin of the capture receipt's report digest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import secrets
import selectors
import signal
import shlex
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
MAX_COMMAND_BYTES = 262_144
MAX_REPORT_BYTES = 4_194_304
COMMAND_TIMEOUT_SECONDS = 12.0
POST_KILL_DRAIN_SECONDS = 0.5
MAX_TREE_ENTRIES = 5_000
MAX_TREE_SECONDS = 2.0
READ_CHUNK_BYTES = 65_536


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
LABORATORY = load_module(
    "laboratory_gate_for_system_truth", ROOT / "scripts/laboratory_gate.py"
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
CREATIVE_EXECUTABLE = re.compile(
    r"^(?:whale|buckelwal|dauersong|animal)(?:[-_.][a-z0-9_-]+)?(?:\.py|\.sh)?$",
    re.I,
)
XRUN_PATTERN = re.compile(r"\b(xrun|underrun|overrun|dropout)\b", re.I)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    error: str | None = None
    duration_ms: int = 0
    stdout_total_bytes: int = 0
    stderr_total_bytes: int = 0
    stdout_sha256: str = ""
    stderr_sha256: str = ""
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


def expected_command_argvs() -> list[tuple[str, ...]]:
    return [*DOCTOR.READ_ONLY_COMMANDS, *READ_ONLY_COMMANDS]


def allowed_returncodes(argv: tuple[str, ...]) -> frozenset[int]:
    if argv[:3] == ("systemctl", "--user", "is-active"):
        return frozenset({0, 3, 4})
    if argv[:3] == ("systemctl", "--user", "show"):
        # A missing optional EasyEffects unit may make the aggregate show return 1
        # while the required units are still fully observed in stdout.
        return frozenset({0, 1})
    return frozenset({0})


def _empty_digest() -> str:
    return hashlib.sha256(b"").hexdigest()


def run_read_only(argv: tuple[str, ...]) -> CommandResult:
    """Run one command with bounded in-memory capture and full-stream digests."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
            env={**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"},
        )
    except FileNotFoundError:
        return CommandResult(
            argv,
            127,
            "",
            "",
            "command-not-found",
            stdout_sha256=_empty_digest(),
            stderr_sha256=_empty_digest(),
        )

    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    streams = {
        "stdout": {
            "file": process.stdout,
            "buffer": bytearray(),
            "total": 0,
            "digest": hashlib.sha256(),
            "truncated": False,
        },
        "stderr": {
            "file": process.stderr,
            "buffer": bytearray(),
            "total": 0,
            "digest": hashlib.sha256(),
            "truncated": False,
        },
    }
    for name, stream in streams.items():
        selector.register(stream["file"], selectors.EVENT_READ, name)

    deadline = started + COMMAND_TIMEOUT_SECONDS
    drain_deadline: float | None = None
    error: str | None = None

    def kill_process_group() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            try:
                process.kill()
            except ProcessLookupError:
                pass

    while selector.get_map():
        now = time.monotonic()
        if error is None and now >= deadline:
            error = "timeout"
            kill_process_group()
            drain_deadline = now + POST_KILL_DRAIN_SECONDS
        if drain_deadline is not None and now >= drain_deadline:
            for key in list(selector.get_map().values()):
                try:
                    selector.unregister(key.fileobj)
                except KeyError:
                    pass
                key.fileobj.close()
            break
        wait_until = drain_deadline if drain_deadline is not None else deadline
        events = selector.select(timeout=max(0.0, min(0.2, wait_until - now)))
        if not events and process.poll() is not None:
            events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
        for key, _ in events:
            name = str(key.data)
            stream = streams[name]
            try:
                chunk = os.read(key.fd, READ_CHUNK_BYTES)
            except OSError:
                chunk = b""
            if not chunk:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            stream["total"] += len(chunk)
            stream["digest"].update(chunk)
            available = MAX_COMMAND_BYTES - len(stream["buffer"])
            if available > 0:
                stream["buffer"].extend(chunk[:available])
            if len(chunk) > available:
                stream["truncated"] = True
    selector.close()
    try:
        returncode = process.wait(timeout=POST_KILL_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        kill_process_group()
        try:
            returncode = process.wait(timeout=POST_KILL_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            returncode = 124
            error = "timeout"

    def text(name: str) -> str:
        # Raw output remains process-local and bounded. Persisted projections either
        # normalize it or store only digests; pre-parse redaction can erase device identity.
        return bytes(streams[name]["buffer"]).decode("utf-8", "replace")

    return CommandResult(
        argv=argv,
        returncode=124 if error == "timeout" else returncode,
        stdout=text("stdout"),
        stderr=text("stderr"),
        error=error,
        duration_ms=round((time.monotonic() - started) * 1000),
        stdout_total_bytes=int(streams["stdout"]["total"]),
        stderr_total_bytes=int(streams["stderr"]["total"]),
        stdout_sha256=streams["stdout"]["digest"].hexdigest(),
        stderr_sha256=streams["stderr"]["digest"].hexdigest(),
        stdout_truncated=bool(streams["stdout"]["truncated"]),
        stderr_truncated=bool(streams["stderr"]["truncated"]),
    )


def command_by_prefix(
    results: Iterable[CommandResult], prefix: tuple[str, ...]
) -> CommandResult | None:
    for result in results:
        if result.argv[: len(prefix)] == prefix:
            return result
    return None


def command_record(result: CommandResult) -> dict[str, Any]:
    """Return command evidence without persisting raw stdout, stderr or arguments."""
    return {
        "argv": list(result.argv),
        "command": shlex.join(result.argv),
        "returncode": result.returncode,
        "allowed_returncodes": sorted(allowed_returncodes(result.argv)),
        "accepted": (
            result.error is None
            and result.returncode in allowed_returncodes(result.argv)
            and not result.stdout_truncated
            and not result.stderr_truncated
        ),
        "error": result.error,
        "duration_ms": result.duration_ms,
        "stdout_total_bytes": result.stdout_total_bytes,
        "stderr_total_bytes": result.stderr_total_bytes,
        "stdout_sha256": result.stdout_sha256 or sha256_bytes(result.stdout.encode()),
        "stderr_sha256": result.stderr_sha256 or sha256_bytes(result.stderr.encode()),
        "stdout_line_count": len(result.stdout.splitlines()),
        "stderr_line_count": len(result.stderr.splitlines()),
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
    }


def doctor_inputs_from_capture(results: list[CommandResult]) -> list[Any]:
    return [
        DOCTOR.CommandResult(
            result.argv,
            result.returncode,
            result.stdout,
            result.stderr,
            result.error,
        )
        for result in results
    ]


def capture_from_doctor_results(results: Iterable[Any]) -> list[CommandResult]:
    captured: list[CommandResult] = []
    for result in results:
        stdout = str(result.stdout)
        stderr = str(result.stderr)
        captured.append(
            CommandResult(
                argv=tuple(result.argv),
                returncode=int(result.returncode),
                stdout=stdout,
                stderr=stderr,
                error=result.error,
                stdout_total_bytes=len(stdout.encode("utf-8")),
                stderr_total_bytes=len(stderr.encode("utf-8")),
                stdout_sha256=sha256_bytes(stdout.encode("utf-8")),
                stderr_sha256=sha256_bytes(stderr.encode("utf-8")),
            )
        )
    return captured


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


REQUIRED_SERVICE_STATES = (
    "pipewire",
    "pipewire-pulse",
    "wireplumber",
    "mopidy",
    "easyeffects",
)
REQUIRED_SERVICE_UNITS = tuple(f"{name}.service" for name in REQUIRED_SERVICE_STATES)


def runtime_observation_completeness(runtime: dict[str, Any]) -> dict[str, Any]:
    services = runtime.get("services") if isinstance(runtime.get("services"), dict) else {}
    limits = (
        runtime.get("service_limits")
        if isinstance(runtime.get("service_limits"), dict)
        else {}
    )
    command_health = (
        runtime.get("command_health")
        if isinstance(runtime.get("command_health"), list)
        else []
    )
    observed_commands = {
        tuple(item.get("argv", [])): item
        for item in command_health
        if isinstance(item, dict) and isinstance(item.get("argv"), list)
    }
    missing_states = sorted(
        name for name in REQUIRED_SERVICE_STATES if services.get(name) == "unknown"
    )
    required_limit_fields = (
        "Id",
        "LoadState",
        "ActiveState",
        "SubState",
        "NRestarts",
        "LimitNOFILE",
    )
    missing_limits = sorted(
        f"{unit}:{field}"
        for unit in REQUIRED_SERVICE_UNITS
        for field in required_limit_fields
        if unit not in limits
        or field not in limits[unit]
        or limits[unit].get(field) is None
        or (
            field == "LimitNOFILE"
            and (
                type(limits[unit].get(field)) is not int
                or limits[unit].get(field, 0) <= 0
            )
        )
    )
    missing_commands = sorted(
        shlex.join(command)
        for command in READ_ONLY_COMMANDS
        if command not in observed_commands
    )
    rejected_commands = sorted(
        shlex.join(command)
        for command, item in observed_commands.items()
        if command in READ_ONLY_COMMANDS and item.get("accepted") is not True
    )
    return {
        "complete": not (
            missing_states or missing_limits or missing_commands or rejected_commands
        ),
        "missing_service_states": missing_states,
        "missing_service_limits": missing_limits,
        "missing_commands": missing_commands,
        "rejected_commands": rejected_commands,
    }


def classify_process(command: str, arguments: str) -> str | None:
    executable = pathlib.Path(command).name.casefold()
    for classification, commands in PROCESS_COMMANDS.items():
        if executable in commands:
            return classification
    if CREATIVE_EXECUTABLE.fullmatch(executable):
        return "creative-runtime"
    try:
        tokens = shlex.split(arguments)
    except ValueError:
        tokens = arguments.split()
    for token in tokens[1:5]:
        name = pathlib.Path(token).name
        if pathlib.Path(name).suffix.casefold() in {".py", ".sh"} and CREATIVE_EXECUTABLE.fullmatch(name):
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
        redacted_arguments = DOCTOR.redact(arguments)
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
                "command_sha256": sha256_bytes(
                    DOCTOR.redact(command).encode("utf-8")
                ),
                "arguments_sha256": sha256_bytes(redacted_arguments.encode("utf-8")),
            }
        )
    return records


def process_fingerprint(processes: list[dict[str, Any]]) -> str:
    stable = sorted(
        [
            (
                str(item.get("classification")),
                str(item.get("command_sha256")),
                str(item.get("arguments_sha256")),
            )
            for item in processes
        ]
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


def absolute_without_resolution(path: pathlib.Path) -> pathlib.Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = pathlib.Path.cwd() / expanded
    if any(part in {"", ".", ".."} for part in expanded.parts[1:]):
        raise ValueError(f"unsafe path component: {path}")
    return expanded


def open_directory_chain(path: pathlib.Path, *, create: bool = False) -> tuple[pathlib.Path, int]:
    absolute = absolute_without_resolution(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return absolute, descriptor
    except Exception:
        os.close(descriptor)
        raise


def bounded_tree_usage(path: pathlib.Path, limit: int = MAX_TREE_ENTRIES) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path_sha256": sha256_bytes(str(absolute_without_resolution(path)).encode()),
        "present": False,
        "bytes": 0,
        "files": 0,
        "directories": 0,
        "truncated": False,
        "time_exhausted": False,
        "errors": 0,
    }
    try:
        _, root_fd = open_directory_chain(path)
    except FileNotFoundError:
        return result
    except OSError:
        result["errors"] = 1
        return result
    result["present"] = True
    stack = [root_fd]
    inspected = 0
    deadline = time.monotonic() + MAX_TREE_SECONDS
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        while stack:
            descriptor = stack.pop()
            try:
                with os.scandir(descriptor) as iterator:
                    for entry in iterator:
                        if time.monotonic() >= deadline:
                            result["time_exhausted"] = True
                            return result
                        inspected += 1
                        if inspected > limit:
                            result["truncated"] = True
                            return result
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                child = os.open(entry.name, flags, dir_fd=descriptor)
                                stack.append(child)
                                result["directories"] += 1
                            elif entry.is_file(follow_symlinks=False):
                                result["files"] += 1
                                result["bytes"] += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            result["errors"] += 1
            finally:
                os.close(descriptor)
    finally:
        for descriptor in stack:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return result


def version_projection(results: Iterable[CommandResult]) -> dict[str, dict[str, Any]]:
    projection: dict[str, dict[str, Any]] = {}
    for prefix, label in (
        (("uname", "-r"), "kernel"),
        (("pipewire", "--version"), "pipewire"),
        (("wireplumber", "--version"), "wireplumber"),
        (("mopidy", "--version"), "mopidy"),
    ):
        result = command_by_prefix(results, prefix)
        if result is None:
            projection[label] = {
                "available": False,
                "output_sha256": _empty_digest(),
                "line_count": 0,
            }
            continue
        evidence = command_record(result)
        projection[label] = {
            "available": evidence["accepted"],
            "output_sha256": evidence["stdout_sha256"],
            "line_count": evidence["stdout_line_count"],
        }
    return projection


def graph_fingerprint(report: dict[str, Any]) -> str:
    graph = report.get("graph") if isinstance(report.get("graph"), dict) else report
    return LABORATORY.graph_fingerprint(graph)


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


def normalize_qobuz_context(
    track_fingerprint: str | None, track_rate_hz: int | None
) -> dict[str, Any] | None:
    if track_fingerprint is None and track_rate_hz is None:
        return None
    if track_fingerprint is None or track_rate_hz is None:
        raise ValueError(
            "Qobuz current-track context requires both fingerprint and rate"
        )
    validate_sha(track_fingerprint, "Qobuz track fingerprint")
    if isinstance(track_rate_hz, bool) or not isinstance(track_rate_hz, int) or track_rate_hz <= 0:
        raise ValueError("Qobuz track rate must be a positive integer")
    return {
        "track_fingerprint": track_fingerprint,
        "track_rate_hz": track_rate_hz,
    }


def laboratory_receipt_binding(gate: str, evidence: dict[str, Any]) -> dict[str, Any]:
    fields_by_gate = {
        "loopback-latency-measurement": (
            "quantum_frames",
            "graph_fingerprint",
        ),
        "xrun-stability-test": (
            "rate_hz",
            "quantum_frames",
            "graph_fingerprint",
        ),
        "qobuz-rate-proof": (
            "track_fingerprint",
            "track_rate_hz",
            "graph_rate_hz",
            "endpoint_rate_hz",
            "resampling_observed",
            "graph_fingerprint",
        ),
    }
    return {
        field: evidence.get(field) for field in fields_by_gate.get(gate, ())
    }


def laboratory_projection(
    state_path: pathlib.Path,
    physical_state_path: pathlib.Path,
    graph: dict[str, Any],
    qobuz_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = LABORATORY.read_state(state_path)
    status = LABORATORY.status_payload(state, state_path, physical_state_path)
    current_graph_fingerprint = LABORATORY.graph_fingerprint(graph)
    resolved = set(status["resolved"])
    incompatible: dict[str, str] = {}
    for gate in sorted(resolved):
        receipt = state.get("gates", {}).get(gate, {})
        evidence = receipt.get("evidence", {}) if isinstance(receipt, dict) else {}
        if not isinstance(evidence, dict):
            incompatible[gate] = "evidence-missing"
            continue
        bound_graph = evidence.get("graph_fingerprint")
        if bound_graph is not None and bound_graph != current_graph_fingerprint:
            incompatible[gate] = "graph-fingerprint-changed"
            continue
        if gate in {"xrun-stability-test", "qobuz-rate-proof"}:
            evidence_rate = evidence.get(
                "rate_hz" if gate == "xrun-stability-test" else "graph_rate_hz"
            )
            if evidence_rate != graph.get("force_rate_hz"):
                incompatible[gate] = "graph-rate-changed"
                continue
        if gate in {"xrun-stability-test", "loopback-latency-measurement"}:
            if evidence.get("quantum_frames") != graph.get("force_quantum_frames"):
                incompatible[gate] = "graph-quantum-changed"
                continue
        if gate == "qobuz-rate-proof":
            if qobuz_context is None:
                incompatible[gate] = "current-track-context-missing"
                continue
            if evidence.get("track_fingerprint") != qobuz_context.get(
                "track_fingerprint"
            ):
                incompatible[gate] = "track-fingerprint-changed"
                continue
            if evidence.get("track_rate_hz") != qobuz_context.get("track_rate_hz"):
                incompatible[gate] = "track-rate-changed"
    resolved -= set(incompatible)
    invalidated = dict(status["invalidated"])
    invalidated.update(incompatible)
    catalog = set(LABORATORY.load_catalog())
    receipts = {
        gate: {
            "status": receipt.get("status"),
            "recorded_at": receipt.get("recorded_at"),
            "evidence_sha256": receipt.get("evidence_sha256"),
            "physical_state_sha256": receipt.get("physical_state_sha256"),
            "binding": laboratory_receipt_binding(
                gate,
                receipt.get("evidence", {})
                if isinstance(receipt.get("evidence"), dict)
                else {},
            ),
        }
        for gate, receipt in sorted(state.get("gates", {}).items())
        if isinstance(receipt, dict)
    }
    return {
        "state_sha256": sha256_json(state),
        "catalog_sha256": state.get("catalog_sha256"),
        "profile_catalog_sha256": state.get("profile_catalog_sha256"),
        "resolved": sorted(resolved),
        "invalidated": dict(sorted(invalidated.items())),
        "unresolved": sorted(catalog - resolved),
        "recorded_count": status["recorded_count"],
        "resolved_count": len(resolved),
        "total_count": len(catalog),
        "complete": len(resolved) == len(catalog),
        "current_graph_fingerprint": current_graph_fingerprint,
        "receipts": receipts,
        "authority": "validated-private-laboratory-state",
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


def laboratory_gate_status(
    laboratory: dict[str, Any], gate: str, unresolved_status: str
) -> dict[str, Any]:
    resolved = set(laboratory["resolved"])
    invalidated = laboratory["invalidated"]
    if gate in resolved:
        return _gate(
            "pass",
            f"Validated private laboratory evidence resolves {gate}.",
            evidence=[laboratory["receipts"][gate]["evidence_sha256"]],
        )
    if gate in invalidated:
        return _gate(
            "invalidated",
            f"Stored evidence for {gate} is invalid under current physical truth.",
            blockers=[f"{gate}: {invalidated[gate]}"],
        )
    return _gate(
        unresolved_status,
        f"Validated evidence for {gate} is absent.",
        blockers=[gate],
    )


def build_gate_status(
    doctor: dict[str, Any],
    physical: dict[str, Any],
    laboratory: dict[str, Any],
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
    command_failures = [
        item["command"] for item in runtime["command_health"] if not item["accepted"]
    ]
    tree_failures = [
        item["path_sha256"]
        for item in runtime["bounded_state_usage"]
        if item["errors"] or item["truncated"] or item["time_exhausted"]
    ]
    completeness = runtime_observation_completeness(runtime)
    completeness_failures = [
        f"service-state:{item}" for item in completeness["missing_service_states"]
    ] + [
        f"service-limit:{item}" for item in completeness["missing_service_limits"]
    ] + [
        f"missing-command:{item}" for item in completeness["missing_commands"]
    ] + [
        f"rejected-command:{item}" for item in completeness["rejected_commands"]
    ]
    runtime_failures = (
        command_failures
        + [f"state-tree:{item}" for item in tree_failures]
        + completeness_failures
    )
    voice = laboratory_gate_status(laboratory, "voice-level-measurement", "measurement-required")
    if unresolved & voice_required and voice["status"] != "pass":
        voice = _gate(
            "blocked",
            "Physical Rode/MOTU facts must be explicit before voice measurement.",
            blockers=sorted(unresolved & voice_required),
        )
    loopback = laboratory_gate_status(
        laboratory, "loopback-latency-measurement", "measurement-required"
    )
    xrun = laboratory_gate_status(
        laboratory, "xrun-stability-test", "measurement-required"
    )
    latency_status = "pass" if loopback["status"] == xrun["status"] == "pass" else "measurement-required"
    latency_blockers = loopback["blockers"] + xrun["blockers"]
    if "invalidated" in {loopback["status"], xrun["status"]}:
        latency_status = "invalidated"
    resolved = set(laboratory["resolved"])
    policy_missing = sorted(
        {"rate-policy-decision", "resampling-decision"} - resolved
    )
    return {
        "single-truth-model": _gate(
            "pass",
            "Contracts, Doctor, physical state, laboratory state and runtime observation are bound in one report.",
            evidence=[
                contracts["aggregate_sha256"],
                runtime["graph_fingerprint"],
                physical["state_sha256"],
                laboratory["state_sha256"],
            ],
        ),
        "host-read-boundary": _gate(
            "pass",
            "Every command is argv-only, mutation-denylist checked and memory bounded.",
            evidence=[sha256_json([list(command) for command in READ_ONLY_COMMANDS])],
        ),
        "safe-listening-calibration": _gate(
            "blocked" if unresolved & safe_required else "measurement-required",
            "Physical listening-chain facts and a low-level calibration are required.",
            blockers=sorted(unresolved & safe_required)
            or ["safe-listening-calibration"],
        ),
        "voice-reference": voice,
        "latency-xrun-baseline": _gate(
            latency_status,
            "Both physical loopback latency and bounded XRun evidence are required.",
            blockers=latency_blockers,
            evidence=loopback["evidence"] + xrun["evidence"],
        ),
        "qobuz-rate-proof": laboratory_gate_status(
            laboratory, "qobuz-rate-proof", "measurement-required"
        ),
        "rate-resampling-policy": _gate(
            "pass" if not policy_missing else "decision-required",
            "Rate and Roland-resampling policy decisions must be validated.",
            blockers=policy_missing,
        ),
        "managed-plugin-host-proof": laboratory_gate_status(
            laboratory, "managed-plugin-host-proof", "validation-required"
        ),
        "device-loss-baseline": _gate(
            "exercise-required",
            "Current presence is observed; unplug/replug and identity change require explicit exercises.",
            blockers=["motu-device-loss-exercise", "roland-device-loss-exercise"],
        ),
        "runtime-storage-observation": _gate(
            "pass" if not runtime_failures else "degraded",
            "Services, relevant processes, logs, limits and bounded state usage are observed.",
            blockers=runtime_failures,
            evidence=[runtime["process_fingerprint"]],
        ),
        "drift-contract": _gate(
            "pass",
            "Integrity-checked reports support field-level drift and catalog-ID remeasurement requests.",
            evidence=[runtime["graph_fingerprint"], contracts["aggregate_sha256"]],
        ),
    }


def xrun_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if XRUN_PATTERN.search(line)]


def build_runtime_projection(results: list[CommandResult], doctor: dict[str, Any]) -> dict[str, Any]:
    processes = parse_processes(command_by_prefix(results, ("ps", "-eo")))
    log_result = command_by_prefix(results, ("journalctl", "--user", "-u"))
    matching_xruns = xrun_lines(log_result.stdout if log_result else "")
    state_home = pathlib.Path(
        os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state")
    )
    cache_home = pathlib.Path(
        os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")
    )
    disk_result = command_by_prefix(results, ("journalctl", "--user", "--disk-usage"))
    command_health = [command_record(result) for result in results]
    services = parse_services(
        command_by_prefix(results, ("systemctl", "--user", "is-active"))
    )
    service_limits = parse_systemd_show(
        command_by_prefix(results, ("systemctl", "--user", "show"))
    )
    projection = {
        "graph_fingerprint": graph_fingerprint(doctor),
        "services": services,
        "service_limits": service_limits,
        "processes": processes,
        "process_fingerprint": process_fingerprint(processes),
        "filesystem": parse_df(command_by_prefix(results, ("df", "-B1"))),
        "bounded_state_usage": [
            bounded_tree_usage(state_home / "audio"),
            bounded_tree_usage(state_home / "hauski-audio"),
            bounded_tree_usage(cache_home / "audio"),
        ],
        "journal": {
            "audio_line_count": len((log_result.stdout if log_result else "").splitlines()),
            "audio_output_sha256": (
                log_result.stdout_sha256
                if log_result and log_result.stdout_sha256
                else sha256_bytes((log_result.stdout if log_result else "").encode())
            ),
            "xrun_like_line_count": len(matching_xruns),
            "xrun_like_lines_sha256": sha256_json(matching_xruns),
            "disk_usage_output_sha256": (
                disk_result.stdout_sha256
                if disk_result and disk_result.stdout_sha256
                else sha256_bytes((disk_result.stdout if disk_result else "").encode())
            ),
        },
        "versions": version_projection(results),
        "command_health": command_health,
    }
    projection["observation_completeness"] = runtime_observation_completeness(
        projection
    )
    return projection


def compute_truth_chain(
    contracts: dict[str, Any],
    runtime: dict[str, Any],
    physical: dict[str, Any],
    laboratory: dict[str, Any],
    playback: dict[str, Any],
) -> str:
    return sha256_json(
        {
            "contracts": contracts["aggregate_sha256"],
            "doctor": runtime["graph_fingerprint"],
            "physical": physical["state_sha256"],
            "laboratory": laboratory["state_sha256"],
            "playback": sha256_json(playback),
            "runtime": sha256_json(
                {
                    "services": runtime["services"],
                    "service_limits": runtime["service_limits"],
                    "process_fingerprint": runtime["process_fingerprint"],
                    "journal": runtime["journal"],
                    "versions": runtime["versions"],
                }
            ),
        }
    )


def report_digest_core(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "report_sha256"}


def build_report(
    doctor_results: Iterable[Any] | None = None,
    runtime_results: list[CommandResult] | None = None,
    *,
    physical_state: pathlib.Path = PHYSICAL.DEFAULT_STATE,
    laboratory_state: pathlib.Path = LABORATORY.DEFAULT_STATE,
    qobuz_track_fingerprint: str | None = None,
    qobuz_track_rate_hz: int | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    assert_read_only_commands()
    if doctor_results is None:
        doctor_capture = [run_read_only(command) for command in DOCTOR.READ_ONLY_COMMANDS]
        normalized_doctor_results = doctor_inputs_from_capture(doctor_capture)
    else:
        normalized_doctor_results = list(doctor_results)
        doctor_capture = capture_from_doctor_results(normalized_doctor_results)
    doctor_report = DOCTOR.build_report(
        normalized_doctor_results, DOCTOR.read_eld_text()
    )
    runtime_results = runtime_results or [run_read_only(command) for command in READ_ONLY_COMMANDS]
    physical = physical_projection(physical_state)
    playback = {
        "qobuz_current_track": normalize_qobuz_context(
            qobuz_track_fingerprint, qobuz_track_rate_hz
        )
    }
    laboratory = laboratory_projection(
        laboratory_state,
        physical_state,
        doctor_report.get("graph", {}),
        playback["qobuz_current_track"],
    )
    contracts = contract_projection()
    runtime = build_runtime_projection(runtime_results, doctor_report)
    generated = generated_at or dt.datetime.now(dt.timezone.utc).isoformat()
    LABORATORY.parse_timestamp(generated, "generated_at")
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_system_truth_report",
        "generated_at": generated,
        "read_only_contract": True,
        "contracts": contracts,
        "doctor": doctor_report,
        "physical": physical,
        "laboratory": laboratory,
        "playback": playback,
        "runtime": runtime,
        "gates": build_gate_status(doctor_report, physical, laboratory, runtime, contracts),
        "commands": [
            command_record(result) for result in [*doctor_capture, *runtime_results]
        ],
        "does_not_establish": [
            "physical controls not explicitly observed",
            "safe listening level without calibration",
            "voice gain or phantom state without human observation",
            "round-trip latency from buffer duration",
            "xrun-free operation without validated evidence",
            "bit-perfect Qobuz playback without qobuz-rate-proof",
            "profile apply or runtime mutation authority",
            "authenticity without an externally pinned or signed report digest",
        ],
    }
    report["truth_chain_sha256"] = compute_truth_chain(
        contracts, runtime, physical, laboratory, playback
    )
    report["report_sha256"] = sha256_json(report_digest_core(report))
    return report


def validate_sha(value: Any, label: str) -> None:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is missing or invalid")


def verify_report(report: dict[str, Any]) -> None:
    if (
        report.get("schema_version") != SCHEMA_VERSION
        or report.get("kind") != "audio_system_truth_report"
    ):
        raise ValueError("truth report schema or kind is unsupported")
    LABORATORY.parse_timestamp(report.get("generated_at"), "generated_at")
    contracts = report.get("contracts")
    if not isinstance(contracts, dict) or not isinstance(contracts.get("bindings"), dict):
        raise ValueError("contract bindings are missing")
    if sha256_json(contracts["bindings"]) != contracts.get("aggregate_sha256"):
        raise ValueError("contract aggregate digest mismatch")
    runtime = report.get("runtime")
    doctor = report.get("doctor")
    physical = report.get("physical")
    laboratory = report.get("laboratory")
    playback = report.get("playback")
    if not all(
        isinstance(value, dict)
        for value in (runtime, doctor, physical, laboratory, playback)
    ):
        raise ValueError("truth report projections are incomplete")
    if runtime.get("graph_fingerprint") != graph_fingerprint(doctor):
        raise ValueError("doctor graph fingerprint mismatch")
    if runtime.get("process_fingerprint") != process_fingerprint(runtime.get("processes", [])):
        raise ValueError("process fingerprint mismatch")
    if laboratory.get("current_graph_fingerprint") != runtime.get("graph_fingerprint"):
        raise ValueError("laboratory graph fingerprint mismatch")
    processes = runtime.get("processes")
    if not isinstance(processes, list):
        raise ValueError("runtime process projection is invalid")
    for index, process in enumerate(processes):
        if not isinstance(process, dict):
            raise ValueError(f"process projection {index} is invalid")
        validate_sha(process.get("command_sha256"), f"process {index} command digest")
        validate_sha(process.get("arguments_sha256"), f"process {index} arguments digest")
        if "command" in process or "arguments" in process:
            raise ValueError("raw process identity must not be persisted")
    versions = runtime.get("versions")
    if not isinstance(versions, dict):
        raise ValueError("runtime version projection is invalid")
    for label in ("kernel", "pipewire", "wireplumber", "mopidy"):
        record = versions.get(label)
        if not isinstance(record, dict):
            raise ValueError(f"version projection is missing: {label}")
        validate_sha(record.get("output_sha256"), f"version {label} digest")
        if set(record) != {"available", "output_sha256", "line_count"}:
            raise ValueError(f"version projection fields are invalid: {label}")
        if not isinstance(record.get("available"), bool):
            raise ValueError(f"version availability is invalid: {label}")
        if not isinstance(record.get("line_count"), int) or record["line_count"] < 0:
            raise ValueError(f"version line count is invalid: {label}")
    journal = runtime.get("journal")
    if not isinstance(journal, dict):
        raise ValueError("runtime journal projection is invalid")
    for key in ("audio_output_sha256", "xrun_like_lines_sha256", "disk_usage_output_sha256"):
        validate_sha(journal.get(key), f"journal {key}")
    for label, projection in (("physical", physical), ("laboratory", laboratory)):
        validate_sha(projection.get("state_sha256"), f"{label} state digest")
    resolved = set(laboratory.get("resolved", []))
    unresolved = set(laboratory.get("unresolved", []))
    invalidated = set((laboratory.get("invalidated") or {}).keys())
    if resolved & unresolved or resolved & invalidated:
        raise ValueError("laboratory gate sets overlap")
    if laboratory.get("resolved_count") != len(resolved):
        raise ValueError("laboratory resolved count mismatch")
    current_qobuz = playback.get("qobuz_current_track")
    if current_qobuz is not None:
        current_qobuz = normalize_qobuz_context(
            current_qobuz.get("track_fingerprint"),
            current_qobuz.get("track_rate_hz"),
        )
    if "qobuz-rate-proof" in resolved:
        if current_qobuz is None:
            raise ValueError("resolved Qobuz proof lacks current-track context")
        receipt = laboratory.get("receipts", {}).get("qobuz-rate-proof", {})
        binding = receipt.get("binding") if isinstance(receipt, dict) else None
        if not isinstance(binding, dict):
            raise ValueError("resolved Qobuz proof lacks projected binding")
        if binding.get("track_fingerprint") != current_qobuz["track_fingerprint"]:
            raise ValueError("Qobuz track fingerprint mismatch")
        if binding.get("track_rate_hz") != current_qobuz["track_rate_hz"]:
            raise ValueError("Qobuz track rate mismatch")
        if binding.get("graph_fingerprint") != runtime.get("graph_fingerprint"):
            raise ValueError("Qobuz graph fingerprint mismatch")
        if binding.get("graph_rate_hz") != doctor.get("graph", {}).get(
            "force_rate_hz"
        ):
            raise ValueError("Qobuz graph rate mismatch")
    commands = report.get("commands")
    if not isinstance(commands, list):
        raise ValueError("command evidence is missing")
    expected_argvs = expected_command_argvs()
    observed_argvs = [
        tuple(command.get("argv", [])) if isinstance(command, dict) else ()
        for command in commands
    ]
    if observed_argvs != expected_argvs:
        raise ValueError("command evidence does not match the canonical command vector")
    assert_read_only_commands(observed_argvs)
    for index, command in enumerate(commands):
        if not isinstance(command, dict):
            raise ValueError(f"command evidence {index} is invalid")
        validate_sha(command.get("stdout_sha256"), f"command {index} stdout digest")
        validate_sha(command.get("stderr_sha256"), f"command {index} stderr digest")
        if "stdout" in command or "stderr" in command:
            raise ValueError("raw command output must not be persisted")
        argv = command.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
            raise ValueError(f"command evidence {index} argv is invalid")
        expected_allowed = sorted(allowed_returncodes(tuple(argv)))
        expected_accepted = (
            command.get("error") is None
            and command.get("returncode") in expected_allowed
            and command.get("stdout_truncated") is False
            and command.get("stderr_truncated") is False
        )
        if command.get("allowed_returncodes") != expected_allowed:
            raise ValueError(f"command evidence {index} allowed returncodes mismatch")
        if command.get("accepted") is not expected_accepted:
            raise ValueError(f"command evidence {index} accepted status mismatch")
        if command.get("command") != shlex.join(argv):
            raise ValueError(f"command evidence {index} display mismatch")
    command_by_argv = {
        tuple(command["argv"]): command for command in commands
    }
    version_commands = {
        "kernel": ("uname", "-r"),
        "pipewire": ("pipewire", "--version"),
        "wireplumber": ("wireplumber", "--version"),
        "mopidy": ("mopidy", "--version"),
    }
    for label, argv in version_commands.items():
        evidence = command_by_argv.get(argv)
        if evidence is None:
            raise ValueError(f"version command evidence is missing: {label}")
        expected_version = {
            "available": evidence["accepted"],
            "output_sha256": evidence["stdout_sha256"],
            "line_count": evidence["stdout_line_count"],
        }
        if versions.get(label) != expected_version:
            raise ValueError(
                f"version projection does not match command evidence: {label}"
            )
    expected_runtime_health = commands[-len(READ_ONLY_COMMANDS) :]
    if runtime.get("command_health") != expected_runtime_health:
        raise ValueError("runtime command-health projection mismatch")
    expected_completeness = runtime_observation_completeness(runtime)
    if runtime.get("observation_completeness") != expected_completeness:
        raise ValueError("runtime observation completeness mismatch")
    doctor_health = doctor.get("command_health")
    expected_doctor_health = [
        {
            "command": shlex.join(tuple(item["argv"])),
            "available": item.get("error") is None,
            "returncode": item.get("returncode"),
        }
        for item in commands[: len(DOCTOR.READ_ONLY_COMMANDS)]
    ]
    if doctor_health != expected_doctor_health:
        raise ValueError("Doctor command-health projection mismatch")
    expected_gates = build_gate_status(doctor, physical, laboratory, runtime, contracts)
    if report.get("gates") != expected_gates:
        raise ValueError("gate projection mismatch")
    validate_sha(report.get("truth_chain_sha256"), "truth chain digest")
    expected_chain = compute_truth_chain(
        contracts, runtime, physical, laboratory, playback
    )
    if report["truth_chain_sha256"] != expected_chain:
        raise ValueError("truth chain digest mismatch")
    validate_sha(report.get("report_sha256"), "truth report digest")
    if sha256_json(report_digest_core(report)) != report["report_sha256"]:
        raise ValueError("truth report digest mismatch")


def _value(report: dict[str, Any], *path: str) -> Any:
    current: Any = report
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def required_remeasurements(changed_fields: set[str]) -> list[str]:
    required: set[str] = set()
    catalog = LABORATORY.load_catalog()
    if "contract_aggregate" in changed_fields:
        required.update(catalog)
    if "physical_state" in changed_fields:
        required.update(
            gate
            for gate, spec in catalog.items()
            if spec.get("binds_physical_state") is True
        )
    if changed_fields & {
        "graph_fingerprint",
        "default_sink",
        "default_source",
        "rate_hz",
        "quantum_frames",
    }:
        required.update(
            {
                "loopback-latency-measurement",
                "xrun-stability-test",
                "qobuz-rate-proof",
                "managed-plugin-host-proof",
            }
        )
    if "motu_present" in changed_fields:
        required.update(
            {
                "voice-level-measurement",
                "loopback-latency-measurement",
            }
        )
    if "roland_present" in changed_fields:
        required.update(
            {
                "loopback-latency-measurement",
                "xrun-stability-test",
                "resampling-decision",
            }
        )
    if changed_fields & {
        "services",
        "service_limits",
        "versions",
        "process_fingerprint",
    }:
        required.update(
            {"managed-plugin-host-proof", "xrun-stability-test", "qobuz-rate-proof"}
        )
    if changed_fields & {"xrun_like_line_count", "xrun_like_lines_sha256"}:
        required.add("xrun-stability-test")
    unknown = required - set(catalog)
    if unknown:
        raise ValueError(f"unknown laboratory remeasurement IDs: {sorted(unknown)}")
    return sorted(required)


def required_followups(changed_fields: set[str]) -> list[str]:
    followups: set[str] = set()
    if "physical_state" in changed_fields:
        followups.update(
            {
                "safe-listening-calibration",
                "motu-device-loss-exercise",
                "roland-device-loss-exercise",
            }
        )
    if "motu_present" in changed_fields:
        followups.update(
            {"safe-listening-calibration", "motu-device-loss-exercise"}
        )
    if "roland_present" in changed_fields:
        followups.add("roland-device-loss-exercise")
    return sorted(followups)


def build_drift_report(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    verify_report(before)
    verify_report(after)
    fields = {
        "contract_aggregate": ("contracts", "aggregate_sha256"),
        "physical_state": ("physical", "state_sha256"),
        "laboratory_state": ("laboratory", "state_sha256"),
        "graph_fingerprint": ("runtime", "graph_fingerprint"),
        "motu_present": ("doctor", "hardware", "motu_m2"),
        "roland_present": ("doctor", "hardware", "roland_fp_30x"),
        "default_sink": ("doctor", "graph", "default_sink"),
        "default_source": ("doctor", "graph", "default_source"),
        "rate_hz": ("doctor", "graph", "force_rate_hz"),
        "quantum_frames": ("doctor", "graph", "force_quantum_frames"),
        "services": ("runtime", "services"),
        "service_limits": ("runtime", "service_limits"),
        "versions": ("runtime", "versions"),
        "process_fingerprint": ("runtime", "process_fingerprint"),
        "xrun_like_line_count": ("runtime", "journal", "xrun_like_line_count"),
        "xrun_like_lines_sha256": ("runtime", "journal", "xrun_like_lines_sha256"),
    }
    changes = [
        {"field": name, "before": _value(before, *path), "after": _value(after, *path)}
        for name, path in fields.items()
        if _value(before, *path) != _value(after, *path)
    ]
    changed_fields = {item["field"] for item in changes}
    material_fields = set(fields)
    return {
        "schema_version": 1,
        "kind": "audio_system_truth_drift_report",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "before_report_sha256": before["report_sha256"],
        "after_report_sha256": after["report_sha256"],
        "changed": bool(changes),
        "material": bool(changed_fields & material_fields),
        "changes": changes,
        "required_remeasurements": required_remeasurements(changed_fields),
        "required_followups": required_followups(changed_fields),
        "does_not_establish": [
            "root cause for drift",
            "safe automatic profile application",
            "physical topology change without human observation",
        ],
    }


def secure_read_bytes(path: pathlib.Path, maximum_bytes: int = MAX_REPORT_BYTES) -> bytes:
    absolute = absolute_without_resolution(path)
    _, parent_fd = open_directory_chain(absolute.parent)
    descriptor = -1
    try:
        descriptor = os.open(
            absolute.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("truth report must be a regular file")
        if metadata.st_size > maximum_bytes:
            raise ValueError(f"truth report exceeds {maximum_bytes} bytes")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise ValueError(f"truth report exceeds {maximum_bytes} bytes")
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def atomic_write_private(path: pathlib.Path, payload: dict[str, Any]) -> None:
    absolute = absolute_without_resolution(path)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise ValueError(f"truth report exceeds {MAX_REPORT_BYTES} bytes")
    _, parent_fd = open_directory_chain(absolute.parent, create=True)
    temporary_name = f".{absolute.name}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    try:
        try:
            existing = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError("truth report output must be absent or a regular file")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            absolute.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def load_report(path: pathlib.Path) -> dict[str, Any]:
    payload = json.loads(secure_read_bytes(path).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("truth report root must be an object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--output", type=pathlib.Path, required=True)
    capture.add_argument("--physical-state", type=pathlib.Path, default=PHYSICAL.DEFAULT_STATE)
    capture.add_argument("--laboratory-state", type=pathlib.Path, default=LABORATORY.DEFAULT_STATE)
    capture.add_argument("--qobuz-track-fingerprint")
    capture.add_argument("--qobuz-track-rate-hz", type=int)
    compare = sub.add_parser("compare")
    compare.add_argument("before", type=pathlib.Path)
    compare.add_argument("after", type=pathlib.Path)
    compare.add_argument("--output", type=pathlib.Path)
    verify = sub.add_parser("verify")
    verify.add_argument("report", type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.command == "capture":
        report = build_report(
            physical_state=args.physical_state,
            laboratory_state=args.laboratory_state,
            qobuz_track_fingerprint=args.qobuz_track_fingerprint,
            qobuz_track_rate_hz=args.qobuz_track_rate_hz,
        )
        atomic_write_private(args.output, report)
        absolute = absolute_without_resolution(args.output)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_system_truth_capture_receipt",
                    "output_basename": absolute.name,
                    "output_path_sha256": sha256_bytes(str(absolute).encode()),
                    "report_sha256": report["report_sha256"],
                    "truth_chain_sha256": report["truth_chain_sha256"],
                    "authenticity_requires_external_digest_pin": True,
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
                    "authenticity_requires_external_digest_pin": True,
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
