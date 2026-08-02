#!/usr/bin/env python3
"""Run the revision-bound Organic whale ablation and external evaluation study.

Internal candidate selection uses only equal-weight leave-one-source-family-out
cross-validation and product contracts. External evaluation is a separate,
read-only phase and can never alter the frozen candidate configuration.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import pathlib
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import evaluate_whale_voice_model as evaluator  # noqa: E402
from build_whale_voice_model import (  # noqa: E402
    analyze_clip,
    downsample,
    read_bound_regular_bytes,
    read_pcm16_mono_bytes,
    regular_file_path,
    sha256_bytes,
)
from compare_whale_organic import organic_phrase_events  # noqa: E402
from whale_live_engine import WhaleVoiceConfig  # noqa: E402
from whale_morph_engine import WhaleMorphVoice, midi_note_frequency  # noqa: E402
from whale_organic_engine import (  # noqa: E402
    ORGANIC_COMPONENT_NAMES,
    OrganicComponentConfig,
    OrganicWhaleMorphVoice,
)
from whale_source_filter_engine import WhaleSourceFilterBank  # noqa: E402

SAMPLE_RATE = 48_000
DURATION_SECONDS = evaluator.EVALUATION_DURATION_SECONDS
DEFINITION_PATH = (
    ROOT / "assets" / "whale-sources" / "studies" / "organic-ablation-v51" / "definition.json"
)
ENGINE_SOURCE_PATHS = (
    ROOT / "scripts" / "build_whale_morph_bank.py",
    ROOT / "scripts" / "build_whale_voice_model.py",
    ROOT / "scripts" / "compare_whale_organic.py",
    ROOT / "scripts" / "evaluate_whale_voice_model.py",
    ROOT / "scripts" / "study_whale_organic_ablation.py",
    ROOT / "scripts" / "whale_live_engine.py",
    ROOT / "scripts" / "whale_morph_engine.py",
    ROOT / "scripts" / "whale_organic_engine.py",
    ROOT / "scripts" / "whale_source_filter_engine.py",
    ROOT / "assets" / "whale-sources" / "morph" / "manifest.json",
    ROOT / "assets" / "whale-sources" / "voice-model" / "manifest.json",
    DEFINITION_PATH,
)
NOTE_CASES = (21, 33, 45)
COMPONENTS = tuple(ORGANIC_COMPONENT_NAMES)


@dataclass(frozen=True)
class Variant:
    variant_id: str
    enabled_components: frozenset[str]
    role: str

    def config(self) -> OrganicComponentConfig:
        return OrganicComponentConfig.from_enabled(self.enabled_components)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.variant_id,
            "role": self.role,
            "enabled_components": sorted(self.enabled_components),
            "disabled_components": sorted(set(COMPONENTS) - self.enabled_components),
        }


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_path(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_bindings() -> dict[str, str]:
    return {str(path.relative_to(ROOT)): sha256_path(path) for path in ENGINE_SOURCE_PATHS}


def load_definition() -> dict[str, Any]:
    payload = read_bound_regular_bytes(
        regular_file_path(DEFINITION_PATH, "Organic ablation definition"),
        "Organic ablation definition",
    )
    value = json.loads(payload.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("kind") != "humpback_whale_organic_ablation_definition"
        or tuple(value.get("components", ())) != COMPONENTS
        or value.get("external_data_used_for_selection") is not False
    ):
        raise RuntimeError("Organic ablation definition is invalid")
    return value


def base_variants() -> list[Variant]:
    full = frozenset(COMPONENTS)
    variants = [
        Variant("morph", frozenset(), "baseline"),
        Variant("organic-full", full, "baseline"),
    ]
    variants.extend(
        Variant(f"organic-drop-{name}", full - {name}, "leave-one-component-out")
        for name in COMPONENTS
    )
    variants.extend(
        Variant(f"organic-only-{name}", frozenset({name}), "isolated-over-morph")
        for name in COMPONENTS
    )
    return variants


def new_voice(
    variant: Variant,
    *,
    bank: WhaleSourceFilterBank | None,
) -> WhaleMorphVoice:
    config = WhaleVoiceConfig(
        sample_rate=SAMPLE_RATE,
        block_frames=128,
        master_gain=0.16,
    )
    if variant.variant_id == "morph":
        return WhaleMorphVoice(config)
    return OrganicWhaleMorphVoice(
        config,
        source_filter_bank=bank,
        component_config=variant.config(),
    )


def render_phrase(
    variant: Variant,
    *,
    bank: WhaleSourceFilterBank | None,
) -> tuple[list[float], float]:
    voice = new_voice(variant, bank=bank)
    output: list[float] = []
    cursor = 0
    total = round(DURATION_SECONDS * SAMPLE_RATE)
    started = time.perf_counter()
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
    elapsed = time.perf_counter() - started
    return output, elapsed / DURATION_SECONDS


def low_band_mean_square(samples: list[float], cutoff_hz: float = 120.0) -> float:
    alpha = 1.0 - math.exp(-2.0 * math.pi * cutoff_hz / SAMPLE_RATE)
    state = 0.0
    energy = 0.0
    for sample in samples:
        state += (sample - state) * alpha
        energy += state * state
    return energy / max(1, len(samples))


def bass_energy(
    variant: Variant,
    *,
    bank: WhaleSourceFilterBank | None,
) -> dict[str, float]:
    result: dict[str, float] = {}
    for note in NOTE_CASES:
        voice = new_voice(variant, bank=bank)
        voice.note_on(note, 80)
        samples = voice.render(SAMPLE_RATE)
        result[f"midi_{note}_below_120hz_mean_square"] = low_band_mean_square(samples)
    return result


def pitch_binding(
    variant: Variant,
    *,
    bank: WhaleSourceFilterBank | None,
) -> dict[str, object]:
    maximum_cents = 0.0
    for note in range(21, 109):
        voice = new_voice(variant, bank=bank)
        voice.note_on(note, 80)
        target = voice.target_frequency
        expected = midi_note_frequency(note)
        cents = abs(1200.0 * math.log2(target / expected))
        maximum_cents = max(maximum_cents, cents)
    return {
        "keys_checked": 88,
        "maximum_target_error_cents": maximum_cents,
        "pass": maximum_cents <= 1.0e-9,
    }


def silence_contract(
    variant: Variant,
    *,
    bank: WhaleSourceFilterBank | None,
) -> dict[str, object]:
    voice = new_voice(variant, bank=bank)
    samples = voice.render(512)
    return {
        "frames_checked": len(samples),
        "exact_zero": samples == [0.0] * len(samples),
    }


def chunk_contract(
    variant: Variant,
    *,
    bank: WhaleSourceFilterBank | None,
) -> dict[str, object]:
    left = new_voice(variant, bank=bank)
    right = new_voice(variant, bank=bank)
    for voice in (left, right):
        voice.note_on(52, 80)
        voice.control_change(1, 49)
    total = 8192
    expected = left.render(total)
    actual: list[float] = []
    sizes = (17, 111, 3, 509, 256, 729, 1, 2048, 73)
    index = 0
    while len(actual) < total:
        size = min(sizes[index % len(sizes)], total - len(actual))
        actual.extend(right.render(size))
        index += 1
    return {
        "frames_checked": total,
        "exact": expected == actual,
        "maximum_absolute_delta": max(
            (abs(a - b) for a, b in zip(expected, actual)), default=0.0
        ),
    }


def feature_metrics(feature_distances: dict[str, float]) -> dict[str, float]:
    return {
        "envelope_distance": feature_distances["envelope"],
        "periodicity_distance": feature_distances["periodicity"],
        "spectral_tilt_distance": feature_distances["spectral_tilt"],
        "high_band_distance": feature_distances["high_band_ratio"],
        "harmonic_profile_distance": feature_distances["harmonic_profile_l1"],
        "resonance_focus_distance": statistics.fmean(
            (
                feature_distances["resonance_ratio_1"],
                feature_distances["resonance_ratio_2"],
            )
        ),
        "pulse_rate_distance": feature_distances["pulse_rate_hz"],
        "pulse_strength_distance": feature_distances["pulse_strength"],
        "subharmonic_distance": feature_distances["subharmonic_strength"],
        "secondary_ratio_distance": feature_distances["secondary_ratio"],
        "secondary_strength_distance": feature_distances["secondary_strength"],
    }


def evaluate_fold(
    variant: Variant,
    source_id: str,
    base_bank: WhaleSourceFilterBank,
) -> dict[str, object]:
    bank = (
        None
        if variant.variant_id == "morph"
        else WhaleSourceFilterBank(excluded_source_ids=frozenset({source_id}))
    )
    target = evaluator.family_trajectory(base_bank, source_id)
    samples, cpu_ratio = render_phrase(variant, bank=bank)
    synthetic = evaluator.synthetic_trajectory(samples)
    distance, distances = evaluator.temporal_distance(synthetic, target)
    product = {
        "peak": max(abs(value) for value in samples),
        "cpu_seconds_per_audio_second": cpu_ratio,
        "bass_energy": bass_energy(variant, bank=bank),
        "pitch_binding": pitch_binding(variant, bank=bank),
        "silence": silence_contract(variant, bank=bank),
        "chunk_invariance": chunk_contract(variant, bank=bank),
    }
    return {
        "source_id": source_id,
        "excluded_from_live_selection": [source_id]
        if variant.variant_id != "morph"
        else [],
        "temporal_total_distance": distance,
        "similarity_score_0_to_1": math.exp(-distance),
        **feature_metrics(distances),
        "product": product,
    }


def aggregate_variant(
    variant: Variant,
    folds: list[dict[str, object]],
) -> dict[str, object]:
    similarities = [float(fold["similarity_score_0_to_1"]) for fold in folds]
    distances = [float(fold["temporal_total_distance"]) for fold in folds]
    cpu_values = [
        float(dict(fold["product"])["cpu_seconds_per_audio_second"])
        for fold in folds
    ]
    peaks = [float(dict(fold["product"])["peak"]) for fold in folds]
    bass: dict[str, dict[str, float]] = {}
    for note in NOTE_CASES:
        key = f"midi_{note}_below_120hz_mean_square"
        values = [
            float(dict(dict(fold["product"])["bass_energy"])[key]) for fold in folds
        ]
        bass[key] = {
            "mean": statistics.fmean(values),
            "minimum": min(values),
            "maximum": max(values),
        }
    product_pass = (
        max(peaks) <= 0.25 + 1.0e-12
        and max(cpu_values) <= 0.65
        and all(dict(dict(fold["product"])["pitch_binding"])["pass"] for fold in folds)
        and all(dict(dict(fold["product"])["silence"])["exact_zero"] for fold in folds)
        and all(dict(dict(fold["product"])["chunk_invariance"])["exact"] for fold in folds)
    )
    return {
        **variant.as_dict(),
        "folds": folds,
        "summary": {
            "mean_similarity": statistics.fmean(similarities),
            "median_similarity": statistics.median(similarities),
            "worst_fold_similarity": min(similarities),
            "best_fold_similarity": max(similarities),
            "similarity_variance": statistics.pvariance(similarities),
            "similarity_stddev": statistics.pstdev(similarities),
            "mean_temporal_total_distance": statistics.fmean(distances),
            "maximum_peak": max(peaks),
            "mean_cpu_seconds_per_audio_second": statistics.fmean(cpu_values),
            "maximum_cpu_seconds_per_audio_second": max(cpu_values),
            "bass_energy": bass,
            "product_contracts_pass": product_pass,
        },
    }


def annotate_against(
    records: list[dict[str, object]],
    baseline_id: str,
) -> None:
    index = {str(record["id"]): record for record in records}
    baseline = index[baseline_id]
    baseline_folds = {
        str(fold["source_id"]): float(fold["similarity_score_0_to_1"])
        for fold in baseline["folds"]
    }
    for record in records:
        deltas = [
            float(fold["similarity_score_0_to_1"])
            - baseline_folds[str(fold["source_id"])]
            for fold in record["folds"]
        ]
        record["comparison_to_morph"] = {
            "mean_similarity_delta": statistics.fmean(deltas),
            "median_similarity_delta": statistics.median(deltas),
            "worst_family_delta": min(deltas),
            "improved_family_count": sum(delta > 1.0e-12 for delta in deltas),
            "unchanged_family_count": sum(abs(delta) <= 1.0e-12 for delta in deltas),
            "worsened_family_count": sum(delta < -1.0e-12 for delta in deltas),
        }


def component_evidence(
    records: list[dict[str, object]],
    definition: dict[str, Any],
) -> list[dict[str, object]]:
    index = {str(record["id"]): record for record in records}
    full = index["organic-full"]
    full_folds = {
        str(fold["source_id"]): float(fold["similarity_score_0_to_1"])
        for fold in full["folds"]
    }
    thresholds = definition["component_qualification"]
    result: list[dict[str, object]] = []
    for component in COMPONENTS:
        isolated = index[f"organic-only-{component}"]
        dropped = index[f"organic-drop-{component}"]
        isolated_cmp = dict(isolated["comparison_to_morph"])
        drop_deltas = [
            float(fold["similarity_score_0_to_1"])
            - full_folds[str(fold["source_id"])]
            for fold in dropped["folds"]
        ]
        disabling_mean_delta = statistics.fmean(drop_deltas)
        disabling_worsened = sum(delta < -1.0e-12 for delta in drop_deltas)
        qualifies = (
            float(isolated_cmp["mean_similarity_delta"])
            >= float(thresholds["minimum_isolated_mean_delta"])
            and int(isolated_cmp["improved_family_count"])
            >= int(thresholds["minimum_isolated_improved_families"])
            and float(isolated_cmp["worst_family_delta"])
            >= -float(thresholds["maximum_isolated_worst_family_loss"])
            and disabling_mean_delta
            <= -float(thresholds["minimum_leave_out_mean_harm"])
            and disabling_worsened
            >= int(thresholds["minimum_leave_out_worsened_families"])
        )
        result.append(
            {
                "component": component,
                "isolated_mean_similarity_delta_vs_morph": isolated_cmp[
                    "mean_similarity_delta"
                ],
                "isolated_worst_family_delta_vs_morph": isolated_cmp[
                    "worst_family_delta"
                ],
                "isolated_improved_family_count": isolated_cmp[
                    "improved_family_count"
                ],
                "isolated_worsened_family_count": isolated_cmp[
                    "worsened_family_count"
                ],
                "leave_out_mean_similarity_delta_vs_full": disabling_mean_delta,
                "leave_out_worsened_family_count_vs_full": disabling_worsened,
                "qualifies_for_combinations": qualifies,
            }
        )
    return result


def combination_variants(evidence: list[dict[str, object]]) -> list[Variant]:
    qualified = [
        str(item["component"])
        for item in sorted(
            evidence,
            key=lambda item: (
                -float(item["isolated_mean_similarity_delta_vs_morph"]),
                str(item["component"]),
            ),
        )
        if item["qualifies_for_combinations"]
    ]
    enabled_sets: list[frozenset[str]] = []
    if len(qualified) >= 2:
        enabled_sets.append(frozenset(qualified[:2]))
    if len(qualified) >= 3:
        enabled_sets.append(frozenset(qualified[:3]))
    if len(qualified) >= 2:
        enabled_sets.append(frozenset(qualified))
    unique: list[frozenset[str]] = []
    for enabled in enabled_sets:
        if enabled not in unique:
            unique.append(enabled)
    return [
        Variant(
            "organic-combo-" + "-".join(name.replace("_", "-") for name in sorted(enabled)),
            enabled,
            "qualified-combination",
        )
        for enabled in unique[:3]
    ]


def pareto_front(records: list[dict[str, object]]) -> list[str]:
    candidates = [
        record
        for record in records
        if dict(record["summary"])["product_contracts_pass"]
    ]
    front: list[str] = []
    for candidate in candidates:
        summary = dict(candidate["summary"])
        dominated = False
        for other in candidates:
            if other is candidate:
                continue
            other_summary = dict(other["summary"])
            no_worse = (
                float(other_summary["mean_similarity"])
                >= float(summary["mean_similarity"])
                and float(other_summary["worst_fold_similarity"])
                >= float(summary["worst_fold_similarity"])
                and float(other_summary["mean_cpu_seconds_per_audio_second"])
                <= float(summary["mean_cpu_seconds_per_audio_second"])
            )
            strictly_better = (
                float(other_summary["mean_similarity"])
                > float(summary["mean_similarity"])
                or float(other_summary["worst_fold_similarity"])
                > float(summary["worst_fold_similarity"])
                or float(other_summary["mean_cpu_seconds_per_audio_second"])
                < float(summary["mean_cpu_seconds_per_audio_second"])
            )
            if no_worse and strictly_better:
                dominated = True
                break
        if not dominated:
            front.append(str(candidate["id"]))
    return sorted(front)


def choose_candidate(
    records: list[dict[str, object]],
    definition: dict[str, Any],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    index = {str(record["id"]): record for record in records}
    morph = index["morph"]
    full = index["organic-full"]
    morph_summary = dict(morph["summary"])
    full_summary = dict(full["summary"])
    criteria = definition["candidate_selection"]
    eligible: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    for record in records:
        summary = dict(record["summary"])
        comparison = dict(record["comparison_to_morph"])
        reasons: list[str] = []
        if record["id"] == "morph":
            reasons.append("stable_fallback")
            passes = True
        else:
            bass_ratios: dict[str, float] = {}
            for note in (21, 33):
                key = f"midi_{note}_below_120hz_mean_square"
                candidate_bass = float(dict(summary["bass_energy"])[key]["mean"])
                full_bass = float(dict(full_summary["bass_energy"])[key]["mean"])
                ratio = candidate_bass / full_bass if full_bass > 0.0 else 1.0
                bass_ratios[key] = ratio
            record["bass_ratios_vs_full_organic"] = bass_ratios
            if min(bass_ratios.values()) < float(
                criteria["minimum_a0_a1_bass_ratio_vs_full_organic"]
            ):
                reasons.append("a0_a1_bass_regression")
            if not summary["product_contracts_pass"]:
                reasons.append("product_contract_failure")
            if float(comparison["mean_similarity_delta"]) < float(
                criteria["minimum_mean_similarity_gain"]
            ):
                reasons.append("insufficient_mean_gain")
            if int(comparison["improved_family_count"]) < int(
                criteria["minimum_improved_families"]
            ):
                reasons.append("too_few_improved_families")
            if float(comparison["worst_family_delta"]) < -float(
                criteria["maximum_worst_family_loss"]
            ):
                reasons.append("worst_family_loss")
            if float(summary["similarity_variance"]) > float(
                morph_summary["similarity_variance"]
            ) * float(criteria["maximum_variance_multiple_vs_morph"]):
                reasons.append("unstable_across_families")
            if float(summary["maximum_cpu_seconds_per_audio_second"]) > float(
                criteria["maximum_cpu_seconds_per_audio_second"]
            ):
                reasons.append("cpu_cost")
            passes = not reasons
        decision = {
            "variant_id": record["id"],
            "eligible": passes,
            "reasons": reasons,
        }
        decisions.append(decision)
        if passes:
            eligible.append(record)
    non_morph = [record for record in eligible if record["id"] != "morph"]
    if not non_morph:
        return morph, decisions
    selected = max(
        non_morph,
        key=lambda record: (
            float(dict(record["summary"])["mean_similarity"]),
            float(dict(record["summary"])["worst_fold_similarity"]),
            -float(dict(record["summary"])["similarity_variance"]),
            -float(dict(record["summary"])["mean_cpu_seconds_per_audio_second"]),
            -len(record["enabled_components"]),
            str(record["id"]),
        ),
    )
    return selected, decisions


def acoustic_fingerprint(records: list[dict[str, object]]) -> str:
    reduced: list[dict[str, object]] = []
    for record in records:
        folds = []
        for fold in record["folds"]:
            folds.append(
                {
                    key: value
                    for key, value in fold.items()
                    if key != "product"
                }
            )
        summary = {
            key: value
            for key, value in dict(record["summary"]).items()
            if "cpu" not in key
        }
        reduced.append(
            {
                "id": record["id"],
                "enabled_components": record["enabled_components"],
                "folds": folds,
                "summary": summary,
            }
        )
    return hashlib.sha256(canonical_json(reduced)).hexdigest()


def csv_summary(records: list[dict[str, object]]) -> str:
    stream = io.StringIO(newline="")
    fields = [
        "variant_id",
        "role",
        "enabled_components",
        "mean_similarity",
        "median_similarity",
        "worst_fold_similarity",
        "similarity_variance",
        "improved_families_vs_morph",
        "worsened_families_vs_morph",
        "maximum_peak",
        "mean_cpu_seconds_per_audio_second",
        "maximum_cpu_seconds_per_audio_second",
        "product_contracts_pass",
    ]
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for record in records:
        summary = dict(record["summary"])
        comparison = dict(record["comparison_to_morph"])
        writer.writerow(
            {
                "variant_id": record["id"],
                "role": record["role"],
                "enabled_components": ";".join(record["enabled_components"]),
                "mean_similarity": f"{float(summary['mean_similarity']):.9f}",
                "median_similarity": f"{float(summary['median_similarity']):.9f}",
                "worst_fold_similarity": f"{float(summary['worst_fold_similarity']):.9f}",
                "similarity_variance": f"{float(summary['similarity_variance']):.12f}",
                "improved_families_vs_morph": comparison["improved_family_count"],
                "worsened_families_vs_morph": comparison["worsened_family_count"],
                "maximum_peak": f"{float(summary['maximum_peak']):.9f}",
                "mean_cpu_seconds_per_audio_second": f"{float(summary['mean_cpu_seconds_per_audio_second']):.6f}",
                "maximum_cpu_seconds_per_audio_second": f"{float(summary['maximum_cpu_seconds_per_audio_second']):.6f}",
                "product_contracts_pass": str(summary["product_contracts_pass"]).lower(),
            }
        )
    return stream.getvalue()


def run_internal(args: argparse.Namespace) -> dict[str, object]:
    definition = load_definition()
    base_bank = WhaleSourceFilterBank()
    records: list[dict[str, object]] = []
    for variant in base_variants():
        print(f"internal variant: {variant.variant_id}", file=sys.stderr, flush=True)
        folds = [
            evaluate_fold(variant, source_id, base_bank)
            for source_id in base_bank.source_ids
        ]
        records.append(aggregate_variant(variant, folds))
    annotate_against(records, "morph")
    evidence = component_evidence(records, definition)
    combinations = combination_variants(evidence)
    for variant in combinations:
        print(f"internal variant: {variant.variant_id}", file=sys.stderr, flush=True)
        folds = [
            evaluate_fold(variant, source_id, base_bank)
            for source_id in base_bank.source_ids
        ]
        records.append(aggregate_variant(variant, folds))
    annotate_against(records, "morph")
    selected, selection_decisions = choose_candidate(records, definition)
    report: dict[str, object] = {
        "schema_version": 1,
        "kind": "humpback_whale_organic_ablation_internal_study",
        "source_revision": args.source_revision,
        "definition": {
            "path": str(DEFINITION_PATH.relative_to(ROOT)),
            "sha256": sha256_path(DEFINITION_PATH),
        },
        "source_bindings": source_bindings(),
        "voice_model_manifest_sha256": base_bank.manifest_sha256,
        "method": "equal-weight-leave-one-source-family-out-component-ablation",
        "external_data_used_for_selection": False,
        "periodicity_complement_double_weighted": False,
        "source_ids": list(base_bank.source_ids),
        "component_evidence": evidence,
        "variants": records,
        "pareto_front_variant_ids": pareto_front(records),
        "selection_decisions": selection_decisions,
        "selected_candidate_id": selected["id"],
        "selected_candidate_enabled_components": selected["enabled_components"],
        "acoustic_fingerprint_sha256": acoustic_fingerprint(records),
        "does_not_establish": [
            "independent_external_generalization",
            "biological_identity",
            "perceptual_equivalence",
            "subjective_naturalness",
        ],
    }
    report_payload = canonical_json(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(report_payload)
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    args.csv.write_text(csv_summary(records), encoding="utf-8")
    candidate = {
        "schema_version": 1,
        "kind": "humpback_whale_organic_frozen_candidate",
        "candidate_id": selected["id"],
        "enabled_components": selected["enabled_components"],
        "source_revision": args.source_revision,
        "source_bindings": source_bindings(),
        "definition_sha256": sha256_path(DEFINITION_PATH),
        "internal_report_sha256": hashlib.sha256(report_payload).hexdigest(),
        "voice_model_manifest_sha256": base_bank.manifest_sha256,
        "external_data_used_for_selection": False,
        "parameters_frozen_before_external_evaluation": True,
    }
    args.candidate.parent.mkdir(parents=True, exist_ok=True)
    args.candidate.write_bytes(canonical_json(candidate))
    return report


def load_frozen_candidate(path: pathlib.Path) -> tuple[dict[str, Any], Variant]:
    payload = read_bound_regular_bytes(
        regular_file_path(path, "frozen Organic candidate"),
        "frozen Organic candidate",
    )
    value = json.loads(payload.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("kind") != "humpback_whale_organic_frozen_candidate"
        or value.get("external_data_used_for_selection") is not False
        or value.get("parameters_frozen_before_external_evaluation") is not True
    ):
        raise RuntimeError("frozen Organic candidate is invalid")
    expected_bindings = value.get("source_bindings")
    if expected_bindings != source_bindings():
        raise RuntimeError("frozen Organic candidate source binding changed")
    enabled = value.get("enabled_components")
    if not isinstance(enabled, list) or not all(isinstance(item, str) for item in enabled):
        raise RuntimeError("frozen Organic candidate component set is invalid")
    variant = Variant(str(value["candidate_id"]), frozenset(enabled), "frozen-candidate")
    variant.config()
    return value, variant


def load_external_manifest(path: pathlib.Path) -> tuple[str, dict[str, Any]]:
    safe = regular_file_path(path, "external whale evaluation manifest")
    payload = read_bound_regular_bytes(safe, "external whale evaluation manifest")
    value = json.loads(payload.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("kind") != "humpback_whale_independent_evaluation_set"
        or value.get("model_or_parameter_tuning_forbidden") is not True
        or not isinstance(value.get("clips"), list)
        or not value["clips"]
    ):
        raise RuntimeError("external whale evaluation manifest is invalid")
    return sha256_bytes(payload), value


def external_clip_target(
    manifest_path: pathlib.Path,
    record: dict[str, Any],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    required = (
        "source_id",
        "raw_file",
        "raw_sha256",
        "processed_file",
        "processed_sha256",
    )
    if not all(isinstance(record.get(key), str) and record[key] for key in required):
        raise RuntimeError("external evaluation record binding is incomplete")
    root = manifest_path.parent
    raw_path = regular_file_path(root / record["raw_file"], "external raw whale clip")
    raw_payload = read_bound_regular_bytes(raw_path, "external raw whale clip")
    if sha256_bytes(raw_payload) != record["raw_sha256"]:
        raise RuntimeError("external raw whale clip hash mismatch")
    processed_path = regular_file_path(
        root / record["processed_file"], "external processed whale clip"
    )
    processed_payload = read_bound_regular_bytes(
        processed_path, "external processed whale clip"
    )
    if sha256_bytes(processed_payload) != record["processed_sha256"]:
        raise RuntimeError("external processed whale clip hash mismatch")
    pcm = read_pcm16_mono_bytes(processed_payload, str(processed_path))
    points, summary = analyze_clip(downsample(pcm))
    return points, {
        "source_id": record["source_id"],
        "license": record.get("license"),
        "source_url": record.get("source_url"),
        "raw_sha256": record["raw_sha256"],
        "processed_sha256": record["processed_sha256"],
        "target_summary": summary,
        "population": record.get("population"),
        "call_type": record.get("call_type"),
        "recording_conditions": record.get("recording_conditions"),
    }


def evaluate_external_variant(
    variant: Variant,
    target: list[dict[str, object]],
) -> dict[str, object]:
    bank = None if variant.variant_id == "morph" else WhaleSourceFilterBank()
    samples, cpu_ratio = render_phrase(variant, bank=bank)
    synthetic = evaluator.synthetic_trajectory(samples)
    distance, distances = evaluator.temporal_distance(synthetic, target)
    return {
        "variant_id": variant.variant_id,
        "enabled_components": sorted(variant.enabled_components),
        "temporal_total_distance": distance,
        "similarity_score_0_to_1": math.exp(-distance),
        **feature_metrics(distances),
        "peak": max(abs(value) for value in samples),
        "cpu_seconds_per_audio_second": cpu_ratio,
    }


def external_summary(clips: list[dict[str, object]], variant_id: str) -> dict[str, object]:
    values = [
        float(next(item for item in clip["results"] if item["variant_id"] == variant_id)["similarity_score_0_to_1"])
        for clip in clips
    ]
    return {
        "variant_id": variant_id,
        "clip_count": len(values),
        "mean_similarity": statistics.fmean(values),
        "median_similarity": statistics.median(values),
        "worst_clip_similarity": min(values),
        "best_clip_similarity": max(values),
        "similarity_variance": statistics.pvariance(values),
    }


def run_external(args: argparse.Namespace) -> dict[str, object]:
    candidate_value, candidate = load_frozen_candidate(args.candidate)
    manifests = [evaluator.EXTERNAL_EVALUATION_MANIFEST]
    manifests.extend(args.additional_manifest)
    bank = WhaleSourceFilterBank()
    candidate_evaluation = Variant(
        "frozen-candidate", candidate.enabled_components, "frozen-candidate"
    )
    variants = [
        Variant("morph", frozenset(), "baseline"),
        Variant("organic-full", frozenset(COMPONENTS), "baseline"),
        candidate_evaluation,
    ]
    unique: dict[str, Variant] = {variant.variant_id: variant for variant in variants}
    clips: list[dict[str, object]] = []
    manifest_bindings: list[dict[str, str]] = []
    seen_sources: set[str] = set()
    for manifest_path in manifests:
        manifest_sha, manifest = load_external_manifest(manifest_path)
        if manifest_path == evaluator.EXTERNAL_EVALUATION_MANIFEST and manifest_sha != evaluator.EXPECTED_EXTERNAL_EVALUATION_MANIFEST_SHA256:
            raise RuntimeError("locked NOAA-PMEL evaluation manifest changed")
        manifest_bindings.append(
            {
                "path": str(manifest_path.relative_to(ROOT)),
                "sha256": manifest_sha,
            }
        )
        for raw_record in manifest["clips"]:
            if not isinstance(raw_record, dict):
                raise RuntimeError("external evaluation clip record is invalid")
            target, metadata = external_clip_target(manifest_path, raw_record)
            source_id = str(metadata["source_id"])
            print(f"external source: {source_id}", file=sys.stderr, flush=True)
            if source_id in seen_sources:
                raise RuntimeError("external evaluation source IDs must be unique")
            seen_sources.add(source_id)
            if source_id in bank.source_ids:
                raise RuntimeError("external evaluation family leaked into voice model")
            results = [
                evaluate_external_variant(variant, target)
                for variant in unique.values()
            ]
            clips.append({**metadata, "results": results})
    summaries = [external_summary(clips, variant_id) for variant_id in unique]
    report = {
        "schema_version": 1,
        "kind": "humpback_whale_organic_external_generalization_study",
        "candidate": {
            "path": str(args.candidate.relative_to(ROOT)),
            "sha256": sha256_path(args.candidate),
            "candidate_id": candidate.variant_id,
            "evaluation_variant_id": candidate_evaluation.variant_id,
            "enabled_components": sorted(candidate.enabled_components),
            "source_revision": candidate_value["source_revision"],
            "internal_report_sha256": candidate_value["internal_report_sha256"],
        },
        "model_or_parameter_tuning_forbidden": True,
        "parameters_changed_after_external_results": False,
        "voice_model_manifest_sha256": bank.manifest_sha256,
        "evaluation_manifests": manifest_bindings,
        "clip_count": len(clips),
        "clips": clips,
        "summaries": summaries,
        "does_not_establish": [
            "biological_identity",
            "perceptual_equivalence",
            "complete_population_generalization",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json(report))
    return report


def current_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    internal = subparsers.add_parser("internal")
    internal.add_argument("--output", type=pathlib.Path, required=True)
    internal.add_argument("--csv", type=pathlib.Path, required=True)
    internal.add_argument("--candidate", type=pathlib.Path, required=True)
    internal.add_argument("--source-revision", default=current_revision())
    external = subparsers.add_parser("external")
    external.add_argument("--candidate", type=pathlib.Path, required=True)
    external.add_argument("--output", type=pathlib.Path, required=True)
    external.add_argument(
        "--additional-manifest", type=pathlib.Path, action="append", default=[]
    )
    args = parser.parse_args()
    report = run_internal(args) if args.command == "internal" else run_external(args)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
