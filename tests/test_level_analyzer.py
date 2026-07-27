import importlib.util
import pathlib
import struct
import tempfile
import unittest
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "level_analyzer", ROOT / "scripts/level_analyzer.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class LevelAnalyzerTests(unittest.TestCase):
    def write_wave(self, path, samples, width=2, channels=1, rate=48000):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(width)
            handle.setframerate(rate)
            raw = b"".join(struct.pack("<h", value) for value in samples)
            handle.writeframes(raw)

    def test_in_range_voice_peak(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "voice.wav"
            self.write_wave(path, [0, 10000, -10000, 0] * 100)
            result = MODULE.analyze(path)
            self.assertEqual(result["voice_target"]["status"], "in-range")
            self.assertEqual(result["bit_depth"], 16)

    def test_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "silence.wav"
            self.write_wave(path, [0] * 100)
            result = MODULE.analyze(path)
            self.assertIsNone(result["maximum_peak_dbfs"])
            self.assertEqual(result["voice_target"]["status"], "silence")

    def test_reports_clipping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "clip.wav"
            self.write_wave(path, [32767, -32768, 0, 0])
            result = MODULE.analyze(path)
            self.assertEqual(result["channels_analysis"][0]["clipped_samples"], 2)
            self.assertEqual(result["voice_target"]["status"], "high")

    def test_rejects_truncated_24_bit_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "truncated-24bit.wav"
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(3)
                handle.setframerate(48000)
                handle.writeframes(b"\x00\x00\x00" * 4)
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, "truncated"):
                MODULE.analyze(path)

    def test_total_sample_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "bounded.wav"
            self.write_wave(path, [0, 0, 0, 0], channels=2)
            original = MODULE.MAX_TOTAL_SAMPLES
            try:
                MODULE.MAX_TOTAL_SAMPLES = 3
                with self.assertRaises(ValueError):
                    MODULE.analyze(path)
            finally:
                MODULE.MAX_TOTAL_SAMPLES = original


if __name__ == "__main__":
    unittest.main()
