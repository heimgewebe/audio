import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "recording_product_test_target", ROOT / "scripts" / "recording_product.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RecordingProductTests(unittest.TestCase):
    def synthetic_session(self, root: pathlib.Path, *, running: bool = False):
        session_id = "a" * 24
        result_path = root / f"{session_id}.result.json"
        final_path = root / "voice-take.wav"
        spec = {
            "schema_version": 1,
            "kind": "audio_recording_session_spec",
            "session_id": session_id,
            "created_at": "2026-08-08T06:00:00+00:00",
            "plan_sha256": "b" * 64,
            "paths": {"final": str(final_path)},
            "plan_identity": {
                "session_type": "voice-recording",
                "profile": "voice-recording",
                "capture": {
                    "sample_rate_hz": 48_000,
                    "sample_format": "s32le",
                    "channels": 2,
                    "maximum_duration_seconds": 600,
                },
                "monitoring": {
                    "mode": "hardware-direct",
                    "endpoint": "motu-m2",
                    "software_loopback": False,
                    "level_claim": "physical-reference-required",
                },
                "source": {
                    "identity": {
                        "sample_rate_hz": 48_000,
                        "sample_format": "s32le",
                        "channels": 2,
                    },
                    "identity_sha256": "c" * 64,
                },
                "physical": {
                    "facts": {
                        "rode_nt1a_connected": True,
                        "rode_nt1a_motu_input": "input-1",
                        "motu_phantom_48v": "on",
                        "motu_input_gain_reference": "mark-1",
                    }
                },
                "laboratory": {"resolved": ["voice-level-measurement"]},
            },
        }
        state = {
            "schema_version": 1,
            "kind": "audio_recording_session_state",
            "session_id": session_id,
            "started_at": "2026-08-08T06:00:01+00:00" if running else None,
            "process": {"pid": 123, "start_ticks": 456} if running else None,
        }
        paths = {"result": result_path}
        return session_id, paths, spec, state

    def test_missing_state_projects_idle_without_creating_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / "not-created"
            report = MODULE.probe(state_root=missing)
        self.assertEqual(report["status"], "idle")
        self.assertIsNone(report["session"])
        self.assertTrue(report["read_only"])
        self.assertFalse(missing.exists())

    def test_state_root_must_remain_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            os.chmod(root, 0o755)
            with self.assertRaisesRegex(MODULE.RecordingProductError, "private"):
                MODULE.probe(state_root=root)

    def test_invalid_session_id_is_rejected_before_media_lookup(self):
        with self.assertRaisesRegex(MODULE.RecordingProductError, "Sitzungs-ID"):
            MODULE.verified_media("../../take")

    def test_running_projection_is_path_free_and_receipt_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            session_id, paths, spec, state = self.synthetic_session(root, running=True)
            with (
                mock.patch.object(MODULE.REC, "_read_session", return_value=(paths, spec, state)),
                mock.patch.object(MODULE.REC, "_identity_matches", return_value=True),
            ):
                report = MODULE._session_projection(
                    root, session_id, active_session_id=session_id
                )
        self.assertEqual(report["status"], "running")
        self.assertTrue(report["active"])
        self.assertTrue(report["source"]["bound"])
        self.assertTrue(report["laboratory"]["voice_level_measurement"])
        serialized = json.dumps(report)
        self.assertNotIn(str(root), serialized)
        self.assertNotIn('"path"', serialized)
        self.assertNotIn('"process"', serialized)

    def test_dead_unfinished_session_projects_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            session_id, paths, spec, state = self.synthetic_session(root, running=False)
            with mock.patch.object(
                MODULE.REC, "_read_session", return_value=(paths, spec, state)
            ):
                report = MODULE._session_projection(
                    root, session_id, active_session_id=session_id
                )
        self.assertEqual(report["status"], "recovery-required")
        self.assertTrue(report["recovery_required"])
        self.assertFalse(report["active"])

    def test_completed_projection_exposes_metadata_but_no_private_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            session_id, paths, spec, state = self.synthetic_session(root, running=False)
            paths["result"].write_text("{}", encoding="utf-8")
            result = {
                "schema_version": 1,
                "kind": "audio_recording_result",
                "session_id": session_id,
                "plan_sha256": spec["plan_sha256"],
                "status": "completed",
                "reason": "capture-completed",
                "started_at": "2026-08-08T06:00:01+00:00",
                "completed_at": "2026-08-08T06:00:05+00:00",
                "artifact": {
                    "path": str(root / "voice-take.wav"),
                    "sha256": "d" * 64,
                    "bytes": 4096,
                    "mode": "0600",
                    "device": 1,
                    "inode": 2,
                    "channels": 2,
                    "bit_depth_container": 32,
                    "sample_rate_hz": 48_000,
                    "frames": 48_000,
                    "duration_seconds": 1.0,
                },
            }
            with (
                mock.patch.object(MODULE.REC, "_read_session", return_value=(paths, spec, state)),
                mock.patch.object(MODULE.REC, "_safe_json_read", return_value=result),
                mock.patch.object(MODULE.REC, "_validate_binding_shape"),
            ):
                report = MODULE._session_projection(
                    root, session_id, active_session_id=None
                )
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["result"]["artifact"]["sha256"], "d" * 64)
        self.assertFalse(report["result"]["artifact"]["current_bytes_verified"])
        serialized = json.dumps(report)
        self.assertNotIn(str(root), serialized)
        self.assertNotIn('"path"', serialized)

    def test_performance_projection_requires_manifest_commit_truth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            session_id, _paths, spec, _state = self.synthetic_session(root)
            spec["plan_identity"]["session_type"] = "piano-vocal-performance"
            spec["paths"].update(
                {
                    "midi_final": str(root / "voice-take.mid"),
                    "manifest_final": str(root / "voice-take.take.json"),
                }
            )
            vocal = {
                "path": spec["paths"]["final"],
                "sha256": "d" * 64,
                "bytes": 4096,
                "mode": "0600",
                "device": 1,
                "inode": 2,
                "channels": 2,
                "bit_depth_container": 32,
                "sample_rate_hz": 48_000,
                "frames": 48_000,
                "duration_seconds": 1.0,
            }
            result = {
                "schema_version": 1,
                "kind": "audio_recording_result",
                "session_id": session_id,
                "plan_sha256": spec["plan_sha256"],
                "status": "completed",
                "reason": "requested-stop",
                "started_at": "2026-08-08T06:00:01+00:00",
                "completed_at": "2026-08-08T06:00:05+00:00",
                "artifacts": {
                    "vocal_wav": vocal,
                    "roland_midi_smf": {"sha256": "e" * 64, "bytes": 128},
                    "take_manifest": {"sha256": "f" * 64, "bytes": 512},
                },
                "does_not_establish": ["sample-accurate-wav-midi-synchronization"],
            }
            with (
                mock.patch.object(MODULE.REC, "_validate_binding_shape"),
                mock.patch.object(
                    MODULE.REC,
                    "_validate_result",
                    side_effect=MODULE.REC.RecordingError("manifest missing"),
                ),
            ):
                with self.assertRaisesRegex(
                    MODULE.RecordingProductError, "Manifest"
                ):
                    MODULE._result_projection(result, spec)
            manifest = {"midi_event_counts": {"note_on": 0, "note_off": 0}}
            with (
                mock.patch.object(MODULE.REC, "_validate_binding_shape"),
                mock.patch.object(MODULE.REC, "_validate_result"),
                mock.patch.object(
                    MODULE.REC, "_validate_performance_manifest", return_value=manifest
                ),
            ):
                projection = MODULE._result_projection(result, spec)
        self.assertTrue(
            projection["artifacts"]["take_manifest"]["commit_truth"]
        )
        self.assertEqual(projection["midi_event_counts"]["note_on"], 0)

    def test_failed_performance_lists_final_siblings_only_as_recovery_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            session_id, _paths, spec, _state = self.synthetic_session(root)
            spec["plan_identity"]["session_type"] = "piano-vocal-performance"
            spec["plan_identity"]["capture"]["maximum_file_bytes"] = 1_000_000
            spec["paths"].update(
                {
                    "partial": str(root / ".voice.partial.wav"),
                    "midi_partial": str(root / ".voice.partial.mid"),
                    "midi_final": str(root / "voice-take.mid"),
                    "manifest_partial": str(root / ".voice.partial.take.json"),
                    "manifest_final": str(root / "voice-take.take.json"),
                }
            )
            for key, payload in (
                ("partial", b"partial wav"),
                ("final", b"final wav"),
                ("midi_partial", b"partial midi"),
                ("midi_final", b"final midi"),
                ("manifest_partial", b"{}"),
            ):
                path = pathlib.Path(spec["paths"][key])
                path.write_bytes(payload)
                path.chmod(0o600)
            inventory = MODULE.REC._performance_path_inventory(spec)
            result = {
                "schema_version": 1,
                "kind": "audio_recording_result",
                "session_id": session_id,
                "plan_sha256": spec["plan_sha256"],
                "status": "failed-preserved",
                "reason": "OSError",
                "failed_at": "2026-08-08T06:00:05+00:00",
                "error": "injected before manifest commit",
                "partial": inventory["wav_partial"],
                "performance_artifacts": inventory,
                "does_not_establish": ["successful-recording"],
            }

            projection = MODULE._result_projection(result, spec)

        self.assertEqual(projection["status"], "failed-preserved")
        self.assertEqual(projection["recovery_artifacts"]["wav_final"], "present")
        self.assertEqual(projection["recovery_artifacts"]["midi_final"], "present")
        self.assertEqual(
            projection["recovery_artifacts"]["manifest_partial"], "present"
        )
        self.assertEqual(
            projection["recovery_artifacts"]["manifest_final"], "missing"
        )
        self.assertNotIn("artifacts", projection)

    def test_modern_performance_projects_mix_wav_not_a_voice_stem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            session_id, _paths, spec, _state = self.synthetic_session(root)
            spec["plan_identity"]["session_type"] = "piano-vocal-performance"
            spec["plan_identity"]["performance"] = {
                "ffmpeg": {}, "audio_capture": {}, "mix": {}
            }
            spec["paths"].update({"manifest_final": str(root / "take.take.json")})
            mix = {
                "path": spec["paths"]["final"],
                "sha256": "d" * 64,
                "bytes": 4096,
                "mode": "0600",
                "device": 1,
                "inode": 2,
                "channels": 2,
                "bit_depth_container": 32,
                "sample_rate_hz": 48_000,
                "frames": 48_000,
                "duration_seconds": 1.0,
            }
            result = {
                "schema_version": 1,
                "kind": "audio_recording_result",
                "session_id": session_id,
                "plan_sha256": spec["plan_sha256"],
                "status": "completed",
                "reason": "requested-stop",
                "started_at": "2026-08-08T06:00:01+00:00",
                "completed_at": "2026-08-08T06:00:05+00:00",
                "artifacts": {
                    "mix_wav": mix,
                    "roland_midi_smf": {"sha256": "e" * 64, "bytes": 128},
                    "take_manifest": {"sha256": "f" * 64, "bytes": 512},
                },
                "does_not_establish": ["sample-accurate-wav-midi-synchronization"],
            }
            with (
                mock.patch.object(MODULE.REC, "_validate_binding_shape"),
                mock.patch.object(MODULE.REC, "_validate_result"),
                mock.patch.object(
                    MODULE.REC,
                    "_validate_performance_manifest",
                    return_value={"midi_event_counts": {"note_on": 0, "note_off": 0}},
                ),
            ):
                projection = MODULE._result_projection(result, spec)
        self.assertIn("mix_wav", projection["artifacts"])
        self.assertNotIn("vocal_wav", projection["artifacts"])


if __name__ == "__main__":
    unittest.main()
