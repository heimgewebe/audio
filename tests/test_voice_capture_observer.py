import copy
import datetime as dt
import importlib.util
import json
import math
import pathlib
import stat
import struct
import sys
import tempfile
import types
import unittest
import wave
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "voice_capture_observer_test_module",
    ROOT / "scripts/voice_capture_observer.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class VoiceCaptureObserverTests(unittest.TestCase):
    SOURCE_NAME = "alsa_input.usb-MOTU_M2_M20000062566-00.Direct__hw_M2__source"

    def physical_state(self, root):
        path = pathlib.Path(root) / "physical.json"
        MODULE.LAB.PHYSICAL.atomic_write_private(
            path, MODULE.LAB.PHYSICAL.empty_state()
        )
        return path

    def source_payload(
        self,
        *,
        serial="MOTU_M2_M20000062566",
        name=None,
        muted=False,
        volume=65536,
    ):
        return {
            "index": 9142,
            "name": name or self.SOURCE_NAME,
            "monitor_source": "",
            "mute": muted,
            "sample_specification": "s32le 2ch 48000Hz",
            "volume": {
                "front-left": {"value": volume},
                "front-right": {"value": volume},
            },
            "properties": {
                "device.class": "sound",
                "media.class": "Audio/Source",
                "device.vendor.id": "0x07fd",
                "device.product.id": "0x0008",
                "device.serial": serial,
                "device.bus_path": "pci-0000:02:00.0-usb-0:9.2:1.0",
            },
        }

    def command_result(self, sources):
        raw = json.dumps(sources)
        return types.SimpleNamespace(
            argv=MODULE.LAB.VOICE_PACTL_SOURCES_ARGV,
            error=None,
            returncode=0,
            stdout=raw,
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_sha256=MODULE.sha256_text(raw),
            stderr_sha256=MODULE.sha256_bytes(b""),
            stdout_total_bytes=len(raw.encode()),
        )

    def snapshot(
        self,
        observed_at="2026-07-30T16:00:00+00:00",
        *,
        serial="MOTU_M2_M20000062566",
        name=None,
        muted=False,
        volume=65536,
    ):
        source = self.source_payload(
            serial=serial, name=name, muted=muted, volume=volume
        )
        identity = MODULE._source_identity(source)
        argv = list(MODULE.LAB.VOICE_PACTL_SOURCES_ARGV)
        value = {
            "schema_version": 1,
            "kind": "audio_voice_source_snapshot",
            "observed_at": observed_at,
            "complete": True,
            "present": True,
            "match_count": 1,
            "ambiguous": False,
            "errors": [],
            "identity": identity,
            "query": {
                "argv": argv,
                "argv_sha256": MODULE.LAB.canonical_value_sha256(argv),
                "returncode": 0,
                "complete": True,
                "stdout_sha256": "a" * 64,
                "stdout_total_bytes": 100,
                "stderr_sha256": "b" * 64,
            },
        }
        value["observation_sha256"] = MODULE.LAB.canonical_value_sha256(value)
        return value

    def write_voice_wave(self, path, seconds=8, peak_dbfs=-9.0):
        amplitude = int((1 << 31) * math.pow(10.0, peak_dbfs / 20.0))
        frame = struct.pack("<ii", amplitude, 0)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(4)
            handle.setframerate(48000)
            for _ in range(seconds):
                handle.writeframes(frame * 48000)

    def process_result(self, output, duration=8):
        self.write_voice_wave(output, duration)
        return {
            "method": MODULE.LAB.VOICE_CAPTURE_METHOD,
            "requested_duration_seconds": duration,
            "capture_started_at": "2026-07-30T16:00:00+00:00",
            "capture_ended_at": "2026-07-30T16:00:09.100000+00:00",
            "duration_seconds": 9.1,
            "stream_ready": True,
            "stream_ready_at": "2026-07-30T16:00:01+00:00",
            "startup_seconds": 1.0,
            "command": MODULE.capture_command_contract(self.SOURCE_NAME),
            "returncode": 0,
            "accepted_returncodes": [0, -2],
            "forced_kill": False,
            "stderr_bytes": 0,
            "stderr_sha256": MODULE.sha256_bytes(b""),
            "stderr_truncated": False,
            "complete": True,
        }

    def capture(self, root, *, after=None, duration=8):
        root = pathlib.Path(root)
        physical = self.physical_state(root)
        wav_output = root / "voice.wav"
        before = self.snapshot()
        after = after or self.snapshot("2026-07-30T16:00:09.200000+00:00")

        def run_capture(source_name, output, requested):
            self.assertEqual(source_name, self.SOURCE_NAME)
            self.assertEqual(requested, duration)
            return self.process_result(output, duration)

        with (
            mock.patch.object(MODULE, "source_snapshot", side_effect=[before, after]),
            mock.patch.object(
                MODULE, "_source_name_from_live_query", return_value=self.SOURCE_NAME
            ),
            mock.patch.object(MODULE, "_run_parecord", side_effect=run_capture),
        ):
            evidence = MODULE.capture_voice_evidence(duration, physical, wav_output)
        return evidence, wav_output

    def test_live_source_snapshot_is_serial_bound_and_unity(self):
        result = self.command_result([self.source_payload()])
        with mock.patch.object(MODULE.SYSTEM_TRUTH, "run_read_only", return_value=result):
            snapshot = MODULE.source_snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["identity"]["vendor_id"], "07fd")
        self.assertEqual(snapshot["identity"]["product_id"], "0008")
        self.assertTrue(snapshot["identity"]["unity_volume"])
        self.assertFalse(snapshot["identity"]["muted"])
        self.assertNotIn("M20000062566", json.dumps(snapshot))

    def test_missing_motu_serial_fails_closed(self):
        result = self.command_result([self.source_payload(serial=None)])
        with mock.patch.object(MODULE.SYSTEM_TRUTH, "run_read_only", return_value=result):
            snapshot = MODULE.source_snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertIn("MOTU source lacks its serial-bound identity", snapshot["errors"])

    def test_serial_and_node_name_must_match(self):
        result = self.command_result(
            [
                self.source_payload(
                    serial="MOTU_M2_M20000062566",
                    name="alsa_input.usb-MOTU_M2_OTHER-00.Direct__hw_M2__source",
                )
            ]
        )
        with mock.patch.object(MODULE.SYSTEM_TRUTH, "run_read_only", return_value=result):
            snapshot = MODULE.source_snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertIn(
            "MOTU source node does not match its serial identity",
            snapshot["errors"],
        )

    def test_muted_or_nonunity_source_never_starts_capture(self):
        for snapshot, blocker in (
            (self.snapshot(muted=True), "motu-source-muted"),
            (self.snapshot(volume=32768), "motu-source-volume-not-unity"),
        ):
            with tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                physical = self.physical_state(root)
                with (
                    mock.patch.object(MODULE, "source_snapshot", return_value=snapshot),
                    mock.patch.object(MODULE, "_run_parecord") as capture,
                ):
                    evidence = MODULE.capture_voice_evidence(
                        8, physical, root / "voice.wav"
                    )
                capture.assert_not_called()
                self.assertIn(blocker, evidence["blockers"])
                self.assertEqual(evidence["result"], "fail")

    def test_source_query_race_is_structured_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            physical = self.physical_state(root)
            before = self.snapshot()
            with (
                mock.patch.object(MODULE, "source_snapshot", return_value=before),
                mock.patch.object(
                    MODULE,
                    "_source_name_from_live_query",
                    side_effect=ValueError("race"),
                ),
                mock.patch.object(MODULE, "_run_parecord") as capture,
            ):
                evidence = MODULE.capture_voice_evidence(
                    8, physical, root / "voice.wav"
                )
            capture.assert_not_called()
            self.assertIn("motu-source-query-race", evidence["blockers"])
            self.assertEqual(evidence["result"], "fail")

    def test_bound_capture_passes_and_writes_private_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence, wav_output = self.capture(directory)
            self.assertEqual(evidence["result"], "pass")
            self.assertEqual(evidence["blockers"], [])
            self.assertEqual(stat.S_IMODE(wav_output.stat().st_mode), 0o600)
            self.assertEqual(evidence["analysis"]["maximum_peak_dbfs"], -9.0)
            MODULE.LAB.validate_evidence("voice-level-measurement", evidence)

    def test_source_identity_change_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            changed = self.snapshot(
                "2026-07-30T16:00:09.200000+00:00",
                serial="MOTU_M2_OTHER",
                name="alsa_input.usb-MOTU_M2_OTHER-00.Direct__hw_M2__source",
            )
            evidence, _ = self.capture(directory, after=changed)
            self.assertEqual(evidence["result"], "fail")
            self.assertIn("motu-source-identity-changed", evidence["blockers"])

    def test_short_capture_is_structured_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence, _ = self.capture(directory, duration=1)
            self.assertEqual(evidence["result"], "fail")
            self.assertIn("voice-capture-too-short", evidence["blockers"])

    def test_legacy_offline_voice_evidence_is_readable_but_unresolvable(self):
        legacy = {
            "schema_version": 1,
            "kind": "audio_level_measurement_evidence",
            "gate": "voice-level-measurement",
            "result": "pass",
            "measured_at": "2026-07-30T16:00:00+00:00",
            "physical_state_sha256": "a" * 64,
            "source_wav": {"sha256": "b" * 64, "bytes": 100},
            "analysis": {
                "kind": "audio_level_analysis",
                "sample_rate_hz": 48000,
                "maximum_peak_dbfs": -9.0,
                "channels_analysis": [{"channel": 1, "clipped_samples": 0}],
            },
        }
        MODULE.LAB.validate_evidence(
            "voice-level-measurement", legacy, allow_legacy_voice=True
        )
        with self.assertRaisesRegex(ValueError, "legacy voice evidence"):
            MODULE.LAB.validate_evidence("voice-level-measurement", legacy)
        state = MODULE.LAB.empty_state()
        state["gates"]["voice-level-measurement"] = {
            "status": "passed",
            "recorded_at": "2026-07-30T16:01:00+00:00",
            "evidence_sha256": MODULE.LAB.canonical_sha256(legacy),
            "physical_state_sha256": "a" * 64,
            "evidence": legacy,
        }
        _, invalidated = MODULE.LAB.gate_resolution(
            state, pathlib.Path("/does/not/matter")
        )
        self.assertEqual(
            invalidated["voice-level-measurement"],
            "legacy-unbound-voice-evidence",
        )

    def test_tampered_implementation_or_command_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence, _ = self.capture(directory)
            tampered = copy.deepcopy(evidence)
            tampered["implementation"]["level_analyzer_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "implementation binding"):
                MODULE.LAB.validate_evidence("voice-level-measurement", tampered)
            tampered = copy.deepcopy(evidence)
            tampered["capture_observation"]["process"]["command"][
                "device_name_sha256"
            ] = "0" * 64
            with self.assertRaisesRegex(ValueError, "another source"):
                MODULE.LAB.validate_evidence("voice-level-measurement", tampered)

    def test_private_wav_output_rejects_symlink_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source.wav"
            source.write_bytes(b"wave")
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaises(OSError):
                MODULE._copy_private_binary(source, alias / "voice.wav")


if __name__ == "__main__":
    unittest.main()
