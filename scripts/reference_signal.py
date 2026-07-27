#!/usr/bin/env python3
"""Generate bounded calibration and latency-reference WAV files without playback."""

from __future__ import annotations

import argparse
import math
import pathlib
import struct
import wave

MAX_DBFS = -12.0
MAX_SAMPLE_RATE_HZ = 192000
MAX_FRAMES = 2_000_000


def amplitude_from_dbfs(dbfs: float) -> float:
    if not math.isfinite(dbfs):
        raise ValueError("dBFS must be finite")
    if dbfs > MAX_DBFS:
        raise ValueError(f"test signal exceeds safe limit of {MAX_DBFS} dBFS")
    return 10 ** (dbfs / 20.0)


def generate_samples(
    kind: str,
    sample_rate: int,
    duration: float,
    dbfs: float,
    frequency: float = 1000.0,
) -> list[int]:
    if sample_rate <= 0 or sample_rate > MAX_SAMPLE_RATE_HZ:
        raise ValueError(f"sample rate must be between 1 and {MAX_SAMPLE_RATE_HZ} Hz")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be finite and positive")
    if kind == "tone" and (
        not math.isfinite(frequency) or frequency <= 0 or frequency >= sample_rate / 2
    ):
        raise ValueError("tone frequency must be finite, positive and below Nyquist")
    frames = round(sample_rate * duration)
    if frames < 1:
        raise ValueError("duration is shorter than one sample")
    if frames > MAX_FRAMES:
        raise ValueError(f"test signal exceeds the {MAX_FRAMES}-frame safety limit")
    peak = amplitude_from_dbfs(dbfs) * 32767
    if kind == "tone":
        return [round(peak * math.sin(2 * math.pi * frequency * index / sample_rate)) for index in range(frames)]
    if kind == "impulse":
        samples = [0] * frames
        samples[0] = round(peak)
        return samples
    raise ValueError(f"unknown signal kind: {kind}")


def write_wav(path: pathlib.Path, samples: list[int], sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--kind", choices=("tone", "impulse"), default="tone")
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--dbfs", type=float, default=-20.0)
    parser.add_argument("--frequency", type=float, default=1000.0)
    args = parser.parse_args()
    samples = generate_samples(args.kind, args.sample_rate, args.duration, args.dbfs, args.frequency)
    write_wav(args.output, samples, args.sample_rate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
