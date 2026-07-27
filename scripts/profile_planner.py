#!/usr/bin/env python3
"""Read-only profile readiness and drift planner."""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "audio-profiles.v1.json"
PHYSICAL_SCRIPT = ROOT / "scripts" / "physical_verification.py"
DOCTOR_SCRIPT = ROOT / "scripts" / "audio_doctor.py"


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PHYSICAL = load_module("physical_verification", PHYSICAL_SCRIPT)
DOCTOR = load_module("audio_doctor", DOCTOR_SCRIPT)


def doctor_report() -> dict[str, Any]:
    results = [DOCTOR.run_read_only(command) for command in DOCTOR.READ_ONLY_COMMANDS]
    return DOCTOR.build_report(results, DOCTOR.read_eld_text())


def load_profiles() -> dict[str, Any]:
    payload = json.loads(PROFILE_PATH.read_text())
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError("profile catalog has no profiles object")
    return profiles


def plan(profile: str, state_path: pathlib.Path) -> dict[str, Any]:
    catalog = load_profiles()
    if profile not in catalog:
        raise ValueError(f"unknown profile: {profile}")
    spec = catalog[profile]
    doctor = doctor_report()
    state = PHYSICAL.read_state(state_path)
    facts = {
        key: item.get("value")
        for key, item in state.get("facts", {}).items()
        if isinstance(item, dict)
    }
    missing_hardware = [
        key
        for key in spec.get("required_hardware", [])
        if not doctor["hardware"].get(key, False)
    ]
    missing_facts = [
        key for key in spec.get("required_physical_facts", []) if key not in facts
    ]
    mismatched_facts: list[dict[str, Any]] = []
    for key, expected in spec.get("required_fact_values", {}).items():
        if key in facts and facts[key] != expected:
            mismatched_facts.append(
                {"fact": key, "expected": expected, "observed": facts[key]}
            )
    unresolved_laboratory_gates = list(spec.get("required_laboratory_gates", []))
    desired = spec.get("desired", {})
    observed = doctor.get("graph", {})
    proposed_changes: list[dict[str, Any]] = []
    observed_keys = {
        "default_sink": "default_sink",
        "default_source": "default_source",
        "rate_hz": "force_rate_hz",
        "quantum_frames": "force_quantum_frames",
    }
    for field, observed_key in observed_keys.items():
        if field not in desired:
            continue
        if observed.get(observed_key) != desired[field]:
            proposed_changes.append(
                {
                    "field": field,
                    "from": observed.get(observed_key),
                    "to": desired[field],
                }
            )
    ready = (
        not missing_hardware
        and not missing_facts
        and not mismatched_facts
        and not unresolved_laboratory_gates
    )
    return {
        "schema_version": 1,
        "kind": "audio_profile_plan",
        "profile": profile,
        "purpose": spec["purpose"],
        "read_only": True,
        "ready_for_laboratory_apply": ready,
        "apply_authority": spec["apply_authority"],
        "missing_hardware": missing_hardware,
        "missing_physical_facts": missing_facts,
        "mismatched_physical_facts": mismatched_facts,
        "unresolved_laboratory_gates": unresolved_laboratory_gates,
        "observed_graph": observed,
        "desired": desired,
        "proposed_changes": proposed_changes,
        "does_not_establish": [
            "apply-authorized",
            "profile-safe",
            "latency-or-xrun-acceptance",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile")
    parser.add_argument("--state", type=pathlib.Path, default=PHYSICAL.DEFAULT_STATE)
    args = parser.parse_args()
    print(json.dumps(plan(args.profile, args.state), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
