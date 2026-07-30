#!/usr/bin/env python3
"""Organic articulation layer for the continuous source-derived whale voice.

The layer keeps the exact 88-key tuning and source-derived morph bank while
adding only signal-coupled, deterministic behaviour: formant inertia,
subharmonic body, short nonlinear frequency jumps, irregular micro-instability,
and two damped vocal/ocean modes. It has no permanent noise generator and is
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
        self.organic_sub_phase = 0.173
        self.organic_jump_phase = 0.619
        self.organic_jump_strength = 0.0
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
            self.organic_jump_phase = ((seed >> 16) & 0xFFFF) / 65536.0
            self.organic_jump_strength = max(
                0.0, (bounded_velocity / 127.0 - 0.72) * 1.25
            )
            self.organic_timbre_note = frequency_to_midi_note(self.current_frequency)
        elif repeated:
            self.organic_jump_strength = 1.0
        elif previous_note is not None:
            interval = abs(note - previous_note)
            self.organic_jump_strength = max(
                self.organic_jump_strength,
                clamp((interval - 4.0) / 18.0, 0.0, 0.48),
            )
            # Legato whale units sweep instead of snapping between piano notes.
            self.glide_seconds = clamp(self.glide_seconds * 1.55, 0.07, 0.72)

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
        # Soft calls emerge from farther below pitch; hard calls still retain a
        # short laryngeal onset rather than a piano-like immediate fundamental.
        onset = -(158.0 - 78.0 * velocity) * math.exp(-age_seconds / 0.34)
        developed = clamp((age_seconds - 0.28) / 1.7, 0.0, 1.0)
        arc = (
            (28.0 + 34.0 * self.modulation)
            * developed
            * math.sin(2.0 * math.pi * (0.083 * age_seconds + 0.11 * self.organic_phrase_serial))
        )
        instability = (
            2.5 + 8.5 * self.modulation + 3.0 * developed
        ) * self.organic_chaos_smooth
        jump = 132.0 * self.organic_jump_strength
        return onset + arc + instability + jump

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
        timbre_alpha = 1.0 - math.exp(-1.0 / (sample_rate * 0.24))
        source_cutoff = clamp(520.0 + frequency * 2.4, 620.0, 4_800.0)
        source_alpha = 1.0 - math.exp(-2.0 * math.pi * source_cutoff / sample_rate)
        mode_a_frequency = clamp(1.55 * frequency + 95.0, 185.0, 1_180.0)
        mode_b_frequency = clamp(2.75 * frequency + 180.0, 360.0, 2_350.0)
        radius_a = math.exp(-1.0 / (sample_rate * (0.42 + 0.42 * self.distance)))
        radius_b = math.exp(-1.0 / (sample_rate * (0.24 + 0.24 * self.distance)))
        coefficient_a = 2.0 * radius_a * math.cos(
            2.0 * math.pi * mode_a_frequency / sample_rate
        )
        coefficient_b = 2.0 * radius_b * math.cos(
            2.0 * math.pi * mode_b_frequency / sample_rate
        )
        jump_decay = 1.0 - math.exp(-1.0 / (sample_rate * 0.17))
        two_pi = 2.0 * math.pi
        activity_attack = 1.0 - math.exp(-1.0 / (sample_rate * 0.006))
        activity_release = 1.0 - math.exp(-1.0 / (sample_rate * 0.035))
        output: list[float] = []

        for source_sample in base:
            self._update_chaos()
            self.organic_timbre_note += (target_timbre - self.organic_timbre_note) * timbre_alpha
            self.organic_source_lowpass += (
                source_sample - self.organic_source_lowpass
            ) * source_alpha
            edge = source_sample - self.organic_source_lowpass
            age_frames = self.organic_control_age_start + self.organic_control_position
            self.organic_control_position += 1
            developed = clamp((age_frames / sample_rate - 0.22) / 1.6, 0.0, 1.0)
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
                0.055
                + 0.105 * velocity
                + 0.120 * self.modulation
                + 0.060 * developed
            )
            turbulence = (
                math.tanh(
                    edge * 48.0
                    + source_sample * 6.0 * self.organic_chaos_smooth
                )
                * irregularity
                * (0.72 + 0.42 * abs(self.organic_chaos_smooth))
            )
            folded_edge = (
                math.tanh((source_sample + 0.82 * edge) * 19.0)
                - math.tanh(source_sample * 7.0)
            ) * (0.052 + 0.048 * developed)

            sub_frequency = frequency * 0.5 * (
                1.0 + 0.0035 * self.organic_chaos_smooth
            )
            self.organic_sub_phase = (
                self.organic_sub_phase + sub_frequency / sample_rate
            ) % 1.0
            sub_amount = (
                0.025
                + 0.075 * (1.0 - velocity)
                + 0.052 * developed
            ) * (1.0 - 0.35 * self.distance)
            sub_sample = 0.0
            if active_envelope > 0.0:
                sub_oscillator = math.sin(
                    two_pi * self.organic_sub_phase
                    + 0.28 * source_sample
                    + 0.16 * self.organic_chaos_smooth
                )
                sub_sample = (
                    sub_oscillator
                    * active_envelope
                    * (0.22 + 0.78 * velocity**1.3)
                    * self.expression
                    * self.config.master_gain
                    * sub_amount
                )

            jump_sample = 0.0
            if self.organic_jump_strength > 1.0e-4 and active_envelope > 0.0:
                jump_ratio = 1.5 if self.organic_phrase_serial % 2 else 2.0
                jump_frequency = frequency * jump_ratio
                self.organic_jump_phase = (
                    self.organic_jump_phase + jump_frequency / sample_rate
                ) % 1.0
                jump_oscillator = math.sin(
                    two_pi * self.organic_jump_phase + 0.45 * edge
                )
                jump_sample = (
                    jump_oscillator
                    * active_envelope
                    * self.config.master_gain
                    * 0.080
                    * self.organic_jump_strength
                )
            self.organic_jump_strength += (
                0.0 - self.organic_jump_strength
            ) * jump_decay

            excitation = source_sample + 0.56 * turbulence + 0.42 * sub_sample
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
            modal_mix = 0.055 + 0.10 * self.distance + 0.035 * developed
            amplitude_wander = 1.0 + irregularity * 0.34 * self.organic_chaos_smooth
            raw = (
                (0.78 * source_sample + sub_sample + jump_sample) * amplitude_wander
                + 1.18 * turbulence
                + folded_edge
                + modal_mix * (0.64 * mode_a + 0.36 * mode_b)
            )
            sample = raw / (1.0 + 1.15 * abs(raw))
            output.append(clamp(sample, -MAX_MASTER_GAIN, MAX_MASTER_GAIN))

        if super().silent and self._tail_energy() < TAIL_THRESHOLD:
            self.organic_mode_a_1 = 0.0
            self.organic_mode_a_2 = 0.0
            self.organic_mode_b_1 = 0.0
            self.organic_mode_b_2 = 0.0
            self.organic_source_lowpass = 0.0
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
