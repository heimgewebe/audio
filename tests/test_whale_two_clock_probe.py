import math
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import whale_two_clock_probe as probe_module  # noqa: E402


class WhaleTwoClockProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = probe_module.TwoClockProbe()

    def test_c2_uses_measured_source_clock_instead_of_played_pitch_clock(self):
        description = self.probe.describe_note(36)
        self.assertAlmostEqual(description["source_clock_hz"], 33.264033264033266, places=9)
        self.assertAlmostEqual(description["pitch_hz"], 65.40639132514966, places=9)
        self.assertGreater(description["pitch_to_source_clock_ratio"], 1.96)
        self.assertGreater(description["decoupling_amount"], 0.95)
        self.assertGreater(description["source_clock_period_ms"], description["pitch_period_ms"] * 1.9)

    def test_a0_does_not_get_a_needless_two_clock_rewrite(self):
        description = self.probe.describe_note(21)
        self.assertAlmostEqual(description["pitch_hz"], 27.5, places=10)
        self.assertAlmostEqual(description["source_clock_hz"], 28.38557066824364, places=9)
        self.assertEqual(description["decoupling_amount"], 0.0)
        legacy = self.probe.render_note(21, 0.12, two_clock=False)
        candidate = self.probe.render_note(21, 0.12, two_clock=True)
        self.assertEqual(legacy, candidate)

    def test_c2_candidate_changes_texture_without_moving_midi_pitch_contract(self):
        description = self.probe.describe_note(36)
        expected_pitch = probe_module.midi_note_frequency(36)
        self.assertAlmostEqual(description["pitch_hz"], expected_pitch, places=12)
        legacy = self.probe.render_note(36, 0.20, two_clock=False)
        candidate = self.probe.render_note(36, 0.20, two_clock=True)
        self.assertEqual(len(legacy), len(candidate))
        self.assertNotEqual(legacy, candidate)
        self.assertTrue(all(math.isfinite(value) for value in candidate))
        self.assertLessEqual(max(abs(value) for value in candidate), probe_module.MAX_MASTER_GAIN)

    def test_source_clock_interpolation_is_positive_and_continuous(self):
        values = [self.probe.source_clock_hz(note / 8.0) for note in range(21 * 8, 108 * 8 + 1)]
        self.assertTrue(all(value > 0.0 and math.isfinite(value) for value in values))
        relative_steps = [
            abs(right - left) / max(left, 1.0e-12)
            for left, right in zip(values, values[1:])
        ]
        self.assertLess(max(relative_steps), 0.05)

    def test_probe_writes_bounded_ab_evidence_without_committed_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            report = probe_module.build_probe(root, (36,), 0.08)
            self.assertEqual(report["status"], "experimental-not-runtime")
            self.assertTrue((root / "legacy-note-36.wav").is_file())
            self.assertTrue((root / "two-clock-note-36.wav").is_file())
            self.assertTrue((root / "report.json").is_file())
            record = report["notes"][0]
            self.assertTrue(record["render_changed"])
            self.assertLessEqual(record["legacy_peak"], probe_module.MAX_MASTER_GAIN)
            self.assertLessEqual(record["two_clock_peak"], probe_module.MAX_MASTER_GAIN)


if __name__ == "__main__":
    unittest.main()
