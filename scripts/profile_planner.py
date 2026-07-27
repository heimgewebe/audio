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
LABORATORY_SCRIPT = ROOT / "scripts" / "laboratory_gate.py"


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PHYSICAL = load_module("physical_verification", PHYSICAL_SCRIPT)
DOCTOR = load_module("audio_doctor", DOCTOR_SCRIPT)
LABORATORY = load_module("laboratory_gate_for_planner", LABORATORY_SCRIPT)


def doctor_report() -> dict[str, Any]:
    results = [DOCTOR.run_read_only(command) for command in DOCTOR.READ_ONLY_COMMANDS]
    return DOCTOR.build_report(results, DOCTOR.read_eld_text())


def load_profiles() -> dict[str, Any]:
    payload = json.loads(PROFILE_PATH.read_text())
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError("profile catalog has no profiles object")
    return profiles


def laboratory_gate_parameter_mismatch(
    gate: str, receipt: dict[str, Any], desired: dict[str, Any]
) -> dict[str, Any] | None:
    evidence = receipt.get("evidence", {})
    expected_rate = desired.get("rate_hz")
    expected_quantum = desired.get(
        "quantum_candidate_frames", desired.get("quantum_frames")
    )
    if gate == "voice-level-measurement" and expected_rate is not None:
        observed_rate = evidence.get("analysis", {}).get("sample_rate_hz")
        if observed_rate != expected_rate:
            return {
                "reason": "profile-parameter-mismatch",
                "field": "sample_rate_hz",
                "expected": expected_rate,
                "observed": observed_rate,
            }
    if gate == "loopback-latency-measurement":
        observed_rate = evidence.get("analysis", {}).get("sample_rate_hz")
        observed_quantum = evidence.get("quantum_frames")
        if expected_rate is not None and observed_rate != expected_rate:
            return {
                "reason": "profile-parameter-mismatch",
                "field": "sample_rate_hz",
                "expected": expected_rate,
                "observed": observed_rate,
            }
        if expected_quantum is not None and observed_quantum != expected_quantum:
            return {
                "reason": "profile-parameter-mismatch",
                "field": "quantum_frames",
                "expected": expected_quantum,
                "observed": observed_quantum,
            }
    if gate == "xrun-stability-test":
        observed_rate = evidence.get("rate_hz")
        observed_quantum = evidence.get("quantum_frames")
        if expected_rate is not None and observed_rate != expected_rate:
            return {
                "reason": "profile-parameter-mismatch",
                "field": "rate_hz",
                "expected": expected_rate,
                "observed": observed_rate,
            }
        if expected_quantum is not None and observed_quantum != expected_quantum:
            return {
                "reason": "profile-parameter-mismatch",
                "field": "quantum_frames",
                "expected": expected_quantum,
                "observed": observed_quantum,
            }
    return None


def plan(
    profile: str,
    state_path: pathlib.Path,
    gate_state_path: pathlib.Path = LABORATORY.DEFAULT_STATE,
) -> dict[str, Any]:
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
    laboratory_state = LABORATORY.read_state(gate_state_path)
    resolved_gate_names, invalidated_laboratory_gates = (
        LABORATORY.gate_resolution(laboratory_state, state_path)
    )
    required_laboratory_gates = list(spec.get("required_laboratory_gates", []))
    desired = spec.get("desired", {})
    incompatible_laboratory_gates: dict[str, dict[str, Any]] = {}
    for gate in required_laboratory_gates:
        if gate not in resolved_gate_names:
            continue
        mismatch = laboratory_gate_parameter_mismatch(
            gate, laboratory_state["gates"][gate], desired
        )
        if mismatch is not None:
            incompatible_laboratory_gates[gate] = mismatch
    resolved_laboratory_gates = [
        gate
        for gate in required_laboratory_gates
        if gate in resolved_gate_names and gate not in incompatible_laboratory_gates
    ]
    unresolved_laboratory_gates = [
        gate
        for gate in required_laboratory_gates
        if gate not in resolved_laboratory_gates
    ]
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
        "laboratory_gate_state_path": str(gate_state_path),
        "resolved_laboratory_gates": resolved_laboratory_gates,
        "unresolved_laboratory_gates": unresolved_laboratory_gates,
        "invalidated_laboratory_gates": {
            gate: reason
            for gate, reason in invalidated_laboratory_gates.items()
            if gate in required_laboratory_gates
        },
        "incompatible_laboratory_gates": incompatible_laboratory_gates,
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
    parser.add_argument(
        "--gates", type=pathlib.Path, default=LABORATORY.DEFAULT_STATE
    )
    args = parser.parse_args()
    print(
        json.dumps(
            plan(args.profile, args.state, args.gates),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
