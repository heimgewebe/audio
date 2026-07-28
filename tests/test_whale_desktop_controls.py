import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import install_whale_desktop_controls as install  # noqa: E402
import whale_desktop_control as control  # noqa: E402


class WhaleDesktopControlTests(unittest.TestCase):
    def test_active_state_and_description_are_user_facing(self):
        self.assertTrue(control.active({"active_state": "active"}))
        self.assertFalse(control.active({"active_state": "inactive"}))
        self.assertEqual(
            control.describe({"active_state": "active", "voice_mode": "realistic"}),
            "Status: active · Modus: realistisch",
        )
        self.assertEqual(
            control.describe({"state": "ready", "voice_mode": "ufo"}),
            "Status: ready · Modus: UFO",
        )

    def test_desktop_entries_use_absolute_nonterminal_commands(self):
        for action in install.ACTIONS:
            text = install.desktop_text(action)
            self.assertIn("Type=Application", text)
            self.assertIn("Terminal=false", text)
            self.assertIn(str(install.CONTROL), text)
            self.assertIn(f" {action}\n", text)


if __name__ == "__main__":
    unittest.main()
