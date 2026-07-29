import pathlib
import sys
import unittest
from unittest import mock

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
            "Status: active · Modus: Sample",
        )
        self.assertEqual(
            control.describe({"state": "ready", "voice_mode": "morph"}),
            "Status: ready · Modus: spielbar",
        )
        self.assertEqual(
            control.describe({"state": "ready", "voice_mode": "ufo"}),
            "Status: ready · Modus: UFO",
        )

    def test_gsettings_string_arrays_support_fresh_and_existing_profiles(self):
        self.assertEqual(install.parse_custom_keybindings("@as []"), [])
        self.assertEqual(
            install.parse_custom_keybindings("['/existing/']"), ["/existing/"]
        )
        with self.assertRaises(ValueError):
            install.parse_custom_keybindings("'not-an-array'")

    def test_desktop_entries_use_absolute_nonterminal_commands(self):
        for action in install.ACTIONS:
            text = install.desktop_text(action)
            self.assertIn("Type=Application", text)
            self.assertIn("Terminal=false", text)
            self.assertIn(str(install.CONTROL), text)
            self.assertIn(install.desktop_exec_quote(action), text)

    def test_desktop_and_shortcut_commands_escape_spaces_and_percent_codes(self):
        control_path = pathlib.Path("/tmp/audio clone 100%/whale control.py")
        with mock.patch.object(install, "CONTROL", control_path):
            desktop = install.desktop_exec_command("toggle")
            shortcut = install.shortcut_command()
        self.assertIn('"/tmp/audio clone 100%%/whale control.py"', desktop)
        self.assertNotIn(" 100%/", desktop)
        self.assertEqual(
            shortcut,
            f"{install.sys.executable} '/tmp/audio clone 100%/whale control.py' toggle",
        )

    def test_desktop_command_rejects_control_characters(self):
        with self.assertRaises(ValueError):
            install.desktop_exec_quote("bad\ncommand")


if __name__ == "__main__":
    unittest.main()
