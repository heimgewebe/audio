#!/usr/bin/env python3
"""Build the frozen NOAA-PMEL external humpback-whale evaluation set v2.

The source recordings are never used by the model builder or runtime. Segment
boundaries are fixed before engine evaluation: four non-overlapping 2-second
segments are sampled uniformly from each of two independent field recordings.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import pathlib
import struct
import wave
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "assets" / "whale-sources" / "evaluation-v2"
TARGET_RATE = 48_000
FADE_FRAMES = 960
LANCZOS_RADIUS = 16
ANALYSIS_LOWPASS_HZ = 1_650
LOCKED_AT = "2026-08-02T11:40:00+02:00"
RIGHTS_STATEMENT = (
    "NOAA PMEL states that, unless otherwise noted, information on its "
    "Acoustics Program pages is public information that may be distributed "
    "freely with NOAA PMEL attribution."
)


@dataclass(frozen=True)
class SourceSpec:
    key: str
    filename: str
    expected_sha256: str
    expected_rate: int
    source_url: str
    source_page: str
    population: str
    recording_conditions: str
    id_prefix: str
    starts_ms: tuple[int, ...]


SOURCES = (
    SourceSpec(
        key="stellwagen",
        filename="HB-ship-SBNMS.wav",
        expected_sha256="24bf234e0d302ca91fd3e31f3b964185244403f74666569b753ca12080b59750",
        expected_rate=44_100,
        source_url=(
            "https://www.pmel.noaa.gov/acoustics/multimedia/HB-ship-SBNMS.wav"
        ),
        source_page="https://www.pmel.noaa.gov/acoustics/multimedia.html",
        population="North Atlantic / Stellwagen Bank",
        recording_conditions="humpback vocalizations with ship noise",
        id_prefix="noaa-pmel-stellwagen-ship-independent",
        starts_ms=(250, 2500, 4750, 7000),
    ),
    SourceSpec(
        key="american-samoa",
        filename="HB-ship-AMSNP.wav",
        expected_sha256="2a6c7035808ae31576146d561e4ca08aea77f0851e4212530974cd6abd0bd0a1",
        expected_rate=5_000,
        source_url=(
            "https://www.pmel.noaa.gov/acoustics/multimedia/HB-ship-AMSNP.wav"
        ),
        source_page="https://www.pmel.noaa.gov/acoustics/multimedia.html",
        population="South Pacific / American Samoa",
        recording_conditions="humpback vocalizations with snapping shrimp",
        id_prefix="noaa-pmel-american-samoa-shrimp-independent",
        starts_ms=(200, 2300, 4400, 6500),
    ),
)
SEGMENT_DURATION_MS = 2_000


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def read_pcm16_mono(payload: bytes, *, label: str) -> tuple[int, list[int]]:
    try:
        with wave.open(io.BytesIO(payload), "rb") as handle:
            if handle.getcomptype() != "NONE":
                raise RuntimeError(f"{label} is compressed")
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise RuntimeError(f"{label} must be mono PCM16")
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            raw = handle.readframes(frames)
            if handle.readframes(1):
                raise RuntimeError(f"{label} contains trailing audio frames")
    except (EOFError, wave.Error) as error:
        raise RuntimeError(f"{label} is not a valid WAV file") from error
    if len(raw) != frames * 2:
        raise RuntimeError(f"{label} PCM payload is truncated")
    return sample_rate, list(struct.unpack(f"<{frames}h", raw))


def rounded_division(numerator: int, denominator: int) -> int:
    if numerator >= 0:
        return (numerator + denominator // 2) // denominator
    return -((-numerator + denominator // 2) // denominator)


def normalized_sinc(value: float) -> float:
    if abs(value) < 1.0e-15:
        return 1.0
    angle = math.pi * value
    return math.sin(angle) / angle


def resample_lanczos_pcm16(
    samples: list[int],
    *,
    source_rate: int,
    output_frames: int,
    radius: int = LANCZOS_RADIUS,
) -> list[int]:
    """Band-limited deterministic interpolation with a Lanczos window.

    Both bound sources are upsampled. Sinc reconstruction suppresses spectral
    images that linear interpolation would introduce; the committed output
    hashes make any platform-specific floating-point drift fail closed.
    """

    if len(samples) < 2 or source_rate <= 0 or output_frames <= 0 or radius < 4:
        raise ValueError("invalid resampling input")
    output: list[int] = []
    for output_index in range(output_frames):
        position = output_index * source_rate / TARGET_RATE
        center = math.floor(position)
        weighted = 0.0
        weight_total = 0.0
        for source_index in range(center - radius + 1, center + radius + 1):
            delta = position - source_index
            if abs(delta) >= radius:
                continue
            weight = normalized_sinc(delta) * normalized_sinc(delta / radius)
            bounded_index = min(max(source_index, 0), len(samples) - 1)
            weighted += samples[bounded_index] * weight
            weight_total += weight
        if abs(weight_total) < 1.0e-15:
            raise RuntimeError("Lanczos resampling produced a zero weight sum")
        value = round(weighted / weight_total)
        output.append(max(-32768, min(32767, value)))
    return output


def apply_fades(samples: list[int]) -> list[int]:
    result = list(samples)
    fade = min(FADE_FRAMES, len(result) // 2)
    if fade <= 1:
        return [0 for _value in result]
    denominator = fade - 1
    for index in range(fade):
        result[index] = rounded_division(result[index] * index, denominator)
        mirrored = len(result) - 1 - index
        result[mirrored] = rounded_division(result[mirrored] * index, denominator)
    return result


def wav_bytes(samples: list[int]) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(TARGET_RATE)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return output.getvalue()


def build_payloads(raw_root: pathlib.Path) -> tuple[bytes, dict[str, bytes]]:
    clips: list[dict[str, object]] = []
    processed: dict[str, bytes] = {}
    source_bindings: list[dict[str, object]] = []
    output_frames = SEGMENT_DURATION_MS * TARGET_RATE // 1000

    for source in SOURCES:
        raw_path = raw_root / source.filename
        if raw_path.is_symlink() or not raw_path.is_file():
            raise RuntimeError(f"missing regular raw source: {raw_path}")
        payload = raw_path.read_bytes()
        actual_sha = sha256_bytes(payload)
        if actual_sha != source.expected_sha256:
            raise RuntimeError(f"raw source hash mismatch: {source.filename}")
        sample_rate, samples = read_pcm16_mono(payload, label=source.filename)
        if sample_rate != source.expected_rate:
            raise RuntimeError(f"raw source rate mismatch: {source.filename}")

        intervals: list[tuple[int, int]] = []
        for start_ms in source.starts_ms:
            end_ms = start_ms + SEGMENT_DURATION_MS
            if intervals and start_ms < intervals[-1][1]:
                raise RuntimeError(f"overlapping segment definition: {source.key}")
            start_frame = start_ms * sample_rate // 1000
            end_frame = end_ms * sample_rate // 1000
            if start_frame * 1000 != start_ms * sample_rate:
                raise RuntimeError(f"non-integral segment start: {source.key}")
            if end_frame * 1000 != end_ms * sample_rate:
                raise RuntimeError(f"non-integral segment end: {source.key}")
            if end_frame > len(samples):
                raise RuntimeError(f"segment exceeds raw source: {source.key}")
            intervals.append((start_ms, end_ms))

        source_bindings.append(
            {
                "filename": source.filename,
                "raw_sha256": actual_sha,
                "sample_rate_hz": sample_rate,
                "source_url": source.source_url,
                "segments_ms": [[start, end] for start, end in intervals],
            }
        )

        for number, (start_ms, end_ms) in enumerate(intervals, start=1):
            start_frame = start_ms * sample_rate // 1000
            end_frame = end_ms * sample_rate // 1000
            segment = samples[start_frame:end_frame]
            converted = apply_fades(
                resample_lanczos_pcm16(
                    segment,
                    source_rate=sample_rate,
                    output_frames=output_frames,
                )
            )
            relative_processed = f"processed/{source.key}-{number:02d}.wav"
            processed_payload = wav_bytes(converted)
            processed[relative_processed] = processed_payload
            source_id = f"{source.id_prefix}-{number:02d}"
            clips.append(
                {
                    "call_type": "unclassified fixed-interval field segment",
                    "description": (
                        f"Fixed non-overlapping segment {number} from the NOAA PMEL "
                        f"{source.key} field recording; selected without listening or "
                        "engine-result inspection."
                    ),
                    "duration_seconds": SEGMENT_DURATION_MS / 1000,
                    "license": "NOAA-PMEL-public-information-free-distribution",
                    "population": source.population,
                    "processed_file": relative_processed,
                    "processed_sha256": sha256_bytes(processed_payload),
                    "processing": [
                        f"select fixed raw interval {start_ms / 1000:.3f}-{end_ms / 1000:.3f} seconds",
                        "deterministic 32-tap Lanczos-windowed sinc interpolation to mono PCM16 48 kHz",
                        "deterministic 20 ms boundary fades reaching exact zero",
                        "no normalization, denoising, content filtering, listening, or engine-based selection",
                    ],
                    "raw_file": f"raw/{source.filename}",
                    "raw_sha256": actual_sha,
                    "recording_conditions": source.recording_conditions,
                    "source_recording_id": source.key,
                    "source_sample_rate_hz": sample_rate,
                    "source_nyquist_hz": sample_rate / 2,
                    "analysis_lowpass_hz": ANALYSIS_LOWPASS_HZ,
                    "sample_rate_hz": TARGET_RATE,
                    "source_id": source_id,
                    "source_page": source.source_page,
                    "source_url": source.source_url,
                    "source_interval_ms": [start_ms, end_ms],
                }
            )

    manifest = {
        "clips": clips,
        "independent_field_recording_count": len(SOURCES),
        "kind": "humpback_whale_independent_evaluation_set",
        "license_basis": RIGHTS_STATEMENT,
        "locked_at": LOCKED_AT,
        "methodology_amendment": {
            "reason": "external review identified spectral imaging from linear interpolation",
            "effect": "replaces the pre-merge linear-resampled derivatives and invalidates their scores",
            "unchanged": [
                "raw source bytes",
                "segment boundaries",
                "frozen candidate",
                "engine parameters",
                "distance model",
            ],
        },
        "model_or_parameter_tuning_forbidden": True,
        "schema_version": 1,
        "segment_count": len(clips),
        "segmentation_policy": (
            "four fixed, uniformly spaced, non-overlapping two-second segments per "
            "recording; boundaries fixed before engine evaluation"
        ),
        "source_bindings": source_bindings,
        "source_ids": [str(clip["source_id"]) for clip in clips],
    }
    return canonical_json(manifest), processed


def write_or_check(output_root: pathlib.Path, *, check: bool) -> None:
    manifest_payload, processed = build_payloads(DEFAULT_ROOT / "raw")
    expected = {"manifest.json": manifest_payload, **processed}
    if check:
        for relative, payload in expected.items():
            path = output_root / relative
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise RuntimeError(f"external evaluation artifact mismatch: {relative}")
        return
    for relative, payload in expected.items():
        path = output_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=pathlib.Path, default=DEFAULT_ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    write_or_check(args.output_root, check=args.check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
