#!/usr/bin/env python3
"""Observe and store bounded, read-only USB audio loss/recovery exercises."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYSTEM_TRUTH_PATH = ROOT / "scripts" / "system_truth.py"
SYS_DEVICES_ROOT = pathlib.Path("/sys/devices")
SOUND_CLASS_DIR = pathlib.Path("/sys/class/sound")
MAX_CLASS_ENTRIES = 64
MAX_SYSFS_BYTES = 4096
MAX_STATE_BYTES = 524_288
MAX_EVIDENCE_BYTES = 262_144
POLL_SECONDS = 1.0
CONTROL_RE = re.compile(r"^controlC[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_STATE = pathlib.Path(
    os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state")
) / "audio" / "device-loss" / "exercises.v1.json"

DEVICE_SPECS: dict[str, dict[str, str]] = {
    "motu_m2": {
        "vendor_id": "07fd",
        "model_id": "0008",
        "label": "MOTU M2",
        "followup_id": "motu-device-loss-exercise",
    },
    "roland_fp_30x": {
        "vendor_id": "0582",
        "model_id": "01b1",
        "label": "Roland FP-30X",
        "followup_id": "roland-device-loss-exercise",
    },
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def monotonic_now() -> float:
    return time.monotonic()


def sleep_for(seconds: float) -> None:
    time.sleep(seconds)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: pathlib.Path) -> str:
    return sha256_bytes(path.read_bytes())


def absolute_without_resolution(path: pathlib.Path) -> pathlib.Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = pathlib.Path.cwd() / expanded
    if any(part in {"", ".", ".."} for part in expanded.parts[1:]):
        raise ValueError(f"unsafe path component: {path}")
    return expanded


def open_directory_chain(
    path: pathlib.Path,
    *,
    create: bool = False,
) -> tuple[pathlib.Path, int]:
    absolute = absolute_without_resolution(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return absolute, descriptor
    except Exception:
        os.close(descriptor)
        raise


def parse_timestamp(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must contain a timezone")
    return parsed


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not result >= 0 or result == float("inf"):
        raise ValueError(f"{label} must be finite and nonnegative")
    return result


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _text(
    value: Any,
    label: str,
    minimum: int = 1,
    maximum: int = 1024,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    result = value.strip()
    if len(result) < minimum or len(result) > maximum:
        raise ValueError(
            f"{label} must contain {minimum} to {maximum} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise ValueError(f"{label} contains control characters")
    return result


def _read_small(path: pathlib.Path, *, optional: bool = False) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("sysfs attribute is not a regular file")
        payload = os.read(descriptor, MAX_SYSFS_BYTES + 1)
        if len(payload) > MAX_SYSFS_BYTES:
            raise ValueError("sysfs attribute exceeds the byte limit")
        return payload.decode("utf-8", errors="strict").strip()
    except FileNotFoundError:
        if optional:
            return None
        raise
    except (OSError, UnicodeError) as exc:
        if optional and isinstance(exc, FileNotFoundError):
            return None
        raise ValueError("sysfs attribute cannot be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _usb_parent(control_path: pathlib.Path) -> pathlib.Path | None:
    for candidate in (control_path, *control_path.parents):
        if candidate == SYS_DEVICES_ROOT.parent:
            break
        vendor = _read_small(candidate / "idVendor", optional=True)
        product = _read_small(candidate / "idProduct", optional=True)
        if vendor and product:
            return candidate
    return None


def _identity_for(device: str, usb_parent: pathlib.Path) -> dict[str, Any] | None:
    spec = DEVICE_SPECS[device]
    vendor_id = (_read_small(usb_parent / "idVendor") or "").casefold()
    model_id = (_read_small(usb_parent / "idProduct") or "").casefold()
    if vendor_id != spec["vendor_id"] or model_id != spec["model_id"]:
        return None
    serial = _read_small(usb_parent / "serial", optional=True)
    port_path = _read_small(usb_parent / "devpath", optional=True)
    bus_number = _read_small(usb_parent / "busnum", optional=True)
    manufacturer = _read_small(usb_parent / "manufacturer", optional=True)
    product = _read_small(usb_parent / "product", optional=True)
    if serial:
        strength = "serial"
        key = {
            "vendor_id": vendor_id,
            "model_id": model_id,
            "serial_sha256": sha256_bytes(serial.encode("utf-8")),
        }
    else:
        if not port_path or not bus_number:
            raise ValueError("device has neither serial nor complete USB port identity")
        strength = "model-port"
        key = {
            "vendor_id": vendor_id,
            "model_id": model_id,
            "bus_number": bus_number,
            "port_path": port_path,
        }
    identity = {
        "device": device,
        "vendor_id": vendor_id,
        "model_id": model_id,
        "identity_strength": strength,
        "serial_sha256": (
            sha256_bytes(serial.encode("utf-8")) if serial else None
        ),
        "bus_number": bus_number,
        "port_path": port_path,
        "manufacturer_sha256": (
            sha256_bytes(manufacturer.encode("utf-8")) if manufacturer else None
        ),
        "product_sha256": (
            sha256_bytes(product.encode("utf-8")) if product else None
        ),
    }
    identity["fingerprint"] = sha256_json(key)
    return identity


def _control_entries() -> list[os.DirEntry[str]]:
    if SOUND_CLASS_DIR.is_symlink() or not SOUND_CLASS_DIR.is_dir():
        raise ValueError("sound class directory is unavailable or unsafe")
    entries: list[os.DirEntry[str]] = []
    with os.scandir(SOUND_CLASS_DIR) as iterator:
        for index, entry in enumerate(iterator):
            if index >= MAX_CLASS_ENTRIES:
                raise ValueError("sound class directory exceeds the entry limit")
            if CONTROL_RE.fullmatch(entry.name):
                entries.append(entry)
    return sorted(entries, key=lambda entry: entry.name)


def scan_device(device: str) -> dict[str, Any]:
    if device not in DEVICE_SPECS:
        raise ValueError(f"unsupported device: {device}")
    observed_at = utc_now().isoformat()
    matches: list[dict[str, Any]] = []
    errors: list[str] = []
    entries: list[os.DirEntry[str]] = []
    try:
        entries = _control_entries()
    except (OSError, ValueError):
        errors.append("sound-class-scan-failed")
    for entry in entries:
        try:
            control = pathlib.Path(entry.path).resolve(strict=True)
            devices_root = SYS_DEVICES_ROOT.resolve(strict=True)
            if not _inside(control, devices_root):
                raise ValueError("sound control escapes sysfs devices root")
            usb_parent = _usb_parent(control)
            if usb_parent is None:
                continue
            identity = _identity_for(device, usb_parent)
            if identity is None:
                continue
            matches.append(
                {
                    "identity": identity,
                    "control_name_sha256": sha256_bytes(
                        entry.name.encode("utf-8")
                    ),
                    "sysfs_path_sha256": sha256_bytes(
                        str(control).encode("utf-8")
                    ),
                }
            )
        except (OSError, ValueError):
            errors.append(f"control-read-failed:{entry.name}")
    ambiguous = len(matches) > 1
    complete = not errors and not ambiguous
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "kind": "audio_device_presence_snapshot",
        "device": device,
        "observed_at": observed_at,
        "complete": complete,
        "present": complete and len(matches) == 1,
        "ambiguous": ambiguous,
        "match_count": len(matches),
        "control_count": len(entries),
        "control_listing_sha256": sha256_json(
            [entry.name for entry in entries]
        ),
        "errors": sorted(set(errors)),
        "matches": matches,
    }
    snapshot["observation_sha256"] = sha256_json(snapshot)
    return snapshot


def _snapshot_without_digest(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in snapshot.items()
        if key != "observation_sha256"
    }


def _validate_identity(identity: Any, device: str, label: str) -> str:
    if not isinstance(identity, dict):
        raise ValueError(f"{label} identity is missing")
    if identity.get("device") != device:
        raise ValueError(f"{label} identity has another device")
    spec = DEVICE_SPECS[device]
    if identity.get("vendor_id") != spec["vendor_id"]:
        raise ValueError(f"{label} identity has another USB vendor")
    if identity.get("model_id") != spec["model_id"]:
        raise ValueError(f"{label} identity has another USB model")
    strength = identity.get("identity_strength")
    if strength not in {"serial", "model-port"}:
        raise ValueError(f"{label} identity strength is invalid")
    serial_sha = identity.get("serial_sha256")
    bus_number = identity.get("bus_number")
    port_path = identity.get("port_path")
    if strength == "serial":
        serial_sha = _sha256(serial_sha, f"{label} serial SHA-256")
        key = {
            "vendor_id": spec["vendor_id"],
            "model_id": spec["model_id"],
            "serial_sha256": serial_sha,
        }
    else:
        _text(bus_number, f"{label} USB bus", 1, 32)
        _text(port_path, f"{label} USB port", 1, 128)
        if serial_sha is not None:
            raise ValueError(f"{label} model-port identity stores a serial")
        key = {
            "vendor_id": spec["vendor_id"],
            "model_id": spec["model_id"],
            "bus_number": bus_number,
            "port_path": port_path,
        }
    for name in ("manufacturer_sha256", "product_sha256"):
        value = identity.get(name)
        if value is not None:
            _sha256(value, f"{label} {name}")
    fingerprint = _sha256(identity.get("fingerprint"), f"{label} fingerprint")
    if fingerprint != sha256_json(key):
        raise ValueError(f"{label} identity fingerprint mismatch")
    return fingerprint


def validate_snapshot(
    snapshot: Any,
    device: str,
    label: str,
    *,
    expected_present: bool,
) -> str | None:
    if not isinstance(snapshot, dict):
        raise ValueError(f"{label} snapshot is missing")
    if (
        snapshot.get("schema_version") != 1
        or snapshot.get("kind") != "audio_device_presence_snapshot"
        or snapshot.get("device") != device
    ):
        raise ValueError(f"{label} snapshot schema is invalid")
    parse_timestamp(snapshot.get("observed_at"), f"{label} observed_at")
    if snapshot.get("complete") is not True:
        raise ValueError(f"{label} snapshot is incomplete")
    if snapshot.get("ambiguous") is not False:
        raise ValueError(f"{label} snapshot is ambiguous")
    if snapshot.get("present") is not expected_present:
        raise ValueError(f"{label} snapshot presence is incorrect")
    if snapshot.get("errors") != []:
        raise ValueError(f"{label} snapshot contains errors")
    _nonnegative_int(snapshot.get("control_count"), f"{label} control count")
    _sha256(
        snapshot.get("control_listing_sha256"),
        f"{label} control-list digest",
    )
    matches = snapshot.get("matches")
    if not isinstance(matches, list):
        raise ValueError(f"{label} snapshot matches are invalid")
    expected_count = 1 if expected_present else 0
    if snapshot.get("match_count") != expected_count or len(matches) != expected_count:
        raise ValueError(f"{label} snapshot match count is inconsistent")
    observed_digest = _sha256(
        snapshot.get("observation_sha256"),
        f"{label} observation SHA-256",
    )
    if observed_digest != sha256_json(_snapshot_without_digest(snapshot)):
        raise ValueError(f"{label} observation digest mismatch")
    if not expected_present:
        return None
    match = matches[0]
    if not isinstance(match, dict):
        raise ValueError(f"{label} device match is invalid")
    _sha256(match.get("control_name_sha256"), f"{label} control-name SHA-256")
    _sha256(match.get("sysfs_path_sha256"), f"{label} sysfs-path SHA-256")
    return _validate_identity(match.get("identity"), device, label)


def _snapshot_identity(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    matches = snapshot.get("matches")
    if not isinstance(matches, list) or len(matches) != 1:
        return None
    match = matches[0]
    if not isinstance(match, dict) or not isinstance(match.get("identity"), dict):
        return None
    return match["identity"]


def _confirmed_absence(
    device: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    deadline = monotonic_now() + timeout_seconds
    first: dict[str, Any] | None = None
    while True:
        snapshot = scan_device(device)
        absent = (
            snapshot.get("complete") is True
            and snapshot.get("present") is False
            and snapshot.get("match_count") == 0
        )
        if absent:
            if first is not None:
                return first, snapshot
            first = snapshot
        else:
            first = None
        remaining = deadline - monotonic_now()
        if remaining <= 0:
            return None, None
        sleep_for(min(POLL_SECONDS, remaining))


def _confirmed_recovery(
    device: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    deadline = monotonic_now() + timeout_seconds
    first: dict[str, Any] | None = None
    first_fingerprint: str | None = None
    while True:
        snapshot = scan_device(device)
        identity = _snapshot_identity(snapshot)
        fingerprint = identity.get("fingerprint") if identity else None
        present = snapshot.get("complete") is True and fingerprint is not None
        if present:
            if first is not None and fingerprint == first_fingerprint:
                return first, snapshot
            first = snapshot
            first_fingerprint = str(fingerprint)
        else:
            first = None
            first_fingerprint = None
        remaining = deadline - monotonic_now()
        if remaining <= 0:
            return None, None
        sleep_for(min(POLL_SECONDS, remaining))


def _fail_evidence(
    device: str,
    started_at: dt.datetime,
    started_monotonic: float,
    loss_timeout_seconds: int,
    recovery_timeout_seconds: int,
    baseline: dict[str, Any],
    blocker: str,
    *,
    loss: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ended_at = utc_now()
    return {
        "schema_version": 1,
        "kind": "audio_device_loss_exercise",
        "device": device,
        "result": "fail",
        "measured_at": ended_at.isoformat(),
        "exercise_started_at": started_at.isoformat(),
        "exercise_ended_at": ended_at.isoformat(),
        "duration_seconds": round(
            max(0.0, monotonic_now() - started_monotonic),
            3,
        ),
        "loss_timeout_seconds": loss_timeout_seconds,
        "recovery_timeout_seconds": recovery_timeout_seconds,
        "baseline": baseline,
        "loss": loss,
        "recovery": None,
        "identity_strength": None,
        "baseline_identity_fingerprint": None,
        "recovery_identity_fingerprint": None,
        "identity_changed": None,
        "blockers": [blocker],
        "does_not_establish": [
            "device recovery without a completed physical unplug/replug exercise",
            "USB identity beyond the fields exposed by the device",
            "safe audio routing or listening level",
        ],
    }


def observe_exercise(
    device: str,
    loss_timeout_seconds: int = 60,
    recovery_timeout_seconds: int = 60,
) -> dict[str, Any]:
    if device not in DEVICE_SPECS:
        raise ValueError(f"unsupported device: {device}")
    loss_timeout_seconds = _nonnegative_int(
        loss_timeout_seconds,
        "loss_timeout_seconds",
    )
    recovery_timeout_seconds = _nonnegative_int(
        recovery_timeout_seconds,
        "recovery_timeout_seconds",
    )
    if loss_timeout_seconds > 3600 or recovery_timeout_seconds > 3600:
        raise ValueError("device exercise timeouts must not exceed 3600 seconds")
    started_at = utc_now()
    started_monotonic = monotonic_now()
    baseline = scan_device(device)
    if (
        baseline.get("complete") is not True
        or baseline.get("present") is not True
        or baseline.get("match_count") != 1
    ):
        return _fail_evidence(
            device,
            started_at,
            started_monotonic,
            loss_timeout_seconds,
            recovery_timeout_seconds,
            baseline,
            "baseline-device-not-uniquely-present",
        )
    baseline_identity = _snapshot_identity(baseline)
    if baseline_identity is None:
        return _fail_evidence(
            device,
            started_at,
            started_monotonic,
            loss_timeout_seconds,
            recovery_timeout_seconds,
            baseline,
            "baseline-device-identity-missing",
        )
    loss_first, loss_confirmed = _confirmed_absence(
        device,
        loss_timeout_seconds,
    )
    if loss_first is None or loss_confirmed is None:
        return _fail_evidence(
            device,
            started_at,
            started_monotonic,
            loss_timeout_seconds,
            recovery_timeout_seconds,
            baseline,
            "device-loss-not-observed",
        )
    loss = {"first": loss_first, "confirmed": loss_confirmed}
    recovery_first, recovery_confirmed = _confirmed_recovery(
        device,
        recovery_timeout_seconds,
    )
    if recovery_first is None or recovery_confirmed is None:
        return _fail_evidence(
            device,
            started_at,
            started_monotonic,
            loss_timeout_seconds,
            recovery_timeout_seconds,
            baseline,
            "device-recovery-not-observed",
            loss=loss,
        )
    recovery_identity = _snapshot_identity(recovery_confirmed)
    if recovery_identity is None:
        return _fail_evidence(
            device,
            started_at,
            started_monotonic,
            loss_timeout_seconds,
            recovery_timeout_seconds,
            baseline,
            "recovery-device-identity-missing",
            loss=loss,
        )
    baseline_fingerprint = str(baseline_identity["fingerprint"])
    recovery_fingerprint = str(recovery_identity["fingerprint"])
    identity_changed = baseline_fingerprint != recovery_fingerprint
    blockers = ["device-identity-changed"] if identity_changed else []
    ended_at = utc_now()
    payload = {
        "schema_version": 1,
        "kind": "audio_device_loss_exercise",
        "device": device,
        "result": "pass" if not blockers else "fail",
        "measured_at": ended_at.isoformat(),
        "exercise_started_at": started_at.isoformat(),
        "exercise_ended_at": ended_at.isoformat(),
        "duration_seconds": round(
            max(0.0, monotonic_now() - started_monotonic),
            3,
        ),
        "loss_timeout_seconds": loss_timeout_seconds,
        "recovery_timeout_seconds": recovery_timeout_seconds,
        "baseline": baseline,
        "loss": loss,
        "recovery": {
            "first": recovery_first,
            "confirmed": recovery_confirmed,
        },
        "identity_strength": baseline_identity["identity_strength"],
        "baseline_identity_fingerprint": baseline_fingerprint,
        "recovery_identity_fingerprint": recovery_fingerprint,
        "identity_changed": identity_changed,
        "blockers": blockers,
        "implementation": {
            "device_loss_exercise_sha256": sha256_file(pathlib.Path(__file__)),
            "system_truth_sha256": sha256_file(SYSTEM_TRUTH_PATH),
        },
        "criteria": {
            "requires_unique_present_baseline": True,
            "requires_two_complete_absence_observations": True,
            "requires_two_matching_recovery_observations": True,
            "requires_unchanged_observable_identity": True,
            "maximum_timeout_seconds_per_phase": 3600,
        },
        "does_not_establish": [
            "safe audio routing or listening level",
            "recovery stability outside the bounded exercise",
            (
                "detection of an identical Roland replacement on the same USB port"
                if baseline_identity["identity_strength"] == "model-port"
                else "device behavior beyond the observed USB identity"
            ),
        ],
    }
    return payload


def validate_evidence(
    evidence: dict[str, Any],
    *,
    allow_stale_implementation: bool = False,
) -> None:
    if (
        evidence.get("schema_version") != 1
        or evidence.get("kind") != "audio_device_loss_exercise"
    ):
        raise ValueError("device exercise evidence schema is invalid")
    device = evidence.get("device")
    if device not in DEVICE_SPECS:
        raise ValueError("device exercise targets an unsupported device")
    if evidence.get("result") != "pass":
        raise ValueError("only passing device exercise evidence can be stored")
    if evidence.get("blockers") != []:
        raise ValueError("passing device exercise evidence contains blockers")
    started = parse_timestamp(
        evidence.get("exercise_started_at"),
        "exercise_started_at",
    )
    ended = parse_timestamp(evidence.get("exercise_ended_at"), "exercise_ended_at")
    measured = parse_timestamp(evidence.get("measured_at"), "measured_at")
    if not started < ended or abs((measured - ended).total_seconds()) > 2.0:
        raise ValueError("device exercise timestamps are inconsistent")
    duration = _number(evidence.get("duration_seconds"), "duration_seconds")
    if abs((ended - started).total_seconds() - duration) > 2.0:
        raise ValueError("device exercise duration contradicts timestamps")
    for field in ("loss_timeout_seconds", "recovery_timeout_seconds"):
        timeout = _nonnegative_int(evidence.get(field), field)
        if timeout > 3600:
            raise ValueError("device exercise timeout exceeds 3600 seconds")
    baseline_fingerprint = validate_snapshot(
        evidence.get("baseline"),
        device,
        "baseline",
        expected_present=True,
    )
    loss = evidence.get("loss")
    if not isinstance(loss, dict):
        raise ValueError("device loss phase is missing")
    validate_snapshot(
        loss.get("first"),
        device,
        "loss first",
        expected_present=False,
    )
    validate_snapshot(
        loss.get("confirmed"),
        device,
        "loss confirmed",
        expected_present=False,
    )
    recovery = evidence.get("recovery")
    if not isinstance(recovery, dict):
        raise ValueError("device recovery phase is missing")
    recovery_first = validate_snapshot(
        recovery.get("first"),
        device,
        "recovery first",
        expected_present=True,
    )
    recovery_confirmed = validate_snapshot(
        recovery.get("confirmed"),
        device,
        "recovery confirmed",
        expected_present=True,
    )
    if recovery_first != recovery_confirmed:
        raise ValueError("device recovery identity is not stable")
    if baseline_fingerprint != recovery_confirmed:
        raise ValueError("device identity changed during the exercise")
    if evidence.get("identity_changed") is not False:
        raise ValueError("device exercise reports an identity change")
    if evidence.get("baseline_identity_fingerprint") != baseline_fingerprint:
        raise ValueError("baseline identity fingerprint mismatch")
    if evidence.get("recovery_identity_fingerprint") != recovery_confirmed:
        raise ValueError("recovery identity fingerprint mismatch")
    baseline_identity = _snapshot_identity(evidence["baseline"])
    if not isinstance(baseline_identity, dict):
        raise ValueError("baseline identity projection is missing")
    if evidence.get("identity_strength") != baseline_identity.get(
        "identity_strength"
    ):
        raise ValueError("device identity strength mismatch")
    phase_times = [
        parse_timestamp(evidence["baseline"]["observed_at"], "baseline time"),
        parse_timestamp(loss["first"]["observed_at"], "loss first time"),
        parse_timestamp(loss["confirmed"]["observed_at"], "loss confirm time"),
        parse_timestamp(recovery["first"]["observed_at"], "recovery first time"),
        parse_timestamp(
            recovery["confirmed"]["observed_at"],
            "recovery confirm time",
        ),
    ]
    if phase_times != sorted(phase_times) or len(set(phase_times)) != len(
        phase_times
    ):
        raise ValueError("device exercise phase timestamps are not strictly ordered")
    if phase_times[0] < started or phase_times[-1] > ended:
        raise ValueError("device exercise phase lies outside the exercise window")
    implementation = evidence.get("implementation")
    if not isinstance(implementation, dict):
        raise ValueError("device exercise implementation binding is missing")
    observed_implementation = {
        "device_loss_exercise_sha256": _sha256(
            implementation.get("device_loss_exercise_sha256"),
            "device exercise implementation SHA-256",
        ),
        "system_truth_sha256": _sha256(
            implementation.get("system_truth_sha256"),
            "system truth implementation SHA-256",
        ),
    }
    if not allow_stale_implementation:
        expected = {
            "device_loss_exercise_sha256": sha256_file(pathlib.Path(__file__)),
            "system_truth_sha256": sha256_file(SYSTEM_TRUTH_PATH),
        }
        if observed_implementation != expected:
            raise ValueError("device exercise implementation binding changed")


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "audio_device_loss_exercise_state",
        "updated_at": None,
        "receipts": {},
        "does_not_establish": [
            "physical exercise without recorded passing evidence",
            "safe audio routing or listening level",
        ],
    }


def secure_read_bytes(path: pathlib.Path, maximum_bytes: int) -> bytes:
    absolute = absolute_without_resolution(path)
    _, parent_fd = open_directory_chain(absolute.parent)
    descriptor = -1
    try:
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("device exercise input must be a regular file")
        if metadata.st_size > maximum_bytes:
            raise ValueError("device exercise JSON exceeds the byte limit")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise ValueError("device exercise JSON exceeds the byte limit")
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def load_json(path: pathlib.Path, maximum_bytes: int) -> dict[str, Any]:
    payload = json.loads(secure_read_bytes(path, maximum_bytes).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("device exercise JSON root must be an object")
    return payload


def validate_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    if (
        state.get("schema_version") != 1
        or state.get("kind") != "audio_device_loss_exercise_state"
    ):
        raise ValueError("device exercise state schema is invalid")
    receipts = state.get("receipts")
    if not isinstance(receipts, dict):
        raise ValueError("device exercise state has no receipts object")
    unknown = sorted(set(receipts) - set(DEVICE_SPECS))
    if unknown:
        raise ValueError(f"device exercise state has unknown devices: {unknown}")
    recorded_times: list[dt.datetime] = []
    for device, receipt in receipts.items():
        if not isinstance(receipt, dict) or receipt.get("status") != "passed":
            raise ValueError(f"device exercise receipt is invalid: {device}")
        evidence = receipt.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("device") != device:
            raise ValueError(f"device exercise evidence is missing: {device}")
        validate_evidence(evidence, allow_stale_implementation=True)
        if receipt.get("evidence_sha256") != sha256_json(evidence):
            raise ValueError(f"device exercise evidence digest mismatch: {device}")
        recorded_times.append(
            parse_timestamp(receipt.get("recorded_at"), f"recorded_at: {device}")
        )
    updated_at = state.get("updated_at")
    if updated_at is None:
        if recorded_times:
            raise ValueError("device exercise state has receipts but no updated_at")
    else:
        updated = parse_timestamp(updated_at, "device exercise updated_at")
        if recorded_times and updated < max(recorded_times):
            raise ValueError("device exercise updated_at predates a receipt")
    if path.exists():
        if path.is_symlink():
            raise ValueError("device exercise state must not be a symbolic link")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError("device exercise state must have mode 0600")


def read_state(path: pathlib.Path = DEFAULT_STATE) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    state = load_json(path, MAX_STATE_BYTES)
    validate_state(path, state)
    return state


def atomic_write_private(path: pathlib.Path, payload: dict[str, Any]) -> None:
    absolute = absolute_without_resolution(path)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_STATE_BYTES:
        raise ValueError("device exercise output exceeds the byte limit")
    _, parent_fd = open_directory_chain(absolute.parent, create=True)
    temporary_name = f".{absolute.name}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    try:
        try:
            existing = os.stat(
                absolute.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError("device exercise output must be a regular file")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            absolute.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def implementation_current(evidence: dict[str, Any]) -> bool:
    implementation = evidence.get("implementation")
    if not isinstance(implementation, dict):
        return False
    return implementation == {
        "device_loss_exercise_sha256": sha256_file(pathlib.Path(__file__)),
        "system_truth_sha256": sha256_file(SYSTEM_TRUTH_PATH),
    }


def current_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    identity = _snapshot_identity(snapshot)
    return {
        "observed_at": snapshot.get("observed_at"),
        "observation_sha256": snapshot.get("observation_sha256"),
        "complete": snapshot.get("complete") is True,
        "present": snapshot.get("present") is True,
        "ambiguous": snapshot.get("ambiguous") is True,
        "match_count": snapshot.get("match_count"),
        "identity_strength": (
            identity.get("identity_strength") if identity else None
        ),
        "identity_fingerprint": (
            identity.get("fingerprint") if identity else None
        ),
    }


def current_identity_binding(current: dict[str, Any]) -> dict[str, Any]:
    return {
        device: {
            "complete": item.get("complete"),
            "present": item.get("present"),
            "ambiguous": item.get("ambiguous"),
            "match_count": item.get("match_count"),
            "identity_strength": item.get("identity_strength"),
            "identity_fingerprint": item.get("identity_fingerprint"),
        }
        for device, item in sorted(current.items())
    }


def resolution(
    state: dict[str, Any],
    current_snapshots: dict[str, dict[str, Any]],
) -> tuple[set[str], dict[str, str]]:
    resolved: set[str] = set()
    invalidated: dict[str, str] = {}
    for device, receipt in state.get("receipts", {}).items():
        evidence = receipt.get("evidence") if isinstance(receipt, dict) else None
        if not isinstance(evidence, dict):
            invalidated[device] = "evidence-missing"
            continue
        if not implementation_current(evidence):
            invalidated[device] = "implementation-changed"
            continue
        snapshot = current_snapshots.get(device)
        if not isinstance(snapshot, dict):
            invalidated[device] = "current-observation-missing"
            continue
        try:
            current_fingerprint = validate_snapshot(
                snapshot,
                device,
                "current",
                expected_present=True,
            )
        except ValueError:
            invalidated[device] = "current-device-not-uniquely-present"
            continue
        if current_fingerprint != evidence.get("recovery_identity_fingerprint"):
            invalidated[device] = "current-device-identity-changed"
            continue
        resolved.add(device)
    return resolved, invalidated


def record_exercise(
    state: dict[str, Any],
    device: str,
    evidence: dict[str, Any],
    *,
    replace: bool = False,
) -> None:
    if device not in DEVICE_SPECS or evidence.get("device") != device:
        raise ValueError("device exercise record target mismatch")
    validate_evidence(evidence)
    receipts = state.setdefault("receipts", {})
    if device in receipts and not replace:
        raise ValueError("device exercise already exists; use --replace")
    now = utc_now().isoformat()
    receipts[device] = {
        "status": "passed",
        "recorded_at": now,
        "evidence_sha256": sha256_json(evidence),
        "evidence": evidence,
    }
    state["updated_at"] = now


def truth_projection(path: pathlib.Path = DEFAULT_STATE) -> dict[str, Any]:
    state = read_state(path)
    current_snapshots = {
        device: scan_device(device) for device in sorted(DEVICE_SPECS)
    }
    current = {
        device: current_summary(snapshot)
        for device, snapshot in sorted(current_snapshots.items())
    }
    resolved, invalidated = resolution(state, current_snapshots)
    receipts = {
        device: {
            "status": receipt.get("status"),
            "recorded_at": receipt.get("recorded_at"),
            "evidence_sha256": receipt.get("evidence_sha256"),
            "identity_strength": (
                receipt.get("evidence", {}).get("identity_strength")
                if isinstance(receipt.get("evidence"), dict)
                else None
            ),
            "identity_fingerprint": (
                receipt.get("evidence", {}).get(
                    "recovery_identity_fingerprint"
                )
                if isinstance(receipt.get("evidence"), dict)
                else None
            ),
        }
        for device, receipt in sorted(state.get("receipts", {}).items())
        if isinstance(receipt, dict)
    }
    unresolved = sorted(set(DEVICE_SPECS) - resolved)
    state_sha256 = sha256_json(state)
    current_identity_sha256 = sha256_json(current_identity_binding(current))
    truth_binding_sha256 = sha256_json(
        {
            "state_sha256": state_sha256,
            "current_identity_sha256": current_identity_sha256,
        }
    )
    return {
        "state_sha256": state_sha256,
        "current": current,
        "current_identity_sha256": current_identity_sha256,
        "truth_binding_sha256": truth_binding_sha256,
        "resolved": sorted(resolved),
        "invalidated": dict(sorted(invalidated.items())),
        "unresolved": unresolved,
        "recorded_count": len(state.get("receipts", {})),
        "resolved_count": len(resolved),
        "total_count": len(DEVICE_SPECS),
        "complete": len(resolved) == len(DEVICE_SPECS),
        "receipts": receipts,
        "authority": "validated-private-device-exercise-state",
    }


def output_receipt(path: pathlib.Path, evidence: dict[str, Any]) -> dict[str, Any]:
    absolute = path.absolute()
    return {
        "schema_version": 1,
        "kind": "audio_device_loss_output_receipt",
        "output_basename": absolute.name,
        "output_path_sha256": sha256_bytes(str(absolute).encode("utf-8")),
        "evidence_result": evidence.get("result"),
        "device": evidence.get("device"),
        "evidence_sha256": sha256_json(evidence),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    observe = sub.add_parser("observe")
    observe.add_argument("device", choices=sorted(DEVICE_SPECS))
    observe.add_argument("--loss-timeout-seconds", type=int, default=60)
    observe.add_argument("--recovery-timeout-seconds", type=int, default=60)
    observe.add_argument("--output", type=pathlib.Path, required=True)
    sub.add_parser("status")
    sub.add_parser("init")
    record = sub.add_parser("record")
    record.add_argument("device", choices=sorted(DEVICE_SPECS))
    record.add_argument("evidence", type=pathlib.Path)
    record.add_argument("--replace", action="store_true")
    clear = sub.add_parser("clear")
    clear.add_argument("device", choices=sorted(DEVICE_SPECS))
    args = parser.parse_args(argv)

    if args.command == "observe":
        evidence = observe_exercise(
            args.device,
            args.loss_timeout_seconds,
            args.recovery_timeout_seconds,
        )
        atomic_write_private(args.output, evidence)
        print(json.dumps(output_receipt(args.output, evidence), indent=2))
        return 0
    if args.command == "init":
        if args.state.exists():
            raise ValueError(f"device exercise state already exists: {args.state}")
        state = empty_state()
        atomic_write_private(args.state, state)
    else:
        state = read_state(args.state)
        if args.command == "record":
            evidence = load_json(args.evidence, MAX_EVIDENCE_BYTES)
            record_exercise(
                state,
                args.device,
                evidence,
                replace=args.replace,
            )
            atomic_write_private(args.state, state)
        elif args.command == "clear":
            state.get("receipts", {}).pop(args.device, None)
            state["updated_at"] = utc_now().isoformat()
            atomic_write_private(args.state, state)
    print(json.dumps(truth_projection(args.state), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
