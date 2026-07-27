import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audio_doctor", ROOT / "scripts/audio_doctor.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class AudioDoctorTests(unittest.TestCase):
    def result(self, argv, stdout, returncode=0):
        return MODULE.CommandResult(tuple(argv), returncode, stdout, "")

    def test_german_pactl_defaults(self):
        text = "Standard-Ziel: alsa_output.usb-MOTU_M2-00\nStandard-Quelle: alsa_input.usb-Roland_Digital_Piano-00\n"
        self.assertEqual(MODULE.normalize_endpoint(MODULE.parse_pactl_default(text, "sink")), "motu-m2")
        self.assertEqual(MODULE.normalize_endpoint(MODULE.parse_pactl_default(text, "source")), "roland-fp-30x")

    def test_report_keeps_physical_state_unknown(self):
        results = [
            self.result(("aplay", "-l"), "Karte 2: M2 [M2], Gerät 0: USB Audio\nKarte 3: Piano [Roland Digital Piano]\n"),
            self.result(("arecord", "-l"), "Karte 2: M2 [M2]\nKarte 3: Piano [Roland Digital Piano]\n"),
            self.result(("wpctl", "status"), "M Series\nRoland Digital Piano\n"),
            self.result(("pw-metadata", "-n", "settings", "0"), "key:'clock.force-rate' value:'48000'\nkey:'clock.force-quantum' value:'1024'\n"),
            self.result(("pactl", "info"), "Default Sink: alsa_output.usb-MOTU_M2-00\nDefault Source: alsa_input.usb-Roland_Digital_Piano-00\n"),
            self.result(("pactl", "list", "short", "sinks"), "1\tmotu\tPipeWire\ts32le 2ch 48000Hz\n"),
            self.result(("pactl", "list", "short", "sources"), "2\troland\tPipeWire\ts24le 2ch 44100Hz\n"),
            self.result(("systemctl", "is-active", "bluetooth"), "inactive\n", 3),
        ]
        report = MODULE.build_report(results)
        self.assertTrue(report["hardware"]["motu_m2"])
        self.assertTrue(report["hardware"]["roland_fp_30x"])
        self.assertEqual(report["graph"]["single_buffer_period_ms"], 21.333)
        self.assertIsNone(report["graph"]["round_trip_latency_ms"])
        self.assertIn("motu_phantom_48v", report["physical_unknowns"])
        self.assertFalse(report["profiles"]["voice_recording"]["software_ready"])

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

    def test_physical_unknowns_come_from_contract(self):
        unknowns = MODULE.physical_unknowns()
        self.assertEqual(len(unknowns), 16)
        self.assertIn("motu_phantom_48v", unknowns)


if __name__ == "__main__":
    unittest.main()
