import copy
import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "rate_policy_observer_test_module",
    ROOT / "scripts/rate_policy_observer.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RatePolicyObserverTests(unittest.TestCase):
    def endpoint(self, device, direction, *, rate_hz=None):
        if device == "motu_m2":
            vendor_id = "0x07fd"
            product_id = "0x0008"
            serial = "MOTU_M2_M20000062566"
            prefix = "MOTU_M2_M20000062566"
            sample_format = "s32le"
            default_rate = 48_000
        else:
            vendor_id = "0x0582"
            product_id = "0x01b1"
            serial = "Roland_Roland_Digital_Piano"
            prefix = "Roland_Roland_Digital_Piano"
            sample_format = "s24le"
            default_rate = 44_100
        name_prefix = "alsa_input" if direction == "source" else "alsa_output"
        return {
            "name": f"{name_prefix}.usb-{prefix}-00.test",
            "sample_specification": (
                f"{sample_format} 2ch {rate_hz or default_rate}Hz"
            ),
            "properties": {
                "device.vendor.id": vendor_id,
                "device.product.id": product_id,
                "device.serial": serial,
            },
        }

    def command_result(self, argv, payload):
        raw = json.dumps(payload)
        return types.SimpleNamespace(
            argv=argv,
            error=None,
            returncode=0,
            stdout=raw,
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_sha256=MODULE.sha256_text(raw),
            stdout_total_bytes=len(raw.encode()),
            stderr_sha256=MODULE.sha256_bytes(b""),
            stderr_total_bytes=0,
        )

    def endpoint_snapshot(self, *, roland_rate=44_100):
        source_result = self.command_result(
            MODULE.LAB.RATE_POLICY_PACTL_SOURCES_ARGV,
            [
                self.endpoint("motu_m2", "source"),
                self.endpoint(
                    "roland_fp_30x",
                    "source",
                    rate_hz=roland_rate,
                ),
            ],
        )
        sink_result = self.command_result(
            MODULE.LAB.RATE_POLICY_PACTL_SINKS_ARGV,
            [
                self.endpoint("motu_m2", "sink"),
                self.endpoint(
                    "roland_fp_30x",
                    "sink",
                    rate_hz=roland_rate,
                ),
            ],
        )
        with mock.patch.object(
            MODULE.SYSTEM_TRUTH,
            "run_read_only",
            side_effect=[source_result, sink_result],
        ):
            return MODULE.endpoint_snapshot()

    def truth_binding(self, *, rate_hz=48_000):
        return {
            "report_sha256": "a" * 64,
            "truth_chain_sha256": "b" * 64,
            "graph_fingerprint": "c" * 64,
            "graph_rate_hz": rate_hz,
            "graph_quantum_frames": 1024,
            "default_sink": "motu-m2",
            "default_source": "motu-m2",
            "hardware": {"motu_m2": True, "roland_fp_30x": True},
            "warning_codes": ["mixed-sample-rates"],
        }

    def evidence(self, gate, *, roland_rate=44_100, graph_rate=48_000):
        endpoints = self.endpoint_snapshot(roland_rate=roland_rate)
        with (
            mock.patch.object(MODULE, "endpoint_snapshot", return_value=endpoints),
            mock.patch.object(
                MODULE,
                "truth_binding",
                return_value=self.truth_binding(rate_hz=graph_rate),
            ),
        ):
            return MODULE.rate_policy_evidence(gate)

    def test_endpoint_snapshot_binds_motu_and_roland_rates_without_raw_ids(self):
        snapshot = self.endpoint_snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["rate_sets_hz"]["motu_m2"], [48_000])
        self.assertEqual(snapshot["rate_sets_hz"]["roland_fp_30x"], [44_100])
        serialized = json.dumps(snapshot)
        self.assertNotIn("M20000062566", serialized)
        self.assertNotIn("Roland_Roland_Digital_Piano", serialized)
        MODULE.LAB._validate_rate_endpoint_snapshot(snapshot)

    def test_monitor_sources_are_not_counted_as_capture_endpoints(self):
        monitor = self.endpoint("motu_m2", "source")
        monitor["monitor_source"] = "alsa_output.usb-MOTU.monitor"
        monitor["properties"]["device.class"] = "monitor"
        self.assertIsNone(MODULE._endpoint_identity(monitor, "source"))

    def test_both_bound_policy_decisions_validate(self):
        for gate in sorted(MODULE.LAB.RATE_POLICY_DECISIONS):
            evidence = self.evidence(gate)
            self.assertEqual(evidence["result"], "pass")
            self.assertEqual(evidence["blockers"], [])
            MODULE.LAB.validate_evidence(gate, evidence)

    def test_additional_hardware_and_missing_warning_do_not_break_binding(self):
        evidence = self.evidence("rate-policy-decision")
        evidence["truth"]["hardware"]["future_device"] = True
        evidence["truth"]["warning_codes"] = []
        MODULE.LAB.validate_evidence("rate-policy-decision", evidence)

    def test_roland_rate_or_graph_drift_fails_closed(self):
        roland = self.evidence("resampling-decision", roland_rate=48_000)
        self.assertEqual(roland["result"], "fail")
        self.assertIn("roland-rate-is-not-44k1", roland["blockers"])
        graph = self.evidence("rate-policy-decision", graph_rate=96_000)
        self.assertEqual(graph["result"], "fail")
        self.assertIn("current-graph-is-not-48k", graph["blockers"])

    def test_tampered_policy_profile_and_implementation_are_rejected(self):
        evidence = self.evidence("rate-policy-decision")
        tampered = copy.deepcopy(evidence)
        tampered["policy"]["default_graph_rate_hz"] = 96_000
        with self.assertRaisesRegex(ValueError, "payload"):
            MODULE.LAB.validate_evidence("rate-policy-decision", tampered)
        tampered = copy.deepcopy(evidence)
        tampered["profiles"]["selected_profiles_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "profile digest"):
            MODULE.LAB.validate_evidence("rate-policy-decision", tampered)
        tampered = copy.deepcopy(evidence)
        tampered["implementation"]["system_truth_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "implementation binding"):
            MODULE.LAB.validate_evidence("rate-policy-decision", tampered)

    def test_legacy_policy_is_readable_but_does_not_resolve(self):
        gate = "rate-policy-decision"
        legacy = {
            "schema_version": 1,
            "kind": "audio_policy_decision",
            "gate": gate,
            "result": "pass",
            "measured_at": "2026-07-30T18:00:00+00:00",
            "physical_state_sha256": None,
            "decision": "graph-48k",
            "justification": "Legacy free-text decision for migration only.",
        }
        MODULE.LAB.validate_evidence(
            gate,
            legacy,
            allow_legacy_policy=True,
        )
        with self.assertRaisesRegex(ValueError, "legacy policy evidence"):
            MODULE.LAB.validate_evidence(gate, legacy)
        state = MODULE.LAB.empty_state()
        state["gates"][gate] = {
            "status": "passed",
            "recorded_at": "2026-07-30T18:01:00+00:00",
            "evidence_sha256": MODULE.LAB.canonical_sha256(legacy),
            "physical_state_sha256": None,
            "evidence": legacy,
        }
        resolved, invalidated = MODULE.LAB.gate_resolution(
            state,
            pathlib.Path("/does/not/matter"),
        )
        self.assertNotIn(gate, resolved)
        self.assertEqual(
            invalidated[gate],
            "legacy-unbound-policy-evidence",
        )

    def test_binding_drift_remains_readable_but_does_not_resolve(self):
        gate = "resampling-decision"
        evidence = self.evidence(gate)
        evidence["decision"] = "historical-roland-resampling-policy"
        evidence["justification"] = (
            "Historical bound decision retained only for migration validation."
        )
        evidence["policy"] = {
            "source_rate_hz": 44_100,
            "target_graph_rate_hz": 48_000,
        }
        evidence["criteria"] = {"historical_contract": True}
        MODULE.LAB.validate_evidence(
            gate,
            evidence,
            allow_stale_policy=True,
        )
        with self.assertRaisesRegex(ValueError, "decision"):
            MODULE.LAB.validate_evidence(gate, evidence)
        state = MODULE.LAB.empty_state()
        state["gates"][gate] = {
            "status": "passed",
            "recorded_at": "2026-07-30T18:01:00+00:00",
            "evidence_sha256": MODULE.LAB.canonical_sha256(evidence),
            "physical_state_sha256": None,
            "evidence": evidence,
        }
        resolved, invalidated = MODULE.LAB.gate_resolution(
            state,
            pathlib.Path("/does/not/matter"),
        )
        self.assertNotIn(gate, resolved)
        self.assertEqual(invalidated[gate], "policy-binding-changed")


if __name__ == "__main__":
    unittest.main()
