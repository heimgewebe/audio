#!/usr/bin/env python3
"""Build a deterministic local humpback-whale sample bank from licensed sources."""

from __future__ import annotations

import argparse
import audioop
import hashlib
import json
import pathlib
import shutil
import struct
import subprocess
import tempfile
import wave
from array import array
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "assets" / "whale-sources"
SOURCE_CATALOG = SOURCE_ROOT / "SOURCES.json"
PROCESSED_ROOT = SOURCE_ROOT / "processed"
MANIFEST_PATH = PROCESSED_ROOT / "manifest.json"
SAMPLE_RATE = 48_000
TARGET_PEAK = 25_500
WINDOW_SECONDS = 0.25
HOP_SECONDS = 0.125
MIN_PEAK_DISTANCE_SECONDS = 3.5
CLIP_SECONDS = {"low": 6.0, "song": 7.0, "high": 5.0}
ROOT_RANGES = {"low": (24, 48), "song": (42, 90), "high": (84, 108)}


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_ffmpeg(source: pathlib.Path, output: pathlib.Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to build the whale sample bank")
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-sample_fmt",
            "s16",
            "-af",
            "highpass=f=22,lowpass=f=7000,acompressor=threshold=-28dB:ratio=2:attack=20:release=250",
            str(output),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffmpeg failed for {source}")


def read_wav(path: pathlib.Path) -> array:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise RuntimeError(f"unexpected intermediate WAV format: {path}")
        if handle.getframerate() != SAMPLE_RATE:
            raise RuntimeError(f"unexpected intermediate sample rate: {path}")
        frames = handle.readframes(handle.getnframes())
    samples = array("h")
    samples.frombytes(frames)
    if struct.pack("=H", 1) != struct.pack("<H", 1):
        samples.byteswap()
    return samples


def candidate_centers(samples: array, count: int, clip_frames: int) -> list[int]:
    window = max(1, round(WINDOW_SECONDS * SAMPLE_RATE))
    hop = max(1, round(HOP_SECONDS * SAMPLE_RATE))
    half_clip = clip_frames // 2
    scored: list[tuple[int, int]] = []
    raw = samples.tobytes()
    for start in range(0, max(1, len(samples) - window), hop):
        center = start + window // 2
        if center < half_clip or center + half_clip >= len(samples):
            continue
        rms = audioop.rms(raw[start * 2 : (start + window) * 2], 2)
        scored.append((rms, center))
    if not scored:
        return [len(samples) // 2]
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected: list[int] = []
    minimum_distance = round(MIN_PEAK_DISTANCE_SECONDS * SAMPLE_RATE)
    for rms, center in scored:
        if rms <= 0:
            continue
        if all(abs(center - previous) >= minimum_distance for previous in selected):
            selected.append(center)
        if len(selected) == count:
            break
    if len(selected) < count:
        evenly = [
            round((index + 1) * len(samples) / (count + 1)) for index in range(count)
        ]
        for center in evenly:
            center = min(max(center, half_clip), len(samples) - half_clip - 1)
            if all(abs(center - previous) >= window for previous in selected):
                selected.append(center)
            if len(selected) == count:
                break
    return sorted(selected[:count])


def normalize_and_fade(samples: array) -> array:
    if not samples:
        raise RuntimeError("cannot normalize an empty clip")
    mean = sum(samples) / len(samples)
    corrected = array(
        "h", (int(max(-32768, min(32767, sample - mean))) for sample in samples)
    )
    peak = max(abs(sample) for sample in corrected) or 1
    scale = min(8.0, TARGET_PEAK / peak)
    fade_frames = min(round(0.04 * SAMPLE_RATE), len(corrected) // 4)
    result = array("h")
    for index, sample in enumerate(corrected):
        gain = 1.0
        if index < fade_frames:
            gain *= index / max(1, fade_frames)
        tail = len(corrected) - 1 - index
        if tail < fade_frames:
            gain *= tail / max(1, fade_frames)
        value = round(sample * scale * gain)
        result.append(max(-32768, min(32767, value)))
    return result


def write_wav(path: pathlib.Path, samples: array) -> None:
    payload = array("h", samples)
    if struct.pack("=H", 1) != struct.pack("<H", 1):
        payload.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(payload.tobytes())


def distributed_roots(count: int, lower: int, upper: int) -> list[int]:
    if count <= 1:
        return [round((lower + upper) / 2)]
    return [
        round(lower + index * (upper - lower) / (count - 1)) for index in range(count)
    ]


def build(source_catalog: pathlib.Path, output_root: pathlib.Path) -> dict[str, Any]:
    catalog = json.loads(source_catalog.read_text(encoding="utf-8"))
    sources = catalog.get("sources")
    if catalog.get("schema_version") != 1 or not isinstance(sources, list):
        raise RuntimeError("whale source catalog has the wrong schema")
    output_root.mkdir(parents=True, exist_ok=True)
    for existing in output_root.glob("*.wav"):
        existing.unlink()

    clips: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="whale-bank-") as directory:
        temporary_root = pathlib.Path(directory)
        for source in sources:
            source_id = str(source["id"])
            category = str(source["category"])
            if category not in CLIP_SECONDS:
                raise RuntimeError(f"unknown source category: {category}")
            source_path = SOURCE_ROOT / str(source["file"])
            if not source_path.is_file():
                raise RuntimeError(f"missing source audio: {source_path}")
            intermediate = temporary_root / f"{source_id}.wav"
            run_ffmpeg(source_path, intermediate)
            samples = read_wav(intermediate)
            clip_frames = round(CLIP_SECONDS[category] * SAMPLE_RATE)
            count = int(source.get("clip_count", 2))
            centers = candidate_centers(samples, count, clip_frames)
            source_records.append(
                {
                    "id": source_id,
                    "file": str(pathlib.Path(source["file"])),
                    "sha256": sha256_file(source_path),
                    "license": source["license"],
                    "attribution": source["attribution"],
                    "source_page": source["source_page"],
                    "category": category,
                }
            )
            for clip_index, center in enumerate(centers, start=1):
                start = max(0, center - clip_frames // 2)
                end = min(len(samples), start + clip_frames)
                start = max(0, end - clip_frames)
                clip_samples = normalize_and_fade(samples[start:end])
                clip_id = f"{source_id}-{clip_index:02d}"
                filename = f"{clip_id}.wav"
                destination = output_root / filename
                write_wav(destination, clip_samples)
                frame_count = len(clip_samples)
                clips.append(
                    {
                        "id": clip_id,
                        "source_id": source_id,
                        "category": category,
                        "file": filename,
                        "frames": frame_count,
                        "sha256": sha256_file(destination),
                        "loop_start_frame": round(frame_count * 0.22),
                        "loop_end_frame": round(frame_count * 0.78),
                        "loop_crossfade_frames": min(
                            round(0.12 * SAMPLE_RATE), round(frame_count * 0.08)
                        ),
                    }
                )

    slots: list[dict[str, Any]] = []
    for category in ("low", "song", "high"):
        category_clips = [clip for clip in clips if clip["category"] == category]
        lower, upper = ROOT_RANGES[category]
        roots = list(range(lower, upper + 1, 4))
        if roots[-1] != upper:
            roots.append(upper)
        for index, root_note in enumerate(roots):
            clip_index = (
                0
                if len(roots) == 1
                else round(index * (len(category_clips) - 1) / (len(roots) - 1))
            )
            clip = category_clips[clip_index]
            slots.append(
                {
                    "clip_id": clip["id"],
                    "root_note": root_note,
                    "minimum_note": max(21, root_note - 3),
                    "maximum_note": min(108, root_note + 3),
                }
            )
    manifest = {
        "schema_version": 1,
        "kind": "humpback_whale_sample_bank",
        "sample_rate_hz": SAMPLE_RATE,
        "channels": 1,
        "sample_width_bytes": 2,
        "source_catalog_sha256": sha256_file(source_catalog),
        "sources": source_records,
        "clips": clips,
        "slots": sorted(slots, key=lambda slot: (slot["root_note"], slot["clip_id"])),
        "render_contract": {
            "maximum_pitch_shift_semitones": 4,
            "monophonic": True,
            "looping": "equal-power-crossfade",
            "default_voice_mode": "realistic",
        },
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=pathlib.Path, default=SOURCE_CATALOG)
    parser.add_argument("--output", type=pathlib.Path, default=PROCESSED_ROOT)
    args = parser.parse_args()
    manifest = build(args.catalog.resolve(), args.output.resolve())
    print(
        json.dumps(
            {
                "state": "built",
                "manifest": str((args.output / "manifest.json").resolve()),
                "source_count": len(manifest["sources"]),
                "clip_count": len(manifest["clips"]),
                "slot_count": len(manifest["slots"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
