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
    playback_state="paused",
    track_id=123456,
    position=0.0,
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
        playback_state=playback_state,
        playback_track_id=track_id,
        playback_position=position,
        uptime_secs=uptime,
    )


def healthy(*, uptime=200, **kwargs):
    return status(qconnect="connected", session=True, uptime=uptime, **kwargs)


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


class FakeQconnectRunner:
    def __init__(self, *, fail_action=None):
        self.commands = []
        self.fail_action = fail_action

    def __call__(self, service, action):
        self.commands.append((service, action))
        if action == self.fail_action:
            raise MODULE.RecoveryError(f"qconnect-command-failed:{action}")
        return f"qconnect {action} ok"


class FakeJournalReader:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def __call__(self, cursor):
        self.calls.append(cursor)
        if not self.values:
            raise AssertionError("unexpected journal read")
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class StopLoop(RuntimeError):
    pass


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
        qconnect_next_attempt=9999.0,
        qconnect_failures=0,
    ):
        state = MODULE._default_state(BOOT)
        state.update(
            {
                "candidate_pid": service.pid,
                "candidate_start_ticks": service.start_ticks,
                "retry_since_monotonic": retry_since,
                "failures": failures,
                "next_attempt_monotonic": next_attempt,
                "qconnect_failures": qconnect_failures,
                "qconnect_next_attempt_monotonic": qconnect_next_attempt,
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
        qconnect_runner=None,
        pcm=None,
        pcm_owned=None,
        boot=BOOT,
        network_evidence_realtime=None,
    ):
        return MODULE.reconcile_once(
            state_path=state_path,
            status_reader=SequenceReader(statuses),
            service_reader=SequenceReader(services),
            runner=runner or FakeRunner(),
            qconnect_action_runner=qconnect_runner or FakeQconnectRunner(),
            pcm_idle_checker=pcm or (lambda _service: None),
            pcm_owned_checker=pcm_owned or (lambda _service: None),
            sleeper=lambda _seconds: None,
            monotonic_clock=SequenceClock(monotonic),
            wall_clock=SequenceClock(wall or [1000.0] * 20),
            boot_id_reader=lambda: boot,
            network_reachability_evidence_realtime=network_evidence_realtime,
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
                "playback": {
                    "state": "paused",
                    "track_id": 99457447,
                    "position": 0,
                    "title": "private metadata is ignored",
                },
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

    def test_daemon_restart_still_waits_five_minutes_while_qconnect_is_backed_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            result = self.reconcile(
                state_path=state_path,
                statuses=[status()],
                services=[SERVICE_A],
                monotonic=[399.9],
            )
            self.assertEqual(result, "noop:qconnect-backoff")

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

    def test_wall_clock_jump_does_not_satisfy_daemon_restart_window(self):
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
            self.assertEqual(result, "noop:qconnect-backoff")

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
                statuses=[status(opened=True, playback_state="playing")],
                services=[],
                monotonic=[500.0],
                runner=runner,
            )
            self.assertEqual(result, "noop:not-candidate")
            self.assertEqual(runner.commands, [])

    def test_paused_open_state_requires_network_truth_and_never_allows_playing(self):
        exhausted = status(qconnect="exhausted", online=False, opened=True)
        self.assertFalse(MODULE._is_recovery_candidate(exhausted))
        self.assertTrue(
            MODULE._is_recovery_candidate(exhausted, allow_network_offline=True)
        )
        self.assertTrue(
            MODULE._is_recovery_candidate(status(qconnect="retrying", opened=True))
        )
        self.assertFalse(
            MODULE._is_recovery_candidate(
                status(qconnect="retrying", opened=True, playback_state="playing")
            )
        )
        self.assertFalse(
            MODULE._is_recovery_candidate(
                status(qconnect="retrying", opened=True, track_id=None)
            )
        )

    def test_exhausted_open_pcm_is_explicitly_blocked_before_qconnect_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path, self.candidate_state(qconnect_next_attempt=0.0)
            )
            qconnect = FakeQconnectRunner()

            def blocked_pcm(_service):
                raise MODULE.RecoveryError("qbzd-pcm-open")

            result = self.reconcile(
                state_path=state_path,
                statuses=[status(qconnect="exhausted", online=False, opened=True)],
                services=[SERVICE_A],
                monotonic=[200.0],
                wall=[1000.0],
                qconnect_runner=qconnect,
                pcm_owned=blocked_pcm,
                network_evidence_realtime=1000.0,
            )
            self.assertEqual(result, "blocked:qbzd-pcm-open")
            self.assertEqual(qconnect.commands, [])

    def test_retrying_paused_open_pcm_recovers_via_narrow_qconnect_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path, self.candidate_state(qconnect_next_attempt=0.0)
            )
            qconnect = FakeQconnectRunner()
            owned = []

            def idle_must_not_run(_service):
                raise AssertionError("daemon PCM-idle gate entered during paused-open QConnect repair")

            stuck = status(
                qconnect="retrying",
                online=False,
                opened=True,
                playback_state="paused",
                track_id=99457447,
                position=0,
            )
            result = self.reconcile(
                state_path=state_path,
                statuses=[
                    stuck,
                    stuck,
                    stuck,
                    stuck,
                    healthy(
                        opened=True,
                        playback_state="paused",
                        track_id=293371503,
                        position=0,
                    ),
                ],
                services=[SERVICE_A] * 6,
                monotonic=[200.0, 202.0, 203.0, 203.5, 204.0],
                wall=[1000.0] * 10,
                qconnect_runner=qconnect,
                pcm=idle_must_not_run,
                pcm_owned=lambda service: owned.append(service),
                network_evidence_realtime=1000.0,
            )
            self.assertEqual(result, "recovered:qconnect")
            self.assertEqual(owned, [SERVICE_A, SERVICE_A, SERVICE_A])
            self.assertEqual(
                [action for _service, action in qconnect.commands],
                ["disable", "enable"],
            )

    def test_paused_open_resume_after_final_owner_scan_blocks_before_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path, self.candidate_state(qconnect_next_attempt=0.0)
            )
            stuck = status(opened=True, track_id=123456, position=0)
            resumed = False
            owner_checks = []
            status_reads = 0

            def read_dynamic_status():
                nonlocal status_reads
                status_reads += 1
                if resumed:
                    return status(
                        opened=True,
                        playback_state="playing",
                        track_id=123456,
                        position=1,
                    )
                return stuck

            def prove_owner(service):
                nonlocal resumed
                owner_checks.append(service)
                if len(owner_checks) == 3:
                    resumed = True

            qconnect = FakeQconnectRunner()
            result = MODULE.reconcile_once(
                state_path=state_path,
                status_reader=read_dynamic_status,
                service_reader=SequenceReader([SERVICE_A] * 4),
                runner=FakeRunner(),
                qconnect_action_runner=qconnect,
                pcm_idle_checker=lambda _service: None,
                pcm_owned_checker=prove_owner,
                sleeper=lambda _seconds: None,
                monotonic_clock=SequenceClock([200.0, 202.0, 203.0]),
                wall_clock=SequenceClock([1000.0] * 10),
                boot_id_reader=lambda: BOOT,
            )
            self.assertEqual(result, "blocked:playback-changed-at-effect-edge")
            self.assertEqual(owner_checks, [SERVICE_A, SERVICE_A, SERVICE_A])
            self.assertEqual(qconnect.commands, [])
            self.assertEqual(status_reads, 4)

    def test_paused_open_candidate_never_relaxes_daemon_restart_pcm_idle_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path,
                self.candidate_state(
                    retry_since=100.0,
                    qconnect_next_attempt=9999.0,
                    next_attempt=0.0,
                ),
            )
            runner = FakeRunner()
            qconnect = FakeQconnectRunner()
            idle_checks = []

            def block_open_pcm(service):
                idle_checks.append(service)
                raise MODULE.RecoveryError("qbzd-pcm-open")

            result = self.reconcile(
                state_path=state_path,
                statuses=[status(opened=True, playback_state="paused")],
                services=[SERVICE_A],
                monotonic=[400.0],
                runner=runner,
                qconnect_runner=qconnect,
                pcm=block_open_pcm,
                pcm_owned=lambda _service: None,
            )
            self.assertEqual(result, "blocked:qbzd-pcm-open")
            self.assertEqual(idle_checks, [SERVICE_A])
            self.assertEqual(qconnect.commands, [])
            self.assertEqual(runner.commands, [])

    def test_paused_open_track_or_position_drift_blocks_before_qconnect_effect(self):
        cases = (
            (status(opened=True, track_id=222, position=0), "track-change"),
            (status(opened=True, track_id=123456, position=1), "position-progress"),
        )
        for second, label in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                state_path = self.state_path(tmp)
                MODULE._store_state(
                    state_path, self.candidate_state(qconnect_next_attempt=0.0)
                )
                qconnect = FakeQconnectRunner()
                first = status(opened=True, track_id=123456, position=0)
                result = self.reconcile(
                    state_path=state_path,
                    statuses=[first, second],
                    services=[SERVICE_A, SERVICE_A],
                    monotonic=[200.0, 202.0],
                    qconnect_runner=qconnect,
                    pcm_owned=lambda _service: None,
                )
                self.assertEqual(result, "blocked:playback-not-stably-paused")
                self.assertEqual(qconnect.commands, [])

    def test_exhausted_state_recovers_via_narrow_qconnect_cycle_when_pcm_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path, self.candidate_state(qconnect_next_attempt=0.0)
            )
            qconnect = FakeQconnectRunner()
            checked = []
            exhausted = status(qconnect="exhausted", online=False, opened=False)
            result = self.reconcile(
                state_path=state_path,
                statuses=[exhausted, exhausted, exhausted, healthy()],
                services=[SERVICE_A] * 5,
                monotonic=[200.0, 202.0, 203.0, 203.5, 204.0],
                wall=[1000.0, 1002.0, 1003.0, 1004.0, 1005.0],
                qconnect_runner=qconnect,
                pcm=lambda service: checked.append(service),
                network_evidence_realtime=1000.0,
            )
            self.assertEqual(result, "recovered:qconnect")
            self.assertEqual(
                [action for _service, action in qconnect.commands],
                ["disable", "enable"],
            )
            self.assertEqual(checked, [SERVICE_A, SERVICE_A, SERVICE_A])

    def test_freshly_armed_exhausted_state_uses_fresh_attestation_before_ninety_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            exhausted = status(qconnect="exhausted", online=False, opened=False)
            first = self.reconcile(
                state_path=state_path,
                statuses=[exhausted],
                services=[SERVICE_A],
                monotonic=[100.0],
                wall=[1000.0],
                network_evidence_realtime=1000.0,
            )
            self.assertEqual(first, "armed")

            qconnect = FakeQconnectRunner()
            checked = []
            second = self.reconcile(
                state_path=state_path,
                statuses=[exhausted, exhausted, exhausted, healthy()],
                services=[SERVICE_A] * 5,
                monotonic=[130.0, 132.0, 133.0, 133.5, 134.0],
                wall=[1030.0, 1032.0, 1033.0, 1034.0, 1035.0],
                qconnect_runner=qconnect,
                pcm=lambda service: checked.append(service),
                network_evidence_realtime=1000.0,
            )
            self.assertEqual(second, "recovered:qconnect")
            self.assertEqual(
                [action for _service, action in qconnect.commands],
                ["disable", "enable"],
            )
            self.assertEqual(checked, [SERVICE_A, SERVICE_A, SERVICE_A])

    def test_retrying_still_requires_ninety_second_stabilization_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path,
                self.candidate_state(
                    retry_since=100.0,
                    qconnect_next_attempt=0.0,
                ),
            )
            qconnect = FakeQconnectRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[status(qconnect="retrying")],
                services=[SERVICE_A],
                monotonic=[130.0],
                qconnect_runner=qconnect,
            )
            self.assertEqual(result, "noop:stabilizing")
            self.assertEqual(qconnect.commands, [])

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

    def test_journal_attested_network_reachability_allows_offline_status_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            runner = FakeRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[
                    status(online=False),
                    status(online=False),
                    status(online=False),
                    healthy(),
                ],
                services=[SERVICE_A, SERVICE_A, SERVICE_A, SERVICE_B],
                monotonic=[400.0, 402.0, 403.0, 403.5, 404.0],
                runner=runner,
                network_evidence_realtime=1000.0,
            )
            self.assertEqual(result, "recovered")
            self.assertEqual(runner.commands, [TRY_RESTART])

    def test_qconnect_network_attestation_must_still_be_fresh_at_effect_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path, self.candidate_state(qconnect_next_attempt=0.0)
            )
            qconnect = FakeQconnectRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[
                    status(online=False),
                    status(online=False),
                    status(online=False),
                ],
                services=[SERVICE_A, SERVICE_A, SERVICE_A],
                monotonic=[200.0, 202.0, 203.0, 203.5],
                wall=[1000.0, 1088.0, 1089.0, 1091.0],
                qconnect_runner=qconnect,
                network_evidence_realtime=1000.0,
            )
            self.assertEqual(result, "blocked:network-attestation-expired")
            self.assertEqual(qconnect.commands, [])

    def test_daemon_network_attestation_must_still_be_fresh_at_effect_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(state_path, self.candidate_state())
            runner = FakeRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[
                    status(online=False),
                    status(online=False),
                    status(online=False),
                ],
                services=[SERVICE_A, SERVICE_A, SERVICE_A],
                monotonic=[400.0, 402.0, 403.0, 403.5],
                wall=[1000.0, 1088.0, 1089.0, 1091.0],
                runner=runner,
                network_evidence_realtime=1000.0,
            )
            self.assertEqual(result, "blocked:network-attestation-expired")
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

    def test_paused_open_pcm_gate_requires_exact_qbzd_owner_and_live_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            asound = root / "asound"
            proc = root / "proc"
            status_path = asound / "card1" / "pcm0p" / "sub0" / "status"
            status_path.parent.mkdir(parents=True)
            (asound / "card1" / "id").write_text("M2\n", encoding="utf-8")
            status_path.write_text("state: RUNNING\nowner_pid   : 222\n", encoding="utf-8")

            service_root = proc / str(SERVICE_A.pid)
            service_root.mkdir(parents=True)
            fields = ["S", *(["1"] * 18), str(SERVICE_A.start_ticks)]
            (service_root / "stat").write_text(
                f"{SERVICE_A.pid} (qbzd) " + " ".join(fields) + "\n",
                encoding="utf-8",
            )
            (service_root / "cgroup").write_text(
                f"0::{SERVICE_A.cgroup}\n", encoding="utf-8"
            )

            owner = proc / "222"
            owner.mkdir(parents=True)
            (owner / "status").write_text("Name:\tqbzd\nTgid:\t111\n", encoding="utf-8")
            (owner / "cgroup").write_text(
                f"0::{SERVICE_A.cgroup}\n", encoding="utf-8"
            )
            MODULE.require_qbzd_pcm_owned(
                SERVICE_A, asound_root=asound, proc_root=proc
            )

            (owner / "cgroup").write_text(
                "0::/user.slice/user-1000.slice/user@1000.service/session.slice/pipewire.service\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                MODULE.RecoveryError, "qbzd-target-pcm-owner-mismatch"
            ):
                MODULE.require_qbzd_pcm_owned(
                    SERVICE_A, asound_root=asound, proc_root=proc
                )

    def test_paused_open_pcm_gate_cannot_be_satisfied_by_wrong_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            asound = root / "asound"
            proc = root / "proc"

            service_root = proc / str(SERVICE_A.pid)
            service_root.mkdir(parents=True)
            fields = ["S", *(["1"] * 18), str(SERVICE_A.start_ticks)]
            (service_root / "stat").write_text(
                f"{SERVICE_A.pid} (qbzd) " + " ".join(fields) + "\n",
                encoding="utf-8",
            )
            (service_root / "cgroup").write_text(
                f"0::{SERVICE_A.cgroup}\n", encoding="utf-8"
            )

            motu_status = asound / "card2" / "pcm0p" / "sub0" / "status"
            motu_status.parent.mkdir(parents=True)
            (asound / "card2" / "id").write_text("M2\n", encoding="utf-8")
            motu_status.write_text("state: RUNNING\nowner_pid   : 333\n", encoding="utf-8")
            helper = proc / "333"
            helper.mkdir(parents=True)
            (helper / "status").write_text("Name:\thelper\nTgid:\t333\n", encoding="utf-8")
            (helper / "cgroup").write_text(
                f"0::{SERVICE_A.cgroup}\n", encoding="utf-8"
            )

            other_status = asound / "card3" / "pcm0p" / "sub0" / "status"
            other_status.parent.mkdir(parents=True)
            (asound / "card3" / "id").write_text("Other\n", encoding="utf-8")
            other_status.write_text("state: RUNNING\nowner_pid   : 222\n", encoding="utf-8")
            exact = proc / "222"
            exact.mkdir(parents=True)
            (exact / "status").write_text("Name:\tqbzd\nTgid:\t111\n", encoding="utf-8")
            (exact / "cgroup").write_text(
                f"0::{SERVICE_A.cgroup}\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(
                MODULE.RecoveryError, "qbzd-target-pcm-owner-mismatch"
            ):
                MODULE.require_qbzd_pcm_owned(
                    SERVICE_A, asound_root=asound, proc_root=proc
                )

    def test_paused_open_pcm_gate_rejects_service_start_tick_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            asound = root / "asound"
            proc = root / "proc"
            status_path = asound / "card1" / "pcm0p" / "sub0" / "status"
            status_path.parent.mkdir(parents=True)
            status_path.write_text("closed\n", encoding="utf-8")
            service_root = proc / str(SERVICE_A.pid)
            service_root.mkdir(parents=True)
            fields = ["S", *(["1"] * 18), str(SERVICE_A.start_ticks + 1)]
            (service_root / "stat").write_text(
                f"{SERVICE_A.pid} (qbzd) " + " ".join(fields) + "\n",
                encoding="utf-8",
            )
            (service_root / "cgroup").write_text(
                f"0::{SERVICE_A.cgroup}\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(MODULE.RecoveryError, "qbzd-process-unverified"):
                MODULE.require_qbzd_pcm_owned(
                    SERVICE_A, asound_root=asound, proc_root=proc
                )

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

    def test_playback_status_fields_fail_closed_on_invalid_effect_evidence(self):
        base = {
            "api_version": 1,
            "version": "2.0.2",
            "uptime_secs": 1,
            "auth": {"state": "logged_in"},
            "network": {"online": True},
            "qconnect": {"state": "retrying", "session_active": False},
            "audio": {
                "backend": "alsa",
                "configured_device": MODULE.EXPECTED_DEVICE,
                "device_present": True,
                "device_open": True,
            },
        }
        invalid = (
            ({"state": "", "track_id": 1, "position": 0}, "playback.state"),
            ({"state": "paused", "track_id": True, "position": 0}, "playback.track_id"),
            ({"state": "paused", "track_id": 1, "position": True}, "playback.position"),
            ({"state": "paused", "track_id": 1, "position": -1}, "playback.position"),
            ({"state": "paused", "track_id": 1, "position": 10**999}, "playback.position"),
        )
        for playback, code in invalid:
            with self.subTest(code=code):
                with self.assertRaisesRegex(MODULE.RecoveryError, f"status-invalid:{code}"):
                    MODULE.classify_status_payload({**base, "playback": playback})

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
            "playback": {"state": "paused", "track_id": 123456, "position": 0},
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

    def test_journal_stream_drain_is_memory_bounded_and_marks_truncation(self):
        import io

        result = {}
        payload = b"x" * (MODULE.MAX_COMMAND_OUTPUT_BYTES + 8193)
        MODULE._drain_journal_stream(io.BytesIO(payload), result)
        self.assertEqual(len(result["bytes"]), MODULE.MAX_COMMAND_OUTPUT_BYTES)
        self.assertTrue(result["truncated"])

    def test_journal_parser_returns_timestamped_events_and_exact_cursor(self):
        cursor = "s=abc123;i=9;b=boot;m=1;t=2;x=3"
        payload = "\n".join(
            [
                json.dumps(
                    {"MESSAGE": "first line", "__REALTIME_TIMESTAMP": "1000000"}
                ),
                json.dumps(
                    {"MESSAGE": "second line", "__REALTIME_TIMESTAMP": "2000000"}
                ),
                "-- cursor: " + cursor,
                "",
            ]
        )
        delta = MODULE.parse_journal_output(payload)
        self.assertEqual(delta.cursor, cursor)
        self.assertEqual(delta.text, "first line\nsecond line")
        self.assertEqual(
            delta.events,
            (
                MODULE.JournalEvent(1_000_000, "first line"),
                MODULE.JournalEvent(2_000_000, "second line"),
            ),
        )

    def test_journal_parser_rejects_missing_duplicate_or_malformed_cursor(self):
        invalid = (
            "",
            "one line\n",
            "-- cursor: s=one\n-- cursor: s=two\n",
            "-- cursor: not-a-systemd-cursor\n",
            "-- cursor: s=has space\n",
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(MODULE.RecoveryError):
                    MODULE.parse_journal_output(payload)

    def test_real_qconnect_failure_journal_lines_trigger_status_probe(self):
        observed = (
            '[QConnect/Transport] Cloud rejected session: code=403 descr="auth"',
            "[QConnect] Lifecycle -> Reconnecting",
            "Reconnect scheduled: attempt=3",
            "Max reconnect attempts exceeded",
            "[QConnect] Reconnect exhausted (11)",
        )
        for line in observed:
            with self.subTest(line=line):
                self.assertTrue(MODULE.journal_requires_status(line))
        self.assertFalse(
            MODULE.journal_requires_status(
                "ALSA lib pcm_dmix.c:999:(snd_pcm_dmix_open) unable to open slave"
            )
        )

    def test_network_reachability_requires_fresh_complete_timestamped_sequence(self):
        fresh = MODULE.JournalDelta(
            "s=proof",
            "",
            (
                MODULE.JournalEvent(1_000_000_000, "[QConnect/Transport] WebSocket connected"),
                MODULE.JournalEvent(1_001_000_000, "[QConnect/Transport] Authenticated with JWT"),
                MODULE.JournalEvent(
                    1_002_000_000,
                    '[QConnect/Transport] Cloud rejected session: msg_id=0 code=0 descr="auth"',
                ),
            ),
        )
        self.assertTrue(
            MODULE.journal_proves_qconnect_network_reachable(fresh, now_wall=1080.0)
        )
        self.assertFalse(
            MODULE.journal_proves_qconnect_network_reachable(fresh, now_wall=1093.0)
        )
        self.assertFalse(
            MODULE.journal_proves_qconnect_network_reachable(fresh, now_wall=995.0)
        )
        self.assertTrue(
            MODULE.journal_proves_qconnect_network_reachable(fresh, now_wall=997.0)
        )

        incomplete = MODULE.JournalDelta(
            "s=incomplete",
            "",
            (
                MODULE.JournalEvent(1_000_000_000, "[QConnect/Transport] WebSocket connected"),
                MODULE.JournalEvent(1_001_000_000, "[QConnect/Transport] Authenticated with JWT"),
            ),
        )
        self.assertFalse(
            MODULE.journal_proves_qconnect_network_reachable(
                incomplete, now_wall=1002.0
            )
        )

        slow_sequence = MODULE.JournalDelta(
            "s=slow",
            "",
            (
                MODULE.JournalEvent(1_000_000_000, "[QConnect/Transport] WebSocket connected"),
                MODULE.JournalEvent(1_001_000_000, "[QConnect/Transport] Authenticated with JWT"),
                MODULE.JournalEvent(
                    1_031_000_001,
                    '[QConnect/Transport] Cloud rejected session: descr="auth"',
                ),
            ),
        )
        self.assertFalse(
            MODULE.journal_proves_qconnect_network_reachable(
                slow_sequence, now_wall=1032.0
            )
        )

    def test_journal_parser_rejects_unproven_event_timestamps(self):
        cursor = "s=abc123;i=9;b=boot;m=1;t=2;x=3"
        invalid_entries = (
            {"MESSAGE": "WebSocket connected"},
            {"MESSAGE": "WebSocket connected", "__REALTIME_TIMESTAMP": "not-a-time"},
            {"MESSAGE": "WebSocket connected", "__REALTIME_TIMESTAMP": "0"},
        )
        for entry in invalid_entries:
            with self.subTest(entry=entry):
                payload = json.dumps(entry) + "\n-- cursor: " + cursor + "\n"
                with self.assertRaisesRegex(
                    MODULE.RecoveryError, "journal-output-invalid"
                ):
                    MODULE.parse_journal_output(payload)

    def test_only_positive_connected_outcomes_enter_quiet_mode(self):
        for result in ("noop:connected", "noop:recovered-naturally", "recovered"):
            with self.subTest(result=result):
                self.assertFalse(MODULE._fast_followup_required(result))
        for result in (
            "noop:not-candidate",
            "noop:changed",
            "noop:boot-changed",
            "armed",
            "noop:stabilizing",
            "noop:backoff",
            "blocked:status-unavailable",
        ):
            with self.subTest(result=result):
                self.assertTrue(MODULE._fast_followup_required(result))

    def test_adaptive_poll_policy_is_quiet_when_healthy_but_fails_safe(self):
        self.assertIsNone(
            MODULE.adaptive_poll_reason(
                fast_followup=False,
                journal_available=True,
                journal_text="",
                last_status_monotonic=100.0,
                now_monotonic=399.9,
            )
        )
        self.assertEqual(
            MODULE.adaptive_poll_reason(
                fast_followup=False,
                journal_available=True,
                journal_text="",
                last_status_monotonic=100.0,
                now_monotonic=400.0,
            ),
            "safety-fallback",
        )
        self.assertEqual(
            MODULE.adaptive_poll_reason(
                fast_followup=False,
                journal_available=False,
                journal_text="",
                last_status_monotonic=100.0,
                now_monotonic=101.0,
            ),
            "journal-fallback",
        )
        self.assertEqual(
            MODULE.adaptive_poll_reason(
                fast_followup=False,
                journal_available=True,
                journal_text="[QConnect] Lifecycle -> Reconnecting",
                last_status_monotonic=100.0,
                now_monotonic=101.0,
            ),
            "journal-trigger",
        )

    def test_run_loop_initial_probe_then_healthy_journal_skips_status_polling(self):
        journal = FakeJournalReader(
            [
                MODULE.JournalDelta("s=0", ""),
                MODULE.JournalDelta("s=1", ""),
                MODULE.JournalDelta("s=2", ""),
                MODULE.JournalDelta("s=3", ""),
            ]
        )
        reconciles = []
        sleeps = 0

        def reconcile(path):
            reconciles.append(path)
            return "noop:connected"

        def sleeper(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 3:
                raise StopLoop

        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            with self.assertRaises(StopLoop):
                MODULE.run_loop(
                    state_path,
                    reconciler=reconcile,
                    journal_reader=journal,
                    sleeper=sleeper,
                    monotonic_clock=SequenceClock([0.0, 0.0, 30.0, 60.0]),
                )
        self.assertEqual(reconciles, [state_path])
        self.assertEqual(journal.calls, [None, "s=0", "s=1", "s=2"])

    def test_run_loop_journal_trigger_enters_old_fast_followup_cadence(self):
        journal = FakeJournalReader(
            [
                MODULE.JournalDelta("s=0", ""),
                MODULE.JournalDelta("s=1", ""),
                MODULE.JournalDelta(
                    "s=2", "[QConnect] Lifecycle -> Reconnecting"
                ),
                MODULE.JournalDelta("s=3", ""),
            ]
        )
        results = iter(["noop:connected", "armed", "noop:stabilizing"])
        reconciles = []
        sleeps = 0

        def reconcile(path):
            reconciles.append(path)
            return next(results)

        def sleeper(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 3:
                raise StopLoop

        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            with self.assertRaises(StopLoop):
                MODULE.run_loop(
                    state_path,
                    reconciler=reconcile,
                    journal_reader=journal,
                    sleeper=sleeper,
                    monotonic_clock=SequenceClock(
                        [0.0, 0.0, 30.0, 30.0, 60.0, 60.0]
                    ),
                )
        self.assertEqual(reconciles, [state_path, state_path, state_path])

    def test_run_loop_keeps_network_override_bounded_to_journal_event_time(self):
        proof_events = (
            MODULE.JournalEvent(1_000_000_000, "[QConnect/Transport] WebSocket connected"),
            MODULE.JournalEvent(1_001_000_000, "[QConnect/Transport] Authenticated with JWT"),
            MODULE.JournalEvent(
                1_002_000_000,
                '[QConnect/Transport] Cloud rejected session: msg_id=0 code=0 descr="auth"',
            ),
        )
        journal = FakeJournalReader(
            [
                MODULE.JournalDelta("s=0", ""),
                MODULE.JournalDelta("s=1", "proof", proof_events),
                MODULE.JournalDelta("s=2", ""),
                MODULE.JournalDelta("s=3", ""),
            ]
        )
        observed_overrides = []
        results = iter(["armed", "noop:stabilizing", "noop:not-candidate"])
        sleeps = 0

        def fake_reconcile_once(
            *, state_path, network_reachability_evidence_realtime=None
        ):
            observed_overrides.append(
                (state_path, network_reachability_evidence_realtime)
            )
            return next(results)

        def sleeper(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 3:
                raise StopLoop

        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            with mock.patch.object(MODULE, "reconcile_once", side_effect=fake_reconcile_once):
                with self.assertRaises(StopLoop):
                    MODULE.run_loop(
                        state_path,
                        journal_reader=journal,
                        sleeper=sleeper,
                        monotonic_clock=SequenceClock(
                            [0.0, 0.0, 30.0, 30.0, 121.0, 121.0]
                        ),
                        wall_clock=SequenceClock([1002.0, 1060.0, 1093.0]),
                    )

        self.assertEqual(
            observed_overrides,
            [(state_path, 1002.0), (state_path, 1002.0), (state_path, None)],
        )

    def test_run_loop_does_not_refresh_stale_journal_proof_when_read_late(self):
        stale_events = (
            MODULE.JournalEvent(1_000_000_000, "[QConnect/Transport] WebSocket connected"),
            MODULE.JournalEvent(1_001_000_000, "[QConnect/Transport] Authenticated with JWT"),
            MODULE.JournalEvent(
                1_002_000_000,
                '[QConnect/Transport] Cloud rejected session: msg_id=0 code=0 descr="auth"',
            ),
        )
        journal = FakeJournalReader(
            [
                MODULE.JournalDelta("s=0", ""),
                MODULE.JournalDelta("s=1", "stale-proof", stale_events),
            ]
        )
        observed_overrides = []

        def fake_reconcile_once(
            *, state_path, network_reachability_evidence_realtime=None
        ):
            observed_overrides.append(
                (state_path, network_reachability_evidence_realtime)
            )
            return "noop:not-candidate"

        def sleeper(_seconds):
            raise StopLoop

        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            with mock.patch.object(MODULE, "reconcile_once", side_effect=fake_reconcile_once):
                with self.assertRaises(StopLoop):
                    MODULE.run_loop(
                        state_path,
                        journal_reader=journal,
                        sleeper=sleeper,
                        monotonic_clock=SequenceClock([500.0, 500.0]),
                        wall_clock=SequenceClock([1200.0]),
                    )

        self.assertEqual(observed_overrides, [(state_path, None)])

    def test_run_loop_replacement_cursor_is_captured_before_fallback_status(self):
        order = []
        values = [
            MODULE.JournalDelta("s=0", ""),
            MODULE.RecoveryError("journal-unavailable"),
            MODULE.JournalDelta("s=1", ""),
            MODULE.JournalDelta("s=2", "[QConnect] Lifecycle -> Reconnecting"),
        ]

        def journal_reader(cursor):
            order.append(("journal", cursor))
            value = values.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        results = iter(["noop:connected", "armed"])

        def reconcile(path):
            order.append(("status", path))
            return next(results)

        sleeps = 0

        def sleeper(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                raise StopLoop

        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            with self.assertRaises(StopLoop):
                MODULE.run_loop(
                    state_path,
                    reconciler=reconcile,
                    journal_reader=journal_reader,
                    sleeper=sleeper,
                    monotonic_clock=SequenceClock([0.0, 0.0, 30.0, 30.0]),
                )

        self.assertEqual(
            order,
            [
                ("journal", None),
                ("journal", "s=0"),
                ("journal", None),
                ("status", state_path),
                ("journal", "s=1"),
                ("status", state_path),
            ],
        )

    def test_run_loop_journal_failure_falls_back_to_status_every_cycle(self):
        unavailable = MODULE.RecoveryError("journal-unavailable")
        journal = FakeJournalReader(
            [
                unavailable,
                MODULE.RecoveryError("journal-unavailable"),
                MODULE.JournalDelta("s=recovered", ""),
            ]
        )
        reconciles = []
        sleeps = 0

        def reconcile(path):
            reconciles.append(path)
            return "noop:connected"

        def sleeper(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                raise StopLoop

        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            with self.assertRaises(StopLoop):
                MODULE.run_loop(
                    state_path,
                    reconciler=reconcile,
                    journal_reader=journal,
                    sleeper=sleeper,
                    monotonic_clock=SequenceClock([0.0, 0.0, 30.0, 30.0]),
                )
        self.assertEqual(reconciles, [state_path, state_path])
        self.assertEqual(journal.calls, [None, None, None])

    def test_qconnect_action_executes_only_pinned_reverified_image(self):
        completed = MODULE.subprocess.CompletedProcess(
            args=(), returncode=0, stdout=b"qconnect ok\n", stderr=b""
        )
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = pathlib.Path(tmp) / "proc"
            process = proc_root / str(SERVICE_A.pid)
            process.mkdir(parents=True)
            binary = pathlib.Path(tmp) / "qbzd"
            binary.write_bytes(b"fake-qbzd")
            binary.chmod(0o700)
            (process / "comm").write_text("qbzd\n", encoding="utf-8")
            fields = ["S", *(["1"] * 18), str(SERVICE_A.start_ticks)]
            (process / "stat").write_text(
                f"{SERVICE_A.pid} (qbzd) " + " ".join(fields) + "\n",
                encoding="utf-8",
            )
            (process / "cgroup").write_text(
                f"0::{SERVICE_A.cgroup}\n", encoding="utf-8"
            )
            (process / "exe").symlink_to(binary)
            service = MODULE.QbzdService(
                pid=SERVICE_A.pid,
                start_ticks=SERVICE_A.start_ticks,
                cgroup=SERVICE_A.cgroup,
                executable=binary,
            )
            with mock.patch.object(
                MODULE.subprocess, "run", return_value=completed
            ) as run:
                result = MODULE.run_qconnect_action(
                    service, "disable", proc_root=proc_root
                )
        self.assertEqual(result, "qconnect ok\n")
        invoked = run.call_args.args[0]
        self.assertEqual(invoked[1:], ("qconnect", "disable"))
        self.assertTrue(invoked[0].startswith("/proc/self/fd/"))
        pinned_fd = int(invoked[0].rsplit("/", 1)[1])
        self.assertEqual(run.call_args.kwargs["pass_fds"], (pinned_fd,))
        self.assertEqual(run.call_args.kwargs["timeout"], MODULE.COMMAND_TIMEOUT_SECONDS)
        self.assertFalse(run.call_args.kwargs["check"])

    def test_qconnect_action_rejects_pid_reuse_before_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = pathlib.Path(tmp) / "proc"
            process = proc_root / str(SERVICE_A.pid)
            process.mkdir(parents=True)
            expected_binary = pathlib.Path(tmp) / "qbzd"
            expected_binary.write_bytes(b"expected-qbzd")
            expected_binary.chmod(0o700)
            reused_binary = pathlib.Path(tmp) / "other-qbzd"
            reused_binary.write_bytes(b"reused-process-image")
            reused_binary.chmod(0o700)
            (process / "comm").write_text("qbzd\n", encoding="utf-8")
            fields = ["S", *(["1"] * 18), str(SERVICE_A.start_ticks)]
            (process / "stat").write_text(
                f"{SERVICE_A.pid} (qbzd) " + " ".join(fields) + "\n",
                encoding="utf-8",
            )
            (process / "cgroup").write_text(
                f"0::{SERVICE_A.cgroup}\n", encoding="utf-8"
            )
            (process / "exe").symlink_to(reused_binary)
            service = MODULE.QbzdService(
                pid=SERVICE_A.pid,
                start_ticks=SERVICE_A.start_ticks,
                cgroup=SERVICE_A.cgroup,
                executable=expected_binary,
            )
            with mock.patch.object(MODULE.subprocess, "run") as run:
                with self.assertRaisesRegex(
                    MODULE.RecoveryError, "qbzd-process-unverified"
                ):
                    MODULE.run_qconnect_action(
                        service, "disable", proc_root=proc_root
                    )
            run.assert_not_called()

    def test_qconnect_action_rejects_unknown_verb_before_subprocess(self):
        with mock.patch.object(MODULE.subprocess, "run") as run:
            with self.assertRaisesRegex(
                MODULE.RecoveryError, "qconnect-action-not-allowed"
            ):
                MODULE.run_qconnect_action(SERVICE_A, "restart")
        run.assert_not_called()

    def test_qconnect_cycle_recovers_after_ninety_seconds_without_daemon_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path, self.candidate_state(qconnect_next_attempt=0.0)
            )
            runner = FakeRunner()
            qconnect = FakeQconnectRunner()
            checked = []
            result = self.reconcile(
                state_path=state_path,
                statuses=[status(), status(), status(), healthy()],
                services=[SERVICE_A] * 5,
                monotonic=[200.0, 202.0, 203.0, 203.5, 204.0],
                wall=[1000.0, 1002.0, 1003.0, 1004.0],
                runner=runner,
                qconnect_runner=qconnect,
                pcm=lambda service: checked.append(service),
            )
            self.assertEqual(result, "recovered:qconnect")
            self.assertEqual(runner.commands, [])
            self.assertEqual(
                [action for _service, action in qconnect.commands],
                ["disable", "enable"],
            )
            self.assertEqual(checked, [SERVICE_A, SERVICE_A, SERVICE_A])
            state = MODULE._load_state(state_path)
            self.assertEqual(state["qconnect_failures"], 0)
            self.assertGreaterEqual(state["qconnect_next_attempt_monotonic"], 1104.0)

    def test_qconnect_cycle_binds_executable_and_blocks_drift_between_actions(self):
        drifted = MODULE.QbzdService(
            pid=SERVICE_A.pid,
            start_ticks=SERVICE_A.start_ticks,
            cgroup=SERVICE_A.cgroup,
            executable=pathlib.Path("/opt/other/qbzd"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path, self.candidate_state(qconnect_next_attempt=0.0)
            )
            qconnect = FakeQconnectRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[status(), status(), status()],
                services=[SERVICE_A, SERVICE_A, SERVICE_A, drifted],
                monotonic=[200.0, 202.0, 203.0, 203.5, 204.0],
                qconnect_runner=qconnect,
            )
            self.assertEqual(
                result, "blocked:qbzd-process-changed-during-qconnect-cycle"
            )
            self.assertEqual(
                [action for _service, action in qconnect.commands], ["disable"]
            )
            state = MODULE._load_state(state_path)
            self.assertEqual(state["qconnect_failures"], 1)
            self.assertEqual(
                state["qconnect_armed_executable"], str(SERVICE_A.executable)
            )

    def test_qconnect_enable_failure_is_durably_backed_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path, self.candidate_state(qconnect_next_attempt=0.0)
            )
            qconnect = FakeQconnectRunner(fail_action="enable")
            result = self.reconcile(
                state_path=state_path,
                statuses=[status(), status(), status()],
                services=[SERVICE_A] * 4,
                monotonic=[200.0, 202.0, 203.0, 203.5, 204.0],
                qconnect_runner=qconnect,
            )
            self.assertEqual(result, "blocked:qconnect-command-failed:enable")
            state = MODULE._load_state(state_path)
            self.assertEqual(state["qconnect_failures"], 1)
            self.assertGreaterEqual(state["qconnect_next_attempt_monotonic"], 324.0)

            followup = self.reconcile(
                state_path=state_path,
                statuses=[status()],
                services=[SERVICE_A],
                monotonic=[250.0],
                qconnect_runner=FakeQconnectRunner(),
            )
            self.assertEqual(followup, "noop:qconnect-backoff")

    def test_qconnect_readback_failure_is_bounded_and_backed_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path, self.candidate_state(qconnect_next_attempt=0.0)
            )
            qconnect = FakeQconnectRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[
                    status(),
                    status(),
                    status(),
                    *([status()] * MODULE.QCONNECT_READBACK_ATTEMPTS),
                ],
                services=[SERVICE_A] * 4,
                monotonic=[200.0, 202.0, 203.0, 203.5, 224.0],
                qconnect_runner=qconnect,
            )
            self.assertEqual(result, "blocked:qconnect-readback")
            state = MODULE._load_state(state_path)
            self.assertEqual(state["qconnect_failures"], 1)
            self.assertGreaterEqual(state["qconnect_next_attempt_monotonic"], 344.0)

    def test_failed_qconnect_cycle_must_restore_enable_before_daemon_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            MODULE._store_state(
                state_path, self.candidate_state(qconnect_next_attempt=0.0)
            )
            runner = FakeRunner()
            qconnect = FakeQconnectRunner(fail_action="disable")
            result = self.reconcile(
                state_path=state_path,
                statuses=[status(), status(), status()],
                services=[SERVICE_A, SERVICE_A, SERVICE_A],
                monotonic=[400.0, 402.0, 403.0, 403.5, 404.0],
                runner=runner,
                qconnect_runner=qconnect,
            )
            self.assertEqual(result, "blocked:qconnect-command-failed:disable")
            self.assertEqual(runner.commands, [])
            self.assertEqual(
                [action for _service, action in qconnect.commands], ["disable"]
            )
            persisted = MODULE._load_state(state_path)
            self.assertTrue(persisted["qconnect_reenable_required"])

            restore = FakeQconnectRunner()
            restored = self.reconcile(
                state_path=state_path,
                statuses=[
                    status(qconnect="disabled"),
                    status(qconnect="retrying"),
                ],
                services=[SERVICE_A],
                monotonic=[430.0, 431.0],
                wall=[1100.0, 1101.0],
                runner=runner,
                qconnect_runner=restore,
            )
            self.assertEqual(restored, "restored:qconnect-enabled")
            self.assertEqual(
                [action for _service, action in restore.commands], ["enable"]
            )
            self.assertFalse(
                MODULE._load_state(state_path)["qconnect_reenable_required"]
            )
            self.assertEqual(runner.commands, [])

    def test_reenable_obligation_accepts_exhausted_as_enabled_control_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            state = self.candidate_state(qconnect_next_attempt=0.0)
            state = MODULE._qconnect_effect_armed_state(state, SERVICE_A, 203.5)
            MODULE._store_state(state_path, state)

            qconnect = FakeQconnectRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[
                    status(qconnect="disabled"),
                    status(qconnect="exhausted", online=False, opened=False),
                ],
                services=[SERVICE_A],
                monotonic=[300.0, 301.0],
                wall=[1000.0, 1001.0],
                qconnect_runner=qconnect,
                network_evidence_realtime=1000.0,
            )
            self.assertEqual(result, "restored:qconnect-enabled")
            self.assertEqual(
                [action for _service, action in qconnect.commands], ["enable"]
            )
            self.assertFalse(
                MODULE._load_state(state_path)["qconnect_reenable_required"]
            )

    def test_qconnect_enable_failure_from_disabled_state_retries_only_enable(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            state = self.candidate_state(qconnect_next_attempt=0.0)
            state = MODULE._qconnect_effect_armed_state(state, SERVICE_A, 203.5)
            MODULE._store_state(state_path, state)
            failing = FakeQconnectRunner(fail_action="enable")
            first = self.reconcile(
                state_path=state_path,
                statuses=[status(qconnect="disabled")],
                services=[SERVICE_A],
                monotonic=[250.0, 251.0],
                qconnect_runner=failing,
            )
            self.assertEqual(
                first, "blocked:qconnect-reenable:qconnect-command-failed:enable"
            )
            self.assertEqual(
                [action for _service, action in failing.commands], ["enable"]
            )
            self.assertTrue(
                MODULE._load_state(state_path)["qconnect_reenable_required"]
            )

            succeeding = FakeQconnectRunner()
            second = self.reconcile(
                state_path=state_path,
                statuses=[
                    status(qconnect="disabled"),
                    status(qconnect="retrying"),
                ],
                services=[SERVICE_A],
                monotonic=[300.0, 301.0],
                qconnect_runner=succeeding,
            )
            self.assertEqual(second, "restored:qconnect-enabled")
            self.assertEqual(
                [action for _service, action in succeeding.commands], ["enable"]
            )
            self.assertFalse(
                MODULE._load_state(state_path)["qconnect_reenable_required"]
            )

    def test_qconnect_reenable_obligation_survives_boot_change(self):
        new_boot = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            state = self.candidate_state(qconnect_next_attempt=0.0)
            state = MODULE._qconnect_effect_armed_state(state, SERVICE_A, 203.5)
            MODULE._store_state(state_path, state)
            qconnect = FakeQconnectRunner()
            result = self.reconcile(
                state_path=state_path,
                statuses=[
                    status(qconnect="disabled"),
                    status(qconnect="retrying"),
                ],
                services=[SERVICE_B],
                monotonic=[10.0, 11.0],
                wall=[1200.0, 1201.0],
                boot=new_boot,
                qconnect_runner=qconnect,
            )
            self.assertEqual(result, "restored:qconnect-enabled")
            self.assertEqual(
                [action for service, action in qconnect.commands],
                ["enable"],
            )
            self.assertEqual(qconnect.commands[0][0], SERVICE_B)
            persisted = MODULE._load_state(state_path)
            self.assertEqual(persisted["boot_id"], new_boot)
            self.assertFalse(persisted["qconnect_reenable_required"])

    def test_legacy_v2_state_migrates_without_granting_qconnect_effect_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            legacy = {
                "schema_version": 2,
                "boot_id": BOOT,
                "candidate_pid": SERVICE_A.pid,
                "candidate_start_ticks": SERVICE_A.start_ticks,
                "retry_since_monotonic": 100.0,
                "failures": 1,
                "next_attempt_monotonic": 900.0,
                "last_recovered_at_unix": None,
                "restart_armed_monotonic": None,
                "restart_armed_pid": None,
                "restart_armed_start_ticks": None,
            }
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            state_path.chmod(0o600)
            migrated = MODULE._load_state(state_path)
            self.assertEqual(migrated["schema_version"], 3)
            self.assertEqual(migrated["qconnect_failures"], 0)
            self.assertEqual(migrated["qconnect_next_attempt_monotonic"], 0.0)
            self.assertFalse(migrated["qconnect_reenable_required"])
            self.assertIsNone(migrated["qconnect_armed_monotonic"])

    def test_state_file_from_environment_uses_systemd_export(self):
        with mock.patch.dict(
            MODULE.os.environ,
            {"STATE_DIRECTORY": "/tmp/qbzd-recovery-state"},
            clear=False,
        ):
            self.assertEqual(
                MODULE._state_file_from_environment(),
                pathlib.Path("/tmp/qbzd-recovery-state/state.json"),
            )

    def test_state_file_from_environment_fails_closed_without_single_absolute_path(self):
        for value in ("", "relative/path", "/tmp/one:/tmp/two"):
            with self.subTest(value=value), mock.patch.dict(
                MODULE.os.environ, {"STATE_DIRECTORY": value}, clear=False
            ):
                with self.assertRaises(MODULE.RecoveryError):
                    MODULE._state_file_from_environment()

    def test_prepare_rollback_projects_exact_legacy_v2_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            state = self.candidate_state(qconnect_next_attempt=77.0)
            MODULE._store_state(state_path, state)

            result = MODULE.prepare_rollback_state(state_path=state_path)

            self.assertEqual(result, "rollback-state:v2-ready")
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 2)
            self.assertEqual(
                set(persisted),
                {
                    "schema_version",
                    "boot_id",
                    "candidate_pid",
                    "candidate_start_ticks",
                    "retry_since_monotonic",
                    "failures",
                    "next_attempt_monotonic",
                    "last_recovered_at_unix",
                    "restart_armed_monotonic",
                    "restart_armed_pid",
                    "restart_armed_start_ticks",
                },
            )

    def test_prepare_rollback_reenables_qconnect_before_v2_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            state = MODULE._qconnect_effect_armed_state(
                self.candidate_state(qconnect_next_attempt=0.0), SERVICE_A, 203.5
            )
            MODULE._store_state(state_path, state)
            statuses = iter(
                [status(qconnect="disabled"), status(qconnect="retrying")]
            )
            qconnect = FakeQconnectRunner()

            result = MODULE.prepare_rollback_state(
                state_path=state_path,
                status_reader=lambda: next(statuses),
                service_reader=lambda: SERVICE_A,
                qconnect_action_runner=qconnect,
                sleeper=lambda _seconds: None,
            )

            self.assertEqual(result, "rollback-state:v2-ready")
            self.assertEqual(
                [action for _service, action in qconnect.commands], ["enable"]
            )
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 2)
            self.assertNotIn("qconnect_reenable_required", persisted)

    def test_prepare_rollback_keeps_v3_obligation_when_reenable_readback_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = self.state_path(tmp)
            state = MODULE._qconnect_effect_armed_state(
                self.candidate_state(qconnect_next_attempt=0.0), SERVICE_A, 203.5
            )
            MODULE._store_state(state_path, state)
            qconnect = FakeQconnectRunner()

            result = MODULE.prepare_rollback_state(
                state_path=state_path,
                status_reader=lambda: status(qconnect="disabled"),
                service_reader=lambda: SERVICE_A,
                qconnect_action_runner=qconnect,
                sleeper=lambda _seconds: None,
            )

            self.assertEqual(result, "blocked:qconnect-reenable-readback")
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 3)
            self.assertTrue(persisted["qconnect_reenable_required"])

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
            self.assertEqual(json.loads(state_path.read_text())["schema_version"], 3)
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
            binary_dir = proc_root / "binary"
            binary_dir.mkdir()
            binary = binary_dir / "qbzd"
            binary.write_bytes(b"fake-qbzd")
            binary.chmod(0o700)
            (process / "exe").symlink_to(binary)

            def service_runner(argv):
                self.assertIn("--property=MainPID", argv)
                return "ActiveState=active\nMainPID=111\n"

            observed = MODULE.read_qbzd_service(
                runner=service_runner, proc_root=proc_root
            )
            self.assertEqual(observed.pid, 111)
            self.assertEqual(observed.start_ticks, 12345)
            self.assertEqual(observed.cgroup, SERVICE_A.cgroup)
            self.assertEqual(observed.executable, binary)

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
