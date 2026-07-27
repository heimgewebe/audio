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
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEVEL_PATH = ROOT / "scripts" / "level_analyzer.py"
LATENCY_PATH = ROOT / "scripts" / "latency_analyzer.py"
LAB_PATH = ROOT / "scripts" / "laboratory_gate.py"
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
    else:
        result = policy_decision_evidence(
            args.gate, args.decision, args.justification
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
