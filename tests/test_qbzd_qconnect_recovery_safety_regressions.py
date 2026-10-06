import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "qbzd_qconnect_recovery.py"
SPEC = importlib.util.spec_from_file_location(
    "qbzd_qconnect_recovery_safety_regressions", MODULE_PATH
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


def status(*, qconnect="exhausted", enabled=False, opened=False):
    return MODULE.QbzdStatus(
        api_version=1,
        version="2.0.2",
        auth_state="logged_in",
        network_online=True,
        qconnect_state=qconnect,
        session_active=False,
        audio_backend="alsa",
        configured_device="front:CARD=M2,DEV=0",
        device_present=True,
        device_open=opened,
        playback_state="paused",
        playback_track_id=123456,
        playback_position=0.0,
        uptime_secs=100,
        qconnect_enabled=enabled,
    )


class SequenceReader:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        if not self.values:
            raise AssertionError("unexpected sequence read")
        return self.values.pop(0)


class FakeQconnectRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, service, action):
        self.commands.append((service, action))
        return f"qconnect {action} ok"


def candidate_state():
    state = MODULE._default_state(BOOT)
    state.update(
        {
            "candidate_pid": SERVICE.pid,
            "candidate_start_ticks": SERVICE.start_ticks,
            "retry_since_monotonic": 100.0,
            "qconnect_next_attempt_monotonic": 0.0,
        }
    )
    return state


class QbzdQconnectSafetyRegressionTests(unittest.TestCase):
    def test_explicit_enabled_false_overrides_exhausted_lifecycle(self):
        self.assertFalse(MODULE._qconnect_control_enabled(status()))
        self.assertTrue(
            MODULE._qconnect_control_enabled(status(qconnect="retrying", enabled=True))
        )

    def test_stale_exhausted_snapshot_cannot_clear_reenable_obligation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = pathlib.Path(tmp) / "state.json"
            state = MODULE._qconnect_effect_armed_state(
                MODULE._default_state(BOOT), SERVICE, 203.5
            )
            MODULE._store_state(state_path, state)
            qconnect = FakeQconnectRunner()

            result = MODULE.reconcile_once(
                state_path=state_path,
                status_reader=SequenceReader(
                    [
                        status(qconnect="exhausted", enabled=False),
                        status(qconnect="retrying", enabled=True),
                    ]
                ),
                service_reader=lambda: SERVICE,
                qconnect_action_runner=qconnect,
                monotonic_clock=lambda: 400.0,
                wall_clock=lambda: 1000.0,
                boot_id_reader=lambda: BOOT,
                sleeper=lambda _seconds: None,
            )

            self.assertEqual(result, "restored:qconnect-enabled")
            self.assertEqual(
                [(service.pid, action) for service, action in qconnect.commands],
                [(SERVICE.pid, "enable")],
            )
            self.assertFalse(
                MODULE._load_state(state_path)["qconnect_reenable_required"]
            )

    def test_kernel_running_rejects_paused_open_effect_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            proc_root = root / "proc"
            asound_root = root / "asound"
            process = proc_root / str(SERVICE.pid)
            process.mkdir(parents=True)
            fields = ["S", *(["1"] * 18), str(SERVICE.start_ticks)]
            (process / "stat").write_text(
                f"{SERVICE.pid} (qbzd) " + " ".join(fields) + "\n",
                encoding="utf-8",
            )
            (process / "status").write_text(
                f"Name:\tqbzd\nTgid:\t{SERVICE.pid}\n", encoding="utf-8"
            )
            (process / "cgroup").write_text(
                f"0::{SERVICE.cgroup}\n", encoding="utf-8"
            )

            card = asound_root / "card2"
            substream = card / "pcm0p" / "sub0"
            substream.mkdir(parents=True)
            (card / "id").write_text("M2\n", encoding="utf-8")
            pcm_status = substream / "status"
            pcm_status.write_text(
                f"state: RUNNING\nowner_pid: {SERVICE.pid}\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(
                MODULE.RecoveryError, "qbzd-target-pcm-not-paused"
            ):
                MODULE.require_qbzd_pcm_paused(
                    SERVICE, asound_root=asound_root, proc_root=proc_root
                )

            pcm_status.write_text(
                f"state: PAUSED\nowner_pid: {SERVICE.pid}\n", encoding="utf-8"
            )
            MODULE.require_qbzd_pcm_paused(
                SERVICE, asound_root=asound_root, proc_root=proc_root
            )


    def test_selected_qconnect_pcm_mode_must_stay_stable_between_gates(self):
        stuck = status(qconnect="retrying", opened=True)
        cases = (
            (
                "paused-to-closed-at-second-gate",
                [None, "closed", "closed"],
                [stuck, stuck],
                [SERVICE, SERVICE],
                [200.0, 202.0],
            ),
            (
                "closed-to-paused-at-second-gate",
                ["closed", "closed", None],
                [stuck, stuck],
                [SERVICE, SERVICE],
                [200.0, 202.0],
            ),
            (
                "paused-to-closed-at-final-gate",
                [None, None, "closed", "closed"],
                [stuck, stuck, stuck],
                [SERVICE, SERVICE, SERVICE],
                [200.0, 202.0, 203.0],
            ),
        )

        for label, outcomes, statuses, services, monotonic in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                state_path = pathlib.Path(tmp) / "state.json"
                MODULE._store_state(state_path, candidate_state())
                qconnect = FakeQconnectRunner()
                remaining = iter(outcomes)

                def pcm_owned(_service):
                    outcome = next(remaining)
                    if outcome == "closed":
                        raise MODULE.RecoveryError(
                            "qbzd-target-pcm-owner-not-found"
                        )

                result = MODULE.reconcile_once(
                    state_path=state_path,
                    status_reader=SequenceReader(statuses),
                    service_reader=SequenceReader(services),
                    qconnect_action_runner=qconnect,
                    pcm_idle_checker=lambda _service: None,
                    pcm_owned_checker=pcm_owned,
                    sleeper=lambda _seconds: None,
                    monotonic_clock=SequenceReader(monotonic),
                    wall_clock=lambda: 1000.0,
                    boot_id_reader=lambda: BOOT,
                )

                self.assertEqual(result, "blocked:qconnect-pcm-mode-changed")
                self.assertEqual(qconnect.commands, [])

    def test_production_effect_edge_rechecks_selected_qconnect_pcm_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = pathlib.Path(tmp) / "state.json"
            MODULE._store_state(state_path, candidate_state())
            stuck = status(qconnect="retrying", opened=True)
            qconnect = FakeQconnectRunner()
            observed_services = []
            modes = iter(
                [
                    MODULE.QCONNECT_PCM_MODE_PAUSED_OWNED,
                    MODULE.QCONNECT_PCM_MODE_PAUSED_OWNED,
                    MODULE.QCONNECT_PCM_MODE_PAUSED_OWNED,
                    MODULE.QCONNECT_PCM_MODE_CLOSED_IDLE,
                ]
            )

            def mode_probe(service, *, pcm_idle_checker, pcm_paused_checker):
                observed_services.append(service)
                return next(modes)

            original = MODULE.require_qconnect_pcm_safe
            MODULE.require_qconnect_pcm_safe = mode_probe
            try:
                result = MODULE.reconcile_once(
                    state_path=state_path,
                    status_reader=SequenceReader([stuck, stuck, stuck, stuck]),
                    service_reader=SequenceReader([SERVICE] * 4),
                    qconnect_action_runner=qconnect,
                    pcm_idle_checker=lambda _service: None,
                    sleeper=lambda _seconds: None,
                    monotonic_clock=SequenceReader([200.0, 202.0, 203.0]),
                    wall_clock=lambda: 1000.0,
                    boot_id_reader=lambda: BOOT,
                )
            finally:
                MODULE.require_qconnect_pcm_safe = original

            self.assertEqual(result, "blocked:qconnect-pcm-mode-changed")
            self.assertEqual(observed_services, [SERVICE] * 4)
            self.assertEqual(qconnect.commands, [])

    def test_closed_idle_fallback_uses_real_alsa_gates_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            proc_root = root / "proc"
            asound_root = root / "asound"

            process = proc_root / str(SERVICE.pid)
            process.mkdir(parents=True)
            fields = ["S", *(["1"] * 18), str(SERVICE.start_ticks)]
            (process / "stat").write_text(
                f"{SERVICE.pid} (qbzd) " + " ".join(fields) + "\n",
                encoding="utf-8",
            )
            (process / "cgroup").write_text(
                f"0::{SERVICE.cgroup}\n", encoding="utf-8"
            )

            target = asound_root / "card2"
            target_status = target / "pcm0p" / "sub0" / "status"
            target_status.parent.mkdir(parents=True)
            (target / "id").write_text("M2\n", encoding="utf-8")
            target_status.write_text("closed\n", encoding="utf-8")

            def production_idle(service):
                return MODULE.require_qbzd_pcm_idle(
                    service, asound_root=asound_root, proc_root=proc_root
                )

            def production_paused(service):
                return MODULE.require_qbzd_pcm_paused(
                    service, asound_root=asound_root, proc_root=proc_root
                )

            self.assertEqual(
                MODULE.require_qconnect_pcm_safe(
                    SERVICE,
                    pcm_idle_checker=production_idle,
                    pcm_paused_checker=production_paused,
                ),
                MODULE.QCONNECT_PCM_MODE_CLOSED_IDLE,
            )

            other_status = asound_root / "card3" / "pcm0p" / "sub0" / "status"
            other_status.parent.mkdir(parents=True)
            other_status.write_text(
                "state: RUNNING\nowner_pid: 222\n", encoding="utf-8"
            )
            qbzd_owner = proc_root / "222"
            qbzd_owner.mkdir(parents=True)
            (qbzd_owner / "status").write_text(
                f"Name:\tqbzd\nTgid:\t{SERVICE.pid}\n", encoding="utf-8"
            )
            (qbzd_owner / "cgroup").write_text(
                f"0::{SERVICE.cgroup}\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(MODULE.RecoveryError, "qbzd-pcm-open"):
                MODULE.require_qconnect_pcm_safe(
                    SERVICE,
                    pcm_idle_checker=production_idle,
                    pcm_paused_checker=production_paused,
                )

            other_status.write_text("closed\n", encoding="utf-8")
            target_status.write_text(
                "state: PAUSED\nowner_pid: 444\n", encoding="utf-8"
            )
            foreign_owner = proc_root / "444"
            foreign_owner.mkdir(parents=True)
            (foreign_owner / "status").write_text(
                "Name:\tpipewire\nTgid:\t444\n", encoding="utf-8"
            )
            (foreign_owner / "cgroup").write_text(
                "0::/user.slice/user-1000.slice/user@1000.service/"
                "session.slice/pipewire.service\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                MODULE.RecoveryError, "qbzd-target-pcm-owner-mismatch"
            ):
                MODULE.require_qconnect_pcm_safe(
                    SERVICE,
                    pcm_idle_checker=production_idle,
                    pcm_paused_checker=production_paused,
                )


if __name__ == "__main__":
    unittest.main()