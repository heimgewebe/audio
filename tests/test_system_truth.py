import copy
import importlib.util
import json
import pathlib
import stat
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "system_truth", ROOT / "scripts/system_truth.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def doctor_results():
    result = MODULE.DOCTOR.CommandResult
    return [
        result(("aplay", "-l"), 0, "Karte 2: M2 [MOTU M2]\nKarte 3: Piano [Roland Digital Piano]\n", ""),
        result(("arecord", "-l"), 0, "Karte 2: M2 [MOTU M2]\nKarte 3: Piano [Roland Digital Piano]\n", ""),
        result(("wpctl", "status"), 0, "MOTU M2\nRoland Digital Piano\n", ""),
        result(
            ("pw-metadata", "-n", "settings", "0"),
            0,
            "key:'clock.force-rate' value:'48000'\nkey:'clock.force-quantum' value:'1024'\n",
            "",
        ),
        result(
            ("pactl", "info"),
            0,
            "Default Sink: alsa_output.usb-MOTU_M2-00\nDefault Source: alsa_input.usb-Roland_Digital_Piano-00\n",
            "",
        ),
        result(
            ("pactl", "list", "short", "sinks"),
            0,
            "1\tmotu\tPipeWire\ts32le 2ch 48000Hz\n",
            "",
        ),
        result(
            ("pactl", "list", "short", "sources"),
            0,
            "2\troland\tPipeWire\ts24le 2ch 44100Hz\n",
            "",
        ),
        result(("systemctl", "is-active", "bluetooth"), 3, "inactive\n", ""),
    ]


def runtime_results():
    result = MODULE.CommandResult
    values = {
        MODULE.READ_ONLY_COMMANDS[0]: "active\nactive\nactive\nactive\ninactive\n",
        MODULE.READ_ONLY_COMMANDS[1]: (
            "Id=pipewire.service\nLoadState=loaded\nActiveState=active\nSubState=running\n"
            "NRestarts=0\nMemoryCurrent=1000\nTasksCurrent=3\nLimitNOFILE=1048576\n\n"
            "Id=mopidy.service\nLoadState=loaded\nActiveState=active\nSubState=running\n"
            "NRestarts=1\nMemoryCurrent=2000\nTasksCurrent=4\nLimitNOFILE=1048576\n"
        ),
        MODULE.READ_ONLY_COMMANDS[2]: (
            "PID PPID STAT ELAPSED %CPU %MEM COMMAND COMMAND\n"
            "42 1 S 120 1.0 0.2 mopidy /usr/bin/mopidy --config ~/.config/mopidy/mopidy.conf\n"
        ),
        MODULE.READ_ONLY_COMMANDS[3]: (
            "Filesystem 1B-blocks Used Available Use% Mounted on\n"
            "/dev/test 1000000 250000 750000 25% /\n"
        ),
        MODULE.READ_ONLY_COMMANDS[4]: "Archived and active journals take up 10.0M in the file system.\n",
        MODULE.READ_ONLY_COMMANDS[5]: "PipeWire graph stable\n",
        MODULE.READ_ONLY_COMMANDS[6]: "6.15.0-test\n",
        MODULE.READ_ONLY_COMMANDS[7]: "pipewire\nCompiled with libpipewire 1.4.7\n",
        MODULE.READ_ONLY_COMMANDS[8]: "wireplumber 0.5.10\n",
        MODULE.READ_ONLY_COMMANDS[9]: "Mopidy 3.4.2\n",
    }
    return [result(command, 0, values.get(command, ""), "") for command in MODULE.READ_ONLY_COMMANDS]


class SystemTruthTests(unittest.TestCase):
    def report(self):
        with tempfile.TemporaryDirectory() as directory:
            physical = pathlib.Path(directory) / "physical.json"
            return MODULE.build_report(
                doctor_results(),
                runtime_results(),
                physical_state=physical,
                generated_at="2026-07-28T00:00:00+00:00",
            )

    def test_commands_are_read_only(self):
        MODULE.assert_read_only_commands()
        with self.assertRaisesRegex(RuntimeError, "mutation-capable"):
            MODULE.assert_read_only_commands((("systemctl", "--user", "restart", "pipewire"),))

    def test_truth_chain_is_stable_across_timestamps(self):
        first = self.report()
        second = self.report()
        self.assertEqual(first["truth_chain_sha256"], second["truth_chain_sha256"])
        self.assertEqual(first["runtime"]["graph_fingerprint"], second["runtime"]["graph_fingerprint"])

    def test_report_preserves_unresolved_physical_and_measurement_gates(self):
        report = self.report()
        self.assertEqual(report["physical"]["resolved_count"], 0)
        self.assertFalse(report["physical"]["complete"])
        self.assertEqual(report["gates"]["single-truth-model"]["status"], "pass")
        self.assertEqual(report["gates"]["host-read-boundary"]["status"], "pass")
        self.assertEqual(report["gates"]["safe-listening-calibration"]["status"], "blocked")
        self.assertEqual(report["gates"]["voice-reference"]["status"], "blocked")
        self.assertEqual(report["gates"]["latency-xrun-baseline"]["status"], "measurement-required")
        self.assertEqual(report["gates"]["device-loss-baseline"]["status"], "exercise-required")
        self.assertIsNone(report["doctor"]["graph"]["round_trip_latency_ms"])

    def test_report_integrity_rejects_tampering(self):
        report = self.report()
        MODULE.verify_report(report)
        report["doctor"]["graph"]["force_rate_hz"] = 96000
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            MODULE.verify_report(report)

    def test_truth_chain_tampering_is_rejected(self):
        report = self.report()
        report["truth_chain_sha256"] = "0" * 64
        report["report_sha256"] = MODULE.sha256_json(MODULE.report_digest_core(report))
        with self.assertRaisesRegex(ValueError, "truth chain digest mismatch"):
            MODULE.verify_report(report)

    def test_drift_requests_remeasurement(self):
        before = self.report()
        after = copy.deepcopy(before)
        after["doctor"]["graph"]["force_rate_hz"] = 96000
        after["runtime"]["graph_fingerprint"] = MODULE.graph_fingerprint(after["doctor"])
        after["truth_chain_sha256"] = MODULE.compute_truth_chain(
            after["contracts"], after["runtime"], after["physical"]
        )
        after["report_sha256"] = MODULE.sha256_json(MODULE.report_digest_core(after))
        drift = MODULE.build_drift_report(before, after)
        self.assertTrue(drift["changed"])
        self.assertTrue(drift["material"])
        self.assertIn("loopback-latency", drift["required_remeasurements"])
        self.assertIn("xrun-stability", drift["required_remeasurements"])

    def test_no_drift_is_empty(self):
        report = self.report()
        drift = MODULE.build_drift_report(report, copy.deepcopy(report))
        self.assertFalse(drift["changed"])
        self.assertFalse(drift["material"])
        self.assertEqual(drift["changes"], [])

    def test_private_atomic_output_has_mode_0600(self):
        report = self.report()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "truth.json"
            MODULE.atomic_write_private(path, report)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            loaded = json.loads(path.read_text())
            MODULE.verify_report(loaded)

    def test_process_projection_is_classified_and_pid_independent(self):
        processes = MODULE.parse_processes(runtime_results()[2])
        self.assertEqual(processes[0]["classification"], "playback")
        first = MODULE.process_fingerprint(processes)
        changed = copy.deepcopy(processes)
        changed[0]["pid"] = 99999
        changed[0]["elapsed_seconds"] = 99999
        self.assertEqual(first, MODULE.process_fingerprint(changed))

    def test_prompt_text_cannot_fake_plugin_host(self):
        fake = MODULE.CommandResult(
            MODULE.READ_ONLY_COMMANDS[2],
            0,
            "PID PPID STAT ELAPSED %CPU %MEM COMMAND COMMAND\n"
            "77 1 S 10 1.0 0.1 codex codex exec review FluidSynth and sfizz safely\n",
            "",
        )
        self.assertEqual(MODULE.parse_processes(fake), [])

    def test_contract_projection_binds_every_required_component(self):
        projection = MODULE.contract_projection()
        self.assertEqual(set(projection["bindings"]), set(MODULE.CONTRACT_PATHS))
        self.assertRegex(projection["aggregate_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
