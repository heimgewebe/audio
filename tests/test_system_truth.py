import copy
import importlib.util
import pathlib
import stat
import sys
import tempfile
import time
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
        result(
            ("aplay", "-l"),
            0,
            "Karte 2: M2 [MOTU M2]\nKarte 3: Piano [Roland Digital Piano]\n",
            "",
        ),
        result(
            ("arecord", "-l"),
            0,
            "Karte 2: M2 [MOTU M2]\nKarte 3: Piano [Roland Digital Piano]\n",
            "",
        ),
        result(("wpctl", "status"), 0, "MOTU M2\nRoland Digital Piano\n", ""),
        result(
            ("pw-metadata", "-n", "settings", "0"),
            0,
            "key:'clock.force-rate' value:'48000'\n"
            "key:'clock.force-quantum' value:'1024'\n",
            "",
        ),
        result(
            ("pactl", "info"),
            0,
            "Default Sink: alsa_output.usb-MOTU_M2-00\n"
            "Default Source: alsa_input.usb-Roland_Digital_Piano-00\n",
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
            "Id=pipewire.service\nLoadState=loaded\nActiveState=active\n"
            "SubState=running\nNRestarts=0\nMemoryCurrent=1000\n"
            "TasksCurrent=3\nLimitNOFILE=1048576\n\n"
            "Id=pipewire-pulse.service\nLoadState=loaded\nActiveState=active\n"
            "SubState=running\nNRestarts=0\nMemoryCurrent=1100\n"
            "TasksCurrent=3\nLimitNOFILE=1048576\n\n"
            "Id=wireplumber.service\nLoadState=loaded\nActiveState=active\n"
            "SubState=running\nNRestarts=0\nMemoryCurrent=1200\n"
            "TasksCurrent=5\nLimitNOFILE=1048576\n\n"
            "Id=mopidy.service\nLoadState=loaded\nActiveState=active\n"
            "SubState=running\nNRestarts=1\nMemoryCurrent=2000\n"
            "TasksCurrent=4\nLimitNOFILE=1048576\n"
        ),
        MODULE.READ_ONLY_COMMANDS[2]: (
            "PID PPID STAT ELAPSED %CPU %MEM COMMAND COMMAND\n"
            "42 1 S 120 1.0 0.2 mopidy /usr/bin/mopidy --config "
            "~/.config/mopidy/mopidy.conf\n"
        ),
        MODULE.READ_ONLY_COMMANDS[3]: (
            "Filesystem 1B-blocks Used Available Use% Mounted on\n"
            "/dev/test 1000000 250000 750000 25% /\n"
        ),
        MODULE.READ_ONLY_COMMANDS[4]: (
            "Archived and active journals take up 10.0M in the file system.\n"
        ),
        MODULE.READ_ONLY_COMMANDS[5]: "PipeWire graph stable\n",
        MODULE.READ_ONLY_COMMANDS[6]: "6.15.0-test\n",
        MODULE.READ_ONLY_COMMANDS[7]: (
            "pipewire\nCompiled with libpipewire 1.4.7\n"
        ),
        MODULE.READ_ONLY_COMMANDS[8]: "wireplumber 0.5.10\n",
        MODULE.READ_ONLY_COMMANDS[9]: "Mopidy 3.4.2\n",
    }
    return [
        result(
            command,
            0,
            values.get(command, ""),
            "",
            stdout_total_bytes=len(values.get(command, "").encode()),
            stdout_sha256=MODULE.sha256_bytes(values.get(command, "").encode()),
            stderr_sha256=MODULE.sha256_bytes(b""),
        )
        for command in MODULE.READ_ONLY_COMMANDS
    ]


def recompute(report):
    report["truth_chain_sha256"] = MODULE.compute_truth_chain(
        report["contracts"],
        report["runtime"],
        report["physical"],
        report["laboratory"],
        report["playback"],
    )
    report["report_sha256"] = MODULE.sha256_json(MODULE.report_digest_core(report))


class SystemTruthTests(unittest.TestCase):
    def report(self, generated_at="2026-07-28T00:00:00+00:00"):
        with tempfile.TemporaryDirectory() as directory:
            physical = pathlib.Path(directory) / "physical.json"
            laboratory = pathlib.Path(directory) / "laboratory.json"
            return MODULE.build_report(
                doctor_results(),
                runtime_results(),
                physical_state=physical,
                laboratory_state=laboratory,
                generated_at=generated_at,
            )

    def test_commands_are_read_only(self):
        MODULE.assert_read_only_commands()
        with self.assertRaisesRegex(RuntimeError, "mutation-capable"):
            MODULE.assert_read_only_commands(
                (("systemctl", "--user", "restart", "pipewire"),)
            )

    def test_truth_chain_is_stable_but_report_binds_timestamp(self):
        first = self.report("2026-07-28T00:00:00+00:00")
        second = self.report("2026-07-29T00:00:00+00:00")
        self.assertEqual(first["truth_chain_sha256"], second["truth_chain_sha256"])
        self.assertNotEqual(first["report_sha256"], second["report_sha256"])
        changed = copy.deepcopy(first)
        changed["generated_at"] = "2099-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "report digest mismatch"):
            MODULE.verify_report(changed)

    def test_report_preserves_unresolved_physical_and_laboratory_gates(self):
        report = self.report()
        self.assertEqual(report["physical"]["resolved_count"], 0)
        self.assertFalse(report["physical"]["complete"])
        self.assertEqual(report["laboratory"]["resolved_count"], 0)
        self.assertIn("qobuz-rate-proof", report["laboratory"]["unresolved"])
        self.assertEqual(report["gates"]["single-truth-model"]["status"], "pass")
        self.assertEqual(report["gates"]["host-read-boundary"]["status"], "pass")
        self.assertEqual(
            report["gates"]["safe-listening-calibration"]["status"], "blocked"
        )
        self.assertEqual(report["gates"]["voice-reference"]["status"], "blocked")
        self.assertEqual(
            report["gates"]["latency-xrun-baseline"]["status"],
            "measurement-required",
        )
        self.assertEqual(
            report["gates"]["qobuz-rate-proof"]["status"],
            "measurement-required",
        )
        self.assertEqual(
            report["gates"]["device-loss-baseline"]["status"],
            "exercise-required",
        )
        self.assertIsNone(report["doctor"]["graph"]["round_trip_latency_ms"])

    def test_contract_aggregate_mismatch_is_rejected_even_with_new_outer_hash(self):
        report = self.report()
        report["contracts"]["bindings"]["signal_path"]["sha256"] = "0" * 64
        report["report_sha256"] = MODULE.sha256_json(
            MODULE.report_digest_core(report)
        )
        with self.assertRaisesRegex(ValueError, "contract aggregate digest mismatch"):
            MODULE.verify_report(report)

    def test_doctor_graph_mismatch_is_rejected_even_with_new_outer_hash(self):
        report = self.report()
        report["doctor"]["graph"]["force_rate_hz"] = 96000
        report["report_sha256"] = MODULE.sha256_json(
            MODULE.report_digest_core(report)
        )
        with self.assertRaisesRegex(ValueError, "doctor graph fingerprint mismatch"):
            MODULE.verify_report(report)

    def test_truth_chain_tampering_is_rejected(self):
        report = self.report()
        report["truth_chain_sha256"] = "0" * 64
        report["report_sha256"] = MODULE.sha256_json(
            MODULE.report_digest_core(report)
        )
        with self.assertRaisesRegex(ValueError, "truth chain digest mismatch"):
            MODULE.verify_report(report)

    def test_graph_fingerprint_uses_laboratory_contract(self):
        report = self.report()
        self.assertEqual(
            report["runtime"]["graph_fingerprint"],
            MODULE.LABORATORY.graph_fingerprint(report["doctor"]["graph"]),
        )

    def test_graph_bound_laboratory_evidence_is_invalidated_by_quantum_drift(self):
        graph = {
            "default_sink": "motu-m2",
            "default_source": "motu-m2",
            "force_rate_hz": 48000,
            "force_quantum_frames": 128,
        }
        evidence = {
            "schema_version": 1,
            "kind": "pipewire_xrun_observation",
            "gate": "xrun-stability-test",
            "result": "pass",
            "measured_at": "2026-07-28T00:00:00+00:00",
            "duration_seconds": 60,
            "xrun_delta": 0,
            "rate_hz": 48000,
            "quantum_frames": 128,
            "graph_fingerprint": MODULE.LABORATORY.graph_fingerprint(graph),
            "physical_state_sha256": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state_path = root / "laboratory.json"
            physical_path = root / "physical.json"
            state = MODULE.LABORATORY.empty_state()
            MODULE.LABORATORY.record_gate(
                state,
                "xrun-stability-test",
                evidence,
                physical_path,
            )
            MODULE.LABORATORY.atomic_write_private(state_path, state)
            matching = MODULE.laboratory_projection(state_path, physical_path, graph)
            self.assertIn("xrun-stability-test", matching["resolved"])
            changed = dict(graph, force_quantum_frames=1024)
            invalidated = MODULE.laboratory_projection(
                state_path, physical_path, changed
            )
            self.assertEqual(
                invalidated["invalidated"]["xrun-stability-test"],
                "graph-fingerprint-changed",
            )

    def test_gate_and_command_status_tampering_is_rejected(self):
        report = self.report()
        report["gates"]["voice-reference"]["status"] = "pass"
        recompute(report)
        with self.assertRaisesRegex(ValueError, "gate projection mismatch"):
            MODULE.verify_report(report)
        report = self.report()
        report["commands"][0]["accepted"] = False
        recompute(report)
        with self.assertRaisesRegex(ValueError, "accepted status mismatch"):
            MODULE.verify_report(report)

    def test_process_projection_hides_arguments_and_preserves_material_changes(self):
        processes = MODULE.parse_processes(runtime_results()[2])
        self.assertEqual(processes[0]["classification"], "playback")
        self.assertNotIn("arguments", processes[0])
        self.assertNotIn("command", processes[0])
        self.assertRegex(processes[0]["command_sha256"], r"^[0-9a-f]{64}$")
        first = MODULE.process_fingerprint(processes)
        pid_changed = copy.deepcopy(processes)
        pid_changed[0]["pid"] = 99999
        pid_changed[0]["elapsed_seconds"] = 99999
        self.assertEqual(first, MODULE.process_fingerprint(pid_changed))
        self.assertNotEqual(first, MODULE.process_fingerprint(processes + processes))
        rate_48 = [
            {
                "classification": "recorder",
                "command_sha256": MODULE.sha256_bytes(b"pw-record"),
                "arguments_sha256": MODULE.sha256_bytes(
                    b"pw-record --rate 48000 --channels 2"
                ),
            }
        ]
        rate_96 = copy.deepcopy(rate_48)
        rate_96[0]["arguments_sha256"] = MODULE.sha256_bytes(
            b"pw-record --rate 96000 --channels 8"
        )
        self.assertNotEqual(
            MODULE.process_fingerprint(rate_48), MODULE.process_fingerprint(rate_96)
        )

    def test_prompt_text_cannot_fake_creative_or_plugin_runtime(self):
        fake = MODULE.CommandResult(
            MODULE.READ_ONLY_COMMANDS[2],
            0,
            "PID PPID STAT ELAPSED %CPU %MEM COMMAND COMMAND\n"
            "77 1 S 10 1.0 0.1 codex codex exec review whale FluidSynth sfizz\n",
            "",
        )
        self.assertEqual(MODULE.parse_processes(fake), [])
        self.assertEqual(
            MODULE.classify_process(
                "python3", "python3 /opt/audio/scripts/whale_live.py start"
            ),
            "creative-runtime",
        )

    def test_canonical_command_vector_rejects_mutation_and_runtime_forgery(self):
        report = self.report()
        mutation = MODULE.CommandResult(
            ("systemctl", "--user", "restart", "pipewire"),
            0,
            "",
            "",
            stdout_sha256=MODULE.sha256_bytes(b""),
            stderr_sha256=MODULE.sha256_bytes(b""),
        )
        report["commands"].append(MODULE.command_record(mutation))
        recompute(report)
        with self.assertRaisesRegex(ValueError, "canonical command vector"):
            MODULE.verify_report(report)

        report = self.report()
        report["runtime"]["command_health"][0]["accepted"] = False
        report["runtime"]["observation_completeness"] = (
            MODULE.runtime_observation_completeness(report["runtime"])
        )
        report["gates"] = MODULE.build_gate_status(
            report["doctor"],
            report["physical"],
            report["laboratory"],
            report["runtime"],
            report["contracts"],
        )
        recompute(report)
        with self.assertRaisesRegex(ValueError, "runtime command-health"):
            MODULE.verify_report(report)

    def test_missing_service_observation_degrades_runtime_gate(self):
        report = self.report()
        results = runtime_results()
        results[0] = MODULE.CommandResult(
            results[0].argv,
            4,
            "",
            "",
            stdout_sha256=MODULE.sha256_bytes(b""),
            stderr_sha256=MODULE.sha256_bytes(b""),
        )
        results[1] = MODULE.CommandResult(
            results[1].argv,
            1,
            "",
            "",
            stdout_sha256=MODULE.sha256_bytes(b""),
            stderr_sha256=MODULE.sha256_bytes(b""),
        )
        runtime = MODULE.build_runtime_projection(results, report["doctor"])
        gates = MODULE.build_gate_status(
            report["doctor"],
            report["physical"],
            report["laboratory"],
            runtime,
            report["contracts"],
        )
        self.assertFalse(runtime["observation_completeness"]["complete"])
        self.assertEqual(gates["runtime-storage-observation"]["status"], "degraded")

    def test_qobuz_proof_requires_matching_current_track_context(self):
        track = "a" * 64
        graph = {
            "default_sink": "motu-m2",
            "default_source": "roland-fp-30x",
            "force_rate_hz": 48000,
            "force_quantum_frames": 1024,
        }
        evidence = {
            "schema_version": 1,
            "kind": "qobuz_rate_observation",
            "gate": "qobuz-rate-proof",
            "result": "pass",
            "measured_at": "2026-07-28T00:00:00+00:00",
            "track_rate_hz": 48000,
            "track_fingerprint": track,
            "graph_rate_hz": 48000,
            "endpoint_rate_hz": 48000,
            "resampling_observed": False,
            "method": "read-only current-track rate observation",
            "graph_fingerprint": MODULE.LABORATORY.graph_fingerprint(graph),
            "physical_state_sha256": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            lab_path = root / "laboratory.json"
            physical_path = root / "physical.json"
            state = MODULE.LABORATORY.empty_state()
            MODULE.LABORATORY.record_gate(
                state, "qobuz-rate-proof", evidence, physical_path
            )
            MODULE.LABORATORY.atomic_write_private(lab_path, state)
            missing = MODULE.build_report(
                doctor_results(),
                runtime_results(),
                physical_state=physical_path,
                laboratory_state=lab_path,
            )
            self.assertNotEqual(missing["gates"]["qobuz-rate-proof"]["status"], "pass")
            matching = MODULE.build_report(
                doctor_results(),
                runtime_results(),
                physical_state=physical_path,
                laboratory_state=lab_path,
                qobuz_track_fingerprint=track,
                qobuz_track_rate_hz=48000,
            )
            self.assertEqual(matching["gates"]["qobuz-rate-proof"]["status"], "pass")
            MODULE.verify_report(matching)
            changed = MODULE.build_report(
                doctor_results(),
                runtime_results(),
                physical_state=physical_path,
                laboratory_state=lab_path,
                qobuz_track_fingerprint="b" * 64,
                qobuz_track_rate_hz=48000,
            )
            self.assertNotEqual(changed["gates"]["qobuz-rate-proof"]["status"], "pass")

    def test_all_relevant_processes_are_fingerprinted_and_identity_hidden(self):
        header = "PID PPID STAT ELAPSED %CPU %MEM COMMAND COMMAND\n"
        lines = [
            f"{index} 1 S 1 0.0 0.0 mopidy /usr/bin/mopidy --instance {index}"
            for index in range(1, 202)
        ]
        result = MODULE.CommandResult(
            MODULE.READ_ONLY_COMMANDS[2], 0, header + "\n".join(lines) + "\n", ""
        )
        processes = MODULE.parse_processes(result)
        self.assertEqual(len(processes), 201)
        self.assertNotIn("mopidy", str(processes))
        changed_lines = list(lines)
        changed_lines[-1] += " --changed"
        changed = MODULE.parse_processes(
            MODULE.CommandResult(
                MODULE.READ_ONLY_COMMANDS[2],
                0,
                header + "\n".join(changed_lines) + "\n",
                "",
            )
        )
        self.assertNotEqual(
            MODULE.process_fingerprint(processes),
            MODULE.process_fingerprint(changed),
        )

    def test_timeout_kills_process_group_with_bounded_drain(self):
        original_timeout = MODULE.COMMAND_TIMEOUT_SECONDS
        original_drain = MODULE.POST_KILL_DRAIN_SECONDS
        try:
            MODULE.COMMAND_TIMEOUT_SECONDS = 0.1
            MODULE.POST_KILL_DRAIN_SECONDS = 0.2
            started = time.monotonic()
            result = MODULE.run_read_only(
                (
                    sys.executable,
                    "-c",
                    "import subprocess,sys,time; "
                    "subprocess.Popen([sys.executable,'-c','import time;time.sleep(2)']); "
                    "time.sleep(5)",
                )
            )
            elapsed = time.monotonic() - started
        finally:
            MODULE.COMMAND_TIMEOUT_SECONDS = original_timeout
            MODULE.POST_KILL_DRAIN_SECONDS = original_drain
        self.assertEqual(result.error, "timeout")
        self.assertLess(elapsed, 0.8)

    def test_non_systemctl_returncode_three_degrades_runtime_gate(self):
        report = self.report()
        results = runtime_results()
        df_index = MODULE.READ_ONLY_COMMANDS.index(("df", "-B1", "/"))
        results[df_index] = MODULE.CommandResult(
            results[df_index].argv,
            3,
            "",
            "df failed",
            stdout_sha256=MODULE.sha256_bytes(b""),
            stderr_sha256=MODULE.sha256_bytes(b"df failed"),
        )
        runtime = MODULE.build_runtime_projection(results, report["doctor"])
        gates = MODULE.build_gate_status(
            report["doctor"],
            report["physical"],
            report["laboratory"],
            runtime,
            report["contracts"],
        )
        self.assertEqual(gates["runtime-storage-observation"]["status"], "degraded")

    def test_truncated_command_output_degrades_runtime_gate(self):
        report = self.report()
        results = runtime_results()
        results[0] = MODULE.CommandResult(
            results[0].argv,
            0,
            results[0].stdout,
            "",
            stdout_total_bytes=MODULE.MAX_COMMAND_BYTES + 1,
            stdout_sha256=MODULE.sha256_bytes(results[0].stdout.encode()),
            stderr_sha256=MODULE.sha256_bytes(b""),
            stdout_truncated=True,
        )
        runtime = MODULE.build_runtime_projection(results, report["doctor"])
        gates = MODULE.build_gate_status(
            report["doctor"],
            report["physical"],
            report["laboratory"],
            runtime,
            report["contracts"],
        )
        self.assertEqual(gates["runtime-storage-observation"]["status"], "degraded")

    def test_subprocess_capture_is_memory_bounded(self):
        result = MODULE.run_read_only(
            (
                sys.executable,
                "-c",
                f"import sys;sys.stdout.write('x'*{MODULE.MAX_COMMAND_BYTES + 10000})",
            )
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout_truncated)
        self.assertEqual(len(result.stdout.encode()), MODULE.MAX_COMMAND_BYTES)
        self.assertGreater(result.stdout_total_bytes, MODULE.MAX_COMMAND_BYTES)

    def test_command_evidence_contains_no_raw_output(self):
        record = MODULE.command_record(runtime_results()[2])
        self.assertNotIn("stdout", record)
        self.assertNotIn("stderr", record)
        self.assertRegex(record["stdout_sha256"], r"^[0-9a-f]{64}$")
        report = self.report()
        for item in report["commands"]:
            self.assertNotIn("stdout", item)
            self.assertNotIn("stderr", item)

    def test_transient_output_is_parsed_before_persisted_redaction(self):
        raw = (
            "Standard-Ziel: alsa_output.usb-MOTU_M2_SERIAL-00.Direct__hw_M2__sink\n"
            "Standard-Quelle: alsa_input.usb-MOTU_M2_SERIAL-00.Direct__hw_M2__source\n"
        )
        capture = MODULE.CommandResult(
            ("pactl", "info"),
            0,
            raw,
            "",
            stdout_total_bytes=len(raw.encode()),
            stdout_sha256=MODULE.sha256_bytes(raw.encode()),
            stderr_sha256=MODULE.sha256_bytes(b""),
        )
        converted = MODULE.doctor_inputs_from_capture([capture])[0]
        self.assertEqual(
            MODULE.DOCTOR.normalize_endpoint(
                MODULE.DOCTOR.parse_pactl_default(converted.stdout, "sink")
            ),
            "motu-m2",
        )
        persisted = MODULE.command_record(capture)
        self.assertNotIn("stdout", persisted)
        self.assertNotIn("SERIAL", str(persisted))

    def test_xrun_lines_count_lines_and_drive_drift(self):
        self.assertEqual(
            MODULE.xrun_lines("xrun and underrun same line\nnormal\noverrun\n"),
            ["xrun and underrun same line", "overrun"],
        )
        before = self.report()
        after = copy.deepcopy(before)
        after["runtime"]["journal"]["xrun_like_line_count"] = 1
        after["runtime"]["journal"]["xrun_like_lines_sha256"] = MODULE.sha256_json(
            ["xrun"]
        )
        recompute(after)
        drift = MODULE.build_drift_report(before, after)
        self.assertTrue(drift["changed"])
        self.assertIn("xrun-stability-test", drift["required_remeasurements"])

    def test_drift_uses_catalog_gate_ids(self):
        before = self.report()
        after = copy.deepcopy(before)
        after["doctor"]["graph"]["force_rate_hz"] = 96000
        after["runtime"]["graph_fingerprint"] = MODULE.graph_fingerprint(after["doctor"])
        after["laboratory"]["current_graph_fingerprint"] = after["runtime"][
            "graph_fingerprint"
        ]
        after["gates"] = MODULE.build_gate_status(
            after["doctor"],
            after["physical"],
            after["laboratory"],
            after["runtime"],
            after["contracts"],
        )
        recompute(after)
        drift = MODULE.build_drift_report(before, after)
        self.assertIn(
            "loopback-latency-measurement", drift["required_remeasurements"]
        )
        self.assertIn("xrun-stability-test", drift["required_remeasurements"])
        self.assertIn("qobuz-rate-proof", drift["required_remeasurements"])
        self.assertNotIn("loopback-latency", drift["required_remeasurements"])

    def test_physical_drift_separates_laboratory_gates_and_followups(self):
        required = MODULE.required_remeasurements({"physical_state"})
        followups = MODULE.required_followups({"physical_state"})
        self.assertIn("voice-level-measurement", required)
        self.assertIn("loopback-latency-measurement", required)
        self.assertTrue(set(required) <= set(MODULE.LABORATORY.load_catalog()))
        self.assertIn("safe-listening-calibration", followups)
        self.assertIn("motu-device-loss-exercise", followups)
        self.assertIn("roland-device-loss-exercise", followups)

    def test_process_drift_is_material(self):
        before = self.report()
        after = copy.deepcopy(before)
        after["runtime"]["processes"].append(
            copy.deepcopy(after["runtime"]["processes"][0])
        )
        after["runtime"]["process_fingerprint"] = MODULE.process_fingerprint(
            after["runtime"]["processes"]
        )
        after["gates"] = MODULE.build_gate_status(
            after["doctor"],
            after["physical"],
            after["laboratory"],
            after["runtime"],
            after["contracts"],
        )
        recompute(after)
        drift = MODULE.build_drift_report(before, after)
        self.assertTrue(drift["material"])
        self.assertIn("managed-plugin-host-proof", drift["required_remeasurements"])

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
            loaded = MODULE.load_report(path)
            MODULE.verify_report(loaded)

    def test_symlink_parent_is_rejected_for_read_and_write(self):
        report = self.report()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            actual = root / "actual"
            actual.mkdir()
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            with self.assertRaises(OSError):
                MODULE.atomic_write_private(alias / "truth.json", report)
            path = actual / "truth.json"
            MODULE.atomic_write_private(path, report)
            with self.assertRaises(OSError):
                MODULE.load_report(alias / "truth.json")

    def test_oversized_report_is_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "large.json"
            with path.open("wb") as handle:
                handle.truncate(MODULE.MAX_REPORT_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "exceeds"):
                MODULE.load_report(path)

    def test_tree_scan_is_incrementally_bounded_and_rejects_symlink_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for index in range(4):
                (root / f"file-{index}").write_text("x")
            result = MODULE.bounded_tree_usage(root, limit=2)
            self.assertTrue(result["truncated"])
            actual = root / "actual"
            actual.mkdir()
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            rejected = MODULE.bounded_tree_usage(alias)
            self.assertEqual(rejected["errors"], 1)

    def test_contract_projection_binds_every_required_component(self):
        projection = MODULE.contract_projection()
        self.assertEqual(set(projection["bindings"]), set(MODULE.CONTRACT_PATHS))
        self.assertRegex(projection["aggregate_sha256"], r"^[0-9a-f]{64}$")

    def test_report_states_external_pin_boundary(self):
        report = self.report()
        self.assertIn(
            "authenticity without an externally pinned or signed report digest",
            report["does_not_establish"],
        )


if __name__ == "__main__":
    unittest.main()
