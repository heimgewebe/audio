#!/usr/bin/env python3
"""Validate and store private laboratory-gate evidence without audio effects."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import re
import stat
import sys
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "inventory" / "laboratory-gates.v1.json"
PROFILE_PATH = ROOT / "profiles" / "audio-profiles.v1.json"
PHYSICAL_SCRIPT = ROOT / "scripts" / "physical_verification.py"
PLUGIN_HOST_OBSERVER_PATH = ROOT / "scripts" / "plugin_host_observer.py"
SYSTEM_TRUTH_PATH = ROOT / "scripts" / "system_truth.py"
MAX_EVIDENCE_BYTES = 131_072
MAX_STATE_BYTES = 524_288
DEFAULT_STATE = pathlib.Path(
    os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state")
) / "audio" / "laboratory" / "gates.v1.json"
MAX_XRUN_JOURNAL_LINES = 5_000
XRUN_JOURNAL_UNITS = (
    "pipewire",
    "pipewire-pulse",
    "wireplumber",
    "mopidy",
    "easyeffects",
)
MAX_PLUGIN_HOST_JOURNAL_LINES = 5_000
MAX_PLUGIN_HOST_MEMORY_BYTES = 2_147_483_648
MAX_PLUGIN_HOST_TASKS = 512
MAX_PLUGIN_HOST_NOFILE = 262_144
PLUGIN_HOST_EXECUTABLES = frozenset(
    {"carla", "easyeffects", "sfizz", "sfizz_jack", "fluidsynth", "qsynth"}
)
SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PHYSICAL = load_module("physical_verification_for_laboratory", PHYSICAL_SCRIPT)


def load_json(path: pathlib.Path, *, maximum_bytes: int | None = None) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"JSON path must not be a symbolic link: {path}")
    if maximum_bytes is not None and path.stat().st_size > maximum_bytes:
        raise ValueError(f"JSON file exceeds {maximum_bytes} bytes: {path}")
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def canonical_value_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def operational_profile_catalog_sha256(
    path: pathlib.Path = PROFILE_PATH,
) -> str:
    payload = load_json(path)
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError("audio profile catalog has no profiles object")
    operational = {
        name: profile
        for name, profile in profiles.items()
        if isinstance(profile, dict)
        and profile.get("operational_status", "available") != "planned"
    }
    if len(operational) != sum(isinstance(profile, dict) for profile in profiles.values()):
        non_objects = [
            name for name, profile in profiles.items() if not isinstance(profile, dict)
        ]
        if non_objects:
            raise ValueError(
                "audio profile catalog contains non-object profiles: "
                + ", ".join(non_objects)
            )
    normalized = dict(payload)
    normalized["profiles"] = operational
    encoded = (json.dumps(normalized, ensure_ascii=False, indent=2) + "\n").encode()
    return hashlib.sha256(encoded).hexdigest()


GRAPH_CONTEXT_FIELDS = (
    "default_sink",
    "default_source",
    "force_rate_hz",
    "force_quantum_frames",
)


def graph_context(graph: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(graph, dict):
        raise ValueError("audio graph context must be an object")
    return {field: graph.get(field) for field in GRAPH_CONTEXT_FIELDS}


def graph_fingerprint(graph: dict[str, Any]) -> str:
    return canonical_sha256(graph_context(graph))


def parse_timestamp(raw: Any, label: str) -> dt.datetime:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} is missing")
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid ISO timestamp") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def journal_timestamp(value: dt.datetime, *, round_up: bool) -> str:
    normalized = value.astimezone(dt.timezone.utc)
    if round_up and normalized.microsecond:
        normalized += dt.timedelta(seconds=1)
    normalized = normalized.replace(microsecond=0)
    return normalized.strftime("%Y-%m-%d %H:%M:%S UTC")


def xrun_journal_argv(started_at: str, ended_at: str) -> tuple[str, ...]:
    started = parse_timestamp(started_at, "XRun observation start")
    ended = parse_timestamp(ended_at, "XRun observation end")
    if ended <= started:
        raise ValueError("XRun observation end must follow the start")
    journal_start = journal_timestamp(started, round_up=False)
    journal_end = journal_timestamp(ended, round_up=True)
    argv: list[str] = ["journalctl", "--user"]
    for unit in XRUN_JOURNAL_UNITS:
        argv.extend(("-u", unit))
    argv.extend(
        (
            "--since",
            journal_start,
            "--until",
            journal_end,
            "--no-pager",
            "--output=cat",
            "-n",
            str(MAX_XRUN_JOURNAL_LINES + 1),
        )
    )
    return tuple(argv)


def plugin_host_journal_argv(
    units: list[str], started_at: str, ended_at: str
) -> tuple[str, ...]:
    started = parse_timestamp(started_at, "plugin-host observation start")
    ended = parse_timestamp(ended_at, "plugin-host observation end")
    if ended <= started:
        raise ValueError("plugin-host observation end must follow the start")
    normalized_units = sorted(set(units))
    if not normalized_units or len(normalized_units) > 16:
        raise ValueError("plugin-host journal query requires 1 to 16 units")
    for unit in normalized_units:
        if SERVICE_RE.fullmatch(unit) is None:
            raise ValueError("plugin-host journal unit is invalid")
    journal_start = journal_timestamp(started, round_up=False)
    journal_end = journal_timestamp(ended, round_up=True)
    argv: list[str] = ["journalctl", "--user"]
    for unit in normalized_units:
        argv.extend(("-u", unit))
    argv.extend(
        (
            "--since",
            journal_start,
            "--until",
            journal_end,
            "--no-pager",
            "--output=cat",
            "-n",
            str(MAX_PLUGIN_HOST_JOURNAL_LINES + 1),
        )
    )
    return tuple(argv)


def load_catalog() -> dict[str, dict[str, Any]]:
    payload = load_json(CATALOG_PATH)
    gates = payload.get("gates")
    if not isinstance(gates, dict):
        raise ValueError("laboratory gate catalog has no gates object")
    return gates


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "audio_laboratory_gate_state",
        "updated_at": None,
        "catalog_sha256": sha256_file(CATALOG_PATH),
        "profile_catalog_sha256": operational_profile_catalog_sha256(),
        "gates": {},
        "does_not_establish": [
            "profile-apply-authority",
            "physical-safety",
            "automatic-playback",
        ],
    }


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _sha256(value: Any, label: str) -> str:
    result = _bounded_text(value, label, 64, 64)
    if re.fullmatch(r"[0-9a-f]{64}", result) is None:
        raise ValueError(f"{label} must be a lowercase hexadecimal SHA-256")
    return result


def _bounded_text(value: Any, label: str, minimum: int = 1, maximum: int = 1000) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    result = value.strip()
    if len(result) < minimum or len(result) > maximum:
        raise ValueError(f"{label} must contain {minimum} to {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise ValueError(f"{label} contains control characters")
    return result


def validate_common(gate: str, evidence: dict[str, Any], spec: dict[str, Any]) -> None:
    if evidence.get("schema_version") != 1:
        raise ValueError("evidence has the wrong schema version")
    if evidence.get("kind") != spec.get("evidence_kind"):
        raise ValueError(f"evidence kind does not match gate: {gate}")
    if evidence.get("gate") != gate:
        raise ValueError(f"evidence gate binding does not match: {gate}")
    if evidence.get("result") != "pass":
        raise ValueError("only passing evidence can resolve a gate")
    parse_timestamp(evidence.get("measured_at"), "evidence measured_at")


def validate_voice_level(evidence: dict[str, Any]) -> None:
    analysis = evidence.get("analysis")
    if not isinstance(analysis, dict) or analysis.get("kind") != "audio_level_analysis":
        raise ValueError("voice evidence has no audio-level analysis")
    _positive_int(analysis.get("sample_rate_hz"), "sample_rate_hz")
    peak = _number(analysis.get("maximum_peak_dbfs"), "maximum_peak_dbfs")
    if peak < -12.0 or peak > -6.0:
        raise ValueError("voice peak is outside -12 to -6 dBFS")
    channels = analysis.get("channels_analysis")
    if not isinstance(channels, list) or not channels:
        raise ValueError("voice analysis has no channel results")
    for item in channels:
        if not isinstance(item, dict) or item.get("clipped_samples") != 0:
            raise ValueError("voice analysis contains clipped samples")
    source = evidence.get("source_wav")
    if not isinstance(source, dict):
        raise ValueError("voice evidence has no source WAV binding")
    _sha256(source.get("sha256"), "source WAV SHA-256")
    _positive_int(source.get("bytes"), "source WAV bytes")


def validate_loopback_latency(evidence: dict[str, Any]) -> None:
    analysis = evidence.get("analysis")
    if not isinstance(analysis, dict) or analysis.get("kind") != "audio_loopback_latency_result":
        raise ValueError("loopback evidence has no latency analysis")
    latency = _number(analysis.get("round_trip_latency_ms"), "round_trip_latency_ms")
    confidence = _number(
        analysis.get("peak_detection_confidence"), "peak_detection_confidence"
    )
    snr = _number(analysis.get("peak_snr_db"), "peak_snr_db")
    if latency <= 0 or latency > 500:
        raise ValueError("round-trip latency must be positive and at most 500 ms")
    sample_rate = _positive_int(analysis.get("sample_rate_hz"), "sample_rate_hz")
    delay_samples = _positive_int(analysis.get("delay_samples"), "delay_samples")
    expected_latency = round(delay_samples / sample_rate * 1000, 3)
    if abs(latency - expected_latency) > 0.0005:
        raise ValueError("round-trip latency contradicts delay samples and sample rate")
    _positive_int(evidence.get("quantum_frames"), "quantum_frames")
    _sha256(evidence.get("graph_fingerprint"), "graph_fingerprint")
    if confidence < 0.8:
        raise ValueError("loopback detection confidence is below 0.8")
    if snr < 20:
        raise ValueError("loopback peak SNR is below 20 dB")
    source_hashes: dict[str, str] = {}
    for key in ("reference_wav", "recorded_wav"):
        source = evidence.get(key)
        if not isinstance(source, dict):
            raise ValueError(f"loopback evidence has no {key} binding")
        source_hashes[key] = _sha256(source.get("sha256"), f"{key} SHA-256")
        _positive_int(source.get("bytes"), f"{key} bytes")
    if source_hashes["reference_wav"] == source_hashes["recorded_wav"]:
        raise ValueError("reference and recorded WAV must contain different bytes")


def has_bound_xrun_observation(evidence: dict[str, Any]) -> bool:
    return all(
        key in evidence
        for key in (
            "requested_duration_seconds",
            "observation_started_at",
            "observation_ended_at",
            "graph_before",
            "graph_after",
            "journal",
        )
    )


def validate_xrun_observation(evidence: dict[str, Any]) -> None:
    duration = _number(evidence.get("duration_seconds"), "duration_seconds")
    if duration < 60 or duration > 86_400:
        raise ValueError("XRun observation must cover 60 to 86400 seconds")
    xrun_delta = _nonnegative_int(evidence.get("xrun_delta"), "xrun_delta")
    if xrun_delta != 0:
        raise ValueError("XRun observation contains new XRuns")
    rate_hz = _positive_int(evidence.get("rate_hz"), "rate_hz")
    quantum_frames = _positive_int(evidence.get("quantum_frames"), "quantum_frames")
    graph_fingerprint_value = _sha256(
        evidence.get("graph_fingerprint"), "graph_fingerprint"
    )
    if not has_bound_xrun_observation(evidence):
        return

    requested = _positive_int(
        evidence.get("requested_duration_seconds"), "requested_duration_seconds"
    )
    if requested < 60 or requested > 86_400:
        raise ValueError("requested XRun duration must cover 60 to 86400 seconds")
    if duration < requested:
        raise ValueError("XRun observation is shorter than the requested duration")
    started = parse_timestamp(
        evidence.get("observation_started_at"), "XRun observation start"
    )
    ended = parse_timestamp(
        evidence.get("observation_ended_at"), "XRun observation end"
    )
    observed_duration = (ended - started).total_seconds()
    if observed_duration < requested:
        raise ValueError("XRun timestamps are shorter than the requested duration")
    if abs(observed_duration - duration) > 2.0:
        raise ValueError("XRun duration contradicts the observation timestamps")
    measured = parse_timestamp(evidence.get("measured_at"), "evidence measured_at")
    if abs((measured - ended).total_seconds()) > 2.0:
        raise ValueError("XRun measured_at does not match the observation end")

    for name in ("graph_before", "graph_after"):
        binding = evidence.get(name)
        if not isinstance(binding, dict):
            raise ValueError(f"XRun evidence has no {name} binding")
        _sha256(binding.get("report_sha256"), f"{name} report SHA-256")
        _sha256(binding.get("truth_chain_sha256"), f"{name} truth-chain SHA-256")
        if (
            _sha256(binding.get("graph_fingerprint"), f"{name} graph fingerprint")
            != graph_fingerprint_value
        ):
            raise ValueError(f"{name} graph fingerprint contradicts the observation")
        if _positive_int(binding.get("rate_hz"), f"{name} rate_hz") != rate_hz:
            raise ValueError(f"{name} rate contradicts the observation")
        if (
            _positive_int(binding.get("quantum_frames"), f"{name} quantum_frames")
            != quantum_frames
        ):
            raise ValueError(f"{name} quantum contradicts the observation")

    journal = evidence.get("journal")
    if not isinstance(journal, dict):
        raise ValueError("XRun evidence has no journal binding")
    if journal.get("source") != "journalctl-user-audio-units":
        raise ValueError("XRun evidence has an unknown journal source")
    expected_argv = list(
        xrun_journal_argv(
            evidence["observation_started_at"], evidence["observation_ended_at"]
        )
    )
    if journal.get("query_argv") != expected_argv:
        raise ValueError("XRun journal query does not match the observation window")
    if (
        _sha256(journal.get("query_argv_sha256"), "XRun query SHA-256")
        != canonical_value_sha256(expected_argv)
    ):
        raise ValueError("XRun journal query digest mismatch")
    if journal.get("returncode") != 0:
        raise ValueError("XRun journal query did not complete successfully")
    if journal.get("stdout_truncated") is not False:
        raise ValueError("XRun journal output is truncated")
    if journal.get("complete") is not True:
        raise ValueError("XRun journal observation is incomplete")
    max_lines = _positive_int(journal.get("max_lines"), "XRun journal max_lines")
    if max_lines != MAX_XRUN_JOURNAL_LINES:
        raise ValueError("XRun journal line limit does not match the contract")
    line_count = _nonnegative_int(journal.get("line_count"), "XRun journal line_count")
    if line_count > max_lines:
        raise ValueError("XRun journal observation exceeds the line limit")
    _nonnegative_int(journal.get("stdout_total_bytes"), "XRun journal bytes")
    _sha256(journal.get("stdout_sha256"), "XRun journal output SHA-256")
    xrun_line_count = _nonnegative_int(
        journal.get("xrun_line_count"), "XRun journal xrun_line_count"
    )
    if xrun_line_count != xrun_delta:
        raise ValueError("XRun journal count contradicts xrun_delta")
    xrun_lines_sha256 = _sha256(
        journal.get("xrun_lines_sha256"), "XRun line-set SHA-256"
    )
    if xrun_delta == 0 and xrun_lines_sha256 != canonical_value_sha256([]):
        raise ValueError("zero-XRun evidence has a non-empty XRun line digest")


def validate_policy_decision(evidence: dict[str, Any]) -> None:
    _bounded_text(evidence.get("decision"), "decision", 2, 120)
    _bounded_text(evidence.get("justification"), "justification", 10, 1000)


def validate_qobuz_rate(evidence: dict[str, Any]) -> None:
    _positive_int(evidence.get("track_rate_hz"), "track_rate_hz")
    _sha256(evidence.get("track_fingerprint"), "track_fingerprint")
    _positive_int(evidence.get("graph_rate_hz"), "graph_rate_hz")
    _positive_int(evidence.get("endpoint_rate_hz"), "endpoint_rate_hz")
    resampling = evidence.get("resampling_observed")
    if not isinstance(resampling, bool):
        raise ValueError("resampling_observed must be boolean")
    if resampling:
        raise ValueError("Qobuz rate proof observed resampling")
    rates = {
        evidence["track_rate_hz"],
        evidence["graph_rate_hz"],
        evidence["endpoint_rate_hz"],
    }
    if len(rates) != 1:
        raise ValueError("Qobuz track, graph and endpoint rates do not match")
    _bounded_text(evidence.get("method"), "method", 5, 500)
    _sha256(evidence.get("graph_fingerprint"), "graph_fingerprint")


def has_bound_plugin_host_observation(evidence: dict[str, Any]) -> bool:
    return all(
        key in evidence
        for key in (
            "requested_duration_seconds",
            "observation_started_at",
            "observation_ended_at",
            "bounded_resources",
            "blockers",
            "implementation",
            "truth_before",
            "truth_after",
            "processes_before",
            "processes_after",
            "journal",
        )
    )


def _validate_plugin_service(service: Any, unit: str, label: str) -> None:
    if not isinstance(service, dict):
        raise ValueError(f"plugin-host {label} has no service projection")
    if service.get("unit") != unit:
        raise ValueError(f"plugin-host {label} service identity mismatch")
    if service.get("load_state") != "loaded":
        raise ValueError(f"plugin-host {label} service is not loaded")
    if service.get("active_state") != "active" or service.get("sub_state") != "running":
        raise ValueError(f"plugin-host {label} service is not running")
    _sha256(service.get("control_group_sha256"), f"{label} control-group SHA-256")
    memory_max = _positive_int(service.get("memory_max_bytes"), f"{label} MemoryMax")
    if memory_max > MAX_PLUGIN_HOST_MEMORY_BYTES:
        raise ValueError(f"plugin-host {label} MemoryMax exceeds the contract")
    memory_current = _nonnegative_int(
        service.get("memory_current_bytes"), f"{label} MemoryCurrent"
    )
    if memory_current > memory_max:
        raise ValueError(f"plugin-host {label} MemoryCurrent exceeds MemoryMax")
    tasks_max = _positive_int(service.get("tasks_max"), f"{label} TasksMax")
    if tasks_max > MAX_PLUGIN_HOST_TASKS:
        raise ValueError(f"plugin-host {label} TasksMax exceeds the contract")
    tasks_current = _nonnegative_int(
        service.get("tasks_current"), f"{label} TasksCurrent"
    )
    if tasks_current > tasks_max:
        raise ValueError(f"plugin-host {label} TasksCurrent exceeds TasksMax")
    nofile = _positive_int(service.get("limit_nofile"), f"{label} LimitNOFILE")
    if nofile > MAX_PLUGIN_HOST_NOFILE:
        raise ValueError(f"plugin-host {label} LimitNOFILE exceeds the contract")
    if service.get("standard_output") not in {"journal", "journal-or-kmsg"}:
        raise ValueError(f"plugin-host {label} stdout is not journal-bound")
    if service.get("standard_error") not in {"inherit", "journal", "journal-or-kmsg"}:
        raise ValueError(f"plugin-host {label} stderr is not journal-bound")
    _positive_int(
        service.get("log_rate_limit_interval_usec"),
        f"{label} LogRateLimitIntervalUSec",
    )
    _positive_int(service.get("log_rate_limit_burst"), f"{label} LogRateLimitBurst")
    _nonnegative_int(service.get("restart_count"), f"{label} NRestarts")
    _sha256(service.get("query_argv_sha256"), f"{label} query argv SHA-256")
    _sha256(service.get("query_stdout_sha256"), f"{label} query output SHA-256")


def _validate_plugin_process(record: Any, label: str) -> tuple[Any, ...]:
    if not isinstance(record, dict):
        raise ValueError(f"plugin-host {label} process is not an object")
    executable = _bounded_text(record.get("executable"), f"{label} executable", 1, 64)
    if executable not in PLUGIN_HOST_EXECUTABLES:
        raise ValueError(f"plugin-host {label} executable is not catalogued")
    pid = _positive_int(record.get("pid"), f"{label} pid")
    _nonnegative_int(record.get("ppid"), f"{label} ppid")
    _nonnegative_int(record.get("elapsed_seconds"), f"{label} elapsed_seconds")
    start_ticks = _positive_int(
        record.get("process_start_ticks"), f"{label} process_start_ticks"
    )
    command_sha = _sha256(record.get("command_sha256"), f"{label} command SHA-256")
    unit = _bounded_text(record.get("unit"), f"{label} unit", 9, 255)
    if SERVICE_RE.fullmatch(unit) is None:
        raise ValueError(f"plugin-host {label} unit is invalid")
    cgroup = _bounded_text(record.get("cgroup"), f"{label} cgroup", 2, 4096)
    if not cgroup.endswith("/" + unit):
        raise ValueError(f"plugin-host {label} cgroup does not end in its unit")
    _sha256(record.get("cgroup_sha256"), f"{label} cgroup SHA-256")
    service = record.get("service")
    _validate_plugin_service(service, unit, label)
    if service.get("control_group") != cgroup:
        raise ValueError(f"plugin-host {label} service cgroup mismatch")
    return (pid, start_ticks, executable, unit, command_sha)


def validate_managed_plugin_host(evidence: dict[str, Any]) -> None:
    if evidence.get("managed_process") is not True:
        raise ValueError("plugin host is not managed")
    if evidence.get("bounded_logs") is not True:
        raise ValueError("plugin host logs are not bounded")
    if evidence.get("standalone_sfizz_jack") is not False:
        raise ValueError("standalone sfizz/JACK operation remains enabled")
    duration = _number(evidence.get("runtime_seconds"), "runtime_seconds")
    if duration < 60 or duration > 86_400:
        raise ValueError("plugin-host validation must cover 60 to 86400 seconds")
    if not has_bound_plugin_host_observation(evidence):
        return
    if evidence.get("bounded_resources") is not True:
        raise ValueError("plugin host resources are not bounded")
    requested = _positive_int(
        evidence.get("requested_duration_seconds"), "requested_duration_seconds"
    )
    if requested < 60 or requested > 86_400 or duration < requested:
        raise ValueError("plugin-host observation is shorter than requested")
    started = parse_timestamp(
        evidence.get("observation_started_at"), "plugin-host observation start"
    )
    ended = parse_timestamp(
        evidence.get("observation_ended_at"), "plugin-host observation end"
    )
    if (ended - started).total_seconds() < requested:
        raise ValueError("plugin-host timestamps are shorter than requested")
    if abs((ended - started).total_seconds() - duration) > 2.0:
        raise ValueError("plugin-host duration contradicts its timestamps")
    measured = parse_timestamp(evidence.get("measured_at"), "evidence measured_at")
    if abs((measured - ended).total_seconds()) > 2.0:
        raise ValueError("plugin-host measured_at does not match the observation end")
    if evidence.get("blockers") != []:
        raise ValueError("plugin-host passing evidence contains blockers")
    implementation = evidence.get("implementation")
    if not isinstance(implementation, dict):
        raise ValueError("plugin-host evidence has no implementation binding")
    expected_implementation = {
        "plugin_host_observer_sha256": sha256_file(PLUGIN_HOST_OBSERVER_PATH),
        "laboratory_gate_sha256": sha256_file(pathlib.Path(__file__)),
        "system_truth_sha256": sha256_file(SYSTEM_TRUTH_PATH),
    }
    for key, expected_sha256 in expected_implementation.items():
        observed_sha256 = _sha256(
            implementation.get(key), f"plugin-host {key}"
        )
        if observed_sha256 != expected_sha256:
            raise ValueError(f"plugin-host implementation binding changed: {key}")
    for label in ("truth_before", "truth_after"):
        binding = evidence.get(label)
        if not isinstance(binding, dict):
            raise ValueError(f"plugin-host evidence has no {label} truth binding")
        _sha256(binding.get("report_sha256"), f"{label} report SHA-256")
        _sha256(binding.get("truth_chain_sha256"), f"{label} truth-chain SHA-256")
        _sha256(binding.get("process_fingerprint"), f"{label} process fingerprint")
    before = evidence.get("processes_before")
    after = evidence.get("processes_after")
    if not isinstance(before, list) or not isinstance(after, list) or not before:
        raise ValueError("plugin-host observation has no process set")
    process_count = _positive_int(evidence.get("process_count"), "process_count")
    if len(before) != process_count or len(after) != process_count:
        raise ValueError("plugin-host process count contradicts the process sets")
    before_ids = [
        _validate_plugin_process(record, f"before[{index}]")
        for index, record in enumerate(before)
    ]
    after_ids = [
        _validate_plugin_process(record, f"after[{index}]")
        for index, record in enumerate(after)
    ]
    if before_ids != after_ids:
        raise ValueError("plugin-host process identity changed during observation")
    if [record["service"]["restart_count"] for record in before] != [
        record["service"]["restart_count"] for record in after
    ]:
        raise ValueError("plugin-host restart count changed during observation")
    journal = evidence.get("journal")
    if not isinstance(journal, dict):
        raise ValueError("plugin-host evidence has no journal binding")
    units = sorted({record["unit"] for record in before})
    if journal.get("source") != "journalctl-user-plugin-host-units":
        raise ValueError("plugin-host evidence has an unknown journal source")
    if journal.get("units") != units:
        raise ValueError("plugin-host journal units contradict the process set")
    expected_argv = list(
        plugin_host_journal_argv(
            units,
            evidence["observation_started_at"],
            evidence["observation_ended_at"],
        )
    )
    if journal.get("query_argv") != expected_argv:
        raise ValueError("plugin-host journal query does not match the observation")
    if (
        _sha256(journal.get("query_argv_sha256"), "plugin-host query SHA-256")
        != canonical_value_sha256(expected_argv)
    ):
        raise ValueError("plugin-host journal query digest mismatch")
    if journal.get("returncode") != 0 or journal.get("complete") is not True:
        raise ValueError("plugin-host journal query is incomplete")
    if journal.get("stdout_truncated") is not False:
        raise ValueError("plugin-host journal output is truncated")
    max_lines = _positive_int(journal.get("max_lines"), "plugin-host max_lines")
    if max_lines != MAX_PLUGIN_HOST_JOURNAL_LINES:
        raise ValueError("plugin-host journal line limit does not match the contract")
    line_count = _nonnegative_int(journal.get("line_count"), "plugin-host line_count")
    if line_count > max_lines:
        raise ValueError("plugin-host journal exceeds the line limit")
    _nonnegative_int(journal.get("stdout_total_bytes"), "plugin-host output bytes")
    _sha256(journal.get("stdout_sha256"), "plugin-host output SHA-256")


VALIDATORS = {
    "voice_level": validate_voice_level,
    "loopback_latency": validate_loopback_latency,
    "xrun_observation": validate_xrun_observation,
    "policy_decision": validate_policy_decision,
    "qobuz_rate": validate_qobuz_rate,
    "managed_plugin_host": validate_managed_plugin_host,
}


def validate_evidence(
    gate: str,
    evidence: dict[str, Any],
    *,
    allow_legacy_xrun: bool = False,
    allow_legacy_plugin_host: bool = False,
) -> dict[str, Any]:
    catalog = load_catalog()
    if gate not in catalog:
        raise ValueError(f"unknown laboratory gate: {gate}")
    spec = catalog[gate]
    validate_common(gate, evidence, spec)
    validator_name = spec.get("validator")
    validator = VALIDATORS.get(validator_name)
    if validator is None:
        raise ValueError(f"unknown laboratory validator: {validator_name}")
    validator(evidence)
    if gate == "xrun-stability-test" and not has_bound_xrun_observation(evidence):
        if not allow_legacy_xrun:
            raise ValueError("legacy XRun evidence lacks a bounded journal observation")
    if gate == "managed-plugin-host-proof" and not has_bound_plugin_host_observation(
        evidence
    ):
        if not allow_legacy_plugin_host:
            raise ValueError("legacy plugin-host evidence lacks a bounded observation")
    return spec


def current_physical_sha256(path: pathlib.Path) -> str:
    if not path.exists():
        raise ValueError("physical state file does not exist")
    PHYSICAL.read_state(path)
    return sha256_file(path)


def validate_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    if state.get("schema_version") != 1 or state.get("kind") != "audio_laboratory_gate_state":
        raise ValueError("laboratory gate state has the wrong schema or kind")
    if state.get("catalog_sha256") != sha256_file(CATALOG_PATH):
        raise ValueError("laboratory gate catalog changed; review existing evidence")
    if state.get("profile_catalog_sha256") != operational_profile_catalog_sha256():
        raise ValueError("audio profile catalog changed; review existing evidence")
    gates = state.get("gates")
    if not isinstance(gates, dict):
        raise ValueError("laboratory gate state has no gates object")
    catalog = load_catalog()
    unknown = sorted(set(gates) - set(catalog))
    if unknown:
        raise ValueError(f"laboratory gate state contains unknown gates: {', '.join(unknown)}")
    recorded_times: list[dt.datetime] = []
    for gate, receipt in gates.items():
        if not isinstance(receipt, dict):
            raise ValueError(f"laboratory receipt is not an object: {gate}")
        if receipt.get("status") != "passed":
            raise ValueError(f"laboratory receipt is not passed: {gate}")
        evidence = receipt.get("evidence")
        if not isinstance(evidence, dict):
            raise ValueError(f"laboratory receipt has no evidence: {gate}")
        validate_evidence(
            gate,
            evidence,
            allow_legacy_xrun=True,
            allow_legacy_plugin_host=True,
        )
        if receipt.get("evidence_sha256") != canonical_sha256(evidence):
            raise ValueError(f"laboratory evidence digest mismatch: {gate}")
        recorded_times.append(
            parse_timestamp(receipt.get("recorded_at"), f"recorded_at: {gate}")
        )
        expected_binding = catalog[gate].get("binds_physical_state") is True
        binding = receipt.get("physical_state_sha256")
        if expected_binding:
            _sha256(binding, f"physical-state binding: {gate}")
        elif binding is not None:
            raise ValueError(f"unbound gate unexpectedly stores physical-state binding: {gate}")
    updated_at = state.get("updated_at")
    if updated_at is None:
        if recorded_times:
            raise ValueError("laboratory state has receipts but no updated_at")
    else:
        updated = parse_timestamp(updated_at, "laboratory updated_at")
        if recorded_times and updated < max(recorded_times):
            raise ValueError("laboratory updated_at predates a receipt")
    if path.is_symlink():
        raise ValueError("laboratory gate state must not be a symbolic link")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("laboratory gate state must have mode 0600")


def read_state(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    if path.is_symlink():
        raise ValueError("laboratory gate state must not be a symbolic link")
    if path.stat().st_size > MAX_STATE_BYTES:
        raise ValueError(f"laboratory gate state exceeds {MAX_STATE_BYTES} bytes")
    state = load_json(path, maximum_bytes=MAX_STATE_BYTES)
    validate_state(path, state)
    return state


def atomic_write_private(path: pathlib.Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("laboratory gate state must not be a symbolic link")
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        path.parent.chmod(0o700)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if len(encoded) > MAX_STATE_BYTES:
        raise ValueError(f"laboratory gate state exceeds {MAX_STATE_BYTES} bytes")
    temp: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temp = pathlib.Path(handle.name)
        temp.chmod(0o600)
        temp.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def record_gate(
    state: dict[str, Any],
    gate: str,
    evidence: dict[str, Any],
    physical_state_path: pathlib.Path,
    *,
    replace: bool = False,
) -> None:
    spec = validate_evidence(gate, evidence)
    if gate in state.get("gates", {}) and not replace:
        raise ValueError(f"laboratory gate already exists; use --replace: {gate}")
    physical_sha: str | None = None
    if spec.get("binds_physical_state") is True:
        physical_sha = current_physical_sha256(physical_state_path)
        if evidence.get("physical_state_sha256") != physical_sha:
            raise ValueError("evidence does not match the current physical state")
    elif evidence.get("physical_state_sha256") is not None:
        raise ValueError("this gate must not contain a physical-state binding")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    state.setdefault("gates", {})[gate] = {
        "status": "passed",
        "recorded_at": now,
        "evidence_sha256": canonical_sha256(evidence),
        "physical_state_sha256": physical_sha,
        "evidence": evidence,
    }
    state["updated_at"] = now


def gate_resolution(
    state: dict[str, Any], physical_state_path: pathlib.Path
) -> tuple[set[str], dict[str, str]]:
    resolved: set[str] = set()
    invalidated: dict[str, str] = {}
    catalog = load_catalog()
    physical_sha: str | None = None
    for gate, receipt in state.get("gates", {}).items():
        if gate == "xrun-stability-test":
            evidence = receipt.get("evidence")
            if not isinstance(evidence, dict) or not has_bound_xrun_observation(evidence):
                invalidated[gate] = "legacy-unbound-xrun-evidence"
                continue
        if gate == "managed-plugin-host-proof":
            evidence = receipt.get("evidence")
            if not isinstance(evidence, dict) or not has_bound_plugin_host_observation(
                evidence
            ):
                invalidated[gate] = "legacy-unbound-plugin-host-evidence"
                continue
        if catalog[gate].get("binds_physical_state") is True:
            if physical_sha is None:
                if not physical_state_path.exists():
                    invalidated[gate] = "physical-state-missing"
                    continue
                physical_sha = current_physical_sha256(physical_state_path)
            if receipt.get("physical_state_sha256") != physical_sha:
                invalidated[gate] = "physical-state-changed"
                continue
        resolved.add(gate)
    return resolved, invalidated


def status_payload(
    state: dict[str, Any], state_path: pathlib.Path, physical_state_path: pathlib.Path
) -> dict[str, Any]:
    catalog = load_catalog()
    resolved, invalidated = gate_resolution(state, physical_state_path)
    return {
        "schema_version": 1,
        "kind": "audio_laboratory_gate_status",
        "state_path": str(state_path),
        "resolved": sorted(resolved),
        "invalidated": invalidated,
        "unresolved": sorted(set(catalog) - resolved),
        "recorded_count": len(state.get("gates", {})),
        "resolved_count": len(resolved),
        "total_count": len(catalog),
        "complete": len(resolved) == len(catalog),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--physical-state", type=pathlib.Path, default=PHYSICAL.DEFAULT_STATE
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("init")
    record = sub.add_parser("record")
    record.add_argument("gate", choices=sorted(load_catalog()))
    record.add_argument("evidence", type=pathlib.Path)
    record.add_argument("--replace", action="store_true")
    clear = sub.add_parser("clear")
    clear.add_argument("gate", choices=sorted(load_catalog()))
    args = parser.parse_args()

    if args.command == "init":
        if args.state.exists():
            raise ValueError(f"laboratory gate state already exists: {args.state}")
        state = empty_state()
        atomic_write_private(args.state, state)
    else:
        state = read_state(args.state)
        if args.command == "record":
            evidence = load_json(args.evidence, maximum_bytes=MAX_EVIDENCE_BYTES)
            record_gate(
                state,
                args.gate,
                evidence,
                args.physical_state,
                replace=args.replace,
            )
            atomic_write_private(args.state, state)
        elif args.command == "clear":
            state.get("gates", {}).pop(args.gate, None)
            state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            atomic_write_private(args.state, state)
    print(
        json.dumps(
            status_payload(state, args.state, args.physical_state),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
