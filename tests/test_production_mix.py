from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "production_mix_test_module", ROOT / "scripts/production_mix.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ProductionMixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.state = self.base / "state"
        self.voice_name = "alsa_input.usb-MOTU_M2_M20000000000-00.Direct__hw_M2__source"
        self.roland_name = (
            "alsa_input.usb-Roland_Roland_Digital_Piano_SERIAL-00.analog-stereo"
        )
        self.monitor_name = (
            "alsa_output.usb-MOTU_M2_M20000000000-00.Direct__hw_M2__sink"
        )
        self.fake_loopback = self.base / "pw-loopback"
        self.fake_loopback.write_text("#!/bin/sh\nexit 0\n")
        self.fake_loopback.chmod(0o755)
        self.fake_executable_binding = MODULE.executable_binding(self.fake_loopback)
        executable_patcher = mock.patch.object(
            MODULE,
            "executable_binding",
            return_value=self.fake_executable_binding,
        )
        executable_patcher.start()
        self.addCleanup(executable_patcher.stop)

    @staticmethod
    def _fingerprinted(value: dict[str, object]) -> dict[str, object]:
        value = dict(value)
        value["fingerprint"] = MODULE.canonical_sha256(value)
        return value

    def voice_identity(self, *, node_name: str | None = None) -> dict[str, object]:
        return self._fingerprinted(
            {
                "vendor_id": "07fd",
                "product_id": "0008",
                "serial_sha256": "1" * 64,
                "node_name_sha256": hashlib.sha256(
                    (node_name or self.voice_name).encode()
                ).hexdigest(),
                "bus_path_sha256": "2" * 64,
                "sample_format": "s32le",
                "sample_rate_hz": 48_000,
                "channels": 2,
                "muted": False,
                "unity_volume": True,
            }
        )

    def roland_identity(self, *, channels: int = 2) -> dict[str, object]:
        return self._fingerprinted(
            {
                "source_kind": "usb-audio",
                "vendor_id": "0582",
                "product_id": "01b1",
                "serial_sha256": "3" * 64,
                "node_name_sha256": hashlib.sha256(
                    self.roland_name.encode()
                ).hexdigest(),
                "bus_path_sha256": "4" * 64,
                "sample_format": "s24le",
                "sample_rate_hz": 44_100,
                "channels": channels,
                "muted": False,
                "unity_volume": True,
            }
        )

    def monitor_identity(
        self, *, channels: int = 2, muted: bool = False
    ) -> dict[str, object]:
        return self._fingerprinted(
            {
                "vendor_id": "07fd",
                "product_id": "0008",
                "serial_sha256": "5" * 64,
                "node_name_sha256": hashlib.sha256(
                    self.monitor_name.encode()
                ).hexdigest(),
                "bus_path_sha256": "6" * 64,
                "sample_format": "s32le",
                "sample_rate_hz": 48_000,
                "channels": channels,
                "muted": muted,
                "volume_sha256": "7" * 64,
            }
        )

    def ready_plan(
        self,
        *,
        selected_input: str = "input-1",
        graph_rate: int = 48_000,
        voice_identity: dict[str, object] | None = None,
        roland_identity: dict[str, object] | None = None,
        monitor_identity: dict[str, object] | None = None,
        service: dict[str, object] | None = None,
        conflicts: dict[str, object] | None = None,
    ) -> dict[str, object]:
        physical = {
            "state_path": str(self.base / "physical.json"),
            "state_sha256": "8" * 64,
            "facts": {
                "rode_nt1a_connected": True,
                "rode_nt1a_motu_input": selected_input,
                "motu_phantom_48v": "on",
                "motu_input_gain_reference": "mark 10",
                "motu_output_to_lake_people": "line out 1-2",
                "lake_people_gain_setting": "0 dB",
                "lake_people_volume_reference": "mark 12",
                "focal_connected_output": "headphone out",
            },
            "error": None,
        }
        laboratory = {
            "state_path": str(self.base / "laboratory.json"),
            "state_sha256": "9" * 64,
            "resolved": ["resampling-decision", "voice-level-measurement"],
            "invalidated": {},
            "receipt_sha256": {
                "voice-level-measurement": "a" * 64,
                "resampling-decision": "b" * 64,
            },
            "error": None,
        }
        voice = voice_identity or self.voice_identity()
        roland = roland_identity or self.roland_identity()
        monitor = monitor_identity or self.monitor_identity()
        empty_service = {
            "unit": "audio-production-mix.service",
            "load_state": "not-found",
            "active_state": "inactive",
            "sub_state": "dead",
            "result": "success",
            "managed": False,
            "spec_sha256": None,
            "identity": None,
            "limits": None,
        }
        clear = {
            "clear": True,
            "blockers": [],
            "endpoint_hits": {},
            "stream_hits": {},
            "query_sha256": "c" * 64,
        }
        with (
            mock.patch.object(
                MODULE.REC, "_physical_projection", return_value=(physical, [])
            ),
            mock.patch.object(
                MODULE.REC, "_laboratory_projection", return_value=(laboratory, [])
            ),
        ):
            return MODULE.build_plan(
                state_root=self.state,
                voice_snapshot_fn=lambda: {"complete": True, "identity": voice},
                roland_snapshot_fn=lambda: {"complete": True, "identity": roland},
                monitor_snapshot_fn=lambda: {
                    "complete": True,
                    "identity": monitor,
                },
                truth_fn=lambda: {
                    "graph_rate_hz": graph_rate,
                    "graph_quantum_frames": 512,
                    "graph_fingerprint": "d" * 64,
                    "report_sha256": "e" * 64,
                    "truth_chain_sha256": "f" * 64,
                },
                service_fn=lambda: service or empty_service,
                conflict_fn=lambda: conflicts or clear,
                executable_fn=lambda: self.fake_executable_binding,
            )

    def persisted_spec(
        self,
        *,
        selected_input: str = "input-1",
        session_id: str = "a" * 24,
    ) -> dict[str, object]:
        plan = self.ready_plan(selected_input=selected_input)
        paths = MODULE._session_paths(self.state, session_id)
        return {
            "schema_version": 1,
            "kind": "audio_production_mix_spec",
            "session_id": session_id,
            "created_at": "2026-08-01T00:00:00+00:00",
            "plan_sha256": plan["plan_sha256"],
            "plan_identity": plan["identity"],
            "raw_nodes": {
                "voice_source": self.voice_name,
                "roland_source": self.roland_name,
                "monitor_sink": self.monitor_name,
            },
            "paths": {
                "ready": str(paths["ready"]),
                "result": str(paths["result"]),
            },
        }

    @staticmethod
    def binding() -> dict[str, object]:
        return {"complete": True, "argv": [], "stdout_sha256": "0" * 64}

    def query_fn(self, payloads: dict[str, list[object]]):
        def query(argv: tuple[str, ...]):
            key = argv[-1].replace("-", "_")
            return payloads[key], self.binding()

        return query

    def endpoint(
        self,
        index: int,
        name: str,
        *,
        rate: int = 48_000,
        channels: int = 2,
        sample_format: str = "s32le",
        monitor_source: str | None = None,
    ) -> dict[str, object]:
        return {
            "index": index,
            "name": name,
            "monitor_source": monitor_source,
            "sample_specification": f"{sample_format} {channels}ch {rate}Hz",
            "channel_map": "front-left,front-right" if channels == 2 else "mono",
            "properties": {},
        }

    @staticmethod
    def stream(
        index: int,
        node_name: str,
        *,
        sink: int | None = None,
        source: int | None = None,
        rate: int = 48_000,
        channels: int = 2,
        media_class: str = "Stream/Output/Audio",
    ) -> dict[str, object]:
        return {
            "index": index,
            "sink": sink,
            "source": source,
            "sample_specification": f"s32le {channels}ch {rate}Hz",
            "channel_map": "front-left,front-right" if channels == 2 else "mono",
            "properties": {
                "node.name": node_name,
                "media.class": media_class,
                "application.name": "test",
            },
        }

    def topology_payloads(self, *, voice_target: int = 10, bus_channels: int = 2):
        return {
            "sinks": [
                self.endpoint(
                    10,
                    "audio-production-bus",
                    monitor_source="audio-production-bus.monitor",
                    channels=bus_channels,
                ),
                self.endpoint(11, self.monitor_name),
            ],
            "sources": [
                self.endpoint(21, "audio-production-mix"),
                self.endpoint(22, self.voice_name),
                self.endpoint(
                    23,
                    self.roland_name,
                    rate=44_100,
                    sample_format="s24le",
                ),
                self.endpoint(20, "audio-production-bus.monitor"),
            ],
            "sink_inputs": [
                self.stream(30, "audio-production-monitor", sink=11),
                self.stream(
                    31,
                    "audio-production-route-voice-playback",
                    sink=voice_target,
                    channels=1,
                ),
                self.stream(32, "audio-production-route-roland-playback", sink=10),
            ],
            "source_outputs": [
                self.stream(40, "audio-production-mix-capture", source=20),
                self.stream(
                    41, "audio-production-route-voice-capture", source=22, channels=1
                ),
                self.stream(
                    42, "audio-production-route-roland-capture", source=23, rate=44_100
                ),
            ],
        }

    def write_session(self, *, running: bool = True):
        MODULE.ensure_private_directory(self.state)
        spec = self.persisted_spec()
        session_id = spec["session_id"]
        paths = MODULE._session_paths(self.state, session_id)
        MODULE._write_private_json(paths["spec"], spec, create_only=True)
        spec_sha = MODULE._binding(paths["spec"], private=True)["sha256"]
        identity = {
            "main_pid": 1234,
            "invocation_id": "1" * 32,
            "control_group_sha256": "2" * 64,
            "exec_start_sha256": "3" * 64,
        }
        state = {
            "schema_version": 1,
            "kind": "audio_production_mix_state",
            "session_id": session_id,
            "spec_sha256": spec_sha,
            "started_at": "2026-08-01T00:00:00+00:00",
            "phase": "running" if running else "starting",
            "service_identity": identity if running else None,
        }
        MODULE._write_private_json(paths["state"], state, create_only=True)
        MODULE._write_private_json(
            paths["active"],
            {
                "schema_version": 1,
                "kind": "audio_production_mix_active",
                "session_id": session_id,
                "spec_sha256": spec_sha,
            },
            create_only=True,
        )
        return paths, spec, state, spec_sha

    def service(self, state: dict[str, object], spec_sha: str, *, active: bool = True):
        return {
            "unit": "audio-production-mix.service",
            "load_state": "loaded" if active else "not-found",
            "active_state": "active" if active else "inactive",
            "sub_state": "running" if active else "dead",
            "result": "success",
            "managed": active,
            "spec_sha256": spec_sha if active else None,
            "identity": state["service_identity"] if active else None,
            "limits": {
                "memory_max_bytes": 268_435_456,
                "tasks_max": 64,
                "limit_nofile": 128,
                "log_rate_limit_interval_usec": 30_000_000,
                "log_rate_limit_burst": 100,
            }
            if active
            else None,
        }

    def test_contract_and_profile_binding_are_consistent(self) -> None:
        contract = MODULE.load_contract()
        self.assertEqual(
            contract["graph"]["virtual_sink"]["node_name"], "audio-production-bus"
        )
        self.assertEqual(
            contract["graph"]["virtual_source"]["node_name"], "audio-production-mix"
        )
        self.assertEqual(contract["process"]["child_count"], 4)
        binding = MODULE.profile_binding()
        self.assertEqual(
            set(binding["selected"]),
            {"production", "voice-recording", "piano-digital-recording"},
        )

    def test_plan_is_read_only_ready_and_stable(self) -> None:
        first = self.ready_plan()
        second = self.ready_plan()
        self.assertTrue(first["ready"])
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertFalse(self.state.exists())
        self.assertEqual(first["identity"]["voice_channel"]["source_position"], "FL")

    def test_plan_preserves_rate_service_conflict_and_channel_blockers(self) -> None:
        service = {
            "load_state": "loaded",
            "active_state": "active",
            "managed": False,
        }
        conflicts = {
            "clear": False,
            "blockers": ["graph-name-conflict:audio-production-bus"],
        }
        plan = self.ready_plan(
            selected_input="invalid",
            graph_rate=44_100,
            service=service,
            conflicts=conflicts,
        )
        self.assertFalse(plan["ready"])
        self.assertIn("graph-rate-is-not-48k", plan["readiness"]["blockers"])
        self.assertIn("managed-service-already-present", plan["readiness"]["blockers"])
        self.assertIn(
            "graph-name-conflict:audio-production-bus", plan["readiness"]["blockers"]
        )
        self.assertIn("voice-channel-selection-invalid", plan["readiness"]["blockers"])

    def test_motu_sink_identity_binds_serial_bus_rate_volume_and_mute(self) -> None:
        contract = MODULE.load_contract()["monitor_target"]
        item = {
            "name": self.monitor_name,
            "sample_specification": "s32le 2ch 48000Hz",
            "mute": False,
            "volume": {"front-left": {"value": 123}, "front-right": {"value": 123}},
            "properties": {
                "device.vendor.id": "0x07fd",
                "device.product.id": "0x0008",
                "device.serial": "MOTU_M2_M20000000000",
                "device.bus_path": "pci-0000:00:14.0-usb-0:1:1.0",
            },
        }
        identity, name = MODULE._motu_sink_identity(item, contract)
        self.assertEqual(name, self.monitor_name)
        self.assertEqual(identity["sample_rate_hz"], 48_000)
        self.assertRegex(identity["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertNotIn("device.serial", json.dumps(identity))

    def test_monitor_projection_rejects_mute_and_channel_drift(self) -> None:
        projection, blockers = MODULE._monitor_projection(
            MODULE.load_contract()["monitor_target"],
            lambda: {
                "complete": True,
                "identity": self.monitor_identity(channels=1, muted=True),
            },
        )
        self.assertIsNotNone(projection["identity"])
        self.assertEqual(blockers, ["motu-sink:channels", "motu-sink:muted"])

    def test_persisted_spec_and_role_commands_are_exactly_bound(self) -> None:
        spec = self.persisted_spec()
        MODULE.validate_spec(spec, state_root=self.state)
        commands = MODULE._role_commands(spec)
        self.assertEqual(
            [item["role"] for item in commands],
            ["bus-monitor", "mix-export", "voice-route", "roland-route"],
        )
        serialized = json.dumps(commands)
        self.assertIn("audio-production-bus", serialized)
        self.assertIn("audio-production-mix", serialized)
        self.assertIn(self.monitor_name, serialized)
        self.assertNotIn("set-default", serialized)
        self.assertNotIn("pipewire.conf.d", serialized)
        self.assertTrue(
            all(item["argv"][0] == str(self.fake_loopback) for item in commands)
        )

    def test_voice_channel_fact_selects_right_capture_position(self) -> None:
        spec = self.persisted_spec(selected_input="input-2")
        command = next(
            item
            for item in MODULE._role_commands(spec)
            if item["role"] == "voice-route"
        )
        props = json.loads(
            command["argv"][command["argv"].index("--capture-props") + 1]
        )
        self.assertEqual(props["audio.position"], ["FR"])
        self.assertEqual(
            command["argv"][command["argv"].index("--channel-map") + 1], "[ MONO ]"
        )

    def test_spec_rejects_raw_node_and_inner_identity_substitution(self) -> None:
        spec = self.persisted_spec()
        changed_raw = json.loads(json.dumps(spec))
        changed_raw["raw_nodes"]["roland_source"] = "substituted"
        with self.assertRaisesRegex(MODULE.ProductionMixError, "raw roland_source"):
            MODULE.validate_spec(changed_raw, state_root=self.state)
        changed_identity = json.loads(json.dumps(spec))
        identity = changed_identity["plan_identity"]["roland_source"]["identity"]
        identity["channels"] = 1
        unbound = dict(identity)
        unbound.pop("fingerprint")
        identity["fingerprint"] = MODULE.canonical_sha256(unbound)
        changed_identity["plan_identity"]["roland_source"]["identity_sha256"] = (
            MODULE.canonical_sha256(identity)
        )
        changed_identity["plan_sha256"] = MODULE.canonical_sha256(
            changed_identity["plan_identity"]
        )
        with self.assertRaisesRegex(MODULE.ProductionMixError, "source projection"):
            MODULE.validate_spec(changed_identity, state_root=self.state)

    def test_spec_rejects_monitor_identity_forgery(self) -> None:
        spec = self.persisted_spec()
        changed = json.loads(json.dumps(spec))
        identity = changed["plan_identity"]["monitor_sink"]["identity"]
        identity["product_id"] = "ffff"
        unbound = dict(identity)
        unbound.pop("fingerprint")
        identity["fingerprint"] = MODULE.canonical_sha256(unbound)
        changed["plan_identity"]["monitor_sink"]["identity_sha256"] = (
            MODULE.canonical_sha256(identity)
        )
        changed["plan_sha256"] = MODULE.canonical_sha256(changed["plan_identity"])
        with self.assertRaisesRegex(MODULE.ProductionMixError, "monitor identity"):
            MODULE.validate_spec(changed, state_root=self.state)

    def test_conflict_snapshot_detects_only_reserved_names(self) -> None:
        payloads = {
            "sinks": [
                self.endpoint(1, "audio-production-bus"),
                self.endpoint(2, "other"),
            ],
            "sources": [],
            "sink_inputs": [self.stream(3, "unrelated", sink=2)],
            "source_outputs": [],
        }
        snapshot = MODULE.graph_conflict_snapshot(
            MODULE.load_contract(), query_fn=self.query_fn(payloads)
        )
        self.assertEqual(
            snapshot["blockers"], ["graph-name-conflict:audio-production-bus"]
        )

    def test_topology_proves_nodes_routes_and_optional_software_streams(self) -> None:
        spec = self.persisted_spec()
        payloads = self.topology_payloads()
        payloads["sink_inputs"].append(self.stream(33, "software-synth", sink=10))
        topology = MODULE.graph_topology_snapshot(
            spec, query_fn=self.query_fn(payloads)
        )
        self.assertTrue(topology["complete"], topology["blockers"])
        self.assertEqual(len(topology["software_instruments"]), 1)
        self.assertEqual(topology["endpoints"]["mix"]["sample_rate_hz"], 48_000)

    def test_topology_rejects_route_target_and_channel_drift(self) -> None:
        spec = self.persisted_spec()
        target_drift = MODULE.graph_topology_snapshot(
            spec, query_fn=self.query_fn(self.topology_payloads(voice_target=11))
        )
        self.assertIn(
            "sink-input-target-drift:audio-production-route-voice-playback",
            target_drift["blockers"],
        )
        format_drift = MODULE.graph_topology_snapshot(
            spec, query_fn=self.query_fn(self.topology_payloads(bus_channels=1))
        )
        self.assertIn("production-bus-format-drift", format_drift["blockers"])

    def test_service_snapshot_binds_marker_identity_and_limits(self) -> None:
        values = {
            "Id": "audio-production-mix.service",
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "Result": "success",
            "MainPID": "4321",
            "ControlGroup": "/user.slice/user-1000.slice/user@1000.service/app.slice/audio-production-mix.service",
            "InvocationID": "a" * 32,
            "Environment": f"{MODULE.MANAGED_MARKER_ENV}=1 {MODULE.SPEC_SHA_ENV}={'b' * 64}",
            "ExecStart": "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 worker ; }",
            "ExecMainStatus": "0",
            "NRestarts": "0",
            "MemoryMax": "268435456",
            "TasksMax": "64",
            "LimitNOFILE": "128",
            "LogRateLimitIntervalUSec": "30000000",
            "LogRateLimitBurst": "100",
        }
        text = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"

        def runner(_argv):
            return subprocess.CompletedProcess([], 0, text, "")

        snapshot = MODULE.service_snapshot(
            "audio-production-mix.service", runner=runner
        )
        self.assertTrue(snapshot["managed"])
        self.assertEqual(snapshot["identity"]["main_pid"], 4321)
        self.assertTrue(MODULE._service_limits_match(snapshot, MODULE.load_contract()))

    def test_systemd_command_is_bounded_and_argv_only(self) -> None:
        command = MODULE._systemd_run_command(self.state / "a.spec.json", "f" * 64)
        self.assertEqual(command[:3], ["systemd-run", "--user", "--collect"])
        self.assertIn("MemoryMax=268435456", command)
        self.assertIn("LimitNOFILE=128", command)
        self.assertIn("RuntimeMaxSec=43200s", command)
        self.assertIn(f"{MODULE.SPEC_SHA_ENV}={'f' * 64}", command)
        self.assertNotIn("sh", command)
        self.assertNotIn("bash", command)

    def test_init_creates_private_state_without_audio_effect(self) -> None:
        result = MODULE.init_state(self.state)
        self.assertFalse(result["audio_effect"])
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)

    def test_start_persists_recoverable_state_before_service_failure(self) -> None:
        plan = self.ready_plan()

        def runner(_argv):
            return subprocess.CompletedProcess([], 1, "", "spawn failed")

        with self.assertRaisesRegex(MODULE.ProductionMixError, "recover session"):
            MODULE.start_graph(
                plan["plan_sha256"],
                state_root=self.state,
                plan_fn=lambda **_kwargs: plan,
                raw_nodes_fn=lambda _contract: {
                    "voice_source": self.voice_name,
                    "roland_source": self.roland_name,
                    "monitor_sink": self.monitor_name,
                },
                runner=runner,
            )
        active = json.loads((self.state / "active.json").read_text())
        state = json.loads(
            MODULE._session_paths(self.state, active["session_id"])["state"].read_text()
        )
        self.assertEqual(state["phase"], "starting")
        self.assertIsNone(state["service_identity"])

    def test_recovery_marks_orphan_and_clears_active(self) -> None:
        paths, _spec, _state, _spec_sha = self.write_session()
        status = MODULE.recover_graph(
            state_root=self.state,
            service_fn=lambda: {
                "load_state": "not-found",
                "active_state": "inactive",
            },
        )
        self.assertEqual(status["status"], "failed-recovered")
        self.assertFalse(paths["active"].exists())
        self.assertTrue(paths["result"].exists())

    def test_recovery_and_stop_refuse_foreign_or_changed_service(self) -> None:
        _paths, _spec, _state, _spec_sha = self.write_session()

        def foreign():
            return {
                "load_state": "loaded",
                "active_state": "active",
                "managed": False,
                "identity": {"main_pid": 999},
            }

        with self.assertRaisesRegex(MODULE.ProductionMixError, "foreign or changed"):
            MODULE.recover_graph(state_root=self.state, service_fn=foreign)
        called = False

        def runner(_argv):
            nonlocal called
            called = True
            return subprocess.CompletedProcess([], 0, "", "")

        with self.assertRaisesRegex(MODULE.ProductionMixError, "identity is not exact"):
            MODULE.stop_graph(state_root=self.state, service_fn=foreign, runner=runner)
        self.assertFalse(called)

    def test_ready_receipt_rejects_command_digest_mutation(self) -> None:
        spec = self.persisted_spec()
        children = []
        identity = {
            "pid": 1234,
            "start_ticks": 1,
            "executable": "/usr/bin/pw-loopback",
            "cmdline_sha256": "1" * 64,
            "process_group": 1234,
        }
        for command in MODULE._role_commands(spec):
            children.append(
                {
                    "role": command["role"],
                    "argv_sha256": MODULE.canonical_sha256(command["argv"]),
                    "identity": identity,
                }
            )
        topology = {"complete": True, "topology_sha256": "2" * 64}
        ready = {
            "schema_version": 1,
            "kind": "audio_production_mix_ready",
            "session_id": spec["session_id"],
            "ready_at": "2026-08-01T00:00:01+00:00",
            "plan_sha256": spec["plan_sha256"],
            "children": children,
            "topology": topology,
            "does_not_establish": ["safe-monitoring-level"],
        }
        MODULE._validate_ready(ready, spec)
        ready["children"][0]["argv_sha256"] = "0" * 64
        with self.assertRaisesRegex(MODULE.ProductionMixError, "command binding"):
            MODULE._validate_ready(ready, spec)

    def test_worker_bus_failure_returns_bounded_failure_and_terminates(self) -> None:
        spec = self.persisted_spec()

        class FakeProcess:
            pid = 1234

            def poll(self):
                return None

            def kill(self):
                return None

            def wait(self, timeout=None):
                return 0

        with (
            mock.patch.object(
                MODULE,
                "_process_identity",
                return_value={
                    "pid": 1234,
                    "start_ticks": 1,
                    "executable": "/usr/bin/pw-loopback",
                    "cmdline_sha256": "3" * 64,
                    "process_group": 1234,
                },
            ),
            mock.patch.object(
                MODULE, "_terminate_children", return_value=True
            ) as terminate,
        ):
            result = MODULE.worker_run(
                spec,
                popen_fn=lambda *_args, **_kwargs: FakeProcess(),
                bus_wait_fn=lambda _name, _children: False,
            )
        self.assertEqual(result["status"], "failed")
        self.assertIn("did not become uniquely observable", result["error"])
        terminate.assert_called_once()
        self.assertEqual(len(result["stderr"]), 1)

    def test_profile_binding_rejects_graph_contract_drift(self) -> None:
        payload = json.loads(MODULE.PROFILE_PATH.read_text())
        payload["profiles"]["production"]["desired"]["production_bus_sink"] = (
            "substituted-bus"
        )
        profile_path = self.base / "audio-profiles.v1.json"
        profile_path.write_text(json.dumps(payload))
        with mock.patch.object(MODULE, "PROFILE_PATH", profile_path):
            with self.assertRaisesRegex(
                MODULE.ProductionMixError, "profile contract is inconsistent"
            ):
                MODULE.profile_binding()

    def test_plan_turns_service_query_failure_into_a_blocker(self) -> None:
        physical = {
            "state_path": str(self.base / "physical.json"),
            "state_sha256": "8" * 64,
            "facts": {
                "rode_nt1a_connected": True,
                "rode_nt1a_motu_input": "input-1",
                "motu_phantom_48v": "on",
                "motu_input_gain_reference": "mark 10",
                "motu_output_to_lake_people": "line out 1-2",
                "lake_people_gain_setting": "0 dB",
                "lake_people_volume_reference": "mark 12",
                "focal_connected_output": "headphone out",
            },
            "error": None,
        }
        laboratory = {
            "state_path": str(self.base / "laboratory.json"),
            "state_sha256": "9" * 64,
            "resolved": ["resampling-decision", "voice-level-measurement"],
            "invalidated": {},
            "receipt_sha256": {
                "voice-level-measurement": "a" * 64,
                "resampling-decision": "b" * 64,
            },
            "error": None,
        }
        with (
            mock.patch.object(
                MODULE.REC, "_physical_projection", return_value=(physical, [])
            ),
            mock.patch.object(
                MODULE.REC, "_laboratory_projection", return_value=(laboratory, [])
            ),
        ):
            plan = MODULE.build_plan(
                state_root=self.state,
                voice_snapshot_fn=lambda: {
                    "complete": True,
                    "identity": self.voice_identity(),
                },
                roland_snapshot_fn=lambda: {
                    "complete": True,
                    "identity": self.roland_identity(),
                },
                monitor_snapshot_fn=lambda: {
                    "complete": True,
                    "identity": self.monitor_identity(),
                },
                truth_fn=lambda: {"graph_rate_hz": 48_000},
                service_fn=lambda: (_ for _ in ()).throw(
                    MODULE.ProductionMixError("query failed")
                ),
                conflict_fn=lambda: {"clear": True, "blockers": []},
                executable_fn=lambda: self.fake_executable_binding,
            )
        self.assertFalse(plan["ready"])
        self.assertIn("service-state-unavailable", plan["readiness"]["blockers"])

    def test_shared_child_termination_signals_all_children_before_waiting(self) -> None:
        children = [
            {
                "identity": {
                    "pid": pid,
                    "start_ticks": 1,
                    "executable": "/usr/bin/pw-loopback",
                    "cmdline_sha256": f"{pid:064x}"[-64:],
                    "process_group": pid,
                }
            }
            for pid in (101, 102, 103, 104)
        ]
        current = [True, True, True, True, False, False, False, False]
        with (
            mock.patch.object(MODULE, "_identity_current", side_effect=current),
            mock.patch.object(MODULE.os, "kill") as kill,
            mock.patch.object(MODULE.os, "killpg") as killpg,
        ):
            self.assertTrue(MODULE._terminate_children(children, grace_seconds=1.0))
        self.assertEqual(
            kill.call_args_list,
            [mock.call(pid, MODULE.signal.SIGTERM) for pid in (101, 102, 103, 104)],
        )
        killpg.assert_not_called()

    def test_limit_mismatch_persists_service_identity_for_exact_stop(self) -> None:
        plan = self.ready_plan()
        identity = {
            "main_pid": 4321,
            "invocation_id": "a" * 32,
            "control_group_sha256": "b" * 64,
            "exec_start_sha256": "c" * 64,
        }

        def service():
            active = json.loads((self.state / "active.json").read_text())
            return {
                "unit": "audio-production-mix.service",
                "load_state": "loaded",
                "active_state": "active",
                "sub_state": "running",
                "result": "success",
                "managed": True,
                "spec_sha256": active["spec_sha256"],
                "identity": identity,
                "limits": {
                    "memory_max_bytes": 1,
                    "tasks_max": 64,
                    "limit_nofile": 128,
                    "log_rate_limit_interval_usec": 30_000_000,
                    "log_rate_limit_burst": 100,
                },
            }

        with self.assertRaisesRegex(MODULE.ProductionMixError, "stop session"):
            MODULE.start_graph(
                plan["plan_sha256"],
                state_root=self.state,
                plan_fn=lambda **_kwargs: plan,
                raw_nodes_fn=lambda _contract: {
                    "voice_source": self.voice_name,
                    "roland_source": self.roland_name,
                    "monitor_sink": self.monitor_name,
                },
                runner=lambda _argv: subprocess.CompletedProcess([], 0, "", ""),
                service_fn=service,
            )
        active = json.loads((self.state / "active.json").read_text())
        stored = json.loads(
            MODULE._session_paths(self.state, active["session_id"])["state"].read_text()
        )
        self.assertEqual(stored["phase"], "running")
        self.assertEqual(stored["service_identity"], identity)

    def test_cli_help_and_wrapper_are_executable(self) -> None:
        result = subprocess.run(
            [str(ROOT / "scripts/audio-production-mix"), "--help"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plan", result.stdout)
        self.assertTrue(os.access(ROOT / "scripts/audio-production-mix", os.X_OK))


if __name__ == "__main__":
    unittest.main()
