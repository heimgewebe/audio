import hashlib
import json
import pathlib
import struct
import sys
import unittest
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_whale_external_evaluation_v2 as builder  # noqa: E402
import study_whale_organic_ablation as study  # noqa: E402
import summarize_whale_organic_external as summary  # noqa: E402
import whale_source_filter_engine as source_filter  # noqa: E402


class WhaleExternalEvaluationV2Tests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "assets" / "whale-sources" / "evaluation-v2"
        self.manifest_path = self.root / "manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def test_manifest_has_eight_segments_from_two_independent_recordings(self):
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(
            self.manifest["kind"], "humpback_whale_independent_evaluation_set"
        )
        self.assertTrue(self.manifest["model_or_parameter_tuning_forbidden"])
        self.assertEqual(self.manifest["independent_field_recording_count"], 2)
        self.assertEqual(self.manifest["segment_count"], 8)
        self.assertEqual(len(self.manifest["clips"]), 8)
        self.assertEqual(len(self.manifest["source_bindings"]), 2)
        self.assertEqual(len(set(self.manifest["source_ids"])), 8)
        self.assertIn("methodology_amendment", self.manifest)

    def test_segment_intervals_are_fixed_non_overlapping_and_in_bounds(self):
        clips_by_raw: dict[str, list[dict[str, object]]] = {}
        for clip in self.manifest["clips"]:
            clips_by_raw.setdefault(str(clip["raw_file"]), []).append(clip)
            self.assertEqual(clip["duration_seconds"], 2.0)
            self.assertEqual(clip["sample_rate_hz"], 48_000)
            self.assertIn(clip["source_sample_rate_hz"], {5_000, 44_100})
            self.assertEqual(clip["analysis_lowpass_hz"], 1_650)
            self.assertLess(clip["analysis_lowpass_hz"], clip["source_nyquist_hz"])
            self.assertEqual(
                clip["call_type"], "unclassified fixed-interval field segment"
            )
        self.assertEqual(set(clips_by_raw), {"raw/HB-ship-SBNMS.wav", "raw/HB-ship-AMSNP.wav"})
        for clips in clips_by_raw.values():
            intervals = sorted(tuple(clip["source_interval_ms"]) for clip in clips)
            self.assertEqual(len(intervals), 4)
            for (start, end), following in zip(intervals, intervals[1:]):
                self.assertEqual(end - start, 2_000)
                self.assertLessEqual(end, following[0])
            self.assertEqual(intervals[-1][1] - intervals[-1][0], 2_000)

    def test_lanczos_derivatives_fade_to_exact_zero(self):
        for clip in self.manifest["clips"]:
            path = self.root / str(clip["processed_file"])
            with wave.open(str(path), "rb") as handle:
                raw = handle.readframes(handle.getnframes())
            samples = struct.unpack(f"<{len(raw) // 2}h", raw)
            self.assertEqual(samples[0], 0)
            self.assertEqual(samples[-1], 0)
            self.assertIn("Lanczos-windowed sinc", " ".join(clip["processing"]))

    def test_raw_and_processed_bytes_are_hash_bound(self):
        for clip in self.manifest["clips"]:
            for path_key, hash_key in (
                ("raw_file", "raw_sha256"),
                ("processed_file", "processed_sha256"),
            ):
                path = self.root / str(clip[path_key])
                self.assertTrue(path.is_file())
                self.assertFalse(path.is_symlink())
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(), clip[hash_key]
                )

    def test_committed_external_set_is_byte_reproducible(self):
        manifest_payload, processed = builder.build_payloads(self.root / "raw")
        self.assertEqual(self.manifest_path.read_bytes(), manifest_payload)
        for relative, payload in processed.items():
            self.assertEqual((self.root / relative).read_bytes(), payload)

    def test_external_csv_summary_is_byte_reproducible(self):
        report_path = (
            ROOT
            / "assets"
            / "whale-sources"
            / "studies"
            / "organic-ablation-v51"
            / "external-report-all.json"
        )
        csv_path = report_path.with_name("external-summary.csv")
        self.assertEqual(
            csv_path.read_bytes(), summary.build_csv(report_path.read_bytes())
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        expected_lines = 1 + sum(len(clip["results"]) for clip in report["clips"])
        self.assertEqual(
            len(csv_path.read_text(encoding="utf-8").splitlines()), expected_lines
        )

    def test_frozen_morph_candidate_is_an_alias_not_a_duplicate_render(self):
        report_path = (
            ROOT
            / "assets"
            / "whale-sources"
            / "studies"
            / "organic-ablation-v51"
            / "external-report-all.json"
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(
            report["candidate"]["evaluation_alias"],
            {"alias": "frozen-candidate", "target": "morph"},
        )
        for clip in report["clips"]:
            self.assertEqual(
                [result["variant_id"] for result in clip["results"]],
                ["morph", "organic-full"],
            )
            self.assertEqual(len(clip["results"]), 2)

    def test_repository_relative_candidate_paths_are_supported(self):
        relative = pathlib.Path(
            "assets/whale-sources/studies/organic-ablation-v51/frozen-candidate.json"
        )
        safe, repository_relative = study.repository_regular_path(
            relative, "test candidate"
        )
        self.assertTrue(safe.is_absolute())
        self.assertEqual(repository_relative, relative)

    def test_external_sources_never_enter_model_or_runtime(self):
        model_source_ids = set(source_filter.WhaleSourceFilterBank().source_ids)
        self.assertTrue(model_source_ids.isdisjoint(self.manifest["source_ids"]))
        forbidden = (
            "assets/whale-sources/evaluation-v2/",
            "noaa-pmel-stellwagen-ship-independent",
            "noaa-pmel-american-samoa-shrimp-independent",
        )
        runtime_paths = (
            ROOT / "scripts" / "build_whale_voice_model.py",
            ROOT / "scripts" / "whale_source_filter_engine.py",
            ROOT / "scripts" / "whale_organic_engine.py",
            ROOT / "scripts" / "whale_live.py",
        )
        for path in runtime_paths:
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                with self.subTest(path=path.name, marker=marker):
                    self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
