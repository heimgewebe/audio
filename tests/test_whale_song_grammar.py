from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import study_whale_song_grammar as study  # noqa: E402
import whale_live  # noqa: E402
from whale_song_grammar import (  # noqa: E402
    MAX_SESSION_UNITS,
    SongGrammarConfig,
    WhaleSongGrammar,
    canonical_plan_json,
    events_for_session,
    iter_phrases,
    plan_sha256,
    structural_metrics,
)


class WhaleSongGrammarTests(unittest.TestCase):
    def make_session(self, **kwargs):
        return WhaleSongGrammar(SongGrammarConfig(**kwargs)).generate()

    def test_same_seed_is_exactly_deterministic(self):
        left = self.make_session(seed=0xB0A7)
        right = self.make_session(seed=0xB0A7)
        self.assertEqual(canonical_plan_json(left), canonical_plan_json(right))
        self.assertEqual(plan_sha256(left), plan_sha256(right))

    def test_reusing_one_generator_is_idempotent(self):
        grammar = WhaleSongGrammar(SongGrammarConfig(seed=0xB0A7))
        first = grammar.generate()
        second = grammar.generate()
        self.assertEqual(canonical_plan_json(first), canonical_plan_json(second))

    def test_different_seed_changes_variants_not_theme_order(self):
        left = self.make_session(seed=1)
        right = self.make_session(seed=2)
        self.assertNotEqual(plan_sha256(left), plan_sha256(right))
        self.assertEqual(
            [[theme.theme_id for theme in cycle.themes] for cycle in left.cycles],
            [[theme.theme_id for theme in cycle.themes] for cycle in right.cycles],
        )

    def test_explicit_hierarchy_and_transition_count(self):
        session = self.make_session(cycles=2, theme_count=4)
        metrics = structural_metrics(session)
        self.assertEqual(metrics["cycle_count"], 2)
        self.assertEqual(metrics["theme_count_per_cycle"], [4, 4])
        self.assertEqual(metrics["theme_order_per_cycle"], [["A", "B", "C", "D"]] * 2)
        self.assertEqual(metrics["transition_phrase_count"], 6)
        self.assertLessEqual(metrics["unit_count"], MAX_SESSION_UNITS)
        for cycle in session.cycles:
            for theme in cycle.themes:
                self.assertGreaterEqual(len(theme.phrases), 2)
                self.assertTrue(all(phrase.family_id == theme.theme_id for phrase in theme.phrases))

    def test_transition_is_a_real_two_family_hybrid(self):
        session = self.make_session(cycles=1, theme_count=3)
        for transition in session.cycles[0].transitions:
            phrase = transition.phrase
            origins = {unit.origin_theme_id for unit in phrase.units}
            self.assertEqual(origins, {transition.from_theme_id, transition.to_theme_id})
            self.assertEqual(phrase.role, "transition")
            self.assertEqual(phrase.from_theme_id, transition.from_theme_id)
            self.assertEqual(phrase.to_theme_id, transition.to_theme_id)
            self.assertGreaterEqual(len(phrase.units), 4)

    def test_variants_stay_inside_their_phrase_family(self):
        session = self.make_session(cycles=2, theme_count=4, phrase_repeats_min=4, phrase_repeats_max=4)
        for cycle in session.cycles:
            for theme in cycle.themes:
                core_kinds = None
                seen_variation = False
                prior_signature = None
                for phrase in theme.phrases:
                    self.assertTrue(all(unit.origin_theme_id == theme.theme_id for unit in phrase.units))
                    kinds = tuple(unit.kind for unit in phrase.units if not unit.flourish)
                    if core_kinds is None:
                        core_kinds = kinds
                    self.assertEqual(kinds, core_kinds)
                    self.assertIn(len(phrase.units), {len(core_kinds), len(core_kinds) + 1})
                    signature = tuple(
                        (unit.note, unit.velocity, unit.duration_seconds, unit.bend_value)
                        for unit in phrase.units
                    )
                    if prior_signature is not None and signature != prior_signature:
                        seen_variation = True
                    prior_signature = signature
                self.assertTrue(seen_variation)

    def test_phrase_boundary_owns_the_trailing_pause(self):
        session = self.make_session(cycles=1, theme_count=2, phrase_repeats_min=2, phrase_repeats_max=2)
        for phrase in iter_phrases(session):
            self.assertEqual(phrase.units[-1].gap_seconds, 0.0)
            self.assertAlmostEqual(phrase.body_end_seconds, phrase.units[-1].sound_end_seconds, places=6)
            self.assertAlmostEqual(
                phrase.end_seconds - phrase.body_end_seconds,
                phrase.boundary_pause_seconds,
                places=6,
            )
            self.assertGreater(phrase.boundary_pause_seconds, 0.4)
            for unit in phrase.units:
                self.assertLessEqual(unit.end_seconds, phrase.body_end_seconds + 1e-6)

    def test_unit_chain_has_no_rounding_gaps(self):
        session = self.make_session(
            cycles=1,
            theme_count=3,
            phrase_repeats_min=3,
            phrase_repeats_max=3,
        )
        for phrase in iter_phrases(session):
            for left, right in zip(phrase.units, phrase.units[1:]):
                self.assertEqual(left.end_seconds, right.start_seconds)

    def test_pause_hierarchy_survives_jitter(self):
        config = SongGrammarConfig(
            cycles=2,
            theme_count=3,
            phrase_repeats_min=3,
            phrase_repeats_max=3,
            phrase_pause_seconds=1.0,
            transition_pause_seconds=1.16,
            cycle_pause_seconds=1.25,
        )
        session = WhaleSongGrammar(config).generate()
        ordinary_boundaries = []
        transition_boundaries = []
        cycle_boundaries = []
        for cycle in session.cycles:
            cycle_boundaries.append(
                cycle.themes[-1].phrases[-1].boundary_pause_seconds
            )
            for theme_index, theme in enumerate(cycle.themes):
                for phrase_index, phrase in enumerate(theme.phrases):
                    is_cycle_boundary = (
                        theme_index == len(cycle.themes) - 1
                        and phrase_index == len(theme.phrases) - 1
                    )
                    if not is_cycle_boundary:
                        ordinary_boundaries.append(phrase.boundary_pause_seconds)
            transition_boundaries.extend(
                transition.phrase.boundary_pause_seconds
                for transition in cycle.transitions
            )
        self.assertLess(max(ordinary_boundaries), min(transition_boundaries))
        self.assertLess(max(transition_boundaries), min(cycle_boundaries))

    def test_default_session_is_revision_stable_and_bounded(self):
        session = self.make_session()
        metrics = structural_metrics(session)
        self.assertEqual(session.duration_seconds, 219.448825)
        self.assertEqual(metrics["phrase_count_total"], 39)
        self.assertEqual(metrics["transition_phrase_count"], 6)
        self.assertEqual(metrics["unit_count"], 172)
        self.assertEqual(metrics["flourish_unit_count"], 9)
        self.assertEqual(
            plan_sha256(session),
            "dc28438135d02b3fad17907abf262fbbcd0df83c45084806a41f55f4e0706001",
        )
        self.assertGreater(session.duration_seconds, 120.0)
        self.assertLess(session.duration_seconds, 600.0)
        self.assertLessEqual(metrics["unit_count"], MAX_SESSION_UNITS)

    def test_near_budget_configuration_generates_within_bound(self):
        session = self.make_session(
            cycles=4,
            theme_count=3,
            phrase_repeats_min=6,
            phrase_repeats_max=6,
        )
        self.assertLessEqual(structural_metrics(session)["unit_count"], MAX_SESSION_UNITS)

    def test_event_translation_is_sorted_bounded_and_closes_voice(self):
        session = self.make_session(cycles=1, theme_count=2, phrase_repeats_min=2, phrase_repeats_max=2)
        limit = min(8.0, session.duration_seconds)
        events = events_for_session(session, until_seconds=limit)
        times = [time_value for time_value, _event in events]
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(0.0 <= time_value <= limit for time_value in times))
        notes = [event.note for _time, event in events if event.kind in {"note_on", "note_off"}]
        self.assertTrue(notes)
        self.assertTrue(all(21 <= note <= 108 for note in notes))
        self.assertEqual(events[-1][1].kind, "control_change")
        self.assertEqual(events[-1][1].controller, 123)
        self.assertEqual(events[-1][0], limit)

    def test_live_product_boundary_is_unchanged(self):
        self.assertEqual(whale_live.DEFAULT_VOICE_MODE, "morph")
        self.assertEqual(whale_live.VOICE_MODES, ("morph", "organic", "realistic", "ufo"))
        self.assertNotIn("song", whale_live.VOICE_MODES)

    def test_study_report_binds_sources_and_truth_levels(self):
        session = self.make_session(cycles=1, theme_count=2, phrase_repeats_min=2, phrase_repeats_max=2)
        report = study.build_report(session, root=ROOT)
        self.assertEqual(report["kind"], "humpback_whale_song_grammar_study")
        self.assertEqual(report["plan_sha256"], plan_sha256(session))
        self.assertEqual(set(report["source_bindings"]), {path.as_posix() for path in study.SOURCE_BINDING_PATHS})
        self.assertTrue(report["truth_levels"]["evidence_backed"])
        self.assertTrue(report["truth_levels"]["engineering_hypotheses"])
        self.assertIn("perceptual realism", report["does_not_establish"])

    def test_study_report_output_rejects_symlink_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            link = root / "report.json"
            link.symlink_to(target)
            with self.assertRaises(RuntimeError):
                study.write_json(link, {"schema_version": 1})

    def test_config_rejects_unbounded_or_inverted_inputs(self):
        with self.assertRaises(ValueError):
            SongGrammarConfig(cycles=99)
        with self.assertRaises(ValueError):
            SongGrammarConfig(theme_count=1)
        with self.assertRaises(ValueError):
            SongGrammarConfig(phrase_repeats_min=5, phrase_repeats_max=4)
        with self.assertRaises(ValueError):
            SongGrammarConfig(phrase_pause_seconds=2.0, transition_pause_seconds=1.0)
        with self.assertRaises(ValueError):
            SongGrammarConfig(
                phrase_pause_seconds=1.0,
                transition_pause_seconds=1.10,
                cycle_pause_seconds=1.30,
            )
        with self.assertRaises(ValueError):
            SongGrammarConfig(
                phrase_pause_seconds=1.0,
                transition_pause_seconds=1.16,
                cycle_pause_seconds=1.20,
            )
        with self.assertRaises(ValueError):
            SongGrammarConfig(
                cycles=4,
                theme_count=6,
                phrase_repeats_min=8,
                phrase_repeats_max=8,
            )


if __name__ == "__main__":
    unittest.main()
