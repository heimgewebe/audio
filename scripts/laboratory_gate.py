#!/usr/bin/env python3
"""Validate and store private laboratory-gate evidence without audio effects."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import re
import stat
import sys
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "inventory" / "laboratory-gates.v1.json"
PROFILE_PATH = ROOT / "profiles" / "audio-profiles.v1.json"
PHYSICAL_SCRIPT = ROOT / "scripts" / "physical_verification.py"
MAX_EVIDENCE_BYTES = 131_072
MAX_STATE_BYTES = 524_288
DEFAULT_STATE = pathlib.Path(
    os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state")
) / "audio" / "laboratory" / "gates.v1.json"


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PHYSICAL = load_module("physical_verification_for_laboratory", PHYSICAL_SCRIPT)


def load_json(path: pathlib.Path, *, maximum_bytes: int | None = None) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"JSON path must not be a symbolic link: {path}")
    if maximum_bytes is not None and path.stat().st_size > maximum_bytes:
        raise ValueError(f"JSON file exceeds {maximum_bytes} bytes: {path}")
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


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


def load_catalog() -> dict[str, dict[str, Any]]:
    payload = load_json(CATALOG_PATH)
    gates = payload.get("gates")
    if not isinstance(gates, dict):
        raise ValueError("laboratory gate catalog has no gates object")
    return gates


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "audio_laboratory_gate_state",
        "updated_at": None,
        "catalog_sha256": sha256_file(CATALOG_PATH),
        "profile_catalog_sha256": sha256_file(PROFILE_PATH),
        "gates": {},
        "does_not_establish": [
            "profile-apply-authority",
            "physical-safety",
            "automatic-playback",
        ],
    }


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _sha256(value: Any, label: str) -> str:
    result = _bounded_text(value, label, 64, 64)
    if re.fullmatch(r"[0-9a-f]{64}", result) is None:
        raise ValueError(f"{label} must be a lowercase hexadecimal SHA-256")
    return result


def _bounded_text(value: Any, label: str, minimum: int = 1, maximum: int = 1000) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    result = value.strip()
    if len(result) < minimum or len(result) > maximum:
        raise ValueError(f"{label} must contain {minimum} to {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise ValueError(f"{label} contains control characters")
    return result


def validate_common(gate: str, evidence: dict[str, Any], spec: dict[str, Any]) -> None:
    if evidence.get("schema_version") != 1:
        raise ValueError("evidence has the wrong schema version")
    if evidence.get("kind") != spec.get("evidence_kind"):
        raise ValueError(f"evidence kind does not match gate: {gate}")
    if evidence.get("gate") != gate:
        raise ValueError(f"evidence gate binding does not match: {gate}")
    if evidence.get("result") != "pass":
        raise ValueError("only passing evidence can resolve a gate")
    parse_timestamp(evidence.get("measured_at"), "evidence measured_at")


def validate_voice_level(evidence: dict[str, Any]) -> None:
    analysis = evidence.get("analysis")
    if not isinstance(analysis, dict) or analysis.get("kind") != "audio_level_analysis":
        raise ValueError("voice evidence has no audio-level analysis")
    _positive_int(analysis.get("sample_rate_hz"), "sample_rate_hz")
    peak = _number(analysis.get("maximum_peak_dbfs"), "maximum_peak_dbfs")
    if peak < -12.0 or peak > -6.0:
        raise ValueError("voice peak is outside -12 to -6 dBFS")
    channels = analysis.get("channels_analysis")
    if not isinstance(channels, list) or not channels:
        raise ValueError("voice analysis has no channel results")
    for item in channels:
        if not isinstance(item, dict) or item.get("clipped_samples") != 0:
            raise ValueError("voice analysis contains clipped samples")
    source = evidence.get("source_wav")
    if not isinstance(source, dict):
        raise ValueError("voice evidence has no source WAV binding")
    _sha256(source.get("sha256"), "source WAV SHA-256")
    _positive_int(source.get("bytes"), "source WAV bytes")


def validate_loopback_latency(evidence: dict[str, Any]) -> None:
    analysis = evidence.get("analysis")
    if not isinstance(analysis, dict) or analysis.get("kind") != "audio_loopback_latency_result":
        raise ValueError("loopback evidence has no latency analysis")
    latency = _number(analysis.get("round_trip_latency_ms"), "round_trip_latency_ms")
    confidence = _number(
        analysis.get("peak_detection_confidence"), "peak_detection_confidence"
    )
    snr = _number(analysis.get("peak_snr_db"), "peak_snr_db")
    if latency < 0 or latency > 500:
        raise ValueError("round-trip latency is outside the accepted measurement range")
    _positive_int(analysis.get("sample_rate_hz"), "sample_rate_hz")
    _nonnegative_int(analysis.get("delay_samples"), "delay_samples")
    _positive_int(evidence.get("quantum_frames"), "quantum_frames")
    if confidence < 0.8:
        raise ValueError("loopback detection confidence is below 0.8")
    if snr < 20:
        raise ValueError("loopback peak SNR is below 20 dB")
    for key in ("reference_wav", "recorded_wav"):
        source = evidence.get(key)
        if not isinstance(source, dict):
            raise ValueError(f"loopback evidence has no {key} binding")
        _sha256(source.get("sha256"), f"{key} SHA-256")
        _positive_int(source.get("bytes"), f"{key} bytes")


def validate_xrun_observation(evidence: dict[str, Any]) -> None:
    duration = _number(evidence.get("duration_seconds"), "duration_seconds")
    if duration < 60 or duration > 86_400:
        raise ValueError("XRun observation must cover 60 to 86400 seconds")
    if evidence.get("xrun_delta") != 0:
        raise ValueError("XRun observation contains new XRuns")
    _positive_int(evidence.get("rate_hz"), "rate_hz")
    _positive_int(evidence.get("quantum_frames"), "quantum_frames")
    _sha256(evidence.get("graph_fingerprint"), "graph_fingerprint")


def validate_policy_decision(evidence: dict[str, Any]) -> None:
    _bounded_text(evidence.get("decision"), "decision", 2, 120)
    _bounded_text(evidence.get("justification"), "justification", 10, 1000)


def validate_qobuz_rate(evidence: dict[str, Any]) -> None:
    _positive_int(evidence.get("track_rate_hz"), "track_rate_hz")
    _positive_int(evidence.get("graph_rate_hz"), "graph_rate_hz")
    _positive_int(evidence.get("endpoint_rate_hz"), "endpoint_rate_hz")
    resampling = evidence.get("resampling_observed")
    if not isinstance(resampling, bool):
        raise ValueError("resampling_observed must be boolean")
    if resampling:
        raise ValueError("Qobuz rate proof observed resampling")
    rates = {
        evidence["track_rate_hz"],
        evidence["graph_rate_hz"],
        evidence["endpoint_rate_hz"],
    }
    if len(rates) != 1:
        raise ValueError("Qobuz track, graph and endpoint rates do not match")
    _bounded_text(evidence.get("method"), "method", 5, 500)
    _sha256(evidence.get("graph_fingerprint"), "graph_fingerprint")


def validate_managed_plugin_host(evidence: dict[str, Any]) -> None:
    if evidence.get("managed_process") is not True:
        raise ValueError("plugin host is not managed")
    if evidence.get("bounded_logs") is not True:
        raise ValueError("plugin host logs are not bounded")
    if evidence.get("standalone_sfizz_jack") is not False:
        raise ValueError("standalone sfizz/JACK operation remains enabled")
    duration = _number(evidence.get("runtime_seconds"), "runtime_seconds")
    if duration < 60 or duration > 86_400:
        raise ValueError("plugin-host validation must cover 60 to 86400 seconds")


VALIDATORS = {
    "voice_level": validate_voice_level,
    "loopback_latency": validate_loopback_latency,
    "xrun_observation": validate_xrun_observation,
    "policy_decision": validate_policy_decision,
    "qobuz_rate": validate_qobuz_rate,
    "managed_plugin_host": validate_managed_plugin_host,
}


def validate_evidence(gate: str, evidence: dict[str, Any]) -> dict[str, Any]:
    catalog = load_catalog()
    if gate not in catalog:
        raise ValueError(f"unknown laboratory gate: {gate}")
    spec = catalog[gate]
    validate_common(gate, evidence, spec)
    validator_name = spec.get("validator")
    validator = VALIDATORS.get(validator_name)
    if validator is None:
        raise ValueError(f"unknown laboratory validator: {validator_name}")
    validator(evidence)
    return spec


def current_physical_sha256(path: pathlib.Path) -> str:
    if not path.exists():
        raise ValueError("physical state file does not exist")
    PHYSICAL.read_state(path)
    return sha256_file(path)


def validate_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    if state.get("schema_version") != 1 or state.get("kind") != "audio_laboratory_gate_state":
        raise ValueError("laboratory gate state has the wrong schema or kind")
    if state.get("catalog_sha256") != sha256_file(CATALOG_PATH):
        raise ValueError("laboratory gate catalog changed; review existing evidence")
    if state.get("profile_catalog_sha256") != sha256_file(PROFILE_PATH):
        raise ValueError("audio profile catalog changed; review existing evidence")
    gates = state.get("gates")
    if not isinstance(gates, dict):
        raise ValueError("laboratory gate state has no gates object")
    catalog = load_catalog()
    unknown = sorted(set(gates) - set(catalog))
    if unknown:
        raise ValueError(f"laboratory gate state contains unknown gates: {', '.join(unknown)}")
    recorded_times: list[dt.datetime] = []
    for gate, receipt in gates.items():
        if not isinstance(receipt, dict):
            raise ValueError(f"laboratory receipt is not an object: {gate}")
        if receipt.get("status") != "passed":
            raise ValueError(f"laboratory receipt is not passed: {gate}")
        evidence = receipt.get("evidence")
        if not isinstance(evidence, dict):
            raise ValueError(f"laboratory receipt has no evidence: {gate}")
        validate_evidence(gate, evidence)
        if receipt.get("evidence_sha256") != canonical_sha256(evidence):
            raise ValueError(f"laboratory evidence digest mismatch: {gate}")
        recorded_times.append(
            parse_timestamp(receipt.get("recorded_at"), f"recorded_at: {gate}")
        )
        expected_binding = catalog[gate].get("binds_physical_state") is True
        binding = receipt.get("physical_state_sha256")
        if expected_binding:
            _sha256(binding, f"physical-state binding: {gate}")
        elif binding is not None:
            raise ValueError(f"unbound gate unexpectedly stores physical-state binding: {gate}")
    updated_at = state.get("updated_at")
    if updated_at is None:
        if recorded_times:
            raise ValueError("laboratory state has receipts but no updated_at")
    else:
        updated = parse_timestamp(updated_at, "laboratory updated_at")
        if recorded_times and updated < max(recorded_times):
            raise ValueError("laboratory updated_at predates a receipt")
    if path.is_symlink():
        raise ValueError("laboratory gate state must not be a symbolic link")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("laboratory gate state must have mode 0600")


def read_state(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    if path.is_symlink():
        raise ValueError("laboratory gate state must not be a symbolic link")
    if path.stat().st_size > MAX_STATE_BYTES:
        raise ValueError(f"laboratory gate state exceeds {MAX_STATE_BYTES} bytes")
    state = load_json(path, maximum_bytes=MAX_STATE_BYTES)
    validate_state(path, state)
    return state


def atomic_write_private(path: pathlib.Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("laboratory gate state must not be a symbolic link")
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        path.parent.chmod(0o700)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if len(encoded) > MAX_STATE_BYTES:
        raise ValueError(f"laboratory gate state exceeds {MAX_STATE_BYTES} bytes")
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


def record_gate(
    state: dict[str, Any],
    gate: str,
    evidence: dict[str, Any],
    physical_state_path: pathlib.Path,
    *,
    replace: bool = False,
) -> None:
    spec = validate_evidence(gate, evidence)
    if gate in state.get("gates", {}) and not replace:
        raise ValueError(f"laboratory gate already exists; use --replace: {gate}")
    physical_sha: str | None = None
    if spec.get("binds_physical_state") is True:
        physical_sha = current_physical_sha256(physical_state_path)
        if evidence.get("physical_state_sha256") != physical_sha:
            raise ValueError("evidence does not match the current physical state")
    elif evidence.get("physical_state_sha256") is not None:
        raise ValueError("this gate must not contain a physical-state binding")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    state.setdefault("gates", {})[gate] = {
        "status": "passed",
        "recorded_at": now,
        "evidence_sha256": canonical_sha256(evidence),
        "physical_state_sha256": physical_sha,
        "evidence": evidence,
    }
    state["updated_at"] = now


def gate_resolution(
    state: dict[str, Any], physical_state_path: pathlib.Path
) -> tuple[set[str], dict[str, str]]:
    resolved: set[str] = set()
    invalidated: dict[str, str] = {}
    catalog = load_catalog()
    physical_sha: str | None = None
    for gate, receipt in state.get("gates", {}).items():
        if catalog[gate].get("binds_physical_state") is True:
            if physical_sha is None:
                if not physical_state_path.exists():
                    invalidated[gate] = "physical-state-missing"
                    continue
                physical_sha = current_physical_sha256(physical_state_path)
            if receipt.get("physical_state_sha256") != physical_sha:
                invalidated[gate] = "physical-state-changed"
                continue
        resolved.add(gate)
    return resolved, invalidated


def status_payload(
    state: dict[str, Any], state_path: pathlib.Path, physical_state_path: pathlib.Path
) -> dict[str, Any]:
    catalog = load_catalog()
    resolved, invalidated = gate_resolution(state, physical_state_path)
    return {
        "schema_version": 1,
        "kind": "audio_laboratory_gate_status",
        "state_path": str(state_path),
        "resolved": sorted(resolved),
        "invalidated": invalidated,
        "unresolved": sorted(set(catalog) - resolved),
        "recorded_count": len(state.get("gates", {})),
        "resolved_count": len(resolved),
        "total_count": len(catalog),
        "complete": len(resolved) == len(catalog),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--physical-state", type=pathlib.Path, default=PHYSICAL.DEFAULT_STATE
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("init")
    record = sub.add_parser("record")
    record.add_argument("gate", choices=sorted(load_catalog()))
    record.add_argument("evidence", type=pathlib.Path)
    record.add_argument("--replace", action="store_true")
    clear = sub.add_parser("clear")
    clear.add_argument("gate", choices=sorted(load_catalog()))
    args = parser.parse_args()

    if args.command == "init":
        if args.state.exists():
            raise ValueError(f"laboratory gate state already exists: {args.state}")
        state = empty_state()
        atomic_write_private(args.state, state)
    else:
        state = read_state(args.state)
        if args.command == "record":
            evidence = load_json(args.evidence, maximum_bytes=MAX_EVIDENCE_BYTES)
            record_gate(
                state,
                args.gate,
                evidence,
                args.physical_state,
                replace=args.replace,
            )
            atomic_write_private(args.state, state)
        elif args.command == "clear":
            state.get("gates", {}).pop(args.gate, None)
            state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            atomic_write_private(args.state, state)
    print(
        json.dumps(
            status_payload(state, args.state, args.physical_state),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
