import importlib.util
import json
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

    def test_prompt_copy_does_not_change_catalog_semantic_hash(self):
        catalog = MODULE.load_json(MODULE.CATALOG_PATH)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            original = root / "original.json"
            changed = root / "changed.json"
            original.write_text(json.dumps(catalog, ensure_ascii=False))
            catalog["facts"]["pioneer_pc_connection"]["prompt"] = (
                "Eine rein redaktionell geänderte Frage"
            )
            changed.write_text(json.dumps(catalog, ensure_ascii=False))
            self.assertNotEqual(MODULE.sha256_file(original), MODULE.sha256_file(changed))
            self.assertEqual(
                MODULE.catalog_semantic_sha256(original),
                MODULE.catalog_semantic_sha256(changed),
            )

    def test_validation_contract_change_changes_catalog_semantic_hash(self):
        catalog = MODULE.load_json(MODULE.CATALOG_PATH)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            original = root / "original.json"
            changed = root / "changed.json"
            original.write_text(json.dumps(catalog, ensure_ascii=False))
            catalog["facts"]["motu_phantom_48v"]["values"] = ["on"]
            changed.write_text(json.dumps(catalog, ensure_ascii=False))
            self.assertNotEqual(
                MODULE.catalog_semantic_sha256(original),
                MODULE.catalog_semantic_sha256(changed),
            )

    def test_current_raw_catalog_binding_is_normalized_without_read_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            state = MODULE.empty_state()
            state["catalog_sha256"] = MODULE.sha256_file(MODULE.CATALOG_PATH)
            MODULE.atomic_write_private(path, state)
            before = path.read_bytes()
            loaded = MODULE.read_state(path)
            self.assertEqual(
                loaded["catalog_sha256"], MODULE.catalog_semantic_sha256()
            )
            self.assertEqual(path.read_bytes(), before)

    def test_known_prompt_only_legacy_binding_is_semantically_scoped(self):
        legacy = "1b8822768b7d809543bb9f037003a828c08177061d54af76c56e58b142f6fd55"
        current_raw = "39a8d395fb8ff44c7466c6c1cd217686ea3b638e6f022edf2ad7e4457fa4deea"
        self.assertEqual(MODULE.sha256_file(MODULE.CATALOG_PATH), current_raw)
        for raw_digest in (legacy, current_raw):
            self.assertEqual(
                MODULE.LEGACY_CATALOG_SHA256_COMPATIBILITY[raw_digest],
                MODULE.catalog_semantic_sha256(),
            )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "state.json"
            state = MODULE.empty_state()
            state["catalog_sha256"] = legacy
            MODULE.record_fact(state, "rode_nt1a_connected", "true", "visual")
            MODULE.atomic_write_private(path, state)
            loaded = MODULE.read_state(path)
            self.assertEqual(
                loaded["catalog_sha256"], MODULE.catalog_semantic_sha256()
            )

            changed_catalog = MODULE.load_json(MODULE.CATALOG_PATH)
            changed_catalog["facts"]["rode_nt1a_connected"][
                "allowed_evidence"
            ] = ["measured"]
            changed_path = root / "catalog.json"
            changed_path.write_text(json.dumps(changed_catalog, ensure_ascii=False))
            original_catalog_path = MODULE.CATALOG_PATH
            MODULE.CATALOG_PATH = changed_path
            try:
                with self.assertRaisesRegex(ValueError, "physical fact catalog changed"):
                    MODULE.read_state(path)
            finally:
                MODULE.CATALOG_PATH = original_catalog_path

    def test_unknown_catalog_binding_stays_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            state = MODULE.empty_state()
            state["catalog_sha256"] = "0" * 64
            MODULE.atomic_write_private(path, state)
            with self.assertRaisesRegex(ValueError, "physical fact catalog changed"):
                MODULE.read_state(path)


if __name__ == "__main__":
    unittest.main()
