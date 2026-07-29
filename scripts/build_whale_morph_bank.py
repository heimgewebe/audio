#!/usr/bin/env python3
"""Build a deterministic, source-derived wavetable bank for the whale voice.

The builder deliberately extracts only phase-aligned periodic cycles from the
existing licensed humpback clips. Long phrases and stationary sea noise are not
copied into the runtime model. The result is a small JSON model whose tables can
be played at any chromatic MIDI pitch without sample zones.
"""

from __future__ import annotations

import argparse
import array
import base64
import hashlib
import json
import math
import os
import pathlib
import stat
import struct
import tempfile
import wave
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_MANIFEST = ROOT / "assets" / "whale-sources" / "processed" / "manifest.json"
DEFAULT_OUTPUT = ROOT / "assets" / "whale-sources" / "morph" / "manifest.json"
SAMPLE_RATE = 48_000
TABLE_SIZE = 1_024
DOWNSAMPLE_FACTOR = 12
ANALYSIS_RATE = SAMPLE_RATE // DOWNSAMPLE_FACTOR
ANALYSIS_WINDOW = ANALYSIS_RATE // 2
ANALYSIS_HOP = ANALYSIS_RATE // 4
MIN_FREQUENCY_HZ = 25.0
MAX_FREQUENCY_HZ = 1_000.0
MIN_LAG = max(2, math.floor(ANALYSIS_RATE / MAX_FREQUENCY_HZ))
MAX_LAG = math.ceil(ANALYSIS_RATE / MIN_FREQUENCY_HZ)
HARMONIC_LEVELS = (64, 32, 16, 8, 4, 2, 1)
MIN_PERIODICITY = 0.58


@dataclass(frozen=True)
class AnchorSpec:
    note: int
    clip_id: str


ANCHORS = (
    AnchorSpec(21, "humpback-moo-nps-02"),
    AnchorSpec(36, "humpback-moo-nps-03"),
    AnchorSpec(48, "humpback-song-cc0-02"),
    AnchorSpec(60, "song-new-caledonia-2010-01"),
    AnchorSpec(72, "song-new-caledonia-2010-02"),
    AnchorSpec(86, "song-foraging-mn133a-01"),
    AnchorSpec(108, "humpback-song-cc0-03"),
)


def absolute_path(path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.fspath(path)))


def reject_symlink_components(path: pathlib.Path, label: str) -> pathlib.Path:
    absolute = absolute_path(path)
    for candidate in [*reversed(absolute.parents), absolute]:
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"{label} must not contain symlink components")
    return absolute


def regular_file_path(path: pathlib.Path, label: str) -> pathlib.Path:
    absolute = reject_symlink_components(path, label)
    try:
        mode = absolute.lstat().st_mode
    except FileNotFoundError as error:
        raise RuntimeError(f"{label} is missing") from error
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"{label} must be a regular non-symlink file")
    return absolute


def validated_output_path(path: pathlib.Path) -> pathlib.Path:
    absolute = reject_symlink_components(path, "whale morph output")
    parent = reject_symlink_components(absolute.parent, "whale morph output parent")
    try:
        parent_mode = parent.lstat().st_mode
    except FileNotFoundError as error:
        raise RuntimeError("whale morph output parent must already exist") from error
    if not stat.S_ISDIR(parent_mode):
        raise RuntimeError("whale morph output parent must be a directory")
    try:
        output_mode = absolute.lstat().st_mode
    except FileNotFoundError:
        return absolute
    if not stat.S_ISREG(output_mode):
        raise RuntimeError("whale morph output must be absent or a regular file")
    return absolute


def source_clip_path(parent: pathlib.Path, filename: str) -> pathlib.Path:
    relative = pathlib.PurePosixPath(filename)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
        raise RuntimeError("whale source clip filename must be one plain basename")
    return regular_file_path(parent / relative.name, "whale source clip")


def sha256_path(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json_object(path: pathlib.Path, label: str) -> dict[str, object]:
    safe_path = regular_file_path(path, label)
    value = json.loads(safe_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} root must be an object")
    return value


def load_source_index(path: pathlib.Path) -> dict[str, dict[str, object]]:
    manifest = load_json_object(path, "whale sample manifest")
    if (
        manifest.get("schema_version") != 2
        or manifest.get("kind") != "humpback_whale_sample_bank"
        or manifest.get("sample_rate_hz") != SAMPLE_RATE
    ):
        raise RuntimeError("whale sample manifest has the wrong schema")
    records = manifest.get("clips")
    if not isinstance(records, list):
        raise RuntimeError("whale sample manifest clips must be an array")
    index: dict[str, dict[str, object]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise RuntimeError("whale sample manifest contains an invalid clip")
        clip_id = record["id"]
        if clip_id in index:
            raise RuntimeError("whale sample manifest contains duplicate clip ids")
        index[clip_id] = record
    return index


def read_pcm16_mono(path: pathlib.Path) -> array.array:
    safe_path = regular_file_path(path, "source clip")
    with wave.open(str(safe_path), "rb") as handle:
        if (
            handle.getnchannels() != 1
            or handle.getsampwidth() != 2
            or handle.getframerate() != SAMPLE_RATE
        ):
            raise RuntimeError(f"unexpected source clip format: {path}")
        payload = handle.readframes(handle.getnframes())
    samples = array.array("h")
    samples.frombytes(payload)
    if struct.pack("=H", 1) != struct.pack("<H", 1):
        samples.byteswap()
    if len(samples) < SAMPLE_RATE:
        raise RuntimeError(f"source clip is too short: {safe_path}")
    return samples


def normalized_correlation(values: list[float], lag: int) -> float:
    count = len(values) - lag
    if count <= 8:
        return -1.0
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
    return product / denominator if denominator > 0.0 else -1.0


def find_period(samples: array.array) -> tuple[int, int, float]:
    downsampled = [float(sample) for sample in samples[::DOWNSAMPLE_FACTOR]]
    best_score = -1.0
    best_start = 0
    best_lag = 0
    for start in range(0, len(downsampled) - ANALYSIS_WINDOW + 1, ANALYSIS_HOP):
        window = downsampled[start : start + ANALYSIS_WINDOW]
        mean = sum(window) / len(window)
        centered = [value - mean for value in window]
        for lag in range(MIN_LAG, MAX_LAG + 1):
            score = normalized_correlation(centered, lag)
            if score > best_score:
                best_score = score
                best_start = start
                best_lag = lag
    if best_lag == 0:
        raise RuntimeError("could not estimate a periodic source window")

    coarse_period = best_lag * DOWNSAMPLE_FACTOR
    full_start = best_start * DOWNSAMPLE_FACTOR
    full_window = [
        float(value)
        for value in samples[full_start : full_start + ANALYSIS_WINDOW * DOWNSAMPLE_FACTOR]
    ]
    full_mean = sum(full_window) / len(full_window)
    centered_full = [value - full_mean for value in full_window]
    refined_period = coarse_period
    refined_score = -1.0
    for period in range(max(2, coarse_period - DOWNSAMPLE_FACTOR), coarse_period + DOWNSAMPLE_FACTOR + 1):
        score = normalized_correlation(centered_full, period)
        if score > refined_score:
            refined_score = score
            refined_period = period
    return full_start, refined_period, refined_score


def interpolate(samples: array.array, position: float) -> float:
    left = int(position)
    if left < 0:
        return float(samples[0])
    if left >= len(samples) - 1:
        return float(samples[-1])
    fraction = position - left
    return samples[left] + (samples[left + 1] - samples[left]) * fraction


def resample_cycle(samples: array.array, start: int, period: int) -> list[float]:
    return [
        interpolate(samples, start + period * index / TABLE_SIZE)
        for index in range(TABLE_SIZE)
    ]


def cycle_similarity(reference: list[float], candidate: list[float]) -> float:
    ref_mean = sum(reference) / len(reference)
    can_mean = sum(candidate) / len(candidate)
    ref_energy = 0.0
    can_energy = 0.0
    product = 0.0
    for left, right in zip(reference, candidate):
        left -= ref_mean
        right -= can_mean
        product += left * right
        ref_energy += left * left
        can_energy += right * right
    denominator = math.sqrt(ref_energy * can_energy)
    return product / denominator if denominator > 0.0 else -1.0


def averaged_table(samples: array.array, window_start: int, period: int) -> list[float]:
    cycle_count = min(24, max(8, (ANALYSIS_WINDOW * DOWNSAMPLE_FACTOR) // period - 2))
    centre = window_start + (ANALYSIS_WINDOW * DOWNSAMPLE_FACTOR) // 2
    first_start = max(0, centre - cycle_count * period // 2)
    reference = resample_cycle(samples, first_start, period)
    cycles: list[list[float]] = []
    search_radius = max(1, period // 10)
    for cycle_index in range(cycle_count):
        predicted = first_start + cycle_index * period
        best_cycle: list[float] | None = None
        best_score = -2.0
        for offset in range(-search_radius, search_radius + 1, max(1, search_radius // 8)):
            start = predicted + offset
            if start < 0 or start + period + 1 >= len(samples):
                continue
            candidate = resample_cycle(samples, start, period)
            score = cycle_similarity(reference, candidate)
            if score > best_score:
                best_score = score
                best_cycle = candidate
        if best_cycle is not None and best_score >= 0.25:
            cycles.append(best_cycle)
    if len(cycles) < 4:
        raise RuntimeError("not enough phase-coherent cycles in source window")
    table = [sum(cycle[index] for cycle in cycles) / len(cycles) for index in range(TABLE_SIZE)]
    mean = sum(table) / len(table)
    table = [value - mean for value in table]
    peak = max(abs(value) for value in table)
    if peak <= 0.0:
        raise RuntimeError("extracted wavetable is silent")
    return [value / peak * 0.92 for value in table]


def harmonic_coefficients(table: list[float], maximum: int) -> list[tuple[float, float]]:
    coefficients: list[tuple[float, float]] = []
    for harmonic in range(1, maximum + 1):
        real = 0.0
        imaginary = 0.0
        for index, value in enumerate(table):
            angle = 2.0 * math.pi * harmonic * index / TABLE_SIZE
            real += value * math.cos(angle)
            imaginary -= value * math.sin(angle)
        coefficients.append((2.0 * real / TABLE_SIZE, 2.0 * imaginary / TABLE_SIZE))
    return coefficients


def truncated_table(coefficients: list[tuple[float, float]], maximum: int) -> list[float]:
    values: list[float] = []
    for index in range(TABLE_SIZE):
        sample = 0.0
        for harmonic, (real, imaginary) in enumerate(coefficients[:maximum], start=1):
            angle = 2.0 * math.pi * harmonic * index / TABLE_SIZE
            sample += real * math.cos(angle) - imaginary * math.sin(angle)
        values.append(sample)
    peak = max(abs(value) for value in values) or 1.0
    return [value / peak * 0.92 for value in values]


def encode_table(values: list[float]) -> dict[str, object]:
    quantized = array.array("h", (round(max(-1.0, min(1.0, value)) * 32767.0) for value in values))
    if struct.pack("=H", 1) != struct.pack("<H", 1):
        quantized.byteswap()
    payload = quantized.tobytes()
    return {
        "encoding": "pcm16le-base64",
        "frames": len(values),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "payload": base64.b64encode(payload).decode("ascii"),
    }


def build(source_manifest: pathlib.Path) -> dict[str, object]:
    source_index = load_source_index(source_manifest)
    processed = source_manifest.parent
    anchors: list[dict[str, object]] = []
    for spec in ANCHORS:
        record = source_index.get(spec.clip_id)
        if record is None:
            raise RuntimeError(f"required source clip is absent: {spec.clip_id}")
        filename = record.get("file")
        expected_sha = record.get("sha256")
        if (
            not isinstance(filename, str)
            or not isinstance(expected_sha, str)
            or len(expected_sha) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha)
        ):
            raise RuntimeError(f"source clip metadata is incomplete: {spec.clip_id}")
        path = source_clip_path(processed, filename)
        actual_sha = sha256_path(path)
        if actual_sha != expected_sha:
            raise RuntimeError(f"source clip hash mismatch: {spec.clip_id}")
        samples = read_pcm16_mono(path)
        window_start, period, periodicity = find_period(samples)
        if periodicity < MIN_PERIODICITY:
            raise RuntimeError(
                f"source clip periodicity is too low: {spec.clip_id} ({periodicity:.4f})"
            )
        table = averaged_table(samples, window_start, period)
        coefficients = harmonic_coefficients(table, max(HARMONIC_LEVELS))
        levels = [
            {
                "maximum_harmonic": maximum,
                "table": encode_table(truncated_table(coefficients, maximum)),
            }
            for maximum in HARMONIC_LEVELS
        ]
        anchors.append(
            {
                "anchor_note": spec.note,
                "clip_id": spec.clip_id,
                "source_filename": filename,
                "source_sha256": actual_sha,
                "analysis_start_frame": window_start,
                "estimated_period_frames": period,
                "estimated_source_frequency_hz": SAMPLE_RATE / period,
                "periodicity": periodicity,
                "levels": levels,
            }
        )
    return {
        "schema_version": 1,
        "kind": "humpback_whale_continuous_morph_bank",
        "sample_rate_hz": SAMPLE_RATE,
        "table_size": TABLE_SIZE,
        "note_range": [21, 108],
        "tuning": "twelve-tone-equal-temperament-a4-440",
        "voice_count": 1,
        "source_sample_manifest": str(source_manifest.relative_to(ROOT)),
        "source_sample_manifest_sha256": sha256_path(source_manifest),
        "extraction": {
            "method": "phase-aligned-period-averaging-with-harmonic-bandlimiting",
            "analysis_rate_hz": ANALYSIS_RATE,
            "minimum_periodicity": MIN_PERIODICITY,
            "permanent_noise_layer": False,
            "long_phrase_playback": False,
            "preset_or_keyboard_zone_selection": False,
        },
        "harmonic_levels": list(HARMONIC_LEVELS),
        "anchors": anchors,
    }


def write_atomic(path: pathlib.Path, value: dict[str, object]) -> None:
    path = validated_output_path(path)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=pathlib.Path, default=DEFAULT_SOURCE_MANIFEST)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    source_manifest = regular_file_path(args.source_manifest, "whale sample manifest")
    try:
        source_manifest.relative_to(ROOT)
    except ValueError as error:
        raise RuntimeError("whale sample manifest escapes repository root") from error
    output = validated_output_path(args.output)
    model = build(source_manifest)
    write_atomic(output, model)
    print(
        json.dumps(
            {
                "state": "built",
                "output": str(output),
                "sha256": sha256_path(output),
                "anchor_count": len(model["anchors"]),
                "table_size": model["table_size"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
