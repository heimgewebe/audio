#!/usr/bin/env python3
"""Create profile- and observation-bound audio rate policy evidence."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import re
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAB_PATH = ROOT / "scripts" / "laboratory_gate.py"
SYSTEM_TRUTH_PATH = ROOT / "scripts" / "system_truth.py"
PROFILE_PATH = ROOT / "profiles" / "audio-profiles.v1.json"
SAMPLE_SPEC_RE = re.compile(
    r"^(?P<format>[A-Za-z0-9_-]+) (?P<channels>[0-9]+)ch (?P<rate>[0-9]+)Hz$"
)


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LAB = load_module("laboratory_gate_for_rate_policy", LAB_PATH)
SYSTEM_TRUTH = load_module("system_truth_for_rate_policy", SYSTEM_TRUTH_PATH)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8", errors="strict"))


def _normalize_usb_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.casefold().removeprefix("0x")
    if re.fullmatch(r"[0-9a-f]{4}", normalized) is None:
        return None
    return normalized


def _run_query(argv: tuple[str, ...]) -> tuple[list[Any], dict[str, Any]]:
    SYSTEM_TRUTH.assert_read_only_commands((argv,))
    result = SYSTEM_TRUTH.run_read_only(argv)
    complete = (
        result.argv == argv
        and result.error is None
        and result.returncode == 0
        and not result.stdout_truncated
        and not result.stderr_truncated
    )
    binding = {
        "query_argv": list(argv),
        "query_argv_sha256": LAB.canonical_value_sha256(list(argv)),
        "returncode": result.returncode,
        "complete": complete,
        "stdout_sha256": result.stdout_sha256,
        "stdout_total_bytes": result.stdout_total_bytes,
        "stderr_sha256": result.stderr_sha256,
        "stderr_total_bytes": result.stderr_total_bytes,
    }
    if not complete:
        raise ValueError(f"rate-policy query failed: {argv[-1]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"rate-policy query is not JSON: {argv[-1]}") from exc
    if not isinstance(payload, list):
        raise ValueError(f"rate-policy query root is not a list: {argv[-1]}")
    return payload, binding


def _endpoint_identity(item: Any, direction: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        raise ValueError("audio endpoint entry is not an object")
    properties = item.get("properties")
    if not isinstance(properties, dict):
        return None
    if direction == "source" and (
        item.get("monitor_source") not in {None, ""}
        or properties.get("device.class") == "monitor"
    ):
        return None
    vendor_id = _normalize_usb_id(properties.get("device.vendor.id"))
    product_id = _normalize_usb_id(properties.get("device.product.id"))
    device: str | None = None
    if vendor_id == "07fd" and product_id == "0008":
        device = "motu_m2"
    elif vendor_id == "0582" and product_id == "01b1":
        device = "roland_fp_30x"
    if device is None:
        return None
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{device} endpoint has no node name")
    specification = item.get("sample_specification")
    match = SAMPLE_SPEC_RE.fullmatch(str(specification or ""))
    if match is None:
        raise ValueError(f"{device} endpoint has an invalid sample specification")
    channels = int(match.group("channels"))
    rate_hz = int(match.group("rate"))
    if channels < 1 or channels > 32 or rate_hz < 8_000 or rate_hz > 384_000:
        raise ValueError(f"{device} endpoint rate or channel count is invalid")
    serial = properties.get("device.serial")
    if not isinstance(serial, str) or not serial:
        raise ValueError(f"{device} endpoint has no device serial")
    projection = {
        "device": device,
        "direction": direction,
        "rate_hz": rate_hz,
        "channels": channels,
        "sample_format": match.group("format"),
        "node_name_sha256": sha256_text(name),
        "device_serial_sha256": sha256_text(serial),
    }
    projection["fingerprint"] = LAB.canonical_value_sha256(projection)
    return projection


def endpoint_snapshot() -> dict[str, Any]:
    endpoints: list[dict[str, Any]] = []
    queries: dict[str, Any] = {}
    for direction, argv in (
        ("source", LAB.RATE_POLICY_PACTL_SOURCES_ARGV),
        ("sink", LAB.RATE_POLICY_PACTL_SINKS_ARGV),
    ):
        payload, binding = _run_query(argv)
        queries[direction] = binding
        for item in payload:
            identity = _endpoint_identity(item, direction)
            if identity is not None:
                endpoints.append(identity)
    endpoints.sort(
        key=lambda item: (
            str(item["device"]),
            str(item["direction"]),
            str(item["fingerprint"]),
        )
    )
    counts = {
        device: sum(1 for item in endpoints if item["device"] == device)
        for device in ("motu_m2", "roland_fp_30x")
    }
    rate_sets = {
        device: sorted(
            {int(item["rate_hz"]) for item in endpoints if item["device"] == device}
        )
        for device in ("motu_m2", "roland_fp_30x")
    }
    blockers: list[str] = []
    if counts["motu_m2"] < 2:
        blockers.append("motu-endpoint-set-incomplete")
    if counts["roland_fp_30x"] < 2:
        blockers.append("roland-endpoint-set-incomplete")
    if rate_sets["motu_m2"] != [48_000]:
        blockers.append("motu-rate-is-not-48k")
    if rate_sets["roland_fp_30x"] != [44_100]:
        blockers.append("roland-rate-is-not-44k1")
    snapshot = {
        "schema_version": 1,
        "kind": "audio_rate_endpoint_snapshot",
        "observed_at": utc_now().isoformat(),
        "complete": not blockers,
        "blockers": sorted(blockers),
        "counts": counts,
        "rate_sets_hz": rate_sets,
        "endpoints": endpoints,
        "queries": queries,
    }
    snapshot["snapshot_sha256"] = LAB.canonical_value_sha256(snapshot)
    return snapshot


def profile_binding() -> dict[str, Any]:
    payload = json.loads(PROFILE_PATH.read_text())
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError("audio profile catalog has no profiles object")
    selected: dict[str, Any] = {}
    for name in LAB.RATE_POLICY_PROFILE_NAMES:
        profile = profiles.get(name)
        if not isinstance(profile, dict):
            raise ValueError(f"rate-policy profile is missing: {name}")
        selected[name] = {
            "desired": profile.get("desired"),
            "required_laboratory_gates": profile.get("required_laboratory_gates"),
            "operational_status": profile.get("operational_status", "available"),
        }
    return {
        "profile_catalog_sha256": LAB.sha256_file(PROFILE_PATH),
        "operational_profile_catalog_sha256": LAB.operational_profile_catalog_sha256(),
        "selected_profiles": selected,
        "selected_profiles_sha256": LAB.canonical_value_sha256(selected),
    }


def truth_binding() -> dict[str, Any]:
    report = SYSTEM_TRUTH.build_report()
    SYSTEM_TRUTH.verify_report(report)
    graph = report.get("doctor", {}).get("graph", {})
    runtime = report.get("runtime", {})
    if not isinstance(graph, dict) or not isinstance(runtime, dict):
        raise ValueError("system truth has no graph projection")
    rate_hz = graph.get("force_rate_hz")
    quantum_frames = graph.get("force_quantum_frames")
    if isinstance(rate_hz, bool) or not isinstance(rate_hz, int) or rate_hz <= 0:
        raise ValueError("system truth has no positive graph rate")
    if (
        isinstance(quantum_frames, bool)
        or not isinstance(quantum_frames, int)
        or quantum_frames <= 0
    ):
        raise ValueError("system truth has no positive graph quantum")
    return {
        "report_sha256": report["report_sha256"],
        "truth_chain_sha256": report["truth_chain_sha256"],
        "graph_fingerprint": runtime["graph_fingerprint"],
        "graph_rate_hz": rate_hz,
        "graph_quantum_frames": quantum_frames,
        "default_sink": graph.get("default_sink"),
        "default_source": graph.get("default_source"),
        "hardware": report.get("doctor", {}).get("hardware"),
        "warning_codes": sorted(
            item.get("code")
            for item in report.get("doctor", {}).get("warnings", [])
            if isinstance(item, dict) and isinstance(item.get("code"), str)
        ),
    }


def implementation_binding() -> dict[str, str]:
    return {
        "rate_policy_observer_sha256": LAB.sha256_file(pathlib.Path(__file__)),
        "laboratory_gate_sha256": LAB.sha256_file(LAB_PATH),
        "system_truth_sha256": LAB.sha256_file(SYSTEM_TRUTH_PATH),
        "profile_catalog_sha256": LAB.sha256_file(PROFILE_PATH),
    }


def rate_policy_evidence(gate: str) -> dict[str, Any]:
    if gate not in LAB.RATE_POLICY_DECISIONS:
        raise ValueError(f"unsupported rate-policy gate: {gate}")
    endpoints = endpoint_snapshot()
    profiles = profile_binding()
    truth = truth_binding()
    blockers = list(endpoints["blockers"])
    if truth["graph_rate_hz"] != 48_000:
        blockers.append("current-graph-is-not-48k")
    if truth["default_sink"] != "motu-m2":
        blockers.append("default-sink-is-not-motu")
    hardware = truth.get("hardware")
    if not isinstance(hardware, dict) or not all(
        hardware.get(device) is True
        for device in ("motu_m2", "roland_fp_30x")
    ):
        blockers.append("required-hardware-not-present")
    if profiles["selected_profiles"] != LAB.RATE_POLICY_PROFILE_CONTRACT:
        blockers.append("profile-contract-mismatch")
    blockers = sorted(set(blockers))
    evidence = {
        "schema_version": 1,
        "kind": "audio_policy_decision",
        "gate": gate,
        "result": "pass" if not blockers else "fail",
        "measured_at": utc_now().isoformat(),
        "physical_state_sha256": None,
        "authority": "bound-observed-rate-policy",
        "method": LAB.RATE_POLICY_METHOD,
        "decision": LAB.RATE_POLICY_DECISIONS[gate],
        "justification": LAB.RATE_POLICY_JUSTIFICATIONS[gate],
        "policy": LAB.RATE_POLICY_PAYLOADS[gate],
        "truth": truth,
        "endpoints": endpoints,
        "profiles": profiles,
        "criteria": LAB.RATE_POLICY_CRITERIA,
        "implementation": implementation_binding(),
        "blockers": blockers,
        "does_not_establish": [
            "bit-perfect Qobuz playback without qobuz-rate-proof",
            "subjective resampler transparency",
            "latency or XRun safety at another graph quantum",
            "profile apply authority",
        ],
    }
    return evidence


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("gate", choices=sorted(LAB.RATE_POLICY_DECISIONS))
    args = parser.parse_args(argv)
    print(json.dumps(rate_policy_evidence(args.gate), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
