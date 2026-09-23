import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import stat
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "h2_ingest_test_target", ROOT / "scripts" / "h2_ingest.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _fixed(value: str, length: int) -> bytes:
    encoded = value.encode("ascii")
    if len(encoded) > length:
        raise ValueError(value)
    return encoded + b"\0" * (length - len(encoded))


def _chunk(chunk_id: bytes, payload: bytes) -> bytes:
    pad = b"\0" if len(payload) & 1 else b""
    return chunk_id + struct.pack("<I", len(payload)) + payload + pad


def write_h2_wav(
    path: pathlib.Path,
    *,
    scene: str,
    role: str,
    frames: int = 441,
    rate: int = 44_100,
    marker: bool = False,
    described_scene: str | None = None,
    recorded_date: str = "2026-09-17",
    recorded_time: str = "19:14:01",
) -> bytes:
    track = {"FRONT": "1", "REAR": "3", "MIX": "5"}[role]
    description = (
        f"zTAKE=001\r\nzSCENE={described_scene or scene}\r\nzTAPE=\r\n"
        "zCIRCLED=FALSE\r\n"
    )
    coding = (
        f"A=PCM,F={rate},W=32,M=stereo,T=H2essential;"
        f"VERSION=1.10;TRK={track};FDR=   0;"
    )
    bext = b"".join(
        [
            _fixed(description, 256),
            _fixed("ZOOM H2essential", 32),
            _fixed("", 32),
            _fixed(recorded_date, 10),
            _fixed(recorded_time, 8),
            struct.pack("<Q", 123456),
            struct.pack("<H", 1),
            b"\0" * 64,
            b"\0" * 190,
            coding.encode("ascii"),
        ]
    )
    fmt = struct.pack("<HHIIHH", 3, 2, rate, rate * 8, 8, 32)
    audio = b"".join(
        struct.pack("<ff", index / max(frames, 1), -index / max(frames, 1))
        for index in range(frames)
    )
    chunks = [_chunk(b"bext", bext), _chunk(b"fmt ", fmt)]
    if marker:
        chunks.append(_chunk(b"cue ", struct.pack("<I", 0)))
    chunks.append(_chunk(b"data", audio))
    body = b"WAVE" + b"".join(chunks)
    payload = b"RIFF" + struct.pack("<I", len(body)) + body
    path.write_bytes(payload)
    return payload


def make_source(
    root: pathlib.Path,
    *,
    scene: str = "170926_191401",
    roles: tuple[str, ...] = ("FRONT", "REAR", "MIX"),
    marker_role: str | None = None,
) -> pathlib.Path:
    source = root / "ZOOM_H2E"
    source.mkdir()
    (source / MODULE.SOURCE_SENTINEL).write_text("synthetic", encoding="utf-8")
    session = source / scene
    session.mkdir()
    for role in roles:
        write_h2_wav(
            session / f"{scene}_{role}.WAV",
            scene=scene,
            role=role,
            marker=role == marker_role,
        )
    return source


class H2IngestTests(unittest.TestCase):
    def test_library_root_prefers_primary_and_falls_back_only_to_legacy_material(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            primary = base / "Audio-Aufnahmen" / "H2-Material"
            legacy = base / "Audio-Material" / "H2"

            self.assertEqual(MODULE._select_library_root(primary, legacy), primary)
            legacy.mkdir(parents=True)
            self.assertEqual(MODULE._select_library_root(primary, legacy), primary)
            primary.mkdir(parents=True)
            self.assertEqual(MODULE._select_library_root(primary, legacy), primary)

            legacy_material = legacy / ("a" * 24)
            legacy_material.mkdir()
            self.assertEqual(MODULE._select_library_root(primary, legacy), legacy)

            primary_material = primary / ("b" * 24)
            primary_material.mkdir()
            with self.assertRaisesRegex(RuntimeError, "Primär- und Legacy-Root"):
                MODULE._select_library_root(primary, legacy)

    def test_material_root_override_keeps_preceding_parent_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            configured = pathlib.Path(directory) / "Material"
            self.assertEqual(
                MODULE._default_library_root(str(configured)),
                configured / "H2",
            )

    def test_scan_groups_one_session_and_preserves_h2_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            source = make_source(
                pathlib.Path(directory),
                marker_role="FRONT",
            )
            report = MODULE.scan(source)
        self.assertTrue(report["read_only"])
        self.assertFalse(report["source_mutated"])
        self.assertEqual(report["count"], 1)
        session = report["sessions"][0]
        self.assertEqual(session["scene"], "170926_191401")
        self.assertEqual(session["take"], "001")
        self.assertEqual(session["recorded_date"], "2026-09-17")
        self.assertEqual(session["recorded_time"], "19:14:01")
        self.assertEqual(session["roles"], ["front", "rear", "mix"])
        self.assertEqual(session["sample_rate_hz"], 44_100)
        self.assertEqual(session["duration_seconds"], 0.01)
        self.assertEqual(session["marker_chunks_observed"], ["cue "])
        for item in session["files"]:
            self.assertEqual(item["audio"]["codec"], "pcm_f32le")
            self.assertEqual(item["audio"]["bits_per_sample"], 32)
            self.assertEqual(item["audio"]["channels"], 2)
            self.assertEqual(item["bwf"]["originator"], "ZOOM H2essential")
            self.assertNotIn("sha256", item)

    def test_scan_reports_invalid_matching_session_instead_of_claiming_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root)
            bad_scene = "170926_191951"
            bad = source / bad_scene
            bad.mkdir()
            write_h2_wav(
                bad / f"{bad_scene}_FRONT.WAV",
                scene=bad_scene,
                described_scene="170926_000000",
                role="FRONT",
            )
            report = MODULE.scan(source)
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["skipped_invalid_sessions"], [bad_scene])

    def test_symlink_track_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            scene = "170926_191401"
            original = source / scene / f"{scene}_FRONT.WAV"
            real = source / scene / "real.wav"
            original.rename(real)
            original.symlink_to(real.name)
            with self.assertRaisesRegex(MODULE.H2IngestError, "Nicht-Datei"):
                MODULE.inspect_scene(source, scene)

    def test_import_is_byte_exact_private_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root)
            library = root / "library"
            scene = "170926_191401"
            source_bytes = {
                path.name: path.read_bytes() for path in (source / scene).glob("*.WAV")
            }
            result = MODULE.import_scene(
                scene,
                source_root=source,
                library_root=library,
            )
            self.assertEqual(result["status"], "imported")
            self.assertFalse(result["source_mutated"])
            material_id = result["material_id"]
            target = library / material_id
            manifest = json.loads((target / "manifest.json").read_text())
            annotations = json.loads((target / "annotations.json").read_text())
            self.assertEqual(manifest["material_id"], material_id)
            self.assertEqual(manifest["integrity"]["master_mutation"], "forbidden")
            self.assertEqual(annotations["material_id"], material_id)
            self.assertEqual(annotations["title"], "")
            self.assertNotIn(str(source), json.dumps(manifest))
            for item in manifest["masters"]:
                archived = target / "master" / item["name"]
                self.assertEqual(archived.read_bytes(), source_bytes[item["name"]])
                self.assertEqual(
                    item["sha256"], hashlib.sha256(source_bytes[item["name"]]).hexdigest()
                )
                self.assertEqual(stat.S_IMODE(archived.stat().st_mode), 0o440)
            self.assertEqual(stat.S_IMODE((target / "manifest.json").stat().st_mode), 0o440)
            self.assertEqual(stat.S_IMODE((target / "annotations.json").stat().st_mode), 0o600)
            repeated = MODULE.import_scene(
                scene,
                source_root=source,
                library_root=library,
            )
            self.assertEqual(repeated["status"], "already-imported")
            self.assertEqual(repeated["material_id"], material_id)
            for path in (source / scene).glob("*.WAV"):
                self.assertEqual(path.read_bytes(), source_bytes[path.name])

    def test_import_rejects_source_replacement_between_scan_and_generation_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            scene = "170926_191401"
            original_inspect_scene = MODULE.inspect_scene
            replaced = False

            def inspect_then_replace(source_root, requested_scene):
                nonlocal replaced
                report = original_inspect_scene(source_root, requested_scene)
                if not replaced:
                    replaced = True
                    write_h2_wav(
                        source / scene / f"{scene}_FRONT.WAV",
                        scene=scene,
                        role="FRONT",
                        frames=220,
                    )
                return report

            with mock.patch.object(
                MODULE,
                "inspect_scene",
                side_effect=inspect_then_replace,
            ):
                with self.assertRaisesRegex(
                    MODULE.H2IngestError,
                    "Sessionprüfung und Vorhash",
                ):
                    MODULE.import_scene(
                        scene,
                        source_root=source,
                        library_root=library,
                    )
            self.assertFalse(library.exists() and any(library.iterdir()))

    def test_repeat_import_preflights_hashes_without_copying_to_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            first = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            with mock.patch.object(
                MODULE,
                "_copy_master",
                side_effect=AssertionError("repeat import must not copy"),
            ):
                repeated = MODULE.import_scene(
                    "170926_191401",
                    source_root=source,
                    library_root=library,
                )
            self.assertEqual(repeated["status"], "already-imported")
            self.assertEqual(repeated["material_id"], first["material_id"])
            self.assertFalse(any(path.name.startswith(".h2-staging-") for path in library.iterdir()))

    def test_split_h2_tracks_are_one_contiguous_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT", "REAR"))
            scene = "170926_191401"
            for role in ("FRONT", "REAR"):
                write_h2_wav(
                    source / scene / f"{scene}_{role}_001.WAV",
                    scene=scene,
                    role=role,
                    frames=220,
                )
            report = MODULE.inspect_scene(source, scene)

        self.assertEqual(report["roles"], ["front", "rear"])
        self.assertEqual(report["segment_count"], 2)
        self.assertEqual(
            [(item["role"], item["segment_index"]) for item in report["files"]],
            [("front", 0), ("front", 1), ("rear", 0), ("rear", 1)],
        )
        self.assertAlmostEqual(report["duration_seconds"], 661 / 44_100, places=9)

    def test_split_h2_segments_may_advance_bwf_clock_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT", "REAR"))
            scene = "170926_191401"
            for role in ("FRONT", "REAR"):
                write_h2_wav(
                    source / scene / f"{scene}_{role}_001.WAV",
                    scene=scene,
                    role=role,
                    frames=220,
                    recorded_date="2026-09-18",
                    recorded_time="00:02:03",
                )
            report = MODULE.inspect_scene(source, scene)

        self.assertEqual(report["recorded_date"], "2026-09-17")
        self.assertEqual(report["recorded_time"], "19:14:01")
        self.assertEqual(report["segment_count"], 2)

    def test_explicit_zero_split_suffix_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            scene = "170926_191401"
            write_h2_wav(
                source / scene / f"{scene}_FRONT_000.WAV",
                scene=scene,
                role="FRONT",
            )
            with self.assertRaisesRegex(MODULE.H2IngestError, "Segmentnummer"):
                MODULE.inspect_scene(source, scene)

    def test_split_h2_tracks_require_matching_segment_sequences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT", "REAR"))
            scene = "170926_191401"
            write_h2_wav(
                source / scene / f"{scene}_FRONT_001.WAV",
                scene=scene,
                role="FRONT",
                frames=220,
            )
            with self.assertRaisesRegex(MODULE.H2IngestError, "Spursegmenten"):
                MODULE.inspect_scene(source, scene)

    def test_cli_wrapper_resolves_module_when_invoked_through_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            link = pathlib.Path(directory) / "audio-h2-ingest"
            link.symlink_to(ROOT / "scripts" / "audio-h2-ingest")
            completed = subprocess.run(
                [str(link), "--help"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Safe, immutable file ingest", completed.stdout)

    def test_verify_rehashes_current_master_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            verified = MODULE.verify_material(
                result["material_id"], library_root=library
            )
            self.assertTrue(verified["verified_current"])
            archived = next((library / result["material_id"] / "master").glob("*.WAV"))
            archived.chmod(0o640)
            with archived.open("ab") as handle:
                handle.write(b"x")
            archived.chmod(0o440)
            with self.assertRaisesRegex(MODULE.H2IngestError, "weicht"):
                MODULE.verify_material(result["material_id"], library_root=library)
            with self.assertRaisesRegex(MODULE.H2IngestError, "weicht"):
                MODULE.import_scene(
                    "170926_191401",
                    source_root=source,
                    library_root=library,
                )

    def test_verify_rejects_coordinated_master_and_manifest_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            target = library / result["material_id"]
            archived = next((target / "master").glob("*.WAV"))
            archived.chmod(0o640)
            with archived.open("ab") as handle:
                handle.write(b"tampered")
            archived.chmod(0o440)

            manifest_path = target / "manifest.json"
            manifest_path.chmod(0o640)
            manifest = json.loads(manifest_path.read_text())
            master = manifest["masters"][0]
            payload = archived.read_bytes()
            master["bytes"] = len(payload)
            master["sha256"] = hashlib.sha256(payload).hexdigest()
            identity = [
                {
                    "name": master["name"],
                    "role": master["role"],
                    "sha256": master["sha256"],
                    "bytes": master["bytes"],
                }
            ]
            manifest["master_set_sha256"] = hashlib.sha256(
                MODULE._canonical_bytes(identity)
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            manifest_path.chmod(0o440)

            with self.assertRaisesRegex(MODULE.H2IngestError, "Material-ID"):
                MODULE.verify_material(result["material_id"], library_root=library)

    def test_verify_rejects_empty_master_set_even_with_matching_material_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            empty_set_sha256 = hashlib.sha256(MODULE._canonical_bytes([])).hexdigest()
            empty_material_id = hashlib.sha256(
                b"zoom-h2essential-import-v1\0" + bytes.fromhex(empty_set_sha256)
            ).hexdigest()[:24]
            target = library / result["material_id"]
            empty_target = library / empty_material_id
            target.rename(empty_target)

            manifest_path = empty_target / "manifest.json"
            manifest_path.chmod(0o640)
            manifest = json.loads(manifest_path.read_text())
            manifest["material_id"] = empty_material_id
            manifest["master_set_sha256"] = empty_set_sha256
            manifest["masters"] = []
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            manifest_path.chmod(0o440)

            annotations_path = empty_target / "annotations.json"
            annotations = json.loads(annotations_path.read_text())
            annotations["material_id"] = empty_material_id
            annotations_path.write_text(
                json.dumps(annotations, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(MODULE.H2IngestError, "keine gültigen Master"):
                MODULE.verify_material(empty_material_id, library_root=library)

    def test_library_rejects_incomplete_manifest_as_ingest_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            manifest_path = library / result["material_id"] / "manifest.json"
            manifest_path.chmod(0o640)
            manifest = json.loads(manifest_path.read_text())
            del manifest["source"]
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            manifest_path.chmod(0o440)

            with self.assertRaisesRegex(MODULE.H2IngestError, "strukturell"):
                MODULE.library(library)

    def test_library_is_shallow_and_does_not_claim_current_byte_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            report = MODULE.library(library)
        self.assertEqual(report["count"], 1)
        item = report["items"][0]
        self.assertEqual(item["material_id"], result["material_id"])
        self.assertFalse(item["current_bytes_verified"])
        self.assertEqual(item["source"]["kind"], "zoom-h2essential-file-transfer")

    def test_library_may_not_be_created_on_the_h2_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = make_source(pathlib.Path(directory), roles=("FRONT",))
            with self.assertRaisesRegex(MODULE.H2IngestError, "niemals auf der H2"):
                MODULE.import_scene(
                    "170926_191401",
                    source_root=source,
                    library_root=source / "ARCHIVE",
                )

    def test_scene_files_must_have_matching_frame_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT", "REAR"))
            scene = "170926_191401"
            write_h2_wav(
                source / scene / f"{scene}_REAR.WAV",
                scene=scene,
                role="REAR",
                frames=220,
            )
            with self.assertRaisesRegex(MODULE.H2IngestError, "nicht konsistent"):
                MODULE.inspect_scene(source, scene)


    def test_annotations_are_atomic_metadata_only_and_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401", source_root=source, library_root=library
            )
            target = library / result["material_id"]
            master = next((target / "master").glob("*.WAV"))
            before_master = hashlib.sha256(master.read_bytes()).hexdigest()
            before_manifest = hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest()
            updated = MODULE.annotate_material(
                result["material_id"],
                title="  Metallgeländer unter Brücke  ",
                note="  kurzer Impuls  ",
                tags=["Metall", "perkussiv", "metall"],
                library_root=library,
            )
            self.assertTrue(updated["changed"])
            self.assertEqual(updated["annotations"]["title"], "Metallgeländer unter Brücke")
            self.assertEqual(updated["annotations"]["note"], "kurzer Impuls")
            self.assertEqual(updated["annotations"]["tags"], ["Metall", "perkussiv"])
            self.assertIsNotNone(updated["annotations"]["updated_at"])
            self.assertEqual(
                hashlib.sha256(master.read_bytes()).hexdigest(), before_master
            )
            self.assertEqual(
                hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest(),
                before_manifest,
            )
            projected = MODULE.library(library)
            self.assertEqual(
                projected["items"][0]["annotations"]["title"],
                "Metallgeländer unter Brücke",
            )

    def test_annotation_replace_does_not_double_close_transferred_fd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "annotations.json"
            path.write_text("{}\n", encoding="utf-8")
            with (
                mock.patch.object(MODULE.os, "replace", side_effect=OSError("replace failed")),
                mock.patch.object(MODULE.os, "close", wraps=os.close) as close_mock,
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    MODULE._write_json_replace(path, {"value": 1}, 0o600)
            close_mock.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "{}\n")
            self.assertFalse(any(root.glob(".metadata-*")))

    def test_annotations_reject_controls_and_excess_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401", source_root=source, library_root=library
            )
            with self.assertRaisesRegex(MODULE.H2IngestError, "Steuerzeichen"):
                MODULE.annotate_material(
                    result["material_id"],
                    title="bad\x01title",
                    note="",
                    tags=[],
                    library_root=library,
                )
            with self.assertRaisesRegex(MODULE.H2IngestError, "Tags"):
                MODULE.annotate_material(
                    result["material_id"],
                    title="ok",
                    note="",
                    tags=[f"tag-{index}" for index in range(MODULE.MAX_TAGS + 1)],
                    library_root=library,
                )

    def test_source_media_prefers_mix_and_is_generation_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root)
            binding = MODULE.source_media(
                "170926_191401", 0, source_root=source
            )
            self.assertEqual(binding["role"], "mix")
            self.assertEqual(binding["segment_index"], 0)
            self.assertEqual(binding["segment_count"], 1)
            path = pathlib.Path(binding["path"])
            self.assertEqual(binding["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            metadata = path.stat()
            self.assertEqual(binding["device"], metadata.st_dev)
            self.assertEqual(binding["inode"], metadata.st_ino)
            self.assertTrue(binding["verified_current"])

    def test_material_media_survives_source_removal_and_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root)
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401", source_root=source, library_root=library
            )
            shutil.rmtree(source)
            binding = MODULE.material_media(
                result["material_id"], 0, library_root=library
            )
            self.assertEqual(binding["role"], "mix")
            self.assertTrue(binding["verified_current"])
            path = pathlib.Path(binding["path"])
            self.assertTrue(path.is_file())
            self.assertEqual(binding["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
