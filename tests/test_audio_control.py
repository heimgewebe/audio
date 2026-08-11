import hashlib
import contextlib
import http.client
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from html.parser import HTMLParser
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audio_control", ROOT / "scripts" / "audio_control.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SurfaceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.routes = []
        self.route_labels = []
        self.inline_handlers = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        if "data-route" in attributes:
            self.routes.append(attributes["data-route"])
            self.route_labels.append(attributes.get("aria-label"))
        self.inline_handlers.extend(
            key for key in attributes if key == "style" or key.startswith("on")
        )


class StubTelemetryCollector(MODULE.LIVE_TELEMETRY.Collector):
    """A harmless collector so telemetry lifecycle tests spawn no subprocess."""

    name = "stub"
    stream_id = "stub-stream"
    label = "Stub"
    interval_seconds = 0.05

    def __init__(self):
        self.calls = 0

    def sample(self, context):
        self.calls += 1
        return {"tick": self.calls}


def stub_telemetry_hub():
    return MODULE.LIVE_TELEMETRY.TelemetryHub([StubTelemetryCollector()])


class InMemorySocket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, mode, _buffering=None):
        if mode != "rb":
            raise AssertionError(f"unexpected socket mode: {mode}")
        return self.input

    def sendall(self, data):
        self.output.write(data)


class InMemoryServer:
    def __init__(self, controller, port=8765):
        self.controller = controller
        self.server_port = port


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.doctor_calls = 0
        self.whale_active = False
        self.whale_mode = None

    def result(self, argv, payload, returncode=0):
        return MODULE.CommandResult(
            tuple(argv),
            returncode,
            json.dumps(payload),
            "",
        )

    def run(self, argv, *, timeout):
        self.calls.append((tuple(argv), timeout))
        executable = pathlib.Path(argv[0]).name
        script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
        if executable == "git":
            return MODULE.CommandResult(
                tuple(argv),
                0,
                "a" * 40 + "\n",
                "",
            )
        if script == "audio_doctor.py":
            self.doctor_calls += 1
            return self.result(
                argv,
                {
                    "schema_version": 1,
                    "kind": "audio_doctor_report",
                    "read_only_contract": True,
                    "hardware": {"motu_m2": True, "roland_fp_30x": True},
                    "device_truth": {
                        "desired": {"motu_m2": True, "roland_fp_30x": True}
                    },
                    "external_endpoints": {},
                    "graph": {
                        "default_sink": "motu-m2",
                        "default_source": "motu-m2",
                        "force_rate_hz": 48000,
                        "force_quantum_frames": 1024,
                    },
                    "warnings": [
                        {
                            "code": "high-live-quantum",
                            "severity": "medium",
                            "detail": "Stability profile.",
                        }
                    ],
                    "physical_unknowns": ["motu_phantom_48v"],
                    "command_health": [
                        {
                            "command": "wpctl status",
                            "available": True,
                            "returncode": 0,
                        }
                    ],
                },
            )
        if script == "profile_planner.py":
            profile = argv[2]
            apply_authority = next(
                item["apply_authority"]
                for item in MODULE.read_profiles()
                if item["id"] == profile
            )
            return self.result(
                argv,
                {
                    "schema_version": 1,
                    "kind": "audio_profile_plan",
                    "profile": profile,
                    "read_only": True,
                    "ready_for_laboratory_apply": False,
                    "apply_authority": apply_authority,
                    "planned_blocker": None,
                    "readiness_blockers": [],
                    "missing_hardware": [],
                    "missing_physical_facts": [],
                    "unresolved_laboratory_gates": [],
                    "proposed_changes": [],
                },
            )
        if script == "recording_product.py":
            operation = argv[2]
            if operation == "probe":
                return self.result(
                    argv,
                    {
                        "schema_version": 1,
                        "kind": "audio_recording_product_probe",
                        "status": "idle",
                        "active_session_id": None,
                        "session": None,
                        "read_only": True,
                    },
                )
            if operation == "library":
                return self.result(
                    argv,
                    {
                        "schema_version": 1,
                        "kind": "audio_recording_product_library",
                        "items": [],
                        "count": 0,
                        "active_count": 0,
                        "trashed_count": 0,
                        "skipped_invalid": 0,
                        "truncated": False,
                        "read_only": True,
                    },
                )
        if script == "audio-record" and argv[2] == "init":
            return self.result(argv, {"schema_version": 1, "status": "ready"})
        if script == "whale_live.py":
            operation = argv[2]
            if operation == "status":
                return self.result(
                    argv,
                    {
                        "unit": "audio-buckelwal-live-voice-v1.service",
                        "load_state": "loaded" if self.whale_active else "not-found",
                        "active_state": "active" if self.whale_active else "inactive",
                        "sub_state": "running" if self.whale_active else "dead",
                        "voice_mode": self.whale_mode,
                        "midi_port": "24:0" if self.whale_active else None,
                        "target": None,
                        "latency_frames": 128 if self.whale_active else None,
                        "runtime_max_seconds": 21600 if self.whale_active else None,
                    },
                )
            if operation == "start":
                self.whale_active = True
                self.whale_mode = argv[4]
                return self.result(
                    argv,
                    {"state": "ready", "voice_mode": self.whale_mode},
                )
            if operation == "mode":
                self.whale_active = True
                self.whale_mode = argv[3]
                return self.result(
                    argv,
                    {"state": "ready", "voice_mode": self.whale_mode},
                )
            if operation == "stop":
                self.whale_active = False
                return self.result(argv, {"state": "stopped"})
        raise AssertionError(f"unexpected command: {argv!r}")


class SequenceSystemdRunner:
    def __init__(self):
        self.calls = []
        self.status_calls = 0

    def run(self, argv, *, timeout):
        self.calls.append((tuple(argv), timeout))
        if argv[:3] == ["systemctl", "--user", "show"]:
            self.status_calls += 1
            if self.status_calls == 1:
                stdout = (
                    "LoadState=not-found\n"
                    "ActiveState=inactive\n"
                    "SubState=dead\n"
                    "Result=success\n"
                    "ExecMainStatus=0\n"
                    "Environment=\n"
                )
            else:
                stdout = (
                    "LoadState=loaded\n"
                    "ActiveState=active\n"
                    "SubState=running\n"
                    "Result=success\n"
                    "ExecMainStatus=0\n"
                    "Environment=AUDIO_CONTROL_HOST=127.0.0.1 "
                    "AUDIO_CONTROL_PORT=8765 "
                    "AUDIO_CONTROL_MANAGED_BY=audio-control-ui-v1\n"
                )
            return MODULE.CommandResult(tuple(argv), 0, stdout, "")
        script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
        if script == "audio-record" and argv[2] == "init":
            return MODULE.CommandResult(tuple(argv), 0, "{}", "")
        if argv[0] == "systemd-run":
            return MODULE.CommandResult(tuple(argv), 0, "", "")
        raise AssertionError(f"unexpected command: {argv!r}")


class UnconfirmedActionRunner(FakeRunner):
    def run(self, argv, *, timeout):
        script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
        if script == "whale_live.py" and argv[2] == "start":
            self.calls.append((tuple(argv), timeout))
            return self.result(
                argv,
                {"state": "ready", "voice_mode": argv[4]},
            )
        return super().run(argv, timeout=timeout)


class PartialModeFailureRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.whale_active = True
        self.whale_mode = "realistic"

    def run(self, argv, *, timeout):
        script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
        if script == "whale_live.py" and argv[2] == "mode":
            self.calls.append((tuple(argv), timeout))
            self.whale_active = False
            self.whale_mode = None
            return self.result(
                argv,
                {"error": "Ersatzstart gescheitert."},
                returncode=1,
            )
        return super().run(argv, timeout=timeout)


class UnreadableActionReadbackRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.fail_next_status = False

    def run(self, argv, *, timeout):
        script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
        if script == "whale_live.py" and argv[2] == "status" and self.fail_next_status:
            self.calls.append((tuple(argv), timeout))
            self.fail_next_status = False
            return self.result(
                argv,
                {"error": "Status-Readback gescheitert."},
                returncode=1,
            )
        result = super().run(argv, timeout=timeout)
        if script == "whale_live.py" and argv[2] == "start":
            self.fail_next_status = True
        return result


class MalformedWhaleStatusRunner(FakeRunner):
    def run(self, argv, *, timeout):
        script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
        if script == "whale_live.py" and argv[2] == "status":
            self.calls.append((tuple(argv), timeout))
            return self.result(argv, {"active_state": "inactive"})
        return super().run(argv, timeout=timeout)


class TransitionalWhaleStatusRunner(FakeRunner):
    def run(self, argv, *, timeout):
        result = super().run(argv, timeout=timeout)
        script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
        if script == "whale_live.py" and argv[2] == "status":
            payload = json.loads(result.stdout)
            payload.update(
                {
                    "load_state": "loaded",
                    "active_state": "activating",
                    "sub_state": "start",
                    "voice_mode": "morph",
                }
            )
            return self.result(argv, payload)
        return result


class IncompleteDoctorRunner(FakeRunner):
    def run(self, argv, *, timeout):
        executable = pathlib.Path(argv[0]).name
        script = pathlib.Path(argv[1]).name if len(argv) > 1 else ""
        if executable != "git" and script == "audio_doctor.py":
            self.calls.append((tuple(argv), timeout))
            self.doctor_calls += 1
            return self.result(
                argv,
                {
                    "schema_version": 1,
                    "kind": "audio_doctor_report",
                    "read_only_contract": True,
                },
            )
        return super().run(argv, timeout=timeout)


class StopSystemdRunner:
    def __init__(self, *, managed=True):
        self.calls = []
        self.status_calls = 0
        self.managed = managed

    def run(self, argv, *, timeout):
        self.calls.append((tuple(argv), timeout))
        if argv[:3] == ["systemctl", "--user", "show"]:
            self.status_calls += 1
            active = self.status_calls < 3
            stdout = (
                "LoadState=loaded\n"
                f"ActiveState={'active' if active else 'inactive'}\n"
                f"SubState={'running' if active else 'dead'}\n"
                "Result=success\n"
                "ExecMainStatus=0\n"
                "Environment=AUDIO_CONTROL_HOST=127.0.0.1 "
                "AUDIO_CONTROL_PORT=8765"
                + (
                    " AUDIO_CONTROL_MANAGED_BY=audio-control-ui-v1"
                    if self.managed
                    else ""
                )
                + "\n"
            )
            return MODULE.CommandResult(tuple(argv), 0, stdout, "")
        if argv[:3] == ["systemctl", "--user", "stop"]:
            return MODULE.CommandResult(tuple(argv), 0, "", "")
        raise AssertionError(f"unexpected command: {argv!r}")


class MismatchedSystemdRunner(SequenceSystemdRunner):
    def run(self, argv, *, timeout):
        result = super().run(argv, timeout=timeout)
        if argv[:3] == ["systemctl", "--user", "show"] and self.status_calls > 1:
            return MODULE.CommandResult(
                result.argv,
                result.returncode,
                result.stdout.replace(
                    "AUDIO_CONTROL_PORT=8765", "AUDIO_CONTROL_PORT=9999"
                ),
                result.stderr,
            )
        return result


class AudioControlTests(unittest.TestCase):
    def controller(self, runner=None):
        return MODULE.AudioControl(
            runner=runner or FakeRunner(),
            action_token="test-token",
            cache_seconds=4,
            clock=lambda: 100.0,
        )

    def test_release_marker_preserves_archived_commit_identity(self):
        commit = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / ".audio-control-release.json"
            marker.write_text(json.dumps({"commit": commit}), encoding="utf-8")
            runner = mock.Mock()
            with mock.patch.object(MODULE, "RELEASE_MARKER", marker):
                self.assertEqual(MODULE.current_revision(runner), commit)
            runner.run.assert_not_called()

    def test_invalid_release_marker_fails_closed_without_git_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / ".audio-control-release.json"
            marker.write_text(json.dumps({"commit": "NOT-A-COMMIT"}), encoding="utf-8")
            runner = mock.Mock()
            with mock.patch.object(MODULE, "RELEASE_MARKER", marker):
                self.assertEqual(MODULE.current_revision(runner), "unavailable")
            runner.run.assert_not_called()

    def test_snapshot_keeps_backend_authority_and_fail_closed_capabilities(self):
        controller = self.controller()
        snapshot = controller.snapshot(refresh=True)
        self.assertEqual(snapshot["kind"], "audio_control_snapshot")
        self.assertEqual(snapshot["service"]["authority"], "local-backend")
        self.assertFalse(snapshot["service"]["browser_audio_authority"])
        self.assertEqual(snapshot["service"]["action_token"], "test-token")
        self.assertEqual(snapshot["repository"]["runtime_head"], "a" * 40)
        self.assertEqual(
            snapshot["repository"]["spec_base_revision"],
            MODULE.SPEC_BASE_REVISION,
        )
        self.assertFalse(snapshot["capabilities"]["profile_apply"])
        self.assertTrue(snapshot["capabilities"]["recording_control"])
        self.assertFalse(snapshot["capabilities"]["dauersong_control"])
        self.assertEqual(snapshot["recording"]["status"], "idle")
        self.assertEqual(snapshot["recording"]["contract"]["profile"], "voice-recording")
        self.assertEqual(snapshot["recording"]["contract"]["source"]["kind"], "motu-voice")
        self.assertEqual(snapshot["recording"]["contract"]["monitoring"]["mode"], "hardware-direct")
        recording_modes = {
            mode["id"]: mode for mode in snapshot["recording"]["modes"]
        }
        self.assertEqual(recording_modes["voice"]["label"], "Nur Gesang")
        self.assertEqual(recording_modes["piano-vocal"]["label"], "Klavier + Gesang")
        self.assertTrue(recording_modes["voice"]["actionable"])
        self.assertFalse(recording_modes["piano-vocal"]["actionable"])
        self.assertEqual(
            recording_modes["piano-vocal"]["blocker"],
            "exact-midi-gate-requires-plan",
        )
        self.assertEqual(snapshot["dauersong"]["status"], "planned-not-executable")
        self.assertEqual(len(snapshot["profiles"]), 10)
        self.assertFalse(snapshot["summary"]["active_whale"])
        self.assertEqual(snapshot["whale"]["contract"]["default_mode"], "morph")
        self.assertEqual(snapshot["whale"]["contract"]["keyboard"]["key_count"], 88)
        self.assertEqual(
            snapshot["whale"]["contract"]["keyboard"]["midi_note_range"],
            [21, 108],
        )
        self.assertEqual(
            {mode["id"] for mode in snapshot["whale"]["contract"]["modes"]},
            {"morph", "organic", "realistic", "ufo"},
        )

        self.assertEqual(snapshot["deployment"]["status"], "source-checkout")
        self.assertFalse(snapshot["deployment"]["automatic"])
        self.assertEqual(snapshot["summary"]["runtime_state"], "healthy")
        self.assertEqual(snapshot["summary"]["profile_state_counts"]["planned"], 1)
        profiles = {profile["id"]: profile for profile in snapshot["profiles"]}
        self.assertEqual(profiles["desktop-mixed"]["dashboard_state"], "plan-ready")
        self.assertEqual(profiles["voice-recording"]["dashboard_state"], "onsite")
        self.assertEqual(profiles["production"]["dashboard_state"], "planned")

    def test_absent_desired_hardware_is_onsite_truth_not_runtime_failure(self):
        controller = self.controller()
        doctor = {
            "warnings": [
                {
                    "code": "voice-source-not-motu",
                    "severity": "high",
                    "detail": "MOTU is not selected.",
                }
            ],
            "physical_unknowns": [],
            "hardware": {"motu_m2": False, "roland_fp_30x": False},
            "device_truth": {"desired": {"motu_m2": True, "roland_fp_30x": True}},
            "graph": {},
            "external_endpoints": {},
            "command_health": [],
            "read_only_contract": True,
        }
        with (
            mock.patch.object(controller, "_doctor", return_value=("ok", doctor, None)),
            mock.patch.object(
                controller,
                "_whale_status",
                return_value=("ok", {"active": False}, None),
            ),
        ):
            snapshot = controller.snapshot(refresh=True)
        self.assertEqual(snapshot["presence"]["state"], "offline")
        self.assertTrue(snapshot["presence"]["onsite_required"])
        self.assertEqual(snapshot["summary"]["state"], "stable")
        self.assertEqual(snapshot["summary"]["runtime_state"], "healthy")
        self.assertEqual(snapshot["summary"]["onsite_warning_count"], 1)
        self.assertEqual(snapshot["summary"]["runtime_high_warning_count"], 0)
        self.assertEqual(
            snapshot["summary"]["operational_state"], "ready-onsite-required"
        )

    def test_voice_source_warning_remains_runtime_attention_when_motu_is_present(self):
        controller = self.controller()
        doctor = {
            "warnings": [
                {
                    "code": "voice-source-not-motu",
                    "severity": "high",
                    "detail": "MOTU is present but not selected.",
                }
            ],
            "physical_unknowns": [],
            "hardware": {"motu_m2": True, "roland_fp_30x": False},
            "device_truth": {"desired": {"motu_m2": True, "roland_fp_30x": True}},
            "graph": {},
            "external_endpoints": {},
            "command_health": [],
            "read_only_contract": True,
        }
        with (
            mock.patch.object(controller, "_doctor", return_value=("ok", doctor, None)),
            mock.patch.object(
                controller,
                "_whale_status",
                return_value=("ok", {"active": False}, None),
            ),
        ):
            snapshot = controller.snapshot(refresh=True)
        self.assertEqual(snapshot["presence"]["state"], "partial")
        self.assertEqual(snapshot["summary"]["state"], "attention")
        self.assertEqual(snapshot["summary"]["runtime_high_warning_count"], 1)
        self.assertEqual(snapshot["summary"]["onsite_warning_count"], 0)

    def test_missing_roland_blocks_only_performance_recording_mode(self):
        controller = self.controller()
        doctor = {
            "warnings": [],
            "physical_unknowns": [],
            "hardware": {"motu_m2": True, "roland_fp_30x": False},
            "device_truth": {"desired": {"motu_m2": True, "roland_fp_30x": True}},
            "graph": {},
            "external_endpoints": {},
            "command_health": [],
            "read_only_contract": True,
        }
        with (
            mock.patch.object(controller, "_doctor", return_value=("ok", doctor, None)),
            mock.patch.object(
                controller, "_whale_status", return_value=("ok", {"active": False}, None)
            ),
        ):
            snapshot = controller.snapshot(refresh=True)
        modes = {mode["id"]: mode for mode in snapshot["recording"]["modes"]}
        self.assertTrue(modes["voice"]["actionable"])
        self.assertFalse(modes["piano-vocal"]["actionable"])
        self.assertEqual(
            modes["piano-vocal"]["blocker"], "roland-midi-source-not-observed"
        )

    def test_deployment_projection_is_bounded_current_and_path_free(self):
        commit = "c" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            marker = root / ".audio-control-release.json"
            latest = root / "latest.json"
            marker.write_text(json.dumps({"commit": commit}), encoding="utf-8")
            latest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "audio_control_deploy_receipt",
                        "commit": commit,
                        "changed": False,
                        "deployed_at_unix": 1_700_000_000,
                        "source_repo": "/private/source",
                        "deployment_repository": "/private/deploy",
                        "service": {"health": {"status": "serving"}},
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(MODULE, "RELEASE_MARKER", marker),
                mock.patch.object(MODULE, "DEPLOY_LATEST", latest),
            ):
                projection = MODULE.deployment_projection(commit)
        self.assertEqual(projection["status"], "current")
        self.assertTrue(projection["in_sync"])
        self.assertEqual(projection["receipt_commit"], commit)
        self.assertEqual(projection["source_ref"], "origin/main")
        serialized = json.dumps(projection)
        self.assertNotIn("/private", serialized)
        self.assertNotIn("source_repo", serialized)
        self.assertNotIn("deployment_repository", serialized)

    def test_deployment_projection_rejects_foreign_receipt_schema(self):
        commit = "d" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            marker = root / ".audio-control-release.json"
            latest = root / "latest.json"
            marker.write_text(json.dumps({"commit": commit}), encoding="utf-8")
            latest.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "kind": "audio_control_deploy_receipt",
                        "commit": commit,
                        "service": {"health": {"status": "serving"}},
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(MODULE, "RELEASE_MARKER", marker),
                mock.patch.object(MODULE, "DEPLOY_LATEST", latest),
            ):
                projection = MODULE.deployment_projection(commit)
        self.assertEqual(projection["status"], "unavailable")
        self.assertFalse(projection["in_sync"])
        self.assertIsNone(projection["receipt_commit"])

    def test_bounded_deploy_reader_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "target.json"
            link = root / "latest.json"
            target.write_text("{}", encoding="utf-8")
            os.symlink(target, link)
            with self.assertRaisesRegex(MODULE.ControlError, "nicht lesbar"):
                MODULE.read_bounded_json_object(
                    link, label="Deploy-Beleg", maximum_bytes=128
                )

    def test_snapshot_cache_and_explicit_refresh(self):
        runner = FakeRunner()
        controller = self.controller(runner)
        first = controller.snapshot()
        second = controller.snapshot()
        self.assertIsNot(first, second)
        self.assertEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(
            first["truth_stream"]["sequence"],
            second["truth_stream"]["sequence"],
        )
        self.assertEqual(runner.doctor_calls, 1)
        controller.snapshot(refresh=True)
        self.assertEqual(runner.doctor_calls, 2)

    def test_snapshot_single_flight_rejects_cached_reads_during_readback(self):
        runner = FakeRunner()
        controller = self.controller(runner)
        controller.snapshot()
        calls_before_readback = list(runner.calls)
        self.assertTrue(controller._snapshot_lock.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(MODULE.ActionBusy, "Aktions-Readback"):
                controller.snapshot()
        finally:
            controller._snapshot_lock.release()
        self.assertEqual(runner.calls, calls_before_readback)

    def test_malformed_success_status_is_unavailable_not_inactive_truth(self):
        snapshot = self.controller(MalformedWhaleStatusRunner()).snapshot(refresh=True)
        self.assertEqual(snapshot["whale"]["status"], "unavailable")
        self.assertEqual(snapshot["whale"]["service"], {})
        self.assertEqual(snapshot["summary"]["state"], "attention")

    def test_incomplete_doctor_success_is_not_rendered_as_healthy(self):
        snapshot = self.controller(IncompleteDoctorRunner()).snapshot(refresh=True)
        self.assertEqual(snapshot["doctor"]["status"], "unavailable")
        self.assertEqual(snapshot["doctor"]["graph"], {})
        self.assertEqual(snapshot["summary"]["state"], "attention")

    def test_doctor_success_rejects_malformed_nested_entries(self):
        runner = FakeRunner()
        result = runner.run(
            [sys.executable, str(MODULE.DOCTOR_SCRIPT)],
            timeout=30,
        )
        report = json.loads(result.stdout)
        mutations = (
            ("warning", lambda value: value["warnings"].append("not-an-object")),
            (
                "physical unknown",
                lambda value: value["physical_unknowns"].append({"id": "not-text"}),
            ),
            (
                "command health",
                lambda value: value["command_health"].append(
                    {"command": "anything", "available": "yes", "returncode": 0}
                ),
            ),
            (
                "desired hardware",
                lambda value: value["device_truth"]["desired"].pop("motu_m2"),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                malformed = json.loads(json.dumps(report))
                mutate(malformed)
                with self.assertRaises(MODULE.ControlError):
                    MODULE.validate_doctor_report(malformed)

    def test_transitional_whale_status_is_not_rendered_as_inactive(self):
        snapshot = self.controller(TransitionalWhaleStatusRunner()).snapshot(
            refresh=True
        )
        self.assertEqual(snapshot["whale"]["status"], "unavailable")
        self.assertEqual(snapshot["whale"]["service"], {})
        self.assertEqual(snapshot["summary"]["state"], "attention")

    def test_whale_action_is_allowlisted_and_read_back(self):
        runner = FakeRunner()
        controller = self.controller(runner)
        result = controller.perform_whale_action(
            {"operation": "start", "mode": "realistic"}
        )
        self.assertEqual(result["result"]["state"], "ready")
        self.assertTrue(result["snapshot"]["whale"]["service"]["active"])
        self.assertEqual(
            result["snapshot"]["whale"]["service"]["voice_mode"], "realistic"
        )
        whale_call = next(call for call, _timeout in runner.calls if "start" in call)
        self.assertEqual(
            whale_call,
            (
                sys.executable,
                str(MODULE.WHALE_SCRIPT),
                "start",
                "--voice-mode",
                "realistic",
            ),
        )

    def test_whale_action_rejects_generic_fields_and_unknown_modes(self):
        controller = self.controller()
        with self.assertRaisesRegex(MODULE.ControlError, "unbekannte Felder"):
            controller.perform_whale_action(
                {
                    "operation": "start",
                    "mode": "realistic",
                    "command": "anything",
                }
            )
        with self.assertRaisesRegex(MODULE.ControlError, "nicht verfügbar"):
            controller.perform_whale_action(
                {"operation": "start", "mode": "not-a-mode"}
            )
        with self.assertRaisesRegex(MODULE.ControlError, "benötigt"):
            controller.perform_whale_action({"operation": "mode"})

    def test_whale_action_fails_closed_without_matching_status_readback(self):
        controller = self.controller(UnconfirmedActionRunner())
        with self.assertRaisesRegex(MODULE.ControlError, "nicht.*bestätigt"):
            controller.perform_whale_action({"operation": "start", "mode": "morph"})

    def test_mode_action_requires_fresh_active_status_before_dispatch(self):
        runner = FakeRunner()
        runner.whale_active = True
        runner.whale_mode = "realistic"
        controller = self.controller(runner)
        before = controller.snapshot()
        self.assertTrue(before["whale"]["service"]["active"])

        runner.whale_active = False
        runner.whale_mode = None
        with self.assertRaisesRegex(MODULE.ControlError, "bereits aktive"):
            controller.perform_whale_action({"operation": "mode", "mode": "morph"})

        self.assertFalse(
            any(
                pathlib.Path(call[1]).name == "whale_live.py" and call[2] == "mode"
                for call, _timeout in runner.calls
                if len(call) > 2
            )
        )
        after = controller.snapshot()
        self.assertFalse(after["whale"]["service"]["active"])
        self.assertEqual(runner.doctor_calls, 2)

    def test_failed_mode_action_replaces_pre_action_cache_with_fresh_truth(self):
        runner = PartialModeFailureRunner()
        controller = self.controller(runner)
        before = controller.snapshot()
        self.assertTrue(before["whale"]["service"]["active"])
        self.assertEqual(before["whale"]["service"]["voice_mode"], "realistic")

        with self.assertRaisesRegex(MODULE.ControlError, "Ersatzstart gescheitert"):
            controller.perform_whale_action({"operation": "mode", "mode": "morph"})

        after = controller.snapshot()
        self.assertFalse(after["whale"]["service"]["active"])
        self.assertEqual(runner.doctor_calls, 2)

    def test_action_readback_failure_is_cached_only_as_unavailable(self):
        runner = UnreadableActionReadbackRunner()
        controller = self.controller(runner)
        controller.snapshot()

        with self.assertRaisesRegex(MODULE.ControlError, "nicht.*bestätigt"):
            controller.perform_whale_action({"operation": "start", "mode": "morph"})

        after = controller.snapshot()
        self.assertEqual(after["whale"]["status"], "unavailable")
        self.assertEqual(after["whale"]["service"], {})
        self.assertEqual(runner.doctor_calls, 2)

    def test_failed_action_keeps_its_cause_when_readback_cannot_be_built(self):
        runner = PartialModeFailureRunner()
        controller = self.controller(runner)
        controller.snapshot()

        with mock.patch.object(
            MODULE,
            "read_profiles",
            side_effect=MODULE.ControlError("Readback-Vertrag unlesbar."),
        ) as read_profiles:
            with self.assertRaisesRegex(MODULE.ControlError, "Ersatzstart gescheitert"):
                controller.perform_whale_action({"operation": "mode", "mode": "morph"})
            read_profiles.assert_called_once_with()
            self.assertIsNone(controller._cached_snapshot)

        after = controller.snapshot()
        self.assertFalse(after["whale"]["service"]["active"])

    def test_whale_action_reserves_readback_before_mutation(self):
        runner = FakeRunner()
        controller = self.controller(runner)
        self.assertTrue(controller._snapshot_lock.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(MODULE.ActionBusy, "Aktions-Readback"):
                controller.perform_whale_action({"operation": "start", "mode": "morph"})
        finally:
            controller._snapshot_lock.release()
        self.assertFalse(runner.whale_active)
        self.assertEqual(runner.calls, [])

    def test_profile_plan_rejects_unknown_identifier_without_subprocess(self):
        runner = FakeRunner()
        controller = self.controller(runner)
        with self.assertRaisesRegex(MODULE.ControlError, "Unbekanntes"):
            controller.profile_plan("../../anything")
        self.assertEqual(runner.calls, [])

    def test_profile_plan_rejects_incomplete_success_schema(self):
        runner = FakeRunner()
        original_run = runner.run

        def incomplete_plan(argv, *, timeout):
            result = original_run(argv, timeout=timeout)
            if pathlib.Path(argv[1]).name == "profile_planner.py":
                payload = json.loads(result.stdout)
                payload.pop("readiness_blockers")
                return runner.result(argv, payload)
            return result

        runner.run = incomplete_plan
        with self.assertRaisesRegex(MODULE.ControlError, "readiness_blockers"):
            self.controller(runner).profile_plan("desktop-mixed")

    def test_profile_plans_bind_each_catalogued_apply_authority(self):
        controller = self.controller()
        for profile in MODULE.read_profiles():
            with self.subTest(profile=profile["id"]):
                report = controller.profile_plan(profile["id"])
                self.assertEqual(
                    report["apply_authority"],
                    profile["apply_authority"],
                )

    def test_profile_plan_concurrency_is_bounded(self):
        controller = self.controller()
        self.assertTrue(controller._plan_lock.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(MODULE.ActionBusy, "Profilplanung"):
                controller.profile_plan("desktop-mixed")
        finally:
            controller._plan_lock.release()

    def test_command_runner_bounds_timeout_and_captured_output(self):
        runner = MODULE.CommandRunner()
        started = time.monotonic()
        with self.assertRaisesRegex(MODULE.ControlError, "Zeitlimit"):
            runner.run(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout=0.05,
            )
        self.assertLess(time.monotonic() - started, 2)
        with self.assertRaisesRegex(MODULE.OutputLimitExceeded, "zu viele Daten"):
            runner.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os; os.write(1, b'x' * "
                        f"{MODULE.MAX_SUBPROCESS_OUTPUT_BYTES + 1})"
                    ),
                ],
                timeout=3,
            )

    def test_managed_start_has_runtime_and_resource_contract(self):
        runner = SequenceSystemdRunner()
        report = MODULE.start_managed_service(
            runner,
            host="127.0.0.1",
            port=8765,
            runtime_seconds=3600,
        )
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["managed_by"], MODULE.UNIT_MANAGED_BY)
        command = next(
            call for call, _timeout in runner.calls if call[0] == "systemd-run"
        )
        self.assertIn(
            f"AUDIO_CONTROL_MANAGED_BY={MODULE.UNIT_MANAGED_BY}",
            command,
        )
        self.assertIn("MemoryMax=134217728", command)
        self.assertIn("CPUQuota=50%", command)
        self.assertIn("TasksMax=32", command)
        self.assertIn("RuntimeMaxSec=3600", command)
        self.assertIn("LogRateLimitBurst=100", command)
        self.assertIn("NoNewPrivileges=yes", command)
        self.assertIn("ProtectSystem=strict", command)
        self.assertIn("ProtectHome=read-only", command)
        self.assertIn(
            f"ReadWritePaths={MODULE.RECORDING_OUTPUT_ROOT} {MODULE.RECORDING_STATE_ROOT}",
            command,
        )
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET", command)
        self.assertNotIn("ProtectKernelModules=yes", command)
        self.assertIn("--service-type", command)
        self.assertIn("notify", command)
        self.assertNotIn("bash", command)
        self.assertNotIn("sh", command)

    def test_managed_start_does_not_claim_ready_for_mismatched_bind(self):
        runner = MismatchedSystemdRunner()
        with (
            mock.patch.object(MODULE.time, "sleep"),
            self.assertRaisesRegex(MODULE.ControlError, "keine Laufbereitschaft"),
        ):
            MODULE.start_managed_service(
                runner,
                host="127.0.0.1",
                port=8765,
                runtime_seconds=3600,
            )

    def test_managed_stop_waits_for_inactive_readback(self):
        runner = StopSystemdRunner()
        report = MODULE.stop_managed_service(runner)
        self.assertEqual(report["state"], "stopped")
        self.assertEqual(report["active_state"], "inactive")
        self.assertEqual(runner.status_calls, 3)

    def test_managed_stop_rejects_same_name_unit_without_ownership_marker(self):
        runner = StopSystemdRunner(managed=False)
        with self.assertRaisesRegex(MODULE.ControlError, "gehört nicht"):
            MODULE.stop_managed_service(runner)
        self.assertFalse(
            any(call[:3] == ("systemctl", "--user", "stop") for call, _ in runner.calls)
        )

    def test_repository_contract_check_covers_every_view(self):
        report = MODULE.validate_repository_contract()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["whale_keyboard_keys"], 88)
        self.assertEqual(
            set(report["areas"]),
            {"home", "hoeren", "aufnehmen", "spielen", "material", "system"},
        )
        self.assertEqual(
            report["replay_scenarios"],
            ["normal", "clip", "xrun", "device-loss", "stale-telemetry", "recovery"],
        )

    def test_serve_validates_contract_before_claiming_readiness(self):
        with (
            mock.patch.object(
                MODULE,
                "validate_repository_contract",
                side_effect=MODULE.ControlError("broken contract"),
            ),
            mock.patch.object(MODULE, "AudioControlHTTPServer") as server_class,
            self.assertRaisesRegex(MODULE.ControlError, "broken contract"),
        ):
            MODULE.serve(host="127.0.0.1", port=8765, cache_seconds=4)
        server_class.assert_not_called()

    def test_host_and_origin_policy_is_loopback_only(self):
        self.assertTrue(MODULE.request_host_is_local("127.0.0.1:8765", 8765))
        self.assertTrue(MODULE.request_host_is_local("localhost:8765", 8765))
        self.assertFalse(MODULE.request_host_is_local("127.0.0.1", 8765))
        self.assertFalse(MODULE.request_host_is_local("127.0.0.2:8765", 8765))
        self.assertFalse(MODULE.request_host_is_local("user@127.0.0.1:8765", 8765))
        self.assertFalse(MODULE.request_host_is_local("evil.example:8765", 8765))
        self.assertFalse(MODULE.request_host_is_local("127.0.0.1:9000", 8765))
        self.assertTrue(MODULE.origin_is_local("http://127.0.0.1:8765", 8765))
        self.assertFalse(MODULE.origin_is_local(None, 8765))
        self.assertFalse(MODULE.origin_is_local("https://127.0.0.1:8765", 8765))
        self.assertFalse(MODULE.origin_is_local("http://evil.example:8765", 8765))
        self.assertFalse(MODULE.origin_is_local("http://127.0.0.1:8765/path", 8765))
        self.assertFalse(
            MODULE.origin_matches_request(
                "http://localhost:8765",
                "127.0.0.1:8765",
                8765,
            )
        )

    def test_static_reader_rejects_symlinks_and_unknown_names(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            target = root / "outside"
            target.write_text("private", encoding="utf-8")
            os.symlink(target, root / "index.html")
            with mock.patch.object(MODULE, "UI_ROOT", root):
                with self.assertRaisesRegex(MODULE.ControlError, "nicht verfügbar"):
                    MODULE.read_static_file("index.html")
                with self.assertRaisesRegex(MODULE.ControlError, "allowlistet"):
                    MODULE.read_static_file("unknown.html")

    def test_whale_contract_rejects_88_key_morph_drift(self):
        contract = json.loads(MODULE.WHALE_PROFILE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = pathlib.Path(temporary_directory) / "whale.json"
            for field, original in contract["voice_modes"]["morph"].items():
                with self.subTest(field=field):
                    drifted = json.loads(json.dumps(contract))
                    if isinstance(original, bool):
                        replacement = not original
                    elif isinstance(original, int):
                        replacement = original + 1
                    elif isinstance(original, list):
                        replacement = [original[0] + 1, *original[1:]]
                    else:
                        replacement = f"{original}-drift"
                    drifted["voice_modes"]["morph"][field] = replacement
                    path.write_text(json.dumps(drifted), encoding="utf-8")
                    with (
                        mock.patch.object(MODULE, "WHALE_PROFILE", path),
                        self.assertRaisesRegex(MODULE.ControlError, "88-Tasten"),
                    ):
                        MODULE.read_whale_contract()

    def test_whale_contract_rejects_organic_mode_drift(self):
        contract = json.loads(MODULE.WHALE_PROFILE.read_text(encoding="utf-8"))
        required_fields = (
            "backend",
            "base_backend",
            "manifest",
            "voice_model_manifest",
            "voice_model_manifest_sha256",
            "trajectory_count",
            "source_family_count",
            "trajectory_selection",
            "evaluation",
            "note_range",
            "tuning",
            "keyboard_slot_count",
            "preset_count",
            "control_key_count",
            "voice_count",
            "permanent_noise_layer",
            "long_phrase_playback",
            "organic_features",
            "temporal_articulation",
            "low_register",
            "maximum_additional_pitch_drift_cents",
            "comparison",
            "hold",
            "legato",
            "detached_retrigger",
            "repeated_note",
            "pitch_bend_range_semitones",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = pathlib.Path(temporary_directory) / "whale.json"
            for field in required_fields:
                with self.subTest(field=field):
                    drifted = json.loads(json.dumps(contract))
                    original = drifted["voice_modes"]["organic"][field]
                    if isinstance(original, bool):
                        replacement = not original
                    elif isinstance(original, int):
                        replacement = original + 1
                    elif isinstance(original, list):
                        replacement = [*original, "drift"]
                    else:
                        replacement = f"{original}-drift"
                    drifted["voice_modes"]["organic"][field] = replacement
                    path.write_text(json.dumps(drifted), encoding="utf-8")
                    with (
                        mock.patch.object(MODULE, "WHALE_PROFILE", path),
                        self.assertRaisesRegex(
                            MODULE.ControlError, "organischen 88-Tasten"
                        ),
                    ):
                        MODULE.read_whale_contract()

    def test_whale_contract_rejects_schema_and_truth_boundary_drift(self):
        contract = json.loads(MODULE.WHALE_PROFILE.read_text(encoding="utf-8"))
        cases = (
            ("schema_version", 2, "Schemavertrag"),
            ("kind", "foreign_profile", "Schemavertrag"),
            ("voice_model", "sample-zones", "Schemavertrag"),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = pathlib.Path(temporary_directory) / "whale.json"
            for field, value, message in cases:
                with self.subTest(field=field):
                    drifted = json.loads(json.dumps(contract))
                    drifted[field] = value
                    path.write_text(json.dumps(drifted), encoding="utf-8")
                    with (
                        mock.patch.object(MODULE, "WHALE_PROFILE", path),
                        self.assertRaisesRegex(MODULE.ControlError, message),
                    ):
                        MODULE.read_whale_contract()
            drifted = json.loads(json.dumps(contract))
            drifted["truth_boundary"]["biological_voice_model_claim"] = True
            path.write_text(json.dumps(drifted), encoding="utf-8")
            with (
                mock.patch.object(MODULE, "WHALE_PROFILE", path),
                self.assertRaisesRegex(MODULE.ControlError, "Wahrheitsgrenze"),
            ):
                MODULE.read_whale_contract()

    def test_static_surface_has_unique_ids_and_no_inline_code(self):
        parser = SurfaceParser()
        parser.feed((ROOT / "ui" / "index.html").read_text())
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertEqual(
            set(parser.routes),
            {"home", "hoeren", "aufnehmen", "spielen", "material", "system"},
        )
        self.assertEqual(parser.inline_handlers, [])
        self.assertNotIn(None, parser.route_labels)
        html = (ROOT / "ui" / "index.html").read_text()
        self.assertIn('rel="icon" href="data:,"', html)
        self.assertIn('aria-labelledby="motion-toggle-label"', html)
        self.assertIn('aria-describedby="motion-toggle-description"', html)
        self.assertIn('aria-labelledby="auto-refresh-toggle-label"', html)
        self.assertIn('aria-describedby="auto-refresh-toggle-description"', html)
        styles = (ROOT / "ui" / "styles.css").read_text()
        self.assertIn("*::before,", styles)
        self.assertIn("*::after {\n  box-sizing: border-box;", styles)
        javascript = (ROOT / "ui" / "app.js").read_text()
        self.assertNotIn("innerHTML", javascript)
        self.assertNotIn(".style.overflow", javascript)
        self.assertIn("AbortController", javascript)
        self.assertIn("keepDialogFocus", javascript)
        self.assertIn("prefersReducedMotion", javascript)
        self.assertIn("wireDepthPanels", javascript)
        self.assertIn("openDepthFocus", javascript)
        self.assertIn('fetchJson("/api/v1/replay"', javascript)
        lesson_javascript = (ROOT / "ui" / "whale-lesson.js").read_text()
        self.assertIn('fetchJson("/api/v1/whale/lesson"', lesson_javascript)
        self.assertIn('audio.addEventListener("play"', lesson_javascript)
        self.assertIn("stopLessonAudio(audio)", lesson_javascript)
        self.assertIn("trackLessonAudio(audio)", lesson_javascript)
        self.assertIn("--mint-border:", styles)
        self.assertIn("border-color: var(--mint-border);", styles)
        self.assertIn('id="whale-learning-lesson"', html)
        self.assertIn('"snapshot_busy"', javascript)
        self.assertIn("Backend beschäftigt", javascript)
        self.assertIn('fetchJson("/api/v1/actions/recording"', javascript)
        self.assertIn('fetchJson("/api/v1/actions/whale"', javascript)
        self.assertEqual(javascript.count("/api/v1/actions/"), 2)

    def test_static_surface_prioritizes_compact_functional_controls(self):
        html = (ROOT / "ui" / "index.html").read_text()
        self.assertNotIn("hero-card", html)
        self.assertNotIn("page-intro", html)
        self.assertNotIn("boundary-card", html)
        self.assertNotIn("Was möchtest du hören oder machen?", html)
        self.assertIn('id="home-metrics"', html)
        self.assertIn('id="home-readiness"', html)
        self.assertIn('id="home-actions"', html)
        self.assertIn('id="home-signal-flow"', html)
        self.assertIn('id="deployment-status"', html)
        self.assertIn('id="system-summary"', html)
        self.assertIn('class="truth-strip"', html)
        self.assertIn("data-depth-panel", html)
        self.assertIn('id="replay-scenario"', html)
        self.assertIn('id="whale-lesson-summary"', html)
        self.assertIn('data-focus-kind="whale-learning"', html)
        self.assertNotIn('data-route="diagnose"', html)
        self.assertNotIn('data-route="einstellungen"', html)

        styles = (ROOT / "ui" / "styles.css").read_text()
        self.assertNotIn(".hero-card", styles)
        self.assertNotIn(".page-intro", styles)
        self.assertIn(".overview-grid", styles)
        self.assertIn(".readiness-grid", styles)
        self.assertIn(".home-command-grid", styles)
        self.assertIn(".home-action-grid", styles)
        self.assertIn(".home-signal-flow", styles)
        self.assertIn(".signal-topology", styles)
        self.assertIn(".truth-strip", styles)
        self.assertIn(".depth-panel", styles)
        self.assertIn(".replay-grid", styles)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", styles)

        javascript = (ROOT / "ui" / "app.js").read_text()
        self.assertIn('href: "#hoeren"', javascript)
        self.assertIn('href: "#aufnehmen"', javascript)
        self.assertIn('href: "#spielen"', javascript)
        self.assertIn('byId("home-metrics")', javascript)
        self.assertIn('byId("home-readiness")', javascript)
        self.assertIn('byId("deployment-status")', javascript)
        self.assertIn('klaenge: "material"', javascript)
        self.assertIn('now: "home"', javascript)
        self.assertIn('setups: "home"', javascript)
        self.assertIn('library: "material"', javascript)
        self.assertIn('installTaskWorkspaceLayout', javascript)
        self.assertIn('diagnose: "system"', javascript)
        self.assertIn('["Rate", graph.force_rate_hz', javascript)
        self.assertIn('["Quantum",', javascript)

    def test_auto_refresh_policy_blocks_dialogs_and_audio_actions(self):
        javascript = (ROOT / "ui" / "app.js").read_text()
        policy_start = javascript.index("function autoRefreshBlocked()")
        policy_end = javascript.index("function autoRefreshTick()", policy_start)
        policy = javascript[policy_start:policy_end]
        self.assertNotIn("activeElement", policy)
        self.assertIn('!byId("dialog-backdrop").hidden', policy)
        self.assertIn("state.loading", policy)
        self.assertIn("state.interactionUntil", policy)
        self.assertIn("state.recordingActionPending", policy)
        self.assertIn("state.whaleActionPending", policy)
        self.assertIn("state.replayPlaying", policy)
        self.assertIn("recordingPlaybackActive()", policy)
        self.assertIn('document.querySelectorAll("audio.recording-player")', javascript)
        self.assertIn("!audio.paused && !audio.ended", javascript)
        self.assertIn("runWhaleAction", javascript)
        self.assertIn("function recordingLibraryActionsAllowed()", javascript)
        self.assertIn("localRecordingLibraryActionsAllowed()", javascript)
        self.assertIn("remoteRecordingLibraryActionsAllowed()", javascript)
        library_gate_start = javascript.index("function localRecordingLibraryActionsAllowed()")
        library_gate_end = javascript.index("function localWhaleActionsAllowed()", library_gate_start)
        library_gate = javascript[library_gate_start:library_gate_end]
        self.assertNotIn("recording?.actionable", library_gate)
        self.assertIn("function whaleActionsAllowed()", javascript)
        self.assertIn("directLoopbackControlOrigin()", javascript)
        self.assertIn("state.remoteBridgeProjection !== true", javascript)
        self.assertIn("state.snapshot?.capabilities?.whale_control === true", javascript)
        self.assertNotIn("WHALE_ACTION_TIMEOUT_MS", javascript)
        self.assertIn('fetchJson("/api/v1/actions/recording"', javascript)
        self.assertIn('fetchJson("/api/v1/actions/whale"', javascript)
        self.assertEqual(javascript.count("/api/v1/actions/"), 2)
        self.assertIn("state.replayPlaying", javascript)
        self.assertIn("stopReplay", javascript)

    def test_recording_shortcut_opens_authoritative_action_workspace(self):
        html = (ROOT / "ui" / "index.html").read_text()
        javascript = (ROOT / "ui" / "app.js").read_text()

        self.assertIn('id="view-aufnehmen" data-view="aufnehmen"', html)
        self.assertIn('id="recording-live-host"', html)
        self.assertIn('id="recording-recent-takes"', html)
        self.assertIn('id="library-view"', html)
        self.assertIn('id="library-category-filter"', html)
        self.assertIn('id="library-sort"', html)
        self.assertIn('"Löschen"', javascript)
        self.assertIn('"Wiederherstellen"', javascript)
        self.assertIn('window.confirm(', javascript)
        self.assertIn('id="recording-recent-title">Letzte Takes</h3>', html)
        self.assertIn('href="#material">Alle Aufnahmen</a>', html)
        self.assertEqual(html.count('id="recorder-workspace"'), 1)
        self.assertIn(
            'id="recorder-workspace" tabindex="-1" aria-label="Recorder-Arbeitsbereich"',
            html,
        )
        self.assertIn(
            'byId("recording-live-host").append(byId("now-signal-lanes"));',
            javascript,
        )
        self.assertIn('lanes.filter((lane) => lane.key === "recording")', javascript)
        recent_start = javascript.index("function renderRecordingRecentTakes()")
        recent_end = javascript.index("function captureRecorderInteraction", recent_start)
        recent = javascript[recent_start:recent_end]
        self.assertIn('item.status === "completed"', recent)
        self.assertIn(".slice(0, 3)", recent)
        self.assertIn('item.status === "completed"', recent)
        self.assertIn("audio.src = item.audio_url", recent)
        self.assertIn('"Klavier + Gesang · Stereo-Mix WAV + MIDI"', javascript)
        self.assertIn('"Stereo-Mix WAV: Gesang + echter Roland-Klang · MIDI zusätzlich"', javascript)
        self.assertIn('"Klavier: MIDI-only · Gesang WAV (Legacy-Take)"', javascript)
        self.assertIn('"WAV sichern"', javascript)
        self.assertIn('"MIDI sichern"', javascript)
        self.assertIn("renderRecordingRecentTakes();", javascript)

        controls_start = javascript.index("function renderRecordingControls(")
        controls_end = javascript.index("async function loadRecordingLibrary", controls_start)
        controls = javascript[controls_start:controls_end]
        self.assertEqual(javascript.count("renderRecordingControls("), 2)
        self.assertIn("const writable = recordingActionsAllowed();", controls)
        self.assertIn("stopButton.disabled = !writable || !active", controls)
        self.assertIn(
            'runRecordingAction({ operation: "stop", session_id: session?.session_id })',
            controls,
        )
        for label in ("Plan prüfen", "Aufnahme starten", "Stop", "Recovery"):
            with self.subTest(label=label):
                self.assertEqual(controls.count(f'"{label}"'), 1)
                self.assertNotIn(f">{label}</button>", html)

        permission_start = javascript.index("function localRecordingActionsAllowed()")
        permission_end = javascript.index("function recordingStatusLabel", permission_start)
        permission = javascript[permission_start:permission_end]
        self.assertIn("state.snapshot?.recording?.actionable === true", permission)
        self.assertIn('typeof state.snapshot?.service?.action_token === "string"', permission)
        self.assertIn("state.snapshot.service.action_token.length >= 16", permission)

        action_start = javascript.index("async function postRecordingAction(")
        action_end = javascript.index("async function runRecordingAction", action_start)
        action = javascript[action_start:action_end]
        self.assertIn("RECORDING_LIBRARY_ACTIONS.has(payload?.operation)", action)
        self.assertIn("recordingLibraryActionsAllowed()", action)
        self.assertIn("recordingActionsAllowed()", action)
        self.assertIn("if (!allowed)", action)
        self.assertIn('"X-Audio-Control-Token": state.snapshot.service.action_token', action)
        self.assertIn('"X-Audio-Bridge-Session": state.remoteWhaleSessionToken', action)
        self.assertIn('/bridge/v1/actions/recording', action)

    def test_recording_library_controls_lock_while_an_action_is_pending(self):
        javascript = (ROOT / "ui" / "app.js").read_text()
        helper_start = javascript.index("function syncRecordingLibraryControls()")
        helper_end = javascript.index("async function runRecordingStart()", helper_start)
        helper = javascript[helper_start:helper_end]
        self.assertEqual(javascript.count("syncRecordingLibraryControls();"), 3)
        start_end = javascript.index("async function runRecordingAction(", helper_end)
        start = javascript[helper_end:start_end]
        action_end = javascript.index("function renderRecordingControls(", start_end)
        action = javascript[start_end:action_end]
        for operation in (start, action):
            self.assertLess(
                operation.index("syncRecordingLibraryControls();"),
                operation.index("try {"),
            )
        library_start = javascript.index("function renderLibrary()")
        library_end = javascript.index("async function loadReplay()", library_start)
        library = javascript[library_start:library_end]
        self.assertLess(
            library.index("target.replaceChildren(...cards);"),
            library.index("syncRecordingLibraryControls();"),
        )

        harness = f"""
{helper}
const attributes = {{}};
const controls = [{{ disabled: false }}, {{ disabled: false }}, {{ disabled: false }}];
const target = {{
  setAttribute(name, value) {{ attributes[name] = value; }},
  querySelectorAll(selector) {{
    if (selector !== ".recording-category-select, .recording-take button") {{
      throw new Error(`unexpected selector: ${{selector}}`);
    }}
    return controls;
  }},
}};
const state = {{ recordingActionPending: true }};
function byId(id) {{ return id === "library-takes" ? target : null; }}
syncRecordingLibraryControls();
const pending = {{ busy: attributes["aria-busy"], disabled: controls.map((control) => control.disabled) }};
state.recordingActionPending = false;
syncRecordingLibraryControls();
const settled = {{ busy: attributes["aria-busy"], disabled: controls.map((control) => control.disabled) }};
process.stdout.write(JSON.stringify({{ pending, settled }}));
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(
            result["pending"],
            {"busy": "true", "disabled": [True, True, True]},
        )
        self.assertEqual(
            result["settled"],
            {"busy": "false", "disabled": [True, True, True]},
        )

    def test_task_workspace_focus_reuses_the_live_panel_dom(self):
        html = (ROOT / "ui" / "index.html").read_text()
        javascript = (ROOT / "ui" / "app.js").read_text()
        styles = (ROOT / "ui" / "styles.css").read_text()
        plan = (ROOT / "docs" / "plans" / "audiozentrale-task-workspaces-v1.md").read_text()

        for route in ("home", "hoeren", "aufnehmen", "spielen", "material", "system"):
            with self.subTest(route=route):
                self.assertIn(f'data-route="{route}"', html)
                self.assertIn(f'id="view-{route}"', html)
        self.assertIn("Home → Arbeitsbereich → Fokus", plan)
        self.assertIn("nicht dupliziert", plan)
        self.assertNotIn("focusLines(panel)", javascript)
        self.assertIn('panel.classList.add("is-workspace-focused")', javascript)
        self.assertIn('panel.classList.remove("is-workspace-focused")', javascript)
        self.assertIn('document.body.classList.add("workspace-focus-open")', javascript)
        self.assertIn("keepDepthFocus", javascript)
        self.assertIn('!node.closest("[hidden]")', javascript)
        self.assertIn("body.workspace-focus-open", styles)
        self.assertIn(".depth-panel.is-workspace-focused", styles)

    def test_whale_rerender_preserves_pending_mode_and_keyboard_focus(self):
        javascript = (ROOT / "ui" / "app.js").read_text()
        render_start = javascript.index("function renderWhale()")
        render_end = javascript.index("function selectedWhaleMode()", render_start)
        renderer = javascript[render_start:render_end]

        self.assertIn("whaleModeDraft: null", javascript)
        self.assertIn("state.whaleModeDraft = null", javascript)
        self.assertIn("const selectedMode = state.whaleModeDraft || currentMode", renderer)
        invalidation_start = renderer.index("if (\n    !writable ||")
        invalidation_end = renderer.index("const selectedMode", invalidation_start)
        invalidation = renderer[invalidation_start:invalidation_end]
        self.assertIn("!writable ||", invalidation)
        self.assertIn("state.whaleModeDraft = null", invalidation)
        self.assertIn("input.checked = mode.id === selectedMode", renderer)
        self.assertIn("state.whaleModeDraft = event.target.value", renderer)
        picker_end = renderer.index('const actions = element("div", "card-actions")')
        picker_prefix = renderer[:picker_end]
        self.assertIn("state.whaleModeDraft = event.target.value", picker_prefix)
        self.assertNotIn("input.disabled = state.loading ||", renderer)
        self.assertIn("const focusedMode =", renderer)
        self.assertIn("input.value === focusedMode", renderer)
        self.assertIn("replacement.focus({ preventScroll: true })", renderer)
        self.assertIn('state.route === "spielen"', renderer)

        selected_start = javascript.index("function selectedWhaleMode()")
        selected_end = javascript.index("function setWhalePending", selected_start)
        selected = javascript[selected_start:selected_end]
        self.assertIn('typeof state.whaleModeDraft === "string"', selected)

        action_start = javascript.index("async function runWhaleAction(")
        action_end = javascript.index("function detailRow", action_start)
        action = javascript[action_start:action_end]
        self.assertIn("state.whaleModeDraft = null", action)

    def test_recorder_rerender_preserves_focus_draft_and_workspace_node(self):
        html = (ROOT / "ui" / "index.html").read_text()
        javascript = (ROOT / "ui" / "app.js").read_text()
        self.assertIn('data-lane="recording" data-control="recorder-workspace"', html)

        controls_start = javascript.index("function renderRecordingControls(")
        controls_end = javascript.index("async function loadRecordingLibrary", controls_start)
        controls = javascript[controls_start:controls_end]
        for key in (
            "take-name",
            "maximum-seconds",
            "plan",
            "start",
            "stop",
            "recovery",
        ):
            with self.subTest(control=key):
                self.assertEqual(controls.count(f'dataset.control = "{key}"'), 1)
        self.assertIn('nameInput.addEventListener("input", () => invalidatePlan({ nameEdited: true }))', controls)
        self.assertIn('durationInput.addEventListener("input", () => invalidatePlan())', controls)
        self.assertIn("name: nameInput.value", controls)
        self.assertIn("automaticName: nameEdited ? false", controls)

        lanes_start = javascript.index("function renderActiveLanes(")
        lanes_end = javascript.index("function homeProfile(", lanes_start)
        lanes = javascript[lanes_start:lanes_end]
        self.assertNotIn('byId("now-signal-lanes").replaceChildren', lanes)
        self.assertIn("existingCards.get(lane.key)", lanes)
        self.assertIn("reconcileKeyedChildren(container, cards)", lanes)
        self.assertIn(
            "restoreRecorderInteraction(workspace, interaction, { preserveDraft })",
            lanes,
        )

        action_start = javascript.index("async function runRecordingAction(")
        action_end = javascript.index("function renderRecordingControls(", action_start)
        action = javascript[action_start:action_end]
        self.assertIn("renderActiveLanes({ preserveDraft: false })", action)
        self.assertIn("renderAll({ preserveRecorderDraft: false })", action)

        helpers_start = javascript.index("function captureRecorderInteraction(")
        helpers_end = javascript.index("function renderActiveLanes(", helpers_start)
        helpers = javascript[helpers_start:helpers_end]
        harness = f"""
{helpers}
const events = [];
const active = {{
  dataset: {{ control: "take-name" }},
  value: "draft-name.wav",
  selectionStart: 2,
  selectionEnd: 8,
  selectionDirection: "forward",
}};
const control = {{
  dataset: {{ control: "take-name" }},
  value: "authoritative.wav",
  disabled: false,
  closest() {{ return null; }},
  focus() {{ events.push("control-focus"); }},
  setSelectionRange(start, end, direction) {{
    events.push(["selection", start, end, direction]);
  }},
}};
const workspace = {{
  dataset: {{ control: "recorder-workspace" }},
  contains(node) {{ return node === active || node === workspace; }},
  querySelectorAll() {{ return [control]; }},
  focus() {{ events.push("workspace-focus"); }},
}};
globalThis.document = {{ activeElement: active }};
const interaction = captureRecorderInteraction(workspace);
restoreRecorderInteraction(workspace, interaction, {{ preserveDraft: true }});
const passive = {{ value: control.value, events: [...events] }};

control.value = "new-authoritative.wav";
events.length = 0;
restoreRecorderInteraction(workspace, interaction, {{ preserveDraft: false }});
const action = {{ value: control.value, events: [...events] }};

control.disabled = true;
events.length = 0;
restoreRecorderInteraction(workspace, interaction, {{ preserveDraft: true }});
const fallback = [...events];

control.disabled = false;
workspace.querySelectorAll = () => [];
events.length = 0;
restoreRecorderInteraction(workspace, interaction, {{ preserveDraft: true }});
const missing = [...events];

document.activeElement = workspace;
events.length = 0;
const workspaceInteraction = captureRecorderInteraction(workspace);
restoreRecorderInteraction(workspace, workspaceInteraction, {{ preserveDraft: true }});
const routeFocus = [...events];

let recorderRemoved = false;
const listening = {{ remove() {{ remove(this); }} }};
const playing = {{ remove() {{ remove(this); }} }};
const stale = {{ remove() {{ remove(this); }} }};
const recorder = {{ remove() {{ recorderRemoved = true; remove(this); }} }};
const parent = {{
  children: [listening, stale, recorder],
  insertBefore(child, reference) {{
    const oldIndex = this.children.indexOf(child);
    if (oldIndex >= 0) this.children.splice(oldIndex, 1);
    const index = reference === null ? this.children.length : this.children.indexOf(reference);
    this.children.splice(index, 0, child);
  }},
}};
function remove(child) {{
  const index = parent.children.indexOf(child);
  if (index >= 0) parent.children.splice(index, 1);
}}
reconcileKeyedChildren(parent, [listening, playing, recorder]);
const keyed = {{
  order: parent.children.map((child) =>
    child === listening ? "listening" : child === playing ? "playing" : "recording"
  ),
  recorderRemoved,
}};
process.stdout.write(JSON.stringify({{ passive, action, fallback, missing, routeFocus, keyed }}));
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["passive"]["value"], "draft-name.wav")
        self.assertEqual(
            result["passive"]["events"],
            ["control-focus", ["selection", 2, 8, "forward"]],
        )
        self.assertEqual(result["action"], {
            "value": "new-authoritative.wav",
            "events": ["control-focus"],
        })
        self.assertEqual(result["fallback"], ["workspace-focus"])
        self.assertEqual(result["missing"], ["workspace-focus"])
        self.assertEqual(result["routeFocus"], ["workspace-focus"])
        self.assertEqual(
            result["keyed"],
            {
                "order": ["listening", "playing", "recording"],
                "recorderRemoved": False,
            },
        )

    def test_home_view_preserves_unreadable_whale_truth(self):
        javascript = (ROOT / "ui" / "app.js").read_text()
        home_start = javascript.index("function renderHome()")
        home_end = javascript.index("function insightCard", home_start)
        home = javascript[home_start:home_end]
        self.assertIn('snapshot.whale.status === "ok"', home)
        self.assertIn('"Walstatus nicht lesbar"', home)
        self.assertIn('"Replay verfügbar · Livezustand nicht lesbar"', home)

    def test_sound_library_requires_confirmed_active_whale_truth(self):
        javascript = (ROOT / "ui" / "app.js").read_text()
        sounds_start = javascript.index("function renderSounds()")
        sounds_end = javascript.index("function soundModeDescription", sounds_start)
        sounds = javascript[sounds_start:sounds_end]
        self.assertIn(
            'whale.status === "ok" && whale.service?.active === true',
            sounds,
        )
        self.assertIn("? whale.service.voice_mode", sounds)
        self.assertIn(": null;", sounds)

    def test_live_telemetry_is_bound_to_the_service_lifetime(self):
        controller = MODULE.AudioControl(
            runner=FakeRunner(),
            action_token="test-token",
            cache_seconds=0,
            telemetry=stub_telemetry_hub(),
        )
        self.assertFalse(controller.telemetry.running)
        started = controller.start_telemetry()
        try:
            self.assertEqual(started["state"], "running")
            self.assertTrue(controller.telemetry.running)
            self.assertEqual(
                controller.telemetry.control.snapshot()["accepted_total"], 1
            )
        finally:
            stopped = controller.stop_telemetry()
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["timed_out"], 0)
        self.assertFalse(controller.telemetry.running)
        self.assertEqual(controller.stop_telemetry()["state"], "already-stopped")

    def test_broken_telemetry_never_blocks_the_control_service(self):
        controller = MODULE.AudioControl(
            runner=FakeRunner(),
            action_token="test-token",
            cache_seconds=0,
            telemetry=None,
        )
        self.assertIsNone(controller.telemetry)
        self.assertEqual(controller.start_telemetry()["state"], "unavailable")
        self.assertEqual(controller.stop_telemetry()["state"], "unavailable")
        with self.assertRaises(MODULE.ControlError):
            controller.telemetry_snapshot()
        snapshot = controller.snapshot(refresh=True)
        self.assertFalse(snapshot["capabilities"]["live_telemetry"])
        self.assertEqual(snapshot["summary"]["runtime_state"], "healthy")

        with mock.patch.object(
            MODULE.load_live_telemetry(),
            "build_default_hub",
            side_effect=RuntimeError("telemetry core is broken"),
        ):
            degraded = MODULE.AudioControl(runner=FakeRunner(), cache_seconds=0)
        self.assertIsNone(degraded.telemetry)
        self.assertEqual(degraded.start_telemetry()["state"], "unavailable")


    def test_live_telemetry_import_failure_is_lazy_and_degrades_service_validation(self):
        real_spec_from_file_location = importlib.util.spec_from_file_location

        class FailingLoader:
            def create_module(self, _spec):
                return None

            def exec_module(self, _module):
                raise SystemExit("broken optional telemetry module")

        def spec_factory(name, location, *args, **kwargs):
            if name == "audio_control_live_telemetry":
                return importlib.util.spec_from_loader(name, FailingLoader())
            return real_spec_from_file_location(name, location, *args, **kwargs)

        module_name = "audio_control_lazy_telemetry_failure_test"
        fresh_spec = real_spec_from_file_location(
            module_name, ROOT / "scripts" / "audio_control.py"
        )
        fresh = importlib.util.module_from_spec(fresh_spec)
        assert fresh_spec and fresh_spec.loader
        previous_modules = {
            name: sys.modules.get(name)
            for name in (
                module_name,
                "audio_control_telemetry_replay",
                "audio_control_whale_learning_lesson",
                "audio_control_live_telemetry",
            )
        }
        sys.modules[module_name] = fresh
        try:
            with mock.patch.object(
                importlib.util,
                "spec_from_file_location",
                side_effect=spec_factory,
            ):
                fresh_spec.loader.exec_module(fresh)
                self.assertIsNone(fresh.AudioControl._build_telemetry())
                degraded = fresh.validate_repository_contract(
                    require_live_telemetry=False
                )
                self.assertEqual(degraded["live_telemetry_streams"], [])
                self.assertEqual(degraded["live_telemetry_safety"], "unavailable")
                with self.assertRaises(fresh.ControlError):
                    fresh.validate_repository_contract()
        finally:
            for name, previous in previous_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

    def test_telemetry_snapshot_stays_passive_and_declares_its_boundary(self):
        controller = self.controller()
        telemetry = controller.telemetry_snapshot()
        self.assertEqual(telemetry["kind"], "audio_live_telemetry_snapshot")
        self.assertEqual(telemetry["authority"], "passive-observation")
        self.assertTrue(telemetry["read_only"])
        self.assertFalse(telemetry["authoritative"])
        self.assertFalse(telemetry["running"])
        self.assertEqual(
            {stream["id"] for stream in telemetry["streams"]},
            set(MODULE.LIVE_TELEMETRY.STREAM_IDS),
        )
        for key in (
            "modifies_defaults",
            "modifies_routes",
            "modifies_profiles",
            "modifies_volumes",
            "modifies_links",
        ):
            with self.subTest(key=key):
                self.assertFalse(telemetry["safety"][key])
        self.assertTrue(telemetry["control_channel"]["lossless"])
        self.assertFalse(telemetry["control_channel"]["shares_telemetry_queue"])
        self.assertEqual(self.runner_calls_for_telemetry(controller), [])

    @staticmethod
    def runner_calls_for_telemetry(controller):
        return [
            call
            for call in controller.runner.calls
            if "pw-dump" in call[0] or "pw-top" in call[0]
        ]

    def test_serve_starts_and_stops_telemetry_with_the_service(self):
        events = []

        class RecordingServer:
            def __init__(self, address, controller):
                self.controller = controller

            def __enter__(self):
                return self

            def __exit__(self, *_exception):
                return False

            def serve_forever(self, poll_interval=0.25):
                events.append(("serving", self.controller.telemetry.running))
                raise KeyboardInterrupt

        with (
            mock.patch.object(MODULE, "validate_repository_contract", return_value={}),
            mock.patch.object(MODULE, "AudioControlHTTPServer", RecordingServer),
            mock.patch.object(MODULE, "notify_systemd_ready", return_value=False),
            mock.patch.object(
                MODULE.load_live_telemetry(),
                "build_default_hub",
                side_effect=lambda **kwargs: stub_telemetry_hub(),
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            MODULE.serve(host="127.0.0.1", port=8765, cache_seconds=0)
        self.assertEqual(events, [("serving", True)])
        readiness = json.loads(output.getvalue())
        self.assertEqual(readiness["live_telemetry"], "running")

    def test_repository_contract_binds_the_passive_telemetry_boundary(self):
        report = MODULE.validate_repository_contract()
        self.assertEqual(
            set(report["live_telemetry_streams"]),
            set(MODULE.LIVE_TELEMETRY.STREAM_IDS),
        )
        self.assertEqual(report["live_telemetry_safety"], "passive-observation")

    def test_telemetry_panel_and_polling_are_bound_without_touching_controls(self):
        html = (ROOT / "ui" / "index.html").read_text()
        self.assertIn('id="live-telemetry"', html)
        self.assertIn('id="telemetry-grid"', html)
        self.assertIn('id="telemetry-authority"', html)
        self.assertIn('id="telemetry-detail"', html)

        styles = (ROOT / "ui" / "styles.css").read_text()
        self.assertIn(".telemetry-grid", styles)
        self.assertIn(".telemetry-card.is-stale", styles)
        self.assertIn('.telemetry-chip[data-availability="unavailable"]', styles)

        javascript = (ROOT / "ui" / "app.js").read_text()
        self.assertIn('fetchJson("/api/v1/telemetry"', javascript)
        self.assertIn("scheduleTelemetryPolling", javascript)
        self.assertIn("TELEMETRY_POLL_MS", javascript)
        self.assertIn("telemetryInFlight", javascript)
        self.assertIn("window.setTimeout(telemetryPollTick", javascript)
        self.assertIn("await requestTelemetry()", javascript)
        self.assertIn('document.addEventListener("visibilitychange"', javascript)
        self.assertIn("Number.isFinite", javascript)
        self.assertIn("unvollständige Beobachtung", javascript)
        telemetry_scheduler_start = javascript.index("function requestTelemetry()")
        telemetry_scheduler_end = javascript.index("function renderSounds()", telemetry_scheduler_start)
        telemetry_scheduler = javascript[telemetry_scheduler_start:telemetry_scheduler_end]
        self.assertNotIn("setInterval", telemetry_scheduler)
        loader_start = javascript.index("async function loadTelemetry()")
        loader_end = javascript.index("function requestTelemetry()", loader_start)
        loader = javascript[loader_start:loader_end]
        self.assertNotIn("showNotice", loader)
        self.assertNotIn("setLoading", loader)
        self.assertNotIn("refreshSnapshot", loader)
        self.assertIn("state.telemetryError", loader)
        renderer_start = javascript.index("function renderTelemetry()")
        renderer_end = javascript.index("async function loadTelemetry()", renderer_start)
        renderer = javascript[renderer_start:renderer_end]
        self.assertIn("state.telemetryError ||", renderer)
        self.assertIn("nicht lesbar", renderer)
        self.assertIn("ausdrücklich als veraltet", renderer)
        self.assertIn("TELEMETRY_AVAILABILITY_LABELS", javascript)
        self.assertNotIn("innerHTML", javascript)
        renderer_all_start = javascript.index("function renderAll(")
        renderer_all_end = javascript.index("function renderTruth()", renderer_all_start)
        self.assertNotIn(
            "renderTelemetry",
            javascript[renderer_all_start:renderer_all_end],
        )

    def test_telemetry_ui_separates_active_levels_from_read_only_authority(self):
        html = (ROOT / "ui" / "index.html").read_text()
        self.assertIn("Read-only-Kern · keine Steuerwirkung", html)
        self.assertIn("aktiver PipeWire-Pegelobserver", html)
        self.assertIn("geteilten PipeWire-Capture-Strom", html)
        self.assertNotIn("Passiv beobachtet · nicht wirkend", html)

        javascript = (ROOT / "ui" / "app.js").read_text()
        helpers_start = javascript.index("function hasActivePipeWireLevel(")
        helpers_end = javascript.index("function finiteTelemetryNumber(", helpers_start)
        helpers = javascript[helpers_start:helpers_end]
        harness = f"""
{helpers}
const active = {{
  id: "audio-levels",
  availability: "live",
  value: {{ source: "active-pipewire-shared-capture" }},
}};
const stale = {{ ...active, availability: "stale" }};
const passive = {{
  ...active,
  value: {{ source: "external-passive-level-file" }},
}};
process.stdout.write(JSON.stringify({{
  active: telemetryObservationSummary([active]),
  stale: telemetryObservationSummary([stale]),
  passive: telemetryObservationSummary([passive]),
  missing: telemetryObservationSummary([]),
  activeSource: telemetryLevelSourceLabel(active),
  passiveSource: telemetryLevelSourceLabel(passive),
}}));
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertIn("Pegel aktiv via PipeWire", result["active"])
        for case in ("stale", "passive", "missing"):
            with self.subTest(case=case):
                self.assertNotIn("Pegel aktiv", result[case])
                self.assertIn("read-only/ohne Steuerwirkung", result[case])
        self.assertEqual(result["activeSource"], "Quelle: PipeWire Shared Capture")
        self.assertEqual(result["passiveSource"], "Quelle: externe Pegeldatei")
        self.assertIn(
            "Telemetriekern: passive-observation · read-only · keine Steuerwirkung",
            javascript,
        )
        self.assertNotIn("passiv beobachtet", javascript)

    def test_specification_is_bound_to_exact_base_revision(self):
        text = (ROOT / "docs" / "plans" / "local-audio-control-ui-v1.md").read_text()
        self.assertIn(MODULE.SPEC_BASE_REVISION, text)
        self.assertIn("keine Digital Audio Workstation", text)
        self.assertIn("88-Tasten-Wal-Morph-Stimme", text)


class AudioControlHTTPTests(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.controller = MODULE.AudioControl(
            runner=self.runner,
            action_token="http-token",
            host="127.0.0.1",
            port=0,
            cache_seconds=0,
        )
        try:
            self.server = MODULE.AudioControlHTTPServer(
                ("127.0.0.1", 0), self.controller
            )
        except PermissionError as error:
            self.skipTest(f"Loopback-Sockets sind in dieser Sandbox gesperrt: {error}")
        self.port = self.server.server_port
        self.controller.port = self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def request(self, method, path, *, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        response_headers = dict(response.getheaders())
        status = response.status
        connection.close()
        return status, response_headers, payload

    def test_health_and_static_assets_have_security_headers(self):
        status, headers, payload = self.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        health = json.loads(payload)
        self.assertEqual(health["authority"], "local-backend")
        self.assertEqual(health["status"], "serving")
        self.assertEqual(health["runtime_head"], "a" * 40)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("object-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("media-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertNotIn("Python", headers["Server"])

        status, headers, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Audiozentrale", payload)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_http_concurrency_and_socket_io_are_explicitly_bounded(self):
        acquired = [
            self.server._request_slots.acquire(blocking=False)
            for _index in range(MODULE.MAX_CONCURRENT_REQUESTS)
        ]
        try:
            self.assertTrue(all(acquired))
            self.assertFalse(self.server._request_slots.acquire(blocking=False))
            self.assertEqual(self.server.request_queue_size, 12)
            self.assertEqual(MODULE.REQUEST_IO_TIMEOUT_SECONDS, 5.0)
        finally:
            for did_acquire in acquired:
                if did_acquire:
                    self.server._request_slots.release()

    def test_parallel_snapshot_is_rejected_without_starting_subprocesses(self):
        self.assertTrue(self.controller._snapshot_lock.acquire(blocking=False))
        try:
            status, _headers, payload = self.request(
                "GET", "/api/v1/snapshot?refresh=1"
            )
        finally:
            self.controller._snapshot_lock.release()
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(payload)["error"]["code"], "snapshot_busy")
        self.assertEqual(self.runner.calls, [])

    def test_nonlocal_host_and_static_path_traversal_are_rejected(self):
        status, _headers, _payload = self.request(
            "GET", "/api/v1/health", headers={"Host": "evil.example"}
        )
        self.assertEqual(status, 421)
        self.assertEqual(
            json.loads(_payload)["error"]["code"],
            "invalid_host",
        )
        status, _headers, _payload = self.request("GET", "/../../README.md")
        self.assertEqual(status, 404)

    def test_post_requires_token_origin_and_json(self):
        body = json.dumps({"operation": "start", "mode": "realistic"})
        base_headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{self.port}",
        }
        status, _headers, _payload = self.request(
            "POST",
            "/api/v1/actions/whale",
            body=body,
            headers=base_headers,
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.runner.whale_active)

        status, _headers, _payload = self.request(
            "POST",
            "/api/v1/actions/whale",
            body=body,
            headers={
                **base_headers,
                "Origin": "https://evil.example",
                "X-Audio-Control-Token": "http-token",
            },
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.runner.whale_active)

        status, _headers, payload = self.request(
            "POST",
            "/api/v1/actions/whale",
            body=body,
            headers={**base_headers, "X-Audio-Control-Token": "http-token"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["snapshot"]["whale"]["service"]["active"])

    def test_profile_plan_endpoint_is_read_only(self):
        status, _headers, payload = self.request(
            "GET", "/api/v1/profiles/desktop-mixed/plan"
        )
        self.assertEqual(status, 200)
        report = json.loads(payload)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["profile"], "desktop-mixed")

    def test_replay_endpoint_is_synthetic_read_only_and_rejects_query(self):
        before = list(self.runner.calls)
        status, _headers, payload = self.request("GET", "/api/v1/replay")
        self.assertEqual(status, 200)
        report = json.loads(payload)
        self.assertFalse(report["authoritative"])
        self.assertEqual(report["authority"], "synthetic-replay")
        self.assertEqual(len(report["catalog"]["scenarios"]), 6)
        self.assertEqual(self.runner.calls, before)
        status, _headers, payload = self.request(
            "GET", "/api/v1/replay?scenario=normal"
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "invalid_query")
        self.assertEqual(self.runner.calls, before)

    def test_telemetry_endpoint_is_read_only_bounded_and_query_free(self):
        before = list(self.runner.calls)
        status, headers, payload = self.request("GET", "/api/v1/telemetry")
        self.assertEqual(status, 200)
        telemetry = json.loads(payload)
        self.assertEqual(telemetry["kind"], "audio_live_telemetry_snapshot")
        self.assertEqual(telemetry["authority"], "passive-observation")
        self.assertTrue(telemetry["read_only"])
        self.assertFalse(telemetry["authoritative"])
        self.assertEqual(
            len(telemetry["streams"]), len(MODULE.LIVE_TELEMETRY.STREAM_IDS)
        )
        for stream in telemetry["streams"]:
            with self.subTest(stream=stream["id"]):
                self.assertIn(
                    stream["availability"],
                    {"starting", "live", "stale", "unavailable"},
                )
                self.assertLessEqual(stream["buffer_depth"], stream["buffer_capacity"])
                self.assertGreaterEqual(stream["sequence"], 0)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        # The endpoint only reads already collected state; it starts nothing.
        self.assertEqual(self.runner.calls, before)

        status, _headers, payload = self.request("GET", "/api/v1/telemetry?stream=xruns")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "invalid_query")
        self.assertEqual(self.runner.calls, before)

    def test_telemetry_endpoint_reports_stale_and_unavailable_explicitly(self):
        self.assertFalse(
            json.loads(self.request("GET", "/api/v1/telemetry")[2])["running"]
        )
        broken = self.controller.telemetry
        self.controller.telemetry = None
        try:
            status, _headers, payload = self.request("GET", "/api/v1/telemetry")
        finally:
            self.controller.telemetry = broken
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(payload)["error"]["code"], "telemetry_unavailable")

        class ExplodingHub:
            def snapshot(self):
                raise RuntimeError("telemetry core exploded")

        self.controller.telemetry = ExplodingHub()
        try:
            status, _headers, payload = self.request("GET", "/api/v1/telemetry")
        finally:
            self.controller.telemetry = broken
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(payload)["error"]["code"], "telemetry_unavailable")
        # A failing telemetry core must not disturb the ordinary state contract.
        status, _headers, payload = self.request("GET", "/api/v1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["kind"], "audio_control_snapshot")

    def test_telemetry_endpoint_rejects_write_methods(self):
        status, _headers, _payload = self.request(
            "POST",
            "/api/v1/telemetry",
            body="{}",
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.port}",
                "X-Audio-Control-Token": "http-token",
            },
        )
        self.assertEqual(status, 404)

    def test_whale_lesson_endpoint_and_audio_are_read_only_and_bound(self):
        before = list(self.runner.calls)
        status, headers, payload = self.request("GET", "/api/v1/whale/lesson")
        self.assertEqual(status, 200)
        lesson = json.loads(payload)
        self.assertFalse(lesson["authoritative"])
        self.assertTrue(lesson["read_only"])
        self.assertEqual(lesson["authority"], "educational-model")
        self.assertEqual(len(lesson["variants"]), 5)
        self.assertEqual(self.runner.calls, before)

        reference = lesson["variants"][0]
        status, audio_headers, audio = self.request("GET", reference["audio_url"])
        self.assertEqual(status, 200)
        self.assertEqual(audio_headers["Content-Type"], "audio/wav")
        self.assertEqual(hashlib.sha256(audio).hexdigest(), reference["audio_sha256"])
        self.assertIn("media-src 'self'", audio_headers["Content-Security-Policy"])
        self.assertEqual(audio_headers["Accept-Ranges"], "bytes")
        self.assertEqual(self.runner.calls, before)

        status, range_headers, partial = self.request(
            "GET",
            reference["audio_url"],
            headers={"Range": "bytes=0-1023"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(partial, audio[:1024])
        self.assertEqual(range_headers["Accept-Ranges"], "bytes")
        self.assertEqual(
            range_headers["Content-Range"],
            f"bytes 0-1023/{len(audio)}",
        )
        self.assertEqual(int(range_headers["Content-Length"]), 1024)

        status, suffix_headers, suffix = self.request(
            "GET",
            reference["audio_url"],
            headers={"Range": "bytes=-32"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(suffix, audio[-32:])
        self.assertEqual(
            suffix_headers["Content-Range"],
            f"bytes {len(audio) - 32}-{len(audio) - 1}/{len(audio)}",
        )

        status, head_headers, head_payload = self.request(
            "HEAD",
            reference["audio_url"],
            headers={"Range": "bytes=0-31"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(head_payload, b"")
        self.assertEqual(int(head_headers["Content-Length"]), 32)
        self.assertEqual(
            head_headers["Content-Range"],
            f"bytes 0-31/{len(audio)}",
        )

        status, invalid_headers, invalid_payload = self.request(
            "GET",
            reference["audio_url"],
            headers={"Range": f"bytes={len(audio)}-"},
        )
        self.assertEqual(status, 416)
        self.assertEqual(invalid_payload, b"")
        self.assertEqual(
            invalid_headers["Content-Range"],
            f"bytes */{len(audio)}",
        )

        status, _headers, payload = self.request(
            "GET", "/api/v1/whale/lesson?variant=morph"
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "invalid_query")
        self.assertEqual(self.runner.calls, before)


class AudioControlInMemoryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.controller = MODULE.AudioControl(
            runner=self.runner,
            action_token="memory-token",
            host="127.0.0.1",
            port=8765,
            cache_seconds=0,
        )
        self.server = InMemoryServer(self.controller)

    def request(self, method, path, *, body=b"", headers=None):
        request_headers = {
            "Host": "127.0.0.1:8765",
            "Connection": "close",
            **(headers or {}),
        }
        if body:
            request_headers["Content-Length"] = str(len(body))
        head = f"{method} {path} HTTP/1.1\r\n".encode()
        encoded_headers = b"".join(
            f"{key}: {value}\r\n".encode() for key, value in request_headers.items()
        )
        return self.raw_request(head + encoded_headers + b"\r\n" + body)

    def raw_request(self, request):
        transport = InMemorySocket(request)
        with contextlib.redirect_stderr(io.StringIO()):
            MODULE.AudioControlHandler(
                transport,
                ("127.0.0.1", 50123),
                self.server,
            )
        response_head, response_body = transport.output.getvalue().split(b"\r\n\r\n", 1)
        lines = response_head.split(b"\r\n")
        status = int(lines[0].split()[1])
        response_headers = {
            key.decode(): value.decode().strip()
            for key, value in (line.split(b":", 1) for line in lines[1:])
        }
        return status, response_headers, response_body

    def test_health_static_and_path_allowlist(self):
        status, headers, payload = self.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["authority"], "local-backend")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, _headers, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Audiozentrale", payload)
        before = list(self.runner.calls)
        status, _headers, payload = self.request("GET", "/api/v1/replay")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(payload)["authoritative"])
        self.assertEqual(self.runner.calls, before)
        status, _headers, payload = self.request("GET", "/api/v1/whale/lesson")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["authority"], "educational-model")
        self.assertEqual(self.runner.calls, before)
        status, _headers, payload = self.request("GET", "/api/v1/telemetry")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["authority"], "passive-observation")
        self.assertEqual(self.runner.calls, before)
        status, _headers, _payload = self.request("GET", "/../../README.md")
        self.assertEqual(status, 404)

    def test_nonlocal_host_is_rejected(self):
        status, _headers, _payload = self.request(
            "GET",
            "/api/v1/health",
            headers={"Host": "evil.example"},
        )
        self.assertEqual(status, 421)

    def test_valid_action_is_executed_and_read_back(self):
        body = json.dumps({"operation": "start", "mode": "realistic"}).encode()
        status, _headers, payload = self.request(
            "POST",
            "/api/v1/actions/whale",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:8765",
                "X-Audio-Control-Token": "memory-token",
            },
        )
        self.assertEqual(status, 200)
        report = json.loads(payload)
        self.assertTrue(report["snapshot"]["whale"]["service"]["active"])

    def test_action_rejects_bad_origin_token_and_content_type(self):
        body = json.dumps({"operation": "start", "mode": "realistic"}).encode()
        cases = [
            {
                "Content-Type": "application/json",
                "Origin": "https://evil.example",
                "X-Audio-Control-Token": "memory-token",
            },
            {
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:8765",
                "X-Audio-Control-Token": "bad-token",
            },
            {
                "Content-Type": "application/json",
                "X-Audio-Control-Token": "memory-token",
            },
            {
                "Content-Type": "text/plain",
                "Origin": "http://127.0.0.1:8765",
                "X-Audio-Control-Token": "memory-token",
            },
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                status, _response_headers, _payload = self.request(
                    "POST",
                    "/api/v1/actions/whale",
                    body=body,
                    headers=headers,
                )
                self.assertIn(status, {403, 415})
        self.assertFalse(self.runner.whale_active)

    def test_header_and_request_line_limits_fail_closed(self):
        oversized_header = (
            b"GET /api/v1/health HTTP/1.1\r\n"
            b"Host: 127.0.0.1:8765\r\n"
            b"X-Fill: "
            + b"x" * MODULE.MAX_HEADER_BYTES
            + b"\r\nConnection: close\r\n\r\n"
        )
        status, _headers, payload = self.raw_request(oversized_header)
        self.assertEqual(status, 431)
        self.assertEqual(
            json.loads(payload)["kind"],
            "audio_control_error",
        )

        oversized_target = (
            b"GET /"
            + b"x" * MODULE.MAX_REQUEST_LINE_BYTES
            + b" HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n\r\n"
        )
        status, _headers, _payload = self.raw_request(oversized_target)
        self.assertEqual(status, 414)

    def test_post_rejects_duplicate_headers_and_transfer_encoding(self):
        body = b'{"operation":"start","mode":"morph"}'
        requests = [
            (
                b"POST /api/v1/actions/whale HTTP/1.1\r\n"
                b"Host: 127.0.0.1:8765\r\n"
                b"Origin: http://127.0.0.1:8765\r\n"
                b"Origin: http://127.0.0.1:8765\r\n"
                b"Content-Type: application/json\r\n"
                b"X-Audio-Control-Token: memory-token\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            ),
            (
                b"POST /api/v1/actions/whale HTTP/1.1\r\n"
                b"Host: 127.0.0.1:8765\r\n"
                b"Origin: http://127.0.0.1:8765\r\n"
                b"Content-Type: application/json\r\n"
                b"X-Audio-Control-Token: memory-token\r\n"
                b"Transfer-Encoding: chunked\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            ),
        ]
        for request in requests:
            with self.subTest(request=request[:80]):
                status, _headers, payload = self.raw_request(request)
                self.assertIn(status, {400, 403})
                self.assertEqual(
                    json.loads(payload)["kind"],
                    "audio_control_error",
                )
        self.assertFalse(self.runner.whale_active)


class TelemetryTruthSeparationTests(unittest.TestCase):
    def test_slow_truth_sequence_and_cache_freshness_are_independent(self):
        now = [100.0]
        runner = FakeRunner()
        controller = MODULE.AudioControl(
            runner=runner,
            telemetry=None,
            cache_seconds=5.0,
            clock=lambda: now[0],
        )
        first = controller.snapshot()
        self.assertEqual(first["truth_stream"]["sequence"], 1)
        self.assertEqual(first["truth_stream"]["freshness"], "fresh")
        self.assertEqual(first["truth_stream"]["age_ms"], 0)

        now[0] += 1.25
        cached = controller.snapshot()
        self.assertEqual(cached["truth_stream"]["sequence"], 1)
        self.assertEqual(cached["truth_stream"]["freshness"], "cached")
        self.assertEqual(cached["truth_stream"]["age_ms"], 1250)
        self.assertEqual(cached["generated_at"], first["generated_at"])

        refreshed = controller.snapshot(refresh=True)
        self.assertEqual(refreshed["truth_stream"]["sequence"], 2)
        self.assertEqual(refreshed["truth_stream"]["freshness"], "fresh")
        self.assertEqual(refreshed["truth_stream"]["age_ms"], 0)
        self.assertEqual(runner.doctor_calls, 2)

    def test_ui_rejects_out_of_order_telemetry_and_tracks_presentation(self):
        source = (ROOT / "ui" / "app.js").read_text()
        for needle in (
            "telemetryRequestSequence: 0",
            "telemetryPresentationSequence: 0",
            "const requestId = ++state.telemetryRequestSequence",
            "requestId !== state.telemetryRequestSequence",
            "state.telemetryPresentationSequence += 1",
            "state.telemetryPresentedRequest = requestId",
            "Wahrheit Seq",
            "snapshot.truth_stream",
            "window.setTimeout(telemetryPollTick",
            "requestTelemetry().finally(() => scheduleTelemetryPolling())",
            "unvollständige Beobachtung",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, source)



if __name__ == "__main__":
    unittest.main()
