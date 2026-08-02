import copy
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/audio_product_model.py"
SPEC = importlib.util.spec_from_file_location("audio_product_model", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

FIXTURES = ROOT / "tests/fixtures/audiozentrale-product-model"


class AudioProductModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = MODULE.load_contract()
        self.valid_workspace = MODULE.read_json_regular(
            MODULE.WORKSPACE_EXAMPLE_PATH, "workspace example"
        )

    def test_model_binds_plan_schemas_profiles_navigation_truth_and_depth(self) -> None:
        model = self.contract["model"]
        self.assertEqual(
            model["plan_binding"],
            {
                "repository": "heimgewebe/audio",
                "path": "docs/plans/audiozentrale-product-v2.md",
                "commit": "25d790ff2fee589f725b8f96a3fd1e9d8ec34dfc",
                "document_sha256": "61979e0e4182291b460274153bee4ce2c770225a5d6c7940ee52bdc4785e47ae",
            },
        )
        for key, path in (
            ("product_model", MODULE.PRODUCT_SCHEMA_PATH),
            ("workspace_state", MODULE.WORKSPACE_SCHEMA_PATH),
        ):
            binding = model["schema_bindings"][key]
            self.assertEqual(binding["path"], path.relative_to(ROOT).as_posix())
            self.assertEqual(
                binding["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )
        profile_binding = model["profile_catalog_binding"]
        self.assertEqual(
            profile_binding["path"],
            MODULE.PROFILE_CATALOG_PATH.relative_to(ROOT).as_posix(),
        )
        self.assertEqual(
            profile_binding["sha256"],
            hashlib.sha256(MODULE.PROFILE_CATALOG_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            [item["id"] for item in model["navigation"]["places"]],
            ["now", "setups", "library", "system"],
        )
        self.assertEqual(
            [item["id"] for item in model["navigation"]["truth_layers"]],
            ["observed", "configured", "physical-open", "executable"],
        )
        self.assertEqual(
            [item["id"] for item in model["navigation"]["display_depths"]],
            ["compact", "expanded", "focus"],
        )

    def test_bound_artifacts_are_hashed_and_parsed_from_single_snapshots(self) -> None:
        original = MODULE.read_regular_bytes
        calls: list[pathlib.Path] = []

        def tracked(path, label, *, maximum_bytes=MODULE.MAX_JSON_BYTES):
            calls.append(pathlib.Path(path))
            return original(path, label, maximum_bytes=maximum_bytes)

        with mock.patch.object(MODULE, "read_regular_bytes", side_effect=tracked):
            MODULE.validate_product_model(copy.deepcopy(self.contract["model"]))

        self.assertEqual(
            calls.count(ROOT / "docs/plans/audiozentrale-product-v2.md"), 1
        )
        self.assertEqual(calls.count(MODULE.PRODUCT_SCHEMA_PATH), 1)
        self.assertEqual(calls.count(MODULE.WORKSPACE_SCHEMA_PATH), 1)
        self.assertEqual(calls.count(MODULE.PROFILE_CATALOG_PATH), 1)

    def test_legacy_migration_is_total_and_truth_is_orthogonal(self) -> None:
        migration = self.contract["model"]["navigation"]["legacy_area_migration"]
        self.assertEqual(
            [item["source"] for item in migration],
            ["start", "hoeren", "spielen", "aufnehmen", "system"],
        )
        self.assertTrue(
            self.contract["truth_layers"].isdisjoint(self.contract["display_depths"])
        )

    def test_module_and_modulation_scope_is_closed_and_typed(self) -> None:
        model = self.contract["model"]
        self.assertEqual(model["signal_types"], ["audio-mono", "audio-stereo", "midi"])
        self.assertEqual(model["contracts"]["module_topology"], "linear-typed-chain")
        self.assertFalse(model["contracts"]["free_audio_or_midi_ports"])
        self.assertFalse(model["contracts"]["routing_cycles"])
        self.assertFalse(model["contracts"]["user_scripts"])
        forbidden = set(model["modulation_contract"]["forbidden_target_kinds"])
        self.assertEqual(
            forbidden,
            {
                "recording",
                "transport",
                "output-selection",
                "master-gain",
                "panic-mute",
                "safety-action",
            },
        )
        whale = self.contract["modules"]["whale-voice"]
        self.assertEqual(whale["input_signal_types"], ["midi"])
        self.assertEqual(whale["output_signal_type"], "audio-stereo")
        for module in self.contract["modules"].values():
            self.assertNotIn("plugin", module)
            self.assertNotIn("ports", module)
            self.assertTrue(module["input_signal_types"])
            self.assertIn(module["latency_class"], {"zero", "low", "buffered"})
            self.assertIn(module["cpu_class"], {"low", "medium", "high"})
            self.assertTrue(
                set(module["modulation_source_kinds"])
                <= set(model["modulation_contract"]["source_kinds"])
            )

    def test_valid_workspace_is_bound_typed_and_has_one_active_setup(self) -> None:
        result = MODULE.validate_workspace(self.valid_workspace, self.contract)
        self.assertEqual(result["active_setup_id"], "reference-focal")
        self.assertEqual(result["setup_count"], 2)
        self.assertEqual(result["take_count"], 1)
        for setup in self.valid_workspace["setups"]:
            for lane in setup["lanes"]:
                self.assertIn(
                    lane["source"]["signal_type"], self.contract["signal_types"]
                )
                self.assertIn(
                    lane["target"]["signal_type"], self.contract["signal_types"]
                )
        take = self.valid_workspace["takes"][0]
        self.assertTrue(take["immutable"])
        self.assertEqual(take["monitoring"]["mode"], "unknown")

    def test_invalid_semantic_cases_fail_with_declared_reason(self) -> None:
        def second_setup_active(workspace):
            workspace["setups"][1]["status"] = "active"

        def active_id_mismatch(workspace):
            workspace["active_setup_id"] = "voice-dry"

        def active_without_lane(workspace):
            workspace["setups"][0]["lanes"] = []

        def unknown_profile(workspace):
            workspace["setups"][0]["profile_refs"] = ["unknown-profile"]

        def duplicate_profile(workspace):
            workspace["setups"][0]["profile_refs"] = [
                "reference-listening",
                "reference-listening",
            ]

        def unknown_module(workspace):
            workspace["setups"][0]["lanes"][0]["modules"][0]["module_id"] = (
                "external-plugin"
            )

        def incompatible_signal_chain(workspace):
            workspace["setups"][0]["lanes"][0]["modules"][0] = {
                "id": "whale-main",
                "module_id": "whale-voice",
                "bypassed": False,
                "parameters": {"mode": "morph", "motion": 0.5},
            }

        def target_signal_mismatch(workspace):
            workspace["setups"][0]["lanes"][0]["target"]["signal_type"] = "audio-mono"

        def forbidden_modulation_target(workspace):
            workspace["setups"][0]["modulation_links"][0]["target"]["kind"] = (
                "master-gain"
            )

        def scene_routing_mutation(workspace):
            workspace["setups"][0]["scenes"][0]["output_selection"] = "receiver"

        def duplicate_macro_override(workspace):
            overrides = workspace["setups"][0]["scenes"][0]["macro_overrides"]
            overrides.append(copy.deepcopy(overrides[0]))

        def mutable_take(workspace):
            workspace["takes"][0]["immutable"] = False

        def take_source_mismatch(workspace):
            workspace["takes"][0]["source_binding"]["source_ref"] = "motu-m2:input-2"

        def take_non_recorder_lane(workspace):
            take = workspace["takes"][0]
            take["setup_id"] = "reference-focal"
            take["scene_id"] = "neutral"
            take["source_binding"] = {
                "lane_id": "playback",
                "source_ref": "pipewire-default-playback",
            }

        def take_end_before_start(workspace):
            workspace["takes"][0]["ended_at"] = "2026-08-01T23:59:59+00:00"

        def finalized_take_without_end(workspace):
            workspace["takes"][0]["ended_at"] = None

        def recording_with_finalization(workspace):
            workspace["takes"][0]["file_status"] = "recording"

        def monitoring_target_contradiction(workspace):
            workspace["takes"][0]["monitoring"]["target_ref"] = "motu-m2:output"

        def truth_depth_conflation(workspace):
            workspace["setups"][0]["truth"]["observed"]["depth"] = "focus"

        def free_graph(workspace):
            workspace["connections"] = []

        def wrong_model_hash(workspace):
            workspace["product_model_sha256"] = "f" * 64

        cases = [
            ("two-active", second_setup_active, "more than one setup is active"),
            ("active-id-mismatch", active_id_mismatch, "active setup id differs"),
            (
                "active-without-lane",
                active_without_lane,
                "active setup has no signal lane",
            ),
            ("unknown-profile", unknown_profile, "profile ref is unknown"),
            (
                "duplicate-profile",
                duplicate_profile,
                "profile_refs contains duplicates",
            ),
            ("unknown-module", unknown_module, "not in the internal catalog"),
            (
                "incompatible-signal-chain",
                incompatible_signal_chain,
                "rejects signal type audio-stereo",
            ),
            (
                "target-signal-mismatch",
                target_signal_mismatch,
                "ends with signal type audio-stereo, expected audio-mono",
            ),
            (
                "forbidden-modulation-target",
                forbidden_modulation_target,
                "modulation target kind is forbidden",
            ),
            ("scene-routing-mutation", scene_routing_mutation, "keys differ"),
            (
                "duplicate-macro-override",
                duplicate_macro_override,
                "duplicates a macro override",
            ),
            ("mutable-take", mutable_take, "must be immutable"),
            (
                "take-source-mismatch",
                take_source_mismatch,
                "source binding differs from its setup lane",
            ),
            (
                "take-non-recorder-lane",
                take_non_recorder_lane,
                "must bind to a recorder lane",
            ),
            ("take-end-before-start", take_end_before_start, "ends before it starts"),
            (
                "finalized-take-without-end",
                finalized_take_without_end,
                "finalized take has no end time",
            ),
            (
                "recording-with-finalization",
                recording_with_finalization,
                "recording state must not be finalized",
            ),
            (
                "monitoring-target-contradiction",
                monitoring_target_contradiction,
                "monitoring target must be absent for unknown",
            ),
            ("truth-depth-conflation", truth_depth_conflation, "keys differ"),
            ("free-graph", free_graph, "forbidden free-graph/DAW keys"),
            ("wrong-model-hash", wrong_model_hash, "product model binding differs"),
        ]
        self.assertEqual(len(cases), 21)
        for name, mutate, expected_error in cases:
            with self.subTest(name=name):
                workspace = copy.deepcopy(self.valid_workspace)
                mutate(workspace)
                with self.assertRaisesRegex(MODULE.ContractError, expected_error):
                    MODULE.validate_workspace(workspace, self.contract)

    def test_strict_json_rejects_duplicate_keys_and_non_finite_numbers(self) -> None:
        with self.assertRaisesRegex(MODULE.ContractError, "duplicate key"):
            MODULE.read_json_regular(
                FIXTURES / "duplicate-key.json", "duplicate fixture"
            )
        with self.assertRaisesRegex(MODULE.ContractError, "non-finite JSON number"):
            MODULE.read_json_regular(
                FIXTURES / "non-finite-number.json", "non-finite fixture"
            )
        with self.assertRaisesRegex(MODULE.ContractError, "canonical JSON"):
            MODULE.canonical_json({"value": float("nan")})

    def test_unhashable_user_values_fail_as_contract_errors(self) -> None:
        workspace = copy.deepcopy(self.valid_workspace)
        workspace["setups"][0]["lanes"][0]["source"]["kind"] = []
        with self.assertRaisesRegex(MODULE.ContractError, "must be a non-empty string"):
            MODULE.validate_workspace(workspace, self.contract)

    def test_templates_reference_only_existing_profiles(self) -> None:
        for template in self.contract["templates"].values():
            self.assertTrue(
                set(template["profile_refs"]) <= self.contract["profile_ids"]
            )

    def test_schema_documents_reject_unknown_root_properties(self) -> None:
        for path in (MODULE.PRODUCT_SCHEMA_PATH, MODULE.WORKSPACE_SCHEMA_PATH):
            schema = MODULE.read_json_regular(path, "schema")
            self.assertEqual(
                schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(schema["type"], "object")
            self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_model_rejects_wrong_schema_and_profile_bindings(self) -> None:
        model = copy.deepcopy(self.contract["model"])
        model["schema_bindings"]["workspace_state"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(MODULE.ContractError, "schema digest differs"):
            MODULE.validate_product_model(model)
        model = copy.deepcopy(self.contract["model"])
        model["profile_catalog_binding"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(
            MODULE.ContractError, "profile catalog digest differs"
        ):
            MODULE.validate_product_model(model)

    def test_safe_reader_rejects_file_and_parent_symlinks_oversize_and_non_object(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            real = root / "real"
            real.mkdir()
            regular = real / "regular.json"
            regular.write_text("{}\n")
            file_link = root / "link.json"
            parent_link = root / "linked-parent"
            try:
                file_link.symlink_to(regular)
                parent_link.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            for path in (file_link, parent_link / "regular.json"):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(
                        MODULE.ContractError, "cannot be opened safely"
                    ):
                        MODULE.read_json_regular(path, "linked fixture")
            oversized = root / "oversized.json"
            oversized.write_text('{"payload":"' + ("x" * 256) + '"}\n')
            with self.assertRaisesRegex(MODULE.ContractError, "exceeds 64 bytes"):
                MODULE.read_json_regular(
                    oversized, "oversized fixture", maximum_bytes=64
                )
            array = root / "array.json"
            array.write_text("[]\n")
            with self.assertRaisesRegex(MODULE.ContractError, "root must be an object"):
                MODULE.read_json_regular(array, "array fixture")

    def test_cli_emits_stable_json_for_success_and_error(self) -> None:
        success = subprocess.run(
            [sys.executable, str(MODULE_PATH), "check"],
            cwd=ROOT,
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        payload = json.loads(success.stdout)
        self.assertEqual(payload["kind"], "audiozentrale_product_model_check")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["model_sha256"], self.contract["model_sha256"])

        with tempfile.TemporaryDirectory() as temporary:
            invalid_workspace = copy.deepcopy(self.valid_workspace)
            invalid_workspace["connections"] = []
            invalid_path = pathlib.Path(temporary) / "free-graph.json"
            invalid_path.write_text(
                json.dumps(invalid_workspace, ensure_ascii=False) + "\n"
            )
            failure = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "validate",
                    str(invalid_path),
                ],
                cwd=ROOT,
                check=False,
                text=True,
                capture_output=True,
            )
        self.assertEqual(failure.returncode, 1)
        error = json.loads(failure.stdout)
        self.assertEqual(error["kind"], "audiozentrale_product_model_error")
        self.assertIn("forbidden free-graph/DAW keys", error["error"])


if __name__ == "__main__":
    unittest.main()
