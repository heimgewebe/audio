import array
import importlib.util
import json
import math
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audio_level_observer_under_test", ROOT / "scripts" / "audio_level_observer.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)
VOICE_SPEC = importlib.util.spec_from_file_location(
    "voice_capture_observer_for_level_observer_test", ROOT / "scripts" / "voice_capture_observer.py"
)
VOICE_MODULE = importlib.util.module_from_spec(VOICE_SPEC)
assert VOICE_SPEC and VOICE_SPEC.loader
VOICE_SPEC.loader.exec_module(VOICE_MODULE)


def motu_source(*, name="alsa_input.usb-MOTU_M2_SERIAL-00.analog-stereo"):
    return {
        "name": name,
        "monitor_source": "",
        "sample_specification": "s32le 2ch 48000Hz",
        "mute": False,
        "volume": {
            "front-left": {"value": 65536},
            "front-right": {"value": 65536},
        },
        "properties": {
            "device.class": "sound",
            "media.class": "Audio/Source",
            "device.vendor.id": "07fd",
            "device.product.id": "0008",
            "device.serial": "MOTU_M2_SERIAL",
            "device.bus_path": "pci-test-usb-0:1",
        },
    }


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
        binding = MODULE._motu_source_binding(motu_source())
        first = MODULE.observation_payload(
            measurement, sequence=1, observed_at=10.0, source_binding=binding
        )
        second = MODULE.observation_payload(
            measurement, sequence=2, observed_at=11.0, source_binding=binding
        )
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
            self.assertEqual(decoded["observer_mode"], "active-recorder-source-capture")
            self.assertEqual(decoded["source_selection"], "recorder-bound-motu-source")
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

    def test_capture_command_is_native_pipewire_capture_on_exact_recorder_source(self):
        binding = MODULE._motu_source_binding(motu_source())
        command = MODULE.build_pw_cat_command(binding["target"])

        self.assertEqual(command[0], "/usr/bin/pw-cat")
        self.assertIn("--record", command)
        self.assertEqual(command[command.index("--target") + 1], binding["target"])
        self.assertNotEqual(binding["target"], "auto")
        self.assertEqual(command[command.index("--rate") + 1], "48000")
        self.assertEqual(command[command.index("--channels") + 1], "2")
        self.assertEqual(command[command.index("--format") + 1], "f32")
        self.assertNotIn("arecord", " ".join(command))
        properties = json.loads(command[command.index("--properties") + 1])
        self.assertEqual(properties["node.name"], MODULE.OBSERVER_ID)

    def test_missing_source_is_waited_for_inside_service_instead_of_failing_systemd(self):
        absent = MODULE.SourceUnavailable("missing")
        sentinel = MODULE.ObserverError("sentinel-stop-after-retry")
        with (
            mock.patch.object(MODULE, "PW_CAT", pathlib.Path("/usr/bin/true")),
            mock.patch.object(MODULE, "remove_output"),
            mock.patch.object(MODULE.signal, "signal", return_value=MODULE.signal.SIG_DFL),
            mock.patch.object(
                MODULE,
                "resolve_recorder_source",
                side_effect=[absent, sentinel],
            ) as resolve,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(MODULE.ObserverError, "sentinel-stop-after-retry"):
                MODULE.run_observer(pathlib.Path("/tmp/not-written-level-test.json"))
        self.assertEqual(resolve.call_count, 2)
        sleep.assert_called_once_with(MODULE.SOURCE_RETRY_SECONDS)

    def test_source_binding_matches_recorder_identity_hash_and_rejects_ambiguous_or_wrong_source(self):
        binding = MODULE._motu_source_binding(motu_source())
        self.assertIsNotNone(binding)
        self.assertRegex(binding["source_identity_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(binding["channel_map"], "front-left,front-right")
        recorder_identity = VOICE_MODULE._source_identity(motu_source())
        self.assertIsNotNone(recorder_identity)
        self.assertEqual(
            binding["source_identity_sha256"],
            VOICE_MODULE.LAB.canonical_value_sha256(recorder_identity),
        )

        wrong = motu_source()
        wrong["properties"]["device.product.id"] = "ffff"
        self.assertIsNone(MODULE._motu_source_binding(wrong))

        completed = mock.Mock(returncode=0, stdout=json.dumps([motu_source()]).encode(), stderr=b"")
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed):
            resolved = MODULE.resolve_recorder_source()
        self.assertEqual(resolved, binding)

        completed.stdout = json.dumps([]).encode()
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed):
            with self.assertRaises(MODULE.SourceUnavailable):
                MODULE.resolve_recorder_source()

        completed.stdout = json.dumps([motu_source(), motu_source()]).encode()
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(MODULE.ObserverError, "ambiguous"):
                MODULE.resolve_recorder_source()

        with mock.patch.object(MODULE, "resolve_recorder_source", return_value=binding):
            self.assertTrue(MODULE.recorder_source_binding_is_current(binding))
            self.assertFalse(
                MODULE.recorder_source_binding_is_current(
                    {**binding, "source_identity_sha256": "f" * 64}
                )
            )



    def test_active_source_drift_removes_published_evidence_before_rebind(self):
        binding = MODULE._motu_source_binding(motu_source())
        measurement = MODULE.analyze_f32le(f32le([0.5, -0.5, 0.0, 0.0]))
        muted = motu_source()
        muted["mute"] = True
        non_unity = motu_source()
        non_unity["volume"]["front-left"]["value"] = 60000
        changed_identity = motu_source()
        changed_identity["properties"]["device.bus_path"] = "pci-test-usb-0:2"
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "levels.json"
            for label, drifted in (("mute", muted), ("unity", non_unity), ("hash", changed_identity)):
                with self.subTest(label=label):
                    MODULE.atomic_write_observation(
                        output,
                        MODULE.observation_payload(
                            measurement, sequence=1, observed_at=10.0, source_binding=binding
                        ),
                    )
                    completed = mock.Mock(
                        returncode=0, stdout=json.dumps([drifted]).encode(), stderr=b""
                    )
                    with mock.patch.object(MODULE.subprocess, "run", return_value=completed):
                        self.assertFalse(MODULE.revalidate_active_source(output, binding))
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
