import importlib.util
import json
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "profile_transition", ROOT / "scripts/profile_transition.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

MOTU = "alsa_output.usb-MOTU_M2_PRIVATE_SERIAL-00.Direct__hw_M2__sink"
MOTU_M4 = "alsa_output.usb-MOTU_M4_PRIVATE_SERIAL-00.analog-stereo"
HDMI = "alsa_output.pci-0000_07_00.1.hdmi-stereo-extra1"
SPDIF = "alsa_output.pci-0000_09_00.4.iec958-stereo"


def ready():
    return {
        "profile": "desktop-mixed",
        "profile_executable": True,
        "ready_for_laboratory_apply": True,
        "apply_authority": MODULE.EXPECTED_APPLY_AUTHORITY,
        "missing_hardware": [],
        "missing_physical_facts": [],
        "mismatched_physical_facts": [],
        "unresolved_laboratory_gates": [],
        "invalidated_laboratory_gates": {},
        "incompatible_laboratory_gates": {},
        "planned_graph_fingerprint": "a" * 64,
    }


class FakeRunner:
    def __init__(
        self,
        *,
        default_sink=HDMI,
        sinks=None,
        force_rate=44_100,
        force_quantum=256,
        fail_on=None,
        drift_after_rate_readback=False,
        drift_before_info_read=None,
    ):
        self.default_sink = default_sink
        self.sinks = list(sinks or [HDMI, MOTU, SPDIF])
        self.sink_properties = {
            name: self.properties_for_sink(name) for name in self.sinks
        }
        self.force_rate = force_rate
        self.force_quantum = force_quantum
        self.fail_on = fail_on
        self.failed = False
        self.drift_after_rate_readback = drift_after_rate_readback
        self.drifted = False
        self.drift_before_info_read = drift_before_info_read
        self.info_reads = 0
        self.calls = []
        self.mutations = []

    @staticmethod
    def properties_for_sink(name):
        if name.startswith("alsa_output.usb-MOTU_M2_"):
            serial = name.split("usb-", 1)[1].split("-00", 1)[0]
            return {
                "device.vendor.id": "07fd",
                "device.product.id": "0008",
                "device.serial": serial,
                "device.bus_path": "pci-0000:00:14.0-usb-0:1:1.0",
            }
        if name.startswith("alsa_output.usb-MOTU_M4_"):
            serial = name.split("usb-", 1)[1].split("-00", 1)[0]
            return {
                "device.vendor.id": "07fd",
                "device.product.id": "0014",
                "device.serial": serial,
                "device.bus_path": "pci-0000:00:14.0-usb-0:2:1.0",
            }
        return {}

    def __call__(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv == ("pactl", "info"):
            self.info_reads += 1
            if (
                self.drift_before_info_read is not None
                and self.info_reads == self.drift_before_info_read[0]
            ):
                self.default_sink = self.drift_before_info_read[1]
            return f"Default Sink: {self.default_sink}\n"
        if argv == ("pactl", "--format=json", "list", "sinks"):
            return json.dumps(
                [
                    {
                        "index": index,
                        "name": name,
                        "properties": self.sink_properties[name],
                    }
                    for index, name in enumerate(self.sinks, 1)
                ]
            )
        if argv == ("pw-metadata", "-n", "settings", "0"):
            lines = ['Found "settings" metadata 31\n']
            if self.force_rate is not None:
                lines.append(
                    f"update: id:0 key:'clock.force-rate' value:'{self.force_rate}' type:''\n"
                )
            if self.force_quantum is not None:
                lines.append(
                    "update: id:0 key:'clock.force-quantum' "
                    f"value:'{self.force_quantum}' type:''\n"
                )
            result = "".join(lines)
            if (
                self.drift_after_rate_readback
                and not self.drifted
                and self.force_rate == 48_000
            ):
                self.default_sink = SPDIF
                self.drifted = True
            return result

        label = None
        if argv[:2] == ("pactl", "set-default-sink"):
            label = "default_sink"
        elif argv[:5] == ("pw-metadata", "-n", "settings", "0", "clock.force-rate"):
            label = "force_rate_hz"
        elif argv[:5] == (
            "pw-metadata",
            "-n",
            "settings",
            "0",
            "clock.force-quantum",
        ):
            label = "force_quantum_frames"
        elif argv[:6] == (
            "pw-metadata",
            "-n",
            "settings",
            "-d",
            "0",
            "clock.force-rate",
        ):
            label = "force_rate_hz"
        elif argv[:6] == (
            "pw-metadata",
            "-n",
            "settings",
            "-d",
            "0",
            "clock.force-quantum",
        ):
            label = "force_quantum_frames"
        else:
            raise AssertionError(f"unexpected command: {argv}")

        if self.fail_on == label and not self.failed:
            self.failed = True
            raise MODULE.TransitionError(
                "injected-failure", f"injected failure: {label}", 3
            )

        self.mutations.append(argv)
        if label == "default_sink":
            if argv[2] not in self.sinks:
                raise MODULE.TransitionError(
                    "fake-sink-missing", "fake sink is missing", 3
                )
            self.default_sink = argv[2]
        elif label == "force_rate_hz":
            self.force_rate = None if "-d" in argv else int(argv[-1])
        elif label == "force_quantum_frames":
            self.force_quantum = None if "-d" in argv else int(argv[-1])
        return ""


class ProfileTransitionTests(unittest.TestCase):
    def test_state_root_preparation_rejects_symlink_truths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "target"
            target.mkdir()
            state = root / "state"
            state.symlink_to(target, target_is_directory=True)
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.ensure_state_root(state)
            self.assertEqual(context.exception.code, "state-root-invalid")

        with tempfile.TemporaryDirectory() as directory:
            state = pathlib.Path(directory) / "state"
            state.mkdir()
            target = pathlib.Path(directory) / "target"
            target.mkdir()
            (state / "operations").symlink_to(target, target_is_directory=True)
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.ensure_state_root(state)
            self.assertEqual(context.exception.code, "state-root-invalid")

    def paths(self, directory):
        root = pathlib.Path(directory)
        return (
            root / "physical.json",
            root / "gates.json",
            root / "state",
        )

    def test_diff_redacts_exact_device_identity(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, _state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            public = MODULE.public_plan(plan)
        encoded = json.dumps(public)
        self.assertNotIn("PRIVATE_SERIAL", encoded)
        self.assertEqual(public["before"]["default_sink"], "hdmi")
        self.assertEqual(public["target"]["default_sink"], "motu-m2")
        self.assertEqual(
            [change["field"] for change in public["changes"]],
            ["force_rate_hz", "force_quantum_frames", "default_sink"],
        )
        self.assertFalse(public["idempotent"])

    def test_apply_is_hash_bound_and_repeat_is_idempotent(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.apply_plan(
                    "desktop-mixed",
                    "0" * 64,
                    physical,
                    gates,
                    state,
                    runner,
                    readiness=ready(),
                )
            self.assertEqual(context.exception.code, "plan-changed")
            self.assertEqual(runner.mutations, [])

            result = MODULE.apply_plan(
                "desktop-mixed",
                plan["plan_sha256"],
                physical,
                gates,
                state,
                runner,
                readiness=ready(),
            )
            first_mutations = list(runner.mutations)
            self.assertEqual(result["status"], "applied")
            self.assertEqual(runner.default_sink, MOTU)
            self.assertEqual(runner.force_rate, 48_000)
            self.assertEqual(runner.force_quantum, 1_024)

            repeated = MODULE.apply_plan(
                "desktop-mixed",
                plan["plan_sha256"],
                physical,
                gates,
                state,
                runner,
                readiness=ready(),
            )
            self.assertEqual(repeated["status"], "already-applied")
            self.assertFalse(repeated["mutated"])
            self.assertEqual(runner.mutations, first_mutations)

            journal = MODULE.read_journal(state, result["operation_id"])
            journal_path = MODULE.journal_path(state, result["operation_id"])
            self.assertEqual(journal["status"], "applied")
            self.assertEqual(stat.S_IMODE(journal_path.stat().st_mode), 0o600)

    def test_apply_failure_rolls_back_completed_and_active_operations(self):
        runner = FakeRunner(fail_on="force_quantum_frames")
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.apply_plan(
                    "desktop-mixed",
                    plan["plan_sha256"],
                    physical,
                    gates,
                    state,
                    runner,
                    readiness=ready(),
                )
            self.assertEqual(context.exception.code, "apply-failed-rolled-back")
            self.assertEqual(runner.default_sink, HDMI)
            self.assertEqual(runner.force_rate, 44_100)
            self.assertEqual(runner.force_quantum, 256)
            journal = MODULE.latest_journal(state)
            self.assertEqual(journal["status"], "failed-rolled-back")

    def test_concurrent_unrelated_drift_is_preserved_during_failure_rollback(self):
        runner = FakeRunner(drift_after_rate_readback=True)
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.apply_plan(
                    "desktop-mixed",
                    plan["plan_sha256"],
                    physical,
                    gates,
                    state,
                    runner,
                    readiness=ready(),
                )
            self.assertEqual(context.exception.code, "apply-failed-rolled-back")
            self.assertEqual(runner.default_sink, SPDIF)
            self.assertEqual(runner.force_rate, 44_100)
            self.assertEqual(runner.force_quantum, 256)
            journal = MODULE.latest_journal(state)
            self.assertEqual(journal["status"], "failed-rolled-back")

    def test_explicit_rollback_restores_bound_pre_state(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            applied = MODULE.apply_plan(
                "desktop-mixed",
                plan["plan_sha256"],
                physical,
                gates,
                state,
                runner,
                readiness=ready(),
            )
            rolled_back = MODULE.rollback_operation(
                state, applied["operation_id"], runner
            )
            self.assertEqual(rolled_back["status"], "rolled-back")
            self.assertEqual(runner.default_sink, HDMI)
            self.assertEqual(runner.force_rate, 44_100)
            self.assertEqual(runner.force_quantum, 256)

    def test_superseded_applied_operation_cannot_be_rolled_back(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            first_plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            first = MODULE.apply_plan(
                "desktop-mixed",
                first_plan["plan_sha256"],
                physical,
                gates,
                state,
                runner,
                readiness=ready(),
            )

            runner.default_sink = SPDIF
            runner.force_rate = 96_000
            runner.force_quantum = 512
            second_plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            second = MODULE.apply_plan(
                "desktop-mixed",
                second_plan["plan_sha256"],
                physical,
                gates,
                state,
                runner,
                readiness=ready(),
            )
            mutations = list(runner.mutations)

            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.rollback_operation(state, first["operation_id"], runner)
            self.assertEqual(context.exception.code, "rollback-superseded")
            self.assertEqual(runner.mutations, mutations)
            self.assertEqual(
                MODULE.read_journal(state, first["operation_id"])["status"],
                "applied",
            )
            self.assertEqual(
                MODULE.read_journal(state, second["operation_id"])["status"],
                "applied",
            )

    def test_rollback_revalidates_each_field_immediately_before_mutation(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            applied = MODULE.apply_plan(
                "desktop-mixed",
                plan["plan_sha256"],
                physical,
                gates,
                state,
                runner,
                readiness=ready(),
            )
            runner.info_reads = 0
            runner.drift_before_info_read = (3, SPDIF)
            mutations = list(runner.mutations)

            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.rollback_operation(state, applied["operation_id"], runner)
            self.assertEqual(context.exception.code, "rollback-drift-conflict")
            self.assertEqual(runner.default_sink, SPDIF)
            self.assertEqual(runner.mutations, mutations)
            journal = MODULE.read_journal(state, applied["operation_id"])
            self.assertEqual(journal["status"], "rollback-blocked")
            self.assertEqual(journal["error"]["code"], "rollback-drift-conflict")

    def test_recording_like_quantum_transition_and_rollback_are_narrow(self):
        runner = FakeRunner(
            default_sink=MOTU,
            force_rate=48_000,
            force_quantum=512,
        )
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            self.assertEqual(
                [operation["field"] for operation in plan["operations"]],
                ["force_quantum_frames"],
            )

            applied = MODULE.apply_plan(
                "desktop-mixed",
                plan["plan_sha256"],
                physical,
                gates,
                state,
                runner,
                readiness=ready(),
            )
            self.assertEqual(runner.default_sink, MOTU)
            self.assertEqual(runner.force_rate, 48_000)
            self.assertEqual(runner.force_quantum, 1_024)

            MODULE.rollback_operation(state, applied["operation_id"], runner)
            self.assertEqual(runner.default_sink, MOTU)
            self.assertEqual(runner.force_rate, 48_000)
            self.assertEqual(runner.force_quantum, 512)
            self.assertTrue(
                all("clock.force-quantum" in command for command in runner.mutations)
            )

    def test_rollback_blocks_unrelated_live_drift(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            applied = MODULE.apply_plan(
                "desktop-mixed",
                plan["plan_sha256"],
                physical,
                gates,
                state,
                runner,
                readiness=ready(),
            )
            runner.force_rate = 96_000
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.rollback_operation(state, applied["operation_id"], runner)
            self.assertEqual(context.exception.code, "rollback-drift-conflict")
            journal = MODULE.read_journal(state, applied["operation_id"])
            self.assertEqual(journal["status"], "rollback-blocked")
            self.assertEqual(runner.force_rate, 96_000)
            blocked_status = MODULE.status(state)
            self.assertTrue(blocked_status["recovery_required"])
            self.assertTrue(blocked_status["attention_required"])

            runner.force_rate = 48_000
            recovered = MODULE.recover(state, runner)
            self.assertEqual(recovered["status"], "recovered-by-rollback")
            self.assertEqual(runner.default_sink, HDMI)
            self.assertEqual(runner.force_rate, 44_100)
            self.assertEqual(runner.force_quantum, 256)

    def test_recover_rolls_back_a_partial_transition(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            operation_id = MODULE.new_operation_id()
            journal = {
                "schema_version": 1,
                "kind": "audio_profile_transition_journal",
                "operation_id": operation_id,
                "profile": "desktop-mixed",
                "plan_sha256": plan["plan_sha256"],
                "plan": plan,
                "created_at": MODULE.utc_now(),
                "updated_at": MODULE.utc_now(),
                "status": "applying",
                "completed_indices": [0],
                "active_index": 1,
                "error": None,
            }
            MODULE.ensure_state_root(state)
            runner(tuple(plan["operations"][0]["apply_argv"]))
            runner(tuple(plan["operations"][1]["apply_argv"]))
            MODULE.write_journal(state, journal)

            result = MODULE.recover(state, runner)
            self.assertEqual(result["status"], "recovered-by-rollback")
            self.assertEqual(runner.default_sink, HDMI)
            self.assertEqual(runner.force_rate, 44_100)
            self.assertEqual(runner.force_quantum, 256)

    def test_recovering_complete_target_preserves_complete_rollback(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            operation_id = MODULE.new_operation_id()
            journal = {
                "schema_version": 1,
                "kind": "audio_profile_transition_journal",
                "operation_id": operation_id,
                "profile": "desktop-mixed",
                "plan_sha256": plan["plan_sha256"],
                "plan": plan,
                "created_at": MODULE.utc_now(),
                "updated_at": MODULE.utc_now(),
                "status": "applying",
                "completed_indices": [0],
                "active_index": 1,
                "error": None,
            }
            MODULE.ensure_state_root(state)
            for operation in plan["operations"]:
                runner(tuple(operation["apply_argv"]))
            MODULE.write_journal(state, journal)

            recovered = MODULE.recover(state, runner)
            self.assertEqual(recovered["status"], "recovered-as-applied")
            recovered_journal = MODULE.read_journal(state, operation_id)
            self.assertEqual(
                recovered_journal["completed_indices"],
                list(range(len(plan["operations"]))),
            )

            rolled_back = MODULE.rollback_operation(state, operation_id, runner)
            self.assertEqual(rolled_back["status"], "rolled-back")
            self.assertEqual(runner.default_sink, HDMI)
            self.assertEqual(runner.force_rate, 44_100)
            self.assertEqual(runner.force_quantum, 256)

    def test_noncontiguous_journal_progress_is_rejected(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            operation_id = MODULE.new_operation_id()
            journal = {
                "schema_version": 1,
                "kind": "audio_profile_transition_journal",
                "operation_id": operation_id,
                "profile": "desktop-mixed",
                "plan_sha256": plan["plan_sha256"],
                "plan": plan,
                "created_at": MODULE.utc_now(),
                "updated_at": MODULE.utc_now(),
                "status": "applying",
                "completed_indices": [1],
                "active_index": None,
                "error": None,
            }
            MODULE.ensure_state_root(state)
            MODULE.write_journal(state, journal)
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.read_journal(state, operation_id)
            self.assertEqual(context.exception.code, "journal-invalid")

    def test_tampered_journal_cannot_expand_the_command_allowlist(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            applied = MODULE.apply_plan(
                "desktop-mixed",
                plan["plan_sha256"],
                physical,
                gates,
                state,
                runner,
                readiness=ready(),
            )
            journal = MODULE.read_journal(state, applied["operation_id"])
            journal["plan"]["operations"][0]["rollback_argv"] = [
                "untrusted-command",
                "--forbidden",
            ]
            unsigned = dict(journal["plan"])
            unsigned.pop("plan_sha256")
            journal["plan"]["plan_sha256"] = MODULE.sha256_payload(unsigned)
            journal["plan_sha256"] = journal["plan"]["plan_sha256"]
            MODULE.atomic_write_private(
                MODULE.journal_path(state, applied["operation_id"]), journal
            )
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.read_journal(state, applied["operation_id"])
            self.assertEqual(context.exception.code, "journal-invalid")

    def test_rehashed_plan_with_unknown_field_is_rejected(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            applied = MODULE.apply_plan(
                "desktop-mixed",
                plan["plan_sha256"],
                physical,
                gates,
                state,
                runner,
                readiness=ready(),
            )
            journal = MODULE.read_journal(state, applied["operation_id"])
            journal["plan"]["unreviewed_extension"] = True
            unsigned = dict(journal["plan"])
            unsigned.pop("plan_sha256")
            journal["plan"]["plan_sha256"] = MODULE.sha256_payload(unsigned)
            journal["plan_sha256"] = journal["plan"]["plan_sha256"]
            MODULE.atomic_write_private(
                MODULE.journal_path(state, applied["operation_id"]), journal
            )
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.read_journal(state, applied["operation_id"])
            self.assertEqual(context.exception.code, "journal-invalid")

    def test_motu_identity_change_invalidates_approved_plan_without_mutation(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            runner.sink_properties[MOTU]["device.bus_path"] = (
                "pci-0000:00:14.0-usb-0:9:1.0"
            )
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.apply_plan(
                    "desktop-mixed",
                    plan["plan_sha256"],
                    physical,
                    gates,
                    state,
                    runner,
                    readiness=ready(),
                )
            self.assertEqual(context.exception.code, "plan-changed")
            self.assertEqual(runner.mutations, [])

    def test_non_m2_motu_sink_is_not_accepted_as_m2(self):
        runner = FakeRunner(sinks=[HDMI, MOTU_M4, SPDIF])
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, _state = self.paths(directory)
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.build_plan(
                    "desktop-mixed",
                    physical,
                    gates,
                    runner,
                    readiness=ready(),
                )
            self.assertEqual(context.exception.code, "motu-sink-ambiguous")
            self.assertEqual(runner.mutations, [])

    def test_motu_identity_is_private_and_bound_into_plan(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, _state = self.paths(directory)
            plan = MODULE.build_plan(
                "desktop-mixed", physical, gates, runner, readiness=ready()
            )
            public = MODULE.public_plan(plan)
        self.assertRegex(plan["target_sink_identity_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("target_sink_identity_sha256", public)
        self.assertNotIn("PRIVATE_SERIAL", json.dumps(public))

    def test_duplicate_motu_sinks_fail_closed(self):
        runner = FakeRunner(sinks=[HDMI, MOTU, MOTU + "-duplicate"])
        with tempfile.TemporaryDirectory() as directory:
            physical, gates, _state = self.paths(directory)
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.build_plan(
                    "desktop-mixed",
                    physical,
                    gates,
                    runner,
                    readiness=ready(),
                )
            self.assertEqual(context.exception.code, "motu-sink-ambiguous")

    def test_ambiguous_pipewire_metadata_fails_closed(self):
        runner = FakeRunner()

        def ambiguous_metadata(argv):
            if tuple(argv) == ("pw-metadata", "-n", "settings", "0"):
                return (
                    "update: id:0 key:'clock.force-rate' value:'44100' type:''\n"
                    "update: id:0 key:'clock.force-rate' value:'48000' type:''\n"
                    "update: id:0 key:'clock.force-quantum' value:'256' type:''\n"
                )
            return runner(argv)

        with tempfile.TemporaryDirectory() as directory:
            physical, gates, _state = self.paths(directory)
            with self.assertRaises(MODULE.TransitionError) as context:
                MODULE.build_plan(
                    "desktop-mixed",
                    physical,
                    gates,
                    ambiguous_metadata,
                    readiness=ready(),
                )
            self.assertEqual(context.exception.code, "metadata-invalid")
            self.assertEqual(runner.mutations, [])

    def test_runtime_command_allowlist_rejects_untrusted_argv(self):
        MODULE.validate_command_argv(("pactl", "info"))
        MODULE.validate_command_argv(("pactl", "set-default-sink", "known-live-sink"))
        with self.assertRaises(MODULE.TransitionError) as context:
            MODULE.validate_command_argv(("untrusted-command", "--forbidden"))
        self.assertEqual(context.exception.code, "command-not-allowed")
        with self.assertRaises(MODULE.TransitionError) as context:
            MODULE.validate_command_argv(
                ("pactl", "set-default-sink", "--option-like-sink")
            )
        self.assertEqual(context.exception.code, "command-not-allowed")
        with self.assertRaises(MODULE.TransitionError) as context:
            MODULE.validate_command_argv(
                (
                    "pw-metadata",
                    "-n",
                    "settings",
                    "0",
                    "clock.force-rate",
                    "9" * (MODULE.MAX_METADATA_DIGITS + 1),
                )
            )
        self.assertEqual(context.exception.code, "command-not-allowed")

    def test_transition_lock_wait_is_bounded(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(MODULE, "LOCK_TIMEOUT_SECONDS", 0.0),
            mock.patch.object(
                MODULE.fcntl,
                "flock",
                side_effect=BlockingIOError("held by another transition"),
            ),
        ):
            with self.assertRaises(MODULE.TransitionError) as context:
                with MODULE.transition_lock(pathlib.Path(directory) / "state"):
                    self.fail("busy transition lock must not enter")
            self.assertEqual(context.exception.code, "transition-busy")

    def test_runtime_command_output_is_bounded_while_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_pactl = pathlib.Path(directory) / "pactl"
            fake_pactl.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                f"os.write(1, b'x' * {MODULE.MAX_COMMAND_OUTPUT_BYTES + 1})\n"
            )
            fake_pactl.chmod(0o700)
            with mock.patch.dict(MODULE.COMMAND_PATHS, {"pactl": fake_pactl}):
                with self.assertRaises(MODULE.TransitionError) as context:
                    MODULE.run_command(("pactl", "info"))
            self.assertEqual(context.exception.code, "command-output-limit")

    def test_status_without_state_is_side_effect_free(self):
        with tempfile.TemporaryDirectory() as directory:
            state = pathlib.Path(directory) / "absent"
            result = MODULE.status(state)
            self.assertEqual(result["status"], "no-operations")
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
