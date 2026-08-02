#!/usr/bin/env python3
"""Deterministic, noise-aware F0 and voicing evaluator for whale studies.

This module is a new evaluator generation. It intentionally does not replace
or reinterpret the legacy analyzer used by the frozen T029 study.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import pathlib
import statistics
from typing import Any

from build_whale_morph_bank import read_bound_regular_bytes, read_pcm16_mono_bytes
from build_whale_voice_model import (
    ANALYSIS_RATE,
    CONTROL_POINTS,
    SAMPLE_RATE as LEGACY_SAMPLE_RATE,
    clamp,
    downsample,
    normalized_autocorrelation,
    windowed_frame,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLE_RATE = LEGACY_SAMPLE_RATE
DEFINITION_PATH = (
    ROOT / "assets" / "whale-sources" / "studies" / "evaluator-v2" / "definition.json"
)


@dataclasses.dataclass(frozen=True)
class EvaluatorConfig:
    frame_seconds: float
    minimum_f0_hz: float
    maximum_f0_hz: float
    minimum_rms: float
    minimum_periodicity: float
    minimum_peak_prominence: float
    boundary_guard_lags: int
    octave_candidate_multiples: tuple[int, ...]
    octave_score_ratio: float
    f0_voicing_minimum_nyquist_hz: float
    high_band_ratio_minimum_nyquist_hz: float


@dataclasses.dataclass(frozen=True)
class FrameResult:
    f0_hz: float | None
    periodicity: float
    confidence: float
    selected_lag: int | None
    strongest_lag: int | None
    peak_prominence: float
    boundary: str | None
    voiced: bool
    reason: str
    rms: float
    octave_candidate_lags: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "f0_hz": round(self.f0_hz, 8) if self.f0_hz is not None else None,
            "periodicity": round(self.periodicity, 8),
            "confidence": round(self.confidence, 8),
            "selected_lag": self.selected_lag,
            "strongest_lag": self.strongest_lag,
            "octave_candidate_lags": list(self.octave_candidate_lags),
            "peak_prominence": round(self.peak_prominence, 8),
            "boundary": self.boundary,
            "voiced": self.voiced,
            "reason": self.reason,
            "rms": round(self.rms, 10),
        }


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_definition(path: pathlib.Path = DEFINITION_PATH) -> tuple[dict[str, Any], str]:
    payload = read_bound_regular_bytes(path, "whale evaluator v2 definition")
    value = json.loads(payload.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("kind") != "humpback_whale_f0_evaluator_v2_definition"
        or value.get("task_id") != "AUDIO-CONTROL-PLANE-V1-T030"
        or value.get("frozen_before_external_evaluation") is not True
        or value.get("external_data_used_for_parameter_selection") is not False
    ):
        raise RuntimeError("whale evaluator v2 definition is invalid")
    return value, sha256_bytes(payload)


def config_from_definition(definition: dict[str, Any]) -> EvaluatorConfig:
    analysis = definition.get("analysis")
    bandwidth = definition.get("bandwidth_strata")
    if not isinstance(analysis, dict) or not isinstance(bandwidth, dict):
        raise RuntimeError("whale evaluator v2 definition lacks analysis contracts")
    search = analysis.get("f0_search_hz")
    multiples = analysis.get("octave_candidate_multiples")
    if (
        not isinstance(search, list)
        or len(search) != 2
        or not all(isinstance(value, (int, float)) for value in search)
        or not isinstance(multiples, list)
        or not multiples
        or not all(isinstance(value, int) and value >= 2 for value in multiples)
    ):
        raise RuntimeError("whale evaluator v2 search contract is invalid")
    config = EvaluatorConfig(
        frame_seconds=float(analysis["frame_seconds"]),
        minimum_f0_hz=float(search[0]),
        maximum_f0_hz=float(search[1]),
        minimum_rms=float(analysis["minimum_rms"]),
        minimum_periodicity=float(analysis["minimum_periodicity"]),
        minimum_peak_prominence=float(analysis["minimum_peak_prominence"]),
        boundary_guard_lags=int(analysis["boundary_guard_lags"]),
        octave_candidate_multiples=tuple(multiples),
        octave_score_ratio=float(analysis["octave_score_ratio"]),
        f0_voicing_minimum_nyquist_hz=float(
            bandwidth["f0_voicing_minimum_nyquist_hz"]
        ),
        high_band_ratio_minimum_nyquist_hz=float(
            bandwidth["high_band_ratio_minimum_nyquist_hz"]
        ),
    )
    if not (
        0.0 < config.minimum_f0_hz < config.maximum_f0_hz < ANALYSIS_RATE / 2
        and 0.0 < config.minimum_periodicity < 1.0
        and 0.0 < config.minimum_peak_prominence < 1.0
        and config.boundary_guard_lags >= 0
    ):
        raise RuntimeError("whale evaluator v2 numeric contract is invalid")
    return config


def local_peak_indices(scores: list[float]) -> list[int]:
    if not scores:
        return []
    peaks: list[int] = []
    for index, score in enumerate(scores):
        left = scores[index - 1] if index else -math.inf
        right = scores[index + 1] if index + 1 < len(scores) else -math.inf
        if score >= left and score >= right:
            peaks.append(index)
    return peaks or [max(range(len(scores)), key=scores.__getitem__)]


def harmonically_related(
    left_lag: int, right_lag: int, allowed_multiples: tuple[int, ...]
) -> bool:
    smaller = min(left_lag, right_lag)
    larger = max(left_lag, right_lag)
    ratio = larger / smaller
    nearest = round(ratio)
    return (
        nearest in (1, *allowed_multiples)
        and abs(ratio - nearest) / nearest <= 0.08
    )


def boundary_name(
    lag: int, minimum_lag: int, maximum_lag: int, guard: int
) -> str | None:
    if lag <= minimum_lag + guard:
        return "search-boundary-high"
    if lag >= maximum_lag - guard:
        return "search-boundary-low"
    return None


def evaluate_frame(frame: list[float], config: EvaluatorConfig) -> FrameResult:
    rms = math.sqrt(sum(value * value for value in frame) / max(1, len(frame)))
    if rms < config.minimum_rms:
        return FrameResult(None, 0.0, 0.0, None, None, 0.0, None, False, "silence", rms)

    minimum_lag = max(2, math.ceil(ANALYSIS_RATE / config.maximum_f0_hz))
    maximum_lag = min(
        len(frame) // 2, math.floor(ANALYSIS_RATE / config.minimum_f0_hz)
    )
    if maximum_lag <= minimum_lag:
        raise RuntimeError("whale evaluator v2 search range is empty")
    scores = [
        clamp(normalized_autocorrelation(frame, lag), 0.0, 1.0)
        for lag in range(minimum_lag, maximum_lag + 1)
    ]
    peak_offsets = local_peak_indices(scores)
    strongest_offset = max(peak_offsets, key=scores.__getitem__)
    strongest_lag = minimum_lag + strongest_offset
    strongest_score = scores[strongest_offset]
    family_offsets = [
        offset
        for offset in peak_offsets
        if scores[offset] >= strongest_score * config.octave_score_ratio
        and harmonically_related(
            strongest_lag,
            minimum_lag + offset,
            config.octave_candidate_multiples,
        )
    ]
    selected_offset = min(family_offsets, key=lambda offset: minimum_lag + offset)
    selected_lag = minimum_lag + selected_offset
    selected_score = scores[selected_offset]
    octave_candidate_lags = tuple(
        sorted(
            minimum_lag + offset
            for offset in family_offsets
            if offset != selected_offset
        )
    )

    radius = max(3, selected_lag // 2)
    left_floor = min(scores[max(0, selected_offset - radius) : selected_offset + 1])
    right_floor = min(
        scores[selected_offset : min(len(scores), selected_offset + radius + 1)]
    )
    prominence = max(0.0, selected_score - max(left_floor, right_floor))
    boundary = boundary_name(
        selected_lag, minimum_lag, maximum_lag, config.boundary_guard_lags
    )
    periodicity_confidence = clamp(
        (selected_score - config.minimum_periodicity)
        / max(1.0e-12, 1.0 - config.minimum_periodicity),
        0.0,
        1.0,
    )
    prominence_confidence = clamp(
        prominence / config.minimum_peak_prominence, 0.0, 1.0
    )
    confidence = min(periodicity_confidence, prominence_confidence)

    if boundary is not None:
        return FrameResult(
            None,
            selected_score,
            0.0,
            selected_lag,
            strongest_lag,
            prominence,
            boundary,
            False,
            boundary,
            rms,
            octave_candidate_lags,
        )
    if selected_score < config.minimum_periodicity:
        reason = "low-periodicity"
    elif prominence < config.minimum_peak_prominence:
        reason = "low-prominence"
    else:
        return FrameResult(
            ANALYSIS_RATE / selected_lag,
            selected_score,
            confidence,
            selected_lag,
            strongest_lag,
            prominence,
            None,
            True,
            "voiced",
            rms,
            octave_candidate_lags,
        )
    return FrameResult(
        None,
        selected_score,
        confidence,
        selected_lag,
        strongest_lag,
        prominence,
        None,
        False,
        reason,
        rms,
        octave_candidate_lags,
    )


def analyze_samples(
    samples: list[int | float],
    *,
    source_nyquist_hz: float,
    input_scale: float = 32768.0,
    config: EvaluatorConfig | None = None,
) -> dict[str, object]:
    definition, definition_sha256 = load_definition()
    active_config = config or config_from_definition(definition)
    reduced = downsample(samples, input_scale=input_scale)
    frame_size = round(active_config.frame_seconds * ANALYSIS_RATE)
    results: list[FrameResult] = []
    for index in range(CONTROL_POINTS):
        phase = index / (CONTROL_POINTS - 1)
        center = round(phase * max(0, len(reduced) - 1))
        results.append(evaluate_frame(windowed_frame(reduced, center, frame_size), active_config))

    voiced = [result.f0_hz for result in results if result.voiced and result.f0_hz]
    periodicities = [result.periodicity for result in results if result.voiced]
    reasons: dict[str, int] = {}
    for result in results:
        reasons[result.reason] = reasons.get(result.reason, 0) + 1
    bandwidth = {
        "source_nyquist_hz": source_nyquist_hz,
        "f0_voicing": (
            "available"
            if source_nyquist_hz >= active_config.f0_voicing_minimum_nyquist_hz
            else "unavailable"
        ),
        "high_band_ratio": (
            "available"
            if source_nyquist_hz >= active_config.high_band_ratio_minimum_nyquist_hz
            else "unavailable"
        ),
        "missing_feature_policy": "unavailable-not-imputed-or-reweighted",
    }
    return {
        "schema_version": 1,
        "kind": "humpback_whale_f0_evaluator_v2_result",
        "definition_sha256": definition_sha256,
        "control_points": CONTROL_POINTS,
        "summary": {
            "median_f0_hz": round(statistics.median(voiced), 8) if voiced else None,
            "median_periodicity": (
                round(statistics.median(periodicities), 8) if periodicities else None
            ),
            "voiced_fraction": round(len(voiced) / CONTROL_POINTS, 8),
            "voiced_frames": len(voiced),
            "unvoiced_frames": CONTROL_POINTS - len(voiced),
            "boundary_hits": sum(result.boundary is not None for result in results),
            "voiced_boundary_hits": sum(
                result.voiced and result.boundary is not None for result in results
            ),
            "reason_counts": dict(sorted(reasons.items())),
        },
        "bandwidth": bandwidth,
        "frames": [result.as_dict() for result in results],
        "does_not_establish": [
            "biological ground-truth f0",
            "species identity",
            "perceptual whale similarity",
        ],
    }


def decode_pcm16_mono(payload: bytes, label: str) -> list[int]:
    return read_pcm16_mono_bytes(payload, label)


if __name__ == "__main__":
    definition, digest = load_definition()
    print(
        json.dumps(
            {
                "kind": definition["kind"],
                "definition_sha256": digest,
                "config": dataclasses.asdict(config_from_definition(definition)),
            },
            indent=2,
            sort_keys=True,
        )
    )
