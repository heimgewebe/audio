#!/usr/bin/env python3
"""Analyze an already recorded loopback WAV without changing audio routing."""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import struct
import wave

MAX_PCM_FRAMES = 2_000_000


def read_mono_pcm16(path: pathlib.Path) -> tuple[int, list[int]]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("only mono 16-bit PCM WAV is supported")
        rate = handle.getframerate()
        frames = handle.getnframes()
        if frames > MAX_PCM_FRAMES:
            raise ValueError(f"WAV exceeds the {MAX_PCM_FRAMES}-frame analysis limit")
        raw = handle.readframes(frames)
    return rate, [value[0] for value in struct.iter_unpack("<h", raw)]


def _rms(values: list[int]) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


def estimate_delay(
    reference: list[int], recorded: list[int], max_delay: int
) -> tuple[int, float, float]:
    if not reference or not recorded:
        raise ValueError("signals must not be empty")
    if max_delay < 0:
        raise ValueError("maximum delay must not be negative")
    reference_peak = max(range(len(reference)), key=lambda index: abs(reference[index]))
    reference_level = abs(reference[reference_peak])
    if reference_level == 0:
        raise ValueError("reference signal contains no impulse")
    if reference_level < _rms(reference) * 6:
        raise ValueError("reference signal is not impulse-dominant")

    search = min(max_delay, max(0, len(recorded) - reference_peak - 1))
    start = reference_peak
    end = min(len(recorded), reference_peak + search + 1)
    if start >= end:
        raise ValueError("recorded signal is too short")
    recorded_peak = max(range(start, end), key=lambda index: abs(recorded[index]))
    peak_level = abs(recorded[recorded_peak])
    if peak_level == 0:
        raise ValueError("recorded signal contains no detectable impulse")

    noise_window = recorded[start:end].copy()
    noise_window[recorded_peak - start] = 0
    noise_rms = max(_rms(noise_window), 1.0)
    peak_snr_db = 20 * math.log10(peak_level / noise_rms)
    confidence = max(0.0, min(1.0, peak_snr_db / 40.0))
    if confidence < 0.5:
        raise ValueError("recorded impulse is not sufficiently above the noise floor")
    delay = recorded_peak - reference_peak
    return delay, round(confidence, 6), round(peak_snr_db, 3)


def analyze(reference_path: pathlib.Path, recorded_path: pathlib.Path, max_ms: float) -> dict[str, object]:
    reference_rate, reference = read_mono_pcm16(reference_path)
    recorded_rate, recorded = read_mono_pcm16(recorded_path)
    if reference_rate != recorded_rate:
        raise ValueError("reference and recorded sample rates differ")
    if not math.isfinite(max_ms) or max_ms <= 0:
        raise ValueError("maximum latency must be finite and positive")
    max_delay = round(reference_rate * max_ms / 1000)
    delay, confidence, peak_snr_db = estimate_delay(reference, recorded, max_delay)
    return {
        "schema_version": 1,
        "kind": "audio_loopback_latency_result",
        "sample_rate_hz": reference_rate,
        "delay_samples": delay,
        "round_trip_latency_ms": round(delay / reference_rate * 1000, 3),
        "peak_detection_confidence": confidence,
        "peak_snr_db": peak_snr_db,
        "method": "offline impulse-peak alignment",
        "does_not_establish": [
            "stable_latency_distribution",
            "xrun_free_operation",
            "device_driver_reported_latency_accuracy"
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=pathlib.Path)
    parser.add_argument("recorded", type=pathlib.Path)
    parser.add_argument("--max-ms", type=float, default=500.0)
    args = parser.parse_args()
    print(json.dumps(analyze(args.reference, args.recorded, args.max_ms), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
