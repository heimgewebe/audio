#!/usr/bin/env python3
"""Offline A/B probe for decoupling humpback texture time from played pitch.

This is deliberately not a production backend. It tests one falsifiable claim:
low-register source-derived texture should keep the source clock measured in the
morph manifest while the audible carrier remains bound to the played MIDI note.
Generated WAV files are evidence artifacts only and are never committed.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import pathlib
import struct
import wave
from dataclasses import dataclass

from whale_live_engine import MAX_MASTER_GAIN, WhaleVoiceConfig, clamp
from whale_morph_engine import WhaleMorphBank, midi_note_frequency

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "whale-two-clock-probe"
DEFAULT_NOTES = (21, 36, 48)
CARRIER_HARMONIC_CAP = 8
TEXTURE_HARMONIC_CAP = 4
TEXTURE_DEPTH = 0.24
OVERCLOCK_START_RATIO = 1.10
OVERCLOCK_FULL_RATIO = 2.00


@dataclass(frozen=True)
class ClockAnchor:
    note: int
    source_frequency_hz: float


class TwoClockProbe:
    """Render legacy and experimental low-register signals from one morph bank."""

    def __init__(self, config: WhaleVoiceConfig | None = None) -> None:
        self.config = config or WhaleVoiceConfig()
        if self.config.sample_rate != 48_000:
            raise ValueError("two-clock probe currently requires 48000 Hz")
        self.bank = WhaleMorphBank()
        manifest = json.loads(self.bank.manifest_path.read_text(encoding="utf-8"))
        raw_anchors = manifest.get("anchors")
        if not isinstance(raw_anchors, list):
            raise RuntimeError("morph manifest anchors are unavailable")
        clocks: list[ClockAnchor] = []
        for raw in raw_anchors:
            if not isinstance(raw, dict):
                raise RuntimeError("morph manifest anchor is invalid")
            note = raw.get("anchor_note")
            source_frequency_hz = raw.get("estimated_source_frequency_hz")
            if (
                not isinstance(note, int)
                or not isinstance(source_frequency_hz, (int, float))
                or not math.isfinite(float(source_frequency_hz))
                or float(source_frequency_hz) <= 0.0
            ):
                raise RuntimeError("morph manifest lacks a valid source clock")
            clocks.append(ClockAnchor(note, float(source_frequency_hz)))
        clocks.sort(key=lambda item: item.note)
        if tuple(item.note for item in clocks) != tuple(anchor.note for anchor in self.bank.anchors):
            raise RuntimeError("source-clock anchors do not match morph-bank anchors")
        self.clock_anchors = tuple(clocks)

    def source_clock_hz(self, timbre_note: float) -> float:
        """Log-interpolate source clocks independently of the played pitch clock."""

        note = clamp(
            float(timbre_note),
            float(self.clock_anchors[0].note),
            float(self.clock_anchors[-1].note),
        )
        if note <= self.clock_anchors[0].note:
            return self.clock_anchors[0].source_frequency_hz
        if note >= self.clock_anchors[-1].note:
            return self.clock_anchors[-1].source_frequency_hz
        for left, right in zip(self.clock_anchors, self.clock_anchors[1:]):
            if left.note <= note <= right.note:
                amount = (note - left.note) / (right.note - left.note)
                left_log = math.log(left.source_frequency_hz)
                right_log = math.log(right.source_frequency_hz)
                return math.exp(left_log + (right_log - left_log) * amount)
        raise AssertionError("source-clock interpolation is incomplete")

    @staticmethod
    def _level_with_cap(anchor, harmonic_cap: int):
        candidates = [
            level for level in anchor.levels if level.maximum_harmonic <= harmonic_cap
        ]
        if not candidates:
            return anchor.levels[-1]
        return max(candidates, key=lambda level: level.maximum_harmonic)

    def _anchor_capped_sample(self, anchor, phase: float, harmonic_cap: int) -> float:
        level = self._level_with_cap(anchor, harmonic_cap)
        return self.bank._table_sample(level.table, phase)

    def capped_sample(self, phase: float, timbre_note: float, harmonic_cap: int) -> float:
        """Sample the same source anchors with a bounded harmonic ceiling."""

        note = clamp(
            float(timbre_note),
            float(self.bank.anchors[0].note),
            float(self.bank.anchors[-1].note),
        )
        if note <= self.bank.anchors[0].note:
            return self._anchor_capped_sample(self.bank.anchors[0], phase, harmonic_cap)
        if note >= self.bank.anchors[-1].note:
            return self._anchor_capped_sample(self.bank.anchors[-1], phase, harmonic_cap)
        for left, right in zip(self.bank.anchors, self.bank.anchors[1:]):
            if left.note <= note <= right.note:
                amount = (note - left.note) / (right.note - left.note)
                left_sample = self._anchor_capped_sample(left, phase, harmonic_cap)
                right_sample = self._anchor_capped_sample(right, phase, harmonic_cap)
                return (
                    left_sample * math.cos(amount * math.pi / 2.0)
                    + right_sample * math.sin(amount * math.pi / 2.0)
                )
        raise AssertionError("capped timbre interpolation is incomplete")

    @staticmethod
    def decoupling_amount(pitch_hz: float, source_clock_hz: float) -> float:
        ratio = pitch_hz / max(source_clock_hz, 1.0e-9)
        span = OVERCLOCK_FULL_RATIO - OVERCLOCK_START_RATIO
        return clamp((ratio - OVERCLOCK_START_RATIO) / span, 0.0, 1.0)

    def describe_note(self, note: int) -> dict[str, float | int]:
        if not 21 <= note <= 108:
            raise ValueError("note must be within the 88-key range")
        pitch_hz = midi_note_frequency(note)
        source_hz = self.source_clock_hz(note)
        amount = self.decoupling_amount(pitch_hz, source_hz)
        return {
            "note": note,
            "pitch_hz": pitch_hz,
            "pitch_period_ms": 1000.0 / pitch_hz,
            "source_clock_hz": source_hz,
            "source_clock_period_ms": 1000.0 / source_hz,
            "pitch_to_source_clock_ratio": pitch_hz / source_hz,
            "decoupling_amount": amount,
            "carrier_harmonic_cap": CARRIER_HARMONIC_CAP,
            "texture_harmonic_cap": TEXTURE_HARMONIC_CAP,
            "texture_depth": TEXTURE_DEPTH,
        }

    def render_note(
        self,
        note: int,
        duration_seconds: float,
        *,
        two_clock: bool,
    ) -> list[float]:
        if not 0.05 <= duration_seconds <= 12.0:
            raise ValueError("duration_seconds must be between 0.05 and 12")
        description = self.describe_note(note)
        pitch_hz = float(description["pitch_hz"])
        source_hz = float(description["source_clock_hz"])
        amount = float(description["decoupling_amount"])
        frames = round(duration_seconds * self.config.sample_rate)
        fade_frames = max(1, min(round(0.04 * self.config.sample_rate), frames // 4))
        pitch_phase = 0.0
        texture_phase = 0.0
        output: list[float] = []
        for index in range(frames):
            pitch_phase = (pitch_phase + pitch_hz / self.config.sample_rate) % 1.0
            legacy = self.bank.sample(pitch_phase, float(note), pitch_hz)
            if two_clock and amount > 0.0:
                texture_phase = (
                    texture_phase + source_hz / self.config.sample_rate
                ) % 1.0
                carrier = self.capped_sample(
                    pitch_phase, float(note), CARRIER_HARMONIC_CAP
                )
                texture = self.capped_sample(
                    texture_phase, float(note), TEXTURE_HARMONIC_CAP
                )
                texture = math.tanh(texture * 1.35)
                separated = carrier * (1.0 + TEXTURE_DEPTH * amount * texture)
                voice_sample = legacy + (separated - legacy) * amount
            else:
                voice_sample = legacy
            fade_in = min(1.0, (index + 1) / fade_frames)
            fade_out = min(1.0, (frames - index) / fade_frames)
            envelope = min(fade_in, fade_out)
            raw = voice_sample * envelope * self.config.master_gain
            sample = raw / (1.0 + 1.05 * abs(raw))
            output.append(clamp(sample, -MAX_MASTER_GAIN, MAX_MASTER_GAIN))
        return output


def write_stereo_pcm16(path: pathlib.Path, samples: list[float], sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    interleaved = array.array("h")
    for sample in samples:
        value = round(clamp(sample, -1.0, 1.0) * 32767.0)
        interleaved.extend((value, value))
    if struct.pack("=H", 1) != struct.pack("<H", 1):
        interleaved.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(interleaved.tobytes())


def build_probe(output_dir: pathlib.Path, notes: tuple[int, ...], duration_seconds: float) -> dict[str, object]:
    probe = TwoClockProbe()
    records: list[dict[str, object]] = []
    for note in notes:
        legacy = probe.render_note(note, duration_seconds, two_clock=False)
        candidate = probe.render_note(note, duration_seconds, two_clock=True)
        legacy_name = f"legacy-note-{note}.wav"
        candidate_name = f"two-clock-note-{note}.wav"
        write_stereo_pcm16(output_dir / legacy_name, legacy, probe.config.sample_rate)
        write_stereo_pcm16(output_dir / candidate_name, candidate, probe.config.sample_rate)
        description = probe.describe_note(note)
        records.append(
            {
                **description,
                "legacy_wav": legacy_name,
                "two_clock_wav": candidate_name,
                "render_changed": legacy != candidate,
                "legacy_peak": max(abs(value) for value in legacy),
                "two_clock_peak": max(abs(value) for value in candidate),
            }
        )
    report = {
        "schema_version": 1,
        "kind": "buckelwal_two_clock_low_register_probe",
        "status": "experimental-not-runtime",
        "hypothesis": (
            "source-derived low-register texture should retain its measured source "
            "clock while played pitch remains MIDI-bound"
        ),
        "sample_rate_hz": probe.config.sample_rate,
        "duration_seconds": duration_seconds,
        "notes": records,
        "non_claims": [
            "production backend approval",
            "perceptual equivalence to a humpback whale",
            "biological source-filter model",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--duration-seconds", type=float, default=4.0)
    parser.add_argument("--note", type=int, action="append", dest="notes")
    args = parser.parse_args()
    notes = tuple(args.notes) if args.notes else DEFAULT_NOTES
    report = build_probe(args.output_dir, notes, args.duration_seconds)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
