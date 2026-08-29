import copy
import json
import math
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import whale_live_engine as live_engine  # noqa: E402
import whale_morph_engine as morph  # noqa: E402


class WhaleMorphBankTests(unittest.TestCase):
    def test_committed_bank_is_source_bound_and_covers_keyboard(self):
        bank = morph.WhaleMorphBank()
        status = bank.status()

        self.assertTrue(status["ready"])
        self.assertEqual(status["note_range"], [21, 108])
        self.assertEqual(status["sample_zones"], 0)
        self.assertFalse(status["permanent_noise_layer"])
        self.assertEqual(bank.anchors[0].note, 21)
        self.assertEqual(bank.anchors[-1].note, 108)
        self.assertGreaterEqual(len(bank.anchors), 3)
        self.assertTrue(all(anchor.periodicity >= 0.58 for anchor in bank.anchors))

    def test_source_clocks_are_bound_to_manifest_anchors(self):
        bank = morph.WhaleMorphBank()
        self.assertAlmostEqual(bank.source_clock_hz(21.0), 28.38557066824364, places=9)
        self.assertAlmostEqual(bank.source_clock_hz(36.0), 33.264033264033266, places=9)
        self.assertAlmostEqual(bank.source_clock_hz(48.0), 105.49450549450549, places=9)
        self.assertTrue(all(anchor.source_frequency_hz > 0.0 for anchor in bank.anchors))

    def test_every_embedded_table_has_bounded_samples(self):
        bank = morph.WhaleMorphBank()
        for anchor in bank.anchors:
            for level in anchor.levels:
                self.assertEqual(len(level.table), bank.table_size)
                self.assertLessEqual(max(abs(value) for value in level.table), 1.0)

    def test_adjacent_timbres_are_continuous_without_zones(self):
        bank = morph.WhaleMorphBank()
        phase = 0.371
        values = [
            bank.sample(phase, note / 8.0, morph.midi_note_frequency(note / 8.0))
            for note in range(21 * 8, 108 * 8 + 1)
        ]
        largest_step = max(abs(right - left) for left, right in zip(values, values[1:]))
        self.assertLess(largest_step, 0.25)

    def test_manifest_symlink_is_rejected_before_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            link = pathlib.Path(directory) / "manifest.json"
            link.symlink_to(morph.DEFAULT_MANIFEST)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                morph.WhaleMorphBank(link)

    def test_anchor_provenance_must_match_bound_source_manifest(self):
        value = json.loads(morph.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        value["anchors"][0]["source_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "provenance"):
                morph.WhaleMorphBank(path)

    def test_bandlimit_transition_never_mixes_an_unsafe_upper_level(self):
        bank = object.__new__(morph.WhaleMorphBank)
        bank.sample_rate = 48_000
        anchor = morph.MorphAnchor(
            60,
            "test",
            1.0,
            (
                morph.MorphLevel(8, (0.8, 0.8)),
                morph.MorphLevel(4, (0.4, 0.4)),
                morph.MorphLevel(2, (0.2, 0.2)),
                morph.MorphLevel(1, (0.1, 0.1)),
            ),
        )
        frequency = 48_000 * 0.45 / 5.0
        amount = math.log2(5.0 / 4.0) / math.log2(8.0 / 4.0)
        expected = 0.2 + (0.4 - 0.2) * amount
        self.assertAlmostEqual(bank._level_sample(anchor, 0.25, frequency), expected)


class WhaleMorphVoiceTests(unittest.TestCase):
    def setUp(self):
        self.config = live_engine.WhaleVoiceConfig(
            sample_rate=48_000,
            block_frames=128,
            master_gain=0.16,
        )

    @staticmethod
    def rms(values):
        return math.sqrt(sum(value * value for value in values) / len(values))

    def test_all_88_keys_use_standard_chromatic_tuning(self):
        frequencies = [morph.midi_note_frequency(note) for note in range(21, 109)]
        self.assertEqual(len(frequencies), 88)
        self.assertAlmostEqual(frequencies[0], 27.5, places=10)
        self.assertAlmostEqual(frequencies[48], 440.0, places=10)
        self.assertAlmostEqual(frequencies[-1], 4186.009044809578, places=9)
        self.assertTrue(
            all(
                math.isclose(right / left, 2.0 ** (1.0 / 12.0), rel_tol=1e-12)
                for left, right in zip(frequencies, frequencies[1:])
            )
        )

    def test_low_register_separates_pitch_and_source_texture_clocks(self):
        voice = morph.WhaleMorphVoice(self.config)
        voice.note_on(36, 80)
        self.assertAlmostEqual(voice.target_frequency, morph.midi_note_frequency(36), places=10)
        self.assertAlmostEqual(voice.target_source_clock_hz, 33.264033264033266, places=9)
        self.assertGreater(voice.target_frequency / voice.target_source_clock_hz, 1.96)
        self.assertGreater(
            morph.source_clock_decoupling_amount(
                voice.target_frequency, voice.target_source_clock_hz, 36.0
            ),
            0.95,
        )
        self.assertEqual(
            morph.source_clock_decoupling_amount(
                morph.midi_note_frequency(21), voice.bank.source_clock_hz(21.0), 21.0
            ),
            0.0,
        )
        self.assertEqual(
            morph.source_clock_decoupling_amount(
                morph.midi_note_frequency(48), voice.bank.source_clock_hz(48.0), 48.0
            ),
            0.0,
        )

    def test_c2_two_clock_path_removes_most_legacy_fast_edge_energy(self):
        with mock.patch.object(
            morph, "source_clock_decoupling_amount", return_value=0.0
        ):
            legacy_voice = morph.WhaleMorphVoice(self.config)
            legacy_voice.note_on(36, 80)
            legacy = legacy_voice.render(self.config.sample_rate)
        candidate_voice = morph.WhaleMorphVoice(self.config)
        candidate_voice.note_on(36, 80)
        candidate = candidate_voice.render(self.config.sample_rate)

        def difference_energy_ratio(values):
            energy = sum(value * value for value in values) or 1.0
            derivative = sum(
                (right - left) ** 2 for left, right in zip(values, values[1:])
            )
            return derivative / energy

        legacy_edge = difference_energy_ratio(legacy)
        candidate_edge = difference_energy_ratio(candidate)
        self.assertGreater(legacy_edge, 0.0)
        self.assertLess(candidate_edge / legacy_edge, 0.10)
        self.assertLessEqual(max(abs(value) for value in candidate), morph.MAX_MASTER_GAIN)

    def test_idle_is_exact_silence_and_does_not_advance(self):
        voice = morph.WhaleMorphVoice(self.config)
        before = {
            key: copy.deepcopy(value)
            for key, value in voice.__dict__.items()
            if key != "bank"
        }
        self.assertEqual(voice.render(4_096), [0.0] * 4_096)
        self.assertEqual(voice.render_f32_stereo(256), bytes(256 * 2 * 4))
        self.assertEqual(
            before,
            {key: value for key, value in voice.__dict__.items() if key != "bank"},
        )

    def test_each_key_targets_its_exact_frequency_and_is_audible(self):
        voice = morph.WhaleMorphVoice(self.config)
        for note in range(21, 109):
            voice.control_change(120, 0)
            voice.note_on(note, 80)
            expected = morph.midi_note_frequency(note)
            self.assertAlmostEqual(voice.target_frequency, expected, places=10)
            samples = voice.render(512)
            self.assertGreater(self.rms(samples), 1e-5, note)

    def test_out_of_range_notes_are_ignored_without_endpoint_or_stuck_voice(self):
        voice = morph.WhaleMorphVoice(self.config)
        before = {
            key: copy.deepcopy(value)
            for key, value in voice.__dict__.items()
            if key != "bank"
        }
        for note in (0, 20, 109, 127):
            voice.note_on(note, 100)
            voice.note_off(note)
        self.assertEqual(
            before,
            {key: value for key, value in voice.__dict__.items() if key != "bank"},
        )
        self.assertTrue(voice.silent)
        self.assertEqual(voice.held_notes, {})

    def test_legato_keeps_one_phase_continuous_voice(self):
        voice = morph.WhaleMorphVoice(self.config)
        voice.note_on(48, 64)
        voice.render(12_000)
        phase_before = voice.phase
        envelope_before = voice.envelope
        voice.note_on(72, 92)
        voice.render(1)

        self.assertNotEqual(voice.phase, 0.0)
        self.assertNotEqual(voice.phase, phase_before)
        self.assertGreater(voice.envelope, envelope_before * 0.98)
        self.assertEqual(voice.active_note, 72)
        self.assertEqual(len(voice.held_notes), 2)

    def test_detached_retrigger_starts_a_new_call(self):
        voice = morph.WhaleMorphVoice(self.config)
        voice.note_on(60, 72)
        voice.render(8_000)
        voice.note_off(60)
        voice.render(self.config.sample_rate * 4)
        self.assertTrue(voice.silent)

        voice.note_on(64, 72)
        self.assertEqual(voice.phase, 0.0)
        self.assertEqual(voice.note_age_frames, 0)
        self.assertEqual(voice.hold_frames, 0)

    def test_repeated_note_rearticulates_without_changing_pitch(self):
        voice = morph.WhaleMorphVoice(self.config)
        voice.note_on(60, 60)
        voice.render(12_000)
        phase_before = voice.phase
        voice.note_on(60, 105)

        self.assertEqual(voice.active_note, 60)
        self.assertAlmostEqual(voice.target_frequency, morph.midi_note_frequency(60))
        self.assertEqual(voice.phase, phase_before)
        self.assertEqual(voice.note_age_frames, 0)
        self.assertGreater(voice.retrigger_strength, 0.9)

    def test_hold_duration_changes_development_without_a_sample_loop(self):
        voice = morph.WhaleMorphVoice(self.config)
        voice.note_on(57, 78)
        first = voice.render(self.config.sample_rate)
        later = voice.render(self.config.sample_rate * 3)
        final = later[-self.config.sample_rate :]

        self.assertGreater(self.rms(first), 0.001)
        self.assertGreater(self.rms(final), 0.001)
        self.assertNotAlmostEqual(self.rms(first), self.rms(final), places=5)
        self.assertGreater(voice.note_age_frames, self.config.sample_rate * 3)

    def test_sustain_keeps_phrase_then_releases(self):
        voice = morph.WhaleMorphVoice(self.config)
        voice.note_on(52, 70)
        voice.render(4_000)
        voice.control_change(64, 127)
        voice.note_off(52)
        self.assertTrue(voice.gate)
        voice.render(4_000)
        voice.control_change(64, 0)
        self.assertFalse(voice.gate)
        voice.render(self.config.sample_rate * 5)
        self.assertTrue(voice.silent)

    def test_release_is_chunk_invariant(self):
        one_shot = morph.WhaleMorphVoice(self.config)
        chunked = morph.WhaleMorphVoice(self.config)
        for voice in (one_shot, chunked):
            voice.note_on(66, 91)
            voice.render(14_000)
            voice.note_off(66)

        total = self.config.sample_rate * 4
        expected = one_shot.render(total)
        actual = []
        remaining = total
        sizes = (1, 17, 128, 511, 1_003)
        index = 0
        while remaining:
            size = min(sizes[index % len(sizes)], remaining)
            actual.extend(chunked.render(size))
            remaining -= size
            index += 1
        self.assertEqual(expected, actual)
        self.assertEqual(
            {key: value for key, value in one_shot.__dict__.items() if key != "bank"},
            {key: value for key, value in chunked.__dict__.items() if key != "bank"},
        )

    def test_pitch_bend_is_bounded_to_two_semitones(self):
        voice = morph.WhaleMorphVoice(self.config)
        voice.note_on(69, 80)
        voice.pitch_bend(8191)
        self.assertAlmostEqual(
            voice.target_frequency,
            440.0 * 2.0 ** ((200.0 * 8191.0 / 8192.0) / 1200.0),
            places=9,
        )

    def test_one_second_render_has_realtime_headroom(self):
        voice = morph.WhaleMorphVoice(self.config)
        voice.note_on(60, 80)
        started = time.perf_counter()
        voice.render(self.config.sample_rate)
        duration = time.perf_counter() - started
        self.assertLess(duration, 0.75)


class WhaleMorphManifestContractTests(unittest.TestCase):
    def test_manifest_declares_no_presets_samples_or_noise_layer(self):
        path = ROOT / "assets" / "whale-sources" / "morph" / "manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        extraction = value["extraction"]
        self.assertFalse(extraction["permanent_noise_layer"])
        self.assertFalse(extraction["long_phrase_playback"])
        self.assertFalse(extraction["preset_or_keyboard_zone_selection"])
        self.assertEqual(value["voice_count"], 1)
        self.assertEqual(value["note_range"], [21, 108])


if __name__ == "__main__":
    unittest.main()
