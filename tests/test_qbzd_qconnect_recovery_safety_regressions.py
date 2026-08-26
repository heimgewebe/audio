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


def status(*, qconnect="exhausted", enabled=False):
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
        device_open=False,
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
                    [status(qconnect="exhausted", enabled=False), status(qconnect="retrying", enabled=True)]
                ),
                service_reader=lambda: SERVICE,
                qconnect_action_runner=qconnect,
                monotonic_clock=lambda: 300.0,
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


if __name__ == "__main__":
    unittest.main()
