#!/usr/bin/env python3
"""Compare a deterministic played morph phrase with real humpback source clips.

The analysis is deliberately dependency-free and bounded. It is not a species
classifier; it supplies reproducible acoustic deltas for iterative synthesis.
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

from build_whale_morph_bank import (  # noqa: E402
    read_bound_regular_bytes,
    read_pcm16_mono_bytes,
    sha256_bytes,
)
from whale_live_engine import MidiEvent, WhaleVoiceConfig, write_stereo_wav  # noqa: E402
from whale_morph_engine import (  # noqa: E402
    WhaleMorphVoice,
    regular_file_path,
)
from whale_organic_engine import OrganicWhaleMorphVoice  # noqa: E402

SAMPLE_RATE = 48_000
ANALYSIS_RATE = 4_000
DOWNSAMPLE_FACTOR = SAMPLE_RATE // ANALYSIS_RATE
REFERENCE_MANIFEST = ROOT / "assets" / "whale-sources" / "processed" / "manifest.json"
REFERENCE_ROOT = REFERENCE_MANIFEST.parent
FEATURES = (
    "pitch_span_semitones",
    "periodicity_median",
    "roughness_median",
    "envelope_cv",
    "high_band_ratio",
    "unit_duration_median_seconds",
)
FEATURE_FLOORS = {
    "pitch_span_semitones": 1.5,
    "periodicity_median": 0.05,
    "roughness_median": 0.05,
    "envelope_cv": 0.08,
    "high_band_ratio": 0.015,
    "unit_duration_median_seconds": 0.15,
}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    amount = position - left
    return ordered[left] + (ordered[right] - ordered[left]) * amount


def organic_phrase_events() -> list[tuple[float, MidiEvent]]:
    """A hand-composed phrase using only causal Roland-style gestures."""

    return [
        # Soft moan, then one continuous rising/falling unit.
        (0.00, MidiEvent("control_change", controller=11, value=105)),
        (0.00, MidiEvent("control_change", controller=1, value=12)),
        (0.00, MidiEvent("note_on", note=45, velocity=44)),
        (1.20, MidiEvent("note_on", note=50, velocity=58)),
        (1.78, MidiEvent("pitch_bend", value=1800)),
        (2.20, MidiEvent("note_on", note=47, velocity=52)),
        (2.78, MidiEvent("pitch_bend", value=-900)),
        (3.18, MidiEvent("pitch_bend", value=0)),
        (3.35, MidiEvent("note_off", note=50)),
        (3.35, MidiEvent("note_off", note=45)),
        (3.35, MidiEvent("note_off", note=47)),
        # Repeated pulsed unit, a common structural gesture in song phrases.
        (4.15, MidiEvent("note_on", note=42, velocity=78)),
        (4.62, MidiEvent("note_on", note=42, velocity=96)),
        (5.05, MidiEvent("note_on", note=45, velocity=82)),
        (5.58, MidiEvent("note_on", note=49, velocity=88)),
        (6.25, MidiEvent("note_off", note=42)),
        (6.25, MidiEvent("note_off", note=45)),
        (6.25, MidiEvent("note_off", note=49)),
        # Longer cry with developing modulation and a sustained legato turn.
        (7.05, MidiEvent("control_change", controller=64, value=127)),
        (7.05, MidiEvent("control_change", controller=1, value=28)),
        (7.05, MidiEvent("note_on", note=53, velocity=68)),
        (8.15, MidiEvent("control_change", controller=1, value=68)),
        (8.65, MidiEvent("note_on", note=60, velocity=74)),
        (9.35, MidiEvent("pitch_bend", value=2600)),
        (10.10, MidiEvent("note_on", note=56, velocity=60)),
        (10.70, MidiEvent("pitch_bend", value=-1700)),
        (11.25, MidiEvent("control_change", controller=1, value=18)),
        (11.55, MidiEvent("pitch_bend", value=0)),
        (11.90, MidiEvent("note_off", note=53)),
        (11.90, MidiEvent("note_off", note=60)),
        (11.90, MidiEvent("note_off", note=56)),
        (12.25, MidiEvent("control_change", controller=64, value=0)),
        # Quiet terminal groan.
        (13.35, MidiEvent("control_change", controller=67, value=46)),
        (13.35, MidiEvent("note_on", note=38, velocity=36)),
        (15.20, MidiEvent("note_off", note=38)),
        (15.20, MidiEvent("control_change", controller=67, value=0)),
    ]


def render_phrase(
    engine: str = "morph", duration_seconds: float = 17.0, gain: float = 0.16
) -> list[float]:
    config = WhaleVoiceConfig(
        sample_rate=SAMPLE_RATE, block_frames=128, master_gain=gain
    )
    if engine == "morph":
        voice = WhaleMorphVoice(config)
    elif engine == "organic":
        voice = OrganicWhaleMorphVoice(config)
    else:
        raise ValueError(f"unknown comparison engine: {engine}")
    output: list[float] = []
    cursor = 0
    total = round(duration_seconds * SAMPLE_RATE)
    for timestamp, event in organic_phrase_events():
        target = min(round(timestamp * SAMPLE_RATE), total)
        if target > cursor:
            output.extend(voice.render(target - cursor))
            cursor = target
        voice.dispatch(event)
    if cursor < total:
        output.extend(voice.render(total - cursor))
    return output


def decode_pcm16_mono(
    payload: bytes,
    label: str,
    maximum_seconds: float = 7.0,
) -> list[float]:
    samples = read_pcm16_mono_bytes(payload, label)
    frames = min(len(samples), round(maximum_seconds * SAMPLE_RATE))
    return [sample / 32768.0 for sample in samples[:frames]]


def read_pcm16_mono(path: pathlib.Path, maximum_seconds: float = 7.0) -> list[float]:
    safe_path = regular_file_path(path, "whale comparison clip")
    payload = read_bound_regular_bytes(safe_path, "whale comparison clip")
    return decode_pcm16_mono(payload, str(safe_path), maximum_seconds)


def downsample(samples: list[float]) -> list[float]:
    usable = len(samples) - len(samples) % DOWNSAMPLE_FACTOR
    return [
        sum(samples[index : index + DOWNSAMPLE_FACTOR]) / DOWNSAMPLE_FACTOR
        for index in range(0, usable, DOWNSAMPLE_FACTOR)
    ]


def normalized_autocorrelation(values: list[float], lag: int) -> float:
    count = len(values) - lag
    if count <= 16:
        return 0.0
    left_energy = 0.0
    right_energy = 0.0
    product = 0.0
    for index in range(count):
        left = values[index]
        right = values[index + lag]
        product += left * right
        left_energy += left * left
        right_energy += right * right
    denominator = math.sqrt(left_energy * right_energy)
    return product / denominator if denominator > 1e-20 else 0.0


def pitch_track(samples: list[float]) -> tuple[list[float], list[float], list[float]]:
    reduced = downsample(samples)
    window = round(0.18 * ANALYSIS_RATE)
    hop = round(0.05 * ANALYSIS_RATE)
    minimum_lag = max(2, round(ANALYSIS_RATE / 1_200.0))
    maximum_lag = round(ANALYSIS_RATE / 35.0)
    pitches: list[float] = []
    periodicities: list[float] = []
    rms_values: list[float] = []
    for start in range(0, max(0, len(reduced) - window + 1), hop):
        frame = reduced[start : start + window]
        mean = sum(frame) / len(frame)
        centered = [
            (value - mean) * (0.5 - 0.5 * math.cos(2.0 * math.pi * index / (window - 1)))
            for index, value in enumerate(frame)
        ]
        rms = math.sqrt(sum(value * value for value in centered) / len(centered))
        rms_values.append(rms)
        if rms < 1e-5:
            pitches.append(0.0)
            periodicities.append(0.0)
            continue
        scores = [
            normalized_autocorrelation(centered, lag)
            for lag in range(minimum_lag, maximum_lag + 1)
        ]
        best_offset = max(range(len(scores)), key=scores.__getitem__)
        best_lag = minimum_lag + best_offset
        best_score = max(0.0, min(1.0, scores[best_offset]))
        # Prefer a plausible lower fundamental when an octave-related lag is
        # nearly as periodic as the strongest harmonic peak.
        doubled = best_lag * 2
        if doubled <= maximum_lag:
            doubled_score = normalized_autocorrelation(centered, doubled)
            if doubled_score >= best_score * 0.92:
                best_lag = doubled
                best_score = max(best_score, doubled_score)
        pitches.append(ANALYSIS_RATE / best_lag if best_score >= 0.22 else 0.0)
        periodicities.append(best_score)
    return pitches, periodicities, rms_values


def unit_durations(rms_values: list[float], hop_seconds: float = 0.05) -> list[float]:
    if not rms_values:
        return []
    peak = max(rms_values)
    threshold = max(1e-6, peak * 0.12)
    durations: list[float] = []
    run = 0
    for value in [*rms_values, 0.0]:
        if value >= threshold:
            run += 1
        elif run:
            duration = run * hop_seconds
            if duration >= 0.15:
                durations.append(duration)
            run = 0
    return durations


def extract_features(samples: list[float]) -> dict[str, float]:
    if not samples:
        raise ValueError("cannot analyze an empty signal")
    peak = max(abs(value) for value in samples)
    if peak <= 0.0:
        raise ValueError("cannot analyze silence")
    normalized = [value / peak for value in samples]
    pitches, periodicities, rms_values = pitch_track(normalized)
    voiced = [pitch for pitch, score in zip(pitches, periodicities) if pitch > 0.0 and score >= 0.22]
    voiced_scores = [score for pitch, score in zip(pitches, periodicities) if pitch > 0.0 and score >= 0.22]
    semitones = [69.0 + 12.0 * math.log2(pitch / 440.0) for pitch in voiced]
    motion: list[float] = []
    for left, right in zip(semitones, semitones[1:]):
        delta = abs(right - left) / 0.05
        if delta <= 240.0:
            motion.append(delta)
    mean_rms = statistics.fmean(rms_values) if rms_values else 0.0
    envelope_cv = (
        statistics.pstdev(rms_values) / mean_rms
        if len(rms_values) > 1 and mean_rms > 1e-12
        else 0.0
    )
    derivative_energy = sum(
        (right - left) ** 2 for left, right in zip(normalized, normalized[1:])
    )
    signal_energy = sum(value * value for value in normalized) or 1.0
    durations = unit_durations(rms_values)
    periodicity = statistics.median(voiced_scores) if voiced_scores else 0.0
    return {
        "duration_seconds": len(samples) / SAMPLE_RATE,
        "pitch_median_hz": statistics.median(voiced) if voiced else 0.0,
        "pitch_span_semitones": (
            percentile(semitones, 0.90) - percentile(semitones, 0.10)
            if semitones
            else 0.0
        ),
        "pitch_motion_semitones_per_second": statistics.median(motion) if motion else 0.0,
        "periodicity_median": periodicity,
        "roughness_median": 1.0 - periodicity,
        "envelope_cv": envelope_cv,
        "high_band_ratio": derivative_energy / signal_energy,
        "unit_duration_median_seconds": statistics.median(durations) if durations else 0.0,
        "peak": peak,
        "voiced_frame_fraction": len(voiced) / max(1, len(pitches)),
    }


def reference_records(
    limit: int,
    manifest_path: pathlib.Path = REFERENCE_MANIFEST,
    source_root: pathlib.Path | None = None,
) -> tuple[str, list[dict[str, object]]]:
    safe_manifest = regular_file_path(manifest_path, "whale comparison manifest")
    manifest_payload = read_bound_regular_bytes(
        safe_manifest, "whale comparison manifest"
    )
    manifest_sha256 = sha256_bytes(manifest_payload)
    value = json.loads(manifest_payload.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 2
        or value.get("kind") != "humpback_whale_sample_bank"
        or value.get("sample_rate_hz") != SAMPLE_RATE
        or not isinstance(value.get("clips"), list)
    ):
        raise RuntimeError("whale comparison manifest has the wrong schema")
    parent = source_root or safe_manifest.parent
    candidates: list[dict[str, object]] = []
    for record in value["clips"]:
        if not isinstance(record, dict) or record.get("category") != "song":
            continue
        clip_id = record.get("id")
        filename = record.get("file")
        source_id = record.get("source_id")
        expected_sha256 = record.get("sha256")
        if not all(
            isinstance(field, str) and field
            for field in (clip_id, filename, source_id, expected_sha256)
        ):
            raise RuntimeError("whale comparison manifest contains incomplete song metadata")
        relative = pathlib.PurePosixPath(filename)
        if relative.is_absolute() or len(relative.parts) != 1:
            raise RuntimeError("whale comparison clip path must be one basename")
        candidates.append(record)

    selected: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    for record in candidates:
        source_id = str(record["source_id"])
        if source_id in seen_sources:
            continue
        path = regular_file_path(
            parent / str(record["file"]), "whale comparison clip"
        )
        payload = read_bound_regular_bytes(path, "whale comparison clip")
        actual_sha256 = sha256_bytes(payload)
        if actual_sha256 != record["sha256"]:
            raise RuntimeError(f"whale comparison clip hash mismatch: {record['id']}")
        selected.append(
            {
                "clip_id": record["id"],
                "source_id": source_id,
                "path": path,
                "sha256": actual_sha256,
                "payload": payload,
            }
        )
        seen_sources.add(source_id)
        if len(selected) >= limit:
            break
    if len(selected) != limit:
        raise RuntimeError("whale comparison lacks enough independent song sources")
    return manifest_sha256, selected


def aggregate_reference(features: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    aggregate: dict[str, dict[str, float]] = {}
    for key in FEATURES:
        values = [record[key] for record in features]
        aggregate[key] = {
            "median": statistics.median(values),
            "q25": percentile(values, 0.25),
            "q75": percentile(values, 0.75),
        }
    return aggregate


def compare(
    synthetic: dict[str, float], aggregate: dict[str, dict[str, float]]
) -> tuple[float, dict[str, dict[str, float]]]:
    deltas: dict[str, dict[str, float]] = {}
    distances: list[float] = []
    for key in FEATURES:
        target = aggregate[key]["median"]
        scale = max(
            aggregate[key]["q75"] - aggregate[key]["q25"], FEATURE_FLOORS[key]
        )
        distance = abs(synthetic[key] - target) / scale
        distances.append(min(distance, 8.0))
        deltas[key] = {
            "synthetic": synthetic[key],
            "reference_median": target,
            "reference_iqr": aggregate[key]["q75"] - aggregate[key]["q25"],
            "normalized_distance": distance,
        }
    mean_distance = statistics.fmean(distances)
    return math.exp(-mean_distance), deltas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--reference-count", type=int, default=6)
    parser.add_argument("--engine", choices=("morph", "organic"), default="morph")
    args = parser.parse_args()
    if not 3 <= args.reference_count <= 10:
        raise SystemExit("reference count must be between 3 and 10")

    synthetic_samples = render_phrase(args.engine)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    write_stereo_wav(args.output, synthetic_samples, SAMPLE_RATE)
    synthetic_features = extract_features(synthetic_samples)

    references: list[dict[str, object]] = []
    reference_features: list[dict[str, float]] = []
    reference_manifest_sha256, selected_references = reference_records(
        args.reference_count
    )
    for record in selected_references:
        path = record["path"]
        if not isinstance(path, pathlib.Path):
            raise AssertionError("validated comparison path lost its type")
        payload = record["payload"]
        if not isinstance(payload, bytes):
            raise AssertionError("validated comparison payload lost its type")
        features = extract_features(decode_pcm16_mono(payload, str(path)))
        reference_features.append(features)
        references.append(
            {
                "clip_id": record["clip_id"],
                "source_id": record["source_id"],
                "path": str(path.relative_to(ROOT)),
                "sha256": record["sha256"],
                "features": features,
            }
        )
    aggregate = aggregate_reference(reference_features)
    similarity, deltas = compare(synthetic_features, aggregate)
    report = {
        "schema_version": 1,
        "kind": "humpback_whale_organic_similarity",
        "method": "bounded_dependency_free_acoustic_feature_comparison",
        "does_not_establish": [
            "biological_identity",
            "perceptual_equivalence",
            "species_classifier_accuracy",
        ],
        "synthetic_output": str(args.output),
        "synthetic_engine": args.engine,
        "scored_features": list(FEATURES),
        "reference_manifest": {
            "path": str(REFERENCE_MANIFEST.relative_to(ROOT)),
            "sha256": reference_manifest_sha256,
        },
        "synthetic": synthetic_features,
        "references": references,
        "reference_aggregate": aggregate,
        "comparison": {
            "similarity_score_0_to_1": similarity,
            "feature_deltas": deltas,
        },
    }
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
