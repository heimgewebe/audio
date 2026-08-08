import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audio_doctor", ROOT / "scripts/audio_doctor.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class AudioDoctorTests(unittest.TestCase):
    def result(self, argv, stdout, returncode=0):
        return MODULE.CommandResult(tuple(argv), returncode, stdout, "")

    def test_german_pactl_defaults(self):
        text = "Standard-Ziel: alsa_output.usb-MOTU_M2-00\nStandard-Quelle: alsa_input.usb-Roland_Digital_Piano-00\n"
        self.assertEqual(
            MODULE.normalize_endpoint(MODULE.parse_pactl_default(text, "sink")),
            "motu-m2",
        )
        self.assertEqual(
            MODULE.normalize_endpoint(MODULE.parse_pactl_default(text, "source")),
            "roland-fp-30x",
        )

    def test_other_motu_models_do_not_satisfy_m2_identity(self):
        self.assertEqual(
            MODULE.normalize_endpoint("alsa_output.usb-MOTU_M4_SERIAL-00"),
            "other",
        )
        self.assertFalse(MODULE.contains_device("MOTU M4 USB Audio", "motu-m2"))
        self.assertFalse(MODULE.contains_device("MOTU M6 USB Audio", "motu-m2"))
        self.assertTrue(MODULE.contains_device("MOTU M2 USB Audio", "motu-m2"))
        self.assertTrue(MODULE.contains_device("Karte 2: M2 [M2]", "motu-m2"))

    def test_report_keeps_physical_state_unknown(self):
        results = [
            self.result(
                ("aplay", "-l"),
                "Karte 2: M2 [M2], Gerät 0: USB Audio\nKarte 3: Piano [Roland Digital Piano]\n",
            ),
            self.result(
                ("arecord", "-l"),
                "Karte 2: M2 [M2]\nKarte 3: Piano [Roland Digital Piano]\n",
            ),
            self.result(("wpctl", "status"), "M Series\nRoland Digital Piano\n"),
            self.result(
                ("pw-metadata", "-n", "settings", "0"),
                "key:'clock.force-rate' value:'48000'\nkey:'clock.force-quantum' value:'1024'\n",
            ),
            self.result(
                ("pactl", "info"),
                "Default Sink: alsa_output.usb-MOTU_M2-00\nDefault Source: alsa_input.usb-Roland_Digital_Piano-00\n",
            ),
            self.result(
                ("pactl", "list", "short", "sinks"),
                "1\tmotu\tPipeWire\ts32le 2ch 48000Hz\n",
            ),
            self.result(
                ("pactl", "list", "short", "sources"),
                "2\troland\tPipeWire\ts24le 2ch 44100Hz\n",
            ),
            self.result(
                ("aconnect", "-l"), "client 24: 'Roland FP-30X' [type=kernel]\n"
            ),
            self.result(("amidi", "-l"), ""),
            self.result(("systemctl", "is-active", "bluetooth"), "inactive\n", 3),
        ]
        report = MODULE.build_report(results)
        self.assertTrue(report["hardware"]["motu_m2"])
        self.assertTrue(report["hardware"]["roland_fp_30x"])
        self.assertTrue(
            report["device_truth"]["observed"]["roland_fp_30x"]["alsa_midi"]
        )
        self.assertEqual(
            report["device_truth"]["configured_defaults"]["default_source"],
            "roland-fp-30x",
        )
        self.assertEqual(
            report["device_truth"]["desired"],
            {"motu_m2": True, "roland_fp_30x": True},
        )
        self.assertEqual(report["graph"]["single_buffer_period_ms"], 21.333)
        self.assertIsNone(report["graph"]["round_trip_latency_ms"])
        self.assertIn("motu_phantom_48v", report["physical_unknowns"])
        self.assertFalse(report["profiles"]["voice_recording"]["software_ready"])

    def test_configured_roland_default_does_not_prove_physical_presence(self):
        results = [
            self.result(("aplay", "-l"), "Karte 0: Generic [Generic]\n"),
            self.result(("arecord", "-l"), "Karte 0: Generic [Generic]\n"),
            self.result(("wpctl", "status"), "Roland Digital Piano (configured)\n"),
            self.result(("pw-metadata", "-n", "settings", "0"), ""),
            self.result(
                ("pactl", "info"),
                "Default Sink: alsa_output.generic\n"
                "Default Source: alsa_input.usb-Roland_Digital_Piano-00\n",
            ),
            self.result(("pactl", "list", "short", "sinks"), ""),
            self.result(("pactl", "list", "short", "sources"), ""),
            self.result(("aconnect", "-l"), "client 0: 'System' [type=kernel]\n"),
            self.result(("amidi", "-l"), ""),
            self.result(("systemctl", "is-active", "bluetooth"), "active\n"),
        ]
        report = MODULE.build_report(results)
        self.assertFalse(report["hardware"]["roland_fp_30x"])
        observed = report["device_truth"]["observed"]["roland_fp_30x"]
        self.assertFalse(observed["present"])
        self.assertFalse(observed["alsa_midi"])
        self.assertTrue(observed["pipewire_graph"])
        self.assertEqual(
            report["device_truth"]["configured_defaults"]["default_source"],
            "roland-fp-30x",
        )
        self.assertIn(
            "configured-default-device-absent",
            {warning["code"] for warning in report["warnings"]},
        )

    def test_serial_redaction(self):
        self.assertNotIn("M20000062566", MODULE.redact("usb-MOTU_M2_M20000062566-00"))

    def test_empty_username_does_not_expand_output(self):
        original = MODULE.os.environ.get("USER")
        try:
            MODULE.os.environ["USER"] = ""
            self.assertEqual(MODULE.redact("plain text"), "plain text")
        finally:
            if original is None:
                MODULE.os.environ.pop("USER", None)
            else:
                MODULE.os.environ["USER"] = original

    def test_atomic_output_is_private_and_preserves_existing_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            output = root / "doctor.json"
            MODULE.atomic_write_output(output, '{"ok": true}\n')
            self.assertEqual(output.read_text(encoding="utf-8"), '{"ok": true}\n')
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

            os.chmod(output, 0o640)
            MODULE.atomic_write_output(output, '{"ok": false}\n')
            self.assertEqual(output.read_text(encoding="utf-8"), '{"ok": false}\n')
            self.assertEqual(output.stat().st_mode & 0o777, 0o640)

    def test_atomic_output_refuses_symlink_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            victim = root / "victim.json"
            victim.write_text("keep", encoding="utf-8")
            output = root / "doctor.json"
            output.symlink_to(victim)

            with self.assertRaisesRegex(OSError, "not a regular file"):
                MODULE.atomic_write_output(output, '{"ok": true}\n')

            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
            self.assertTrue(output.is_symlink())

    def test_atomic_output_refuses_symlink_anywhere_in_parent_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            real = root / "real"
            nested = real / "nested"
            nested.mkdir(parents=True)
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)

            with self.assertRaises(OSError):
                MODULE.atomic_write_output(linked / "nested" / "doctor.json", '{}\n')

            self.assertFalse((nested / "doctor.json").exists())

    def test_physical_unknowns_come_from_contract(self):
        unknowns = MODULE.physical_unknowns()
        self.assertEqual(len(unknowns), 16)
        self.assertIn("motu_phantom_48v", unknowns)


if __name__ == "__main__":
    unittest.main()
