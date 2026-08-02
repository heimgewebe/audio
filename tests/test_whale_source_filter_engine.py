import collections
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

MODEL_SHA256 = "1bbd10566bbfc9ee9159c994de456d408ed003cea65602faee8076b308d0ee8a"


class WhaleVoiceModelBuilderTests(unittest.TestCase):
    def test_committed_model_is_byte_reproducible_from_bound_sources(self):
        expected = builder.encode_manifest(builder.build_manifest())
        actual = builder.DEFAULT_OUTPUT.read_bytes()
        self.assertEqual(expected, actual)
        self.assertEqual(builder.sha256_bytes(actual), MODEL_SHA256)

    def test_source_family_catalog_is_exact_and_complete(self):
        model = json.loads(builder.DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        trajectories = model["trajectories"]
        processed = json.loads(builder.SOURCE_MANIFEST.read_text(encoding="utf-8"))
        source_index = {record["id"]: record for record in processed["clips"]}
        self.assertEqual(model["schema_version"], 2)
        self.assertEqual(model["source_ids"], list(builder.EXPECTED_SOURCE_IDS))
        self.assertEqual(len(trajectories), len(processed["clips"]))
        self.assertEqual({record["clip_id"] for record in trajectories}, set(source_index))
        for record in trajectories:
            source = source_index[record["clip_id"]]
            self.assertEqual(record["source_id"], source["source_id"])
            self.assertEqual(record["source_sha256"], source["sha256"])
            self.assertEqual(len(record["points"]), builder.CONTROL_POINTS)
            for point in record["points"]:
                self.assertEqual(
                    len(point["harmonic_profile"]), builder.HARMONIC_COUNT
                )
                self.assertAlmostEqual(
                    point["periodicity"] + point["roughness"], 1.0, places=7
                )

    def test_antialias_filter_suppresses_out_of_band_tones(self):
        def tone(frequency):
            return [
                math.sin(2.0 * math.pi * frequency * index / builder.SAMPLE_RATE)
                for index in range(builder.SAMPLE_RATE)
            ]

        passband = builder.downsample(tone(1_000.0), input_scale=1.0)
        stopband = builder.downsample(tone(6_000.0), input_scale=1.0)
        passband_rms = math.sqrt(sum(value * value for value in passband) / len(passband))
        stopband_rms = math.sqrt(sum(value * value for value in stopband) / len(stopband))
        self.assertGreater(passband_rms, 0.45)
        self.assertLess(stopband_rms, passband_rms * 0.02)

    def test_secondary_ratio_and_strength_are_independent(self):
        model = json.loads(builder.DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        active = [
            point
            for record in model["trajectories"]
            for point in record["points"]
            if point["secondary_strength"] > 0.0
        ]
        non_unison = [
            point for point in active if abs(point["secondary_ratio"] - 1.0) > 0.05
        ]
        self.assertGreater(len(active), 100)
        self.assertGreater(len(non_unison), len(active) // 2)
        self.assertTrue(
            any(
                abs(point["secondary_ratio"] - point["secondary_strength"]) > 0.1
                for point in active
            )
        )

    def test_model_declares_honest_analysis_and_no_phrase_playback(self):
        model = json.loads(builder.DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        contract = model["model_contract"]
        evaluation = model["evaluation_contract"]
        self.assertFalse(contract["plays_recorded_phrase"])
        self.assertFalse(contract["permanent_noise_layer"])
        self.assertTrue(contract["main_fundamental_bound_to_played_note"])
        self.assertIn("harmonic_resonance_emphasis_ratios", contract["features"])
        self.assertNotIn("formant_ratios", contract["features"])
        self.assertEqual(
            evaluation["strategy"], "leave-one-source-family-out-cross-validation"
        )
        self.assertFalse(evaluation["independent_test_claim"])


class WhaleSourceFilterBankTests(unittest.TestCase):
    def setUp(self):
        self.bank = source_filter.WhaleSourceFilterBank()

    def test_bank_is_exact_hash_bound_and_ready(self):
        status = self.bank.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["manifest_sha256"], MODEL_SHA256)
        self.assertEqual(status["expected_manifest_sha256"], MODEL_SHA256)
        self.assertEqual(status["trajectory_count"], 19)
        self.assertEqual(status["live_trajectory_count"], 19)
        self.assertEqual(status["source_ids"], list(builder.EXPECTED_SOURCE_IDS))
        self.assertEqual(status["excluded_source_ids"], [])

    def test_any_schema_valid_model_mutation_is_rejected_by_hash(self):
        value = json.loads(builder.DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        original = value["trajectories"][0]["points"][0]["envelope"]
        value["trajectories"][0]["points"][0]["envelope"] = (
            0.0 if original != 0.0 else 0.1
        )
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "manifest hash mismatch"):
                source_filter.WhaleSourceFilterBank(path)
            status = source_filter.source_filter_bank_status(path)
            self.assertFalse(status["ready"])
            self.assertIn("manifest hash mismatch", status["blocking_reason"])

    def test_family_exclusion_is_complete(self):
        excluded = builder.EXPECTED_SOURCE_IDS[3]
        bank = source_filter.WhaleSourceFilterBank(
            excluded_source_ids=frozenset({excluded})
        )
        selected = {
            bank._trajectory(note, seed, unit).source_id
            for note in (21, 57, 96)
            for seed in range(32)
            for unit in range(24)
        }
        self.assertNotIn(excluded, selected)
        self.assertEqual(bank.status()["excluded_source_ids"], [excluded])

    def test_family_then_clip_selection_is_balanced(self):
        for note in (21, 57, 96):
            families = self.bank._candidate_families(note)
            counts = collections.Counter(
                self.bank._trajectory(note, seed, unit).source_id
                for seed in range(256)
                for unit in range(32)
            )
            expected = 1.0 / len(families)
            total = sum(counts.values())
            for source_id, _clips in families:
                self.assertAlmostEqual(counts[source_id] / total, expected, delta=0.02)

    def test_unit_transition_is_continuous_and_each_unit_uses_own_duration(self):
        note = 69
        seed = 1
        first = self.bank._trajectory(note, seed, 0)
        second = self.bank._trajectory(note, seed, 1)
        first_frames = self.bank._unit_frames(first, 48_000)
        second_frames = self.bank._unit_frames(second, 48_000)
        before = self.bank.control(
            note=note,
            seed=seed,
            age_frames=first_frames - 1,
            sample_rate=48_000,
        )
        boundary = self.bank.control(
            note=note,
            seed=seed,
            age_frames=first_frames,
            sample_rate=48_000,
        )
        scalar_names = (
            "envelope",
            "periodicity",
            "high_band_ratio",
            "spectral_tilt",
            "resonance_ratio_1",
            "resonance_ratio_2",
            "pulse_rate_hz",
            "pulse_strength",
            "subharmonic_strength",
            "secondary_ratio",
            "secondary_strength",
        )
        self.assertLess(
            max(abs(getattr(before, name) - getattr(boundary, name)) for name in scalar_names),
            0.01,
        )
        self.assertLess(
            max(
                abs(left - right)
                for left, right in zip(
                    before.harmonic_profile, boundary.harmonic_profile
                )
            ),
            0.01,
        )
        unit_index, _trajectory, _frames, _local = self.bank._unit_position(
            note=note,
            seed=seed,
            age_frames=first_frames + second_frames - 1,
            sample_rate=48_000,
        )
        self.assertEqual(unit_index, 1)
        unit_index, _trajectory, _frames, _local = self.bank._unit_position(
            note=note,
            seed=seed,
            age_frames=first_frames + second_frames,
            sample_rate=48_000,
        )
        self.assertEqual(unit_index, 2)

    def test_timeline_supports_full_six_hour_runtime_and_bounds_cache(self):
        age_frames = 21_600 * 48_000 - 1
        control = self.bank.control(
            note=45,
            seed=0x12345678,
            age_frames=age_frames,
            sample_rate=48_000,
        )
        self.assertTrue(0.0 <= control.envelope <= 1.0)
        self.assertGreater(len(self.bank._timeline_cache[(45, 0x12345678, 48_000)]), 1_000)
        for seed in range(40):
            self.bank.control(
                note=57,
                seed=seed,
                age_frames=0,
                sample_rate=48_000,
            )
        self.assertLessEqual(
            len(self.bank._timeline_cache), self.bank._timeline_cache_limit
        )

    def test_upper_harmonic_profile_changes_rendered_output(self):
        value = json.loads(builder.DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        for record in value["trajectories"]:
            for point in record["points"]:
                profile = point["harmonic_profile"]
                total = min(sum(profile), 1.0)
                point["harmonic_profile"] = [0.0] * 7 + [total]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            modified = source_filter.WhaleSourceFilterBank(
                path, expected_manifest_sha256=None
            )
            config = live.WhaleVoiceConfig(
                sample_rate=48_000, block_frames=128, master_gain=0.16
            )
            original_voice = source_filter.WhaleSourceFilterVoice(
                config, source_filter_bank=self.bank
            )
            modified_voice = source_filter.WhaleSourceFilterVoice(
                config, source_filter_bank=modified
            )
            for voice in (original_voice, modified_voice):
                voice.note_on(57, 80)
            self.assertNotEqual(original_voice.render(4096), modified_voice.render(4096))


class WhaleIndependentEvaluationTests(unittest.TestCase):
    def test_independent_source_is_hash_bound_and_absent_from_model(self):
        report = evaluator.evaluate_external("morph")
        self.assertEqual(
            report["source_id"],
            "noaa-pmel-alaska-winter-1999-independent",
        )
        self.assertTrue(report["model_or_parameter_tuning_forbidden"])
        self.assertIn("population_generalization", report["does_not_establish"])
        self.assertNotIn(
            report["source_id"],
            source_filter.WhaleSourceFilterBank().source_ids,
        )
        self.assertTrue(0.0 <= report["similarity_score_0_to_1"] <= 1.0)
        self.assertEqual(
            report["evaluation_manifest_sha256"],
            evaluator.EXPECTED_EXTERNAL_EVALUATION_MANIFEST_SHA256,
        )

    def test_independent_processed_clip_mutation_is_rejected(self):
        manifest_payload = evaluator.EXTERNAL_EVALUATION_MANIFEST.read_bytes()
        manifest = json.loads(manifest_payload.decode("utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "raw").mkdir()
            (root / "processed").mkdir()
            evaluation_root = evaluator.EXTERNAL_EVALUATION_MANIFEST.parent
            (root / manifest["clips"][0]["raw_file"]).write_bytes(
                (evaluation_root / manifest["clips"][0]["raw_file"]).read_bytes()
            )
            processed_path = root / manifest["clips"][0]["processed_file"]
            processed_payload = bytearray(
                (evaluation_root / manifest["clips"][0]["processed_file"]).read_bytes()
            )
            processed_payload[-1] ^= 1
            processed_path.write_bytes(processed_payload)
            manifest_path = root / "manifest.json"
            manifest_path.write_bytes(manifest_payload)
            old = evaluator.EXTERNAL_EVALUATION_MANIFEST
            evaluator.EXTERNAL_EVALUATION_MANIFEST = manifest_path
            try:
                with self.assertRaisesRegex(RuntimeError, "processed whale clip"):
                    evaluator.evaluate_external("morph")
            finally:
                evaluator.EXTERNAL_EVALUATION_MANIFEST = old


class WhaleVoiceModelCrossValidationTests(unittest.TestCase):
    def test_cross_validation_is_family_weighted_temporal_and_honest(self):
        report = evaluator.evaluate("organic")
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["family_weighting"], "equal")
        self.assertEqual(report["fold_count"], len(builder.EXPECTED_SOURCE_IDS))
        self.assertIn(
            "independent_unseen_dataset_generalization",
            report["does_not_establish"],
        )
        self.assertNotIn("roughness", report["folds"][0]["feature_distances"])
        self.assertIn("harmonic_profile_l1", report["folds"][0]["feature_distances"])
        for fold in report["folds"]:
            self.assertEqual(
                fold["excluded_from_live_selection"], [fold["source_id"]]
            )
            self.assertTrue(0.0 <= fold["similarity_score_0_to_1"] <= 1.0)
        self.assertLess(report["maximum_peak"], 0.25)


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
