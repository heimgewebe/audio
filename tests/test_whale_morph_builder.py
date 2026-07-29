import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_whale_morph_bank as builder  # noqa: E402


class WhaleMorphBuilderSafetyTests(unittest.TestCase):
    def test_source_manifest_symlink_is_rejected_before_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            link = pathlib.Path(directory) / "manifest.json"
            link.symlink_to(builder.DEFAULT_SOURCE_MANIFEST)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                builder.regular_file_path(link, "whale sample manifest")

    def test_output_symlink_is_rejected_without_replacing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "target.json"
            target.write_text("unchanged\n", encoding="utf-8")
            link = root / "manifest.json"
            link.symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "symlink"):
                builder.write_atomic(link, {"state": "unexpected"})
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged\n")
            self.assertTrue(link.is_symlink())

    def test_output_parent_must_already_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "missing" / "manifest.json"
            with self.assertRaisesRegex(RuntimeError, "parent must already exist"):
                builder.validated_output_path(path)

    def test_duplicate_source_clip_ids_are_rejected(self):
        value = {
            "schema_version": 2,
            "kind": "humpback_whale_sample_bank",
            "sample_rate_hz": 48_000,
            "clips": [{"id": "duplicate"}, {"id": "duplicate"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "duplicate clip ids"):
                builder.load_source_index(path)

    def test_source_clip_filename_must_be_plain_basename(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = pathlib.Path(directory)
            for filename in ("../outside.wav", "subdir/clip.wav", "/absolute.wav"):
                with self.subTest(filename=filename):
                    with self.assertRaisesRegex(RuntimeError, "plain basename"):
                        builder.source_clip_path(parent, filename)


if __name__ == "__main__":
    unittest.main()
