import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "laboratory_gate", ROOT / "scripts/laboratory_gate.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class LaboratoryGateTests(unittest.TestCase):
    def physical_state(self, root):
        path = pathlib.Path(root) / "physical.json"
        MODULE.PHYSICAL.atomic_write_private(path, MODULE.PHYSICAL.empty_state())
        return path

    def voice_evidence(self, physical):
        return {
            "schema_version": 1,
            "kind": "audio_level_measurement_evidence",
            "gate": "voice-level-measurement",
            "result": "pass",
            "measured_at": "2026-07-27T12:00:00+00:00",
            "physical_state_sha256": MODULE.sha256_file(physical),
            "source_wav": {"sha256": "a" * 64, "bytes": 100},
            "analysis": {
                "kind": "audio_level_analysis",
                "sample_rate_hz": 48000,
                "maximum_peak_dbfs": -9.0,
                "channels_analysis": [{"channel": 1, "clipped_samples": 0}],
            },
        }

    def test_private_state_and_policy_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "gates.json"
            state = MODULE.empty_state()
            evidence = {
                "schema_version": 1,
                "kind": "audio_policy_decision",
                "gate": "resampling-decision",
                "result": "pass",
                "measured_at": "2026-07-27T12:00:00+00:00",
                "physical_state_sha256": None,
                "decision": "graph-48k",
                "justification": "Gemischter Betrieb verwendet kontrolliertes Resampling.",
            }
            MODULE.record_gate(
                state,
                "resampling-decision",
                evidence,
                pathlib.Path(directory) / "missing-physical.json",
            )
            MODULE.atomic_write_private(state_path, state)
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            loaded = MODULE.read_state(state_path)
            resolved, invalidated = MODULE.gate_resolution(
                loaded, pathlib.Path(directory) / "missing-physical.json"
            )
            self.assertIn("resampling-decision", resolved)
            self.assertEqual(invalidated, {})

    def test_physical_change_invalidates_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            physical = self.physical_state(directory)
            state = MODULE.empty_state()
            MODULE.record_gate(
                state,
                "voice-level-measurement",
                self.voice_evidence(physical),
                physical,
            )
            resolved, invalidated = MODULE.gate_resolution(state, physical)
            self.assertIn("voice-level-measurement", resolved)
            changed = MODULE.PHYSICAL.read_state(physical)
            MODULE.PHYSICAL.record_fact(
                changed, "rode_nt1a_connected", "true", "visual"
            )
            MODULE.PHYSICAL.atomic_write_private(physical, changed)
            resolved, invalidated = MODULE.gate_resolution(state, physical)
            self.assertNotIn("voice-level-measurement", resolved)
            self.assertEqual(
                invalidated["voice-level-measurement"], "physical-state-changed"
            )

    def test_rejects_duplicate_and_bad_voice_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            physical = self.physical_state(directory)
            state = MODULE.empty_state()
            evidence = self.voice_evidence(physical)
            MODULE.record_gate(state, "voice-level-measurement", evidence, physical)
            with self.assertRaises(ValueError):
                MODULE.record_gate(
                    state, "voice-level-measurement", evidence, physical
                )
            bad = json.loads(json.dumps(evidence))
            bad["analysis"]["maximum_peak_dbfs"] = -20.0
            with self.assertRaises(ValueError):
                MODULE.validate_evidence("voice-level-measurement", bad)

    def test_rejects_tampered_or_insecure_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "gates.json"
            MODULE.atomic_write_private(path, MODULE.empty_state())
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                MODULE.read_state(path)
            path.chmod(0o600)
            payload = json.loads(path.read_text())
            payload["catalog_sha256"] = "0" * 64
            path.write_text(json.dumps(payload))
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                MODULE.read_state(path)

    def test_strict_xrun_and_qobuz_validation(self):
        common = {
            "schema_version": 1,
            "result": "pass",
            "measured_at": "2026-07-27T12:00:00+00:00",
            "physical_state_sha256": None,
        }
        xrun = {
            **common,
            "kind": "pipewire_xrun_observation",
            "gate": "xrun-stability-test",
            "duration_seconds": 60,
            "xrun_delta": 1,
            "rate_hz": 48000,
            "quantum_frames": 128,
            "graph_fingerprint": "b" * 64,
        }
        with self.assertRaises(ValueError):
            MODULE.validate_evidence("xrun-stability-test", xrun)
        qobuz = {
            **common,
            "kind": "qobuz_rate_observation",
            "gate": "qobuz-rate-proof",
            "track_rate_hz": 96000,
            "graph_rate_hz": 96000,
            "endpoint_rate_hz": 96000,
            "resampling_observed": "unknown",
            "method": "observed endpoint and graph metadata",
            "graph_fingerprint": "c" * 64,
        }
        with self.assertRaises(ValueError):
            MODULE.validate_evidence("qobuz-rate-proof", qobuz)

    def test_rejects_nonhex_digest_and_resampled_qobuz(self):
        bad_voice = {
            "schema_version": 1,
            "kind": "audio_level_measurement_evidence",
            "gate": "voice-level-measurement",
            "result": "pass",
            "measured_at": "2026-07-27T12:00:00+00:00",
            "physical_state_sha256": "0" * 64,
            "source_wav": {"sha256": "z" * 64, "bytes": 100},
            "analysis": {
                "kind": "audio_level_analysis",
                "sample_rate_hz": 48000,
                "maximum_peak_dbfs": -9.0,
                "channels_analysis": [{"channel": 1, "clipped_samples": 0}],
            },
        }
        with self.assertRaises(ValueError):
            MODULE.validate_evidence("voice-level-measurement", bad_voice)
        qobuz = {
            "schema_version": 1,
            "kind": "qobuz_rate_observation",
            "gate": "qobuz-rate-proof",
            "result": "pass",
            "measured_at": "2026-07-27T12:00:00+00:00",
            "physical_state_sha256": None,
            "track_rate_hz": 96000,
            "track_fingerprint": "d" * 64,
            "graph_rate_hz": 48000,
            "endpoint_rate_hz": 48000,
            "resampling_observed": True,
            "method": "observed graph and endpoint metadata",
            "graph_fingerprint": "c" * 64,
        }
        with self.assertRaises(ValueError):
            MODULE.validate_evidence("qobuz-rate-proof", qobuz)

    def test_rejects_contradictory_loopback_latency(self):
        evidence = {
            "schema_version": 1,
            "kind": "audio_loopback_latency_evidence",
            "gate": "loopback-latency-measurement",
            "result": "pass",
            "measured_at": "2026-07-27T12:00:00+00:00",
            "physical_state_sha256": "0" * 64,
            "quantum_frames": 128,
            "graph_fingerprint": "1" * 64,
            "reference_wav": {"sha256": "a" * 64, "bytes": 100},
            "recorded_wav": {"sha256": "b" * 64, "bytes": 200},
            "analysis": {
                "kind": "audio_loopback_latency_result",
                "sample_rate_hz": 48000,
                "delay_samples": 24000,
                "round_trip_latency_ms": 1.0,
                "peak_snr_db": 40.0,
                "peak_detection_confidence": 1.0,
            },
        }
        with self.assertRaisesRegex(ValueError, "contradicts"):
            MODULE.validate_evidence("loopback-latency-measurement", evidence)

    def test_rejects_boolean_and_float_xrun_delta(self):
        base = {
            "schema_version": 1,
            "kind": "pipewire_xrun_observation",
            "gate": "xrun-stability-test",
            "result": "pass",
            "measured_at": "2026-07-27T12:00:00+00:00",
            "physical_state_sha256": None,
            "duration_seconds": 60,
            "rate_hz": 48000,
            "quantum_frames": 128,
            "graph_fingerprint": "c" * 64,
        }
        for malformed in (False, 0.0, True, 1.0):
            with self.subTest(xrun_delta=malformed):
                evidence = {**base, "xrun_delta": malformed}
                with self.assertRaises(ValueError):
                    MODULE.validate_evidence("xrun-stability-test", evidence)
        MODULE.validate_evidence(
            "xrun-stability-test", {**base, "xrun_delta": 0}
        )

    def test_rejects_zero_delay_or_identical_loopback_sources(self):
        evidence = {
            "schema_version": 1,
            "kind": "audio_loopback_latency_evidence",
            "gate": "loopback-latency-measurement",
            "result": "pass",
            "measured_at": "2026-07-27T12:00:00+00:00",
            "physical_state_sha256": "0" * 64,
            "quantum_frames": 128,
            "graph_fingerprint": "1" * 64,
            "reference_wav": {"sha256": "a" * 64, "bytes": 100},
            "recorded_wav": {"sha256": "a" * 64, "bytes": 100},
            "analysis": {
                "kind": "audio_loopback_latency_result",
                "sample_rate_hz": 48000,
                "delay_samples": 0,
                "round_trip_latency_ms": 0.0,
                "peak_snr_db": 40.0,
                "peak_detection_confidence": 1.0,
            },
        }
        with self.assertRaises(ValueError):
            MODULE.validate_evidence("loopback-latency-measurement", evidence)
        evidence["analysis"]["delay_samples"] = 480
        evidence["analysis"]["round_trip_latency_ms"] = 10.0
        with self.assertRaisesRegex(ValueError, "different bytes"):
            MODULE.validate_evidence("loopback-latency-measurement", evidence)


if __name__ == "__main__":
    unittest.main()
