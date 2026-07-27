import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("latency_analyzer", ROOT / "scripts/latency_analyzer.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

REFERENCE_SPEC = importlib.util.spec_from_file_location("reference_signal", ROOT / "scripts/reference_signal.py")
REFERENCE = importlib.util.module_from_spec(REFERENCE_SPEC)
assert REFERENCE_SPEC and REFERENCE_SPEC.loader
REFERENCE_SPEC.loader.exec_module(REFERENCE)


class LatencyAnalyzerTests(unittest.TestCase):
    def test_estimates_known_delay(self):
        reference = [20000] + [0] * 127
        recorded = [0] * 240 + reference + [0] * 128
        delay, confidence, peak_snr_db = MODULE.estimate_delay(reference, recorded, 500)
        self.assertEqual(delay, 240)
        self.assertEqual(confidence, 1.0)
        self.assertGreaterEqual(peak_snr_db, 80.0)

    def test_rejects_silence_and_non_impulse_reference(self):
        with self.assertRaises(ValueError):
            MODULE.estimate_delay([0] * 128, [0] * 256, 100)
        with self.assertRaises(ValueError):
            MODULE.estimate_delay([1000] * 128, [0] * 20 + [1000] * 128, 100)

    def test_analyzes_wave_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            reference_path = root / "reference.wav"
            recorded_path = root / "recorded.wav"
            reference = [20000] + [0] * 127
            recorded = [0] * 480 + reference + [0] * 128
            REFERENCE.write_wav(reference_path, reference, 48000)
            REFERENCE.write_wav(recorded_path, recorded, 48000)
            result = MODULE.analyze(reference_path, recorded_path, 20)
            self.assertEqual(result["delay_samples"], 480)
            self.assertEqual(result["round_trip_latency_ms"], 10.0)
            self.assertEqual(result["method"], "offline impulse-peak alignment")
            self.assertGreaterEqual(result["peak_snr_db"], 80.0)


if __name__ == "__main__":
    unittest.main()
