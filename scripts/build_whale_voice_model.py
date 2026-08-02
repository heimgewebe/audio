#!/usr/bin/env python3
"""Build a source-bound temporal source-filter model from humpback recordings.

The builder is dependency-free, deterministic, and bounded. It extracts
relative control trajectories rather than playable phrases. Every analyzed WAV
is read from one immutable byte snapshot and checked against the processed
sample manifest before analysis.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import statistics
import tempfile
from collections.abc import Iterable

from build_whale_morph_bank import (
    read_bound_regular_bytes,
    read_pcm16_mono_bytes,
    regular_file_path,
    sha256_bytes,
    source_clip_path,
    validated_output_path,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLE_RATE = 48_000
ANALYSIS_RATE = 4_000
DOWNSAMPLE = SAMPLE_RATE // ANALYSIS_RATE
CONTROL_POINTS = 48
HARMONIC_COUNT = 8
LOWPASS_CUTOFF_HZ = 1_650.0
LOWPASS_ORDER = 8
BUTTERWORTH_Q = (0.50979558, 0.60134489, 0.89997622, 2.56291545)
SOURCE_MANIFEST = ROOT / "assets" / "whale-sources" / "processed" / "manifest.json"
DEFAULT_OUTPUT = ROOT / "assets" / "whale-sources" / "voice-model" / "manifest.json"
EXPECTED_SOURCE_IDS = (
    "humpback-moo-nps",
    "humpback-song-cc0",
    "humpback-wheezeblow-nps",
    "song-antarctic-area-v-2010",
    "song-eastern-australia-2010",
    "song-foraging-mn132a",
    "song-foraging-mn133a",
    "song-new-caledonia-2010",
)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = clamp(fraction, 0.0, 1.0) * (len(ordered) - 1)
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    amount = position - left
    return ordered[left] + (ordered[right] - ordered[left]) * amount


def _lowpass_coefficients(cutoff_hz: float, sample_rate: int, q: float) -> tuple[float, ...]:
    omega = 2.0 * math.pi * cutoff_hz / sample_rate
    cosine = math.cos(omega)
    alpha = math.sin(omega) / (2.0 * q)
    a0 = 1.0 + alpha
    return (
        ((1.0 - cosine) * 0.5) / a0,
        (1.0 - cosine) / a0,
        ((1.0 - cosine) * 0.5) / a0,
        (-2.0 * cosine) / a0,
        (1.0 - alpha) / a0,
    )


def downsample(
    samples: Iterable[int | float], *, input_scale: float = 32768.0
) -> list[float]:
    """Low-pass and decimate 48 kHz input to 4 kHz without spectral folding."""

    if not math.isfinite(input_scale) or input_scale <= 0.0:
        raise ValueError("input_scale must be finite and positive")
    coefficients = tuple(
        _lowpass_coefficients(LOWPASS_CUTOFF_HZ, SAMPLE_RATE, q)
        for q in BUTTERWORTH_Q
    )
    states = [[0.0, 0.0] for _ in coefficients]
    reduced: list[float] = []
    for index, raw in enumerate(samples):
        value = float(raw) / input_scale
        for section, (b0, b1, b2, a1, a2) in enumerate(coefficients):
            z1, z2 = states[section]
            output = b0 * value + z1
            states[section][0] = b1 * value - a1 * output + z2
            states[section][1] = b2 * value - a2 * output
            value = output
        if index % DOWNSAMPLE == DOWNSAMPLE - 1:
            reduced.append(value)
    return reduced


def normalized_autocorrelation(values: list[float], lag: int) -> float:
    count = len(values) - lag
    if count <= 24:
        return 0.0
    product = 0.0
    left_energy = 0.0
    right_energy = 0.0
    for index in range(count):
        left = values[index]
        right = values[index + lag]
        product += left * right
        left_energy += left * left
        right_energy += right * right
    denominator = math.sqrt(left_energy * right_energy)
    return product / denominator if denominator > 1.0e-20 else 0.0


def windowed_frame(samples: list[float], center: int, frames: int) -> list[float]:
    half = frames // 2
    start = max(0, min(len(samples) - frames, center - half))
    frame = samples[start : start + frames]
    if len(frame) < frames:
        frame = [*frame, *([0.0] * (frames - len(frame)))]
    mean = statistics.fmean(frame)
    denominator = max(1, frames - 1)
    return [
        (value - mean)
        * (0.5 - 0.5 * math.cos(2.0 * math.pi * index / denominator))
        for index, value in enumerate(frame)
    ]


def periodicity_features(
    frame: list[float],
) -> tuple[float, float, float, float, float]:
    minimum_lag = max(2, round(ANALYSIS_RATE / 1_200.0))
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
    f0 = ANALYSIS_RATE / best_lag if best_score >= 0.20 else 0.0

    secondary_ratio = 1.0
    secondary_strength = 0.0
    for offset in sorted(range(len(scores)), key=scores.__getitem__, reverse=True):
        lag = minimum_lag + offset
        if lag == best_lag:
            continue
        score = clamp(scores[offset], 0.0, 1.0)
        ratio = best_lag / lag
        if not 0.55 <= ratio <= 2.40:
            continue
        if min(abs(ratio - target) for target in (0.5, 1.0, 2.0)) < 0.10:
            continue
        threshold = max(0.34, best_score * 0.72)
        if score < threshold:
            continue
        secondary_ratio = ratio
        secondary_strength = clamp((score - threshold) / 0.24, 0.0, 1.0)
        break

    double_score = (
        clamp(normalized_autocorrelation(frame, best_lag * 2), 0.0, 1.0)
        if best_lag * 2 <= maximum_lag
        else 0.0
    )
    subharmonic = clamp((double_score - best_score * 0.76) / 0.24, 0.0, 1.0)
    return f0, best_score, subharmonic, secondary_ratio, secondary_strength


def harmonic_profile(frame: list[float], f0_hz: float) -> list[float]:
    if f0_hz <= 0.0:
        return [0.0] * HARMONIC_COUNT
    values: list[float] = []
    for harmonic in range(1, HARMONIC_COUNT + 1):
        frequency = f0_hz * harmonic
        if frequency >= ANALYSIS_RATE * 0.46:
            values.append(0.0)
            continue
        angular = 2.0 * math.pi * frequency / ANALYSIS_RATE
        real = 0.0
        imaginary = 0.0
        for index, value in enumerate(frame):
            phase = angular * index
            real += value * math.cos(phase)
            imaginary -= value * math.sin(phase)
        values.append(math.hypot(real, imaginary) / len(frame))
    total = sum(values)
    if total <= 1.0e-12:
        return [0.0] * HARMONIC_COUNT
    return [value / total for value in values]


def resonance_emphasis_ratios(harmonics: list[float]) -> tuple[float, float]:
    """Return two broad harmonic-emphasis ratios, not biological formants."""

    ranked = sorted(
        range(1, len(harmonics)),
        key=lambda harmonic: harmonics[harmonic],
        reverse=True,
    )
    selected: list[float] = []
    for harmonic in ranked:
        ratio = float(harmonic + 1)
        if selected and abs(ratio - selected[0]) < 1.0:
            continue
        selected.append(ratio)
        if len(selected) == 2:
            break
    defaults = (2.0, 4.0)
    while len(selected) < 2:
        selected.append(defaults[len(selected)])
    selected.sort()
    return selected[0], selected[1]


def pulse_features(samples: list[float], center: int) -> tuple[float, float]:
    step = max(1, ANALYSIS_RATE // 100)
    radius = round(0.75 * ANALYSIS_RATE)
    start = max(0, center - radius)
    end = min(len(samples), center + radius)
    envelope = [
        statistics.fmean(abs(value) for value in samples[index : index + step])
        for index in range(start, end, step)
        if samples[index : index + step]
    ]
    if len(envelope) < 40:
        return 2.4, 0.0
    mean = statistics.fmean(envelope)
    centered = [value - mean for value in envelope]
    minimum_lag = 12
    maximum_lag = min(len(centered) // 2, 83)
    scored = [
        (normalized_autocorrelation(centered, lag), lag)
        for lag in range(minimum_lag, maximum_lag + 1)
    ]
    score, lag = max(scored)
    strength = clamp((score - 0.16) / 0.54, 0.0, 1.0)
    return 100.0 / lag, strength


def analyze_clip(samples: list[float]) -> tuple[list[dict[str, object]], dict[str, float]]:
    if len(samples) < round(0.25 * ANALYSIS_RATE):
        raise RuntimeError("whale voice source clip is too short for analysis")
    peak = max(abs(value) for value in samples) or 1.0
    frame_size = round(0.18 * ANALYSIS_RATE)
    points: list[dict[str, object]] = []
    f0_values: list[float] = []
    periodicities: list[float] = []
    for index in range(CONTROL_POINTS):
        phase = index / (CONTROL_POINTS - 1)
        center = round(phase * (len(samples) - 1))
        frame = windowed_frame(samples, center, frame_size)
        rms = math.sqrt(sum(value * value for value in frame) / len(frame))
        derivative = sum(
            (right - left) ** 2 for left, right in zip(frame, frame[1:])
        )
        energy = sum(value * value for value in frame) or 1.0
        (
            f0,
            periodicity,
            subharmonic,
            secondary_ratio,
            secondary_strength,
        ) = periodicity_features(frame)
        harmonics = harmonic_profile(frame, f0)
        weighted_harmonic = sum(
            (harmonic + 1) * value for harmonic, value in enumerate(harmonics)
        )
        resonance_ratio_1, resonance_ratio_2 = resonance_emphasis_ratios(harmonics)
        pulse_rate, pulse_strength = pulse_features(samples, center)
        local = samples[
            max(0, center - round(0.08 * ANALYSIS_RATE)) : min(
                len(samples), center + round(0.08 * ANALYSIS_RATE)
            )
        ]
        local_mean = statistics.fmean(abs(value) for value in local) if local else 0.0
        local_deviation = (
            statistics.pstdev(abs(value) for value in local) / local_mean
            if len(local) > 1 and local_mean > 1.0e-9
            else 0.0
        )
        points.append(
            {
                "phase": round(phase, 8),
                "envelope": round(clamp(rms / peak * 2.8, 0.0, 1.0), 8),
                "periodicity": round(periodicity, 8),
                "roughness": round(1.0 - periodicity, 8),
                "high_band_ratio": round(
                    clamp(derivative / energy / 2.5, 0.0, 1.0), 8
                ),
                "spectral_tilt": round(
                    clamp((weighted_harmonic - 1.0) / 7.0, 0.0, 1.0), 8
                ),
                "resonance_ratio_1": round(
                    clamp(resonance_ratio_1, 1.2, 8.0), 8
                ),
                "resonance_ratio_2": round(
                    clamp(resonance_ratio_2, 1.8, 12.0), 8
                ),
                "harmonic_profile": [round(value, 8) for value in harmonics],
                "pulse_rate_hz": round(clamp(pulse_rate, 1.2, 8.0), 8),
                "pulse_strength": round(
                    clamp(max(pulse_strength, local_deviation * 0.18), 0.0, 1.0),
                    8,
                ),
                "subharmonic_strength": round(subharmonic, 8),
                "secondary_ratio": round(clamp(secondary_ratio, 0.55, 2.40), 8),
                "secondary_strength": round(secondary_strength, 8),
            }
        )
        if f0 > 0.0:
            f0_values.append(f0)
            periodicities.append(periodicity)
    duration = len(samples) / ANALYSIS_RATE
    summary = {
        "duration_seconds": round(duration, 8),
        "median_f0_hz": round(
            statistics.median(f0_values) if f0_values else 0.0, 8
        ),
        "median_periodicity": round(
            statistics.median(periodicities) if periodicities else 0.0, 8
        ),
        "voiced_fraction": round(len(f0_values) / CONTROL_POINTS, 8),
        "envelope_q10": round(
            percentile([float(point["envelope"]) for point in points], 0.10), 8
        ),
        "envelope_q90": round(
            percentile([float(point["envelope"]) for point in points], 0.90), 8
        ),
    }
    return points, summary


def load_source_manifest() -> tuple[bytes, dict[str, object]]:
    path = regular_file_path(SOURCE_MANIFEST, "whale voice source manifest")
    payload = read_bound_regular_bytes(path, "whale voice source manifest")
    value = json.loads(payload.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 2
        or value.get("kind") != "humpback_whale_sample_bank"
        or value.get("sample_rate_hz") != SAMPLE_RATE
        or not isinstance(value.get("clips"), list)
    ):
        raise RuntimeError("whale voice source manifest has the wrong schema")
    return payload, value


def build_manifest() -> dict[str, object]:
    source_payload, source_manifest = load_source_manifest()
    source_root = SOURCE_MANIFEST.parent
    trajectories: list[dict[str, object]] = []
    source_ids: set[str] = set()
    for raw_record in source_manifest["clips"]:
        if not isinstance(raw_record, dict):
            raise RuntimeError("whale voice source clip must be an object")
        clip_id = raw_record.get("id")
        source_id = raw_record.get("source_id")
        filename = raw_record.get("file")
        expected_sha = raw_record.get("sha256")
        category = raw_record.get("category")
        if not all(
            isinstance(value, str) and value
            for value in (clip_id, source_id, filename, expected_sha, category)
        ):
            raise RuntimeError("whale voice source clip metadata is incomplete")
        path = source_clip_path(source_root, filename)
        clip_payload = read_bound_regular_bytes(path, "whale voice source clip")
        actual_sha = sha256_bytes(clip_payload)
        if actual_sha != expected_sha:
            raise RuntimeError(f"whale voice source clip hash mismatch: {clip_id}")
        pcm = read_pcm16_mono_bytes(clip_payload, str(path))
        reduced = downsample(pcm)
        points, summary = analyze_clip(reduced)
        source_ids.add(source_id)
        trajectories.append(
            {
                "id": f"trajectory-{clip_id}",
                "clip_id": clip_id,
                "source_id": source_id,
                "source_file": filename,
                "source_sha256": actual_sha,
                "category": category,
                "summary": summary,
                "points": points,
            }
        )
    trajectories.sort(key=lambda record: str(record["id"]))
    expected_sources = set(EXPECTED_SOURCE_IDS)
    if source_ids != expected_sources:
        missing = sorted(expected_sources - source_ids)
        unexpected = sorted(source_ids - expected_sources)
        raise RuntimeError(
            "whale voice source family catalog changed: "
            f"missing={missing}, unexpected={unexpected}"
        )
    return {
        "schema_version": 2,
        "kind": "humpback_whale_temporal_source_filter_bank",
        "sample_rate_hz": SAMPLE_RATE,
        "analysis_rate_hz": ANALYSIS_RATE,
        "analysis_filter": {
            "kind": "butterworth-lowpass-before-decimation",
            "order": LOWPASS_ORDER,
            "cutoff_hz": LOWPASS_CUTOFF_HZ,
            "decimation_factor": DOWNSAMPLE,
        },
        "control_points": CONTROL_POINTS,
        "harmonic_count": HARMONIC_COUNT,
        "note_range": [21, 108],
        "tuning": "twelve-tone-equal-temperament-a4-440",
        "voice_count": 1,
        "source_sample_manifest": str(SOURCE_MANIFEST.relative_to(ROOT)),
        "source_sample_manifest_sha256": sha256_bytes(source_payload),
        "source_ids": list(EXPECTED_SOURCE_IDS),
        "evaluation_contract": {
            "strategy": "leave-one-source-family-out-cross-validation",
            "family_weighting": "equal",
            "temporal_alignment": "normalized-48-point-sequence",
            "independent_test_claim": False,
        },
        "model_contract": {
            "plays_recorded_phrase": False,
            "permanent_noise_layer": False,
            "main_fundamental_bound_to_played_note": True,
            "trajectory_selection": "gesture-seeded-family-then-clip-balanced",
            "long_hold": "per-trajectory-duration-with-continuous-unit-crossfade",
            "features": [
                "envelope",
                "periodicity",
                "roughness",
                "high_band_ratio",
                "spectral_tilt",
                "harmonic_resonance_emphasis_ratios",
                "harmonic_profile",
                "pulse_rate_and_strength",
                "subharmonic_strength",
                "secondary_frequency_ratio_and_strength",
            ],
        },
        "trajectories": trajectories,
    }


def encode_manifest(value: dict[str, object]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_atomic(path: pathlib.Path, payload: bytes) -> None:
    output = validated_output_path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = encode_manifest(build_manifest())
    if args.check:
        current = read_bound_regular_bytes(args.output, "whale voice model")
        if current != payload:
            raise SystemExit("whale voice model is not reproducible from bound sources")
        print(json.dumps({"status": "ok", "sha256": sha256_bytes(payload)}))
        return 0
    write_atomic(args.output, payload)
    print(
        json.dumps(
            {
                "status": "written",
                "output": str(args.output),
                "sha256": sha256_bytes(payload),
                "bytes": len(payload),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
