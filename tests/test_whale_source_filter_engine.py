import json
import math
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_whale_voice_model as builder  # noqa: E402
import evaluate_whale_voice_model as evaluator  # noqa: E402
import whale_live_engine as live  # noqa: E402
import whale_morph_engine as morph  # noqa: E402
import whale_source_filter_engine as source_filter  # noqa: E402


class WhaleVoiceModelBuilderTests(unittest.TestCase):
    def test_committed_model_is_byte_reproducible_from_bound_sources(self):
        expected = builder.encode_manifest(builder.build_manifest())
        actual = builder.DEFAULT_OUTPUT.read_bytes()
        self.assertEqual(expected, actual)
        self.assertEqual(
            builder.sha256_bytes(actual),
            "c2f5a99f0d9c95f75830ba6f2122cfbdd12e847b54eca6bbee80b563d07a9541",
        )

    def test_source_family_holdout_is_disjoint_and_complete(self):
        model = json.loads(builder.DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        train = set(model["train_source_ids"])
        holdout = set(model["holdout_source_ids"])
        trajectories = model["trajectories"]
        processed = json.loads(builder.SOURCE_MANIFEST.read_text(encoding="utf-8"))
        source_index = {record["id"]: record for record in processed["clips"]}

        self.assertFalse(train & holdout)
        self.assertGreaterEqual(len(train), 4)
        self.assertGreaterEqual(len(holdout), 2)
        self.assertEqual(len(trajectories), len(processed["clips"]))
        self.assertEqual({record["clip_id"] for record in trajectories}, set(source_index))
        for record in trajectories:
            source = source_index[record["clip_id"]]
            self.assertEqual(record["source_id"], source["source_id"])
            self.assertEqual(record["source_sha256"], source["sha256"])
            self.assertEqual(
                record["split"],
                "holdout" if record["source_id"] in holdout else "train",
            )
            self.assertEqual(len(record["points"]), builder.CONTROL_POINTS)
            for point in record["points"]:
                self.assertEqual(
                    len(point["harmonic_profile"]), builder.HARMONIC_COUNT
                )
                self.assertTrue(0.0 <= point["periodicity"] <= 1.0)
                self.assertTrue(0.0 <= point["roughness"] <= 1.0)
                self.assertTrue(0.0 <= point["pulse_strength"] <= 1.0)

    def test_model_declares_no_phrase_playback_or_permanent_noise(self):
        model = json.loads(builder.DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        contract = model["model_contract"]
        self.assertFalse(contract["plays_recorded_phrase"])
        self.assertFalse(contract["permanent_noise_layer"])
        self.assertTrue(contract["main_fundamental_bound_to_played_note"])


class WhaleSourceFilterBankTests(unittest.TestCase):
    def setUp(self):
        self.bank = source_filter.WhaleSourceFilterBank()

    def test_bank_is_source_bound_and_ready(self):
        status = self.bank.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["trajectory_count"], 19)
        self.assertEqual(status["train_trajectory_count"], 12)
        self.assertEqual(status["holdout_trajectory_count"], 7)
        self.assertFalse(status["permanent_noise_layer"])
        self.assertFalse(status["recorded_phrase_playback"])
        self.assertFalse(
            set(status["train_source_ids"]) & set(status["holdout_source_ids"])
        )

    def test_live_selection_never_uses_holdout_sources(self):
        holdout = set(self.bank.holdout_source_ids)
        selected = {
            self.bank._trajectory(note, seed, unit).source_id
            for note in (21, 33, 45, 57, 69, 84, 96, 108)
            for seed in (0, 1, 0x12345678, 0xFFFFFFFF)
            for unit in range(16)
        }
        self.assertTrue(selected)
        self.assertFalse(selected & holdout)
        self.assertTrue(selected <= set(self.bank.train_source_ids))

    def test_long_hold_uses_evolving_source_controls(self):
        values = [
            self.bank.control(
                note=45,
                seed=0x12345678,
                age_frames=round(seconds * 48_000),
                sample_rate=48_000,
            )
            for seconds in (0.0, 0.4, 1.2, 2.6, 4.2, 6.8)
        ]
        signatures = {
            (
                round(value.envelope, 4),
                round(value.periodicity, 4),
                round(value.formant_ratio_1, 4),
                round(value.pulse_strength, 4),
                round(value.secondary_strength, 4),
            )
            for value in values
        }
        self.assertGreaterEqual(len(signatures), 5)

    def test_tampered_source_manifest_binding_is_rejected(self):
        value = json.loads(builder.DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        value["source_sample_manifest_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                source_filter.WhaleSourceFilterBank(path)


class WhaleVoiceModelHoldoutTests(unittest.TestCase):
    def test_organic_voice_improves_whole_source_family_holdout(self):
        morph_report = evaluator.evaluate("morph")
        organic_report = evaluator.evaluate("organic")
        morph_score = float(morph_report["holdout_similarity_score_0_to_1"])
        organic_score = float(organic_report["holdout_similarity_score_0_to_1"])
        self.assertGreater(organic_score, morph_score * 1.15)
        self.assertLess(float(organic_report["peak"]), 0.24)
        self.assertEqual(
            organic_report["holdout_source_ids"],
            list(source_filter.WhaleSourceFilterBank().holdout_source_ids),
        )
        self.assertFalse(
            set(organic_report["train_source_ids"])
            & set(organic_report["holdout_source_ids"])
        )


class WhaleSourceFilterVoiceTests(unittest.TestCase):
    def setUp(self):
        self.config = live.WhaleVoiceConfig(
            sample_rate=48_000,
            block_frames=128,
            master_gain=0.16,
        )

    @staticmethod
    def rms(values):
        return math.sqrt(sum(value * value for value in values) / len(values))

    def test_idle_is_exact_silence(self):
        voice = source_filter.WhaleSourceFilterVoice(self.config)
        self.assertEqual(voice.render(4096), [0.0] * 4096)
        self.assertEqual(voice.render_f32_stereo(128), bytes(128 * 2 * 4))

    def test_main_fundamental_remains_bound_to_played_note(self):
        voice = source_filter.WhaleSourceFilterVoice(self.config)
        for note in (21, 33, 45, 57, 69, 84, 96, 108):
            voice.control_change(120, 0)
            voice.note_on(note, 80)
            self.assertAlmostEqual(
                voice.target_frequency,
                morph.midi_note_frequency(note),
                places=10,
            )
            self.assertGreater(self.rms(voice.render(512)), 1.0e-5)

    def test_same_gesture_is_deterministic_and_chunk_invariant(self):
        one_shot = source_filter.WhaleSourceFilterVoice(self.config)
        chunked = source_filter.WhaleSourceFilterVoice(self.config)
        for voice in (one_shot, chunked):
            voice.note_on(52, 80)
            voice.control_change(1, 49)
        total = 8192
        expected = one_shot.render(total)
        actual = []
        sizes = (17, 111, 3, 509, 256, 729, 1, 2048, 73)
        index = 0
        while len(actual) < total:
            size = min(sizes[index % len(sizes)], total - len(actual))
            actual.extend(chunked.render(size))
            index += 1
        self.assertEqual(expected, actual)
        excluded = {"bank", "source_filter_bank"}
        self.assertEqual(
            {key: value for key, value in one_shot.__dict__.items() if key not in excluded},
            {key: value for key, value in chunked.__dict__.items() if key not in excluded},
        )
        self.assertEqual(
            one_shot.source_filter_bank.status(),
            chunked.source_filter_bank.status(),
        )

    def test_output_is_bounded_and_has_realtime_headroom(self):
        voice = source_filter.WhaleSourceFilterVoice(self.config)
        voice.note_on(57, 80)
        started = time.perf_counter()
        samples = voice.render(self.config.sample_rate)
        duration = time.perf_counter() - started
        self.assertLess(duration, 0.62)
        self.assertLessEqual(max(abs(value) for value in samples), 0.25)
        self.assertGreater(self.rms(samples), 1.0e-4)


if __name__ == "__main__":
    unittest.main()
