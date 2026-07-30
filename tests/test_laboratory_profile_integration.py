import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "profile_planner_laboratory", ROOT / "scripts/profile_planner.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class LaboratoryProfileIntegrationTests(unittest.TestCase):
    def doctor(self):
        return {
            "hardware": {"motu_m2": True, "roland_fp_30x": True},
            "graph": {
                "default_sink": "motu-m2",
                "default_source": "roland-fp-30x",
                "force_rate_hz": 48000,
                "force_quantum_frames": 1024,
            },
        }

    def test_policy_gate_makes_piano_recording_ready(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            MODULE, "doctor_report", self.doctor
        ):
            root = pathlib.Path(directory)
            physical = root / "physical.json"
            gates = root / "gates.json"
            MODULE.PHYSICAL.atomic_write_private(
                physical, MODULE.PHYSICAL.empty_state()
            )
            state = MODULE.LABORATORY.empty_state()
            evidence = {
                "schema_version": 1,
                "kind": "audio_policy_decision",
                "gate": "resampling-decision",
                "result": "pass",
                "measured_at": "2026-07-27T12:00:00+00:00",
                "physical_state_sha256": None,
                "decision": "graph-48k",
                "justification": "Gemischte Sitzungen verwenden kontrolliertes Resampling.",
            }
            MODULE.LABORATORY.record_gate(
                state, "resampling-decision", evidence, physical
            )
            MODULE.LABORATORY.atomic_write_private(gates, state)
            result = MODULE.plan(
                "piano-digital-recording", physical, gates
            )
            self.assertTrue(result["ready_for_laboratory_apply"])
            self.assertEqual(
                result["resolved_laboratory_gates"], ["resampling-decision"]
            )
            self.assertEqual(result["unresolved_laboratory_gates"], [])

    def test_live_profile_rejects_wrong_quantum_evidence(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            MODULE, "doctor_report", self.doctor
        ):
            root = pathlib.Path(directory)
            physical = root / "physical.json"
            gates = root / "gates.json"
            physical_state = MODULE.PHYSICAL.empty_state()
            MODULE.PHYSICAL.record_fact(
                physical_state,
                "focal_connected_output",
                "lake-people-front",
                "visual",
            )
            MODULE.PHYSICAL.atomic_write_private(physical, physical_state)
            physical_sha = MODULE.LABORATORY.sha256_file(physical)
            state = MODULE.LABORATORY.empty_state()
            planned_graph = MODULE.planned_graph_context(
                self.doctor()["graph"],
                MODULE.load_profiles()["piano-software-live"]["desired"],
            )
            loopback = {
                "schema_version": 1,
                "kind": "audio_loopback_latency_evidence",
                "gate": "loopback-latency-measurement",
                "result": "pass",
                "measured_at": "2026-07-27T12:00:00+00:00",
                "physical_state_sha256": physical_sha,
                "quantum_frames": 256,
                "graph_fingerprint": MODULE.LABORATORY.graph_fingerprint(
                    planned_graph
                ),
                "reference_wav": {"sha256": "a" * 64, "bytes": 100},
                "recorded_wav": {"sha256": "b" * 64, "bytes": 200},
                "analysis": {
                    "kind": "audio_loopback_latency_result",
                    "sample_rate_hz": 48000,
                    "delay_samples": 480,
                    "round_trip_latency_ms": 10.0,
                    "peak_snr_db": 40.0,
                    "peak_detection_confidence": 1.0,
                },
            }
            xrun_graph = MODULE.LABORATORY.graph_fingerprint(planned_graph)
            xrun_started = "2026-07-27T12:00:00+00:00"
            xrun_ended = "2026-07-27T12:01:00+00:00"
            xrun_argv = list(
                MODULE.LABORATORY.xrun_journal_argv(xrun_started, xrun_ended)
            )
            xrun_binding = {
                "graph_fingerprint": xrun_graph,
                "rate_hz": 48000,
                "quantum_frames": 128,
            }
            xrun = {
                "schema_version": 1,
                "kind": "pipewire_xrun_observation",
                "gate": "xrun-stability-test",
                "result": "pass",
                "measured_at": xrun_ended,
                "physical_state_sha256": None,
                "requested_duration_seconds": 60,
                "duration_seconds": 60,
                "observation_started_at": xrun_started,
                "observation_ended_at": xrun_ended,
                "xrun_delta": 0,
                "rate_hz": 48000,
                "quantum_frames": 128,
                "graph_fingerprint": xrun_graph,
                "graph_before": {
                    **xrun_binding,
                    "report_sha256": "a" * 64,
                    "truth_chain_sha256": "b" * 64,
                },
                "graph_after": {
                    **xrun_binding,
                    "report_sha256": "c" * 64,
                    "truth_chain_sha256": "d" * 64,
                },
                "journal": {
                    "source": "journalctl-user-audio-units",
                    "query_argv": xrun_argv,
                    "query_argv_sha256": MODULE.LABORATORY.canonical_value_sha256(
                        xrun_argv
                    ),
                    "returncode": 0,
                    "stdout_sha256": "e" * 64,
                    "stdout_total_bytes": 0,
                    "stdout_truncated": False,
                    "line_count": 0,
                    "max_lines": MODULE.LABORATORY.MAX_XRUN_JOURNAL_LINES,
                    "xrun_line_count": 0,
                    "xrun_lines_sha256": MODULE.LABORATORY.canonical_value_sha256([]),
                    "complete": True,
                },
            }
            MODULE.LABORATORY.record_gate(
                state, "loopback-latency-measurement", loopback, physical
            )
            MODULE.LABORATORY.record_gate(
                state, "xrun-stability-test", xrun, physical
            )
            MODULE.LABORATORY.atomic_write_private(gates, state)
            result = MODULE.plan("piano-software-live", physical, gates)
            self.assertFalse(result["ready_for_laboratory_apply"])
            self.assertEqual(
                result["incompatible_laboratory_gates"]
                ["loopback-latency-measurement"]["field"],
                "quantum_frames",
            )
            self.assertEqual(
                result["incompatible_laboratory_gates"]
                ["loopback-latency-measurement"]["expected"],
                128,
            )
            self.assertEqual(
                result["unresolved_laboratory_gates"],
                ["loopback-latency-measurement"],
            )

    def test_xrun_gate_rejects_unrelated_graph(self):
        desired = MODULE.load_profiles()["piano-software-live"]["desired"]
        planned = MODULE.planned_graph_context(self.doctor()["graph"], desired)
        receipt = {
            "evidence": {
                "rate_hz": 48000,
                "quantum_frames": 128,
                "graph_fingerprint": "f" * 64,
            }
        }
        mismatch = MODULE.laboratory_gate_parameter_mismatch(
            "xrun-stability-test",
            receipt,
            desired,
            MODULE.LABORATORY.graph_fingerprint(planned),
        )
        self.assertEqual(mismatch["reason"], "planned-graph-mismatch")

    def test_qobuz_gate_requires_current_track_and_graph(self):
        desired = MODULE.load_profiles()["qobuz-exclusive"]["desired"]
        planned = MODULE.planned_graph_context(
            self.doctor()["graph"], desired, qobuz_track_rate_hz=96000
        )
        graph_fingerprint = MODULE.LABORATORY.graph_fingerprint(planned)
        receipt = {
            "evidence": {
                "track_fingerprint": "a" * 64,
                "track_rate_hz": 96000,
                "graph_fingerprint": graph_fingerprint,
            }
        }
        missing = MODULE.laboratory_gate_parameter_mismatch(
            "qobuz-rate-proof", receipt, desired, graph_fingerprint
        )
        self.assertEqual(missing["reason"], "current-track-context-missing")
        accepted = MODULE.laboratory_gate_parameter_mismatch(
            "qobuz-rate-proof",
            receipt,
            desired,
            graph_fingerprint,
            qobuz_track_fingerprint="a" * 64,
            qobuz_track_rate_hz=96000,
        )
        self.assertIsNone(accepted)


if __name__ == "__main__":
    unittest.main()
