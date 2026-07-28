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


if __name__ == "__main__":
    unittest.main()
