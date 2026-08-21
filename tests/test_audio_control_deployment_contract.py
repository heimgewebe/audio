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

    def test_level_observer_is_pipewire_only_and_coupled_to_the_ui_lifecycle(self):
        self.assertEqual(address_families(LEVEL_OBSERVER_UNIT_PATH), {"AF_UNIX"})
        observer = LEVEL_OBSERVER_UNIT_PATH.read_text(encoding="utf-8")
        ui = UI_UNIT_PATH.read_text(encoding="utf-8")
        self.assertIn("PartOf=audio-control-ui-v1.service", observer)
        self.assertIn("RuntimeDirectory=audio-control-level-observer", observer)
        self.assertIn("--target auto", observer)
        self.assertIn("Wants=audio-control-level-observer-v1.service", ui)

    def test_qobuz_recovery_is_narrow_and_coupled_to_the_ui_lifecycle(self):
        self.assertEqual(address_families(QOBUZ_RECOVERY_UNIT_PATH), {"AF_UNIX"})
        recovery = QOBUZ_RECOVERY_UNIT_PATH.read_text(encoding="utf-8")
        ui = UI_UNIT_PATH.read_text(encoding="utf-8")
        self.assertIn("PartOf=audio-control-ui-v1.service", recovery)
        self.assertIn("StateDirectory=audio-qobuz-desktop-recovery", recovery)
        self.assertIn("StateDirectoryMode=0700", recovery)
        self.assertIn("%S/audio-qobuz-desktop-recovery/state.json", recovery)
        self.assertNotIn("RuntimeDirectory=audio-qobuz-desktop-recovery", recovery)
        self.assertIn("qobuz_desktop_recovery.py run", recovery)
        self.assertIn("Wants=", ui)
        self.assertIn("audio-qobuz-desktop-recovery-v1.service", ui)


if __name__ == "__main__":
    unittest.main()
