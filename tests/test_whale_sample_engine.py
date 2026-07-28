import copy
import json
import math
import pathlib
import struct
import sys
import unittest
from unittest import mock

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

    def test_all_manifest_slots_and_clips_are_reachable_from_the_keyboard(self):
        selected = [self.bank.select(note) for note in range(21, 109)]
        selected_slots = {(slot.root_note, slot.clip.clip_id) for slot in selected}
        manifest_slots = {
            (int(record["root_note"]), str(record["clip_id"]))
            for record in self.bank.manifest["slots"]
        }
        selected_clips = {slot.clip.clip_id for slot in selected}
        manifest_clips = {str(record["id"]) for record in self.bank.manifest["clips"]}
        self.assertEqual(selected_slots, manifest_slots)
        self.assertEqual(selected_clips, manifest_clips)

    def test_catalog_contains_immutable_sources_and_complete_cc_by_notice(self):
        catalog = json.loads(
            (ROOT / "assets" / "whale-sources" / "SOURCES.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(catalog["schema_version"], 2)
        for source in catalog["sources"]:
            self.assertRegex(source["expected_sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(source["expected_bytes"], 0)
            self.assertTrue(source["creators"])
            self.assertTrue(source["changes"])
            self.assertTrue(source["license_url"].startswith("https://"))
            if source["license"] == "CC-BY-2.5":
                self.assertEqual(
                    source["license_url"],
                    "https://creativecommons.org/licenses/by/2.5/",
                )
                self.assertGreaterEqual(len(source["creators"]), 4)
        notice = (ROOT / "assets" / "whale-sources" / "NOTICE.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("https://creativecommons.org/licenses/by/2.5/", notice)
        self.assertIn("Änderungen:", notice)

    def test_registers_choose_natural_source_families(self):
        self.assertEqual(self.bank.select(24).clip.category, "low")
        self.assertEqual(self.bank.select(60).clip.category, "song")
        self.assertEqual(self.bank.select(104).clip.category, "high")

    def test_bank_rejects_catalog_and_raw_source_hash_mismatch(self):
        actual_sha256 = sample.sha256_file

        def bad_catalog(path):
            if pathlib.Path(path).name == "SOURCES.json":
                return "0" * 64
            return actual_sha256(path)

        with mock.patch.object(sample, "sha256_file", side_effect=bad_catalog):
            with self.assertRaisesRegex(RuntimeError, "source catalog hash mismatch"):
                sample.WhaleSampleBank()

        def bad_raw_source(path):
            path = pathlib.Path(path)
            if path.suffix in {".ogg", ".oga"}:
                return "0" * 64
            return actual_sha256(path)

        with mock.patch.object(sample, "sha256_file", side_effect=bad_raw_source):
            with self.assertRaisesRegex(RuntimeError, "source audio hash mismatch"):
                sample.WhaleSampleBank()


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

    def test_detached_note_in_same_zone_restarts_original_phrase(self):
        voice = self.voice()
        voice.note_on(60, 96)
        voice.render(4_000)
        original_layer = voice.current
        original_clip = original_layer.slot.clip.clip_id
        voice.note_off(60)
        voice.render(512)

        voice.note_on(60, 96)

        self.assertIs(voice.previous, original_layer)
        self.assertIsNot(voice.current, original_layer)
        self.assertEqual(voice.current.slot.clip.clip_id, original_clip)
        self.assertEqual(voice.current.position, 0.0)
        self.assertEqual(voice.crossfade_remaining, voice.crossfade_total)

    def test_loop_wrap_skips_the_already_crossfaded_head(self):
        voice = self.voice()
        voice.note_on(60, 96)
        layer = voice.current
        clip = layer.slot.clip
        layer.position = float(clip.loop_end - clip.loop_crossfade)
        layer.rate = 1.0
        layer.target_rate = 1.0

        voice.render(clip.loop_crossfade + 1)

        self.assertAlmostEqual(
            layer.position, clip.loop_start + clip.loop_crossfade + 1, places=4
        )

    def test_detached_phrase_resets_hold_duration(self):
        voice = self.voice()
        voice.note_on(48, 100)
        voice.render(48_000 * 12)
        voice.note_off(48)
        voice.render(48_000 * 4)
        self.assertTrue(voice.silent)

        voice.note_on(60, 100)
        self.assertEqual(voice.hold_frames, 0)
        voice.render(48_000)
        voice.note_off(60)

        self.assertEqual(voice.hold_frames, 48_000)
        self.assertAlmostEqual(voice.release_seconds, 0.53, places=2)

    def test_rapid_third_note_preserves_the_current_crossfade_sample(self):
        voice = self.voice()
        voice.note_on(30, 100)
        voice.render(8_000)
        voice.note_on(70, 100)
        voice.render(voice.crossfade_total // 3)
        uninterrupted = copy.deepcopy(voice)
        expected = uninterrupted.render(1)[0]

        voice.note_on(104, 100)
        self.assertGreaterEqual(len(voice.fading_layers), 2)
        actual = voice.render(1)[0]

        self.assertAlmostEqual(actual, expected, places=7)
        tail = voice.render(voice.crossfade_total + 512)
        self.assertFalse(voice.fading_layers)
        self.assertTrue(all(math.isfinite(value) for value in tail))
        self.assertLessEqual(max(abs(value) for value in tail), sample.MAX_MASTER_GAIN)

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

    def test_pitch_bend_stays_inside_total_four_semitone_contract(self):
        voice = self.voice()
        for note in range(21, 109):
            slot = self.bank.select(note)
            for bend in (-8192, 8191):
                voice.pitch_bend(bend)
                semitones = 12.0 * math.log2(voice._rate_for(note, slot))
                self.assertLessEqual(abs(semitones), 4.0 + 1e-12)

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
