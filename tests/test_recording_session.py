from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import signal
import struct
import sys
import tempfile
import types
import unittest
import wave
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "recording_session_test_module", ROOT / "scripts/recording_session.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RecordingSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.output = self.base / "recordings"
        self.output.mkdir(mode=0o700)
        self.state = self.base / "state"

    @staticmethod
    def source_identity(
        session_type: str = "voice-recording", *, channels: int = 2
    ) -> dict[str, object]:
        common: dict[str, object] = {
            "node_name_sha256": "2" * 64,
            "sample_format": "s24le"
            if session_type == "roland-audio-recording"
            else "s32le",
            "sample_rate_hz": 44_100
            if session_type == "roland-audio-recording"
            else 48_000,
            "channels": channels,
            "muted": False,
            "unity_volume": True,
        }
        if session_type == "voice-recording":
            common.update(
                {
                    "vendor_id": "07fd",
                    "product_id": "0008",
                    "serial_sha256": "1" * 64,
                    "bus_path_sha256": "3" * 64,
                }
            )
        elif session_type == "roland-audio-recording":
            common.update(
                {
                    "source_kind": "usb-audio",
                    "vendor_id": "0582",
                    "product_id": "01b1",
                    "serial_sha256": "1" * 64,
                    "bus_path_sha256": "3" * 64,
                }
            )
        elif session_type == "production-mix-recording":
            common.update(
                {
                    "source_kind": "named-pipewire-source",
                    "declared_upstream_roles": [
                        "voice",
                        "roland",
                        "software-instrument",
                    ],
                    "object_serial_sha256": "8" * 64,
                }
            )
        elif session_type == "piano-vocal-performance":
            audio = dict(common)
            audio.update(
                {
                    "vendor_id": "07fd",
                    "product_id": "0008",
                    "serial_sha256": "1" * 64,
                    "bus_path_sha256": "3" * 64,
                }
            )
            audio["fingerprint"] = MODULE.canonical_sha256(audio)
            usb = {
                "vendor_id": "0582",
                "product_id": "01b1",
                "identity_strength": "model-usb-port",
                "bus_number": "1",
                "port_path": "2.3",
            }
            usb["fingerprint"] = MODULE.canonical_sha256(usb)
            midi = {
                "address": "24:0",
                "client": 24,
                "port": 0,
                "kernel_card": 2,
                "kernel_client_label_sha256": "4" * 64,
                "kernel_port_label_sha256": "5" * 64,
                "arecordmidi_client_label_sha256": "6" * 64,
                "arecordmidi_port_label_sha256": "7" * 64,
                "usb": usb,
            }
            midi["fingerprint"] = MODULE.canonical_sha256(midi)
            roland_audio = {
                "node_name_sha256": "9" * 64,
                "sample_format": "s24le",
                "sample_rate_hz": 44_100,
                "channels": channels,
                "muted": False,
                "unity_volume": True,
                "source_kind": "usb-audio",
                "vendor_id": "0582",
                "product_id": "01b1",
                "serial_sha256": "a" * 64,
                "bus_path_sha256": "b" * 64,
            }
            roland_audio["fingerprint"] = MODULE.canonical_sha256(roland_audio)
            performance = {"audio": audio, "roland_audio": roland_audio, "midi": midi}
            performance["fingerprint"] = MODULE.canonical_sha256(performance)
            return performance
        else:
            raise AssertionError(session_type)
        common["fingerprint"] = MODULE.canonical_sha256(common)
        return common

    @staticmethod
    def managed_graph_snapshot(*, complete: bool = True) -> dict[str, object]:
        binding = {
            "schema_version": 1,
            "kind": "audio_production_mix_runtime_binding",
            "session_id": "f" * 24,
            "plan_sha256": "a" * 64,
            "service_identity_sha256": "b" * 64,
            "topology_sha256": "c" * 64,
            "virtual_sink": "audio-production-bus",
            "virtual_source": "audio-production-mix",
            "sample_format": "s32le",
            "sample_rate_hz": 48_000,
            "channels": 2,
            "channel_map": "front-left,front-right",
        }
        return {
            "complete": complete,
            "binding": binding if complete else None,
            "binding_sha256": MODULE.canonical_sha256(binding) if complete else None,
            "error": None if complete else "not ready",
        }

    @staticmethod
    def refresh_source_binding(plan: dict[str, object], source_name: str) -> None:
        identity = plan["identity"]["source"]["identity"]
        identity["node_name_sha256"] = hashlib.sha256(source_name.encode()).hexdigest()
        unbound = dict(identity)
        unbound.pop("fingerprint", None)
        identity["fingerprint"] = MODULE.canonical_sha256(unbound)
        plan["identity"]["source"]["identity_sha256"] = MODULE.canonical_sha256(
            identity
        )
        plan["plan_sha256"] = MODULE.canonical_sha256(plan["identity"])

    def ready_plan(
        self,
        *,
        session_type: str = "voice-recording",
        free: int = 20_000_000_000,
        source_identity: dict[str, object] | None = None,
    ) -> dict[str, object]:
        contract = MODULE.load_catalog(session_type)
        physical = {
            "state_path": str(self.base / "physical.json"),
            "state_sha256": "5" * 64,
            "facts": {
                "rode_nt1a_connected": True,
                "rode_nt1a_motu_input": "input-1",
                "motu_phantom_48v": "on",
                "motu_input_gain_reference": "mark 10",
            }
            if session_type in {"voice-recording", "piano-vocal-performance"}
            else {},
            "error": None,
        }
        laboratory = {
            "state_path": str(self.base / "lab.json"),
            "state_sha256": "6" * 64,
            "resolved": list(contract["required_laboratory_gates"]),
            "invalidated": {},
            "receipt_sha256": {
                gate: "7" * 64 for gate in contract["required_laboratory_gates"]
            },
            "error": None,
        }
        identity = source_identity or self.source_identity(session_type)
        source = {
            "identity": identity,
            "identity_sha256": MODULE.canonical_sha256(identity),
            "error": None,
        }
        if session_type == "production-mix-recording":
            managed = self.managed_graph_snapshot()
            source["managed_graph"] = {
                "binding": managed["binding"],
                "binding_sha256": managed["binding_sha256"],
            }
        with (
            mock.patch.object(
                MODULE, "_physical_projection", return_value=(physical, [])
            ),
            mock.patch.object(
                MODULE, "_laboratory_projection", return_value=(laboratory, [])
            ),
            mock.patch.object(MODULE, "_source_projection", return_value=(source, [])),
            mock.patch.object(
                MODULE,
                "contract_bindings",
                return_value=[{"path": "x", "sha256": "9" * 64}],
            ),
            mock.patch.object(
                MODULE,
                "parecord_binding",
                return_value={"launcher": "/usr/bin/parecord"},
            ),
            mock.patch.object(
                MODULE,
                "arecordmidi_binding",
                return_value={
                    "launcher": "/usr/bin/arecordmidi",
                    "resolved": {"path": "/usr/bin/arecordmidi", "sha256": "a" * 64},
                },
            ),
            mock.patch.object(
                MODULE,
                "ffmpeg_binding",
                return_value={
                    "launcher": "/usr/bin/ffmpeg",
                    "resolved": {"path": "/usr/bin/ffmpeg", "sha256": "c" * 64},
                },
            ),
        ):
            return MODULE.build_plan(
                "take-01.wav",
                60,
                session_type=session_type,
                output_root=self.output,
                state_root=self.state,
                disk_usage_fn=lambda _path: types.SimpleNamespace(free=free),
            )

    def plan_with_snapshot(
        self, session_type: str, snapshot: dict[str, object]
    ) -> dict[str, object]:
        contract = MODULE.load_catalog(session_type)
        snapshot = dict(snapshot)
        if session_type == "production-mix-recording":
            snapshot.setdefault("source_complete", snapshot.get("complete"))
            snapshot.setdefault("managed_graph", self.managed_graph_snapshot())
        physical = {
            "state_path": str(self.base / "physical.json"),
            "state_sha256": None,
            "facts": {},
            "error": None,
        }
        laboratory = {
            "state_path": str(self.base / "lab.json"),
            "state_sha256": "6" * 64,
            "resolved": list(contract["required_laboratory_gates"]),
            "invalidated": {},
            "receipt_sha256": {
                gate: "7" * 64 for gate in contract["required_laboratory_gates"]
            },
            "error": None,
        }
        with (
            mock.patch.object(
                MODULE, "_physical_projection", return_value=(physical, [])
            ),
            mock.patch.object(
                MODULE, "_laboratory_projection", return_value=(laboratory, [])
            ),
            mock.patch.object(
                MODULE,
                "contract_bindings",
                return_value=[{"path": "x", "sha256": "9" * 64}],
            ),
            mock.patch.object(
                MODULE,
                "parecord_binding",
                return_value={"launcher": "/usr/bin/parecord"},
            ),
        ):
            return MODULE.build_plan(
                "take-01.wav",
                60,
                session_type=session_type,
                output_root=self.output,
                state_root=self.state,
                source_snapshot_fn=lambda: snapshot,
                disk_usage_fn=lambda _path: types.SimpleNamespace(free=20_000_000_000),
            )

    def test_catalog_and_byte_budget_are_consistent(self) -> None:
        contract = MODULE.load_catalog()
        capture = contract["capture"]
        self.assertEqual(capture["sample_format"], "s32le")
        self.assertEqual(
            MODULE.maximum_file_bytes(capture, 10),
            48_000 * 2 * 4 * 10 + 1_048_576,
        )

    def test_plan_exposes_canonical_structured_readiness_checks(self) -> None:
        plan = self.ready_plan()
        checks = plan["readiness"]["checks"]
        self.assertEqual(
            [check["id"] for check in checks],
            ["output", "physical", "laboratory", "source", "tools", "storage", "session"],
        )
        self.assertTrue(all(check == {"id": check["id"], "status": "ready", "blockers": []} for check in checks))
        self.assertEqual(plan["readiness"]["blockers"], [])

    def test_catalog_exposes_four_explicit_session_contracts(self) -> None:
        contracts = {name: MODULE.load_catalog(name) for name in MODULE.SESSION_TYPES}
        self.assertEqual(
            set(contracts),
            {
                "voice-recording",
                "piano-vocal-performance",
                "roland-audio-recording",
                "production-mix-recording",
            },
        )
        self.assertEqual(contracts["voice-recording"]["required_laboratory_gates"], [])
        self.assertEqual(
            contracts["voice-recording"]["advisory_laboratory_gates"],
            ["voice-level-measurement"],
        )
        self.assertEqual(
            contracts["piano-vocal-performance"]["required_laboratory_gates"],
            ["resampling-decision"],
        )
        self.assertEqual(
            contracts["piano-vocal-performance"]["advisory_laboratory_gates"],
            ["voice-level-measurement"],
        )
        self.assertEqual(
            contracts["roland-audio-recording"]["required_laboratory_gates"],
            ["resampling-decision"],
        )
        self.assertEqual(
            contracts["roland-audio-recording"]["source"]["required_sample_rate_hz"],
            44_100,
        )
        self.assertEqual(
            contracts["production-mix-recording"]["source"]["node_name"],
            "audio-production-mix",
        )
        self.assertEqual(
            {contract["process"]["client_name"] for contract in contracts.values()},
            {
                "audio-voice-recording",
                "audio-piano-vocal-performance",
                "audio-roland-recording",
                "audio-production-recording",
            },
        )
        self.assertTrue(
            all(
                contract["monitoring"]["software_loopback"] is False
                for contract in contracts.values()
            )
        )

    def test_catalog_rejects_unknown_profile_reference(self) -> None:
        payload = json.loads(MODULE.PROFILE_PATH.read_text())
        del payload["profiles"]["production"]
        profile_path = self.base / "audio-profiles.v1.json"
        profile_path.write_text(json.dumps(payload))
        with mock.patch.object(MODULE, "PROFILE_PATH", profile_path):
            with self.assertRaisesRegex(
                MODULE.RecordingError, "unknown audio profiles: production"
            ):
                MODULE.load_catalog("production-mix-recording")

    def test_roland_plan_binds_44k1_source_to_48k_output(self) -> None:
        identity = self.source_identity("roland-audio-recording")
        plan = self.plan_with_snapshot(
            "roland-audio-recording",
            {"complete": True, "identity": identity},
        )
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["identity"]["session_type"], "roland-audio-recording")
        self.assertEqual(
            plan["identity"]["source"]["identity"]["sample_rate_hz"], 44_100
        )
        self.assertEqual(plan["identity"]["capture"]["sample_rate_hz"], 48_000)
        self.assertEqual(
            plan["identity"]["laboratory"]["resolved"], ["resampling-decision"]
        )

    def test_roland_device_loss_and_channel_drift_fail_closed(self) -> None:
        missing = self.plan_with_snapshot(
            "roland-audio-recording",
            {"complete": False, "identity": None},
        )
        self.assertFalse(missing["ready"])
        self.assertIn("roland-source-not-unique", missing["readiness"]["blockers"])
        drifted_identity = self.source_identity("roland-audio-recording", channels=1)
        drifted = self.plan_with_snapshot(
            "roland-audio-recording",
            {"complete": True, "identity": drifted_identity},
        )
        self.assertFalse(drifted["ready"])
        self.assertIn("roland-source:channels", drifted["readiness"]["blockers"])

    def test_roland_pactl_identity_is_serial_and_bus_bound(self) -> None:
        contract = MODULE.load_catalog("roland-audio-recording")["source"]
        item = {
            "name": "alsa_input.usb-Roland_Roland_Digital_Piano_SERIAL-00.analog-stereo",
            "monitor_source": None,
            "sample_specification": "s24le 2ch 44100Hz",
            "mute": False,
            "volume": {
                "front-left": {"value": 65_536},
                "front-right": {"value": 65_536},
            },
            "properties": {
                "device.class": "sound",
                "media.class": "Audio/Source",
                "device.vendor.id": "0x0582",
                "device.product.id": "0x01b1",
                "device.serial": "Roland_Roland_Digital_Piano_SERIAL",
                "device.bus_path": "pci-0000:00:14.0-usb-0:2:1.0",
            },
        }
        identity, source_name = MODULE._pactl_source_identity(item, contract)
        self.assertEqual(source_name, item["name"])
        self.assertEqual(identity["source_kind"], "usb-audio")
        self.assertEqual(identity["sample_rate_hz"], 44_100)
        self.assertRegex(identity["serial_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(identity["bus_path_sha256"], r"^[0-9a-f]{64}$")

    def test_production_mix_requires_pipewire_object_identity(self) -> None:
        contract = MODULE.load_catalog("production-mix-recording")["source"]
        item = {
            "name": "audio-production-mix",
            "monitor_source": None,
            "sample_specification": "s32le 2ch 48000Hz",
            "mute": False,
            "volume": {
                "front-left": {"value": 65_536},
                "front-right": {"value": 65_536},
            },
            "properties": {"media.class": "Audio/Source"},
        }
        with self.assertRaisesRegex(ValueError, "PipeWire object identity"):
            MODULE._pactl_source_identity(item, contract)

    def test_production_mix_plan_requires_one_exact_named_source(self) -> None:
        identity = self.source_identity("production-mix-recording")
        ready = self.plan_with_snapshot(
            "production-mix-recording",
            {"complete": True, "identity": identity},
        )
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["identity"]["profile"], "production")
        self.assertEqual(
            ready["identity"]["source"]["identity"]["declared_upstream_roles"],
            ["voice", "roland", "software-instrument"],
        )
        missing = self.plan_with_snapshot(
            "production-mix-recording",
            {"complete": False, "identity": None, "match_count": 0},
        )
        self.assertIn(
            "production-mix-source-not-unique", missing["readiness"]["blockers"]
        )

    def test_production_mix_plan_requires_exact_managed_graph(self) -> None:
        identity = self.source_identity("production-mix-recording")
        blocked = self.plan_with_snapshot(
            "production-mix-recording",
            {
                "complete": True,
                "source_complete": True,
                "identity": identity,
                "managed_graph": self.managed_graph_snapshot(complete=False),
            },
        )
        self.assertFalse(blocked["ready"])
        self.assertIn(
            "production-mix-graph-not-ready", blocked["readiness"]["blockers"]
        )
        self.assertNotIn(
            "production-mix-source-not-unique", blocked["readiness"]["blockers"]
        )

    def test_managed_graph_snapshot_binds_exact_status_and_format(self) -> None:
        service_identity = {
            "main_pid": 1234,
            "invocation_id": "a" * 32,
            "control_group_sha256": "b" * 64,
            "exec_start_sha256": "c" * 64,
        }
        status = {
            "session_id": "d" * 24,
            "status": "ready",
            "service_identity_exact": True,
            "child_identities_exact": True,
            "plan_sha256": "e" * 64,
            "service": {"identity": service_identity},
            "topology": {
                "complete": True,
                "topology_sha256": "f" * 64,
                "endpoints": {
                    "mix": {
                        "sample_format": "s32le",
                        "sample_rate_hz": 48_000,
                        "channels": 2,
                        "channel_map": "front-left,front-right",
                    }
                },
            },
        }
        module = types.SimpleNamespace(graph_status=lambda: status)
        with mock.patch.object(MODULE, "_production_mix_module", return_value=module):
            snapshot = MODULE._managed_production_mix_snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["binding"]["virtual_source"], "audio-production-mix")
        self.assertEqual(
            snapshot["binding_sha256"], MODULE.canonical_sha256(snapshot["binding"])
        )
        status["child_identities_exact"] = False
        with mock.patch.object(MODULE, "_production_mix_module", return_value=module):
            changed = MODULE._managed_production_mix_snapshot()
        self.assertFalse(changed["complete"])

    def test_persisted_production_spec_rejects_graph_format_forgery(self) -> None:
        spec = self.persisted_spec(session_type="production-mix-recording")
        MODULE._validate_persisted_spec(spec, state_root=self.state)
        changed = json.loads(json.dumps(spec))
        managed = changed["plan_identity"]["source"]["managed_graph"]
        managed["binding"]["sample_rate_hz"] = 44_100
        managed["binding_sha256"] = MODULE.canonical_sha256(managed["binding"])
        changed["plan_sha256"] = MODULE.canonical_sha256(changed["plan_identity"])
        with self.assertRaisesRegex(
            MODULE.RecordingError, "managed production-mix binding"
        ):
            MODULE._validate_persisted_spec(changed, state_root=self.state)

    def test_persisted_specs_and_commands_remain_session_typed(self) -> None:
        for session_type in MODULE.SESSION_TYPES:
            with self.subTest(session_type=session_type):
                spec = self.persisted_spec(session_type=session_type)
                MODULE._validate_persisted_spec(spec, state_root=self.state)
                argv = MODULE._parecord_argv(
                    spec,
                    pathlib.Path("/usr/bin/parecord"),
                    pathlib.Path(spec["paths"]["partial"]),
                )
                contract = MODULE.load_catalog(session_type)
                self.assertIn(
                    f"--client-name={contract['process']['client_name']}", argv
                )
                self.assertIn(
                    f"--stream-name={contract['process']['stream_name_prefix']}-{spec['session_id']}",
                    argv,
                )
                self.assertIn("--rate=48000", argv)
                self.assertIn("--channels=2", argv)

    def test_plain_filename_rejects_paths_hidden_files_and_controls(self) -> None:
        for invalid in ("../take.wav", ".take.wav", "take", "take\n.wav", " /tmp.wav"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(MODULE.RecordingError):
                    MODULE._plain_wav_name(invalid)
        self.assertEqual(MODULE._plain_wav_name("Stimme 01.wav"), "Stimme 01.wav")

    def test_private_directory_creation_allows_root_owned_parents(self) -> None:
        nested = self.base / "a" / "b" / "c"
        result = MODULE.ensure_private_directory(nested)
        self.assertEqual(result, nested)
        self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o700)

    def test_private_directory_rejects_symlink_component(self) -> None:
        real = self.base / "real"
        real.mkdir()
        link = self.base / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(MODULE.RecordingError):
            MODULE.ensure_private_directory(link / "child")

    def test_plan_is_ready_and_digest_is_stable_for_same_bound_inputs(self) -> None:
        first = self.ready_plan()
        second = self.ready_plan()
        self.assertTrue(first["ready"])
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(first["readiness"]["blockers"], [])
        self.assertFalse(self.state.exists())

    def test_performance_plan_binds_arecordmidi_digest_smpte_outputs_and_collisions(self) -> None:
        plan = self.ready_plan(session_type="piano-vocal-performance")
        self.assertTrue(plan["ready"])
        performance = plan["identity"]["performance"]
        self.assertEqual(performance["midi_output"]["name"], "take-01.mid")
        self.assertEqual(performance["manifest_output"]["name"], "take-01.take.json")
        self.assertEqual(
            performance["timing"],
            {"basis": "SMPTE", "fps": 25, "ticks_per_frame": 40, "nominal_resolution_ms": 1},
        )
        self.assertEqual(
            performance["capture_argv"],
            ["-p", "<plan-bound-client:port>", "-f", "25", "-t", "40", "<private-partial.mid>"],
        )
        self.assertEqual(performance["arecordmidi"]["resolved"]["sha256"], "a" * 64)
        self.assertEqual(performance["ffmpeg"]["resolved"]["sha256"], "c" * 64)
        self.assertEqual(
            performance["audio_capture"],
            {
                "sample_rate_hz": 48_000,
                "sources": ["motu-voice", "roland-fp-30x-usb-audio"],
                "maximum_spawn_spread_ns": MODULE.MAX_AUDIO_SPAWN_SPREAD_NS,
                "maximum_frame_difference": MODULE.MAX_AUDIO_FRAME_DIFFERENCE_FRAMES,
                "stems": "private-temporary-not-published",
            },
        )
        self.assertEqual(performance["mix"]["method"], "offline-ffmpeg-amix")
        self.assertEqual(performance["mix"]["duration"], "shortest")
        self.assertTrue(performance["mix"]["normalize"])
        self.assertEqual(plan["identity"]["monitoring"]["mode"], "hardware-direct")
        self.assertFalse(plan["identity"]["monitoring"]["software_loopback"])
        self.assertIn("roland_audio", plan["identity"]["source"]["identity"])
        (self.output / "take-01.mid").write_bytes(b"occupied")
        blocked = self.ready_plan(session_type="piano-vocal-performance")
        self.assertIn("midi-output-already-exists", blocked["readiness"]["blockers"])

    def test_performance_plan_rejects_timing_or_argv_drift(self) -> None:
        spec = self.persisted_spec(session_type="piano-vocal-performance")
        MODULE._validate_persisted_spec(spec, state_root=self.state)
        spec["plan_identity"]["performance"]["capture_argv"][4] = "24"
        spec["plan_sha256"] = MODULE.canonical_sha256(spec["plan_identity"])
        with self.assertRaisesRegex(MODULE.RecordingError, "performance recording plan"):
            MODULE._validate_persisted_spec(spec, state_root=self.state)

    def test_legacy_performance_spec_remains_readable_but_not_modernized(self) -> None:
        spec = self.persisted_spec(session_type="piano-vocal-performance")
        identity = spec["plan_identity"]
        performance = identity["performance"]
        for field in ("ffmpeg", "audio_capture", "mix"):
            performance.pop(field)
        performance["synchronization_boundary"] = (
            "process-start alignment only; not sample-accurate WAV/MIDI synchronization"
        )
        source = identity["source"]["identity"]
        source.pop("roland_audio")
        unbound = {"audio": source["audio"], "midi": source["midi"]}
        source["fingerprint"] = MODULE.canonical_sha256(unbound)
        identity["source"]["identity_sha256"] = MODULE.canonical_sha256(source)
        spec.pop("source_names")
        spec["source_name"] = "legacy-voice"
        spec["paths"].pop("voice_partial")
        spec["paths"].pop("roland_partial")
        spec["paths"].pop("mix_raw_partial")
        spec["plan_sha256"] = MODULE.canonical_sha256(identity)
        MODULE._validate_persisted_spec(spec, state_root=self.state)
        with self.assertRaisesRegex(MODULE.RecordingError, "legacy performance takes"):
            with mock.patch.object(
                MODULE, "contract_bindings", return_value=identity["contracts"]
            ), mock.patch.object(
                MODULE, "parecord_binding", return_value=identity["parecord"]
            ):
                MODULE._validate_spec(spec)

    def test_pre_bounded_tail_modern_performance_spec_is_readable_but_not_runnable(self) -> None:
        spec = self.persisted_spec(session_type="piano-vocal-performance")
        identity = spec["plan_identity"]
        identity["performance"]["audio_capture"].pop("maximum_frame_difference")
        spec["plan_sha256"] = MODULE.canonical_sha256(identity)

        MODULE._validate_persisted_spec(spec, state_root=self.state)
        self.assertEqual(
            MODULE._performance_audio_capture_generation(identity),
            "pre-bounded-tail-v1",
        )
        with (
            mock.patch.object(MODULE, "contract_bindings", return_value=identity["contracts"]),
            mock.patch.object(MODULE, "parecord_binding", return_value=identity["parecord"]),
        ):
            with self.assertRaisesRegex(MODULE.RecordingError, "pre-bounded-tail"):
                MODULE._validate_spec(spec)

    def test_modern_performance_spec_rejects_missing_roland_audio_identity(self) -> None:
        spec = self.persisted_spec(session_type="piano-vocal-performance")
        source = spec["plan_identity"]["source"]["identity"]
        source.pop("roland_audio")
        source["fingerprint"] = MODULE.canonical_sha256(
            {"audio": source["audio"], "midi": source["midi"]}
        )
        spec["plan_identity"]["source"]["identity_sha256"] = MODULE.canonical_sha256(source)
        spec["plan_sha256"] = MODULE.canonical_sha256(spec["plan_identity"])
        with self.assertRaisesRegex(MODULE.RecordingError, "modern performance recording source"):
            MODULE._validate_persisted_spec(spec, state_root=self.state)

    def test_completed_legacy_performance_result_with_vocal_wav_and_midi_remains_valid(self) -> None:
        spec = self.persisted_spec(session_type="piano-vocal-performance")
        identity = spec["plan_identity"]
        for field in ("ffmpeg", "audio_capture", "mix"):
            identity["performance"].pop(field)
        identity["performance"]["synchronization_boundary"] = (
            "process-start alignment only; not sample-accurate WAV/MIDI synchronization"
        )
        source = identity["source"]["identity"]
        source.pop("roland_audio")
        source["fingerprint"] = MODULE.canonical_sha256(
            {"audio": source["audio"], "midi": source["midi"]}
        )
        identity["source"]["identity_sha256"] = MODULE.canonical_sha256(source)
        spec.pop("source_names")
        spec["source_name"] = "legacy-voice"
        spec["paths"].pop("voice_partial")
        spec["paths"].pop("roland_partial")
        spec["paths"].pop("mix_raw_partial")
        spec["plan_sha256"] = MODULE.canonical_sha256(identity)
        MODULE._validate_persisted_spec(spec, state_root=self.state)

        final = pathlib.Path(spec["paths"]["final"])
        midi_final = pathlib.Path(spec["paths"]["midi_final"])
        manifest_final = pathlib.Path(spec["paths"]["manifest_final"])
        self.write_wave(final)
        midi_final.write_bytes(
            b"MThd"
            + struct.pack(">IHHH", 6, 0, 1, MODULE.MIDI.SMPTE_DIVISION)
            + b"MTrk"
            + struct.pack(">I", 4)
            + b"\x00\xff\x2f\x00"
        )
        midi_final.chmod(0o600)
        vocal = MODULE._validate_recorded_wave(final, identity["capture"])
        midi = MODULE._safe_regular_binding(
            midi_final,
            maximum_bytes=MODULE.MIDI.MAX_MIDI_BYTES,
            require_private=True,
            include_identity=True,
        )
        midi_meta = MODULE.MIDI.validate_smf(midi_final)
        does_not_establish = ["legacy-performance-readability"]
        MODULE._atomic_private_json(
            manifest_final,
            {
                "schema_version": 1,
                "kind": "piano_vocal_performance_take_manifest",
                "session_id": spec["session_id"],
                "plan_sha256": spec["plan_sha256"],
                "worker_capture_epoch_monotonic_ns": 1,
                "capture_timeline_offsets_ns": {
                    "midi_spawn_requested_offset_ns": 0,
                    "midi_running_observed_offset_ns": 1,
                    "audio_spawn_requested_offset_ns": 2,
                    "audio_running_observed_offset_ns": 3,
                    "session_ready_offset_ns": 4,
                },
                "midi_timing": identity["performance"]["timing"],
                "synchronization_boundary": identity["performance"]["synchronization_boundary"],
                "artifacts": {"vocal_wav": vocal, "roland_midi_smf": midi},
                "midi_event_counts": midi_meta["event_counts"],
                "midi_note_velocity": midi_meta["note_velocity"],
                "does_not_establish": does_not_establish,
            },
            create_only=True,
        )
        manifest = MODULE._safe_regular_binding(
            manifest_final,
            maximum_bytes=MODULE.MAX_JSON_BYTES,
            require_private=True,
            include_identity=True,
        )
        result = {
            "schema_version": 1,
            "kind": "audio_recording_result",
            "session_id": spec["session_id"],
            "status": "completed",
            "reason": "requested-stop",
            "started_at": "2026-07-31T00:00:00+00:00",
            "completed_at": "2026-07-31T00:00:01+00:00",
            "plan_sha256": spec["plan_sha256"],
            "processes": {
                "audio": {"returncode": 0, "stderr_bytes": 0, "stderr_sha256": hashlib.sha256(b"").hexdigest()},
                "midi": {"returncode": 0, "stderr_bytes": 0, "stderr_sha256": hashlib.sha256(b"").hexdigest()},
                "forced_kill": False,
            },
            "artifacts": {
                "vocal_wav": vocal,
                "roland_midi_smf": midi,
                "take_manifest": manifest,
            },
            "midi_event_counts": midi_meta["event_counts"],
            "does_not_establish": does_not_establish,
        }
        MODULE._validate_result(result, spec)

    def test_capture_child_group_is_owned_and_bounded_cleanup_reaps_it(self) -> None:
        with tempfile.TemporaryFile() as stderr:
            child = MODULE._spawn_capture_child(
                [sys.executable, "-c", "import time; time.sleep(30)"], stderr
            )
            self.addCleanup(lambda: child.poll() is None and child.kill())
            self.assertEqual(os.getpgid(child.pid), child.pid)
            returncodes, forced = MODULE._stop_capture_children([child], 2)
        self.assertFalse(forced)
        self.assertIn(returncodes[0], {-signal.SIGINT, 0})
        self.assertIsNotNone(child.poll())

    def test_arecordmidi_sigint_exit_requires_normal_finalization_code(self) -> None:
        self.assertTrue(MODULE._performance_child_exit_codes_clean([0, 0]))
        self.assertTrue(
            MODULE._performance_child_exit_codes_clean([-signal.SIGINT, 0])
        )
        self.assertFalse(
            MODULE._performance_child_exit_codes_clean([0, -signal.SIGINT])
        )
        self.assertFalse(MODULE._performance_child_exit_codes_clean([0, 1]))

    def test_wav_fsize_limit_uses_64_mib_floor_without_lowering_larger_budget(self) -> None:
        floor = 64 * 1024 * 1024
        self.assertEqual(MODULE.PARECORD_WAV_FSIZE_FLOOR_BYTES, floor)
        self.assertEqual(MODULE._wav_compatible_fsize_limit(2_200_576), floor)
        self.assertEqual(MODULE._wav_compatible_fsize_limit(floor + 1), floor + 1)

    def test_midi_capture_ready_accepts_private_empty_partial_only_for_live_child(self) -> None:
        partial = self.output / ".buffered.partial.mid"

        class FakeChild:
            def __init__(self, returncode):
                self.returncode = returncode

            def poll(self):
                return self.returncode

        partial.touch()
        partial.chmod(0o600)
        self.assertEqual(partial.stat().st_size, 0)
        self.assertTrue(MODULE._midi_capture_process_ready(FakeChild(None), partial))
        self.assertFalse(MODULE._midi_capture_process_ready(FakeChild(1), partial))

        partial.unlink()
        self.assertFalse(MODULE._midi_capture_process_ready(FakeChild(None), partial))

        partial.touch()
        partial.chmod(0o644)
        self.assertFalse(MODULE._midi_capture_process_ready(FakeChild(None), partial))

        partial.unlink()
        target = self.output / "target.mid"
        target.touch()
        target.chmod(0o600)
        partial.symlink_to(target)
        self.assertFalse(MODULE._midi_capture_process_ready(FakeChild(None), partial))

    def test_performance_mix_accepts_bounded_tail_difference_and_rejects_larger_drift(self) -> None:
        self.assertEqual(MODULE._performance_mix_frame_count(48_000, 48_000), 48_000)
        self.assertEqual(
            MODULE._performance_mix_frame_count(48_000, 47_760),
            47_760,
        )
        with self.assertRaisesRegex(MODULE.RecordingError, "bounded frame difference"):
            MODULE._performance_mix_frame_count(48_000, 48_000 - MODULE.MAX_AUDIO_FRAME_DIFFERENCE_FRAMES - 1)

    def test_modern_performance_startup_requires_both_audio_stems_and_ready_receipt(self) -> None:
        spec = self.persisted_spec(session_type="piano-vocal-performance")
        voice = pathlib.Path(spec["paths"]["voice_partial"])
        roland = pathlib.Path(spec["paths"]["roland_partial"])
        ready = self.state / ".performance.ready.json"
        MODULE.ensure_private_directory(self.state)

        voice.write_bytes(b"v" * 45)
        self.assertFalse(
            MODULE._recording_startup_ready(spec, worker_ready_path=ready)
        )
        roland.write_bytes(b"r" * 45)
        self.assertFalse(
            MODULE._recording_startup_ready(spec, worker_ready_path=ready)
        )
        ready.write_bytes(b"{}")
        self.assertTrue(MODULE._recording_startup_ready(spec, worker_ready_path=ready))

    def test_mid_publication_failure_preserves_uncommitted_siblings_without_manifest(self) -> None:
        wav_partial = self.output / ".song.partial.wav"
        wav_final = self.output / "song.wav"
        midi_partial = self.output / ".song.partial.mid"
        midi_final = self.output / "song.mid"
        manifest_final = self.output / "song.take.json"
        wav_partial.write_bytes(b"private-wave")
        midi_partial.write_bytes(b"private-midi")
        wav_partial.chmod(0o600)
        midi_partial.chmod(0o600)
        wav_binding = MODULE._safe_regular_binding(
            wav_partial, maximum_bytes=1024, require_private=True, include_identity=True
        )
        midi_binding = MODULE._safe_regular_binding(
            midi_partial, maximum_bytes=1024, require_private=True, include_identity=True
        )
        MODULE._link_no_replace_keep_partial(
            wav_partial, wav_final, wav_binding, maximum_bytes=1024
        )
        with mock.patch.object(MODULE.os, "link", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                MODULE._link_no_replace_keep_partial(
                    midi_partial, midi_final, midi_binding, maximum_bytes=1024
                )
        self.assertTrue(wav_final.is_file())
        self.assertTrue(wav_partial.is_file())
        self.assertTrue(midi_partial.is_file())
        self.assertFalse(midi_final.exists())
        self.assertFalse(manifest_final.exists())

    def test_worker_failure_before_manifest_commit_is_failed_preserved(self) -> None:
        MODULE.ensure_private_directory(self.state)
        spec = self.persisted_spec(
            session_id="c" * 24,
            name="performance.wav",
            session_type="piano-vocal-performance",
        )
        session_paths = MODULE._session_paths(self.state, spec["session_id"])
        MODULE._atomic_private_json(session_paths["spec"], spec, create_only=True)
        spec_binding = MODULE._safe_regular_binding(
            session_paths["spec"], require_private=True
        )
        silent_smf = (
            b"MThd"
            + struct.pack(">IHHH", 6, 0, 1, MODULE.MIDI.SMPTE_DIVISION)
            + b"MTrk"
            + struct.pack(">I", 4)
            + b"\x00\xff\x2f\x00"
        )

        class FakeProcess:
            @staticmethod
            def poll():
                return None

            @staticmethod
            def wait(*, timeout):
                return 0

        def fake_spawn(argv, _stderr):
            output = pathlib.Path(argv[-1])
            if output.suffix == ".mid":
                output.write_bytes(silent_smf)
                output.chmod(0o600)
            elif output.suffix == ".s32le":
                output.write_bytes(b"\0" * 1000 * 2 * 4)
                output.chmod(0o600)
            else:
                self.write_wave(output)
            return FakeProcess()

        real_link = MODULE._link_no_replace_keep_partial
        link_count = 0

        def fail_manifest_link(partial, final, binding, *, maximum_bytes):
            nonlocal link_count
            link_count += 1
            if link_count == 3:
                raise OSError("injected before final manifest")
            return real_link(
                partial, final, binding, maximum_bytes=maximum_bytes
            )

        def run_performance(_spec):
            return MODULE._performance_worker_run(
                _spec,
                pathlib.Path("/fixture/parecord"),
                pathlib.Path("/fixture/arecordmidi"),
                pathlib.Path("/fixture/ffmpeg"),
            )

        with (
            mock.patch.object(MODULE, "worker_run", side_effect=run_performance),
            mock.patch.object(MODULE, "_spawn_capture_child", side_effect=fake_spawn),
            mock.patch.object(
                MODULE, "_stop_capture_children", return_value=([0, 0, 0], False)
            ),
            mock.patch.object(
                MODULE.time,
                "monotonic",
                side_effect=[0.0, 0.1, 0.2, 0.3, 0.4, 100.0],
            ),
            mock.patch.object(MODULE.resource, "setrlimit"),
            mock.patch.object(MODULE.os, "umask"),
            mock.patch.object(MODULE.signal, "signal"),
            mock.patch.object(
                MODULE,
                "_link_no_replace_keep_partial",
                side_effect=fail_manifest_link,
            ),
        ):
            returncode = MODULE.worker_entry(
                session_paths["spec"], spec_binding["sha256"]
            )

        self.assertEqual(returncode, 1)
        result = MODULE._safe_json_read(session_paths["result"], require_private=True)
        MODULE._validate_result(result, spec)
        self.assertEqual(result["status"], "failed-preserved")
        self.assertTrue(pathlib.Path(spec["paths"]["final"]).is_file())
        self.assertTrue(pathlib.Path(spec["paths"]["midi_final"]).is_file())
        self.assertFalse(pathlib.Path(spec["paths"]["manifest_final"]).exists())
        self.assertIsNotNone(result["performance_artifacts"]["mix_final"])
        self.assertIsNotNone(result["performance_artifacts"]["midi_final"])
        self.assertIsNone(result["performance_artifacts"]["manifest_final"])

    def test_roland_spawn_failure_stops_the_already_started_voice_capture(self) -> None:
        spec = self.persisted_spec(session_type="piano-vocal-performance")
        midi_partial = pathlib.Path(spec["paths"]["midi_partial"])

        class FakeProcess:
            def poll(self):
                return None

        midi_process = FakeProcess()
        voice_process = FakeProcess()
        spawned = []

        def fake_spawn(argv, _stderr):
            spawned.append(argv)
            if len(spawned) == 1:
                midi_partial.touch()
                midi_partial.chmod(0o600)
                return midi_process
            if len(spawned) == 2:
                return voice_process
            raise OSError("injected Roland spawn failure")

        cleanup = mock.Mock(return_value=([0, 0], False))
        with (
            mock.patch.object(MODULE, "_spawn_capture_child", side_effect=fake_spawn),
            mock.patch.object(MODULE, "_stop_capture_children", cleanup),
            mock.patch.object(MODULE.resource, "setrlimit"),
            mock.patch.object(MODULE.os, "umask"),
            mock.patch.object(MODULE.signal, "signal"),
        ):
            with self.assertRaisesRegex(OSError, "Roland spawn failure"):
                MODULE._performance_worker_run(
                    spec,
                    pathlib.Path("/fixture/parecord"),
                    pathlib.Path("/fixture/arecordmidi"),
                    pathlib.Path("/fixture/ffmpeg"),
                )

        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(cleanup.call_args.args[0], [midi_process, voice_process])

    def test_source_projection_ignores_volatile_observation_metadata(self) -> None:
        identity = self.source_identity()
        contract = MODULE.load_catalog()["source"]
        first, first_blockers = MODULE._source_projection(
            contract,
            lambda: {
                "complete": True,
                "identity": identity,
                "observed_at": "2026-07-31T10:00:00+00:00",
                "observation_sha256": "a" * 64,
            },
        )
        second, second_blockers = MODULE._source_projection(
            contract,
            lambda: {
                "complete": True,
                "identity": identity,
                "observed_at": "2026-07-31T10:00:01+00:00",
                "observation_sha256": "b" * 64,
            },
        )
        self.assertEqual(first_blockers, [])
        self.assertEqual(second_blockers, [])
        self.assertEqual(first, second)
        self.assertNotIn("observation_sha256", first)
        self.assertEqual(first["identity_sha256"], MODULE.canonical_sha256(identity))

    def test_plan_blocks_low_space_existing_output_and_active_pointer(self) -> None:
        low = self.ready_plan(free=1)
        self.assertIn("free-space-insufficient", low["readiness"]["blockers"])
        storage = next(check for check in low["readiness"]["checks"] if check["id"] == "storage")
        self.assertEqual(storage["status"], "blocked")
        self.assertEqual(storage["blockers"], ["free-space-insufficient"])
        (self.output / "take-01.wav").write_bytes(b"occupied")
        occupied = self.ready_plan()
        self.assertIn("output-already-exists", occupied["readiness"]["blockers"])
        (self.output / "take-01.wav").unlink()
        self.state.mkdir(mode=0o700)
        (self.state / "active.json").write_text("{}")
        active = self.ready_plan()
        self.assertIn(
            "active-session-requires-status-or-recovery",
            active["readiness"]["blockers"],
        )

    def test_plan_preserves_physical_laboratory_and_source_blockers(self) -> None:
        with (
            mock.patch.object(
                MODULE,
                "_physical_projection",
                return_value=(
                    {"state_sha256": None},
                    ["physical-fact:rode_nt1a_connected"],
                ),
            ),
            mock.patch.object(
                MODULE,
                "_laboratory_projection",
                return_value=(
                    {"state_sha256": None},
                    ["laboratory-gate:voice-level-measurement"],
                ),
            ),
            mock.patch.object(
                MODULE,
                "_source_projection",
                return_value=({"identity": None}, ["motu-source-not-unique"]),
            ),
            mock.patch.object(MODULE, "contract_bindings", return_value=[]),
            mock.patch.object(MODULE, "parecord_binding", return_value={}),
        ):
            plan = MODULE.build_plan(
                "take.wav",
                10,
                output_root=self.output,
                state_root=self.state,
                disk_usage_fn=lambda _path: types.SimpleNamespace(free=20_000_000_000),
            )
        self.assertFalse(plan["ready"])
        self.assertEqual(
            plan["readiness"]["blockers"],
            [
                "motu-source-not-unique",
                "physical-fact:rode_nt1a_connected",
            ],
        )
        self.assertEqual(
            plan["readiness"]["advisories"],
            [
                {
                    "id": "voice-level",
                    "status": "attention",
                    "notices": ["laboratory-gate:voice-level-measurement"],
                }
            ],
        )

    def test_voice_level_is_advisory_and_does_not_block_recording_readiness(self) -> None:
        physical = {
            "state_sha256": "5" * 64,
            "facts": {
                "rode_nt1a_connected": True,
                "rode_nt1a_motu_input": "input-1",
                "motu_phantom_48v": "on",
                "motu_input_gain_reference": "mark 10",
            },
            "error": None,
        }
        source_identity = self.source_identity("voice-recording")
        source = {
            "identity": source_identity,
            "identity_sha256": MODULE.canonical_sha256(source_identity),
            "error": None,
        }
        advisory = {
            "state_sha256": "6" * 64,
            "resolved": [],
            "invalidated": {"voice-level-measurement": "missing"},
            "receipt_sha256": {},
            "error": None,
        }
        with (
            mock.patch.object(MODULE, "_physical_projection", return_value=(physical, [])),
            mock.patch.object(
                MODULE,
                "_laboratory_projection",
                return_value=(
                    advisory,
                    ["laboratory-gate:voice-level-measurement"],
                ),
            ),
            mock.patch.object(MODULE, "_source_projection", return_value=(source, [])),
            mock.patch.object(MODULE, "contract_bindings", return_value=[]),
            mock.patch.object(MODULE, "parecord_binding", return_value={}),
        ):
            plan = MODULE.build_plan(
                "voice-advisory.wav",
                10,
                session_type="voice-recording",
                output_root=self.output,
                state_root=self.state,
                disk_usage_fn=lambda _path: types.SimpleNamespace(free=20_000_000_000),
            )
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["readiness"]["blockers"], [])
        self.assertEqual(
            plan["readiness"]["advisories"],
            [
                {
                    "id": "voice-level",
                    "status": "attention",
                    "notices": ["laboratory-gate:voice-level-measurement"],
                }
            ],
        )

    def test_piano_resampling_remains_hard_while_voice_level_is_advisory(self) -> None:
        physical = {
            "state_sha256": "5" * 64,
            "facts": {
                "rode_nt1a_connected": True,
                "rode_nt1a_motu_input": "input-1",
                "motu_phantom_48v": "on",
                "motu_input_gain_reference": "mark 10",
            },
            "error": None,
        }
        source_identity = self.source_identity("piano-vocal-performance")
        source = {
            "identity": source_identity,
            "identity_sha256": MODULE.canonical_sha256(source_identity),
            "error": None,
        }

        def laboratory_projection(_path, _physical, required):
            gate = required[0]
            projection = {
                "state_sha256": "6" * 64,
                "resolved": [],
                "invalidated": {gate: "missing"},
                "receipt_sha256": {},
                "error": None,
            }
            return projection, [f"laboratory-gate:{gate}"]

        with (
            mock.patch.object(MODULE, "_physical_projection", return_value=(physical, [])),
            mock.patch.object(
                MODULE, "_laboratory_projection", side_effect=laboratory_projection
            ),
            mock.patch.object(MODULE, "_source_projection", return_value=(source, [])),
            mock.patch.object(MODULE, "contract_bindings", return_value=[]),
            mock.patch.object(MODULE, "parecord_binding", return_value={}),
            mock.patch.object(MODULE, "arecordmidi_binding", return_value={}),
            mock.patch.object(MODULE, "ffmpeg_binding", return_value={}),
        ):
            plan = MODULE.build_plan(
                "piano-advisory.wav",
                10,
                session_type="piano-vocal-performance",
                output_root=self.output,
                state_root=self.state,
                disk_usage_fn=lambda _path: types.SimpleNamespace(free=20_000_000_000),
            )
        self.assertFalse(plan["ready"])
        self.assertEqual(
            plan["readiness"]["blockers"],
            ["laboratory-gate:resampling-decision"],
        )
        self.assertEqual(
            plan["readiness"]["advisories"],
            [
                {
                    "id": "voice-level",
                    "status": "attention",
                    "notices": ["laboratory-gate:voice-level-measurement"],
                }
            ],
        )

    def test_atomic_private_json_is_mode_0600_and_create_only(self) -> None:
        path = self.state / "receipt.json"
        MODULE._atomic_private_json(path, {"value": 1}, create_only=True)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(
            MODULE._safe_json_read(path, require_private=True), {"value": 1}
        )
        with self.assertRaises(MODULE.RecordingError):
            MODULE._atomic_private_json(path, {"value": 2}, create_only=True)

    def test_json_payload_and_digest_share_one_bound_snapshot(self) -> None:
        path = self.state / "bound.json"
        payload = {"alpha": 1, "beta": [2, 3]}
        MODULE._atomic_private_json(path, payload, create_only=True)
        observed, binding = MODULE._safe_json_read_with_binding(
            path, require_private=True
        )
        self.assertEqual(observed, payload)
        self.assertEqual(
            binding["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.assertEqual(binding["bytes"], path.stat().st_size)

    def test_static_bindings_do_not_capture_inode_identity(self) -> None:
        path = self.base / "bound.bin"
        path.write_bytes(b"bound")
        path.chmod(0o600)
        static = MODULE._safe_regular_binding(path, require_private=True)
        transient = MODULE._safe_regular_binding(
            path, require_private=True, include_identity=True
        )
        self.assertNotIn("device", static)
        self.assertNotIn("inode", static)
        self.assertEqual(transient["device"], path.stat().st_dev)
        self.assertEqual(transient["inode"], path.stat().st_ino)

    def test_physical_projection_scopes_historical_catalog_drift(self) -> None:
        path = self.base / "physical.json"
        state = MODULE.PHYSICAL.empty_state()
        MODULE.PHYSICAL.record_fact(
            state, "rode_nt1a_connected", "true", "visual"
        )
        state["catalog_sha256"] = "f" * 64
        state["facts"]["retired-unrelated-fact"] = {"not": "current"}
        MODULE.PHYSICAL.atomic_write_private(path, state)

        projection, blockers = MODULE._physical_projection(
            path, {"rode_nt1a_connected": True}
        )

        self.assertEqual(blockers, [])
        self.assertEqual(projection["facts"], {"rode_nt1a_connected": True})
        self.assertIsNotNone(projection["state_sha256"])

    def test_physical_projection_still_rejects_invalid_required_fact(self) -> None:
        path = self.base / "physical.json"
        state = MODULE.PHYSICAL.empty_state()
        MODULE.PHYSICAL.record_fact(
            state, "rode_nt1a_connected", "true", "visual"
        )
        state["catalog_sha256"] = "f" * 64
        state["facts"]["rode_nt1a_connected"]["evidence"] = "invented"
        MODULE.PHYSICAL.atomic_write_private(path, state)

        projection, blockers = MODULE._physical_projection(
            path, {"rode_nt1a_connected": True}
        )

        self.assertEqual(blockers, ["physical-state-invalid"])
        self.assertIn("invalid evidence", projection["error"])

    def test_laboratory_projection_scopes_historical_and_unrelated_drift(self) -> None:
        path = self.base / "laboratory.json"
        gate = "voice-level-measurement"
        evidence = {"kind": "bound-current-evidence"}
        now = MODULE.utc_now()
        receipt = {
            "status": "passed",
            "recorded_at": now,
            "evidence_sha256": MODULE.LAB.canonical_sha256(evidence),
            "physical_state_sha256": "a" * 64,
            "evidence": evidence,
        }
        state = MODULE.LAB.empty_state()
        state["catalog_sha256"] = "1" * 64
        state["profile_catalog_sha256"] = "2" * 64
        state["gates"] = {gate: receipt, "retired-unrelated-gate": "invalid"}
        state["updated_at"] = now
        MODULE.LAB.atomic_write_private(path, state)

        with (
            mock.patch.object(MODULE.LAB, "validate_evidence"),
            mock.patch.object(
                MODULE.LAB, "has_bound_voice_capture", return_value=True
            ),
        ):
            projection, blockers = MODULE._laboratory_projection(
                path,
                {
                    "state_path": str(self.base / "physical.json"),
                    "state_sha256": "a" * 64,
                },
                [gate],
            )

        self.assertEqual(blockers, [])
        self.assertEqual(projection["resolved"], [gate])
        self.assertEqual(projection["invalidated"], {})
        self.assertIsNotNone(projection["state_sha256"])

    def test_laboratory_projection_binds_exact_physical_snapshot(self) -> None:
        path = self.base / "laboratory.json"
        gate = "voice-level-measurement"
        evidence = {"kind": "bound-current-evidence"}
        now = MODULE.utc_now()
        receipt = {
            "status": "passed",
            "recorded_at": now,
            "evidence_sha256": MODULE.LAB.canonical_sha256(evidence),
            "physical_state_sha256": "b" * 64,
            "evidence": evidence,
        }
        state = MODULE.LAB.empty_state()
        state["gates"] = {gate: receipt}
        state["updated_at"] = now
        MODULE.LAB.atomic_write_private(path, state)

        with (
            mock.patch.object(MODULE.LAB, "validate_evidence"),
            mock.patch.object(
                MODULE.LAB, "has_bound_voice_capture", return_value=True
            ),
        ):
            projection, blockers = MODULE._laboratory_projection(
                path,
                {
                    "state_path": str(self.base / "physical.json"),
                    "state_sha256": "a" * 64,
                },
                [gate],
            )

        self.assertEqual(blockers, [f"laboratory-gate:{gate}"])
        self.assertEqual(projection["invalidated"][gate], "physical-state-changed")
        self.assertIsNotNone(projection["state_sha256"])

    def test_live_performance_preconditions_reserve_four_audio_artifacts(self) -> None:
        spec = self.persisted_spec(session_type="piano-vocal-performance", maximum_seconds=1)
        plan = spec["plan_identity"]
        maximum = int(plan["capture"]["maximum_file_bytes"])
        reserve = int(plan["capture"]["free_space_reserve_bytes"])
        contract = MODULE.load_catalog("piano-vocal-performance")
        with (
            mock.patch.object(MODULE, "load_catalog", return_value=contract),
            mock.patch.object(MODULE, "_physical_projection", return_value=(plan["physical"], [])),
            mock.patch.object(MODULE, "_laboratory_projection", return_value=(plan["laboratory"], [])),
            mock.patch.object(MODULE, "_source_projection", return_value=(plan["source"], [])),
            mock.patch.object(
                MODULE.shutil,
                "disk_usage",
                return_value=MODULE.shutil._ntuple_diskusage(0, 0, reserve + 3 * maximum + MODULE.MIDI.MAX_MIDI_BYTES + MODULE.MAX_JSON_BYTES),
            ),
        ):
            with self.assertRaisesRegex(MODULE.RecordingError, "free space fell below"):
                MODULE._validate_live_preconditions(spec)

    def test_live_voice_preconditions_do_not_rebind_advisory_laboratory_state(self) -> None:
        spec = self.persisted_spec(session_type="voice-recording", maximum_seconds=1)
        plan = spec["plan_identity"]
        self.assertIsNone(plan["laboratory"]["state_sha256"])
        contract = MODULE.load_catalog("voice-recording")
        self.assertEqual(contract["required_laboratory_gates"], [])
        with (
            mock.patch.object(MODULE, "load_catalog", return_value=contract),
            mock.patch.object(
                MODULE, "_physical_projection", return_value=(plan["physical"], [])
            ),
            mock.patch.object(MODULE, "_laboratory_projection") as laboratory_projection,
            mock.patch.object(
                MODULE, "_source_projection", return_value=(plan["source"], [])
            ),
            mock.patch.object(
                MODULE.shutil,
                "disk_usage",
                return_value=MODULE.shutil._ntuple_diskusage(
                    0,
                    0,
                    int(plan["capture"]["free_space_reserve_bytes"])
                    + int(plan["capture"]["maximum_file_bytes"])
                    + 1,
                ),
            ),
        ):
            MODULE._validate_live_preconditions(spec)
        laboratory_projection.assert_not_called()

    def test_live_preconditions_reject_source_identity_drift(self) -> None:
        physical = {"state_path": str(self.base / "physical.json")}
        laboratory = {"state_path": str(self.base / "laboratory.json")}
        planned_source = {"identity": {"fingerprint": "planned"}}
        changed_source = {"identity": {"fingerprint": "changed"}}
        spec = {
            "plan_identity": {
                "session_type": "voice-recording",
                "physical": physical,
                "laboratory": laboratory,
                "source": planned_source,
                "output": {
                    "root": str(self.output),
                    "path": str(self.output / "take.wav"),
                },
                "capture": {
                    "maximum_file_bytes": 1_000_000,
                    "free_space_reserve_bytes": 1_000_000,
                },
            }
        }
        contract = {
            "required_physical_facts": {},
            "required_laboratory_gates": ["voice-level-measurement"],
            "source": {},
        }
        with (
            mock.patch.object(MODULE, "load_catalog", return_value=contract),
            mock.patch.object(
                MODULE, "_physical_projection", return_value=(physical, [])
            ),
            mock.patch.object(
                MODULE, "_laboratory_projection", return_value=(laboratory, [])
            ),
            mock.patch.object(
                MODULE, "_source_projection", return_value=(changed_source, [])
            ),
        ):
            with self.assertRaisesRegex(
                MODULE.RecordingError, "recording source identity changed"
            ):
                MODULE._validate_live_preconditions(spec)

    def write_wave(self, path: pathlib.Path, frames: int = 1000) -> None:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(4)
            handle.setframerate(48_000)
            handle.writeframes(b"\0" * frames * 2 * 4)
        path.chmod(0o600)

    def capture_contract(self, maximum_seconds: int = 60) -> dict[str, object]:
        return {
            "sample_rate_hz": 48_000,
            "channels": 2,
            "maximum_duration_seconds": maximum_seconds,
            "maximum_file_bytes": 48_000 * 2 * 4 * maximum_seconds + 1_048_576,
        }

    def persisted_spec(
        self,
        *,
        session_id: str = "a" * 24,
        name: str = "take.wav",
        maximum_seconds: int = 60,
        session_type: str = "voice-recording",
    ) -> dict[str, object]:
        plan_identity = self.ready_plan(session_type=session_type)["identity"]
        final = self.output / name
        plan_identity["output"] = {
            "root": str(self.output),
            "name": name,
            "path": str(final),
            "mode": "0600",
            "overwrite": False,
        }
        capture = plan_identity["capture"]
        capture["maximum_duration_seconds"] = maximum_seconds
        capture["maximum_file_bytes"] = 48_000 * 2 * 4 * maximum_seconds + 1_048_576
        plan_identity["state_root"] = str(self.state)
        paths = MODULE._session_paths(self.state, session_id)
        spec = {
            "schema_version": 1,
            "kind": "audio_recording_session_spec",
            "session_id": session_id,
            "created_at": "2026-07-31T00:00:00+00:00",
            "plan_sha256": MODULE.canonical_sha256(plan_identity),
            "plan_identity": plan_identity,
            "paths": {
                "partial": str(self.output / f".{final.stem}.{session_id}.partial.wav"),
                "final": str(final),
                "result": str(paths["result"]),
            },
        }
        if session_type == "piano-vocal-performance":
            midi_final = final.with_suffix(".mid")
            manifest_final = final.with_suffix(".take.json")
            plan_identity["performance"]["midi_output"].update(
                {"name": midi_final.name, "path": str(midi_final)}
            )
            plan_identity["performance"]["manifest_output"].update(
                {"name": manifest_final.name, "path": str(manifest_final)}
            )
            spec["midi_source"] = "24:0"
            spec["source_names"] = {"voice": "redacted-voice", "roland": "redacted-roland"}
            spec["paths"].update(
                {
                    "voice_partial": str(
                        self.output / f".{final.stem}.{session_id}.voice.partial.wav"
                    ),
                    "roland_partial": str(
                        self.output / f".{final.stem}.{session_id}.roland.partial.wav"
                    ),
                    "mix_raw_partial": str(
                        self.output / f".{final.stem}.{session_id}.mix.partial.s32le"
                    ),
                    "midi_partial": str(
                        self.output / f".{final.stem}.{session_id}.partial.mid"
                    ),
                    "midi_final": str(midi_final),
                    "manifest_partial": str(
                        self.output
                        / f".{final.stem}.{session_id}.partial.take.json"
                    ),
                    "manifest_final": str(manifest_final),
                }
            )
            spec["plan_sha256"] = MODULE.canonical_sha256(plan_identity)
        else:
            spec["source_name"] = "redacted"
        return spec

    def completed_result(self, spec: dict[str, object]) -> dict[str, object]:
        final = pathlib.Path(spec["paths"]["final"])
        self.write_wave(final)
        artifact = MODULE._validate_recorded_wave(
            final, spec["plan_identity"]["capture"]
        )
        return {
            "schema_version": 1,
            "kind": "audio_recording_result",
            "session_id": spec["session_id"],
            "status": "completed",
            "reason": "requested-stop",
            "started_at": "2026-07-31T00:00:00+00:00",
            "completed_at": "2026-07-31T00:00:01+00:00",
            "plan_sha256": spec["plan_sha256"],
            "process": {
                "returncode": 0,
                "forced_kill": False,
                "stderr_bytes": 0,
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                "stderr_truncated": False,
            },
            "artifact": artifact,
            "does_not_establish": ["subjective-recording-quality"],
        }

    def test_wave_validation_and_no_replace_publication(self) -> None:
        partial = self.output / ".partial.wav"
        final = self.output / "final.wav"
        self.write_wave(partial)
        artifact = MODULE._validate_recorded_wave(partial, self.capture_contract())
        self.assertEqual(artifact["sample_rate_hz"], 48_000)
        self.assertEqual(artifact["bit_depth_container"], 32)
        MODULE._publish_no_replace(partial, final, artifact)
        self.assertFalse(partial.exists())
        self.assertTrue(final.is_file())
        self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o600)

    def test_publication_never_overwrites_and_preserves_partial(self) -> None:
        partial = self.output / ".partial.wav"
        final = self.output / "final.wav"
        self.write_wave(partial)
        artifact = MODULE._validate_recorded_wave(partial, self.capture_contract())
        final.write_bytes(b"existing")
        with self.assertRaises(MODULE.RecordingError):
            MODULE._publish_no_replace(partial, final, artifact)
        self.assertTrue(partial.exists())
        self.assertEqual(final.read_bytes(), b"existing")

    def test_publication_rejects_partial_changed_after_validation(self) -> None:
        partial = self.output / ".changed.partial.wav"
        final = self.output / "changed.wav"
        self.write_wave(partial)
        artifact = MODULE._validate_recorded_wave(partial, self.capture_contract())
        with partial.open("ab") as handle:
            handle.write(b"changed")
        with self.assertRaises(MODULE.RecordingError):
            MODULE._publish_no_replace(partial, final, artifact)
        self.assertTrue(partial.exists())
        self.assertFalse(final.exists())

    def test_process_identity_detects_mutation(self) -> None:
        identity = MODULE._proc_identity(os.getpid())
        self.assertIsNotNone(identity)
        self.assertTrue(MODULE._identity_matches(identity))
        changed = dict(identity)
        changed["start_ticks"] += 1
        self.assertFalse(MODULE._identity_matches(changed))

    def test_exact_termination_stops_only_the_bound_process(self) -> None:
        process = subprocess.Popen(
            [os.sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            identity = MODULE._proc_identity(process.pid)
            self.assertIsNotNone(identity)
            changed = dict(identity)
            changed["start_ticks"] += 1
            self.assertTrue(MODULE._terminate_exact_process(changed, grace_seconds=0.1))
            self.assertIsNone(process.poll())
            self.assertTrue(
                MODULE._terminate_exact_process(identity, grace_seconds=1.0)
            )
            process.wait(timeout=5)
            self.assertIsNone(MODULE._proc_identity(process.pid))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_recovery_preserves_partial_and_clears_active_pointer(self) -> None:
        MODULE.ensure_private_directory(self.state)
        session_id = "a" * 24
        paths = MODULE._session_paths(self.state, session_id)
        spec = self.persisted_spec(session_id=session_id, name="take.wav")
        partial = pathlib.Path(spec["paths"]["partial"])
        partial.write_bytes(b"partial bytes")
        partial.chmod(0o600)
        MODULE._atomic_private_json(paths["spec"], spec, create_only=True)
        spec_sha = MODULE._safe_regular_binding(paths["spec"], require_private=True)[
            "sha256"
        ]
        state = {
            "schema_version": 1,
            "kind": "audio_recording_session_state",
            "session_id": session_id,
            "spec_sha256": spec_sha,
            "started_at": "2026-07-31T00:00:00+00:00",
            "process": {
                "pid": 999_999_999,
                "start_ticks": 1,
                "executable": "/missing",
                "cmdline_sha256": "0" * 64,
                "process_group": 999_999_999,
            },
        }
        MODULE._atomic_private_json(paths["state"], state, create_only=True)
        MODULE._atomic_private_json(
            paths["active"],
            {
                "schema_version": 1,
                "kind": "audio_recording_active",
                "session_id": session_id,
                "spec_sha256": spec_sha,
            },
            create_only=True,
        )
        status = MODULE.recover_session(state_root=self.state)
        self.assertEqual(status["status"], "failed-preserved")
        self.assertFalse(paths["active"].exists())
        self.assertTrue(partial.exists())
        result = MODULE._safe_json_read(paths["result"], require_private=True)
        self.assertEqual(result["reason"], "worker-exited-without-terminal-receipt")
        self.assertIsNotNone(result["partial"])

    def test_persisted_spec_binds_output_mode_and_state_root(self) -> None:
        spec = self.persisted_spec()
        MODULE._validate_persisted_spec(spec, state_root=self.state)

        bad_mode = json.loads(json.dumps(spec))
        bad_mode["plan_identity"]["output"]["mode"] = "0644"
        bad_mode["plan_sha256"] = MODULE.canonical_sha256(bad_mode["plan_identity"])
        with self.assertRaisesRegex(MODULE.RecordingError, "output plan"):
            MODULE._validate_persisted_spec(bad_mode, state_root=self.state)

        bad_root = json.loads(json.dumps(spec))
        bad_root["plan_identity"]["state_root"] = str(self.base / "other-state")
        bad_root["plan_sha256"] = MODULE.canonical_sha256(bad_root["plan_identity"])
        with self.assertRaisesRegex(MODULE.RecordingError, "state root"):
            MODULE._validate_persisted_spec(bad_root, state_root=self.state)

    def test_pre_advisory_voice_spec_remains_readable_for_recovery(self) -> None:
        spec = self.persisted_spec()
        plan = spec["plan_identity"]
        advisory = plan.pop("advisory_laboratory")
        plan["laboratory"] = advisory
        spec["plan_sha256"] = MODULE.canonical_sha256(plan)
        MODULE._validate_persisted_spec(spec, state_root=self.state)

    def test_session_state_rejects_malformed_process_identity(self) -> None:
        state = {
            "schema_version": 1,
            "kind": "audio_recording_session_state",
            "session_id": "a" * 24,
            "spec_sha256": "b" * 64,
            "started_at": "2026-07-31T00:00:00+00:00",
            "phase": "running",
            "process": {
                "pid": 1234,
                "start_ticks": 1,
                "executable": "/usr/bin/python3",
                "process_group": 1234,
            },
        }
        with self.assertRaisesRegex(MODULE.RecordingError, "process identity"):
            MODULE._validate_session_state(
                state, session_id="a" * 24, spec_sha256="b" * 64
            )

    def test_completed_result_detects_invalid_exit_and_artifact_drift(self) -> None:
        spec = self.persisted_spec(session_id="d" * 24, name="completed.wav")
        result = self.completed_result(spec)
        MODULE._validate_result(result, spec)

        unknown_field = json.loads(json.dumps(result))
        unknown_field["artifact"]["unexpected"] = True
        with self.assertRaisesRegex(MODULE.RecordingError, "binding fields"):
            MODULE._validate_result(unknown_field, spec)

        reversed_timeline = json.loads(json.dumps(result))
        reversed_timeline["completed_at"] = "2026-07-30T23:59:59+00:00"
        with self.assertRaisesRegex(MODULE.RecordingError, "timeline"):
            MODULE._validate_result(reversed_timeline, spec)

        invalid_exit = json.loads(json.dumps(result))
        invalid_exit["process"]["returncode"] = 1
        with self.assertRaisesRegex(MODULE.RecordingError, "process receipt"):
            MODULE._validate_result(invalid_exit, spec)

        final = pathlib.Path(spec["paths"]["final"])
        with final.open("ab") as handle:
            handle.write(b"drift")
        with self.assertRaisesRegex(MODULE.RecordingError, "no longer matches"):
            MODULE._validate_result(result, spec)

    def test_failed_result_rejects_artifact_path_substitution(self) -> None:
        spec = self.persisted_spec(session_id="e" * 24, name="failed.wav")
        result = {
            "schema_version": 1,
            "kind": "audio_recording_result",
            "session_id": spec["session_id"],
            "status": "failed-preserved",
            "reason": "RecordingError",
            "failed_at": "2026-07-31T00:00:01+00:00",
            "error": "capture failed",
            "plan_sha256": spec["plan_sha256"],
            "partial": {
                "path": str(self.output / "substituted.partial.wav"),
                "error": "missing",
            },
            "does_not_establish": ["successful-recording"],
        }
        with self.assertRaisesRegex(MODULE.RecordingError, "path does not match"):
            MODULE._validate_result(result, spec)

    def test_parecord_binding_covers_launcher_and_resolved_binary(self) -> None:
        resolved = self.base / "pacat"
        resolved.write_text("#!/bin/sh\nexit 0\n")
        resolved.chmod(0o755)
        launcher = self.base / "parecord"
        launcher.symlink_to(resolved.name)

        binding = MODULE.parecord_binding(launcher)

        self.assertEqual(binding["launcher"], str(launcher))
        self.assertEqual(binding["launcher_symlink_target"], resolved.name)
        self.assertEqual(binding["resolved"]["path"], str(resolved))
        self.assertEqual(
            binding["resolved"]["sha256"],
            hashlib.sha256(resolved.read_bytes()).hexdigest(),
        )

    def test_parecord_binding_rejects_missing_test_executable(self) -> None:
        with self.assertRaises(MODULE.RecordingError):
            MODULE.parecord_binding(self.base / "missing-parecord")

    def test_arecordmidi_binding_hashes_the_current_resolved_binary(self) -> None:
        resolved = self.base / "arecordmidi-fixture"
        resolved.write_bytes(b"#!/bin/sh\nexit 0\n")
        resolved.chmod(0o755)
        launcher = self.base / "arecordmidi"
        launcher.symlink_to(resolved.name)

        binding = MODULE.arecordmidi_binding(launcher)

        self.assertEqual(binding["launcher"], str(launcher))
        self.assertEqual(binding["launcher_symlink_target"], resolved.name)
        self.assertEqual(
            binding["resolved"]["sha256"],
            hashlib.sha256(resolved.read_bytes()).hexdigest(),
        )

    def test_performance_start_rejects_arecordmidi_digest_drift(self) -> None:
        spec = self.persisted_spec(session_type="piano-vocal-performance")
        planned = spec["plan_identity"]["performance"]["arecordmidi"]
        changed = json.loads(json.dumps(planned))
        changed["resolved"]["sha256"] = "f" * 64
        self.assertNotEqual(changed, planned)
        with (
            mock.patch.object(
                MODULE,
                "contract_bindings",
                return_value=spec["plan_identity"]["contracts"],
            ),
            mock.patch.object(
                MODULE,
                "parecord_binding",
                return_value=spec["plan_identity"]["parecord"],
            ),
            mock.patch.object(MODULE, "arecordmidi_binding", return_value=changed),
        ):
            with self.assertRaisesRegex(MODULE.RecordingError, "arecordmidi changed"):
                MODULE._validate_spec(spec)

    def test_worker_cleanly_stops_fake_recorder_and_publishes_wav(self) -> None:
        fake = self.base / "fake-parecord"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import signal, sys, time, wave\n"
            "stop = False\n"
            "def handler(_signum, _frame):\n"
            "    global stop\n"
            "    stop = True\n"
            "signal.signal(signal.SIGINT, handler)\n"
            "output = sys.argv[-1]\n"
            "with wave.open(output, 'wb') as handle:\n"
            "    handle.setnchannels(2)\n"
            "    handle.setsampwidth(4)\n"
            "    handle.setframerate(48000)\n"
            "    handle.writeframes(b'\\0' * 4800 * 2 * 4)\n"
            "while not stop:\n"
            "    time.sleep(0.02)\n"
        )
        fake.chmod(0o755)
        partial = self.output / ".worker.partial.wav"
        final = self.output / "worker.wav"
        capture = {
            "sample_rate_hz": 48_000,
            "sample_format": "s32le",
            "channels": 2,
            "channel_map": "front-left,front-right",
            "maximum_duration_seconds": 1,
            "maximum_file_bytes": 2_000_000,
            "startup_timeout_seconds": 2,
            "stop_grace_seconds": 2,
        }
        plan_identity = {
            "session_type": "voice-recording",
            "capture": capture,
            "process": MODULE.load_catalog("voice-recording")["process"],
            "parecord": {"resolved": {"path": str(fake)}},
        }
        spec = {
            "session_id": "b" * 24,
            "source_name": "fake-source",
            "plan_sha256": MODULE.canonical_sha256(plan_identity),
            "plan_identity": plan_identity,
            "paths": {
                "partial": str(partial),
                "final": str(final),
                "result": str(self.state / "unused.json"),
            },
        }
        spec_path = self.base / "spec.json"
        spec_path.write_text(json.dumps(spec))
        result_path = self.base / "worker-result.json"
        helper = (
            "import importlib.util,json,pathlib,sys;"
            "module_path,spec_path,_fake_path,result_path=sys.argv[1:];"
            "s=importlib.util.spec_from_file_location('recording_worker_integration',module_path);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "payload=json.loads(pathlib.Path(spec_path).read_text());"
            "result=m.worker_run(payload,validate_spec=False);"
            "pathlib.Path(result_path).write_text(json.dumps(result))"
        )
        completed = subprocess.run(
            [
                os.sys.executable,
                "-c",
                helper,
                str(ROOT / "scripts/recording_session.py"),
                str(spec_path),
                str(fake),
                str(result_path),
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(result_path.read_text())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reason"], "maximum-duration")
        self.assertTrue(final.is_file())
        self.assertFalse(partial.exists())
        self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o600)

    def test_start_persists_recoverable_state_before_spawn_failure(self) -> None:
        plan = self.ready_plan()
        source_name = "fake-source"
        self.refresh_source_binding(plan, source_name)
        with (
            mock.patch.object(MODULE, "build_plan", return_value=plan),
            mock.patch.object(
                MODULE.VOICE, "_source_name_from_live_query", return_value=source_name
            ),
            mock.patch.object(
                MODULE.subprocess, "Popen", side_effect=OSError("spawn failed")
            ),
        ):
            with self.assertRaises(MODULE.RecordingError) as context:
                MODULE.start_session(
                    "take-01.wav",
                    60,
                    plan["plan_sha256"],
                    output_root=self.output,
                    state_root=self.state,
                )
        self.assertIn("recover session", str(context.exception))
        active = json.loads((self.state / "active.json").read_text())
        session_id = active["session_id"]
        state_path = MODULE._session_paths(self.state, session_id)["state"]
        stored = json.loads(state_path.read_text())
        self.assertEqual(stored["phase"], "starting")
        self.assertIsNone(stored["process"])
        recovered = MODULE.recover_session(state_root=self.state, session_id=session_id)
        self.assertEqual(recovered["status"], "failed-preserved")
        self.assertFalse((self.state / "active.json").exists())

    def test_start_timeout_uses_exact_termination_and_remains_recoverable(self) -> None:
        plan = self.ready_plan()
        source_name = "fake-source"
        self.refresh_source_binding(plan, source_name)
        plan["identity"]["capture"]["startup_timeout_seconds"] = 1
        plan["plan_sha256"] = MODULE.canonical_sha256(plan["identity"])
        identity = {
            "pid": 43210,
            "start_ticks": 98765,
            "executable": "/usr/bin/python3",
            "cmdline_sha256": "c" * 64,
            "process_group": 43210,
        }

        class FakeProcess:
            pid = 43210

            @staticmethod
            def poll():
                return None

        with (
            mock.patch.object(MODULE, "build_plan", return_value=plan),
            mock.patch.object(
                MODULE.VOICE, "_source_name_from_live_query", return_value=source_name
            ),
            mock.patch.object(MODULE.subprocess, "Popen", return_value=FakeProcess()),
            mock.patch.object(MODULE, "_proc_identity", return_value=identity),
            mock.patch.object(
                MODULE, "_terminate_exact_process", return_value=True
            ) as terminate,
        ):
            with self.assertRaises(MODULE.RecordingError) as context:
                MODULE.start_session(
                    "take-01.wav",
                    60,
                    plan["plan_sha256"],
                    output_root=self.output,
                    state_root=self.state,
                )
        self.assertIn("recover session", str(context.exception))
        terminate.assert_called_once_with(
            identity,
            grace_seconds=float(plan["identity"]["capture"]["stop_grace_seconds"]),
        )
        active = json.loads((self.state / "active.json").read_text())
        session_id = active["session_id"]
        state_path = MODULE._session_paths(self.state, session_id)["state"]
        stored = json.loads(state_path.read_text())
        self.assertEqual(stored["phase"], "running")
        self.assertEqual(stored["process"], identity)
        recovered = MODULE.recover_session(state_root=self.state, session_id=session_id)
        self.assertEqual(recovered["status"], "failed-preserved")
        self.assertFalse((self.state / "active.json").exists())


if __name__ == "__main__":
    unittest.main()
