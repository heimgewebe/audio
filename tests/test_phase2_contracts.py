import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Phase2ContractTests(unittest.TestCase):
    def payload(self, relative):
        return json.loads((ROOT / relative).read_text())

    def test_physical_catalog_matches_null_template(self):
        catalog = self.payload("inventory/physical-facts.v1.json")["facts"]
        template = self.payload("inventory/physical-verification.v1.json")["facts"]
        self.assertEqual(set(catalog), set(template))
        self.assertEqual(len(catalog), 16)
        self.assertTrue(all(value is None for value in template.values()))

    def test_profiles_reference_known_facts_and_valid_gates(self):
        facts = set(self.payload("inventory/physical-facts.v1.json")["facts"])
        profiles = self.payload("profiles/audio-profiles.v1.json")["profiles"]
        self.assertEqual(
            set(profiles),
            {
                "desktop-mixed",
                "reference-listening",
                "voice-recording",
                "piano-digital-recording",
                "piano-software-live",
                "receiver",
                "bluetooth-convenience",
                "qobuz-exclusive",
                "experimental",
                "production",
            },
        )
        for name, profile in profiles.items():
            with self.subTest(profile=name):
                required = set(profile.get("required_physical_facts", []))
                required_values = set(profile.get("required_fact_values", {}))
                self.assertLessEqual(required, facts)
                self.assertLessEqual(required_values, required)
                gates = profile.get("required_laboratory_gates")
                self.assertIsInstance(gates, list)
                self.assertEqual(len(gates), len(set(gates)))
                self.assertTrue(all(isinstance(gate, str) and gate for gate in gates))
                self.assertIn("apply_authority", profile)

    def test_profiles_never_claim_direct_apply_authority(self):
        profiles = self.payload("profiles/audio-profiles.v1.json")["profiles"]
        for name, profile in profiles.items():
            with self.subTest(profile=name):
                authority = profile["apply_authority"]
                self.assertNotEqual(authority, "authorized")
                self.assertNotEqual(authority, "automatic")

    def test_architecture_decisions_close_phase2_without_apply_authority(self):
        decisions = self.payload("inventory/audio-architecture-decisions.v1.json")
        self.assertEqual(decisions["schema_version"], 1)
        self.assertEqual(decisions["kind"], "audio_architecture_decisions")
        self.assertEqual(decisions["status"], "accepted")
        self.assertEqual(decisions["task_id"], "AUDIO-CONTROL-PLANE-V1-T002")
        self.assertTrue(decisions["host_observation"]["read_only"])
        self.assertFalse(
            decisions["host_observation"]["mopidy"]["version_metadata_consistent"]
        )
        self.assertEqual(
            decisions["host_observation"]["ardour"]["user_plugin_discovery"],
            "not-proven",
        )
        self.assertFalse(
            decisions["host_observation"]["easyeffects"]["user_service_active"]
        )
        self.assertEqual(
            decisions["host_observation"]["easyeffects"]["process_activity"],
            "not-established",
        )
        self.assertIn("belegt", decisions["evidence"])
        self.assertIn("plausibel", decisions["evidence"])
        self.assertIn("spekulativ", decisions["evidence"])
        self.assertTrue(decisions["refresh_before_effect"])
        self.assertIn(
            "permission to apply or activate an audio profile",
            decisions["does_not_establish"],
        )

    def test_architecture_profile_bindings_reference_existing_profiles(self):
        profiles = self.payload("profiles/audio-profiles.v1.json")["profiles"]
        decisions = self.payload("inventory/audio-architecture-decisions.v1.json")
        bindings = decisions["profile_bindings"]
        self.assertLessEqual(set(bindings), set(profiles))
        self.assertEqual(bindings["desktop-mixed"]["rate_hz"], 48000)
        self.assertEqual(
            bindings["piano-digital-recording"]["resampling"], "single-stage"
        )
        self.assertEqual(
            bindings["production"]["session_runtime"],
            "flatpak:org.ardour.Ardour",
        )
        self.assertEqual(bindings["qobuz-exclusive"]["easyeffects"], "bypass")
        self.assertEqual(
            bindings["reference-listening"]["rate_policy"],
            "fixed-48000-fallback-until-track-native-proof",
        )
        self.assertEqual(
            bindings["production"]["plugin_discovery"], "requires-scan-proof"
        )

    def test_pipewire_qobuz_ardour_plugin_and_easyeffects_decisions(self):
        decisions = self.payload("inventory/audio-architecture-decisions.v1.json")[
            "decisions"
        ]
        self.assertEqual(decisions["pipewire_alsa"]["graph_core"], "pipewire")
        self.assertEqual(
            decisions["pipewire_alsa"]["exclusive_alsa"],
            "conditional-experiment-only",
        )
        self.assertEqual(decisions["rate_policy"]["mixed_graph_rate_hz"], 48000)
        self.assertEqual(
            decisions["quantum_policy"]["candidate_authority"],
            "laboratory-gated",
        )
        self.assertEqual(
            decisions["qobuz_mopidy"]["mopidy_role"],
            "provider-and-comfort-adapter",
        )
        self.assertFalse(decisions["qobuz_mopidy"]["exclusive_or_bitperfect_claim"])
        self.assertEqual(
            decisions["ardour"]["canonical_runtime"]["distribution"], "flatpak"
        )
        self.assertEqual(
            decisions["ardour"]["native_runtime"]["status"],
            "installed-noncanonical",
        )
        self.assertEqual(decisions["plugins"]["format_priority"][0], "lv2")
        self.assertFalse(decisions["easyeffects"]["automatic_activation"])
        self.assertEqual(decisions["easyeffects"]["default_state"], "inactive")
        self.assertEqual(
            decisions["ardour"]["canonical_runtime"]["plugin_discovery"],
            "requires-scan-proof",
        )
        self.assertEqual(
            decisions["plugins"]["user_plugin_visibility"], "requires-scan-proof"
        )

    def test_system_truth_binds_architecture_decisions(self):
        contract = self.payload("inventory/system-truth.v1.json")
        self.assertIn(
            "inventory/audio-architecture-decisions.v1.json",
            contract["bound_components"],
        )


if __name__ == "__main__":
    unittest.main()
