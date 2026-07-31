from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import types
import unittest
import wave
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "recording_session_test_module", ROOT / "scripts/recording_session.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RecordingSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.output = self.base / "recordings"
        self.output.mkdir(mode=0o700)
        self.state = self.base / "state"

    @staticmethod
    def source_identity() -> dict[str, object]:
        return {
            "vendor_id": "07fd",
            "product_id": "0008",
            "serial_sha256": "1" * 64,
            "node_name_sha256": "2" * 64,
            "bus_path_sha256": "3" * 64,
            "sample_format": "s32le",
            "sample_rate_hz": 48000,
            "channels": 2,
            "muted": False,
            "unity_volume": True,
            "fingerprint": "4" * 64,
        }

    def ready_plan(self, *, free: int = 20_000_000_000) -> dict[str, object]:
        physical = {
            "state_path": str(self.base / "physical.json"),
            "state_sha256": "5" * 64,
            "facts": {
                "rode_nt1a_connected": True,
                "rode_nt1a_motu_input": "input-1",
                "motu_phantom_48v": "on",
                "motu_input_gain_reference": "mark 10",
            },
            "error": None,
        }
        laboratory = {
            "state_path": str(self.base / "lab.json"),
            "state_sha256": "6" * 64,
            "resolved": ["voice-level-measurement"],
            "invalidated": {},
            "receipt_sha256": {"voice-level-measurement": "7" * 64},
            "error": None,
        }
        source = {
            "identity": self.source_identity(),
            "identity_sha256": MODULE.canonical_sha256(self.source_identity()),
            "error": None,
        }
        with (
            mock.patch.object(MODULE, "_physical_projection", return_value=(physical, [])),
            mock.patch.object(MODULE, "_laboratory_projection", return_value=(laboratory, [])),
            mock.patch.object(MODULE, "_source_projection", return_value=(source, [])),
            mock.patch.object(MODULE, "contract_bindings", return_value=[{"path": "x", "sha256": "9" * 64}]),
            mock.patch.object(MODULE, "parecord_binding", return_value={"launcher": "/usr/bin/parecord"}),
        ):
            return MODULE.build_plan(
                "take-01.wav",
                60,
                output_root=self.output,
                state_root=self.state,
                disk_usage_fn=lambda _path: types.SimpleNamespace(free=free),
            )

    def test_catalog_and_byte_budget_are_consistent(self) -> None:
        contract = MODULE.load_catalog()
        capture = contract["capture"]
        self.assertEqual(capture["sample_format"], "s32le")
        self.assertEqual(
            MODULE.maximum_file_bytes(capture, 10),
            48_000 * 2 * 4 * 10 + 1_048_576,
        )

    def test_plain_filename_rejects_paths_hidden_files_and_controls(self) -> None:
        for invalid in ("../take.wav", ".take.wav", "take", "take\n.wav", " /tmp.wav"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(MODULE.RecordingError):
                    MODULE._plain_wav_name(invalid)
        self.assertEqual(MODULE._plain_wav_name("Stimme 01.wav"), "Stimme 01.wav")

    def test_private_directory_creation_allows_root_owned_parents(self) -> None:
        nested = self.base / "a" / "b" / "c"
        result = MODULE.ensure_private_directory(nested)
        self.assertEqual(result, nested)
        self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o700)

    def test_private_directory_rejects_symlink_component(self) -> None:
        real = self.base / "real"
        real.mkdir()
        link = self.base / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(MODULE.RecordingError):
            MODULE.ensure_private_directory(link / "child")

    def test_plan_is_ready_and_digest_is_stable_for_same_bound_inputs(self) -> None:
        first = self.ready_plan()
        second = self.ready_plan()
        self.assertTrue(first["ready"])
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(first["readiness"]["blockers"], [])
        self.assertFalse(self.state.exists())

    def test_source_projection_ignores_volatile_observation_metadata(self) -> None:
        identity = self.source_identity()
        contract = MODULE.load_catalog()["source"]
        first, first_blockers = MODULE._source_projection(
            contract,
            lambda: {
                "complete": True,
                "identity": identity,
                "observed_at": "2026-07-31T10:00:00+00:00",
                "observation_sha256": "a" * 64,
            },
        )
        second, second_blockers = MODULE._source_projection(
            contract,
            lambda: {
                "complete": True,
                "identity": identity,
                "observed_at": "2026-07-31T10:00:01+00:00",
                "observation_sha256": "b" * 64,
            },
        )
        self.assertEqual(first_blockers, [])
        self.assertEqual(second_blockers, [])
        self.assertEqual(first, second)
        self.assertNotIn("observation_sha256", first)
        self.assertEqual(first["identity_sha256"], MODULE.canonical_sha256(identity))

    def test_plan_blocks_low_space_existing_output_and_active_pointer(self) -> None:
        low = self.ready_plan(free=1)
        self.assertIn("free-space-insufficient", low["readiness"]["blockers"])
        (self.output / "take-01.wav").write_bytes(b"occupied")
        occupied = self.ready_plan()
        self.assertIn("output-already-exists", occupied["readiness"]["blockers"])
        (self.output / "take-01.wav").unlink()
        self.state.mkdir(mode=0o700)
        (self.state / "active.json").write_text("{}")
        active = self.ready_plan()
        self.assertIn(
            "active-session-requires-status-or-recovery",
            active["readiness"]["blockers"],
        )

    def test_plan_preserves_physical_laboratory_and_source_blockers(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_physical_projection",
                return_value=({"state_sha256": None}, ["physical-fact:rode_nt1a_connected"]),
            ),
            mock.patch.object(
                MODULE,
                "_laboratory_projection",
                return_value=({"state_sha256": None}, ["laboratory-gate:voice-level-measurement"]),
            ),
            mock.patch.object(
                MODULE,
                "_source_projection",
                return_value=({"identity": None}, ["motu-source-not-unique"]),
            ),
            mock.patch.object(MODULE, "contract_bindings", return_value=[]),
            mock.patch.object(MODULE, "parecord_binding", return_value={}),
        ):
            plan = MODULE.build_plan(
                "take.wav",
                10,
                output_root=self.output,
                state_root=self.state,
                disk_usage_fn=lambda _path: types.SimpleNamespace(free=20_000_000_000),
            )
        self.assertFalse(plan["ready"])
        self.assertEqual(
            plan["readiness"]["blockers"],
            [
                "laboratory-gate:voice-level-measurement",
                "motu-source-not-unique",
                "physical-fact:rode_nt1a_connected",
            ],
        )

    def test_atomic_private_json_is_mode_0600_and_create_only(self) -> None:
        path = self.state / "receipt.json"
        MODULE._atomic_private_json(path, {"value": 1}, create_only=True)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(MODULE._safe_json_read(path, require_private=True), {"value": 1})
        with self.assertRaises(MODULE.RecordingError):
            MODULE._atomic_private_json(path, {"value": 2}, create_only=True)

    def test_json_payload_and_digest_share_one_bound_snapshot(self) -> None:
        path = self.state / "bound.json"
        payload = {"alpha": 1, "beta": [2, 3]}
        MODULE._atomic_private_json(path, payload, create_only=True)
        observed, binding = MODULE._safe_json_read_with_binding(
            path, require_private=True
        )
        self.assertEqual(observed, payload)
        self.assertEqual(
            binding["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.assertEqual(binding["bytes"], path.stat().st_size)

    def test_laboratory_projection_binds_exact_physical_snapshot(self) -> None:
        gate = "voice-level-measurement"
        receipt = {
            "evidence": {"kind": "bound"},
            "physical_state_sha256": "b" * 64,
        }
        state = {"gates": {gate: receipt}}
        with (
            mock.patch.object(
                MODULE,
                "_read_optional_state",
                return_value=(state, {"sha256": "c" * 64}),
            ),
            mock.patch.object(
                MODULE.LAB,
                "load_catalog",
                return_value={gate: {"binds_physical_state": True}},
            ),
            mock.patch.object(
                MODULE.LAB, "has_bound_voice_capture", return_value=True
            ),
        ):
            projection, blockers = MODULE._laboratory_projection(
                self.base / "laboratory.json",
                {"state_sha256": "a" * 64},
                [gate],
            )
        self.assertEqual(blockers, [f"laboratory-gate:{gate}"])
        self.assertEqual(projection["invalidated"][gate], "physical-state-changed")
        self.assertEqual(projection["state_sha256"], "c" * 64)

    def test_live_preconditions_reject_source_identity_drift(self) -> None:
        physical = {"state_path": str(self.base / "physical.json")}
        laboratory = {"state_path": str(self.base / "laboratory.json")}
        planned_source = {"identity": {"fingerprint": "planned"}}
        changed_source = {"identity": {"fingerprint": "changed"}}
        spec = {
            "plan_identity": {
                "physical": physical,
                "laboratory": laboratory,
                "source": planned_source,
                "output": {
                    "root": str(self.output),
                    "path": str(self.output / "take.wav"),
                },
                "capture": {
                    "maximum_file_bytes": 1_000_000,
                    "free_space_reserve_bytes": 1_000_000,
                },
            }
        }
        contract = {
            "required_physical_facts": {},
            "required_laboratory_gates": ["voice-level-measurement"],
            "source": {},
        }
        with (
            mock.patch.object(MODULE, "load_catalog", return_value=contract),
            mock.patch.object(
                MODULE, "_physical_projection", return_value=(physical, [])
            ),
            mock.patch.object(
                MODULE, "_laboratory_projection", return_value=(laboratory, [])
            ),
            mock.patch.object(
                MODULE, "_source_projection", return_value=(changed_source, [])
            ),
        ):
            with self.assertRaisesRegex(
                MODULE.RecordingError, "MOTU source identity changed"
            ):
                MODULE._validate_live_preconditions(spec)

    def write_wave(self, path: pathlib.Path, frames: int = 1000) -> None:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(4)
            handle.setframerate(48_000)
            handle.writeframes(b"\0" * frames * 2 * 4)
        path.chmod(0o600)

    def capture_contract(self, maximum_seconds: int = 60) -> dict[str, object]:
        return {
            "sample_rate_hz": 48_000,
            "channels": 2,
            "maximum_duration_seconds": maximum_seconds,
            "maximum_file_bytes": 48_000 * 2 * 4 * maximum_seconds + 1_048_576,
        }

    def test_wave_validation_and_no_replace_publication(self) -> None:
        partial = self.output / ".partial.wav"
        final = self.output / "final.wav"
        self.write_wave(partial)
        artifact = MODULE._validate_recorded_wave(partial, self.capture_contract())
        self.assertEqual(artifact["sample_rate_hz"], 48_000)
        self.assertEqual(artifact["bit_depth_container"], 32)
        MODULE._publish_no_replace(partial, final, artifact)
        self.assertFalse(partial.exists())
        self.assertTrue(final.is_file())
        self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o600)

    def test_publication_never_overwrites_and_preserves_partial(self) -> None:
        partial = self.output / ".partial.wav"
        final = self.output / "final.wav"
        self.write_wave(partial)
        artifact = MODULE._validate_recorded_wave(partial, self.capture_contract())
        final.write_bytes(b"existing")
        with self.assertRaises(MODULE.RecordingError):
            MODULE._publish_no_replace(partial, final, artifact)
        self.assertTrue(partial.exists())
        self.assertEqual(final.read_bytes(), b"existing")

    def test_publication_rejects_partial_changed_after_validation(self) -> None:
        partial = self.output / ".changed.partial.wav"
        final = self.output / "changed.wav"
        self.write_wave(partial)
        artifact = MODULE._validate_recorded_wave(partial, self.capture_contract())
        with partial.open("ab") as handle:
            handle.write(b"changed")
        with self.assertRaises(MODULE.RecordingError):
            MODULE._publish_no_replace(partial, final, artifact)
        self.assertTrue(partial.exists())
        self.assertFalse(final.exists())

    def test_process_identity_detects_mutation(self) -> None:
        identity = MODULE._proc_identity(os.getpid())
        self.assertIsNotNone(identity)
        self.assertTrue(MODULE._identity_matches(identity))
        changed = dict(identity)
        changed["start_ticks"] += 1
        self.assertFalse(MODULE._identity_matches(changed))

    def test_exact_termination_stops_only_the_bound_process(self) -> None:
        process = subprocess.Popen(
            [os.sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            identity = MODULE._proc_identity(process.pid)
            self.assertIsNotNone(identity)
            changed = dict(identity)
            changed["start_ticks"] += 1
            self.assertTrue(
                MODULE._terminate_exact_process(changed, grace_seconds=0.1)
            )
            self.assertIsNone(process.poll())
            self.assertTrue(
                MODULE._terminate_exact_process(identity, grace_seconds=1.0)
            )
            process.wait(timeout=5)
            self.assertIsNone(MODULE._proc_identity(process.pid))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_recovery_preserves_partial_and_clears_active_pointer(self) -> None:
        MODULE.ensure_private_directory(self.state)
        session_id = "a" * 24
        paths = MODULE._session_paths(self.state, session_id)
        partial = self.output / ".take.partial.wav"
        partial.write_bytes(b"partial bytes")
        partial.chmod(0o600)
        final = self.output / "take.wav"
        plan_identity = {
            "capture": {"maximum_file_bytes": 1_048_576},
        }
        spec = {
            "schema_version": 1,
            "kind": "audio_recording_session_spec",
            "session_id": session_id,
            "created_at": "2026-07-31T00:00:00+00:00",
            "plan_sha256": MODULE.canonical_sha256(plan_identity),
            "plan_identity": plan_identity,
            "source_name": "redacted",
            "paths": {
                "partial": str(partial),
                "final": str(final),
                "result": str(paths["result"]),
            },
        }
        MODULE._atomic_private_json(paths["spec"], spec, create_only=True)
        spec_sha = MODULE._safe_regular_binding(paths["spec"], require_private=True)["sha256"]
        state = {
            "schema_version": 1,
            "kind": "audio_recording_session_state",
            "session_id": session_id,
            "spec_sha256": spec_sha,
            "started_at": "2026-07-31T00:00:00+00:00",
            "process": {
                "pid": 999_999_999,
                "start_ticks": 1,
                "executable": "/missing",
                "cmdline_sha256": "0" * 64,
                "process_group": 999_999_999,
            },
        }
        MODULE._atomic_private_json(paths["state"], state, create_only=True)
        MODULE._atomic_private_json(
            paths["active"],
            {
                "schema_version": 1,
                "kind": "audio_recording_active",
                "session_id": session_id,
                "spec_sha256": spec_sha,
            },
            create_only=True,
        )
        status = MODULE.recover_session(state_root=self.state)
        self.assertEqual(status["status"], "failed-preserved")
        self.assertFalse(paths["active"].exists())
        self.assertTrue(partial.exists())
        result = MODULE._safe_json_read(paths["result"], require_private=True)
        self.assertEqual(result["reason"], "worker-exited-without-terminal-receipt")
        self.assertIsNotNone(result["partial"])

    def test_parecord_binding_covers_launcher_and_resolved_binary(self) -> None:
        binding = MODULE.parecord_binding()
        self.assertEqual(binding["launcher"], "/usr/bin/parecord")
        self.assertEqual(binding["resolved"]["path"], "/usr/bin/pacat")
        self.assertEqual(len(binding["resolved"]["sha256"]), 64)

    def test_worker_cleanly_stops_fake_recorder_and_publishes_wav(self) -> None:
        fake = self.base / "fake-parecord"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import signal, sys, time, wave\n"
            "stop = False\n"
            "def handler(_signum, _frame):\n"
            "    global stop\n"
            "    stop = True\n"
            "signal.signal(signal.SIGINT, handler)\n"
            "output = sys.argv[-1]\n"
            "with wave.open(output, 'wb') as handle:\n"
            "    handle.setnchannels(2)\n"
            "    handle.setsampwidth(4)\n"
            "    handle.setframerate(48000)\n"
            "    handle.writeframes(b'\\0' * 4800 * 2 * 4)\n"
            "while not stop:\n"
            "    time.sleep(0.02)\n"
        )
        fake.chmod(0o755)
        partial = self.output / ".worker.partial.wav"
        final = self.output / "worker.wav"
        capture = {
            "sample_rate_hz": 48_000,
            "channels": 2,
            "maximum_duration_seconds": 1,
            "maximum_file_bytes": 2_000_000,
            "startup_timeout_seconds": 2,
            "stop_grace_seconds": 2,
        }
        plan_identity = {
            "capture": capture,
            "parecord": {"resolved": {"path": str(fake)}},
        }
        spec = {
            "session_id": "b" * 24,
            "source_name": "fake-source",
            "plan_sha256": MODULE.canonical_sha256(plan_identity),
            "plan_identity": plan_identity,
            "paths": {
                "partial": str(partial),
                "final": str(final),
                "result": str(self.state / "unused.json"),
            },
        }
        spec_path = self.base / "spec.json"
        spec_path.write_text(json.dumps(spec))
        result_path = self.base / "worker-result.json"
        helper = (
            "import importlib.util,json,pathlib,sys;"
            "module_path,spec_path,_fake_path,result_path=sys.argv[1:];"
            "s=importlib.util.spec_from_file_location('recording_worker_integration',module_path);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "payload=json.loads(pathlib.Path(spec_path).read_text());"
            "result=m.worker_run(payload,validate_spec=False);"
            "pathlib.Path(result_path).write_text(json.dumps(result))"
        )
        completed = subprocess.run(
            [
                os.sys.executable,
                "-c",
                helper,
                str(ROOT / "scripts/recording_session.py"),
                str(spec_path),
                str(fake),
                str(result_path),
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(result_path.read_text())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reason"], "maximum-duration")
        self.assertTrue(final.is_file())
        self.assertFalse(partial.exists())
        self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o600)


    def test_start_persists_recoverable_state_before_spawn_failure(self) -> None:
        plan = self.ready_plan()
        source_name = "fake-source"
        plan["identity"]["source"]["identity"]["node_name_sha256"] = (
            hashlib.sha256(source_name.encode()).hexdigest()
        )
        plan["plan_sha256"] = "a" * 64
        with (
            mock.patch.object(MODULE, "build_plan", return_value=plan),
            mock.patch.object(
                MODULE.VOICE, "_source_name_from_live_query", return_value=source_name
            ),
            mock.patch.object(
                MODULE.subprocess, "Popen", side_effect=OSError("spawn failed")
            ),
        ):
            with self.assertRaises(MODULE.RecordingError) as context:
                MODULE.start_session(
                    "take-01.wav",
                    60,
                    plan["plan_sha256"],
                    output_root=self.output,
                    state_root=self.state,
                )
        self.assertIn("recover session", str(context.exception))
        active = json.loads((self.state / "active.json").read_text())
        session_id = active["session_id"]
        state_path = MODULE._session_paths(self.state, session_id)["state"]
        stored = json.loads(state_path.read_text())
        self.assertEqual(stored["phase"], "starting")
        self.assertIsNone(stored["process"])
        recovered = MODULE.recover_session(
            state_root=self.state, session_id=session_id
        )
        self.assertEqual(recovered["status"], "failed-preserved")
        self.assertFalse((self.state / "active.json").exists())

    def test_start_timeout_uses_exact_termination_and_remains_recoverable(self) -> None:
        plan = self.ready_plan()
        source_name = "fake-source"
        plan["identity"]["source"]["identity"]["node_name_sha256"] = (
            hashlib.sha256(source_name.encode()).hexdigest()
        )
        plan["identity"]["capture"]["startup_timeout_seconds"] = 0
        plan["plan_sha256"] = "b" * 64
        identity = {
            "pid": 43210,
            "start_time_ticks": 98765,
            "executable": "/usr/bin/python3",
            "process_group": 43210,
        }

        class FakeProcess:
            pid = 43210

            @staticmethod
            def poll():
                return None

        with (
            mock.patch.object(MODULE, "build_plan", return_value=plan),
            mock.patch.object(
                MODULE.VOICE, "_source_name_from_live_query", return_value=source_name
            ),
            mock.patch.object(MODULE.subprocess, "Popen", return_value=FakeProcess()),
            mock.patch.object(MODULE, "_proc_identity", return_value=identity),
            mock.patch.object(
                MODULE, "_terminate_exact_process", return_value=True
            ) as terminate,
        ):
            with self.assertRaises(MODULE.RecordingError) as context:
                MODULE.start_session(
                    "take-01.wav",
                    60,
                    plan["plan_sha256"],
                    output_root=self.output,
                    state_root=self.state,
                )
        self.assertIn("recover session", str(context.exception))
        terminate.assert_called_once_with(
            identity,
            grace_seconds=float(
                plan["identity"]["capture"]["stop_grace_seconds"]
            ),
        )
        active = json.loads((self.state / "active.json").read_text())
        session_id = active["session_id"]
        state_path = MODULE._session_paths(self.state, session_id)["state"]
        stored = json.loads(state_path.read_text())
        self.assertEqual(stored["phase"], "running")
        self.assertEqual(stored["process"], identity)
        recovered = MODULE.recover_session(
            state_root=self.state, session_id=session_id
        )
        self.assertEqual(recovered["status"], "failed-preserved")
        self.assertFalse((self.state / "active.json").exists())


if __name__ == "__main__":
    unittest.main()
