#!/usr/bin/env python3
"""Cross-validate rendered whale voices against source-family trajectories.

Every outer fold excludes one complete source family from Organic trajectory
selection and scores the normalized 48-point output against that family. Source
families receive equal weight. The report is a regression indicator, not an
independent biological or perceptual test.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import sys
from collections.abc import Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_whale_voice_model import (  # noqa: E402
    ANALYSIS_RATE,
    CONTROL_POINTS,
    analyze_clip,
    downsample,
    read_bound_regular_bytes,
    read_pcm16_mono_bytes,
    regular_file_path,
    sha256_bytes,
)
from compare_whale_organic import organic_phrase_events  # noqa: E402
from whale_live_engine import WhaleVoiceConfig  # noqa: E402
from whale_morph_engine import WhaleMorphVoice  # noqa: E402
from whale_organic_engine import OrganicWhaleMorphVoice  # noqa: E402
from whale_source_filter_engine import (  # noqa: E402
    SourceFilterPoint,
    WhaleSourceFilterBank,
)

SAMPLE_RATE = 48_000
EVALUATION_DURATION_SECONDS = 8.0
EXTERNAL_EVALUATION_MANIFEST = (
    ROOT / "assets" / "whale-sources" / "evaluation" / "manifest.json"
)
EXPECTED_EXTERNAL_EVALUATION_MANIFEST_SHA256 = (
    "a7409a5b27ca03bf04a4f10d558208b985b184933bc0b1b4b9d45d43bec8a0ff"
)
SCALAR_FEATURES = (
    "envelope",
    "periodicity",
    "high_band_ratio",
    "spectral_tilt",
    "resonance_ratio_1",
    "resonance_ratio_2",
    "pulse_rate_hz",
    "pulse_strength",
    "subharmonic_strength",
    "secondary_strength",
)
FEATURE_SCALES = {
    "envelope": 0.16,
    "periodicity": 0.18,
    "high_band_ratio": 0.10,
    "spectral_tilt": 0.12,
    "resonance_ratio_1": 1.4,
    "resonance_ratio_2": 2.0,
    "pulse_rate_hz": 1.5,
    "pulse_strength": 0.18,
    "subharmonic_strength": 0.10,
    "secondary_strength": 0.10,
    "secondary_ratio": 0.45,
    "harmonic_profile_l1": 0.35,
}


def point_dict(point: SourceFilterPoint) -> dict[str, object]:
    return {
        "phase": point.phase,
        "envelope": point.envelope,
        "periodicity": point.periodicity,
        "roughness": point.roughness,
        "high_band_ratio": point.high_band_ratio,
        "spectral_tilt": point.spectral_tilt,
        "resonance_ratio_1": point.resonance_ratio_1,
        "resonance_ratio_2": point.resonance_ratio_2,
        "harmonic_profile": list(point.harmonic_profile),
        "pulse_rate_hz": point.pulse_rate_hz,
        "pulse_strength": point.pulse_strength,
        "subharmonic_strength": point.subharmonic_strength,
        "secondary_ratio": point.secondary_ratio,
        "secondary_strength": point.secondary_strength,
    }


def family_trajectory(
    bank: WhaleSourceFilterBank, source_id: str
) -> list[dict[str, object]]:
    trajectories = [
        trajectory for trajectory in bank.trajectories if trajectory.source_id == source_id
    ]
    if not trajectories:
        raise RuntimeError(f"voice model family has no trajectories: {source_id}")
    aggregate: list[dict[str, object]] = []
    for index in range(bank.control_points):
        points = [point_dict(trajectory.points[index]) for trajectory in trajectories]
        point: dict[str, object] = {
            "phase": index / (bank.control_points - 1),
            "roughness": 0.0,
        }
        for key in SCALAR_FEATURES:
            point[key] = statistics.median(float(item[key]) for item in points)
        point["roughness"] = 1.0 - float(point["periodicity"])
        strengths = [float(item["secondary_strength"]) for item in points]
        weighted = sum(
            float(item["secondary_ratio"]) * strength
            for item, strength in zip(points, strengths)
        )
        point["secondary_ratio"] = (
            weighted / sum(strengths) if sum(strengths) > 1.0e-12 else 1.0
        )
        profiles = [item["harmonic_profile"] for item in points]
        point["harmonic_profile"] = [
            statistics.median(float(profile[harmonic]) for profile in profiles)
            for harmonic in range(bank.harmonic_count)
        ]
        aggregate.append(point)
    return aggregate


def render_phrase(
    engine: str,
    *,
    excluded_source_ids: frozenset[str] = frozenset(),
) -> list[float]:
    config = WhaleVoiceConfig(
        sample_rate=SAMPLE_RATE,
        block_frames=128,
        master_gain=0.16,
    )
    if engine == "morph":
        voice = WhaleMorphVoice(config)
    elif engine == "organic":
        bank = WhaleSourceFilterBank(excluded_source_ids=excluded_source_ids)
        voice = OrganicWhaleMorphVoice(config, source_filter_bank=bank)
    else:
        raise ValueError(f"unknown whale evaluation engine: {engine}")
    output: list[float] = []
    cursor = 0
    total = round(EVALUATION_DURATION_SECONDS * SAMPLE_RATE)
    for timestamp, event in organic_phrase_events():
        target = min(round(timestamp * SAMPLE_RATE), total)
        if target > cursor:
            output.extend(voice.render(target - cursor))
            cursor = target
        if cursor >= total:
            break
        voice.dispatch(event)
    if cursor < total:
        output.extend(voice.render(total - cursor))
    return output


def synthetic_trajectory(samples: Iterable[float]) -> list[dict[str, object]]:
    reduced = downsample(samples, input_scale=1.0)
    points, _summary = analyze_clip(reduced)
    return points


def temporal_distance(
    synthetic: list[dict[str, object]], target: list[dict[str, object]]
) -> tuple[float, dict[str, float]]:
    if len(synthetic) != CONTROL_POINTS or len(target) != CONTROL_POINTS:
        raise RuntimeError("temporal trajectories do not share the control grid")
    feature_totals = {key: 0.0 for key in SCALAR_FEATURES}
    feature_totals["secondary_ratio"] = 0.0
    feature_totals["harmonic_profile_l1"] = 0.0
    for synthetic_point, target_point in zip(synthetic, target):
        for key in SCALAR_FEATURES:
            feature_totals[key] += min(
                abs(float(synthetic_point[key]) - float(target_point[key]))
                / FEATURE_SCALES[key],
                8.0,
            )
        strength = max(
            float(synthetic_point["secondary_strength"]),
            float(target_point["secondary_strength"]),
        )
        feature_totals["secondary_ratio"] += min(
            abs(
                float(synthetic_point["secondary_ratio"])
                - float(target_point["secondary_ratio"])
            )
            / FEATURE_SCALES["secondary_ratio"],
            8.0,
        ) * strength
        synthetic_profile = synthetic_point["harmonic_profile"]
        target_profile = target_point["harmonic_profile"]
        feature_totals["harmonic_profile_l1"] += min(
            sum(
                abs(float(left) - float(right))
                for left, right in zip(synthetic_profile, target_profile)
            )
            / FEATURE_SCALES["harmonic_profile_l1"],
            8.0,
        )
    feature_distances = {
        key: value / CONTROL_POINTS for key, value in feature_totals.items()
    }
    return statistics.fmean(feature_distances.values()), feature_distances


def evaluate(engine: str) -> dict[str, object]:
    base_bank = WhaleSourceFilterBank()
    folds: list[dict[str, object]] = []
    shared_morph_samples = render_phrase("morph") if engine == "morph" else None
    for source_id in base_bank.source_ids:
        target = family_trajectory(base_bank, source_id)
        if engine == "organic":
            samples = render_phrase(
                "organic", excluded_source_ids=frozenset({source_id})
            )
            excluded = [source_id]
        else:
            samples = shared_morph_samples
            excluded = []
        if samples is None:
            raise AssertionError("evaluation render is unavailable")
        synthetic = synthetic_trajectory(samples)
        distance, feature_distances = temporal_distance(synthetic, target)
        folds.append(
            {
                "source_id": source_id,
                "excluded_from_live_selection": excluded,
                "temporal_distance": distance,
                "similarity_score_0_to_1": math.exp(-distance),
                "feature_distances": feature_distances,
                "peak": max(abs(value) for value in samples),
            }
        )
    scores = [float(fold["similarity_score_0_to_1"]) for fold in folds]
    distances = [float(fold["temporal_distance"]) for fold in folds]
    return {
        "schema_version": 2,
        "kind": "humpback_whale_voice_source_family_cross_validation",
        "engine": engine,
        "method": "leave-one-source-family-out-normalized-temporal-trajectory-distance",
        "family_weighting": "equal",
        "does_not_establish": [
            "independent_unseen_dataset_generalization",
            "biological_identity",
            "perceptual_equivalence",
            "species_classifier_accuracy",
        ],
        "analysis_rate_hz": ANALYSIS_RATE,
        "control_points": CONTROL_POINTS,
        "voice_model_manifest": str(base_bank.manifest_path.relative_to(ROOT)),
        "voice_model_manifest_sha256": base_bank.manifest_sha256,
        "source_ids": list(base_bank.source_ids),
        "fold_count": len(folds),
        "folds": folds,
        "mean_similarity_score_0_to_1": statistics.fmean(scores),
        "median_similarity_score_0_to_1": statistics.median(scores),
        "mean_temporal_distance": statistics.fmean(distances),
        "duration_seconds_per_fold": EVALUATION_DURATION_SECONDS,
        "maximum_peak": max(float(fold["peak"]) for fold in folds),
    }



def evaluate_external(engine: str) -> dict[str, object]:
    manifest_path = regular_file_path(
        EXTERNAL_EVALUATION_MANIFEST,
        "independent whale evaluation manifest",
    )
    manifest_payload = read_bound_regular_bytes(
        manifest_path, "independent whale evaluation manifest"
    )
    manifest_sha256 = sha256_bytes(manifest_payload)
    if manifest_sha256 != EXPECTED_EXTERNAL_EVALUATION_MANIFEST_SHA256:
        raise RuntimeError("independent whale evaluation manifest hash mismatch")
    manifest = json.loads(manifest_payload.decode("utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("kind") != "humpback_whale_independent_evaluation_set"
        or manifest.get("model_or_parameter_tuning_forbidden") is not True
        or not isinstance(manifest.get("clips"), list)
        or len(manifest["clips"]) != 1
    ):
        raise RuntimeError("independent whale evaluation manifest is invalid")
    record = manifest["clips"][0]
    if not isinstance(record, dict):
        raise RuntimeError("independent whale evaluation record is invalid")
    expected_source_id = "noaa-pmel-alaska-winter-1999-independent"
    processed_file = record.get("processed_file")
    processed_sha = record.get("processed_sha256")
    raw_file = record.get("raw_file")
    raw_sha = record.get("raw_sha256")
    if (
        record.get("source_id") != expected_source_id
        or not isinstance(processed_file, str)
        or not isinstance(processed_sha, str)
        or not isinstance(raw_file, str)
        or not isinstance(raw_sha, str)
    ):
        raise RuntimeError("independent whale evaluation binding is incomplete")
    evaluation_root = manifest_path.parent
    raw_path = regular_file_path(evaluation_root / raw_file, "independent raw whale clip")
    raw_payload = read_bound_regular_bytes(raw_path, "independent raw whale clip")
    if sha256_bytes(raw_payload) != raw_sha:
        raise RuntimeError("independent raw whale clip hash mismatch")
    processed_path = regular_file_path(
        evaluation_root / processed_file, "independent processed whale clip"
    )
    processed_payload = read_bound_regular_bytes(
        processed_path, "independent processed whale clip"
    )
    if sha256_bytes(processed_payload) != processed_sha:
        raise RuntimeError("independent processed whale clip hash mismatch")
    pcm = read_pcm16_mono_bytes(processed_payload, str(processed_path))
    target_points, target_summary = analyze_clip(downsample(pcm))
    samples = render_phrase(engine)
    synthetic_points = synthetic_trajectory(samples)
    distance, feature_distances = temporal_distance(
        synthetic_points, target_points
    )
    bank = WhaleSourceFilterBank()
    if expected_source_id in bank.source_ids:
        raise RuntimeError("independent evaluation family leaked into voice model")
    return {
        "schema_version": 1,
        "kind": "humpback_whale_voice_independent_external_evaluation",
        "engine": engine,
        "method": "locked-external-source-normalized-temporal-trajectory-distance",
        "source_id": expected_source_id,
        "locked_at": manifest.get("locked_at"),
        "model_or_parameter_tuning_forbidden": True,
        "does_not_establish": [
            "population_generalization",
            "biological_identity",
            "perceptual_equivalence",
            "species_classifier_accuracy",
        ],
        "evaluation_manifest_sha256": manifest_sha256,
        "processed_clip_sha256": processed_sha,
        "voice_model_manifest_sha256": bank.manifest_sha256,
        "temporal_distance": distance,
        "similarity_score_0_to_1": math.exp(-distance),
        "feature_distances": feature_distances,
        "target_summary": target_summary,
        "peak": max(abs(value) for value in samples),
        "duration_seconds": len(samples) / SAMPLE_RATE,
    }

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("morph", "organic"), required=True)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--external", action="store_true")
    args = parser.parse_args()
    report = evaluate_external(args.engine) if args.external else evaluate(args.engine)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    score_key = (
        "similarity_score_0_to_1"
        if args.external
        else "mean_similarity_score_0_to_1"
    )
    score = float(report[score_key])
    if not 0.0 <= score <= 1.0:
        raise AssertionError("cross-validation score is outside its contract")
    peak_key = "peak" if args.external else "maximum_peak"
    if float(report[peak_key]) > 0.25 + 1.0e-12:
        raise RuntimeError("evaluated voice exceeds the hard peak limit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
