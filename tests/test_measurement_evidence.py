import importlib.util
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


if __name__ == "__main__":
    unittest.main()
