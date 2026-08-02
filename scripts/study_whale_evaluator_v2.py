#!/usr/bin/env python3
"""Build the preregistered controlled and locked-test evaluator-v2 reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import random
import statistics
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import evaluate_whale_f0_v2 as evaluator  # noqa: E402
from build_whale_morph_bank import (  # noqa: E402
    read_bound_regular_bytes,
    regular_file_path,
    sha256_bytes,
)
from build_whale_voice_model import (  # noqa: E402
    ANALYSIS_RATE,
    CONTROL_POINTS,
    clamp,
    downsample,
    normalized_autocorrelation,
    windowed_frame,
)

STUDY_ROOT = ROOT / "assets" / "whale-sources" / "studies" / "evaluator-v2"
REFERENCE_REPORT = STUDY_ROOT / "reference-corpus.json"
SENSITIVITY_REPORT = STUDY_ROOT / "sensitivity-report.json"
FROZEN_DEFINITION_COMMIT = "bfe237b4fa21a89a712ad49b4bde709ab46d6106"


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_report(path: pathlib.Path, value: object) -> str:
    payload = canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def definition_commit() -> str:
    # The final preregistration commit is part of the scientific contract.
    # Do not derive it from local Git history: CI and source archives may be shallow.
    return FROZEN_DEFINITION_COMMIT


def sine_wave(frequency: float, seconds: float, amplitude: float = 0.75) -> list[float]:
    frames = round(seconds * evaluator.SAMPLE_RATE)
    return [
        amplitude * math.sin(2.0 * math.pi * frequency * index / evaluator.SAMPLE_RATE)
        for index in range(frames)
    ]


def deterministic_noise(seconds: float, seed: int, amplitude: float = 0.75) -> list[float]:
    generator = random.Random(seed)
    return [
        amplitude * (2.0 * generator.random() - 1.0)
        for _ in range(round(seconds * evaluator.SAMPLE_RATE))
    ]


def harmonic_complex(fundamental: float, seconds: float) -> list[float]:
    frames = round(seconds * evaluator.SAMPLE_RATE)
    components = ((2, 0.55), (3, 0.35), (4, 0.22), (5, 0.12))
    return [
        sum(
            amplitude
            * math.sin(
                2.0 * math.pi * fundamental * harmonic * index / evaluator.SAMPLE_RATE
            )
            for harmonic, amplitude in components
        )
        / sum(amplitude for _, amplitude in components)
        for index in range(frames)
    ]


def pulsed_tone(frequency: float, seconds: float) -> list[float]:
    frames = round(seconds * evaluator.SAMPLE_RATE)
    return [
        0.75
        * (0.25 + 0.75 * max(0.0, math.sin(2.0 * math.pi * 3.0 * index / evaluator.SAMPLE_RATE)))
        * math.sin(2.0 * math.pi * frequency * index / evaluator.SAMPLE_RATE)
        for index in range(frames)
    ]


def ship_noise_mixture(frequency: float, seconds: float, seed: int) -> list[float]:
    noise = deterministic_noise(seconds, seed, 0.22)
    output: list[float] = []
    for index, random_value in enumerate(noise):
        time = index / evaluator.SAMPLE_RATE
        output.append(
            0.48 * math.sin(2.0 * math.pi * frequency * time)
            + 0.16 * math.sin(2.0 * math.pi * 37.0 * time)
            + 0.10 * math.sin(2.0 * math.pi * 74.0 * time)
            + random_value
        )
    return output


def controlled_samples(case: dict[str, Any], seconds: float, seed: int) -> list[int | float]:
    case_id = case["id"]
    if case_id == "tone-80":
        return sine_wave(80.0, seconds)
    if case_id == "tone-220":
        return sine_wave(220.0, seconds)
    if case_id == "tone-520":
        return sine_wave(520.0, seconds)
    if case_id == "missing-fundamental-110":
        return harmonic_complex(110.0, seconds)
    if case_id == "pulsed-tone-150":
        return pulsed_tone(150.0, seconds)
    if case_id == "tone-180-with-ship-noise":
        return ship_noise_mixture(180.0, seconds, seed)
    if case_id == "white-noise":
        return deterministic_noise(seconds, seed)
    if case_id == "upper-bound-tone-800":
        return sine_wave(800.0, seconds)
    if case_id == "independent-whale-annotation-105hz":
        path = regular_file_path(ROOT / case["source_clip"], "annotated whale clip")
        payload = read_bound_regular_bytes(path, "annotated whale clip")
        if sha256_bytes(payload) != case["source_sha256"]:
            raise RuntimeError("annotated whale clip hash mismatch")
        annotation_path = regular_file_path(
            ROOT / case["annotation_source"], "prior whale annotation"
        )
        annotation = json.loads(
            read_bound_regular_bytes(annotation_path, "prior whale annotation").decode("utf-8")
        )
        anchor = next(
            item
            for item in annotation["anchors"]
            if item.get("clip_id") == case["annotation_clip_id"]
        )
        if (
            anchor.get("source_sha256") != case["source_sha256"]
            or abs(float(anchor["estimated_source_frequency_hz"]) - float(case["expected_f0_hz"]))
            > 1.0e-12
        ):
            raise RuntimeError("prior whale annotation binding mismatch")
        samples = evaluator.decode_pcm16_mono(payload, str(path))
        start = int(anchor["analysis_start_frame"])
        # The prior annotation was computed over the Morph builder's exact
        # 2000-sample window at 4 kHz (0.5 seconds at 48 kHz).
        frames = round(0.5 * evaluator.SAMPLE_RATE)
        return samples[start : start + frames]
    raise RuntimeError(f"unknown controlled evaluator case: {case_id}")


def assess_controlled(case: dict[str, Any], result: dict[str, object]) -> dict[str, object]:
    summary = result["summary"]
    if not isinstance(summary, dict):
        raise AssertionError("evaluator summary lost its type")
    checks: dict[str, object] = {}
    expected = case.get("expected_f0_hz")
    median = summary.get("median_f0_hz")
    if isinstance(expected, (int, float)):
        relative_error = (
            abs(float(median) - float(expected)) / float(expected)
            if isinstance(median, (int, float))
            else None
        )
        checks["relative_f0_error"] = relative_error
        checks["f0_error_pass"] = (
            relative_error is not None
            and relative_error <= float(case["maximum_relative_error"])
        )
    if "minimum_voiced_fraction" in case:
        checks["voiced_fraction_pass"] = float(summary["voiced_fraction"]) >= float(
            case["minimum_voiced_fraction"]
        )
    if "maximum_voiced_fraction" in case:
        checks["voiced_fraction_pass"] = float(summary["voiced_fraction"]) <= float(
            case["maximum_voiced_fraction"]
        )
    if "maximum_voiced_boundary_hits" in case:
        checks["voiced_boundary_pass"] = int(summary["voiced_boundary_hits"]) <= int(
            case["maximum_voiced_boundary_hits"]
        )
        reason_counts = summary.get("reason_counts", {})
        checks["expected_reason_pass"] = (
            isinstance(reason_counts, dict)
            and int(reason_counts.get(str(case["expected_reason"]), 0)) > 0
        )
    checks["pass"] = all(value is True for key, value in checks.items() if key.endswith("_pass"))
    return checks


def build_controlled_report() -> dict[str, object]:
    definition, definition_sha256 = evaluator.load_definition()
    corpus = definition["controlled_reference_corpus"]
    seconds = float(corpus["duration_seconds"])
    seed = int(corpus["deterministic_noise_seed"])
    cases: list[dict[str, object]] = []
    for raw_case in corpus["cases"]:
        case = dict(raw_case)
        samples = controlled_samples(case, seconds, seed)
        input_scale = 32768.0 if all(isinstance(value, int) for value in samples[:64]) else 1.0
        result = evaluator.analyze_samples(
            samples,
            source_nyquist_hz=24000.0,
            input_scale=input_scale,
        )
        assessment = assess_controlled(case, result)
        cases.append(
            {
                "id": case["id"],
                "kind": case["kind"],
                "expectation": case,
                "result": result,
                "assessment": assessment,
            }
        )
    return {
        "schema_version": 1,
        "kind": "humpback_whale_f0_evaluator_v2_controlled_report",
        "definition": {
            "path": str(evaluator.DEFINITION_PATH.relative_to(ROOT)),
            "sha256": definition_sha256,
            "commit": definition_commit(),
        },
        "all_pass": all(bool(case["assessment"]["pass"]) for case in cases),
        "cases": cases,
        "does_not_establish": [
            "biological f0 truth",
            "external generalization",
            "engine release approval",
        ],
    }


def legacy_frame(frame: list[float]) -> dict[str, object]:
    minimum_lag = max(2, round(ANALYSIS_RATE / 1200.0))
    maximum_lag = min(len(frame) // 2, round(ANALYSIS_RATE / 28.0))
    scores = [
        normalized_autocorrelation(frame, lag)
        for lag in range(minimum_lag, maximum_lag + 1)
    ]
    best_offset = max(range(len(scores)), key=scores.__getitem__)
    best_lag = minimum_lag + best_offset
    best_score = clamp(scores[best_offset], 0.0, 1.0)
    doubled = best_lag * 2
    if doubled <= maximum_lag:
        doubled_score = normalized_autocorrelation(frame, doubled)
        if doubled_score >= best_score * 0.92:
            best_lag = doubled
            best_score = max(best_score, doubled_score)
    return {
        "selected_lag": best_lag,
        "periodicity": round(best_score, 8),
        "f0_hz": round(ANALYSIS_RATE / best_lag, 8) if best_score >= 0.20 else None,
        "voiced": best_score >= 0.20,
    }


def legacy_analyze(samples: list[int]) -> dict[str, object]:
    reduced = downsample(samples)
    frame_size = round(0.18 * ANALYSIS_RATE)
    frames: list[dict[str, object]] = []
    for index in range(CONTROL_POINTS):
        phase = index / (CONTROL_POINTS - 1)
        center = round(phase * max(0, len(reduced) - 1))
        frames.append(legacy_frame(windowed_frame(reduced, center, frame_size)))
    voiced = [float(frame["f0_hz"]) for frame in frames if frame["f0_hz"] is not None]
    return {
        "summary": {
            "median_f0_hz": round(statistics.median(voiced), 8) if voiced else None,
            "voiced_fraction": round(len(voiced) / CONTROL_POINTS, 8),
            "lag_3_hits": sum(frame["selected_lag"] == 3 for frame in frames),
            "lag_3_voiced_hits": sum(
                frame["selected_lag"] == 3 and bool(frame["voiced"]) for frame in frames
            ),
        },
        "frames": frames,
    }


def external_records(definition: dict[str, Any]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for binding in definition["locked_external_sets"]:
        manifest_path = regular_file_path(ROOT / binding["path"], "locked manifest")
        manifest_payload = read_bound_regular_bytes(manifest_path, "locked manifest")
        manifest = json.loads(manifest_payload.decode("utf-8"))
        if manifest.get("model_or_parameter_tuning_forbidden") is not True:
            raise RuntimeError("external manifest is not locked against tuning")
        for raw_clip in manifest["clips"]:
            clip = dict(raw_clip)
            path = regular_file_path(
                manifest_path.parent / clip["processed_file"], "locked external clip"
            )
            payload = read_bound_regular_bytes(path, "locked external clip")
            if sha256_bytes(payload) != clip["processed_sha256"]:
                raise RuntimeError(f"external clip hash mismatch: {clip.get('source_id')}")
            records.append(
                {
                    "manifest_path": str(manifest_path.relative_to(ROOT)),
                    "manifest_sha256": sha256_bytes(manifest_payload),
                    "clip": clip,
                    "path": path,
                    "payload": payload,
                }
            )
    return records


def recording_aggregate(segments: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for segment in segments:
        grouped.setdefault(str(segment["source_recording_id"]), []).append(segment)
    output: list[dict[str, object]] = []
    for recording_id, values in sorted(grouped.items()):
        old_voiced = [float(value["legacy"]["summary"]["voiced_fraction"]) for value in values]
        new_voiced = [float(value["v2"]["summary"]["voiced_fraction"]) for value in values]
        output.append(
            {
                "source_recording_id": recording_id,
                "segment_count": len(values),
                "legacy_lag_3_hits": sum(
                    int(value["legacy"]["summary"]["lag_3_hits"]) for value in values
                ),
                "legacy_lag_3_voiced_hits": sum(
                    int(value["legacy"]["summary"]["lag_3_voiced_hits"])
                    for value in values
                ),
                "legacy_mean_voiced_fraction": round(statistics.fmean(old_voiced), 8),
                "v2_mean_voiced_fraction": round(statistics.fmean(new_voiced), 8),
                "v2_voiced_boundary_hits": sum(
                    int(value["v2"]["summary"]["voiced_boundary_hits"]) for value in values
                ),
            }
        )
    return output


def build_external_report() -> dict[str, object]:
    definition, definition_sha256 = evaluator.load_definition()
    segments: list[dict[str, object]] = []
    for record in external_records(definition):
        clip = record["clip"]
        payload = record["payload"]
        if not isinstance(clip, dict) or not isinstance(payload, bytes):
            raise AssertionError("validated external record lost its type")
        samples = evaluator.decode_pcm16_mono(payload, str(record["path"]))
        source_nyquist = float(
            clip.get("source_nyquist_hz", float(clip.get("sample_rate_hz", 48000)) / 2)
        )
        segments.append(
            {
                "source_id": clip["source_id"],
                "source_recording_id": clip.get("source_recording_id", clip["source_id"]),
                "population": clip.get("population"),
                "recording_conditions": clip.get("recording_conditions"),
                "processed_file": str(record["path"].relative_to(ROOT)),
                "processed_sha256": clip["processed_sha256"],
                "source_sample_rate_hz": clip.get("source_sample_rate_hz", clip.get("sample_rate_hz")),
                "source_nyquist_hz": source_nyquist,
                "legacy": legacy_analyze(samples),
                "v2": evaluator.analyze_samples(
                    samples,
                    source_nyquist_hz=source_nyquist,
                ),
            }
        )
    contract = definition["external_success_contract"]
    stellwagen = [
        segment
        for segment in segments
        if segment["source_recording_id"] == "stellwagen"
    ]
    observed = [
        int(segment["legacy"]["summary"]["lag_3_voiced_hits"])
        for segment in stellwagen[:3]
    ]
    expected = [int(value) for value in contract["legacy_stellwagen_boundary_counts_first_three"]]
    v2_voiced_boundary_hits = sum(
        int(segment["v2"]["summary"]["voiced_boundary_hits"]) for segment in segments
    )
    checks = {
        "legacy_saturation_reproduced": observed == expected,
        "observed_legacy_stellwagen_voiced_counts": observed,
        "expected_legacy_stellwagen_voiced_counts": expected,
        "counting_semantics": "lag-3 frames that also satisfy the frozen legacy voiced threshold",
        "v2_voiced_boundary_hits": v2_voiced_boundary_hits,
        "v2_boundary_contract_pass": v2_voiced_boundary_hits
        == int(contract["new_voiced_boundary_hits_must_equal"]),
    }
    return {
        "schema_version": 1,
        "kind": "humpback_whale_f0_evaluator_v2_sensitivity_report",
        "definition": {
            "path": str(evaluator.DEFINITION_PATH.relative_to(ROOT)),
            "sha256": definition_sha256,
            "commit": definition_commit(),
        },
        "locked_test_only": True,
        "parameter_or_threshold_selection_from_external": False,
        "segments": segments,
        "recordings": recording_aggregate(segments),
        "checks": checks,
        "post_external_implementation_corrections": [
            {
                "id": "legacy-voiced-count-semantics",
                "finding": "the first report compared raw lag-3 selections with the preregistered voiced lag-3 counts",
                "correction": "compare lag_3_voiced_hits with [36, 40, 34] and retain raw lag_3_hits as a separate diagnostic",
                "parameter_or_threshold_change": False,
                "audio_or_frame_result_change": False,
            },
            {
                "id": "octave-multiplier-definition-conformance",
                "finding": "self-review found the implementation accepted multiplier 4 although the frozen definition allows only [2, 3]",
                "correction": "read the allowed multiplier set from the frozen definition",
                "parameter_or_threshold_change": False,
                "previous_controlled_report_sha256": "5d88147f2c5a203bc22dd3b9ec14496b28439dbb85be661ee0a319c2fbe8f88c",
                "previous_sensitivity_report_sha256": "8913f7012c80028002cdfad292387080432fc1c4cdf3c8c409837ed64af83b46",
                "observed_effect": {
                    "stellwagen_v2_mean_voiced_fraction_before": 0.18229167,
                    "stellwagen_v2_mean_voiced_fraction_after": 0.1875,
                    "v2_voiced_boundary_hits_before": 0,
                    "v2_voiced_boundary_hits_after": 0,
                },
            },
        ],
        "pass": bool(checks["legacy_saturation_reproduced"])
        and bool(checks["v2_boundary_contract_pass"]),
        "interpretation": {
            "stable": "T029 remains a frozen no-engine-change study.",
            "weaker": "Legacy exact-F0 and harmonic-derived values on noisy boundary-saturated frames are not reliable.",
            "withdrawn": "A lag-3 value near 1333.33 Hz under low-periodicity ship noise is not treated as whale F0.",
            "new": "Evaluator v2 reports those frames as unvoiced with explicit diagnostics.",
        },
        "does_not_establish": [
            "better engine sound",
            "biological f0 truth",
            "permission to retune T029",
            "production engine change",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controlled-only", action="store_true")
    args = parser.parse_args()
    controlled = build_controlled_report()
    controlled_sha = write_report(REFERENCE_REPORT, controlled)
    if not controlled["all_pass"]:
        raise SystemExit("controlled evaluator contract failed")
    result: dict[str, object] = {
        "controlled_report": str(REFERENCE_REPORT.relative_to(ROOT)),
        "controlled_sha256": controlled_sha,
    }
    if not args.controlled_only:
        external = build_external_report()
        external_sha = write_report(SENSITIVITY_REPORT, external)
        if not external["pass"]:
            raise SystemExit("locked external evaluator contract failed")
        result.update(
            {
                "sensitivity_report": str(SENSITIVITY_REPORT.relative_to(ROOT)),
                "sensitivity_sha256": external_sha,
            }
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
