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
        identity = {
            "vendor_id": "07fd",
            "product_id": "0008",
            "serial_sha256": "a" * 64,
            "node_name_sha256": "b" * 64,
            "bus_path_sha256": "c" * 64,
            "sample_format": "s32le",
            "sample_rate_hz": 48000,
            "channels": 2,
            "muted": False,
            "unity_volume": True,
        }
        identity["fingerprint"] = MODULE.canonical_value_sha256(identity)
        argv = list(MODULE.VOICE_PACTL_SOURCES_ARGV)

        def snapshot(observed_at, output_sha):
            value = {
                "schema_version": 1,
                "kind": "audio_voice_source_snapshot",
                "observed_at": observed_at,
                "complete": True,
                "present": True,
                "match_count": 1,
                "ambiguous": False,
                "errors": [],
                "identity": identity,
                "query": {
                    "argv": argv,
                    "argv_sha256": MODULE.canonical_value_sha256(argv),
                    "returncode": 0,
                    "complete": True,
                    "stdout_sha256": output_sha,
                    "stdout_total_bytes": 100,
                    "stderr_sha256": "d" * 64,
                },
            }
            value["observation_sha256"] = MODULE.canonical_value_sha256(value)
            return value

        command = {
            "executable": "/usr/bin/parecord",
            "fixed_arguments": list(MODULE.VOICE_PARECORD_FIXED_ARGUMENTS),
            "device_name_sha256": identity["node_name_sha256"],
            "output_role": "private-temporary-wav",
        }
        command["contract_sha256"] = MODULE.canonical_value_sha256(command)
        return {
            "schema_version": 1,
            "kind": "audio_level_measurement_evidence",
            "gate": "voice-level-measurement",
            "result": "pass",
            "measured_at": "2026-07-27T12:00:09+00:00",
            "physical_state_sha256": MODULE.sha256_file(physical),
            "source_wav": {"name": "voice.wav", "sha256": "e" * 64, "bytes": 100},
            "analysis": {
                "kind": "audio_level_analysis",
                "sample_rate_hz": 48000,
                "channels": 2,
                "bit_depth": 32,
                "duration_seconds": 8.0,
                "maximum_peak_dbfs": -9.0,
                "channels_analysis": [
                    {"channel": 1, "clipped_samples": 0},
                    {"channel": 2, "clipped_samples": 0},
                ],
            },
            "capture_observation": {
                "method": MODULE.VOICE_CAPTURE_METHOD,
                "before": snapshot("2026-07-27T12:00:00+00:00", "f" * 64),
                "after": snapshot("2026-07-27T12:00:09.200000+00:00", "1" * 64),
                "process": {
                    "method": MODULE.VOICE_CAPTURE_METHOD,
                    "requested_duration_seconds": 8,
                    "capture_started_at": "2026-07-27T12:00:00+00:00",
                    "capture_ended_at": "2026-07-27T12:00:09.100000+00:00",
                    "duration_seconds": 9.1,
                    "stream_ready": True,
                    "stream_ready_at": "2026-07-27T12:00:01+00:00",
                    "startup_seconds": 1.0,
                    "command": command,
                    "returncode": 0,
                    "accepted_returncodes": [0, -2],
                    "forced_kill": False,
                    "stderr_bytes": 0,
                    "stderr_sha256": "2" * 64,
                    "stderr_truncated": False,
                    "complete": True,
                },
                "stable_source_identity": True,
            },
            "implementation": {
                "voice_capture_observer_sha256": MODULE.sha256_file(
                    MODULE.VOICE_CAPTURE_OBSERVER_PATH
                ),
                "laboratory_gate_sha256": MODULE.sha256_file(
                    pathlib.Path(MODULE.__file__)
                ),
                "level_analyzer_sha256": MODULE.sha256_file(
                    MODULE.LEVEL_ANALYZER_PATH
                ),
                "system_truth_sha256": MODULE.sha256_file(
                    MODULE.SYSTEM_TRUTH_PATH
                ),
            },
            "criteria": {
                "peak_dbfs_range": [-12.0, -6.0],
                "maximum_clipped_samples_per_channel": 0,
                "minimum_capture_duration_seconds": MODULE.VOICE_MIN_CAPTURE_SECONDS,
                "maximum_capture_duration_seconds": MODULE.VOICE_MAX_CAPTURE_SECONDS,
                "maximum_startup_seconds": MODULE.VOICE_STARTUP_TIMEOUT_SECONDS,
                "required_sample_rate_hz": 48000,
                "required_channels": 2,
                "required_bit_depth": 32,
                "requires_motu_serial_identity": True,
                "requires_unity_capture_volume": True,
                "requires_stable_source_identity": True,
            },
            "blockers": [],
        }

    def xrun_evidence(
        self,
        *,
        xrun_delta=0,
        graph_fingerprint="c" * 64,
        rate_hz=48000,
        quantum_frames=128,
    ):
        started = "2026-07-27T12:00:00+00:00"
        ended = "2026-07-27T12:01:00+00:00"
        argv = list(MODULE.xrun_journal_argv(started, ended))
        graph = {
            "graph_fingerprint": graph_fingerprint,
            "rate_hz": rate_hz,
            "quantum_frames": quantum_frames,
        }
        return {
            "schema_version": 1,
            "kind": "pipewire_xrun_observation",
            "gate": "xrun-stability-test",
            "result": "pass",
            "measured_at": ended,
            "physical_state_sha256": None,
            "requested_duration_seconds": 60,
            "duration_seconds": 60,
            "observation_started_at": started,
            "observation_ended_at": ended,
            "xrun_delta": xrun_delta,
            "rate_hz": rate_hz,
            "quantum_frames": quantum_frames,
            "graph_fingerprint": graph_fingerprint,
            "graph_before": {
                **graph,
                "report_sha256": "a" * 64,
                "truth_chain_sha256": "b" * 64,
            },
            "graph_after": {
                **graph,
                "report_sha256": "d" * 64,
                "truth_chain_sha256": "e" * 64,
            },
            "journal": {
                "source": "journalctl-user-audio-units",
                "query_argv": argv,
                "query_argv_sha256": MODULE.canonical_value_sha256(argv),
                "returncode": 0,
                "stdout_sha256": "f" * 64,
                "stdout_total_bytes": 0,
                "stdout_truncated": False,
                "line_count": 0,
                "max_lines": MODULE.MAX_XRUN_JOURNAL_LINES,
                "xrun_line_count": xrun_delta,
                "xrun_lines_sha256": MODULE.canonical_value_sha256([]),
                "complete": True,
            },
        }

    def test_xrun_journal_window_covers_fractional_utc_bounds(self):
        argv = list(
            MODULE.xrun_journal_argv(
                "2026-07-27T14:00:00.900000+02:00",
                "2026-07-27T14:01:00.100000+02:00",
            )
        )
        since = argv.index("--since")
        until = argv.index("--until")
        self.assertEqual(argv[since + 1], "2026-07-27 12:00:00 UTC")
        self.assertEqual(argv[until + 1], "2026-07-27 12:01:01 UTC")


    def test_planned_profiles_do_not_invalidate_operational_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "profiles.json"
            base = {
                "schema_version": 1,
                "kind": "audio_profile_catalog",
                "profiles": {
                    "available": {"purpose": "test"},
                },
            }
            path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n")
            before = MODULE.operational_profile_catalog_sha256(path)
            base["profiles"]["future"] = {
                "purpose": "future",
                "operational_status": "planned",
            }
            path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n")
            after = MODULE.operational_profile_catalog_sha256(path)
            self.assertEqual(before, after)

    def test_private_state_preserves_legacy_policy_as_invalidated(self):
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
            recorded_at = "2026-07-27T12:01:00+00:00"
            state["gates"]["resampling-decision"] = {
                "status": "passed",
                "recorded_at": recorded_at,
                "evidence_sha256": MODULE.canonical_sha256(evidence),
                "physical_state_sha256": None,
                "evidence": evidence,
            }
            state["updated_at"] = recorded_at
            MODULE.atomic_write_private(state_path, state)
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            loaded = MODULE.read_state(state_path)
            resolved, invalidated = MODULE.gate_resolution(
                loaded, pathlib.Path(directory) / "missing-physical.json"
            )
            self.assertNotIn("resampling-decision", resolved)
            self.assertEqual(
                invalidated["resampling-decision"],
                "legacy-unbound-policy-evidence",
            )

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
        xrun = self.xrun_evidence(xrun_delta=1)
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
        base = self.xrun_evidence()
        for malformed in (False, 0.0, True, 1.0):
            with self.subTest(xrun_delta=malformed):
                evidence = {**base, "xrun_delta": malformed}
                with self.assertRaises(ValueError):
                    MODULE.validate_evidence("xrun-stability-test", evidence)
        MODULE.validate_evidence("xrun-stability-test", base)

    def test_xrun_requires_bound_journal_and_keeps_legacy_state_readable(self):
        evidence = self.xrun_evidence()
        MODULE.validate_evidence("xrun-stability-test", evidence)
        tampered = json.loads(json.dumps(evidence))
        tampered["journal"]["query_argv"][-1] = "7"
        with self.assertRaisesRegex(ValueError, "journal query"):
            MODULE.validate_evidence("xrun-stability-test", tampered)

        legacy = {
            "schema_version": 1,
            "kind": "pipewire_xrun_observation",
            "gate": "xrun-stability-test",
            "result": "pass",
            "measured_at": "2026-07-27T12:01:00+00:00",
            "physical_state_sha256": None,
            "duration_seconds": 60,
            "xrun_delta": 0,
            "rate_hz": 48000,
            "quantum_frames": 128,
            "graph_fingerprint": "c" * 64,
        }
        MODULE.validate_evidence(
            "xrun-stability-test", legacy, allow_legacy_xrun=True
        )
        with self.assertRaisesRegex(ValueError, "legacy XRun"):
            MODULE.validate_evidence("xrun-stability-test", legacy)

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
