import array
import importlib.util
import json
import math
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audio_level_observer_under_test", ROOT / "scripts" / "audio_level_observer.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def f32le(values):
    samples = array.array("f", values)
    if MODULE.sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class AudioLevelObserverTests(unittest.TestCase):
    def test_linear_to_dbfs_has_an_explicit_silence_floor_and_full_scale_ceiling(self):
        self.assertEqual(MODULE.linear_to_dbfs(0.0), -160.0)
        self.assertEqual(MODULE.linear_to_dbfs(0.5), -6.021)
        self.assertEqual(MODULE.linear_to_dbfs(1.0), 0.0)
        self.assertEqual(MODULE.linear_to_dbfs(2.0), 0.0)
        for invalid in (-0.1, math.inf, math.nan):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                MODULE.linear_to_dbfs(invalid)

    def test_stereo_peak_and_rms_are_calculated_from_real_interleaved_samples(self):
        result = MODULE.analyze_f32le(f32le([1.0, 0.0, 0.0, 0.0]))

        self.assertEqual(result["frames"], 2)
        self.assertEqual(result["peak_dbfs"], 0.0)
        self.assertEqual(result["rms_dbfs"], -6.021)
        self.assertTrue(result["clipping"])
        self.assertEqual(result["clipped_samples"], 1)
        self.assertEqual(result["channels_analysis"][0]["channel"], "FL")
        self.assertEqual(result["channels_analysis"][0]["rms_dbfs"], -3.01)
        self.assertEqual(result["channels_analysis"][1]["channel"], "FR")
        self.assertEqual(result["channels_analysis"][1]["rms_dbfs"], -160.0)

    def test_pcm_validation_rejects_partial_empty_and_non_finite_frames(self):
        for payload in (b"", b"\0" * 4, f32le([math.nan, 0.0])):
            with self.subTest(size=len(payload)), self.assertRaises(ValueError):
                MODULE.analyze_f32le(payload)

    def test_atomic_json_output_is_private_bounded_and_replaceable(self):
        measurement = MODULE.analyze_f32le(f32le([0.5, -0.5, 0.0, 0.0]))
        first = MODULE.observation_payload(measurement, sequence=1, observed_at=10.0)
        second = MODULE.observation_payload(measurement, sequence=2, observed_at=11.0)
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "levels.json"
            MODULE.atomic_write_observation(output, first)
            MODULE.atomic_write_observation(output, second)

            payload = output.read_bytes()
            decoded = json.loads(payload)
            self.assertLessEqual(len(payload), MODULE.MAX_JSON_BYTES)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(decoded["sequence"], 2)
            self.assertEqual(decoded["peak_dbfs"], -6.021)
            self.assertEqual(decoded["observer_mode"], "active-pipewire-shared-capture")
            self.assertEqual(decoded["source_selection"], "pipewire-default-source")
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [output])

    def test_json_output_rejects_non_finite_and_untrusted_targets(self):
        with self.assertRaises(MODULE.ObserverError):
            MODULE.encode_observation({"peak_dbfs": math.nan})
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            output = root / "levels.json"
            output.symlink_to(target)
            with self.assertRaisesRegex(MODULE.ObserverError, "regular file"):
                MODULE.atomic_write_observation(output, {"peak_dbfs": -6.0})
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")

    def test_capture_command_is_native_pipewire_shared_capture_on_auto_target(self):
        command = MODULE.build_pw_cat_command()

        self.assertEqual(command[0], "/usr/bin/pw-cat")
        self.assertIn("--record", command)
        self.assertEqual(command[command.index("--target") + 1], "auto")
        self.assertEqual(command[command.index("--rate") + 1], "48000")
        self.assertEqual(command[command.index("--channels") + 1], "2")
        self.assertEqual(command[command.index("--format") + 1], "f32")
        self.assertNotIn("arecord", " ".join(command))
        properties = json.loads(command[command.index("--properties") + 1])
        self.assertEqual(properties["node.name"], MODULE.OBSERVER_ID)


if __name__ == "__main__":
    unittest.main()
