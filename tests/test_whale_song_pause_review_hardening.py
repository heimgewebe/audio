from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import calibrate_whale_song_interphrase_pause as calibration  # noqa: E402

CORPUS_ROOT = ROOT / "assets" / "whale-sources" / "song-corpus-v1"
CANDIDATE_PATH = CORPUS_ROOT / "pause-calibration-v1" / "candidate.json"
BASELINE_PATH = CORPUS_ROOT / "evaluation.json"


class WhaleSongPauseReviewHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candidate = json.loads(CANDIDATE_PATH.read_text())

    def test_canonical_frozen_candidate_survives_validator_only_hardening(self):
        calibration.validate_candidate(self.candidate)
        self.assertEqual(
            self.candidate["candidate_sha256"], calibration.FROZEN_V1_CANDIDATE_SHA256
        )
        current = calibration.code_bindings()
        frozen = calibration.FROZEN_V1_CODE_BINDINGS
        self.assertNotEqual(
            current[calibration.CALIBRATION_SCRIPT_BINDING_PATH],
            frozen[calibration.CALIBRATION_SCRIPT_BINDING_PATH],
        )
        for relative in calibration.CODE_BINDING_PATHS:
            if relative == calibration.CALIBRATION_SCRIPT_BINDING_PATH:
                continue
            self.assertEqual(current[relative], frozen[relative])

    def test_repository_relative_baseline_is_normalized_before_use(self):
        relative_root = CORPUS_ROOT.relative_to(ROOT)
        relative_baseline = BASELINE_PATH.relative_to(ROOT)
        root, baseline, loaded = calibration.validate_evaluation_inputs(
            self.candidate, relative_root, relative_baseline
        )
        self.assertEqual(root, CORPUS_ROOT)
        self.assertEqual(baseline, BASELINE_PATH)
        self.assertEqual(
            loaded["evaluation_sha256"],
            "435e2c3132ea393c22a18c2dac78dd68b4d3f04917fb076a08f17e4476136617",
        )

    def test_out_of_repository_baseline_is_rejected_before_holdout_access(self):
        with self.assertRaisesRegex(ValueError, "inside the repository"):
            calibration.validate_evaluation_inputs(
                self.candidate,
                CORPUS_ROOT,
                pathlib.Path("/tmp/t044-other-evaluation.json"),
            )

    def test_existing_canonical_holdout_blocks_direct_re_evaluation(self):
        with mock.patch.object(
            calibration,
            "build_split_corpus",
            side_effect=AssertionError("holdout must not be reopened"),
        ) as builder:
            with self.assertRaisesRegex(RuntimeError, "globally single-use"):
                calibration.evaluate_frozen_candidate(
                    self.candidate, CORPUS_ROOT, BASELINE_PATH
                )
        builder.assert_not_called()

    def test_mismatched_holdout_manifest_is_rejected_before_baseline_or_holdout(self):
        real_sha = calibration.sha256_file

        def mismatched(path: pathlib.Path) -> str:
            if path.name == "source-manifest.json":
                return "0" * 64
            return real_sha(path)

        with mock.patch.object(calibration, "sha256_file", side_effect=mismatched):
            with mock.patch.object(
                calibration,
                "load_baseline_evaluation",
                side_effect=AssertionError("baseline must not be read after manifest mismatch"),
            ) as baseline_loader:
                with self.assertRaisesRegex(ValueError, "holdout corpus source manifest mismatch"):
                    calibration.validate_evaluation_inputs(
                        self.candidate, CORPUS_ROOT, BASELINE_PATH
                    )
        baseline_loader.assert_not_called()

    def test_alternate_baseline_revision_is_rejected_before_empirical_access(self):
        alternate = calibration.load_baseline_evaluation(BASELINE_PATH)
        alternate = dict(alternate)
        alternate["evaluation_sha256"] = "2" * 64
        with mock.patch.object(
            calibration, "load_baseline_evaluation", return_value=alternate
        ):
            with mock.patch.object(
                calibration,
                "load_empirical_structure",
                side_effect=AssertionError("empirical data must not be read for the wrong oracle"),
            ) as empirical_loader:
                with self.assertRaisesRegex(ValueError, "frozen T044 oracle revision"):
                    calibration.validate_evaluation_inputs(
                        self.candidate, CORPUS_ROOT, BASELINE_PATH
                    )
        empirical_loader.assert_not_called()

    def test_baseline_identity_is_bound_to_same_source_manifest(self):
        empirical = calibration.load_empirical_structure(
            CORPUS_ROOT / calibration.EMPIRICAL_STRUCTURE_NAME
        )
        mismatched = dict(empirical)
        mismatched["source_manifest_sha256"] = "1" * 64
        with mock.patch.object(
            calibration, "load_empirical_structure", return_value=mismatched
        ):
            with self.assertRaisesRegex(ValueError, "candidate and baseline source manifest mismatch"):
                calibration.validate_evaluation_inputs(
                    self.candidate, CORPUS_ROOT, BASELINE_PATH
                )

    def test_existing_canonical_holdout_blocks_alternate_output_before_read(self):
        alternate = ROOT / ".t044-alternate-holdout.json"
        self.assertFalse(alternate.exists())
        with mock.patch.object(
            calibration,
            "load_candidate",
            side_effect=AssertionError("candidate must not be reread after one-shot holdout"),
        ) as candidate_loader:
            with self.assertRaisesRegex(RuntimeError, "globally single-use"):
                calibration.main(
                    [
                        "evaluate",
                        "--candidate",
                        str(CANDIDATE_PATH),
                        "--output",
                        str(alternate),
                    ]
                )
        candidate_loader.assert_not_called()
        self.assertFalse(alternate.exists())


if __name__ == "__main__":
    unittest.main()
