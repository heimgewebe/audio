import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class LaboratoryContractTests(unittest.TestCase):
    def test_profile_gates_are_catalogued(self):
        profiles = json.loads(
            (ROOT / "profiles/audio-profiles.v1.json").read_text()
        )["profiles"]
        catalog = json.loads(
            (ROOT / "inventory/laboratory-gates.v1.json").read_text()
        )["gates"]
        required = {
            gate
            for profile in profiles.values()
            for gate in profile.get("required_laboratory_gates", [])
        }
        self.assertEqual(required, set(catalog))
        for gate, spec in catalog.items():
            with self.subTest(gate=gate):
                self.assertIn("evidence_kind", spec)
                self.assertIn("validator", spec)
                self.assertIsInstance(spec["binds_physical_state"], bool)


if __name__ == "__main__":
    unittest.main()
