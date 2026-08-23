import contextlib
import io
import json
import pathlib
import sys
import unittest
from unittest import mock

from tests.test_audio_control import FakeRunner, MODULE, ROOT


class RecordingActionRunner(FakeRunner):
    PLAN_SHA = "e" * 64
    SESSION_ID = "f" * 24

    def __init__(
        self, *, malformed_terminal=False, terminal_session_type="voice-recording"
    ):
        super().__init__()
        self.malformed_terminal = malformed_terminal
        self.terminal_session_type = terminal_session_type

    def voice_plan(self, argv):
        session_type = argv[argv.index("--session-type") + 1]
        audio_identity = {
            "sample_rate_hz": 48_000,
            "sample_format": "s32le",
            "channels": 2,
        }
        source_identity = audio_identity
        performance = None
        if session_type == "piano-vocal-performance":
            source_identity = {
                "audio": audio_identity,
                "midi": {"fingerprint": "b" * 64},
            }
            performance = {
                "timing": {
                    "basis": "SMPTE",
                    "fps": 25,
                    "ticks_per_frame": 40,
                    "nominal_resolution_ms": 1,
                }
            }
        identity = {
            "session_type": session_type,
            "profile": "voice-recording",
            "output": {
                "name": argv[3],
                "mode": "0600",
                "overwrite": False,
            },
            "capture": {
                "sample_rate_hz": 48_000,
                "sample_format": "s32le",
                "channels": 2,
                "container": "wav",
                "maximum_duration_seconds": 600,
                "maximum_file_bytes": 250_000_000,
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
            "source": {
                "identity": source_identity,
                "identity_sha256": "a" * 64,
            },
            "monitoring": {
                "mode": "hardware-direct",
                "endpoint": "motu-m2",
                "software_loopback": False,
                "level_claim": "physical-reference-required",
            },
        }
        if performance is not None:
            identity["performance"] = performance
        return self.result(
            argv,
            {
                "schema_version": 1,
                "kind": "audio_recording_plan",
                "ready": True,
                "plan_sha256": self.PLAN_SHA,
                "identity": identity,
                "readiness": {
                    "blockers": [],
                    "checks": [
                        {"id": check_id, "status": "ready", "blockers": []}
                        for check_id in MODULE.RECORDING_READINESS_CHECK_IDS
                    ],
                    "free_bytes": 10_000_000_000,
                    "required_file_bytes": 250_000_000,
                    "required_free_bytes": 1_250_000_000,
                },
            },
        )

    def run(self, argv, *, timeout):
        script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
        if script == "recording_product.py" and argv[2] == "media":
            self.calls.append((tuple(argv), timeout))
            return self.result(
                argv,
                {
                    "schema_version": 1,
                    "kind": "audio_recording_product_media_binding",
                    "session_id": self.SESSION_ID,
                    "path": "/private/take.wav",
                    "sha256": "1" * 64,
                    "bytes": 123456,
                    "mode": "0600",
                    "device": 7,
                    "inode": 11,
                    "channels": 2,
                    "sample_rate_hz": 48_000,
                    "duration_seconds": 3.25,
                    "verified_current": True,
                },
            )
        if script == "recording_product.py" and argv[2] == "midi":
            self.calls.append((tuple(argv), timeout))
            return self.result(
                argv,
                {
                    "schema_version": 1,
                    "kind": "audio_recording_product_midi_binding",
                    "session_id": self.SESSION_ID,
                    "path": "/private/take.mid",
                    "sha256": "2" * 64,
                    "bytes": 4567,
                    "mode": "0600",
                    "device": 7,
                    "inode": 12,
                    "verified_current": True,
                },
            )
        if script == "audio-record":
            operation = argv[2]
            self.calls.append((tuple(argv), timeout))
            if operation == "plan":
                return self.voice_plan(argv)
            if operation == "start":
                expected_index = argv.index("--expected-plan-sha256") + 1
                session_type = argv[argv.index("--session-type") + 1]
                return self.result(
                    argv,
                    {
                        "schema_version": 1,
                        "kind": "audio_recording_start_receipt",
                        "session_id": self.SESSION_ID,
                        "session_type": session_type,
                        "status": "running",
                        "plan_sha256": argv[expected_index],
                    },
                )
            if operation in {"stop", "recover"}:
                if self.malformed_terminal:
                    return self.result(
                        argv,
                        {
                            "schema_version": 1,
                            "kind": "foreign_receipt",
                            "session_id": self.SESSION_ID,
                            "session_type": self.terminal_session_type,
                            "status": "completed",
                        },
                    )
                return self.result(
                    argv,
                    {
                        "schema_version": 1,
                        "kind": "audio_recording_status",
                        "session_id": self.SESSION_ID,
                        "session_type": self.terminal_session_type,
                        "status": "completed",
                    },
                )
        return super().run(argv, timeout=timeout)


def running_snapshot(session_id, plan_sha, session_type="voice-recording"):
    return {
        "schema_version": 1,
        "kind": "audio_control_snapshot",
        "recording": {
            "status": "running",
            "session": {
                "session_id": session_id,
                "plan_sha256": plan_sha,
                "session_type": session_type,
                "active": True,
            },
        },
    }


def stopped_snapshot(session_id, plan_sha):
    return {
        "schema_version": 1,
        "kind": "audio_control_snapshot",
        "recording": {
            "status": "completed",
            "session": {
                "session_id": session_id,
                "plan_sha256": plan_sha,
                "active": False,
                "recovery_required": False,
            },
        },
    }


class AudioControlRecordingTests(unittest.TestCase):
    def controller(self, runner=None):
        return MODULE.AudioControl(
            runner=runner or RecordingActionRunner(),
            action_token="recording-test-token",
            cache_seconds=4,
            clock=lambda: 100.0,
        )

    def test_voice_contract_binds_motu_rode_phantom_gain_level_and_monitoring(self):
        contract = MODULE.read_voice_recording_contract()
        self.assertEqual(contract["profile"], "voice-recording")
        self.assertEqual(contract["source"]["interface"], "MOTU M2")
        self.assertEqual(contract["source"]["microphone"], "RØDE NT1-A")
        self.assertEqual(contract["source"]["sample_rate_hz"], 48_000)
        self.assertEqual(contract["monitoring"]["mode"], "hardware-direct")
        self.assertFalse(contract["monitoring"]["software_loopback"])
        self.assertIn(
            "minimal-not-software-measured",
            contract["monitoring"]["latency_expectation"],
        )
        self.assertEqual(contract["required_physical_facts"]["motu_phantom_48v"], "on")
        self.assertEqual(
            contract["required_laboratory_gates"], ["voice-level-measurement"]
        )
        self.assertEqual(
            contract["levels"]["authority"], "reference-targets-not-live-measurement"
        )

    def test_recording_plan_is_typed_path_free_and_plan_hash_bound(self):
        runner = RecordingActionRunner()
        result = self.controller(runner).perform_recording_action(
            {"operation": "plan", "mode": "voice", "name": "voice-take.wav", "maximum_seconds": 600}
        )
        self.assertEqual(result["operation"], "plan")
        plan = result["plan"]
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["plan_sha256"], runner.PLAN_SHA)
        self.assertTrue(plan["source"]["bound"])
        self.assertTrue(plan["physical"]["rode_nt1a_connected"])
        self.assertEqual(plan["physical"]["motu_phantom_48v"], "on")
        self.assertTrue(plan["laboratory"]["voice_level_measurement"])
        self.assertEqual(
            [check["id"] for check in plan["readiness"]["checks"]],
            list(MODULE.RECORDING_READINESS_CHECK_IDS),
        )
        self.assertTrue(all(check["status"] == "ready" for check in plan["readiness"]["checks"]))
        serialized = json.dumps(plan)
        self.assertNotIn('"path"', serialized)
        self.assertNotIn(str(MODULE.RECORDING_OUTPUT_ROOT), serialized)
        plan_call = next(
            call for call, _timeout in runner.calls if pathlib.Path(call[1]).name == "audio-record"
        )
        self.assertIn("--session-type", plan_call)
        self.assertIn("voice-recording", plan_call)

    def test_recording_plan_rejects_inconsistent_structured_preflight(self):
        runner = RecordingActionRunner()
        original = runner.voice_plan

        def inconsistent(argv):
            result = original(argv)
            payload = json.loads(result.stdout)
            payload["readiness"]["checks"][0] = {
                "id": "output",
                "status": "blocked",
                "blockers": ["output-already-exists"],
            }
            return runner.result(argv, payload)

        runner.voice_plan = inconsistent
        with self.assertRaisesRegex(MODULE.ControlError, "Startblockern"):
            self.controller(runner).perform_recording_action(
                {
                    "operation": "plan",
                    "mode": "voice",
                    "name": "voice-take.wav",
                    "maximum_seconds": 600,
                }
            )

    def test_recording_intent_rejects_generic_fields_paths_and_bad_hash_before_effect(self):
        for name in ("../take.wav", r"nested\take.wav", "take.txt"):
            runner = RecordingActionRunner()
            with self.subTest(name=name), self.assertRaises(MODULE.ControlError):
                self.controller(runner).perform_recording_action(
                    {"operation": "plan", "mode": "voice", "name": name, "maximum_seconds": 60}
                )
            self.assertEqual(runner.calls, [])

        runner = RecordingActionRunner()
        controller = self.controller(runner)
        with self.assertRaisesRegex(MODULE.ControlError, "unbekannte"):
            controller.perform_recording_action(
                {
                    "operation": "plan",
                    "mode": "voice",
                    "name": "take.wav",
                    "maximum_seconds": 60,
                    "command": "parecord anything",
                }
            )
        self.assertEqual(runner.calls, [])

        for payload in (
            {
                "operation": "plan",
                "mode": "voice",
                "name": "take.wav",
                "maximum_seconds": 60,
                "session_type": "production-mix-recording",
            },
            {
                "operation": "plan",
                "mode": "production-mix-recording",
                "name": "take.wav",
                "maximum_seconds": 60,
            },
        ):
            with self.assertRaises(MODULE.ControlError):
                controller.perform_recording_action(payload)
        self.assertEqual(runner.calls, [])

        with self.assertRaisesRegex(MODULE.ControlError, "Plan-Hash"):
            controller.perform_recording_action(
                {
                    "operation": "start",
                    "mode": "voice",
                    "name": "take.wav",
                    "maximum_seconds": 60,
                    "expected_plan_sha256": "not-a-hash",
                }
            )
        self.assertEqual(runner.calls, [])

    def test_start_passes_expected_plan_hash_and_requires_matching_readback(self):
        runner = RecordingActionRunner()
        controller = self.controller(runner)
        with mock.patch.object(
            controller,
            "_readback_after_mutation",
            return_value=running_snapshot(runner.SESSION_ID, runner.PLAN_SHA),
        ):
            result = controller.perform_recording_action(
                {
                    "operation": "start",
                    "mode": "voice",
                    "name": "take.wav",
                    "maximum_seconds": 60,
                    "expected_plan_sha256": runner.PLAN_SHA,
                }
            )
        self.assertEqual(result["session_id"], runner.SESSION_ID)
        start_call = next(
            call
            for call, _timeout in runner.calls
            if pathlib.Path(call[1]).name == "audio-record" and call[2] == "start"
        )
        index = start_call.index("--expected-plan-sha256")
        self.assertEqual(start_call[index + 1], runner.PLAN_SHA)

        mismatch = self.controller(RecordingActionRunner())
        with mock.patch.object(
            mismatch,
            "_readback_after_mutation",
            return_value=running_snapshot("0" * 24, runner.PLAN_SHA),
        ):
            with self.assertRaisesRegex(MODULE.ControlError, "nicht.*bestätigt"):
                mismatch.perform_recording_action(
                    {
                        "operation": "start",
                        "mode": "voice",
                        "name": "take.wav",
                        "maximum_seconds": 60,
                        "expected_plan_sha256": runner.PLAN_SHA,
                    }
                )

    def test_performance_mode_maps_only_to_fixed_session_type(self):
        runner = RecordingActionRunner()
        result = self.controller(runner).perform_recording_action(
            {
                "operation": "plan",
                "mode": "piano-vocal",
                "name": "song.wav",
                "maximum_seconds": 600,
            }
        )
        self.assertEqual(result["mode"], "piano-vocal")
        self.assertEqual(result["plan"]["session_type"], "piano-vocal-performance")
        self.assertEqual(result["plan"]["performance"]["product"], "Stereo-Mix WAV + Roland MIDI")
        plan_call = next(
            call for call, _timeout in runner.calls if call[2] == "plan"
        )
        index = plan_call.index("--session-type")
        self.assertEqual(plan_call[index + 1], "piano-vocal-performance")

    def test_stop_requires_typed_status_receipt_and_inactive_readback(self):
        runner = RecordingActionRunner()
        controller = self.controller(runner)
        with mock.patch.object(
            controller,
            "_readback_after_mutation",
            return_value=stopped_snapshot(runner.SESSION_ID, runner.PLAN_SHA),
        ):
            result = controller.perform_recording_action(
                {"operation": "stop", "session_id": runner.SESSION_ID}
            )
        self.assertEqual(result["operation"], "stop")
        self.assertFalse(result["snapshot"]["recording"]["session"]["active"])
        self.assertEqual(result["verification"]["status"], "verified")
        self.assertTrue(result["verification"]["media"]["verified_current"])
        self.assertNotIn("path", result["verification"]["media"])
        self.assertIsNone(result["verification"]["midi"])

        malformed = RecordingActionRunner(malformed_terminal=True)
        malformed_controller = self.controller(malformed)
        with mock.patch.object(
            malformed_controller,
            "_readback_after_mutation",
            return_value=stopped_snapshot(malformed.SESSION_ID, malformed.PLAN_SHA),
        ):
            with self.assertRaisesRegex(MODULE.ControlError, "Statusbeleg"):
                malformed_controller.perform_recording_action(
                    {"operation": "stop", "session_id": malformed.SESSION_ID}
                )

    def test_piano_vocal_stop_verifies_both_audio_and_midi_without_paths(self):
        runner = RecordingActionRunner(
            terminal_session_type="piano-vocal-performance"
        )
        controller = self.controller(runner)
        with mock.patch.object(
            controller,
            "_readback_after_mutation",
            return_value=stopped_snapshot(runner.SESSION_ID, runner.PLAN_SHA),
        ):
            result = controller.perform_recording_action(
                {"operation": "stop", "session_id": runner.SESSION_ID}
            )
        verification = result["verification"]
        self.assertEqual(verification["status"], "verified")
        self.assertTrue(verification["media"]["verified_current"])
        self.assertTrue(verification["midi"]["verified_current"])
        self.assertNotIn("path", json.dumps(verification))
        product_commands = [
            call[0][2]
            for call in runner.calls
            if pathlib.Path(call[0][1]).name == "recording_product.py"
        ]
        self.assertEqual(product_commands, ["media", "midi"])

    def test_stop_preserves_successful_side_effect_when_current_media_verification_fails(self):
        class UnverifiedMediaRunner(RecordingActionRunner):
            def run(self, argv, *, timeout):
                script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
                if script == "recording_product.py" and argv[2] == "media":
                    self.calls.append((tuple(argv), timeout))
                    return self.result(
                        argv,
                        {
                            "schema_version": 1,
                            "kind": "audio_recording_product_error",
                            "error": "private implementation detail must not escape",
                        },
                        returncode=2,
                    )
                return super().run(argv, timeout=timeout)

        runner = UnverifiedMediaRunner()
        controller = self.controller(runner)
        with mock.patch.object(
            controller,
            "_readback_after_mutation",
            return_value=stopped_snapshot(runner.SESSION_ID, runner.PLAN_SHA),
        ):
            result = controller.perform_recording_action(
                {"operation": "stop", "session_id": runner.SESSION_ID}
            )
        self.assertEqual(result["operation"], "stop")
        self.assertEqual(result["verification"]["status"], "unverified")
        self.assertEqual(
            result["verification"]["reason"],
            "current-artifact-verification-failed",
        )
        self.assertNotIn("private implementation detail", json.dumps(result))

    def test_library_actions_are_typed_and_require_exact_metadata_readback(self):
        class LibraryActionRunner(RecordingActionRunner):
            def run(self, argv, *, timeout):
                script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
                if script == "recording_product.py" and argv[2] in {
                    "categorize",
                    "trash",
                    "restore",
                }:
                    operation = argv[2]
                    self.calls.append((tuple(argv), timeout))
                    category = (
                        argv[argv.index("--category") + 1]
                        if "--category" in argv
                        else "practice"
                    )
                    trashed = operation == "trash"
                    return self.result(
                        argv,
                        {
                            "schema_version": 1,
                            "kind": "audio_recording_library_action_result",
                            "operation": operation,
                            "session_id": self.SESSION_ID,
                            "changed": True,
                            "library": {
                                "schema_version": 1,
                                "kind": "audio_recording_library_metadata",
                                "session_id": self.SESSION_ID,
                                "category": category,
                                "trashed": trashed,
                                "updated_at": "2026-08-10T20:00:00+00:00",
                                "trashed_at": (
                                    "2026-08-10T20:00:00+00:00" if trashed else None
                                ),
                            },
                        },
                    )
                if (
                    script == "recording_product.py"
                    and argv[2] == "probe"
                    and "--session-id" in argv
                ):
                    self.calls.append((tuple(argv), timeout))
                    return self.result(
                        argv,
                        {
                            "schema_version": 1,
                            "kind": "audio_recording_product_probe",
                            "status": "completed",
                            "active_session_id": None,
                            "session": {
                                "session_id": self.SESSION_ID,
                                "session_type": "voice-recording",
                                "plan_sha256": self.PLAN_SHA,
                                "library": {
                                    "schema_version": 1,
                                    "kind": "audio_recording_library_metadata",
                                    "session_id": self.SESSION_ID,
                                    "category": "practice",
                                    "trashed": False,
                                    "updated_at": "2026-08-10T20:00:00+00:00",
                                    "trashed_at": None,
                                },
                            },
                            "read_only": True,
                        },
                    )
                return super().run(argv, timeout=timeout)

        runner = LibraryActionRunner()
        controller = self.controller(runner)
        expected_library = {
            "schema_version": 1,
            "kind": "audio_recording_library_metadata",
            "session_id": runner.SESSION_ID,
            "category": "practice",
            "trashed": False,
            "updated_at": "2026-08-10T20:00:00+00:00",
            "trashed_at": None,
        }
        with (
            mock.patch.object(
                controller,
                "_readback_after_mutation",
                return_value=stopped_snapshot(runner.SESSION_ID, runner.PLAN_SHA),
            ),
            mock.patch.object(
                controller,
                "recording_library",
                side_effect=AssertionError(
                    "mutation readback must not use the capped library listing"
                ),
            ),
        ):
            result = controller.perform_recording_action(
                {
                    "operation": "categorize",
                    "session_id": runner.SESSION_ID,
                    "category": "practice",
                }
            )
        self.assertEqual(result["operation"], "categorize")
        self.assertEqual(result["library"]["category"], "practice")
        call = next(
            call
            for call, _timeout in runner.calls
            if pathlib.Path(call[1]).name == "recording_product.py"
        )
        self.assertIn("categorize", call)
        self.assertIn("--category", call)
        probe_call = next(
            call
            for call, _timeout in runner.calls
            if pathlib.Path(call[1]).name == "recording_product.py"
            and call[2] == "probe"
        )
        self.assertEqual(
            probe_call[probe_call.index("--session-id") + 1], runner.SESSION_ID
        )

        with self.assertRaisesRegex(MODULE.ControlError, "Aufnahmekategorie"):
            controller.perform_recording_action(
                {
                    "operation": "categorize",
                    "session_id": runner.SESSION_ID,
                    "category": "arbitrary",
                }
            )

    def test_access_log_drops_request_line_and_user_controlled_content(self):
        handler = object.__new__(MODULE.AudioControlHandler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.command = "POST"
        handler.log_date_time_string = lambda: "08/Aug/2026:08:05:00 +0200"
        output = io.StringIO()
        secret = "spoken-secret-content"
        with contextlib.redirect_stderr(output):
            handler.log_message(
                '"%s" %s %s',
                f"POST /api/v1/actions/recording?utterance={secret} HTTP/1.1",
                200,
                42,
            )
        text = output.getvalue()
        self.assertNotIn(secret, text)
        self.assertNotIn("utterance", text)
        self.assertNotIn("/api/", text)
        self.assertIn("POST status=200 bytes=42", text)

    def test_browser_exposes_typed_local_audio_actions_and_remote_bridge_is_sticky(self):
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertEqual(javascript.count("/api/v1/actions/"), 4)
        self.assertIn('/api/v1/actions/recording', javascript)
        self.assertIn('/api/v1/actions/whale', javascript)
        self.assertIn('/api/v1/actions/dauersong', javascript)
        self.assertIn('/api/v1/actions/operating-mode', javascript)
        self.assertNotIn('/bridge/v1/actions/dauersong', javascript)
        self.assertIn("state.remoteBridgeProjection = true", javascript)
        self.assertIn("state.remoteBridgeProjection !== true", javascript)
        self.assertIn("state.snapshot?.capabilities?.whale_control === true", javascript)
        self.assertIn("microphone=()", (ROOT / "scripts" / "audio_control.py").read_text())

    def test_ui_mode_switch_invalidates_and_binds_the_plan(self):
        javascript = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('name: automaticTakeName("voice")', javascript)
        self.assertIn('automaticName: true', javascript)
        self.assertIn('function automaticTakeName(', javascript)
        self.assertIn('function ensureAutomaticTakeNameFree()', javascript)
        self.assertIn('RECORDING_COLLISION_BLOCKERS', javascript)
        self.assertIn('async function runRecordingStart()', javascript)
        self.assertIn('await requestRecordingPlan({ autoRenameCollision: true })', javascript)
        self.assertIn('const attemptedNames = new Set();', javascript)
        self.assertIn('attemptedNames.add(input.name);', javascript)
        self.assertIn('nextAutomaticTakeName(input.mode, attemptedNames)', javascript)
        self.assertIn('runRecordingStart();', javascript)
        self.assertIn('nameEdited ? false : state.recordingDraft.automaticName === true', javascript)
        self.assertIn('modeButton.dataset.recordingMode = mode.id', javascript)
        self.assertIn('state.recordingPlan = null', javascript)
        self.assertIn('input?.mode === state.recordingDraft.mode', javascript)
        self.assertIn('plan.session_type ===', javascript)
        self.assertIn('mode: state.recordingDraft.mode', javascript)
        self.assertIn('"Stereo-Mix WAV: Gesang + echter Roland-Klang · MIDI zusätzlich"', javascript)
        self.assertIn('state.remoteBridgeProjection !== true', javascript)
        self.assertIn('operation: "categorize"', javascript)
        self.assertIn('operation: "trash"', javascript)
        self.assertIn('operation: "restore"', javascript)


if __name__ == "__main__":
    unittest.main()
