#!/usr/bin/env python3
"""Provider-neutral media and playlist contracts with offline-only simulation.

This module deliberately contains no network, raw transport, external command,
service, playback, or account adapter. A future provider integration must implement ``PlaylistPort``
behind a separately reviewed live boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import re
import unicodedata
from typing import Any, Protocol, Sequence

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "profiles" / "media-providers.v1.json"

PROVIDER_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
PROVIDER_REF_RE = re.compile(
    r"^(?P<provider>[a-z][a-z0-9-]{0,31}):(?P<kind>track):"
    r"(?P<item_id>[A-Za-z0-9][A-Za-z0-9._~-]{0,127})$"
)
OPAQUE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,160}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
RESAMPLING_VALUES = frozenset({"none", "single-stage", "unknown"})
PARALLEL_MIXING_VALUES = frozenset({"absent", "present", "unknown"})
IMPORT_FORMATS = frozenset({"text", "json", "provider-refs"})
IMPORT_OPERATIONS = frozenset({"add", "replace"})


class MediaProviderError(ValueError):
    """Fail-closed contract violation."""


class PlaylistPort(Protocol):
    """Narrow provider boundary used by reviewed write plans.

    T005 ships no production implementation of this protocol.
    """

    def export_playlist(
        self, *, provider: str, account: str, playlist_id: str
    ) -> dict[str, Any]: ...

    def replace_playlist(
        self,
        *,
        provider: str,
        account: str,
        playlist_id: str,
        expected_revision: str,
        tracks: Sequence[str],
    ) -> dict[str, Any]: ...


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise MediaProviderError(f"{label} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum:
        raise MediaProviderError(f"{label} is outside its text bound")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise MediaProviderError(f"{label} contains control characters")
    return normalized


def _opaque(value: Any, label: str) -> str:
    normalized = _normalize_text(value, label, maximum=160)
    if OPAQUE_RE.fullmatch(normalized) is None:
        raise MediaProviderError(f"{label} is invalid")
    return normalized


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MediaProviderError(f"{label} must be a positive integer")
    return value


def _with_digest(payload: dict[str, Any], field: str) -> dict[str, Any]:
    if field in payload:
        raise MediaProviderError(f"{field} is reserved")
    result = copy.deepcopy(payload)
    result[field] = sha256_json(payload)
    return result


def _verify_digest(value: dict[str, Any], field: str, label: str) -> None:
    claimed = value.get(field)
    if not isinstance(claimed, str) or HEX64_RE.fullmatch(claimed) is None:
        raise MediaProviderError(f"{label} digest is invalid")
    payload = copy.deepcopy(value)
    payload.pop(field, None)
    if sha256_json(payload) != claimed:
        raise MediaProviderError(f"{label} digest mismatch")


def load_catalog(path: pathlib.Path | None = None) -> dict[str, Any]:
    catalog_path = CATALOG_PATH if path is None else path
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "kind",
        "core_contract",
        "providers",
        "import_contract",
        "track_format_proof_contract",
        "write_contract",
        "live_effects",
        "decision_binding",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise MediaProviderError("media provider catalog shape is invalid")
    if (
        raw["schema_version"] != 1
        or raw["kind"] != "media_provider_catalog"
        or raw["core_contract"] != "provider-neutral-v1"
    ):
        raise MediaProviderError("media provider catalog identity is invalid")
    providers = raw["providers"]
    if not isinstance(providers, dict) or not providers:
        raise MediaProviderError("media provider catalog has no providers")
    for provider, config in providers.items():
        if PROVIDER_RE.fullmatch(provider) is None or not isinstance(config, dict):
            raise MediaProviderError("media provider catalog entry is invalid")
        expected = {
            "adapter",
            "adapter_role",
            "general_audio_core",
            "fallback_transport",
            "exclusive_or_bitperfect_claim",
            "exclusive_profile_requires",
            "provider_ref",
            "write_authority",
        }
        if set(config) != expected:
            raise MediaProviderError(f"media provider entry shape is invalid: {provider}")
        ref = config["provider_ref"]
        if (
            not isinstance(ref, dict)
            or set(ref) != {"kind", "prefix"}
            or ref["kind"] != "track"
            or ref["prefix"] != f"{provider}:track:"
        ):
            raise MediaProviderError(f"media provider ref contract is invalid: {provider}")
        if config["write_authority"] != "simulation-only-t005":
            raise MediaProviderError(f"media provider write boundary is invalid: {provider}")
    live = raw["live_effects"]
    live_fields = {
        "provider_account_mutation",
        "playlist_mutation",
        "playback_route_mutation",
        "production_provider_implementation",
    }
    if (
        not isinstance(live, dict)
        or set(live) != live_fields
        or any(type(live[field]) is not bool or live[field] is not False for field in live_fields)
    ):
        raise MediaProviderError("T005 catalog must deny all live effects")
    decision = raw["decision_binding"]
    if not isinstance(decision, dict) or not decision:
        raise MediaProviderError("media provider decision binding is invalid")
    return raw


def normalize_provider_ref(value: Any) -> str:
    if isinstance(value, str):
        text = _normalize_text(value, "provider ref", maximum=196)
        match = PROVIDER_REF_RE.fullmatch(text)
        if match is None:
            raise MediaProviderError("provider ref is invalid")
        return f"{match.group('provider')}:track:{match.group('item_id')}"
    if isinstance(value, dict):
        if set(value) == {"ref"}:
            return normalize_provider_ref(value["ref"])
        if set(value) != {"provider", "kind", "item_id"}:
            raise MediaProviderError("provider ref object shape is invalid")
        provider = _normalize_text(value["provider"], "provider", maximum=32)
        kind = _normalize_text(value["kind"], "provider item kind", maximum=16)
        item_id = _normalize_text(value["item_id"], "provider item id", maximum=128)
        if (
            PROVIDER_RE.fullmatch(provider) is None
            or kind != "track"
            or ITEM_ID_RE.fullmatch(item_id) is None
        ):
            raise MediaProviderError("provider ref object is invalid")
        return f"{provider}:track:{item_id}"
    raise MediaProviderError("provider ref must be text or an object")


def provider_from_ref(value: str) -> str:
    match = PROVIDER_REF_RE.fullmatch(normalize_provider_ref(value))
    assert match is not None
    return match.group("provider")


def _import_entries(source: Any, input_format: str) -> tuple[list[Any], list[dict[str, Any]]]:
    if input_format not in IMPORT_FORMATS:
        raise MediaProviderError("import format is not supported")
    if input_format == "text":
        if not isinstance(source, str):
            raise MediaProviderError("text import source must be text")
        normalized = unicodedata.normalize("NFC", source.replace("\r\n", "\n").replace("\r", "\n"))
        entries = [line.strip() for line in normalized.split("\n")]
        return [line for line in entries if line and not line.startswith("#")], []
    if input_format == "json":
        if not isinstance(source, str):
            raise MediaProviderError("JSON import source must be text")
        try:
            payload = json.loads(source)
        except (json.JSONDecodeError, UnicodeError):
            return [], [{"index": None, "code": "invalid-json"}]
        if isinstance(payload, dict):
            if set(payload) != {"tracks"} or not isinstance(payload["tracks"], list):
                return [], [{"index": None, "code": "invalid-json-root"}]
            return list(payload["tracks"]), []
        if isinstance(payload, list):
            return list(payload), []
        return [], [{"index": None, "code": "invalid-json-root"}]
    if isinstance(source, (str, bytes)) or not isinstance(source, Sequence):
        raise MediaProviderError("provider-refs source must be a sequence")
    return list(source), []


def normalize_import(
    source: Any,
    *,
    input_format: str,
    operation: str,
    dry_run: bool,
    existing_tracks: Sequence[str] = (),
) -> dict[str, Any]:
    if operation not in IMPORT_OPERATIONS:
        raise MediaProviderError("import operation is not supported")
    if not isinstance(dry_run, bool):
        raise MediaProviderError("dry_run must be boolean")
    existing = [normalize_provider_ref(item) for item in existing_tracks]
    if len(existing) != len(set(existing)):
        raise MediaProviderError("existing playlist contains duplicate provider refs")

    entries, errors = _import_entries(source, input_format)
    imported: list[str] = []
    first_indices: dict[str, int] = {}
    duplicates: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        try:
            ref = normalize_provider_ref(entry)
        except MediaProviderError as exc:
            errors.append({"index": index, "code": "invalid-provider-ref", "detail": str(exc)})
            continue
        if ref in first_indices:
            duplicates.append(
                {
                    "ref": ref,
                    "first_index": first_indices[ref],
                    "duplicate_index": index,
                }
            )
            continue
        first_indices[ref] = index
        imported.append(ref)

    if operation == "replace":
        desired = list(imported)
        skipped_existing: list[str] = []
    else:
        desired = list(existing)
        skipped_existing = []
        present = set(existing)
        for ref in imported:
            if ref in present:
                skipped_existing.append(ref)
                continue
            desired.append(ref)
            present.add(ref)

    payload = {
        "schema_version": 1,
        "kind": "media_import_manifest",
        "input_format": input_format,
        "operation": operation,
        "dry_run": dry_run,
        "existing_tracks": existing,
        "imported_tracks": imported,
        "desired_tracks": desired,
        "duplicates": duplicates,
        "skipped_existing": skipped_existing,
        "errors": errors,
    }
    return _with_digest(payload, "import_sha256")


def validate_import_manifest(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MediaProviderError("import manifest must be an object")
    required = {
        "schema_version",
        "kind",
        "input_format",
        "operation",
        "dry_run",
        "existing_tracks",
        "imported_tracks",
        "desired_tracks",
        "duplicates",
        "skipped_existing",
        "errors",
        "import_sha256",
    }
    if set(value) != required or value.get("schema_version") != 1 or value.get("kind") != "media_import_manifest":
        raise MediaProviderError("import manifest shape is invalid")
    _verify_digest(value, "import_sha256", "import manifest")
    for field in ("existing_tracks", "imported_tracks", "desired_tracks", "skipped_existing"):
        items = value.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise MediaProviderError(f"import manifest {field} is invalid")
        normalized = [normalize_provider_ref(item) for item in items]
        if normalized != items:
            raise MediaProviderError(f"import manifest {field} is not canonical")
    if value.get("operation") not in IMPORT_OPERATIONS or value.get("input_format") not in IMPORT_FORMATS:
        raise MediaProviderError("import manifest operation or format is invalid")
    if not isinstance(value.get("dry_run"), bool):
        raise MediaProviderError("import manifest dry_run is invalid")
    if not isinstance(value.get("duplicates"), list) or not isinstance(value.get("errors"), list):
        raise MediaProviderError("import manifest diagnostics are invalid")
    return copy.deepcopy(value)


def normalize_track_identity(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "provider",
        "kind",
        "item_id",
        "title",
        "artists",
        "album",
    }:
        raise MediaProviderError("track identity shape is invalid")
    ref = normalize_provider_ref(
        {
            "provider": value["provider"],
            "kind": value["kind"],
            "item_id": value["item_id"],
        }
    )
    artists = value["artists"]
    if not isinstance(artists, list) or not artists:
        raise MediaProviderError("track identity artists are invalid")
    normalized_artists = [_normalize_text(item, "track artist", maximum=160) for item in artists]
    if len(normalized_artists) != len(set(normalized_artists)):
        raise MediaProviderError("track identity artists contain duplicates")
    match = PROVIDER_REF_RE.fullmatch(ref)
    assert match is not None
    return {
        "provider": match.group("provider"),
        "kind": "track",
        "item_id": match.group("item_id"),
        "title": _normalize_text(value["title"], "track title", maximum=300),
        "artists": normalized_artists,
        "album": _normalize_text(value["album"], "track album", maximum=300),
    }


def build_track_format_proof(
    track_identity: dict[str, Any],
    *,
    container: str,
    codec: str,
    track_rate_hz: int,
    graph_rate_hz: int,
    endpoint_rate_hz: int,
    resampling: str,
    parallel_mixing: str,
) -> dict[str, Any]:
    identity = normalize_track_identity(track_identity)
    if resampling not in RESAMPLING_VALUES:
        raise MediaProviderError("resampling classification is invalid")
    if parallel_mixing not in PARALLEL_MIXING_VALUES:
        raise MediaProviderError("parallel mixing classification is invalid")
    rates = (
        _positive_int(track_rate_hz, "track rate"),
        _positive_int(graph_rate_hz, "graph rate"),
        _positive_int(endpoint_rate_hz, "endpoint rate"),
    )
    if resampling == "none" and len(set(rates)) != 1:
        raise MediaProviderError("no-resampling proof contradicts observed rates")
    payload = {
        "schema_version": 1,
        "kind": "track_format_proof",
        "track_identity": identity,
        "track_identity_sha256": sha256_json(identity),
        "container": _normalize_text(container, "container", maximum=32).lower(),
        "codec": _normalize_text(codec, "codec", maximum=32).lower(),
        "track_rate_hz": rates[0],
        "graph_rate_hz": rates[1],
        "endpoint_rate_hz": rates[2],
        "resampling": resampling,
        "parallel_mixing": parallel_mixing,
    }
    return _with_digest(payload, "proof_sha256")


def validate_track_format_proof(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "kind",
        "track_identity",
        "track_identity_sha256",
        "container",
        "codec",
        "track_rate_hz",
        "graph_rate_hz",
        "endpoint_rate_hz",
        "resampling",
        "parallel_mixing",
        "proof_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise MediaProviderError("track format proof shape is invalid")
    _verify_digest(value, "proof_sha256", "track format proof")
    identity = normalize_track_identity(value["track_identity"])
    if value["track_identity_sha256"] != sha256_json(identity):
        raise MediaProviderError("track title identity digest mismatch")
    rebuilt = build_track_format_proof(
        identity,
        container=value["container"],
        codec=value["codec"],
        track_rate_hz=value["track_rate_hz"],
        graph_rate_hz=value["graph_rate_hz"],
        endpoint_rate_hz=value["endpoint_rate_hz"],
        resampling=value["resampling"],
        parallel_mixing=value["parallel_mixing"],
    )
    if rebuilt != value:
        raise MediaProviderError("track format proof is not canonical")
    return copy.deepcopy(value)


def playlist_content_sha256(tracks: Sequence[str]) -> str:
    normalized = [normalize_provider_ref(item) for item in tracks]
    if len(normalized) != len(set(normalized)):
        raise MediaProviderError("playlist contains duplicate provider refs")
    return sha256_json(normalized)


def playlist_snapshot(
    *,
    provider: str,
    account: str,
    playlist_id: str,
    revision: str,
    tracks: Sequence[str],
) -> dict[str, Any]:
    provider_name = _normalize_text(provider, "provider", maximum=32)
    if PROVIDER_RE.fullmatch(provider_name) is None:
        raise MediaProviderError("provider is invalid")
    normalized_tracks = [normalize_provider_ref(item) for item in tracks]
    if any(provider_from_ref(item) != provider_name for item in normalized_tracks):
        raise MediaProviderError("playlist contains a ref from another provider")
    payload = {
        "schema_version": 1,
        "kind": "playlist_snapshot",
        "provider": provider_name,
        "account": _opaque(account, "account"),
        "playlist_id": _opaque(playlist_id, "playlist id"),
        "revision": _opaque(revision, "playlist revision"),
        "tracks": normalized_tracks,
        "content_sha256": playlist_content_sha256(normalized_tracks),
    }
    return _with_digest(payload, "snapshot_sha256")


def validate_playlist_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "kind",
        "provider",
        "account",
        "playlist_id",
        "revision",
        "tracks",
        "content_sha256",
        "snapshot_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise MediaProviderError("playlist snapshot shape is invalid")
    _verify_digest(value, "snapshot_sha256", "playlist snapshot")
    rebuilt = playlist_snapshot(
        provider=value["provider"],
        account=value["account"],
        playlist_id=value["playlist_id"],
        revision=value["revision"],
        tracks=value["tracks"],
    )
    if rebuilt != value:
        raise MediaProviderError("playlist snapshot is not canonical")
    return copy.deepcopy(value)


def _target(provider: str, account: str, playlist_id: str) -> dict[str, str]:
    provider_name = _normalize_text(provider, "provider", maximum=32)
    if PROVIDER_RE.fullmatch(provider_name) is None:
        raise MediaProviderError("provider is invalid")
    return {
        "provider": provider_name,
        "account": _opaque(account, "account"),
        "playlist_id": _opaque(playlist_id, "playlist id"),
    }


def build_write_plan(
    port: PlaylistPort,
    *,
    provider: str,
    account: str,
    playlist_id: str,
    import_manifest: dict[str, Any],
) -> dict[str, Any]:
    manifest = validate_import_manifest(import_manifest)
    if manifest["errors"]:
        raise MediaProviderError("import manifest contains errors")
    target = _target(provider, account, playlist_id)
    effective_catalog = load_catalog()
    providers = effective_catalog["providers"]
    if not isinstance(providers, dict) or target["provider"] not in providers:
        raise MediaProviderError("target provider is not catalogued")
    preimage = validate_playlist_snapshot(port.export_playlist(**target))
    if any(preimage[key] != target[key] for key in target):
        raise MediaProviderError("provider export target mismatch")
    if manifest["existing_tracks"] != preimage["tracks"]:
        raise MediaProviderError("import manifest is stale against provider preimage")
    desired_tracks = list(manifest["desired_tracks"])
    if any(provider_from_ref(item) != target["provider"] for item in desired_tracks):
        raise MediaProviderError("write plan contains a ref from another provider")
    payload = {
        "schema_version": 1,
        "kind": "playlist_write_plan",
        "target": target,
        "catalog_sha256": sha256_json(effective_catalog),
        "operation": manifest["operation"],
        "dry_run": manifest["dry_run"],
        "expected_revision": preimage["revision"],
        "preimage_export": preimage,
        "preimage_export_sha256": preimage["snapshot_sha256"],
        "import_manifest": manifest,
        "import_manifest_sha256": manifest["import_sha256"],
        "desired_tracks": desired_tracks,
        "desired_content_sha256": playlist_content_sha256(desired_tracks),
    }
    return _with_digest(payload, "plan_sha256")


def validate_write_plan(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "kind",
        "target",
        "catalog_sha256",
        "operation",
        "dry_run",
        "expected_revision",
        "preimage_export",
        "preimage_export_sha256",
        "import_manifest",
        "import_manifest_sha256",
        "desired_tracks",
        "desired_content_sha256",
        "plan_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise MediaProviderError("playlist write plan shape is invalid")
    _verify_digest(value, "plan_sha256", "playlist write plan")
    target = value["target"]
    if not isinstance(target, dict) or set(target) != {"provider", "account", "playlist_id"}:
        raise MediaProviderError("playlist write target is invalid")
    canonical_target = _target(target["provider"], target["account"], target["playlist_id"])
    if canonical_target != target:
        raise MediaProviderError("playlist write target is not canonical")
    catalog = load_catalog()
    if value["catalog_sha256"] != sha256_json(catalog):
        raise MediaProviderError("playlist write catalog binding mismatch")
    if target["provider"] not in catalog["providers"]:
        raise MediaProviderError("playlist write provider is not catalogued")
    preimage = validate_playlist_snapshot(value["preimage_export"])
    manifest = validate_import_manifest(value["import_manifest"])
    if value["preimage_export_sha256"] != preimage["snapshot_sha256"]:
        raise MediaProviderError("playlist write preimage binding mismatch")
    if value["import_manifest_sha256"] != manifest["import_sha256"]:
        raise MediaProviderError("playlist write import binding mismatch")
    if value["expected_revision"] != preimage["revision"]:
        raise MediaProviderError("playlist write revision binding mismatch")
    if manifest["existing_tracks"] != preimage["tracks"]:
        raise MediaProviderError("playlist write preimage content mismatch")
    if value["desired_tracks"] != manifest["desired_tracks"]:
        raise MediaProviderError("playlist write desired content mismatch")
    if value["desired_content_sha256"] != playlist_content_sha256(value["desired_tracks"]):
        raise MediaProviderError("playlist write desired digest mismatch")
    if value["operation"] != manifest["operation"] or value["dry_run"] != manifest["dry_run"]:
        raise MediaProviderError("playlist write import mode mismatch")
    return copy.deepcopy(value)


def _receipt(payload: dict[str, Any], field: str = "receipt_sha256") -> dict[str, Any]:
    return _with_digest(payload, field)


def apply_write_plan(
    port: PlaylistPort, plan: dict[str, Any], *, reviewed_plan_sha256: str
) -> dict[str, Any]:
    validated = validate_write_plan(plan)
    if reviewed_plan_sha256 != validated["plan_sha256"]:
        raise MediaProviderError("reviewed plan digest mismatch")
    if validated["dry_run"]:
        raise MediaProviderError("dry-run plan cannot be applied")
    target = validated["target"]
    current = validate_playlist_snapshot(port.export_playlist(**target))
    if any(current[key] != target[key] for key in target):
        raise MediaProviderError("provider readback target mismatch")

    desired_digest = validated["desired_content_sha256"]
    if current["content_sha256"] == desired_digest and current["tracks"] == validated["desired_tracks"]:
        postimage = current
        operations_applied = 0
        result = "already-desired"
    else:
        if current["snapshot_sha256"] != validated["preimage_export_sha256"]:
            raise MediaProviderError("playlist write plan is stale")
        port.replace_playlist(
            **target,
            expected_revision=current["revision"],
            tracks=validated["desired_tracks"],
        )
        postimage = validate_playlist_snapshot(port.export_playlist(**target))
        if (
            postimage["tracks"] != validated["desired_tracks"]
            or postimage["content_sha256"] != desired_digest
        ):
            raise MediaProviderError("playlist write readback mismatch")
        operations_applied = 1
        result = "applied"

    return _receipt(
        {
            "schema_version": 1,
            "kind": "playlist_write_receipt",
            "target": copy.deepcopy(target),
            "plan_sha256": validated["plan_sha256"],
            "operation": validated["operation"],
            "result": result,
            "operations_applied": operations_applied,
            "preimage": validated["preimage_export"],
            "postimage": postimage,
            "readback_content_sha256": postimage["content_sha256"],
        }
    )


def validate_write_receipt(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "kind",
        "target",
        "plan_sha256",
        "operation",
        "result",
        "operations_applied",
        "preimage",
        "postimage",
        "readback_content_sha256",
        "receipt_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise MediaProviderError("playlist write receipt shape is invalid")
    _verify_digest(value, "receipt_sha256", "playlist write receipt")
    if value["result"] not in {"applied", "already-desired"}:
        raise MediaProviderError("playlist write receipt result is invalid")
    if value["operations_applied"] not in {0, 1}:
        raise MediaProviderError("playlist write operation count is invalid")
    if (
        (value["result"] == "applied" and value["operations_applied"] != 1)
        or (value["result"] == "already-desired" and value["operations_applied"] != 0)
    ):
        raise MediaProviderError("playlist write receipt result contradicts operation count")
    if value["operation"] not in IMPORT_OPERATIONS:
        raise MediaProviderError("playlist write receipt operation is invalid")
    plan_digest = value["plan_sha256"]
    if not isinstance(plan_digest, str) or HEX64_RE.fullmatch(plan_digest) is None:
        raise MediaProviderError("playlist write receipt plan digest is invalid")
    target = value["target"]
    if not isinstance(target, dict) or set(target) != {"provider", "account", "playlist_id"}:
        raise MediaProviderError("playlist write receipt target is invalid")
    if _target(target["provider"], target["account"], target["playlist_id"]) != target:
        raise MediaProviderError("playlist write receipt target is not canonical")
    preimage = validate_playlist_snapshot(value["preimage"])
    postimage = validate_playlist_snapshot(value["postimage"])
    if value["readback_content_sha256"] != postimage["content_sha256"]:
        raise MediaProviderError("playlist write receipt readback digest mismatch")
    if any(
        image[key] != target[key]
        for image in (preimage, postimage)
        for key in ("provider", "account", "playlist_id")
    ):
        raise MediaProviderError("playlist write receipt target drift")
    return copy.deepcopy(value)


def rollback_write(
    port: PlaylistPort,
    plan: dict[str, Any],
    write_receipt: dict[str, Any],
    *,
    expected_receipt_sha256: str,
) -> dict[str, Any]:
    validated_plan = validate_write_plan(plan)
    receipt = validate_write_receipt(write_receipt)
    if receipt["receipt_sha256"] != expected_receipt_sha256:
        raise MediaProviderError("rollback receipt digest mismatch")
    if receipt["plan_sha256"] != validated_plan["plan_sha256"]:
        raise MediaProviderError("rollback plan binding mismatch")
    if receipt["target"] != validated_plan["target"]:
        raise MediaProviderError("rollback target binding mismatch")
    if receipt["operation"] != validated_plan["operation"]:
        raise MediaProviderError("rollback operation binding mismatch")
    if receipt["preimage"] != validated_plan["preimage_export"]:
        raise MediaProviderError("rollback preimage binding mismatch")
    if (
        receipt["postimage"]["tracks"] != validated_plan["desired_tracks"]
        or receipt["postimage"]["content_sha256"] != validated_plan["desired_content_sha256"]
    ):
        raise MediaProviderError("rollback postimage binding mismatch")
    target = validated_plan["target"]
    current = validate_playlist_snapshot(port.export_playlist(**target))
    preimage = validated_plan["preimage_export"]
    already_restored = (
        current["content_sha256"] == preimage["content_sha256"]
        and current["tracks"] == preimage["tracks"]
    )
    if receipt["operations_applied"] == 0 and not already_restored:
        raise MediaProviderError("no-op write receipt cannot authorize rollback")

    if already_restored:
        restored = current
        operations_applied = 0
        result = "already-restored"
    else:
        if current["snapshot_sha256"] != receipt["postimage"]["snapshot_sha256"]:
            raise MediaProviderError("rollback refused after playlist drift")
        port.replace_playlist(
            **target,
            expected_revision=current["revision"],
            tracks=preimage["tracks"],
        )
        restored = validate_playlist_snapshot(port.export_playlist(**target))
        if (
            restored["tracks"] != preimage["tracks"]
            or restored["content_sha256"] != preimage["content_sha256"]
        ):
            raise MediaProviderError("playlist rollback readback mismatch")
        operations_applied = 1
        result = "restored"

    return _receipt(
        {
            "schema_version": 1,
            "kind": "playlist_rollback_receipt",
            "target": copy.deepcopy(target),
            "plan_sha256": validated_plan["plan_sha256"],
            "write_receipt_sha256": receipt["receipt_sha256"],
            "result": result,
            "operations_applied": operations_applied,
            "restored_content_sha256": restored["content_sha256"],
            "restored": restored,
        }
    )


class SimulatedPlaylistProvider:
    """Deterministic in-memory provider for T005 tests and dry integration work."""

    def __init__(self) -> None:
        self._playlists: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._counters: dict[tuple[str, str, str], int] = {}
        self.write_count = 0

    def seed_playlist(
        self,
        *,
        provider: str,
        account: str,
        playlist_id: str,
        revision: str,
        tracks: Sequence[str],
    ) -> dict[str, Any]:
        target = _target(provider, account, playlist_id)
        key = (target["provider"], target["account"], target["playlist_id"])
        snapshot = playlist_snapshot(**target, revision=revision, tracks=tracks)
        self._playlists[key] = snapshot
        self._counters[key] = 0
        return copy.deepcopy(snapshot)

    def export_playlist(
        self, *, provider: str, account: str, playlist_id: str
    ) -> dict[str, Any]:
        target = _target(provider, account, playlist_id)
        key = (target["provider"], target["account"], target["playlist_id"])
        if key not in self._playlists:
            raise MediaProviderError("simulated playlist does not exist")
        return copy.deepcopy(self._playlists[key])

    def replace_playlist(
        self,
        *,
        provider: str,
        account: str,
        playlist_id: str,
        expected_revision: str,
        tracks: Sequence[str],
    ) -> dict[str, Any]:
        target = _target(provider, account, playlist_id)
        key = (target["provider"], target["account"], target["playlist_id"])
        current = self.export_playlist(**target)
        if current["revision"] != expected_revision:
            raise MediaProviderError("simulated provider revision mismatch")
        self._counters[key] = self._counters.get(key, 0) + 1
        revision = f"sim-{self._counters[key]:08d}"
        snapshot = playlist_snapshot(**target, revision=revision, tracks=tracks)
        self._playlists[key] = snapshot
        self.write_count += 1
        return copy.deepcopy(snapshot)
