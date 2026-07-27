import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "calibration_pack", ROOT / "scripts/calibration_pack.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class CalibrationPackTests(unittest.TestCase):
    def test_headphone_pack_is_bounded_and_nonplaying(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            result = MODULE.create_pack("headphone-reference", output)
            self.assertFalse(result["automatic_playback"])
            manifest = json.loads((output / "manifest.v1.json").read_text())
            self.assertEqual(manifest["signal"]["dbfs"], -20.0)
            self.assertEqual(
                len(manifest["generator"]["calibration_pack_sha256"]), 64
            )
            self.assertEqual(
                len(manifest["generator"]["reference_signal_sha256"]), 64
            )
            with wave.open(str(output / "headphone-reference.wav"), "rb") as handle:
                self.assertEqual(handle.getframerate(), 48000)
                self.assertEqual(handle.getnframes(), 240000)

    def test_voice_pack_has_no_wave(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            result = MODULE.create_pack("voice-gain", output)
            self.assertEqual(result["artifacts"], [])
            self.assertFalse(any(output.glob("*.wav")))

    def test_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            output.mkdir()
            with self.assertRaises(FileExistsError):
                MODULE.create_pack("motu-loopback", output)

    def test_cleans_staging_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            output = root / "pack"
            with mock.patch.object(
                MODULE.REFERENCE, "generate_samples", side_effect=RuntimeError("boom")
            ):
                with self.assertRaises(RuntimeError):
                    MODULE.create_pack("motu-loopback", output)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".pack.*")), [])


if __name__ == "__main__":
    unittest.main()
