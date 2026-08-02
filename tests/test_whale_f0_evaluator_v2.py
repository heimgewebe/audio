from __future__ import annotations

import importlib
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

evaluator = importlib.import_module("evaluate_whale_f0_v2")
study = importlib.import_module("study_whale_evaluator_v2")


class WhaleF0EvaluatorV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.definition, cls.definition_sha256 = evaluator.load_definition()
        cls.controlled = study.build_controlled_report()
        cls.external = study.build_external_report()
        cls.controlled_path = (
            ROOT
            / "assets"
            / "whale-sources"
            / "studies"
            / "evaluator-v2"
            / "reference-corpus.json"
        )
        cls.external_path = cls.controlled_path.with_name("sensitivity-report.json")

    def test_definition_is_frozen_before_external_evaluation(self) -> None:
        self.assertEqual(
            self.definition["kind"], "humpback_whale_f0_evaluator_v2_definition"
        )
        self.assertTrue(self.definition["frozen_before_external_evaluation"])
        self.assertFalse(self.definition["external_data_used_for_parameter_selection"])
        amendment = self.definition["methodology_amendment"]
        self.assertEqual(
            amendment["stage"], "controlled-development-before-any-external-v2-evaluation"
        )
        self.assertFalse(amendment["external_results_observed"])
        self.assertEqual(
            amendment["previous_definition_commit"],
            "a8d15d16e17ee8196e76aca39d17d530001ec435",
        )
        self.assertEqual(
            amendment["previous_definition_sha256"],
            "5d5d265cf11b18db708252767c60be927f7cf34d9fb42283355d106c72c26670",
        )
        self.assertEqual(
            study.FROZEN_DEFINITION_COMMIT,
            "bfe237b4fa21a89a712ad49b4bde709ab46d6106",
        )
        self.assertEqual(study.definition_commit(), study.FROZEN_DEFINITION_COMMIT)

    def test_numeric_contract_remains_locked(self) -> None:
        analysis = self.definition["analysis"]
        self.assertEqual(analysis["f0_search_hz"], [28.0, 800.0])
        self.assertEqual(analysis["minimum_periodicity"], 0.38)
        self.assertEqual(analysis["minimum_peak_prominence"], 0.04)
        self.assertEqual(analysis["octave_score_ratio"], 0.88)
        self.assertEqual(analysis["boundary_guard_lags"], 1)
        self.assertEqual(analysis["octave_candidate_multiples_role"], "diagnostic-only")

    def test_controlled_reference_corpus_passes(self) -> None:
        self.assertTrue(self.controlled["all_pass"])
        cases = {case["id"]: case for case in self.controlled["cases"]}
        self.assertEqual(cases["tone-80"]["result"]["summary"]["median_f0_hz"], 80.0)
        self.assertIsNone(
            cases["white-noise"]["result"]["summary"]["median_f0_hz"]
        )
        self.assertEqual(
            cases["white-noise"]["result"]["summary"]["voiced_fraction"], 0.0
        )
        boundary = cases["upper-bound-tone-800"]["result"]["summary"]
        self.assertEqual(boundary["voiced_boundary_hits"], 0)
        self.assertEqual(boundary["reason_counts"], {"search-boundary-high": 48})
        whale = cases["independent-whale-annotation-105hz"]
        self.assertTrue(whale["assessment"]["pass"])
        self.assertEqual(
            whale["expectation"]["source_sha256"],
            "6bbbd349a623e685850b9e6fe2fd6a345a28c2dc0e9350c96c683075d53c8767",
        )

    def test_octave_candidates_are_diagnostics(self) -> None:
        cases = {case["id"]: case for case in self.controlled["cases"]}
        frames = cases["tone-520"]["result"]["frames"]
        self.assertTrue(any(frame["octave_candidate_lags"] for frame in frames))
        self.assertTrue(all(frame["selected_lag"] == 8 for frame in frames[8:-8]))
        for frame in frames:
            strongest = frame["strongest_lag"]
            for candidate in frame["octave_candidate_lags"]:
                ratio = max(candidate, strongest) / min(candidate, strongest)
                self.assertIn(round(ratio), (1, 2, 3))

    def test_locked_external_sensitivity_contract_passes(self) -> None:
        self.assertTrue(self.external["locked_test_only"])
        self.assertFalse(self.external["parameter_or_threshold_selection_from_external"])
        self.assertTrue(self.external["pass"])
        checks = self.external["checks"]
        self.assertEqual(
            checks["observed_legacy_stellwagen_voiced_counts"], [36, 40, 34]
        )
        self.assertEqual(
            checks["expected_legacy_stellwagen_voiced_counts"], [36, 40, 34]
        )
        self.assertEqual(checks["v2_voiced_boundary_hits"], 0)
        self.assertTrue(checks["v2_boundary_contract_pass"])
        corrections = {
            item["id"]: item
            for item in self.external["post_external_implementation_corrections"]
        }
        self.assertEqual(
            set(corrections),
            {
                "legacy-voiced-count-semantics",
                "octave-multiplier-definition-conformance",
            },
        )
        for correction in corrections.values():
            self.assertFalse(correction["parameter_or_threshold_change"])

    def test_external_results_remain_recording_and_segment_visible(self) -> None:
        self.assertEqual(len(self.external["segments"]), 9)
        recordings = {
            item["source_recording_id"]: item for item in self.external["recordings"]
        }
        self.assertEqual(set(recordings), {"alaska-winter-1999", "stellwagen", "american-samoa"})
        self.assertEqual(recordings["stellwagen"]["segment_count"], 4)
        self.assertEqual(recordings["stellwagen"]["legacy_lag_3_voiced_hits"], 126)
        self.assertEqual(recordings["stellwagen"]["v2_voiced_boundary_hits"], 0)
        self.assertEqual(recordings["alaska-winter-1999"]["v2_mean_voiced_fraction"], 0.0)

    def test_samoa_high_band_is_unavailable_without_reweighting(self) -> None:
        samoa = [
            item
            for item in self.external["segments"]
            if item["source_recording_id"] == "american-samoa"
        ]
        self.assertEqual(len(samoa), 4)
        for segment in samoa:
            bandwidth = segment["v2"]["bandwidth"]
            self.assertEqual(bandwidth["source_nyquist_hz"], 2500.0)
            self.assertEqual(bandwidth["f0_voicing"], "available")
            self.assertEqual(bandwidth["high_band_ratio"], "unavailable")
            self.assertEqual(
                bandwidth["missing_feature_policy"],
                "unavailable-not-imputed-or-reweighted",
            )

    def test_reports_are_byte_reproducible(self) -> None:
        self.assertEqual(
            study.canonical_json(self.controlled), self.controlled_path.read_bytes()
        )
        self.assertEqual(
            study.canonical_json(self.external), self.external_path.read_bytes()
        )
        committed_controlled = json.loads(self.controlled_path.read_text())
        committed_external = json.loads(self.external_path.read_text())
        self.assertEqual(
            committed_controlled["definition"]["sha256"], self.definition_sha256
        )
        self.assertEqual(
            committed_external["definition"]["sha256"], self.definition_sha256
        )


if __name__ == "__main__":
    unittest.main()
