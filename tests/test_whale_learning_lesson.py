import hashlib
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "whale_learning_lesson.py"
SPEC = importlib.util.spec_from_file_location(
    "whale_learning_lesson_test", MODULE_PATH
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class WhaleLearningLessonTests(unittest.TestCase):
    def test_contract_binds_truth_layers_audio_and_features(self):
        lesson = MODULE.load_lesson_contract()
        self.assertFalse(lesson["authoritative"])
        self.assertTrue(lesson["read_only"])
        self.assertEqual(
            [variant["id"] for variant in lesson["variants"]],
            list(MODULE.VARIANT_IDS),
        )
        self.assertEqual(
            set(lesson["truth_layers"]),
            {"observation", "model", "extrapolation"},
        )
        self.assertEqual(
            lesson["blind_comparison"]["candidate_ids"],
            ["morph", "articulation"],
        )
        self.assertEqual(len(lesson["model_sources"]["sources"]), 4)
        self.assertIn(
            "CC-BY-2.5",
            {source["license"] for source in lesson["model_sources"]["sources"]},
        )
        for variant in lesson["variants"]:
            payload = (ROOT / "ui" / variant["audio_file"]).read_bytes()
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(), variant["audio_sha256"]
            )
            self.assertEqual(len(variant["features"]["envelope"]), 48)
            self.assertEqual(len(variant["features"]["periodicity"]), 48)
            self.assertEqual(len(variant["features"]["roughness"]), 48)

    def test_audio_hash_drift_fails_closed(self):
        source_manifest = (
            ROOT / "inventory" / "buckelwal-learning-lesson.v1.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            ui_root = root / "ui"
            ui_root.mkdir()
            manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
            for variant in manifest["variants"]:
                shutil.copy2(
                    ROOT / "ui" / variant["audio_file"],
                    ui_root / variant["audio_file"],
                )
            target_manifest = root / "lesson.json"
            target_manifest.write_text(json.dumps(manifest), encoding="utf-8")
            first = ui_root / manifest["variants"][0]["audio_file"]
            first.write_bytes(first.read_bytes() + b"x")
            with self.assertRaisesRegex(MODULE.LessonError, "Größe|SHA-256"):
                MODULE.load_lesson_contract(
                    target_manifest, ui_root=ui_root
                )

    def test_model_and_reference_repository_bindings_fail_closed(self):
        source = json.loads(
            (ROOT / "inventory" / "buckelwal-learning-lesson.v1.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            ui_root = root / "ui"
            shutil.copytree(ROOT / "ui", ui_root)
            manifest = root / "lesson.json"
            source["model_sources"]["morph_manifest"]["sha256"] = "0" * 64
            manifest.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.LessonError, "Morphmanifest.*SHA-256"):
                MODULE.load_lesson_contract(manifest, ui_root=ui_root)
            source = json.loads(
                (ROOT / "inventory" / "buckelwal-learning-lesson.v1.json").read_text(
                    encoding="utf-8"
                )
            )
            source["model_sources"]["sources"].reverse()
            manifest.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.LessonError, "Morphankern"):
                MODULE.load_lesson_contract(manifest, ui_root=ui_root)
            source = json.loads(
                (ROOT / "inventory" / "buckelwal-learning-lesson.v1.json").read_text(
                    encoding="utf-8"
                )
            )
            source["reference_source"]["sha256"] = "0" * 64
            manifest.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.LessonError, "Referenzquelle.*SHA-256"):
                MODULE.load_lesson_contract(manifest, ui_root=ui_root)

    def test_unknown_variant_and_truth_collapse_fail_closed(self):
        source = json.loads(
            (
                ROOT / "inventory" / "buckelwal-learning-lesson.v1.json"
            ).read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            ui_root = root / "ui"
            shutil.copytree(ROOT / "ui", ui_root)
            manifest = root / "lesson.json"
            source["variants"][1]["id"] = "free-form"
            manifest.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.LessonError, "ID"):
                MODULE.load_lesson_contract(manifest, ui_root=ui_root)
            source["variants"][1]["id"] = "morph"
            source["truth_layers"].pop("extrapolation")
            manifest.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.LessonError, "Wahrheitsebenen"):
                MODULE.load_lesson_contract(manifest, ui_root=ui_root)


if __name__ == "__main__":
    unittest.main()
