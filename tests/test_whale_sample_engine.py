import math
import pathlib
import struct
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import whale_live_engine as synth  # noqa: E402
import whale_sample_engine as sample  # noqa: E402


class WhaleSampleBankTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank = sample.WhaleSampleBank()

    def test_bank_has_licensed_sources_and_bounded_pitch_slots(self):
        status = self.bank.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["source_count"], 8)
        self.assertEqual(status["clip_count"], 19)
        self.assertEqual(status["slot_count"], 27)
        self.assertEqual(
            status["licenses"],
            ["CC-BY-2.5", "CC0-1.0", "Public-Domain-US-NPS"],
        )
        for note in range(21, 109):
            slot = self.bank.select(note)
            self.assertLessEqual(abs(note - slot.root_note), 4)

    def test_registers_choose_natural_source_families(self):
        self.assertEqual(self.bank.select(24).clip.category, "low")
        self.assertEqual(self.bank.select(60).clip.category, "song")
        self.assertEqual(self.bank.select(104).clip.category, "high")


class WhaleSampleVoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank = sample.WhaleSampleBank()
        cls.config = synth.WhaleVoiceConfig(sample_rate=48_000, block_frames=128)

    def voice(self):
        return sample.WhaleSampleVoice(self.config, bank=self.bank)

    def test_idle_is_exact_zero_and_f32_shape_is_bounded(self):
        voice = self.voice()
        self.assertEqual(voice.render(512), [0.0] * 512)
        self.assertEqual(voice.render_f32_stereo(128), bytes(128 * 2 * 4))
        with self.assertRaises(ValueError):
            voice.render(-1)
        with self.assertRaises(ValueError):
            voice.render_f32_stereo(48_000 * 30 + 1)

    def test_real_recording_is_audible_deterministic_and_bounded(self):
        def render_once():
            voice = self.voice()
            voice.note_on(60, 96)
            return voice.render(48_000)

        first = render_once()
        second = render_once()
        self.assertEqual(first, second)
        self.assertGreater(max(abs(value) for value in first), 0.01)
        self.assertLessEqual(max(abs(value) for value in first), sample.MAX_MASTER_GAIN)

    def test_legato_crossfades_between_recordings(self):
        voice = self.voice()
        voice.note_on(30, 80)
        before = voice.render(8_000)
        old_clip = voice.current.slot.clip.clip_id
        voice.note_on(72, 110)
        self.assertIsNotNone(voice.previous)
        self.assertNotEqual(old_clip, voice.current.slot.clip.clip_id)
        transition = voice.render(voice.crossfade_total + 512)
        self.assertIsNone(voice.previous)
        self.assertTrue(all(math.isfinite(value) for value in transition))
        self.assertLess(max(abs(value) for value in transition), sample.MAX_MASTER_GAIN)
        self.assertGreater(max(abs(value) for value in before), 0.005)

    def test_sustain_release_and_panic_follow_midi_contract(self):
        voice = self.voice()
        voice.note_on(48, 100)
        voice.render(4_000)
        voice.control_change(64, 127)
        voice.note_off(48)
        self.assertTrue(voice.gate)
        sustained = voice.render(2_000)
        self.assertGreater(max(abs(value) for value in sustained), 0.001)
        voice.control_change(64, 0)
        self.assertFalse(voice.gate)
        released = voice.render(48_000 * 4)
        self.assertTrue(voice.silent)
        self.assertEqual(released[-128:], [0.0] * 128)

        voice.note_on(60, 100)
        voice.render(512)
        voice.control_change(120, 0)
        self.assertTrue(voice.silent)
        self.assertEqual(voice.render_f32_stereo(64), bytes(64 * 8))

    def test_stereo_payload_is_little_endian_float32(self):
        voice = self.voice()
        voice.note_on(65, 90)
        payload = voice.render_f32_stereo(64)
        self.assertEqual(len(payload), 64 * 8)
        values = struct.unpack("<" + "f" * 128, payload)
        self.assertTrue(all(math.isfinite(value) for value in values))
        self.assertLessEqual(
            max(abs(value) for value in values), sample.MAX_MASTER_GAIN
        )


if __name__ == "__main__":
    unittest.main()
