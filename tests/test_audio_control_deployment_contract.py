import hashlib
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "ui" / "index.html"
DEPLOY_UNIT_PATH = ROOT / "systemd" / "user" / "audio-control-deploy.service"
UI_UNIT_PATH = ROOT / "systemd" / "user" / "audio-control-ui-v1.service"
LEVEL_OBSERVER_UNIT_PATH = (
    ROOT / "systemd" / "user" / "audio-control-level-observer-v1.service"
)
QOBUZ_RECOVERY_UNIT_PATH = (
    ROOT / "systemd" / "user" / "audio-qobuz-desktop-recovery-v1.service"
)
QBZD_QCONNECT_RECOVERY_UNIT_PATH = (
    ROOT / "systemd" / "user" / "audio-qbzd-qconnect-recovery-v1.service"
)
LEGACY_INDEX_BLOB_SHA = "4a1e80316512a24f780359c8f7e45194226c4f88"
DEPLOYMENT_CONTRACT_PATTERN = (
    r'<meta\s+name="audio-control-deployment-contract"\s+'
    r'content="revision-bound-v1"\s*>'
)


def address_families(path: pathlib.Path) -> set[str]:
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("RestrictAddressFamilies=")
    ]
    if len(lines) != 1:
        raise AssertionError(
            f"expected exactly one RestrictAddressFamilies line in {path.name}"
        )
    return set(lines[0].split("=", 1)[1].split())


def read_write_paths(path: pathlib.Path) -> set[str]:
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("ReadWritePaths=")
    ]
    if len(lines) != 1:
        raise AssertionError(f"expected exactly one ReadWritePaths line in {path.name}")
    return set(lines[0].split("=", 1)[1].split())


class AudioControlDeploymentContractTests(unittest.TestCase):
    def test_index_distinguishes_revision_bound_first_hop_from_legacy(self):
        payload = INDEX_PATH.read_bytes()
        git_blob = hashlib.sha1(
            f"blob {len(payload)}\0".encode("ascii") + payload,
            usedforsecurity=False,
        ).hexdigest()

        self.assertNotEqual(git_blob, LEGACY_INDEX_BLOB_SHA)
        self.assertRegex(payload.decode("utf-8"), DEPLOYMENT_CONTRACT_PATTERN)

    def test_deploy_unit_allows_only_required_address_families(self):
        self.assertEqual(
            address_families(DEPLOY_UNIT_PATH),
            {"AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK"},
        )

    def test_ui_unit_does_not_gain_deploy_only_netlink_access(self):
        self.assertEqual(
            address_families(UI_UNIT_PATH),
            {"AF_UNIX", "AF_INET"},
        )

    def test_ui_mode_receipts_use_systemd_managed_private_state(self):
        ui = UI_UNIT_PATH.read_text(encoding="utf-8")
        self.assertIn("StateDirectory=audio-control-ui", ui)
        self.assertIn("StateDirectoryMode=0700", ui)
        self.assertIn("ProtectHome=read-only", ui)

    def test_ui_sandbox_grants_only_exact_runtime_write_roots(self):
        self.assertEqual(
            read_write_paths(UI_UNIT_PATH),
            {
                "%h/Music/Audio-Aufnahmen",
                "%h/.local/state/audio/recordings-v1",
                "%h/.local/state/audio/profile-transitions-v1",
            },
        )
        self.assertNotIn("%h/.local/state", read_write_paths(UI_UNIT_PATH))
        self.assertNotIn("%h/.local/state/audio", read_write_paths(UI_UNIT_PATH))

    def test_ui_bootstraps_canonical_transition_root_before_sandboxed_start(self):
        ui = UI_UNIT_PATH.read_text(encoding="utf-8")
        prepare = (
            "ExecStartPre=+/usr/bin/python3 "
            "%h/.local/share/audio-control-ui/current/scripts/audio_control.py "
            "prepare-runtime-state"
        )
        start = (
            "ExecStart=/usr/bin/python3 "
            "%h/.local/share/audio-control-ui/current/scripts/audio_control.py serve"
        )
        self.assertIn(prepare, ui)
        self.assertIn(start, ui)
        self.assertLess(ui.index(prepare), ui.index(start))
        self.assertNotIn(
            "%h/.local/state/audio/profile-transitions-v1",
            read_write_paths(DEPLOY_UNIT_PATH),
        )

    def test_level_observer_is_pipewire_only_and_coupled_to_the_ui_lifecycle(self):
        self.assertEqual(address_families(LEVEL_OBSERVER_UNIT_PATH), {"AF_UNIX"})
        observer = LEVEL_OBSERVER_UNIT_PATH.read_text(encoding="utf-8")
        ui = UI_UNIT_PATH.read_text(encoding="utf-8")
        self.assertIn("PartOf=audio-control-ui-v1.service", observer)
        self.assertIn("RuntimeDirectory=audio-control-level-observer", observer)
        self.assertIn("--target auto", observer)
        self.assertIn("Wants=audio-control-level-observer-v1.service", ui)

    def test_qobuz_recovery_uses_systemd_exported_state_directory(self):
        self.assertEqual(address_families(QOBUZ_RECOVERY_UNIT_PATH), {"AF_UNIX"})
        recovery = QOBUZ_RECOVERY_UNIT_PATH.read_text(encoding="utf-8")
        ui = UI_UNIT_PATH.read_text(encoding="utf-8")
        self.assertIn("PartOf=audio-control-ui-v1.service", recovery)
        self.assertIn("StateDirectory=audio-qobuz-desktop-recovery", recovery)
        self.assertIn("StateDirectoryMode=0700", recovery)
        self.assertIn("${STATE_DIRECTORY}/state.json", recovery)
        self.assertNotIn("%S/audio-qobuz-desktop-recovery/state.json", recovery)
        self.assertNotIn("RuntimeDirectory=audio-qobuz-desktop-recovery", recovery)
        self.assertIn("qobuz_desktop_recovery.py run", recovery)
        self.assertIn("audio-qobuz-desktop-recovery-v1.service", ui)

    def test_qbzd_qconnect_recovery_is_loopback_only_and_ui_lifecycle_bound(self):
        self.assertEqual(
            address_families(QBZD_QCONNECT_RECOVERY_UNIT_PATH),
            {"AF_UNIX", "AF_INET"},
        )
        recovery = QBZD_QCONNECT_RECOVERY_UNIT_PATH.read_text(encoding="utf-8")
        ui = UI_UNIT_PATH.read_text(encoding="utf-8")
        self.assertIn("PartOf=audio-control-ui-v1.service", recovery)
        self.assertNotIn("Wants=qbzd.service", recovery)
        self.assertIn("After=network.target qbzd.service", recovery)
        self.assertIn("StateDirectory=audio-qbzd-qconnect-recovery", recovery)
        self.assertIn("StateDirectoryMode=0700", recovery)
        self.assertIn("${STATE_DIRECTORY}/state.json", recovery)
        self.assertIn("qbzd_qconnect_recovery.py run", recovery)
        self.assertIn("audio-qbzd-qconnect-recovery-v1.service", ui)


if __name__ == "__main__":
    unittest.main()
