import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "qbzd_qconnect_recovery.py"
SPEC = importlib.util.spec_from_file_location("qbzd_qconnect_recovery", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

BOOT = "12345678-1234-1234-1234-123456789abc"
SERVICE_A = MODULE.QbzdService(
    pid=111,
    start_ticks=1000,
    cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/qbzd.service",
)
SERVICE_B = MODULE.QbzdService(
    pid=222,
    start_ticks=2000,
    cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/qbzd.service",
)
TRY_RESTART = ("systemctl", "--user", "try-restart", "qbzd.service")


def status(
    *,
    qconnect="retrying",
    session=False,
    auth="logged_in",
    online=True,
    backend="alsa",
    device="front:CARD=M2,DEV=0",
    present=True,
    opened=False,
    uptime=100,
):
    return MODULE.QbzdStatus(
        api_version=1,
        version="2.0.2",
        auth_state=auth,
        network_online=online,
        qconnect_state=qconnect,
        session_active=session,
        audio_backend=backend,
        configured_device=device,
        device_present=present,
        device_open=opened,
        uptime_secs=uptime,
    )


def healthy(*, uptime=200):
    return status(qconnect="connected", session=True, uptime=uptime)


class SequenceReader:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.values:
            raise AssertionError("unexpected sequence read")
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class SequenceClock(SequenceReader):
    pass


class FakeRunner:
    def __init__(self, *, fail_restart=False):
        self.commands = []
        self.fail_restart = fail_restart

    def __call__(self, argv):
        self.commands.append(argv)
        if argv == TRY_RESTART:
            if self.fail_restart:
                raise MODULE.RecoveryError("command-failed:systemctl")
            return ""
        raise AssertionError(f"unexpected command: {argv!r}")


class QbzdQconnectRecoveryTests(unittest.TestCase):
    def state_path(self, root):
        return pathlib.Path(root) / "state.json"

    def candidate_state(
        self,
        *,
        retry_since=100.0,
        service=SERVICE_A,
        next_attempt=0.0,
        failures=0,
        restart_armed=None,
    ):
        state = MODULE._default_state(BOOT)
        state.update(
            {
                "candidate_pid": service.pid,
                "candidate_start_ticks": service.start_ticks,
                "retry_since_monotonic": retry_since,
                "failures": failures,
                "next_attempt_monotonic": next_attempt,
            }
        )
        if restart_armed is not None:
            state.update(
                {
                    "restart_armed_monotonic": restart_armed,
                    "restart_armed_pid": service.pid,
                    "restart_armed_start_ticks": service.start_ticks,
                }
            )
        return state

    def reconcile(
        self,
        *,
        state_path,
        statuses,
        services,
        monotonic,
        wall=None,
        runner=None,
        pcm=None,
        boot=BOOT,
    ):
        return MODULE.reconcile_once(
            state_path=state_path,
            status_reader=SequenceReader(statuses),
            service_reader=SequenceReader(services),
            runner=runner or FakeRunner(),
            pcm_idle_checker=pcm or (lambda _service: None),
            sleeper=lambda _seconds: None,
            monotonic_clock=SequenceClock(monotonic),
            wall_clock=SequenceClock(wall or [1000.0] * 20),
            boot_id_reader=lambda: boot,
        )

    def test_classifies_expected_status_payload(self):
        parsed = MODULE.classify_status_payload(
            {
                "api_version": 1,
                "version": "2.0.2",
                "uptime_secs": 123,
                "auth": {"state": "logged_in", "user_id": 123},
                "network": {"online": True},
                "qconnect": {"state": "retrying", "session_active": False},
                "audio": {
                    "backend": "alsa",
                    "configured_device": "front:CARD=M2,DEV=0",
                    "device_present": True,
                    "device_open": False,
                },
                "playback": {"title": "private metadata is ignored"},
            }
        )
        self.assertTrue(MODULE._is_recovery_candidate(parsed))
        self.assertFalse(MODULE._is_healthy(parsed))

    def test_connected_state_never_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            runner = FakeRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[healthy()],
                services=[],
                monotonic=[200.0],
                runner=runner,
            )
            self.assertEqual(result, "noop:connected")
            self.assertEqual(runner.commands, [])
            state_data = MODULE._load_state(state_path)
            self.assertIsNone(state_data["retry_since_monotonic"])
            self.assertEqual(state_data["failures"], 0)

    def test_retrying_is_armed_to_exact_process_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            runner = FakeRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[status()],
                services=[SERVICE_A],
                monotonic=[100.0],
                runner=runner,
            )
            self.assertEqual(result, "armed")
            state_data = MODULE._load_state(state_path)
            self.assertEqual(state_data["boot_id"], BOOT)
            self.assertEqual(state_data["candidate_pid"], SERVICE_A.pid)
            self.assertEqual(
                state_data["candidate_start_ticks"], SERVICE_A.start_ticks
            )
            self.assertEqual(state_data["retry_since_monotonic"], 100.0)
            self.assertEqual(runner.commands, [])

    def test_five_minute_stability_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            result = self.reconcile(
                state_path=state_path,
                statuses=[status()],
                services=[SERVICE_A],
                monotonic=[399.9],
            )
            self.assertEqual(result, "noop:stabilizing")

    def test_process_restart_resets_five_minute_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            result = self.reconcile(
                state_path=state_path,
                statuses=[status(uptime=2)],
                services=[SERVICE_B],
                monotonic=[401.0],
            )
            self.assertEqual(result, "armed")
            state_data = MODULE._load_state(state_path)
            self.assertEqual(state_data["candidate_pid"], SERVICE_B.pid)
            self.assertEqual(state_data["retry_since_monotonic"], 401.0)

    def test_same_pid_new_start_ticks_resets_five_minute_window(self):
        replacement = MODULE.QbzdService(
            pid=SERVICE_A.pid,
            start_ticks=SERVICE_A.start_ticks + 1,
            cgroup=SERVICE_A.cgroup,
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            result = self.reconcile(
                state_path=state_path,
                statuses=[status()],
                services=[replacement],
                monotonic=[401.0],
            )
            self.assertEqual(result, "armed")
            self.assertEqual(
                MODULE._load_state(state_path)["candidate_start_ticks"],
                replacement.start_ticks,
            )

    def test_wall_clock_jump_does_not_satisfy_stability(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            result = self.reconcile(
                state_path=state_path,
                statuses=[status()],
                services=[SERVICE_A],
                monotonic=[399.0],
                wall=[10_000_000_000.0],
            )
            self.assertEqual(result, "noop:stabilizing")

    def test_boot_change_discards_old_monotonic_binding(self):
        new_boot = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            result = self.reconcile(
                state_path=state_path,
                statuses=[status()],
                services=[SERVICE_A],
                monotonic=[1000.0],
                boot=new_boot,
            )
            self.assertEqual(result, "armed")
            state_data = MODULE._load_state(state_path)
            self.assertEqual(state_data["boot_id"], new_boot)
            self.assertEqual(state_data["retry_since_monotonic"], 1000.0)

    def test_stable_idle_retry_try_restarts_and_requires_connected_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            runner = FakeRunner()
            checked = []
            result = self.reconcile(
                state_path=state_path,
                statuses=[
                    status(uptime=500),
                    status(uptime=502),
                    status(uptime=503),
                    healthy(uptime=3),
                ],
                services=[SERVICE_A, SERVICE_A, SERVICE_A, SERVICE_B],
                monotonic=[400.0, 402.0, 403.0, 403.5, 404.0],
                wall=[1000.0, 1002.0, 1003.0, 1004.0],
                runner=runner,
                pcm=lambda service: checked.append(service),
            )
            self.assertEqual(result, "recovered")
            self.assertEqual(runner.commands, [TRY_RESTART])
            self.assertEqual(checked, [SERVICE_A, SERVICE_A, SERVICE_A])
            state_data = MODULE._load_state(state_path)
            self.assertEqual(state_data["failures"], 0)
            self.assertEqual(state_data["last_recovered_at_unix"], 1004.0)
            self.assertEqual(state_data["next_attempt_monotonic"], 1304.0)
            self.assertIsNone(state_data["restart_armed_monotonic"])

    def test_restart_attempt_is_durably_armed_before_command_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            runner = FakeRunner(fail_restart=True)
            result = self.reconcile(
                state_path=state_path,
                statuses=[status(), status(), status()],
                services=[SERVICE_A, SERVICE_A, SERVICE_A],
                monotonic=[400.0, 402.0, 403.0, 403.5],
                runner=runner,
            )
            self.assertEqual(result, "blocked:command-failed:systemctl")
            state_data = MODULE._load_state(state_path)
            self.assertEqual(state_data["restart_armed_monotonic"], 403.5)
            self.assertGreaterEqual(state_data["next_attempt_monotonic"], 1303.5)

            followup_runner = FakeRunner()
            followup = self.reconcile(
                state_path=state_path,
                statuses=[status()],
                services=[SERVICE_A],
                monotonic=[430.0],
                runner=followup_runner,
            )
            self.assertEqual(followup, "noop:backoff")
            self.assertEqual(followup_runner.commands, [])

    def test_unknown_restart_outcome_then_connected_gets_full_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            state = self.candidate_state(restart_armed=403.5, next_attempt=1303.5)
            MODULE._store_state(state_path, state)
            result = self.reconcile(
                state_path=state_path,
                statuses=[healthy()],
                services=[],
                monotonic=[500.0],
                wall=[2000.0],
            )
            self.assertEqual(result, "noop:connected")
            state_data = MODULE._load_state(state_path)
            self.assertEqual(state_data["next_attempt_monotonic"], 1400.0)
            self.assertEqual(state_data["last_recovered_at_unix"], 2000.0)

    def test_failed_readback_backoff_starts_at_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            runner = FakeRunner()
            errors = [MODULE.RecoveryError("status-unavailable")] * MODULE.READBACK_ATTEMPTS
            result = self.reconcile(
                state_path=state_path,
                statuses=[status(), status(), status(), *errors],
                services=[SERVICE_A, SERVICE_A, SERVICE_A],
                monotonic=[400.0, 402.0, 403.0, 403.5, 460.0],
                runner=runner,
            )
            self.assertEqual(result, "blocked:restart-readback")
            self.assertEqual(runner.commands, [TRY_RESTART])
            state_data = MODULE._load_state(state_path)
            self.assertEqual(state_data["failures"], 1)
            self.assertEqual(state_data["next_attempt_monotonic"], 1360.0)
            self.assertEqual(state_data["restart_armed_monotonic"], 403.5)

    def test_open_audio_device_blocks_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            runner = FakeRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[status(opened=True)],
                services=[],
                monotonic=[500.0],
                runner=runner,
            )
            self.assertEqual(result, "noop:not-candidate")
            self.assertEqual(runner.commands, [])

    def test_offline_or_logged_out_state_blocks_restart(self):
        for observed in (status(online=False), status(auth="logged_out")):
            with self.subTest(observed=observed):
                with tempfile.TemporaryDirectory() as tmp:
                    runner = FakeRunner()
                    result = self.reconcile(
                        state_path=self.state_path(tmp),
                        statuses=[observed],
                        services=[],
                        monotonic=[500.0],
                        runner=runner,
                    )
                    self.assertEqual(result, "noop:not-candidate")
                    self.assertEqual(runner.commands, [])

    def test_process_change_at_final_gate_blocks_restart_and_rearms(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            runner = FakeRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[status(), status()],
                services=[SERVICE_A, SERVICE_B],
                monotonic=[400.0, 402.0],
                runner=runner,
            )
            self.assertEqual(result, "noop:qbzd-restarted")
            self.assertEqual(runner.commands, [])
            state_data = MODULE._load_state(state_path)
            self.assertEqual(state_data["candidate_pid"], SERVICE_B.pid)
            self.assertEqual(state_data["retry_since_monotonic"], 402.0)

    def test_pcm_gate_blocks_when_alsa_owner_is_qbzd_worker_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            status_path = root / "asound" / "card1" / "pcm0p" / "sub0" / "status"
            status_path.parent.mkdir(parents=True)
            status_path.write_text("state: RUNNING\nowner_pid   : 222\n", encoding="utf-8")
            owner = root / "proc" / "222"
            owner.mkdir(parents=True)
            (owner / "status").write_text("Name:\tqbzd\nTgid:\t111\n", encoding="utf-8")
            (owner / "cgroup").write_text(f"0::{SERVICE_A.cgroup}\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.RecoveryError, "qbzd-pcm-open"):
                MODULE.require_qbzd_pcm_idle(
                    SERVICE_A,
                    asound_root=root / "asound",
                    proc_root=root / "proc",
                )

    def test_pcm_gate_blocks_child_in_same_qbzd_service_cgroup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            status_path = root / "asound" / "card1" / "pcm0p" / "sub0" / "status"
            status_path.parent.mkdir(parents=True)
            status_path.write_text("state: RUNNING\nowner_pid   : 333\n", encoding="utf-8")
            owner = root / "proc" / "333"
            owner.mkdir(parents=True)
            (owner / "status").write_text("Name:\thelper\nTgid:\t333\n", encoding="utf-8")
            (owner / "cgroup").write_text(f"0::{SERVICE_A.cgroup}\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.RecoveryError, "qbzd-pcm-open"):
                MODULE.require_qbzd_pcm_idle(
                    SERVICE_A,
                    asound_root=root / "asound",
                    proc_root=root / "proc",
                )

    def test_pcm_gate_allows_other_process_but_fails_on_unreadable_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            status_path = root / "asound" / "card1" / "pcm0p" / "sub0" / "status"
            status_path.parent.mkdir(parents=True)
            status_path.write_text("state: RUNNING\nowner_pid   : 444\n", encoding="utf-8")
            owner = root / "proc" / "444"
            owner.mkdir(parents=True)
            (owner / "status").write_text("Name:\tpipewire\nTgid:\t444\n", encoding="utf-8")
            (owner / "cgroup").write_text(
                "0::/user.slice/user-1000.slice/user@1000.service/session.slice/pipewire.service\n",
                encoding="utf-8",
            )
            MODULE.require_qbzd_pcm_idle(
                SERVICE_A,
                asound_root=root / "asound",
                proc_root=root / "proc",
            )
            (owner / "status").write_text("Name:\tpipewire\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.RecoveryError, "alsa-owner-unreadable"):
                MODULE.require_qbzd_pcm_idle(
                    SERVICE_A,
                    asound_root=root / "asound",
                    proc_root=root / "proc",
                )

    def test_pcm_gate_blocks_restart_even_when_status_claims_device_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            runner = FakeRunner()

            def blocked_pcm(_service):
                raise MODULE.RecoveryError("qbzd-pcm-open")

            result = self.reconcile(
                state_path=state_path,
                statuses=[status(opened=False)],
                services=[SERVICE_A],
                monotonic=[400.0],
                runner=runner,
                pcm=blocked_pcm,
            )
            self.assertEqual(result, "blocked:qbzd-pcm-open")
            self.assertEqual(runner.commands, [])

    def test_api_version_rejects_bool_and_float_discriminators(self):
        base = {
            "version": "2.0.2",
            "uptime_secs": 1,
            "auth": {"state": "logged_in"},
            "network": {"online": True},
            "qconnect": {"state": "retrying", "session_active": False},
            "audio": {
                "backend": "alsa",
                "configured_device": MODULE.EXPECTED_DEVICE,
                "device_present": True,
                "device_open": False,
            },
        }
        for value in (True, 1.0):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    MODULE.RecoveryError, "status-invalid:api-version"
                ):
                    MODULE.classify_status_payload(
                        {"api_version": value, **base}
                    )

    def test_state_schema_rejects_bool_and_float_discriminators(self):
        for value in (True, 2.0):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as tmp:
                    state_path = self.state_path(tmp)
                    payload = MODULE._default_state(BOOT)
                    payload["schema_version"] = value
                    state_path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(MODULE.RecoveryError, "state-invalid"):
                        MODULE._load_state(state_path)

    def test_nonfinite_negative_and_schema1_state_fail_closed(self):
        payloads = [
            '{"schema_version":2,"boot_id":"%s","candidate_pid":111,"candidate_start_ticks":1000,"retry_since_monotonic":NaN,"failures":0,"next_attempt_monotonic":0,"last_recovered_at_unix":null,"restart_armed_monotonic":null,"restart_armed_pid":null,"restart_armed_start_ticks":null}\n'
            % BOOT,
            json.dumps(
                {
                    **self.candidate_state(),
                    "retry_since_monotonic": -1.0,
                }
            ),
            json.dumps({"schema_version": 1}),
        ]
        for payload in payloads:
            with self.subTest(payload=payload[:40]):
                with tempfile.TemporaryDirectory() as tmp:
                    state_path = self.state_path(tmp)
                    state_path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(MODULE.RecoveryError):
                        MODULE._load_state(state_path)

    def test_state_binding_fields_must_be_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            invalid = MODULE._default_state(BOOT)
            invalid["candidate_pid"] = 111
            state_path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.RecoveryError, "candidate-binding"):
                MODULE._load_state(state_path)

    def test_command_contract_allows_only_observe_and_try_restart(self):
        MODULE.check_contract()
        with self.assertRaisesRegex(MODULE.RecoveryError, "command-not-allowed"):
            MODULE._validate_command(("systemctl", "--user", "restart", "qbzd.service"))
        with self.assertRaisesRegex(MODULE.RecoveryError, "command-not-allowed"):
            MODULE._validate_command(("systemctl", "--user", "restart", "wireplumber.service"))

    def test_state_file_is_private_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, MODULE._default_state(BOOT))
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(state_path.read_text())["schema_version"], 2)
            target = pathlib.Path(tmp) / "target"
            target.write_text("{}", encoding="utf-8")
            state_path.unlink()
            state_path.symlink_to(target)
            with self.assertRaisesRegex(MODULE.RecoveryError, "state-invalid"):
                MODULE._load_state(state_path)

    def test_read_qbzd_service_binds_start_ticks_and_cgroup(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = pathlib.Path(tmp)
            process = proc_root / "111"
            process.mkdir()
            (process / "comm").write_text("qbzd\n", encoding="utf-8")
            fields = ["S", *(["1"] * 18), "12345"]
            (process / "stat").write_text(
                "111 (qbzd) " + " ".join(fields) + "\n", encoding="utf-8"
            )
            (process / "cgroup").write_text(f"0::{SERVICE_A.cgroup}\n", encoding="utf-8")

            def service_runner(argv):
                self.assertIn("--property=MainPID", argv)
                return "ActiveState=active\nMainPID=111\n"

            observed = MODULE.read_qbzd_service(
                runner=service_runner, proc_root=proc_root
            )
            self.assertEqual(observed.pid, 111)
            self.assertEqual(observed.start_ticks, 12345)
            self.assertEqual(observed.cgroup, SERVICE_A.cgroup)

    def test_post_effect_state_write_failure_cannot_repeat_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            original_store = MODULE._store_state
            writes = 0

            def store_then_fail(path, state):
                nonlocal writes
                writes += 1
                if writes == 1:
                    original_store(path, state)
                    return
                raise OSError("forced post-effect state failure")

            runner = FakeRunner()
            with mock.patch.object(MODULE, "_store_state", side_effect=store_then_fail):
                with self.assertRaisesRegex(OSError, "post-effect"):
                    self.reconcile(
                        state_path=state_path,
                        statuses=[status(), status(), status(), healthy()],
                        services=[SERVICE_A, SERVICE_A, SERVICE_A, SERVICE_B],
                        monotonic=[400.0, 402.0, 403.0, 403.5, 404.0],
                        runner=runner,
                    )
            self.assertEqual(runner.commands, [TRY_RESTART])
            persisted = MODULE._load_state(state_path)
            self.assertEqual(persisted["restart_armed_monotonic"], 403.5)
            self.assertGreaterEqual(persisted["next_attempt_monotonic"], 1303.5)


if __name__ == "__main__":
    unittest.main()
