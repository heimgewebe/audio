import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import whale_two_clock_probe as probe_module  # noqa: E402


class WhaleTwoClockAcousticContractTests(unittest.TestCase):
    def test_c2_separated_clock_removes_most_legacy_fast_edge_energy(self):
        probe = probe_module.TwoClockProbe()
        legacy = probe.render_note(36, 0.25, two_clock=False)
        candidate = probe.render_note(36, 0.25, two_clock=True)
        legacy_edge = probe_module.difference_energy_ratio(legacy)
        candidate_edge = probe_module.difference_energy_ratio(candidate)

        self.assertGreater(legacy_edge, 0.0)
        self.assertLess(candidate_edge / legacy_edge, 0.10)

    def test_a0_remains_exactly_unchanged_when_source_clock_is_not_overclocked(self):
        probe = probe_module.TwoClockProbe()
        legacy = probe.render_note(21, 0.25, two_clock=False)
        candidate = probe.render_note(21, 0.25, two_clock=True)
        self.assertEqual(legacy, candidate)


if __name__ == "__main__":
    unittest.main()
