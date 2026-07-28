import hashlib
import json
import pathlib
import struct
import sys
import tempfile
import unittest
import wave
from array import array
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_whale_sample_bank as builder  # noqa: E402


class WhaleSampleBuilderTests(unittest.TestCase):
    def make_root(
        self,
        directory: str,
        *,
        file_value: str = "raw/source-low.ogg",
        payload: bytes = b"licensed-source",
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        root = pathlib.Path(directory)
        raw = root / "raw"
        raw.mkdir(parents=True)
        source_records = []
        first_source = raw / "source-low.ogg"
        for category in ("low", "song", "high"):
            source = raw / f"source-{category}.ogg"
            source_payload = payload + category.encode("ascii")
            source.write_bytes(source_payload)
            configured_file = (
                file_value if category == "low" else f"raw/source-{category}.ogg"
            )
            source_records.append(
                {
                    "id": f"source-{category}",
                    "file": configured_file,
                    "category": category,
                    "license": "CC0-1.0",
                    "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                    "title": f"Test {category} source",
                    "creators": ["Test creator"],
                    "attribution": f"Test creator — Test {category} source",
                    "source_page": "https://example.invalid/source",
                    "changes": "Test derivative processing.",
                    "expected_bytes": len(source_payload),
                    "expected_sha256": hashlib.sha256(source_payload).hexdigest(),
                    "clip_count": 1,
                }
            )
        catalog = {
            "schema_version": 2,
            "kind": "humpback_whale_source_catalog",
            "sources": source_records,
        }
        catalog_path = root / "SOURCES.json"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        return root, catalog_path, first_source

    @staticmethod
    def fake_ffmpeg(_source: pathlib.Path, output: pathlib.Path) -> None:
        samples = array(
            "h", (1200 if index % 200 < 100 else -1200 for index in range(8 * 48_000))
        )
        if struct.pack("=H", 1) != struct.pack("<H", 1):
            samples.byteswap()
        with wave.open(str(output), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48_000)
            handle.writeframes(samples.tobytes())

    def test_candidate_rms_has_no_audioop_dependency(self):
        samples = array("h", [0] * 48_000 + [2000] * 48_000 + [0] * 48_000)
        centers = builder.candidate_centers(samples, 1, 48_000)
        self.assertEqual(len(centers), 1)
        self.assertGreater(centers[0], 48_000)

    def test_raw_hash_is_authoritative_before_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, source = self.make_root(directory)
            source.write_bytes(b"tampered")
            with mock.patch.object(builder, "run_ffmpeg") as ffmpeg:
                with self.assertRaisesRegex(RuntimeError, "byte-size mismatch"):
                    builder.build(catalog, root / "processed")
            ffmpeg.assert_not_called()

    def test_rejects_traversal_symlinks_and_external_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(
                directory, file_value="../outside.ogg"
            )
            with self.assertRaisesRegex(RuntimeError, "unsafe whale source path"):
                builder.build(catalog, root / "processed")

        with tempfile.TemporaryDirectory() as directory:
            root, catalog, source = self.make_root(directory)
            external = root / "outside.ogg"
            external.write_bytes(source.read_bytes())
            source.unlink()
            source.symlink_to(external)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                builder.build(catalog, root / "processed")

        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            (root / "nested").mkdir()
            with self.assertRaisesRegex(RuntimeError, "direct child"):
                builder.build(catalog, root / "nested" / "processed")

    def test_rejects_unreferenced_symlink_in_raw_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, source = self.make_root(directory)
            (root / "raw" / "unlisted.ogg").symlink_to(source)
            with self.assertRaisesRegex(
                RuntimeError, "unsafe raw whale source entries"
            ):
                builder.build(catalog, root / "processed")

    def test_failed_build_preserves_existing_bank_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            output = root / "processed"
            output.mkdir()
            marker = output / "marker.txt"
            marker.write_text("old-bank", encoding="utf-8")
            with mock.patch.object(
                builder, "run_ffmpeg", side_effect=RuntimeError("synthetic failure")
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                    builder.build(catalog, output)
            self.assertEqual(marker.read_text(encoding="utf-8"), "old-bank")
            self.assertFalse(list(root.glob(".processed-staging-*")))
            self.assertFalse(list(root.glob(".processed-backup-*")))

    def test_atomic_exchange_failure_preserves_existing_bank(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            output = root / "processed"
            output.mkdir()
            marker = output / "marker.txt"
            marker.write_text("old-bank", encoding="utf-8")
            with (
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
                mock.patch.object(
                    builder,
                    "_rename_exchange",
                    side_effect=RuntimeError("exchange unavailable"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "exchange unavailable"):
                    builder.build(catalog, output)
            self.assertEqual(marker.read_text(encoding="utf-8"), "old-bank")
            self.assertFalse(list(root.glob(".processed-staging-*")))

    def test_successful_build_atomically_replaces_old_bank(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            output = root / "processed"
            output.mkdir()
            (output / "marker.txt").write_text("old-bank", encoding="utf-8")
            with mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg):
                manifest = builder.build(catalog, output)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertFalse((output / "marker.txt").exists())
            self.assertTrue((output / "manifest.json").is_file())
            self.assertEqual(len(list(output.glob("*.wav"))), 3)
            self.assertFalse(list(root.glob(".processed-staging-*")))
            self.assertFalse(list(root.glob(".processed-backup-*")))


if __name__ == "__main__":
    unittest.main()
