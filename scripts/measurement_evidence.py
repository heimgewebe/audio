#!/usr/bin/env python3
"""Create hash-bound laboratory evidence from offline analyses or decisions."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
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


def file_binding(path: pathlib.Path) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError(f"source file must not be a symbolic link: {path}")
    if not path.is_file():
        raise ValueError(f"source file does not exist: {path}")
    size = path.stat().st_size
    if size < 1 or size > MAX_SOURCE_BYTES:
        raise ValueError(f"source file must contain 1 to {MAX_SOURCE_BYTES} bytes")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return {
        "name": path.name,
        "sha256": digest.hexdigest(),
        "bytes": size,
    }


def measured_at() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def physical_sha(path: pathlib.Path) -> str:
    return LAB.current_physical_sha256(path)


def voice_level_evidence(
    wav: pathlib.Path, physical_state: pathlib.Path
) -> dict[str, Any]:
    analysis = LEVEL.analyze(wav)
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
        "source_wav": file_binding(wav),
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
) -> dict[str, Any]:
    analysis = LATENCY.analyze(reference, recorded, max_ms)
    confidence = float(analysis["peak_detection_confidence"])
    snr = float(analysis["peak_snr_db"])
    latency = float(analysis["round_trip_latency_ms"])
    passed = confidence >= 0.8 and snr >= 20.0 and 0.0 <= latency <= 500.0
    return {
        "schema_version": 1,
        "kind": "audio_loopback_latency_evidence",
        "gate": "loopback-latency-measurement",
        "result": "pass" if passed else "fail",
        "measured_at": measured_at(),
        "physical_state_sha256": physical_sha(physical_state),
        "quantum_frames": quantum_frames,
        "reference_wav": file_binding(reference),
        "recorded_wav": file_binding(recorded),
        "analysis": analysis,
        "criteria": {
            "minimum_peak_detection_confidence": 0.8,
            "minimum_peak_snr_db": 20.0,
            "maximum_round_trip_latency_ms": 500.0,
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
        )
    else:
        result = policy_decision_evidence(
            args.gate, args.decision, args.justification
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
