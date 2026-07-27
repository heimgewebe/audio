import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "physical_verification", ROOT / "scripts/physical_verification.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class PhysicalVerificationTests(unittest.TestCase):
    def test_validates_enum_and_evidence(self):
        state = MODULE.empty_state()
        MODULE.record_fact(state, "motu_phantom_48v", "on", "visual")
        self.assertEqual(state["facts"]["motu_phantom_48v"]["value"], "on")
        with self.assertRaises(ValueError):
            MODULE.record_fact(state, "motu_phantom_48v", "off", "visual")
        MODULE.record_fact(
            state, "motu_phantom_48v", "off", "visual", replace=True
        )
        self.assertEqual(state["facts"]["motu_phantom_48v"]["value"], "off")
        with self.assertRaises(ValueError):
            MODULE.record_fact(state, "motu_phantom_48v", "maybe", "visual")
        with self.assertRaises(ValueError):
            MODULE.record_fact(state, "motu_phantom_48v", "on", "measured")

    def test_private_atomic_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            state = MODULE.empty_state()
            MODULE.record_fact(state, "rode_nt1a_connected", "true", "visual")
            MODULE.atomic_write_private(path, state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = MODULE.read_state(path)
            self.assertTrue(loaded["facts"]["rode_nt1a_connected"]["value"])

    def test_status_lists_all_unresolved(self):
        status = MODULE.status_payload(MODULE.empty_state(), pathlib.Path("state.json"))
        self.assertEqual(status["total_count"], 16)
        self.assertEqual(status["resolved_count"], 0)
        self.assertFalse(status["complete"])

    def test_rejects_control_characters(self):
        with self.assertRaises(ValueError):
            MODULE.parse_value({"type": "string", "max_length": 120}, "bad\nvalue")

    def test_rejects_tampered_or_insecure_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            state = MODULE.empty_state()
            MODULE.record_fact(state, "rode_nt1a_connected", "true", "visual")
            MODULE.atomic_write_private(path, state)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                MODULE.read_state(path)
            path.chmod(0o600)
            payload = MODULE.load_json(path)
            payload["facts"]["rode_nt1a_connected"]["authority"] = "manual-edit"
            path.write_text(MODULE.json.dumps(payload))
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                MODULE.read_state(path)

    def test_rejects_symlink_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "target.json"
            MODULE.atomic_write_private(target, MODULE.empty_state())
            link = root / "state.json"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                MODULE.read_state(link)

    def test_rejects_invalid_or_naive_timestamps(self):
        with self.assertRaises(ValueError):
            MODULE.parse_timestamp("not-a-time", "test")
        with self.assertRaises(ValueError):
            MODULE.parse_timestamp("2026-07-27T12:00:00", "test")
        parsed = MODULE.parse_timestamp("2026-07-27T12:00:00+02:00", "test")
        self.assertIsNotNone(parsed.utcoffset())


if __name__ == "__main__":
    unittest.main()
