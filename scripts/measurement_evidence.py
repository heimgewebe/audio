#!/usr/bin/env python3
"""Create hash-bound laboratory evidence from offline analyses or decisions."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import os
import stat
import tempfile
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEVEL_PATH = ROOT / "scripts" / "level_analyzer.py"
LATENCY_PATH = ROOT / "scripts" / "latency_analyzer.py"
LAB_PATH = ROOT / "scripts" / "laboratory_gate.py"
SYSTEM_TRUTH_PATH = ROOT / "scripts" / "system_truth.py"
PLUGIN_HOST_PATH = ROOT / "scripts" / "plugin_host_observer.py"
QOBUZ_RATE_PATH = ROOT / "scripts" / "qobuz_rate_observer.py"
MAX_SOURCE_BYTES = 536_870_912


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LEVEL = load_module("level_analyzer_for_evidence", LEVEL_PATH)
LATENCY = load_module("latency_analyzer_for_evidence", LATENCY_PATH)
LAB = load_module("laboratory_gate_for_evidence", LAB_PATH)
SYSTEM_TRUTH = load_module("system_truth_for_evidence", SYSTEM_TRUTH_PATH)
PLUGIN_HOST = load_module("plugin_host_observer_for_evidence", PLUGIN_HOST_PATH)
QOBUZ_RATE = load_module("qobuz_rate_observer_for_evidence", QOBUZ_RATE_PATH)


@contextlib.contextmanager
def stable_source_copy(path: pathlib.Path):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"source file cannot be opened safely: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"source file is not regular: {path}")
        if before.st_size < 1 or before.st_size > MAX_SOURCE_BYTES:
            raise ValueError(
                f"source file must contain 1 to {MAX_SOURCE_BYTES} bytes"
            )
        with tempfile.TemporaryDirectory(prefix="audio-evidence-") as directory:
            snapshot = pathlib.Path(directory) / "source.wav"
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(os.dup(descriptor), "rb", closefd=True) as source, snapshot.open(
                "wb"
            ) as target:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_SOURCE_BYTES:
                        raise ValueError("source file grew beyond the evidence limit")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            snapshot.chmod(0o600)
            after = os.fstat(descriptor)
            stable_fields = (
                "st_dev",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
                raise ValueError("source file changed while the evidence snapshot was created")
            if total != before.st_size:
                raise ValueError("source file byte count changed during snapshot")
            binding = {
                "name": path.name,
                "sha256": digest.hexdigest(),
                "bytes": total,
            }
            yield snapshot, binding
    finally:
        os.close(descriptor)


def file_binding(path: pathlib.Path) -> dict[str, object]:
    with stable_source_copy(path) as (_, binding):
        return binding


def measured_at() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def monotonic_now() -> float:
    return time.monotonic()


def sleep_for(seconds: int) -> None:
    time.sleep(seconds)


def physical_sha(path: pathlib.Path) -> str:
    return LAB.current_physical_sha256(path)


def voice_level_evidence(
    wav: pathlib.Path, physical_state: pathlib.Path
) -> dict[str, Any]:
    with stable_source_copy(wav) as (snapshot, binding):
        analysis = LEVEL.analyze(snapshot)
    channels = analysis.get("channels_analysis", [])
    no_clipping = bool(channels) and all(
        isinstance(item, dict) and item.get("clipped_samples") == 0
        for item in channels
    )
    passed = analysis.get("voice_target", {}).get("status") == "in-range" and no_clipping
    return {
        "schema_version": 1,
        "kind": "audio_level_measurement_evidence",
        "gate": "voice-level-measurement",
        "result": "pass" if passed else "fail",
        "measured_at": measured_at(),
        "physical_state_sha256": physical_sha(physical_state),
        "source_wav": binding,
        "analysis": analysis,
        "criteria": {
            "peak_dbfs_range": [-12.0, -6.0],
            "maximum_clipped_samples_per_channel": 0,
        },
        "does_not_establish": [
            "microphone-identity",
            "analog-gain-position",
            "safe-monitoring-level",
        ],
    }


def loopback_latency_evidence(
    reference: pathlib.Path,
    recorded: pathlib.Path,
    physical_state: pathlib.Path,
    max_ms: float,
    quantum_frames: int,
    graph_fingerprint: str,
) -> dict[str, Any]:
    with stable_source_copy(reference) as (reference_snapshot, reference_binding):
        with stable_source_copy(recorded) as (recorded_snapshot, recorded_binding):
            analysis = LATENCY.analyze(
                reference_snapshot, recorded_snapshot, max_ms
            )
    confidence = float(analysis["peak_detection_confidence"])
    snr = float(analysis["peak_snr_db"])
    latency = float(analysis["round_trip_latency_ms"])
    distinct_sources = reference_binding["sha256"] != recorded_binding["sha256"]
    passed = (
        confidence >= 0.8
        and snr >= 20.0
        and 0.0 < latency <= 500.0
        and int(analysis["delay_samples"]) > 0
        and distinct_sources
    )
    return {
        "schema_version": 1,
        "kind": "audio_loopback_latency_evidence",
        "gate": "loopback-latency-measurement",
        "result": "pass" if passed else "fail",
        "measured_at": measured_at(),
        "physical_state_sha256": physical_sha(physical_state),
        "quantum_frames": quantum_frames,
        "graph_fingerprint": graph_fingerprint,
        "reference_wav": reference_binding,
        "recorded_wav": recorded_binding,
        "analysis": analysis,
        "criteria": {
            "minimum_peak_detection_confidence": 0.8,
            "minimum_peak_snr_db": 20.0,
            "minimum_delay_samples": 1,
            "maximum_round_trip_latency_ms": 500.0,
            "reference_and_recording_must_differ": True,
        },
        "does_not_establish": [
            "latency-distribution",
            "xrun-free-operation",
            "subjective-playability",
        ],
    }


def policy_decision_evidence(
    gate: str, decision: str, justification: str
) -> dict[str, Any]:
    if gate not in {"resampling-decision", "rate-policy-decision"}:
        raise ValueError("policy decision gate is not supported")
    payload = {
        "schema_version": 1,
        "kind": "audio_policy_decision",
        "gate": gate,
        "result": "pass",
        "measured_at": measured_at(),
        "physical_state_sha256": None,
        "decision": decision,
        "justification": justification,
        "authority": "explicit-operator-decision",
        "does_not_establish": [
            "bit-perfect-playback",
            "absence-of-resampling",
            "profile-apply-authority",
        ],
    }
    LAB.validate_evidence(gate, payload)
    return payload


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase hexadecimal SHA-256")
    return value


def graph_observation_binding(report: dict[str, Any]) -> dict[str, Any]:
    SYSTEM_TRUTH.verify_report(report)
    doctor = report.get("doctor")
    runtime = report.get("runtime")
    if not isinstance(doctor, dict) or not isinstance(runtime, dict):
        raise ValueError("truth report has no doctor or runtime projection")
    graph = doctor.get("graph")
    if not isinstance(graph, dict):
        raise ValueError("truth report has no graph projection")
    return {
        "report_sha256": _sha256(report.get("report_sha256"), "truth report SHA-256"),
        "truth_chain_sha256": _sha256(
            report.get("truth_chain_sha256"), "truth-chain SHA-256"
        ),
        "graph_fingerprint": _sha256(
            runtime.get("graph_fingerprint"), "graph fingerprint"
        ),
        "rate_hz": _positive_int(graph.get("force_rate_hz"), "graph rate_hz"),
        "quantum_frames": _positive_int(
            graph.get("force_quantum_frames"), "graph quantum_frames"
        ),
    }


def _require_expected_graph(
    binding: dict[str, Any],
    expected_rate_hz: int,
    expected_quantum_frames: int,
    expected_graph_fingerprint: str,
) -> None:
    if binding["rate_hz"] != expected_rate_hz:
        raise ValueError("observed graph rate does not match the expected rate")
    if binding["quantum_frames"] != expected_quantum_frames:
        raise ValueError("observed graph quantum does not match the expected quantum")
    if binding["graph_fingerprint"] != expected_graph_fingerprint:
        raise ValueError("observed graph fingerprint does not match the expected graph")


def xrun_observation_evidence(
    duration_seconds: int,
    expected_rate_hz: int,
    expected_quantum_frames: int,
    expected_graph_fingerprint: str,
) -> dict[str, Any]:
    duration_seconds = _positive_int(duration_seconds, "duration_seconds")
    if duration_seconds < 60 or duration_seconds > 86_400:
        raise ValueError("XRun observation must cover 60 to 86400 seconds")
    expected_rate_hz = _positive_int(expected_rate_hz, "expected_rate_hz")
    expected_quantum_frames = _positive_int(
        expected_quantum_frames, "expected_quantum_frames"
    )
    expected_graph_fingerprint = _sha256(
        expected_graph_fingerprint, "expected graph fingerprint"
    )

    before = graph_observation_binding(SYSTEM_TRUTH.build_report())
    _require_expected_graph(
        before,
        expected_rate_hz,
        expected_quantum_frames,
        expected_graph_fingerprint,
    )
    started_at = utc_now()
    started_monotonic = monotonic_now()
    sleep_for(duration_seconds)
    ended_monotonic = monotonic_now()
    ended_at = utc_now()
    actual_duration = ended_monotonic - started_monotonic
    if actual_duration < duration_seconds:
        raise ValueError("XRun observation ended before the requested duration")

    after = graph_observation_binding(SYSTEM_TRUTH.build_report())
    _require_expected_graph(
        after,
        expected_rate_hz,
        expected_quantum_frames,
        expected_graph_fingerprint,
    )
    for field in ("graph_fingerprint", "rate_hz", "quantum_frames"):
        if before[field] != after[field]:
            raise ValueError(f"audio graph changed during XRun observation: {field}")

    started_text = started_at.isoformat()
    ended_text = ended_at.isoformat()
    argv = LAB.xrun_journal_argv(started_text, ended_text)
    SYSTEM_TRUTH.assert_read_only_commands((argv,))
    result = SYSTEM_TRUTH.run_read_only(argv)
    if result.argv != argv:
        raise ValueError("bounded XRun journal result is bound to another command")
    if result.error is not None or result.returncode != 0:
        raise ValueError("bounded XRun journal query failed")
    if result.stdout_truncated or result.stderr_truncated:
        raise ValueError("bounded XRun journal query was truncated")
    lines = result.stdout.splitlines()
    if len(lines) > LAB.MAX_XRUN_JOURNAL_LINES:
        raise ValueError("XRun journal window exceeds the line limit")
    xrun_lines = [
        line.strip() for line in lines if SYSTEM_TRUTH.XRUN_PATTERN.search(line)
    ]
    payload = {
        "schema_version": 1,
        "kind": "pipewire_xrun_observation",
        "gate": "xrun-stability-test",
        "result": "pass" if not xrun_lines else "fail",
        "measured_at": ended_text,
        "physical_state_sha256": None,
        "requested_duration_seconds": duration_seconds,
        "duration_seconds": round(actual_duration, 3),
        "observation_started_at": started_text,
        "observation_ended_at": ended_text,
        "xrun_delta": len(xrun_lines),
        "rate_hz": expected_rate_hz,
        "quantum_frames": expected_quantum_frames,
        "graph_fingerprint": expected_graph_fingerprint,
        "graph_before": before,
        "graph_after": after,
        "journal": {
            "source": "journalctl-user-audio-units",
            "query_argv": list(argv),
            "query_argv_sha256": LAB.canonical_value_sha256(list(argv)),
            "returncode": result.returncode,
            "stdout_sha256": result.stdout_sha256,
            "stdout_total_bytes": result.stdout_total_bytes,
            "stdout_truncated": result.stdout_truncated,
            "line_count": len(lines),
            "max_lines": LAB.MAX_XRUN_JOURNAL_LINES,
            "xrun_line_count": len(xrun_lines),
            "xrun_lines_sha256": LAB.canonical_value_sha256(xrun_lines),
            "complete": True,
        },
        "does_not_establish": [
            "absence of audio defects not reported as XRuns",
            "stability outside the bounded observation window",
            "subjective playback quality",
        ],
    }
    return payload


def emit_evidence(
    payload: dict[str, Any], output: pathlib.Path | None = None
) -> dict[str, Any] | None:
    if output is None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return None
    SYSTEM_TRUTH.atomic_write_private(output, payload)
    receipt = {
        "schema_version": 1,
        "kind": "audio_evidence_output_receipt",
        "output_basename": output.name,
        "evidence_kind": payload.get("kind"),
        "evidence_result": payload.get("result"),
        "evidence_sha256": LAB.canonical_sha256(payload),
    }
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    voice = sub.add_parser("voice-level")
    voice.add_argument("wav", type=pathlib.Path)
    voice.add_argument(
        "--physical-state", type=pathlib.Path, default=LAB.PHYSICAL.DEFAULT_STATE
    )

    loopback = sub.add_parser("loopback-latency")
    loopback.add_argument("reference", type=pathlib.Path)
    loopback.add_argument("recorded", type=pathlib.Path)
    loopback.add_argument("--max-ms", type=float, default=500.0)
    loopback.add_argument("--quantum-frames", type=int, required=True)
    loopback.add_argument("--graph-fingerprint", required=True)
    loopback.add_argument(
        "--physical-state", type=pathlib.Path, default=LAB.PHYSICAL.DEFAULT_STATE
    )

    policy = sub.add_parser("policy-decision")
    policy.add_argument(
        "gate", choices=("rate-policy-decision", "resampling-decision")
    )
    policy.add_argument("decision")
    policy.add_argument("justification")

    xrun = sub.add_parser("xrun-observation")
    xrun.add_argument("--duration-seconds", type=int, default=60)
    xrun.add_argument("--expected-rate-hz", type=int, required=True)
    xrun.add_argument("--expected-quantum-frames", type=int, required=True)
    xrun.add_argument("--expected-graph-fingerprint", required=True)
    xrun.add_argument("--output", type=pathlib.Path)

    plugin_host = sub.add_parser("managed-plugin-host-observation")
    plugin_host.add_argument("--duration-seconds", type=int, default=60)
    plugin_host.add_argument("--output", type=pathlib.Path)

    qobuz = sub.add_parser("qobuz-rate-observation")
    qobuz.add_argument("--duration-seconds", type=int, default=60)
    qobuz.add_argument("--start-timeout-seconds", type=int, default=60)
    qobuz.add_argument("--output", type=pathlib.Path)

    args = parser.parse_args()
    if args.command == "voice-level":
        result = voice_level_evidence(args.wav, args.physical_state)
    elif args.command == "loopback-latency":
        result = loopback_latency_evidence(
            args.reference,
            args.recorded,
            args.physical_state,
            args.max_ms,
            args.quantum_frames,
            args.graph_fingerprint,
        )
    elif args.command == "xrun-observation":
        result = xrun_observation_evidence(
            args.duration_seconds,
            args.expected_rate_hz,
            args.expected_quantum_frames,
            args.expected_graph_fingerprint,
        )
    elif args.command == "managed-plugin-host-observation":
        result = PLUGIN_HOST.managed_plugin_host_evidence(args.duration_seconds)
    elif args.command == "qobuz-rate-observation":
        result = QOBUZ_RATE.qobuz_rate_observation(
            args.duration_seconds,
            args.start_timeout_seconds,
        )
    else:
        result = policy_decision_evidence(
            args.gate, args.decision, args.justification
        )
    output = (
        args.output
        if args.command
        in {
            "xrun-observation",
            "managed-plugin-host-observation",
            "qobuz-rate-observation",
        }
        else None
    )
    emit_evidence(result, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
