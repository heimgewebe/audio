import argparse
import contextlib
import importlib.util
import io
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DEPLOY = load_module("audio_control_deploy_runtime_roots", "scripts/audio_control_deploy.py")
INSTALL = load_module(
    "install_audio_control_autodeploy_runtime_roots",
    "scripts/install_audio_control_autodeploy.py",
)


class AudioControlRuntimeRootTests(unittest.TestCase):
    def test_default_runtime_roots_are_accepted(self):
        self.assertEqual(
            DEPLOY.validate_runtime_roots(
                DEPLOY.DEFAULT_DEPLOY_ROOT, DEPLOY.DEFAULT_STATE_ROOT
            ),
            (DEPLOY.DEFAULT_DEPLOY_ROOT, DEPLOY.DEFAULT_STATE_ROOT),
        )
        self.assertEqual(
            INSTALL.validate_runtime_roots(
                INSTALL.DEFAULT_DEPLOY_ROOT, INSTALL.DEFAULT_STATE_ROOT
            ),
            (INSTALL.DEFAULT_DEPLOY_ROOT, INSTALL.DEFAULT_STATE_ROOT),
        )

    def test_installer_rejects_nondefault_roots_before_effects(self):
        cases = (
            (INSTALL.DEFAULT_DEPLOY_ROOT.parent / "custom-audio-ui", INSTALL.DEFAULT_STATE_ROOT),
            (INSTALL.DEFAULT_DEPLOY_ROOT, INSTALL.DEFAULT_STATE_ROOT.parent / "custom-audio-state"),
        )
        for deploy_root, state_root in cases:
            with self.subTest(deploy_root=deploy_root, state_root=state_root):
                args = argparse.Namespace(
                    host="127.0.0.1",
                    port=8765,
                    source_repo=INSTALL.DEFAULT_SOURCE_REPO,
                    deploy_root=deploy_root,
                    state_root=state_root,
                    remote="origin",
                    branch="main",
                    expected_commit="",
                )
                with (
                    mock.patch.object(INSTALL, "validate_source_repo") as source,
                    mock.patch.object(INSTALL, "ensure_absolute_directory") as mkdir,
                    self.assertRaises(INSTALL.InstallError),
                ):
                    INSTALL.install(args)
                source.assert_not_called()
                mkdir.assert_not_called()

    def test_deployer_operations_reject_nondefault_roots_before_effects(self):
        sync_args = argparse.Namespace(
            source_repo=DEPLOY.DEFAULT_SOURCE_REPO,
            deploy_root=DEPLOY.DEFAULT_DEPLOY_ROOT.parent / "custom-audio-ui",
            state_root=DEPLOY.DEFAULT_STATE_ROOT,
            remote="origin",
            branch="main",
            unit=DEPLOY.DEFAULT_UNIT,
            host=DEPLOY.DEFAULT_HOST,
            port=DEPLOY.DEFAULT_PORT,
            expected_commit="",
        )
        with (
            mock.patch.object(DEPLOY, "ensure_source_repo") as source,
            self.assertRaises(DEPLOY.DeployError),
        ):
            DEPLOY.sync(sync_args)
        source.assert_not_called()

        status_args = argparse.Namespace(
            deploy_root=DEPLOY.DEFAULT_DEPLOY_ROOT,
            state_root=DEPLOY.DEFAULT_STATE_ROOT.parent / "custom-audio-state",
            unit=DEPLOY.DEFAULT_UNIT,
        )
        with (
            mock.patch.object(DEPLOY, "ensure_private_directory") as mkdir,
            self.assertRaises(DEPLOY.DeployError),
        ):
            DEPLOY.status(status_args)
        mkdir.assert_not_called()

    def test_deployer_cli_rejects_nondefault_roots_before_dispatch(self):
        cases = (
            ("--deploy-root", DEPLOY.DEFAULT_DEPLOY_ROOT.parent / "custom-audio-ui"),
            ("--state-root", DEPLOY.DEFAULT_STATE_ROOT.parent / "custom-audio-state"),
        )
        for option, value in cases:
            with self.subTest(option=option):
                stderr = io.StringIO()
                with (
                    mock.patch.object(sys, "argv", ["audio_control_deploy.py", option, str(value), "sync"]),
                    mock.patch.object(DEPLOY, "sync") as sync,
                    contextlib.redirect_stderr(stderr),
                ):
                    self.assertEqual(DEPLOY.main(), 1)
                sync.assert_not_called()
                self.assertIn("Version 1 unterstützt nur", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
