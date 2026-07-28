import hashlib
import io
import json
import os
import pathlib
import stat
import subprocess
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
    def snapshot_tree(root: pathlib.Path) -> dict[str, tuple[object, ...]]:
        snapshot = {}
        for path in (root, *sorted(root.rglob("*"))):
            relative = "." if path == root else path.relative_to(root).as_posix()
            metadata = path.lstat()
            if path.is_file():
                payload = path.read_bytes()
                snapshot[relative] = (
                    "file",
                    metadata.st_dev,
                    metadata.st_ino,
                    len(payload),
                    hashlib.sha256(payload).hexdigest(),
                    payload,
                )
            elif path.is_dir():
                snapshot[relative] = (
                    "directory",
                    metadata.st_dev,
                    metadata.st_ino,
                )
            else:
                snapshot[relative] = (
                    "other",
                    metadata.st_dev,
                    metadata.st_ino,
                    os.readlink(path) if path.is_symlink() else None,
                )
        return snapshot

    def run_builder(
        self, root: pathlib.Path, catalog: pathlib.Path, output: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "build_whale_sample_bank.py"),
                "--catalog",
                str(catalog),
                "--output",
                output,
            ],
            cwd=root,
            check=False,
            text=True,
            capture_output=True,
        )

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

    def test_mutation_after_catalog_check_is_rejected_before_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, source = self.make_root(directory)
            original_snapshot = builder.snapshot_verified_source

            def mutate_then_snapshot(record, source_fd, destination):
                if record["category"] == "low":
                    source.write_bytes(b"changed-after-catalog-validation")
                return original_snapshot(record, source_fd, destination)

            with (
                mock.patch.object(
                    builder,
                    "snapshot_verified_source",
                    side_effect=mutate_then_snapshot,
                ),
                mock.patch.object(builder, "run_ffmpeg") as ffmpeg,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "snapshot byte-size mismatch"
                ):
                    builder.build(catalog, root / "processed")
            ffmpeg.assert_not_called()

    def test_ffmpeg_uses_the_verified_private_source_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, source = self.make_root(directory)
            expected = source.read_bytes()
            observed_sources = []

            def inspect_snapshot(snapshot, output):
                observed_sources.append(snapshot)
                self.assertEqual(snapshot.parent.name, ".sources")
                if snapshot.name.startswith("source-low"):
                    source.write_bytes(b"replacement-after-snapshot")
                    self.assertEqual(snapshot.read_bytes(), expected)
                    self.assertNotEqual(snapshot.read_bytes(), source.read_bytes())
                self.fake_ffmpeg(snapshot, output)

            with mock.patch.object(builder, "run_ffmpeg", side_effect=inspect_snapshot):
                builder.build(catalog, root / "processed")
            self.assertEqual(len(observed_sources), 3)
            self.assertTrue(all(not snapshot.exists() for snapshot in observed_sources))

    def test_transient_catalog_exchange_cannot_change_bound_build_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            original_catalog = catalog.read_bytes()
            displaced = root / ".original-catalog"
            real_read = builder._read_descriptor_bytes
            exchange_count = 0

            def exchange_while_reading(catalog_fd):
                nonlocal exchange_count
                exchange_count += 1
                os.rename(catalog, displaced)
                catalog.write_bytes(b"[]")
                try:
                    return real_read(catalog_fd)
                finally:
                    catalog.unlink()
                    os.rename(displaced, catalog)

            with (
                mock.patch.object(
                    builder,
                    "_read_descriptor_bytes",
                    side_effect=exchange_while_reading,
                ),
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
            ):
                manifest = builder.build(catalog, root / "processed")

            self.assertEqual(exchange_count, 1)
            self.assertEqual(len(manifest["sources"]), 3)
            self.assertEqual(len(manifest["clips"]), 3)
            self.assertEqual(
                manifest["source_catalog_sha256"],
                hashlib.sha256(original_catalog).hexdigest(),
            )
            self.assertEqual(catalog.read_bytes(), original_catalog)

    def test_manifest_hash_covers_the_exact_catalog_bytes_given_to_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            catalog_bytes = catalog.read_bytes()
            parsed_bytes = []
            real_parse = builder._parse_catalog_bytes

            def capture_parsed_bytes(payload):
                parsed_bytes.append(payload)
                return real_parse(payload)

            with (
                mock.patch.object(
                    builder,
                    "_parse_catalog_bytes",
                    side_effect=capture_parsed_bytes,
                ),
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
            ):
                manifest = builder.build(catalog, root / "processed")

            self.assertEqual(parsed_bytes, [catalog_bytes])
            self.assertEqual(
                manifest["source_catalog_sha256"],
                hashlib.sha256(parsed_bytes[0]).hexdigest(),
            )

    def test_snapshot_reads_bound_fd_during_transient_raw_path_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, source = self.make_root(directory)
            original_payload = source.read_bytes()
            displaced = root / ".bound-source-low.ogg"
            real_snapshot = builder.snapshot_verified_source
            exchange_count = 0

            def exchange_then_snapshot(record, source_fd, destination):
                nonlocal exchange_count
                if record["category"] != "low":
                    return real_snapshot(record, source_fd, destination)
                exchange_count += 1
                os.rename(source, displaced)
                source.write_bytes(b"replacement-at-raw-path")
                try:
                    result = real_snapshot(record, source_fd, destination)
                    self.assertEqual(destination.read_bytes(), original_payload)
                    return result
                finally:
                    source.unlink()
                    os.rename(displaced, source)

            with (
                mock.patch.object(
                    builder,
                    "snapshot_verified_source",
                    side_effect=exchange_then_snapshot,
                ),
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
            ):
                manifest = builder.build(catalog, root / "processed")

            self.assertEqual(exchange_count, 1)
            self.assertEqual(len(manifest["clips"]), 3)
            self.assertEqual(source.read_bytes(), original_payload)

    def test_each_catalog_source_is_opened_once_relative_to_bound_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            raw_metadata = (root / "raw").stat()
            raw_identity = (raw_metadata.st_dev, raw_metadata.st_ino)
            source_opens = []
            real_open = builder._open_nofollow

            def observe_open(directory_fd, name, *, directory):
                if name.startswith("source-"):
                    parent_metadata = os.fstat(directory_fd)
                    source_opens.append(
                        (
                            name,
                            directory,
                            parent_metadata.st_dev,
                            parent_metadata.st_ino,
                        )
                    )
                return real_open(directory_fd, name, directory=directory)

            with (
                mock.patch.object(
                    builder,
                    "_open_nofollow",
                    side_effect=observe_open,
                ),
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
            ):
                builder.build(catalog, root / "processed")

            self.assertEqual(
                source_opens,
                [
                    ("source-low.ogg", False, *raw_identity),
                    ("source-song.ogg", False, *raw_identity),
                    ("source-high.ogg", False, *raw_identity),
                ],
            )

    def test_bound_source_descriptors_keep_zero_offset_and_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            observed_descriptors = []
            real_snapshot = builder.snapshot_verified_source

            def observe_descriptor(source, source_fd, destination):
                self.assertEqual(os.lseek(source_fd, 0, os.SEEK_CUR), 0)
                result = real_snapshot(source, source_fd, destination)
                self.assertEqual(os.lseek(source_fd, 0, os.SEEK_CUR), 0)
                observed_descriptors.append(source_fd)
                return result

            with (
                mock.patch.object(
                    builder,
                    "snapshot_verified_source",
                    side_effect=observe_descriptor,
                ),
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
            ):
                builder.build(catalog, root / "processed")

            self.assertEqual(len(observed_descriptors), 3)
            self.assertEqual(len(set(observed_descriptors)), 3)
            for descriptor in observed_descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_repeated_build_is_byte_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            output = root / "processed"

            def file_payloads():
                return {
                    path.relative_to(output).as_posix(): path.read_bytes()
                    for path in sorted(output.rglob("*"))
                    if path.is_file()
                }

            with mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg):
                first_manifest = builder.build(catalog, output)
                first_payloads = file_payloads()
                second_manifest = builder.build(catalog, output)
                second_payloads = file_payloads()

            self.assertEqual(second_manifest, first_manifest)
            self.assertEqual(second_payloads, first_payloads)

    def test_non_object_catalog_root_is_controlled_json_error(self):
        for payload in ("[]", "null", '"scalar"'):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as directory:
                    root = pathlib.Path(directory)
                    (root / "raw").mkdir()
                    catalog = root / "SOURCES.json"
                    catalog.write_text(payload, encoding="utf-8")
                    output = root / "processed"
                    stderr = io.StringIO()

                    with mock.patch("sys.stderr", stderr):
                        result = builder.main(
                            [
                                "--catalog",
                                str(catalog),
                                "--output",
                                str(output),
                            ]
                        )

                    self.assertEqual(result, 2)
                    error_text = stderr.getvalue().strip()
                    self.assertEqual(len(error_text.splitlines()), 1)
                    response = json.loads(error_text)
                    self.assertEqual(response["state"], "blocked")
                    self.assertIn("catalog root must be an object", response["error"])
                    self.assertFalse(output.exists())

    def test_invalid_builder_arguments_are_one_json_error(self):
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            result = builder.main(["--unknown-option"])

        lines = stderr.getvalue().splitlines()
        self.assertEqual(result, 2)
        self.assertEqual(len(lines), 1)
        response = json.loads(lines[0])
        self.assertEqual(response["state"], "blocked")
        self.assertIn("unrecognized arguments", response["error"])

    def test_raw_hash_is_authoritative_before_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, source = self.make_root(directory)
            source.write_bytes(b"tampered")
            with mock.patch.object(builder, "run_ffmpeg") as ffmpeg:
                with self.assertRaisesRegex(RuntimeError, "byte-size mismatch"):
                    builder.build(catalog, root / "processed")
            ffmpeg.assert_not_called()

    def test_cli_blocks_every_raw_output_spelling_without_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            raw_before = self.snapshot_tree(root / "raw")
            raw_inode = (root / "raw").stat().st_ino
            output_spellings = (
                "raw",
                "raw/",
                "./raw",
                str(root / "raw"),
                f"{root}/./raw",
            )

            for output in output_spellings:
                with self.subTest(output=output):
                    result = self.run_builder(root, catalog, output)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    error_lines = result.stderr.strip().splitlines()
                    self.assertEqual(len(error_lines), 1)
                    response = json.loads(error_lines[0])
                    self.assertEqual(response["state"], "blocked")
                    self.assertIn("protected", response["error"])
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(
                        self.snapshot_tree(root / "raw"),
                        raw_before,
                    )
                    self.assertEqual((root / "raw").stat().st_ino, raw_inode)
                    self.assertFalse(list(root.glob(".raw-staging-*")))
                    self.assertFalse(list(root.glob(".raw-backup-*")))

    def test_raw_output_is_blocked_before_any_staging_or_destructive_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            raw_before = self.snapshot_tree(root / "raw")
            stderr = io.StringIO()
            with (
                mock.patch("sys.stderr", stderr),
                mock.patch.object(builder.tempfile, "mkdtemp") as make_staging,
                mock.patch.object(builder, "_atomic_replace_directory") as replace,
                mock.patch.object(builder, "_rename_exchange") as exchange,
                mock.patch.object(builder.os, "replace") as os_replace,
                mock.patch.object(builder.shutil, "rmtree") as remove_tree,
            ):
                result = builder.main(
                    ["--catalog", str(catalog), "--output", str(root / "raw")]
                )

            self.assertEqual(result, 2)
            self.assertEqual(len(stderr.getvalue().strip().splitlines()), 1)
            self.assertEqual(json.loads(stderr.getvalue())["state"], "blocked")
            make_staging.assert_not_called()
            replace.assert_not_called()
            exchange.assert_not_called()
            os_replace.assert_not_called()
            remove_tree.assert_not_called()
            self.assertEqual(self.snapshot_tree(root / "raw"), raw_before)

    def test_cli_blocks_symlink_and_parent_aliases_to_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            raw_before = self.snapshot_tree(root / "raw")
            (root / "raw-alias").symlink_to(root / "raw", target_is_directory=True)
            root_alias = root.parent / f"{root.name}-alias"
            root_alias.symlink_to(root, target_is_directory=True)
            self.addCleanup(root_alias.unlink)
            outputs = (
                str(root / "raw-alias"),
                str(root_alias / "raw"),
            )

            for output in outputs:
                with self.subTest(output=output):
                    result = self.run_builder(root, catalog, output)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    error_lines = result.stderr.strip().splitlines()
                    self.assertEqual(len(error_lines), 1)
                    self.assertEqual(json.loads(error_lines[0])["state"], "blocked")
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(self.snapshot_tree(root / "raw"), raw_before)
                    self.assertFalse(list(root.glob(".*-staging-*")))

    def test_existing_custom_output_must_not_contain_input_aliases(self):
        alias_kinds = ("catalog-hardlink", "source-hardlink", "source-symlink")
        for alias_kind in alias_kinds:
            with self.subTest(alias_kind=alias_kind):
                with tempfile.TemporaryDirectory() as directory:
                    root, catalog, source = self.make_root(directory)
                    output = root / "custom"
                    output.mkdir()
                    alias = output / "bound-input"
                    if alias_kind == "catalog-hardlink":
                        os.link(catalog, alias)
                    elif alias_kind == "source-hardlink":
                        os.link(source, alias)
                    else:
                        alias.symlink_to(source)
                    raw_before = self.snapshot_tree(root / "raw")

                    with (
                        mock.patch.object(builder, "run_ffmpeg") as ffmpeg,
                        mock.patch.object(builder.tempfile, "mkdtemp") as make_staging,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "protected"):
                            builder.build(catalog, output)

                    ffmpeg.assert_not_called()
                    make_staging.assert_not_called()
                    self.assertEqual(self.snapshot_tree(root / "raw"), raw_before)
                    self.assertTrue(alias.exists())
                    self.assertFalse(list(root.glob(".custom-staging-*")))

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

    def test_missing_output_never_falls_back_from_bound_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            output = root / "processed"
            real_rename_noreplace = builder._rename_noreplace

            def fail_output_install(directory_fd, first_name, second_name):
                if second_name == output.name:
                    raise RuntimeError("bound rename unavailable")
                return real_rename_noreplace(
                    directory_fd,
                    first_name,
                    second_name,
                )

            with (
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
                mock.patch.object(
                    builder,
                    "_rename_noreplace",
                    side_effect=fail_output_install,
                ),
                mock.patch.object(builder.os, "replace") as unsafe_replace,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "bound rename unavailable.*layout restored"
                ):
                    builder.build(catalog, output)

            unsafe_replace.assert_not_called()
            self.assertFalse(output.exists())
            self.assertFalse(list(root.glob(".processed-staging-*")))

    def test_race_after_last_validation_is_rolled_back_without_raw_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            raw = root / "raw"
            output = root / "processed"
            output.mkdir()
            (output / "marker.txt").write_text("old-bank", encoding="utf-8")
            raw_before = self.snapshot_tree(raw)
            output_before = self.snapshot_tree(output)
            protected_identities = {
                (values[1], values[2]) for values in raw_before.values()
            }
            real_capture = builder._capture_replace_state
            real_rmtree = builder.shutil.rmtree
            capture_count = 0
            removed_identities = []

            def capture_then_swap(*args, **kwargs):
                nonlocal capture_count
                state = real_capture(*args, **kwargs)
                capture_count += 1
                displaced = root / ".race-displaced"
                os.rename(raw, displaced)
                os.rename(output, raw)
                os.rename(displaced, output)
                return state

            def observe_rmtree(path, *args, **kwargs):
                candidate = pathlib.Path(path)
                if candidate.exists():
                    metadata = candidate.lstat()
                    removed_identities.append((metadata.st_dev, metadata.st_ino))
                return real_rmtree(path, *args, **kwargs)

            with (
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
                mock.patch.object(
                    builder,
                    "_capture_replace_state",
                    side_effect=capture_then_swap,
                ),
                mock.patch.object(builder.shutil, "rmtree", side_effect=observe_rmtree),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "protected identity changed.*layout restored"
                ):
                    builder.build(catalog, output)

            self.assertEqual(capture_count, 1)
            self.assertEqual(self.snapshot_tree(raw), raw_before)
            self.assertEqual(self.snapshot_tree(output), output_before)
            self.assertTrue(
                protected_identities.isdisjoint(removed_identities),
                "recursive cleanup targeted a protected raw identity",
            )
            self.assertFalse(list(root.glob(".processed-staging-*")))
            self.assertFalse((root / ".race-displaced").exists())

    def test_race_after_exchange_is_rolled_back_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            raw = root / "raw"
            output = root / "processed"
            output.mkdir()
            (output / "marker.txt").write_text("old-bank", encoding="utf-8")
            raw_before = self.snapshot_tree(raw)
            output_before = self.snapshot_tree(output)
            real_exchange = builder._rename_exchange

            def swap_raw_with_backup(bindings, state):
                real_exchange(bindings.source_root_fd, "raw", state.staging_name)

            with (
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
                mock.patch.object(
                    builder,
                    "_after_exchange_before_cleanup",
                    side_effect=swap_raw_with_backup,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "protected identity changed.*layout restored"
                ):
                    builder.build(catalog, output)

            self.assertEqual(self.snapshot_tree(raw), raw_before)
            self.assertEqual(self.snapshot_tree(output), output_before)
            self.assertFalse(list(root.glob(".processed-staging-*")))

    def test_failed_rollback_preserves_every_path_without_recursive_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            raw = root / "raw"
            output = root / "processed"
            output.mkdir()
            (output / "marker.txt").write_text("old-bank", encoding="utf-8")
            raw_before = self.snapshot_tree(raw)
            raw_identity = (
                raw.stat().st_dev,
                raw.stat().st_ino,
                stat.S_IFMT(raw.stat().st_mode),
            )
            real_exchange = builder._rename_exchange
            real_rmtree = builder.shutil.rmtree
            exchange_count = 0
            race_started = False
            cleanup_after_race = []

            def fail_rollback(directory_fd, first_name, second_name):
                nonlocal exchange_count
                exchange_count += 1
                if exchange_count == 1:
                    return real_exchange(directory_fd, first_name, second_name)
                raise RuntimeError("synthetic rollback failure")

            def race_after_exchange(bindings, state):
                nonlocal race_started
                real_exchange(bindings.source_root_fd, "raw", state.staging_name)
                race_started = True

            def observe_rmtree(path, *args, **kwargs):
                if race_started:
                    cleanup_after_race.append(pathlib.Path(path))
                return real_rmtree(path, *args, **kwargs)

            with (
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
                mock.patch.object(
                    builder, "_rename_exchange", side_effect=fail_rollback
                ),
                mock.patch.object(
                    builder,
                    "_after_exchange_before_cleanup",
                    side_effect=race_after_exchange,
                ),
                mock.patch.object(builder.shutil, "rmtree", side_effect=observe_rmtree),
            ):
                with self.assertRaisesRegex(
                    builder._RecoveryRequiredError,
                    "outcome is unknown.*no further recursive deletion.*manual recovery",
                ):
                    builder.build(catalog, output)

            staging_paths = list(root.glob(".processed-staging-*"))
            self.assertEqual(len(staging_paths), 1)
            staging = staging_paths[0]
            staging_metadata = staging.stat()
            self.assertEqual(
                (
                    staging_metadata.st_dev,
                    staging_metadata.st_ino,
                    stat.S_IFMT(staging_metadata.st_mode),
                ),
                raw_identity,
            )
            self.assertEqual(self.snapshot_tree(staging), raw_before)
            self.assertTrue(raw.is_dir())
            self.assertTrue(output.is_dir())
            self.assertEqual(cleanup_after_race, [])

    def _assert_retention_race_preserves_raw(self, seam_name):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            raw = root / "raw"
            raw_before = self.snapshot_tree(raw)
            raw_metadata = raw.stat()
            raw_identity = (
                raw_metadata.st_dev,
                raw_metadata.st_ino,
                stat.S_IFMT(raw_metadata.st_mode),
            )
            output = root / "processed"
            output.mkdir()
            (output / "marker.txt").write_text("old-bank", encoding="utf-8")
            race_started = False
            destructive_calls = []

            def exchange_raw_before_retention(
                source_parent_fd, source_name, _destination_name, _expected, label
            ):
                nonlocal race_started
                if label != "output backup":
                    return
                race_started = True
                builder._rename_exchange(source_parent_fd, "raw", source_name)

            def exchange_raw_after_retention(
                bindings, destination_name, _destination_fd, _expected, label
            ):
                nonlocal race_started
                if label != "output backup":
                    return
                race_started = True
                builder._rename_exchange(
                    bindings.source_root_fd, "raw", destination_name
                )

            def forbid_delete(*args, **kwargs):
                destructive_calls.append((args, kwargs))
                raise AssertionError("cleanup must retain instead of delete")

            seam = (
                exchange_raw_before_retention
                if seam_name == "_before_retention_rename"
                else exchange_raw_after_retention
            )
            with (
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
                mock.patch.object(builder, seam_name, side_effect=seam),
                mock.patch.object(builder.os, "unlink", side_effect=forbid_delete),
                mock.patch.object(builder.os, "rmdir", side_effect=forbid_delete),
            ):
                with self.assertRaisesRegex(
                    builder._RecoveryRequiredError,
                    "outcome is unknown.*manual recovery",
                ):
                    builder.build(catalog, output)

            retained = list(root.glob(f"{builder.RETAINED_CLEANUP_PREFIX}*"))
            retained_raw = [
                candidate
                for candidate in retained
                if (
                    candidate.stat().st_dev,
                    candidate.stat().st_ino,
                    stat.S_IFMT(candidate.stat().st_mode),
                )
                == raw_identity
            ]
            self.assertTrue(race_started)
            self.assertEqual(destructive_calls, [])
            self.assertEqual(len(retained_raw), 1)
            self.assertEqual(self.snapshot_tree(retained_raw[0]), raw_before)

            root_fd = os.open(root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
            try:
                builder._rename_exchange(root_fd, "raw", retained_raw[0].name)
            finally:
                os.close(root_fd)
            restored = raw.stat()
            self.assertEqual(
                (
                    restored.st_dev,
                    restored.st_ino,
                    stat.S_IFMT(restored.st_mode),
                ),
                raw_identity,
            )
            self.assertEqual(self.snapshot_tree(raw), raw_before)

    def test_retention_race_before_rename_preserves_raw_without_delete(self):
        self._assert_retention_race_preserves_raw("_before_retention_rename")

    def test_retention_race_after_open_preserves_raw_without_delete(self):
        self._assert_retention_race_preserves_raw("_after_retention_open")

    def _assert_raw_race_before_temporary_cleanup(self, temporary_name):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            raw = root / "raw"
            raw_before = self.snapshot_tree(raw)
            raw_metadata = raw.stat()
            race_started = False

            def exchange_raw_with_temporary(bindings, bound):
                nonlocal race_started
                if bound.name != temporary_name:
                    return
                race_started = True
                builder._rename_exchange_between(
                    bindings.source_root_fd,
                    "raw",
                    bound.parent_fd,
                    bound.name,
                )

            with (
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
                mock.patch.object(
                    builder,
                    "_before_temporary_cleanup",
                    side_effect=exchange_raw_with_temporary,
                ),
            ):
                with self.assertRaisesRegex(
                    builder._RecoveryRequiredError,
                    "failed staging outcome is unknown.*manual recovery",
                ):
                    builder.build(catalog, root / "processed")

            staging_paths = list(root.glob(".processed-staging-*"))
            self.assertTrue(race_started)
            self.assertEqual(len(staging_paths), 1)
            staging = staging_paths[0]
            moved_raw = staging / temporary_name
            moved_metadata = moved_raw.stat()
            self.assertEqual(
                (moved_metadata.st_dev, moved_metadata.st_ino),
                (raw_metadata.st_dev, raw_metadata.st_ino),
            )
            self.assertEqual(self.snapshot_tree(moved_raw), raw_before)

            root_fd = os.open(root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
            staging_fd = os.open(
                staging,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
            )
            try:
                builder._rename_exchange_between(
                    root_fd,
                    "raw",
                    staging_fd,
                    temporary_name,
                )
            finally:
                os.close(staging_fd)
                os.close(root_fd)
            self.assertEqual(self.snapshot_tree(raw), raw_before)
            restored_metadata = raw.stat()
            self.assertEqual(
                (restored_metadata.st_dev, restored_metadata.st_ino),
                (raw_metadata.st_dev, raw_metadata.st_ino),
            )

    def test_raw_race_before_intermediate_cleanup_is_fail_closed(self):
        self._assert_raw_race_before_temporary_cleanup(".intermediate")

    def test_raw_race_before_source_snapshot_cleanup_is_fail_closed(self):
        self._assert_raw_race_before_temporary_cleanup(".sources")

    def test_cleanup_retains_complete_directories_without_delete_syscalls(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            output = root / "processed"
            output.mkdir()
            regular = output / "regular.txt"
            regular.write_text("old-bank", encoding="utf-8")
            os.link(regular, output / "hardlink.txt")
            (output / "symlink.txt").symlink_to("regular.txt")
            nested = output / "nested"
            nested.mkdir()
            (nested / "child.txt").write_text("nested", encoding="utf-8")
            old_output = self.snapshot_tree(output)
            retention_evidence = []
            destructive_calls = []

            def forbid_delete(*args, **kwargs):
                destructive_calls.append((args, kwargs))
                raise AssertionError("builder cleanup must never unlink or rmdir")

            with (
                mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg),
                mock.patch.object(builder.os, "unlink", side_effect=forbid_delete),
                mock.patch.object(builder.os, "rmdir", side_effect=forbid_delete),
            ):
                manifest = builder.build(
                    catalog,
                    output,
                    retention_evidence=retention_evidence,
                )

            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(destructive_calls, [])
            self.assertFalse((output / "regular.txt").exists())
            self.assertFalse((output / "hardlink.txt").exists())
            self.assertFalse((output / "symlink.txt").exists())
            self.assertFalse((output / "nested").exists())
            self.assertTrue((output / "manifest.json").is_file())

            output_backups = [
                pathlib.Path(item["path"])
                for item in retention_evidence
                if item["label"] == "output backup"
            ]
            self.assertEqual(len(retention_evidence), 3)
            self.assertEqual(len(output_backups), 1)
            self.assertEqual(output_backups[0].parent, root)
            self.assertEqual(self.snapshot_tree(output_backups[0]), old_output)
            self.assertEqual(
                len(list(root.glob(f"{builder.RETAINED_CLEANUP_PREFIX}*"))),
                3,
            )

    def test_retained_cleanup_capacity_blocks_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            for index in range(6):
                (root / f"{builder.RETAINED_CLEANUP_PREFIX}{index}").mkdir()

            with mock.patch.object(builder, "run_ffmpeg") as run_ffmpeg:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "retained whale cleanup capacity exhausted.*no staging was created",
                ):
                    builder.build(catalog, root / "processed")

            run_ffmpeg.assert_not_called()
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

    def test_safe_custom_direct_child_is_still_atomically_replaceable(self):
        with tempfile.TemporaryDirectory() as directory:
            root, catalog, _source = self.make_root(directory)
            output = root / "custom-bank"
            output.mkdir()
            (output / "marker.txt").write_text("old-bank", encoding="utf-8")
            with mock.patch.object(builder, "run_ffmpeg", side_effect=self.fake_ffmpeg):
                manifest = builder.build(catalog, output)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertFalse((output / "marker.txt").exists())
            self.assertTrue((output / "manifest.json").is_file())
            self.assertEqual(len(list(output.glob("*.wav"))), 3)
            self.assertFalse(list(root.glob(".custom-bank-staging-*")))
            self.assertFalse(list(root.glob(".custom-bank-backup-*")))


if __name__ == "__main__":
    unittest.main()
