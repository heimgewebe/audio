import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audio_control_deploy_recording_source_binding_release_test",
    ROOT / "scripts" / "audio_control_deploy.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RecordingSourceBindingReleaseTests(unittest.TestCase):
    def test_release_closure_is_exact_and_versioned_separately_from_level_v1(self):
        expected = {
            "scripts/motu_capture_identity.py",
            "scripts/voice_capture_observer.py",
            "scripts/level_analyzer.py",
            "scripts/system_truth.py",
            "scripts/laboratory_gate.py",
            "scripts/physical_verification.py",
            "scripts/rate_policy_observer.py",
            "scripts/audio_level_observer.py",
            "scripts/audio_live_telemetry.py",
            f"systemd/user/{MODULE.LEVEL_OBSERVER_UNIT}",
            MODULE.RECORDER_BOUND_LEVEL_RELEASE_SENTINEL,
        }
        self.assertEqual(set(MODULE.RECORDER_BOUND_LEVEL_CRITICAL_RELEASE_FILES), expected)
        self.assertNotIn(
            "scripts/motu_capture_identity.py",
            set(MODULE.LEVEL_OBSERVER_CRITICAL_RELEASE_FILES),
        )
        self.assertNotEqual(
            MODULE.RECORDER_BOUND_LEVEL_RELEASE_SENTINEL,
            MODULE.LEVEL_OBSERVER_RELEASE_SENTINEL,
        )


if __name__ == "__main__":
    unittest.main()
