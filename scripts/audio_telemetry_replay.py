#!/usr/bin/env python3
"""Strict, deterministic telemetry replay contract for Audiozentrale v2."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import pathlib
import re
import stat
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "inventory" / "audiozentrale-telemetry-replay.v1.json"
SCHEMA_PATH = ROOT / "schemas" / "audiozentrale-telemetry-replay.v1.schema.json"
PRODUCT_MODEL_PATH = ROOT / "inventory" / "audiozentrale-product-model.v1.json"
MAX_JSON_BYTES = 2_000_000
ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SCENARIO_IDS = (
    "normal",
    "clip",
    "xrun",
    "device-loss",
    "stale-telemetry",
    "recovery",
)
FRAME_KEYS = {
    "index",
    "offset_ms",
    "peak_dbfs",
    "rms_dbfs",
    "midi_note",
    "midi_velocity",
    "xrun_total",
    "device_state",
    "telemetry_age_ms",
    "event",
}
EVENTS = {"none", "midi", "clip", "xrun", "device-loss", "stale", "recovery"}


class ReplayError(RuntimeError):
    """Controlled telemetry replay contract failure."""


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ReplayError(f"non-finite JSON number: {value}")


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ReplayError("value is not canonical JSON") from error


def _absolute_without_resolution(path: pathlib.Path, label: str) -> pathlib.Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = pathlib.Path.cwd() / expanded
    if any(part in {"", ".", ".."} for part in expanded.parts[1:]):
        raise ReplayError(f"{label} contains an unsafe path component")
    return expanded


def _open_regular_no_follow(path: pathlib.Path, label: str) -> int:
    absolute = _absolute_without_resolution(path, label)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    directory = os.open("/", directory_flags)
    try:
        for component in absolute.parts[1:-1]:
            child = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(absolute.name, file_flags, dir_fd=directory)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ReplayError(f"{label} path contains a symlink") from error
        raise ReplayError(f"{label} cannot be opened safely") from error
    finally:
        os.close(directory)
    return descriptor


def read_json_snapshot(
    path: pathlib.Path,
    label: str,
    *,
    maximum_bytes: int = MAX_JSON_BYTES,
) -> tuple[dict[str, Any], bytes]:
    if isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int):
        raise ReplayError(f"{label} maximum size is invalid")
    if maximum_bytes <= 0 or maximum_bytes > MAX_JSON_BYTES:
        raise ReplayError(f"{label} maximum size is outside the contract")
    descriptor = _open_regular_no_follow(path, label)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReplayError(f"{label} is not a regular file")
        if before.st_size > maximum_bytes:
            raise ReplayError(f"{label} violates the file type or size contract")
        data = bytearray()
        while len(data) <= maximum_bytes:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum_bytes:
            raise ReplayError(f"{label} exceeds the byte limit")
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            any(
                getattr(after, field) != getattr(before, field)
                for field in stable_fields
            )
            or len(data) != before.st_size
        ):
            raise ReplayError(f"{label} changed while it was read")
        payload = bytes(data)
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_nonfinite,
            )
        except UnicodeDecodeError as error:
            raise ReplayError(f"{label} is not UTF-8") from error
        except json.JSONDecodeError as error:
            raise ReplayError(f"{label} is not valid JSON") from error
        if not isinstance(value, dict):
            raise ReplayError(f"{label} root must be an object")
        return value, payload
    finally:
        os.close(descriptor)


def require_exact_keys(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReplayError(f"{label} must be an object")
    observed = set(value)
    if observed != keys:
        raise ReplayError(f"{label} keys differ")
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayError(f"{label} must be a non-empty string")
    return value


def require_id(value: Any, label: str) -> str:
    value = require_string(value, label)
    if not ID_RE.fullmatch(value):
        raise ReplayError(f"{label} is not a stable id")
    return value


def require_integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ReplayError(f"{label} is outside the allowed range")
    return value


def require_number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReplayError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ReplayError(f"{label} is outside the allowed range")
    return number


def validate_frame(
    frame: Any,
    *,
    scenario_id: str,
    expected_index: int,
    interval_ms: int,
    stale_after_ms: int,
    previous_xruns: int | None,
) -> tuple[dict[str, Any], int]:
    frame = require_exact_keys(
        frame, FRAME_KEYS, f"{scenario_id} frame[{expected_index}]"
    )
    index = require_integer(frame["index"], "frame index", 0, 255)
    if index != expected_index:
        raise ReplayError(f"{scenario_id} frame indices are not contiguous")
    offset = require_integer(frame["offset_ms"], "frame offset", 0, 600_000)
    if offset != index * interval_ms:
        raise ReplayError(f"{scenario_id} frame offset differs from the interval")
    peak = require_number(frame["peak_dbfs"], "frame peak", -120.0, 0.0)
    rms = require_number(frame["rms_dbfs"], "frame RMS", -120.0, 0.0)
    if rms > peak:
        raise ReplayError(f"{scenario_id} frame RMS exceeds its peak")
    note = frame["midi_note"]
    if note is not None:
        require_integer(note, "MIDI note", 0, 127)
    velocity = require_integer(frame["midi_velocity"], "MIDI velocity", 0, 127)
    if note is None and velocity != 0:
        raise ReplayError(f"{scenario_id} frame has velocity without a MIDI note")
    xruns = require_integer(frame["xrun_total"], "XRun total", 0, 1_000_000)
    if previous_xruns is not None and xruns < previous_xruns:
        raise ReplayError(f"{scenario_id} XRun total is not monotonic")
    device = require_string(frame["device_state"], "device state")
    if device not in {"online", "lost", "recovering"}:
        raise ReplayError(f"{scenario_id} frame has an unknown device state")
    age = require_integer(frame["telemetry_age_ms"], "telemetry age", 0, 600_000)
    event = require_string(frame["event"], "frame event")
    if event not in EVENTS:
        raise ReplayError(f"{scenario_id} frame has an unknown event")
    if event == "clip" and peak < -1.0:
        raise ReplayError("clip event does not reach the clipping boundary")
    if event == "xrun" and (previous_xruns is None or xruns <= previous_xruns):
        raise ReplayError("XRun event does not increment the counter")
    if event == "device-loss" and device != "lost":
        raise ReplayError("device-loss event does not mark the device lost")
    if event == "stale" and age <= stale_after_ms:
        raise ReplayError("stale event does not exceed the stale threshold")
    if event == "recovery" and device not in {"recovering", "online"}:
        raise ReplayError("recovery event has an invalid device state")
    return frame, xruns


def validate_catalog(catalog: Any) -> dict[str, Any]:
    catalog = require_exact_keys(
        catalog,
        {
            "schema_version",
            "kind",
            "catalog_id",
            "product_model_sha256",
            "schema_binding",
            "sample_interval_ms",
            "stale_after_ms",
            "scenarios",
        },
        "replay catalog",
    )
    if catalog["schema_version"] != 1:
        raise ReplayError("replay schema version is unsupported")
    if catalog["kind"] != "audiozentrale_telemetry_replay_catalog":
        raise ReplayError("replay kind is unsupported")
    if catalog["catalog_id"] != "audiozentrale-phase0-replay-v1":
        raise ReplayError("replay catalog id differs")
    product_sha = require_string(
        catalog["product_model_sha256"], "product model digest"
    )
    if not SHA256_RE.fullmatch(product_sha):
        raise ReplayError("product model digest is invalid")
    product_model, _ = read_json_snapshot(PRODUCT_MODEL_PATH, "product model")
    observed_product_sha = hashlib.sha256(canonical_json(product_model)).hexdigest()
    if product_sha != observed_product_sha:
        raise ReplayError("replay product model binding differs")
    binding = require_exact_keys(
        catalog["schema_binding"], {"path", "sha256"}, "replay schema binding"
    )
    if binding["path"] != SCHEMA_PATH.relative_to(ROOT).as_posix():
        raise ReplayError("replay schema path differs")
    schema_sha = require_string(binding["sha256"], "replay schema digest")
    if not SHA256_RE.fullmatch(schema_sha):
        raise ReplayError("replay schema digest is invalid")
    _, schema_bytes = read_json_snapshot(SCHEMA_PATH, "replay schema")
    if hashlib.sha256(schema_bytes).hexdigest() != schema_sha:
        raise ReplayError("replay schema digest differs")
    interval_ms = require_integer(
        catalog["sample_interval_ms"], "sample interval", 100, 2000
    )
    stale_after_ms = require_integer(
        catalog["stale_after_ms"], "stale threshold", 500, 10_000
    )
    scenarios = catalog["scenarios"]
    if not isinstance(scenarios, list) or len(scenarios) != len(SCENARIO_IDS):
        raise ReplayError("replay catalog must contain exactly six scenarios")
    scenario_ids: list[str] = []
    covered_events: set[str] = set()
    for scenario_index, raw_scenario in enumerate(scenarios):
        scenario = require_exact_keys(
            raw_scenario,
            {"id", "label", "description", "frames"},
            f"scenario[{scenario_index}]",
        )
        scenario_id = require_id(scenario["id"], "scenario id")
        scenario_ids.append(scenario_id)
        require_string(scenario["label"], "scenario label")
        require_string(scenario["description"], "scenario description")
        frames = scenario["frames"]
        if not isinstance(frames, list) or not 4 <= len(frames) <= 64:
            raise ReplayError(f"{scenario_id} frame count is outside the contract")
        previous_xruns: int | None = None
        device_states: list[str] = []
        for frame_index, raw_frame in enumerate(frames):
            frame, previous_xruns = validate_frame(
                raw_frame,
                scenario_id=scenario_id,
                expected_index=frame_index,
                interval_ms=interval_ms,
                stale_after_ms=stale_after_ms,
                previous_xruns=previous_xruns,
            )
            covered_events.add(frame["event"])
            device_states.append(frame["device_state"])
        if scenario_id == "recovery" and not (
            device_states[0] == "lost"
            and "recovering" in device_states
            and device_states[-1] == "online"
        ):
            raise ReplayError(
                "recovery scenario does not prove lost-to-online recovery"
            )
    if tuple(scenario_ids) != SCENARIO_IDS:
        raise ReplayError("replay scenarios differ from the canonical order")
    required_events = {"midi", "clip", "xrun", "device-loss", "stale", "recovery"}
    if not required_events <= covered_events:
        raise ReplayError("replay event coverage is incomplete")
    return catalog


def load_replay_contract() -> dict[str, Any]:
    catalog, catalog_bytes = read_json_snapshot(CATALOG_PATH, "replay catalog")
    catalog = validate_catalog(catalog)
    return {
        "schema_version": 1,
        "kind": "audio_control_telemetry_replay",
        "authority": "synthetic-replay",
        "authoritative": False,
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "schema_sha256": catalog["schema_binding"]["sha256"],
        "product_model_sha256": catalog["product_model_sha256"],
        "catalog": catalog,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audio-telemetry-replay")
    parser.add_argument(
        "command", choices=("check", "show"), nargs="?", default="check"
    )
    args = parser.parse_args(argv)
    try:
        contract = load_replay_contract()
    except ReplayError as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_control_telemetry_replay_error",
                    "error": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    if args.command == "show":
        print(json.dumps(contract, ensure_ascii=False, sort_keys=True))
    else:
        catalog = contract["catalog"]
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_control_telemetry_replay_check",
                    "status": "ok",
                    "catalog_id": catalog["catalog_id"],
                    "catalog_sha256": contract["catalog_sha256"],
                    "scenario_ids": [item["id"] for item in catalog["scenarios"]],
                    "frame_count": sum(
                        len(item["frames"]) for item in catalog["scenarios"]
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
