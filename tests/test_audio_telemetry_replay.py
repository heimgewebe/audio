import contextlib
import copy
import hashlib
import io
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audio_telemetry_replay", ROOT / "scripts" / "audio_telemetry_replay.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class AudioTelemetryReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = MODULE.load_replay_contract()
        self.catalog = self.contract["catalog"]

    def test_catalog_is_hash_and_schema_bound(self) -> None:
        self.assertFalse(self.contract["authoritative"])
        self.assertEqual(self.contract["authority"], "synthetic-replay")
        self.assertEqual(
            self.contract["catalog_sha256"],
            hashlib.sha256(MODULE.CATALOG_PATH.read_bytes()).hexdigest(),
        )
        schema = json.loads(MODULE.SCHEMA_PATH.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(self.catalog)
        self.assertEqual(
            self.catalog["schema_binding"]["sha256"],
            hashlib.sha256(MODULE.SCHEMA_PATH.read_bytes()).hexdigest(),
        )

    def test_six_scenarios_cover_required_events_deterministically(self) -> None:
        self.assertEqual(
            tuple(item["id"] for item in self.catalog["scenarios"]),
            MODULE.SCENARIO_IDS,
        )
        self.assertEqual(
            sum(len(item["frames"]) for item in self.catalog["scenarios"]), 48
        )
        events = {
            frame["event"]
            for scenario in self.catalog["scenarios"]
            for frame in scenario["frames"]
        }
        self.assertTrue(
            {"midi", "clip", "xrun", "device-loss", "stale", "recovery"} <= events
        )
        first = MODULE.load_replay_contract()
        second = MODULE.load_replay_contract()
        self.assertEqual(MODULE.canonical_json(first), MODULE.canonical_json(second))

    def test_frame_semantics_reject_clip_xrun_stale_and_recovery_drift(self) -> None:
        cases = []
        clip = copy.deepcopy(self.catalog)
        clip["scenarios"][1]["frames"][3]["peak_dbfs"] = -20
        clip["scenarios"][1]["frames"][3]["rms_dbfs"] = -30
        cases.append((clip, "clipping boundary"))
        xrun = copy.deepcopy(self.catalog)
        xrun["scenarios"][2]["frames"][3]["xrun_total"] = 4
        cases.append((xrun, "does not increment"))
        stale = copy.deepcopy(self.catalog)
        stale["scenarios"][4]["frames"][5]["telemetry_age_ms"] = 1000
        cases.append((stale, "stale threshold"))
        recovery = copy.deepcopy(self.catalog)
        recovery["scenarios"][5]["frames"][-1]["device_state"] = "recovering"
        cases.append((recovery, "lost-to-online"))
        for catalog, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(MODULE.ReplayError, message),
            ):
                MODULE.validate_catalog(catalog)

    def test_frame_contract_rejects_nonmonotonic_or_inconsistent_values(self) -> None:
        cases = []
        rms = copy.deepcopy(self.catalog)
        rms["scenarios"][0]["frames"][0]["rms_dbfs"] = -20
        cases.append((rms, "RMS exceeds"))
        midi = copy.deepcopy(self.catalog)
        midi["scenarios"][0]["frames"][0]["midi_velocity"] = 20
        cases.append((midi, "velocity without"))
        offsets = copy.deepcopy(self.catalog)
        offsets["scenarios"][0]["frames"][1]["offset_ms"] = 251
        cases.append((offsets, "offset differs"))
        xrun = copy.deepcopy(self.catalog)
        xrun["scenarios"][2]["frames"][4]["xrun_total"] = 3
        cases.append((xrun, "not monotonic"))
        for catalog, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(MODULE.ReplayError, message),
            ):
                MODULE.validate_catalog(catalog)

    def test_catalog_rejects_product_and_schema_binding_drift(self) -> None:
        product = copy.deepcopy(self.catalog)
        product["product_model_sha256"] = "f" * 64
        with self.assertRaisesRegex(MODULE.ReplayError, "product model binding"):
            MODULE.validate_catalog(product)
        schema = copy.deepcopy(self.catalog)
        schema["schema_binding"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(MODULE.ReplayError, "schema digest differs"):
            MODULE.validate_catalog(schema)

    def test_reader_rejects_duplicate_nonfinite_oversize_and_symlink_chain(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"a":1,"a":2}')
            with self.assertRaisesRegex(MODULE.ReplayError, "duplicate JSON key"):
                MODULE.read_json_snapshot(duplicate, "duplicate")
            nonfinite = root / "nonfinite.json"
            nonfinite.write_text('{"a":NaN}')
            with self.assertRaisesRegex(MODULE.ReplayError, "non-finite"):
                MODULE.read_json_snapshot(nonfinite, "nonfinite")
            oversized = root / "oversized.json"
            oversized.write_text('{"a":"0123456789"}')
            with self.assertRaisesRegex(MODULE.ReplayError, "size contract"):
                MODULE.read_json_snapshot(oversized, "oversized", maximum_bytes=4)
            real = root / "real"
            real.mkdir()
            target = real / "catalog.json"
            target.write_text("{}")
            alias = root / "alias"
            os.symlink(real, alias)
            with self.assertRaisesRegex(MODULE.ReplayError, "contains a symlink"):
                MODULE.read_json_snapshot(alias / "catalog.json", "symlinked")
            unsafe = real / ".." / "real" / "catalog.json"
            with self.assertRaisesRegex(MODULE.ReplayError, "unsafe path component"):
                MODULE.read_json_snapshot(unsafe, "unsafe")
            with self.assertRaisesRegex(MODULE.ReplayError, "maximum size is invalid"):
                MODULE.read_json_snapshot(target, "boolean-limit", maximum_bytes=True)

    def test_catalog_hash_and_parser_share_one_snapshot(self) -> None:
        original = MODULE.read_json_snapshot
        calls: list[pathlib.Path] = []

        def tracked(path, label, *, maximum_bytes=MODULE.MAX_JSON_BYTES):
            calls.append(pathlib.Path(path))
            return original(path, label, maximum_bytes=maximum_bytes)

        with mock.patch.object(MODULE, "read_json_snapshot", side_effect=tracked):
            MODULE.load_replay_contract()
        self.assertEqual(calls.count(MODULE.CATALOG_PATH), 1)
        self.assertEqual(calls.count(MODULE.SCHEMA_PATH), 1)
        self.assertEqual(calls.count(MODULE.PRODUCT_MODEL_PATH), 1)

    def test_cli_is_stable_and_read_only(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(MODULE.main(["check"]), 0)
            self.assertEqual(MODULE.main(["show"]), 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(json.loads(lines[0])["status"], "ok")
        self.assertFalse(json.loads(lines[1])["authoritative"])
        source = MODULE.__file__ and pathlib.Path(MODULE.__file__).read_text()
        self.assertNotIn("subprocess", source)
        self.assertNotIn("socket", source)
        self.assertNotIn("systemctl", source)


if __name__ == "__main__":
    unittest.main()
