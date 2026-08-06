import copy
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "profile_manager", ROOT / "scripts/profile_manager.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ProfileManagerTests(unittest.TestCase):
    def setUp(self):
        self.catalogs = MODULE.load_catalogs()
        self.profile_ids = sorted(self.catalogs["contracts"]["profiles"])

    def write_state(self, directory, payload):
        path = pathlib.Path(directory) / "state.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return path

    def test_transition_contract_matches_published_schema(self):
        schema = json.loads(
            (ROOT / "schemas/audio-profile-catalog.v1.schema.json").read_text()
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(self.catalogs["contracts"])

    def test_catalog_binding_is_exact_and_aliases_are_forbidden(self):
        self.assertEqual(
            set(self.catalogs["product"]["profiles"]),
            set(self.catalogs["contracts"]["profiles"]),
        )
        self.assertEqual(self.catalogs["contracts"]["aliases"], {})
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            product = root / "audio-profiles.v1.json"
            contracts = root / "audio-profile-contracts.v1.json"
            product.write_bytes(MODULE.PRODUCT_CATALOG_PATH.read_bytes() + b"\n")
            contracts.write_bytes(MODULE.CONTRACT_CATALOG_PATH.read_bytes())
            with self.assertRaises(MODULE.ProfileManagerError) as context:
                MODULE.load_catalogs(product, contracts)
        self.assertEqual(context.exception.code, "catalog-binding-invalid")
        with self.assertRaises(MODULE.ProfileManagerError) as context:
            MODULE.build_plan(
                "desktop",
                "desktop-mixed",
                MODULE.simulation_snapshot("desktop-mixed", self.catalogs),
                self.catalogs,
            )
        self.assertEqual(context.exception.code, "profile-unknown")

    def test_every_profile_has_the_complete_typed_transition_contract(self):
        required = MODULE.REQUIRED_CONTRACT_FIELDS
        for profile_id, contract in self.catalogs["contracts"]["profiles"].items():
            with self.subTest(profile=profile_id):
                self.assertEqual(set(contract), required)
                self.assertTrue(contract["protects_recording"])
                self.assertEqual(
                    set(contract["lifecycle"]["start"]),
                    set(contract["lifecycle"]["stop"]),
                )
                self.assertTrue(contract["readback"])
                self.assertTrue(contract["rollback"])

    def test_doctor_plan_and_diff_are_pure(self):
        snapshot = MODULE.simulation_snapshot("desktop-mixed", self.catalogs)
        snapshot["foreign_processes"] = ["foreign-recorder"]
        snapshot["foreign_routes"] = ["foreign-route"]
        before = copy.deepcopy(snapshot)
        report = MODULE.doctor(snapshot, self.catalogs)
        plan = MODULE.build_plan(
            "desktop-mixed", "voice-recording", snapshot, self.catalogs
        )
        diff = MODULE.public_diff(plan)
        self.assertEqual(snapshot, before)
        self.assertTrue(report["read_only"])
        self.assertTrue(plan["read_only"])
        self.assertTrue(diff["read_only"])
        self.assertEqual(plan["effect_scope"], "repository-and-simulation-only")
        self.assertEqual(diff["plan_sha256"], plan["plan_sha256"])

    def test_all_directed_profile_pairs_are_deterministic_reversible_and_narrow(self):
        pair_count = 0
        for source in self.profile_ids:
            for target in self.profile_ids:
                pair_count += 1
                snapshot = MODULE.simulation_snapshot(source, self.catalogs)
                snapshot["foreign_processes"] = ["foreign-player", "foreign-recorder"]
                snapshot["foreign_routes"] = ["foreign-route-a", "foreign-route-b"]
                plan = MODULE.build_plan(source, target, snapshot, self.catalogs)
                repeated_plan = MODULE.build_plan(
                    source, target, snapshot, self.catalogs
                )
                with self.subTest(source=source, target=target):
                    self.assertEqual(plan, repeated_plan)
                    self.assertFalse(plan["blockers"])
                    self.assertTrue(plan["ready_for_simulated_apply"])
                    self.assertEqual(
                        plan["rollback_operations"],
                        MODULE.rollback_operations(plan["operations"]),
                    )
                    for operation in plan["operations"]:
                        self.assertEqual(operation["owner"], MODULE.MANAGER_ID)
                        self.assertIn(operation["kind"], MODULE.OPERATION_KINDS)
                        self.assertNotIn("pipewire", json.dumps(operation).lower())
                        self.assertNotIn("kill", json.dumps(operation).lower())
                    with tempfile.TemporaryDirectory() as directory:
                        state_path = self.write_state(directory, snapshot)
                        receipt = MODULE.apply_simulated(
                            plan, plan["plan_sha256"], state_path
                        )
                        current = MODULE.read_json(state_path)
                        self.assertEqual(
                            current["foreign_processes"], snapshot["foreign_processes"]
                        )
                        self.assertEqual(
                            current["foreign_routes"], snapshot["foreign_routes"]
                        )
                        self.assertEqual(
                            MODULE.state_projection(MODULE.normalize_snapshot(current)),
                            plan["target"],
                        )
                        replay = MODULE.apply_simulated(
                            plan, plan["plan_sha256"], state_path
                        )
                        self.assertTrue(replay["idempotent"])
                        self.assertEqual(replay["operations_applied"], 0)
                        if not receipt["idempotent"]:
                            rollback = MODULE.rollback_simulated(
                                receipt, receipt["receipt_sha256"], state_path
                            )
                            restored = MODULE.normalize_snapshot(
                                MODULE.read_json(state_path)
                            )
                            self.assertEqual(
                                restored, MODULE.normalize_snapshot(snapshot)
                            )
                            self.assertEqual(
                                rollback["restored_state_sha256"],
                                receipt["before_state_sha256"],
                            )
        self.assertEqual(pair_count, len(self.profile_ids) ** 2)

    def test_active_or_unknown_recording_blocks_every_material_transition(self):
        for recording_state in ("active", "unknown"):
            snapshot = MODULE.simulation_snapshot(
                "desktop-mixed", self.catalogs, recording_state=recording_state
            )
            plan = MODULE.build_plan(
                "desktop-mixed", "production", snapshot, self.catalogs
            )
            with self.subTest(recording_state=recording_state):
                self.assertIn(f"recording-{recording_state}", plan["blockers"])
                self.assertFalse(plan["ready_for_simulated_apply"])
                with tempfile.TemporaryDirectory() as directory:
                    state_path = self.write_state(directory, snapshot)
                    with self.assertRaises(MODULE.ProfileManagerError) as context:
                        MODULE.apply_simulated(plan, plan["plan_sha256"], state_path)
                    self.assertEqual(context.exception.code, "plan-blocked")
                    self.assertEqual(
                        MODULE.normalize_snapshot(MODULE.read_json(state_path)),
                        MODULE.normalize_snapshot(snapshot),
                    )

    def test_apply_requires_the_exact_reviewed_plan_and_fresh_state(self):
        snapshot = MODULE.simulation_snapshot("desktop-mixed", self.catalogs)
        plan = MODULE.build_plan(
            "desktop-mixed", "reference-listening", snapshot, self.catalogs
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory, snapshot)
            with self.assertRaises(MODULE.ProfileManagerError) as context:
                MODULE.apply_simulated(plan, "0" * 64, state_path)
            self.assertEqual(context.exception.code, "plan-hash-mismatch")
            changed = MODULE.read_json(state_path)
            changed["foreign_processes"] = ["appeared-after-review"]
            state_path.write_text(json.dumps(changed) + "\n")
            with self.assertRaises(MODULE.ProfileManagerError) as context:
                MODULE.apply_simulated(plan, plan["plan_sha256"], state_path)
            self.assertEqual(context.exception.code, "state-changed")

    def test_apply_uses_one_atomic_replace_and_receipt_is_complete(self):
        snapshot = MODULE.simulation_snapshot("desktop-mixed", self.catalogs)
        plan = MODULE.build_plan(
            "desktop-mixed", "piano-software-live", snapshot, self.catalogs
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory, snapshot)
            original_replace = MODULE.os.replace
            calls = []

            def recording_replace(source, target):
                calls.append((pathlib.Path(source), pathlib.Path(target)))
                return original_replace(source, target)

            with mock.patch.object(MODULE.os, "replace", side_effect=recording_replace):
                receipt = MODULE.apply_simulated(plan, plan["plan_sha256"], state_path)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], state_path)
            self.assertRegex(receipt["receipt_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                {
                    "schema_version",
                    "kind",
                    "manager",
                    "effect_scope",
                    "plan_sha256",
                    "plan",
                    "idempotent",
                    "operations_applied",
                    "before_state_sha256",
                    "after_state_sha256",
                    "pre_state",
                    "readback",
                    "receipt_sha256",
                },
                set(receipt),
            )

    def test_rollback_refuses_drift_and_tampered_receipts(self):
        snapshot = MODULE.simulation_snapshot("desktop-mixed", self.catalogs)
        plan = MODULE.build_plan(
            "desktop-mixed", "voice-recording", snapshot, self.catalogs
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory, snapshot)
            receipt = MODULE.apply_simulated(plan, plan["plan_sha256"], state_path)
            tampered = copy.deepcopy(receipt)
            tampered["operations_applied"] += 1
            with self.assertRaises(MODULE.ProfileManagerError) as context:
                MODULE.rollback_simulated(
                    tampered, receipt["receipt_sha256"], state_path
                )
            self.assertEqual(context.exception.code, "receipt-invalid")
            drifted = MODULE.read_json(state_path)
            drifted["foreign_routes"].append("new-foreign-route")
            state_path.write_text(json.dumps(drifted) + "\n")
            with self.assertRaises(MODULE.ProfileManagerError) as context:
                MODULE.rollback_simulated(
                    receipt, receipt["receipt_sha256"], state_path
                )
            self.assertEqual(context.exception.code, "rollback-drift")

    def test_rehashed_plan_cannot_change_canonical_target_or_fields(self):
        snapshot = MODULE.simulation_snapshot("desktop-mixed", self.catalogs)
        plan = MODULE.build_plan(
            "desktop-mixed", "voice-recording", snapshot, self.catalogs
        )
        tampered = copy.deepcopy(plan)
        tampered["target"]["sink"] = "foreign-sink"
        tampered["operations"] = MODULE.build_operations(
            tampered["before"], tampered["target"]
        )
        tampered["rollback_operations"] = MODULE.rollback_operations(
            tampered["operations"]
        )
        unsigned = dict(tampered)
        unsigned.pop("plan_sha256")
        tampered["plan_sha256"] = MODULE.sha256_payload(unsigned)
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory, snapshot)
            with self.assertRaises(MODULE.ProfileManagerError) as context:
                MODULE.apply_simulated(tampered, tampered["plan_sha256"], state_path)
        self.assertEqual(context.exception.code, "plan-invalid")
        unknown = copy.deepcopy(plan)
        unknown["unexpected"] = True
        unsigned = dict(unknown)
        unsigned.pop("plan_sha256")
        unknown["plan_sha256"] = MODULE.sha256_payload(unsigned)
        with self.assertRaises(MODULE.ProfileManagerError) as context:
            MODULE.validate_plan(unknown, self.catalogs)
        self.assertEqual(context.exception.code, "plan-invalid")
        recording_snapshot = MODULE.simulation_snapshot(
            "desktop-mixed", self.catalogs, recording_state="active"
        )
        recording_plan = MODULE.build_plan(
            "desktop-mixed", "voice-recording", recording_snapshot, self.catalogs
        )
        recording_plan["blockers"] = []
        recording_plan["ready_for_simulated_apply"] = True
        unsigned = dict(recording_plan)
        unsigned.pop("plan_sha256")
        recording_plan["plan_sha256"] = MODULE.sha256_payload(unsigned)
        with self.assertRaises(MODULE.ProfileManagerError) as context:
            MODULE.validate_plan(recording_plan, self.catalogs)
        self.assertEqual(context.exception.code, "plan-invalid")

    def test_rehashed_receipt_cannot_expand_rollback_pre_state(self):
        snapshot = MODULE.simulation_snapshot("desktop-mixed", self.catalogs)
        plan = MODULE.build_plan(
            "desktop-mixed", "voice-recording", snapshot, self.catalogs
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory, snapshot)
            receipt = MODULE.apply_simulated(plan, plan["plan_sha256"], state_path)
            forged = copy.deepcopy(receipt)
            forged["pre_state"]["foreign_routes"].append("forged-route")
            forged["before_state_sha256"] = MODULE.sha256_payload(forged["pre_state"])
            unsigned = dict(forged)
            unsigned.pop("receipt_sha256")
            forged["receipt_sha256"] = MODULE.sha256_payload(unsigned)
            with self.assertRaises(MODULE.ProfileManagerError) as context:
                MODULE.rollback_simulated(forged, forged["receipt_sha256"], state_path)
            self.assertEqual(context.exception.code, "receipt-invalid")

    def test_json_reader_is_bounded_and_rejects_symlinks(self):
        snapshot = MODULE.simulation_snapshot("desktop-mixed", self.catalogs)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state_path = self.write_state(directory, snapshot)
            link = root / "state-link.json"
            link.symlink_to(state_path)
            with self.assertRaises(MODULE.ProfileManagerError) as context:
                MODULE.read_json(link)
            self.assertEqual(context.exception.code, "json-symlink")
            oversized = root / "oversized.json"
            oversized.write_bytes(b"{" + b" " * MODULE.MAX_JSON_BYTES + b"}")
            with self.assertRaises(MODULE.ProfileManagerError) as context:
                MODULE.read_json(oversized)
            self.assertEqual(context.exception.code, "json-too-large")

    def test_same_profile_plan_has_no_audio_operations(self):
        for profile_id in self.profile_ids:
            snapshot = MODULE.simulation_snapshot(profile_id, self.catalogs)
            plan = MODULE.build_plan(profile_id, profile_id, snapshot, self.catalogs)
            with self.subTest(profile=profile_id):
                self.assertEqual(plan["operations"], [])
                self.assertEqual(plan["rollback_operations"], [])
                self.assertTrue(plan["ready_for_simulated_apply"])


if __name__ == "__main__":
    unittest.main()
