import hashlib
import pathlib
import shutil
import tempfile
import unittest

from tests.test_h2_ingest import MODULE, make_source


def _file_hashes(root: pathlib.Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class H2RemoteInboxIdentityTests(unittest.TestCase):
    def test_remote_transport_preserves_h2_material_identity(self):
        scene = "170926_191401"
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            direct_parent = base / "direct"
            direct_parent.mkdir()
            direct_source = make_source(direct_parent, scene=scene)

            remote_source = (
                base / "H2-Remote-Inbox" / "ipad-260926"
            )
            remote_source.parent.mkdir()
            shutil.copytree(direct_source, remote_source)

            direct_before = _file_hashes(direct_source)
            remote_before = _file_hashes(remote_source)
            self.assertEqual(direct_before, remote_before)

            direct_result = MODULE.import_scene(
                scene,
                source_root=direct_source,
                library_root=base / "library-direct",
            )
            remote_result = MODULE.import_scene(
                scene,
                source_root=remote_source,
                library_root=base / "library-remote",
            )

            self.assertEqual(
                direct_result["material_id"],
                remote_result["material_id"],
            )
            self.assertEqual(
                direct_result["master_set_sha256"],
                remote_result["master_set_sha256"],
            )
            self.assertFalse(direct_result["source_mutated"])
            self.assertFalse(remote_result["source_mutated"])
            self.assertEqual(_file_hashes(direct_source), direct_before)
            self.assertEqual(_file_hashes(remote_source), remote_before)

            direct_verify = MODULE.verify_material(
                direct_result["material_id"],
                library_root=base / "library-direct",
            )
            remote_verify = MODULE.verify_material(
                remote_result["material_id"],
                library_root=base / "library-remote",
            )
            self.assertEqual(
                direct_verify["master_set_sha256"],
                remote_verify["master_set_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
