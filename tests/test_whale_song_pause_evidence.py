from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import calibrate_whale_song_interphrase_pause as calibration  # noqa: E402
import whale_song_corpus as corpus_lib  # noqa: E402

EVIDENCE_ROOT = (
    ROOT
    / "assets"
    / "whale-sources"
    / "song-corpus-v1"
    / "pause-calibration-v1"
)
CANDIDATE_PATH = EVIDENCE_ROOT / "candidate.json"
HOLDOUT_PATH = EVIDENCE_ROOT / "holdout-evaluation.json"


class WhaleSongPauseEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candidate = json.loads(CANDIDATE_PATH.read_text())
        cls.evaluation = json.loads(HOLDOUT_PATH.read_text())

    def test_frozen_candidate_is_self_and_code_bound(self):
        calibration.validate_candidate(self.candidate)
        self.assertEqual(
            self.candidate["candidate_sha256"],
            "f343b326b149f0ea4b6c76bb0a0db0123eb8a7e91ca0c84a500f05a0ca22521e",
        )
        self.assertEqual(self.candidate["final_config"]["phrase_pause_seconds"], 0.947703)
        self.assertEqual(self.candidate["development_source_binding"]["record_count"], 15)
        self.assertEqual(self.candidate["development_source_binding"]["phrase_count"], 1605)

    def test_holdout_evaluation_is_self_bound_and_single_ordinal(self):
        expected = self.evaluation["evaluation_sha256"]
        payload = dict(self.evaluation)
        payload.pop("evaluation_sha256")
        self.assertEqual(calibration.sha256_json(payload), expected)
        self.assertEqual(
            expected,
            "dd1f4f916dee9210006eb38a08a393bec7f29cecd55e26ae46bac5f60c8ac691",
        )
        self.assertEqual(self.evaluation["candidate_sha256"], self.candidate["candidate_sha256"])
        self.assertEqual(self.evaluation["holdout_evaluation_ordinal"], 1)
        self.assertFalse(self.evaluation["split_contract"]["holdout_used_for_selection"])
        self.assertFalse(
            self.evaluation["split_contract"]["post_holdout_tuning_within_same_experiment"]
        )

    def test_all_current_metrics_are_reported_without_regression(self):
        metrics = self.evaluation["per_feature"]
        self.assertEqual(set(metrics), set(corpus_lib.STRUCTURAL_FEATURES))
        self.assertEqual(len(metrics), 7)
        for values in metrics.values():
            self.assertLessEqual(
                values["calibrated_relative_absolute_error"],
                values["historical_projection_relative_absolute_error"],
            )
            self.assertLess(
                values["calibrated_relative_absolute_error"],
                values["default_relative_absolute_error"],
            )

    def test_holdout_improves_gap_and_all_feature_mean(self):
        result = self.evaluation["holdout_result"]
        self.assertEqual(result["feature_count"], 7)
        self.assertEqual(result["historical_development_projection"], 0.270037)
        self.assertEqual(result["calibrated"], 0.241312)
        self.assertTrue(result["calibrated_improves_historical_projection"])
        self.assertTrue(result["calibrated_improves_default"])
        gap = result["interphrase_gap"]
        self.assertEqual(gap["historical_projection_relative_absolute_error"], 0.227868)
        self.assertEqual(gap["default_relative_absolute_error"], 0.081012)
        self.assertEqual(gap["calibrated_relative_absolute_error"], 0.046549)

    def test_oracle_is_the_revision_bound_existing_baseline(self):
        oracle = self.evaluation["oracle_binding"]
        self.assertEqual(
            oracle["baseline_evaluation_sha256"],
            "435e2c3132ea393c22a18c2dac78dd68b4d3f04917fb076a08f17e4476136617",
        )
        self.assertEqual(oracle["feature_count"], 7)
        self.assertEqual(set(oracle["canonical_features"]), set(corpus_lib.STRUCTURAL_FEATURES))
        self.assertEqual(set(oracle["comparison_contract"]), set(corpus_lib.STRUCTURAL_FEATURES))

    def test_evidence_does_not_change_or_authorize_defaults(self):
        decision = self.evaluation["decision_boundary"]
        self.assertFalse(decision["changes_live_or_default"])
        self.assertTrue(decision["separate_default_decision_required_if_evidence_warrants"])
        self.assertTrue(decision["this_evaluation_does_not_authorize_default_change"])
        profile = json.loads((ROOT / "profiles/buckelwal-live-voice-v1.json").read_text())
        self.assertEqual(profile["default_voice_mode"], "morph")

    def test_canonical_holdout_output_cannot_be_reused_by_cli(self):
        with self.assertRaisesRegex(RuntimeError, "single-use"):
            calibration.main(
                [
                    "evaluate",
                    "--candidate",
                    str(CANDIDATE_PATH),
                    "--output",
                    str(HOLDOUT_PATH),
                ]
            )


if __name__ == "__main__":
    unittest.main()
