#!/usr/bin/env python3
"""Record explicit physical audio observations in a private atomic state file."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import stat
import tempfile
from collections.abc import Iterable
from typing import Any

CONTROL = re.compile(r"[\x00-\x1f\x7f]")
ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "inventory" / "physical-facts.v1.json"
TEMPLATE_PATH = ROOT / "inventory" / "physical-verification.v1.json"
MAX_STATE_BYTES = 65_536
LEGACY_PROMPT_ONLY_SOURCE_SHA256 = (
    "1b8822768b7d809543bb9f037003a828c08177061d54af76c56e58b142f6fd55"
)
LEGACY_PROMPT_ONLY_SUCCESSOR_SHA256 = (
    "39a8d395fb8ff44c7466c6c1cd217686ea3b638e6f022edf2ad7e4457fa4deea"
)
LEGACY_PROMPT_ONLY_CHANGED_FACTS = frozenset({"pioneer_pc_connection"})
DEFAULT_STATE = pathlib.Path(
    os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state")
) / "audio" / "physical" / "latest.v1.json"


def load_json(path: pathlib.Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def catalog_observation_sha256(
    fact_keys: Iterable[str], path: pathlib.Path | None = None
) -> str:
    """Hash global catalog rules plus only the fact specs bound by this state."""

    catalog = load_json(path or CATALOG_PATH)
    facts = catalog.get("facts")
    if not isinstance(facts, dict):
        raise ValueError("physical fact catalog has no facts object")
    key_set = set(fact_keys)
    if any(not isinstance(key, str) or not key for key in key_set):
        raise ValueError("physical observation contains an invalid fact key")
    keys = sorted(key_set)
    unknown = sorted(set(keys) - set(facts))
    if unknown:
        raise ValueError(f"physical observation refers to unknown facts: {', '.join(unknown)}")
    scoped = {key: value for key, value in catalog.items() if key != "facts"}
    scoped["facts"] = {key: facts[key] for key in keys}
    encoded = json.dumps(
        scoped, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def legacy_prompt_only_catalog_compatible(
    observed_sha256: Any, current_raw_sha256: str, fact_keys: Iterable[str]
) -> bool:
    """Allow the one proven prompt-only legacy transition for unaffected facts."""

    return bool(
        observed_sha256 == LEGACY_PROMPT_ONLY_SOURCE_SHA256
        and current_raw_sha256 == LEGACY_PROMPT_ONLY_SUCCESSOR_SHA256
        and set(fact_keys).isdisjoint(LEGACY_PROMPT_ONLY_CHANGED_FACTS)
    )


def parse_value(spec: dict[str, Any], raw: str) -> Any:
    kind = spec.get("type")
    if CONTROL.search(raw):
        raise ValueError("control characters are not allowed")
    if kind == "boolean":
        lowered = raw.lower()
        if lowered in {"true", "yes", "ja", "1"}:
            return True
        if lowered in {"false", "no", "nein", "0"}:
            return False
        raise ValueError("boolean value must be true or false")
    if kind == "enum":
        allowed = spec.get("values", [])
        if raw not in allowed:
            raise ValueError(f"value must be one of: {', '.join(allowed)}")
        return raw
    if kind == "string":
        maximum = int(spec.get("max_length", 120))
        value = raw.strip()
        if not value or len(value) > maximum:
            raise ValueError(f"string must contain 1 to {maximum} characters")
        return value
    raise ValueError(f"unsupported fact type: {kind}")


def validate_stored_value(spec: dict[str, Any], value: Any) -> None:
    kind = spec.get("type")
    if kind == "boolean":
        if type(value) is not bool:
            raise ValueError("stored boolean fact is not a boolean")
        return
    if kind == "enum":
        if not isinstance(value, str) or value not in spec.get("values", []):
            raise ValueError("stored enum fact is outside the catalog")
        return
    if kind == "string":
        if not isinstance(value, str):
            raise ValueError("stored string fact is not a string")
        if CONTROL.search(value) or value != value.strip():
            raise ValueError("stored string fact contains invalid characters or whitespace")
        maximum = int(spec.get("max_length", 120))
        if not value or len(value) > maximum:
            raise ValueError("stored string fact has an invalid length")
        return
    raise ValueError(f"unsupported fact type: {kind}")


def parse_timestamp(raw: Any, label: str) -> dt.datetime:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} is missing")
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid ISO timestamp") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "physical_audio_observation",
        "updated_at": None,
        "catalog_sha256": catalog_observation_sha256(()),
        "template_sha256": sha256_file(TEMPLATE_PATH),
        "facts": {},
        "does_not_establish": [
            "software-verification-of-analog-controls",
            "calibrated-safe-listening-level",
            "measured-round-trip-latency",
        ],
    }


def validate_state(path: pathlib.Path, payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 1 or payload.get("kind") != "physical_audio_observation":
        raise ValueError("physical observation state has the wrong schema or kind")
    if payload.get("template_sha256") != sha256_file(TEMPLATE_PATH):
        raise ValueError("physical verification template changed; review observations before reuse")
    catalog = load_json(CATALOG_PATH).get("facts", {})
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        raise ValueError("physical observation state has no facts object")
    unknown = sorted(set(facts) - set(catalog))
    if unknown:
        raise ValueError(f"physical observation state contains unknown facts: {', '.join(unknown)}")
    observed_catalog_sha256 = payload.get("catalog_sha256")
    current_raw_catalog_sha256 = sha256_file(CATALOG_PATH)
    expected_observation_sha256 = catalog_observation_sha256(facts)
    if not (
        observed_catalog_sha256 == expected_observation_sha256
        or observed_catalog_sha256 == current_raw_catalog_sha256
        or legacy_prompt_only_catalog_compatible(
            observed_catalog_sha256, current_raw_catalog_sha256, facts
        )
    ):
        raise ValueError("physical fact catalog changed; review observations before reuse")
    observed_times: list[dt.datetime] = []
    for key, item in facts.items():
        if not isinstance(item, dict):
            raise ValueError(f"stored fact is not an object: {key}")
        evidence = item.get("evidence")
        if evidence not in catalog[key].get("allowed_evidence", []):
            raise ValueError(f"stored fact has invalid evidence: {key}")
        if item.get("authority") != "explicit-human-observation":
            raise ValueError(f"stored fact has invalid authority: {key}")
        observed_times.append(
            parse_timestamp(item.get("observed_at"), f"stored fact timestamp: {key}")
        )
        validate_stored_value(catalog[key], item.get("value"))
    updated_at = payload.get("updated_at")
    if updated_at is None:
        if observed_times:
            raise ValueError("physical observation state has facts but no updated_at")
    else:
        updated = parse_timestamp(updated_at, "physical observation updated_at")
        if observed_times and updated < max(observed_times):
            raise ValueError("physical observation updated_at predates a stored fact")
    if path.is_symlink():
        raise ValueError("physical observation state must not be a symbolic link")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("physical observation state must have mode 0600")


def read_state(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    if path.stat().st_size > MAX_STATE_BYTES:
        raise ValueError(f"physical observation state exceeds {MAX_STATE_BYTES} bytes")
    payload = load_json(path)
    validate_state(path, payload)
    # Normalize raw/legacy catalog bindings only in memory. A later explicit
    # mutation persists the observation-scoped binding; reads remain read-only.
    payload["catalog_sha256"] = catalog_observation_sha256(payload.get("facts", {}))
    return payload


def atomic_write_private(path: pathlib.Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("physical observation state must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    if len(encoded) > MAX_STATE_BYTES:
        raise ValueError(f"physical observation state exceeds {MAX_STATE_BYTES} bytes")
    temp: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temp = pathlib.Path(handle.name)
        temp.chmod(0o600)
        temp.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def record_fact(
    state: dict[str, Any], key: str, raw: str, evidence: str, *, replace: bool = False
) -> None:
    catalog = load_json(CATALOG_PATH).get("facts", {})
    if key not in catalog:
        raise ValueError(f"unknown physical fact: {key}")
    if key in state.get("facts", {}) and not replace:
        raise ValueError(f"physical fact already exists; use --replace: {key}")
    spec = catalog[key]
    allowed_evidence = spec.get("allowed_evidence", [])
    if evidence not in allowed_evidence:
        raise ValueError(
            f"evidence for {key} must be one of: {', '.join(allowed_evidence)}"
        )
    value = parse_value(spec, raw)
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    state.setdefault("facts", {})[key] = {
        "value": value,
        "evidence": evidence,
        "observed_at": observed_at,
        "authority": "explicit-human-observation",
    }
    state["catalog_sha256"] = catalog_observation_sha256(state["facts"])
    state["updated_at"] = observed_at


def status_payload(state: dict[str, Any], state_path: pathlib.Path) -> dict[str, Any]:
    catalog = load_json(CATALOG_PATH).get("facts", {})
    facts = state.get("facts", {})
    unresolved = sorted(set(catalog) - set(facts))
    return {
        "schema_version": 1,
        "kind": "physical_audio_verification_status",
        "state_path": str(state_path),
        "resolved_count": len(facts),
        "total_count": len(catalog),
        "complete": not unresolved,
        "resolved": {
            key: value.get("value") for key, value in sorted(facts.items())
        },
        "unresolved": unresolved,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("init")
    record = sub.add_parser("record")
    record.add_argument("key")
    record.add_argument("value")
    record.add_argument("--evidence", required=True, choices=("visual", "measured"))
    record.add_argument("--replace", action="store_true")
    clear = sub.add_parser("clear")
    clear.add_argument("key")
    args = parser.parse_args()

    if args.command == "init":
        if args.state.exists():
            raise ValueError(f"physical observation state already exists: {args.state}")
        state = empty_state()
        atomic_write_private(args.state, state)
    else:
        state = read_state(args.state)
        if args.command == "record":
            record_fact(
                state, args.key, args.value, args.evidence, replace=args.replace
            )
            atomic_write_private(args.state, state)
        elif args.command == "clear":
            catalog = load_json(CATALOG_PATH).get("facts", {})
            if args.key not in catalog:
                raise ValueError(f"unknown physical fact: {args.key}")
            state.get("facts", {}).pop(args.key, None)
            state["catalog_sha256"] = catalog_observation_sha256(
                state.get("facts", {})
            )
            state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            atomic_write_private(args.state, state)
    print(json.dumps(status_payload(state, args.state), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
