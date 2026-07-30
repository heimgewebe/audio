import copy
import datetime as dt
import importlib.util
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "device_loss_exercise_test_module",
    ROOT / "scripts/device_loss_exercise.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class DeviceLossExerciseTests(unittest.TestCase):
    def identity(self, device="motu_m2", *, marker="a", strength="serial"):
        spec = MODULE.DEVICE_SPECS[device]
        if strength == "serial":
            key = {
                "vendor_id": spec["vendor_id"],
                "model_id": spec["model_id"],
                "serial_sha256": marker * 64,
            }
            serial_sha = marker * 64
            bus_number = "1"
            port_path = "9.2"
        else:
            key = {
                "vendor_id": spec["vendor_id"],
                "model_id": spec["model_id"],
                "bus_number": "1",
                "port_path": "9.3",
            }
            serial_sha = None
            bus_number = "1"
            port_path = "9.3"
        return {
            "device": device,
            "vendor_id": spec["vendor_id"],
            "model_id": spec["model_id"],
            "identity_strength": strength,
            "serial_sha256": serial_sha,
            "bus_number": bus_number,
            "port_path": port_path,
            "manufacturer_sha256": "b" * 64,
            "product_sha256": "c" * 64,
            "fingerprint": MODULE.sha256_json(key),
        }

    def snapshot(
        self,
        device,
        observed_at,
        *,
        present,
        identity=None,
    ):
        matches = []
        if present:
            matches = [
                {
                    "identity": identity or self.identity(device),
                    "control_name_sha256": "d" * 64,
                    "sysfs_path_sha256": "e" * 64,
                }
            ]
        value = {
            "schema_version": 1,
            "kind": "audio_device_presence_snapshot",
            "device": device,
            "observed_at": observed_at,
            "complete": True,
            "present": present,
            "ambiguous": False,
            "match_count": len(matches),
            "control_count": 4,
            "control_listing_sha256": "f" * 64,
            "errors": [],
            "matches": matches,
        }
        value["observation_sha256"] = MODULE.sha256_json(value)
        return value

    def evidence(self, device="motu_m2", *, strength="serial"):
        base = dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.timezone.utc)
        identity = self.identity(device, strength=strength)
        baseline = self.snapshot(device, base.isoformat(), present=True, identity=identity)
        loss_first = self.snapshot(
            device,
            (base + dt.timedelta(seconds=1)).isoformat(),
            present=False,
        )
        loss_confirmed = self.snapshot(
            device,
            (base + dt.timedelta(seconds=2)).isoformat(),
            present=False,
        )
        recovery_first = self.snapshot(
            device,
            (base + dt.timedelta(seconds=3)).isoformat(),
            present=True,
            identity=identity,
        )
        recovery_confirmed = self.snapshot(
            device,
            (base + dt.timedelta(seconds=4)).isoformat(),
            present=True,
            identity=identity,
        )
        return {
            "schema_version": 1,
            "kind": "audio_device_loss_exercise",
            "device": device,
            "result": "pass",
            "measured_at": (base + dt.timedelta(seconds=5)).isoformat(),
            "exercise_started_at": base.isoformat(),
            "exercise_ended_at": (base + dt.timedelta(seconds=5)).isoformat(),
            "duration_seconds": 5.0,
            "loss_timeout_seconds": 60,
            "recovery_timeout_seconds": 60,
            "baseline": baseline,
            "loss": {"first": loss_first, "confirmed": loss_confirmed},
            "recovery": {
                "first": recovery_first,
                "confirmed": recovery_confirmed,
            },
            "identity_strength": strength,
            "baseline_identity_fingerprint": identity["fingerprint"],
            "recovery_identity_fingerprint": identity["fingerprint"],
            "identity_changed": False,
            "blockers": [],
            "implementation": {
                "device_loss_exercise_sha256": MODULE.sha256_file(
                    ROOT / "scripts/device_loss_exercise.py"
                ),
                "system_truth_sha256": MODULE.sha256_file(
                    ROOT / "scripts/system_truth.py"
                ),
            },
        }

    def test_identity_uses_serial_or_model_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for key, value in {
                "idVendor": "07fd",
                "idProduct": "0008",
                "serial": "M20000062566",
                "busnum": "1",
                "devpath": "9.2",
                "manufacturer": "MOTU",
                "product": "M2",
            }.items():
                (root / key).write_text(value)
            motu = MODULE._identity_for("motu_m2", root)
            self.assertEqual(motu["identity_strength"], "serial")
            self.assertIsNotNone(motu["serial_sha256"])
            (root / "serial").unlink()
            for key, value in {
                "idVendor": "0582",
                "idProduct": "01b1",
                "devpath": "9.3",
                "manufacturer": "Roland",
                "product": "Roland Digital Piano",
            }.items():
                (root / key).write_text(value)
            roland = MODULE._identity_for("roland_fp_30x", root)
            self.assertEqual(roland["identity_strength"], "model-port")
            self.assertIsNone(roland["serial_sha256"])

    def test_motu_without_serial_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for key, value in {
                "idVendor": "07fd",
                "idProduct": "0008",
                "busnum": "1",
                "devpath": "9.2",
                "manufacturer": "MOTU",
                "product": "M2",
            }.items():
                (root / key).write_text(value)
            with self.assertRaisesRegex(ValueError, "requires its USB serial"):
                MODULE._identity_for("motu_m2", root)

    def test_observe_passes_only_after_loss_and_matching_recovery(self):
        evidence = self.evidence()
        baseline = evidence["baseline"]
        loss = evidence["loss"]
        recovery = evidence["recovery"]
        started = MODULE.parse_timestamp(evidence["exercise_started_at"], "started")
        ended = MODULE.parse_timestamp(evidence["exercise_ended_at"], "ended")
        with (
            mock.patch.object(MODULE, "scan_device", return_value=baseline),
            mock.patch.object(
                MODULE,
                "_confirmed_absence",
                return_value=(loss["first"], loss["confirmed"]),
            ),
            mock.patch.object(
                MODULE,
                "_confirmed_recovery",
                return_value=(recovery["first"], recovery["confirmed"]),
            ),
            mock.patch.object(MODULE, "utc_now", side_effect=[started, ended]),
            mock.patch.object(MODULE, "monotonic_now", side_effect=[10.0, 15.0]),
        ):
            observed = MODULE.observe_exercise("motu_m2", 60, 60)
        self.assertEqual(observed["result"], "pass")
        self.assertFalse(observed["identity_changed"])
        MODULE.validate_evidence(observed)

    def test_loss_not_observed_is_structured_fail(self):
        evidence = self.evidence()
        started = MODULE.parse_timestamp(evidence["exercise_started_at"], "started")
        ended = MODULE.parse_timestamp(evidence["exercise_ended_at"], "ended")
        with (
            mock.patch.object(MODULE, "scan_device", return_value=evidence["baseline"]),
            mock.patch.object(MODULE, "_confirmed_absence", return_value=(None, None)),
            mock.patch.object(MODULE, "utc_now", side_effect=[started, ended]),
            mock.patch.object(MODULE, "monotonic_now", side_effect=[10.0, 11.0]),
        ):
            observed = MODULE.observe_exercise("motu_m2", 0, 0)
        self.assertEqual(observed["result"], "fail")
        self.assertEqual(observed["blockers"], ["device-loss-not-observed"])

    def test_changed_identity_fails_and_tampering_is_rejected(self):
        evidence = self.evidence()
        changed = copy.deepcopy(evidence["recovery"])
        other = self.identity("motu_m2", marker="9")
        changed["first"] = self.snapshot(
            "motu_m2",
            changed["first"]["observed_at"],
            present=True,
            identity=other,
        )
        changed["confirmed"] = self.snapshot(
            "motu_m2",
            changed["confirmed"]["observed_at"],
            present=True,
            identity=other,
        )
        started = MODULE.parse_timestamp(evidence["exercise_started_at"], "started")
        ended = MODULE.parse_timestamp(evidence["exercise_ended_at"], "ended")
        with (
            mock.patch.object(MODULE, "scan_device", return_value=evidence["baseline"]),
            mock.patch.object(
                MODULE,
                "_confirmed_absence",
                return_value=(evidence["loss"]["first"], evidence["loss"]["confirmed"]),
            ),
            mock.patch.object(
                MODULE,
                "_confirmed_recovery",
                return_value=(changed["first"], changed["confirmed"]),
            ),
            mock.patch.object(MODULE, "utc_now", side_effect=[started, ended]),
            mock.patch.object(MODULE, "monotonic_now", side_effect=[10.0, 15.0]),
        ):
            observed = MODULE.observe_exercise("motu_m2", 60, 60)
        self.assertEqual(observed["result"], "fail")
        self.assertEqual(observed["blockers"], ["device-identity-changed"])
        tampered = self.evidence()
        tampered["baseline"]["present"] = False
        with self.assertRaisesRegex(ValueError, "observation digest mismatch|presence"):
            MODULE.validate_evidence(tampered)

    def test_private_state_resolves_and_code_drift_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "device-loss.json"
            state = MODULE.empty_state()
            evidence = self.evidence()
            MODULE.record_exercise(state, "motu_m2", evidence)
            MODULE.atomic_write_private(path, state)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            loaded = MODULE.read_state(path)
            matching = evidence["recovery"]["confirmed"]
            absent = self.snapshot(
                "roland_fp_30x",
                "2026-07-30T12:06:00+00:00",
                present=False,
            )
            with mock.patch.object(
                MODULE,
                "scan_device",
                side_effect=lambda device: matching if device == "motu_m2" else absent,
            ):
                projection = MODULE.truth_projection(path)
            self.assertIn("motu_m2", projection["resolved"])
            self.assertIn("roland_fp_30x", projection["unresolved"])
            loaded["receipts"]["motu_m2"]["evidence"]["implementation"][
                "system_truth_sha256"
            ] = "0" * 64
            loaded["receipts"]["motu_m2"]["evidence_sha256"] = MODULE.sha256_json(
                loaded["receipts"]["motu_m2"]["evidence"]
            )
            MODULE.atomic_write_private(path, loaded)
            with mock.patch.object(
                MODULE,
                "scan_device",
                side_effect=lambda device: matching if device == "motu_m2" else absent,
            ):
                invalidated = MODULE.truth_projection(path)
            self.assertEqual(
                invalidated["invalidated"]["motu_m2"],
                "implementation-changed",
            )

    def test_sound_class_scan_error_degrades_without_false_presence(self):
        with mock.patch.object(
            MODULE,
            "_control_entries",
            side_effect=OSError("unavailable"),
        ):
            snapshot = MODULE.scan_device("motu_m2")
        self.assertFalse(snapshot["complete"])
        self.assertFalse(snapshot["present"])
        self.assertEqual(snapshot["errors"], ["sound-class-scan-failed"])

    def test_private_io_rejects_symlink_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "target"
            target.mkdir()
            direct = target / "state.json"
            MODULE.atomic_write_private(direct, MODULE.empty_state())
            alias = root / "alias"
            alias.symlink_to(target, target_is_directory=True)
            with self.assertRaises(OSError):
                MODULE.atomic_write_private(
                    alias / "new.json",
                    MODULE.empty_state(),
                )
            with self.assertRaises(OSError):
                MODULE.load_json(alias / "state.json", MODULE.MAX_STATE_BYTES)


if __name__ == "__main__":
    unittest.main()
