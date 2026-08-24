import hashlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY_PATH = ROOT / "scripts" / "audio_control_deploy.py"
CONTROL_PATH = ROOT / "scripts" / "audio_control.py"

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

DEPLOY = load("audio_control_profile_transition_release_contract", DEPLOY_PATH)
CONTROL = load("audio_control_profile_transition_runtime_binding", CONTROL_PATH)


class AudioControlProfileTransitionReleaseTests(unittest.TestCase):
    def write_bound_release(self, root: pathlib.Path) -> pathlib.Path:
        hashes = {}
        for index, relative in enumerate(
            sorted(CONTROL.PROFILE_TRANSITION_RELEASE_BINDING_FILES)
        ):
            payload = f"bound-{index}-{relative}\n".encode()
            target = root.joinpath(*pathlib.PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            hashes[relative] = hashlib.sha256(payload).hexdigest()
        marker = root / ".audio-control-release.json"
        marker.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "kind": "audio_control_release",
                    "commit": "a" * 40,
                    "critical_sha256": hashes,
                }
            ),
            encoding="utf-8",
        )
        return marker

    def test_sentinel_binds_the_transition_runtime_closure(self):
        expected = {
            "scripts/profile_transition.py",
            "scripts/profile_planner.py",
            "scripts/audio_doctor.py",
            "scripts/physical_verification.py",
            "scripts/laboratory_gate.py",
            "profiles/audio-profiles.v1.json",
            "inventory/physical-facts.v1.json",
            "inventory/physical-verification.v1.json",
            "inventory/laboratory-gates.v1.json",
            "tests/test_audio_control_profile_transition_release.py",
        }
        self.assertEqual(set(DEPLOY.PROFILE_TRANSITION_CRITICAL_RELEASE_FILES), expected)
        self.assertEqual(set(CONTROL.PROFILE_TRANSITION_RELEASE_BINDING_FILES), expected)
        self.assertEqual(
            DEPLOY.PROFILE_TRANSITION_RELEASE_SENTINEL,
            "tests/test_audio_control_profile_transition_release.py",
        )

    def test_transition_closure_is_conditional_for_legacy_release_compatibility(self):
        self.assertTrue(
            set(DEPLOY.PROFILE_TRANSITION_CRITICAL_RELEASE_FILES).isdisjoint(
                DEPLOY.BASE_CRITICAL_RELEASE_FILES
            )
        )

    def test_deployed_legacy_marker_blocks_transition_until_new_binding_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            marker = release / ".audio-control-release.json"
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "audio_control_release",
                        "commit": "b" * 40,
                        "index_sha256": "0" * 64,
                        "app_sha256": "1" * 64,
                        "styles_sha256": "2" * 64,
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(CONTROL, "ROOT", release),
                mock.patch.object(CONTROL, "RELEASE_MARKER", marker),
                self.assertRaisesRegex(CONTROL.ControlError, "Release-Bindung"),
            ):
                CONTROL.verify_profile_transition_release_binding()

    def test_complete_binding_allows_transition_and_runtime_drift_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            marker = self.write_bound_release(release)
            with (
                mock.patch.object(CONTROL, "ROOT", release),
                mock.patch.object(CONTROL, "RELEASE_MARKER", marker),
            ):
                receipt = CONTROL.verify_profile_transition_release_binding()
                self.assertTrue(receipt["bound"])
                self.assertTrue(receipt["executable"])
                (release / "scripts" / "profile_transition.py").write_text(
                    "drifted\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(CONTROL.ControlError, "gedriftet"):
                    CONTROL.verify_profile_transition_release_binding()

    def test_missing_marker_is_allowed_only_for_real_source_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory)
            marker = source / ".audio-control-release.json"
            (source / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
            with (
                mock.patch.object(CONTROL, "ROOT", source),
                mock.patch.object(CONTROL, "RELEASE_MARKER", marker),
            ):
                receipt = CONTROL.verify_profile_transition_release_binding()
                self.assertEqual(receipt["status"], "source-checkout")
            (source / ".git").unlink()
            with (
                mock.patch.object(CONTROL, "ROOT", source),
                mock.patch.object(CONTROL, "RELEASE_MARKER", marker),
                self.assertRaisesRegex(CONTROL.ControlError, "Release-Bindung"),
            ):
                CONTROL.verify_profile_transition_release_binding()

    def test_missing_marker_allows_only_canonical_hidden_release_validation_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            releases = base / "releases"
            releases.mkdir()
            candidate = releases / ("." + "c" * 40 + ".candidate")
            candidate.mkdir()
            marker = candidate / ".audio-control-release.json"
            with (
                mock.patch.object(CONTROL, "ROOT", candidate),
                mock.patch.object(CONTROL, "RELEASE_MARKER", marker),
                mock.patch.object(CONTROL, "DEPLOY_RELEASE_ROOT", releases),
            ):
                receipt = CONTROL.verify_profile_transition_release_binding()
                self.assertEqual(receipt["status"], "release-validation")
            activated = releases / ("c" * 40)
            activated.mkdir()
            with (
                mock.patch.object(CONTROL, "ROOT", activated),
                mock.patch.object(
                    CONTROL, "RELEASE_MARKER", activated / ".audio-control-release.json"
                ),
                mock.patch.object(CONTROL, "DEPLOY_RELEASE_ROOT", releases),
                self.assertRaisesRegex(CONTROL.ControlError, "Release-Bindung"),
            ):
                CONTROL.verify_profile_transition_release_binding()

    def test_binding_failure_prevents_transition_module_import(self):
        with (
            mock.patch.object(CONTROL, "_PROFILE_TRANSITION_MODULE", None),
            mock.patch.object(CONTROL, "_PROFILE_TRANSITION_IMPORT_ERROR", None),
            mock.patch.object(
                CONTROL,
                "verify_profile_transition_release_binding",
                side_effect=CONTROL.ControlError("binding blocked"),
            ),
            mock.patch.object(CONTROL.importlib.util, "spec_from_file_location") as spec,
            self.assertRaisesRegex(CONTROL.ControlError, "binding blocked"),
        ):
            CONTROL.load_profile_transition()
        spec.assert_not_called()

    def test_base_bound_bootstrap_cli_routes_without_audio_effect(self):
        receipt = {
            "schema_version": 1,
            "kind": "audio_runtime_state_bootstrap",
            "status": "ready",
            "prepared_write_roots": 4,
            "private_state": True,
            "audio_mutated": False,
        }
        with (
            mock.patch.object(
                CONTROL, "prepare_runtime_state_bootstrap", return_value=receipt
            ) as prepare,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(CONTROL.main(["prepare-runtime-state"]), 0)
        prepare.assert_called_once_with()

    def test_base_bound_bootstrap_prepares_exact_static_write_roots_audio_free(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            output = base / "Music" / "Audio-Aufnahmen"
            recording = base / ".local" / "state" / "audio" / "recordings-v1"
            transition = base / ".local" / "state" / "audio" / "profile-transitions-v1"
            laboratory = base / ".local" / "state" / "audio" / "laboratory"
            with (
                mock.patch.object(CONTROL, "RECORDING_OUTPUT_ROOT", output),
                mock.patch.object(CONTROL, "STATIC_RECORDING_OUTPUT_ROOT", output),
                mock.patch.object(CONTROL, "RECORDING_STATE_ROOT", recording),
                mock.patch.object(CONTROL, "STATIC_RECORDING_STATE_ROOT", recording),
                mock.patch.object(CONTROL, "PROFILE_TRANSITION_STATE_ROOT", transition),
                mock.patch.object(
                    CONTROL, "STATIC_PROFILE_TRANSITION_STATE_ROOT", transition
                ),
                mock.patch.object(CONTROL, "LABORATORY_STATE_ROOT", laboratory),
                mock.patch.object(
                    CONTROL, "STATIC_LABORATORY_STATE_ROOT", laboratory
                ),
            ):
                receipt = CONTROL.prepare_runtime_state_bootstrap()
            self.assertEqual(receipt["status"], "ready")
            self.assertEqual(receipt["prepared_write_roots"], 4)
            self.assertTrue(receipt["private_state"])
            self.assertFalse(receipt["audio_mutated"])
            self.assertNotIn(str(base), repr(receipt))
            for path in (
                output, recording, transition, laboratory, transition / "operations"
            ):
                self.assertTrue(path.is_dir())
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_base_bound_bootstrap_rejects_override_before_creating_any_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            output = base / "custom" / "Audio-Aufnahmen"
            expected_output = base / "Music" / "Audio-Aufnahmen"
            recording = base / ".local" / "state" / "audio" / "recordings-v1"
            transition = base / ".local" / "state" / "audio" / "profile-transitions-v1"
            laboratory = base / ".local" / "state" / "audio" / "laboratory"
            with (
                mock.patch.object(CONTROL, "RECORDING_OUTPUT_ROOT", output),
                mock.patch.object(
                    CONTROL, "STATIC_RECORDING_OUTPUT_ROOT", expected_output
                ),
                mock.patch.object(CONTROL, "RECORDING_STATE_ROOT", recording),
                mock.patch.object(CONTROL, "STATIC_RECORDING_STATE_ROOT", recording),
                mock.patch.object(CONTROL, "PROFILE_TRANSITION_STATE_ROOT", transition),
                mock.patch.object(
                    CONTROL, "STATIC_PROFILE_TRANSITION_STATE_ROOT", transition
                ),
                mock.patch.object(CONTROL, "LABORATORY_STATE_ROOT", laboratory),
                mock.patch.object(
                    CONTROL, "STATIC_LABORATORY_STATE_ROOT", laboratory
                ),
                self.assertRaisesRegex(CONTROL.ControlError, "systemd-Schreibvertrag"),
            ):
                CONTROL.prepare_runtime_state_bootstrap()
            self.assertFalse(output.exists())
            self.assertFalse(expected_output.exists())
            self.assertFalse(recording.exists())
            self.assertFalse(transition.exists())
            self.assertFalse(laboratory.exists())

    def test_base_bound_bootstrap_rejects_symlink_path_component(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            real = base / "real"
            real.mkdir()
            linked = base / "linked"
            linked.symlink_to(real, target_is_directory=True)
            output = linked / "Audio-Aufnahmen"
            recording = base / ".local" / "state" / "audio" / "recordings-v1"
            transition = base / ".local" / "state" / "audio" / "profile-transitions-v1"
            laboratory = base / ".local" / "state" / "audio" / "laboratory"
            with (
                mock.patch.object(CONTROL, "RECORDING_OUTPUT_ROOT", output),
                mock.patch.object(CONTROL, "STATIC_RECORDING_OUTPUT_ROOT", output),
                mock.patch.object(CONTROL, "RECORDING_STATE_ROOT", recording),
                mock.patch.object(CONTROL, "STATIC_RECORDING_STATE_ROOT", recording),
                mock.patch.object(CONTROL, "PROFILE_TRANSITION_STATE_ROOT", transition),
                mock.patch.object(
                    CONTROL, "STATIC_PROFILE_TRANSITION_STATE_ROOT", transition
                ),
                mock.patch.object(CONTROL, "LABORATORY_STATE_ROOT", laboratory),
                mock.patch.object(
                    CONTROL, "STATIC_LABORATORY_STATE_ROOT", laboratory
                ),
                self.assertRaisesRegex(CONTROL.ControlError, "unsichere Pfadkomponente"),
            ):
                CONTROL.prepare_runtime_state_bootstrap()


if __name__ == "__main__":
    unittest.main()
