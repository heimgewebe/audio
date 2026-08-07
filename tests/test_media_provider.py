import copy
import importlib.util
import inspect
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "media_provider", ROOT / "scripts" / "media_provider.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class MediaProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = MODULE.SimulatedPlaylistProvider()
        self.target = {
            "provider": "qobuz",
            "account": "account-main",
            "playlist_id": "playlist-42",
        }
        self.preimage = self.provider.seed_playlist(
            **self.target,
            revision="revision-1",
            tracks=["qobuz:track:1"],
        )

    def manifest(self, refs, *, operation="add", dry_run=False):
        return MODULE.normalize_import(
            refs,
            input_format="provider-refs",
            operation=operation,
            dry_run=dry_run,
            existing_tracks=self.provider.export_playlist(**self.target)["tracks"],
        )

    def plan(self, refs, *, operation="add", dry_run=False):
        return MODULE.build_write_plan(
            self.provider,
            **self.target,
            import_manifest=self.manifest(refs, operation=operation, dry_run=dry_run),
        )

    def test_catalog_matches_phase2_decision_and_denies_live_effects(self):
        catalog = MODULE.load_catalog()
        decisions = json.loads(
            (ROOT / "inventory" / "audio-architecture-decisions.v1.json").read_text()
        )
        qobuz = catalog["providers"]["qobuz"]
        phase2 = decisions["decisions"]["qobuz_mopidy"]
        self.assertEqual(qobuz["adapter_role"], phase2["mopidy_role"])
        self.assertEqual(qobuz["general_audio_core"], phase2["general_audio_core"])
        self.assertEqual(
            qobuz["exclusive_or_bitperfect_claim"],
            phase2["exclusive_or_bitperfect_claim"],
        )
        self.assertTrue(all(value is False for value in catalog["live_effects"].values()))
        self.assertEqual(qobuz["write_authority"], "simulation-only-t005")

    def test_core_source_has_no_provider_or_transport_implementation(self):
        source = pathlib.Path(MODULE.__file__).read_text().casefold()
        for forbidden in (
            "mopidy",
            "qobuz",
            "jsonrpc",
            "urllib",
            "requests",
            "socket",
            "subprocess",
            "http://",
            "https://",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertFalse(hasattr(MODULE, "main"))

    def test_schema_and_catalog_are_json_and_schema_binds_t005_boundary(self):
        schema = json.loads(
            (ROOT / "schemas" / "media-provider-catalog.v1.schema.json").read_text()
        )
        catalog = json.loads(
            (ROOT / "profiles" / "media-providers.v1.json").read_text()
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(
            schema["properties"]["live_effects"]["properties"][
                "playlist_mutation"
            ]["const"],
            False,
        )
        self.assertEqual(catalog["decision_binding"]["task_id"], "AUDIO-CONTROL-PLANE-V1-T002")

    def test_track_format_proof_binds_title_codec_rates_and_mixing(self):
        identity = {
            "provider": "qobuz",
            "kind": "track",
            "item_id": "123",
            "title": "E\u0301tude",
            "artists": ["Artist"],
            "album": "Album",
        }
        proof = MODULE.build_track_format_proof(
            identity,
            container="FLAC",
            codec="FLAC",
            track_rate_hz=96000,
            graph_rate_hz=96000,
            endpoint_rate_hz=96000,
            resampling="none",
            parallel_mixing="absent",
        )
        self.assertEqual(proof["track_identity"]["title"], "Étude")
        self.assertEqual(
            proof["track_identity_sha256"],
            MODULE.sha256_json(proof["track_identity"]),
        )
        self.assertEqual(proof["container"], "flac")
        self.assertEqual(proof["codec"], "flac")
        self.assertEqual(proof["track_rate_hz"], 96000)
        self.assertEqual(proof["parallel_mixing"], "absent")
        self.assertEqual(MODULE.validate_track_format_proof(proof), proof)

    def test_track_format_proof_rejects_tamper_and_false_no_resampling_claim(self):
        identity = {
            "provider": "qobuz",
            "kind": "track",
            "item_id": "123",
            "title": "Track",
            "artists": ["Artist"],
            "album": "Album",
        }
        with self.assertRaisesRegex(MODULE.MediaProviderError, "contradicts"):
            MODULE.build_track_format_proof(
                identity,
                container="flac",
                codec="flac",
                track_rate_hz=44100,
                graph_rate_hz=48000,
                endpoint_rate_hz=48000,
                resampling="none",
                parallel_mixing="unknown",
            )
        proof = MODULE.build_track_format_proof(
            identity,
            container="flac",
            codec="flac",
            track_rate_hz=44100,
            graph_rate_hz=48000,
            endpoint_rate_hz=48000,
            resampling="single-stage",
            parallel_mixing="present",
        )
        tampered = copy.deepcopy(proof)
        tampered["track_identity"]["title"] = "Other"
        with self.assertRaisesRegex(MODULE.MediaProviderError, "digest"):
            MODULE.validate_track_format_proof(tampered)

    def test_text_import_reports_duplicates_existing_items_and_errors(self):
        manifest = MODULE.normalize_import(
            "# comment\r\nqobuz:track:1\r\n qobuz:track:2 \nqobuz:track:2\ninvalid",
            input_format="text",
            operation="add",
            dry_run=True,
            existing_tracks=["qobuz:track:1"],
        )
        self.assertEqual(manifest["imported_tracks"], ["qobuz:track:1", "qobuz:track:2"])
        self.assertEqual(manifest["desired_tracks"], ["qobuz:track:1", "qobuz:track:2"])
        self.assertEqual(manifest["skipped_existing"], ["qobuz:track:1"])
        self.assertEqual(len(manifest["duplicates"]), 1)
        self.assertEqual(manifest["errors"][0]["code"], "invalid-provider-ref")
        self.assertEqual(MODULE.validate_import_manifest(manifest), manifest)

    def test_json_and_provider_ref_imports_normalize_to_same_replace_content(self):
        json_manifest = MODULE.normalize_import(
            json.dumps(
                {
                    "tracks": [
                        {"provider": "qobuz", "kind": "track", "item_id": "2"},
                        {"ref": "qobuz:track:3"},
                    ]
                }
            ),
            input_format="json",
            operation="replace",
            dry_run=True,
            existing_tracks=["qobuz:track:1"],
        )
        refs_manifest = MODULE.normalize_import(
            ["qobuz:track:2", "qobuz:track:3"],
            input_format="provider-refs",
            operation="replace",
            dry_run=True,
            existing_tracks=["qobuz:track:1"],
        )
        self.assertEqual(json_manifest["desired_tracks"], refs_manifest["desired_tracks"])
        self.assertEqual(json_manifest["desired_tracks"], ["qobuz:track:2", "qobuz:track:3"])

    def test_malformed_json_is_reported_and_blocks_write_plan(self):
        manifest = MODULE.normalize_import(
            "{broken",
            input_format="json",
            operation="replace",
            dry_run=True,
            existing_tracks=["qobuz:track:1"],
        )
        self.assertEqual(manifest["errors"], [{"index": None, "code": "invalid-json"}])
        with self.assertRaisesRegex(MODULE.MediaProviderError, "contains errors"):
            MODULE.build_write_plan(
                self.provider, **self.target, import_manifest=manifest
            )

    def test_write_plan_binds_target_revision_and_full_preimage_export(self):
        plan = self.plan(["qobuz:track:2"])
        self.assertEqual(plan["target"], self.target)
        self.assertEqual(plan["expected_revision"], "revision-1")
        self.assertEqual(plan["preimage_export"], self.preimage)
        self.assertEqual(plan["preimage_export_sha256"], self.preimage["snapshot_sha256"])
        self.assertEqual(plan["preimage_export"]["tracks"], ["qobuz:track:1"])
        self.assertEqual(
            plan["preimage_export"]["content_sha256"],
            MODULE.playlist_content_sha256(["qobuz:track:1"]),
        )
        self.assertEqual(MODULE.validate_write_plan(plan), plan)

    def test_write_plan_rejects_stale_import_manifest_and_cross_provider_ref(self):
        stale = MODULE.normalize_import(
            ["qobuz:track:2"],
            input_format="provider-refs",
            operation="add",
            dry_run=False,
            existing_tracks=[],
        )
        with self.assertRaisesRegex(MODULE.MediaProviderError, "stale"):
            MODULE.build_write_plan(self.provider, **self.target, import_manifest=stale)
        foreign = MODULE.normalize_import(
            ["other:track:2"],
            input_format="provider-refs",
            operation="add",
            dry_run=False,
            existing_tracks=["qobuz:track:1"],
        )
        with self.assertRaisesRegex(MODULE.MediaProviderError, "another provider"):
            MODULE.build_write_plan(self.provider, **self.target, import_manifest=foreign)

    def test_write_plan_binds_canonical_catalog_and_has_no_override(self):
        self.assertNotIn("catalog", inspect.signature(MODULE.build_write_plan).parameters)
        plan = self.plan(["qobuz:track:2"])
        self.assertEqual(plan["catalog_sha256"], MODULE.sha256_json(MODULE.load_catalog()))

        original_catalog_path = MODULE.CATALOG_PATH
        with tempfile.TemporaryDirectory() as directory:
            changed = copy.deepcopy(MODULE.load_catalog())
            changed["providers"]["qobuz"]["adapter"] = "mopidy-qobuz-hires-v2"
            changed_path = pathlib.Path(directory) / "media-providers.v1.json"
            changed_path.write_text(json.dumps(changed), encoding="utf-8")
            MODULE.CATALOG_PATH = changed_path
            try:
                with self.assertRaisesRegex(MODULE.MediaProviderError, "catalog binding"):
                    MODULE.validate_write_plan(plan)
            finally:
                MODULE.CATALOG_PATH = original_catalog_path

    def test_catalog_decision_binding_stays_outside_provider_neutral_core(self):
        catalog = MODULE.load_catalog()
        decisions = json.loads(
            (ROOT / "inventory" / "audio-architecture-decisions.v1.json").read_text()
        )
        phase2 = decisions["decisions"]["qobuz_mopidy"]
        binding = catalog["decision_binding"]
        self.assertEqual(binding["qobuz_mopidy_role"], phase2["mopidy_role"])
        self.assertEqual(binding["general_audio_core"], phase2["general_audio_core"])
        self.assertEqual(
            binding["exclusive_or_bitperfect_claim"],
            phase2["exclusive_or_bitperfect_claim"],
        )

    def test_dry_run_plan_cannot_write(self):
        plan = self.plan(["qobuz:track:2"], dry_run=True)
        with self.assertRaisesRegex(MODULE.MediaProviderError, "dry-run"):
            MODULE.apply_write_plan(
                self.provider, plan, reviewed_plan_sha256=plan["plan_sha256"]
            )
        self.assertEqual(self.provider.write_count, 0)
        self.assertEqual(self.provider.export_playlist(**self.target), self.preimage)

    def test_apply_requires_exact_reviewed_plan_and_full_readback(self):
        plan = self.plan(["qobuz:track:2"])
        with self.assertRaisesRegex(MODULE.MediaProviderError, "reviewed plan"):
            MODULE.apply_write_plan(
                self.provider, plan, reviewed_plan_sha256="0" * 64
            )
        receipt = MODULE.apply_write_plan(
            self.provider, plan, reviewed_plan_sha256=plan["plan_sha256"]
        )
        self.assertEqual(receipt["result"], "applied")
        self.assertEqual(receipt["operations_applied"], 1)
        self.assertEqual(receipt["postimage"]["tracks"], ["qobuz:track:1", "qobuz:track:2"])
        self.assertEqual(
            receipt["readback_content_sha256"],
            MODULE.playlist_content_sha256(["qobuz:track:1", "qobuz:track:2"]),
        )
        self.assertEqual(MODULE.validate_write_receipt(receipt), receipt)

    def test_second_apply_of_same_plan_is_idempotent(self):
        plan = self.plan(["qobuz:track:2"])
        first = MODULE.apply_write_plan(
            self.provider, plan, reviewed_plan_sha256=plan["plan_sha256"]
        )
        second = MODULE.apply_write_plan(
            self.provider, plan, reviewed_plan_sha256=plan["plan_sha256"]
        )
        self.assertEqual(first["operations_applied"], 1)
        self.assertEqual(second["operations_applied"], 0)
        self.assertEqual(second["result"], "already-desired")
        self.assertEqual(self.provider.write_count, 1)

    def test_apply_blocks_stale_plan_after_external_content_change(self):
        plan = self.plan(["qobuz:track:2"])
        self.provider.replace_playlist(
            **self.target,
            expected_revision="revision-1",
            tracks=["qobuz:track:1", "qobuz:track:3"],
        )
        with self.assertRaisesRegex(MODULE.MediaProviderError, "stale"):
            MODULE.apply_write_plan(
                self.provider, plan, reviewed_plan_sha256=plan["plan_sha256"]
            )

    def test_replace_operation_replaces_complete_content(self):
        plan = self.plan(["qobuz:track:9"], operation="replace")
        receipt = MODULE.apply_write_plan(
            self.provider, plan, reviewed_plan_sha256=plan["plan_sha256"]
        )
        self.assertEqual(receipt["postimage"]["tracks"], ["qobuz:track:9"])

    def test_full_content_rollback_restores_preimage_and_is_idempotent(self):
        plan = self.plan(["qobuz:track:2"])
        receipt = MODULE.apply_write_plan(
            self.provider, plan, reviewed_plan_sha256=plan["plan_sha256"]
        )
        rollback = MODULE.rollback_write(
            self.provider,
            plan,
            receipt,
            expected_receipt_sha256=receipt["receipt_sha256"],
        )
        self.assertEqual(rollback["result"], "restored")
        self.assertEqual(rollback["operations_applied"], 1)
        self.assertEqual(rollback["restored"]["tracks"], self.preimage["tracks"])
        self.assertEqual(
            rollback["restored_content_sha256"], self.preimage["content_sha256"]
        )
        second = MODULE.rollback_write(
            self.provider,
            plan,
            receipt,
            expected_receipt_sha256=receipt["receipt_sha256"],
        )
        self.assertEqual(second["result"], "already-restored")
        self.assertEqual(second["operations_applied"], 0)
        self.assertEqual(self.provider.write_count, 2)

    def test_noop_receipt_cannot_rollback_external_desired_state(self):
        plan = self.plan(["qobuz:track:2"])
        self.provider.replace_playlist(
            **self.target,
            expected_revision="revision-1",
            tracks=["qobuz:track:1", "qobuz:track:2"],
        )
        receipt = MODULE.apply_write_plan(
            self.provider, plan, reviewed_plan_sha256=plan["plan_sha256"]
        )
        self.assertEqual(receipt["result"], "already-desired")
        self.assertEqual(receipt["operations_applied"], 0)
        with self.assertRaisesRegex(MODULE.MediaProviderError, "no-op write receipt"):
            MODULE.rollback_write(
                self.provider,
                plan,
                receipt,
                expected_receipt_sha256=receipt["receipt_sha256"],
            )
        self.assertEqual(
            self.provider.export_playlist(**self.target)["tracks"],
            ["qobuz:track:1", "qobuz:track:2"],
        )
        self.assertEqual(self.provider.write_count, 1)

    def test_rollback_refuses_drift_after_write(self):
        plan = self.plan(["qobuz:track:2"])
        receipt = MODULE.apply_write_plan(
            self.provider, plan, reviewed_plan_sha256=plan["plan_sha256"]
        )
        current = self.provider.export_playlist(**self.target)
        self.provider.replace_playlist(
            **self.target,
            expected_revision=current["revision"],
            tracks=["qobuz:track:77"],
        )
        with self.assertRaisesRegex(MODULE.MediaProviderError, "drift"):
            MODULE.rollback_write(
                self.provider,
                plan,
                receipt,
                expected_receipt_sha256=receipt["receipt_sha256"],
            )

    def test_rehashed_receipt_cannot_rebind_target_or_operation_count(self):
        plan = self.plan(["qobuz:track:2"])
        receipt = MODULE.apply_write_plan(
            self.provider, plan, reviewed_plan_sha256=plan["plan_sha256"]
        )
        forged_target = copy.deepcopy(receipt)
        forged_target["target"]["playlist_id"] = "playlist-other"
        forged_target["receipt_sha256"] = MODULE.sha256_json(
            {key: value for key, value in forged_target.items() if key != "receipt_sha256"}
        )
        with self.assertRaisesRegex(MODULE.MediaProviderError, "target drift"):
            MODULE.validate_write_receipt(forged_target)

        forged_count = copy.deepcopy(receipt)
        forged_count["operations_applied"] = 0
        forged_count["receipt_sha256"] = MODULE.sha256_json(
            {key: value for key, value in forged_count.items() if key != "receipt_sha256"}
        )
        with self.assertRaisesRegex(MODULE.MediaProviderError, "contradicts"):
            MODULE.validate_write_receipt(forged_count)

    def test_rollback_rejects_rehashed_receipt_with_different_valid_preimage(self):
        plan = self.plan(["qobuz:track:2"])
        receipt = MODULE.apply_write_plan(
            self.provider, plan, reviewed_plan_sha256=plan["plan_sha256"]
        )
        forged = copy.deepcopy(receipt)
        forged["preimage"] = MODULE.playlist_snapshot(
            **self.target,
            revision="revision-forged",
            tracks=["qobuz:track:88"],
        )
        forged["receipt_sha256"] = MODULE.sha256_json(
            {key: value for key, value in forged.items() if key != "receipt_sha256"}
        )
        with self.assertRaisesRegex(MODULE.MediaProviderError, "preimage binding"):
            MODULE.rollback_write(
                self.provider,
                plan,
                forged,
                expected_receipt_sha256=forged["receipt_sha256"],
            )

    def test_playlist_snapshot_rejects_duplicate_and_cross_provider_content(self):
        with self.assertRaises(MODULE.MediaProviderError):
            MODULE.playlist_snapshot(
                **self.target,
                revision="r1",
                tracks=["qobuz:track:1", "qobuz:track:1"],
            )
        with self.assertRaisesRegex(MODULE.MediaProviderError, "another provider"):
            MODULE.playlist_snapshot(
                **self.target,
                revision="r1",
                tracks=["other:track:1"],
            )

    def test_uncatalogued_provider_is_rejected_before_write(self):
        other = MODULE.SimulatedPlaylistProvider()
        other.seed_playlist(
            provider="other",
            account="a",
            playlist_id="p",
            revision="r1",
            tracks=["other:track:1"],
        )
        manifest = MODULE.normalize_import(
            ["other:track:2"],
            input_format="provider-refs",
            operation="add",
            dry_run=False,
            existing_tracks=["other:track:1"],
        )
        with self.assertRaisesRegex(MODULE.MediaProviderError, "not catalogued"):
            MODULE.build_write_plan(
                other,
                provider="other",
                account="a",
                playlist_id="p",
                import_manifest=manifest,
            )
        self.assertEqual(other.write_count, 0)


if __name__ == "__main__":
    unittest.main()
