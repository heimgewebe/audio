import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "qbzd_qconnect_recovery.py"
SPEC = importlib.util.spec_from_file_location(
    "qbzd_qconnect_recovery_review_regressions", MODULE_PATH
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

BOOT = "12345678-1234-1234-1234-123456789abc"
SERVICE = MODULE.QbzdService(
    pid=111,
    start_ticks=1000,
    cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/qbzd.service",
)


class SequenceReader:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        if not self.values:
            raise AssertionError("unexpected sequence read")
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeQconnectRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, service, action):
        self.commands.append((service, action))
        return f"qconnect {action} ok"


def status(
    *,
    qconnect="retrying",
    session=False,
    opened=False,
    qconnect_enabled=None,
):
    return MODULE.QbzdStatus(
        api_version=1,
        version="2.0.2",
        auth_state="logged_in",
        network_online=True,
        qconnect_state=qconnect,
        session_active=session,
        audio_backend="alsa",
        configured_device=MODULE.EXPECTED_DEVICE,
        device_present=True,
        device_open=opened,
        playback_state="paused",
        playback_track_id=123456,
        playback_position=0.0,
        uptime_secs=100,
        qconnect_enabled=qconnect_enabled,
    )


class QbzdQconnectReviewRegressionTests(unittest.TestCase):
    def reconcile(
        self,
        *,
        state_path,
        statuses,
        services,
        monotonic,
        qconnect_runner,
    ):
        return MODULE.reconcile_once(
            state_path=state_path,
            status_reader=SequenceReader(statuses),
            service_reader=SequenceReader(services),
            runner=lambda _argv: "",
            qconnect_action_runner=qconnect_runner,
            pcm_idle_checker=lambda _service: None,
            pcm_owned_checker=lambda _service: None,
            sleeper=lambda _seconds: None,
            monotonic_clock=SequenceReader(monotonic),
            wall_clock=SequenceReader([1000.0] * 20),
            boot_id_reader=lambda: BOOT,
        )

    def test_reenable_readback_error_persists_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = pathlib.Path(tmp) / "state.json"
            state = MODULE._qconnect_effect_armed_state(
                MODULE._default_state(BOOT), SERVICE, 100.0
            )
            MODULE._store_state(state_path, state)
            deadline = float(state["qconnect_next_attempt_monotonic"])
            qconnect = FakeQconnectRunner()

            result = self.reconcile(
                state_path=state_path,
                statuses=[
                    status(qconnect="disabled", qconnect_enabled=False),
                    *(
                        [MODULE.RecoveryError("status-unavailable")]
                        * MODULE.QCONNECT_READBACK_ATTEMPTS
                    ),
                ],
                services=[SERVICE],
                monotonic=[deadline, deadline + 1.0],
                qconnect_runner=qconnect,
            )

            self.assertEqual(result, "blocked:qconnect-reenable-readback")
            self.assertEqual(
                [action for _service, action in qconnect.commands], ["enable"]
            )
            persisted = MODULE._load_state(state_path)
            self.assertTrue(persisted["qconnect_reenable_required"])
            self.assertEqual(persisted["qconnect_failures"], 1)
            retry_deadline = float(persisted["qconnect_next_attempt_monotonic"])
            self.assertGreaterEqual(
                retry_deadline,
                deadline + 1.0 + MODULE.QCONNECT_FAILURE_BACKOFF_BASE_SECONDS,
            )

            followup = self.reconcile(
                state_path=state_path,
                statuses=[status(qconnect="disabled", qconnect_enabled=False)],
                services=[],
                monotonic=[retry_deadline - 1.0],
                qconnect_runner=FakeQconnectRunner(),
            )
            self.assertEqual(followup, "noop:qconnect-backoff")

    def test_post_cycle_healthy_but_disabled_is_not_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = pathlib.Path(tmp) / "state.json"
            state = MODULE._default_state(BOOT)
            state.update(
                {
                    "candidate_pid": SERVICE.pid,
                    "candidate_start_ticks": SERVICE.start_ticks,
                    "retry_since_monotonic": 100.0,
                    "qconnect_next_attempt_monotonic": 0.0,
                }
            )
            MODULE._store_state(state_path, state)
            qconnect = FakeQconnectRunner()
            stuck = status(qconnect="retrying", session=False, opened=False)
            contradictory = status(
                qconnect="connected",
                session=True,
                opened=False,
                qconnect_enabled=False,
            )

            result = self.reconcile(
                state_path=state_path,
                statuses=[
                    stuck,
                    stuck,
                    stuck,
                    *([contradictory] * MODULE.QCONNECT_READBACK_ATTEMPTS),
                ],
                services=[SERVICE, SERVICE, SERVICE, SERVICE],
                monotonic=[200.0, 202.0, 203.0, 203.5, 224.0],
                qconnect_runner=qconnect,
            )

            self.assertEqual(result, "blocked:qconnect-reenable-required")
            self.assertEqual(
                [action for _service, action in qconnect.commands],
                ["disable", "enable"],
            )
            persisted = MODULE._load_state(state_path)
            self.assertTrue(persisted["qconnect_reenable_required"])
            self.assertEqual(persisted["qconnect_failures"], 1)


if __name__ == "__main__":
    unittest.main()
