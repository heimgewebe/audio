import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compare_whale_organic as comparison  # noqa: E402
import whale_morph_engine as morph  # noqa: E402


class WhaleOrganicComparisonTests(unittest.TestCase):
    def test_reference_selection_is_manifest_and_hash_bound(self):
        manifest_sha256, records = comparison.reference_records(6)

        self.assertEqual(
            manifest_sha256,
            morph.sha256_path(comparison.REFERENCE_MANIFEST),
        )
        self.assertEqual(len(records), 6)
        self.assertEqual(len({record["source_id"] for record in records}), 6)
        for record in records:
            path = record["path"]
            payload = record["payload"]
            self.assertIsInstance(path, pathlib.Path)
            self.assertIsInstance(payload, bytes)
            self.assertEqual(record["sha256"], morph.sha256_path(path))
            self.assertGreater(
                len(comparison.decode_pcm16_mono(payload, str(path), 0.2)),
                0,
            )

    def test_reference_selection_rejects_manifest_hash_drift(self):
        manifest = json.loads(
            comparison.REFERENCE_MANIFEST.read_text(encoding="utf-8")
        )
        first_song = next(
            record for record in manifest["clips"] if record.get("category") == "song"
        )
        first_song["sha256"] = "0" * 64

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                comparison.reference_records(
                    3,
                    manifest_path=path,
                    source_root=comparison.REFERENCE_ROOT,
                )


    def test_temporal_features_measure_acoustic_state_diversity(self):
        morph_samples = comparison.render_phrase("morph")
        organic_samples = comparison.render_phrase("organic")
        morph_features = comparison.temporal_state_features(morph_samples)
        organic_features = comparison.temporal_state_features(organic_samples)

        self.assertEqual(
            organic_features,
            comparison.temporal_state_features(organic_samples),
        )
        for features in (morph_features, organic_features):
            self.assertAlmostEqual(
                features["tonal_fraction"]
                + features["mixed_fraction"]
                + features["rough_fraction"],
                1.0,
                places=10,
            )
        self.assertGreater(
            organic_features["rough_fraction"],
            morph_features["rough_fraction"] + 0.03,
        )
        self.assertGreater(
            organic_features["state_entropy"],
            morph_features["state_entropy"] + 0.10,
        )
        self.assertGreater(
            organic_features["highband_q90"],
            morph_features["highband_q90"] * 1.25,
        )
        self.assertGreater(
            organic_features["envelope_pulse_index"],
            morph_features["envelope_pulse_index"] * 1.25,
        )
        self.assertLess(organic_features["rough_fraction"], 0.20)

    def test_temporal_comparison_uses_declared_feature_set(self):
        samples = comparison.render_phrase("organic", 3.0)
        features = comparison.temporal_state_features(samples)
        aggregate = {
            key: {"median": features[key], "q25": features[key], "q75": features[key]}
            for key in comparison.TEMPORAL_FEATURES
        }
        score, deltas = comparison.compare_temporal(features, aggregate)
        self.assertEqual(score, 1.0)
        self.assertEqual(set(deltas), set(comparison.TEMPORAL_FEATURES))

    def test_degenerate_pitch_motion_is_reported_but_not_scored(self):
        self.assertNotIn("pitch_motion_semitones_per_second", comparison.FEATURES)
        features = comparison.extract_features(comparison.render_phrase("morph", 2.0))
        self.assertIn("pitch_motion_semitones_per_second", features)


if __name__ == "__main__":
    unittest.main()
