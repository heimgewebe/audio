import argparse
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
    ) -> None:
        files = {
            "scripts/audio_control.py": b"print('control')\n",
            "ui/index.html": b"<!doctype html><title>Audio</title>\n",
            "ui/app.js": b'"use strict";\n',
            "ui/styles.css": b"body { margin: 0; }\n",
        }
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
            self.assertEqual(set(report["static_sha256"]), {"/", "/app.js", "/styles.css"})

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

    def test_runtime_environment_is_port_bound_and_fail_closed(self):
        payload = MODULE.runtime_environment_payload("127.0.0.1", 9876).decode()
        self.assertIn('AUDIO_CONTROL_HOST="127.0.0.1"', payload)
        self.assertIn('AUDIO_CONTROL_PORT="9876"', payload)
        self.assertIn(
            f'AUDIO_CONTROL_MANAGED_BY="{MODULE.UI_MANAGED_BY}"', payload
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
        self.assertEqual(len(MODULE.RUNTIME_FILES), 4)
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
