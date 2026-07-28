#!/usr/bin/env python3
"""Sample-based, monophonic humpback-whale voice for the Roland keyboard."""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import struct
import wave
from array import array
from dataclasses import dataclass
from typing import Any

from whale_live_engine import MidiEvent, WhaleVoiceConfig, clamp

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_BANK_MANIFEST = (
    ROOT / "assets" / "whale-sources" / "processed" / "manifest.json"
)
MAX_SAMPLE_VALUE = 32768.0
MAX_MASTER_GAIN = 0.25
SILENCE_THRESHOLD = 1e-7


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SampleClip:
    clip_id: str
    category: str
    samples: array
    loop_start: int
    loop_end: int
    loop_crossfade: int


@dataclass(frozen=True)
class SampleSlot:
    clip: SampleClip
    root_note: int
    minimum_note: int
    maximum_note: int


@dataclass
class PlaybackLayer:
    slot: SampleSlot
    position: float
    rate: float
    target_rate: float


class WhaleSampleBank:
    def __init__(self, manifest_path: pathlib.Path = DEFAULT_BANK_MANIFEST) -> None:
        self.manifest_path = manifest_path.resolve()
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema_version") != 1
            or manifest.get("kind") != "humpback_whale_sample_bank"
        ):
            raise RuntimeError("whale sample bank manifest has the wrong schema")
        if manifest.get("sample_rate_hz") != 48_000:
            raise RuntimeError("whale sample bank must use 48000 Hz")
        if manifest.get("channels") != 1 or manifest.get("sample_width_bytes") != 2:
            raise RuntimeError("whale sample bank must be mono PCM16")
        source_records = manifest.get("sources")
        clip_records = manifest.get("clips")
        slot_records = manifest.get("slots")
        if not all(
            isinstance(value, list)
            for value in (source_records, clip_records, slot_records)
        ):
            raise RuntimeError("whale sample bank manifest is incomplete")
        allowed_licenses = {"CC0-1.0", "Public-Domain-US-NPS", "CC-BY-2.5"}
        for source in source_records:
            if source.get("license") not in allowed_licenses:
                raise RuntimeError(
                    f"unsupported whale source license: {source.get('license')}"
                )
            if not source.get("source_page") or not source.get("attribution"):
                raise RuntimeError("whale source attribution is incomplete")

        clips: dict[str, SampleClip] = {}
        root = self.manifest_path.parent
        for record in clip_records:
            clip_id = str(record["id"])
            path = root / str(record["file"])
            if not path.is_file():
                raise RuntimeError(f"whale sample clip is missing: {path}")
            if sha256_file(path) != record.get("sha256"):
                raise RuntimeError(f"whale sample clip hash mismatch: {path}")
            samples = self._read_wav(path)
            if len(samples) != int(record["frames"]):
                raise RuntimeError(f"whale sample frame count mismatch: {path}")
            loop_start = int(record["loop_start_frame"])
            loop_end = int(record["loop_end_frame"])
            loop_crossfade = int(record["loop_crossfade_frames"])
            if not 0 <= loop_start < loop_end <= len(samples):
                raise RuntimeError(f"invalid whale sample loop: {clip_id}")
            if not 1 <= loop_crossfade < (loop_end - loop_start) // 2:
                raise RuntimeError(f"invalid whale sample loop crossfade: {clip_id}")
            clips[clip_id] = SampleClip(
                clip_id=clip_id,
                category=str(record["category"]),
                samples=samples,
                loop_start=loop_start,
                loop_end=loop_end,
                loop_crossfade=loop_crossfade,
            )

        self.slots: tuple[SampleSlot, ...] = tuple(
            SampleSlot(
                clip=clips[str(record["clip_id"])],
                root_note=int(record["root_note"]),
                minimum_note=int(record["minimum_note"]),
                maximum_note=int(record["maximum_note"]),
            )
            for record in slot_records
        )
        if not self.slots:
            raise RuntimeError("whale sample bank has no keyboard slots")
        self.sources = tuple(source_records)
        self.manifest = manifest

    @staticmethod
    def _read_wav(path: pathlib.Path) -> array:
        with wave.open(str(path), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise RuntimeError(f"unexpected whale sample format: {path}")
            if handle.getframerate() != 48_000:
                raise RuntimeError(f"unexpected whale sample rate: {path}")
            payload = handle.readframes(handle.getnframes())
        samples = array("h")
        samples.frombytes(payload)
        if struct.pack("=H", 1) != struct.pack("<H", 1):
            samples.byteswap()
        return array("f", (sample / MAX_SAMPLE_VALUE for sample in samples))

    def select(self, note: int) -> SampleSlot:
        note = int(clamp(note, 21, 108))
        preferred = "low" if note <= 48 else "high" if note >= 85 else "song"
        candidates = [slot for slot in self.slots if slot.clip.category == preferred]
        if not candidates:
            candidates = list(self.slots)
        containing = [
            slot
            for slot in candidates
            if slot.minimum_note <= note <= slot.maximum_note
        ]
        pool = containing or candidates
        return min(pool, key=lambda slot: (abs(note - slot.root_note), slot.root_note))

    def status(self) -> dict[str, Any]:
        return {
            "ready": True,
            "manifest": str(self.manifest_path),
            "source_count": len(self.sources),
            "clip_count": len({slot.clip.clip_id for slot in self.slots}),
            "slot_count": len(self.slots),
            "licenses": sorted({str(source["license"]) for source in self.sources}),
        }


def sample_bank_status(
    manifest_path: pathlib.Path = DEFAULT_BANK_MANIFEST,
) -> dict[str, Any]:
    try:
        return WhaleSampleBank(manifest_path).status()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        return {"ready": False, "manifest": str(manifest_path), "error": str(error)}


class WhaleSampleVoice:
    """One natural-recording voice with bounded resampling and crossfaded loops."""

    def __init__(
        self,
        config: WhaleVoiceConfig | None = None,
        *,
        bank: WhaleSampleBank | None = None,
    ) -> None:
        self.config = config or WhaleVoiceConfig()
        if self.config.sample_rate != 48_000:
            raise ValueError("realistic whale voice currently requires 48000 Hz")
        self.bank = bank or WhaleSampleBank()
        self.held_notes: dict[int, tuple[int, int]] = {}
        self._order = 0
        self.active_note: int | None = None
        self.gate = False
        self.sustain = 0
        self.modulation = 0.0
        self.expression = 1.0
        self.distance = 0.0
        self.pitch_bend_cents = 0.0
        self.velocity = 0.5
        self.target_velocity = 0.5
        self.envelope = 0.0
        self.attack_seconds = 0.055
        self.release_seconds = 0.9
        self.hold_frames = 0
        self.current: PlaybackLayer | None = None
        self.previous: PlaybackLayer | None = None
        self.crossfade_total = round(self.config.sample_rate * 0.09)
        self.crossfade_remaining = 0
        self.flutter_phase = 0.0

    @property
    def active(self) -> bool:
        return self.gate or self.envelope > SILENCE_THRESHOLD

    @property
    def silent(self) -> bool:
        return not self.gate and self.envelope == 0.0 and self.previous is None

    def dispatch(self, event: MidiEvent) -> None:
        if event.kind == "note_on":
            self.note_on(event.note, event.velocity)
        elif event.kind == "note_off":
            self.note_off(event.note)
        elif event.kind == "control_change":
            self.control_change(event.controller, event.value)
        elif event.kind == "pitch_bend":
            self.pitch_bend(event.value)

    def _rate_for(self, note: int, slot: SampleSlot) -> float:
        semitones = clamp(note - slot.root_note, -4, 4)
        return 2.0 ** ((semitones + self.pitch_bend_cents / 100.0) / 12.0)

    def _retarget(self, note: int, velocity: int, *, detached: bool) -> None:
        slot = self.bank.select(note)
        rate = self._rate_for(note, slot)
        self.active_note = note
        self.target_velocity = velocity / 127.0
        self.attack_seconds = clamp(0.09 - 0.06 * self.target_velocity, 0.025, 0.09)
        if self.current and self.current.slot.clip.clip_id == slot.clip.clip_id:
            self.current.target_rate = rate
            self.gate = True
            return
        new_layer = PlaybackLayer(slot=slot, position=0.0, rate=rate, target_rate=rate)
        if self.current and self.envelope > SILENCE_THRESHOLD:
            self.previous = self.current
            self.crossfade_remaining = self.crossfade_total
        else:
            self.previous = None
            self.crossfade_remaining = 0
            if detached:
                self.envelope = 0.0
        self.current = new_layer
        self.gate = True

    def note_on(self, note: int, velocity: int) -> None:
        note = int(clamp(note, 21, 108))
        velocity = int(clamp(velocity, 1, 127))
        detached = not self.gate and not self.held_notes
        self._order += 1
        self.held_notes[note] = (velocity, self._order)
        self._retarget(note, velocity, detached=detached)

    def note_off(self, note: int) -> None:
        self.held_notes.pop(note, None)
        if self.held_notes:
            next_note, (velocity, _order) = max(
                self.held_notes.items(), key=lambda item: item[1][1]
            )
            self._retarget(next_note, velocity, detached=False)
            return
        if self.sustain >= 64:
            return
        self._begin_release()

    def _begin_release(self, pedal_value: int | None = None) -> None:
        self.gate = False
        held_seconds = self.hold_frames / self.config.sample_rate
        effective_pedal = self.sustain if pedal_value is None else pedal_value
        pedal_tail = 1.2 * (clamp(effective_pedal, 0, 127) / 127.0)
        self.release_seconds = clamp(0.45 + held_seconds * 0.08 + pedal_tail, 0.45, 3.2)

    def control_change(self, controller: int, value: int) -> None:
        value = int(clamp(value, 0, 127))
        if controller == 64:
            previous = self.sustain
            was_down = previous >= 64
            self.sustain = value
            if was_down and value < 64 and not self.held_notes:
                self._begin_release(previous)
        elif controller == 1:
            self.modulation = value / 127.0
        elif controller == 11:
            self.expression = value / 127.0
        elif controller == 67:
            self.distance = value / 127.0
        elif controller == 120:
            self._silence_immediately()
        elif controller == 123:
            self.held_notes.clear()
            self.sustain = 0
            self._begin_release()

    def pitch_bend(self, value: int) -> None:
        bounded = clamp(value, -8192, 8191)
        self.pitch_bend_cents = 120.0 * bounded / 8192.0
        if self.current and self.active_note is not None:
            self.current.target_rate = self._rate_for(
                self.active_note, self.current.slot
            )

    def _silence_immediately(self) -> None:
        self.held_notes.clear()
        self.active_note = None
        self.gate = False
        self.sustain = 0
        self.envelope = 0.0
        self.hold_frames = 0
        self.current = None
        self.previous = None
        self.crossfade_remaining = 0

    @staticmethod
    def _interpolated(samples: array, position: float) -> float:
        if position <= 0:
            return samples[0]
        if position >= len(samples) - 1:
            return samples[-1]
        left = int(position)
        fraction = position - left
        return samples[left] + (samples[left + 1] - samples[left]) * fraction

    def _layer_sample(self, layer: PlaybackLayer, looping: bool) -> float:
        clip = layer.slot.clip
        if layer.position >= len(clip.samples) - 1:
            if looping:
                layer.position = float(clip.loop_start)
            else:
                return 0.0
        position = layer.position
        sample = self._interpolated(clip.samples, position)
        if looping and position >= clip.loop_end - clip.loop_crossfade:
            progress = (
                position - (clip.loop_end - clip.loop_crossfade)
            ) / clip.loop_crossfade
            progress = clamp(progress, 0.0, 1.0)
            alternate_position = clip.loop_start + (
                position - (clip.loop_end - clip.loop_crossfade)
            )
            alternate = self._interpolated(clip.samples, alternate_position)
            sample = sample * math.cos(progress * math.pi / 2.0) + alternate * math.sin(
                progress * math.pi / 2.0
            )
        rate_alpha = 1.0 - math.exp(-1.0 / (self.config.sample_rate * 0.08))
        layer.rate += (layer.target_rate - layer.rate) * rate_alpha
        flutter_cents = self.modulation * 2.5 * math.sin(self.flutter_phase)
        effective_rate = layer.rate * 2.0 ** (flutter_cents / 1200.0)
        layer.position += effective_rate
        if looping and layer.position >= clip.loop_end:
            layer.position = clip.loop_start + (layer.position - clip.loop_end)
        return sample

    def render(self, frames: int) -> list[float]:
        if frames < 0 or frames > self.config.sample_rate * 30:
            raise ValueError("render frame count is outside the bounded range")
        if frames == 0:
            return []
        if self.silent:
            return [0.0] * frames
        output: list[float] = []
        attack_alpha = 1.0 - math.exp(
            -1.0 / (self.config.sample_rate * max(self.attack_seconds, 0.001))
        )
        release_alpha = 1.0 - math.exp(
            -6.907755278982137
            / (self.config.sample_rate * max(self.release_seconds, 0.001))
        )
        velocity_alpha = 1.0 - math.exp(-1.0 / (self.config.sample_rate * 0.045))
        looping = self.gate or self.sustain >= 64
        for index in range(frames):
            if self.gate:
                self.envelope += (1.0 - self.envelope) * attack_alpha
                self.hold_frames += 1
            else:
                self.envelope += (0.0 - self.envelope) * release_alpha
                if self.envelope < SILENCE_THRESHOLD:
                    self.envelope = 0.0
                    self.current = None
                    self.previous = None
                    self.crossfade_remaining = 0
                    output.extend([0.0] * (frames - index))
                    break
            self.velocity += (self.target_velocity - self.velocity) * velocity_alpha
            self.flutter_phase = (
                self.flutter_phase + 2.0 * math.pi * 0.7 / self.config.sample_rate
            ) % (2.0 * math.pi)
            current_sample = (
                self._layer_sample(self.current, looping) if self.current else 0.0
            )
            if self.previous and self.crossfade_remaining > 0:
                previous_sample = self._layer_sample(self.previous, looping)
                progress = 1.0 - self.crossfade_remaining / self.crossfade_total
                current_mix = math.sin(progress * math.pi / 2.0)
                previous_mix = math.cos(progress * math.pi / 2.0)
                voice_sample = (
                    previous_sample * previous_mix + current_sample * current_mix
                )
                self.crossfade_remaining -= 1
                if self.crossfade_remaining == 0:
                    self.previous = None
            else:
                voice_sample = current_sample
            velocity_gain = 0.34 + 0.66 * self.velocity**1.25
            distance_gain = 1.0 - 0.42 * self.distance
            raw = (
                voice_sample
                * self.envelope
                * velocity_gain
                * self.expression
                * distance_gain
                * self.config.master_gain
            )
            sample = raw / (1.0 + 0.9 * abs(raw))
            output.append(clamp(sample, -MAX_MASTER_GAIN, MAX_MASTER_GAIN))
        return output

    def render_f32_stereo(self, frames: int) -> bytes:
        if frames < 0 or frames > self.config.sample_rate * 30:
            raise ValueError("render frame count is outside the bounded range")
        if self.silent:
            return bytes(frames * 2 * 4)
        mono = self.render(frames)
        interleaved = array("f")
        for sample in mono:
            interleaved.append(sample * 0.99)
            interleaved.append(sample)
        if struct.pack("=I", 1) != struct.pack("<I", 1):
            interleaved.byteswap()
        return interleaved.tobytes()
