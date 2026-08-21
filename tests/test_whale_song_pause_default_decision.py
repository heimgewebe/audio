from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from whale_song_grammar import SongGrammarConfig  # noqa: E402

DECISION_PATH = (
    ROOT
    / "assets"
    / "whale-sources"
    / "song-corpus-v1"
    / "pause-calibration-v1"
    / "default-decision-v1.json"
)
CANDIDATE_PATH = DECISION_PATH.parent / "candidate.json"
HOLDOUT_PATH = DECISION_PATH.parent / "holdout-evaluation.json"
BASELINE_PATH = DECISION_PATH.parents[1] / "evaluation.json"
T043_DOC_PATH = ROOT / "docs" / "experiments" / "buckelwal-song-hierarchy-controlled-blind-v1.md"
GRAMMAR_PATH = SCRIPTS / "whale_song_grammar.py"


def _load(path: pathlib.Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain one JSON object")
    return value


def _sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class WhaleSongPauseDefaultDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decision = _load(DECISION_PATH)
        self.candidate = _load(CANDIDATE_PATH)
        self.holdout = _load(HOLDOUT_PATH)

    def test_decision_internal_sha256_is_stable(self) -> None:
        expected = self.decision.get("decision_sha256")
        self.assertIsInstance(expected, str)
        unhashed = dict(self.decision)
        unhashed.pop("decision_sha256")
        self.assertEqual(_sha256_json(unhashed), expected)

    def test_decision_binds_frozen_t044_artifacts(self) -> None:
        candidate_binding = self.decision["evaluated_candidate"]
        self.assertEqual(candidate_binding["phrase_pause_seconds"], 0.947703)
        self.assertEqual(candidate_binding["candidate_sha256"], self.candidate["candidate_sha256"])
        self.assertEqual(candidate_binding["candidate_file_sha256"], _sha256_file(CANDIDATE_PATH))

        t044 = self.decision["source_evidence"]["t044_holdout_calibration"]
        self.assertEqual(t044["holdout_evaluation_sha256"], self.holdout["evaluation_sha256"])
        self.assertEqual(t044["holdout_evaluation_file_sha256"], _sha256_file(HOLDOUT_PATH))
        self.assertEqual(t044["baseline_evaluation_file_sha256"], _sha256_file(BASELINE_PATH))
        self.assertEqual(t044["holdout_evaluation_ordinal"], 1)
        self.assertEqual(self.holdout["holdout_evaluation_ordinal"], 1)

    def test_structural_evidence_reports_all_current_features(self) -> None:
        structural = self.decision["structural_evidence"]
        per_feature = self.holdout["per_feature"]
        self.assertEqual(structural["feature_count"], 7)
        self.assertEqual(len(per_feature), 7)
        self.assertEqual(
            set(structural["per_feature_calibrated_relative_absolute_error"]),
            set(per_feature),
        )
        for feature, metrics in per_feature.items():
            self.assertEqual(
                structural["per_feature_calibrated_relative_absolute_error"][feature],
                metrics["calibrated_relative_absolute_error"],
            )
        holdout_result = self.holdout["holdout_result"]
        overall = structural["overall_mean_relative_absolute_error"]
        self.assertEqual(overall["calibrated"], holdout_result["calibrated"])
        self.assertEqual(
            overall["historical_development_projection"],
            holdout_result["historical_development_projection"],
        )
        self.assertEqual(overall["default"], holdout_result["default"])

    def test_perceptual_evidence_remains_indeterminate_without_bound_real_responses(self) -> None:
        t043 = self.decision["source_evidence"]["t043_controlled_blind_protocol"]
        self.assertEqual(t043["perceptual_result"], "indeterminate")
        self.assertEqual(t043["real_frozen_human_responses_bound_to_t043_receipt"], 0)
        t043_doc = T043_DOC_PATH.read_text(encoding="utf-8")
        self.assertIn("`response-template.json` startet mit einer leeren Antwortliste", t043_doc)
        self.assertIn("perceptuelle Ergebnis zwingend `indeterminate`", t043_doc)

    def test_positive_structural_result_does_not_authorize_default_promotion(self) -> None:
        holdout_result = self.holdout["holdout_result"]
        self.assertTrue(holdout_result["calibrated_improves_default"])
        self.assertTrue(holdout_result["calibrated_improves_historical_projection"])
        decision = self.decision["decision"]
        self.assertEqual(decision["verdict"], "retain_study_candidate")
        self.assertFalse(decision["default_promotion_authorized"])
        current_default = decision["current_default_observed"]
        self.assertEqual(current_default["voice_mode"], "morph")
        self.assertEqual(
            current_default["phrase_pause_seconds"], SongGrammarConfig().phrase_pause_seconds
        )
        self.assertEqual(current_default["grammar_source_file_sha256"], _sha256_file(GRAMMAR_PATH))

    def test_holdout_cannot_be_reused_for_retuning_or_second_t044_evaluation(self) -> None:
        contract = self.decision["holdout_reuse_contract"]
        self.assertFalse(contract["tuning_allowed"])
        self.assertFalse(contract["threshold_selection_allowed"])
        self.assertFalse(contract["candidate_selection_allowed"])
        self.assertFalse(contract["second_t044_holdout_evaluation_allowed"])
        self.assertTrue(self.decision["future_reconsideration"]["requires_new_decision"])
        self.assertTrue(
            self.decision["future_reconsideration"]["promotion_implementation_must_be_separate"]
        )


if __name__ == "__main__":
    unittest.main()
