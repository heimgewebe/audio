import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "calibration_pack", ROOT / "scripts/calibration_pack.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

REVISION = {
    "authority": "clean-git-checkout",
    "commit": "1" * 40,
    "tree": "2" * 40,
    "clean": True,
}
OTHER_REVISION = {
    "authority": "clean-git-checkout",
    "commit": "3" * 40,
    "tree": "4" * 40,
    "clean": True,
}


class CalibrationPackTests(unittest.TestCase):
    def create(self, name: str, output: pathlib.Path, created_at: str = "2026-07-31T00:00:00+00:00"):
        with mock.patch.object(
            MODULE, "repository_binding", return_value=REVISION
        ):
            return MODULE.create_pack(
                name,
                output,
                created_at=created_at,
            )

    def validate(self, output: pathlib.Path, expected_name: str | None = None):
        with mock.patch.object(MODULE, "repository_binding", return_value=REVISION):
            return MODULE.validate_pack(output, expected_name)

    @staticmethod
    def manifest(output: pathlib.Path) -> dict:
        return json.loads((output / MODULE.MANIFEST_NAME).read_text())

    @staticmethod
    def write_manifest(output: pathlib.Path, payload: dict) -> None:
        (output / MODULE.MANIFEST_NAME).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )

    def test_headphone_pack_is_bounded_nonplaying_and_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            result = self.create("headphone-reference", output)
            self.assertFalse(result["automatic_playback"])
            manifest = self.manifest(output)
            identity = manifest["identity"]
            self.assertFalse(identity["automatic_playback"])
            self.assertEqual(identity["signal"]["dbfs"], -20.0)
            self.assertGreaterEqual(len(identity["contracts"]), 10)
            self.assertEqual(manifest["pack_sha256"], MODULE.canonical_sha256(identity))
            with wave.open(str(output / "headphone-reference.wav"), "rb") as handle:
                self.assertEqual(handle.getframerate(), 48000)
                self.assertEqual(handle.getnchannels(), 1)
                self.assertEqual(handle.getsampwidth(), 2)
                self.assertEqual(handle.getnframes(), 240000)
            validation = self.validate(output, "headphone-reference")
            self.assertTrue(validation["valid"])
            self.assertFalse(validation["automatic_playback"])

    def test_mirrored_output_reference_packs_lower_non_target_chain(self):
        catalog = MODULE.load_catalog()
        headphone = catalog["headphone-reference"]["safety_gates"]
        receiver = catalog["receiver-reference"]["safety_gates"]
        self.assertTrue(any("Pioneer receiver" in gate for gate in headphone))
        self.assertTrue(any("non-target receiver chain" in gate for gate in headphone))
        self.assertTrue(any("Lake People volume" in gate for gate in receiver))
        self.assertTrue(any("non-target headphone chain" in gate for gate in receiver))

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name in ("headphone-reference", "receiver-reference"):
                output = root / name
                self.create(name, output)
                identity = self.manifest(output)["identity"]
                self.assertEqual(identity["safety_gates"], catalog[name]["safety_gates"])

    def test_voice_pack_has_no_wave_and_names_its_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            result = self.create("voice-gain", output)
            self.assertEqual(result["artifact_count"], 0)
            self.assertFalse(any(output.glob("*.wav")))
            identity = self.manifest(output)["identity"]
            self.assertEqual(
                identity["required_laboratory_gates"],
                ["voice-level-measurement"],
            )
            self.validate(output, "voice-gain")

    def test_pack_identity_is_deterministic_across_creation_times(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            first = root / "first"
            second = root / "second"
            first_receipt = self.create(
                "motu-loopback", first, "2026-07-31T00:00:00+00:00"
            )
            second_receipt = self.create(
                "motu-loopback", second, "2026-08-01T00:00:00+00:00"
            )
            self.assertEqual(first_receipt["pack_sha256"], second_receipt["pack_sha256"])
            self.assertEqual(
                (first / "motu-loopback.wav").read_bytes(),
                (second / "motu-loopback.wav").read_bytes(),
            )
            self.assertNotEqual(
                self.manifest(first)["created_at"], self.manifest(second)["created_at"]
            )

    def test_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            output.mkdir()
            with self.assertRaises(FileExistsError):
                self.create("motu-loopback", output)

    def test_cleans_staging_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            output = root / "pack"
            with mock.patch.object(
                MODULE.REFERENCE, "generate_samples", side_effect=RuntimeError("boom")
            ):
                with self.assertRaises(RuntimeError):
                    self.create("motu-loopback", output)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".pack.*")), [])

    def test_validation_rejects_artifact_byte_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("headphone-reference", output)
            with (output / "headphone-reference.wav").open("ab") as handle:
                handle.write(b"tamper")
            with self.assertRaisesRegex(ValueError, "artifact changed"):
                self.validate(output)

    def test_validation_rejects_manifest_limitation_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("voice-gain", output)
            manifest = self.manifest(output)
            manifest["does_not_establish"] = []
            self.write_manifest(output, manifest)
            with self.assertRaisesRegex(ValueError, "limitations changed"):
                self.validate(output)

    def test_wave_parser_uses_the_hash_bound_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("headphone-reference", output)
            original_open = MODULE.wave.open
            with mock.patch.object(MODULE.wave, "open", wraps=original_open) as opener:
                self.validate(output)
            parsed_source = opener.call_args.args[0]
            self.assertTrue(hasattr(parsed_source, "getvalue"))
            self.assertIsInstance(parsed_source.getvalue(), bytes)

    def test_validation_rejects_manifest_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("voice-gain", output)
            manifest = self.manifest(output)
            manifest["identity"]["automatic_playback"] = True
            self.write_manifest(output, manifest)
            with self.assertRaisesRegex(ValueError, "automatic playback"):
                self.validate(output)

    def test_validation_rejects_repository_revision_change(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("voice-gain", output)
            with mock.patch.object(
                MODULE, "repository_binding", return_value=OTHER_REVISION
            ):
                with self.assertRaisesRegex(ValueError, "revision changed"):
                    MODULE.validate_pack(output)

    def test_validation_rejects_contract_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("voice-gain", output)
            changed = MODULE.contract_bindings()
            changed[0] = dict(changed[0], sha256="f" * 64)
            with mock.patch.object(MODULE, "repository_binding", return_value=REVISION):
                with mock.patch.object(MODULE, "contract_bindings", return_value=changed):
                    with self.assertRaisesRegex(ValueError, "contract drift"):
                        MODULE.validate_pack(output)

    def test_validation_rejects_symlink_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            output = root / "pack"
            target = root / "target.wav"
            self.create("motu-loopback", output)
            artifact = output / "motu-loopback.wav"
            artifact.replace(target)
            artifact.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                self.validate(output)

    def test_validation_rejects_traversal_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("motu-loopback", output)
            manifest = self.manifest(output)
            manifest["identity"]["artifacts"][0]["path"] = "../escape.wav"
            manifest["pack_sha256"] = MODULE.canonical_sha256(manifest["identity"])
            self.write_manifest(output, manifest)
            with self.assertRaisesRegex(ValueError, "plain relative filename"):
                self.validate(output)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_validation_rejects_special_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("motu-loopback", output)
            artifact = output / "motu-loopback.wav"
            artifact.unlink()
            os.mkfifo(artifact)
            with self.assertRaisesRegex(ValueError, "non-regular"):
                self.validate(output)

    def test_validation_rejects_unexpected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("voice-gain", output)
            unexpected = output / "surprise.txt"
            unexpected.write_text("unexpected")
            unexpected.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "unexpected or missing"):
                self.validate(output)

    def test_validation_rejects_oversized_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("voice-gain", output)
            with mock.patch.object(MODULE, "MAX_PACK_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "total byte limit"):
                    self.validate(output)

    def test_catalog_rejects_unknown_profile_reference(self):
        packs = MODULE.load_catalog()
        packs = {name: dict(item) for name, item in packs.items()}
        packs["voice-gain"]["allowed_profiles"] = ["unknown-profile"]
        profiles = json.loads((ROOT / "profiles/audio-profiles.v1.json").read_text())
        physical = json.loads((ROOT / "inventory/physical-facts.v1.json").read_text())
        gates = json.loads((ROOT / "inventory/laboratory-gates.v1.json").read_text())
        with self.assertRaisesRegex(ValueError, "unknown profiles"):
            MODULE.validate_catalog_references(packs, profiles, physical, gates)

    def test_validation_rejects_unsafe_file_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("voice-gain", output)
            (output / MODULE.MANIFEST_NAME).chmod(0o600)
            with self.assertRaisesRegex(ValueError, "unsafe mode"):
                self.validate(output)

    def test_validation_rejects_timestamp_without_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "pack"
            self.create("voice-gain", output)
            manifest = self.manifest(output)
            manifest["created_at"] = "2026-07-31T00:00:00"
            self.write_manifest(output, manifest)
            with self.assertRaisesRegex(ValueError, "include a timezone"):
                self.validate(output)

    def test_revision_binding_rejects_invalid_value(self):
        invalid = dict(REVISION, commit="not-a-git-object")
        with self.assertRaisesRegex(ValueError, "canonical Git object"):
            MODULE.validate_revision_binding(invalid)

    def test_repository_binding_rejects_untracked_checkout(self):
        with mock.patch.object(
            MODULE, "_run_git", side_effect=[str(ROOT), "?? untracked-file"]
        ):
            with self.assertRaisesRegex(ValueError, "repository is not clean"):
                MODULE.repository_binding()

    def test_legacy_create_syntax_and_new_validate_syntax_parse(self):
        legacy = MODULE.parse_args(["voice-gain", "/tmp/pack"])
        self.assertEqual(legacy.command, "create")
        current = MODULE.parse_args(["validate", "/tmp/pack", "--pack", "voice-gain"])
        self.assertEqual(current.command, "validate")
        self.assertEqual(current.pack, "voice-gain")


if __name__ == "__main__":
    unittest.main()
