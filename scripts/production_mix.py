#!/usr/bin/env python3
"""Plan and manage one fail-closed PipeWire production-mix graph."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import resource
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "profiles" / "production-mix-graph.v1.json"
PROFILE_PATH = ROOT / "profiles" / "audio-profiles.v1.json"
RECORDING_PATH = ROOT / "scripts" / "recording_session.py"
RATE_PATH = ROOT / "scripts" / "rate_policy_observer.py"
SYSTEM_TRUTH_PATH = ROOT / "scripts" / "system_truth.py"
WRAPPER_PATH = ROOT / "scripts" / "audio-production-mix"
DEFAULT_STATE_ROOT = (
    pathlib.Path(os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state"))
    / "audio"
    / "production-mix-v1"
)
MAX_JSON_BYTES = 524_288
MAX_BINDING_BYTES = 64_000_000
HEX64_RE = re.compile(r"[0-9a-f]{64}")
SESSION_ID_RE = re.compile(r"[0-9a-f]{24}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SAMPLE_SPEC_RE = re.compile(
    r"^(?P<format>[A-Za-z0-9_-]+) (?P<channels>[0-9]+)ch (?P<rate>[0-9]+)Hz$"
)
SERVICE_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "MainPID",
    "ControlGroup",
    "InvocationID",
    "Environment",
    "ExecStart",
    "ExecMainStatus",
    "NRestarts",
    "MemoryMax",
    "TasksMax",
    "LimitNOFILE",
    "LogRateLimitIntervalUSec",
    "LogRateLimitBurst",
)
MANAGED_MARKER_ENV = "AUDIO_PRODUCTION_MIX_MANAGED"
SPEC_SHA_ENV = "AUDIO_PRODUCTION_MIX_SPEC_SHA256"
PULSE_QUERIES = {
    "sinks": ("pactl", "--format=json", "list", "sinks"),
    "sources": ("pactl", "--format=json", "list", "sources"),
    "sink_inputs": ("pactl", "--format=json", "list", "sink-inputs"),
    "source_outputs": ("pactl", "--format=json", "list", "source-outputs"),
}


class ProductionMixError(RuntimeError):
    """A fail-closed production-mix contract error."""


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ProductionMixError(f"module cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REC = load_module("recording_session_for_production_mix", RECORDING_PATH)
RATE = load_module("rate_policy_for_production_mix", RATE_PATH)
SYSTEM_TRUTH = load_module("system_truth_for_production_mix", SYSTEM_TRUTH_PATH)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProductionMixError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProductionMixError(f"{label} must be a nonnegative integer")
    return value


def _valid_text(value: Any, *, maximum: int = 4096) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= maximum
        and CONTROL_RE.search(value) is None
    )


def _valid_timestamp(value: Any) -> bool:
    if not _valid_text(value, maximum=100):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _normalize_usb_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold().removeprefix("0x")
    return normalized if re.fullmatch(r"[0-9a-f]{4}", normalized) else None


def _json_props(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _read_json_with_binding(
    path: pathlib.Path, *, private: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return REC._safe_json_read_with_binding(
            path, maximum_bytes=MAX_JSON_BYTES, require_private=private
        )
    except (OSError, REC.RecordingError) as exc:
        raise ProductionMixError(str(exc)) from exc


def _read_json(path: pathlib.Path, *, private: bool = False) -> dict[str, Any]:
    value, _binding_value = _read_json_with_binding(path, private=private)
    return value


def _write_private_json(
    path: pathlib.Path, value: dict[str, Any], *, create_only: bool = False
) -> None:
    try:
        REC._atomic_private_json(path, value, create_only=create_only)
    except (OSError, REC.RecordingError) as exc:
        raise ProductionMixError(str(exc)) from exc


def _binding(
    path: pathlib.Path,
    *,
    maximum_bytes: int = MAX_BINDING_BYTES,
    private: bool = False,
) -> dict[str, Any]:
    try:
        return REC._safe_regular_binding(
            path, maximum_bytes=maximum_bytes, require_private=private
        )
    except (OSError, REC.RecordingError) as exc:
        raise ProductionMixError(str(exc)) from exc


def ensure_private_directory(path: pathlib.Path) -> pathlib.Path:
    try:
        return REC.ensure_private_directory(path)
    except (OSError, REC.RecordingError) as exc:
        raise ProductionMixError(str(exc)) from exc


def _session_paths(
    state_root: pathlib.Path, session_id: str
) -> dict[str, pathlib.Path]:
    if not isinstance(session_id, str) or SESSION_ID_RE.fullmatch(session_id) is None:
        raise ProductionMixError("production-mix session id is invalid")
    root = REC.lexical_absolute(state_root)
    return {
        "spec": root / f"{session_id}.spec.json",
        "state": root / f"{session_id}.state.json",
        "ready": root / f"{session_id}.ready.json",
        "result": root / f"{session_id}.result.json",
        "active": root / "active.json",
    }


def load_contract() -> dict[str, Any]:
    value = _read_json(CONTRACT_PATH)
    required = {
        "schema_version",
        "kind",
        "profile",
        "unit",
        "graph",
        "required_physical_facts",
        "required_laboratory_gates",
        "monitor_target",
        "process",
        "rules",
    }
    if (
        set(value) != required
        or value.get("schema_version") != 1
        or value.get("kind") != "audio_production_mix_graph_contract"
        or value.get("profile") != "production"
        or value.get("unit") != "audio-production-mix.service"
    ):
        raise ProductionMixError("production-mix contract schema is invalid")
    graph = value.get("graph")
    if not isinstance(graph, dict) or set(graph) != {
        "rate_hz",
        "sample_format",
        "channels",
        "channel_map",
        "channel_positions",
        "group",
        "virtual_sink",
        "virtual_source",
        "monitor_stream",
        "routes",
    }:
        raise ProductionMixError("production-mix graph contract fields are invalid")
    if (
        graph.get("rate_hz") != 48_000
        or graph.get("sample_format") != "s32le"
        or graph.get("channels") != 2
        or graph.get("channel_map") != "front-left,front-right"
        or graph.get("channel_positions") != ["FL", "FR"]
        or graph.get("group") != "audio-production-mix-v1"
        or graph.get("monitor_stream") != "audio-production-monitor"
    ):
        raise ProductionMixError("production-mix graph format is inconsistent")
    for key, expected in (
        ("virtual_sink", ("audio-production-bus", "Audio Production Bus")),
        ("virtual_source", ("audio-production-mix", "Audio Production Mix")),
    ):
        item = graph.get(key)
        if (
            not isinstance(item, dict)
            or set(item) != {"node_name", "description"}
            or (item.get("node_name"), item.get("description")) != expected
        ):
            raise ProductionMixError(f"production-mix {key} contract is invalid")
    routes = graph.get("routes")
    if not isinstance(routes, dict) or set(routes) != {
        "voice",
        "roland",
        "software-instrument",
    }:
        raise ProductionMixError("production-mix route set is invalid")
    voice = routes["voice"]
    if (
        not isinstance(voice, dict)
        or set(voice)
        != {
            "capture_node",
            "playback_node",
            "source_session",
            "selected_channel_fact",
            "selected_channel_map",
            "capture_channels",
            "capture_position",
            "playback_channels",
            "playback_position",
            "target",
        }
        or voice.get("capture_node") != "audio-production-route-voice-capture"
        or voice.get("playback_node") != "audio-production-route-voice-playback"
        or voice.get("source_session") != "voice-recording"
        or voice.get("selected_channel_fact") != "rode_nt1a_motu_input"
        or voice.get("selected_channel_map") != {"input-1": "FL", "input-2": "FR"}
        or voice.get("capture_channels") != 1
        or voice.get("capture_position") != "MONO"
        or voice.get("playback_channels") != 1
        or voice.get("playback_position") != "MONO"
        or voice.get("target") != "audio-production-bus"
    ):
        raise ProductionMixError("production-mix voice route is invalid")
    roland = routes["roland"]
    if (
        not isinstance(roland, dict)
        or set(roland)
        != {
            "capture_node",
            "playback_node",
            "source_session",
            "source_rate_hz",
            "capture_channels",
            "capture_positions",
            "playback_channels",
            "playback_positions",
            "target",
        }
        or roland.get("capture_node") != "audio-production-route-roland-capture"
        or roland.get("playback_node") != "audio-production-route-roland-playback"
        or roland.get("source_session") != "roland-audio-recording"
        or roland.get("source_rate_hz") != 44_100
        or roland.get("capture_channels") != 2
        or roland.get("capture_positions") != ["FL", "FR"]
        or roland.get("playback_channels") != 2
        or roland.get("playback_positions") != ["FL", "FR"]
        or roland.get("target") != "audio-production-bus"
    ):
        raise ProductionMixError("production-mix Roland route is invalid")
    software = routes["software-instrument"]
    if (
        not isinstance(software, dict)
        or set(software)
        != {"mode", "target", "required_when_active", "accepted_media_classes"}
        or software.get("mode") != "direct-target"
        or software.get("target") != "audio-production-bus"
        or software.get("required_when_active") is not True
        or software.get("accepted_media_classes") != ["Stream/Output/Audio"]
    ):
        raise ProductionMixError("production-mix software route is invalid")
    physical = value.get("required_physical_facts")
    expected_physical = {
        "rode_nt1a_connected": True,
        "rode_nt1a_motu_input": ["input-1", "input-2"],
        "motu_phantom_48v": "on",
        "motu_input_gain_reference": "non-empty-string",
        "motu_output_to_lake_people": "non-empty-string",
        "lake_people_gain_setting": "non-empty-string",
        "lake_people_volume_reference": "non-empty-string",
        "focal_connected_output": "non-empty-string",
    }
    if physical != expected_physical:
        raise ProductionMixError("production-mix physical requirements are invalid")
    if value.get("required_laboratory_gates") != [
        "voice-level-measurement",
        "resampling-decision",
    ]:
        raise ProductionMixError("production-mix laboratory gates are invalid")
    monitor = value.get("monitor_target")
    if (
        not isinstance(monitor, dict)
        or set(monitor)
        != {
            "device",
            "vendor_id",
            "product_id",
            "serial_prefix",
            "node_name_prefix",
            "required_sample_formats",
            "required_sample_rate_hz",
            "required_channels",
            "requires_unmuted",
        }
        or monitor.get("device") != "motu_m2"
        or monitor.get("vendor_id") != "07fd"
        or monitor.get("product_id") != "0008"
        or monitor.get("serial_prefix") != "MOTU_M2_"
        or monitor.get("node_name_prefix") != "alsa_output.usb-MOTU_M2_"
        or monitor.get("required_sample_formats") != ["s32le"]
        or monitor.get("required_sample_rate_hz") != 48_000
        or monitor.get("required_channels") != 2
        or monitor.get("requires_unmuted") is not True
    ):
        raise ProductionMixError("production-mix monitor target is invalid")
    process = value.get("process")
    process_fields = {
        "executable",
        "child_count",
        "startup_timeout_seconds",
        "stop_grace_seconds",
        "topology_poll_seconds",
        "topology_failure_limit",
        "maximum_stderr_bytes_per_child",
        "maximum_open_files",
        "memory_max_bytes",
        "tasks_max",
        "cpu_quota_percent",
        "runtime_max_seconds",
        "log_rate_limit_interval_seconds",
        "log_rate_limit_burst",
    }
    if (
        not isinstance(process, dict)
        or set(process) != process_fields
        or process.get("executable") != "/usr/bin/pw-loopback"
        or process.get("child_count") != 4
        or process.get("startup_timeout_seconds") != 10
        or process.get("stop_grace_seconds") != 8
        or process.get("topology_poll_seconds") != 1
        or process.get("topology_failure_limit") != 3
        or process.get("maximum_stderr_bytes_per_child") != 65_536
        or process.get("maximum_open_files") != 128
        or process.get("memory_max_bytes") != 268_435_456
        or process.get("tasks_max") != 64
        or process.get("cpu_quota_percent") != 80
        or process.get("runtime_max_seconds") != 43_200
        or process.get("log_rate_limit_interval_seconds") != 30
        or process.get("log_rate_limit_burst") != 100
    ):
        raise ProductionMixError("production-mix process contract is invalid")
    rules = value.get("rules")
    if (
        not isinstance(rules, list)
        or len(rules) < 5
        or any(not _valid_text(item, maximum=300) for item in rules)
    ):
        raise ProductionMixError("production-mix rules are invalid")
    return value


def profile_binding() -> dict[str, Any]:
    payload, catalog_binding = _read_json_with_binding(PROFILE_PATH)
    profiles = payload.get("profiles")
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "audio_profile_catalog"
        or set(payload) != {"schema_version", "kind", "profiles"}
        or not isinstance(profiles, dict)
    ):
        raise ProductionMixError("audio profile catalog schema is invalid")
    selected: dict[str, Any] = {}
    for name in ("production", "voice-recording", "piano-digital-recording"):
        profile = profiles.get(name)
        if not isinstance(profile, dict):
            raise ProductionMixError(f"production-mix profile is missing: {name}")
        selected[name] = {
            "desired": profile.get("desired"),
            "required_hardware": profile.get("required_hardware"),
            "required_physical_facts": profile.get("required_physical_facts"),
            "required_laboratory_gates": profile.get("required_laboratory_gates"),
            "apply_authority": profile.get("apply_authority"),
            "operational_status": profile.get("operational_status", "available"),
            "planned_blocker": profile.get("planned_blocker"),
        }
    contract = load_contract()
    production = selected["production"]
    expected_desired = {
        "session_engine": "ardour",
        "graph_manager": "audio-production-mix-v1",
        "production_bus_sink": contract["graph"]["virtual_sink"]["node_name"],
        "recording_mix_source": contract["graph"]["virtual_source"]["node_name"],
        "default_sink": "motu-m2",
        "default_source": "motu-m2",
        "monitor_sink": "motu-m2",
        "voice_route": "selected-motu-input-to-mono-production-bus",
        "roland_route": "single-stage-44k1-to-48k-production-bus",
        "software_instrument_route": "direct-target-audio-production-bus",
        "rate_hz": contract["graph"]["rate_hz"],
        "sample_format": contract["graph"]["sample_format"],
        "channels": contract["graph"]["channels"],
        "quantum_candidate_frames": 512,
        "reopen_proof_required": True,
    }
    if (
        contract["profile"] != "production"
        or production.get("operational_status") != "planned"
        or production.get("apply_authority") != "planned-not-executable"
        or not _valid_text(production.get("planned_blocker"), maximum=500)
        or production.get("required_hardware") != ["motu_m2", "roland_fp_30x"]
        or production.get("required_physical_facts")
        != list(contract["required_physical_facts"])
        or production.get("required_laboratory_gates")
        != contract["required_laboratory_gates"]
        or production.get("desired") != expected_desired
        or selected["voice-recording"].get("required_laboratory_gates")
        != ["voice-level-measurement"]
        or selected["piano-digital-recording"].get("required_laboratory_gates")
        != ["resampling-decision"]
    ):
        raise ProductionMixError("production-mix profile contract is inconsistent")
    return {
        "catalog_sha256": catalog_binding["sha256"],
        "catalog_bytes": catalog_binding["bytes"],
        "selected": selected,
        "selected_sha256": canonical_sha256(selected),
    }


def contract_bindings() -> list[dict[str, Any]]:
    paths = (
        CONTRACT_PATH,
        PROFILE_PATH,
        pathlib.Path(__file__).resolve(),
        WRAPPER_PATH,
        RECORDING_PATH,
        RATE_PATH,
        SYSTEM_TRUTH_PATH,
        ROOT / "profiles" / "recording-sessions.v1.json",
        ROOT / "inventory" / "physical-facts.v1.json",
        ROOT / "inventory" / "physical-verification.v1.json",
        ROOT / "inventory" / "laboratory-gates.v1.json",
    )
    return [_binding(path) for path in paths]


def executable_binding(path: pathlib.Path | None = None) -> dict[str, Any]:
    contract = load_contract()
    launcher = path or pathlib.Path(contract["process"]["executable"])
    if (
        not launcher.is_absolute()
        or not launcher.is_file()
        or not os.access(launcher, os.X_OK)
    ):
        raise ProductionMixError(f"pw-loopback executable is unavailable: {launcher}")
    metadata = launcher.lstat()
    link_target: str | None = None
    resolved = launcher
    if stat.S_ISLNK(metadata.st_mode):
        link_target = os.readlink(launcher)
        resolved = launcher.resolve(strict=True)
    return {
        "launcher": str(launcher),
        "launcher_symlink_target": link_target,
        "resolved": _binding(resolved),
    }


def _run_json_query(
    argv: tuple[str, ...],
    *,
    query_fn: Callable[[tuple[str, ...]], tuple[list[Any], dict[str, Any]]]
    | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    try:
        return (query_fn or RATE._run_query)(argv)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ProductionMixError(f"audio graph query failed: {argv[-1]}") from exc


def _volume_values(item: dict[str, Any]) -> list[int]:
    try:
        return REC.VOICE._source_volume_values(item)
    except (AttributeError, TypeError, ValueError):
        return []


def _motu_sink_identity(
    item: Any, contract: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(item, dict):
        raise ProductionMixError("pactl sink item is not an object")
    properties = item.get("properties")
    if not isinstance(properties, dict):
        return None, None
    vendor_id = _normalize_usb_id(properties.get("device.vendor.id"))
    product_id = _normalize_usb_id(properties.get("device.product.id"))
    if vendor_id != contract["vendor_id"] or product_id != contract["product_id"]:
        return None, None
    name = item.get("name")
    serial = properties.get("device.serial")
    bus_path = properties.get("device.bus_path")
    if not isinstance(name, str) or not name.startswith(contract["node_name_prefix"]):
        raise ProductionMixError("MOTU monitor sink node name is invalid")
    if not isinstance(serial, str) or not serial.startswith(contract["serial_prefix"]):
        raise ProductionMixError("MOTU monitor sink lacks serial identity")
    if f"usb-{serial}-00" not in name:
        raise ProductionMixError("MOTU monitor sink node does not match its serial")
    if not isinstance(bus_path, str) or not bus_path:
        raise ProductionMixError("MOTU monitor sink lacks USB bus identity")
    match = SAMPLE_SPEC_RE.fullmatch(str(item.get("sample_specification", "")))
    if match is None:
        raise ProductionMixError("MOTU monitor sink sample specification is invalid")
    volume = _volume_values(item)
    if not volume:
        raise ProductionMixError("MOTU monitor sink volume is unavailable")
    identity = {
        "vendor_id": vendor_id,
        "product_id": product_id,
        "serial_sha256": hashlib.sha256(serial.encode()).hexdigest(),
        "node_name_sha256": hashlib.sha256(name.encode()).hexdigest(),
        "bus_path_sha256": hashlib.sha256(bus_path.encode()).hexdigest(),
        "sample_format": match.group("format"),
        "sample_rate_hz": int(match.group("rate")),
        "channels": int(match.group("channels")),
        "muted": item.get("mute"),
        "volume_sha256": canonical_sha256(volume),
    }
    identity["fingerprint"] = canonical_sha256(identity)
    return identity, name


def _motu_sink_snapshot(
    contract: dict[str, Any],
    *,
    query_fn: Callable[[tuple[str, ...]], tuple[list[Any], dict[str, Any]]]
    | None = None,
) -> dict[str, Any]:
    payload, query = _run_json_query(PULSE_QUERIES["sinks"], query_fn=query_fn)
    matches: list[tuple[dict[str, Any], str]] = []
    errors: list[str] = []
    for item in payload:
        try:
            identity, name = _motu_sink_identity(item, contract)
        except ProductionMixError as exc:
            errors.append(str(exc))
            continue
        if identity is not None and name is not None:
            matches.append((identity, name))
    complete = not errors and len(matches) == 1
    return {
        "complete": complete,
        "match_count": len(matches),
        "ambiguous": len(matches) > 1,
        "errors": sorted(set(errors)),
        "identity": matches[0][0] if complete else None,
        "node_name": matches[0][1] if complete else None,
        "query": query,
    }


def _monitor_projection(
    contract: dict[str, Any], snapshot_fn: Callable[[], dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    try:
        snapshot = snapshot_fn()
    except (OSError, ProductionMixError, ValueError, json.JSONDecodeError) as exc:
        return {"identity": None, "error": str(exc)}, ["motu-sink-query-failed"]
    identity = snapshot.get("identity")
    blockers: list[str] = []
    if snapshot.get("complete") is not True or not isinstance(identity, dict):
        blockers.append("motu-sink-not-unique")
        identity = None
    if identity is not None:
        expected = {
            "vendor_id": contract["vendor_id"],
            "product_id": contract["product_id"],
            "sample_rate_hz": contract["required_sample_rate_hz"],
            "channels": contract["required_channels"],
            "muted": False,
        }
        for field, expected_value in expected.items():
            if identity.get(field) != expected_value:
                blockers.append(f"motu-sink:{field}")
        if identity.get("sample_format") not in contract["required_sample_formats"]:
            blockers.append("motu-sink:sample_format")
    return {
        "identity": identity,
        "identity_sha256": canonical_sha256(identity) if identity is not None else None,
        "error": None,
    }, blockers


def _parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _parse_environment(value: str) -> dict[str, str]:
    try:
        items = shlex.split(value)
    except ValueError as exc:
        raise ProductionMixError("managed service environment is invalid") from exc
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            continue
        key, setting = item.split("=", 1)
        result[key] = setting
    return result


def _run_capture(
    argv: list[str], *, timeout: float = 15.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=REC._restricted_environment(),
        close_fds=True,
    )


def service_snapshot(
    unit: str,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    if unit != "audio-production-mix.service":
        raise ProductionMixError("production-mix unit is invalid")
    argv = [
        "systemctl",
        "--user",
        "show",
        unit,
        "--no-pager",
        "--property=" + ",".join(SERVICE_PROPERTIES),
    ]
    result = (runner or _run_capture)(argv)
    values = _parse_key_values(result.stdout)
    if result.returncode != 0 and values.get("LoadState") != "not-found":
        detail = (
            result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        )
        raise ProductionMixError(f"systemctl show failed: {detail}")
    if values.get("LoadState") == "not-found":
        return {
            "unit": unit,
            "load_state": "not-found",
            "active_state": values.get("ActiveState", "inactive"),
            "sub_state": values.get("SubState", "dead"),
            "result": values.get("Result", "success"),
            "managed": False,
            "spec_sha256": None,
            "identity": None,
            "limits": None,
        }
    missing = sorted(set(SERVICE_PROPERTIES) - set(values))
    if missing:
        raise ProductionMixError(f"systemctl show is incomplete: {missing}")
    environment = _parse_environment(values["Environment"])
    main_pid = int(values["MainPID"] or "0")
    invocation = values["InvocationID"]
    control_group = values["ControlGroup"]
    exec_start = values["ExecStart"]
    identity: dict[str, Any] | None = None
    if main_pid >= 2 and invocation and control_group and exec_start:
        identity = {
            "main_pid": main_pid,
            "invocation_id": invocation,
            "control_group_sha256": hashlib.sha256(control_group.encode()).hexdigest(),
            "exec_start_sha256": hashlib.sha256(exec_start.encode()).hexdigest(),
        }

    def parse_limit(name: str) -> int | None:
        raw = values[name]
        if raw == "infinity":
            return None
        return int(raw)

    return {
        "unit": values["Id"],
        "load_state": values["LoadState"],
        "active_state": values["ActiveState"],
        "sub_state": values["SubState"],
        "result": values["Result"],
        "exec_main_status": int(values["ExecMainStatus"] or "0"),
        "restart_count": int(values["NRestarts"] or "0"),
        "managed": environment.get(MANAGED_MARKER_ENV) == "1",
        "spec_sha256": environment.get(SPEC_SHA_ENV),
        "identity": identity,
        "limits": {
            "memory_max_bytes": parse_limit("MemoryMax"),
            "tasks_max": parse_limit("TasksMax"),
            "limit_nofile": parse_limit("LimitNOFILE"),
            "log_rate_limit_interval_usec": int(
                values["LogRateLimitIntervalUSec"] or "0"
            ),
            "log_rate_limit_burst": int(values["LogRateLimitBurst"] or "0"),
        },
    }


def _service_blockers(snapshot: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    if snapshot.get("load_state") == "not-found":
        return []
    active = snapshot.get("active_state")
    if active in {"active", "activating", "reloading", "deactivating"}:
        return ["managed-service-already-present"]
    if snapshot.get("managed") is not True:
        return ["foreign-service-unit-present"]
    return ["stale-managed-service-unit-present"]


def _query_all_graph(
    *,
    query_fn: Callable[[tuple[str, ...]], tuple[list[Any], dict[str, Any]]]
    | None = None,
) -> tuple[dict[str, list[Any]], dict[str, dict[str, Any]]]:
    payloads: dict[str, list[Any]] = {}
    bindings: dict[str, dict[str, Any]] = {}
    for key, argv in PULSE_QUERIES.items():
        payload, binding = _run_json_query(argv, query_fn=query_fn)
        payloads[key] = payload
        bindings[key] = binding
    return payloads, bindings


def _index_by_name(items: list[Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str):
            result.setdefault(name, []).append(item)
    return result


def _stream_node_name(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    properties = item.get("properties")
    if not isinstance(properties, dict):
        return None
    value = properties.get("node.name")
    return value if isinstance(value, str) and value else None


def _sample_projection(item: dict[str, Any]) -> dict[str, Any] | None:
    match = SAMPLE_SPEC_RE.fullmatch(str(item.get("sample_specification", "")))
    if match is None:
        return None
    return {
        "sample_format": match.group("format"),
        "channels": int(match.group("channels")),
        "sample_rate_hz": int(match.group("rate")),
        "channel_map": item.get("channel_map"),
    }


def graph_conflict_snapshot(
    contract: dict[str, Any],
    *,
    query_fn: Callable[[tuple[str, ...]], tuple[list[Any], dict[str, Any]]]
    | None = None,
) -> dict[str, Any]:
    payloads, bindings = _query_all_graph(query_fn=query_fn)
    graph = contract["graph"]
    endpoint_names = {
        graph["virtual_sink"]["node_name"],
        graph["virtual_source"]["node_name"],
    }
    stream_names = {
        graph["monitor_stream"],
        "audio-production-mix-capture",
        graph["routes"]["voice"]["capture_node"],
        graph["routes"]["voice"]["playback_node"],
        graph["routes"]["roland"]["capture_node"],
        graph["routes"]["roland"]["playback_node"],
    }
    endpoint_hits: dict[str, int] = {}
    for name in endpoint_names:
        endpoint_hits[name] = sum(
            1
            for item in payloads["sinks"] + payloads["sources"]
            if isinstance(item, dict) and item.get("name") == name
        )
    stream_hits = {
        name: sum(
            1
            for item in payloads["sink_inputs"] + payloads["source_outputs"]
            if _stream_node_name(item) == name
        )
        for name in stream_names
    }
    blockers = [
        f"graph-name-conflict:{name}"
        for name, count in sorted(endpoint_hits.items() | stream_hits.items())
        if count > 0
    ]
    return {
        "clear": not blockers,
        "blockers": blockers,
        "endpoint_hits": endpoint_hits,
        "stream_hits": stream_hits,
        "query_sha256": canonical_sha256(bindings),
    }


def _find_one(
    items: list[Any], predicate: Callable[[dict[str, Any]], bool]
) -> dict[str, Any] | None:
    matches = [item for item in items if isinstance(item, dict) and predicate(item)]
    return matches[0] if len(matches) == 1 else None


def graph_topology_snapshot(
    spec: dict[str, Any],
    *,
    query_fn: Callable[[tuple[str, ...]], tuple[list[Any], dict[str, Any]]]
    | None = None,
) -> dict[str, Any]:
    payloads, bindings = _query_all_graph(query_fn=query_fn)
    plan = spec["plan_identity"]
    graph = plan["graph"]
    raw = spec["raw_nodes"]
    sinks = _index_by_name(payloads["sinks"])
    sources = _index_by_name(payloads["sources"])
    blockers: list[str] = []

    def unique_endpoint(index: dict[str, list[dict[str, Any]]], name: str, role: str):
        values = index.get(name, [])
        if len(values) != 1:
            blockers.append(f"{role}-not-unique")
            return None
        return values[0]

    bus = unique_endpoint(sinks, graph["virtual_sink"]["node_name"], "production-bus")
    mix = unique_endpoint(
        sources, graph["virtual_source"]["node_name"], "production-mix"
    )
    monitor_sink = unique_endpoint(sinks, raw["monitor_sink"], "motu-monitor-sink")
    voice_source = unique_endpoint(sources, raw["voice_source"], "voice-source")
    roland_source = unique_endpoint(sources, raw["roland_source"], "roland-source")
    expected_format = {
        "sample_format": graph["sample_format"],
        "channels": graph["channels"],
        "sample_rate_hz": graph["rate_hz"],
        "channel_map": graph["channel_map"],
    }
    for role, endpoint in (("production-bus", bus), ("production-mix", mix)):
        if endpoint is not None and _sample_projection(endpoint) != expected_format:
            blockers.append(f"{role}-format-drift")
    indices: dict[str, int | None] = {}
    for role, endpoint in (
        ("bus", bus),
        ("mix", mix),
        ("monitor", monitor_sink),
        ("voice", voice_source),
        ("roland", roland_source),
    ):
        index = endpoint.get("index") if isinstance(endpoint, dict) else None
        indices[role] = index if isinstance(index, int) else None
        if endpoint is not None and indices[role] is None:
            blockers.append(f"{role}-index-invalid")
    bus_monitor_name = bus.get("monitor_source") if isinstance(bus, dict) else None
    bus_monitor = (
        unique_endpoint(sources, bus_monitor_name, "production-bus-monitor")
        if isinstance(bus_monitor_name, str) and bus_monitor_name
        else None
    )
    if bus_monitor is None and not isinstance(bus_monitor_name, str):
        blockers.append("production-bus-monitor-name-invalid")
    bus_monitor_index = (
        bus_monitor.get("index") if isinstance(bus_monitor, dict) else None
    )
    if not isinstance(bus_monitor_index, int):
        blockers.append("production-bus-monitor-index-invalid")
        bus_monitor_index = None

    sink_roles = {
        graph["monitor_stream"]: indices["monitor"],
        graph["routes"]["voice"]["playback_node"]: indices["bus"],
        graph["routes"]["roland"]["playback_node"]: indices["bus"],
    }
    source_roles = {
        "audio-production-mix-capture": bus_monitor_index,
        graph["routes"]["voice"]["capture_node"]: indices["voice"],
        graph["routes"]["roland"]["capture_node"]: indices["roland"],
    }
    role_projection: dict[str, Any] = {}
    for node_name, target_index in sink_roles.items():
        stream = _find_one(
            payloads["sink_inputs"],
            lambda item, n=node_name: _stream_node_name(item) == n,
        )
        observed = stream.get("sink") if isinstance(stream, dict) else None
        if stream is None:
            blockers.append(f"sink-input-missing:{node_name}")
        elif observed != target_index:
            blockers.append(f"sink-input-target-drift:{node_name}")
        role_projection[node_name] = {
            "direction": "sink-input",
            "target_index": target_index,
            "observed_index": observed,
            "sample": _sample_projection(stream) if isinstance(stream, dict) else None,
        }
    for node_name, target_index in source_roles.items():
        stream = _find_one(
            payloads["source_outputs"],
            lambda item, n=node_name: _stream_node_name(item) == n,
        )
        observed = stream.get("source") if isinstance(stream, dict) else None
        if stream is None:
            blockers.append(f"source-output-missing:{node_name}")
        elif observed != target_index:
            blockers.append(f"source-output-target-drift:{node_name}")
        role_projection[node_name] = {
            "direction": "source-output",
            "target_index": target_index,
            "observed_index": observed,
            "sample": _sample_projection(stream) if isinstance(stream, dict) else None,
        }
    known_bus_nodes = {
        graph["routes"]["voice"]["playback_node"],
        graph["routes"]["roland"]["playback_node"],
    }
    software_streams: list[dict[str, Any]] = []
    for item in payloads["sink_inputs"]:
        if not isinstance(item, dict) or item.get("sink") != indices["bus"]:
            continue
        node_name = _stream_node_name(item)
        if node_name in known_bus_nodes:
            continue
        properties = item.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        media_class = properties.get("media.class")
        if (
            media_class
            not in graph["routes"]["software-instrument"]["accepted_media_classes"]
        ):
            blockers.append("software-stream-media-class-invalid")
        software_streams.append(
            {
                "node_name_sha256": (
                    hashlib.sha256(node_name.encode()).hexdigest()
                    if isinstance(node_name, str)
                    else None
                ),
                "media_class": media_class,
                "application_name_sha256": (
                    hashlib.sha256(
                        str(properties.get("application.name")).encode()
                    ).hexdigest()
                    if properties.get("application.name") not in {None, ""}
                    else None
                ),
                "sample": _sample_projection(item),
            }
        )
    result = {
        "complete": not blockers,
        "blockers": sorted(set(blockers)),
        "endpoints": {
            "bus": _sample_projection(bus) if isinstance(bus, dict) else None,
            "mix": _sample_projection(mix) if isinstance(mix, dict) else None,
        },
        "roles": role_projection,
        "software_instruments": software_streams,
        "query_sha256": canonical_sha256(bindings),
    }
    result["topology_sha256"] = canonical_sha256(result)
    return result


def truth_binding() -> dict[str, Any]:
    try:
        truth = RATE.truth_binding()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProductionMixError("audio graph truth cannot be bound") from exc
    return truth


def build_plan(
    *,
    state_root: pathlib.Path = DEFAULT_STATE_ROOT,
    physical_state: pathlib.Path = REC.PHYSICAL.DEFAULT_STATE,
    laboratory_state: pathlib.Path = REC.LAB.DEFAULT_STATE,
    voice_snapshot_fn: Callable[[], dict[str, Any]] | None = None,
    roland_snapshot_fn: Callable[[], dict[str, Any]] | None = None,
    monitor_snapshot_fn: Callable[[], dict[str, Any]] | None = None,
    truth_fn: Callable[[], dict[str, Any]] = truth_binding,
    service_fn: Callable[[], dict[str, Any]] | None = None,
    conflict_fn: Callable[[], dict[str, Any]] | None = None,
    executable_fn: Callable[[], dict[str, Any]] = executable_binding,
) -> dict[str, Any]:
    contract = load_contract()
    state_root = REC.lexical_absolute(state_root)
    physical, physical_blockers = REC._physical_projection(
        REC.lexical_absolute(physical_state), contract["required_physical_facts"]
    )
    laboratory, laboratory_blockers = REC._laboratory_projection(
        REC.lexical_absolute(laboratory_state),
        physical,
        contract["required_laboratory_gates"],
    )
    voice_contract = REC.load_catalog("voice-recording")["source"]
    roland_contract = REC.load_catalog("roland-audio-recording")["source"]
    voice, voice_blockers = REC._source_projection(voice_contract, voice_snapshot_fn)
    roland, roland_blockers = REC._source_projection(
        roland_contract, roland_snapshot_fn
    )
    monitor, monitor_blockers = _monitor_projection(
        contract["monitor_target"],
        monitor_snapshot_fn
        or (lambda: _motu_sink_snapshot(contract["monitor_target"])),
    )
    blockers = [
        *physical_blockers,
        *laboratory_blockers,
        *voice_blockers,
        *roland_blockers,
        *monitor_blockers,
    ]
    try:
        truth = truth_fn()
    except (OSError, ProductionMixError, RuntimeError, ValueError) as exc:
        truth = {"error": str(exc)}
        blockers.append("graph-truth-unavailable")
    else:
        if truth.get("graph_rate_hz") != contract["graph"]["rate_hz"]:
            blockers.append("graph-rate-is-not-48k")
    try:
        executable = executable_fn()
    except (OSError, ProductionMixError) as exc:
        executable = {"error": str(exc)}
        blockers.append("pw-loopback-unavailable")
    try:
        service = (service_fn or (lambda: service_snapshot(contract["unit"])))()
    except (OSError, ProductionMixError, RuntimeError, ValueError) as exc:
        service = {"error": str(exc)}
        blockers.append("service-state-unavailable")
    else:
        blockers.extend(_service_blockers(service, contract))
    try:
        conflicts = (conflict_fn or (lambda: graph_conflict_snapshot(contract)))()
    except (OSError, ProductionMixError, ValueError) as exc:
        conflicts = {
            "clear": False,
            "blockers": ["graph-conflict-query-failed"],
            "error": str(exc),
        }
    blockers.extend(conflicts.get("blockers", []))
    active = state_root / "active.json"
    if active.exists() or active.is_symlink():
        blockers.append("active-graph-requires-status-or-recovery")
    selected_input = physical.get("facts", {}).get("rode_nt1a_motu_input")
    selected_position = contract["graph"]["routes"]["voice"][
        "selected_channel_map"
    ].get(selected_input)
    if selected_position not in {"FL", "FR"}:
        blockers.append("voice-channel-selection-invalid")
    identity = {
        "schema_version": 1,
        "kind": "audio_production_mix_plan_identity",
        "unit": contract["unit"],
        "state_root": str(state_root),
        "graph": contract["graph"],
        "voice_channel": {
            "physical_input": selected_input,
            "source_position": selected_position,
            "playback_position": "MONO",
        },
        "physical": physical,
        "laboratory": laboratory,
        "voice_source": voice,
        "roland_source": roland,
        "monitor_sink": monitor,
        "graph_truth": truth,
        "profiles": profile_binding(),
        "contracts": contract_bindings(),
        "executable": executable,
        "process": contract["process"],
    }
    plan_sha = canonical_sha256(identity)
    blockers = sorted(set(str(item) for item in blockers))
    return {
        "schema_version": 1,
        "kind": "audio_production_mix_plan",
        "ready": not blockers,
        "plan_sha256": plan_sha,
        "identity": identity,
        "readiness": {
            "blockers": blockers,
            "service": service,
            "conflicts": conflicts,
        },
        "does_not_establish": [
            "safe-monitoring-level",
            "subjective-recording-quality",
            "current-software-instrument-presence",
            "absence-of-resampling-outside-the-declared-Roland-route",
            "successful-future-graph-start",
        ],
    }


def _expected_plan_fields() -> set[str]:
    return {
        "schema_version",
        "kind",
        "unit",
        "state_root",
        "graph",
        "voice_channel",
        "physical",
        "laboratory",
        "voice_source",
        "roland_source",
        "monitor_sink",
        "graph_truth",
        "profiles",
        "contracts",
        "executable",
        "process",
    }


def _validate_projection(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "identity",
        "identity_sha256",
        "error",
    }:
        raise ProductionMixError(f"{label} projection fields are invalid")
    identity = value.get("identity")
    digest = value.get("identity_sha256")
    if (
        value.get("error") is not None
        or not isinstance(identity, dict)
        or not isinstance(digest, str)
        or HEX64_RE.fullmatch(digest) is None
        or canonical_sha256(identity) != digest
    ):
        raise ProductionMixError(f"{label} projection is not bound")
    return identity


def validate_spec(
    spec: dict[str, Any], *, state_root: pathlib.Path | None = None
) -> None:
    required = {
        "schema_version",
        "kind",
        "session_id",
        "created_at",
        "plan_sha256",
        "plan_identity",
        "raw_nodes",
        "paths",
    }
    if set(spec) != required:
        raise ProductionMixError("production-mix spec fields are invalid")
    session_id = spec.get("session_id")
    plan = spec.get("plan_identity")
    plan_sha = spec.get("plan_sha256")
    if (
        spec.get("schema_version") != 1
        or spec.get("kind") != "audio_production_mix_spec"
        or not isinstance(session_id, str)
        or SESSION_ID_RE.fullmatch(session_id) is None
        or not _valid_timestamp(spec.get("created_at"))
        or not isinstance(plan, dict)
        or set(plan) != _expected_plan_fields()
        or plan.get("schema_version") != 1
        or plan.get("kind") != "audio_production_mix_plan_identity"
        or not isinstance(plan_sha, str)
        or HEX64_RE.fullmatch(plan_sha) is None
        or canonical_sha256(plan) != plan_sha
    ):
        raise ProductionMixError("production-mix spec schema is invalid")
    contract = load_contract()
    if (
        plan.get("unit") != contract["unit"]
        or plan.get("graph") != contract["graph"]
        or plan.get("process") != contract["process"]
        or plan.get("profiles") != profile_binding()
    ):
        raise ProductionMixError("production-mix plan no longer matches its contract")
    if plan.get("contracts") != contract_bindings():
        raise ProductionMixError("production-mix implementation contracts changed")
    if plan.get("executable") != executable_binding():
        raise ProductionMixError("pw-loopback executable changed after planning")
    voice_identity = _validate_projection(plan.get("voice_source"), "voice source")
    roland_identity = _validate_projection(plan.get("roland_source"), "Roland source")
    monitor_identity = _validate_projection(plan.get("monitor_sink"), "monitor sink")
    try:
        REC._validate_planned_source_projection(
            plan["voice_source"], REC.load_catalog("voice-recording")
        )
        REC._validate_planned_source_projection(
            plan["roland_source"], REC.load_catalog("roland-audio-recording")
        )
    except REC.RecordingError as exc:
        raise ProductionMixError("production-mix source projection is invalid") from exc
    monitor_fields = {
        "vendor_id",
        "product_id",
        "serial_sha256",
        "node_name_sha256",
        "bus_path_sha256",
        "sample_format",
        "sample_rate_hz",
        "channels",
        "muted",
        "volume_sha256",
        "fingerprint",
    }
    monitor_without_fingerprint = dict(monitor_identity)
    monitor_fingerprint = monitor_without_fingerprint.pop("fingerprint", None)
    if (
        set(monitor_identity) != monitor_fields
        or monitor_identity.get("vendor_id") != contract["monitor_target"]["vendor_id"]
        or monitor_identity.get("product_id")
        != contract["monitor_target"]["product_id"]
        or monitor_identity.get("sample_format")
        not in contract["monitor_target"]["required_sample_formats"]
        or monitor_identity.get("sample_rate_hz")
        != contract["monitor_target"]["required_sample_rate_hz"]
        or monitor_identity.get("channels")
        != contract["monitor_target"]["required_channels"]
        or monitor_identity.get("muted") is not False
        or not isinstance(monitor_fingerprint, str)
        or HEX64_RE.fullmatch(monitor_fingerprint) is None
        or canonical_sha256(monitor_without_fingerprint) != monitor_fingerprint
        or any(
            not isinstance(monitor_identity.get(key), str)
            or HEX64_RE.fullmatch(monitor_identity[key]) is None
            for key in (
                "serial_sha256",
                "node_name_sha256",
                "bus_path_sha256",
                "volume_sha256",
            )
        )
    ):
        raise ProductionMixError("production-mix monitor identity is invalid")
    raw = spec.get("raw_nodes")
    if not isinstance(raw, dict) or set(raw) != {
        "voice_source",
        "roland_source",
        "monitor_sink",
    }:
        raise ProductionMixError("production-mix raw node fields are invalid")
    for key, identity in (
        ("voice_source", voice_identity),
        ("roland_source", roland_identity),
        ("monitor_sink", monitor_identity),
    ):
        value = raw.get(key)
        if not _valid_text(value) or hashlib.sha256(
            value.encode()
        ).hexdigest() != identity.get("node_name_sha256"):
            raise ProductionMixError(
                f"production-mix raw {key} does not match the plan"
            )
    channel = plan.get("voice_channel")
    if (
        not isinstance(channel, dict)
        or set(channel)
        != {
            "physical_input",
            "source_position",
            "playback_position",
        }
        or channel.get("physical_input") not in {"input-1", "input-2"}
        or channel.get("source_position")
        != contract["graph"]["routes"]["voice"]["selected_channel_map"].get(
            channel.get("physical_input")
        )
        or channel.get("playback_position") != "MONO"
    ):
        raise ProductionMixError("production-mix voice channel binding is invalid")
    raw_root = plan.get("state_root")
    if not _valid_text(raw_root) or not pathlib.Path(raw_root).is_absolute():
        raise ProductionMixError("production-mix state root is invalid")
    root = REC.lexical_absolute(pathlib.Path(raw_root))
    if root != pathlib.Path(raw_root) or (
        state_root is not None and REC.lexical_absolute(state_root) != root
    ):
        raise ProductionMixError("production-mix state root does not match the session")
    paths = spec.get("paths")
    expected_paths = _session_paths(root, session_id)
    if not isinstance(paths, dict) or set(paths) != {"ready", "result"}:
        raise ProductionMixError("production-mix spec paths are invalid")
    for key in ("ready", "result"):
        raw_path = paths.get(key)
        if not _valid_text(raw_path) or pathlib.Path(raw_path) != expected_paths[key]:
            raise ProductionMixError(
                "production-mix spec path does not match the session"
            )


def _role_commands(spec: dict[str, Any]) -> list[dict[str, Any]]:
    validate_spec(spec)
    plan = spec["plan_identity"]
    graph = plan["graph"]
    routes = graph["routes"]
    raw = spec["raw_nodes"]
    executable = plan["executable"]["resolved"]["path"]
    group = graph["group"]
    channel_map = "[ FL, FR ]"
    common_playback = {
        "node.passive": True,
        "node.dont-reconnect": True,
        "stream.dont-remix": True,
    }
    bus_capture = {
        "node.name": graph["virtual_sink"]["node_name"],
        "node.description": graph["virtual_sink"]["description"],
        "media.class": "Audio/Sink",
        "node.virtual": True,
        "audio.position": ["FL", "FR"],
    }
    bus_playback = {
        **common_playback,
        "node.name": graph["monitor_stream"],
        "media.name": "Audio Production Monitor",
        "audio.position": ["FL", "FR"],
    }
    export_capture = {
        "node.name": "audio-production-mix-capture",
        "stream.capture.sink": True,
        "node.passive": True,
        "node.dont-reconnect": True,
        "stream.dont-remix": True,
        "audio.position": ["FL", "FR"],
    }
    export_playback = {
        "node.name": graph["virtual_source"]["node_name"],
        "node.description": graph["virtual_source"]["description"],
        "media.class": "Audio/Source",
        "node.virtual": True,
        "audio.position": ["FL", "FR"],
    }
    voice_capture = {
        "node.name": routes["voice"]["capture_node"],
        "node.passive": True,
        "node.dont-reconnect": True,
        "stream.dont-remix": True,
        "audio.position": [plan["voice_channel"]["source_position"]],
    }
    voice_playback = {
        "node.name": routes["voice"]["playback_node"],
        "node.passive": True,
        "node.dont-reconnect": True,
        "stream.dont-remix": False,
        "audio.position": ["MONO"],
    }
    roland_capture = {
        "node.name": routes["roland"]["capture_node"],
        "node.passive": True,
        "node.dont-reconnect": True,
        "stream.dont-remix": True,
        "audio.position": ["FL", "FR"],
    }
    roland_playback = {
        "node.name": routes["roland"]["playback_node"],
        "node.passive": True,
        "node.dont-reconnect": True,
        "stream.dont-remix": True,
        "audio.position": ["FL", "FR"],
    }
    return [
        {
            "role": "bus-monitor",
            "argv": [
                executable,
                "--name",
                "audio-production-bus-loopback",
                "--group",
                group,
                "--channels",
                "2",
                "--channel-map",
                channel_map,
                "--playback",
                raw["monitor_sink"],
                "--capture-props",
                _json_props(bus_capture),
                "--playback-props",
                _json_props(bus_playback),
            ],
        },
        {
            "role": "mix-export",
            "argv": [
                executable,
                "--name",
                "audio-production-mix-export-loopback",
                "--group",
                group,
                "--channels",
                "2",
                "--channel-map",
                channel_map,
                "--capture",
                graph["virtual_sink"]["node_name"],
                "--capture-props",
                _json_props(export_capture),
                "--playback-props",
                _json_props(export_playback),
            ],
        },
        {
            "role": "voice-route",
            "argv": [
                executable,
                "--name",
                "audio-production-voice-loopback",
                "--group",
                group,
                "--channels",
                "1",
                "--channel-map",
                "[ MONO ]",
                "--capture",
                raw["voice_source"],
                "--playback",
                graph["virtual_sink"]["node_name"],
                "--capture-props",
                _json_props(voice_capture),
                "--playback-props",
                _json_props(voice_playback),
            ],
        },
        {
            "role": "roland-route",
            "argv": [
                executable,
                "--name",
                "audio-production-roland-loopback",
                "--group",
                group,
                "--channels",
                "2",
                "--channel-map",
                channel_map,
                "--capture",
                raw["roland_source"],
                "--playback",
                graph["virtual_sink"]["node_name"],
                "--capture-props",
                _json_props(roland_capture),
                "--playback-props",
                _json_props(roland_playback),
            ],
        },
    ]


def _read_active(state_root: pathlib.Path) -> str:
    active = REC.lexical_absolute(state_root) / "active.json"
    payload = _read_json(active, private=True)
    if set(payload) != {"schema_version", "kind", "session_id", "spec_sha256"}:
        raise ProductionMixError("production-mix active pointer fields are invalid")
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "audio_production_mix_active"
        or not isinstance(payload.get("session_id"), str)
        or SESSION_ID_RE.fullmatch(payload["session_id"]) is None
        or not isinstance(payload.get("spec_sha256"), str)
        or HEX64_RE.fullmatch(payload["spec_sha256"]) is None
    ):
        raise ProductionMixError("production-mix active pointer is invalid")
    paths = _session_paths(state_root, payload["session_id"])
    if _binding(paths["spec"], private=True)["sha256"] != payload["spec_sha256"]:
        raise ProductionMixError("production-mix active pointer does not bind its spec")
    return payload["session_id"]


def _read_session(
    state_root: pathlib.Path, session_id: str
) -> tuple[dict[str, pathlib.Path], dict[str, Any], dict[str, Any]]:
    paths = _session_paths(state_root, session_id)
    spec = _read_json(paths["spec"], private=True)
    validate_spec(spec, state_root=state_root)
    spec_sha = _binding(paths["spec"], private=True)["sha256"]
    state = _read_json(paths["state"], private=True)
    required = {
        "schema_version",
        "kind",
        "session_id",
        "spec_sha256",
        "started_at",
        "phase",
        "service_identity",
    }
    if (
        set(state) != required
        or state.get("schema_version") != 1
        or state.get("kind") != "audio_production_mix_state"
        or state.get("session_id") != session_id
        or state.get("spec_sha256") != spec_sha
        or not _valid_timestamp(state.get("started_at"))
        or state.get("phase") not in {"starting", "running"}
    ):
        raise ProductionMixError("production-mix state is invalid")
    identity = state.get("service_identity")
    if state["phase"] == "starting":
        if identity is not None:
            raise ProductionMixError(
                "starting production-mix state has a service identity"
            )
    elif (
        not isinstance(identity, dict)
        or set(identity)
        != {"main_pid", "invocation_id", "control_group_sha256", "exec_start_sha256"}
        or not _positive_int(identity.get("main_pid"), "service main PID")
        or not _valid_text(identity.get("invocation_id"), maximum=128)
        or any(
            not isinstance(identity.get(key), str)
            or HEX64_RE.fullmatch(identity[key]) is None
            for key in ("control_group_sha256", "exec_start_sha256")
        )
    ):
        raise ProductionMixError("running production-mix service identity is invalid")
    return paths, spec, state


def _process_identity(pid: int) -> dict[str, Any]:
    identity = REC._proc_identity(pid)
    if identity is None:
        raise ProductionMixError("loopback child process identity is unavailable")
    return identity


def _identity_current(identity: dict[str, Any]) -> bool:
    try:
        return REC._identity_matches(identity)
    except (OSError, REC.RecordingError, TypeError, ValueError):
        return False


def _terminate_children(
    children: list[dict[str, Any]], *, grace_seconds: float
) -> bool:
    if grace_seconds < 0:
        raise ProductionMixError("production-mix termination grace is invalid")
    identities: list[dict[str, Any]] = []
    complete = True
    for item in children:
        identity = item.get("identity")
        if not isinstance(identity, dict):
            complete = False
            continue
        identities.append(identity)
        if not _identity_current(identity):
            continue
        try:
            os.kill(identity["pid"], signal.SIGTERM)
        except ProcessLookupError:
            continue
        except (OSError, KeyError, TypeError, ValueError):
            complete = False
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not any(_identity_current(identity) for identity in identities):
            return complete
        time.sleep(0.05)
    for identity in identities:
        if not _identity_current(identity):
            continue
        try:
            if identity.get("process_group") == identity.get("pid"):
                os.killpg(identity["process_group"], signal.SIGKILL)
            else:
                os.kill(identity["pid"], signal.SIGKILL)
        except ProcessLookupError:
            continue
        except (OSError, KeyError, TypeError, ValueError):
            complete = False
    kill_deadline = time.monotonic() + 2.0
    while time.monotonic() < kill_deadline:
        if not any(_identity_current(identity) for identity in identities):
            return complete
        time.sleep(0.02)
    return complete and not any(_identity_current(identity) for identity in identities)


def _stderr_receipts(
    children: list[dict[str, Any]], maximum_bytes: int
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in children:
        handle = item.get("stderr")
        payload = b""
        truncated = False
        if handle is not None:
            try:
                handle.seek(0)
                payload = handle.read(maximum_bytes + 1)
                truncated = len(payload) > maximum_bytes
                payload = payload[:maximum_bytes]
            except (OSError, ValueError):
                payload = b""
                truncated = True
        result.append(
            {
                "role": item.get("role"),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "truncated": truncated,
            }
        )
    return result


def _close_child_handles(children: list[dict[str, Any]]) -> None:
    for item in children:
        handle = item.get("stderr")
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def _wait_for_bus(
    bus_name: str,
    children: list[dict[str, Any]],
    *,
    timeout_seconds: float,
    query_fn: Callable[[tuple[str, ...]], tuple[list[Any], dict[str, Any]]]
    | None = None,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if any(not _identity_current(item["identity"]) for item in children):
            return False
        try:
            sinks, _binding_value = _run_json_query(
                PULSE_QUERIES["sinks"], query_fn=query_fn
            )
        except ProductionMixError:
            time.sleep(0.05)
            continue
        count = sum(
            1
            for item in sinks
            if isinstance(item, dict) and item.get("name") == bus_name
        )
        if count == 1:
            return True
        if count > 1:
            return False
        time.sleep(0.05)
    return False


def worker_run(
    spec: dict[str, Any],
    *,
    popen_fn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    topology_fn: Callable[[dict[str, Any]], dict[str, Any]] = graph_topology_snapshot,
    bus_wait_fn: Callable[[str, list[dict[str, Any]]], bool] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    validate_spec(spec)
    process_contract = spec["plan_identity"]["process"]
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(
        resource.RLIMIT_NOFILE,
        (
            int(process_contract["maximum_open_files"]),
            int(process_contract["maximum_open_files"]),
        ),
    )
    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_int = signal.signal(signal.SIGINT, request_stop)
    children: list[dict[str, Any]] = []
    started_at = utc_now()
    failure: str | None = None
    topology: dict[str, Any] | None = None
    try:
        commands = _role_commands(spec)
        if len(commands) != process_contract["child_count"]:
            raise ProductionMixError("production-mix child command count changed")
        for index, command in enumerate(commands):
            if index == 1:
                wait = bus_wait_fn or (
                    lambda name, current: _wait_for_bus(
                        name,
                        current,
                        timeout_seconds=float(
                            process_contract["startup_timeout_seconds"]
                        ),
                    )
                )
                if not wait(
                    spec["plan_identity"]["graph"]["virtual_sink"]["node_name"],
                    children,
                ):
                    raise ProductionMixError(
                        "production bus did not become uniquely observable"
                    )
            stderr_file = tempfile.TemporaryFile()
            process = popen_fn(
                command["argv"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                cwd=str(ROOT),
                env=REC._restricted_environment(),
                start_new_session=True,
                close_fds=True,
            )
            identity: dict[str, Any] | None = None
            for _attempt in range(100):
                try:
                    identity = _process_identity(process.pid)
                except ProductionMixError:
                    if process.poll() is not None:
                        break
                    sleep_fn(0.01)
                    continue
                break
            if identity is None:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                stderr_file.close()
                raise ProductionMixError(
                    f"loopback child identity could not be established: {command['role']}"
                )
            children.append(
                {
                    "role": command["role"],
                    "argv_sha256": canonical_sha256(command["argv"]),
                    "process": process,
                    "identity": identity,
                    "stderr": stderr_file,
                }
            )
        deadline = monotonic_fn() + float(process_contract["startup_timeout_seconds"])
        while monotonic_fn() < deadline:
            if any(not _identity_current(item["identity"]) for item in children):
                break
            try:
                topology = topology_fn(spec)
            except (OSError, ProductionMixError, ValueError):
                topology = None
            if isinstance(topology, dict) and topology.get("complete") is True:
                break
            sleep_fn(0.05)
        if not isinstance(topology, dict) or topology.get("complete") is not True:
            raise ProductionMixError("production-mix topology did not become ready")
        ready = {
            "schema_version": 1,
            "kind": "audio_production_mix_ready",
            "session_id": spec["session_id"],
            "ready_at": utc_now(),
            "plan_sha256": spec["plan_sha256"],
            "children": [
                {
                    "role": item["role"],
                    "argv_sha256": item["argv_sha256"],
                    "identity": item["identity"],
                }
                for item in children
            ],
            "topology": topology,
            "does_not_establish": [
                "safe-monitoring-level",
                "subjective-recording-quality",
                "software-instrument-presence",
            ],
        }
        _write_private_json(
            pathlib.Path(spec["paths"]["ready"]), ready, create_only=True
        )
        failures = 0
        while not stop_requested:
            dead_roles = [
                item["role"]
                for item in children
                if not _identity_current(item["identity"])
            ]
            if dead_roles:
                raise ProductionMixError(
                    "loopback child exited or changed identity: "
                    + ", ".join(dead_roles)
                )
            try:
                current = topology_fn(spec)
            except (OSError, ProductionMixError, ValueError):
                current = {"complete": False, "blockers": ["topology-query-failed"]}
            if current.get("complete") is True:
                topology = current
                failures = 0
            else:
                failures += 1
                if failures >= process_contract["topology_failure_limit"]:
                    raise ProductionMixError(
                        "production-mix topology drifted: "
                        + ", ".join(str(x) for x in current.get("blockers", []))
                    )
            sleep_fn(float(process_contract["topology_poll_seconds"]))
    except Exception as exc:
        failure = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        terminated = _terminate_children(
            children, grace_seconds=float(process_contract["stop_grace_seconds"])
        )
        stderr = _stderr_receipts(
            children, int(process_contract["maximum_stderr_bytes_per_child"])
        )
        _close_child_handles(children)
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
    clean_stderr = all(item["truncated"] is False for item in stderr)
    if failure is None and stop_requested and terminated and clean_stderr:
        return {
            "schema_version": 1,
            "kind": "audio_production_mix_result",
            "session_id": spec["session_id"],
            "status": "stopped",
            "reason": "requested-stop",
            "started_at": started_at,
            "completed_at": utc_now(),
            "plan_sha256": spec["plan_sha256"],
            "children_terminated": True,
            "stderr": stderr,
            "last_topology_sha256": (
                topology.get("topology_sha256") if isinstance(topology, dict) else None
            ),
            "does_not_establish": [
                "safe-monitoring-level",
                "subjective-recording-quality",
                "persistent-PipeWire-configuration",
            ],
        }
    return {
        "schema_version": 1,
        "kind": "audio_production_mix_result",
        "session_id": spec["session_id"],
        "status": "failed",
        "reason": "worker-failure",
        "started_at": started_at,
        "completed_at": utc_now(),
        "plan_sha256": spec["plan_sha256"],
        "error": failure or "children could not be terminated through the bounded path",
        "children_terminated": terminated,
        "stderr": stderr,
        "last_topology_sha256": (
            topology.get("topology_sha256") if isinstance(topology, dict) else None
        ),
        "does_not_establish": [
            "complete-graph-teardown",
            "safe-monitoring-level",
            "successful-production-mix",
        ],
    }


def worker_entry(spec_path: pathlib.Path, expected_spec_sha256: str) -> int:
    result_path: pathlib.Path | None = None
    spec: dict[str, Any] | None = None
    try:
        spec = _read_json(spec_path, private=True)
        binding = _binding(spec_path, private=True)
        if binding["sha256"] != expected_spec_sha256:
            raise ProductionMixError("production-mix worker spec digest changed")
        validate_spec(spec, state_root=spec_path.parent)
        result_path = pathlib.Path(spec["paths"]["result"])
        result = worker_run(spec)
        _write_private_json(result_path, result, create_only=True)
        return 0 if result["status"] == "stopped" else 1
    except Exception as exc:
        if spec is not None and result_path is not None and not result_path.exists():
            failure = {
                "schema_version": 1,
                "kind": "audio_production_mix_result",
                "session_id": spec.get("session_id"),
                "status": "failed",
                "reason": "worker-entry-failure",
                "started_at": utc_now(),
                "completed_at": utc_now(),
                "plan_sha256": spec.get("plan_sha256"),
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                "children_terminated": False,
                "stderr": [],
                "last_topology_sha256": None,
                "does_not_establish": [
                    "complete-graph-teardown",
                    "safe-monitoring-level",
                    "successful-production-mix",
                ],
            }
            try:
                _write_private_json(result_path, failure, create_only=True)
            except Exception:
                pass
        return 1


def _service_limits_match(snapshot: dict[str, Any], contract: dict[str, Any]) -> bool:
    limits = snapshot.get("limits")
    process = contract["process"]
    return isinstance(limits, dict) and limits == {
        "memory_max_bytes": process["memory_max_bytes"],
        "tasks_max": process["tasks_max"],
        "limit_nofile": process["maximum_open_files"],
        "log_rate_limit_interval_usec": process["log_rate_limit_interval_seconds"]
        * 1_000_000,
        "log_rate_limit_burst": process["log_rate_limit_burst"],
    }


def _service_exact(
    snapshot: dict[str, Any], state: dict[str, Any], spec_sha256: str
) -> bool:
    return (
        snapshot.get("load_state") == "loaded"
        and snapshot.get("active_state")
        in {"active", "activating", "reloading", "deactivating"}
        and snapshot.get("managed") is True
        and snapshot.get("spec_sha256") == spec_sha256
        and snapshot.get("identity") == state.get("service_identity")
    )


def _validate_ready(value: dict[str, Any], spec: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "kind",
        "session_id",
        "ready_at",
        "plan_sha256",
        "children",
        "topology",
        "does_not_establish",
    }
    if (
        set(value) != required
        or value.get("schema_version") != 1
        or value.get("kind") != "audio_production_mix_ready"
        or value.get("session_id") != spec["session_id"]
        or value.get("plan_sha256") != spec["plan_sha256"]
        or not _valid_timestamp(value.get("ready_at"))
        or not isinstance(value.get("does_not_establish"), list)
        or not value["does_not_establish"]
    ):
        raise ProductionMixError("production-mix ready receipt is invalid")
    children = value.get("children")
    if not isinstance(children, list) or len(children) != 4:
        raise ProductionMixError("production-mix ready child set is invalid")
    expected_commands = _role_commands(spec)
    expected = {
        item["role"]: canonical_sha256(item["argv"]) for item in expected_commands
    }
    observed: set[str] = set()
    for child in children:
        if not isinstance(child, dict) or set(child) != {
            "role",
            "argv_sha256",
            "identity",
        }:
            raise ProductionMixError("production-mix ready child fields are invalid")
        role = child.get("role")
        if role not in expected or role in observed:
            raise ProductionMixError("production-mix ready child role is invalid")
        if child.get("argv_sha256") != expected[role]:
            raise ProductionMixError("production-mix child command binding changed")
        try:
            REC._validate_process_identity(child.get("identity"))
        except REC.RecordingError as exc:
            raise ProductionMixError(
                "production-mix child identity is invalid"
            ) from exc
        observed.add(role)
    topology = value.get("topology")
    if (
        not isinstance(topology, dict)
        or topology.get("complete") is not True
        or not isinstance(topology.get("topology_sha256"), str)
        or HEX64_RE.fullmatch(topology["topology_sha256"]) is None
    ):
        raise ProductionMixError("production-mix ready topology is invalid")


def _validate_result(value: dict[str, Any], spec: dict[str, Any]) -> None:
    common = {
        "schema_version",
        "kind",
        "session_id",
        "status",
        "reason",
        "started_at",
        "completed_at",
        "plan_sha256",
        "children_terminated",
        "stderr",
        "last_topology_sha256",
        "does_not_establish",
    }
    status = value.get("status")
    expected = (
        common if status in {"stopped", "failed-recovered"} else common | {"error"}
    )
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or value.get("kind") != "audio_production_mix_result"
        or value.get("session_id") != spec["session_id"]
        or value.get("plan_sha256") != spec["plan_sha256"]
        or status not in {"stopped", "failed", "failed-recovered"}
        or not _valid_text(value.get("reason"), maximum=100)
        or not _valid_timestamp(value.get("started_at"))
        or not _valid_timestamp(value.get("completed_at"))
        or dt.datetime.fromisoformat(value["completed_at"])
        < dt.datetime.fromisoformat(value["started_at"])
        or not isinstance(value.get("children_terminated"), bool)
        or not isinstance(value.get("stderr"), list)
        or not isinstance(value.get("does_not_establish"), list)
        or not value["does_not_establish"]
    ):
        raise ProductionMixError("production-mix result is invalid")
    if status == "stopped" and (
        value.get("reason") != "requested-stop"
        or value.get("children_terminated") is not True
        or len(value["stderr"]) != 4
    ):
        raise ProductionMixError("clean production-mix result is invalid")
    if status == "failed" and not _valid_text(value.get("error"), maximum=600):
        raise ProductionMixError("failed production-mix result lacks an error")
    topology_sha = value.get("last_topology_sha256")
    if topology_sha is not None and (
        not isinstance(topology_sha, str) or HEX64_RE.fullmatch(topology_sha) is None
    ):
        raise ProductionMixError("production-mix result topology digest is invalid")
    for item in value["stderr"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "bytes", "sha256", "truncated"}
            or not _valid_text(item.get("role"), maximum=100)
            or not isinstance(item.get("bytes"), int)
            or item["bytes"] < 0
            or not isinstance(item.get("sha256"), str)
            or HEX64_RE.fullmatch(item["sha256"]) is None
            or not isinstance(item.get("truncated"), bool)
        ):
            raise ProductionMixError("production-mix stderr receipt is invalid")


def _clear_active(state_root: pathlib.Path, session_id: str) -> None:
    active = REC.lexical_absolute(state_root) / "active.json"
    if not active.exists() and not active.is_symlink():
        return
    value = _read_json(active, private=True)
    if value.get("session_id") != session_id:
        raise ProductionMixError(
            "active production-mix pointer belongs to another session"
        )
    active.unlink()
    descriptor = os.open(active.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def init_state(state_root: pathlib.Path = DEFAULT_STATE_ROOT) -> dict[str, Any]:
    root = ensure_private_directory(state_root)
    return {
        "schema_version": 1,
        "kind": "audio_production_mix_initialization_receipt",
        "state_root": str(root),
        "mode": "0700",
        "audio_effect": False,
    }


def _raw_node_names(contract: dict[str, Any]) -> dict[str, str]:
    voice = REC._source_name_for_contract(REC.load_catalog("voice-recording")["source"])
    roland = REC._source_name_for_contract(
        REC.load_catalog("roland-audio-recording")["source"]
    )
    monitor = _motu_sink_snapshot(contract["monitor_target"])
    monitor_name = monitor.get("node_name")
    if monitor.get("complete") is not True or not isinstance(monitor_name, str):
        raise ProductionMixError("MOTU monitor sink is missing or ambiguous")
    return {
        "voice_source": voice,
        "roland_source": roland,
        "monitor_sink": monitor_name,
    }


def _systemd_run_command(spec_path: pathlib.Path, spec_sha256: str) -> list[str]:
    contract = load_contract()
    process = contract["process"]
    return [
        "systemd-run",
        "--user",
        "--collect",
        "--quiet",
        "--service-type=exec",
        "--unit",
        contract["unit"].removesuffix(".service"),
        "--setenv",
        f"{MANAGED_MARKER_ENV}=1",
        "--setenv",
        f"{SPEC_SHA_ENV}={spec_sha256}",
        "--property",
        "Description=Audio Production Mix v1",
        "--property",
        "Restart=no",
        "--property",
        "KillMode=control-group",
        "--property",
        f"TimeoutStartSec={process['startup_timeout_seconds']}s",
        "--property",
        f"TimeoutStopSec={process['stop_grace_seconds'] + 5}s",
        "--property",
        f"MemoryMax={process['memory_max_bytes']}",
        "--property",
        f"CPUQuota={process['cpu_quota_percent']}%",
        "--property",
        f"TasksMax={process['tasks_max']}",
        "--property",
        f"LimitNOFILE={process['maximum_open_files']}",
        "--property",
        f"RuntimeMaxSec={process['runtime_max_seconds']}s",
        "--property",
        f"LogRateLimitIntervalSec={process['log_rate_limit_interval_seconds']}s",
        "--property",
        f"LogRateLimitBurst={process['log_rate_limit_burst']}",
        sys.executable,
        str(pathlib.Path(__file__).resolve()),
        "_worker",
        "--spec",
        str(spec_path),
        "--expected-spec-sha256",
        spec_sha256,
    ]


def start_graph(
    expected_plan_sha256: str,
    *,
    state_root: pathlib.Path = DEFAULT_STATE_ROOT,
    physical_state: pathlib.Path = REC.PHYSICAL.DEFAULT_STATE,
    laboratory_state: pathlib.Path = REC.LAB.DEFAULT_STATE,
    plan_fn: Callable[..., dict[str, Any]] = build_plan,
    raw_nodes_fn: Callable[[dict[str, Any]], dict[str, str]] = _raw_node_names,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_capture,
    service_fn: Callable[[], dict[str, Any]] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if (
        not isinstance(expected_plan_sha256, str)
        or HEX64_RE.fullmatch(expected_plan_sha256) is None
    ):
        raise ProductionMixError("expected production-mix plan digest is invalid")
    root = REC.lexical_absolute(state_root)
    contract = load_contract()
    with REC.state_lock(root):
        plan = plan_fn(
            state_root=root,
            physical_state=physical_state,
            laboratory_state=laboratory_state,
        )
        if plan.get("plan_sha256") != expected_plan_sha256:
            raise ProductionMixError(
                "production-mix plan changed; review the new plan before start"
            )
        if plan.get("ready") is not True:
            raise ProductionMixError(
                "production-mix plan is blocked: "
                + ", ".join(plan.get("readiness", {}).get("blockers", []))
            )
        raw_nodes = raw_nodes_fn(contract)
        identities = {
            "voice_source": plan["identity"]["voice_source"]["identity"],
            "roland_source": plan["identity"]["roland_source"]["identity"],
            "monitor_sink": plan["identity"]["monitor_sink"]["identity"],
        }
        for key, identity in identities.items():
            if hashlib.sha256(raw_nodes[key].encode()).hexdigest() != identity.get(
                "node_name_sha256"
            ):
                raise ProductionMixError(
                    f"production-mix {key} changed between plan and start"
                )
        session_id = secrets.token_hex(12)
        paths = _session_paths(root, session_id)
        spec = {
            "schema_version": 1,
            "kind": "audio_production_mix_spec",
            "session_id": session_id,
            "created_at": utc_now(),
            "plan_sha256": plan["plan_sha256"],
            "plan_identity": plan["identity"],
            "raw_nodes": raw_nodes,
            "paths": {
                "ready": str(paths["ready"]),
                "result": str(paths["result"]),
            },
        }
        _write_private_json(paths["spec"], spec, create_only=True)
        spec_sha = _binding(paths["spec"], private=True)["sha256"]
        state = {
            "schema_version": 1,
            "kind": "audio_production_mix_state",
            "session_id": session_id,
            "spec_sha256": spec_sha,
            "started_at": utc_now(),
            "phase": "starting",
            "service_identity": None,
        }
        _write_private_json(paths["state"], state, create_only=True)
        _write_private_json(
            paths["active"],
            {
                "schema_version": 1,
                "kind": "audio_production_mix_active",
                "session_id": session_id,
                "spec_sha256": spec_sha,
            },
            create_only=True,
        )
        command = _systemd_run_command(paths["spec"], spec_sha)
        result = runner(command)
        if result.returncode != 0:
            detail = (
                result.stderr.strip() or result.stdout.strip() or str(result.returncode)
            )
            raise ProductionMixError(
                f"production-mix service start failed; recover session {session_id}: {detail}"
            )
        current_service: dict[str, Any] | None = None
        for _attempt in range(200):
            current_service = (
                service_fn()
                if service_fn is not None
                else service_snapshot(contract["unit"])
            )
            if (
                current_service.get("load_state") == "loaded"
                and current_service.get("active_state") == "active"
                and current_service.get("managed") is True
                and current_service.get("spec_sha256") == spec_sha
                and isinstance(current_service.get("identity"), dict)
            ):
                state["phase"] = "running"
                state["service_identity"] = current_service["identity"]
                _write_private_json(paths["state"], state)
                if not _service_limits_match(current_service, contract):
                    raise ProductionMixError(
                        f"production-mix service limits do not match; stop session {session_id}"
                    )
                break
            if current_service.get("active_state") in {"failed", "inactive"}:
                break
            sleep_fn(0.05)
        else:
            current_service = None
        if state["phase"] != "running":
            raise ProductionMixError(
                f"production-mix service identity was not established; recover session {session_id}"
            )
    deadline = time.monotonic() + float(contract["process"]["startup_timeout_seconds"])
    while time.monotonic() < deadline:
        if paths["result"].exists() or paths["result"].is_symlink():
            result_value = _read_json(paths["result"], private=True)
            _validate_result(result_value, spec)
            raise ProductionMixError(
                f"production-mix worker failed during start; recover session {session_id}"
            )
        if paths["ready"].exists() or paths["ready"].is_symlink():
            ready = _read_json(paths["ready"], private=True)
            _validate_ready(ready, spec)
            current_topology = graph_topology_snapshot(spec)
            if current_topology.get("complete") is True:
                return {
                    "schema_version": 1,
                    "kind": "audio_production_mix_start_receipt",
                    "session_id": session_id,
                    "status": "ready",
                    "plan_sha256": plan["plan_sha256"],
                    "unit": contract["unit"],
                    "virtual_sink": contract["graph"]["virtual_sink"]["node_name"],
                    "virtual_source": contract["graph"]["virtual_source"]["node_name"],
                    "topology_sha256": current_topology["topology_sha256"],
                }
        sleep_fn(0.05)
    raise ProductionMixError(
        f"production-mix graph did not become ready; recover session {session_id}"
    )


def graph_status(
    *,
    state_root: pathlib.Path = DEFAULT_STATE_ROOT,
    session_id: str | None = None,
    service_fn: Callable[[], dict[str, Any]] | None = None,
    topology_fn: Callable[[dict[str, Any]], dict[str, Any]] = graph_topology_snapshot,
) -> dict[str, Any]:
    root = REC.lexical_absolute(state_root)
    resolved = session_id or _read_active(root)
    paths, spec, state = _read_session(root, resolved)
    spec_sha = _binding(paths["spec"], private=True)["sha256"]
    contract = load_contract()
    service = (
        service_fn() if service_fn is not None else service_snapshot(contract["unit"])
    )
    exact = _service_exact(service, state, spec_sha)
    result: dict[str, Any] | None = None
    if paths["result"].exists() or paths["result"].is_symlink():
        result = _read_json(paths["result"], private=True)
        _validate_result(result, spec)
    ready: dict[str, Any] | None = None
    children_exact = False
    if paths["ready"].exists() or paths["ready"].is_symlink():
        ready = _read_json(paths["ready"], private=True)
        _validate_ready(ready, spec)
        children_exact = all(
            _identity_current(item["identity"]) for item in ready["children"]
        )
    topology: dict[str, Any] | None = None
    if exact:
        try:
            topology = topology_fn(spec)
        except (OSError, ProductionMixError, ValueError) as exc:
            topology = {
                "complete": False,
                "blockers": ["topology-query-failed"],
                "error": str(exc),
            }
    if result is not None:
        status = result["status"]
        recovery_required = False
    elif (
        exact
        and ready is not None
        and children_exact
        and topology
        and topology.get("complete") is True
    ):
        status = "ready"
        recovery_required = False
    elif exact:
        status = "starting-or-degraded"
        recovery_required = False
    elif service.get("active_state") in {
        "active",
        "activating",
        "reloading",
        "deactivating",
    }:
        status = "identity-mismatch"
        recovery_required = True
    else:
        status = "recovery-required"
        recovery_required = True
    return {
        "schema_version": 1,
        "kind": "audio_production_mix_status",
        "session_id": resolved,
        "status": status,
        "recovery_required": recovery_required,
        "service_identity_exact": exact,
        "child_identities_exact": children_exact,
        "plan_sha256": spec["plan_sha256"],
        "service": service,
        "topology": topology,
        "result": result,
    }


def stop_graph(
    *,
    state_root: pathlib.Path = DEFAULT_STATE_ROOT,
    session_id: str | None = None,
    service_fn: Callable[[], dict[str, Any]] | None = None,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run_capture,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    root = REC.lexical_absolute(state_root)
    contract = load_contract()
    with REC.state_lock(root):
        resolved = session_id or _read_active(root)
        paths, spec, state = _read_session(root, resolved)
        if paths["result"].exists() or paths["result"].is_symlink():
            result = graph_status(
                state_root=root, session_id=resolved, service_fn=service_fn
            )
            _clear_active(root, resolved)
            return result
        spec_sha = _binding(paths["spec"], private=True)["sha256"]
        service = (
            service_fn()
            if service_fn is not None
            else service_snapshot(contract["unit"])
        )
        if not _service_exact(service, state, spec_sha):
            raise ProductionMixError(
                "production-mix service identity is not exact; use recovery"
            )
        result = runner(["systemctl", "--user", "stop", contract["unit"]])
        if result.returncode != 0:
            detail = (
                result.stderr.strip() or result.stdout.strip() or str(result.returncode)
            )
            raise ProductionMixError(f"production-mix stop failed: {detail}")
        deadline = time.monotonic() + float(
            contract["process"]["stop_grace_seconds"] + 5
        )
        while time.monotonic() < deadline:
            current = (
                service_fn()
                if service_fn is not None
                else service_snapshot(contract["unit"])
            )
            if (
                current.get("active_state") in {"inactive", "failed"}
                or current.get("load_state") == "not-found"
            ):
                break
            sleep_fn(0.05)
        else:
            raise ProductionMixError("production-mix service did not stop in time")
        result_value: dict[str, Any] | None = None
        for _attempt in range(100):
            if paths["result"].exists() or paths["result"].is_symlink():
                result_value = _read_json(paths["result"], private=True)
                _validate_result(result_value, spec)
                break
            sleep_fn(0.05)
        if result_value is None:
            raise ProductionMixError(
                "production-mix service stopped without a terminal receipt; use recovery"
            )
        _clear_active(root, resolved)
        return graph_status(state_root=root, session_id=resolved, service_fn=service_fn)


def recover_graph(
    *,
    state_root: pathlib.Path = DEFAULT_STATE_ROOT,
    session_id: str | None = None,
    service_fn: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = REC.lexical_absolute(state_root)
    contract = load_contract()
    with REC.state_lock(root):
        resolved = session_id or _read_active(root)
        paths, spec, state = _read_session(root, resolved)
        spec_sha = _binding(paths["spec"], private=True)["sha256"]
        service = (
            service_fn()
            if service_fn is not None
            else service_snapshot(contract["unit"])
        )
        if _service_exact(service, state, spec_sha):
            return graph_status(
                state_root=root, session_id=resolved, service_fn=service_fn
            )
        if service.get("active_state") in {
            "active",
            "activating",
            "reloading",
            "deactivating",
        }:
            raise ProductionMixError(
                "a foreign or changed production-mix service is active; recovery remains fail-closed"
            )
        if not paths["result"].exists() and not paths["result"].is_symlink():
            recovered = {
                "schema_version": 1,
                "kind": "audio_production_mix_result",
                "session_id": resolved,
                "status": "failed-recovered",
                "reason": "service-exited-without-terminal-receipt",
                "started_at": state["started_at"],
                "completed_at": utc_now(),
                "plan_sha256": spec["plan_sha256"],
                "children_terminated": False,
                "stderr": [],
                "last_topology_sha256": None,
                "does_not_establish": [
                    "complete-graph-teardown",
                    "safe-monitoring-level",
                    "successful-production-mix",
                ],
            }
            _write_private_json(paths["result"], recovered, create_only=True)
        else:
            existing = _read_json(paths["result"], private=True)
            _validate_result(existing, spec)
        _clear_active(root, resolved)
        return graph_status(state_root=root, session_id=resolved, service_fn=service_fn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--state-root", type=pathlib.Path, default=DEFAULT_STATE_ROOT)
    for command in ("plan", "start"):
        item = sub.add_parser(command)
        item.add_argument("--state-root", type=pathlib.Path, default=DEFAULT_STATE_ROOT)
        item.add_argument(
            "--physical-state",
            type=pathlib.Path,
            default=REC.PHYSICAL.DEFAULT_STATE,
        )
        item.add_argument(
            "--laboratory-state",
            type=pathlib.Path,
            default=REC.LAB.DEFAULT_STATE,
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "_worker":
        return worker_entry(args.spec, args.expected_spec_sha256)
    try:
        if args.command == "init":
            result = init_state(args.state_root)
        elif args.command == "plan":
            result = build_plan(
                state_root=args.state_root,
                physical_state=args.physical_state,
                laboratory_state=args.laboratory_state,
            )
        elif args.command == "start":
            result = start_graph(
                args.expected_plan_sha256,
                state_root=args.state_root,
                physical_state=args.physical_state,
                laboratory_state=args.laboratory_state,
            )
        elif args.command == "status":
            result = graph_status(
                state_root=args.state_root, session_id=args.session_id
            )
        elif args.command == "stop":
            result = stop_graph(state_root=args.state_root, session_id=args.session_id)
        else:
            result = recover_graph(
                state_root=args.state_root, session_id=args.session_id
            )
    except (
        OSError,
        ProductionMixError,
        REC.RecordingError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_production_mix_error",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
