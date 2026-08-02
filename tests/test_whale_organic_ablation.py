import hashlib
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import evaluate_whale_voice_model as evaluator  # noqa: E402
import study_whale_organic_ablation as study  # noqa: E402
import whale_live_engine as live  # noqa: E402
import whale_morph_engine as morph  # noqa: E402
import whale_organic_engine as organic  # noqa: E402
import whale_source_filter_engine as source_filter  # noqa: E402


class FixedStudyBank(source_filter.WhaleSourceFilterBank):
    """Deterministic non-zero control fixture for every source component."""

    def control(self, *, note, seed, age_frames, sample_rate):
        del note, seed, age_frames, sample_rate
        return source_filter.SourceFilterControl(
            envelope=0.72,
            periodicity=0.61,
            roughness=0.47,
            high_band_ratio=0.29,
            spectral_tilt=0.58,
            resonance_ratio_1=2.4,
            resonance_ratio_2=4.7,
            harmonic_profile=(0.31, 0.22, 0.16, 0.11, 0.08, 0.055, 0.04, 0.025),
            pulse_rate_hz=3.4,
            pulse_strength=0.63,
            subharmonic_strength=0.41,
            secondary_ratio=1.43,
            secondary_strength=0.34,
        )


class OrganicAblationSwitchTests(unittest.TestCase):
    def setUp(self):
        self.config = live.WhaleVoiceConfig(
            sample_rate=48_000,
            block_frames=128,
            master_gain=0.16,
        )
        self.fixed_bank = FixedStudyBank()

    def render(self, enabled, frames=48_000):
        voice = organic.OrganicWhaleMorphVoice(
            self.config,
            source_filter_bank=self.fixed_bank,
            component_config=organic.OrganicComponentConfig.from_enabled(
                frozenset(enabled)
            ),
        )
        voice.note_on(50, 84)
        voice.control_change(1, 72)
        return voice.render(frames)

    def test_component_catalog_is_exact_and_ordered(self):
        self.assertEqual(
            organic.ORGANIC_COMPONENT_NAMES,
            (
                "source_envelope",
                "periodicity_roughness",
                "pulse",
                "subharmonic",
                "secondary_frequency",
                "resonance_focus",
                "harmonic_profile",
                "register_bass",
                "articulation_states",
                "pitch_contour",
            ),
        )
        self.assertEqual(study.COMPONENTS, organic.ORGANIC_COMPONENT_NAMES)

    def test_all_disabled_is_bit_exact_morph(self):
        plain = morph.WhaleMorphVoice(self.config)
        neutral = organic.OrganicWhaleMorphVoice(
            self.config,
            component_config=organic.OrganicComponentConfig.morph_neutral(),
        )
        for voice in (plain, neutral):
            voice.note_on(52, 80)
            voice.control_change(1, 49)
        self.assertEqual(plain.render(8192), neutral.render(8192))

    def test_all_enabled_is_bit_exact_default_organic(self):
        default = organic.OrganicWhaleMorphVoice(self.config)
        explicit = organic.OrganicWhaleMorphVoice(
            self.config,
            component_config=organic.OrganicComponentConfig(),
        )
        for voice in (default, explicit):
            voice.note_on(50, 84)
            voice.control_change(1, 72)
        self.assertEqual(default.render(48_000), explicit.render(48_000))

    def test_each_component_is_individually_observable_over_morph(self):
        baseline = self.render(())
        for component in organic.ORGANIC_COMPONENT_NAMES:
            with self.subTest(component=component):
                isolated = self.render((component,))
                self.assertNotEqual(isolated, baseline)

    def test_each_component_can_be_removed_from_full_organic(self):
        full = frozenset(organic.ORGANIC_COMPONENT_NAMES)
        baseline = self.render(full)
        for component in organic.ORGANIC_COMPONENT_NAMES:
            with self.subTest(component=component):
                dropped = self.render(full - {component})
                self.assertNotEqual(dropped, baseline)

    def test_unknown_component_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "unknown Organic components"):
            organic.OrganicComponentConfig.from_enabled(frozenset({"unknown"}))
        with self.assertRaisesRegex(ValueError, "unknown source-filter components"):
            source_filter.SourceFilterComponentConfig.from_enabled(
                frozenset({"unknown"})
            )

    def test_every_isolated_component_remains_chunk_invariant(self):
        sizes = (17, 111, 3, 509, 256, 729, 1, 2048, 73)
        for component in organic.ORGANIC_COMPONENT_NAMES:
            with self.subTest(component=component):
                component_config = organic.OrganicComponentConfig.from_enabled(
                    frozenset({component})
                )
                left = organic.OrganicWhaleMorphVoice(
                    self.config, component_config=component_config
                )
                right = organic.OrganicWhaleMorphVoice(
                    self.config, component_config=component_config
                )
                for voice in (left, right):
                    voice.note_on(52, 80)
                    voice.control_change(1, 49)
                total = 8192
                expected = left.render(total)
                actual = []
                index = 0
                while len(actual) < total:
                    size = min(sizes[index % len(sizes)], total - len(actual))
                    actual.extend(right.render(size))
                    index += 1
                self.assertEqual(expected, actual)


class OrganicAblationStudyContractTests(unittest.TestCase):
    def test_definition_is_external_blind_and_periodicity_is_not_doubled(self):
        definition = study.load_definition()
        self.assertFalse(definition["external_data_used_for_selection"])
        self.assertFalse(definition["periodicity_complement_double_weighted"])
        self.assertEqual(tuple(definition["components"]), study.COMPONENTS)
        self.assertEqual(
            definition["candidate_selection"][
                "minimum_a0_a1_bass_ratio_vs_full_organic"
            ],
            0.95,
        )

    def test_low_bass_regression_rejects_non_morph_candidate(self):
        definition = study.load_definition()

        def record(variant_id, similarity, bass):
            return {
                "id": variant_id,
                "enabled_components": [],
                "comparison_to_morph": {
                    "mean_similarity_delta": similarity - 0.5,
                    "improved_family_count": 8,
                    "worst_family_delta": 0.0,
                },
                "summary": {
                    "mean_similarity": similarity,
                    "worst_fold_similarity": similarity,
                    "similarity_variance": 0.0001,
                    "mean_cpu_seconds_per_audio_second": 0.1,
                    "maximum_cpu_seconds_per_audio_second": 0.1,
                    "product_contracts_pass": True,
                    "bass_energy": {
                        "midi_21_below_120hz_mean_square": {"mean": bass},
                        "midi_33_below_120hz_mean_square": {"mean": bass},
                    },
                },
            }

        morph_record = record("morph", 0.5, 0.5)
        full_record = record("organic-full", 0.51, 1.0)
        weak_bass = record("organic-only-pulse", 0.52, 0.94)
        selected, decisions = study.choose_candidate(
            [morph_record, full_record, weak_bass], definition
        )
        decision = next(
            item for item in decisions if item["variant_id"] == weak_bass["id"]
        )
        self.assertIn("a0_a1_bass_regression", decision["reasons"])
        self.assertNotEqual(selected["id"], weak_bass["id"])

    def test_base_matrix_is_bounded_complete_and_unique(self):
        variants = study.base_variants()
        self.assertEqual(len(variants), 22)
        self.assertEqual(len({variant.variant_id for variant in variants}), 22)
        roles = [variant.role for variant in variants]
        self.assertEqual(roles.count("baseline"), 2)
        self.assertEqual(roles.count("leave-one-component-out"), 10)
        self.assertEqual(roles.count("isolated-over-morph"), 10)

    def test_study_exposes_all_required_distance_metrics(self):
        distances = {
            key: 0.0
            for key in (
                *evaluator.SCALAR_FEATURES,
                "secondary_ratio",
                "harmonic_profile_l1",
            )
        }
        metrics = study.feature_metrics(distances)
        self.assertEqual(
            set(metrics),
            {
                "envelope_distance",
                "periodicity_distance",
                "spectral_tilt_distance",
                "high_band_distance",
                "harmonic_profile_distance",
                "resonance_focus_distance",
                "pulse_rate_distance",
                "pulse_strength_distance",
                "subharmonic_distance",
                "secondary_ratio_distance",
                "secondary_strength_distance",
            },
        )
        self.assertNotIn("roughness_distance", metrics)

    def test_locked_noaa_manifest_is_byte_identical(self):
        payload = evaluator.EXTERNAL_EVALUATION_MANIFEST.read_bytes()
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            evaluator.EXPECTED_EXTERNAL_EVALUATION_MANIFEST_SHA256,
        )

    def test_external_evaluation_paths_do_not_enter_builder_or_runtime(self):
        forbidden = (
            "assets/whale-sources/evaluation/",
            "assets/whale-sources/evaluation-v2/",
            "noaa-pmel-alaska-winter-1999-independent",
        )
        runtime_paths = (
            ROOT / "scripts" / "build_whale_voice_model.py",
            ROOT / "scripts" / "whale_source_filter_engine.py",
            ROOT / "scripts" / "whale_organic_engine.py",
            ROOT / "scripts" / "whale_live.py",
        )
        for path in runtime_paths:
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                with self.subTest(path=path.name, marker=marker):
                    self.assertNotIn(marker, text)

    def test_external_source_is_absent_from_model_family_catalog(self):
        manifest = json.loads(
            evaluator.EXTERNAL_EVALUATION_MANIFEST.read_text(encoding="utf-8")
        )
        model_sources = set(source_filter.WhaleSourceFilterBank().source_ids)
        for clip in manifest["clips"]:
            self.assertNotIn(clip["source_id"], model_sources)


if __name__ == "__main__":
    unittest.main()
