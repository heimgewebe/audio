import datetime as dt
import importlib.util
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "plugin_host_observer", ROOT / "scripts/plugin_host_observer.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class PluginHostObserverTests(unittest.TestCase):
    def service(self, **overrides):
        value = {
            "unit": "synth.service",
            "load_state": "loaded",
            "active_state": "active",
            "sub_state": "running",
            "control_group": (
                "/user.slice/user-1000.slice/user@1000.service/"
                "app.slice/synth.service"
            ),
            "control_group_sha256": "a" * 64,
            "memory_current_bytes": 16_777_216,
            "memory_max_bytes": 268_435_456,
            "tasks_current": 4,
            "tasks_max": 32,
            "limit_nofile": 8192,
            "standard_output": "journal",
            "standard_error": "inherit",
            "log_rate_limit_interval_usec": 1_000_000,
            "log_rate_limit_burst": 100,
            "restart_count": 0,
            "query_argv_sha256": "b" * 64,
            "query_stdout_sha256": "c" * 64,
        }
        value.update(overrides)
        return value

    def process(self, **overrides):
        value = {
            "pid": 1234,
            "ppid": 1,
            "elapsed_seconds": 120,
            "executable": "fluidsynth",
            "command_sha256": "d" * 64,
            "process_start_ticks": 999,
            "unit": "synth.service",
            "cgroup": (
                "/user.slice/user-1000.slice/user@1000.service/"
                "app.slice/synth.service"
            ),
            "cgroup_sha256": "e" * 64,
            "service": self.service(),
        }
        value.update(overrides)
        return value

    def snapshot(self, process=None):
        processes = [process or self.process()]
        return {
            "processes": processes,
            "process_count": len(processes),
            "query_argv_sha256": "f" * 64,
            "query_stdout_sha256": "0" * 64,
        }

    def truth(self, marker):
        return {
            "report_sha256": marker * 64,
            "truth_chain_sha256": "1" * 64,
            "process_fingerprint": "2" * 64,
        }

    def journal_result(self, argv):
        return MODULE.SYSTEM_TRUTH.CommandResult(
            argv=argv,
            returncode=0,
            stdout="",
            stderr="",
            stdout_total_bytes=0,
            stderr_total_bytes=0,
            stdout_sha256="3" * 64,
            stderr_sha256="4" * 64,
        )

    def test_bound_observation_passes_validator(self):
        started = dt.datetime(2026, 7, 30, 8, 0, tzinfo=dt.timezone.utc)
        ended = started + dt.timedelta(seconds=60)
        argv = MODULE.LAB.plugin_host_journal_argv(
            ["synth.service"], started.isoformat(), ended.isoformat()
        )
        with (
            mock.patch.object(
                MODULE, "_truth_binding", side_effect=[self.truth("a"), self.truth("b")]
            ),
            mock.patch.object(
                MODULE,
                "process_snapshot",
                side_effect=[self.snapshot(), self.snapshot()],
            ),
            mock.patch.object(MODULE, "utc_now", side_effect=[started, ended]),
            mock.patch.object(MODULE, "monotonic_now", side_effect=[10.0, 70.0]),
            mock.patch.object(MODULE, "sleep_for") as sleep,
            mock.patch.object(
                MODULE, "_run_read_only", return_value=self.journal_result(argv)
            ),
        ):
            evidence = MODULE.managed_plugin_host_evidence(60)
        sleep.assert_called_once_with(60)
        self.assertEqual(evidence["result"], "pass")
        self.assertEqual(evidence["blockers"], [])
        self.assertTrue(evidence["managed_process"])
        self.assertTrue(evidence["bounded_resources"])
        self.assertTrue(evidence["bounded_logs"])
        MODULE.LAB.validate_evidence("managed-plugin-host-proof", evidence)
        tampered = {
            **evidence,
            "implementation": {
                **evidence["implementation"],
                "system_truth_sha256": "0" * 64,
            },
        }
        with self.assertRaisesRegex(ValueError, "implementation binding changed"):
            MODULE.LAB.validate_evidence("managed-plugin-host-proof", tampered)

    def test_unbounded_service_returns_fail_receipt(self):
        started = dt.datetime(2026, 7, 30, 8, 0, tzinfo=dt.timezone.utc)
        ended = started + dt.timedelta(seconds=60)
        unbounded = self.process(
            service=self.service(
                memory_max_bytes=None,
                tasks_max=70_351,
                limit_nofile=1_048_576,
                log_rate_limit_interval_usec=0,
                log_rate_limit_burst=0,
            )
        )
        argv = MODULE.LAB.plugin_host_journal_argv(
            ["synth.service"], started.isoformat(), ended.isoformat()
        )
        with (
            mock.patch.object(
                MODULE, "_truth_binding", side_effect=[self.truth("a"), self.truth("b")]
            ),
            mock.patch.object(
                MODULE,
                "process_snapshot",
                side_effect=[self.snapshot(unbounded), self.snapshot(unbounded)],
            ),
            mock.patch.object(MODULE, "utc_now", side_effect=[started, ended]),
            mock.patch.object(MODULE, "monotonic_now", side_effect=[10.0, 70.0]),
            mock.patch.object(MODULE, "sleep_for"),
            mock.patch.object(
                MODULE, "_run_read_only", return_value=self.journal_result(argv)
            ),
        ):
            evidence = MODULE.managed_plugin_host_evidence(60)
        self.assertEqual(evidence["result"], "fail")
        self.assertFalse(evidence["bounded_resources"])
        self.assertFalse(evidence["bounded_logs"])
        self.assertIn("before:synth.service:memory-max-unbounded", evidence["blockers"])
        self.assertIn("before:synth.service:tasks-max-too-large", evidence["blockers"])
        self.assertIn("before:synth.service:log-rate-burst-unbounded", evidence["blockers"])

    def test_legacy_receipt_is_readable_but_not_resolved(self):
        legacy = {
            "schema_version": 1,
            "kind": "managed_plugin_host_validation",
            "gate": "managed-plugin-host-proof",
            "result": "pass",
            "measured_at": "2026-07-30T08:00:00+00:00",
            "physical_state_sha256": None,
            "managed_process": True,
            "bounded_logs": True,
            "standalone_sfizz_jack": False,
            "runtime_seconds": 60,
        }
        MODULE.LAB.validate_evidence(
            "managed-plugin-host-proof",
            legacy,
            allow_legacy_plugin_host=True,
        )
        with self.assertRaisesRegex(ValueError, "legacy plugin-host"):
            MODULE.LAB.validate_evidence("managed-plugin-host-proof", legacy)
        state = MODULE.LAB.empty_state()
        state["gates"]["managed-plugin-host-proof"] = {
            "status": "passed",
            "recorded_at": "2026-07-30T08:00:00+00:00",
            "evidence_sha256": MODULE.LAB.canonical_sha256(legacy),
            "physical_state_sha256": None,
            "evidence": legacy,
        }
        resolved, invalidated = MODULE.LAB.gate_resolution(
            state, pathlib.Path("/missing-physical-state")
        )
        self.assertNotIn("managed-plugin-host-proof", resolved)
        self.assertEqual(
            invalidated["managed-plugin-host-proof"],
            "legacy-unbound-plugin-host-evidence",
        )

    def test_unmanaged_process_is_reported_as_blocker(self):
        process_line = "1234 1 60 sfizz /usr/bin/sfizz instrument.sfz\n"
        with mock.patch.object(
            MODULE, "_proc_identity", side_effect=ValueError("not service managed")
        ):
            records = MODULE._parse_processes(process_line)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["executable"], "sfizz")
        self.assertIsNone(records[0]["unit"])
        self.assertEqual(
            MODULE._service_blockers(records[0]),
            ["sfizz:unmanaged-or-unreadable"],
        )

    def test_proc_parsers_bind_start_time_and_service(self):
        stat_text = "123 (fluidsynth) S " + " ".join(str(i) for i in range(1, 30))
        self.assertEqual(MODULE._process_start_ticks(stat_text), 19)
        unit, cgroup = MODULE._service_from_cgroup(
            "0::/user.slice/user-1000.slice/user@1000.service/"
            "app.slice/fluidsynth.service\n"
        )
        self.assertEqual(unit, "fluidsynth.service")
        self.assertTrue(cgroup.endswith("/fluidsynth.service"))


if __name__ == "__main__":
    unittest.main()
