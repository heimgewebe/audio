from concurrent.futures import ThreadPoolExecutor

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

    def test_wav_rejects_chunk_id_accumulation_during_iteration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            scene = "170926_191401"
            path = root / f"{scene}_FRONT.WAV"
            original = write_h2_wav(path, scene=scene, role="FRONT")
            data_offset = original.find(b"data")
            self.assertGreater(data_offset, 12)
            ancillary = _chunk(b"JUNK", b"") * 32
            expanded = (
                b"RIFF"
                + struct.pack("<I", len(original) - 8 + len(ancillary))
                + original[8:data_offset]
                + ancillary
                + original[data_offset:]
            )
            path.write_bytes(expanded)

            with mock.patch.object(MODULE, "MAX_WAVE_CHUNK_IDS_JSON_BYTES", 48):
                with self.assertRaisesRegex(
                    MODULE.H2IngestError,
                    "Chunk-Metadatenbudget",
                ):
                    MODULE.inspect_wav(path, scene, "FRONT")

    def test_scene_rejects_cumulative_manifest_metadata_before_full_serialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            scene = "170926_191401"
            session = root / scene
            session.mkdir()
            names = [
                f"{scene}_FRONT.WAV",
                *[
                    f"{scene}_FRONT_{index:03d}.WAV"
                    for index in range(1, 8)
                ],
            ]
            for name in names:
                (session / name).write_bytes(b"x")

            control_payload = "\x01" * (MODULE.MAX_BEXT_BYTES - 604)
            coding_history = "A=" + control_payload

            def inspected(path, expected_scene, role):
                self.assertEqual(expected_scene, scene)
                return {
                    "name": path.name,
                    "role": role.lower(),
                    "bytes": 1,
                    "audio": {
                        "codec": "pcm_f32le",
                        "sample_rate_hz": 44_100,
                        "channels": 2,
                        "bits_per_sample": 32,
                        "frames": 441,
                        "duration_seconds": 0.01,
                    },
                    "bwf": {
                        "description": f"zTAKE=001\\r\\nzSCENE={scene}\\r\\n",
                        "description_fields": {
                            "zTAKE": "001",
                            "zSCENE": scene,
                        },
                        "originator": MODULE.SOURCE_ORIGINATOR,
                        "originator_reference": "",
                        "recorded_date": "2026-09-17",
                        "recorded_time": "19:14:01",
                        "time_reference_samples": 123456,
                        "version": 1,
                        "coding_history": coding_history,
                        "coding_fields": {"A": control_payload},
                    },
                    "chunk_ids": ["bext", "fmt ", "data"],
                    "marker_chunks_observed": [],
                }

            with mock.patch.object(
                MODULE,
                "inspect_wav",
                side_effect=inspected,
            ) as inspector:
                with self.assertRaisesRegex(
                    MODULE.H2IngestError,
                    "sichere Metadatenbudget",
                ):
                    MODULE.inspect_scene(root, scene)

            self.assertLess(inspector.call_count, len(names))
            self.assertLessEqual(inspector.call_count, 2)

    def test_manifest_master_metadata_budget_leaves_bounded_envelope_headroom(self):
        self.assertEqual(
            MODULE.MAX_MANIFEST_MASTER_METADATA_BYTES
            + MODULE.MANIFEST_METADATA_ENVELOPE_RESERVE_BYTES,
            MODULE.MAX_METADATA_JSON_BYTES,
        )
        self.assertGreaterEqual(
            MODULE.MANIFEST_METADATA_ENVELOPE_RESERVE_BYTES,
            64 * 1024,
        )

    def test_control_scan_projection_keeps_only_controller_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            source = make_source(pathlib.Path(directory))
            report = MODULE.scan(source, projection="control")
        self.assertEqual(report["projection"], MODULE.CONTROL_SCAN_PROJECTION)
        self.assertEqual(report["count"], 1)
        session = report["sessions"][0]
        self.assertNotIn("files", session)
        self.assertEqual(session["total_bytes"], session["max_file_bytes"] * 3)
        self.assertGreater(session["max_file_bytes"], 0)
        self.assertEqual(session["roles"], ["front", "rear", "mix"])

    def test_control_scan_budget_is_shallow_and_size_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            source = make_source(pathlib.Path(directory))
            report = MODULE.scan(source, projection="budget")
            expected_bytes = (
                report["candidate_file_count"]
                * MODULE.CONTROL_SCAN_METADATA_BUDGET_BYTES_PER_FILE
            )
        self.assertEqual(report["kind"], "audio_h2_source_scan_budget")
        self.assertEqual(report["projection"], MODULE.CONTROL_SCAN_BUDGET_PROJECTION)
        self.assertEqual(report["matching_session_count"], 1)
        self.assertEqual(report["candidate_file_count"], 3)
        self.assertEqual(report["total_candidate_bytes"], expected_bytes)
        self.assertNotIn("sessions", report)
        self.assertTrue(report["read_only"])
        self.assertFalse(report["source_mutated"])

    def test_control_scan_discards_large_bwf_payload_and_fits_runner_cap(self):
        huge_bwf = "x" * MODULE.MAX_BEXT_BYTES
        full_session = {
            "scene": "170926_191401",
            "recorded_date": "2026-09-17",
            "recorded_time": "19:14:01",
            "sample_rate_hz": 96_000,
            "duration_seconds": 999_999_999.999999,
            "roles": ["front", "rear", "mix"],
            "segment_count": 3,
            "files": [
                {
                    "role": ("front", "rear", "mix")[index % 3],
                    "segment_index": index // 3,
                    "bytes": 4_294_967_295,
                    "bwf": {"coding_history": huge_bwf},
                }
                for index in range(9)
            ],
        }
        self.assertGreater(
            len(json.dumps(full_session, ensure_ascii=False).encode("utf-8")),
            1_048_576,
        )
        compact = MODULE._control_scan_session(full_session)
        self.assertNotIn("files", compact)
        self.assertEqual(compact["total_bytes"], 9 * 4_294_967_295)
        self.assertEqual(compact["max_file_bytes"], 4_294_967_295)

        worst_case = {
            "schema_version": MODULE.SCHEMA_VERSION,
            "kind": "audio_h2_source_scan",
            "projection": MODULE.CONTROL_SCAN_PROJECTION,
            "device": {
                "model": MODULE.SOURCE_ORIGINATOR,
                "transport": "file-transfer",
                "volume_hint": "x" * 255,
            },
            "sessions": [
                {
                    **compact,
                    "scene": f"{index:06d}_{index:06d}",
                }
                for index in range(MODULE.MAX_CONTROL_SCAN_SESSIONS)
            ],
            "count": MODULE.MAX_CONTROL_SCAN_SESSIONS,
            "skipped_invalid_sessions": [],
            "read_only": True,
            "source_mutated": False,
        }
        encoded = json.dumps(
            worst_case,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        self.assertLess(len(encoded), 1_048_576)

    def test_control_scan_bounds_matching_scene_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            source = make_source(pathlib.Path(directory))
            (source / "170926_191402").mkdir()
            for projection in ("control", "budget"):
                with (
                    self.subTest(projection=projection),
                    mock.patch.object(MODULE, "MAX_CONTROL_SCAN_SESSIONS", 1),
                    self.assertRaisesRegex(MODULE.H2IngestError, "Session-Limit"),
                ):
                    MODULE.scan(source, projection=projection)

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
            real = root / "real.wav"
            original.rename(real)
            original.symlink_to(real)
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
            entries = list(library.iterdir()) if library.exists() else []
            self.assertEqual(
                [path.name for path in entries],
                [".h2-import.lock"],
            )
            self.assertTrue(entries[0].is_file())
            self.assertEqual(stat.S_IMODE(entries[0].stat().st_mode), 0o600)

    def test_legacy_oversized_manifest_is_migrated_to_bound_control_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            material = library / result["material_id"]
            manifest_path = material / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["legacy_padding"] = "x" * (MODULE.MAX_METADATA_JSON_BYTES + 4096)
            oversized = MODULE._canonical_bytes(manifest) + b"\n"
            self.assertGreater(len(oversized), MODULE.MAX_METADATA_JSON_BYTES)
            os.chmod(manifest_path, 0o640)
            manifest_path.write_bytes(oversized)
            os.chmod(manifest_path, 0o440)
            before = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

            with self.assertRaisesRegex(
                MODULE.H2IngestError,
                "Control-Sidecar|sicher lesbar|nicht lesbar",
            ):
                MODULE.library(library, projection="control")

            migration = MODULE.migrate_legacy_manifests(library)
            self.assertEqual(migration["migrated"], 1)
            self.assertIs(migration["read_only_originals"], True)
            control_path = material / MODULE.LEGACY_MANIFEST_CONTROL_NAME
            self.assertTrue(control_path.is_file())
            self.assertEqual(stat.S_IMODE(control_path.stat().st_mode), 0o440)
            self.assertEqual(
                hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                before,
            )

            projected = MODULE.library(library, projection="control")
            self.assertEqual(projected["count"], 1)
            self.assertEqual(projected["items"][0]["material_id"], result["material_id"])
            verified = MODULE.verify_material(
                result["material_id"],
                library_root=library,
            )
            self.assertTrue(verified["verified_current"])
            annotated = MODULE.annotate_material(
                result["material_id"],
                title="Legacy",
                note="weiter lesbar",
                tags=["migration"],
                library_root=library,
            )
            self.assertTrue(annotated["changed"])

            second = MODULE.migrate_legacy_manifests(library)
            self.assertEqual(second["migrated"], 0)
            self.assertEqual(second["already_bound"], 1)

    def test_legacy_manifest_migration_streams_dense_audio_metadata_without_full_json_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            material = library / result["material_id"]
            manifest_path = material / "manifest.json"
            original = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_audio = original["masters"][0]["audio"]
            expected_duration = expected_audio["duration_seconds"]
            raw = manifest_path.read_text(encoding="utf-8").rstrip()
            dense = ",".join(["{}"] * 6000)
            payload = raw.replace(
                '"audio":{',
                '"audio":{"legacy_padding":[' + dense + '],',
                1,
            ) + "\n"
            self.assertGreater(len(payload), 8192)
            os.chmod(manifest_path, 0o640)
            manifest_path.write_text(payload, encoding="utf-8")
            os.chmod(manifest_path, 0o440)
            before = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            real_loads = json.loads

            def bounded_loads(value, *args, **kwargs):
                self.assertLessEqual(len(value), 8192)
                return real_loads(value, *args, **kwargs)

            with (
                mock.patch.object(MODULE, "MAX_METADATA_JSON_BYTES", 8192),
                mock.patch.object(MODULE.json, "loads", side_effect=bounded_loads),
            ):
                migration = MODULE.migrate_legacy_manifests(library)

            self.assertEqual(migration["manifest_migrated"], 1)
            self.assertEqual(
                hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                before,
            )
            control = MODULE._read_json_regular(
                material / MODULE.LEGACY_MANIFEST_CONTROL_NAME
            )
            self.assertEqual(control["masters"][0]["audio"], expected_audio)
            media = MODULE.material_media(
                result["material_id"],
                0,
                library_root=library,
            )
            self.assertEqual(media["duration_seconds"], expected_duration)

    def test_legacy_manifest_stream_projection_rejects_invalid_skipped_json(self):
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
            raw = manifest_path.read_text(encoding="utf-8").rstrip()
            payload = raw.replace(
                '"audio":{',
                '"audio":{"legacy_padding":[{},],',
                1,
            ) + "\n"
            os.chmod(manifest_path, 0o640)
            manifest_path.write_text(payload, encoding="utf-8")
            os.chmod(manifest_path, 0o440)
            with (
                mock.patch.object(MODULE, "MAX_METADATA_JSON_BYTES", 512),
                self.assertRaisesRegex(MODULE.H2IngestError, "ungültiges JSON"),
            ):
                MODULE._read_legacy_manifest_projection(manifest_path)

    def test_legacy_manifest_stream_projection_preserves_last_duplicate_key(self):
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
            raw = manifest_path.read_text(encoding="utf-8").rstrip()
            payload = raw[:-1] + ',"imported_at":"last-value"}\n'
            os.chmod(manifest_path, 0o640)
            manifest_path.write_text(payload, encoding="utf-8")
            os.chmod(manifest_path, 0o440)
            with mock.patch.object(MODULE, "MAX_METADATA_JSON_BYTES", 512):
                projected = MODULE._read_legacy_manifest_projection(manifest_path)
            self.assertEqual(projected["imported_at"], "last-value")
            self.assertEqual(
                projected["imported_at"],
                json.loads(payload)["imported_at"],
            )

    def test_legacy_oversized_annotations_are_migrated_without_rewriting_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            material = library / result["material_id"]
            annotations_path = material / "annotations.json"
            annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
            annotations["title"] = "Legacy title"
            annotations["note"] = "Legacy note"
            annotations["tags"] = ["legacy", "markers"]
            annotations["updated_at"] = "2026-09-01T12:34:56+00:00"
            annotations["markers"] = ["x" * (MODULE.MAX_METADATA_JSON_BYTES + 4096)]
            oversized = MODULE._canonical_bytes(annotations) + b"\n"
            self.assertGreater(len(oversized), MODULE.MAX_METADATA_JSON_BYTES)
            self.assertLess(len(oversized), MODULE.MAX_LEGACY_ANNOTATIONS_JSON_BYTES)
            annotations_path.write_bytes(oversized)
            before = hashlib.sha256(oversized).hexdigest()

            with self.assertRaisesRegex(
                MODULE.H2IngestError,
                "Control-Sidecar|Größenlimit|nicht lesbar",
            ):
                MODULE.library(library, projection="control")

            migration = MODULE.migrate_legacy_manifests(library)
            self.assertEqual(
                migration["kind"],
                "audio_h2_legacy_manifest_migration",
            )
            self.assertEqual(migration["annotations_migrated"], 1)
            self.assertEqual(migration["manifest_migrated"], 0)
            self.assertEqual(migration["migrated"], 0)
            control_path = material / MODULE.LEGACY_ANNOTATIONS_CONTROL_NAME
            self.assertTrue(control_path.is_file())
            self.assertLess(control_path.stat().st_size, MODULE.MAX_METADATA_JSON_BYTES)
            self.assertEqual(stat.S_IMODE(control_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(annotations_path.stat().st_mode), 0o440)
            self.assertEqual(hashlib.sha256(annotations_path.read_bytes()).hexdigest(), before)
            preserved = json.loads(annotations_path.read_text(encoding="utf-8"))
            self.assertEqual(preserved["markers"], annotations["markers"])

            projected = MODULE.library(library, projection="control")
            self.assertEqual(projected["count"], 1)
            compact = projected["items"][0]["annotations"]
            self.assertEqual(compact["title"], "Legacy title")
            self.assertEqual(compact["note"], "Legacy note")
            self.assertEqual(compact["tags"], ["legacy", "markers"])
            self.assertEqual(
                MODULE._read_annotations(
                    material,
                    result["material_id"],
                )["updated_at"],
                "2026-09-01T12:34:56+00:00",
            )

            media = MODULE.material_media(
                result["material_id"],
                0,
                library_root=library,
            )
            self.assertTrue(media["verified_current"])
            annotated = MODULE.annotate_material(
                result["material_id"],
                title="Neu",
                note="kompakt",
                tags=["edited"],
                library_root=library,
            )
            self.assertTrue(annotated["changed"])
            self.assertEqual(annotated["annotations"]["title"], "Neu")
            self.assertEqual(hashlib.sha256(annotations_path.read_bytes()).hexdigest(), before)

            second = MODULE.migrate_legacy_manifests(library)
            self.assertEqual(second["migrated"], 0)
            self.assertEqual(second["annotations_already_bound"], 1)

    def test_legacy_annotation_migration_streams_dense_markers_without_materializing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            material = library / result["material_id"]
            annotations_path = material / "annotations.json"
            dense_markers = b",".join([b"{}"] * 1800)
            payload = (
                b'{"kind":"audio_material_annotations","markers":['
                + dense_markers
                + b'],"material_id":"'
                + result["material_id"].encode("ascii")
                + b'","note":"Legacy note","schema_version":1,'
                + b'"tags":["legacy"],"title":"Legacy title","updated_at":null}\n'
            )
            self.assertGreater(len(payload), 4096)
            annotations_path.write_bytes(payload)
            before = hashlib.sha256(payload).hexdigest()
            real_loads = json.loads

            def bounded_loads(value, *args, **kwargs):
                self.assertLessEqual(len(value), 1024)
                return real_loads(value, *args, **kwargs)

            with (
                mock.patch.object(MODULE, "MAX_METADATA_JSON_BYTES", 4096),
                mock.patch.object(MODULE.json, "loads", side_effect=bounded_loads),
            ):
                migration = MODULE.migrate_legacy_manifests(library)

            self.assertEqual(migration["annotations_migrated"], 1)
            self.assertEqual(hashlib.sha256(annotations_path.read_bytes()).hexdigest(), before)
            projected = MODULE._read_json_regular(
                material / MODULE.LEGACY_ANNOTATIONS_CONTROL_NAME
            )
            self.assertEqual(projected["title"], "Legacy title")
            self.assertEqual(projected["note"], "Legacy note")
            self.assertEqual(projected["tags"], ["legacy"])

    def test_legacy_annotation_stream_projection_rejects_invalid_marker_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "annotations.json"
            padding = "x" * (MODULE.MAX_METADATA_JSON_BYTES + 1024)
            path.write_text(
                '{"schema_version":1,"kind":"audio_material_annotations",'
                '"material_id":"aaaaaaaaaaaaaaaaaaaaaaaa","title":"","note":"'
                + padding
                + '","tags":[],"markers":[{},],"updated_at":null}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                MODULE.H2IngestError,
                "ungültiges JSON",
            ):
                MODULE._read_legacy_annotations_projection(path)

    def test_legacy_annotation_stream_projection_preserves_last_duplicate_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "annotations.json"
            padding = "x" * (MODULE.MAX_METADATA_JSON_BYTES + 1024)
            payload = (
                '{"schema_version":1,"kind":"audio_material_annotations",'
                '"material_id":"aaaaaaaaaaaaaaaaaaaaaaaa",'
                '"title":"first","markers":{},"title":"last",'
                '"note":"kept","tags":["a"],"markers":[],'
                '"padding":"' + padding + '","updated_at":null}'
            )
            path.write_text(payload, encoding="utf-8")
            projected = MODULE._read_legacy_annotations_projection(path)
            reference = json.loads(payload)
            self.assertEqual(projected["title"], reference["title"])
            self.assertEqual(projected["markers"], reference["markers"])
            self.assertEqual(projected["tags"], reference["tags"])

    def test_legacy_annotation_migration_recovers_partial_control_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            material = library / result["material_id"]
            annotations_path = material / "annotations.json"
            annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
            annotations["title"] = "Legacy title"
            annotations["note"] = "Legacy note"
            annotations["tags"] = ["legacy"]
            annotations["markers"] = ["x" * (MODULE.MAX_METADATA_JSON_BYTES + 4096)]
            oversized = MODULE._canonical_bytes(annotations) + b"\n"
            annotations_path.write_bytes(oversized)
            before = hashlib.sha256(oversized).hexdigest()

            control_path = material / MODULE.LEGACY_ANNOTATIONS_CONTROL_NAME
            control_path.write_bytes(b'{"schema_version":1,"kind":"audio_material_')

            migration = MODULE.migrate_legacy_manifests(library)

            self.assertEqual(migration["annotations_migrated"], 1)
            self.assertEqual(migration["annotations_already_bound"], 0)
            self.assertEqual(hashlib.sha256(annotations_path.read_bytes()).hexdigest(), before)
            self.assertEqual(stat.S_IMODE(annotations_path.stat().st_mode), 0o440)
            self.assertEqual(stat.S_IMODE(control_path.stat().st_mode), 0o600)
            observed = MODULE._read_json_regular(control_path)
            projected = MODULE._annotations_from_legacy_control(
                observed,
                result["material_id"],
                annotations_metadata=annotations_path.stat(),
            )
            self.assertEqual(projected["title"], "Legacy title")
            self.assertEqual(projected["note"], "Legacy note")
            self.assertEqual(projected["tags"], ["legacy"])
            self.assertEqual(projected["markers"], [])

            second = MODULE.migrate_legacy_manifests(library)
            self.assertEqual(second["annotations_migrated"], 0)
            self.assertEqual(second["annotations_already_bound"], 1)
            self.assertEqual(hashlib.sha256(annotations_path.read_bytes()).hexdigest(), before)

    def test_legacy_annotation_migration_does_not_mask_control_sidecar_io_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            material = library / result["material_id"]
            annotations_path = material / "annotations.json"
            annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
            annotations["markers"] = ["x" * (MODULE.MAX_METADATA_JSON_BYTES + 4096)]
            annotations_path.write_bytes(MODULE._canonical_bytes(annotations) + b"\n")

            control_path = material / MODULE.LEGACY_ANNOTATIONS_CONTROL_NAME
            control_path.write_text("{}", encoding="utf-8")
            real_read = MODULE._read_json_regular

            def read_with_permission_failure(path, **kwargs):
                if path == control_path:
                    try:
                        raise PermissionError("denied")
                    except PermissionError as cause:
                        raise MODULE.H2IngestError(
                            "Metadatendatei ist nicht sicher lesbar."
                        ) from cause
                return real_read(path, **kwargs)

            with mock.patch.object(
                MODULE,
                "_read_json_regular",
                side_effect=read_with_permission_failure,
            ):
                with self.assertRaisesRegex(
                    MODULE.H2IngestError,
                    "nicht sicher lesbar",
                ):
                    MODULE.migrate_legacy_manifests(library)

            self.assertEqual(control_path.read_text(encoding="utf-8"), "{}")

    def test_legacy_annotation_binding_fails_closed_after_original_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            material = library / result["material_id"]
            annotations_path = material / "annotations.json"
            annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
            annotations["markers"] = ["x" * (MODULE.MAX_METADATA_JSON_BYTES + 4096)]
            annotations_path.write_bytes(MODULE._canonical_bytes(annotations) + b"\n")
            MODULE.migrate_legacy_manifests(library)

            os.chmod(annotations_path, 0o600)
            annotations_path.write_bytes(annotations_path.read_bytes() + b" ")
            os.chmod(annotations_path, 0o440)
            with self.assertRaisesRegex(
                MODULE.H2IngestError,
                "aktuelle, gebundene Control-Sidecar",
            ):
                MODULE.library(library, projection="control")
            with self.assertRaisesRegex(
                MODULE.H2IngestError,
                "aktuelle, gebundene Control-Sidecar",
            ):
                MODULE.migrate_legacy_manifests(library)

    def test_legacy_annotation_migration_rehashes_bound_original_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            material = library / result["material_id"]
            annotations_path = material / "annotations.json"
            annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
            annotations["markers"] = ["x" * (MODULE.MAX_METADATA_JSON_BYTES + 4096)]
            annotations_path.write_bytes(MODULE._canonical_bytes(annotations) + b"\n")
            MODULE.migrate_legacy_manifests(library)

            before = annotations_path.stat()
            payload = bytearray(annotations_path.read_bytes())
            marker_offset = payload.find(b"x")
            self.assertGreaterEqual(marker_offset, 0)
            payload[marker_offset] = ord("y")
            os.chmod(annotations_path, 0o600)
            annotations_path.write_bytes(payload)
            os.utime(
                annotations_path,
                ns=(before.st_atime_ns, before.st_mtime_ns),
            )
            os.chmod(annotations_path, 0o440)
            after = annotations_path.stat()
            self.assertEqual(after.st_size, before.st_size)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(after.st_ino, before.st_ino)

            with self.assertRaisesRegex(
                MODULE.H2IngestError,
                "gebundenen Migrationsbeleg",
            ):
                MODULE.migrate_legacy_manifests(library)

    def test_legacy_annotation_migration_resumes_after_prior_material_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            second_scene = "180926_191401"
            second_session = source / second_scene
            second_session.mkdir()
            write_h2_wav(
                second_session / f"{second_scene}_FRONT.WAV",
                scene=second_scene,
                role="FRONT",
            )
            library = root / "library"
            first = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            second = MODULE.import_scene(
                second_scene,
                source_root=source,
                library_root=library,
            )

            def oversize(material_id: str, marker: str) -> tuple[pathlib.Path, str]:
                path = library / material_id / "annotations.json"
                value = json.loads(path.read_text(encoding="utf-8"))
                value["markers"] = [
                    marker * (MODULE.MAX_METADATA_JSON_BYTES + 4096)
                ]
                payload = MODULE._canonical_bytes(value) + b"\n"
                path.write_bytes(payload)
                return path, hashlib.sha256(payload).hexdigest()

            first_path, first_hash = oversize(first["material_id"], "x")
            initial = MODULE.migrate_legacy_manifests(library)
            self.assertEqual(initial["annotations_migrated"], 1)
            self.assertEqual(initial["annotations_compact"], 1)

            second_path, second_hash = oversize(second["material_id"], "z")
            resumed = MODULE.migrate_legacy_manifests(library)
            self.assertEqual(resumed["annotations_migrated"], 1)
            self.assertEqual(resumed["annotations_already_bound"], 1)
            self.assertEqual(
                hashlib.sha256(first_path.read_bytes()).hexdigest(),
                first_hash,
            )
            self.assertEqual(
                hashlib.sha256(second_path.read_bytes()).hexdigest(),
                second_hash,
            )
            self.assertEqual(
                MODULE.library(library, projection="control")["count"],
                2,
            )

    def test_legacy_annotation_sidecar_binding_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            material = library / result["material_id"]
            annotations_path = material / "annotations.json"
            annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
            annotations["markers"] = ["x" * (MODULE.MAX_METADATA_JSON_BYTES + 4096)]
            annotations_path.write_bytes(MODULE._canonical_bytes(annotations) + b"\n")
            MODULE.migrate_legacy_manifests(library)

            control_path = material / MODULE.LEGACY_ANNOTATIONS_CONTROL_NAME
            control = json.loads(control_path.read_text(encoding="utf-8"))
            control["legacy_annotations"]["sha256"] = "0" * 64
            control_path.write_bytes(MODULE._canonical_bytes(control) + b"\n")

            with self.assertRaisesRegex(
                MODULE.H2IngestError,
                "gebundenen Migrationsbeleg",
            ):
                MODULE.migrate_legacy_manifests(library)

    def test_legacy_migration_receipt_atomic_publish_failure_leaves_no_partial_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            receipt_path = root / MODULE.LEGACY_MIGRATION_RECEIPT_NAME
            payload = {
                "schema_version": MODULE.SCHEMA_VERSION,
                "kind": "audio_h2_legacy_migration_receipt",
                "status": "success",
            }

            with mock.patch.object(
                MODULE.os,
                "replace",
                side_effect=OSError("interrupted"),
            ):
                with self.assertRaisesRegex(
                    MODULE.H2IngestError,
                    "atomar veröffentlicht",
                ):
                    MODULE._write_legacy_migration_receipt(root, payload)

            self.assertFalse(receipt_path.exists())
            self.assertFalse(
                any(path.name.startswith(".metadata-") for path in root.iterdir())
            )

            MODULE._write_legacy_migration_receipt(root, payload)
            self.assertEqual(
                MODULE._read_json_regular(receipt_path),
                payload,
            )
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)

    def test_durable_migration_recovers_partial_receipt_and_republishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            imported = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            material = library / imported["material_id"]
            annotations_path = material / "annotations.json"
            annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
            annotations["markers"] = [
                "x" * (MODULE.MAX_METADATA_JSON_BYTES + 4096)
            ]
            annotations_path.write_bytes(MODULE._canonical_bytes(annotations) + b"\n")
            receipt_path = library / MODULE.LEGACY_MIGRATION_RECEIPT_NAME
            receipt_path.write_bytes(b'{"schema_version":1,"kind":"audio_h2_')
            commit = "b" * 40
            inactive = {
                "LoadState": "not-found",
                "ActiveState": "inactive",
                "SubState": "dead",
            }

            def launch(_root, _inventory):
                MODULE._run_legacy_migration_worker(library)
                return MODULE._legacy_migration_worker_unit(library)

            with (
                mock.patch.object(
                    MODULE,
                    "_durable_migration_release_commit",
                    return_value=commit,
                ),
                mock.patch.object(
                    MODULE,
                    "_legacy_migration_systemd_state",
                    return_value=inactive,
                ),
                mock.patch.object(
                    MODULE,
                    "_launch_legacy_migration_worker",
                    side_effect=launch,
                ) as launcher,
            ):
                result = MODULE.migrate_legacy_manifests_durable(library)

            self.assertTrue(result["durable_receipt_reused"])
            launcher.assert_called_once()
            receipt = MODULE._read_json_regular(receipt_path)
            self.assertEqual(receipt["release_commit"], commit)
            self.assertEqual(receipt["status"], "success")
            self.assertRegex(receipt["postcondition_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)

    def test_legacy_migration_receipt_io_error_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            receipt_path = root / MODULE.LEGACY_MIGRATION_RECEIPT_NAME
            receipt_path.write_text("{}", encoding="utf-8")
            real_read = MODULE._read_json_regular

            def read_with_permission_failure(path, **kwargs):
                if path == receipt_path:
                    try:
                        raise PermissionError("denied")
                    except PermissionError as cause:
                        raise MODULE.H2IngestError(
                            "Metadatendatei ist nicht sicher lesbar."
                        ) from cause
                return real_read(path, **kwargs)

            with mock.patch.object(
                MODULE,
                "_read_json_regular",
                side_effect=read_with_permission_failure,
            ):
                with self.assertRaisesRegex(
                    MODULE.H2IngestError,
                    "nicht sicher lesbar",
                ):
                    MODULE._read_legacy_migration_receipt(root)

    def test_durable_migration_survives_caller_timeout_via_release_bound_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            imported = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            material = library / imported["material_id"]
            annotations_path = material / "annotations.json"
            annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
            annotations["markers"] = [
                "x" * (MODULE.MAX_METADATA_JSON_BYTES + 4096)
            ]
            annotations_path.write_bytes(MODULE._canonical_bytes(annotations) + b"\n")
            commit = "a" * 40
            inactive = {
                "LoadState": "not-found",
                "ActiveState": "inactive",
                "SubState": "dead",
            }

            def launch(_root, _inventory):
                MODULE._run_legacy_migration_worker(library)
                return MODULE._legacy_migration_worker_unit(library)

            with (
                mock.patch.object(
                    MODULE,
                    "_durable_migration_release_commit",
                    return_value=commit,
                ),
                mock.patch.object(
                    MODULE,
                    "_legacy_migration_systemd_state",
                    return_value=inactive,
                ),
                mock.patch.object(
                    MODULE,
                    "_launch_legacy_migration_worker",
                    side_effect=launch,
                ) as launcher,
            ):
                first = MODULE.migrate_legacy_manifests_durable(library)
                MODULE.annotate_material(
                    imported["material_id"],
                    title="Nach Migration",
                    note="legitime mutable Änderung",
                    tags=["edited"],
                    library_root=library,
                )
                second = MODULE.migrate_legacy_manifests_durable(library)

            self.assertEqual(first["annotations_migrated"], 1)
            self.assertTrue(first["durable_worker"])
            self.assertTrue(first["durable_receipt_reused"])
            self.assertTrue(second["durable_receipt_reused"])
            launcher.assert_called_once()
            receipt = json.loads(
                (library / MODULE.LEGACY_MIGRATION_RECEIPT_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["release_commit"], commit)
            self.assertEqual(receipt["status"], "success")
            self.assertRegex(receipt["postcondition_sha256"], r"^[0-9a-f]{64}$")

    def test_durable_worker_is_detached_bounded_and_uses_immutable_release_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory = {
                "library_root": str(root),
                "material_count": 4,
                "candidate_file_count": 2,
                "candidate_bytes": 20 * 1024 * 1024,
                "candidate_metadata_sha256": "0" * 64,
            }
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="",
            )
            with mock.patch.object(
                MODULE.subprocess,
                "run",
                return_value=completed,
            ) as runner:
                unit = MODULE._launch_legacy_migration_worker(root, inventory)

            argv = runner.call_args.args[0]
            self.assertEqual(argv[:5], [
                "systemd-run",
                "--user",
                "--collect",
                "--no-block",
                "--quiet",
            ])
            self.assertIn(
                f"--property=MemoryMax={MODULE.LEGACY_MIGRATION_WORKER_MEMORY_MAX_BYTES}",
                argv,
            )
            self.assertIn(f"--property=ReadWritePaths={root}", argv)
            self.assertIn("--property=ProtectSystem=strict", argv)
            self.assertIn("--property=ProtectHome=read-only", argv)
            self.assertIn("_migrate-legacy-manifests-worker", argv)
            worker_index = argv.index("_migrate-legacy-manifests-worker")
            script_path = pathlib.Path(argv[worker_index - 1])
            self.assertTrue(script_path.is_absolute())
            self.assertEqual(script_path, pathlib.Path(MODULE.__file__).resolve())
            self.assertTrue(unit.endswith(".service"))

    def test_migrate_legacy_cli_routes_through_durable_release_handoff(self):
        expected = {
            "schema_version": MODULE.SCHEMA_VERSION,
            "kind": "audio_h2_legacy_manifest_migration",
            "library_root": "/tmp/example",
            "migrated": 0,
            "already_bound": 0,
            "compact": 0,
            "manifest_migrated": 0,
            "manifest_already_bound": 0,
            "annotations_migrated": 0,
            "annotations_already_bound": 0,
            "annotations_compact": 0,
            "read_only_originals": True,
        }
        with (
            mock.patch.object(
                MODULE,
                "migrate_legacy_manifests_durable",
                return_value=expected,
            ) as durable,
            mock.patch("builtins.print"),
        ):
            status = MODULE.main(
                [
                    "migrate-legacy-manifests",
                    "--library-root",
                    "/tmp/example",
                ]
            )
        self.assertEqual(status, 0)
        durable.assert_called_once_with(pathlib.Path("/tmp/example"))

    def test_launch_only_durable_migration_schedules_worker_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory = {
                "library_root": str(root),
                "material_count": 2,
                "candidate_file_count": 1,
                "candidate_bytes": 8 * 1024 * 1024,
                "candidate_metadata_sha256": "1" * 64,
            }
            unit = "audio-h2-legacy-migrate-v1-example.service"
            with (
                mock.patch.object(
                    MODULE,
                    "_durable_migration_release_commit",
                    return_value="a" * 40,
                ),
                mock.patch.object(
                    MODULE,
                    "_legacy_migration_inventory",
                    return_value=inventory,
                ),
                mock.patch.object(
                    MODULE,
                    "_durable_migration_receipt_result",
                    return_value=None,
                ),
                mock.patch.object(
                    MODULE,
                    "_launch_legacy_migration_worker",
                    return_value=unit,
                ) as launcher,
                mock.patch.object(
                    MODULE.time,
                    "sleep",
                    side_effect=AssertionError("launch-only must not wait"),
                ),
            ):
                result = MODULE.launch_legacy_manifests_durable(root)

            launcher.assert_called_once_with(root, inventory)
            self.assertEqual(result["kind"], "audio_h2_legacy_migration_launch")
            self.assertEqual(result["status"], "scheduled")
            self.assertTrue(result["launch_only"])
            self.assertTrue(result["durable_worker"])
            self.assertEqual(result["unit"], unit)

    def test_migrate_legacy_cli_launch_only_routes_to_nonblocking_handoff(self):
        expected = {
            "schema_version": MODULE.SCHEMA_VERSION,
            "kind": "audio_h2_legacy_migration_launch",
            "status": "scheduled",
        }
        with (
            mock.patch.object(
                MODULE,
                "launch_legacy_manifests_durable",
                return_value=expected,
            ) as launch,
            mock.patch.object(
                MODULE,
                "migrate_legacy_manifests_durable",
                side_effect=AssertionError("launch-only must not use wait path"),
            ),
            mock.patch("builtins.print"),
        ):
            status = MODULE.main(
                [
                    "migrate-legacy-manifests",
                    "--launch-only",
                    "--library-root",
                    "/tmp/example",
                ]
            )
        self.assertEqual(status, 0)
        launch.assert_called_once_with(pathlib.Path("/tmp/example"))

    def test_metadata_limit_preserves_shared_service_memory_headroom(self):
        unit = (
            ROOT / "systemd" / "user" / "audio-control-ui-v1.service"
        ).read_text(encoding="utf-8")
        memory_line = next(
            line for line in unit.splitlines() if line.startswith("MemoryMax=")
        )
        memory_max = int(memory_line.split("=", 1)[1])
        self.assertLessEqual(
            MODULE.MAX_METADATA_JSON_BYTES * 64,
            memory_max,
        )
        control = (ROOT / "scripts" / "audio_control.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "H2_MAX_METADATA_JSON_BYTES = 2 * 1024 * 1024",
            control,
        )

    def test_metadata_reader_rejects_oversized_json_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "manifest.json"
            with path.open("wb") as handle:
                handle.truncate(MODULE.MAX_METADATA_JSON_BYTES + 1)
            with self.assertRaisesRegex(MODULE.H2IngestError, "Größenlimit"):
                MODULE._read_json_regular(path)

    def test_import_rejects_oversized_manifest_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            with (
                mock.patch.object(MODULE, "MAX_METADATA_JSON_BYTES", 256),
                self.assertRaisesRegex(MODULE.H2IngestError, "Größenlimit"),
            ):
                MODULE.import_scene(
                    "170926_191401",
                    source_root=source,
                    library_root=library,
                )
            entries = list(library.iterdir())
            self.assertEqual([path.name for path in entries], [".h2-import.lock"])
            self.assertFalse(
                any(path.name.startswith(".h2-staging-") for path in entries)
            )
            self.assertFalse(
                any(
                    path.is_dir() and MODULE.MATERIAL_ID_RE.fullmatch(path.name)
                    for path in entries
                )
            )

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

    def test_control_library_projection_is_compact_and_fits_runner_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            tags = ["😀" * 47 + chr(0x1F600 + index) for index in range(MODULE.MAX_TAGS)]
            MODULE.annotate_material(
                result["material_id"],
                title="😀" * MODULE.MAX_TITLE_CHARS,
                note="😀" * MODULE.MAX_NOTE_CHARS,
                tags=tags,
                library_root=library,
            )
            report = MODULE.library(library, projection="control")

        self.assertEqual(report["projection"], MODULE.CONTROL_LIBRARY_PROJECTION)
        self.assertEqual(report["count"], 1)
        compact = report["items"][0]
        self.assertNotIn("masters", compact)
        self.assertNotIn("markers", compact)
        self.assertGreater(compact["total_bytes"], 0)
        self.assertGreater(compact["max_file_bytes"], 0)
        self.assertEqual(compact["roles"], ["front"])
        self.assertEqual(compact["segment_count"], 1)
        self.assertEqual(compact["annotations"]["note"], "😀" * MODULE.MAX_NOTE_CHARS)

        worst_case = {
            "schema_version": MODULE.SCHEMA_VERSION,
            "kind": "audio_material_library",
            "projection": MODULE.CONTROL_LIBRARY_PROJECTION,
            "items": [
                {
                    **compact,
                    "material_id": f"{index:024x}",
                    "imported_at": "x" * 64,
                    "total_bytes": 2**63 - 1,
                    "max_file_bytes": 2**63 - 1,
                    "roles": ["front", "rear", "mix"],
                    "segment_count": MODULE.MAX_SESSION_FILES,
                }
                for index in range(MODULE.MAX_CONTROL_LIBRARY_ITEMS)
            ],
            "count": MODULE.MAX_CONTROL_LIBRARY_ITEMS,
            "total_count": MODULE.MAX_CONTROL_LIBRARY_ITEMS,
            "truncated": False,
            "read_only": True,
        }
        encoded = json.dumps(
            worst_case,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        self.assertLess(len(encoded), 1_048_576)

    def test_control_library_truncates_legacy_archive_without_whole_archive_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            first = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            second_scene = "170926_191402"
            second = source / second_scene
            second.mkdir()
            write_h2_wav(
                second / f"{second_scene}_FRONT.WAV",
                scene=second_scene,
                role="FRONT",
                recorded_time="19:14:02",
            )
            second_result = MODULE.import_scene(
                second_scene,
                source_root=source,
                library_root=library,
            )

            first_manifest = library / first["material_id"] / "manifest.json"
            second_manifest = library / second_result["material_id"] / "manifest.json"
            os.utime(first_manifest, ns=(1_000_000_000, 1_000_000_000))
            os.utime(second_manifest, ns=(2_000_000_000, 2_000_000_000))

            with mock.patch.object(MODULE, "MAX_CONTROL_LIBRARY_ITEMS", 1):
                report = MODULE.library(library, projection="control")

            self.assertEqual(report["count"], 1)
            self.assertEqual(report["total_count"], 2)
            self.assertIs(report["truncated"], True)
            self.assertEqual(
                report["items"][0]["material_id"],
                second_result["material_id"],
            )

    def test_control_library_rejects_new_material_before_publish_at_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            first = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            second_scene = "170926_191402"
            second = source / second_scene
            second.mkdir()
            write_h2_wav(
                second / f"{second_scene}_FRONT.WAV",
                scene=second_scene,
                role="FRONT",
                recorded_time="19:14:02",
            )
            with mock.patch.object(MODULE, "MAX_CONTROL_LIBRARY_ITEMS", 1):
                repeated = MODULE.import_scene(
                    "170926_191401",
                    source_root=source,
                    library_root=library,
                )
                self.assertEqual(repeated["status"], "already-imported")
                self.assertEqual(repeated["material_id"], first["material_id"])
                with self.assertRaisesRegex(MODULE.H2IngestError, "Material-Limit"):
                    MODULE.import_scene(
                        second_scene,
                        source_root=source,
                        library_root=library,
                    )
                report = MODULE.library(library, projection="control")
            self.assertEqual(report["count"], 1)
            material_dirs = [
                path
                for path in library.iterdir()
                if path.is_dir() and MODULE.MATERIAL_ID_RE.fullmatch(path.name)
            ]
            self.assertEqual([path.name for path in material_dirs], [first["material_id"]])
            self.assertFalse(
                any(path.name.startswith(".h2-staging-") for path in library.iterdir())
            )

    def test_control_library_capacity_is_serialized_across_concurrent_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            second_scene = "170926_191402"
            second = source / second_scene
            second.mkdir()
            write_h2_wav(
                second / f"{second_scene}_FRONT.WAV",
                scene=second_scene,
                described_scene=second_scene,
                role="FRONT",
                recorded_time="19:14:02",
            )
            library = root / "library"
            library.mkdir(mode=0o700)
            with (
                mock.patch.object(MODULE, "MAX_CONTROL_LIBRARY_ITEMS", 1),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                futures = [
                    executor.submit(
                        MODULE.import_scene,
                        scene,
                        source_root=source,
                        library_root=library,
                    )
                    for scene in ("170926_191401", second_scene)
                ]
                results = []
                errors = []
                for future in futures:
                    try:
                        results.append(future.result())
                    except MODULE.H2IngestError as error:
                        errors.append(str(error))

            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertIn("Material-Limit", errors[0])
            report = MODULE.library(library, projection="control")
            self.assertEqual(report["count"], 1)
            material_dirs = [
                path
                for path in library.iterdir()
                if path.is_dir() and MODULE.MATERIAL_ID_RE.fullmatch(path.name)
            ]
            self.assertEqual(len(material_dirs), 1)

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

    def test_annotation_replace_rejects_oversized_payload_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = make_source(root, roles=("FRONT",))
            library = root / "library"
            result = MODULE.import_scene(
                "170926_191401",
                source_root=source,
                library_root=library,
            )
            annotations_path = (
                library / result["material_id"] / "annotations.json"
            )
            current = json.loads(annotations_path.read_text(encoding="utf-8"))
            current["markers"] = ["x" * 4096]
            original = MODULE._canonical_bytes(current) + b"\n"
            annotations_path.write_bytes(original)
            limit = len(original) + 1

            with (
                mock.patch.object(MODULE, "MAX_METADATA_JSON_BYTES", limit),
                self.assertRaisesRegex(MODULE.H2IngestError, "Größenlimit"),
            ):
                MODULE.annotate_material(
                    result["material_id"],
                    title="neuer Titel",
                    note="",
                    tags=[],
                    library_root=library,
                )

            self.assertEqual(annotations_path.read_bytes(), original)
            self.assertFalse(any(annotations_path.parent.glob(".metadata-*")))

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
