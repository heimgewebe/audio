from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "capture_baseline.py"
SPEC = importlib.util.spec_from_file_location("capture_baseline", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CaptureBaselineTests(unittest.TestCase):
    def test_command_contract_is_read_only(self) -> None:
        MODULE.assert_read_only_commands()

    def test_secret_and_identity_redaction(self) -> None:
        source = "password = hunter2\nhttp://u:p@example.test\nalex@heim-pc\nserial=ABC123"
        redacted = MODULE.redact_text(source, hostname="heim-pc", username="alex")
        for forbidden in ("hunter2", "u:p@", "alex", "heim-pc", "ABC123"):
            self.assertNotIn(forbidden, redacted)

    def test_hdmi2_is_not_misclassified_as_motu(self) -> None:
        self.assertEqual(MODULE.normalize_device_name("alsa_output.hdmi2"), "alsa_output.hdmi2")

    def test_default_device_normalization(self) -> None:
        values = MODULE.parse_pactl_defaults(
            "Default Sink: alsa_output.usb-MOTU_M2_SERIAL.Direct\n"
            "Default Source: alsa_input.usb-Roland_Digital_Piano_SERIAL\n"
        )
        self.assertEqual(values["default_sink"], "motu-m2")
        self.assertEqual(values["default_source"], "roland-fp-30x")

    def test_hardware_identity_accepts_established_models_and_usb_ids(self) -> None:
        detected = MODULE.detect_hardware(
            "alsa_output.usb-MOTU_M2_SERIAL.Direct",
            "alsa_input.usb-Roland_Roland_Digital_Piano_SERIAL",
        )
        self.assertEqual(detected, {"motu_m2": True, "roland_fp_30x": True})
        self.assertEqual(MODULE.normalize_device_name("Bus 001: ID 07fd:0008"), "motu-m2")
        self.assertEqual(MODULE.normalize_device_name("Bus 001: ID 0582:01b1"), "roland-fp-30x")
        self.assertEqual(MODULE.normalize_device_name("alsa_input.usb-Roland_Roland_Digital_Piano_SERIAL"), "roland-fp-30x")

    def test_hardware_identity_rejects_other_vendor_models(self) -> None:
        detected = MODULE.detect_hardware(
            "MOTU M4 USB Audio",
            "Roland JUNO-DS",
            "unrelated M2 adapter",
        )
        self.assertEqual(detected, {"motu_m2": False, "roland_fp_30x": False})
        self.assertEqual(MODULE.normalize_device_name("MOTU M6 USB Audio"), "motu-other")
        self.assertEqual(MODULE.normalize_device_name("Roland JUNO-DS"), "roland-other")

    def test_unmatched_vendor_endpoints_do_not_preserve_private_serials(self) -> None:
        motu_endpoint = "alsa_output.usb-MOTU_M4_PRIVATE_SERIAL-00.pro-output-0"
        roland_endpoint = "alsa_input.usb-Roland_JUNO_DS_PRIVATE_SERIAL-00.analog-stereo"

        for endpoint, expected in ((motu_endpoint, "motu-other"), (roland_endpoint, "roland-other")):
            normalized = MODULE.normalize_device_name(endpoint)
            self.assertEqual(normalized, expected)
            self.assertNotIn("PRIVATE_SERIAL", normalized)

    def test_wpctl_configured_defaults(self) -> None:
        values = MODULE.parse_wpctl_configured_defaults(
            "0. Audio/Sink    alsa_output.usb-MOTU_M2_SERIAL.Direct\n"
            "1. Audio/Source  alsa_input.usb-Roland_Digital_Piano_SERIAL\n"
        )
        self.assertEqual(values["configured_default_sink"], "motu-m2")
        self.assertEqual(values["configured_default_source"], "roland-fp-30x")

    def test_dpkg_command_preserves_version_shape(self) -> None:
        from unittest.mock import patch
        import subprocess

        completed = subprocess.CompletedProcess(
            args=["dpkg-query"], returncode=0,
            stdout=b"alsa-topology-conf\t1.2.5.1-2\n", stderr=b"",
        )
        with patch.object(MODULE.subprocess, "run", return_value=completed):
            record = MODULE.run_command(("dpkg-query", "-W"), hostname="host", username="user")
        self.assertIn("1.2.5.1-2", record["stdout"])
        self.assertNotIn("<ipv4>", record["stdout"])

    def test_package_version_is_not_mistaken_for_ipv4(self) -> None:
        value = MODULE.redact_text(
            "alsa-topology-conf\t1.2.5.1-2", hostname="host", username="user", redact_network=False
        )
        self.assertIn("1.2.5.1-2", value)

    def test_german_pactl_defaults(self) -> None:
        values = MODULE.parse_pactl_defaults(
            "Standard-Ziel: alsa_output.pci-0000_09_00.4.iec958-stereo\n"
            "Standard-Quelle: alsa_output.pci-0000_09_00.4.iec958-stereo.monitor\n"
        )
        self.assertEqual(values["default_sink"], "alsa_output.pci-0000_09_00.4.iec958-stereo")
        self.assertEqual(values["default_source"], "alsa_output.pci-0000_09_00.4.iec958-stereo.monitor")

    def test_private_json_is_mode_600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            digest = MODULE.write_json(path, {"ok": True}, 0o600)
            self.assertEqual(len(digest), 64)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_write_json_does_not_follow_predictable_temporary_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "receipt.json"
            victim = root / "victim.txt"
            victim.write_text("keep-me", encoding="utf-8")
            legacy_temporary = path.with_suffix(path.suffix + ".tmp")
            legacy_temporary.symlink_to(victim)

            MODULE.write_json(path, {"ok": True}, 0o600)

            self.assertEqual(victim.read_text(encoding="utf-8"), "keep-me")
            self.assertTrue(legacy_temporary.is_symlink())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
