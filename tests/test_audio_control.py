import contextlib
import http.client
import importlib.util
import io
import json
import os
import pathlib
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
                    "device_truth": {},
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
        self.assertFalse(snapshot["capabilities"]["recording_control"])
        self.assertFalse(snapshot["capabilities"]["dauersong_control"])
        self.assertEqual(snapshot["recording"]["status"], "planned-not-executable")
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
            {"morph", "realistic", "ufo"},
        )

    def test_snapshot_cache_and_explicit_refresh(self):
        runner = FakeRunner()
        controller = self.controller(runner)
        first = controller.snapshot()
        second = controller.snapshot()
        self.assertIs(first, second)
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
            {
                "start",
                "spielen",
                "aufnehmen",
                "hoeren",
                "klaenge",
                "verbindungen",
                "diagnose",
                "einstellungen",
            },
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
            {
                "start",
                "spielen",
                "aufnehmen",
                "hoeren",
                "klaenge",
                "verbindungen",
                "diagnose",
                "einstellungen",
            },
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
        self.assertIn("setWhalePending(true)", javascript)
        self.assertIn('byId("whale-primary-action")?.focus', javascript)
        self.assertIn('"snapshot_busy"', javascript)
        self.assertIn("Backend beschäftigt", javascript)
        self.assertIn("/api/v1/actions/whale", javascript)

    def test_static_surface_prioritizes_compact_functional_controls(self):
        html = (ROOT / "ui" / "index.html").read_text()
        self.assertNotIn("hero-card", html)
        self.assertNotIn("page-intro", html)
        self.assertNotIn("boundary-card", html)
        self.assertNotIn("Was möchtest du hören oder machen?", html)
        self.assertIn('id="home-metrics"', html)
        self.assertIn('class="view-toolbar"', html)

        styles = (ROOT / "ui" / "styles.css").read_text()
        self.assertNotIn(".hero-card", styles)
        self.assertNotIn(".page-intro", styles)
        self.assertIn(".overview-grid", styles)
        self.assertIn(".mode-choice:has(input:focus-visible)", styles)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", styles)

        javascript = (ROOT / "ui" / "app.js").read_text()
        self.assertIn('byId("home-metrics")', javascript)
        self.assertIn('["Rate", graph.force_rate_hz', javascript)
        self.assertIn('["Quantum",', javascript)

    def test_auto_refresh_policy_does_not_treat_persistent_focus_as_interaction(self):
        javascript = (ROOT / "ui" / "app.js").read_text()
        policy_start = javascript.index("function autoRefreshBlocked()")
        policy_end = javascript.index("function autoRefreshTick()", policy_start)
        policy = javascript[policy_start:policy_end]
        self.assertNotIn("activeElement", policy)
        self.assertIn('!byId("dialog-backdrop").hidden', policy)
        self.assertIn("state.loading", policy)
        self.assertIn("state.actionPending", policy)
        self.assertIn("state.interactionUntil", policy)
        action_start = javascript.index("async function runWhaleAction")
        action_end = javascript.index("function detailRow", action_start)
        action = javascript[action_start:action_end]
        self.assertIn("const WHALE_ACTION_TIMEOUT_MS = 90000;", javascript)
        self.assertIn("timeoutMs: WHALE_ACTION_TIMEOUT_MS", action)
        self.assertNotIn("timeoutMs: 70000", action)
        self.assertIn('fetchJson("/api/v1/snapshot?refresh=1"', action)
        readback = action.index('fetchJson("/api/v1/snapshot?refresh=1"')
        self.assertLess(
            action.index("renderAll()", readback),
            action.index("showNotice(actionMessage)"),
        )
        self.assertIn('status: "unavailable"', action)
        self.assertIn("service: {}", action)

    def test_home_view_preserves_unreadable_whale_truth(self):
        javascript = (ROOT / "ui" / "app.js").read_text()
        home_start = javascript.index("function renderHome()")
        home_end = javascript.index("function insightCard", home_start)
        home = javascript[home_start:home_end]
        self.assertIn('snapshot.whale.status === "ok"', home)
        self.assertIn('"Walstatus nicht lesbar"', home)
        self.assertIn('"Zustand nicht lesbar · keine Inaktivitätsannahme"', home)

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
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("object-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("media-src 'none'", headers["Content-Security-Policy"])
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


if __name__ == "__main__":
    unittest.main()
