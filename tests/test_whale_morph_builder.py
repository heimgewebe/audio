import array
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
import wave
from unittest import mock

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


    @staticmethod
    def pcm16_wav_bytes(sample_value: int) -> bytes:
        samples = array.array("h", [sample_value]) * builder.SAMPLE_RATE
        if sys.byteorder != "little":
            samples.byteswap()
        payload = io.BytesIO()
        with wave.open(payload, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(builder.SAMPLE_RATE)
            handle.writeframes(samples.tobytes())
        return payload.getvalue()

    def test_manifest_hash_is_bound_to_the_parsed_snapshot(self):
        original = {
            "schema_version": 2,
            "kind": "humpback_whale_sample_bank",
            "sample_rate_hz": builder.SAMPLE_RATE,
            "clips": [{"id": "original"}],
        }
        replacement = {**original, "clips": [{"id": "replacement"}]}
        original_payload = json.dumps(original).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.json"
            path.write_bytes(original_payload)
            real_parse = builder.parse_source_index

            def parse_then_replace(payload: bytes):
                index = real_parse(payload)
                path.write_text(json.dumps(replacement), encoding="utf-8")
                return index

            with mock.patch.object(builder, "parse_source_index", side_effect=parse_then_replace):
                snapshot = builder.load_source_manifest_snapshot(path)

            self.assertEqual(set(snapshot.index), {"original"})
            self.assertEqual(snapshot.sha256, hashlib.sha256(original_payload).hexdigest())
            self.assertEqual(set(builder.load_source_index(path)), {"replacement"})

    def test_clip_hash_and_decode_use_the_same_snapshot(self):
        original_payload = self.pcm16_wav_bytes(1234)
        replacement_payload = self.pcm16_wav_bytes(-2345)
        expected_sha = hashlib.sha256(original_payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "clip.wav"
            path.write_bytes(original_payload)
            real_sha256 = builder.sha256_bytes

            def hash_then_replace(payload: bytes):
                digest = real_sha256(payload)
                path.write_bytes(replacement_payload)
                return digest

            with mock.patch.object(builder, "sha256_bytes", side_effect=hash_then_replace):
                snapshot = builder.load_source_clip_snapshot(path, expected_sha, "clip")

            self.assertEqual(snapshot.sha256, expected_sha)
            self.assertEqual(snapshot.samples[0], 1234)
            self.assertEqual(builder.read_pcm16_mono(path)[0], -2345)

    def test_source_clip_filename_must_be_plain_basename(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = pathlib.Path(directory)
            for filename in ("../outside.wav", "subdir/clip.wav", "/absolute.wav"):
                with self.subTest(filename=filename):
                    with self.assertRaisesRegex(RuntimeError, "plain basename"):
                        builder.source_clip_path(parent, filename)


if __name__ == "__main__":
    unittest.main()
