import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "systemd" / "user" / "audio-control-ui-v1.service"


class AudioControlUpgradeMigrationTests(unittest.TestCase):
    def test_legacy_install_can_bootstrap_runtime_environment(self):
        lines = SERVICE_PATH.read_text(encoding="utf-8").splitlines()
        active = [
            line.strip()
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        ]

        runtime_condition = (
            "ConditionPathExists=%h/.config/audio-control-ui/runtime.env"
        )
        legacy_environment = (
            "EnvironmentFile=-%h/.config/audio-control-deploy.env"
        )
        runtime_environment = (
            "EnvironmentFile=-%h/.config/audio-control-ui/runtime.env"
        )
        exec_start = next(line for line in active if line.startswith("ExecStart="))

        self.assertNotIn(runtime_condition, active)
        self.assertIn("Environment=AUDIO_CONTROL_PORT=8765", active)
        self.assertIn(legacy_environment, active)
        self.assertIn(runtime_environment, active)
        self.assertLess(active.index(legacy_environment), active.index(runtime_environment))
        self.assertIn("--host 127.0.0.1", exec_start)
        self.assertIn("--port ${AUDIO_CONTROL_PORT}", exec_start)
        self.assertNotIn("${AUDIO_CONTROL_HOST}", exec_start)


if __name__ == "__main__":
    unittest.main()
