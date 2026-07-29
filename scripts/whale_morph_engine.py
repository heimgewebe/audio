#!/usr/bin/env python3
"""Continuous, source-derived monophonic humpback-whale instrument.

Every MIDI note from A0 to C8 uses twelve-tone equal temperament. Timbre is
morphed continuously between internal source-derived anchors; no key selects a
sample, preset, or control function. The engine contains no permanent noise
layer and never plays a recorded phrase.
"""

from __future__ import annotations

import array
import base64
import hashlib
import json
import math
import os
import pathlib
import stat
import struct
from dataclasses import dataclass

from whale_live_engine import MidiEvent, WhaleVoiceConfig, clamp

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "assets" / "whale-sources" / "morph" / "manifest.json"
SILENCE_THRESHOLD = 1e-7
MAX_MASTER_GAIN = 0.25


def absolute_path(path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(os.path.abspath(os.fspath(path)))


def regular_file_path(path: pathlib.Path, label: str) -> pathlib.Path:
    absolute = absolute_path(path)
    for candidate in [*reversed(absolute.parents), absolute]:
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError as error:
            if candidate == absolute:
                raise RuntimeError(f"{label} is missing") from error
            continue
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"{label} must not contain symlink components")
    if not stat.S_ISREG(absolute.lstat().st_mode):
        raise RuntimeError(f"{label} must be a regular non-symlink file")
    return absolute


def sha256_path(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def midi_note_frequency(note: float) -> float:
    """Return 12-TET frequency for a MIDI note, with A4 fixed at 440 Hz."""

    return 440.0 * 2.0 ** ((float(note) - 69.0) / 12.0)


def frequency_to_midi_note(frequency_hz: float) -> float:
    if frequency_hz <= 0.0 or not math.isfinite(frequency_hz):
        raise ValueError("frequency must be positive and finite")
    return 69.0 + 12.0 * math.log2(frequency_hz / 440.0)


@dataclass(frozen=True)
class MorphLevel:
    maximum_harmonic: int
    table: tuple[float, ...]


@dataclass(frozen=True)
class MorphAnchor:
    note: int
    clip_id: str
    periodicity: float
    levels: tuple[MorphLevel, ...]


class WhaleMorphBank:
    """Validated embedded wavetable bank with continuous timbre interpolation."""

    def __init__(self, manifest_path: pathlib.Path = DEFAULT_MANIFEST) -> None:
        self.manifest_path = regular_file_path(manifest_path, "whale morph manifest")
        value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("whale morph manifest root must be an object")
        if (
            value.get("schema_version") != 1
            or value.get("kind") != "humpback_whale_continuous_morph_bank"
            or value.get("sample_rate_hz") != 48_000
            or value.get("note_range") != [21, 108]
            or value.get("tuning") != "twelve-tone-equal-temperament-a4-440"
            or value.get("voice_count") != 1
        ):
            raise RuntimeError("whale morph manifest has the wrong schema")
        table_size = value.get("table_size")
        if not isinstance(table_size, int) or not 128 <= table_size <= 8_192:
            raise RuntimeError("whale morph table size is invalid")
        self.sample_rate = 48_000
        self.table_size = table_size

        source_manifest = value.get("source_sample_manifest")
        source_sha = value.get("source_sample_manifest_sha256")
        if not isinstance(source_manifest, str) or not isinstance(source_sha, str):
            raise RuntimeError("whale morph source provenance is incomplete")
        relative_source = pathlib.PurePosixPath(source_manifest)
        if (
            relative_source.is_absolute()
            or not relative_source.parts
            or any(part in {"", ".", ".."} for part in relative_source.parts)
        ):
            raise RuntimeError("whale morph source manifest path is invalid")
        source_path = regular_file_path(
            ROOT.joinpath(*relative_source.parts), "whale morph source manifest"
        )
        try:
            source_path.relative_to(ROOT)
        except ValueError as error:
            raise RuntimeError("whale morph source manifest escapes repository root") from error
        if sha256_path(source_path) != source_sha:
            raise RuntimeError("whale morph source manifest hash mismatch")
        source_value = json.loads(source_path.read_text(encoding="utf-8"))
        if (
            not isinstance(source_value, dict)
            or source_value.get("schema_version") != 2
            or source_value.get("kind") != "humpback_whale_sample_bank"
        ):
            raise RuntimeError("whale morph source manifest has the wrong schema")
        raw_source_clips = source_value.get("clips")
        if not isinstance(raw_source_clips, list):
            raise RuntimeError("whale morph source manifest clips must be an array")
        source_clips: dict[str, dict[str, object]] = {}
        for raw_source_clip in raw_source_clips:
            if (
                not isinstance(raw_source_clip, dict)
                or not isinstance(raw_source_clip.get("id"), str)
                or not isinstance(raw_source_clip.get("file"), str)
                or not isinstance(raw_source_clip.get("sha256"), str)
            ):
                raise RuntimeError("whale morph source manifest contains an invalid clip")
            source_id = raw_source_clip["id"]
            if source_id in source_clips:
                raise RuntimeError("whale morph source manifest contains duplicate clip ids")
            source_clips[source_id] = raw_source_clip

        raw_anchors = value.get("anchors")
        if not isinstance(raw_anchors, list) or len(raw_anchors) < 3:
            raise RuntimeError("whale morph bank needs at least three anchors")
        anchors: list[MorphAnchor] = []
        for raw_anchor in raw_anchors:
            if not isinstance(raw_anchor, dict):
                raise RuntimeError("whale morph anchor must be an object")
            note = raw_anchor.get("anchor_note")
            clip_id = raw_anchor.get("clip_id")
            source_filename = raw_anchor.get("source_filename")
            source_anchor_sha = raw_anchor.get("source_sha256")
            periodicity = raw_anchor.get("periodicity")
            raw_levels = raw_anchor.get("levels")
            if (
                not isinstance(note, int)
                or not 21 <= note <= 108
                or not isinstance(clip_id, str)
                or not isinstance(source_filename, str)
                or not isinstance(source_anchor_sha, str)
                or not isinstance(periodicity, (int, float))
                or not 0.0 <= float(periodicity) <= 1.0
                or not isinstance(raw_levels, list)
            ):
                raise RuntimeError("whale morph anchor metadata is invalid")
            source_record = source_clips.get(clip_id)
            if (
                source_record is None
                or source_record.get("file") != source_filename
                or source_record.get("sha256") != source_anchor_sha
            ):
                raise RuntimeError("whale morph anchor provenance does not match source manifest")
            levels: list[MorphLevel] = []
            previous_harmonic = 1 << 30
            for raw_level in raw_levels:
                if not isinstance(raw_level, dict):
                    raise RuntimeError("whale morph level must be an object")
                maximum = raw_level.get("maximum_harmonic")
                raw_table = raw_level.get("table")
                if (
                    not isinstance(maximum, int)
                    or maximum <= 0
                    or maximum >= previous_harmonic
                    or not isinstance(raw_table, dict)
                    or raw_table.get("encoding") != "pcm16le-base64"
                    or raw_table.get("frames") != table_size
                    or not isinstance(raw_table.get("sha256"), str)
                    or not isinstance(raw_table.get("payload"), str)
                ):
                    raise RuntimeError("whale morph level metadata is invalid")
                try:
                    payload = base64.b64decode(raw_table["payload"], validate=True)
                except (ValueError, TypeError) as error:
                    raise RuntimeError("whale morph table base64 is invalid") from error
                if hashlib.sha256(payload).hexdigest() != raw_table["sha256"]:
                    raise RuntimeError("whale morph table hash mismatch")
                samples = array.array("h")
                samples.frombytes(payload)
                if struct.pack("=H", 1) != struct.pack("<H", 1):
                    samples.byteswap()
                if len(samples) != table_size:
                    raise RuntimeError("whale morph table frame count mismatch")
                levels.append(
                    MorphLevel(maximum, tuple(sample / 32768.0 for sample in samples))
                )
                previous_harmonic = maximum
            if not levels or levels[-1].maximum_harmonic != 1:
                raise RuntimeError("whale morph anchor lacks a fundamental-only level")
            anchors.append(MorphAnchor(note, clip_id, float(periodicity), tuple(levels)))
        anchors.sort(key=lambda anchor: anchor.note)
        if [anchor.note for anchor in anchors] != sorted({anchor.note for anchor in anchors}):
            raise RuntimeError("whale morph anchor notes must be unique")
        if anchors[0].note != 21 or anchors[-1].note != 108:
            raise RuntimeError("whale morph anchors must cover both keyboard endpoints")
        expected_levels = tuple(level.maximum_harmonic for level in anchors[0].levels)
        if any(
            tuple(level.maximum_harmonic for level in anchor.levels) != expected_levels
            for anchor in anchors[1:]
        ):
            raise RuntimeError("whale morph anchors use inconsistent harmonic levels")
        self.anchors = tuple(anchors)
        self.harmonic_levels = expected_levels

    def status(self) -> dict[str, object]:
        return {
            "ready": True,
            "manifest": str(self.manifest_path),
            "manifest_sha256": sha256_path(self.manifest_path),
            "anchor_count": len(self.anchors),
            "table_size": self.table_size,
            "note_range": [21, 108],
            "tuning": "twelve-tone-equal-temperament-a4-440",
            "permanent_noise_layer": False,
            "sample_zones": 0,
        }

    @staticmethod
    def _table_sample(table: tuple[float, ...], phase: float) -> float:
        position = (phase % 1.0) * len(table)
        left = int(position) % len(table)
        right = (left + 1) % len(table)
        fraction = position - int(position)
        return table[left] + (table[right] - table[left]) * fraction

    def _level_sample(self, anchor: MorphAnchor, phase: float, frequency_hz: float) -> float:
        # The richer table in a transition must already be below the 0.45 *
        # sample-rate guard. Blending an unsafe upper table would reintroduce
        # exactly the aliased harmonics the mip levels are meant to remove.
        desired = max(1.0, self.sample_rate * 0.45 / max(frequency_hz, 1.0))
        ascending = tuple(reversed(anchor.levels))
        current_index = 0
        for index, level in enumerate(ascending):
            if level.maximum_harmonic <= desired:
                current_index = index
            else:
                break
        current = ascending[current_index]
        if current_index == 0:
            return self._table_sample(current.table, phase)
        lower = ascending[current_index - 1]
        next_threshold = (
            ascending[current_index + 1].maximum_harmonic
            if current_index + 1 < len(ascending)
            else current.maximum_harmonic * 2
        )
        span = math.log2(next_threshold / current.maximum_harmonic)
        amount = (
            math.log2(desired / current.maximum_harmonic) / span if span > 0.0 else 1.0
        )
        low_sample = self._table_sample(lower.table, phase)
        safe_sample = self._table_sample(current.table, phase)
        return low_sample + (safe_sample - low_sample) * clamp(amount, 0.0, 1.0)

    def sample(self, phase: float, timbre_note: float, frequency_hz: float) -> float:
        note = clamp(timbre_note, float(self.anchors[0].note), float(self.anchors[-1].note))
        if note <= self.anchors[0].note:
            return self._level_sample(self.anchors[0], phase, frequency_hz)
        if note >= self.anchors[-1].note:
            return self._level_sample(self.anchors[-1], phase, frequency_hz)
        for left, right in zip(self.anchors, self.anchors[1:]):
            if left.note <= note <= right.note:
                amount = (note - left.note) / (right.note - left.note)
                left_sample = self._level_sample(left, phase, frequency_hz)
                right_sample = self._level_sample(right, phase, frequency_hz)
                # Equal-power morph avoids a level hole when unrelated source cycles cancel.
                return (
                    left_sample * math.cos(amount * math.pi / 2.0)
                    + right_sample * math.sin(amount * math.pi / 2.0)
                )
        raise AssertionError("timbre anchor selection is incomplete")


def morph_bank_status(manifest_path: pathlib.Path = DEFAULT_MANIFEST) -> dict[str, object]:
    try:
        return WhaleMorphBank(manifest_path).status()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        return {
            "ready": False,
            "manifest": str(manifest_path),
            "blocking_reason": str(error),
        }


class WhaleMorphVoice:
    """One continuous source-derived voice across all 88 piano keys."""

    def __init__(
        self,
        config: WhaleVoiceConfig | None = None,
        *,
        bank: WhaleMorphBank | None = None,
    ) -> None:
        self.config = config or WhaleVoiceConfig()
        if self.config.sample_rate != 48_000:
            raise ValueError("continuous whale voice currently requires 48000 Hz")
        self.bank = bank or WhaleMorphBank()
        self.held_notes: dict[int, tuple[int, int]] = {}
        self._order = 0
        self.active_note: int | None = None
        self.gate = False
        self.sustain = 0
        self.modulation = 0.0
        self.expression = 1.0
        self.distance = 0.0
        self.pitch_bend_cents = 0.0
        self.envelope = 0.0
        self.velocity = 0.5
        self.target_velocity = 0.5
        self.current_frequency = midi_note_frequency(60)
        self.target_frequency = self.current_frequency
        self.glide_seconds = 0.12
        self.attack_seconds = 0.055
        self.release_seconds = 0.8
        self.phase = 0.0
        self.motion_phase = 0.31
        self.second_motion_phase = 1.7
        self.vibrato_phase = 0.0
        self.note_age_frames = 0
        self.hold_frames = 0
        self.retrigger_strength = 0.0
        self.depth_state = 0.0

    @property
    def active(self) -> bool:
        return self.gate or self.envelope > SILENCE_THRESHOLD

    @property
    def silent(self) -> bool:
        return not self.gate and self.envelope == 0.0

    def dispatch(self, event: MidiEvent) -> None:
        if event.kind == "note_on":
            self.note_on(event.note, event.velocity)
        elif event.kind == "note_off":
            self.note_off(event.note)
        elif event.kind == "control_change":
            self.control_change(event.controller, event.value)
        elif event.kind == "pitch_bend":
            self.pitch_bend(event.value)

    def _target_for_note(self, note: int) -> float:
        return midi_note_frequency(note) * 2.0 ** (self.pitch_bend_cents / 1200.0)

    def note_on(self, note: int, velocity: int) -> None:
        note = int(clamp(note, 21, 108))
        velocity = int(clamp(velocity, 1, 127))
        detached = not self.gate and not self.held_notes
        repeated = self.gate and self.active_note == note
        old_frequency = max(self.current_frequency, 1.0)
        self._order += 1
        self.held_notes[note] = (velocity, self._order)
        self.active_note = note
        self.target_frequency = self._target_for_note(note)
        self.target_velocity = velocity / 127.0
        interval_octaves = abs(math.log2(self.target_frequency / old_frequency))
        self.glide_seconds = clamp(
            0.035 + interval_octaves * (0.12 - 0.05 * self.target_velocity),
            0.035,
            0.36,
        )
        self.attack_seconds = clamp(0.085 - 0.065 * self.target_velocity, 0.018, 0.085)
        self.gate = True
        if detached:
            self.current_frequency = self.target_frequency
            self.velocity = self.target_velocity
            self.envelope = 0.0
            self.phase = 0.0
            self.note_age_frames = 0
            self.hold_frames = 0
            self.retrigger_strength = 0.0
        elif repeated:
            self.envelope *= 0.76
            self.note_age_frames = 0
            self.retrigger_strength = 1.0

    def note_off(self, note: int) -> None:
        self.held_notes.pop(note, None)
        if self.held_notes:
            next_note, (velocity, _order) = max(
                self.held_notes.items(), key=lambda item: item[1][1]
            )
            self.active_note = next_note
            self.target_frequency = self._target_for_note(next_note)
            self.target_velocity = velocity / 127.0
            self.glide_seconds = 0.11
            self.gate = True
            return
        if self.sustain >= 64:
            return
        self._begin_release()

    def _begin_release(self, pedal_value: int | None = None) -> None:
        self.gate = False
        held_seconds = self.hold_frames / self.config.sample_rate
        effective_pedal = self.sustain if pedal_value is None else pedal_value
        pedal_tail = 1.25 * (clamp(effective_pedal, 0, 127) / 127.0)
        self.release_seconds = clamp(0.32 + 0.11 * held_seconds + pedal_tail, 0.32, 3.6)

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
        self.pitch_bend_cents = 200.0 * bounded / 8192.0
        if self.active_note is not None:
            self.target_frequency = self._target_for_note(self.active_note)

    def _silence_immediately(self) -> None:
        self.held_notes.clear()
        self.active_note = None
        self.gate = False
        self.sustain = 0
        self.envelope = 0.0
        self.note_age_frames = 0
        self.hold_frames = 0
        self.retrigger_strength = 0.0
        self.depth_state = 0.0

    def render(self, frames: int) -> list[float]:
        if frames < 0 or frames > self.config.sample_rate * 30:
            raise ValueError("render frame count is outside the bounded range")
        if frames == 0:
            return []
        if self.silent:
            return [0.0] * frames

        sample_rate = self.config.sample_rate
        envelope = self.envelope
        velocity = self.velocity
        current_frequency = self.current_frequency
        phase = self.phase
        motion_phase = self.motion_phase
        second_motion_phase = self.second_motion_phase
        vibrato_phase = self.vibrato_phase
        note_age_frames = self.note_age_frames
        hold_frames = self.hold_frames
        retrigger_strength = self.retrigger_strength
        depth_state = self.depth_state
        glide_alpha = 1.0 - math.exp(-1.0 / (sample_rate * max(self.glide_seconds, 0.001)))
        attack_alpha = 1.0 - math.exp(-1.0 / (sample_rate * max(self.attack_seconds, 0.001)))
        release_alpha = 1.0 - math.exp(
            -6.907755278982137 / (sample_rate * max(self.release_seconds, 0.001))
        )
        velocity_alpha = 1.0 - math.exp(-1.0 / (sample_rate * 0.04))
        retrigger_alpha = 1.0 - math.exp(-1.0 / (sample_rate * 0.14))
        depth_alpha = 1.0 - math.exp(-1.0 / (sample_rate * 0.025))
        output: list[float] = []

        for index in range(frames):
            current_frequency += (self.target_frequency - current_frequency) * glide_alpha
            velocity += (self.target_velocity - velocity) * velocity_alpha
            if self.gate:
                envelope += (1.0 - envelope) * attack_alpha
                hold_frames += 1
            else:
                envelope += (0.0 - envelope) * release_alpha
                if envelope < SILENCE_THRESHOLD:
                    envelope = 0.0
                    output.extend([0.0] * (frames - index))
                    break

            age_seconds = note_age_frames / sample_rate
            hold_development = clamp((age_seconds - 0.35) / 2.2, 0.0, 1.0)
            long_development = clamp((age_seconds - 2.0) / 4.0, 0.0, 1.0)
            motion_phase = (motion_phase + 2.0 * math.pi * 0.17 / sample_rate) % (2.0 * math.pi)
            second_motion_phase = (
                second_motion_phase + 2.0 * math.pi * 0.061 / sample_rate
            ) % (2.0 * math.pi)
            vibrato_phase = (
                vibrato_phase + 2.0 * math.pi * (2.2 + 0.55 * velocity) / sample_rate
            ) % (2.0 * math.pi)
            slow_arc = math.sin(motion_phase)
            second_arc = math.sin(second_motion_phase)
            vibrato_depth = (1.2 + 5.8 * hold_development + 8.0 * self.modulation) * (
                0.65 + 0.35 * long_development
            )
            onset_cents = (
                -(12.0 + 30.0 * (1.0 - velocity))
                * math.exp(-age_seconds / 0.19)
            )
            contour_cents = (
                onset_cents
                + vibrato_depth * math.sin(vibrato_phase)
                + hold_development * (4.5 * slow_arc + 2.0 * second_arc)
            )
            frequency = current_frequency * 2.0 ** (contour_cents / 1200.0)
            phase = (phase + frequency / sample_rate) % 1.0

            base_note = frequency_to_midi_note(max(current_frequency, 1.0))
            timbre_motion = hold_development * (
                2.4 * slow_arc + 1.1 * long_development * second_arc
            )
            velocity_brightness = (velocity - 0.5) * 4.0
            timbre_note = base_note + timbre_motion + velocity_brightness
            voice_sample = self.bank.sample(phase, timbre_note, frequency)

            retrigger_strength += (0.0 - retrigger_strength) * retrigger_alpha
            articulation_gain = 1.0 + 0.14 * retrigger_strength
            phrase_gain = 0.88 + 0.08 * slow_arc * hold_development + 0.04 * second_arc
            velocity_gain = 0.22 + 0.78 * velocity**1.3
            target_depth = self.distance
            depth_state += (target_depth - depth_state) * depth_alpha
            distance_gain = 1.0 - 0.48 * depth_state
            raw = (
                voice_sample
                * envelope
                * articulation_gain
                * phrase_gain
                * velocity_gain
                * self.expression
                * distance_gain
                * self.config.master_gain
            )
            sample = raw / (1.0 + 1.05 * abs(raw))
            output.append(clamp(sample, -MAX_MASTER_GAIN, MAX_MASTER_GAIN))
            note_age_frames += 1

        self.envelope = envelope
        self.velocity = velocity
        self.current_frequency = current_frequency
        self.phase = phase
        self.motion_phase = motion_phase
        self.second_motion_phase = second_motion_phase
        self.vibrato_phase = vibrato_phase
        self.note_age_frames = note_age_frames
        self.hold_frames = hold_frames
        self.retrigger_strength = retrigger_strength
        self.depth_state = depth_state
        return output

    def render_f32_stereo(self, frames: int) -> bytes:
        if frames < 0 or frames > self.config.sample_rate * 30:
            raise ValueError("render frame count is outside the bounded range")
        if self.silent:
            return bytes(frames * 2 * 4)
        mono = self.render(frames)
        interleaved = array.array("f")
        for sample in mono:
            interleaved.append(sample * 0.987)
            interleaved.append(sample)
        if struct.pack("=I", 1) != struct.pack("<I", 1):
            interleaved.byteswap()
        return interleaved.tobytes()
