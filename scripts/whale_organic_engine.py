#!/usr/bin/env python3
"""Organic articulation layer for the continuous source-derived whale voice.

The layer keeps the exact 88-key tuning and source-derived morph bank while
adding only signal-coupled, deterministic behaviour: formant inertia,
register-aware bass body, irregular micro-instability, pulsed articulation, and
two short damped vocal modes. It has no permanent noise generator and is
exactly silent when no note or bounded modal tail is active.
"""

from __future__ import annotations

import math

from whale_live_engine import WhaleVoiceConfig, clamp
from whale_morph_engine import (
    MAX_MASTER_GAIN,
    WhaleMorphBank,
    WhaleMorphVoice,
    frequency_to_midi_note,
)

TAIL_THRESHOLD = 2.0e-7


class OrganicWhaleMorphVoice(WhaleMorphVoice):
    """Continuous morph voice with deterministic humpback-like nonlinearities."""

    def __init__(
        self,
        config: WhaleVoiceConfig | None = None,
        *,
        bank: WhaleMorphBank | None = None,
    ) -> None:
        super().__init__(config, bank=bank)
        self.organic_phrase_serial = 0
        self.organic_chaos = 0.371
        self.organic_chaos_target = 0.0
        self.organic_chaos_smooth = 0.0
        self.organic_chaos_counter = 0
        self.organic_chaos_alpha = 1.0 - math.exp(
            -1.0 / (self.config.sample_rate * 0.018)
        )
        self.organic_source_lowpass = 0.0
        self.organic_bass_lowpass = 0.0
        self.organic_sub_phase = 0.173
        self.organic_bass_phase = 0.619
        self.organic_pulse_strength = 0.0
        self.organic_timbre_note = frequency_to_midi_note(self.current_frequency)
        self.organic_mode_a_1 = 0.0
        self.organic_mode_a_2 = 0.0
        self.organic_mode_b_1 = 0.0
        self.organic_mode_b_2 = 0.0
        self.organic_activity = 0.0
        self.organic_nominal_target_frequency = self.target_frequency
        self.organic_control_remaining = 0
        self.organic_control_position = 0
        self.organic_control_age_start = 0
        self.organic_control_frequency = self.current_frequency
        self.organic_control_velocity = self.velocity

    def _tail_energy(self) -> float:
        return max(
            abs(self.organic_mode_a_1),
            abs(self.organic_mode_a_2),
            abs(self.organic_mode_b_1),
            abs(self.organic_mode_b_2),
        )

    @property
    def active(self) -> bool:
        return super().active or self._tail_energy() >= TAIL_THRESHOLD

    @property
    def silent(self) -> bool:
        return super().silent and self._tail_energy() < TAIL_THRESHOLD

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
            # A note/gesture-derived seed makes every call reproducible while
            # avoiding an identical machine-like instability trajectory.
            seed = (
                note * 0x45D9F3B
                + bounded_velocity * 0x119DE1F3
                + self.organic_phrase_serial * 0x27D4EB2D
            ) & 0xFFFFFFFF
            self.organic_chaos = 0.19 + 0.61 * (seed / 0xFFFFFFFF)
            self.organic_chaos_target = 2.0 * self.organic_chaos - 1.0
            self.organic_chaos_smooth = 0.0
            self.organic_chaos_counter = 0
            self.organic_sub_phase = ((seed >> 8) & 0xFFFF) / 65536.0
            self.organic_bass_phase = ((seed >> 16) & 0xFFFF) / 65536.0
            self.organic_pulse_strength = max(
                0.06, (bounded_velocity / 127.0 - 0.58) * 0.34
            )
            self.organic_timbre_note = frequency_to_midi_note(self.current_frequency)
        elif repeated:
            self.organic_pulse_strength = 0.72
        elif previous_note is not None:
            interval = abs(note - previous_note)
            self.organic_pulse_strength = max(
                self.organic_pulse_strength,
                clamp(interval / 36.0, 0.04, 0.24),
            )
            # Keep a vocal connection, but avoid theremin-like slow sweeps.
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
        self.organic_source_lowpass = 0.0
        self.organic_bass_lowpass = 0.0
        self.organic_mode_a_1 = 0.0
        self.organic_mode_a_2 = 0.0
        self.organic_mode_b_1 = 0.0
        self.organic_mode_b_2 = 0.0
        self.organic_activity = 0.0
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
        age_seconds = self.note_age_frames / self.config.sample_rate
        velocity = clamp(self.velocity, 0.0, 1.0)
        # The base morph voice already supplies onset, vibrato and slow arcs.
        # This layer adds only small laryngeal drift, never a second glissando.
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
        instability = (
            0.8 + 2.7 * self.modulation + 1.2 * developed
        ) * self.organic_chaos_smooth
        pulse = 3.5 * self.organic_pulse_strength
        return onset + drift + instability + pulse

    def _update_chaos(self) -> None:
        self.organic_chaos_counter += 1
        if self.organic_chaos_counter >= 400:
            self.organic_chaos_counter = 0
            value = clamp(self.organic_chaos, 0.001, 0.999)
            self.organic_chaos = 3.91 * value * (1.0 - value)
            self.organic_chaos_target = 2.0 * self.organic_chaos - 1.0
        self.organic_chaos_smooth += (
            self.organic_chaos_target - self.organic_chaos_smooth
        ) * self.organic_chaos_alpha

    @staticmethod
    def _resonator_sample(
        excitation: float,
        first: float,
        second: float,
        excitation_gain: float,
        coefficient: float,
        radius_squared: float,
    ) -> tuple[float, float, float]:
        value = (
            excitation_gain * excitation
            + coefficient * first
            - radius_squared * second
        )
        value = clamp(value, -0.8, 0.8)
        return value, value, first

    def _process_block(self, base: list[float]) -> list[float]:
        sample_rate = self.config.sample_rate
        frequency = max(self.organic_control_frequency, 1.0)
        velocity = self.organic_control_velocity
        target_timbre = frequency_to_midi_note(frequency)
        bass_weight = clamp((55.0 - target_timbre) / 24.0, 0.0, 1.0)
        bass_weight = bass_weight * bass_weight * (3.0 - 2.0 * bass_weight)
        timbre_alpha = 1.0 - math.exp(-1.0 / (sample_rate * 0.20))
        source_cutoff = clamp(
            300.0 + frequency * 2.1 + (1.0 - bass_weight) * 560.0,
            280.0,
            4_200.0,
        )
        source_alpha = 1.0 - math.exp(-2.0 * math.pi * source_cutoff / sample_rate)
        bass_cutoff = clamp(frequency * 3.2, 75.0, 560.0)
        bass_alpha = 1.0 - math.exp(-2.0 * math.pi * bass_cutoff / sample_rate)
        mode_a_frequency = clamp(1.18 * frequency + 52.0, 92.0, 860.0)
        mode_b_frequency = clamp(2.05 * frequency + 118.0, 210.0, 1_800.0)
        radius_a = math.exp(-1.0 / (sample_rate * (0.18 + 0.24 * self.distance)))
        radius_b = math.exp(-1.0 / (sample_rate * (0.11 + 0.16 * self.distance)))
        coefficient_a = 2.0 * radius_a * math.cos(
            2.0 * math.pi * mode_a_frequency / sample_rate
        )
        coefficient_b = 2.0 * radius_b * math.cos(
            2.0 * math.pi * mode_b_frequency / sample_rate
        )
        pulse_decay = 1.0 - math.exp(-1.0 / (sample_rate * 0.11))
        two_pi = 2.0 * math.pi
        activity_attack = 1.0 - math.exp(-1.0 / (sample_rate * 0.008))
        activity_release = 1.0 - math.exp(-1.0 / (sample_rate * 0.045))
        output: list[float] = []

        for source_sample in base:
            self._update_chaos()
            self.organic_timbre_note += (
                target_timbre - self.organic_timbre_note
            ) * timbre_alpha
            self.organic_source_lowpass += (
                source_sample - self.organic_source_lowpass
            ) * source_alpha
            self.organic_bass_lowpass += (
                source_sample - self.organic_bass_lowpass
            ) * bass_alpha
            edge = source_sample - self.organic_source_lowpass
            age_frames = self.organic_control_age_start + self.organic_control_position
            self.organic_control_position += 1
            developed = clamp((age_frames / sample_rate - 0.28) / 2.0, 0.0, 1.0)
            activity_target = clamp(
                abs(source_sample) / max(self.config.master_gain * 0.08, 1.0e-6),
                0.0,
                1.0,
            )
            activity_alpha = (
                activity_attack
                if activity_target > self.organic_activity
                else activity_release
            )
            self.organic_activity += (
                activity_target - self.organic_activity
            ) * activity_alpha
            active_envelope = self.organic_activity
            irregularity = (
                0.028
                + 0.052 * velocity
                + 0.060 * self.modulation
                + 0.028 * developed
            )
            turbulence = (
                math.tanh(
                    edge * 22.0
                    + source_sample * 2.2 * self.organic_chaos_smooth
                )
                * irregularity
                * (0.48 + 0.24 * abs(self.organic_chaos_smooth))
            )

            sub_frequency = frequency * 0.5 * (
                1.0 + 0.0018 * self.organic_chaos_smooth
            )
            self.organic_sub_phase = (
                self.organic_sub_phase + sub_frequency / sample_rate
            ) % 1.0
            sub_sample = (
                math.sin(two_pi * self.organic_sub_phase)
                * active_envelope
                * self.expression
                * self.config.master_gain
                * bass_weight
                * 0.018
            )

            self.organic_bass_phase = (
                self.organic_bass_phase + frequency / sample_rate
            ) % 1.0
            bass_oscillator = (
                math.sin(two_pi * self.organic_bass_phase)
                + 0.23
                * math.sin(
                    2.0 * two_pi * self.organic_bass_phase
                    + 0.18 * self.organic_chaos_smooth
                )
            )
            velocity_gain = 0.22 + 0.78 * velocity**1.3
            bass_body = (
                0.48 * self.organic_bass_lowpass
                + 0.52
                * bass_oscillator
                * active_envelope
                * velocity_gain
                * self.expression
                * self.config.master_gain
            ) * bass_weight * (1.05 + 0.45 * (1.0 - velocity))

            self.organic_pulse_strength += (
                0.0 - self.organic_pulse_strength
            ) * pulse_decay
            pulse_gain = 1.0 + 0.18 * self.organic_pulse_strength
            pulse_breath = turbulence * 0.30 * self.organic_pulse_strength

            excitation = (
                source_sample
                + 0.30 * turbulence
                + 0.20 * bass_body
            )
            (
                mode_a,
                self.organic_mode_a_1,
                self.organic_mode_a_2,
            ) = self._resonator_sample(
                excitation,
                self.organic_mode_a_1,
                self.organic_mode_a_2,
                1.0 - radius_a,
                coefficient_a,
                radius_a * radius_a,
            )
            (
                mode_b,
                self.organic_mode_b_1,
                self.organic_mode_b_2,
            ) = self._resonator_sample(
                excitation,
                self.organic_mode_b_1,
                self.organic_mode_b_2,
                1.0 - radius_b,
                coefficient_b,
                radius_b * radius_b,
            )
            modal_mix = 0.022 + 0.042 * self.distance + 0.018 * developed
            amplitude_wander = 1.0 + irregularity * 0.18 * self.organic_chaos_smooth
            source_mix = 0.86 - 0.18 * bass_weight
            raw = (
                source_mix * source_sample * amplitude_wander * pulse_gain
                + bass_body
                + sub_sample
                + 0.62 * turbulence
                + pulse_breath
                + modal_mix * (0.70 * mode_a + 0.30 * mode_b)
            )
            sample = raw / (1.0 + 0.92 * abs(raw))
            output.append(clamp(sample, -MAX_MASTER_GAIN, MAX_MASTER_GAIN))

        if super().silent and self._tail_energy() < TAIL_THRESHOLD:
            self.organic_mode_a_1 = 0.0
            self.organic_mode_a_2 = 0.0
            self.organic_mode_b_1 = 0.0
            self.organic_mode_b_2 = 0.0
            self.organic_source_lowpass = 0.0
            self.organic_bass_lowpass = 0.0
            self.organic_activity = 0.0
        return output

    def render(self, frames: int) -> list[float]:
        if frames < 0 or frames > self.config.sample_rate * 30:
            raise ValueError("render frame count is outside the bounded range")
        if frames == 0:
            return []
        if self.silent:
            return [0.0] * frames

        output: list[float] = []
        remaining = frames
        while remaining:
            if self.silent:
                output.extend([0.0] * remaining)
                break
            if self.organic_control_remaining <= 0:
                self._start_control_segment()
            block = min(remaining, self.organic_control_remaining)
            base = super().render(block)
            output.extend(self._process_block(base))
            self.organic_control_remaining -= block
            remaining -= block
        return output

    def render_f32_stereo(self, frames: int) -> bytes:
        # Reuse the parent encoder, which dispatches to this class's render().
        return super().render_f32_stereo(frames)
