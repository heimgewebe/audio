import argparse
import hashlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "audio_control_deploy.py"
SPEC = importlib.util.spec_from_file_location("audio_control_deploy", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class AudioControlDeployTests(unittest.TestCase):
    @staticmethod
    def write_release(
        release: pathlib.Path,
        commit: str,
        *,
        created_at: int = 1,
        profile_transition: bool = False,
    ) -> None:
        files = {
            "scripts/audio_control.py": b"print('control')\n",
            "scripts/dauersong_live.py": b"print('dauersong')\n",
            "inventory/dauersong-v9-legacy.v1.json": b"{}\n",
            "systemd/user/grabowski-dauersong.service.d/zz-audio-control-v1.conf": b"[Service]\nRestart=no\n",
            "scripts/audio_level_observer.py": b"print('observer')\n",
            "scripts/audio_live_telemetry.py": b"print('telemetry')\n",
            "scripts/qobuz_desktop_recovery.py": b"print('recovery')\n",
            "docs/qobuz-desktop-recovery.md": b"# Recovery\n",
            "scripts/qbzd_qconnect_recovery.py": b"print('qbzd recovery')\n",
            "docs/qbzd-qconnect-recovery.md": b"# QBZD Recovery\n",
            "scripts/audio_remote_bridge.py": b"print('bridge')\n",
            "scripts/audio_remote_bridge_tailscale.py": b"print('tailscale')\n",
            "scripts/audio_remote_bridge_ipad_probe.py": b"print('ipad probe')\n",
            "inventory/audiozentrale-remote-bridge.v1.json": b"{}\n",
            "schemas/audiozentrale-remote-bridge.v1.schema.json": b"{}\n",
            "systemd/user/audio-remote-bridge-v1.service": b"[Service]\nExecStart=/usr/bin/true\n",
            "systemd/user/audio-control-level-observer-v1.service": b"[Service]\nExecStart=/usr/bin/true\n",
            "systemd/user/audio-qobuz-desktop-recovery-v1.service": b"[Service]\nExecStart=/usr/bin/true\n",
            "systemd/user/audio-qbzd-qconnect-recovery-v1.service": b"[Service]\nExecStart=/usr/bin/true\n",
            "systemd/user/audio-control-ui-v1.service": b"[Service]\nExecStart=/usr/bin/true\n",
            "inventory/audiozentrale-ipad-pwa.v1.json": b"{}\n",
            "schemas/audiozentrale-ipad-pwa.v1.schema.json": b"{}\n",
            "scripts/audio_telemetry_replay.py": b"def load_replay_contract(): return {}\n",
            "inventory/audiozentrale-telemetry-replay.v1.json": b"{}\n",
            "schemas/audiozentrale-telemetry-replay.v1.schema.json": b"{}\n",
            "scripts/whale_learning_lesson.py": b"def load_lesson_contract(): return {}\n",
            "inventory/buckelwal-learning-lesson.v1.json": b"{}\n",
            "schemas/buckelwal-learning-lesson.v1.schema.json": b"{}\n",
            "assets/whale-sources/processed/manifest.json": b"{}\n",
            "assets/whale-sources/processed/humpback-song-cc0-01.wav": b"RIFF-source\n",
            "assets/whale-sources/morph/manifest.json": b"{}\n",
            "ui/whale-lesson.js": b'"use strict";\n',
            "ui/whale-learning-reference.wav": b"RIFF-reference\n",
            "ui/whale-learning-morph.wav": b"RIFF-morph\n",
            "ui/whale-learning-envelope.wav": b"RIFF-envelope\n",
            "ui/whale-learning-periodicity.wav": b"RIFF-periodicity\n",
            "ui/whale-learning-articulation.wav": b"RIFF-articulation\n",
            "ui/index.html": b"<!doctype html><title>Audio</title>\n",
            "ui/app.js": b'"use strict";\n',
            "ui/styles.css": b"body { margin: 0; }\n",
            "ui/sw.js": b'"use strict";\n',
            "ui/manifest.webmanifest": b'{"name":"Audiozentrale"}\n',
            "ui/icon-180.png": b"PNG-180\n",
            "ui/icon-192.png": b"PNG-192\n",
            "ui/icon-512.png": b"PNG-512\n",
            "tests/test_audio_control.py": b"import unittest\n",
            "tests/test_audio_level_observer.py": b"import unittest\n",
            "tests/test_audio_live_telemetry.py": b"import unittest\n",
            "tests/test_qobuz_desktop_recovery.py": b"import unittest\n",
            "tests/test_audio_ipad_pwa.py": b"import unittest\n",
            "tests/test_audio_remote_bridge.py": b"import unittest\n",
        }
        if profile_transition:
            files.update(
                {
                    "scripts/profile_transition.py": b"DEFAULT_STATE_ROOT = None\n",
                    "scripts/profile_planner.py": b"# planner\n",
                    "scripts/audio_doctor.py": b"# doctor\n",
                    "scripts/physical_verification.py": b"# physical\n",
                    "scripts/laboratory_gate.py": b"# laboratory\n",
                    "profiles/audio-profiles.v1.json": b"{}\n",
                    "inventory/physical-facts.v1.json": b"{}\n",
                    "inventory/physical-verification.v1.json": b"{}\n",
                    "inventory/laboratory-gates.v1.json": b"{}\n",
                    MODULE.PROFILE_TRANSITION_RELEASE_SENTINEL: b"import unittest\n",
                }
            )
        for relative, payload in files.items():
            target = release / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        hashes = MODULE.release_hashes(release)
        marker = {
            "schema_version": 2,
            "kind": "audio_control_release",
            "commit": commit,
            "created_at_unix": created_at,
            "critical_sha256": hashes,
            "index_sha256": hashes["ui/index.html"],
            "app_sha256": hashes["ui/app.js"],
            "styles_sha256": hashes["ui/styles.css"],
        }
        (release / ".audio-control-release.json").write_text(
            json.dumps(marker), encoding="utf-8"
        )

    @staticmethod
    def sync_args(root: pathlib.Path) -> argparse.Namespace:
        return argparse.Namespace(
            source_repo=root,
            deploy_root=root / "deploy",
            state_root=root / "state",
            remote="origin",
            branch="main",
            expected_commit="",
            unit="audio-control-ui-v1.service",
            host="127.0.0.1",
            port=8765,
        )

    def failed_candidate_rollback(
        self,
        root: pathlib.Path,
        *,
        previous: str,
        commit: str,
        previous_supports_recovery: bool,
        previous_recovery_error: Exception | None = None,
    ) -> tuple[Exception, mock.Mock, pathlib.Path]:
        args = self.sync_args(root)
        args.deploy_root.mkdir()
        args.state_root.mkdir()
        previous_release = args.deploy_root / "releases" / previous
        previous_release.mkdir(parents=True)
        if previous_supports_recovery:
            sentinel = previous_release / MODULE.QOBUZ_RECOVERY_RELEASE_SENTINEL
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("recovery\n", encoding="utf-8")
        release = args.deploy_root / "releases" / commit
        self.write_release(release, commit)
        converge = mock.Mock(side_effect=previous_recovery_error)
        with (
            mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
            mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
            mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
            mock.patch.object(
                MODULE,
                "prepare_deployment_repository",
                return_value=(root / "repository.git", []),
            ),
            mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
            mock.patch.object(
                MODULE, "upgrade_current_release_marker", return_value={"changed": False}
            ),
            mock.patch.object(MODULE, "read_current_commit", return_value=previous),
            mock.patch.object(
                MODULE, "prepare_release", return_value=(release, [], False)
            ),
            mock.patch.object(MODULE, "release_supports_remote_bridge", return_value=False),
            mock.patch.object(MODULE, "switch_current"),
            mock.patch.object(
                MODULE,
                "reconcile_runtime_environment",
                return_value=({"changed": False}, None),
            ),
            mock.patch.object(MODULE, "install_release_runtime", return_value=([], [])),
            mock.patch.object(MODULE, "activate_service", return_value=[]),
            mock.patch.object(
                MODULE,
                "verify_service",
                side_effect=MODULE.DeployError("candidate UI unhealthy"),
            ),
            mock.patch.object(MODULE, "stop_qobuz_recovery_for_rollback") as stop,
            mock.patch.object(MODULE, "converge_qobuz_recovery", converge),
            self.assertRaises(MODULE.DeployError) as raised,
        ):
            MODULE.sync(args)
        stop.assert_called_once_with()
        return raised.exception, converge, previous_release

    def test_dauersong_hardening_is_installed_after_other_runtime_files(self):
        self.assertEqual(
            list(MODULE.RUNTIME_FILES)[-1], MODULE.DAUERSONG_HARDENING_RELATIVE
        )

    def test_dauersong_runtime_files_are_release_critical(self):
        expected = {
            "scripts/dauersong_live.py",
            "inventory/dauersong-v9-legacy.v1.json",
            "systemd/user/grabowski-dauersong.service.d/zz-audio-control-v1.conf",
        }
        self.assertTrue(expected <= set(MODULE.BASE_CRITICAL_RELEASE_FILES))
        commit = "8" * 40
        for missing in sorted(expected):
            with self.subTest(missing=missing):
                with tempfile.TemporaryDirectory() as directory:
                    release = pathlib.Path(directory)
                    self.write_release(release, commit)
                    (release / missing).unlink()
                    with self.assertRaisesRegex(
                        MODULE.DeployError, "Kritische Releasedatei"
                    ):
                        MODULE.release_hashes(release)

    def test_replay_runtime_files_are_release_critical(self):
        expected = {
            "scripts/audio_telemetry_replay.py",
            "inventory/audiozentrale-telemetry-replay.v1.json",
            "schemas/audiozentrale-telemetry-replay.v1.schema.json",
            "scripts/whale_learning_lesson.py",
            "inventory/buckelwal-learning-lesson.v1.json",
            "schemas/buckelwal-learning-lesson.v1.schema.json",
            "assets/whale-sources/processed/manifest.json",
            "assets/whale-sources/processed/humpback-song-cc0-01.wav",
            "assets/whale-sources/morph/manifest.json",
            "ui/whale-lesson.js",
            "ui/whale-learning-reference.wav",
            "ui/whale-learning-morph.wav",
            "ui/whale-learning-envelope.wav",
            "ui/whale-learning-periodicity.wav",
            "ui/whale-learning-articulation.wav",
        }
        self.assertTrue(expected <= set(MODULE.BASE_CRITICAL_RELEASE_FILES))
        commit = "9" * 40
        for missing in sorted(expected):
            with self.subTest(missing=missing):
                with tempfile.TemporaryDirectory() as directory:
                    release = pathlib.Path(directory)
                    self.write_release(release, commit)
                    (release / missing).unlink()
                    with self.assertRaisesRegex(
                        MODULE.DeployError, "Kritische Releasedatei"
                    ):
                        MODULE.release_hashes(release)

    def test_ipad_pwa_runtime_files_are_release_critical(self):
        expected = {
            "inventory/audiozentrale-ipad-pwa.v1.json",
            "schemas/audiozentrale-ipad-pwa.v1.schema.json",
            "ui/sw.js",
            "ui/manifest.webmanifest",
            "ui/icon-180.png",
            "ui/icon-192.png",
            "ui/icon-512.png",
        }
        self.assertTrue(expected <= set(MODULE.BASE_CRITICAL_RELEASE_FILES))
        commit = "8" * 40
        for missing in sorted(expected):
            with self.subTest(missing=missing):
                with tempfile.TemporaryDirectory() as directory:
                    release = pathlib.Path(directory)
                    self.write_release(release, commit)
                    (release / missing).unlink()
                    with self.assertRaisesRegex(
                        MODULE.DeployError, "Kritische Releasedatei"
                    ):
                        MODULE.release_hashes(release)

    def test_remote_bridge_runtime_files_are_release_critical(self):
        expected = set(MODULE.REMOTE_BRIDGE_CRITICAL_RELEASE_FILES)
        self.assertTrue(expected <= set(MODULE.BASE_CRITICAL_RELEASE_FILES))
        self.assertIn(
            "systemd/user/audio-remote-bridge-v1.service", MODULE.RUNTIME_FILES
        )
        commit = "7" * 40
        for missing in sorted(expected):
            with self.subTest(missing=missing):
                with tempfile.TemporaryDirectory() as directory:
                    release = pathlib.Path(directory)
                    self.write_release(release, commit)
                    (release / missing).unlink()
                    with self.assertRaisesRegex(
                        MODULE.DeployError, "Kritische Releasedatei"
                    ):
                        MODULE.release_hashes(release)

    def test_profile_transition_runtime_closure_is_release_critical(self):
        expected = set(MODULE.PROFILE_TRANSITION_CRITICAL_RELEASE_FILES)
        commit = "3" * 40
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            self.write_release(release, commit, profile_transition=True)
            self.assertTrue(expected <= set(MODULE.critical_release_paths(release)))
            marker = MODULE.verify_release_marker(release, expected_commit=commit)
            self.assertTrue(expected <= set(marker["critical_sha256"]))

        for missing in sorted(
            expected - {MODULE.PROFILE_TRANSITION_RELEASE_SENTINEL}
        ):
            with self.subTest(missing=missing):
                with tempfile.TemporaryDirectory() as directory:
                    release = pathlib.Path(directory)
                    self.write_release(release, commit, profile_transition=True)
                    (release / missing).unlink()
                    with self.assertRaisesRegex(
                        MODULE.DeployError, "Kritische Releasedatei"
                    ):
                        MODULE.release_hashes(release)
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            self.write_release(release, commit, profile_transition=True)
            (release / MODULE.PROFILE_TRANSITION_RELEASE_SENTINEL).unlink()
            with self.assertRaisesRegex(MODULE.DeployError, "Gebundene Releasedatei"):
                MODULE.verify_release_marker(release, expected_commit=commit)

    def test_profile_transition_runtime_drift_fails_service_readback_closed(self):
        commit = "2" * 40
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            self.write_release(release, commit, profile_transition=True)
            (release / "scripts" / "profile_transition.py").write_text(
                "tampered transition\n", encoding="utf-8"
            )
            with (
                mock.patch.object(MODULE, "fetch_bytes") as fetch,
                self.assertRaisesRegex(MODULE.DeployError, "Releasedatei weicht"),
            ):
                MODULE.verify_service(
                    release, host="127.0.0.1", port=8765, attempts=1
                )
            fetch.assert_not_called()

    def test_pre_profile_transition_sentinel_marker_upgrade_remains_supported(self):
        commit = "1" * 40
        with tempfile.TemporaryDirectory() as directory:
            deploy_root = pathlib.Path(directory)
            release = deploy_root / "releases" / commit
            self.write_release(release, commit)
            sentinel = release / MODULE.PROFILE_TRANSITION_RELEASE_SENTINEL
            self.assertFalse(sentinel.exists())
            self.assertTrue(
                set(MODULE.PROFILE_TRANSITION_CRITICAL_RELEASE_FILES).isdisjoint(
                    MODULE.critical_release_paths(release)
                )
            )
            marker_path = release / ".audio-control-release.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "audio_control_release",
                        "commit": commit,
                        "created_at_unix": marker["created_at_unix"],
                        "index_sha256": marker["index_sha256"],
                        "app_sha256": marker["app_sha256"],
                        "styles_sha256": marker["styles_sha256"],
                    }
                ),
                encoding="utf-8",
            )
            MODULE.switch_current(deploy_root, commit)
            expected_payloads = {
                relative: (release / relative).read_bytes()
                for relative in MODULE.critical_release_paths(release)
            }

            def git_readback(argv, **_kwargs):
                command = tuple(argv)
                if command[-1] == "--show-object-format":
                    return MODULE.CommandResult(command, 0, "sha1\n", "", 0.1)
                relative = command[-1].split(":", 1)[1]
                oid = MODULE.git_blob_oid(expected_payloads[relative], "sha1")
                return MODULE.CommandResult(command, 0, oid + "\n", "", 0.1)

            with mock.patch.object(MODULE, "run_command", side_effect=git_readback):
                receipt = MODULE.upgrade_current_release_marker(
                    pathlib.Path(directory) / "repository.git", deploy_root
                )
            self.assertTrue(receipt["changed"])
            upgraded = MODULE.verify_release_marker(release, expected_commit=commit)
            self.assertEqual(set(upgraded["critical_sha256"]), set(expected_payloads))

    def test_level_observer_runtime_files_are_release_critical(self):
        expected = set(MODULE.LEVEL_OBSERVER_CRITICAL_RELEASE_FILES)
        self.assertIn(
            "systemd/user/audio-control-level-observer-v1.service",
            MODULE.RUNTIME_FILES,
        )
        commit = "6" * 40
        for missing in sorted(expected):
            with self.subTest(missing=missing):
                with tempfile.TemporaryDirectory() as directory:
                    release = pathlib.Path(directory)
                    self.write_release(release, commit)
                    (release / missing).unlink()
                    with self.assertRaisesRegex(
                        MODULE.DeployError, "Kritische Releasedatei"
                    ):
                        MODULE.release_hashes(release)

    def test_qobuz_recovery_runtime_files_are_release_critical(self):
        expected = set(MODULE.QOBUZ_RECOVERY_CRITICAL_RELEASE_FILES)
        self.assertIn(
            "systemd/user/audio-qobuz-desktop-recovery-v1.service",
            MODULE.RUNTIME_FILES,
        )
        commit = "7" * 40
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            self.write_release(release, commit)
            self.assertIn(
                MODULE.QOBUZ_RECOVERY_RELEASE_SENTINEL,
                MODULE.critical_release_paths(release),
            )
        for missing in sorted(expected - {MODULE.QOBUZ_RECOVERY_RELEASE_SENTINEL}):
            with self.subTest(missing=missing):
                with tempfile.TemporaryDirectory() as directory:
                    release = pathlib.Path(directory)
                    self.write_release(release, commit)
                    (release / missing).unlink()
                    with self.assertRaisesRegex(
                        MODULE.DeployError, "Kritische Releasedatei"
                    ):
                        MODULE.release_hashes(release)

    def test_qbzd_qconnect_recovery_runtime_files_are_release_critical(self):
        expected = set(MODULE.QBZD_QCONNECT_RECOVERY_CRITICAL_RELEASE_FILES)
        self.assertIn(
            f"systemd/user/{MODULE.QBZD_QCONNECT_RECOVERY_UNIT}",
            MODULE.RUNTIME_FILES,
        )
        commit = "4" * 40
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            self.write_release(release, commit)
            sentinel = release / MODULE.QBZD_QCONNECT_RECOVERY_RELEASE_SENTINEL
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("import unittest\n", encoding="utf-8")
            self.assertIn(
                MODULE.QBZD_QCONNECT_RECOVERY_RELEASE_SENTINEL,
                MODULE.critical_release_paths(release),
            )
            MODULE.release_hashes(release)
            for missing in sorted(
                expected - {MODULE.QBZD_QCONNECT_RECOVERY_RELEASE_SENTINEL}
            ):
                with self.subTest(missing=missing):
                    target = release / missing
                    payload = target.read_bytes()
                    target.unlink()
                    with self.assertRaisesRegex(
                        MODULE.DeployError, "Kritische Releasedatei"
                    ):
                        MODULE.release_hashes(release)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)

    def test_pre_remote_bridge_marker_upgrade_remains_supported(self):
        commit = "5" * 40
        with tempfile.TemporaryDirectory() as directory:
            deploy_root = pathlib.Path(directory)
            release = deploy_root / "releases" / commit
            self.write_release(release, commit)
            for relative in MODULE.REMOTE_BRIDGE_CRITICAL_RELEASE_FILES:
                (release / relative).unlink()
            (release / MODULE.REMOTE_BRIDGE_RELEASE_SENTINEL).unlink()

            marker_path = release / ".audio-control-release.json"
            legacy = {
                "schema_version": 1,
                "kind": "audio_control_release",
                "commit": commit,
                "created_at_unix": 1,
                "index_sha256": MODULE.sha256_path(release / "ui" / "index.html"),
                "app_sha256": MODULE.sha256_path(release / "ui" / "app.js"),
                "styles_sha256": MODULE.sha256_path(release / "ui" / "styles.css"),
            }
            marker_path.write_text(json.dumps(legacy), encoding="utf-8")
            MODULE.switch_current(deploy_root, commit)
            expected_payloads = {
                relative: (release / relative).read_bytes()
                for relative in MODULE.critical_release_paths(release)
            }
            self.assertTrue(
                set(MODULE.REMOTE_BRIDGE_CRITICAL_RELEASE_FILES).isdisjoint(
                    expected_payloads
                )
            )

            def git_readback(argv, **_kwargs):
                command = tuple(argv)
                if command[-1] == "--show-object-format":
                    return MODULE.CommandResult(command, 0, "sha1\n", "", 0.1)
                relative = command[-1].split(":", 1)[1]
                oid = MODULE.git_blob_oid(expected_payloads[relative], "sha1")
                return MODULE.CommandResult(command, 0, oid + "\n", "", 0.1)

            with mock.patch.object(MODULE, "run_command", side_effect=git_readback):
                receipt = MODULE.upgrade_current_release_marker(
                    pathlib.Path(directory) / "repository.git", deploy_root
                )
            self.assertTrue(receipt["changed"])
            upgraded = MODULE.verify_release_marker(release, expected_commit=commit)
            self.assertEqual(set(upgraded["critical_sha256"]), set(expected_payloads))

    def test_pre_pwa_legacy_release_marker_upgrade_remains_supported(self):
        commit = "6" * 40
        with tempfile.TemporaryDirectory() as directory:
            deploy_root = pathlib.Path(directory)
            release = deploy_root / "releases" / commit
            self.write_release(release, commit)
            for relative in MODULE.PWA_CRITICAL_RELEASE_FILES:
                (release / relative).unlink()
            (release / MODULE.PWA_RELEASE_SENTINEL).unlink()

            marker_path = release / ".audio-control-release.json"
            legacy = {
                "schema_version": 1,
                "kind": "audio_control_release",
                "commit": commit,
                "created_at_unix": 1,
                "index_sha256": MODULE.sha256_path(release / "ui" / "index.html"),
                "app_sha256": MODULE.sha256_path(release / "ui" / "app.js"),
                "styles_sha256": MODULE.sha256_path(release / "ui" / "styles.css"),
            }
            marker_path.write_text(json.dumps(legacy), encoding="utf-8")
            MODULE.switch_current(deploy_root, commit)
            expected_payloads = {
                relative: (release / relative).read_bytes()
                for relative in MODULE.critical_release_paths(release)
            }
            self.assertTrue(
                set(MODULE.PWA_CRITICAL_RELEASE_FILES).isdisjoint(expected_payloads)
            )

            def git_readback(argv, **_kwargs):
                command = tuple(argv)
                if command[-1] == "--show-object-format":
                    return MODULE.CommandResult(command, 0, "sha1\n", "", 0.1)
                relative = command[-1].split(":", 1)[1]
                oid = MODULE.git_blob_oid(expected_payloads[relative], "sha1")
                return MODULE.CommandResult(command, 0, oid + "\n", "", 0.1)

            with mock.patch.object(MODULE, "run_command", side_effect=git_readback):
                receipt = MODULE.upgrade_current_release_marker(
                    pathlib.Path(directory) / "repository.git", deploy_root
                )
            self.assertTrue(receipt["changed"])
            upgraded = MODULE.verify_release_marker(release, expected_commit=commit)
            self.assertEqual(set(upgraded["critical_sha256"]), set(expected_payloads))

    def test_member_names_are_fail_closed(self):
        self.assertEqual(
            MODULE.validate_member_name("ui/index.html"), ("ui", "index.html")
        )
        for value in ("/etc/passwd", "../escape", "ui/../../escape"):
            with self.subTest(value=value), self.assertRaises(MODULE.DeployError):
                MODULE.validate_member_name(value)

    def test_current_pointer_is_commit_and_content_bound(self):
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            release = root / "releases" / commit
            self.write_release(release, commit)
            MODULE.switch_current(root, commit)
            self.assertEqual(MODULE.read_current_commit(root), commit)

            (release / "ui" / "app.js").write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(MODULE.DeployError):
                MODULE.read_current_commit(root)

    def test_legacy_marker_is_verified_for_existing_live_release(self):
        commit = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            self.write_release(release, commit)
            marker_path = release / ".audio-control-release.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            legacy = {
                "schema_version": 1,
                "kind": "audio_control_release",
                "commit": commit,
                "created_at_unix": marker["created_at_unix"],
                "index_sha256": marker["index_sha256"],
                "app_sha256": marker["app_sha256"],
                "styles_sha256": marker["styles_sha256"],
            }
            marker_path.write_text(json.dumps(legacy), encoding="utf-8")
            self.assertEqual(
                MODULE.verify_release_marker(release, expected_commit=commit)[
                    "schema_version"
                ],
                1,
            )
            (release / "ui" / "styles.css").write_text("tampered", encoding="utf-8")
            with self.assertRaises(MODULE.DeployError):
                MODULE.verify_release_marker(release, expected_commit=commit)

    def test_incomplete_marker_upgrade_is_git_blob_bound(self):
        commit = "c" * 40
        missing = {
            "scripts/audio_telemetry_replay.py",
            "inventory/audiozentrale-telemetry-replay.v1.json",
            "schemas/audiozentrale-telemetry-replay.v1.schema.json",
        }
        with tempfile.TemporaryDirectory() as directory:
            deploy_root = pathlib.Path(directory)
            release = deploy_root / "releases" / commit
            self.write_release(release, commit)
            marker_path = release / ".audio-control-release.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            for relative in missing:
                marker["critical_sha256"].pop(relative)
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            previous_sha256 = hashlib.sha256(marker_path.read_bytes()).hexdigest()
            MODULE.switch_current(deploy_root, commit)
            expected_payloads = {
                relative: (release / relative).read_bytes()
                for relative in MODULE.critical_release_paths(release)
            }

            def git_readback(argv, **_kwargs):
                command = tuple(argv)
                if command[-1] == "--show-object-format":
                    return MODULE.CommandResult(command, 0, "sha1\n", "", 0.1)
                relative = command[-1].split(":", 1)[1]
                oid = MODULE.git_blob_oid(expected_payloads[relative], "sha1")
                return MODULE.CommandResult(command, 0, oid + "\n", "", 0.1)

            with mock.patch.object(MODULE, "run_command", side_effect=git_readback):
                receipt = MODULE.upgrade_current_release_marker(
                    pathlib.Path(directory) / "repository.git", deploy_root
                )

            self.assertTrue(receipt["changed"])
            self.assertEqual(receipt["critical_file_count"], len(expected_payloads))
            upgraded = MODULE.verify_release_marker(release, expected_commit=commit)
            self.assertEqual(
                set(upgraded["critical_sha256"]), set(expected_payloads)
            )
            self.assertEqual(
                upgraded["upgraded_from_marker_sha256"], previous_sha256
            )

    def test_incomplete_marker_upgrade_rejects_unbound_file(self):
        commit = "d" * 40
        missing = "scripts/audio_telemetry_replay.py"
        with tempfile.TemporaryDirectory() as directory:
            deploy_root = pathlib.Path(directory)
            release = deploy_root / "releases" / commit
            self.write_release(release, commit)
            expected_payloads = {
                relative: (release / relative).read_bytes()
                for relative in MODULE.critical_release_paths(release)
            }
            marker_path = release / ".audio-control-release.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["critical_sha256"].pop(missing)
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            previous_marker = marker_path.read_bytes()
            (release / missing).write_bytes(b"tampered replay\n")
            MODULE.switch_current(deploy_root, commit)

            def git_readback(argv, **_kwargs):
                command = tuple(argv)
                if command[-1] == "--show-object-format":
                    return MODULE.CommandResult(command, 0, "sha1\n", "", 0.1)
                relative = command[-1].split(":", 1)[1]
                oid = MODULE.git_blob_oid(expected_payloads[relative], "sha1")
                return MODULE.CommandResult(command, 0, oid + "\n", "", 0.1)

            with (
                mock.patch.object(MODULE, "run_command", side_effect=git_readback),
                self.assertRaisesRegex(MODULE.DeployError, "Git-Blob"),
            ):
                MODULE.upgrade_current_release_marker(
                    pathlib.Path(directory) / "repository.git", deploy_root
                )
            self.assertEqual(marker_path.read_bytes(), previous_marker)

    def test_current_marker_upgrade_is_a_noop(self):
        commit = "e" * 40
        with tempfile.TemporaryDirectory() as directory:
            deploy_root = pathlib.Path(directory)
            release = deploy_root / "releases" / commit
            self.write_release(release, commit)
            MODULE.switch_current(deploy_root, commit)
            with mock.patch.object(MODULE, "run_command") as run:
                receipt = MODULE.upgrade_current_release_marker(
                    pathlib.Path(directory) / "repository.git", deploy_root
                )
            self.assertEqual(receipt["reason"], "marker-current")
            self.assertFalse(receipt["changed"])
            run.assert_not_called()

    def test_service_activation_fails_closed_when_stop_fails(self):
        daemon_reload = MODULE.CommandResult(
            ("systemctl", "--user", "daemon-reload"), 0, "", "", 0.1
        )
        with (
            mock.patch.object(MODULE, "run_command", return_value=daemon_reload) as run,
            mock.patch.object(
                MODULE,
                "service_command",
                side_effect=MODULE.DeployError("stop failed"),
            ) as service,
            self.assertRaises(MODULE.DeployError),
        ):
            MODULE.activate_service("audio-control-ui-v1.service")
        run.assert_called_once_with(
            ["systemctl", "--user", "daemon-reload"], timeout=30
        )
        service.assert_called_once_with("stop", "audio-control-ui-v1.service")

    @staticmethod
    def scoped_remote_bridge_health() -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "audio_remote_bridge_health",
            "status": "serving",
            "projection": "read-only-plus-scoped-actions",
            "effect_authority": True,
            "effect_scope": list(MODULE.REMOTE_BRIDGE_EFFECT_SCOPE),
            "effect_exclusions": [
                "profiles",
                "routing",
                "devices",
                "system",
            ],
            "allowed_methods": ["GET", "HEAD", "POST"],
            "remote_action": {
                "session_route": "/bridge/v1/session",
                "action_route": "/bridge/v1/actions/whale",
                "recording_action_route": "/bridge/v1/actions/recording",
                "session_ttl_seconds": 900,
                "token_header": "X-Audio-Bridge-Session",
                "backend_token_exposed": False,
                "tailscale_identity_required": True,
                "session_identity_bound": True,
            },
            "backend": {
                "host": "127.0.0.1",
                "port": 8765,
                "remote_exposure": False,
            },
        }

    def test_remote_bridge_effect_scope_matches_release_contract(self):
        contract = json.loads(
            (ROOT / "inventory" / "audiozentrale-remote-bridge.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            MODULE.REMOTE_BRIDGE_EFFECT_SCOPE, contract["bridge"]["effect_scope"]
        )
        self.assertEqual(
            self.scoped_remote_bridge_health()["effect_scope"],
            MODULE.REMOTE_BRIDGE_EFFECT_SCOPE,
        )

    def test_remote_bridge_verifier_accepts_only_scoped_action_health(self):
        payload = self.scoped_remote_bridge_health()
        body = json.dumps(payload).encode("utf-8")

        class Response:
            status = 200

            def read(self, _limit):
                return body

            def getheader(self, name, default=""):
                return "read-only-v1" if name == "X-Audio-Remote-Bridge" else default

        class Connection:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                pass

        with mock.patch.object(MODULE.http.client, "HTTPConnection", Connection):
            report = MODULE.verify_remote_bridge(attempts=1)
        self.assertEqual(report["marker"], "read-only-v1")
        self.assertEqual(report["health"], payload)

        cases = {
            "legacy-read-only": {
                **payload,
                "projection": "read-only",
                "effect_authority": False,
            },
            "scope-expanded": {
                **payload,
                "effect_scope": [*payload["effect_scope"], "routing:apply"],
            },
            "exclusions-weakened": {
                **payload,
                "effect_exclusions": ["profiles", "routing", "devices"],
            },
            "backend-token-exposed": {
                **payload,
                "remote_action": {
                    **payload["remote_action"],
                    "backend_token_exposed": True,
                },
            },
            "identity-unbound": {
                **payload,
                "remote_action": {
                    **payload["remote_action"],
                    "session_identity_bound": False,
                },
            },
            "backend-exposed": {
                **payload,
                "backend": {**payload["backend"], "remote_exposure": True},
            },
        }
        for name, candidate in cases.items():
            with self.subTest(name=name):
                self.assertIsNotNone(
                    MODULE.remote_bridge_health_error("read-only-v1", candidate)
                )

    def test_missing_remote_bridge_unit_is_inactive_not_a_deploy_blocker(self):
        result = MODULE.CommandResult(
            ("systemctl", "--user", "show", MODULE.REMOTE_BRIDGE_UNIT),
            4,
            "LoadState=not-found\nActiveState=inactive\n",
            "Unit could not be found",
            0.1,
        )
        with mock.patch.object(MODULE, "run_command", return_value=result):
            activity = MODULE.read_service_activity(MODULE.REMOTE_BRIDGE_UNIT)
        self.assertFalse(activity["active"])
        self.assertEqual(activity["active_state"], "not-found")
        self.assertEqual(activity["load_state"], "not-found")

    def test_raw_service_readback_preserves_transitional_state(self):
        result = MODULE.CommandResult(
            ("systemctl", "--user", "show", MODULE.QOBUZ_RECOVERY_UNIT),
            0,
            "LoadState=loaded\nActiveState=activating\nSubState=auto-restart\n",
            "",
            0.1,
        )
        with mock.patch.object(MODULE, "run_command", return_value=result):
            activity = MODULE.read_service_activity_raw(MODULE.QOBUZ_RECOVERY_UNIT)
        self.assertEqual(activity["active_state"], "activating")
        self.assertEqual(activity["sub_state"], "auto-restart")

    def test_service_readback_binds_html_javascript_css_and_health(self):
        commit = "c" * 40
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            self.write_release(release, commit)
            payloads = {
                "/": (
                    200,
                    (release / "ui" / "index.html").read_bytes(),
                    "text/html; charset=utf-8",
                ),
                "/app.js": (
                    200,
                    (release / "ui" / "app.js").read_bytes(),
                    "application/javascript; charset=utf-8",
                ),
                "/styles.css": (
                    200,
                    (release / "ui" / "styles.css").read_bytes(),
                    "text/css; charset=utf-8",
                ),
                "/sw.js": (
                    200,
                    (release / "ui" / "sw.js").read_bytes(),
                    "application/javascript; charset=utf-8",
                ),
                "/manifest.webmanifest": (
                    200,
                    (release / "ui" / "manifest.webmanifest").read_bytes(),
                    "application/manifest+json",
                ),
                "/icon-180.png": (200, (release / "ui" / "icon-180.png").read_bytes(), "image/png"),
                "/icon-192.png": (200, (release / "ui" / "icon-192.png").read_bytes(), "image/png"),
                "/icon-512.png": (200, (release / "ui" / "icon-512.png").read_bytes(), "image/png"),
                "/whale-lesson.js": (
                    200,
                    (release / "ui" / "whale-lesson.js").read_bytes(),
                    "application/javascript; charset=utf-8",
                ),
                "/whale-learning-reference.wav": (
                    200,
                    (release / "ui" / "whale-learning-reference.wav").read_bytes(),
                    "audio/wav",
                ),
                "/api/v1/health": (
                    200,
                    json.dumps(
                        {
                            "status": "serving",
                            "authority": "local-backend",
                            "runtime_head": commit,
                        }
                    ).encode(),
                    "application/json",
                ),
            }
            with mock.patch.object(
                MODULE,
                "fetch_bytes",
                side_effect=lambda _host, _port, path: payloads[path],
            ):
                report = MODULE.verify_service(
                    release, host="127.0.0.1", port=8765, attempts=1
                )
            self.assertEqual(
                set(report["static_sha256"]),
                {
                    "/",
                    "/app.js",
                    "/styles.css",
                    "/sw.js",
                    "/manifest.webmanifest",
                    "/icon-180.png",
                    "/icon-192.png",
                    "/icon-512.png",
                    "/whale-lesson.js",
                    "/whale-learning-reference.wav",
                },
            )

            payloads["/app.js"] = (200, b"stale", "application/javascript")
            with (
                mock.patch.object(
                    MODULE,
                    "fetch_bytes",
                    side_effect=lambda _host, _port, path: payloads[path],
                ),
                self.assertRaises(MODULE.DeployError),
            ):
                MODULE.verify_service(
                    release, host="127.0.0.1", port=8765, attempts=1
                )

            payloads["/app.js"] = (
                200,
                (release / "ui" / "app.js").read_bytes(),
                "application/javascript; charset=utf-8",
            )
            payloads["/api/v1/health"] = (
                200,
                json.dumps(
                    {
                        "status": "serving",
                        "authority": "local-backend",
                        "runtime_head": "d" * 40,
                    }
                ).encode(),
                "application/json",
            )
            with (
                mock.patch.object(
                    MODULE,
                    "fetch_bytes",
                    side_effect=lambda _host, _port, path: payloads[path],
                ),
                self.assertRaises(MODULE.DeployError),
            ):
                MODULE.verify_service(
                    release, host="127.0.0.1", port=8765, attempts=1
                )

    def test_validate_release_requires_and_checks_the_pwa_surface(self):
        commit = "7" * 40
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            self.write_release(release, commit)
            qbzd_sentinel = release / MODULE.QBZD_QCONNECT_RECOVERY_RELEASE_SENTINEL
            qbzd_sentinel.parent.mkdir(parents=True, exist_ok=True)
            qbzd_sentinel.write_text("import unittest\n", encoding="utf-8")

            def successful(argv, **_kwargs):
                command = tuple(str(item) for item in argv)
                return MODULE.CommandResult(command, 0, "", "", 0.01)

            with (
                mock.patch.object(MODULE, "run_command", side_effect=successful) as run,
                mock.patch.object(MODULE.shutil, "which", return_value="/usr/bin/node"),
            ):
                MODULE.validate_release(release)

            commands = [tuple(call.args[0]) for call in run.call_args_list]
            self.assertIn(
                (sys.executable, "-m", "unittest", "tests/test_audio_ipad_pwa.py"),
                commands,
            )
            self.assertIn(
                (sys.executable, "scripts/qbzd_qconnect_recovery.py", "check"),
                commands,
            )
            self.assertIn(
                (
                    sys.executable,
                    "-m",
                    "unittest",
                    "tests/test_qbzd_qconnect_recovery.py",
                ),
                commands,
            )
            self.assertIn(("/usr/bin/node", "--check", "ui/sw.js"), commands)

            (release / "ui" / "manifest.webmanifest").unlink()
            with self.assertRaisesRegex(MODULE.DeployError, "Release ist unvollständig"):
                MODULE.validate_release(release)

    def test_target_fetch_uses_private_bare_repository_and_redacts_remote(self):
        commit = "9" * 40
        repository = pathlib.Path("/state/audio-control-deploy/repository.git")
        results = [
            MODULE.CommandResult(("git", "check-ref-format"), 0, "main\n", "", 0.1),
            MODULE.CommandResult(("git", "fetch"), 0, "", "", 0.2),
            MODULE.CommandResult(("git", "rev-parse"), 0, commit + "\n", "", 0.1),
        ]
        with mock.patch.object(MODULE, "run_command", side_effect=results) as run:
            observed, receipts = MODULE.resolve_target(repository, branch="main")
        self.assertEqual(observed, commit)
        fetch_argv = run.call_args_list[1].args[0]
        self.assertIn("--git-dir", fetch_argv)
        self.assertIn(str(repository), fetch_argv)
        self.assertNotIn("-C", fetch_argv)
        self.assertIn("--no-write-fetch-head", fetch_argv)
        self.assertEqual(len(receipts), 3)

        command = MODULE.CommandResult(
            ("git", "--git-dir", "/state/repo.git", "remote", "add", "source", "ssh://private"),
            0,
            "",
            "",
            0.1,
        )
        self.assertEqual(command.receipt(redact_indexes={6})["argv"][6], "<redacted>")
        with self.assertRaises(MODULE.DeployError):
            MODULE.validate_remote_url("https://user:secret@example.invalid/repo.git")
        with self.assertRaises(MODULE.DeployError):
            MODULE.validate_remote_url("file:///tmp/repo.git")

    def test_unchanged_healthy_release_does_not_restart_every_timer_tick(self):
        commit = "d" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            release = args.deploy_root / "releases" / commit
            release.mkdir(parents=True)
            service = {"url": "http://127.0.0.1:8765/"}
            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE,
                    "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
                mock.patch.object(MODULE, "read_current_commit", return_value=commit),
                mock.patch.object(
                    MODULE,
                    "prepare_release",
                    return_value=(release, [], False),
                ),
                mock.patch.object(
                    MODULE,
                    "reconcile_runtime_environment",
                    return_value=(
                        {"changed": False, "host": "127.0.0.1", "port": 8765},
                        None,
                    ),
                ),
                mock.patch.object(MODULE, "verify_service", return_value=service),
                mock.patch.object(MODULE, "activate_service") as activate,
                mock.patch.object(
                    MODULE,
                    "prune_releases",
                    return_value={"keep": 3, "removed": [], "warnings": []},
                ),
            ):
                report = MODULE.sync(args)
            self.assertFalse(report["changed"])
            self.assertEqual(report["service_commands"], [])
            self.assertFalse(report["runtime_environment"]["changed"])
            activate.assert_not_called()

    def test_qobuz_recovery_introduction_converges_on_new_deployer_second_pass(self):
        commit = "9" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            release = root / "release"
            self.write_release(release, commit)
            runtime_only = {
                "scripts/audio_control_deploy.py": b"print('deployer')\n",
                "systemd/user/audio-control-deploy.service": b"[Service]\nExecStart=/usr/bin/true\n",
                "systemd/user/audio-control-deploy.timer": b"[Timer]\nOnActiveSec=60\n",
            }
            for relative, payload in runtime_only.items():
                target = release / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            destinations = root / "runtime"
            full_mapping = {
                relative: (destinations / pathlib.Path(relative).name, mode)
                for relative, (_destination, mode) in MODULE.RUNTIME_FILES.items()
            }
            recovery_relative = f"systemd/user/{MODULE.QOBUZ_RECOVERY_UNIT}"
            recovery_destination = full_mapping[recovery_relative][0]
            old_mapping = {
                relative: binding
                for relative, binding in full_mapping.items()
                if relative != recovery_relative
            }
            command = MODULE.CommandResult(
                ("systemd-analyze", "--user", "verify"), 0, "", "", 0.1
            )

            # The historical first invocation can install only its old mapping.
            with (
                mock.patch.object(MODULE, "RUNTIME_FILES", old_mapping),
                mock.patch.object(MODULE, "run_command", return_value=command),
            ):
                MODULE.install_release_runtime(release)
            self.assertFalse(recovery_destination.exists())

            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            active = {"value": False}

            def activity(unit):
                self.assertEqual(unit, MODULE.QOBUZ_RECOVERY_UNIT)
                loaded = recovery_destination.exists()
                return {
                    "unit": unit,
                    "load_state": "loaded" if loaded else "not-found",
                    "active": loaded and active["value"],
                    "active_state": "active" if active["value"] else "inactive",
                    "readback": {},
                }

            def service(action, unit, **_kwargs):
                self.assertEqual((action, unit), ("start", MODULE.QOBUZ_RECOVERY_UNIT))
                active["value"] = True
                return MODULE.CommandResult(
                    ("systemctl", "--user", action, unit), 0, "", "", 0.1
                )

            with (
                mock.patch.object(MODULE, "RUNTIME_FILES", full_mapping),
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(MODULE, "prepare_deployment_repository", return_value=(root / "repository.git", [])),
                mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
                mock.patch.object(MODULE, "upgrade_current_release_marker", return_value={"changed": False}),
                mock.patch.object(MODULE, "read_current_commit", return_value=commit),
                mock.patch.object(MODULE, "prepare_release", return_value=(release, [], False)),
                mock.patch.object(MODULE, "reconcile_runtime_environment", return_value=({"changed": False}, None)),
                mock.patch.object(MODULE, "run_command", return_value=command),
                mock.patch.object(MODULE, "activate_service", return_value=[]),
                mock.patch.object(MODULE, "verify_service", return_value={"url": "http://127.0.0.1:8765/"}),
                mock.patch.object(MODULE, "read_service_activity", side_effect=activity),
                mock.patch.object(MODULE, "service_command", side_effect=service),
                mock.patch.object(MODULE, "prune_releases", return_value={"keep": 3, "removed": [], "warnings": []}),
            ):
                report = MODULE.sync(args)

            self.assertTrue(recovery_destination.is_file())
            self.assertTrue(report["qobuz_recovery"]["active"])
            self.assertEqual(len(report["qobuz_recovery"]["activation"]), 1)

    def test_qbzd_qconnect_recovery_converges_inactive_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            sentinel = release / MODULE.QBZD_QCONNECT_RECOVERY_RELEASE_SENTINEL
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("recovery\n", encoding="utf-8")
            inactive = {
                "unit": MODULE.QBZD_QCONNECT_RECOVERY_UNIT,
                "load_state": "loaded",
                "active": False,
                "active_state": "inactive",
            }
            active = {
                "unit": MODULE.QBZD_QCONNECT_RECOVERY_UNIT,
                "load_state": "loaded",
                "active": True,
                "active_state": "active",
            }
            command = MODULE.CommandResult(
                (
                    "systemctl",
                    "--user",
                    "start",
                    MODULE.QBZD_QCONNECT_RECOVERY_UNIT,
                ),
                0,
                "",
                "",
                0.1,
            )
            with (
                mock.patch.object(
                    MODULE,
                    "read_service_activity",
                    side_effect=[inactive, active],
                ),
                mock.patch.object(
                    MODULE, "service_command", return_value=command
                ) as service,
            ):
                result = MODULE.converge_qbzd_qconnect_recovery(release)
            service.assert_called_once_with(
                "start", MODULE.QBZD_QCONNECT_RECOVERY_UNIT
            )
            self.assertTrue(result["active"])
            self.assertEqual(result["active_state"], "active")
            self.assertEqual(len(result["activation"]), 1)

    def test_failed_candidate_stops_qbzd_recovery_before_rollback(self):
        commit = "3" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            release = args.deploy_root / "releases" / commit
            release.mkdir(parents=True)
            sentinel = release / MODULE.QBZD_QCONNECT_RECOVERY_RELEASE_SENTINEL
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("recovery\n", encoding="utf-8")
            events = []

            def stop_qbzd():
                events.append("stop-qbzd-recovery")
                return []

            def restore(_backups):
                events.append("restore-runtime")

            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE,
                    "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
                mock.patch.object(
                    MODULE,
                    "upgrade_current_release_marker",
                    return_value={"changed": False},
                ),
                mock.patch.object(MODULE, "read_current_commit", return_value=commit),
                mock.patch.object(
                    MODULE, "prepare_release", return_value=(release, [], False)
                ),
                mock.patch.object(
                    MODULE, "release_supports_remote_bridge", return_value=False
                ),
                mock.patch.object(
                    MODULE, "release_supports_qobuz_recovery", return_value=False
                ),
                mock.patch.object(
                    MODULE,
                    "reconcile_runtime_environment",
                    return_value=({"changed": False}, None),
                ),
                mock.patch.object(
                    MODULE,
                    "install_release_runtime",
                    return_value=([{"source": "systemd/user/fake.service"}], [{"path": "fake"}]),
                ),
                mock.patch.object(
                    MODULE,
                    "verify_service",
                    side_effect=MODULE.DeployError("candidate UI unhealthy"),
                ),
                mock.patch.object(MODULE, "activate_service", return_value=[]),
                mock.patch.object(
                    MODULE,
                    "stop_qbzd_qconnect_recovery_for_rollback",
                    side_effect=stop_qbzd,
                ) as stop,
                mock.patch.object(
                    MODULE, "restore_release_runtime", side_effect=restore
                ),
                mock.patch.object(MODULE, "restore_runtime_environment"),
                mock.patch.object(MODULE, "read_service_activity_raw", return_value={
                    "load_state": "not-found",
                    "active": False,
                    "active_state": "not-found",
                    "sub_state": "dead",
                    "readback": {},
                }),
                self.assertRaises(MODULE.DeployError),
            ):
                MODULE.sync(args)
            stop.assert_called_once_with()
            self.assertIn("restore-runtime", events)
            self.assertLess(
                events.index("stop-qbzd-recovery"),
                events.index("restore-runtime"),
            )

    def test_qobuz_recovery_convergence_fails_inactive_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            sentinel = release / MODULE.QOBUZ_RECOVERY_RELEASE_SENTINEL
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("recovery\n", encoding="utf-8")
            inactive = {
                "load_state": "loaded",
                "active": False,
                "active_state": "inactive",
            }
            command = MODULE.CommandResult(
                ("systemctl", "--user", "start", MODULE.QOBUZ_RECOVERY_UNIT),
                0, "", "", 0.1,
            )
            with (
                mock.patch.object(MODULE, "read_service_activity", return_value=inactive),
                mock.patch.object(MODULE, "service_command", return_value=command),
                self.assertRaisesRegex(MODULE.DeployError, "nicht aktiv"),
            ):
                MODULE.converge_qobuz_recovery(release)

    def test_failed_release_stops_new_recovery_before_runtime_rollback(self):
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            release = args.deploy_root / "releases" / commit
            sentinel = release / MODULE.QOBUZ_RECOVERY_RELEASE_SENTINEL
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("recovery\n", encoding="utf-8")
            recovery = root / "runtime" / MODULE.QOBUZ_RECOVERY_UNIT
            update = {
                "source": f"systemd/user/{MODULE.QOBUZ_RECOVERY_UNIT}",
                "destination": str(recovery),
                "sha256": "b" * 64,
                "mode": "0o600",
            }
            backup = {"path": str(recovery), "payload": None, "mode": None}
            events = []
            active = {"value": False}

            def install(_release):
                recovery.parent.mkdir(parents=True, exist_ok=True)
                recovery.write_text("[Service]\nExecStart=/usr/bin/true\n", encoding="utf-8")
                recovery.chmod(0o600)
                return [update], [backup]

            def activity(_unit):
                loaded = recovery.exists()
                events.append(f"read:{active['value']}:{loaded}")
                return {
                    "load_state": "loaded" if loaded else "not-found",
                    "active": active["value"],
                    "active_state": "active" if active["value"] else "inactive",
                }

            def service(action, unit, **_kwargs):
                self.assertEqual(unit, MODULE.QOBUZ_RECOVERY_UNIT)
                events.append(action)
                active["value"] = action != "stop"
                return MODULE.CommandResult(("systemctl", "--user", action, unit), 0, "", "", 0.1)

            def raw_activity(_unit):
                return {
                    "load_state": "loaded",
                    "active": active["value"],
                    "active_state": "active" if active["value"] else "inactive",
                    "sub_state": "running" if active["value"] else "dead",
                    "readback": {},
                }

            def run(argv, **_kwargs):
                events.append("reload")
                self.assertFalse(recovery.exists())
                return MODULE.CommandResult(tuple(argv), 0, "", "", 0.1)

            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(MODULE, "prepare_deployment_repository", return_value=(root / "repository.git", [])),
                mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
                mock.patch.object(MODULE, "upgrade_current_release_marker", return_value={"changed": False}),
                mock.patch.object(MODULE, "read_current_commit", return_value=commit),
                mock.patch.object(MODULE, "prepare_release", return_value=(release, [], False)),
                mock.patch.object(MODULE, "reconcile_runtime_environment", return_value=({"changed": False}, None)),
                mock.patch.object(MODULE, "install_release_runtime", side_effect=install),
                mock.patch.object(MODULE, "activate_service", return_value=[]),
                mock.patch.object(MODULE, "verify_service", return_value={"url": "http://127.0.0.1:8765/"}),
                mock.patch.object(MODULE, "read_service_activity", side_effect=activity),
                mock.patch.object(MODULE, "read_service_activity_raw", side_effect=raw_activity),
                mock.patch.object(MODULE, "service_command", side_effect=service),
                mock.patch.object(MODULE, "run_command", side_effect=run),
                mock.patch.object(MODULE, "prune_releases", side_effect=MODULE.DeployError("forced readback failure")),
                self.assertRaisesRegex(
                    MODULE.DeployError,
                    "Rollback war unvollständig.*Qobuz-Recovery-Unit ist nicht installiert",
                ),
            ):
                MODULE.sync(args)

            self.assertFalse(recovery.exists())
            self.assertFalse(active["value"])
            self.assertLess(events.index("stop"), events.index("reload"))

    def test_implicit_ui_wants_start_is_stopped_before_pointer_rollback(self):
        previous = "3" * 40
        commit = "4" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            previous_release = args.deploy_root / "releases" / previous
            previous_release.mkdir(parents=True)
            release = args.deploy_root / "releases" / commit
            self.write_release(release, commit)
            active = {"value": False}
            events = []

            def switch(_root, target):
                events.append(f"pointer:{target}")

            def activate(_unit):
                events.append("ui")
                if events.count("ui") == 1:
                    active["value"] = True
                    events.append("implicit-recovery-start")
                return []

            def raw_activity(_unit):
                return {
                    "load_state": "loaded",
                    "active": active["value"],
                    "active_state": "active" if active["value"] else "inactive",
                    "sub_state": "running" if active["value"] else "dead",
                    "readback": {},
                }

            def service(action, unit, **_kwargs):
                self.assertEqual((action, unit), ("stop", MODULE.QOBUZ_RECOVERY_UNIT))
                events.append("stop-recovery")
                active["value"] = False
                return MODULE.CommandResult(
                    ("systemctl", "--user", action, unit), 0, "", "", 0.1
                )

            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE,
                    "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
                mock.patch.object(
                    MODULE, "upgrade_current_release_marker", return_value={"changed": False}
                ),
                mock.patch.object(MODULE, "read_current_commit", return_value=previous),
                mock.patch.object(
                    MODULE, "prepare_release", return_value=(release, [], False)
                ),
                mock.patch.object(
                    MODULE, "release_supports_remote_bridge", return_value=False
                ),
                mock.patch.object(MODULE, "switch_current", side_effect=switch),
                mock.patch.object(
                    MODULE,
                    "reconcile_runtime_environment",
                    return_value=({"changed": False}, None),
                ),
                mock.patch.object(MODULE, "install_release_runtime", return_value=([], [])),
                mock.patch.object(MODULE, "activate_service", side_effect=activate),
                mock.patch.object(
                    MODULE,
                    "verify_service",
                    side_effect=MODULE.DeployError("candidate UI unhealthy"),
                ),
                mock.patch.object(
                    MODULE, "read_service_activity_raw", side_effect=raw_activity
                ),
                mock.patch.object(MODULE, "service_command", side_effect=service),
                mock.patch.object(MODULE, "converge_qobuz_recovery") as converge,
                self.assertRaisesRegex(MODULE.DeployError, "candidate UI unhealthy"),
            ):
                MODULE.sync(args)

            converge.assert_not_called()
            self.assertFalse(active["value"])
            self.assertLess(
                events.index("implicit-recovery-start"), events.index("stop-recovery")
            )
            self.assertLess(
                events.index("stop-recovery"), events.index(f"pointer:{previous}")
            )

    def test_script_only_candidate_restart_is_stopped_before_pointer_rollback(self):
        previous = "1" * 40
        commit = "2" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            previous_release = args.deploy_root / "releases" / previous
            release = args.deploy_root / "releases" / commit
            self.write_release(previous_release, previous)
            self.write_release(release, commit)
            (release / "scripts" / "qobuz_desktop_recovery.py").write_bytes(
                b"print('candidate recovery')\n"
            )
            active = {"value": True}
            events = []

            def switch(_root, target):
                events.append(f"pointer:{target}")

            def activity(unit):
                self.assertEqual(unit, MODULE.QOBUZ_RECOVERY_UNIT)
                return {
                    "unit": unit,
                    "load_state": "loaded",
                    "active": active["value"],
                    "active_state": "active" if active["value"] else "inactive",
                    "sub_state": "running" if active["value"] else "dead",
                    "readback": {},
                }

            def service(action, unit, **_kwargs):
                self.assertEqual(unit, MODULE.QOBUZ_RECOVERY_UNIT)
                events.append(action)
                active["value"] = action != "stop"
                return MODULE.CommandResult(
                    ("systemctl", "--user", action, unit), 0, "", "", 0.1
                )

            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE,
                    "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
                mock.patch.object(
                    MODULE,
                    "upgrade_current_release_marker",
                    return_value={"changed": False},
                ),
                mock.patch.object(MODULE, "read_current_commit", return_value=previous),
                mock.patch.object(
                    MODULE, "prepare_release", return_value=(release, [], False)
                ),
                mock.patch.object(MODULE, "release_supports_remote_bridge", return_value=False),
                mock.patch.object(MODULE, "switch_current", side_effect=switch),
                mock.patch.object(
                    MODULE,
                    "reconcile_runtime_environment",
                    return_value=({"changed": False}, None),
                ),
                mock.patch.object(MODULE, "install_release_runtime", return_value=([], [])),
                mock.patch.object(MODULE, "activate_service", return_value=[]),
                mock.patch.object(
                    MODULE,
                    "verify_service",
                    return_value={"url": "http://127.0.0.1:8765/"},
                ),
                mock.patch.object(MODULE, "read_service_activity", side_effect=activity),
                mock.patch.object(MODULE, "read_service_activity_raw", side_effect=activity),
                mock.patch.object(MODULE, "service_command", side_effect=service),
                mock.patch.object(
                    MODULE,
                    "prune_releases",
                    side_effect=MODULE.DeployError("forced postflight failure"),
                ),
                self.assertRaisesRegex(MODULE.DeployError, "forced postflight failure"),
            ):
                MODULE.sync(args)

            self.assertEqual(events[0], f"pointer:{commit}")
            self.assertLess(events.index("restart"), events.index("stop"))
            self.assertLess(events.index("stop"), events.index(f"pointer:{previous}"))
            self.assertTrue(active["value"])
            self.assertLess(events.index(f"pointer:{previous}"), events.index("start"))

    def test_rollback_fails_closed_when_previous_recovery_does_not_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            error, converge, previous_release = self.failed_candidate_rollback(
                pathlib.Path(directory),
                previous="5" * 40,
                commit="6" * 40,
                previous_supports_recovery=True,
                previous_recovery_error=MODULE.DeployError(
                    "previous recovery inactive"
                ),
            )
            self.assertRegex(
                str(error), "Rollback war unvollständig.*previous recovery inactive"
            )
            converge.assert_called_once_with(previous_release)

    def test_rollback_succeeds_when_previous_recovery_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            error, converge, previous_release = self.failed_candidate_rollback(
                pathlib.Path(directory),
                previous="7" * 40,
                commit="8" * 40,
                previous_supports_recovery=True,
            )
            self.assertRegex(str(error), "candidate UI unhealthy")
            converge.assert_called_once_with(previous_release)

    def test_rollback_accepts_legacy_previous_release_without_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            error, converge, _previous_release = self.failed_candidate_rollback(
                pathlib.Path(directory),
                previous="9" * 40,
                commit="a" * 40,
                previous_supports_recovery=False,
            )
            self.assertRegex(str(error), "candidate UI unhealthy")
            converge.assert_not_called()

    def test_rollback_fails_closed_if_recovery_cannot_be_stopped(self):
        active = {
            "load_state": "loaded",
            "active": False,
            "active_state": "activating",
            "sub_state": "auto-restart",
            "readback": {},
        }
        command = MODULE.CommandResult(
            ("systemctl", "--user", "stop", MODULE.QOBUZ_RECOVERY_UNIT),
            0, "", "", 0.1,
        )
        with (
            mock.patch.object(MODULE, "read_service_activity_raw", return_value=active),
            mock.patch.object(MODULE, "service_command", return_value=command),
            self.assertRaisesRegex(MODULE.DeployError, "nicht sicher stoppen"),
        ):
            MODULE.stop_qobuz_recovery_for_rollback(sleeper=lambda _seconds: None)

    def test_rollback_stopper_converges_transitional_and_auto_restart_states(self):
        inactive = {
            "load_state": "loaded",
            "active": False,
            "active_state": "inactive",
            "sub_state": "dead",
            "readback": {},
        }
        command = MODULE.CommandResult(
            ("systemctl", "--user", "stop", MODULE.QOBUZ_RECOVERY_UNIT),
            0, "", "", 0.1,
        )
        for active_state, sub_state in (
            ("activating", "start"),
            ("deactivating", "stop-sigterm"),
            ("activating", "auto-restart"),
        ):
            with self.subTest(active_state=active_state, sub_state=sub_state):
                transitional = {
                    "load_state": "loaded",
                    "active": False,
                    "active_state": active_state,
                    "sub_state": sub_state,
                    "readback": {},
                }
                with (
                    mock.patch.object(
                        MODULE,
                        "read_service_activity_raw",
                        side_effect=[transitional, transitional, inactive],
                    ),
                    mock.patch.object(
                        MODULE, "service_command", return_value=command
                    ) as stop,
                ):
                    MODULE.stop_qobuz_recovery_for_rollback(
                        sleeper=lambda _seconds: None
                    )
                stop.assert_called_once_with("stop", MODULE.QOBUZ_RECOVERY_UNIT)

    def test_unchanged_release_reconciles_stale_deploy_runtime(self):
        commit = "4" * 40
        update = {
            "source": "scripts/audio_control_deploy.py",
            "destination": "/tmp/audio-control-deploy.py",
            "sha256": "5" * 64,
            "mode": "0o700",
        }
        backup = {
            "path": update["destination"],
            "payload": b"old",
            "mode": 0o700,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            release = args.deploy_root / "releases" / commit
            release.mkdir(parents=True)
            service = {"url": "http://127.0.0.1:8765/"}
            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE,
                    "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
                mock.patch.object(MODULE, "read_current_commit", return_value=commit),
                mock.patch.object(
                    MODULE,
                    "prepare_release",
                    return_value=(release, [], False),
                ),
                mock.patch.object(
                    MODULE,
                    "reconcile_runtime_environment",
                    return_value=(
                        {"changed": False, "host": "127.0.0.1", "port": 8765},
                        None,
                    ),
                ),
                mock.patch.object(
                    MODULE,
                    "install_release_runtime",
                    return_value=([update], [backup]),
                ) as install_runtime,
                mock.patch.object(MODULE, "verify_service", return_value=service),
                mock.patch.object(MODULE, "activate_service") as activate,
                mock.patch.object(
                    MODULE,
                    "prune_releases",
                    return_value={"keep": 3, "removed": [], "warnings": []},
                ),
            ):
                report = MODULE.sync(args)
            self.assertFalse(report["changed"])
            self.assertEqual(report["runtime_updates"], [update])
            install_runtime.assert_called_once_with(release)
            activate.assert_not_called()

    def test_unchanged_dauersong_dropin_drift_triggers_daemon_reload(self):
        commit = "d" * 40
        update = {
            "source": "systemd/user/grabowski-dauersong.service.d/zz-audio-control-v1.conf",
            "destination": "/tmp/zz-audio-control-v1.conf",
            "sha256": "e" * 64,
            "mode": "0o600",
        }
        backup = {"path": update["destination"], "payload": b"old", "mode": 0o600}
        daemon_reload = MODULE.CommandResult(
            ("systemctl", "--user", "daemon-reload"), 0, "", "", 0.1
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            release = args.deploy_root / "releases" / commit
            release.mkdir(parents=True)
            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE, "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
                mock.patch.object(MODULE, "read_current_commit", return_value=commit),
                mock.patch.object(
                    MODULE, "prepare_release", return_value=(release, [], False)
                ),
                mock.patch.object(
                    MODULE, "reconcile_runtime_environment",
                    return_value=({"changed": False, "host": "127.0.0.1", "port": 8765}, None),
                ),
                mock.patch.object(
                    MODULE, "install_release_runtime", return_value=([update], [backup])
                ),
                mock.patch.object(MODULE, "run_command", return_value=daemon_reload) as run,
                mock.patch.object(MODULE, "activate_service") as activate,
                mock.patch.object(
                    MODULE, "verify_service", return_value={"url": "http://127.0.0.1:8765/"}
                ),
                mock.patch.object(
                    MODULE, "prune_releases",
                    return_value={"keep": 3, "removed": [], "warnings": []},
                ),
            ):
                report = MODULE.sync(args)
        self.assertFalse(report["changed"])
        self.assertEqual(report["runtime_updates"], [update])
        activate.assert_not_called()
        run.assert_called_once_with(
            ["systemctl", "--user", "daemon-reload"], timeout=30
        )

    def test_unchanged_ui_unit_drift_restarts_service(self):
        commit = "6" * 40
        update = {
            "source": "systemd/user/audio-control-ui-v1.service",
            "destination": "/tmp/audio-control-ui-v1.service",
            "sha256": "7" * 64,
            "mode": "0o600",
        }
        backup = {
            "path": update["destination"],
            "payload": b"old-unit",
            "mode": 0o600,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            release = args.deploy_root / "releases" / commit
            release.mkdir(parents=True)
            service = {"url": "http://127.0.0.1:8765/"}
            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE,
                    "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
                mock.patch.object(MODULE, "read_current_commit", return_value=commit),
                mock.patch.object(
                    MODULE,
                    "prepare_release",
                    return_value=(release, [], False),
                ),
                mock.patch.object(
                    MODULE,
                    "reconcile_runtime_environment",
                    return_value=(
                        {"changed": False, "host": "127.0.0.1", "port": 8765},
                        None,
                    ),
                ),
                mock.patch.object(
                    MODULE,
                    "install_release_runtime",
                    return_value=([update], [backup]),
                ),
                mock.patch.object(
                    MODULE, "activate_service", return_value=[]
                ) as activate,
                mock.patch.object(MODULE, "verify_service", return_value=service),
                mock.patch.object(
                    MODULE,
                    "prune_releases",
                    return_value={"keep": 3, "removed": [], "warnings": []},
                ),
            ):
                report = MODULE.sync(args)
            self.assertFalse(report["changed"])
            self.assertEqual(report["runtime_updates"], [update])
            activate.assert_called_once_with(args.unit)

    def test_changed_release_restarts_only_previously_active_remote_bridge(self):
        old_commit = "a" * 40
        new_commit = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            old_release = args.deploy_root / "releases" / old_commit
            old_sentinel = old_release / MODULE.REMOTE_BRIDGE_RELEASE_SENTINEL
            old_sentinel.parent.mkdir(parents=True)
            old_sentinel.write_text("bridge\n", encoding="utf-8")
            release = args.deploy_root / "releases" / new_commit
            sentinel = release / MODULE.REMOTE_BRIDGE_RELEASE_SENTINEL
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("bridge\n", encoding="utf-8")
            switched = []
            activity = {
                "unit": MODULE.REMOTE_BRIDGE_UNIT,
                "active": True,
                "active_state": "active",
                "readback": {},
            }
            bridge_after = {**activity}
            bridge_health = {"marker": "read-only-v1"}
            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(MODULE, "prepare_deployment_repository", return_value=(root / "repository.git", [])),
                mock.patch.object(MODULE, "resolve_target", return_value=(new_commit, [])),
                mock.patch.object(MODULE, "read_current_commit", return_value=old_commit),
                mock.patch.object(MODULE, "read_service_activity", return_value=activity) as read_activity,
                mock.patch.object(MODULE, "prepare_release", return_value=(release, [], True)),
                mock.patch.object(MODULE, "switch_current", side_effect=lambda _root, commit: switched.append(commit)),
                mock.patch.object(MODULE, "reconcile_runtime_environment", return_value=({"changed": False, "host": "127.0.0.1", "port": 8765}, None)),
                mock.patch.object(MODULE, "install_release_runtime", return_value=([], [])),
                mock.patch.object(MODULE, "activate_service", return_value=[] ) as activate,
                mock.patch.object(MODULE, "verify_service", return_value={"url": "http://127.0.0.1:8765/"}),
                mock.patch.object(MODULE, "restart_remote_bridge", return_value=([{"restart": True}], bridge_after, bridge_health)) as restart_bridge,
                mock.patch.object(MODULE, "prune_releases", return_value={"keep": 3, "removed": [], "warnings": []}),
            ):
                report = MODULE.sync(args)
            self.assertEqual(switched, [new_commit])
            read_activity.assert_called_once_with(MODULE.REMOTE_BRIDGE_UNIT)
            activate.assert_called_once_with(args.unit)
            restart_bridge.assert_called_once_with()
            self.assertTrue(report["remote_bridge"]["before"]["active"])
            self.assertTrue(report["remote_bridge"]["restart_required"])
            self.assertEqual(report["remote_bridge"]["health"], bridge_health)

    def test_changed_release_never_starts_previously_inactive_remote_bridge(self):
        old_commit = "c" * 40
        new_commit = "d" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            for commit in (old_commit, new_commit):
                sentinel = args.deploy_root / "releases" / commit / MODULE.REMOTE_BRIDGE_RELEASE_SENTINEL
                sentinel.parent.mkdir(parents=True)
                sentinel.write_text("bridge\n", encoding="utf-8")
            release = args.deploy_root / "releases" / new_commit
            activity = {
                "unit": MODULE.REMOTE_BRIDGE_UNIT,
                "active": False,
                "active_state": "inactive",
                "readback": {},
            }
            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(MODULE, "prepare_deployment_repository", return_value=(root / "repository.git", [])),
                mock.patch.object(MODULE, "resolve_target", return_value=(new_commit, [])),
                mock.patch.object(MODULE, "read_current_commit", return_value=old_commit),
                mock.patch.object(MODULE, "read_service_activity", return_value=activity),
                mock.patch.object(MODULE, "prepare_release", return_value=(release, [], True)),
                mock.patch.object(MODULE, "switch_current"),
                mock.patch.object(MODULE, "reconcile_runtime_environment", return_value=({"changed": False, "host": "127.0.0.1", "port": 8765}, None)),
                mock.patch.object(MODULE, "install_release_runtime", return_value=([], [])),
                mock.patch.object(MODULE, "activate_service", return_value=[]),
                mock.patch.object(MODULE, "verify_service", return_value={"url": "http://127.0.0.1:8765/"}),
                mock.patch.object(MODULE, "restart_remote_bridge") as restart_bridge,
                mock.patch.object(MODULE, "prune_releases", return_value={"keep": 3, "removed": [], "warnings": []}),
            ):
                report = MODULE.sync(args)
            restart_bridge.assert_not_called()
            self.assertFalse(report["remote_bridge"]["before"]["active"])
            self.assertFalse(report["remote_bridge"]["restart_required"])
            self.assertEqual(report["remote_bridge"]["activation"], [])

    def test_bridge_unit_drift_restarts_only_an_active_bridge(self):
        commit = "5" * 40
        update = {
            "source": "systemd/user/audio-remote-bridge-v1.service",
            "destination": "/tmp/audio-remote-bridge-v1.service",
            "sha256": "8" * 64,
            "mode": "0o600",
        }
        backup = {"path": update["destination"], "payload": b"old-unit", "mode": 0o600}
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            release = args.deploy_root / "releases" / commit
            sentinel = release / MODULE.REMOTE_BRIDGE_RELEASE_SENTINEL
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("bridge\n", encoding="utf-8")
            activity = {"unit": MODULE.REMOTE_BRIDGE_UNIT, "active": True, "active_state": "active", "readback": {}}
            daemon_reload = MODULE.CommandResult(("systemctl", "--user", "daemon-reload"), 0, "", "", 0.1)
            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(MODULE, "prepare_deployment_repository", return_value=(root / "repository.git", [])),
                mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
                mock.patch.object(MODULE, "read_current_commit", return_value=commit),
                mock.patch.object(MODULE, "read_service_activity", return_value=activity),
                mock.patch.object(MODULE, "prepare_release", return_value=(release, [], False)),
                mock.patch.object(MODULE, "reconcile_runtime_environment", return_value=({"changed": False, "host": "127.0.0.1", "port": 8765}, None)),
                mock.patch.object(MODULE, "install_release_runtime", return_value=([update], [backup])),
                mock.patch.object(MODULE, "run_command", return_value=daemon_reload) as run,
                mock.patch.object(MODULE, "activate_service") as activate,
                mock.patch.object(MODULE, "verify_service", return_value={"url": "http://127.0.0.1:8765/"}),
                mock.patch.object(MODULE, "restart_remote_bridge", return_value=([{"restart": True}], activity, {"marker": "read-only-v1"})) as restart_bridge,
                mock.patch.object(MODULE, "prune_releases", return_value={"keep": 3, "removed": [], "warnings": []}),
            ):
                report = MODULE.sync(args)
            activate.assert_not_called()
            run.assert_called_once_with(["systemctl", "--user", "daemon-reload"], timeout=30)
            restart_bridge.assert_called_once_with()
            self.assertFalse(report["changed"])
            self.assertTrue(report["remote_bridge"]["restart_required"])

    def test_failed_bridge_restart_rolls_release_and_active_bridge_back(self):
        old_commit = "7" * 40
        new_commit = "8" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            for commit in (old_commit, new_commit):
                sentinel = args.deploy_root / "releases" / commit / MODULE.REMOTE_BRIDGE_RELEASE_SENTINEL
                sentinel.parent.mkdir(parents=True)
                sentinel.write_text("bridge\n", encoding="utf-8")
            release = args.deploy_root / "releases" / new_commit
            switched = []
            activity = {"unit": MODULE.REMOTE_BRIDGE_UNIT, "active": True, "active_state": "active", "readback": {}}
            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(MODULE, "prepare_deployment_repository", return_value=(root / "repository.git", [])),
                mock.patch.object(MODULE, "resolve_target", return_value=(new_commit, [])),
                mock.patch.object(MODULE, "read_current_commit", return_value=old_commit),
                mock.patch.object(MODULE, "read_service_activity", return_value=activity),
                mock.patch.object(MODULE, "prepare_release", return_value=(release, [], True)),
                mock.patch.object(MODULE, "switch_current", side_effect=lambda _root, commit: switched.append(commit)),
                mock.patch.object(MODULE, "reconcile_runtime_environment", return_value=({"changed": False, "host": "127.0.0.1", "port": 8765}, None)),
                mock.patch.object(MODULE, "install_release_runtime", return_value=([], [])),
                mock.patch.object(MODULE, "activate_service", return_value=[]) as activate,
                mock.patch.object(MODULE, "verify_service", return_value={"url": "http://127.0.0.1:8765/"}),
                mock.patch.object(MODULE, "restart_remote_bridge", side_effect=MODULE.DeployError("bridge restart failed")),
                self.assertRaisesRegex(MODULE.DeployError, "bridge restart failed"),
            ):
                MODULE.sync(args)
            self.assertEqual(switched, [new_commit, old_commit])
            self.assertEqual(
                [call.args[0] for call in activate.call_args_list],
                [args.unit, args.unit, MODULE.REMOTE_BRIDGE_UNIT],
            )

    def test_changed_release_rolls_pointer_back_after_failed_readback(self):
        old_commit = "e" * 40
        new_commit = "f" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            release = args.deploy_root / "releases" / new_commit
            release.mkdir(parents=True)
            switched = []
            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE,
                    "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(
                    MODULE, "resolve_target", return_value=(new_commit, [])
                ),
                mock.patch.object(
                    MODULE, "read_current_commit", return_value=old_commit
                ),
                mock.patch.object(
                    MODULE,
                    "prepare_release",
                    return_value=(release, [], True),
                ),
                mock.patch.object(
                    MODULE,
                    "switch_current",
                    side_effect=lambda _root, commit: switched.append(commit),
                ),
                mock.patch.object(
                    MODULE,
                    "reconcile_runtime_environment",
                    return_value=(
                        {"changed": False, "host": "127.0.0.1", "port": 8765},
                        None,
                    ),
                ),
                mock.patch.object(
                    MODULE, "install_release_runtime", return_value=([], [])
                ),
                mock.patch.object(MODULE, "activate_service", return_value=[]),
                mock.patch.object(
                    MODULE,
                    "verify_service",
                    side_effect=MODULE.DeployError("wrong revision"),
                ),
            ):
                with self.assertRaises(MODULE.DeployError):
                    MODULE.sync(args)
            self.assertEqual(switched, [new_commit, old_commit])

    def test_runtime_install_failure_also_rolls_pointer_back(self):
        old_commit = "1" * 40
        new_commit = "2" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            release = args.deploy_root / "releases" / new_commit
            release.mkdir(parents=True)
            switched = []
            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE,
                    "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(
                    MODULE, "resolve_target", return_value=(new_commit, [])
                ),
                mock.patch.object(
                    MODULE, "read_current_commit", return_value=old_commit
                ),
                mock.patch.object(
                    MODULE,
                    "prepare_release",
                    return_value=(release, [], True),
                ),
                mock.patch.object(
                    MODULE,
                    "switch_current",
                    side_effect=lambda _root, commit: switched.append(commit),
                ),
                mock.patch.object(
                    MODULE,
                    "reconcile_runtime_environment",
                    return_value=(
                        {"changed": False, "host": "127.0.0.1", "port": 8765},
                        None,
                    ),
                ),
                mock.patch.object(
                    MODULE,
                    "install_release_runtime",
                    side_effect=MODULE.DeployError("runtime rejected"),
                ),
                mock.patch.object(MODULE, "activate_service", return_value=[]),
            ):
                with self.assertRaises(MODULE.DeployError):
                    MODULE.sync(args)
            self.assertEqual(switched, [new_commit, old_commit])

    def test_runtime_install_failure_contains_auto_restarted_candidate_before_rollback(self):
        old_commit = "3" * 40
        new_commit = "4" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            previous_release = args.deploy_root / "releases" / old_commit
            previous_release.mkdir(parents=True)
            release = args.deploy_root / "releases" / new_commit
            self.write_release(release, new_commit)
            events = []
            active = {"value": False}

            def switch(_root, target):
                events.append(f"pointer:{target}")

            def install(_release):
                self.assertEqual(events[-1], f"pointer:{new_commit}")
                active["value"] = True
                events.append("candidate-auto-restarted")
                raise MODULE.DeployError("runtime rejected after pointer switch")

            def raw_activity(_unit):
                events.append(
                    "read:candidate-running"
                    if active["value"]
                    else "read:candidate-stopped"
                )
                return {
                    "unit": MODULE.QOBUZ_RECOVERY_UNIT,
                    "load_state": "loaded",
                    "active": active["value"],
                    "active_state": "active" if active["value"] else "inactive",
                    "sub_state": "running" if active["value"] else "dead",
                    "readback": {},
                }

            def service(action, unit, **_kwargs):
                self.assertEqual((action, unit), ("stop", MODULE.QOBUZ_RECOVERY_UNIT))
                events.append("stop-candidate")
                active["value"] = False
                return MODULE.CommandResult(
                    ("systemctl", "--user", action, unit), 0, "", "", 0.1
                )

            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE,
                    "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(
                    MODULE, "resolve_target", return_value=(new_commit, [])
                ),
                mock.patch.object(
                    MODULE,
                    "upgrade_current_release_marker",
                    return_value={"changed": False},
                ),
                mock.patch.object(
                    MODULE, "read_current_commit", return_value=old_commit
                ),
                mock.patch.object(
                    MODULE, "prepare_release", return_value=(release, [], False)
                ),
                mock.patch.object(
                    MODULE, "release_supports_remote_bridge", return_value=False
                ),
                mock.patch.object(MODULE, "switch_current", side_effect=switch),
                mock.patch.object(
                    MODULE,
                    "reconcile_runtime_environment",
                    return_value=({"changed": False}, None),
                ),
                mock.patch.object(
                    MODULE, "install_release_runtime", side_effect=install
                ),
                mock.patch.object(
                    MODULE, "read_service_activity_raw", side_effect=raw_activity
                ),
                mock.patch.object(MODULE, "service_command", side_effect=service),
                mock.patch.object(MODULE, "activate_service", return_value=[]),
                self.assertRaisesRegex(
                    MODULE.DeployError, "runtime rejected after pointer switch"
                ),
            ):
                MODULE.sync(args)

            self.assertFalse(active["value"])
            self.assertLess(
                events.index("read:candidate-running"),
                events.index("stop-candidate"),
            )
            self.assertLess(
                events.index("stop-candidate"),
                events.index("read:candidate-stopped"),
            )
            self.assertLess(
                events.index("read:candidate-stopped"),
                events.index(f"pointer:{old_commit}"),
            )

    def test_partial_runtime_install_is_contained_before_restore_and_pointer_rollback(self):
        old_commit = "5" * 40
        new_commit = "6" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            previous_release = args.deploy_root / "releases" / old_commit
            previous_release.mkdir(parents=True)
            release = args.deploy_root / "releases" / new_commit
            self.write_release(release, new_commit)
            deploy_relative = "scripts/audio_control_deploy.py"
            recovery_relative = f"systemd/user/{MODULE.QOBUZ_RECOVERY_UNIT}"
            deploy_source = release / deploy_relative
            deploy_source.write_bytes(b"print('candidate deployer')\n")
            deploy_destination = root / "runtime" / "audio-control-deploy.py"
            recovery_destination = root / "runtime" / MODULE.QOBUZ_RECOVERY_UNIT
            deploy_destination.parent.mkdir()
            deploy_destination.write_bytes(b"original deployer\n")
            deploy_destination.chmod(0o640)
            recovery_destination.write_bytes(b"original recovery unit\n")
            recovery_destination.chmod(0o644)
            runtime_files = {
                deploy_relative: (deploy_destination, 0o700),
                recovery_relative: (recovery_destination, 0o600),
            }
            command = MODULE.CommandResult(
                ("systemd-analyze", "--user", "verify"), 0, "", "", 0.1
            )
            events = []
            active = {"value": False}
            pointer = {"value": old_commit}
            install_calls = {"value": 0}
            original_atomic_replace = MODULE.atomic_replace_bytes

            def switch(_root, target):
                pointer["value"] = target
                events.append(f"pointer:{target}")

            def replace(path, payload, mode):
                if payload in {b"original deployer\n", b"original recovery unit\n"}:
                    events.append(f"runtime-restore:{path.name}")
                    original_atomic_replace(path, payload, mode)
                    return
                install_calls["value"] += 1
                if install_calls["value"] == 2:
                    events.append("runtime-install-failed")
                    raise OSError("forced second runtime replacement failure")
                original_atomic_replace(path, payload, mode)
                active["value"] = True
                events.append("candidate-auto-restarted")

            def raw_activity(_unit):
                events.append(
                    "read:candidate-running"
                    if active["value"]
                    else "read:candidate-stopped"
                )
                return {
                    "unit": MODULE.QOBUZ_RECOVERY_UNIT,
                    "load_state": "loaded",
                    "active": active["value"],
                    "active_state": "active" if active["value"] else "inactive",
                    "sub_state": "running" if active["value"] else "dead",
                    "readback": {},
                }

            def service(action, unit, **_kwargs):
                self.assertEqual((action, unit), ("stop", MODULE.QOBUZ_RECOVERY_UNIT))
                events.append("stop-candidate")
                active["value"] = False
                return MODULE.CommandResult(
                    ("systemctl", "--user", action, unit), 0, "", "", 0.1
                )

            with (
                mock.patch.object(MODULE, "RUNTIME_FILES", runtime_files),
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE,
                    "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(MODULE, "resolve_target", return_value=(new_commit, [])),
                mock.patch.object(
                    MODULE,
                    "upgrade_current_release_marker",
                    return_value={"changed": False},
                ),
                mock.patch.object(MODULE, "read_current_commit", return_value=old_commit),
                mock.patch.object(
                    MODULE, "prepare_release", return_value=(release, [], False)
                ),
                mock.patch.object(
                    MODULE, "release_supports_remote_bridge", return_value=False
                ),
                mock.patch.object(MODULE, "switch_current", side_effect=switch),
                mock.patch.object(
                    MODULE,
                    "reconcile_runtime_environment",
                    return_value=({"changed": False}, None),
                ),
                mock.patch.object(MODULE, "atomic_replace_bytes", side_effect=replace),
                mock.patch.object(MODULE, "run_command", return_value=command),
                mock.patch.object(
                    MODULE, "read_service_activity_raw", side_effect=raw_activity
                ),
                mock.patch.object(MODULE, "service_command", side_effect=service),
                mock.patch.object(MODULE, "activate_service", return_value=[]),
                self.assertRaisesRegex(
                    MODULE.ReleaseRuntimeInstallError,
                    "forced second runtime replacement failure",
                ),
            ):
                MODULE.sync(args)

            first_restore = next(
                index
                for index, event in enumerate(events)
                if event.startswith("runtime-restore:")
            )
            self.assertLess(events.index("stop-candidate"), first_restore)
            self.assertLess(events.index("read:candidate-stopped"), first_restore)
            self.assertLess(first_restore, events.index(f"pointer:{old_commit}"))
            self.assertEqual(deploy_destination.read_bytes(), b"original deployer\n")
            self.assertEqual(deploy_destination.stat().st_mode & 0o777, 0o640)
            self.assertEqual(
                recovery_destination.read_bytes(), b"original recovery unit\n"
            )
            self.assertEqual(recovery_destination.stat().st_mode & 0o777, 0o644)
            self.assertFalse(active["value"])
            self.assertEqual(pointer["value"], old_commit)

    def test_runtime_environment_is_port_bound_and_fail_closed(self):
        with mock.patch.dict(
            MODULE.os.environ, {"XDG_RUNTIME_DIR": "/run/user/1234"}
        ):
            payload = MODULE.runtime_environment_payload("127.0.0.1", 9876).decode()
        self.assertIn('AUDIO_CONTROL_HOST="127.0.0.1"', payload)
        self.assertIn('AUDIO_CONTROL_PORT="9876"', payload)
        self.assertIn(
            f'AUDIO_CONTROL_MANAGED_BY="{MODULE.UI_MANAGED_BY}"', payload
        )
        self.assertIn(
            'AUDIO_TELEMETRY_LEVEL_SOURCE="/run/user/1234/'
            'audio-control-level-observer/levels.json"',
            payload,
        )
        with self.assertRaises(MODULE.DeployError):
            MODULE.runtime_environment_payload("0.0.0.0", 9876)
        for port in (0, 1023, 65536):
            with self.subTest(port=port), self.assertRaises(MODULE.DeployError):
                MODULE.runtime_environment_payload("127.0.0.1", port)

    def test_runtime_environment_is_created_in_existing_state_root(self):
        with tempfile.TemporaryDirectory() as directory:
            state_root = pathlib.Path(directory) / "state"
            state_root.mkdir()
            runtime_env = state_root / "runtime.env"
            report, backup = MODULE.reconcile_runtime_environment(
                runtime_env, host="127.0.0.1", port=8765
            )
            self.assertTrue(report["changed"])
            self.assertIsNone(backup["payload"])
            self.assertEqual(runtime_env.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                runtime_env.read_bytes(),
                MODULE.runtime_environment_payload("127.0.0.1", 8765),
            )

    def test_runtime_environment_failure_restores_config_and_service(self):
        commit = "3" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            args = self.sync_args(root)
            args.deploy_root.mkdir()
            args.state_root.mkdir()
            release = args.deploy_root / "releases" / commit
            release.mkdir(parents=True)
            backup = {
                "path": str(root / "runtime.env"),
                "payload": b"old-config",
                "mode": 0o600,
            }
            with (
                mock.patch.object(MODULE, "DEFAULT_DEPLOY_ROOT", args.deploy_root),
                mock.patch.object(MODULE, "DEFAULT_STATE_ROOT", args.state_root),
                mock.patch.object(MODULE, "ensure_source_repo", return_value=root),
                mock.patch.object(
                    MODULE,
                    "prepare_deployment_repository",
                    return_value=(root / "repository.git", []),
                ),
                mock.patch.object(MODULE, "resolve_target", return_value=(commit, [])),
                mock.patch.object(MODULE, "read_current_commit", return_value=commit),
                mock.patch.object(
                    MODULE,
                    "prepare_release",
                    return_value=(release, [], False),
                ),
                mock.patch.object(
                    MODULE,
                    "reconcile_runtime_environment",
                    return_value=(
                        {"changed": True, "host": "127.0.0.1", "port": 8765},
                        backup,
                    ),
                ),
                mock.patch.object(MODULE, "restore_runtime_environment") as restore,
                mock.patch.object(MODULE, "activate_service", return_value=[]) as activate,
                mock.patch.object(
                    MODULE,
                    "verify_service",
                    side_effect=MODULE.DeployError("wrong endpoint"),
                ),
            ):
                with self.assertRaises(MODULE.DeployError):
                    MODULE.sync(args)
            restore.assert_called_once_with(backup)
            self.assertEqual(activate.call_count, 2)

    def test_release_runtime_validation_uses_bound_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            release = root / "release"
            destinations = root / "installed"
            runtime_files = {}
            expected_units = {}
            for relative, (_destination, mode) in MODULE.RUNTIME_FILES.items():
                source = release / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                if relative.endswith(".py"):
                    payload = b"print('deploy')\n"
                else:
                    payload = f"# bound {relative}\n".encode()
                    if pathlib.Path(relative).suffix in {".service", ".timer"}:
                        expected_units[pathlib.Path(relative).name] = payload
                source.write_bytes(payload)
                destination = destinations / pathlib.Path(relative).name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"stale")
                destination.chmod(mode)
                runtime_files[relative] = (destination, mode)

            def verify(argv, **_kwargs):
                self.assertEqual(argv[:3], ["systemd-analyze", "--user", "verify"])
                observed = {
                    pathlib.Path(value).name: pathlib.Path(value).read_bytes()
                    for value in argv[3:]
                }
                self.assertEqual(observed, expected_units)
                return MODULE.CommandResult(tuple(argv), 0, "", "", 0.1)

            with (
                mock.patch.object(MODULE, "RUNTIME_FILES", runtime_files),
                mock.patch.object(MODULE, "run_command", side_effect=verify),
            ):
                updates, backups = MODULE.install_release_runtime(release)
            self.assertEqual(len(updates), len(runtime_files))
            self.assertEqual(len(backups), len(runtime_files))

    def test_partial_runtime_install_failure_propagates_every_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            release = root / "release"
            deploy_relative = "scripts/audio_control_deploy.py"
            unit_relative = f"systemd/user/{MODULE.QOBUZ_RECOVERY_UNIT}"
            sources = {
                deploy_relative: b"print('new deployer')\n",
                unit_relative: b"[Service]\nExecStart=/usr/bin/true\n",
            }
            destinations = {
                deploy_relative: root / "runtime" / "audio-control-deploy.py",
                unit_relative: root / "runtime" / MODULE.QOBUZ_RECOVERY_UNIT,
            }
            original = {
                deploy_relative: (b"old deployer\n", 0o640),
                unit_relative: (b"old recovery unit\n", 0o644),
            }
            runtime_files = {}
            for relative, payload in sources.items():
                source = release / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(payload)
                destination = destinations[relative]
                destination.parent.mkdir(parents=True, exist_ok=True)
                old_payload, old_mode = original[relative]
                destination.write_bytes(old_payload)
                destination.chmod(old_mode)
                runtime_files[relative] = (
                    destination,
                    0o700 if relative == deploy_relative else 0o600,
                )
            command = MODULE.CommandResult(
                ("systemd-analyze", "--user", "verify"), 0, "", "", 0.1
            )
            calls = {"value": 0}
            original_atomic_replace = MODULE.atomic_replace_bytes

            def fail_second(path, payload, mode):
                calls["value"] += 1
                if calls["value"] == 2:
                    raise OSError("forced partial install")
                original_atomic_replace(path, payload, mode)

            with (
                mock.patch.object(MODULE, "RUNTIME_FILES", runtime_files),
                mock.patch.object(MODULE, "run_command", return_value=command),
                mock.patch.object(
                    MODULE, "atomic_replace_bytes", side_effect=fail_second
                ),
                self.assertRaises(MODULE.ReleaseRuntimeInstallError) as raised,
            ):
                MODULE.install_release_runtime(release)

            error = raised.exception
            self.assertEqual(len(error.updates), 1)
            self.assertEqual(len(error.backups), 2)
            self.assertEqual(
                [backup["path"] for backup in error.backups],
                [str(destinations[relative]) for relative in sources],
            )
            self.assertEqual(
                destinations[deploy_relative].read_bytes(), sources[deploy_relative]
            )
            self.assertEqual(
                destinations[unit_relative].read_bytes(), original[unit_relative][0]
            )

            MODULE.restore_release_runtime(error.backups)
            for relative, destination in destinations.items():
                old_payload, old_mode = original[relative]
                self.assertEqual(destination.read_bytes(), old_payload)
                self.assertEqual(destination.stat().st_mode & 0o777, old_mode)

    def test_runtime_rollback_preserves_dauersong_safety_ratchet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            safety = root / "zz-audio-control-v1.conf"
            ordinary = root / "audio-control-ui-v1.service"
            safety.write_bytes(b"new-safe-100-percent\n")
            ordinary.write_bytes(b"new-runtime\n")
            backups = [
                {"path": str(safety), "payload": b"old-185-percent\n", "mode": 0o600},
                {"path": str(ordinary), "payload": b"old-runtime\n", "mode": 0o600},
            ]
            with mock.patch.object(MODULE, "DAUERSONG_HARDENING_DESTINATION", safety):
                MODULE.restore_release_runtime(backups)
            self.assertEqual(safety.read_bytes(), b"new-safe-100-percent\n")
            self.assertEqual(ordinary.read_bytes(), b"old-runtime\n")

    def test_runtime_rollback_never_removes_new_dauersong_safety_dropin(self):
        with tempfile.TemporaryDirectory() as directory:
            safety = pathlib.Path(directory) / "zz-audio-control-v1.conf"
            safety.write_bytes(b"safe-cap\n")
            backups = [{"path": str(safety), "payload": None, "mode": None}]
            with mock.patch.object(MODULE, "DAUERSONG_HARDENING_DESTINATION", safety):
                MODULE.restore_release_runtime(backups)
            self.assertEqual(safety.read_bytes(), b"safe-cap\n")

    def test_release_runtime_update_requires_complete_bound_set(self):
        with tempfile.TemporaryDirectory() as directory:
            release = pathlib.Path(directory)
            source = release / "scripts" / "audio_control_deploy.py"
            source.parent.mkdir(parents=True)
            source.write_text("print('partial')\n", encoding="utf-8")
            with self.assertRaises(MODULE.DeployError):
                MODULE.install_release_runtime(release)

    def test_release_retention_keeps_current_and_two_recent_predecessors(self):
        commits = [format(number, "040x") for number in range(1, 5)]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            releases = root / "releases"
            for created_at, commit in enumerate(commits, start=1):
                self.write_release(
                    releases / commit,
                    commit,
                    created_at=created_at,
                )
            report = MODULE.prune_releases(
                root,
                current_commit=commits[-1],
                keep=3,
            )
            self.assertEqual(report["removed"], [commits[0]])
            self.assertFalse((releases / commits[0]).exists())
            for commit in commits[1:]:
                self.assertTrue((releases / commit).is_dir())

    def test_runtime_environment_repairs_mode_only_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "runtime.env"
            payload = MODULE.runtime_environment_payload("127.0.0.1", 8765)
            path.write_bytes(payload)
            path.chmod(0o644)

            report, backup = MODULE.reconcile_runtime_environment(
                path, host="127.0.0.1", port=8765
            )

            self.assertTrue(report["changed"])
            self.assertEqual(report["mode"], "0o600")
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertEqual(backup["payload"], payload)
            self.assertEqual(backup["mode"], 0o644)

            unchanged, second_backup = MODULE.reconcile_runtime_environment(
                path, host="127.0.0.1", port=8765
            )
            self.assertFalse(unchanged["changed"])
            self.assertEqual(unchanged["mode"], "0o600")
            self.assertIsNone(second_backup)

    def test_systemd_contract_is_persistent_and_revision_bound(self):
        service = (ROOT / "systemd" / "user" / "audio-control-ui-v1.service").read_text()
        service_directives = [
            line.strip()
            for line in service.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        exec_start = next(
            line for line in service_directives if line.startswith("ExecStart=")
        )
        deploy = (ROOT / "systemd" / "user" / "audio-control-deploy.service").read_text()
        timer = (ROOT / "systemd" / "user" / "audio-control-deploy.timer").read_text()
        installer = (ROOT / "scripts" / "install_audio_control_autodeploy.py").read_text()

        self.assertIn("%h/.local/share/audio-control-ui/current", service)
        legacy_environment = "EnvironmentFile=-%h/.config/audio-control-deploy.env"
        runtime_environment = (
            "EnvironmentFile=-%h/.local/state/audio-control-deploy/runtime.env"
        )
        self.assertIn("Environment=AUDIO_CONTROL_PORT=8765", service_directives)
        self.assertIn(legacy_environment, service_directives)
        self.assertIn(runtime_environment, service_directives)
        self.assertLess(
            service_directives.index(legacy_environment),
            service_directives.index(runtime_environment),
        )
        self.assertNotIn(
            "ConditionPathExists=%h/.local/state/audio-control-deploy/runtime.env",
            service_directives,
        )
        self.assertIn("--host 127.0.0.1", exec_start)
        self.assertNotIn("--host ${AUDIO_CONTROL_HOST}", exec_start)
        self.assertIn("--port ${AUDIO_CONTROL_PORT}", exec_start)
        self.assertNotIn("--port 8765", service)
        self.assertIn("Restart=on-failure", service)
        self.assertNotIn("RuntimeMaxSec", service)
        self.assertNotIn("Restart=no", service)
        self.assertIn("audio-control-deploy.py sync", deploy)
        self.assertIn("%h/.local/libexec", deploy)
        self.assertIn("%h/.config/systemd/user", deploy)
        self.assertNotIn("%h/.config/audio-control-ui", deploy)
        self.assertIn("%h/.local/state/audio-control-deploy", deploy)
        self.assertEqual(len(MODULE.RUNTIME_FILES), 9)
        self.assertIn("systemd/user/audio-remote-bridge-v1.service", MODULE.RUNTIME_FILES)
        self.assertIn(
            "systemd/user/grabowski-dauersong.service.d/zz-audio-control-v1.conf",
            MODULE.RUNTIME_FILES,
        )
        self.assertIn(
            "systemd/user/audio-control-level-observer-v1.service",
            MODULE.RUNTIME_FILES,
        )
        self.assertIn(
            "systemd/user/audio-qobuz-desktop-recovery-v1.service",
            MODULE.RUNTIME_FILES,
        )
        self.assertEqual(MODULE.DEFAULT_RELEASE_RETENTION, 3)
        self.assertIn("OnUnitActiveSec=60s", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("--expected-commit", installer)
        self.assertNotIn('config_root / "audio-control-ui"', installer)
        deployer = (ROOT / "scripts" / "audio_control_deploy.py").read_text()
        self.assertIn(
            'UI_RUNTIME_ENV = DEFAULT_STATE_ROOT / "runtime.env"', deployer
        )
        self.assertIn("validate_ui_endpoint(args.host, args.port)", installer)
        self.assertNotIn("shell=True", installer)


if __name__ == "__main__":
    unittest.main()
