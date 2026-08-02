#!/usr/bin/env python3
"""Organic playable layer over the temporal source-filter whale foundation.

The source-filter foundation supplies source-derived formant, periodicity,
pulsation, nonlinear, subharmonic, and secondary-frequency trajectories. This
layer deliberately adds only the musical contracts that must remain stable:
small anti-theremin pitch drift, a register-aware deep-bass body, lightweight
state emphasis, deterministic gestures, and exact silence when inactive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from whale_live_engine import WhaleVoiceConfig, clamp
from whale_morph_engine import MAX_MASTER_GAIN, WhaleMorphBank, frequency_to_midi_note
from whale_source_filter_engine import (
    SOURCE_FILTER_COMPONENT_NAMES,
    SourceFilterComponentConfig,
    WhaleSourceFilterBank,
    WhaleSourceFilterVoice,
)

STATE_TONAL = 0
STATE_PULSED = 1
STATE_ROUGH = 2
STATE_BROKEN = 3
STATE_COUNT = 4
STATE_ONSET_SECONDS = 0.42
STATE_CROSSFADE_SECONDS = 0.14
ORGANIC_COMPONENT_NAMES = (
    *SOURCE_FILTER_COMPONENT_NAMES,
    "register_bass",
    "articulation_states",
    "pitch_contour",
)


@dataclass(frozen=True)
class OrganicComponentConfig:
    """Complete immutable ten-component Organic study configuration."""

    source_filter: SourceFilterComponentConfig = field(
        default_factory=SourceFilterComponentConfig
    )
    register_bass: bool = True
    articulation_states: bool = True
    pitch_contour: bool = True

    @classmethod
    def morph_neutral(cls) -> "OrganicComponentConfig":
        return cls(
            source_filter=SourceFilterComponentConfig.morph_neutral(),
            register_bass=False,
            articulation_states=False,
            pitch_contour=False,
        )

    @classmethod
    def from_enabled(cls, enabled: frozenset[str]) -> "OrganicComponentConfig":
        unknown = enabled - set(ORGANIC_COMPONENT_NAMES)
        if unknown:
            raise ValueError(f"unknown Organic components: {sorted(unknown)}")
        source_enabled = frozenset(enabled & set(SOURCE_FILTER_COMPONENT_NAMES))
        return cls(
            source_filter=SourceFilterComponentConfig.from_enabled(source_enabled),
            register_bass="register_bass" in enabled,
            articulation_states="articulation_states" in enabled,
            pitch_contour="pitch_contour" in enabled,
        )

    def enabled_names(self) -> tuple[str, ...]:
        enabled = list(self.source_filter.enabled_names())
        for name in ("register_bass", "articulation_states", "pitch_contour"):
            if getattr(self, name):
                enabled.append(name)
        return tuple(enabled)


STATE_PATTERNS = (
    (
        STATE_TONAL,
        STATE_PULSED,
        STATE_ROUGH,
        STATE_TONAL,
        STATE_TONAL,
        STATE_PULSED,
        STATE_BROKEN,
        STATE_TONAL,
    ),
    (
        STATE_TONAL,
        STATE_TONAL,
        STATE_ROUGH,
        STATE_PULSED,
        STATE_TONAL,
        STATE_BROKEN,
        STATE_TONAL,
        STATE_PULSED,
    ),
    (
        STATE_TONAL,
        STATE_PULSED,
        STATE_TONAL,
        STATE_ROUGH,
        STATE_TONAL,
        STATE_TONAL,
        STATE_BROKEN,
        STATE_PULSED,
    ),
    (
        STATE_TONAL,
        STATE_ROUGH,
        STATE_TONAL,
        STATE_PULSED,
        STATE_TONAL,
        STATE_BROKEN,
        STATE_TONAL,
        STATE_PULSED,
    ),
)


class OrganicWhaleMorphVoice(WhaleSourceFilterVoice):
    """Source-derived temporal voice with bounded musical augmentation."""

    def __init__(
        self,
        config: WhaleVoiceConfig | None = None,
        *,
        bank: WhaleMorphBank | None = None,
        source_filter_bank: WhaleSourceFilterBank | None = None,
        component_config: OrganicComponentConfig | None = None,
    ) -> None:
        self.organic_components = component_config or OrganicComponentConfig()
        super().__init__(
            config,
            source_filter_bank=source_filter_bank,
            morph_bank=bank,
            component_config=self.organic_components.source_filter,
        )
        self.organic_phrase_serial = 0
        self.organic_state_seed = 0
        self.organic_state_segment_seconds = 1.20
        self.organic_state_phase_offset = 0.0
        self.organic_bass_lowpass = 0.0
        self.organic_bass_phase = 0.619
        self.organic_activity = 0.0
        self.organic_pulse_strength = 0.0
        self.organic_nominal_target_frequency = self.target_frequency
        self.organic_control_remaining = 0
        self.organic_control_position = 0
        self.organic_control_age_start = 0
        self.organic_control_frequency = self.current_frequency
        self.organic_control_velocity = self.velocity

    def _restore_nominal_target(self) -> None:
        self.target_frequency = self.organic_nominal_target_frequency
        self.organic_control_remaining = 0

    def _capture_nominal_target(self) -> None:
        self.organic_nominal_target_frequency = self.target_frequency
        self.organic_control_remaining = 0

    def note_on(self, note: int, velocity: int) -> None:
        detached = not self.gate and not self.held_notes
        repeated = self.gate and self.active_note == note
        previous_note = self.active_note
        self._restore_nominal_target()
        super().note_on(note, velocity)
        if not 21 <= note <= 108:
            return
        self._capture_nominal_target()
        bounded_velocity = int(clamp(velocity, 1, 127))
        if detached:
            self.organic_phrase_serial += 1
            seed = (
                note * 0x45D9F3B
                + bounded_velocity * 0x119DE1F3
                + self.organic_phrase_serial * 0x27D4EB2D
            ) & 0xFFFFFFFF
            self.organic_state_seed = seed
            self.organic_state_segment_seconds = 0.70 + 0.26 * (
                ((seed >> 5) & 0xFFFF) / 65535.0
            )
            self.organic_state_phase_offset = (
                ((seed >> 13) & 0xFFFF) / 65536.0
            )
            self.organic_bass_phase = ((seed >> 16) & 0xFFFF) / 65536.0
            self.organic_pulse_strength = (
                max(0.06, (bounded_velocity / 127.0 - 0.58) * 0.34)
                if self.organic_components.articulation_states
                else 0.0
            )
        elif repeated and self.organic_components.articulation_states:
            self.organic_pulse_strength = 0.72
        elif previous_note is not None:
            interval = abs(note - previous_note)
            if self.organic_components.articulation_states:
                self.organic_pulse_strength = max(
                    self.organic_pulse_strength,
                    clamp(interval / 36.0, 0.04, 0.24),
                )
            if self.organic_components.pitch_contour:
                self.glide_seconds = clamp(self.glide_seconds * 0.72, 0.035, 0.18)

    def note_off(self, note: int) -> None:
        if 21 <= note <= 108:
            self._restore_nominal_target()
        super().note_off(note)
        if 21 <= note <= 108:
            self._capture_nominal_target()

    def control_change(self, controller: int, value: int) -> None:
        relevant = controller in {1, 11, 64, 67, 120, 123}
        if relevant:
            self._restore_nominal_target()
        super().control_change(controller, value)
        if relevant:
            self.organic_control_remaining = 0

    def pitch_bend(self, value: int) -> None:
        self._restore_nominal_target()
        super().pitch_bend(value)
        self._capture_nominal_target()

    def _silence_immediately(self) -> None:
        super()._silence_immediately()
        self.organic_bass_lowpass = 0.0
        self.organic_activity = 0.0
        self.organic_pulse_strength = 0.0
        self.organic_nominal_target_frequency = self.target_frequency
        self.organic_control_remaining = 0

    def _start_control_segment(self) -> None:
        self.target_frequency = self.organic_nominal_target_frequency
        contour = self._macro_contour_cents()
        self.target_frequency = self.organic_nominal_target_frequency * 2.0 ** (
            contour / 1200.0
        )
        self.organic_control_remaining = self.config.block_frames
        self.organic_control_position = 0
        self.organic_control_age_start = self.note_age_frames
        self.organic_control_frequency = max(self.current_frequency, 1.0)
        self.organic_control_velocity = clamp(self.velocity, 0.0, 1.0)

    def _macro_contour_cents(self) -> float:
        if not self.organic_components.pitch_contour:
            return 0.0
        age_seconds = self.note_age_frames / self.config.sample_rate
        velocity = clamp(self.velocity, 0.0, 1.0)
        onset = -(16.0 - 7.0 * velocity) * math.exp(-age_seconds / 0.16)
        developed = clamp((age_seconds - 0.45) / 2.4, 0.0, 1.0)
        drift = (
            (3.0 + 4.0 * self.modulation)
            * developed
            * math.sin(
                2.0
                * math.pi
                * (0.071 * age_seconds + 0.11 * self.organic_phrase_serial)
            )
        )
        pulse = 3.5 * self.organic_pulse_strength
        return onset + drift + pulse

    @staticmethod
    def _mix32(value: int) -> int:
        value &= 0xFFFFFFFF
        value ^= value >> 16
        value = (value * 0x7FEB352D) & 0xFFFFFFFF
        value ^= value >> 15
        value = (value * 0x846CA68B) & 0xFFFFFFFF
        value ^= value >> 16
        return value & 0xFFFFFFFF

    def _state_code(self, segment_index: int) -> int:
        if segment_index < 0:
            return STATE_TONAL
        pattern_index = self._mix32(
            self.organic_state_seed + self.organic_phrase_serial * 0x85EBCA6B
        ) % len(STATE_PATTERNS)
        pattern = STATE_PATTERNS[pattern_index]
        return pattern[segment_index % len(pattern)]

    def _state_weights(self, age_frames: int) -> tuple[float, float, float, float]:
        if not self.organic_components.articulation_states:
            return (1.0, 0.0, 0.0, 0.0)
        age_seconds = max(0, age_frames) / self.config.sample_rate
        if age_seconds < STATE_ONSET_SECONDS:
            return (1.0, 0.0, 0.0, 0.0)
        relative = (age_seconds - STATE_ONSET_SECONDS) / self.organic_state_segment_seconds
        segment_index = max(0, int(relative))
        phase = relative - segment_index
        previous = (
            STATE_TONAL
            if segment_index == 0
            else self._state_code(segment_index - 1)
        )
        current = self._state_code(segment_index)
        transition_fraction = min(
            0.49,
            STATE_CROSSFADE_SECONDS / self.organic_state_segment_seconds,
        )
        amount = clamp(phase / max(transition_fraction, 1.0e-6), 0.0, 1.0)
        amount = amount * amount * (3.0 - 2.0 * amount)
        weights = [0.0] * STATE_COUNT
        weights[previous] += 1.0 - amount
        weights[current] += amount
        return tuple(weights)  # type: ignore[return-value]

    def _process_block(self, base: list[float]) -> list[float]:
        register_bass = self.organic_components.register_bass
        articulation_states = self.organic_components.articulation_states
        if not (register_bass or articulation_states):
            return list(base)

        sample_rate = self.config.sample_rate
        master_gain = self.config.master_gain
        frequency = max(self.organic_control_frequency, 1.0)
        velocity = self.organic_control_velocity
        note = frequency_to_midi_note(frequency)
        bass_weight = clamp((55.0 - note) / 24.0, 0.0, 1.0)
        bass_weight = bass_weight * bass_weight * (3.0 - 2.0 * bass_weight)
        if not register_bass:
            bass_weight = 0.0
        bass_cutoff = clamp(frequency * 3.2, 75.0, 560.0)
        bass_alpha = 1.0 - math.exp(-2.0 * math.pi * bass_cutoff / sample_rate)
        activity_attack = 1.0 - math.exp(-1.0 / (sample_rate * 0.008))
        activity_release = 1.0 - math.exp(-1.0 / (sample_rate * 0.050))
        pulse_decay = 1.0 - math.exp(-1.0 / (sample_rate * 0.11))
        _tonal, pulsed, rough, broken = self._state_weights(
            self.organic_control_age_start + len(base) // 2
        )
        state_texture = rough + 0.55 * broken
        state_pulse_rate = 2.0 + 1.2 * (
            ((self.organic_state_seed >> 19) & 0xFF) / 255.0
        )
        two_pi = 2.0 * math.pi
        velocity_gain = 0.22 + 0.78 * velocity**1.3
        # Bind invariant lookups and mutable state locally without regrouping
        # the per-sample arithmetic; the rendered doubles remain bit-identical.
        source_filter_control = self.source_filter_control
        source_roughness = clamp(source_filter_control.roughness, 0.0, 1.0)
        source_high_band = clamp(source_filter_control.high_band_ratio, 0.0, 1.0)
        state_edge_drive = 22.0 + 52.0 * source_roughness
        activity_denominator = max(master_gain * 0.08, 1.0e-6)
        expression = self.expression
        organic_control_age_start = self.organic_control_age_start
        organic_control_position = self.organic_control_position
        organic_bass_lowpass = self.organic_bass_lowpass
        organic_bass_phase = self.organic_bass_phase
        organic_activity = self.organic_activity
        organic_pulse_strength = self.organic_pulse_strength
        organic_state_phase_offset = self.organic_state_phase_offset
        bass_phase_increment = frequency / sample_rate
        output: list[float] = []
        append = output.append
        clamp_value = clamp
        sin = math.sin
        tanh = math.tanh

        for source_sample in base:
            organic_bass_lowpass += (
                source_sample - organic_bass_lowpass
            ) * bass_alpha
            activity_target = clamp_value(
                abs(source_sample) / activity_denominator,
                0.0,
                1.0,
            )
            activity_alpha = (
                activity_attack
                if activity_target > organic_activity
                else activity_release
            )
            organic_activity += (
                activity_target - organic_activity
            ) * activity_alpha
            age_frames = organic_control_age_start + organic_control_position
            organic_control_position += 1
            organic_bass_phase = (
                organic_bass_phase + bass_phase_increment
            ) % 1.0
            bass_oscillator = sin(two_pi * organic_bass_phase)
            bass_oscillator += 0.23 * sin(
                2.0 * two_pi * organic_bass_phase
            )
            bass_body = (
                0.47 * organic_bass_lowpass
                + 0.55
                * bass_oscillator
                * organic_activity
                * velocity_gain
                * expression
                * master_gain
            ) * bass_weight * (1.05 + 0.45 * (1.0 - velocity))
            organic_pulse_strength += (
                0.0 - organic_pulse_strength
            ) * pulse_decay
            state_seconds = age_frames / sample_rate
            pulse_phase = (
                state_pulse_rate * state_seconds + organic_state_phase_offset
            ) % 1.0
            pulse_wave = 1.0 - abs(2.0 * pulse_phase - 1.0)
            pulse_wave = pulse_wave * pulse_wave * (3.0 - 2.0 * pulse_wave)
            pulse_gain = (
                1.0
                + 0.10 * organic_pulse_strength
                - pulsed * 0.055 * (1.0 - pulse_wave)
                - broken * 0.035 * (1.0 - pulse_wave)
                if articulation_states
                else 1.0
            )
            edge = source_sample - organic_bass_lowpass
            state_edge = (
                tanh(edge * state_edge_drive)
                * state_texture
                * source_roughness
                * (0.041 + 0.072 * source_high_band)
                if articulation_states
                else 0.0
            )
            raw = source_sample * pulse_gain + bass_body + state_edge
            sample = raw / (1.0 + 1.20 * abs(raw))
            append(clamp_value(sample, -MAX_MASTER_GAIN, MAX_MASTER_GAIN))

        self.organic_bass_lowpass = organic_bass_lowpass
        self.organic_bass_phase = organic_bass_phase
        self.organic_activity = organic_activity
        self.organic_pulse_strength = organic_pulse_strength
        self.organic_control_position = organic_control_position
        if super().silent:
            self.organic_bass_lowpass = 0.0
            self.organic_activity = 0.0
        return output

    def render(self, frames: int) -> list[float]:
        if frames < 0 or frames > self.config.sample_rate * 30:
            raise ValueError("render frame count is outside the bounded range")
        if frames == 0:
            return []
        if self.silent:
            self._silence_immediately()
            return [0.0] * frames
        output: list[float] = []
        remaining = frames
        while remaining:
            if self.organic_control_remaining <= 0:
                self._start_control_segment()
            block = min(remaining, self.organic_control_remaining)
            base = super().render(block)
            output.extend(self._process_block(base))
            self.organic_control_remaining -= block
            remaining -= block
            if self.silent and remaining:
                output.extend([0.0] * remaining)
                self._silence_immediately()
                break
        return output

    def render_f32_stereo(self, frames: int) -> bytes:
        return super().render_f32_stereo(frames)
