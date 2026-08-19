import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dauersong_live", ROOT / "scripts" / "dauersong_live.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class DauersongLiveTests(unittest.TestCase):
    def test_runtime_sources_are_hash_bound_and_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            payload = b"print('bound')\n"
            (root / "runner.py").write_bytes(payload)
            manifest = {
                "legacy_root": "~/.local/state/grabowski-music",
                "required_files": {
                    "runner.py": {
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                },
            }
            with mock.patch.object(MODULE, "legacy_root", return_value=root):
                self.assertTrue(MODULE.validate_runtime_sources(manifest)["ready"])
                (root / "runner.py").write_bytes(payload + b"# drift\n")
                drifted = MODULE.validate_runtime_sources(manifest)
        self.assertFalse(drifted["ready"])
        self.assertEqual(drifted["errors"], ["source-drift:runner.py"])

    def test_soundfont_symlink_requires_bound_target_content_and_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            payload = b"soundfont"
            target = root / "font.sf2"
            target.write_bytes(payload)
            configured = root / "default.sf3"
            configured.symlink_to(target)
            manifest = {
                "soundfont": str(configured),
                "soundfont_binding": {
                    "configured_path": str(configured),
                    "resolved_path": str(target),
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "owner_uid": os.getuid(),
                    "forbid_group_or_other_write": True,
                },
            }
            self.assertTrue(MODULE.validate_soundfont(manifest)["ready"])
            target.write_bytes(payload + b"drift")
            self.assertFalse(MODULE.validate_soundfont(manifest)["ready"])

    def test_service_status_requires_effective_late_dropin_and_bounds(self):
        stdout = "\n".join(
            [
                "LoadState=loaded",
                "ActiveState=inactive",
                "SubState=dead",
                "Result=success",
                "MainPID=0",
                "Environment=GRABOWSKI_STREAM_VOLUME=100 AUDIO_DAUERSONG_MANAGED_BY=audio-control-v1 AUDIO_DAUERSONG_RUNTIME_MAX_SECONDS=21600",
                "Restart=no",
                "MemoryMax=536870912",
                "TasksMax=32",
                "LimitNOFILE=1024",
                "RuntimeMaxUSec=6h",
                "CPUQuotaPerSecUSec=1.5s",
                "DropInPaths=/home/alex/.config/systemd/user/grabowski-dauersong.service.d/volume.conf /home/alex/.config/systemd/user/grabowski-dauersong.service.d/zz-audio-control-v1.conf",
                "NeedDaemonReload=no",
            ]
        )
        completed = subprocess.CompletedProcess(["systemctl"], 0, stdout=stdout, stderr="")
        with mock.patch.object(MODULE, "run_capture", return_value=completed):
            status = MODULE.service_status()
        self.assertTrue(status["hardening_ready"])
        self.assertFalse(status["active"])
        self.assertEqual(status["hardening"]["stream_volume_percent"], 100)
        self.assertEqual(status["hardening"]["restart"], "no")
        self.assertTrue(status["hardening"]["dropin_last"])
        self.assertEqual(status["hardening"]["need_daemon_reload"], "no")

    def test_service_status_rejects_later_dropin_or_pending_reload(self):
        base = [
            "LoadState=loaded",
            "ActiveState=inactive",
            "SubState=dead",
            "Result=success",
            "MainPID=0",
            "Environment=GRABOWSKI_STREAM_VOLUME=100 AUDIO_DAUERSONG_MANAGED_BY=audio-control-v1 AUDIO_DAUERSONG_RUNTIME_MAX_SECONDS=21600",
            "Restart=no",
            "MemoryMax=536870912",
            "TasksMax=32",
            "LimitNOFILE=1024",
            "RuntimeMaxUSec=6h",
            "CPUQuotaPerSecUSec=1.5s",
        ]
        cases = (
            (
                "DropInPaths=/home/alex/.config/systemd/user/grabowski-dauersong.service.d/zz-audio-control-v1.conf /home/alex/.config/systemd/user/grabowski-dauersong.service.d/zzz-override.conf",
                "NeedDaemonReload=no",
            ),
            (
                "DropInPaths=/home/alex/.config/systemd/user/grabowski-dauersong.service.d/zz-audio-control-v1.conf",
                "NeedDaemonReload=yes",
            ),
        )
        for dropins, reload_state in cases:
            with self.subTest(dropins=dropins, reload_state=reload_state):
                completed = subprocess.CompletedProcess(
                    ["systemctl"], 0, stdout="\n".join([*base, dropins, reload_state]), stderr=""
                )
                with mock.patch.object(MODULE, "run_capture", return_value=completed):
                    status = MODULE.service_status()
                self.assertFalse(status["hardening_ready"])

    def test_active_runtime_binds_the_process_environment_not_only_reloaded_config(self):
        stdout = "\n".join(
            [
                "LoadState=loaded",
                "ActiveState=active",
                "SubState=running",
                "Result=success",
                "MainPID=1234",
                "Environment=GRABOWSKI_STREAM_VOLUME=100 AUDIO_DAUERSONG_MANAGED_BY=audio-control-v1 AUDIO_DAUERSONG_RUNTIME_MAX_SECONDS=21600",
                "Restart=no",
                "MemoryMax=536870912",
                "TasksMax=32",
                "LimitNOFILE=1024",
                "RuntimeMaxUSec=6h",
                "CPUQuotaPerSecUSec=1.5s",
                "DropInPaths=/home/alex/.config/systemd/user/grabowski-dauersong.service.d/zz-audio-control-v1.conf",
                "NeedDaemonReload=no",
            ]
        )
        completed = subprocess.CompletedProcess(["systemctl"], 0, stdout=stdout, stderr="")
        with (
            mock.patch.object(MODULE, "run_capture", return_value=completed),
            mock.patch.object(MODULE, "process_environment_value", return_value="185"),
        ):
            status = MODULE.service_status()
        self.assertTrue(status["hardening_ready"])
        self.assertEqual(status["hardening"]["stream_volume_percent"], 100)
        self.assertEqual(status["hardening"]["running_stream_volume_percent"], 185)

    def test_full_status_rejects_old_running_process_even_if_stream_is_temporarily_clamped(self):
        service = {
            "unit": MODULE.UNIT_NAME,
            "load_state": "loaded",
            "active_state": "active",
            "sub_state": "running",
            "result": "success",
            "main_pid": 1234,
            "active": True,
            "hardening_ready": True,
            "hardening": {
                "stream_volume_percent": 100,
                "runtime_max_seconds": MODULE.MAX_RUNTIME_SECONDS,
                "managed_by": MODULE.MANAGED_BY,
                "running_stream_volume_percent": 185,
            },
        }
        host = {
            "ready": True,
            "source_binding": {"ready": True, "errors": []},
            "soundfont": {"ready": True},
        }
        stream = {"found": True, "indexes": [77], "max_volume_percent": 100}
        with (
            mock.patch.object(MODULE, "load_manifest", return_value={}),
            mock.patch.object(MODULE, "service_status", return_value=service),
            mock.patch.object(MODULE, "host_verification", return_value=host),
            mock.patch.object(MODULE, "stream_status", return_value=stream),
            mock.patch.object(MODULE, "live_status_snapshot", return_value={}),
        ):
            status = MODULE.full_status()
        self.assertFalse(status["runtime_safe"])
        self.assertEqual(status["configured_stream_volume_percent"], 100)
        self.assertEqual(status["hardening"]["running_stream_volume_percent"], 185)

    def test_stop_is_issued_before_readback_and_survives_transient_status_failure(self):
        inactive = {
            "load_state": "loaded",
            "active_state": "inactive",
            "sub_state": "dead",
            "active": False,
        }
        completed = subprocess.CompletedProcess(
            ["systemctl", "--user", "stop", MODULE.UNIT_NAME], 0, stdout="", stderr=""
        )
        with (
            mock.patch.object(MODULE, "run_capture", return_value=completed) as run,
            mock.patch.object(
                MODULE, "service_status", side_effect=[RuntimeError("status unavailable"), inactive]
            ),
            mock.patch.object(MODULE.time, "sleep"),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(MODULE.stop_service(), 0)
        run.assert_called_once_with(["systemctl", "--user", "stop", MODULE.UNIT_NAME])

    def test_stop_of_missing_unit_is_idempotent_after_effect_attempt(self):
        missing = {
            "load_state": "not-found",
            "active_state": "inactive",
            "sub_state": "dead",
            "active": False,
        }
        completed = subprocess.CompletedProcess(
            ["systemctl", "--user", "stop", MODULE.UNIT_NAME], 5, stdout="", stderr="not loaded"
        )
        with (
            mock.patch.object(MODULE, "run_capture", return_value=completed) as run,
            mock.patch.object(MODULE, "service_status", return_value=missing),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(MODULE.stop_service(), 0)
        run.assert_called_once_with(["systemctl", "--user", "stop", MODULE.UNIT_NAME])

    def test_stream_status_selects_only_fluidsynth_descendants(self):
        entries = [
            {
                "index": 77,
                "properties": {"application.process.id": "101", "application.process.binary": "fluidsynth"},
                "volume": {"left": {"value_percent": "100%"}, "right": {"value_percent": "100%"}},
            },
            {
                "index": 88,
                "properties": {"application.process.id": "999", "application.process.binary": "fluidsynth"},
                "volume": {"left": {"value_percent": "185%"}},
            },
        ]
        completed = subprocess.CompletedProcess(["pactl"], 0, stdout=json.dumps(entries), stderr="")
        with (
            mock.patch.object(MODULE, "descendant_pids", return_value={100, 101}),
            mock.patch.object(MODULE.shutil, "which", return_value="/usr/bin/pactl"),
            mock.patch.object(MODULE, "run_capture", return_value=completed),
        ):
            status = MODULE.stream_status(100)
        self.assertEqual(status["indexes"], [77])
        self.assertEqual(status["max_volume_percent"], 100)

    def test_start_uses_existing_service_and_requires_safe_readback(self):
        inactive = {"active": False}
        active = {
            "active": True,
            "hardening_ready": True,
            "source_binding_ready": True,
            "main_pid": 1234,
            "active_state": "active",
            "runtime_safe": False,
        }
        confirmed = {**active, "runtime_safe": True}
        with tempfile.TemporaryDirectory() as directory:
            ecosystem = pathlib.Path(directory) / "ecosystem"
            ecosystem.mkdir()
            live = ecosystem / "live-status.json"
            live.write_text("{}\n")
            before = live.stat().st_mtime_ns

            def fake_run(argv, timeout=10):
                if argv[:3] == ["systemctl", "--user", "start"]:
                    os.utime(live, ns=(before + 1_000_000, before + 1_000_000))
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with (
                mock.patch.object(MODULE, "runtime_doctor", return_value={"ready": True}),
                mock.patch.object(MODULE, "full_status", side_effect=[inactive, active, confirmed]),
                mock.patch.object(MODULE, "load_manifest", return_value={}),
                mock.patch.object(MODULE, "legacy_root", return_value=pathlib.Path(directory)),
                mock.patch.object(MODULE, "run_capture", side_effect=fake_run) as run,
                mock.patch.object(
                    MODULE,
                    "enforce_stream_volume",
                    return_value={"found": True, "indexes": [77], "max_volume_percent": 100},
                ),
                mock.patch("builtins.print"),
            ):
                self.assertEqual(MODULE.start_service(), 0)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(["systemctl", "--user", "start", "grabowski-dauersong.service"], calls)

    def test_cli_parser_exposes_every_supported_subcommand(self):
        parser = MODULE.build_parser()
        for command in ("doctor", "status", "start", "stop", "recover", "verify-host"):
            with self.subTest(command=command):
                self.assertEqual(parser.parse_args([command]).command, command)

    def test_repo_dropin_overrides_185_percent_and_bounds_existing_service(self):
        dropin = (
            ROOT
            / "systemd"
            / "user"
            / "grabowski-dauersong.service.d"
            / "zz-audio-control-v1.conf"
        ).read_text()
        self.assertIn("GRABOWSKI_STREAM_VOLUME=100", dropin)
        self.assertNotIn("GRABOWSKI_STREAM_VOLUME=185", dropin)
        self.assertIn("ExecStartPre=", dropin)
        self.assertIn("Restart=no", dropin)
        self.assertIn("RuntimeMaxSec=21600", dropin)
        self.assertIn("MemoryMax=536870912", dropin)
        self.assertIn("CPUQuota=150%", dropin)
        self.assertIn("TasksMax=32", dropin)
        self.assertIn("LimitNOFILE=1024", dropin)


if __name__ == "__main__":
    unittest.main()
