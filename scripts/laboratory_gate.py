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
QOBUZ_RATE_OBSERVER_PATH = ROOT / "scripts" / "qobuz_rate_observer.py"
VOICE_CAPTURE_OBSERVER_PATH = ROOT / "scripts" / "voice_capture_observer.py"
LEVEL_ANALYZER_PATH = ROOT / "scripts" / "level_analyzer.py"
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
MAX_QOBUZ_JOURNAL_LINES = 5_000
QOBUZ_URI_RE = re.compile(r"^qobuz:track:(?P<track_id>[0-9]+)$")
QOBUZ_METHOD = "bounded-mopidy-qobuz-pulse-pipewire-observation-v1"
QOBUZ_RPC_PAYLOAD = (
    {"jsonrpc": "2.0", "id": 1, "method": "core.playback.get_state"},
    {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "core.playback.get_current_track",
    },
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "core.playback.get_time_position",
    },
)
QOBUZ_PACTL_INFO_ARGV = ("pactl", "--format=json", "info")
QOBUZ_PACTL_SINKS_ARGV = ("pactl", "--format=json", "list", "sinks")
QOBUZ_PACTL_INPUTS_ARGV = ("pactl", "--format=json", "list", "sink-inputs")
VOICE_CAPTURE_METHOD = "bounded-motu-serial-live-voice-capture-v1"
VOICE_PACTL_SOURCES_ARGV = ("pactl", "--format=json", "list", "sources")
VOICE_PARECORD_FIXED_ARGUMENTS = (
    "--record",
    "--rate=48000",
    "--format=s32le",
    "--channels=2",
    "--no-remix",
    "--file-format=wav",
    "--client-name=audio-voice-reference",
    "--stream-name=voice-reference",
)
VOICE_MIN_CAPTURE_SECONDS = 8
VOICE_MAX_CAPTURE_SECONDS = 20
VOICE_MAX_STDERR_BYTES = 65_536
VOICE_STARTUP_TIMEOUT_SECONDS = 5
RATE_POLICY_OBSERVER_PATH = ROOT / "scripts" / "rate_policy_observer.py"
RATE_POLICY_METHOD = "bound-audio-rate-policy-v1"
RATE_POLICY_PACTL_SOURCES_ARGV = ("pactl", "--format=json", "list", "sources")
RATE_POLICY_PACTL_SINKS_ARGV = ("pactl", "--format=json", "list", "sinks")
RATE_POLICY_PROFILE_NAMES = (
    "desktop-mixed",
    "reference-listening",
    "voice-recording",
    "piano-digital-recording",
    "qobuz-exclusive",
)
RATE_POLICY_PROFILE_CONTRACT = {
    "desktop-mixed": {
        "desired": {
            "default_sink": "motu-m2",
            "rate_hz": 48_000,
            "quantum_frames": 1024,
            "resampling": "allowed",
        },
        "required_laboratory_gates": [],
        "operational_status": "available",
    },
    "reference-listening": {
        "desired": {
            "default_sink": "motu-m2",
            "rate_policy": "measure-before-decision",
            "quantum_frames": 1024,
            "dsp": "bypass",
        },
        "required_laboratory_gates": ["rate-policy-decision"],
        "operational_status": "available",
    },
    "voice-recording": {
        "desired": {
            "default_sink": "motu-m2",
            "default_source": "motu-m2",
            "rate_hz": 48_000,
            "quantum_candidate_frames": 512,
            "recording_peak_dbfs": [-12, -6],
        },
        "required_laboratory_gates": ["voice-level-measurement"],
        "operational_status": "available",
    },
    "piano-digital-recording": {
        "desired": {
            "default_sink": "motu-m2",
            "rate_hz": 48_000,
            "quantum_candidate_frames": 512,
            "roland_audio_rate_hz": 44_100,
        },
        "required_laboratory_gates": ["resampling-decision"],
        "operational_status": "available",
    },
    "qobuz-exclusive": {
        "desired": {
            "rate_policy": "track-native-if-proven",
            "parallel_mixing": "forbidden",
        },
        "required_laboratory_gates": ["qobuz-rate-proof"],
        "operational_status": "available",
    },
}
RATE_POLICY_DECISIONS = {
    "rate-policy-decision": "fixed-48k-default-track-native-exclusive-if-proven",
    "resampling-decision": "roland-44k1-single-stage-to-48k",
}
RATE_POLICY_JUSTIFICATIONS = {
    "rate-policy-decision": (
        "Use a stable 48 kHz graph for mixed playback, recording and software "
        "instruments. Permit track-native Qobuz only in the exclusive profile "
        "after a matching qobuz-rate-proof; otherwise fall back to 48 kHz."
    ),
    "resampling-decision": (
        "Convert Roland FP-30X digital audio once from its observed 44.1 kHz "
        "endpoint rate into the 48 kHz recording graph. Keep MIDI independent "
        "and forbid an additional intentional resampling stage."
    ),
}
RATE_POLICY_PAYLOADS = {
    "rate-policy-decision": {
        "default_graph_rate_hz": 48_000,
        "mixed_playback_rate_hz": 48_000,
        "reference_listening_rate_hz": 48_000,
        "qobuz_exclusive": {
            "mode": "track-native-if-proven",
            "requires_gate": "qobuz-rate-proof",
            "parallel_mixing": "forbidden",
            "fallback_rate_hz": 48_000,
        },
        "automatic_rate_switching_without_proof": False,
    },
    "resampling-decision": {
        "source_device": "roland_fp_30x",
        "source_rate_hz": 44_100,
        "target_graph_rate_hz": 48_000,
        "resampling_stage": "pipewire-single-stage",
        "midi_path_resampled": False,
        "additional_intentional_resampling_stage": "forbidden",
        "qobuz_policy_independent": True,
    },
}
RATE_POLICY_CRITERIA = {
    "required_graph_rate_hz": 48_000,
    "required_motu_endpoint_rate_hz": 48_000,
    "required_roland_endpoint_rate_hz": 44_100,
    "required_profiles": list(RATE_POLICY_PROFILE_NAMES),
    "qobuz_native_requires_gate": "qobuz-rate-proof",
    "parallel_mixing_at_track_native": False,
    "single_roland_resampling_stage": True,
}


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


def qobuz_journal_argv(started_at: str, ended_at: str) -> tuple[str, ...]:
    started = parse_timestamp(started_at, "Qobuz wait start")
    ended = parse_timestamp(ended_at, "Qobuz observation end")
    if ended <= started:
        raise ValueError("Qobuz observation end must follow the wait start")
    return (
        "journalctl",
        "--user",
        "-u",
        "mopidy.service",
        "--since",
        journal_timestamp(started, round_up=False),
        "--until",
        journal_timestamp(ended, round_up=True),
        "--no-pager",
        "--output=json",
        "-n",
        str(MAX_QOBUZ_JOURNAL_LINES + 1),
    )


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


def has_bound_voice_capture(evidence: dict[str, Any]) -> bool:
    return isinstance(evidence.get("capture_observation"), dict) and isinstance(
        evidence.get("implementation"), dict
    )


def _validate_voice_source_snapshot(snapshot: Any, label: str) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValueError(f"{label} voice source snapshot is missing")
    if (
        snapshot.get("schema_version") != 1
        or snapshot.get("kind") != "audio_voice_source_snapshot"
    ):
        raise ValueError(f"{label} voice source snapshot schema is invalid")
    parse_timestamp(snapshot.get("observed_at"), f"{label} voice source timestamp")
    if (
        snapshot.get("complete") is not True
        or snapshot.get("present") is not True
        or snapshot.get("match_count") != 1
        or snapshot.get("ambiguous") is not False
        or snapshot.get("errors") != []
    ):
        raise ValueError(f"{label} MOTU source is not uniquely ready")
    identity = snapshot.get("identity")
    if not isinstance(identity, dict):
        raise ValueError(f"{label} MOTU source identity is missing")
    if identity.get("vendor_id") != "07fd" or identity.get("product_id") != "0008":
        raise ValueError(f"{label} source is not a MOTU M2")
    for field in ("serial_sha256", "node_name_sha256", "bus_path_sha256"):
        _sha256(identity.get(field), f"{label} {field}")
    if identity.get("sample_format") != "s32le":
        raise ValueError(f"{label} MOTU source is not s32le")
    if _positive_int(identity.get("sample_rate_hz"), f"{label} source rate") != 48_000:
        raise ValueError(f"{label} MOTU source is not 48 kHz")
    if _positive_int(identity.get("channels"), f"{label} source channels") != 2:
        raise ValueError(f"{label} MOTU source is not stereo")
    if identity.get("muted") is not False:
        raise ValueError(f"{label} MOTU source is muted")
    if identity.get("unity_volume") is not True:
        raise ValueError(f"{label} MOTU source is not at unity volume")
    fingerprint = _sha256(identity.get("fingerprint"), f"{label} source fingerprint")
    if fingerprint != canonical_value_sha256(
        {key: value for key, value in identity.items() if key != "fingerprint"}
    ):
        raise ValueError(f"{label} MOTU source fingerprint mismatch")
    query = snapshot.get("query")
    if not isinstance(query, dict):
        raise ValueError(f"{label} MOTU source query is missing")
    expected_argv = list(VOICE_PACTL_SOURCES_ARGV)
    if query.get("argv") != expected_argv:
        raise ValueError(f"{label} MOTU source query vector mismatch")
    if _sha256(query.get("argv_sha256"), f"{label} source query digest") != (
        canonical_value_sha256(expected_argv)
    ):
        raise ValueError(f"{label} MOTU source query digest mismatch")
    if query.get("returncode") != 0 or query.get("complete") is not True:
        raise ValueError(f"{label} MOTU source query is incomplete")
    _sha256(query.get("stdout_sha256"), f"{label} source output digest")
    _nonnegative_int(query.get("stdout_total_bytes"), f"{label} source output bytes")
    _sha256(query.get("stderr_sha256"), f"{label} source stderr digest")
    observation_sha = _sha256(
        snapshot.get("observation_sha256"), f"{label} source observation digest"
    )
    expected_observation = canonical_value_sha256(
        {key: value for key, value in snapshot.items() if key != "observation_sha256"}
    )
    if observation_sha != expected_observation:
        raise ValueError(f"{label} source observation digest mismatch")
    return identity


def validate_bound_voice_capture(evidence: dict[str, Any]) -> None:
    observation = evidence.get("capture_observation")
    if not isinstance(observation, dict):
        raise ValueError("voice evidence has no bounded live capture")
    if observation.get("method") != VOICE_CAPTURE_METHOD:
        raise ValueError("voice capture method does not match the contract")
    before = _validate_voice_source_snapshot(observation.get("before"), "before")
    after = _validate_voice_source_snapshot(observation.get("after"), "after")
    if before.get("fingerprint") != after.get("fingerprint"):
        raise ValueError("MOTU source identity changed during voice capture")
    if observation.get("stable_source_identity") is not True:
        raise ValueError("voice capture does not assert stable MOTU identity")
    process = observation.get("process")
    if not isinstance(process, dict):
        raise ValueError("voice capture process evidence is missing")
    if process.get("method") != VOICE_CAPTURE_METHOD:
        raise ValueError("voice capture process method mismatch")
    requested = _positive_int(
        process.get("requested_duration_seconds"),
        "voice requested duration",
    )
    if requested < VOICE_MIN_CAPTURE_SECONDS or requested > VOICE_MAX_CAPTURE_SECONDS:
        raise ValueError("voice capture duration is outside 8 to 20 seconds")
    started = parse_timestamp(process.get("capture_started_at"), "voice capture start")
    ended = parse_timestamp(process.get("capture_ended_at"), "voice capture end")
    if ended <= started:
        raise ValueError("voice capture timestamps are not ordered")
    if process.get("stream_ready") is not True:
        raise ValueError("voice capture stream never became ready")
    ready_at = parse_timestamp(
        process.get("stream_ready_at"), "voice capture stream ready"
    )
    if ready_at <= started or ready_at >= ended:
        raise ValueError("voice capture readiness timestamp is outside the process")
    startup = _number(process.get("startup_seconds"), "voice capture startup")
    if startup > VOICE_STARTUP_TIMEOUT_SECONDS + 0.5:
        raise ValueError("voice capture startup exceeds the timeout")
    measured_startup = (ready_at - started).total_seconds()
    if abs(measured_startup - startup) > 1.0:
        raise ValueError("voice capture startup contradicts its timestamps")
    duration = _number(process.get("duration_seconds"), "voice capture duration")
    if duration < startup + requested - 0.5 or duration > startup + requested + 5:
        raise ValueError("voice capture process duration contradicts the request")
    command = process.get("command")
    if not isinstance(command, dict):
        raise ValueError("voice capture command binding is missing")
    if command.get("executable") != "/usr/bin/parecord":
        raise ValueError("voice capture executable is not canonical")
    if command.get("fixed_arguments") != list(VOICE_PARECORD_FIXED_ARGUMENTS):
        raise ValueError("voice capture fixed arguments do not match")
    if command.get("device_name_sha256") != before.get("node_name_sha256"):
        raise ValueError("voice capture command targets another source")
    if command.get("output_role") != "private-temporary-wav":
        raise ValueError("voice capture output role is invalid")
    command_sha = _sha256(
        command.get("contract_sha256"), "voice capture command contract digest"
    )
    if command_sha != canonical_value_sha256(
        {key: value for key, value in command.items() if key != "contract_sha256"}
    ):
        raise ValueError("voice capture command contract digest mismatch")
    if process.get("accepted_returncodes") != [0, -2]:
        raise ValueError("voice capture accepted returncodes mismatch")
    if process.get("returncode") not in {0, -2}:
        raise ValueError("voice capture process did not exit cleanly")
    if process.get("forced_kill") is not False or process.get("complete") is not True:
        raise ValueError("voice capture process is incomplete")
    stderr_bytes = _nonnegative_int(
        process.get("stderr_bytes"), "voice capture stderr bytes"
    )
    if stderr_bytes > VOICE_MAX_STDERR_BYTES:
        raise ValueError("voice capture stderr exceeds the byte limit")
    _sha256(process.get("stderr_sha256"), "voice capture stderr digest")
    if process.get("stderr_truncated") is not False:
        raise ValueError("voice capture stderr is truncated")
    analysis = evidence.get("analysis")
    if not isinstance(analysis, dict):
        raise ValueError("voice capture analysis is missing")
    if analysis.get("sample_rate_hz") != 48_000:
        raise ValueError("voice capture WAV is not 48 kHz")
    if analysis.get("channels") != 2 or analysis.get("bit_depth") != 32:
        raise ValueError("voice capture WAV is not stereo 32-bit PCM")
    wav_duration = _number(
        analysis.get("duration_seconds"), "voice capture WAV duration"
    )
    if wav_duration < requested - 1 or wav_duration > requested + 2:
        raise ValueError("voice capture WAV duration contradicts the request")
    if evidence.get("blockers") != []:
        raise ValueError("passing voice capture contains blockers")
    criteria = evidence.get("criteria")
    expected_criteria = {
        "peak_dbfs_range": [-12.0, -6.0],
        "maximum_clipped_samples_per_channel": 0,
        "minimum_capture_duration_seconds": VOICE_MIN_CAPTURE_SECONDS,
        "maximum_capture_duration_seconds": VOICE_MAX_CAPTURE_SECONDS,
        "maximum_startup_seconds": VOICE_STARTUP_TIMEOUT_SECONDS,
        "required_sample_rate_hz": 48_000,
        "required_channels": 2,
        "required_bit_depth": 32,
        "requires_motu_serial_identity": True,
        "requires_unity_capture_volume": True,
        "requires_stable_source_identity": True,
    }
    if criteria != expected_criteria:
        raise ValueError("voice capture criteria do not match the contract")
    implementation = evidence.get("implementation")
    if not isinstance(implementation, dict):
        raise ValueError("voice capture implementation binding is missing")
    expected_implementation = {
        "voice_capture_observer_sha256": sha256_file(VOICE_CAPTURE_OBSERVER_PATH),
        "laboratory_gate_sha256": sha256_file(pathlib.Path(__file__)),
        "level_analyzer_sha256": sha256_file(LEVEL_ANALYZER_PATH),
        "system_truth_sha256": sha256_file(SYSTEM_TRUTH_PATH),
    }
    if implementation != expected_implementation:
        raise ValueError("voice capture implementation binding changed")


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


def has_bound_policy_decision(evidence: dict[str, Any]) -> bool:
    return all(
        key in evidence
        for key in (
            "method",
            "policy",
            "truth",
            "endpoints",
            "profiles",
            "criteria",
            "implementation",
            "blockers",
        )
    )


def _validate_rate_policy_query(
    binding: Any,
    expected_argv: tuple[str, ...],
    label: str,
    *,
    require_current: bool = True,
) -> None:
    if not isinstance(binding, dict):
        raise ValueError(f"rate-policy {label} query binding is missing")
    observed_argv = binding.get("query_argv")
    if (
        not isinstance(observed_argv, list)
        or not observed_argv
        or any(not isinstance(item, str) or not item for item in observed_argv)
    ):
        raise ValueError(f"rate-policy {label} query vector is invalid")
    if require_current and observed_argv != list(expected_argv):
        raise ValueError(f"rate-policy {label} query vector mismatch")
    observed_digest = _sha256(
        binding.get("query_argv_sha256"),
        f"rate-policy {label} query digest",
    )
    if observed_digest != canonical_value_sha256(observed_argv):
        raise ValueError(f"rate-policy {label} query digest mismatch")
    if binding.get("returncode") != 0 or binding.get("complete") is not True:
        raise ValueError(f"rate-policy {label} query is incomplete")
    _sha256(binding.get("stdout_sha256"), f"rate-policy {label} stdout digest")
    _nonnegative_int(
        binding.get("stdout_total_bytes"),
        f"rate-policy {label} stdout bytes",
    )
    _sha256(binding.get("stderr_sha256"), f"rate-policy {label} stderr digest")
    _nonnegative_int(
        binding.get("stderr_total_bytes"),
        f"rate-policy {label} stderr bytes",
    )


def _validate_rate_endpoint_snapshot(
    snapshot: Any,
    *,
    require_current: bool = True,
) -> None:
    if not isinstance(snapshot, dict):
        raise ValueError("rate-policy endpoint snapshot is missing")
    if (
        snapshot.get("schema_version") != 1
        or snapshot.get("kind") != "audio_rate_endpoint_snapshot"
    ):
        raise ValueError("rate-policy endpoint snapshot schema is invalid")
    parse_timestamp(snapshot.get("observed_at"), "rate-policy endpoint timestamp")
    if snapshot.get("complete") is not True or snapshot.get("blockers") != []:
        raise ValueError("rate-policy endpoint snapshot is incomplete")
    counts = snapshot.get("counts")
    rates = snapshot.get("rate_sets_hz")
    endpoints = snapshot.get("endpoints")
    queries = snapshot.get("queries")
    if not all(isinstance(value, dict) for value in (counts, rates, queries)):
        raise ValueError("rate-policy endpoint projections are invalid")
    if not isinstance(endpoints, list):
        raise ValueError("rate-policy endpoint list is invalid")
    devices = ("motu_m2", "roland_fp_30x")
    if set(counts) != set(devices) or set(rates) != set(devices):
        raise ValueError("rate-policy endpoint device set is invalid")
    normalized_rates: dict[str, list[int]] = {}
    for device in devices:
        if _positive_int(counts.get(device), f"rate-policy {device} count") < 2:
            raise ValueError(f"rate-policy {device} endpoint set is incomplete")
        values = rates.get(device)
        if not isinstance(values, list) or not values:
            raise ValueError(f"rate-policy {device} rate set is invalid")
        normalized = [
            _positive_int(value, f"rate-policy {device} rate")
            for value in values
        ]
        if normalized != sorted(set(normalized)):
            raise ValueError(f"rate-policy {device} rate set is not canonical")
        normalized_rates[device] = normalized
    if len(endpoints) != counts["motu_m2"] + counts["roland_fp_30x"]:
        raise ValueError("rate-policy endpoint count is inconsistent")
    observed_directions: dict[str, set[str]] = {
        "motu_m2": set(),
        "roland_fp_30x": set(),
    }
    observed_rates: dict[str, set[int]] = {
        "motu_m2": set(),
        "roland_fp_30x": set(),
    }
    for index, endpoint in enumerate(endpoints):
        if not isinstance(endpoint, dict):
            raise ValueError(f"rate-policy endpoint {index} is invalid")
        device = endpoint.get("device")
        direction = endpoint.get("direction")
        if device not in observed_directions or direction not in {"source", "sink"}:
            raise ValueError(f"rate-policy endpoint {index} identity is invalid")
        rate_hz = _positive_int(
            endpoint.get("rate_hz"),
            "rate-policy endpoint rate",
        )
        _positive_int(endpoint.get("channels"), "rate-policy endpoint channels")
        _bounded_text(
            endpoint.get("sample_format"),
            "rate-policy endpoint sample format",
            2,
            32,
        )
        _sha256(endpoint.get("node_name_sha256"), "rate-policy node digest")
        _sha256(endpoint.get("device_serial_sha256"), "rate-policy serial digest")
        fingerprint = _sha256(
            endpoint.get("fingerprint"),
            "rate-policy endpoint fingerprint",
        )
        if fingerprint != canonical_value_sha256(
            {key: value for key, value in endpoint.items() if key != "fingerprint"}
        ):
            raise ValueError(f"rate-policy endpoint {index} fingerprint mismatch")
        observed_directions[device].add(direction)
        observed_rates[device].add(rate_hz)
    if any(value != {"source", "sink"} for value in observed_directions.values()):
        raise ValueError("rate-policy source/sink coverage is incomplete")
    calculated_rates = {
        device: sorted(observed_rates[device])
        for device in devices
    }
    if calculated_rates != normalized_rates:
        raise ValueError("rate-policy endpoint rate projection is inconsistent")
    if require_current and normalized_rates != {
        "motu_m2": [48_000],
        "roland_fp_30x": [44_100],
    }:
        raise ValueError("rate-policy endpoint rate sets do not match the contract")
    _validate_rate_policy_query(
        queries.get("source"),
        RATE_POLICY_PACTL_SOURCES_ARGV,
        "source",
        require_current=require_current,
    )
    _validate_rate_policy_query(
        queries.get("sink"),
        RATE_POLICY_PACTL_SINKS_ARGV,
        "sink",
        require_current=require_current,
    )
    snapshot_digest = _sha256(
        snapshot.get("snapshot_sha256"),
        "rate-policy endpoint snapshot digest",
    )
    if snapshot_digest != canonical_value_sha256(
        {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    ):
        raise ValueError("rate-policy endpoint snapshot digest mismatch")


def validate_bound_policy_decision(
    evidence: dict[str, Any],
    *,
    require_current: bool = True,
) -> None:
    gate = evidence.get("gate")
    if gate not in RATE_POLICY_DECISIONS:
        raise ValueError("rate-policy evidence targets another gate")
    if evidence.get("authority") != "bound-observed-rate-policy":
        raise ValueError("rate-policy authority is invalid")
    method = _bounded_text(evidence.get("method"), "rate-policy method", 2, 120)
    decision = _bounded_text(evidence.get("decision"), "decision", 2, 120)
    justification = _bounded_text(
        evidence.get("justification"),
        "justification",
        10,
        1000,
    )
    policy = evidence.get("policy")
    criteria = evidence.get("criteria")
    if not isinstance(policy, dict) or not policy:
        raise ValueError("rate-policy payload is missing")
    if not isinstance(criteria, dict) or not criteria:
        raise ValueError("rate-policy criteria are missing")
    if require_current:
        if method != RATE_POLICY_METHOD:
            raise ValueError("rate-policy method is not canonical")
        if decision != RATE_POLICY_DECISIONS[gate]:
            raise ValueError("rate-policy decision does not match the gate")
        if justification != RATE_POLICY_JUSTIFICATIONS[gate]:
            raise ValueError("rate-policy justification does not match the contract")
        if policy != RATE_POLICY_PAYLOADS[gate]:
            raise ValueError("rate-policy payload does not match the contract")
        if criteria != RATE_POLICY_CRITERIA:
            raise ValueError("rate-policy criteria do not match the contract")
    if evidence.get("blockers") != []:
        raise ValueError("passing rate-policy evidence contains blockers")
    truth = evidence.get("truth")
    if not isinstance(truth, dict):
        raise ValueError("rate-policy truth binding is missing")
    for field in ("report_sha256", "truth_chain_sha256", "graph_fingerprint"):
        _sha256(truth.get(field), f"rate-policy {field}")
    graph_rate = _positive_int(truth.get("graph_rate_hz"), "rate-policy graph rate")
    _positive_int(truth.get("graph_quantum_frames"), "rate-policy graph quantum")
    default_sink = _bounded_text(
        truth.get("default_sink"),
        "rate-policy default sink",
        2,
        256,
    )
    default_source = _bounded_text(
        truth.get("default_source"),
        "rate-policy default source",
        2,
        256,
    )
    if require_current and graph_rate != 48_000:
        raise ValueError("rate-policy graph is not 48 kHz")
    if require_current and default_sink != "motu-m2":
        raise ValueError("rate-policy default sink is not MOTU")
    if require_current and default_source != "motu-m2":
        raise ValueError("rate-policy default source is not MOTU")
    hardware = truth.get("hardware")
    if not isinstance(hardware, dict) or not all(
        hardware.get(device) is True
        for device in ("motu_m2", "roland_fp_30x")
    ):
        raise ValueError("rate-policy hardware projection is incomplete")
    warning_codes = truth.get("warning_codes")
    if not isinstance(warning_codes, list) or any(
        not isinstance(item, str) for item in warning_codes
    ):
        raise ValueError("rate-policy warning projection is invalid")
    _validate_rate_endpoint_snapshot(
        evidence.get("endpoints"),
        require_current=require_current,
    )
    profiles = evidence.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError("rate-policy profile binding is missing")
    profile_catalog_sha = _sha256(
        profiles.get("profile_catalog_sha256"),
        "rate-policy profile catalog digest",
    )
    operational_sha = _sha256(
        profiles.get("operational_profile_catalog_sha256"),
        "rate-policy operational catalog digest",
    )
    selected_profiles = profiles.get("selected_profiles")
    if not isinstance(selected_profiles, dict) or not selected_profiles:
        raise ValueError("rate-policy selected profile binding is invalid")
    selected_sha = _sha256(
        profiles.get("selected_profiles_sha256"),
        "rate-policy selected profile digest",
    )
    if selected_sha != canonical_value_sha256(selected_profiles):
        raise ValueError("rate-policy selected profile digest mismatch")
    if require_current and selected_profiles != RATE_POLICY_PROFILE_CONTRACT:
        raise ValueError("rate-policy selected profiles do not match the contract")
    implementation = evidence.get("implementation")
    if not isinstance(implementation, dict):
        raise ValueError("rate-policy implementation binding is missing")
    observed_implementation = {
        key: _sha256(implementation.get(key), f"rate-policy {key}")
        for key in (
            "rate_policy_observer_sha256",
            "laboratory_gate_sha256",
            "system_truth_sha256",
            "profile_catalog_sha256",
        )
    }
    if implementation.get("profile_catalog_sha256") != profile_catalog_sha:
        raise ValueError("rate-policy profile catalog bindings disagree")
    if require_current:
        expected_implementation = {
            "rate_policy_observer_sha256": sha256_file(RATE_POLICY_OBSERVER_PATH),
            "laboratory_gate_sha256": sha256_file(pathlib.Path(__file__)),
            "system_truth_sha256": sha256_file(SYSTEM_TRUTH_PATH),
            "profile_catalog_sha256": sha256_file(PROFILE_PATH),
        }
        if observed_implementation != expected_implementation:
            raise ValueError("rate-policy implementation binding changed")
        if profile_catalog_sha != sha256_file(PROFILE_PATH):
            raise ValueError("rate-policy profile catalog changed")
        if operational_sha != operational_profile_catalog_sha256():
            raise ValueError("rate-policy operational profile catalog changed")

def policy_decision_binding_current(evidence: dict[str, Any]) -> bool:
    try:
        validate_bound_policy_decision(evidence, require_current=True)
    except (OSError, ValueError):
        return False
    return True


def has_bound_qobuz_observation(evidence: dict[str, Any]) -> bool:
    return all(
        key in evidence
        for key in (
            "requested_duration_seconds",
            "start_timeout_seconds",
            "wait_started_at",
            "wait_duration_seconds",
            "baseline",
            "baseline_departure_observed",
            "observation_started_at",
            "observation_ended_at",
            "duration_seconds",
            "track_identity",
            "stream_rate_hz",
            "blockers",
            "implementation",
            "truth_before",
            "truth_after",
            "pulse_before",
            "pulse_after",
            "journal",
            "playback",
        )
    )


def _validate_qobuz_query(binding: Any, expected_argv: tuple[str, ...], label: str) -> None:
    if not isinstance(binding, dict):
        raise ValueError(f"Qobuz {label} query binding is missing")
    expected_list = list(expected_argv)
    if binding.get("query_argv") != expected_list:
        raise ValueError(f"Qobuz {label} query argv mismatch")
    if (
        _sha256(binding.get("query_argv_sha256"), f"Qobuz {label} argv SHA-256")
        != canonical_value_sha256(expected_list)
    ):
        raise ValueError(f"Qobuz {label} query digest mismatch")
    _sha256(binding.get("stdout_sha256"), f"Qobuz {label} stdout SHA-256")
    _sha256(binding.get("stderr_sha256"), f"Qobuz {label} stderr SHA-256")
    _nonnegative_int(binding.get("stdout_total_bytes"), f"Qobuz {label} stdout bytes")
    _nonnegative_int(binding.get("stderr_total_bytes"), f"Qobuz {label} stderr bytes")
    if binding.get("complete") is not True:
        raise ValueError(f"Qobuz {label} query is incomplete")


def _validate_qobuz_pulse(snapshot: Any, label: str) -> tuple[tuple[Any, ...], int]:
    if not isinstance(snapshot, dict):
        raise ValueError(f"Qobuz {label} Pulse snapshot is missing")
    if snapshot.get("blockers") != []:
        raise ValueError(f"Qobuz {label} Pulse snapshot contains blockers")
    sink = snapshot.get("default_sink")
    stream = snapshot.get("mopidy_stream")
    if not isinstance(sink, dict) or not isinstance(stream, dict):
        raise ValueError(f"Qobuz {label} Pulse route is incomplete")
    sink_name = _bounded_text(sink.get("name"), f"Qobuz {label} sink name", 1, 512)
    sink_index = _positive_int(sink.get("index"), f"Qobuz {label} sink index")
    endpoint_rate = _positive_int(sink.get("rate_hz"), f"Qobuz {label} sink rate")
    _sha256(
        sink.get("sample_specification_sha256"),
        f"Qobuz {label} sink sample-spec SHA-256",
    )
    stream_index = _positive_int(
        stream.get("index"), f"Qobuz {label} stream index"
    )
    stream_sink = _positive_int(
        stream.get("sink_index"), f"Qobuz {label} stream sink"
    )
    if stream_sink != sink_index:
        raise ValueError(f"Qobuz {label} stream is not on the default sink")
    stream_rate = _positive_int(
        stream.get("rate_hz"), f"Qobuz {label} stream rate"
    )
    for key in (
        "sample_specification_sha256",
        "application_name_sha256",
        "application_binary_sha256",
        "media_name_sha256",
    ):
        value = stream.get(key)
        if value is not None:
            _sha256(value, f"Qobuz {label} stream {key}")
    queries = snapshot.get("queries")
    if not isinstance(queries, dict):
        raise ValueError(f"Qobuz {label} Pulse query set is missing")
    _validate_qobuz_query(
        queries.get("info"), QOBUZ_PACTL_INFO_ARGV, f"{label} info"
    )
    _validate_qobuz_query(
        queries.get("sinks"),
        QOBUZ_PACTL_SINKS_ARGV,
        f"{label} sinks",
    )
    _validate_qobuz_query(
        queries.get("sink_inputs"),
        QOBUZ_PACTL_INPUTS_ARGV,
        f"{label} sink-inputs",
    )
    identity = (sink_name, sink_index, stream_index, stream_sink, stream_rate)
    return identity, endpoint_rate


def validate_qobuz_rate(evidence: dict[str, Any]) -> None:
    track_rate = _positive_int(evidence.get("track_rate_hz"), "track_rate_hz")
    track_fingerprint = _sha256(evidence.get("track_fingerprint"), "track_fingerprint")
    stream_rate = evidence.get("stream_rate_hz")
    if stream_rate is not None:
        stream_rate = _positive_int(stream_rate, "stream_rate_hz")
    graph_rate = _positive_int(evidence.get("graph_rate_hz"), "graph_rate_hz")
    endpoint_rate = _positive_int(evidence.get("endpoint_rate_hz"), "endpoint_rate_hz")
    resampling = evidence.get("resampling_observed")
    if not isinstance(resampling, bool):
        raise ValueError("resampling_observed must be boolean")
    if resampling:
        raise ValueError("Qobuz rate proof observed resampling")
    rates = {
        track_rate,
        graph_rate,
        endpoint_rate,
    }
    if stream_rate is not None:
        rates.add(stream_rate)
    if len(rates) != 1:
        raise ValueError("Qobuz track, stream, graph and endpoint rates do not match")
    method = _bounded_text(evidence.get("method"), "method", 5, 500)
    graph_fingerprint_value = _sha256(
        evidence.get("graph_fingerprint"), "graph_fingerprint"
    )
    if not has_bound_qobuz_observation(evidence):
        return
    if method != QOBUZ_METHOD:
        raise ValueError("Qobuz observation method is not canonical")
    requested = _positive_int(
        evidence.get("requested_duration_seconds"), "requested_duration_seconds"
    )
    if requested < 60 or requested > 3_600:
        raise ValueError("Qobuz observation must cover 60 to 3600 seconds")
    start_timeout = _nonnegative_int(
        evidence.get("start_timeout_seconds"), "start_timeout_seconds"
    )
    if start_timeout > 3_600:
        raise ValueError("Qobuz start timeout exceeds 3600 seconds")
    wait_started = parse_timestamp(evidence.get("wait_started_at"), "Qobuz wait start")
    wait_duration = _number(
        evidence.get("wait_duration_seconds"), "wait_duration_seconds"
    )
    if wait_duration < 0 or wait_duration > start_timeout + 2.0:
        raise ValueError("Qobuz wait duration exceeds the start timeout")
    baseline = evidence.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("Qobuz baseline binding is missing")
    baseline_state = baseline.get("state")
    if baseline_state not in {"playing", "paused", "stopped"}:
        raise ValueError("Qobuz baseline playback state is invalid")
    baseline_position = baseline.get("position_ms")
    if baseline_position is not None:
        _nonnegative_int(baseline_position, "Qobuz baseline position")
    baseline_fingerprint = baseline.get("track_fingerprint")
    if baseline_fingerprint is not None:
        baseline_fingerprint = _sha256(
            baseline_fingerprint, "Qobuz baseline track fingerprint"
        )
    _sha256(
        baseline.get("rpc_response_sha256"),
        "Qobuz baseline RPC response SHA-256",
    )
    departure_observed = evidence.get("baseline_departure_observed")
    if not isinstance(departure_observed, bool):
        raise ValueError("Qobuz baseline departure flag is not boolean")
    if baseline_fingerprint is None and departure_observed is not True:
        raise ValueError("Qobuz empty baseline must be marked departed")
    if (
        baseline_state == "playing"
        and baseline_fingerprint == track_fingerprint
        and departure_observed is not True
    ):
        raise ValueError("Qobuz preexisting playing track was not restarted")
    started = parse_timestamp(
        evidence.get("observation_started_at"), "Qobuz observation start"
    )
    ended = parse_timestamp(
        evidence.get("observation_ended_at"), "Qobuz observation end"
    )
    if not wait_started <= started < ended:
        raise ValueError("Qobuz observation timestamps are inconsistent")
    wall_wait_duration = (started - wait_started).total_seconds()
    if wall_wait_duration > start_timeout + 2.0:
        raise ValueError("Qobuz wall-clock wait exceeds the start timeout")
    if abs(wall_wait_duration - wait_duration) > 2.0:
        raise ValueError("Qobuz wait duration contradicts its timestamps")
    duration = _number(evidence.get("duration_seconds"), "duration_seconds")
    if duration < requested or abs((ended - started).total_seconds() - duration) > 2.0:
        raise ValueError("Qobuz observation duration is inconsistent")
    measured = parse_timestamp(evidence.get("measured_at"), "evidence measured_at")
    if abs((measured - ended).total_seconds()) > 2.0:
        raise ValueError("Qobuz measured_at does not match the observation end")
    if evidence.get("blockers") != []:
        raise ValueError("Qobuz passing evidence contains blockers")

    identity = evidence.get("track_identity")
    if not isinstance(identity, dict):
        raise ValueError("Qobuz track identity is missing")
    uri = _bounded_text(identity.get("uri"), "Qobuz track URI", 15, 255)
    uri_match = QOBUZ_URI_RE.fullmatch(uri)
    if uri_match is None or identity.get("track_id") != uri_match.group("track_id"):
        raise ValueError("Qobuz track URI and track ID disagree")
    identity_copy = dict(identity)
    identity_fingerprint = _sha256(
        identity_copy.pop("fingerprint", None), "Qobuz identity fingerprint"
    )
    if identity_fingerprint != canonical_value_sha256(identity_copy):
        raise ValueError("Qobuz track identity fingerprint mismatch")
    if track_fingerprint != identity_fingerprint:
        raise ValueError("Qobuz top-level track fingerprint mismatch")
    for key in ("name_sha256", "album_sha256"):
        if identity.get(key) is not None:
            _sha256(identity.get(key), f"Qobuz track {key}")
    _sha256(identity.get("artists_sha256"), "Qobuz artists SHA-256")
    _nonnegative_int(identity.get("artist_count"), "Qobuz artist count")
    if identity.get("length_ms") is not None:
        _positive_int(identity.get("length_ms"), "Qobuz track length_ms")

    implementation = evidence.get("implementation")
    if not isinstance(implementation, dict):
        raise ValueError("Qobuz implementation binding is missing")
    expected_implementation = {
        "qobuz_rate_observer_sha256": sha256_file(QOBUZ_RATE_OBSERVER_PATH),
        "laboratory_gate_sha256": sha256_file(pathlib.Path(__file__)),
        "system_truth_sha256": sha256_file(SYSTEM_TRUTH_PATH),
    }
    for key, expected in expected_implementation.items():
        if _sha256(implementation.get(key), f"Qobuz {key}") != expected:
            raise ValueError(f"Qobuz implementation binding changed: {key}")

    truth_identities: list[tuple[Any, ...]] = []
    for label in ("truth_before", "truth_after"):
        binding = evidence.get(label)
        if not isinstance(binding, dict):
            raise ValueError(f"Qobuz {label} truth binding is missing")
        _sha256(binding.get("report_sha256"), f"Qobuz {label} report SHA-256")
        _sha256(
            binding.get("truth_chain_sha256"),
            f"Qobuz {label} truth-chain SHA-256",
        )
        observed_graph = _sha256(
            binding.get("graph_fingerprint"), f"Qobuz {label} graph fingerprint"
        )
        observed_rate = _positive_int(
            binding.get("rate_hz"), f"Qobuz {label} graph rate"
        )
        observed_quantum = _positive_int(
            binding.get("quantum_frames"), f"Qobuz {label} graph quantum"
        )
        truth_identities.append((observed_graph, observed_rate, observed_quantum))
    if truth_identities[0] != truth_identities[1]:
        raise ValueError("Qobuz graph changed during observation")
    if truth_identities[0][0] != graph_fingerprint_value:
        raise ValueError("Qobuz graph fingerprint contradicts truth binding")
    if truth_identities[0][1] != graph_rate:
        raise ValueError("Qobuz graph rate contradicts truth binding")

    pulse_before, before_endpoint = _validate_qobuz_pulse(
        evidence.get("pulse_before"), "before"
    )
    pulse_after, after_endpoint = _validate_qobuz_pulse(
        evidence.get("pulse_after"), "after"
    )
    if pulse_before != pulse_after or before_endpoint != after_endpoint:
        raise ValueError("Qobuz Pulse route changed during observation")
    if pulse_before[-1] != stream_rate or before_endpoint != endpoint_rate:
        raise ValueError("Qobuz top-level Pulse rates contradict the snapshots")

    journal = evidence.get("journal")
    if not isinstance(journal, dict):
        raise ValueError("Qobuz journal binding is missing")
    if journal.get("source") != "mopidy-service-qobuz-downloadable-event":
        raise ValueError("Qobuz journal source is unknown")
    expected_argv = list(
        qobuz_journal_argv(evidence["wait_started_at"], evidence["observation_ended_at"])
    )
    if journal.get("query_argv") != expected_argv:
        raise ValueError("Qobuz journal query does not match the observation")
    if (
        _sha256(journal.get("query_argv_sha256"), "Qobuz journal argv SHA-256")
        != canonical_value_sha256(expected_argv)
    ):
        raise ValueError("Qobuz journal query digest mismatch")
    if journal.get("returncode") != 0 or journal.get("complete") is not True:
        raise ValueError("Qobuz journal query is incomplete")
    if journal.get("stdout_truncated") is not False:
        raise ValueError("Qobuz journal output is truncated")
    line_count = _nonnegative_int(journal.get("line_count"), "Qobuz journal lines")
    max_lines = _positive_int(journal.get("max_lines"), "Qobuz journal max lines")
    if max_lines != MAX_QOBUZ_JOURNAL_LINES or line_count > max_lines:
        raise ValueError("Qobuz journal line bound is invalid")
    _sha256(journal.get("stdout_sha256"), "Qobuz journal stdout SHA-256")
    _nonnegative_int(journal.get("stdout_total_bytes"), "Qobuz journal bytes")
    event_count = _positive_int(
        journal.get("matching_event_count"), "Qobuz matching event count"
    )
    event = journal.get("event")
    if not isinstance(event, dict) or event_count != 1:
        raise ValueError("Qobuz downloadable event is missing")
    matching_events_sha256 = _sha256(
        journal.get("matching_events_sha256"),
        "Qobuz matching events SHA-256",
    )
    if matching_events_sha256 != canonical_value_sha256([event]):
        raise ValueError("Qobuz matching event digest mismatch")
    if event.get("track_id") != identity.get("track_id"):
        raise ValueError("Qobuz downloadable event has another track ID")
    if event.get("extension") != "FLAC":
        raise ValueError("Qobuz downloadable event is not FLAC")
    _positive_int(event.get("bit_depth"), "Qobuz bit depth")
    if _positive_int(event.get("rate_hz"), "Qobuz event rate") != track_rate:
        raise ValueError("Qobuz downloadable event rate mismatch")
    event_time = parse_timestamp(event.get("observed_at"), "Qobuz event timestamp")
    if event_time < wait_started or event_time > ended:
        raise ValueError("Qobuz downloadable event is outside the observation window")
    _sha256(event.get("message_sha256"), "Qobuz event message SHA-256")

    playback = evidence.get("playback")
    if not isinstance(playback, dict):
        raise ValueError("Qobuz playback binding is missing")
    sample_count = _positive_int(playback.get("sample_count"), "Qobuz sample count")
    if sample_count < 2 or playback.get("position_monotonic") is not True:
        raise ValueError("Qobuz playback was not continuously observed")
    first_position = _nonnegative_int(
        playback.get("first_position_ms"), "Qobuz first position"
    )
    last_position = _nonnegative_int(
        playback.get("last_position_ms"), "Qobuz last position"
    )
    if last_position < first_position:
        raise ValueError("Qobuz playback position moved backwards")
    request_sha256 = _sha256(
        playback.get("rpc_request_sha256"), "Qobuz RPC request SHA-256"
    )
    if request_sha256 != canonical_value_sha256(QOBUZ_RPC_PAYLOAD):
        raise ValueError("Qobuz RPC request does not contain the canonical getters")
    _sha256(
        playback.get("rpc_response_chain_sha256"),
        "Qobuz RPC response-chain SHA-256",
    )


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
    allow_legacy_voice: bool = False,
    allow_legacy_policy: bool = False,
    allow_stale_policy: bool = False,
    allow_legacy_xrun: bool = False,
    allow_legacy_plugin_host: bool = False,
    allow_legacy_qobuz: bool = False,
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
    if gate == "voice-level-measurement":
        if has_bound_voice_capture(evidence):
            validate_bound_voice_capture(evidence)
        elif not allow_legacy_voice:
            raise ValueError(
                "legacy voice evidence lacks a bounded MOTU live capture"
            )
    if gate in RATE_POLICY_DECISIONS:
        if has_bound_policy_decision(evidence):
            validate_bound_policy_decision(
                evidence,
                require_current=not allow_stale_policy,
            )
        elif not allow_legacy_policy:
            raise ValueError(
                "legacy policy evidence lacks bound rate observations"
            )
    if gate == "xrun-stability-test" and not has_bound_xrun_observation(evidence):
        if not allow_legacy_xrun:
            raise ValueError("legacy XRun evidence lacks a bounded journal observation")
    if gate == "managed-plugin-host-proof" and not has_bound_plugin_host_observation(
        evidence
    ):
        if not allow_legacy_plugin_host:
            raise ValueError("legacy plugin-host evidence lacks a bounded observation")
    if gate == "qobuz-rate-proof" and not has_bound_qobuz_observation(evidence):
        if not allow_legacy_qobuz:
            raise ValueError("legacy Qobuz evidence lacks a bounded observation")
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
            allow_legacy_voice=True,
            allow_legacy_policy=True,
            allow_stale_policy=True,
            allow_legacy_xrun=True,
            allow_legacy_plugin_host=True,
            allow_legacy_qobuz=True,
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
        if gate == "voice-level-measurement":
            evidence = receipt.get("evidence")
            if not isinstance(evidence, dict) or not has_bound_voice_capture(
                evidence
            ):
                invalidated[gate] = "legacy-unbound-voice-evidence"
                continue
        if gate in RATE_POLICY_DECISIONS:
            evidence = receipt.get("evidence")
            if not isinstance(evidence, dict) or not has_bound_policy_decision(
                evidence
            ):
                invalidated[gate] = "legacy-unbound-policy-evidence"
                continue
            if not policy_decision_binding_current(evidence):
                invalidated[gate] = "policy-binding-changed"
                continue
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
        if gate == "qobuz-rate-proof":
            evidence = receipt.get("evidence")
            if not isinstance(evidence, dict) or not has_bound_qobuz_observation(
                evidence
            ):
                invalidated[gate] = "legacy-unbound-qobuz-evidence"
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
