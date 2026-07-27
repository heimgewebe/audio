#!/usr/bin/env python3
"""Dependency-free gesture and synthesis engine for Buckelwal Live Voice v1.

The first product slice deliberately synthesizes a whale-like monophonic voice
without distributing third-party recordings.  Its public boundary is designed
so a later sample/resynthesis backend can replace the oscillator bank while the
Roland gesture semantics remain stable.
"""

from __future__ import annotations

import math
import pathlib
import re
import struct
import wave
from array import array
from dataclasses import dataclass
from typing import Iterable

MIN_MIDI_NOTE = 21
MAX_MIDI_NOTE = 108
MAX_MASTER_GAIN = 0.25
DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_BLOCK_FRAMES = 128
MAX_OFFLINE_DURATION_SECONDS = 30.0

_NOTE_RE = re.compile(
    r"\bNote\s+(on|off)\s+(\d+)\s*,\s*note\s+(\d+)\s*,\s*velocity\s+(\d+)",
    re.IGNORECASE,
)
_CONTROL_RE = re.compile(
    r"\bControl\s+change\s+(\d+)\s*,\s*controller\s+(\d+)\s*,\s*value\s+(\d+)",
    re.IGNORECASE,
)
_PITCH_RE = re.compile(
    r"\bPitch\s+bend\s+(\d+)\s*,\s*value\s+(-?\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MidiEvent:
    kind: str
    channel: int = 0
    note: int = 0
    velocity: int = 0
    controller: int = 0
    value: int = 0


@dataclass(frozen=True)
class WhaleVoiceConfig:
    sample_rate: int = DEFAULT_SAMPLE_RATE
    block_frames: int = DEFAULT_BLOCK_FRAMES
    master_gain: float = 0.16
    min_frequency_hz: float = 34.0
    max_frequency_hz: float = 1_850.0
    min_note: int = MIN_MIDI_NOTE
    max_note: int = MAX_MIDI_NOTE

    def __post_init__(self) -> None:
        if not 8_000 <= self.sample_rate <= 192_000:
            raise ValueError("sample_rate must be between 8000 and 192000 Hz")
        if not 16 <= self.block_frames <= 4_096:
            raise ValueError("block_frames must be between 16 and 4096")
        if (
            not math.isfinite(self.master_gain)
            or not 0 < self.master_gain <= MAX_MASTER_GAIN
        ):
            raise ValueError(
                f"master_gain must be positive and at most {MAX_MASTER_GAIN}"
            )
        if (
            not 1
            <= self.min_frequency_hz
            < self.max_frequency_hz
            < self.sample_rate / 2
        ):
            raise ValueError(
                "frequency range must be positive, ordered and below Nyquist"
            )
        if not 0 <= self.min_note < self.max_note <= 127:
            raise ValueError("MIDI note range is invalid")


def clamp(value: float, lower: float, upper: float) -> float:
    return lower if value < lower else upper if value > upper else value


def parse_aseqdump_line(line: str) -> MidiEvent | None:
    """Parse the stable human-readable event forms emitted by alsa-utils aseqdump."""

    match = _NOTE_RE.search(line)
    if match:
        action, channel, note, velocity = match.groups()
        note_value = int(note)
        velocity_value = int(velocity)
        if action.lower() == "off" or velocity_value == 0:
            return MidiEvent("note_off", int(channel), note_value, velocity_value)
        return MidiEvent("note_on", int(channel), note_value, velocity_value)

    match = _CONTROL_RE.search(line)
    if match:
        channel, controller, value = (int(part) for part in match.groups())
        return MidiEvent(
            "control_change",
            channel=channel,
            controller=controller,
            value=value,
        )

    match = _PITCH_RE.search(line)
    if match:
        channel, value = (int(part) for part in match.groups())
        return MidiEvent("pitch_bend", channel=channel, value=value)
    return None


def note_to_whale_hz(note: int, config: WhaleVoiceConfig | None = None) -> float:
    """Map all 88 piano keys monotonically into a broad humpback-vocal range."""

    cfg = config or WhaleVoiceConfig()
    bounded = int(clamp(note, cfg.min_note, cfg.max_note))
    position = (bounded - cfg.min_note) / (cfg.max_note - cfg.min_note)
    # Slightly expand the low half where groans and body resonances need room.
    warped = position**1.08
    ratio = cfg.max_frequency_hz / cfg.min_frequency_hz
    return cfg.min_frequency_hz * ratio**warped


def register_position(note: int, config: WhaleVoiceConfig | None = None) -> float:
    cfg = config or WhaleVoiceConfig()
    return clamp((note - cfg.min_note) / (cfg.max_note - cfg.min_note), 0.0, 1.0)


class WhaleVoice:
    """One phase-continuous, last-note-priority whale voice.

    Held notes are remembered in order.  The newest held note is the melodic
    target; earlier held notes can be released without interrupting the current
    glide.  Sustain CC64 defers the final release.  The engine never creates a
    piano chord, which keeps the result interpretable as one animal.
    """

    def __init__(
        self, config: WhaleVoiceConfig | None = None, *, seed: int = 0xB0A7
    ) -> None:
        self.config = config or WhaleVoiceConfig()
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
        self.current_frequency = note_to_whale_hz(48, self.config)
        self.target_frequency = self.current_frequency
        self.current_register = register_position(48, self.config)
        self.target_register = self.current_register
        self.glide_seconds = 0.18
        self.attack_seconds = 0.06
        self.release_seconds = 0.8
        self.note_age_frames = 0
        self.hold_frames = 0
        self.retrigger_fade_total = max(1, round(self.config.sample_rate * 0.006))
        self.retrigger_fade_remaining = 0

        self.phase = 0.0
        self.sub_phase = 0.0
        self.formant_phase = 0.0
        self.carrier_mod_phase = 0.0
        self.pulse_phase = 0.0
        self.detuned_phase = 0.0
        self.slow_arc_phase = 0.7
        self.second_arc_phase = 2.1
        self.flutter_phase = 0.0
        self.noise_state = seed & 0xFFFFFFFF
        self.noise_lowpass = 0.0

    @property
    def active(self) -> bool:
        return self.gate or self.envelope > 1e-5

    def dispatch(self, event: MidiEvent) -> None:
        if event.kind == "note_on":
            self.note_on(event.note, event.velocity)
        elif event.kind == "note_off":
            self.note_off(event.note)
        elif event.kind == "control_change":
            self.control_change(event.controller, event.value)
        elif event.kind == "pitch_bend":
            self.pitch_bend(event.value)

    def note_on(self, note: int, velocity: int) -> None:
        note = int(clamp(note, self.config.min_note, self.config.max_note))
        velocity = int(clamp(velocity, 1, 127))
        phrase_continues = self.gate or bool(self.held_notes)
        old_frequency = max(self.current_frequency, 1.0)

        self._order += 1
        self.held_notes[note] = (velocity, self._order)
        self.active_note = note
        self.target_frequency = note_to_whale_hz(note, self.config)
        self.target_register = register_position(note, self.config)
        self.target_velocity = velocity / 127.0
        interval_octaves = abs(math.log2(self.target_frequency / old_frequency))
        # Big leaps become deliberate whale sweeps. Harder attacks move faster.
        self.glide_seconds = clamp(
            0.055 + interval_octaves * (0.20 - 0.08 * self.target_velocity),
            0.055,
            0.75,
        )
        self.attack_seconds = clamp(0.085 - 0.06 * self.target_velocity, 0.018, 0.085)
        self.gate = True
        if not phrase_continues:
            self.glide_seconds = 0.02
            if self.envelope > 1e-5:
                # Preserve the current sample, then fade the old phrase to zero before
                # resetting oscillator, register and envelope state for the new onset.
                self.retrigger_fade_remaining = self.retrigger_fade_total
            else:
                self._reset_phrase_to_target()

    def note_off(self, note: int) -> None:
        self.held_notes.pop(note, None)
        if self.held_notes:
            next_note, (velocity, _order) = max(
                self.held_notes.items(), key=lambda item: item[1][1]
            )
            self.active_note = next_note
            self.target_frequency = note_to_whale_hz(next_note, self.config)
            self.target_register = register_position(next_note, self.config)
            self.target_velocity = velocity / 127.0
            self.glide_seconds = 0.14
            self.gate = True
            return
        if self.sustain >= 64:
            return
        self._begin_release()

    def control_change(self, controller: int, value: int) -> None:
        value = int(clamp(value, 0, 127))
        if controller == 64:  # damper: phrase continuity
            previous_sustain = self.sustain
            was_down = previous_sustain >= 64
            self.sustain = value
            if was_down and value < 64 and not self.held_notes:
                self._begin_release(previous_sustain)
        elif controller == 1:  # optional modulation wheel from another controller
            self.modulation = value / 127.0
        elif controller == 11:  # expression
            self.expression = value / 127.0
        elif controller == 67:  # soft pedal: distance/depth
            self.distance = value / 127.0
        elif controller == 120:  # all sound off: MIDI panic must be immediate
            self._silence_immediately()
        elif controller == 123:  # all notes off: preserve the natural release
            self.held_notes.clear()
            self.sustain = 0
            self._begin_release()

    def pitch_bend(self, value: int) -> None:
        # aseqdump reports ALSA pitch bend around zero. Keep the range narrow.
        bounded = clamp(value, -8192, 8191)
        self.pitch_bend_cents = 180.0 * bounded / 8192.0

    def _silence_immediately(self) -> None:
        self.held_notes.clear()
        self.active_note = None
        self.gate = False
        self.sustain = 0
        self.envelope = 0.0
        self.note_age_frames = 0
        self.hold_frames = 0
        self.retrigger_fade_remaining = 0
        self.phase = 0.0
        self.sub_phase = 0.0
        self.formant_phase = 0.0
        self.carrier_mod_phase = 0.0
        self.pulse_phase = 0.0
        self.detuned_phase = 0.0
        self.slow_arc_phase = 0.7
        self.second_arc_phase = 2.1
        self.flutter_phase = 0.0
        self.noise_lowpass = 0.0

    def _reset_phrase_to_target(self) -> None:
        self.note_age_frames = 0
        self.hold_frames = 0
        self.envelope = 0.0
        self.velocity = self.target_velocity
        self.current_frequency = self.target_frequency
        self.current_register = self.target_register
        self.phase = 0.0
        self.sub_phase = 0.0
        self.formant_phase = 0.0
        self.carrier_mod_phase = 0.0
        self.pulse_phase = 0.0
        self.detuned_phase = 0.0
        self.slow_arc_phase = 0.7
        self.second_arc_phase = 2.1
        self.flutter_phase = 0.0
        self.retrigger_fade_remaining = 0

    def _begin_release(self, pedal_value: int | None = None) -> None:
        self.gate = False
        held_seconds = self.hold_frames / self.config.sample_rate
        effective_pedal = self.sustain if pedal_value is None else pedal_value
        pedal_tail = 1.4 * (clamp(effective_pedal, 0, 127) / 127.0)
        self.release_seconds = clamp(0.35 + held_seconds * 0.13 + pedal_tail, 0.35, 3.8)

    def render(self, frames: int) -> list[float]:
        if frames < 0 or frames > self.config.sample_rate * 30:
            raise ValueError("render frame count is outside the bounded range")
        if frames == 0:
            return []

        sample_rate = self.config.sample_rate
        two_pi = 2.0 * math.pi
        envelope = self.envelope
        velocity = self.velocity
        current_frequency = self.current_frequency
        current_register = self.current_register
        phase = self.phase
        sub_phase = self.sub_phase
        formant_phase = self.formant_phase
        carrier_mod_phase = self.carrier_mod_phase
        pulse_phase = self.pulse_phase
        detuned_phase = self.detuned_phase
        slow_arc_phase = self.slow_arc_phase
        second_arc_phase = self.second_arc_phase
        flutter_phase = self.flutter_phase
        noise_state = self.noise_state
        noise_lowpass = self.noise_lowpass
        note_age_frames = self.note_age_frames
        hold_frames = self.hold_frames
        retrigger_fade_remaining = self.retrigger_fade_remaining

        glide_alpha = 1.0 - math.exp(
            -1.0 / (sample_rate * max(self.glide_seconds, 0.001))
        )
        attack_alpha = 1.0 - math.exp(
            -1.0 / (sample_rate * max(self.attack_seconds, 0.001))
        )
        # Treat release_seconds as audible duration: about -60 dB at its end.
        release_alpha = 1.0 - math.exp(
            -6.907755278982137 / (sample_rate * max(self.release_seconds, 0.001))
        )
        velocity_alpha = 1.0 - math.exp(-1.0 / (sample_rate * 0.045))
        output: list[float] = []

        for _index in range(frames):
            fading_old_phrase = retrigger_fade_remaining > 0
            if not fading_old_phrase:
                current_frequency += (
                    self.target_frequency - current_frequency
                ) * glide_alpha
                current_register += (
                    self.target_register - current_register
                ) * glide_alpha
                velocity += (self.target_velocity - velocity) * velocity_alpha
            if not fading_old_phrase:
                if self.gate:
                    envelope += (1.0 - envelope) * attack_alpha
                else:
                    envelope += (0.0 - envelope) * release_alpha

            register = clamp(current_register, 0.0, 1.0)
            body_weight = 0.78 - 0.42 * register
            formant_weight = 0.18 + 0.38 * (1.0 - abs(register - 0.48) * 1.55)
            whistle_weight = 0.06 + 0.52 * register**1.45
            noise_cut = 0.018 + 0.11 * register

            # Integrated contour phases remain continuous while register glides.
            slow_arc = math.sin(slow_arc_phase)
            second_arc = math.sin(second_arc_phase)
            flutter = math.sin(flutter_phase)
            slow_arc_phase = (
                slow_arc_phase + two_pi * (0.071 + register * 0.023) / sample_rate
            ) % two_pi
            second_arc_phase = (
                second_arc_phase + two_pi * (0.193 - register * 0.041) / sample_rate
            ) % two_pi
            flutter_phase = (
                flutter_phase + two_pi * (1.7 + register * 2.8) / sample_rate
            ) % two_pi
            contour_cents = (
                (18.0 + 46.0 * register) * slow_arc
                + (7.0 + 13.0 * register) * second_arc
                + (1.5 + 7.0 * self.modulation) * flutter
                + self.pitch_bend_cents
            )
            frequency = current_frequency * (2.0 ** (contour_cents / 1200.0))
            phase_increment = two_pi * frequency / sample_rate
            phase = (phase + phase_increment) % two_pi
            sub_phase = (sub_phase + phase_increment * 0.502) % two_pi
            formant_increment = phase_increment * (1.92 + 0.28 * slow_arc)
            formant_phase = (formant_phase + formant_increment) % two_pi
            carrier_mod_phase = (carrier_mod_phase + formant_increment * 0.23) % two_pi
            pulse_phase = (
                pulse_phase + phase_increment * (2.87 + 0.07 * second_arc)
            ) % two_pi
            detuned_phase = (detuned_phase + phase_increment * 1.0065) % two_pi

            carrier = math.sin(phase + 0.24 * math.sin(carrier_mod_phase))
            sub = math.sin(sub_phase)
            formant = math.sin(formant_phase + 0.37 * slow_arc)
            whistle = math.sin(pulse_phase + 0.18 * flutter)
            detuned = math.sin(detuned_phase)

            noise_state = (1_664_525 * noise_state + 1_013_904_223) & 0xFFFFFFFF
            white = (noise_state / 2_147_483_648.0) - 1.0
            noise_lowpass += (white - noise_lowpass) * noise_cut

            roughness = (0.015 + 0.085 * velocity**1.6 + 0.08 * self.modulation) * (
                0.55 + 0.45 * register
            )
            tone = (
                body_weight * (0.72 * carrier + 0.28 * sub)
                + formant_weight * formant
                + whistle_weight * whistle
                + roughness * detuned
                + (0.018 + 0.035 * velocity) * noise_lowpass
            )
            phrase_breath = 0.82 + 0.12 * slow_arc + 0.045 * second_arc
            velocity_gain = 0.22 + 0.78 * velocity**1.35
            distance_gain = 1.0 - 0.48 * self.distance
            retrigger_gain = (
                retrigger_fade_remaining / self.retrigger_fade_total
                if fading_old_phrase
                else 1.0
            )
            raw = (
                tone
                * phrase_breath
                * velocity_gain
                * envelope
                * retrigger_gain
                * self.expression
                * distance_gain
                * self.config.master_gain
            )
            # Bounded soft clipping protects against unexpected partial alignment.
            sample = raw / (1.0 + 1.7 * abs(raw))
            output.append(clamp(sample, -MAX_MASTER_GAIN, MAX_MASTER_GAIN))

            if fading_old_phrase:
                retrigger_fade_remaining -= 1
                if retrigger_fade_remaining == 0:
                    envelope = 0.0
                    velocity = self.target_velocity
                    current_frequency = self.target_frequency
                    current_register = self.target_register
                    phase = 0.0
                    sub_phase = 0.0
                    formant_phase = 0.0
                    carrier_mod_phase = 0.0
                    pulse_phase = 0.0
                    detuned_phase = 0.0
                    slow_arc_phase = 0.7
                    second_arc_phase = 2.1
                    flutter_phase = 0.0
                    note_age_frames = 0
                    hold_frames = 0
                    continue
            note_age_frames += 1
            if self.gate:
                hold_frames += 1

        self.envelope = 0.0 if not self.gate and envelope < 1e-7 else envelope
        self.velocity = velocity
        self.current_frequency = current_frequency
        self.current_register = current_register
        self.phase = phase
        self.sub_phase = sub_phase
        self.formant_phase = formant_phase
        self.carrier_mod_phase = carrier_mod_phase
        self.pulse_phase = pulse_phase
        self.detuned_phase = detuned_phase
        self.slow_arc_phase = slow_arc_phase
        self.second_arc_phase = second_arc_phase
        self.flutter_phase = flutter_phase
        self.noise_state = noise_state
        self.noise_lowpass = noise_lowpass
        self.note_age_frames = note_age_frames
        self.hold_frames = hold_frames
        self.retrigger_fade_remaining = retrigger_fade_remaining
        return output

    def render_f32_stereo(self, frames: int) -> bytes:
        mono = self.render(frames)
        interleaved = array("f")
        for sample in mono:
            # Very small movement avoids a dead-centre point without fake surround.
            interleaved.append(sample * 0.985)
            interleaved.append(sample)
        if struct.pack("=I", 1) != struct.pack("<I", 1):
            interleaved.byteswap()
        return interleaved.tobytes()


def render_timeline(
    events: Iterable[tuple[float, MidiEvent]],
    duration_seconds: float,
    config: WhaleVoiceConfig | None = None,
) -> list[float]:
    cfg = config or WhaleVoiceConfig()
    if (
        not math.isfinite(duration_seconds)
        or not 0 < duration_seconds <= MAX_OFFLINE_DURATION_SECONDS
    ):
        raise ValueError(
            f"duration_seconds must be positive and at most {MAX_OFFLINE_DURATION_SECONDS:g}"
        )
    ordered = sorted(events, key=lambda item: item[0])
    if any(
        time_value < 0 or time_value > duration_seconds
        for time_value, _event in ordered
    ):
        raise ValueError("timeline event is outside the requested duration")

    voice = WhaleVoice(cfg)
    result: list[float] = []
    cursor = 0
    for time_value, event in ordered:
        target = round(time_value * cfg.sample_rate)
        if target > cursor:
            result.extend(voice.render(target - cursor))
            cursor = target
        voice.dispatch(event)
    total = round(duration_seconds * cfg.sample_rate)
    if cursor < total:
        result.extend(voice.render(total - cursor))
    return result


def default_demo_events() -> list[tuple[float, MidiEvent]]:
    """A short phrase that demonstrates hold, overlap, register and pedal."""

    return [
        (0.15, MidiEvent("note_on", note=42, velocity=42)),
        (1.80, MidiEvent("note_on", note=50, velocity=62)),
        (2.15, MidiEvent("note_off", note=42)),
        (3.60, MidiEvent("note_on", note=57, velocity=78)),
        (3.95, MidiEvent("note_off", note=50)),
        (5.10, MidiEvent("control_change", controller=64, value=96)),
        (5.20, MidiEvent("note_on", note=69, velocity=54)),
        (5.55, MidiEvent("note_off", note=57)),
        (6.95, MidiEvent("note_off", note=69)),
        (7.75, MidiEvent("note_on", note=76, velocity=88)),
        (8.25, MidiEvent("note_on", note=64, velocity=45)),
        (8.45, MidiEvent("note_off", note=76)),
        (9.50, MidiEvent("note_off", note=64)),
        (9.90, MidiEvent("control_change", controller=64, value=0)),
    ]


def signal_metrics(samples: list[float]) -> dict[str, float | int]:
    if not samples:
        return {"frames": 0, "peak": 0.0, "peak_dbfs": float("-inf"), "rms": 0.0}
    peak = max(abs(sample) for sample in samples)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    return {
        "frames": len(samples),
        "peak": peak,
        "peak_dbfs": 20.0 * math.log10(peak) if peak else float("-inf"),
        "rms": rms,
    }


def write_stereo_wav(
    path: pathlib.Path, samples: list[float], sample_rate: int
) -> None:
    if sample_rate <= 0 or sample_rate > 192_000:
        raise ValueError("invalid sample rate")
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for sample in samples:
        bounded = clamp(sample, -1.0, 1.0)
        value = round(bounded * 32767.0)
        frames.extend(struct.pack("<hh", value, value))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
