#!/usr/bin/env python3
"""Bounded offline PCM WAV peak and RMS analyzer."""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import struct
import wave

MAX_TOTAL_SAMPLES = 2_000_000


def decode_samples(raw: bytes, width: int) -> list[int]:
    if width == 2:
        return [value[0] for value in struct.iter_unpack("<h", raw)]
    if width == 3:
        result: list[int] = []
        for offset in range(0, len(raw), 3):
            chunk = raw[offset : offset + 3]
            unsigned = int.from_bytes(chunk, "little", signed=False)
            result.append(unsigned - (1 << 24) if unsigned & (1 << 23) else unsigned)
        return result
    if width == 4:
        return [value[0] for value in struct.iter_unpack("<i", raw)]
    raise ValueError("only 16-, 24- and 32-bit integer PCM WAV is supported")


def dbfs(level: float, full_scale: int) -> float | None:
    if level <= 0:
        return None
    return round(20 * math.log10(level / full_scale), 3)


def analyze(path: pathlib.Path) -> dict[str, object]:
    with wave.open(str(path), "rb") as handle:
        if handle.getcomptype() != "NONE":
            raise ValueError("compressed WAV is not supported")
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
        if channels < 1 or channels > 8:
            raise ValueError("channel count must be between 1 and 8")
        if rate <= 0:
            raise ValueError("sample rate must be positive")
        if frames < 1:
            raise ValueError("WAV must contain at least one frame")
        if frames * channels > MAX_TOTAL_SAMPLES:
            raise ValueError(
                f"WAV exceeds the {MAX_TOTAL_SAMPLES}-sample analysis limit"
            )
        raw = handle.readframes(frames)
    samples = decode_samples(raw, width)
    if len(samples) != frames * channels:
        raise ValueError("WAV payload is truncated")
    full_scale = 1 << (width * 8 - 1)
    maximum_positive = full_scale - 1
    minimum_negative = -full_scale
    per_channel: list[dict[str, object]] = []
    for channel in range(channels):
        values = samples[channel::channels]
        peak = max(abs(value) for value in values)
        rms = math.sqrt(sum(value * value for value in values) / len(values))
        clipped = sum(
            1 for value in values if value in {minimum_negative, maximum_positive}
        )
        per_channel.append(
            {
                "channel": channel + 1,
                "peak_dbfs": dbfs(peak, full_scale),
                "rms_dbfs": dbfs(rms, full_scale),
                "clipped_samples": clipped,
            }
        )
    peak_values = [
        item["peak_dbfs"]
        for item in per_channel
        if item["peak_dbfs"] is not None
    ]
    maximum_peak = max(peak_values) if peak_values else None
    return {
        "schema_version": 1,
        "kind": "audio_level_analysis",
        "sample_rate_hz": rate,
        "channels": channels,
        "bit_depth": width * 8,
        "frames": frames,
        "duration_seconds": round(frames / rate, 6),
        "maximum_peak_dbfs": maximum_peak,
        "channels_analysis": per_channel,
        "voice_target": {
            "peak_dbfs_range": [-12.0, -6.0],
            "status": "silence"
            if maximum_peak is None
            else "low"
            if maximum_peak < -12.0
            else "high"
            if maximum_peak > -6.0
            else "in-range",
        },
        "does_not_establish": [
            "analog_gain_position",
            "microphone_identity",
            "perceived-loudness",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", type=pathlib.Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.wav), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
