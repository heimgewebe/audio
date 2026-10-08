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

    def test_render_format_is_bounded(self):
        prepared = self.prepare()
        render = Path(prepared["expected_render"])
        data = bytearray(render_wave(render))
        data[22:24] = b"\x08\x00"  # eight channels; violates mono/stereo gate
        render.write_bytes(data)
        with self.assertRaisesRegex(EDIT.EditError, "Kanäle"):
            self.finish(prepared["edit_id"])
        self.assertFalse((self.edit_root / "renders").exists())


if __name__ == "__main__":
    unittest.main()