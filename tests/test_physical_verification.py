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

    def test_catalog_binding_ignores_unobserved_prompt_changes(self):
        catalog = MODULE.load_json(MODULE.CATALOG_PATH)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            original = root / "original.json"
            changed = root / "changed.json"
            original.write_text(json.dumps(catalog, ensure_ascii=False))
            catalog["facts"]["pioneer_pc_connection"]["prompt"] = (
                "Eine andere Pioneer-Frage"
            )
            changed.write_text(json.dumps(catalog, ensure_ascii=False))
            self.assertNotEqual(MODULE.sha256_file(original), MODULE.sha256_file(changed))
            self.assertEqual(
                MODULE.catalog_observation_sha256(
                    ["rode_nt1a_connected"], original
                ),
                MODULE.catalog_observation_sha256(
                    ["rode_nt1a_connected"], changed
                ),
            )

    def test_catalog_binding_tracks_observed_prompt_and_validation_semantics(self):
        catalog = MODULE.load_json(MODULE.CATALOG_PATH)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            original = root / "original.json"
            prompt_changed = root / "prompt.json"
            validation_changed = root / "validation.json"
            rules_changed = root / "rules.json"
            original.write_text(json.dumps(catalog, ensure_ascii=False))

            prompt_catalog = json.loads(json.dumps(catalog))
            prompt_catalog["facts"]["rode_nt1a_connected"]["prompt"] = (
                "Ist das Mikrofon tatsächlich per XLR verbunden?"
            )
            prompt_changed.write_text(json.dumps(prompt_catalog, ensure_ascii=False))

            validation_catalog = json.loads(json.dumps(catalog))
            validation_catalog["facts"]["rode_nt1a_connected"][
                "allowed_evidence"
            ] = ["measured"]
            validation_changed.write_text(
                json.dumps(validation_catalog, ensure_ascii=False)
            )

            rules_catalog = json.loads(json.dumps(catalog))
            rules_catalog["rules"].append("new global observation rule")
            rules_changed.write_text(json.dumps(rules_catalog, ensure_ascii=False))

            baseline = MODULE.catalog_observation_sha256(
                ["rode_nt1a_connected"], original
            )
            for candidate in (prompt_changed, validation_changed, rules_changed):
                self.assertNotEqual(
                    baseline,
                    MODULE.catalog_observation_sha256(
                        ["rode_nt1a_connected"], candidate
                    ),
                )

    def test_record_refreshes_observation_scoped_catalog_binding(self):
        state = MODULE.empty_state()
        MODULE.record_fact(state, "rode_nt1a_connected", "true", "visual")
        self.assertEqual(
            state["catalog_sha256"],
            MODULE.catalog_observation_sha256(state["facts"]),
        )

    def test_current_raw_catalog_binding_normalizes_without_read_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            state = MODULE.empty_state()
            MODULE.record_fact(state, "rode_nt1a_connected", "true", "visual")
            state["catalog_sha256"] = MODULE.sha256_file(MODULE.CATALOG_PATH)
            MODULE.atomic_write_private(path, state)
            before = path.read_bytes()
            loaded = MODULE.read_state(path)
            self.assertEqual(
                loaded["catalog_sha256"],
                MODULE.catalog_observation_sha256(loaded["facts"]),
            )
            self.assertEqual(path.read_bytes(), before)

    def test_proven_legacy_prompt_transition_accepts_only_unaffected_facts(self):
        self.assertTrue(
            MODULE.legacy_prompt_only_catalog_compatible(
                MODULE.LEGACY_PROMPT_ONLY_SOURCE_SHA256,
                MODULE.LEGACY_PROMPT_ONLY_SUCCESSOR_SHA256,
                ["rode_nt1a_connected"],
            )
        )
        self.assertFalse(
            MODULE.legacy_prompt_only_catalog_compatible(
                MODULE.LEGACY_PROMPT_ONLY_SOURCE_SHA256,
                MODULE.LEGACY_PROMPT_ONLY_SUCCESSOR_SHA256,
                ["pioneer_pc_connection"],
            )
        )
        self.assertFalse(
            MODULE.legacy_prompt_only_catalog_compatible(
                MODULE.LEGACY_PROMPT_ONLY_SOURCE_SHA256,
                "f" * 64,
                ["rode_nt1a_connected"],
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            unaffected = root / "unaffected.json"
            state = MODULE.empty_state()
            MODULE.record_fact(state, "rode_nt1a_connected", "true", "visual")
            state["catalog_sha256"] = MODULE.LEGACY_PROMPT_ONLY_SOURCE_SHA256
            MODULE.atomic_write_private(unaffected, state)
            loaded = MODULE.read_state(unaffected)
            self.assertEqual(
                loaded["catalog_sha256"],
                MODULE.catalog_observation_sha256(loaded["facts"]),
            )

            changed = root / "changed.json"
            state = MODULE.empty_state()
            MODULE.record_fact(state, "pioneer_pc_connection", "RCA", "visual")
            state["catalog_sha256"] = MODULE.LEGACY_PROMPT_ONLY_SOURCE_SHA256
            MODULE.atomic_write_private(changed, state)
            with self.assertRaisesRegex(ValueError, "physical fact catalog changed"):
                MODULE.read_state(changed)

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
