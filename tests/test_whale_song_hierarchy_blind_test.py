#!/usr/bin/env python3
from __future__ import annotations

import collections
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_whale_song_hierarchy_blind_test as controlled  # noqa: E402
from whale_song_grammar import iter_units, plan_sha256  # noqa: E402

CORPUS_ROOT = ROOT / "assets" / "whale-sources" / "song-corpus-v1"


class WhaleSongHierarchyBlindTest(unittest.TestCase):
    def test_condition_plans_keep_exact_units_and_order(self):
        structured, flat, control = controlled.build_condition_plans(
            CORPUS_ROOT,
            seconds=30.0,
        )
        structured_units = list(iter_units(structured))
        flat_units = list(iter_units(flat))
        self.assertGreaterEqual(len(structured_units), 3)
        self.assertEqual(
            [controlled._unit_identity(unit) for unit in structured_units],
            [controlled._unit_identity(unit) for unit in flat_units],
        )
        self.assertEqual(
            [unit.unit_id for unit in structured_units],
            [unit.unit_id for unit in flat_units],
        )
        self.assertTrue(control["same_concrete_unit_inventory"])
        self.assertTrue(control["same_unit_order"])
        self.assertTrue(control["same_total_inter_block_pause_budget"])
        self.assertIn("transition", control["source_phrase_roles"])
        self.assertEqual(
            control["structured_duration_seconds"],
            control["flat_boundary_duration_seconds"],
        )
        source_boundaries = [
            item["boundary_pause_seconds"] for item in control["source_boundaries"]
        ]
        self.assertGreater(len(set(source_boundaries)), 1)
        self.assertTrue(
            any(
                abs(value - control["flat_boundary_pause_seconds"]) > 1.0e-6
                for value in source_boundaries
            )
        )
        self.assertNotEqual(
            [unit.start_seconds for unit in structured_units],
            [unit.start_seconds for unit in flat_units],
        )
        self.assertNotEqual(plan_sha256(structured), plan_sha256(flat))
        self.assertLessEqual(control["render_seconds"], 30.0)

    def test_short_window_without_before_transition_after_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "too short"):
            controlled.build_condition_plans(CORPUS_ROOT, seconds=10.0)

    def test_trial_schedule_is_deterministic_and_counterbalanced(self):
        seed = bytes.fromhex("11" * 32)
        left = controlled._trial_schedule(seed, 4)
        right = controlled._trial_schedule(seed, 4)
        self.assertEqual(left, right)
        a_assignments = collections.Counter(
            item["assignment"]["A"] for item in left
        )
        self.assertEqual(
            a_assignments,
            collections.Counter(
                {"structured_timing": 2, "flat_boundary_timing": 2}
            ),
        )
        presentation_orders = collections.Counter(
            tuple(item["presentation_order"]) for item in left
        )
        self.assertEqual(
            presentation_orders,
            collections.Counter({("A", "B"): 2, ("B", "A"): 2}),
        )
        first_conditions = collections.Counter(
            item["assignment"][item["presentation_order"][0]] for item in left
        )
        self.assertEqual(
            first_conditions,
            collections.Counter(
                {"structured_timing": 2, "flat_boundary_timing": 2}
            ),
        )
        with self.assertRaises(ValueError):
            controlled._trial_schedule(seed, 2)
        with self.assertRaises(ValueError):
            controlled._trial_schedule(b"short", 4)

    def test_controlled_builder_writes_anonymous_level_matched_protocol(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            manifest, answer, responses = controlled.build_controlled_blind_test(
                root,
                corpus_root=CORPUS_ROOT,
                seconds=16.0,
                gain=0.06,
                trials=4,
                assignment_seed=bytes.fromhex("22" * 32),
            )
            self.assertEqual(
                manifest["protocol"],
                "matched-inventory-boundary-timing-ablation-v1",
            )
            self.assertEqual(manifest["perceptual_result"]["status"], "indeterminate")
            self.assertEqual(responses["responses"], [])
            self.assertEqual(
                responses["perceptual_result_without_responses"], "indeterminate"
            )
            self.assertEqual(
                manifest["pair_identity_sha256"], answer["pair_identity_sha256"]
            )
            self.assertNotIn("trial_assignments", manifest)
            self.assertNotIn("condition_plans", manifest)
            self.assertNotIn("assignment_seed_hex", manifest)
            self.assertEqual(answer["assignment_seed_hex"], "22" * 32)
            self.assertEqual(
                manifest["assignment_seed_sha256"],
                answer["assignment_seed_sha256"],
            )
            self.assertNotEqual(
                manifest["assignment_seed_sha256"],
                answer["assignment_seed_hex"],
            )
            self.assertEqual(len(manifest["trials"]), 4)
            self.assertEqual(len(answer["trial_assignments"]), 4)

            wav_hashes: set[str] = set()
            for trial in manifest["trials"]:
                serialized_trial = json.dumps(trial, sort_keys=True)
                self.assertNotIn("structured_timing", serialized_trial)
                self.assertNotIn("flat_boundary_timing", serialized_trial)
                self.assertEqual(set(trial["samples"]), {"A", "B"})
                a_metrics = trial["samples"]["A"]["signal_metrics"]
                b_metrics = trial["samples"]["B"]["signal_metrics"]
                self.assertAlmostEqual(a_metrics["rms"], b_metrics["rms"], places=9)
                for label in ("A", "B"):
                    sample = trial["samples"][label]
                    path = root / sample["file"]
                    self.assertTrue(path.is_file())
                    self.assertGreater(path.stat().st_size, 44)
                    self.assertLessEqual(
                        sample["signal_metrics"]["level_match_scale"], 1.0
                    )
                    self.assertLessEqual(sample["signal_metrics"]["peak"], 1.0)
                    self.assertLessEqual(
                        sample["signal_metrics"]["peak"],
                        sample["signal_metrics"]["source_peak_before_level_match"]
                        + 1.0e-12,
                    )
                    wav_hashes.add(sample["sha256"])
            self.assertEqual(len(wav_hashes), 2)
            self.assertTrue((root / "blind-manifest.json").is_file())
            self.assertTrue((root / "answer-key.json").is_file())
            self.assertTrue((root / "response-template.json").is_file())

            assignments = answer["trial_assignments"]
            self.assertEqual(
                collections.Counter(item["assignment"]["A"] for item in assignments),
                collections.Counter(
                    {"structured_timing": 2, "flat_boundary_timing": 2}
                ),
            )
            self.assertTrue(
                manifest["stimulus_control"]["same_concrete_unit_inventory"]
            )
            self.assertTrue(manifest["stimulus_control"]["same_unit_order"])
            self.assertTrue(
                manifest["stimulus_control"]["same_total_inter_block_pause_budget"]
            )
            self.assertFalse(
                manifest["level_and_duration_control"]["amplification_allowed"]
            )
            self.assertFalse(
                manifest["level_and_duration_control"]["clipping_allowed"]
            )


if __name__ == "__main__":
    unittest.main()
