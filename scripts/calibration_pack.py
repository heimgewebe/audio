#!/usr/bin/env python3
"""Create and validate bounded non-playing calibration packs."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import wave
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
REFERENCE_PATH = ROOT / "scripts" / "reference_signal.py"
CATALOG_PATH = ROOT / "inventory" / "calibration-packs.v1.json"
CONTRACT_PATHS = (
    pathlib.Path("profiles/audio-profiles.v1.json"),
    pathlib.Path("profiles/reference-levels.v1.json"),
    pathlib.Path("inventory/physical-facts.v1.json"),
    pathlib.Path("inventory/physical-verification.v1.json"),
    pathlib.Path("inventory/signal-path.v1.json"),
    pathlib.Path("inventory/system-truth.v1.json"),
    pathlib.Path("inventory/laboratory-gates.v1.json"),
)
MAX_CONTRACT_BYTES = 2_000_000
MAX_MANIFEST_BYTES = 262_144
MAX_ARTIFACT_BYTES = 32_000_000
MAX_PACK_BYTES = 64_000_000
MANIFEST_NAME = "manifest.v2.json"
MANIFEST_LIMITATIONS = [
    "gate-completion",
    "safe-listening-level",
    "physical-cable-correctness",
    "measurement-result",
    "permission-to-play-a-signal",
]

SPEC = importlib.util.spec_from_file_location("reference_signal", REFERENCE_PATH)
REFERENCE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(REFERENCE)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def safe_read_bytes(
    path: pathlib.Path, *, root: pathlib.Path, maximum_bytes: int
) -> tuple[bytes, os.stat_result]:
    root = root.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes the allowed root: {path}") from exc
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"symbolic links are forbidden: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"path component is not a directory: {current}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ValueError(f"file cannot be opened safely: {candidate}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"path is not a regular file: {candidate}")
        if before.st_size < 1 or before.st_size > maximum_bytes:
            raise ValueError(
                f"file must contain 1 to {maximum_bytes} bytes: {candidate}"
            )
        chunks: list[bytes] = []
        total = 0
        with os.fdopen(os.dup(descriptor), "rb", closefd=True) as handle:
            while chunk := handle.read(1024 * 1024):
                total += len(chunk)
                if total > maximum_bytes:
                    raise ValueError(f"file grew beyond its byte limit: {candidate}")
                chunks.append(chunk)
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise ValueError(f"file changed while it was read: {candidate}")
        if total != before.st_size:
            raise ValueError(f"file byte count changed while it was read: {candidate}")
        return b"".join(chunks), before
    finally:
        os.close(descriptor)


def file_binding(
    relative_path: pathlib.Path, *, maximum_bytes: int = MAX_CONTRACT_BYTES
) -> dict[str, object]:
    path = ROOT / relative_path
    content, metadata = safe_read_bytes(
        path, root=ROOT, maximum_bytes=maximum_bytes
    )
    return {
        "path": relative_path.as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": metadata.st_size,
    }


def load_json_object(
    path: pathlib.Path, *, root: pathlib.Path, maximum_bytes: int
) -> dict[str, Any]:
    content, _ = safe_read_bytes(path, root=root, maximum_bytes=maximum_bytes)
    try:
        value = json.loads(content.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"JSON file is not UTF-8: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def validate_catalog_references(
    packs: dict[str, dict[str, Any]],
    profiles_payload: dict[str, Any],
    physical_payload: dict[str, Any],
    gates_payload: dict[str, Any],
) -> None:
    profiles = profiles_payload.get("profiles")
    facts = physical_payload.get("facts")
    gates = gates_payload.get("gates")
    if not all(isinstance(value, dict) for value in (profiles, facts, gates)):
        raise ValueError("referenced audio catalogs have invalid projections")
    for name, item in packs.items():
        unknown_profiles = sorted(set(item["allowed_profiles"]) - set(profiles))
        unknown_facts = sorted(set(item["required_physical_facts"]) - set(facts))
        unknown_gates = sorted(set(item["required_laboratory_gates"]) - set(gates))
        if unknown_profiles:
            raise ValueError(
                f"calibration pack {name} references unknown profiles: "
                + ", ".join(unknown_profiles)
            )
        if unknown_facts:
            raise ValueError(
                f"calibration pack {name} references unknown physical facts: "
                + ", ".join(unknown_facts)
            )
        if unknown_gates:
            raise ValueError(
                f"calibration pack {name} references unknown laboratory gates: "
                + ", ".join(unknown_gates)
            )


def load_catalog() -> dict[str, dict[str, Any]]:
    payload = load_json_object(
        CATALOG_PATH, root=ROOT, maximum_bytes=MAX_CONTRACT_BYTES
    )
    if payload.get("schema_version") != 1 or payload.get("kind") != (
        "audio_calibration_pack_catalog"
    ):
        raise ValueError("calibration-pack catalog schema is invalid")
    packs = payload.get("packs")
    if not isinstance(packs, dict) or not packs:
        raise ValueError("calibration-pack catalog has no packs")
    required_fields = {
        "purpose",
        "allowed_profiles",
        "required_physical_facts",
        "required_laboratory_gates",
        "signal",
        "safety_gates",
        "facts_or_receipts_to_record",
    }
    for name, item in packs.items():
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or not isinstance(item, dict)
            or set(item) != required_fields
        ):
            raise ValueError(f"invalid calibration-pack catalog entry: {name}")
        for field in (
            "allowed_profiles",
            "required_physical_facts",
            "required_laboratory_gates",
            "safety_gates",
            "facts_or_receipts_to_record",
        ):
            values = item[field]
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)
                or values != list(dict.fromkeys(values))
            ):
                raise ValueError(f"invalid {field} for calibration pack: {name}")
        signal = item["signal"]
        if signal is not None:
            if (
                not isinstance(signal, dict)
                or set(signal) != {
                    "kind",
                    "sample_rate_hz",
                    "channels",
                    "bit_depth",
                    "dbfs",
                    "duration_seconds",
                    "frequency_hz",
                }
                or signal["kind"] not in {"tone", "impulse"}
                or signal["sample_rate_hz"] != 48_000
                or signal["channels"] != 1
                or signal["bit_depth"] != 16
            ):
                raise ValueError(f"invalid signal contract for calibration pack: {name}")
    validate_catalog_references(
        packs,
        load_json_object(
            ROOT / "profiles/audio-profiles.v1.json",
            root=ROOT,
            maximum_bytes=MAX_CONTRACT_BYTES,
        ),
        load_json_object(
            ROOT / "inventory/physical-facts.v1.json",
            root=ROOT,
            maximum_bytes=MAX_CONTRACT_BYTES,
        ),
        load_json_object(
            ROOT / "inventory/laboratory-gates.v1.json",
            root=ROOT,
            maximum_bytes=MAX_CONTRACT_BYTES,
        ),
    )
    return packs


def _run_git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if result.returncode != 0:
        raise ValueError(
            f"repository revision cannot be established: {' '.join(arguments)}"
        )
    return result.stdout.strip()


def validate_revision_binding(value: Any) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "authority",
        "commit",
        "tree",
        "clean",
    }:
        raise ValueError("repository revision binding is invalid")
    if value.get("authority") != "clean-git-checkout" or value.get("clean") is not True:
        raise ValueError("calibration pack requires a clean Git revision")
    for label in ("commit", "tree"):
        object_id = value.get(label)
        if (
            not isinstance(object_id, str)
            or len(object_id) != 40
            or object_id != object_id.lower()
            or any(character not in "0123456789abcdef" for character in object_id)
        ):
            raise ValueError(f"repository {label} is not a canonical Git object")
    return dict(value)


def repository_binding() -> dict[str, object]:
    top = pathlib.Path(_run_git("rev-parse", "--show-toplevel")).resolve()
    if top != ROOT.resolve():
        raise ValueError("calibration generator is not in the repository root")
    dirty = _run_git("status", "--porcelain=v1", "--untracked-files=normal")
    if dirty:
        raise ValueError("repository is not clean; calibration pack is not usable")
    return validate_revision_binding(
        {
            "authority": "clean-git-checkout",
            "commit": _run_git("rev-parse", "HEAD"),
            "tree": _run_git("rev-parse", "HEAD^{tree}"),
            "clean": True,
        }
    )


def contract_bindings() -> list[dict[str, object]]:
    paths = (pathlib.Path("inventory/calibration-packs.v1.json"),) + CONTRACT_PATHS + (
        pathlib.Path("scripts/calibration_pack.py"),
        pathlib.Path("scripts/reference_signal.py"),
    )
    return [file_binding(path) for path in paths]


def atomic_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("calibration manifest exceeds its byte limit")
    temp: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temp = pathlib.Path(handle.name)
        temp.chmod(0o644)
        temp.replace(path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def _artifact_snapshot(
    path: pathlib.Path,
) -> tuple[dict[str, object], bytes]:
    content, metadata = safe_read_bytes(
        path, root=path.parent, maximum_bytes=MAX_ARTIFACT_BYTES
    )
    return (
        {
            "path": path.name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": metadata.st_size,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        },
        content,
    )


def _artifact_binding(path: pathlib.Path) -> dict[str, object]:
    binding, _ = _artifact_snapshot(path)
    return binding


def _build_identity(
    name: str,
    spec: dict[str, Any],
    artifacts: list[dict[str, object]],
    revision: dict[str, object],
    contracts: list[dict[str, object]],
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "pack": name,
        "automatic_playback": False,
        "repository": revision,
        "catalog_entry_sha256": canonical_sha256(spec),
        "contracts": contracts,
        "purpose": spec["purpose"],
        "allowed_profiles": spec["allowed_profiles"],
        "required_physical_facts": spec["required_physical_facts"],
        "required_laboratory_gates": spec["required_laboratory_gates"],
        "signal": spec["signal"],
        "safety_gates": spec["safety_gates"],
        "facts_or_receipts_to_record": spec["facts_or_receipts_to_record"],
        "artifacts": artifacts,
    }


def create_pack(
    name: str,
    output: pathlib.Path,
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    catalog = load_catalog()
    if name not in catalog:
        raise ValueError(f"unknown calibration pack: {name}")
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink():
        raise ValueError("calibration-pack parent must not be a symbolic link")
    revision = repository_binding()
    contracts = contract_bindings()
    staging = pathlib.Path(
        tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent)
    )
    try:
        spec = catalog[name]
        artifacts: list[dict[str, object]] = []
        signal = spec["signal"]
        if signal is not None:
            wav = staging / f"{name}.wav"
            samples = REFERENCE.generate_samples(
                signal["kind"],
                signal["sample_rate_hz"],
                signal["duration_seconds"],
                signal["dbfs"],
                signal["frequency_hz"],
            )
            REFERENCE.write_wav(wav, samples, signal["sample_rate_hz"])
            wav.chmod(0o644)
            artifacts.append(_artifact_binding(wav))
        identity = _build_identity(name, spec, artifacts, revision, contracts)
        creation_timestamp = (
            created_at or dt.datetime.now(dt.timezone.utc).isoformat()
        )
        parse_timestamp(creation_timestamp)
        manifest = {
            "schema_version": 2,
            "kind": "audio_calibration_pack",
            "created_at": creation_timestamp,
            "pack_sha256": canonical_sha256(identity),
            "identity": identity,
            "does_not_establish": list(MANIFEST_LIMITATIONS),
        }
        atomic_json(staging / MANIFEST_NAME, manifest)
        total_bytes = sum(item.stat().st_size for item in staging.iterdir())
        if total_bytes > MAX_PACK_BYTES:
            raise ValueError("calibration pack exceeds its total byte limit")
        staging.replace(output)
        return {
            "schema_version": 1,
            "kind": "audio_calibration_pack_creation_receipt",
            "pack": name,
            "path": str(output),
            "pack_sha256": manifest["pack_sha256"],
            "manifest": _artifact_binding(output / MANIFEST_NAME),
            "artifact_count": len(artifacts),
            "automatic_playback": False,
            "repository": revision,
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_relative_artifact_name(value: Any) -> str:
    if not isinstance(value, str) or not value or pathlib.PurePosixPath(value).name != value:
        raise ValueError("artifact path must be one plain relative filename")
    if value in {".", "..", MANIFEST_NAME}:
        raise ValueError("artifact path is reserved")
    return value


def _validate_repository_binding(observed: Any) -> dict[str, object]:
    if not isinstance(observed, dict):
        raise ValueError("repository binding is missing")
    observed = validate_revision_binding(observed)
    current = repository_binding()
    if observed != current:
        raise ValueError("repository revision changed since pack creation")
    return current


def _validate_contracts(observed: Any) -> list[dict[str, object]]:
    if not isinstance(observed, list):
        raise ValueError("contract bindings are missing")
    current = contract_bindings()
    if observed != current:
        raise ValueError("repository contract drift invalidates the calibration pack")
    return current


def _validate_wave(content: bytes, signal: dict[str, Any]) -> None:
    with wave.open(io.BytesIO(content), "rb") as handle:
        if handle.getframerate() != signal["sample_rate_hz"]:
            raise ValueError("calibration WAV sample rate changed")
        if handle.getnchannels() != signal["channels"]:
            raise ValueError("calibration WAV channel count changed")
        if handle.getsampwidth() * 8 != signal["bit_depth"]:
            raise ValueError("calibration WAV bit depth changed")
        expected_frames = round(
            signal["sample_rate_hz"] * signal["duration_seconds"]
        )
        if handle.getnframes() != expected_frames:
            raise ValueError("calibration WAV duration changed")


def parse_timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("calibration creation timestamp is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("calibration creation timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("calibration creation timestamp must include a timezone")
    return parsed


def validate_pack(path: pathlib.Path, expected_name: str | None = None) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("calibration pack must not be a symbolic link")
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("calibration pack path is not a directory")
    entries = list(os.scandir(path))
    if not entries:
        raise ValueError("calibration pack is empty")
    total_bytes = 0
    names: set[str] = set()
    for entry in entries:
        entry_metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(entry_metadata.st_mode):
            raise ValueError(f"symbolic links are forbidden: {entry.name}")
        if not stat.S_ISREG(entry_metadata.st_mode):
            raise ValueError(f"non-regular pack entry is forbidden: {entry.name}")
        if stat.S_IMODE(entry_metadata.st_mode) != 0o644:
            raise ValueError(f"calibration pack entry has an unsafe mode: {entry.name}")
        if entry.name in names:
            raise ValueError(f"duplicate pack entry: {entry.name}")
        names.add(entry.name)
        total_bytes += entry_metadata.st_size
    if total_bytes > MAX_PACK_BYTES:
        raise ValueError("calibration pack exceeds its total byte limit")
    if MANIFEST_NAME not in names:
        raise ValueError("calibration manifest is missing")
    manifest = load_json_object(
        path / MANIFEST_NAME, root=path, maximum_bytes=MAX_MANIFEST_BYTES
    )
    if set(manifest) != {
        "schema_version",
        "kind",
        "created_at",
        "pack_sha256",
        "identity",
        "does_not_establish",
    } or manifest.get("schema_version") != 2 or manifest.get("kind") != (
        "audio_calibration_pack"
    ):
        raise ValueError("calibration manifest schema is invalid")
    parse_timestamp(manifest.get("created_at"))
    if manifest.get("does_not_establish") != MANIFEST_LIMITATIONS:
        raise ValueError("calibration manifest limitations changed")
    identity = manifest.get("identity")
    expected_identity_fields = {
        "schema_version",
        "pack",
        "automatic_playback",
        "repository",
        "catalog_entry_sha256",
        "contracts",
        "purpose",
        "allowed_profiles",
        "required_physical_facts",
        "required_laboratory_gates",
        "signal",
        "safety_gates",
        "facts_or_receipts_to_record",
        "artifacts",
    }
    if (
        not isinstance(identity, dict)
        or set(identity) != expected_identity_fields
        or identity.get("schema_version") != 2
    ):
        raise ValueError("calibration identity is invalid")
    pack_name = identity.get("pack")
    catalog = load_catalog()
    if not isinstance(pack_name, str) or pack_name not in catalog:
        raise ValueError("calibration pack name is not catalogued")
    if expected_name is not None and pack_name != expected_name:
        raise ValueError("calibration pack name does not match the expectation")
    spec = catalog[pack_name]
    if identity.get("automatic_playback") is not False:
        raise ValueError("automatic playback is forbidden")
    if identity.get("catalog_entry_sha256") != canonical_sha256(spec):
        raise ValueError("calibration catalog entry changed")
    expected_projection = {
        "purpose": spec["purpose"],
        "allowed_profiles": spec["allowed_profiles"],
        "required_physical_facts": spec["required_physical_facts"],
        "required_laboratory_gates": spec["required_laboratory_gates"],
        "signal": spec["signal"],
        "safety_gates": spec["safety_gates"],
        "facts_or_receipts_to_record": spec["facts_or_receipts_to_record"],
    }
    for field, expected in expected_projection.items():
        if identity.get(field) != expected:
            raise ValueError(f"calibration identity field drifted: {field}")
    revision = _validate_repository_binding(identity.get("repository"))
    contracts = _validate_contracts(identity.get("contracts"))
    artifacts = identity.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("calibration artifact list is invalid")
    expected_names = {MANIFEST_NAME}
    seen_artifacts: set[str] = set()
    artifact_contents: dict[str, bytes] = {}
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "sha256",
            "bytes",
            "mode",
        }:
            raise ValueError("calibration artifact binding is invalid")
        name = _validate_relative_artifact_name(item["path"])
        if name in seen_artifacts:
            raise ValueError("duplicate calibration artifact binding")
        seen_artifacts.add(name)
        expected_names.add(name)
        artifact_path = path / name
        current, content = _artifact_snapshot(artifact_path)
        if item != current:
            raise ValueError(f"calibration artifact changed: {name}")
        artifact_contents[name] = content
    signal = spec["signal"]
    if signal is None:
        if artifacts:
            raise ValueError("signal-free calibration pack contains artifacts")
    else:
        expected_wav = f"{pack_name}.wav"
        if seen_artifacts != {expected_wav}:
            raise ValueError("calibration pack has the wrong signal artifact set")
        _validate_wave(artifact_contents[expected_wav], signal)
    if names != expected_names:
        raise ValueError("calibration pack contains unexpected or missing files")
    expected_pack_sha = canonical_sha256(identity)
    if manifest.get("pack_sha256") != expected_pack_sha:
        raise ValueError("calibration pack identity digest mismatch")
    return {
        "schema_version": 1,
        "kind": "audio_calibration_pack_validation_receipt",
        "valid": True,
        "pack": pack_name,
        "path": str(path),
        "pack_sha256": expected_pack_sha,
        "repository": revision,
        "contract_count": len(contracts),
        "artifact_count": len(artifacts),
        "automatic_playback": False,
        "does_not_establish": [
            "physical-safety",
            "measurement-result",
            "laboratory-gate-completion",
        ],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    catalog = load_catalog()
    if len(argv) == 2 and argv[0] in catalog:
        argv = ["create", *argv]
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("pack", choices=sorted(catalog))
    create.add_argument("output", type=pathlib.Path)
    validate = sub.add_parser("validate")
    validate.add_argument("path", type=pathlib.Path)
    validate.add_argument("--pack", choices=sorted(catalog))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "create":
        result = create_pack(args.pack, args.output)
    else:
        result = validate_pack(args.path, args.pack)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
