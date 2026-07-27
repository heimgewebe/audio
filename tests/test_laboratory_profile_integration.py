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


if __name__ == "__main__":
    unittest.main()
