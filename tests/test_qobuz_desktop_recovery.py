import importlib.util
import inspect
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "qobuz_desktop_recovery.py"
SPEC = importlib.util.spec_from_file_location("qobuz_desktop_recovery", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

USB_SERIAL = "M20000062566"
PW_SERIAL = f"MOTU_M2_{USB_SERIAL}"
MOTU_NAME = f"alsa_output.usb-{PW_SERIAL}-00.Direct__hw_M2__sink"
BUS_PATH = "pci-0000:00:14.0-usb-0:5:1.0"
RESTART = ("systemctl", "--user", "restart", "wireplumber.service")
OBSERVER_STOP = (
    "systemctl",
    "--user",
    "stop",
    "audio-control-level-observer-v1.service",
)
OBSERVER_START = (
    "systemctl",
    "--user",
    "start",
    "audio-control-level-observer-v1.service",
)


def motu_sink(*, serial=PW_SERIAL, bus_path=BUS_PATH, muted=False, volume=65_536):
    name = f"alsa_output.usb-{serial}-00.Direct__hw_M2__sink"
    return {
        "name": name,
        "mute": muted,
        "volume": {
            "front-left": {"value": volume},
            "front-right": {"value": volume},
        },
        "properties": {
            "device.vendor.id": "07fd",
            "device.product.id": "0008",
            "device.serial": serial,
            "device.bus_path": bus_path,
        },
    }


def other_sink():
    return {
        "name": "alsa_output.pci-generic.analog-stereo",
        "mute": False,
        "volume": {"front-left": {"value": 65_536}},
        "properties": {},
    }


class FakeRunner:
    def __init__(self, case, *, sink_present, recover_on_restart=True):
        self.case = case
        self.sink_present = sink_present
        self.recover_on_restart = recover_on_restart
        self.commands = []
        self.default_sink = "alsa_output.pci-generic.analog-stereo"
        self.muted = True
        self.volume = 32_768
        self.observer_state = "active"
        self.close_observer_capture = True
        self.restart_owner = None
        self.inventory_override = None

    def inventory(self):
        if self.inventory_override is not None:
            return self.inventory_override
        result = [other_sink()]
        if self.sink_present:
            result.append(motu_sink(muted=self.muted, volume=self.volume))
        return result

    def __call__(self, argv):
        self.commands.append(argv)
        if argv[:3] == ("systemctl", "--user", "is-active"):
            return "active\n"
        if argv[:4] == (
            "systemctl",
            "--user",
            "show",
            "audio-control-level-observer-v1.service",
        ):
            return f"{self.observer_state}\n"
        if argv == OBSERVER_STOP:
            self.observer_state = "inactive"
            if self.close_observer_capture:
                self.case.close_pcm("c")
            return ""
        if argv == OBSERVER_START:
            self.observer_state = "active"
            return ""
        if argv == RESTART:
            if self.recover_on_restart:
                self.sink_present = True
                if self.restart_owner is not None:
                    pid, executable = self.restart_owner
                    self.case.open_pcm("p", pid=pid, executable=executable)
            return ""
        if argv == ("pactl", "--format=json", "list", "sinks"):
            return json.dumps(self.inventory())
        if argv == ("pactl", "info"):
            return f"Default Sink: {self.default_sink}\n"
        if argv[:3] == ("pactl", "set-sink-volume", MOTU_NAME):
            self.volume = 65_536
            return ""
        if argv[:3] == ("pactl", "set-sink-mute", MOTU_NAME):
            self.muted = False
            return ""
        if argv == ("pactl", "set-default-sink", MOTU_NAME):
            self.default_sink = MOTU_NAME
            return ""
        raise AssertionError(f"unexpected command: {argv!r}")


class QobuzDesktopRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.asound = self.root / "proc" / "asound"
        self.proc = self.root / "proc"
        self.sound_class = self.root / "sys" / "class" / "sound"
        self.sys_devices = self.root / "sys" / "devices"
        self.state = self.root / "state" / "state.json"
        self.make_card()

    def make_card(
        self,
        number=1,
        *,
        card_id="M2",
        usb_id="07fd:0008",
        serial=USB_SERIAL,
        vendor_id="07fd",
        product_id="0008",
        controller="0000:00:14.0",
        port_chain=None,
    ):
        card = self.asound / f"card{number}"
        card.mkdir(parents=True, exist_ok=True)
        (card / "id").write_text(f"{card_id}\n", encoding="utf-8")
        (card / "usbid").write_text(f"{usb_id}\n", encoding="utf-8")
        for suffix in ("p", "c"):
            substream = card / f"pcm0{suffix}" / "sub0"
            substream.mkdir(parents=True, exist_ok=True)
            (substream / "hw_params").write_text("closed\n", encoding="utf-8")
            (substream / "status").write_text("closed\n", encoding="utf-8")

        ports = str(number + 4) if port_chain is None else port_chain
        usb_root = self.sys_devices / f"pci0000:00/{controller}/usb1"
        usb = usb_root
        port_parts = ports.split(".")
        for index in range(1, len(port_parts) + 1):
            usb /= f"1-{'.'.join(port_parts[:index])}"
        interface = usb / f"1-{ports}:1.0"
        interface.mkdir(parents=True, exist_ok=True)
        (usb / "idVendor").write_text(f"{vendor_id}\n", encoding="utf-8")
        (usb / "idProduct").write_text(f"{product_id}\n", encoding="utf-8")
        (usb / "serial").write_text(f"{serial}\n", encoding="utf-8")
        class_card = self.sound_class / f"card{number}"
        class_card.mkdir(parents=True, exist_ok=True)
        device_link = class_card / "device"
        if device_link.exists() or device_link.is_symlink():
            device_link.unlink()
        device_link.symlink_to(interface)
        return card

    def pcm(self, direction, *, number=1):
        return self.asound / f"card{number}" / f"pcm0{direction}" / "sub0"

    def close_pcm(self, direction, *, number=1):
        (self.pcm(direction, number=number) / "hw_params").write_text(
            "closed\n", encoding="utf-8"
        )
        (self.pcm(direction, number=number) / "status").write_text(
            "closed\n", encoding="utf-8"
        )

    def open_pcm(
        self, direction, *, number=1, pid=4242, executable="/opt/qobuz/qobuz"
    ):
        (self.pcm(direction, number=number) / "hw_params").write_text(
            "access: MMAP_INTERLEAVED\nformat: S32_LE\nrate: 96000\n",
            encoding="utf-8",
        )
        (self.pcm(direction, number=number) / "status").write_text(
            f"state: RUNNING\nowner_pid: {pid}\n", encoding="utf-8"
        )
        process = self.proc / str(pid)
        process.mkdir(parents=True, exist_ok=True)
        exe = process / "exe"
        if exe.exists() or exe.is_symlink():
            exe.unlink()
        exe.symlink_to(executable)

    def runner(self, *, sink_present, recover_on_restart=True):
        # Normal operation includes the repository-owned capture observer.
        self.open_pcm("c", pid=100, executable=MODULE.PIPEWIRE_EXECUTABLE)
        return FakeRunner(
            self, sink_present=sink_present, recover_on_restart=recover_on_restart
        )

    def reconcile(self, runner, *, now=1_000.0):
        return MODULE.reconcile_once(
            asound_root=self.asound,
            state_path=self.state,
            sound_class_root=self.sound_class,
            sys_devices_root=self.sys_devices,
            proc_root=self.proc,
            runner=runner,
            now=now,
            sleeper=lambda _seconds: None,
        )

    def state_payload(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_healthy_sink_without_pending_handoff_preserves_intentional_default(self):
        runner = self.runner(sink_present=True)
        runner.muted = False
        runner.volume = 65_536

        self.assertEqual(self.reconcile(runner), "noop:sink-present")
        self.assertEqual(runner.default_sink, "alsa_output.pci-generic.analog-stereo")
        self.assertFalse(
            any(command[1].startswith("set-") for command in runner.commands)
        )
        self.assertNotIn(OBSERVER_STOP, runner.commands)
        self.assertFalse(self.state.exists())

    def test_exact_controller_and_usb_chain_identity_is_accepted(self):
        self.make_card(
            1,
            controller="0000:00:01.2/0000:02:00.0",
            port_chain="9.2",
        )
        physical = MODULE.resolve_unique_motu_card(
            self.asound,
            sound_class_root=self.sound_class,
            sys_devices_root=self.sys_devices,
        )
        self.assertIsNotNone(physical)
        expected_bus_path = "pci-0000:02:00.0-usb-0:9.2:1.0"
        self.assertEqual(physical.bus_path, expected_bus_path)

        runner = self.runner(sink_present=False)
        runner.inventory_override = [
            other_sink(), motu_sink(bus_path=expected_bus_path)
        ]
        self.assertEqual(self.reconcile(runner), "noop:sink-present")

    def test_exact_bus_path_with_missing_serial_blocks_without_restart(self):
        runner = self.runner(sink_present=False)
        malformed = other_sink()
        malformed["properties"]["device.bus_path"] = BUS_PATH
        runner.inventory_override = [other_sink(), malformed]

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(OBSERVER_STOP, runner.commands)
        self.assertNotIn(RESTART, runner.commands)

    def test_exact_bus_path_with_malformed_vendor_and_name_blocks_without_restart(self):
        runner = self.runner(sink_present=False)
        malformed = other_sink()
        malformed["name"] = "alsa_output.usb-broken.contract"
        malformed["properties"].update(
            {
                "device.vendor.id": "07fg",
                "device.product.id": "0008",
                "device.serial": "broken",
                "device.bus_path": BUS_PATH,
            }
        )
        runner.inventory_override = [other_sink(), malformed]

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(OBSERVER_STOP, runner.commands)
        self.assertNotIn(RESTART, runner.commands)

    def test_unrelated_bus_path_and_metadata_remain_irrelevant(self):
        physical = MODULE.resolve_unique_motu_card(
            self.asound,
            sound_class_root=self.sound_class,
            sys_devices_root=self.sys_devices,
        )
        self.assertIsNotNone(physical)
        unrelated = other_sink()
        unrelated["properties"]["device.bus_path"] = (
            "pci-0000:00:14.0-usb-0:9:1.0"
        )

        self.assertIsNone(MODULE.resolve_motu_sink([unrelated], physical))

    def test_normal_exact_sink_identity_is_accepted(self):
        physical = MODULE.resolve_unique_motu_card(
            self.asound,
            sound_class_root=self.sound_class,
            sys_devices_root=self.sys_devices,
        )
        self.assertIsNotNone(physical)
        exact = motu_sink()

        resolved = MODULE.resolve_motu_sink([other_sink(), exact], physical)

        self.assertIsNotNone(resolved)
        self.assertIs(resolved[0], exact)
        self.assertEqual(resolved[1].name, MOTU_NAME)

    def test_same_usb_suffix_on_different_pci_controller_is_rejected(self):
        runner = self.runner(sink_present=False)
        runner.inventory_override = [
            other_sink(),
            motu_sink(bus_path="pci-0000:02:00.0-usb-0:5:1.0"),
        ]

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(OBSERVER_STOP, runner.commands)
        self.assertNotIn(RESTART, runner.commands)

    def test_moved_usb_port_is_rejected(self):
        runner = self.runner(sink_present=False)
        runner.inventory_override = [
            other_sink(),
            motu_sink(bus_path="pci-0000:00:14.0-usb-0:6:1.0"),
        ]

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(RESTART, runner.commands)

    def test_malformed_canonical_sysfs_path_is_rejected(self):
        device_link = self.sound_class / "card1" / "device"
        usb = device_link.resolve().parent
        malformed_interface = usb / "1-5:not-an-interface"
        malformed_interface.mkdir()
        device_link.unlink()
        device_link.symlink_to(malformed_interface)
        runner = self.runner(sink_present=False)

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertEqual(runner.commands, [])

    def test_repo_observer_is_quiesced_and_restored_around_single_restart(self):
        runner = self.runner(sink_present=False)

        self.assertEqual(self.reconcile(runner), "recovered")
        self.assertEqual(runner.commands.count(RESTART), 1)
        self.assertEqual(runner.commands.count(OBSERVER_STOP), 1)
        self.assertEqual(runner.commands.count(OBSERVER_START), 1)
        self.assertLess(
            runner.commands.index(OBSERVER_STOP), runner.commands.index(RESTART)
        )
        self.assertGreater(
            runner.commands.index(OBSERVER_START), runner.commands.index(RESTART)
        )
        state = self.state_payload()
        self.assertFalse(state["handoff_pending"])
        self.assertEqual(state["next_attempt_at"], 1_120.0)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.parent.stat().st_mode & 0o777, 0o700)

    def test_persistent_quiesce_marker_repairs_observer_after_process_crash(self):
        marker = self.state.with_name("level-observer-quiesced")
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"level-observer-quiesced-v1\n")
        marker.chmod(0o600)
        runner = self.runner(sink_present=True)
        runner.observer_state = "inactive"

        self.assertEqual(self.reconcile(runner), "noop:sink-present")
        self.assertEqual(runner.commands[0], OBSERVER_START)
        self.assertEqual(runner.observer_state, "active")
        self.assertFalse(marker.exists())

    def test_actual_capture_remaining_after_observer_stop_blocks_restart(self):
        runner = self.runner(sink_present=False)
        runner.close_observer_capture = False

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(RESTART, runner.commands)
        self.assertEqual(runner.commands.count(OBSERVER_START), 1)
        self.assertEqual(runner.observer_state, "active")
        self.assertFalse(self.state.exists())

    def test_active_capture_on_another_card_blocks_before_observer_quiesce(self):
        self.make_card(
            0,
            card_id="PCH",
            usb_id="1234:5678",
            serial="OTHER_CAPTURE",
            vendor_id="1234",
            product_id="5678",
        )
        runner = self.runner(sink_present=False)
        self.open_pcm("c", number=0, pid=7000, executable="/usr/bin/recorder")

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(OBSERVER_STOP, runner.commands)
        self.assertNotIn(RESTART, runner.commands)

    def test_active_playback_on_another_card_blocks_before_observer_quiesce(self):
        self.make_card(
            0,
            card_id="HDMI",
            usb_id="1234:5678",
            serial="OTHER_PLAYBACK",
            vendor_id="1234",
            product_id="5678",
        )
        runner = self.runner(sink_present=False)
        self.open_pcm("p", number=0, pid=7001, executable="/usr/bin/player")

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(OBSERVER_STOP, runner.commands)
        self.assertNotIn(RESTART, runner.commands)

    def test_unreadable_or_unknown_other_card_pcm_status_blocks(self):
        self.make_card(
            0,
            card_id="PCH",
            usb_id="1234:5678",
            serial="OTHER_AMBIGUOUS",
            vendor_id="1234",
            product_id="5678",
        )
        for payload in (None, "state: UNKNOWN\n"):
            with self.subTest(payload=payload):
                status = self.pcm("c", number=0) / "status"
                if payload is None:
                    status.unlink(missing_ok=True)
                else:
                    status.write_text(payload, encoding="utf-8")
                runner = self.runner(sink_present=False)

                self.assertEqual(self.reconcile(runner), "blocked")
                self.assertNotIn(OBSERVER_STOP, runner.commands)
                self.assertNotIn(RESTART, runner.commands)

    def test_all_other_card_pcms_closed_permits_recovery(self):
        self.make_card(
            0,
            card_id="PCH",
            usb_id="1234:5678",
            serial="OTHER_CLOSED",
            vendor_id="1234",
            product_id="5678",
        )
        runner = self.runner(sink_present=False)

        self.assertEqual(self.reconcile(runner), "recovered")
        self.assertEqual(runner.commands.count(RESTART), 1)

    def test_direct_playback_blocks_restart_and_observer_is_restored(self):
        runner = self.runner(sink_present=False)
        self.open_pcm("p")

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(RESTART, runner.commands)
        self.assertNotIn(OBSERVER_STOP, runner.commands)
        self.assertEqual(runner.observer_state, "active")

    def test_ambiguous_pcm_blocks_before_absence_stabilization(self):
        runner = self.runner(sink_present=False)
        self.open_pcm("p", executable=MODULE.PIPEWIRE_EXECUTABLE)
        (self.pcm("p") / "status").write_text("closed\n", encoding="utf-8")
        sleeper = mock.Mock()

        result = MODULE.reconcile_once(
            asound_root=self.asound,
            state_path=self.state,
            sound_class_root=self.sound_class,
            sys_devices_root=self.sys_devices,
            proc_root=self.proc,
            runner=runner,
            now=1_000.0,
            sleeper=sleeper,
        )

        self.assertEqual(result, "blocked")
        sleeper.assert_not_called()
        self.assertNotIn(OBSERVER_STOP, runner.commands)
        self.assertNotIn(RESTART, runner.commands)

    def test_pcm_opening_at_final_boundary_prevents_restart(self):
        runner = self.runner(sink_present=False)
        original_store = MODULE._store_state

        def store_then_race(path, state):
            original_store(path, state)
            self.open_pcm("p", pid=9001, executable="/opt/qobuz/qobuz")

        with mock.patch.object(MODULE, "_store_state", side_effect=store_then_race):
            self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(RESTART, runner.commands)
        self.assertEqual(runner.observer_state, "active")
        self.assertTrue(self.state_payload()["handoff_pending"])

    def test_swapped_serial_sink_is_ambiguous_and_never_restarted(self):
        runner = self.runner(sink_present=False)
        runner.inventory_override = [other_sink(), motu_sink(serial="MOTU_M2_OTHER")]

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(RESTART, runner.commands)
        self.assertNotIn(OBSERVER_STOP, runner.commands)

    def test_malformed_motu_looking_candidate_is_ambiguous(self):
        runner = self.runner(sink_present=False)
        malformed = motu_sink()
        del malformed["properties"]["device.serial"]
        runner.inventory_override = [other_sink(), malformed]

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(RESTART, runner.commands)

    def test_bus_port_mismatch_is_ambiguous(self):
        runner = self.runner(sink_present=False)
        runner.inventory_override = [
            other_sink(),
            motu_sink(bus_path="pci-x-usb-0:9:1.0"),
        ]

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(RESTART, runner.commands)

    def test_missing_physical_usb_serial_fails_closed_before_effects(self):
        serial = next(self.sys_devices.rglob("serial"))
        serial.unlink()
        runner = self.runner(sink_present=False)

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertEqual(runner.commands, [])

    def test_handoff_pending_allows_natural_reappearance_to_be_normalized(self):
        first = self.runner(sink_present=False, recover_on_restart=False)
        self.assertEqual(self.reconcile(first), "blocked")
        self.assertTrue(self.state_payload()["handoff_pending"])

        second = self.runner(sink_present=True)
        self.assertEqual(self.reconcile(second, now=1_001.0), "handoff-restored")
        self.assertNotIn(RESTART, second.commands)
        self.assertEqual(second.default_sink, MOTU_NAME)
        self.assertFalse(self.state_payload()["handoff_pending"])

    def test_natural_reappearance_during_stabilization_is_a_healthy_noop(self):
        runner = self.runner(sink_present=False)

        def reappear(_seconds):
            runner.sink_present = True

        result = MODULE.reconcile_once(
            asound_root=self.asound,
            state_path=self.state,
            sound_class_root=self.sound_class,
            sys_devices_root=self.sys_devices,
            proc_root=self.proc,
            runner=runner,
            now=1_000.0,
            sleeper=reappear,
        )

        self.assertEqual(result, "noop:sink-present")
        self.assertNotIn(RESTART, runner.commands)
        self.assertNotIn(OBSERVER_STOP, runner.commands)
        self.assertFalse(self.state.exists())

    def test_pending_handoff_reappearance_during_stabilization_is_completed(self):
        first = self.runner(sink_present=False, recover_on_restart=False)
        self.assertEqual(self.reconcile(first), "blocked")
        second = self.runner(sink_present=False)

        def reappear(_seconds):
            second.sink_present = True

        result = MODULE.reconcile_once(
            asound_root=self.asound,
            state_path=self.state,
            sound_class_root=self.sound_class,
            sys_devices_root=self.sys_devices,
            proc_root=self.proc,
            runner=second,
            now=1_031.0,
            sleeper=reappear,
        )

        self.assertEqual(result, "handoff-restored")
        self.assertNotIn(RESTART, second.commands)
        self.assertEqual(second.default_sink, MOTU_NAME)
        self.assertFalse(self.state_payload()["handoff_pending"])

    def test_backoff_and_handoff_survive_process_stop_start(self):
        first = self.runner(sink_present=False, recover_on_restart=False)
        self.assertEqual(self.reconcile(first), "blocked")

        new_process_runner = self.runner(sink_present=False)
        self.assertEqual(
            self.reconcile(new_process_runner, now=1_001.0), "noop:backoff"
        )
        self.assertNotIn(OBSERVER_STOP, new_process_runner.commands)
        self.assertNotIn(RESTART, new_process_runner.commands)

    def test_pipewire_owned_playback_after_sink_recreation_is_valid_readback(self):
        runner = self.runner(sink_present=False)
        runner.restart_owner = (321, MODULE.PIPEWIRE_EXECUTABLE)

        self.assertEqual(self.reconcile(runner), "recovered")
        self.assertEqual(runner.default_sink, MOTU_NAME)

    def test_direct_client_reacquisition_after_recreation_blocks_normalization(self):
        runner = self.runner(sink_present=False)
        runner.restart_owner = (322, "/opt/qobuz/qobuz")

        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertIn(RESTART, runner.commands)
        self.assertFalse(
            any(command[1].startswith("set-") for command in runner.commands)
        )
        self.assertTrue(self.state_payload()["handoff_pending"])
        self.assertEqual(runner.observer_state, "active")

    def test_ambiguous_cards_and_unreadable_capture_pcm_fail_closed(self):
        runner = self.runner(sink_present=False)
        self.make_card(2, card_id="M2_1", serial="M20000099999")
        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertEqual(runner.commands, [])

        card2_device = (self.sound_class / "card2" / "device").resolve()
        usb2 = card2_device.parent
        (usb2 / "idVendor").write_text("1234\n", encoding="utf-8")
        (usb2 / "idProduct").write_text("5678\n", encoding="utf-8")
        (self.pcm("c") / "status").unlink()
        runner.close_observer_capture = False
        self.assertEqual(self.reconcile(runner), "blocked")
        self.assertNotIn(RESTART, runner.commands)

    def test_m2_absent_is_a_noop(self):
        card_device = (self.sound_class / "card1" / "device").resolve()
        usb = card_device.parent
        (usb / "idVendor").write_text("1234\n", encoding="utf-8")
        (usb / "idProduct").write_text("5678\n", encoding="utf-8")
        runner = self.runner(sink_present=False)

        self.assertEqual(self.reconcile(runner), "noop:m2-absent")
        self.assertEqual(runner.commands, [])

    def test_recovery_has_no_dependency_on_stale_qobuz_api_fields(self):
        source = inspect.getsource(MODULE)
        self.assertNotIn("playback_state", source)
        self.assertNotIn("device_open", source)
        self.assertNotIn("qbzd", source.casefold())
        runner = self.runner(sink_present=False)
        self.assertEqual(self.reconcile(runner), "recovered")
        self.assertTrue(
            all(command[0] in {"pactl", "systemctl"} for command in runner.commands)
        )


if __name__ == "__main__":
    unittest.main()
