import hashlib
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "ui" / "index.html"
LEGACY_INDEX_BLOB_SHA = "4a1e80316512a24f780359c8f7e45194226c4f88"
DEPLOYMENT_CONTRACT = (
    '<meta\n'
    '      name="audio-control-deployment-contract"\n'
    '      content="revision-bound-v1"\n'
    '    >'
)


class AudioControlDeploymentContractTests(unittest.TestCase):
    def test_index_distinguishes_revision_bound_first_hop_from_legacy(self):
        payload = INDEX_PATH.read_bytes()
        git_blob = hashlib.sha1(
            f"blob {len(payload)}\0".encode("ascii") + payload,
            usedforsecurity=False,
        ).hexdigest()

        self.assertNotEqual(git_blob, LEGACY_INDEX_BLOB_SHA)
        self.assertIn(DEPLOYMENT_CONTRACT, payload.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
