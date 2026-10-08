"""Regression tests use only synthetic H2 WAVs and isolated temp directories."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import audio_material_edit as EDIT
import h2_ingest as H2

SPEC = importlib.util.spec_from_file_location("h2_edit_fixture", ROOT / "tests" / "test_h2_ingest.py")
FIXTURE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(FIXTURE)

SCENE = "170926_191401"
MASTER = f"{SCENE}_MIX.WAV"


def render_wave(path: Path, *, frames: int = 640) -> bytes:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b"\x00\x00\x00\x00" * frames)
    return path.read_bytes()


class EditorRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = FIXTURE.make_source(self.root)
        self.library = self.root / "library"
        self.edit_root = self.root / "edits"
        receipt = H2.import_scene(SCENE, source_root=self.source, library_root=self.library)
        self.material_id = receipt["material_id"]
        self.original = self.library / self.material_id / "master" / MASTER
        self.original_digest = hashlib.sha256(self.original.read_bytes()).hexdigest()

    def prepare(self):
        return EDIT.prepare(self.material_id, MASTER, library_root=self.library, edit_root=self.edit_root)

    def finish(self, edit_id):
        return EDIT.finish(edit_id, library_root=self.library, edit_root=self.edit_root)

    def test_prepare_fsyncs_new_directory_entries_in_their_parents(self):
        from unittest import mock

        observed = set()
        actual_fsync = os.fsync

        def recorded(fd):
            metadata = os.fstat(fd)
            observed.add((metadata.st_dev, metadata.st_ino))
            return actual_fsync(fd)

        with mock.patch.object(EDIT.os, "fsync", side_effect=recorded):
            self.prepare()

        parent = self.root.stat()
        edit = self.edit_root.stat()
        self.assertIn((parent.st_dev, parent.st_ino), observed)
        self.assertIn((edit.st_dev, edit.st_ino), observed)

    def test_first_finish_fsyncs_render_directory_parent(self):
        from unittest import mock

        prepared = self.prepare()
        render_wave(Path(prepared["expected_render"]))
        self.assertFalse((self.edit_root / "renders").exists())
        observed = set()
        actual_fsync = os.fsync

        def recorded(fd):
            metadata = os.fstat(fd)
            observed.add((metadata.st_dev, metadata.st_ino))
            return actual_fsync(fd)

        with mock.patch.object(EDIT.os, "fsync", side_effect=recorded):
            result = self.finish(prepared["edit_id"])

        edit = self.edit_root.stat()
        self.assertIn((edit.st_dev, edit.st_ino), observed)
        self.assertTrue(Path(result["audio"]).exists())

    def test_failed_parent_fsync_blocks_first_render_publication(self):
        from unittest import mock

        prepared = self.prepare()
        render_wave(Path(prepared["expected_render"]))
        edit = self.edit_root.stat()
        actual_fsync = os.fsync

        def failing_parent(fd):
            metadata = os.fstat(fd)
            if (metadata.st_dev, metadata.st_ino) == (edit.st_dev, edit.st_ino):
                raise OSError("injected parent fsync failure")
            return actual_fsync(fd)

        with mock.patch.object(EDIT.os, "fsync", side_effect=failing_parent):
            with self.assertRaisesRegex(OSError, "injected parent fsync failure"):
                self.finish(prepared["edit_id"])

        self.assertTrue((self.edit_root / "renders").is_dir())
        self.assertEqual(list((self.edit_root / "renders").iterdir()), [])
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_full_roundtrip_preserves_master_and_writes_immutable_provenance(self):
        prepared = self.prepare()
        self.assertTrue(prepared["source_verified"])
        working = Path(prepared["working_copy"])
        self.assertEqual(hashlib.sha256(working.read_bytes()).hexdigest(), self.original_digest)
        self.assertNotEqual(working.stat().st_ino, self.original.stat().st_ino)
        expected = render_wave(Path(prepared["expected_render"]))
        result = self.finish(prepared["edit_id"])
        archived = Path(result["audio"])
        self.assertEqual(archived.read_bytes(), expected)
        self.assertEqual(result["render_sha256"], hashlib.sha256(expected).hexdigest())
        manifest = json.loads((archived.parent / "manifest.json").read_text())
        self.assertEqual(manifest["source"]["material_id"], self.material_id)
        self.assertEqual(manifest["source"]["master_sha256"], self.original_digest)
        self.assertEqual(manifest["derived_id"], result["derived_id"])
        self.assertTrue(result["original_untouched"])
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)
        self.assertTrue(H2.verify_material(self.material_id, library_root=self.library)["verified_current"])
        self.assertEqual(self.prepare()["edit_id"], prepared["edit_id"])
        self.assertEqual(self.finish(prepared["edit_id"])["derived_id"], result["derived_id"])

    def test_changes_create_new_derived_version_not_replace_previous(self):
        prepared = self.prepare()
        render = Path(prepared["expected_render"])
        render_wave(render, frames=640)
        first = self.finish(prepared["edit_id"])
        before = Path(first["audio"]).read_bytes()
        render_wave(render, frames=700)
        second = self.finish(prepared["edit_id"])
        self.assertNotEqual(first["derived_id"], second["derived_id"])
        self.assertEqual(Path(first["audio"]).read_bytes(), before)
        self.assertNotEqual(Path(second["audio"]).read_bytes(), before)

    def test_changed_working_copy_is_blocked_without_harming_master(self):
        prepared = self.prepare()
        copy = Path(prepared["working_copy"])
        os.chmod(copy, 0o600)
        copy.write_bytes(b"tampered")
        with self.assertRaisesRegex(EDIT.EditError, "Arbeitskopie"):
            self.prepare()
        render_wave(Path(prepared["expected_render"]))
        with self.assertRaisesRegex(EDIT.EditError, "Arbeitskopie"):
            self.finish(prepared["edit_id"])
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_symlink_render_is_rejected(self):
        prepared = self.prepare()
        outside = self.root / "outside.wav"
        render_wave(outside)
        Path(prepared["expected_render"]).symlink_to(outside)
        with self.assertRaisesRegex(EDIT.EditError, "symlinkfrei"):
            self.finish(prepared["edit_id"])
        self.assertFalse((self.edit_root / "renders").exists())

    def test_broken_wave_rejected_before_publishing(self):
        prepared = self.prepare()
        Path(prepared["expected_render"]).write_bytes(b"RIFFfake")
        with self.assertRaisesRegex(EDIT.EditError, "WAV"):
            self.finish(prepared["edit_id"])
        self.assertFalse((self.edit_root / "renders").exists())

    def test_tampered_archived_render_does_not_get_overwritten(self):
        prepared = self.prepare()
        render_wave(Path(prepared["expected_render"]))
        first = self.finish(prepared["edit_id"])
        archive = Path(first["audio"])
        os.chmod(archive, 0o600)
        archive.write_bytes(b"bad")
        with self.assertRaisesRegex(EDIT.EditError, "Archivergebnis"):
            self.finish(prepared["edit_id"])
        self.assertEqual(archive.read_bytes(), b"bad")
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_traversal_and_wrong_master_blocked(self):
        with self.assertRaisesRegex(EDIT.EditError, "Material-ID"):
            EDIT.prepare("../bad", MASTER, library_root=self.library, edit_root=self.edit_root)
        with self.assertRaisesRegex(EDIT.EditError, "Masterdateiname"):
            EDIT.prepare(self.material_id, "../foo.wav", library_root=self.library, edit_root=self.edit_root)
        with self.assertRaisesRegex(EDIT.EditError, "gehört nicht"):
            EDIT.prepare(self.material_id, f"{SCENE}_REAR_001.WAV", library_root=self.library, edit_root=self.edit_root)
        with self.assertRaisesRegex(EDIT.EditError, "innerhalb"):
            EDIT.prepare(self.material_id, MASTER, library_root=self.library, edit_root=self.library / "edits")
        self.assertFalse((self.library / "edits").exists())

    def test_cli_exercises_prepare_and_finish(self):
        import subprocess

        prepare = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "audio-material-edit"),
                "prepare", self.material_id, MASTER,
                "--library-root", str(self.library),
                "--edit-root", str(self.edit_root),
            ],
            check=True, capture_output=True, text=True,
        )
        prepared = json.loads(prepare.stdout)
        render_wave(Path(prepared["expected_render"]))
        finish = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "audio-material-edit"),
                "finish", prepared["edit_id"],
                "--library-root", str(self.library),
                "--edit-root", str(self.edit_root),
            ],
            check=True, capture_output=True, text=True,
        )
        result = json.loads(finish.stdout)
        self.assertEqual(result["kind"], "audio_edit_render_archived")
        self.assertTrue(Path(result["audio"]).exists())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_parent_traversal_root_rejected_before_creating_any_directory(self):
        traversal = self.root / "unused" / ".." / "library" / "edits"
        with self.assertRaisesRegex(EDIT.EditError, "Parent-Traversal"):
            EDIT.prepare(self.material_id, MASTER, library_root=self.library, edit_root=traversal)
        self.assertFalse((self.root / "unused").exists())
        self.assertFalse((self.library / "edits").exists())

    def test_render_header_and_digest_share_a_single_file_generation(self):
        prepared = self.prepare()
        render = Path(prepared["expected_render"])
        expected_bytes = render_wave(render)
        info, digest, size = EDIT._wave_info(render)
        self.assertEqual(digest, hashlib.sha256(expected_bytes).hexdigest())
        self.assertEqual(size, len(expected_bytes))
        self.assertEqual(info["channels"], 2)
        self.assertEqual(info["sample_rate_hz"], 48000)

    def test_atomic_publication_cannot_replace_racing_target(self):
        from unittest import mock

        prepared = self.prepare()
        render_wave(Path(prepared["expected_render"]))
        original_rename = EDIT._rename_noreplace

        # An EMPTY preexisting directory is the dangerous Linux rename(2)
        # case: the old check-then-rename implementation could replace it.
        def race(source_fd, source, target_fd, target):
            os.mkdir(target, mode=0o700, dir_fd=target_fd)
            return original_rename(source_fd, source, target_fd, target)

        with mock.patch.object(EDIT, "_rename_noreplace", side_effect=race):
            with self.assertRaisesRegex(EDIT.EditError, "existiert"):
                self.finish(prepared["edit_id"])
        renders = self.edit_root / "renders"
        published = [entry for entry in renders.iterdir() if entry.is_dir() and len(entry.name) == 24]
        self.assertEqual(len(published), 1)
        self.assertEqual(list(published[0].iterdir()), [])
        self.assertFalse((published[0] / "audio.wav").exists())
        staging = list(renders.glob(".audio-edit-staging-*"))
        self.assertEqual(len(staging), 1)
        self.assertTrue((staging[0] / "audio.wav").is_file())
        self.assertTrue((staging[0] / "manifest.json").is_file())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_failed_publication_never_cleans_replaced_staging_directory(self):
        from unittest import mock

        prepared = self.prepare()
        render_wave(Path(prepared["expected_render"]))
        renders = self.edit_root / "renders"
        replaced = []
        moved = []

        def replace_stage_then_fail(source_fd, source, target_fd, target):
            original = renders / source
            saved = renders / "original-stage-preserved"
            original.rename(saved)
            original.mkdir(mode=0o700)
            (original / "audio.wav").write_bytes(b"user-created-audio")
            (original / "manifest.json").write_bytes(b"user-created-manifest")
            (original / "sentinel").write_bytes(b"must-preserve")
            replaced.append(original)
            moved.append(saved)
            raise EDIT.EditError("injected publication failure")

        with mock.patch.object(EDIT, "_rename_noreplace", side_effect=replace_stage_then_fail):
            with self.assertRaisesRegex(EDIT.EditError, "injected publication failure"):
                self.finish(prepared["edit_id"])

        self.assertEqual(len(replaced), 1)
        self.assertEqual((replaced[0] / "audio.wav").read_bytes(), b"user-created-audio")
        self.assertEqual((replaced[0] / "manifest.json").read_bytes(), b"user-created-manifest")
        self.assertEqual((replaced[0] / "sentinel").read_bytes(), b"must-preserve")
        self.assertTrue((moved[0] / "audio.wav").is_file())
        self.assertTrue((moved[0] / "manifest.json").is_file())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_staging_swap_before_first_open_preserves_foreign_files(self):
        from unittest import mock

        prepared = self.prepare()
        render_wave(Path(prepared["expected_render"]))
        renders = self.edit_root / "renders"
        actual_child = EDIT._child_dir
        replacements = []

        def swapped_before_open(root_fd, name, **kwargs):
            if name.startswith(".audio-edit-staging-"):
                original = renders / name
                saved = renders / "unopened-original-stage"
                original.rename(saved)
                original.mkdir(mode=0o700)
                (original / "audio.wav").write_bytes(b"external-audio")
                (original / "manifest.json").write_bytes(b"external-manifest")
                (original / "sentinel").write_bytes(b"external-marker")
                replacements.append(original)
            return actual_child(root_fd, name, **kwargs)

        with mock.patch.object(EDIT, "_child_dir", side_effect=swapped_before_open):
            with self.assertRaises(FileExistsError):
                self.finish(prepared["edit_id"])

        self.assertEqual(len(replacements), 1)
        self.assertEqual((replacements[0] / "audio.wav").read_bytes(), b"external-audio")
        self.assertEqual((replacements[0] / "manifest.json").read_bytes(), b"external-manifest")
        self.assertEqual((replacements[0] / "sentinel").read_bytes(), b"external-marker")
        self.assertTrue((renders / "unopened-original-stage").is_dir())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_prepare_cannot_replace_racing_empty_workspace(self):
        from unittest import mock

        original_rename = EDIT._rename_noreplace

        def race(source_fd, source, target_fd, target):
            os.mkdir(target, mode=0o700, dir_fd=target_fd)
            return original_rename(source_fd, source, target_fd, target)

        with mock.patch.object(EDIT, "_rename_noreplace", side_effect=race):
            with self.assertRaisesRegex(EDIT.EditError, "existiert"):
                self.prepare()
        workspace_root = self.edit_root / "working"
        preserved = [entry for entry in workspace_root.iterdir() if entry.is_dir() and len(entry.name) == 24]
        self.assertEqual(len(preserved), 1)
        self.assertEqual(list(preserved[0].iterdir()), [])
        staging = list(workspace_root.glob(".audio-edit-staging-*"))
        self.assertEqual(len(staging), 1)
        self.assertTrue((staging[0] / "input.wav").is_file())
        self.assertTrue((staging[0] / "manifest.json").is_file())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_hardlinked_input_to_immutable_master_is_rejected(self):
        prepared = self.prepare()
        input_path = Path(prepared["working_copy"])
        input_path.unlink()
        os.link(self.original, input_path)
        self.assertEqual(input_path.stat().st_ino, self.original.stat().st_ino)
        self.assertGreater(self.original.stat().st_nlink, 1)

        with self.assertRaisesRegex(EDIT.EditError, "Arbeitskopie"):
            self.prepare()
        render_wave(Path(prepared["expected_render"]))
        with self.assertRaisesRegex(EDIT.EditError, "Arbeitskopie"):
            self.finish(prepared["edit_id"])
        self.assertFalse((self.edit_root / "renders").exists())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_existing_workspace_rechecks_master_after_verify_directory_swap(self):
        from unittest import mock

        prepared = self.prepare()
        master_directory = self.original.parent
        preserved = master_directory.with_name("master-original")
        original_verify = H2.verify_material

        def verified_then_swapped(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            master_directory.rename(preserved)
            master_directory.mkdir(mode=0o700)
            (master_directory / MASTER).write_bytes(b"replaced-after-verify")
            return result

        with mock.patch.object(H2, "verify_material", side_effect=verified_then_swapped):
            with self.assertRaisesRegex(EDIT.EditError, "Master"):
                self.prepare()
        self.assertTrue(Path(prepared["working_copy"]).exists())
        self.assertEqual(hashlib.sha256((preserved / MASTER).read_bytes()).hexdigest(),
                         self.original_digest)
        self.assertFalse((self.edit_root / "renders").exists())

    def test_existing_workspace_rechecks_sibling_master_after_directory_swap(self):
        from unittest import mock

        prepared = self.prepare()
        master_directory = self.original.parent
        saved = master_directory.with_name("master-before-swap")
        original_verify = H2.verify_material

        def verified_then_swapped(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            master_directory.rename(saved)
            master_directory.mkdir(mode=0o700)
            for old_file in saved.iterdir():
                if old_file.is_file():
                    (master_directory / old_file.name).write_bytes(old_file.read_bytes())
            # Only FRONT changes; the selected MIX remains byte-identical.
            (master_directory / f"{SCENE}_FRONT.WAV").write_bytes(b"changed-sibling")
            return result

        with mock.patch.object(H2, "verify_material", side_effect=verified_then_swapped):
            with self.assertRaisesRegex(EDIT.EditError, "Master-Set"):
                self.prepare()
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)
        self.assertTrue(Path(prepared["working_copy"]).exists())
        self.assertFalse((self.edit_root / "renders").exists())

    def test_finish_rejects_missing_sibling_master_after_verify(self):
        from unittest import mock

        prepared = self.prepare()
        render_wave(Path(prepared["expected_render"]))
        original_verify = H2.verify_material
        sibling = self.original.parent / f"{SCENE}_FRONT.WAV"

        def verified_then_missing(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            os.chmod(sibling.parent, 0o700)  # simulate a same-owner directory mutation
            sibling.unlink()
            return result

        with mock.patch.object(H2, "verify_material", side_effect=verified_then_missing):
            with self.assertRaisesRegex(EDIT.EditError, "Master-Set"):
                self.finish(prepared["edit_id"])
        self.assertFalse((self.edit_root / "renders").exists())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_swapped_renders_parent_cannot_redirect_publication(self):
        from unittest import mock

        prepared = self.prepare()
        render_wave(Path(prepared["expected_render"]))
        outside = self.root / "outside"
        outside.mkdir()
        original_rename = EDIT._rename_noreplace

        def redirect_after_fd_was_opened(source_fd, source, target_fd, target):
            self.assertTrue((self.edit_root / "renders").is_dir())
            (self.edit_root / "renders").rename(self.edit_root / "renders-moved")
            (self.edit_root / "renders").symlink_to(outside, target_is_directory=True)
            return original_rename(source_fd, source, target_fd, target)

        with mock.patch.object(EDIT, "_rename_noreplace", side_effect=redirect_after_fd_was_opened):
            with self.assertRaises(EDIT.EditError):
                self.finish(prepared["edit_id"])
        self.assertEqual(list(outside.iterdir()), [])
        detached = list((self.edit_root / "renders-moved").iterdir())
        self.assertEqual(len(detached), 1)
        self.assertEqual((detached[0] / "audio.wav").is_file(), True)
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_swapped_working_parent_cannot_redirect_existing_input(self):
        from unittest import mock

        prepared = self.prepare()
        outside = self.root / "outside"
        outside.mkdir()
        match_original = EDIT._matches_at

        def replace_checked_parent(fd, filename, size, sha, **kwargs):
            if filename == "input.wav":
                (self.edit_root / "working").rename(self.edit_root / "working-moved")
                (self.edit_root / "working").symlink_to(outside, target_is_directory=True)
            return match_original(fd, filename, size, sha, **kwargs)

        with mock.patch.object(EDIT, "_matches_at", side_effect=replace_checked_parent):
            with self.assertRaises(EDIT.EditError):
                self.prepare()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.edit_root / "working-moved" / prepared["edit_id"] / "input.wav").exists())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_fifo_render_rejects_without_blocking(self):
        import subprocess

        prepared = self.prepare()
        fifo = Path(prepared["expected_render"])
        os.mkfifo(fifo, mode=0o600)
        result = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "audio-material-edit"),
                "finish", prepared["edit_id"],
                "--library-root", str(self.library),
                "--edit-root", str(self.edit_root),
            ],
            capture_output=True, text=True, timeout=5, check=False,
        )
        self.assertEqual(result.returncode, 2)
        blocked = json.loads(result.stderr)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("reguläre Datei", blocked["error"])
        self.assertFalse((self.edit_root / "renders").exists())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_zero_byte_write_fails_without_publishing(self):
        from unittest import mock

        with mock.patch.object(EDIT.os, "write", return_value=0):
            with self.assertRaisesRegex(EDIT.EditError, "vollständig geschrieben"):
                self.prepare()
        working = self.edit_root / "working"
        staging = list(working.glob(".audio-edit-staging-*"))
        self.assertEqual(len(staging), 1)
        self.assertEqual((staging[0] / "input.wav").stat().st_size, 0)
        self.assertFalse((staging[0] / "manifest.json").exists())
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), self.original_digest)

    def test_render_format_is_bounded(self):
        prepared = self.prepare()
        render = Path(prepared["expected_render"])
        data = bytearray(render_wave(render))
        data[22:24] = b"\x08\x00"  # eight channels; violates mono/stereo gate
        render.write_bytes(data)
        with self.assertRaisesRegex(EDIT.EditError, "Kanäle"):
            self.finish(prepared["edit_id"])