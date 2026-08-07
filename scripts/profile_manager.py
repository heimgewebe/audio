#!/usr/bin/env python3
"""Deterministic, repository-only audio profile manager.

The module models every canonical audio profile and every directed transition.
It deliberately has no live PipeWire, ALSA, MIDI, service or process adapter.
The only mutating surface applies a reviewed plan to a caller-supplied JSON
simulation file with atomic replace and complete rollback evidence.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import pathlib
import stat
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
PRODUCT_CATALOG_PATH = ROOT / "profiles" / "audio-profiles.v1.json"
CONTRACT_CATALOG_PATH = ROOT / "profiles" / "audio-profile-contracts.v1.json"
MANAGER_ID = "audio-profile-manager-v1"
EFFECT_SCOPE = "repository-and-simulation-only"
REQUIRED_CONTRACT_FIELDS = frozenset(
    {
        "devices",
        "source",
        "sink",
        "midi",
        "monitoring",
        "rate",
        "quantum",
        "resampling",
        "dsp",
        "channels",
        "limits",
        "lifecycle",
        "readback",
        "rollback",
        "managed_routes",
        "protects_recording",
    }
)
SCALAR_STATE_FIELDS = (
    "source",
    "sink",
    "midi",
    "monitoring",
    "rate",
    "quantum",
    "resampling",
    "dsp",
    "channels",
    "limits",
)
RECORDING_STATES = frozenset({"inactive", "active", "unknown"})
OPERATION_KINDS = frozenset(
    {
        "set-field",
        "start-managed-service",
        "stop-managed-service",
        "add-managed-route",
        "remove-managed-route",
    }
)
MAX_JSON_BYTES = 1_048_576
STATE_PROJECTION_FIELDS = frozenset(
    {"active_profile", *SCALAR_STATE_FIELDS, "managed_services", "managed_routes"}
)
PLAN_DISCLOSURES = [
    "live-profile-apply-authority",
    "physical-hardware-readiness",
    "safe-listening-level",
    "recording-or-playback-effect",
]
PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "manager",
        "effect_scope",
        "source_profile",
        "target_profile",
        "catalog_sha256",
        "contracts_sha256",
        "observed_state_sha256",
        "observation",
        "before",
        "target",
        "operations",
        "rollback_operations",
        "blockers",
        "ready_for_simulated_apply",
        "read_only",
        "apply_mode",
        "requires_exact_plan_sha256",
        "preserves_foreign_processes",
        "preserves_foreign_routes",
        "forbids_global_audio_shutdown",
        "does_not_establish",
        "plan_sha256",
    }
)
APPLY_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "manager",
        "effect_scope",
        "plan_sha256",
        "plan",
        "idempotent",
        "operations_applied",
        "before_state_sha256",
        "after_state_sha256",
        "pre_state",
        "readback",
        "receipt_sha256",
    }
)


class ProfileManagerError(RuntimeError):
    """Controlled contract or simulation failure."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_payload(payload: Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_json_snapshot(path: pathlib.Path) -> tuple[dict[str, Any], bytes]:
    path = pathlib.Path(path)
    try:
        parent = path.parent.resolve(strict=True)
        if parent != path.parent.absolute():
            raise ProfileManagerError(
                "json-symlink-parent", "JSON parent path must not contain symlinks"
            )
        if path.is_symlink():
            raise ProfileManagerError("json-symlink", "JSON path must not be a symlink")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ProfileManagerError(
                    "json-not-regular", "JSON path is not regular"
                )
            if metadata.st_size > MAX_JSON_BYTES:
                raise ProfileManagerError(
                    "json-too-large", "JSON exceeds the byte limit"
                )
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(MAX_JSON_BYTES + 1)
        finally:
            os.close(fd)
    except ProfileManagerError:
        raise
    except OSError as exc:
        raise ProfileManagerError(
            "json-unavailable", "Cannot read JSON safely"
        ) from exc
    if len(raw) > MAX_JSON_BYTES:
        raise ProfileManagerError("json-too-large", "JSON exceeds the byte limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileManagerError(
            "json-unavailable", "Cannot parse valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ProfileManagerError("json-root-invalid", "JSON root must be an object")
    return payload, raw


def read_json(path: pathlib.Path) -> dict[str, Any]:
    payload, _raw = _read_json_snapshot(path)
    return payload


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ProfileManagerError(
            "contract-invalid", f"{label} must be a list of non-empty strings"
        )
    if len(value) != len(set(value)):
        raise ProfileManagerError("contract-invalid", f"{label} contains duplicates")
    return list(value)


def load_catalogs(
    product_path: pathlib.Path = PRODUCT_CATALOG_PATH,
    contract_path: pathlib.Path = CONTRACT_CATALOG_PATH,
) -> dict[str, Any]:
    product, product_raw = _read_json_snapshot(product_path)
    contracts, _contract_raw = _read_json_snapshot(contract_path)
    if (
        product.get("schema_version") != 1
        or product.get("kind") != "audio_profile_catalog"
    ):
        raise ProfileManagerError(
            "catalog-invalid", "Canonical profile catalog root contract is invalid"
        )
    product_profiles = product.get("profiles")
    if not isinstance(product_profiles, dict) or not product_profiles:
        raise ProfileManagerError(
            "catalog-invalid", "Canonical profile catalog has no profiles"
        )
    if contracts.get("schema_version") != 1:
        raise ProfileManagerError(
            "contract-invalid", "Transition contract schema version is unsupported"
        )
    if contracts.get("kind") != "audio_profile_transition_contract_catalog":
        raise ProfileManagerError(
            "contract-invalid", "Transition contract kind is invalid"
        )
    if (
        contracts.get("manager") != MANAGER_ID
        or contracts.get("effect_scope") != EFFECT_SCOPE
    ):
        raise ProfileManagerError(
            "contract-invalid", "Transition contract authority is invalid"
        )
    binding = contracts.get("canonical_profile_catalog")
    if not isinstance(binding, dict):
        raise ProfileManagerError(
            "catalog-binding-invalid", "Canonical catalog binding is missing"
        )
    observed_product_sha256 = sha256_bytes(product_raw)
    if binding.get("path") != "profiles/audio-profiles.v1.json":
        raise ProfileManagerError(
            "catalog-binding-invalid", "Canonical catalog path changed"
        )
    if binding.get("sha256") != observed_product_sha256:
        raise ProfileManagerError(
            "catalog-binding-invalid", "Canonical catalog hash changed"
        )
    if contracts.get("aliases") != {}:
        raise ProfileManagerError(
            "aliases-forbidden", "Profile aliases cannot create a second truth"
        )
    transition_profiles = contracts.get("profiles")
    if not isinstance(transition_profiles, dict):
        raise ProfileManagerError(
            "contract-invalid", "Transition contracts have no profiles object"
        )
    if set(transition_profiles) != set(product_profiles):
        raise ProfileManagerError(
            "profile-set-mismatch",
            "Transition profile ids must exactly match the canonical profile catalog",
        )
    for profile_id, spec in transition_profiles.items():
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or not isinstance(spec, dict)
        ):
            raise ProfileManagerError(
                "contract-invalid", "Profile transition contract is invalid"
            )
        missing = sorted(REQUIRED_CONTRACT_FIELDS - set(spec))
        unknown = sorted(set(spec) - REQUIRED_CONTRACT_FIELDS)
        if missing or unknown:
            raise ProfileManagerError(
                "contract-invalid",
                f"{profile_id} contract fields differ: missing={missing}, unknown={unknown}",
            )
        _string_list(spec["devices"], f"{profile_id}.devices")
        for field in ("source", "sink", "midi", "monitoring", "resampling", "dsp"):
            if not isinstance(spec[field], str) or not spec[field]:
                raise ProfileManagerError(
                    "contract-invalid", f"{profile_id}.{field} is invalid"
                )
        for field in ("rate", "quantum", "channels", "limits"):
            if not isinstance(spec[field], dict):
                raise ProfileManagerError(
                    "contract-invalid", f"{profile_id}.{field} must be an object"
                )
        lifecycle = spec["lifecycle"]
        if not isinstance(lifecycle, dict) or set(lifecycle) != {"start", "stop"}:
            raise ProfileManagerError(
                "contract-invalid", f"{profile_id}.lifecycle is invalid"
            )
        starts = _string_list(lifecycle["start"], f"{profile_id}.lifecycle.start")
        stops = _string_list(lifecycle["stop"], f"{profile_id}.lifecycle.stop")
        if set(starts) != set(stops):
            raise ProfileManagerError(
                "contract-invalid", f"{profile_id} start and stop ownership sets differ"
            )
        _string_list(spec["readback"], f"{profile_id}.readback")
        _string_list(spec["rollback"], f"{profile_id}.rollback")
        _string_list(spec["managed_routes"], f"{profile_id}.managed_routes")
        if spec["protects_recording"] is not True:
            raise ProfileManagerError(
                "recording-protection-invalid",
                f"{profile_id} does not protect recordings",
            )
    return {
        "product": product,
        "contracts": contracts,
        "product_sha256": observed_product_sha256,
        "contracts_sha256": sha256_payload(contracts),
    }


def profile_state(profile_id: str, catalogs: dict[str, Any]) -> dict[str, Any]:
    try:
        spec = catalogs["contracts"]["profiles"][profile_id]
    except KeyError as exc:
        raise ProfileManagerError(
            "profile-unknown", f"Unknown canonical profile: {profile_id}"
        ) from exc
    state: dict[str, Any] = {"active_profile": profile_id}
    for field in SCALAR_STATE_FIELDS:
        state[field] = copy.deepcopy(spec[field])
    state["managed_services"] = sorted(spec["lifecycle"]["start"])
    state["managed_routes"] = sorted(spec["managed_routes"])
    return state


def simulation_snapshot(
    profile_id: str,
    catalogs: dict[str, Any],
    *,
    recording_state: str = "inactive",
) -> dict[str, Any]:
    if recording_state not in RECORDING_STATES:
        raise ProfileManagerError(
            "recording-state-invalid", "Recording state is invalid"
        )
    return {
        "schema_version": 1,
        "kind": "audio_profile_simulation_state",
        "state_known": True,
        **profile_state(profile_id, catalogs),
        "recording": {"state": recording_state},
        "foreign_processes": [],
        "foreign_routes": [],
    }


def normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if snapshot.get("schema_version") != 1:
        raise ProfileManagerError(
            "snapshot-invalid", "Snapshot schema version is unsupported"
        )
    if snapshot.get("kind") != "audio_profile_simulation_state":
        raise ProfileManagerError("snapshot-invalid", "Snapshot kind is invalid")
    if not isinstance(snapshot.get("state_known"), bool):
        raise ProfileManagerError(
            "snapshot-invalid", "Snapshot state_known must be boolean"
        )
    recording = snapshot.get("recording")
    if (
        not isinstance(recording, dict)
        or recording.get("state") not in RECORDING_STATES
    ):
        raise ProfileManagerError(
            "snapshot-invalid", "Snapshot recording state is invalid"
        )
    normalized = copy.deepcopy(snapshot)
    for field in (
        "managed_services",
        "managed_routes",
        "foreign_processes",
        "foreign_routes",
    ):
        normalized[field] = sorted(_string_list(normalized.get(field, []), field))
    active_profile = normalized.get("active_profile")
    if active_profile is not None and (
        not isinstance(active_profile, str) or not active_profile
    ):
        raise ProfileManagerError("snapshot-invalid", "active_profile is invalid")
    for field in SCALAR_STATE_FIELDS:
        if field not in normalized:
            raise ProfileManagerError(
                "snapshot-invalid", f"Snapshot field is missing: {field}"
            )
    last_plan = normalized.get("last_plan_sha256")
    if last_plan is not None and (
        not isinstance(last_plan, str)
        or len(last_plan) != 64
        or any(char not in "0123456789abcdef" for char in last_plan)
    ):
        raise ProfileManagerError("snapshot-invalid", "last_plan_sha256 is invalid")
    return normalized


def state_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    projection = {"active_profile": snapshot["active_profile"]}
    for field in SCALAR_STATE_FIELDS:
        projection[field] = copy.deepcopy(snapshot[field])
    projection["managed_services"] = list(snapshot["managed_services"])
    projection["managed_routes"] = list(snapshot["managed_routes"])
    return projection


def doctor(
    snapshot: dict[str, Any], catalogs: dict[str, Any] | None = None
) -> dict[str, Any]:
    catalogs = catalogs or load_catalogs()
    observed = normalize_snapshot(snapshot)
    active_profile = observed["active_profile"]
    blockers: list[str] = []
    if not observed["state_known"]:
        blockers.append("state-unknown")
    if active_profile not in catalogs["contracts"]["profiles"]:
        blockers.append("active-profile-unknown")
    return {
        "schema_version": 1,
        "kind": "audio_profile_doctor",
        "manager": MANAGER_ID,
        "read_only": True,
        "effect_scope": EFFECT_SCOPE,
        "active_profile": active_profile,
        "recording_state": observed["recording"]["state"],
        "foreign_process_count": len(observed["foreign_processes"]),
        "foreign_route_count": len(observed["foreign_routes"]),
        "blockers": blockers,
        "ready_for_planning": not blockers,
        "snapshot_sha256": sha256_payload(observed),
        "catalog_sha256": catalogs["product_sha256"],
        "contracts_sha256": catalogs["contracts_sha256"],
    }


def _operation(kind: str, **fields: Any) -> dict[str, Any]:
    if kind not in OPERATION_KINDS:
        raise ProfileManagerError(
            "operation-invalid", f"Unsupported operation kind: {kind}"
        )
    return {"kind": kind, "owner": MANAGER_ID, **fields}


def build_operations(
    before: dict[str, Any], target: dict[str, Any]
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for field in SCALAR_STATE_FIELDS:
        if before[field] != target[field]:
            operations.append(
                _operation(
                    "set-field",
                    field=field,
                    before=copy.deepcopy(before[field]),
                    after=copy.deepcopy(target[field]),
                )
            )
    before_services = set(before["managed_services"])
    target_services = set(target["managed_services"])
    for service in sorted(before_services - target_services):
        operations.append(_operation("stop-managed-service", service=service))
    for service in sorted(target_services - before_services):
        operations.append(_operation("start-managed-service", service=service))
    before_routes = set(before["managed_routes"])
    target_routes = set(target["managed_routes"])
    for route in sorted(before_routes - target_routes):
        operations.append(_operation("remove-managed-route", route=route))
    for route in sorted(target_routes - before_routes):
        operations.append(_operation("add-managed-route", route=route))
    if before["active_profile"] != target["active_profile"]:
        operations.append(
            _operation(
                "set-field",
                field="active_profile",
                before=before["active_profile"],
                after=target["active_profile"],
            )
        )
    return operations


def rollback_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inverse: list[dict[str, Any]] = []
    for operation in reversed(operations):
        kind = operation["kind"]
        if kind == "set-field":
            inverse.append(
                _operation(
                    "set-field",
                    field=operation["field"],
                    before=copy.deepcopy(operation["after"]),
                    after=copy.deepcopy(operation["before"]),
                )
            )
        elif kind == "start-managed-service":
            inverse.append(
                _operation("stop-managed-service", service=operation["service"])
            )
        elif kind == "stop-managed-service":
            inverse.append(
                _operation("start-managed-service", service=operation["service"])
            )
        elif kind == "add-managed-route":
            inverse.append(_operation("remove-managed-route", route=operation["route"]))
        elif kind == "remove-managed-route":
            inverse.append(_operation("add-managed-route", route=operation["route"]))
        else:  # pragma: no cover - guarded by operation construction
            raise ProfileManagerError(
                "operation-invalid", f"Cannot invert operation: {kind}"
            )
    return inverse


def build_plan(
    source_profile: str,
    target_profile: str,
    snapshot: dict[str, Any],
    catalogs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    catalogs = catalogs or load_catalogs()
    if source_profile not in catalogs["contracts"]["profiles"]:
        raise ProfileManagerError(
            "profile-unknown", f"Unknown source profile: {source_profile}"
        )
    if target_profile not in catalogs["contracts"]["profiles"]:
        raise ProfileManagerError(
            "profile-unknown", f"Unknown target profile: {target_profile}"
        )
    observed = normalize_snapshot(snapshot)
    before = state_projection(observed)
    target = profile_state(target_profile, catalogs)
    operations = build_operations(before, target)
    blockers: list[str] = []
    if not observed["state_known"]:
        blockers.append("state-unknown")
    if observed["active_profile"] != source_profile:
        blockers.append("source-profile-mismatch")
    recording_state = observed["recording"]["state"]
    if recording_state != "inactive" and operations:
        blockers.append(f"recording-{recording_state}")
    unsigned = {
        "schema_version": 1,
        "kind": "audio_profile_transition_plan",
        "manager": MANAGER_ID,
        "effect_scope": EFFECT_SCOPE,
        "source_profile": source_profile,
        "target_profile": target_profile,
        "catalog_sha256": catalogs["product_sha256"],
        "contracts_sha256": catalogs["contracts_sha256"],
        "observed_state_sha256": sha256_payload(observed),
        "observation": copy.deepcopy(observed),
        "before": before,
        "target": target,
        "operations": operations,
        "rollback_operations": rollback_operations(operations),
        "blockers": sorted(set(blockers)),
        "ready_for_simulated_apply": not blockers,
        "read_only": True,
        "apply_mode": "reviewed-plan-to-simulation-file-only",
        "requires_exact_plan_sha256": True,
        "preserves_foreign_processes": True,
        "preserves_foreign_routes": True,
        "forbids_global_audio_shutdown": True,
        "does_not_establish": list(PLAN_DISCLOSURES),
    }
    return {**unsigned, "plan_sha256": sha256_payload(unsigned)}


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProfileManagerError("plan-invalid", f"{label} is not a SHA-256 digest")
    return value


def _validate_projection(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != STATE_PROJECTION_FIELDS:
        raise ProfileManagerError(
            "plan-invalid", f"{label} state projection is invalid"
        )
    if not isinstance(value["active_profile"], str) or not value["active_profile"]:
        raise ProfileManagerError("plan-invalid", f"{label}.active_profile is invalid")
    _string_list(value["managed_services"], f"{label}.managed_services")
    _string_list(value["managed_routes"], f"{label}.managed_routes")
    return value


def validate_plan(plan: dict[str, Any], catalogs: dict[str, Any] | None = None) -> None:
    catalogs = catalogs or load_catalogs()
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
        raise ProfileManagerError("plan-invalid", "Plan fields are not closed")
    if (
        plan["schema_version"] != 1
        or plan["kind"] != "audio_profile_transition_plan"
        or plan["manager"] != MANAGER_ID
        or plan["effect_scope"] != EFFECT_SCOPE
    ):
        raise ProfileManagerError("plan-invalid", "Plan root contract is invalid")
    if plan["catalog_sha256"] != catalogs["product_sha256"]:
        raise ProfileManagerError(
            "plan-invalid", "Plan product catalog binding drifted"
        )
    if plan["contracts_sha256"] != catalogs["contracts_sha256"]:
        raise ProfileManagerError(
            "plan-invalid", "Plan transition contract binding drifted"
        )
    observed_digest = _require_sha256(
        plan["observed_state_sha256"], "observed_state_sha256"
    )
    observation = normalize_snapshot(plan["observation"])
    if sha256_payload(observation) != observed_digest:
        raise ProfileManagerError(
            "plan-invalid", "Plan observation hash does not match observation"
        )
    expected_digest = _require_sha256(plan["plan_sha256"], "plan_sha256")
    profiles = catalogs["contracts"]["profiles"]
    source_profile = plan["source_profile"]
    target_profile = plan["target_profile"]
    if source_profile not in profiles or target_profile not in profiles:
        raise ProfileManagerError("plan-invalid", "Plan references an unknown profile")
    before = _validate_projection(plan["before"], "before")
    if before != state_projection(observation):
        raise ProfileManagerError(
            "plan-invalid", "Plan before state is not its observation"
        )
    target = _validate_projection(plan["target"], "target")
    expected_target = profile_state(target_profile, catalogs)
    if target != expected_target:
        raise ProfileManagerError("plan-invalid", "Plan target is not canonical")
    expected_operations = build_operations(before, expected_target)
    if plan["operations"] != expected_operations:
        raise ProfileManagerError("plan-invalid", "Plan operations are not canonical")
    if plan["rollback_operations"] != rollback_operations(expected_operations):
        raise ProfileManagerError(
            "plan-invalid", "Plan rollback is not the exact inverse"
        )
    expected_blockers: list[str] = []
    if not observation["state_known"]:
        expected_blockers.append("state-unknown")
    if observation["active_profile"] != source_profile:
        expected_blockers.append("source-profile-mismatch")
    recording_state = observation["recording"]["state"]
    if recording_state != "inactive" and expected_operations:
        expected_blockers.append(f"recording-{recording_state}")
    expected_blockers = sorted(set(expected_blockers))
    blockers = _string_list(plan["blockers"], "plan.blockers")
    if blockers != expected_blockers:
        raise ProfileManagerError("plan-invalid", "Plan blockers are not canonical")
    if plan["ready_for_simulated_apply"] is not (not blockers):
        raise ProfileManagerError("plan-invalid", "Plan readiness contradicts blockers")
    if plan["read_only"] is not True:
        raise ProfileManagerError("plan-invalid", "Plan must be read-only")
    if plan["apply_mode"] != "reviewed-plan-to-simulation-file-only":
        raise ProfileManagerError("plan-invalid", "Plan apply mode is invalid")
    for field in (
        "requires_exact_plan_sha256",
        "preserves_foreign_processes",
        "preserves_foreign_routes",
        "forbids_global_audio_shutdown",
    ):
        if plan[field] is not True:
            raise ProfileManagerError(
                "plan-invalid", f"Plan invariant is false: {field}"
            )
    if plan["does_not_establish"] != PLAN_DISCLOSURES:
        raise ProfileManagerError("plan-invalid", "Plan disclosures are invalid")
    unsigned = dict(plan)
    unsigned.pop("plan_sha256")
    if sha256_payload(unsigned) != expected_digest:
        raise ProfileManagerError(
            "plan-invalid", "Plan hash does not match its content"
        )


def public_diff(plan: dict[str, Any]) -> dict[str, Any]:
    validate_plan(plan)
    return {
        "schema_version": 1,
        "kind": "audio_profile_transition_diff",
        "manager": MANAGER_ID,
        "read_only": True,
        "effect_scope": EFFECT_SCOPE,
        "source_profile": plan["source_profile"],
        "target_profile": plan["target_profile"],
        "operations": copy.deepcopy(plan["operations"]),
        "blockers": list(plan["blockers"]),
        "ready_for_simulated_apply": plan["ready_for_simulated_apply"],
        "plan_sha256": plan["plan_sha256"],
    }


def _apply_operation(state: dict[str, Any], operation: dict[str, Any]) -> None:
    kind = operation["kind"]
    if kind == "set-field":
        state[operation["field"]] = copy.deepcopy(operation["after"])
        return
    if kind in {"start-managed-service", "stop-managed-service"}:
        values = set(state["managed_services"])
        service = operation["service"]
        if kind == "start-managed-service":
            values.add(service)
        else:
            values.discard(service)
        state["managed_services"] = sorted(values)
        return
    if kind in {"add-managed-route", "remove-managed-route"}:
        values = set(state["managed_routes"])
        route = operation["route"]
        if kind == "add-managed-route":
            values.add(route)
        else:
            values.discard(route)
        state["managed_routes"] = sorted(values)
        return
    raise ProfileManagerError("operation-invalid", f"Unsupported operation: {kind}")


def atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path = pathlib.Path(path)
    _read_json_snapshot(path)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        + b"\n"
    )
    if len(encoded) > MAX_JSON_BYTES:
        raise ProfileManagerError("json-too-large", "JSON exceeds the byte limit")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = pathlib.Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _receipt(unsigned: dict[str, Any]) -> dict[str, Any]:
    return {**unsigned, "receipt_sha256": sha256_payload(unsigned)}


def apply_simulated(
    plan: dict[str, Any],
    expected_plan_sha256: str,
    state_path: pathlib.Path,
) -> dict[str, Any]:
    catalogs = load_catalogs()
    validate_plan(plan, catalogs)
    if expected_plan_sha256 != plan["plan_sha256"]:
        raise ProfileManagerError(
            "plan-hash-mismatch", "Exact reviewed plan hash is required"
        )
    current = normalize_snapshot(read_json(state_path))
    if (
        current.get("last_plan_sha256") == plan["plan_sha256"]
        and state_projection(current) == plan["target"]
    ):
        return _receipt(
            {
                "schema_version": 1,
                "kind": "audio_profile_simulated_apply_receipt",
                "manager": MANAGER_ID,
                "effect_scope": EFFECT_SCOPE,
                "plan_sha256": plan["plan_sha256"],
                "plan": copy.deepcopy(plan),
                "idempotent": True,
                "operations_applied": 0,
                "before_state_sha256": sha256_payload(current),
                "after_state_sha256": sha256_payload(current),
                "pre_state": current,
                "readback": state_projection(current),
            }
        )
    if plan["blockers"]:
        raise ProfileManagerError("plan-blocked", ",".join(plan["blockers"]))
    if sha256_payload(current) != plan["observed_state_sha256"]:
        raise ProfileManagerError(
            "state-changed", "Simulation state changed after plan review"
        )
    if state_projection(current) != plan["before"]:
        raise ProfileManagerError(
            "plan-invalid", "Plan before state is not observed state"
        )
    recomputed = build_plan(
        plan["source_profile"], plan["target_profile"], current, catalogs
    )
    if recomputed != plan:
        raise ProfileManagerError(
            "plan-invalid", "Plan is not reproducible from observation"
        )
    after = copy.deepcopy(current)
    for operation in plan["operations"]:
        _apply_operation(after, operation)
    after["last_plan_sha256"] = plan["plan_sha256"]
    if state_projection(after) != plan["target"]:
        raise ProfileManagerError(
            "readback-mismatch", "Simulated target readback differs from plan"
        )
    before_sha256 = sha256_payload(current)
    after_sha256 = sha256_payload(after)
    atomic_write_json(state_path, after)
    readback = normalize_snapshot(read_json(state_path))
    if sha256_payload(readback) != after_sha256:
        raise ProfileManagerError(
            "readback-mismatch", "Atomic simulated write readback differs"
        )
    return _receipt(
        {
            "schema_version": 1,
            "kind": "audio_profile_simulated_apply_receipt",
            "manager": MANAGER_ID,
            "effect_scope": EFFECT_SCOPE,
            "plan_sha256": plan["plan_sha256"],
            "plan": copy.deepcopy(plan),
            "idempotent": False,
            "operations_applied": len(plan["operations"]),
            "before_state_sha256": before_sha256,
            "after_state_sha256": after_sha256,
            "pre_state": current,
            "readback": state_projection(readback),
        }
    )


def validate_receipt(
    receipt: dict[str, Any], catalogs: dict[str, Any] | None = None
) -> None:
    catalogs = catalogs or load_catalogs()
    if not isinstance(receipt, dict) or set(receipt) != APPLY_RECEIPT_FIELDS:
        raise ProfileManagerError(
            "receipt-invalid", "Apply receipt fields are not closed"
        )
    if (
        receipt["schema_version"] != 1
        or receipt["kind"] != "audio_profile_simulated_apply_receipt"
        or receipt["manager"] != MANAGER_ID
        or receipt["effect_scope"] != EFFECT_SCOPE
    ):
        raise ProfileManagerError("receipt-invalid", "Receipt root contract is invalid")
    expected = _require_sha256(receipt["receipt_sha256"], "receipt_sha256")
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256")
    if sha256_payload(unsigned) != expected:
        raise ProfileManagerError(
            "receipt-invalid", "Receipt hash does not match content"
        )
    plan = receipt["plan"]
    if not isinstance(plan, dict):
        raise ProfileManagerError("receipt-invalid", "Receipt plan is invalid")
    validate_plan(plan, catalogs)
    if receipt["plan_sha256"] != plan["plan_sha256"]:
        raise ProfileManagerError("receipt-invalid", "Receipt plan binding is invalid")
    _require_sha256(receipt["before_state_sha256"], "before_state_sha256")
    _require_sha256(receipt["after_state_sha256"], "after_state_sha256")
    if not isinstance(receipt["idempotent"], bool):
        raise ProfileManagerError(
            "receipt-invalid", "Receipt idempotence flag is invalid"
        )
    operations_applied = receipt["operations_applied"]
    if isinstance(operations_applied, bool) or not isinstance(operations_applied, int):
        raise ProfileManagerError(
            "receipt-invalid", "Receipt operation count is invalid"
        )
    pre_state = normalize_snapshot(receipt["pre_state"])
    if sha256_payload(pre_state) != receipt["before_state_sha256"]:
        raise ProfileManagerError(
            "receipt-invalid", "Receipt pre-state hash is invalid"
        )
    if receipt["readback"] != plan["target"]:
        raise ProfileManagerError(
            "receipt-invalid", "Receipt readback is not plan target"
        )
    if receipt["idempotent"]:
        if operations_applied != 0:
            raise ProfileManagerError(
                "receipt-invalid", "Idempotent receipt has operations"
            )
        if receipt["before_state_sha256"] != receipt["after_state_sha256"]:
            raise ProfileManagerError(
                "receipt-invalid", "Idempotent receipt changed state"
            )
        if pre_state.get("last_plan_sha256") != plan["plan_sha256"]:
            raise ProfileManagerError(
                "receipt-invalid", "Idempotent receipt lacks plan marker"
            )
        if state_projection(pre_state) != plan["target"]:
            raise ProfileManagerError(
                "receipt-invalid", "Idempotent state is not target"
            )
        return
    if operations_applied != len(plan["operations"]):
        raise ProfileManagerError("receipt-invalid", "Receipt operation count drifted")
    if receipt["before_state_sha256"] != plan["observed_state_sha256"]:
        raise ProfileManagerError(
            "receipt-invalid", "Receipt pre-state is not plan observation"
        )
    if state_projection(pre_state) != plan["before"]:
        raise ProfileManagerError(
            "receipt-invalid", "Receipt pre-state is not plan before"
        )
    derived = copy.deepcopy(pre_state)
    for operation in plan["operations"]:
        _apply_operation(derived, operation)
    derived["last_plan_sha256"] = plan["plan_sha256"]
    if sha256_payload(derived) != receipt["after_state_sha256"]:
        raise ProfileManagerError(
            "receipt-invalid", "Receipt after-state is not reproducible"
        )
    if state_projection(derived) != receipt["readback"]:
        raise ProfileManagerError(
            "receipt-invalid", "Receipt readback is not reproducible"
        )


def rollback_simulated(
    receipt: dict[str, Any],
    expected_receipt_sha256: str,
    state_path: pathlib.Path,
) -> dict[str, Any]:
    validate_receipt(receipt)
    if expected_receipt_sha256 != receipt["receipt_sha256"]:
        raise ProfileManagerError(
            "receipt-hash-mismatch", "Exact apply receipt hash is required"
        )
    if receipt.get("idempotent"):
        raise ProfileManagerError(
            "rollback-not-applicable", "Idempotent replay has no mutation to roll back"
        )
    current = normalize_snapshot(read_json(state_path))
    if sha256_payload(current) != receipt["after_state_sha256"]:
        raise ProfileManagerError(
            "rollback-drift", "Simulation state drifted after apply"
        )
    pre_state = normalize_snapshot(receipt["pre_state"])
    atomic_write_json(state_path, pre_state)
    readback = normalize_snapshot(read_json(state_path))
    if sha256_payload(readback) != receipt["before_state_sha256"]:
        raise ProfileManagerError(
            "rollback-readback-mismatch", "Rollback readback differs from pre-state"
        )
    return _receipt(
        {
            "schema_version": 1,
            "kind": "audio_profile_simulated_rollback_receipt",
            "manager": MANAGER_ID,
            "effect_scope": EFFECT_SCOPE,
            "apply_receipt_sha256": receipt["receipt_sha256"],
            "restored_state_sha256": receipt["before_state_sha256"],
            "readback": state_projection(readback),
        }
    )


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--state", type=pathlib.Path, required=True)
    for name in ("plan", "diff"):
        item = subparsers.add_parser(name)
        item.add_argument("--source", required=True)
        item.add_argument("--target", required=True)
        item.add_argument("--state", type=pathlib.Path, required=True)
    apply_parser = subparsers.add_parser("apply-simulated")
    apply_parser.add_argument("--plan", type=pathlib.Path, required=True)
    apply_parser.add_argument("--expected-plan-sha256", required=True)
    apply_parser.add_argument("--state", type=pathlib.Path, required=True)
    rollback_parser = subparsers.add_parser("rollback-simulated")
    rollback_parser.add_argument("--receipt", type=pathlib.Path, required=True)
    rollback_parser.add_argument("--expected-receipt-sha256", required=True)
    rollback_parser.add_argument("--state", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    try:
        catalogs = load_catalogs()
        if args.command == "doctor":
            _print(doctor(read_json(args.state), catalogs))
        elif args.command in {"plan", "diff"}:
            plan = build_plan(args.source, args.target, read_json(args.state), catalogs)
            _print(plan if args.command == "plan" else public_diff(plan))
        elif args.command == "apply-simulated":
            _print(
                apply_simulated(
                    read_json(args.plan), args.expected_plan_sha256, args.state
                )
            )
        elif args.command == "rollback-simulated":
            _print(
                rollback_simulated(
                    read_json(args.receipt),
                    args.expected_receipt_sha256,
                    args.state,
                )
            )
        else:  # pragma: no cover
            raise ProfileManagerError("command-invalid", "Unsupported command")
    except ProfileManagerError as exc:
        _print(
            {
                "schema_version": 1,
                "kind": "audio_profile_manager_error",
                "code": exc.code,
                "detail": exc.detail,
            }
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
