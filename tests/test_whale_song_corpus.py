from __future__ import annotations

import collections
import dataclasses
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_whale_song_blind_test as blind  # noqa: E402
import evaluate_whale_song_grammar_structure as evaluation  # noqa: E402
import whale_live  # noqa: E402
import whale_song_corpus as corpus_lib  # noqa: E402
from whale_song_grammar import (  # noqa: E402
    SongGrammarConfig,
    WhaleSongGrammar,
    iter_units,
    plan_sha256,
)

CORPUS_ROOT = ROOT / "assets" / "whale-sources" / "song-corpus-v1"


class WhaleSongCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = corpus_lib.build_corpus(CORPUS_ROOT)
        cls.development = corpus_lib.split_summary(cls.corpus, "development")
        cls.holdout = corpus_lib.split_summary(cls.corpus, "holdout")

    def test_source_manifest_is_cc_by_and_temporally_frozen(self):
        manifest = corpus_lib.load_source_manifest(CORPUS_ROOT)
        self.assertEqual(manifest["dataset"]["license"], "CC BY 4.0")
        self.assertEqual(manifest["split"]["development_years"], [2012, 2013, 2014, 2015, 2016])
        self.assertEqual(manifest["split"]["holdout_years"], [2017, 2018, 2019])
        self.assertEqual(len(manifest["records"]), 26)
        self.assertTrue(all(not record["audio_external"]["downloaded_into_repository"] for record in manifest["records"]))

    def test_each_record_split_must_match_its_frozen_year(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary) / "corpus"
            shutil.copytree(CORPUS_ROOT, target)
            manifest_path = target / "source-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            holdout_record = next(
                record for record in manifest["records"] if record["year"] == 2018
            )
            holdout_record["split"] = "development"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                corpus_lib.load_source_manifest(target)

    def test_all_26_annotation_files_are_hash_bound(self):
        manifest = corpus_lib.load_source_manifest(CORPUS_ROOT)
        seen = set()
        for record in manifest["records"]:
            path, payload = corpus_lib._annotation_payload(CORPUS_ROOT, record)
            self.assertTrue(path.is_file())
            self.assertTrue(payload)
            seen.add(path.name)
        self.assertEqual(len(seen), 26)

    def test_published_category_nomenclature_preserves_code_without_unit_decoding(self):
        parsed = corpus_lib.parse_category("Ii11312")
        self.assertEqual(parsed.phrase_type, "Ii")
        self.assertEqual(parsed.repetition_code, "11312")
        self.assertEqual(parsed.status, "repetition-code-preserved-unparsed")
        self.assertNotIn("unit", dataclasses.asdict(parsed))
        with_zero = corpus_lib.parse_category("Ib210")
        self.assertEqual(with_zero.phrase_type, "Ib")
        self.assertEqual(with_zero.repetition_code, "210")
        self.assertEqual(with_zero.status, "repetition-code-preserved-unparsed")

    def test_phrase_only_and_unknown_categories_do_not_invent_units(self):
        phrase_only = corpus_lib.parse_category("Ha")
        self.assertEqual(phrase_only.phrase_type, "Ha")
        self.assertIsNone(phrase_only.repetition_code)
        self.assertEqual(phrase_only.status, "phrase-only")
        unknown = corpus_lib.parse_category("?")
        self.assertIsNone(unknown.phrase_type)
        self.assertIsNone(unknown.repetition_code)
        self.assertEqual(unknown.status, "unclassified")

    def test_corpus_matches_released_selection_table_rows(self):
        self.assertEqual(self.corpus["record_count"], 26)
        self.assertEqual(self.corpus["phrase_count"], 2312)
        self.assertEqual(self.development["record_count"], 15)
        self.assertEqual(self.development["phrase_count"], 1605)
        self.assertEqual(self.holdout["record_count"], 11)
        self.assertEqual(self.holdout["phrase_count"], 707)
        self.assertAlmostEqual(
            self.development["feature_vector"]["mean_published_units_per_song"],
            158.382105,
            places=6,
        )
        self.assertAlmostEqual(
            self.holdout["feature_vector"]["mean_published_units_per_song"],
            184.772174,
            places=6,
        )
        self.assertEqual(self.development["published_song_count"], 38)
        self.assertEqual(self.holdout["published_song_count"], 23)
        self.assertAlmostEqual(
            self.development["feature_vector"]["mean_phrases_per_published_song"],
            42.236842,
            places=6,
        )
        self.assertAlmostEqual(
            self.holdout["feature_vector"]["mean_phrases_per_published_song"],
            30.739130,
            places=6,
        )
        self.assertIn("published song count", self.development["aggregation_contract"]["published_units_per_song"])

        self.assertNotIn(
            "published_units_per_song", self.development["feature_distributions"]
        )
        self.assertIn(
            "published_units_per_song",
            self.development["recording_equal_weight_summaries"],
        )

    def test_unit_timestamps_are_never_fabricated(self):
        phrases = [phrase for record in self.corpus["records"] for phrase in record["phrases"]]
        self.assertTrue(phrases)
        self.assertTrue(all(phrase["unit_timing"] == "unobserved" for phrase in phrases))
        serialized = json.dumps(phrases, sort_keys=True)
        self.assertNotIn("unit_start_seconds", serialized)
        self.assertNotIn("reconstructed_unit_count", serialized)
        self.assertNotIn("repetition_counts", serialized)
        self.assertIn("per-unit timestamp boundaries", self.corpus["does_not_establish"])
        self.assertIn(
            "unit sequence/count reconstruction from the raw repetition-code suffix alone",
            self.corpus["does_not_establish"],
        )

    def test_only_one_release_table_requires_chronological_normalization(self):
        reordered = [
            record["summary"]["recording_id"]
            for record in self.corpus["records"]
            if record["summary"]["source_table_reordered"]
        ]
        self.assertEqual(reordered, ["HS140716-1131-ESM"])
        record = next(record for record in self.corpus["records"] if record["summary"]["recording_id"] == reordered[0])
        starts = [phrase["begin_seconds"] for phrase in record["phrases"]]
        self.assertEqual(starts, sorted(starts))
        self.assertGreater(record["summary"]["source_rows_moved_by_time_sort"], 0)
        self.assertEqual(record["summary"]["overlap_phrase_count"], 0)

    def test_development_and_holdout_are_materially_different(self):
        dev = self.development["feature_vector"]
        hold = self.holdout["feature_vector"]
        self.assertGreater(hold["mean_phrase_duration_seconds"], dev["mean_phrase_duration_seconds"])
        self.assertGreater(hold["mean_published_units_per_song"], dev["mean_published_units_per_song"])
        self.assertNotEqual(hold["mean_theme_sequence_length"], dev["mean_theme_sequence_length"])

    def test_training_projection_uses_development_only_and_is_valid(self):
        recommendation = corpus_lib.training_recommendations(self.development)
        self.assertFalse(recommendation["uses_holdout"])
        self.assertEqual(recommendation["source_split"], "development")
        self.assertEqual(recommendation["candidate_count"], 127)
        self.assertEqual(len(recommendation["model_ensemble_seeds"]), 8)
        self.assertEqual(
            len(set(recommendation["model_ensemble_seeds"])), 8
        )
        projected = recommendation["projected_current_config"]
        config = SongGrammarConfig(
            theme_count=projected["theme_count"],
            phrase_repeats_min=projected["phrase_repeats_min"],
            phrase_repeats_max=projected["phrase_repeats_max"],
            phrase_pause_seconds=projected["phrase_pause_seconds"],
        )
        self.assertEqual(config.theme_count, 6)
        self.assertEqual((config.phrase_repeats_min, config.phrase_repeats_max), (6, 6))
        self.assertAlmostEqual(config.phrase_pause_seconds, 0.716662, places=6)
        self.assertTrue(
            recommendation["clamped_or_jointly_constrained"][
                "search_space_enforces_joint_unit_budget"
            ]
        )

    def test_training_projection_rejects_holdout_input(self):
        with self.assertRaises(ValueError):
            corpus_lib.training_recommendations(self.holdout)

    def test_training_projection_is_independent_of_holdout_object(self):
        first = corpus_lib.training_recommendations(self.development)
        mutated_corpus = json.loads(json.dumps(self.corpus))
        for record in mutated_corpus["records"]:
            if record["summary"]["split"] == "holdout":
                record["summary"]["published_song_count"] = 999999
                record["phrases"] = []
        second_development = corpus_lib.split_summary(mutated_corpus, "development")
        second = corpus_lib.training_recommendations(second_development)
        self.assertEqual(first, second)

    def test_structural_evaluation_is_frozen_and_holdout_improves(self):
        empirical, report = evaluation.build_reports(CORPUS_ROOT)
        self.assertEqual(empirical["development"], self.development)
        self.assertEqual(empirical["holdout"], self.holdout)
        self.assertFalse(report["split_contract"]["holdout_used_for_selection"])
        result = report["holdout_result"]
        self.assertTrue(result["lower_is_better"])
        self.assertTrue(result["fitted_improves_holdout"])
        self.assertLess(result["development_fitted"], result["default"])
        self.assertLess(result["delta_fitted_minus_default"], 0)
        robustness = report["seed_robustness"]
        self.assertEqual(robustness["seed_count"], 8)
        self.assertEqual(robustness["fitted_beats_default_seed_count"], 8)
        self.assertLess(
            robustness["development_fitted_holdout_distance"]["summary"]["max"],
            robustness["default_holdout_distance"]["summary"]["min"],
        )

        self.assertIn(
            "engineering proxy",
            report["comparison_contract"]["mean_interphrase_gap_seconds"],
        )
        self.assertEqual(
            empirical,
            json.loads(
                (CORPUS_ROOT / "empirical-structure.json").read_text(encoding="utf-8")
            ),
        )
        self.assertEqual(
            report,
            json.loads(
                (CORPUS_ROOT / "evaluation.json").read_text(encoding="utf-8")
            ),
        )

    @staticmethod
    def _unit_inventory(session):
        return collections.Counter(
            (
                unit.kind,
                unit.origin_theme_id,
                unit.duration_seconds,
                unit.note,
                unit.velocity,
                unit.bend_value,
                unit.pulse_count,
                unit.flourish,
            )
            for unit in iter_units(session)
        )

    def test_structure_ablation_preserves_unit_inventory_but_changes_plan(self):
        recommendation = corpus_lib.training_recommendations(self.development)
        structured = WhaleSongGrammar(evaluation.fitted_config(recommendation)).generate()
        left = corpus_lib.make_structure_ablation(structured, seed=structured.seed ^ 0x51A7)
        right = corpus_lib.make_structure_ablation(structured, seed=structured.seed ^ 0x51A7)
        self.assertEqual(self._unit_inventory(structured), self._unit_inventory(left))
        self.assertEqual(plan_sha256(left), plan_sha256(right))
        self.assertNotEqual(plan_sha256(structured), plan_sha256(left))
        self.assertEqual(len(left.cycles), 1)
        self.assertEqual(len(left.cycles[0].themes), 1)
        self.assertEqual(len(left.cycles[0].themes[0].phrases), 1)

    def test_blind_pair_is_anonymous_and_renders_nonempty_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, answer = blind.build_blind_pair(
                pathlib.Path(temporary),
                corpus_root=CORPUS_ROOT,
                seconds=0.25,
                gain=0.08,
            )
            self.assertNotIn("assignment", manifest)
            self.assertEqual(set(answer["assignment"].values()), {"structured", "structure_ablation"})
            for label in ("A", "B"):
                sample = pathlib.Path(temporary) / manifest["samples"][label]["file"]
                self.assertTrue(sample.is_file())
                self.assertGreater(sample.stat().st_size, 44)
                self.assertLessEqual(manifest["samples"][label]["signal_metrics"]["level_match_scale"], 1.0)
            self.assertAlmostEqual(
                manifest["samples"]["A"]["signal_metrics"]["rms"],
                manifest["samples"]["B"]["signal_metrics"]["rms"],
                places=9,
            )
            self.assertTrue((pathlib.Path(temporary) / "blind-manifest.json").is_file())
            self.assertTrue((pathlib.Path(temporary) / "answer-key.json").is_file())

    def test_tampered_annotation_fails_hash_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary) / "corpus"
            shutil.copytree(CORPUS_ROOT, target)
            manifest = corpus_lib.load_source_manifest(target)
            first = manifest["records"][0]["annotation"]["file"]
            path = target / first
            path.write_bytes(path.read_bytes() + b"tamper")
            with self.assertRaises(ValueError):
                corpus_lib.build_corpus(target)

    def test_symlink_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary)
            real = target / "manifest.json"
            real.write_text("{}\n", encoding="utf-8")
            root = target / "corpus"
            root.mkdir()
            (root / "source-manifest.json").symlink_to(real)
            with self.assertRaises(RuntimeError):
                corpus_lib.load_source_manifest(root)

    def test_manifest_cannot_escape_raw_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary) / "corpus"
            shutil.copytree(CORPUS_ROOT, target)
            manifest_path = target / "source-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["records"][0]["annotation"]["file"] = "raw/../../outside.txt"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                corpus_lib.build_corpus(target)

    def test_oversized_manifest_is_rejected_before_parse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "source-manifest.json").write_bytes(
                b"x" * (corpus_lib.MAX_MANIFEST_BYTES + 1)
            )
            with self.assertRaises(ValueError):
                corpus_lib.load_source_manifest(root)

    def test_symlink_blind_output_is_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            real = root / "real"
            real.mkdir()
            link = root / "blind"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                blind.build_blind_pair(
                    link,
                    corpus_root=CORPUS_ROOT,
                    seconds=0.1,
                    gain=0.08,
                )
            self.assertEqual(list(real.iterdir()), [])

    def test_live_product_boundary_remains_unchanged(self):
        self.assertEqual(whale_live.DEFAULT_VOICE_MODE, "morph")
        self.assertEqual(whale_live.VOICE_MODES, ("morph", "organic", "realistic", "ufo"))
        self.assertNotIn("song", whale_live.VOICE_MODES)


if __name__ == "__main__":
    unittest.main()
