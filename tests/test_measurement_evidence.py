import datetime as dt
import importlib.util
import json
import pathlib
import struct
import tempfile
import unittest
import wave
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "measurement_evidence", ROOT / "scripts/measurement_evidence.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class MeasurementEvidenceTests(unittest.TestCase):
    def physical_state(self, root):
        path = pathlib.Path(root) / "physical.json"
        MODULE.LAB.PHYSICAL.atomic_write_private(
            path, MODULE.LAB.PHYSICAL.empty_state()
        )
        return path

    def write_wave(self, path, samples, rate=48000):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(
                b"".join(struct.pack("<h", sample) for sample in samples)
            )

    def truth_report(
        self,
        *,
        graph_fingerprint="c" * 64,
        rate_hz=48000,
        quantum_frames=1024,
        report_sha256="a" * 64,
        truth_chain_sha256="b" * 64,
    ):
        return {
            "report_sha256": report_sha256,
            "truth_chain_sha256": truth_chain_sha256,
            "runtime": {"graph_fingerprint": graph_fingerprint},
            "doctor": {
                "graph": {
                    "force_rate_hz": rate_hz,
                    "force_quantum_frames": quantum_frames,
                }
            },
        }

    def test_voice_evidence_pass_and_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            physical = self.physical_state(root)
            good = root / "good.wav"
            low = root / "low.wav"
            self.write_wave(good, [0, 10000, -10000, 0] * 100)
            self.write_wave(low, [0, 1000, -1000, 0] * 100)
            passed = MODULE.voice_level_evidence(good, physical)
            failed = MODULE.voice_level_evidence(low, physical)
            self.assertEqual(passed["result"], "pass")
            self.assertEqual(failed["result"], "fail")
            MODULE.LAB.validate_evidence("voice-level-measurement", passed)

    def test_loopback_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            physical = self.physical_state(root)
            reference = root / "reference.wav"
            recorded = root / "recorded.wav"
            ref = [0] * 2000
            ref[10] = 12000
            rec = [0] * 2500
            rec[490] = 10000
            self.write_wave(reference, ref)
            self.write_wave(recorded, rec)
            evidence = MODULE.loopback_latency_evidence(
                reference, recorded, physical, 100.0, 128, "d" * 64
            )
            self.assertEqual(evidence["result"], "pass")
            self.assertEqual(
                evidence["analysis"]["round_trip_latency_ms"], 10.0
            )
            self.assertEqual(evidence["quantum_frames"], 128)
            self.assertEqual(evidence["graph_fingerprint"], "d" * 64)
            MODULE.LAB.validate_evidence(
                "loopback-latency-measurement", evidence
            )

    def test_policy_decision_is_validated(self):
        evidence = MODULE.policy_decision_evidence(
            "resampling-decision",
            "graph-48k",
            "Der gemeinsame Graph bleibt für gemischte Sitzungen auf 48 kHz.",
        )
        self.assertEqual(evidence["result"], "pass")
        MODULE.LAB.validate_evidence("resampling-decision", evidence)

    def test_xrun_observation_is_bound_to_graph_and_journal(self):
        started = dt.datetime(2026, 7, 30, 8, 0, tzinfo=dt.timezone.utc)
        ended = started + dt.timedelta(seconds=60)
        argv = MODULE.LAB.xrun_journal_argv(started.isoformat(), ended.isoformat())
        command_result = MODULE.SYSTEM_TRUTH.CommandResult(
            argv=argv,
            returncode=0,
            stdout="quiet\n",
            stderr="",
            duration_ms=1,
            stdout_total_bytes=6,
            stderr_total_bytes=0,
            stdout_sha256="f" * 64,
            stderr_sha256="e" * 64,
        )
        before = self.truth_report()
        after = self.truth_report(
            report_sha256="d" * 64, truth_chain_sha256="e" * 64
        )
        with (
            mock.patch.object(
                MODULE.SYSTEM_TRUTH, "build_report", side_effect=[before, after]
            ),
            mock.patch.object(MODULE.SYSTEM_TRUTH, "verify_report"),
            mock.patch.object(MODULE, "utc_now", side_effect=[started, ended]),
            mock.patch.object(MODULE, "monotonic_now", side_effect=[10.0, 70.0]),
            mock.patch.object(MODULE, "sleep_for") as sleep,
            mock.patch.object(
                MODULE.SYSTEM_TRUTH, "run_read_only", return_value=command_result
            ),
        ):
            evidence = MODULE.xrun_observation_evidence(
                60, 48000, 1024, "c" * 64
            )
        sleep.assert_called_once_with(60)
        self.assertEqual(evidence["result"], "pass")
        self.assertEqual(evidence["journal"]["query_argv"], list(argv))
        self.assertEqual(evidence["graph_before"]["report_sha256"], "a" * 64)
        self.assertEqual(evidence["graph_after"]["report_sha256"], "d" * 64)
        MODULE.LAB.validate_evidence("xrun-stability-test", evidence)

    def test_xrun_observation_rejects_graph_drift(self):
        started = dt.datetime(2026, 7, 30, 8, 0, tzinfo=dt.timezone.utc)
        ended = started + dt.timedelta(seconds=60)
        before = self.truth_report()
        after = self.truth_report(
            graph_fingerprint="d" * 64,
            report_sha256="d" * 64,
            truth_chain_sha256="e" * 64,
        )
        with (
            mock.patch.object(
                MODULE.SYSTEM_TRUTH, "build_report", side_effect=[before, after]
            ),
            mock.patch.object(MODULE.SYSTEM_TRUTH, "verify_report"),
            mock.patch.object(MODULE, "utc_now", side_effect=[started, ended]),
            mock.patch.object(MODULE, "monotonic_now", side_effect=[10.0, 70.0]),
            mock.patch.object(MODULE, "sleep_for"),
        ):
            with self.assertRaisesRegex(ValueError, "expected graph"):
                MODULE.xrun_observation_evidence(60, 48000, 1024, "c" * 64)

    def test_emit_evidence_writes_private_hash_bound_output(self):
        payload = {
            "schema_version": 1,
            "kind": "pipewire_xrun_observation",
            "result": "pass",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "evidence.json"
            with mock.patch("builtins.print"):
                receipt = MODULE.emit_evidence(payload, output)
            self.assertEqual(json.loads(output.read_text()), payload)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                receipt["evidence_sha256"], MODULE.LAB.canonical_sha256(payload)
            )
            self.assertEqual(receipt["output_basename"], "evidence.json")

    def test_rejects_symlink_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "target.wav"
            target.write_bytes(b"x")
            link = root / "link.wav"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                MODULE.file_binding(link)

    def test_voice_analysis_and_hash_use_same_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            physical = self.physical_state(root)
            source = root / "voice.wav"
            self.write_wave(source, [0, 10000, -10000, 0] * 100)
            original_bytes = source.read_bytes()
            original_analyze = MODULE.LEVEL.analyze

            def mutate_original(snapshot):
                source.write_bytes(b"changed-after-snapshot")
                return original_analyze(snapshot)

            with mock.patch.object(MODULE.LEVEL, "analyze", side_effect=mutate_original):
                evidence = MODULE.voice_level_evidence(source, physical)
            import hashlib

            self.assertEqual(
                evidence["source_wav"]["sha256"],
                hashlib.sha256(original_bytes).hexdigest(),
            )
            self.assertNotEqual(
                evidence["source_wav"]["sha256"],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )

    def test_identical_loopback_sources_do_not_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            physical = self.physical_state(root)
            source = root / "same.wav"
            samples = [0] * 2000
            samples[10] = 12000
            self.write_wave(source, samples)
            evidence = MODULE.loopback_latency_evidence(
                source, source, physical, 100.0, 128, "d" * 64
            )
            self.assertEqual(evidence["result"], "fail")
            with self.assertRaises(ValueError):
                MODULE.LAB.validate_evidence(
                    "loopback-latency-measurement", evidence
                )


if __name__ == "__main__":
    unittest.main()
