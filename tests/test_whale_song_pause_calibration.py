from __future__ import annotations

import dataclasses
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import calibrate_whale_song_interphrase_pause as calibration  # noqa: E402
import whale_song_corpus as corpus_lib  # noqa: E402

CORPUS_ROOT = ROOT / "assets" / "whale-sources" / "song-corpus-v1"


class WhaleSongPauseCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.development_corpus = corpus_lib.build_split_corpus(
            CORPUS_ROOT, "development"
        )
        cls.development = corpus_lib.split_summary(
            cls.development_corpus, "development"
        )
        cls.recommendation = corpus_lib.training_recommendations(cls.development)

    def test_split_builder_reads_only_requested_annotation_payloads(self):
        original = corpus_lib._annotation_payload
        seen_splits: list[str] = []

        def guarded(root, record):
            split = record["split"]
            seen_splits.append(split)
            if split == "holdout":
                raise AssertionError("development build opened a holdout annotation")
            return original(root, record)

        with mock.patch.object(corpus_lib, "_annotation_payload", side_effect=guarded):
            development = corpus_lib.build_split_corpus(CORPUS_ROOT, "development")

        self.assertEqual(development["selected_split"], "development")
        self.assertEqual(development["record_count"], 15)
        self.assertEqual(development["phrase_count"], 1605)
        self.assertEqual(set(seen_splits), {"development"})
        self.assertEqual(len(seen_splits), 15)

    def test_holdout_split_is_separate_and_manifest_bound(self):
        bindings = corpus_lib.split_source_bindings(CORPUS_ROOT, "development")
        self.assertEqual(len(bindings), 15)
        self.assertTrue(all(2012 <= item["year"] <= 2016 for item in bindings))
        self.assertNotIn(2017, {item["year"] for item in bindings})
        self.assertNotIn(2018, {item["year"] for item in bindings})
        self.assertNotIn(2019, {item["year"] for item in bindings})
        self.assertEqual(
            self.development_corpus["source_manifest_sha256"],
            calibration.sha256_file(CORPUS_ROOT / "source-manifest.json"),
        )

    def test_pause_calibration_rejects_holdout_summary(self):
        fake = dict(self.development)
        fake["split"] = "holdout"
        with self.assertRaisesRegex(ValueError, "development split"):
            calibration.calibrate_development_pause(fake, self.recommendation)

    def test_calibration_changes_only_phrase_pause(self):
        base = calibration.config_from_projection(self.recommendation)
        result = calibration.calibrate_development_pause(
            self.development, self.recommendation
        )
        final = calibration.SongGrammarConfig(**result["final_config"])
        base_values = dataclasses.asdict(base)
        final_values = dataclasses.asdict(final)
        changed = {
            key
            for key in base_values
            if base_values[key] != final_values[key]
        }
        self.assertEqual(changed, {"phrase_pause_seconds"})
        self.assertEqual(final.theme_count, 6)
        self.assertEqual((final.phrase_repeats_min, final.phrase_repeats_max), (6, 6))
        self.assertGreater(final.phrase_pause_seconds, base.phrase_pause_seconds)
        self.assertLessEqual(final.phrase_pause_seconds, 1.19)
        self.assertLess(
            result["winner"]["development_gap_relative_absolute_error"],
            0.00001,
        )

    def test_freeze_never_requests_holdout_split(self):
        real_builder = corpus_lib.build_split_corpus
        requested: list[str] = []

        def guarded(root, split):
            requested.append(split)
            if split == "holdout":
                raise AssertionError("freeze requested holdout")
            return real_builder(root, split)

        with mock.patch.object(calibration, "build_split_corpus", side_effect=guarded):
            candidate = calibration.freeze_candidate(CORPUS_ROOT)

        self.assertEqual(requested, ["development"])
        self.assertEqual(
            candidate["freeze_contract"][
                "holdout_annotation_payload_access_during_freeze"
            ],
            "forbidden",
        )
        self.assertEqual(
            candidate["baseline_validation"]["status"],
            "deferred-until-post-freeze-holdout-evaluation",
        )
        self.assertEqual(
            {item["year"] for item in candidate["development_source_binding"]["annotation_bindings"]},
            {2012, 2013, 2014, 2015, 2016},
        )

    def test_freeze_is_deterministic_and_self_bound(self):
        first = calibration.freeze_candidate(CORPUS_ROOT)
        second = calibration.freeze_candidate(CORPUS_ROOT)
        self.assertEqual(first, second)
        calibration.validate_candidate(first)
        self.assertEqual(first["task_id"], calibration.TASK_ID)
        self.assertEqual(first["experiment_id"], calibration.EXPERIMENT_ID)
        self.assertEqual(
            set(first["code_bindings"]), set(calibration.CODE_BINDING_PATHS)
        )

    def test_gap_definition_and_metric_contract_are_explicit(self):
        candidate = calibration.freeze_candidate(CORPUS_ROOT)
        gap = candidate["gap_definition"]
        self.assertEqual(
            gap["development_reference_distribution"]["count"], 1589
        )
        self.assertAlmostEqual(
            gap["development_reference_distribution"]["mean"], 1.037118, places=6
        )
        self.assertTrue(gap["not_unit_or_intra_phrase_gap"])
        self.assertIn("not separately labelled", gap["theme_boundary_status"])
        metrics = candidate["metric_contract"]
        self.assertEqual(metrics["historical_task_wording_feature_count"], 6)
        self.assertEqual(metrics["canonical_evaluator_feature_count"], 7)
        self.assertEqual(tuple(metrics["canonical_features"]), corpus_lib.STRUCTURAL_FEATURES)
        self.assertIn("do not omit", metrics["resolution"])

    def test_historical_values_are_evidence_not_thresholds(self):
        candidate = calibration.freeze_candidate(CORPUS_ROOT)
        historical = candidate["historical_task_intake"]
        self.assertAlmostEqual(
            historical["paused_gap_relative_error"],
            0.22786836858667633,
        )
        self.assertAlmostEqual(
            historical["unpaused_baseline_gap_relative_error"],
            0.08101189807106207,
        )
        self.assertFalse(historical["use_as_acceptance_threshold"])


if __name__ == "__main__":
    unittest.main()
