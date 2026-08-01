#!/usr/bin/env python3
"""Evaluate rendered whale voices against source-family holdout trajectories.

The evaluator never scores against a source family used for live trajectory
selection. It reports a bounded engineering indicator, not biological identity
or perceptual equivalence.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_whale_voice_model import (  # noqa: E402
    ANALYSIS_RATE,
    CONTROL_POINTS,
    DOWNSAMPLE,
    analyze_clip,
    percentile,
)
from compare_whale_organic import render_phrase  # noqa: E402
from whale_source_filter_engine import WhaleSourceFilterBank  # noqa: E402

FEATURES = (
    "periodicity",
    "roughness",
    "high_band_ratio",
    "spectral_tilt",
    "pulse_strength",
    "subharmonic_strength",
    "secondary_strength",
)
FLOORS = {
    "periodicity": 0.08,
    "roughness": 0.08,
    "high_band_ratio": 0.06,
    "spectral_tilt": 0.08,
    "pulse_strength": 0.10,
    "subharmonic_strength": 0.04,
    "secondary_strength": 0.04,
}


def reduce_rendered(samples: list[float]) -> list[float]:
    usable = len(samples) - len(samples) % DOWNSAMPLE
    return [
        sum(samples[index : index + DOWNSAMPLE]) / DOWNSAMPLE
        for index in range(0, usable, DOWNSAMPLE)
    ]


def trajectory_vector(points: list[dict[str, object]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key in FEATURES:
        values = [float(point[key]) for point in points]
        result[key] = statistics.median(values)
    return result


def synthetic_vector(samples: list[float]) -> tuple[dict[str, float], dict[str, float]]:
    points, summary = analyze_clip(reduce_rendered(samples))
    return trajectory_vector(points), summary


def aggregate_vectors(vectors: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    aggregate: dict[str, dict[str, float]] = {}
    for key in FEATURES:
        values = [vector[key] for vector in vectors]
        aggregate[key] = {
            "median": statistics.median(values),
            "q25": percentile(values, 0.25),
            "q75": percentile(values, 0.75),
        }
    return aggregate


def score(
    vector: dict[str, float], aggregate: dict[str, dict[str, float]]
) -> tuple[float, dict[str, dict[str, float]]]:
    distances: list[float] = []
    deltas: dict[str, dict[str, float]] = {}
    for key in FEATURES:
        target = aggregate[key]["median"]
        spread = max(aggregate[key]["q75"] - aggregate[key]["q25"], FLOORS[key])
        distance = abs(vector[key] - target) / spread
        distances.append(min(distance, 8.0))
        deltas[key] = {
            "synthetic": vector[key],
            "holdout_median": target,
            "holdout_iqr": aggregate[key]["q75"] - aggregate[key]["q25"],
            "normalized_distance": distance,
        }
    return math.exp(-statistics.fmean(distances)), deltas


def evaluate(engine: str) -> dict[str, object]:
    bank = WhaleSourceFilterBank()
    manifest = json.loads(bank.manifest_path.read_text(encoding="utf-8"))
    trajectories = manifest["trajectories"]
    if not isinstance(trajectories, list):
        raise RuntimeError("voice model trajectories are unavailable")
    holdout_vectors = [
        trajectory_vector(record["points"])
        for record in trajectories
        if isinstance(record, dict)
        and record.get("split") == "holdout"
        and isinstance(record.get("points"), list)
    ]
    train_vectors = [
        trajectory_vector(record["points"])
        for record in trajectories
        if isinstance(record, dict)
        and record.get("split") == "train"
        and isinstance(record.get("points"), list)
    ]
    if len(holdout_vectors) < 2 or len(train_vectors) < 4:
        raise RuntimeError("voice model evaluation split is incomplete")
    synthetic_samples = render_phrase(engine)
    vector, summary = synthetic_vector(synthetic_samples)
    holdout_aggregate = aggregate_vectors(holdout_vectors)
    train_aggregate = aggregate_vectors(train_vectors)
    holdout_score, deltas = score(vector, holdout_aggregate)
    train_score, _train_deltas = score(vector, train_aggregate)
    return {
        "schema_version": 1,
        "kind": "humpback_whale_voice_model_holdout_evaluation",
        "engine": engine,
        "method": "source-family-held-out-temporal-control-feature-distance",
        "does_not_establish": [
            "biological_identity",
            "perceptual_equivalence",
            "species_classifier_accuracy",
        ],
        "analysis_rate_hz": ANALYSIS_RATE,
        "control_points": CONTROL_POINTS,
        "voice_model_manifest": str(bank.manifest_path.relative_to(ROOT)),
        "voice_model_manifest_sha256": bank.status()["manifest_sha256"],
        "train_source_ids": list(bank.train_source_ids),
        "holdout_source_ids": list(bank.holdout_source_ids),
        "synthetic": vector,
        "synthetic_summary": summary,
        "holdout_aggregate": holdout_aggregate,
        "train_aggregate": train_aggregate,
        "holdout_similarity_score_0_to_1": holdout_score,
        "train_similarity_score_0_to_1": train_score,
        "holdout_feature_deltas": deltas,
        "peak": max(abs(value) for value in synthetic_samples),
        "duration_seconds": len(synthetic_samples) / 48_000,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("morph", "organic"), required=True)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    report = evaluate(args.engine)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    if not 0.0 <= float(report["holdout_similarity_score_0_to_1"]) <= 1.0:
        raise AssertionError("holdout score is outside its contract")
    if float(report["peak"]) > 0.25 + 1.0e-12:
        raise RuntimeError("evaluated voice exceeds the hard peak limit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
