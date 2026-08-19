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
            ]
        )
        completed = subprocess.CompletedProcess(["systemctl"], 0, stdout=stdout, stderr="")
        with mock.patch.object(MODULE, "run_capture", return_value=completed):
            status = MODULE.service_status()
        self.assertTrue(status["hardening_ready"])
        self.assertFalse(status["active"])
        self.assertEqual(status["hardening"]["stream_volume_percent"], 100)
        self.assertEqual(status["hardening"]["restart"], "no")

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
