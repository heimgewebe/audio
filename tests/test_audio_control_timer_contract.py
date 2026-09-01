import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TIMER = ROOT / "systemd" / "user" / "audio-control-deploy.timer"


class AudioControlTimerContractTests(unittest.TestCase):
    def test_first_tick_is_bound_to_timer_activation(self):
        directives = {
            line.strip()
            for line in TIMER.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertIn("OnActiveSec=20s", directives)
        self.assertIn("OnUnitActiveSec=60s", directives)
        self.assertNotIn("OnBootSec=20s", directives)
        self.assertIn("Unit=audio-control-deploy.service", directives)


if __name__ == "__main__":
    unittest.main()
