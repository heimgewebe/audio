import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "profile_planner", ROOT / "scripts/profile_planner.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ProfilePlannerTests(unittest.TestCase):
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

    def test_voice_is_blocked_without_physical_facts(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(MODULE, "doctor_report", self.doctor),
        ):
            state = pathlib.Path(directory) / "state.json"
            result = MODULE.plan(
                "voice-recording", state, pathlib.Path(directory) / "gates.json"
            )
            self.assertFalse(result["ready_for_laboratory_apply"])
            self.assertIn("motu_phantom_48v", result["missing_physical_facts"])
            self.assertEqual(
                result["unresolved_laboratory_gates"], ["voice-level-measurement"]
            )
            self.assertIn(
                {
                    "field": "default_source",
                    "from": "roland-fp-30x",
                    "to": "motu-m2",
                },
                result["proposed_changes"],
            )

    def test_desktop_mixed_is_ready(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(MODULE, "doctor_report", self.doctor),
        ):
            result = MODULE.plan(
                "desktop-mixed",
                pathlib.Path(directory) / "state.json",
                pathlib.Path(directory) / "gates.json",
            )
            self.assertTrue(result["ready_for_laboratory_apply"])
            self.assertEqual(result["proposed_changes"], [])

    def test_gate_free_profile_does_not_read_unrelated_laboratory_state(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(MODULE, "doctor_report", self.doctor),
            mock.patch.object(
                MODULE.LABORATORY,
                "read_state",
                side_effect=AssertionError(
                    "gate-free profile must not read laboratory state"
                ),
            ),
        ):
            result = MODULE.plan(
                "desktop-mixed",
                pathlib.Path(directory) / "state.json",
                pathlib.Path(directory) / "stale-gates.json",
            )
            self.assertTrue(result["ready_for_laboratory_apply"])
            self.assertEqual(result["resolved_laboratory_gates"], [])

    def test_gated_profile_still_requires_valid_laboratory_state(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(MODULE, "doctor_report", self.doctor),
            mock.patch.object(
                MODULE.LABORATORY,
                "read_state",
                side_effect=ValueError("stale laboratory state"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "stale laboratory state"):
                MODULE.plan(
                    "voice-recording",
                    pathlib.Path(directory) / "state.json",
                    pathlib.Path(directory) / "stale-gates.json",
                )

    def test_production_is_known_but_explicitly_not_executable(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(MODULE, "doctor_report", self.doctor),
        ):
            result = MODULE.plan(
                "production",
                pathlib.Path(directory) / "state.json",
                pathlib.Path(directory) / "gates.json",
            )
            self.assertEqual(result["operational_status"], "planned")
            self.assertFalse(result["profile_executable"])
            self.assertFalse(result["ready_for_laboratory_apply"])
            self.assertEqual(
                result["readiness_blockers"], ["profile-planned-not-executable"]
            )
            self.assertTrue(result["planned_blocker"])

    def test_mismatched_required_fact_blocks(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(MODULE, "doctor_report", self.doctor),
        ):
            state_path = pathlib.Path(directory) / "state.json"
            state = MODULE.PHYSICAL.empty_state()
            MODULE.PHYSICAL.record_fact(state, "transmitter_tx_mode", "rx", "visual")
            MODULE.PHYSICAL.record_fact(state, "transmitter_input", "optical", "visual")
            MODULE.PHYSICAL.record_fact(state, "transmitter_codec", "aptx-hd", "visual")
            MODULE.PHYSICAL.record_fact(
                state, "transmitter_paired_target", "Testgerät", "visual"
            )
            MODULE.PHYSICAL.atomic_write_private(state_path, state)
            result = MODULE.plan(
                "bluetooth-convenience",
                state_path,
                pathlib.Path(directory) / "gates.json",
            )
            self.assertFalse(result["ready_for_laboratory_apply"])
            self.assertEqual(result["mismatched_physical_facts"][0]["expected"], "tx")

    def test_piano_recording_is_blocked_by_resampling_gate(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(MODULE, "doctor_report", self.doctor),
        ):
            result = MODULE.plan(
                "piano-digital-recording",
                pathlib.Path(directory) / "state.json",
                pathlib.Path(directory) / "gates.json",
            )
            self.assertFalse(result["ready_for_laboratory_apply"])
            self.assertEqual(result["missing_physical_facts"], [])
            self.assertEqual(
                result["unresolved_laboratory_gates"], ["resampling-decision"]
            )


if __name__ == "__main__":
    unittest.main()
