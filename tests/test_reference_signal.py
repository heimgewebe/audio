import importlib.util
import pathlib
import tempfile
import unittest
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reference_signal", ROOT / "scripts/reference_signal.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ReferenceSignalTests(unittest.TestCase):
    def test_refuses_excessive_level(self):
        with self.assertRaises(ValueError):
            MODULE.generate_samples("tone", 48000, 0.1, -6.0)

    def test_rejects_invalid_parameters(self):
        invalid = (
            ("tone", 0, 0.1, -20.0, 1000.0),
            ("tone", 48000, 0.0, -20.0, 1000.0),
            ("tone", 48000, 0.1, float("nan"), 1000.0),
            ("tone", 48000, 0.1, -20.0, 0.0),
            ("tone", 48000, 0.1, -20.0, 24000.0),
            ("tone", 192001, 0.1, -20.0, 1000.0),
            ("tone", 48000, 60.0, -20.0, 1000.0),
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                MODULE.generate_samples(*arguments)

    def test_writes_bounded_wave(self):
        samples = MODULE.generate_samples("tone", 48000, 0.1, -20.0)
        self.assertLessEqual(max(abs(value) for value in samples), round(32767 * 0.1) + 1)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tone.wav"
            MODULE.write_wav(path, samples, 48000)
            with wave.open(str(path), "rb") as handle:
                self.assertEqual(handle.getframerate(), 48000)
                self.assertEqual(handle.getnchannels(), 1)
                self.assertEqual(handle.getsampwidth(), 2)


if __name__ == "__main__":
    unittest.main()
