import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build_whale_learning_lesson.py"
SPEC = importlib.util.spec_from_file_location(
    "build_whale_learning_lesson_normalize_test", BUILDER_PATH
)
assert SPEC and SPEC.loader
BUILDER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUILDER
SPEC.loader.exec_module(BUILDER)


class WhaleLearningNormalizeTests(unittest.TestCase):
    def test_normalize_scales_active_rms_to_target(self):
        normalized = BUILDER.normalize([0.1, -0.1])

        self.assertAlmostEqual(normalized[0], BUILDER.TARGET_RMS)
        self.assertAlmostEqual(normalized[1], -BUILDER.TARGET_RMS)

    def test_normalize_leaves_subthreshold_silence_unamplified(self):
        samples = [0.0, 1.0e-6, -1.0e-6]

        self.assertEqual(BUILDER.normalize(samples), samples)

    def test_normalize_caps_gain_at_peak_limit(self):
        normalized = BUILDER.normalize([1.0, *([0.001] * 99)])

        self.assertAlmostEqual(normalized[0], BUILDER.PEAK_LIMIT)
        self.assertAlmostEqual(normalized[1], 0.0003)
        self.assertLessEqual(max(abs(value) for value in normalized), BUILDER.PEAK_LIMIT)


if __name__ == "__main__":
    unittest.main()
