#!/usr/bin/env python3
"""Read-only audio signal-path doctor for the Heim-PC."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import itertools
import os
import pathlib
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable

READ_ONLY_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("aplay", "-l"),
    ("arecord", "-l"),
    ("wpctl", "status"),
    ("pw-metadata", "-n", "settings", "0"),
    ("pactl", "info"),
    ("pactl", "list", "short", "sinks"),
    ("pactl", "list", "short", "sources"),
    ("aconnect", "-l"),
    ("amidi", "-l"),
    ("systemctl", "is-active", "bluetooth"),
)

MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
MAX_ELD_FILES = 32
MAX_ELD_BYTES = 256 * 1024
STREAM_CHUNK_BYTES = 8192
MOPIDY_RPC_URL = "http://127.0.0.1:6680/mopidy/rpc"
MAX_MOPIDY_RPC_BYTES = 64 * 1024
MOPIDY_RPC_TIMEOUT_SECONDS = 1.5
QBZD_STATUS_URL = "http://127.0.0.1:8182/api/status"
MAX_QBZD_STATUS_BYTES = 64 * 1024
QBZD_STATUS_TIMEOUT_SECONDS = 1.5
QBZD_MOTU_DEVICE = "front:CARD=M2,DEV=0"
MAX_ALSA_CARD_SCAN = 32  # Bound /proc/asound enumeration; real card counts are far lower.
MAX_ALSA_TEXT_BYTES = 4096  # ALSA proc control files are tiny; cap malformed/unbounded input.

SERIAL_PATTERNS = (
    re.compile(r"usb-[^\s]+", re.IGNORECASE),
    re.compile(r"M2_[A-Z0-9-]+", re.IGNORECASE),
)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    error: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    stdout_total_bytes: int | None = None
    stderr_total_bytes: int | None = None
    stdout_retained_bytes: int | None = None
    stderr_retained_bytes: int | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None


class BoundedText(str):
    truncated: bool
    observed_items: int
    max_items: int
    max_bytes: int
    retained_bytes: int

    def __new__(
        cls,
        value: str,
        *,
        truncated: bool,
        observed_items: int,
        max_items: int,
        max_bytes: int,
        retained_bytes: int,
    ) -> "BoundedText":
        instance = str.__new__(cls, value)
        instance.truncated = truncated
        instance.observed_items = observed_items
        instance.max_items = max_items
        instance.max_bytes = max_bytes
        instance.retained_bytes = retained_bytes
        return instance


def decode_bounded_utf8(data: bytes, limit: int) -> tuple[str, bool]:
    """Decode bytes while keeping the UTF-8 representation within the byte budget."""
    if limit < 1:
        raise ValueError("decode limit must be positive")
    text = data.decode("utf-8", errors="replace")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    # `encoded` is valid UTF-8. Ignore only a final partial code point after slicing.
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _drain_bounded_stream(
    stream: object, limit: int, result: dict[str, object]
) -> None:
    kept = bytearray()
    total_bytes = 0
    digest = hashlib.sha256()
    truncated = False
    try:
        while True:
            chunk = stream.read(STREAM_CHUNK_BYTES)  # type: ignore[attr-defined]
            if not chunk:
                break
            total_bytes += len(chunk)
            digest.update(chunk)
            remaining = max(0, limit - len(kept))
            if remaining:
                kept.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
    finally:
        stream.close()  # type: ignore[attr-defined]
    result["bytes"] = bytes(kept)
    result["truncated"] = truncated
    result["total_bytes"] = total_bytes
    result["sha256"] = digest.hexdigest()


def run_read_only(
    argv: tuple[str, ...],
    timeout: float = 4.0,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except FileNotFoundError as exc:
        return CommandResult(argv, 127, "", "", type(exc).__name__)

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_result: dict[str, object] = {}
    stderr_result: dict[str, object] = {}
    readers = [
        threading.Thread(
            target=_drain_bounded_stream,
            args=(process.stdout, max_output_bytes, stdout_result),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_bounded_stream,
            args=(process.stderr, max_output_bytes, stderr_result),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        returncode = process.wait()
    for reader in readers:
        reader.join()

    stdout_bytes = stdout_result.get("bytes", b"")
    stderr_bytes = stderr_result.get("bytes", b"")
    assert isinstance(stdout_bytes, bytes)
    assert isinstance(stderr_bytes, bytes)
    stdout_text, stdout_decode_truncated = decode_bounded_utf8(
        stdout_bytes, max_output_bytes
    )
    stderr_text, stderr_decode_truncated = decode_bounded_utf8(
        stderr_bytes, max_output_bytes
    )
    return CommandResult(
        argv=argv,
        returncode=124 if timed_out else returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        error="TimeoutExpired" if timed_out else None,
        stdout_truncated=bool(stdout_result.get("truncated", False))
        or stdout_decode_truncated,
        stderr_truncated=bool(stderr_result.get("truncated", False))
        or stderr_decode_truncated,
        timed_out=timed_out,
        stdout_total_bytes=int(stdout_result.get("total_bytes", len(stdout_bytes))),
        stderr_total_bytes=int(stderr_result.get("total_bytes", len(stderr_bytes))),
        stdout_retained_bytes=len(stdout_bytes),
        stderr_retained_bytes=len(stderr_bytes),
        stdout_sha256=str(
            stdout_result.get("sha256", hashlib.sha256(stdout_bytes).hexdigest())
        ),
        stderr_sha256=str(
            stderr_result.get("sha256", hashlib.sha256(stderr_bytes).hexdigest())
        ),
    )


def redact(text: str) -> str:
    value = text
    username = os.environ.get("USER", "").strip()
    hostname = os.uname().nodename.strip()
    if username:
        value = value.replace(username, "<user>")
    if hostname:
        value = value.replace(hostname, "<host>")
    for pattern in SERIAL_PATTERNS:
        value = pattern.sub("<redacted-device-id>", value)
    return value


def parse_setting(text: str, key: str) -> int | None:
    match = re.search(rf"key:'{re.escape(key)}' value:'([0-9]+)'", text)
    return int(match.group(1)) if match else None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def observe_mopidy_qobuz() -> dict[str, object]:
    """Observe whether the current local Mopidy runtime exposes a Qobuz backend."""
    request_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "core.library.browse",
            "params": {"uri": None},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        MOPIDY_RPC_URL,
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
    try:
        with opener.open(request, timeout=MOPIDY_RPC_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_MOPIDY_RPC_BYTES + 1)
            status = getattr(response, "status", 200)
            final_url = response.geturl()
    except (OSError, urllib.error.URLError):
        return {
            "rpc_reachable": False,
            "backend_registered": False,
            "status": "rpc-unavailable",
            "reason": "local-mopidy-rpc-unavailable",
        }
    if final_url != MOPIDY_RPC_URL or status != 200 or len(body) > MAX_MOPIDY_RPC_BYTES:
        return {
            "rpc_reachable": False,
            "backend_registered": False,
            "status": "rpc-invalid",
            "reason": "local-mopidy-rpc-response-invalid",
        }
    try:
        payload = json.loads(body.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        return {
            "rpc_reachable": False,
            "backend_registered": False,
            "status": "rpc-invalid",
            "reason": "local-mopidy-rpc-response-invalid",
        }
    return classify_mopidy_qobuz_payload(payload)


def classify_mopidy_qobuz_payload(payload: object) -> dict[str, object]:
    """Classify one bounded Mopidy root-browse JSON-RPC response fail-closed."""
    invalid = {
        "rpc_reachable": True,
        "backend_registered": False,
        "status": "rpc-invalid",
        "reason": "local-mopidy-rpc-response-invalid",
    }
    if not isinstance(payload, dict):
        return invalid
    if payload.get("jsonrpc") != "2.0" or payload.get("id") != 1:
        return invalid
    if "error" in payload:
        return {**invalid, "reason": "local-mopidy-rpc-error"}
    result = payload.get("result")
    if not isinstance(result, list):
        return invalid
    backend_registered = any(
        isinstance(item, dict)
        and isinstance(item.get("uri"), str)
        and item["uri"].casefold().startswith("qobuz:")
        for item in result
    )
    return {
        "rpc_reachable": True,
        "backend_registered": backend_registered,
        "status": "available" if backend_registered else "backend-unavailable",
        "reason": None if backend_registered else "qobuz-backend-not-registered",
    }


def parse_alsa_hw_params(text: str) -> dict[str, object]:
    """Parse one ALSA hw_params projection without inferring a closed PCM state."""
    value = text.strip()
    if not value or value == "closed":
        return {"open": False, "rate_hz": None, "format": None, "channels": None}
    fields: dict[str, str] = {}
    for line in value.splitlines():
        key, separator, raw = line.partition(":")
        if separator:
            fields[key.strip()] = raw.strip()
    rate_match = re.match(r"(?P<rate>[0-9]+)(?:\s|$)", fields.get("rate", ""))
    channel_text = fields.get("channels", "")
    channels = int(channel_text) if channel_text.isdigit() else None
    return {
        "open": True,
        "rate_hz": int(rate_match.group("rate")) if rate_match else None,
        "format": fields.get("format") or None,
        "channels": channels,
    }


def _read_small_text(path: pathlib.Path, limit: int = MAX_ALSA_TEXT_BYTES) -> str | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeError:
        return None


def parse_alsa_pcm_status(text: str) -> dict[str, object]:
    """Parse ALSA PCM state and owner PID for immediate local ownership classification."""
    value = text.strip()
    if not value or value == "closed":
        return {"state": "CLOSED", "owner_pid": None}
    state_match = re.search(r"^state:\s*([A-Z_]+)\s*$", value, re.MULTILINE)
    owner_match = re.search(r"^owner_pid\s*:\s*([0-9]+)\s*$", value, re.MULTILINE)
    return {
        "state": state_match.group(1) if state_match else "UNKNOWN",
        "owner_pid": int(owner_match.group(1)) if owner_match else None,
    }


def classify_process_owner(
    owner_pid: int | None,
    proc_root: pathlib.Path = pathlib.Path("/proc"),
) -> str:
    """Classify a PCM owner without exposing process identifiers or unrelated names."""
    if owner_pid is None or owner_pid <= 0:
        return "unknown"
    comm = _read_small_text(proc_root / str(owner_pid) / "comm", 128)
    if comm is None:
        return "unknown"
    process = comm.strip()
    if process == "qbzd":
        return "qbzd"
    if process == "pipewire":
        return "pipewire"
    return "other"


def observe_motu_playback_hw_params(
    asound_root: pathlib.Path = pathlib.Path("/proc/asound"),
    proc_root: pathlib.Path = pathlib.Path("/proc"),
) -> dict[str, object]:
    """Observe the currently open MOTU M2 playback PCM directly from ALSA proc state."""
    try:
        candidates = sorted(
            (
                item
                for item in asound_root.iterdir()
                if item.is_dir() and re.fullmatch(r"card[0-9]+", item.name)
            ),
            key=lambda item: int(item.name[4:]),
        )[:MAX_ALSA_CARD_SCAN]
    except OSError:
        candidates = []
    for card in candidates:
        card_id = _read_small_text(card / "id", 128)
        if card_id is None or card_id.strip() != "M2":
            continue
        pcm_dir = card / "pcm0p" / "sub0"
        hw_before = _read_small_text(pcm_dir / "hw_params")
        status_text = _read_small_text(pcm_dir / "status")
        hw_after = _read_small_text(pcm_dir / "hw_params")
        if hw_before is None or hw_after is None:
            return {
                "observed": True,
                "card_id": "M2",
                "open": False,
                "rate_hz": None,
                "format": None,
                "channels": None,
                "pcm_state": "UNKNOWN",
                "owner_class": "unknown",
                "snapshot_consistent": False,
                "reason": "motu-playback-hw-params-unavailable",
            }
        snapshot_consistent = hw_before == hw_after
        parsed = parse_alsa_hw_params(hw_after if snapshot_consistent else "closed")
        status = parse_alsa_pcm_status(status_text or "")
        owner_class = (
            classify_process_owner(status.get("owner_pid"), proc_root)
            if snapshot_consistent
            else "unknown"
        )
        return {
            "observed": True,
            "card_id": "M2",
            **parsed,
            "pcm_state": status.get("state"),
            "owner_class": owner_class,
            "snapshot_consistent": snapshot_consistent,
            "reason": None if snapshot_consistent else "motu-playback-snapshot-changed",
        }
    return {
        "observed": False,
        "card_id": None,
        "open": False,
        "rate_hz": None,
        "format": None,
        "channels": None,
        "pcm_state": "CLOSED",
        "owner_class": "unknown",
        "snapshot_consistent": True,
        "reason": "motu-m2-alsa-card-not-observed",
    }


def classify_qbzd_status_payload(payload: object) -> dict[str, object]:
    """Project QBZD status onto a small, non-sensitive reference-path allowlist."""
    invalid = {
        "api_reachable": True,
        "status": "api-invalid",
        "reason": "local-qbzd-status-response-invalid",
        "reference_provider_ready": False,
        "track_native_proven": False,
        "rate_proof_state": "blocked",
    }
    if not isinstance(payload, dict) or payload.get("api_version") != 1:
        return invalid
    audio = payload.get("audio")
    auth = payload.get("auth")
    qconnect = payload.get("qconnect")
    playback = payload.get("playback")
    if not isinstance(audio, dict) or not isinstance(auth, dict) or not isinstance(qconnect, dict):
        return invalid
    playback_state = (
        playback.get("state")
        if isinstance(playback, dict) and isinstance(playback.get("state"), str)
        else None
    )
    backend = audio.get("backend") if isinstance(audio.get("backend"), str) else None
    configured_device = audio.get("configured_device") if isinstance(audio.get("configured_device"), str) else None
    auth_state = auth.get("state") if isinstance(auth.get("state"), str) else None
    subscription = auth.get("subscription") if isinstance(auth.get("subscription"), str) else None
    qconnect_state = qconnect.get("state") if isinstance(qconnect.get("state"), str) else None
    session_active = qconnect.get("session_active") is True
    device_name = qconnect.get("device_name") if isinstance(qconnect.get("device_name"), str) else None
    device_present = audio.get("device_present") is True
    raw_device_open = audio.get("device_open")
    device_open = raw_device_open if isinstance(raw_device_open, bool) else None
    sample_rate = audio.get("sample_rate")
    if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or sample_rate <= 0:
        sample_rate = None
    bit_depth = audio.get("bit_depth")
    if not isinstance(bit_depth, int) or isinstance(bit_depth, bool) or bit_depth <= 0:
        bit_depth = None
    bit_perfect = audio.get("bit_perfect") if isinstance(audio.get("bit_perfect"), str) else None
    version = payload.get("version") if isinstance(payload.get("version"), str) else None
    ready = (
        auth_state == "logged_in"
        and backend == "alsa"
        and configured_device == QBZD_MOTU_DEVICE
        and device_present
        and qconnect_state == "connected"
        and session_active
    )
    if ready:
        status = "available"
        reason = None
    elif auth_state != "logged_in":
        status = "not-ready"
        reason = "qbzd-qobuz-auth-not-ready"
    elif backend != "alsa" or configured_device != QBZD_MOTU_DEVICE or not device_present:
        status = "not-ready"
        reason = "qbzd-reference-audio-route-not-ready"
    else:
        status = "not-ready"
        reason = "qbzd-qconnect-session-not-ready"
    return {
        "api_reachable": True,
        "status": status,
        "reason": reason,
        "reference_provider_ready": ready,
        "version": version,
        "auth_state": auth_state,
        "subscription": subscription,
        "audio": {
            "backend": backend,
            "configured_device": configured_device,
            "device_present": device_present,
            "device_open": device_open,
            "sample_rate_hz": sample_rate,
            "bit_depth": bit_depth,
            "bit_perfect_mode": bit_perfect,
        },
        "qconnect": {
            "state": qconnect_state,
            "session_active": session_active,
            "device_name": device_name,
        },
        "playback_state": playback_state,
        "track_native_proven": False,
        "rate_proof_state": "ready-awaiting-playback" if ready else "blocked",
    }


def observe_qbzd_qobuz() -> dict[str, object]:
    """Read fixed loopback QBZD status and discard account/playback identity fields."""
    request = urllib.request.Request(QBZD_STATUS_URL, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=QBZD_STATUS_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_QBZD_STATUS_BYTES + 1)
            status = getattr(response, "status", 200)
            final_url = response.geturl()
    except urllib.error.HTTPError:
        return {
            "api_reachable": True,
            "status": "api-invalid",
            "reason": "local-qbzd-status-response-invalid",
            "reference_provider_ready": False,
            "track_native_proven": False,
            "rate_proof_state": "blocked",
        }
    except (OSError, urllib.error.URLError):
        return {
            "api_reachable": False,
            "status": "api-unavailable",
            "reason": "local-qbzd-status-unavailable",
            "reference_provider_ready": False,
            "track_native_proven": False,
            "rate_proof_state": "blocked",
        }
    if final_url != QBZD_STATUS_URL or status != 200 or len(body) > MAX_QBZD_STATUS_BYTES:
        return {
            "api_reachable": True,
            "status": "api-invalid",
            "reason": "local-qbzd-status-response-invalid",
            "reference_provider_ready": False,
            "track_native_proven": False,
            "rate_proof_state": "blocked",
        }
    try:
        payload = json.loads(body.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        return {
            "api_reachable": True,
            "status": "api-invalid",
            "reason": "local-qbzd-status-response-invalid",
            "reference_provider_ready": False,
            "track_native_proven": False,
            "rate_proof_state": "blocked",
        }
    return classify_qbzd_status_payload(payload)


def is_motu_m2_endpoint(name: str | None) -> bool:
    if not name:
        return False
    lowered = name.casefold()
    return bool(
        re.search(r"(?:^|[._\s-])motu[._\s-]+m2(?:[._\s-]|$)", lowered)
        or re.search(r"(?:^|[._\s-])m2[._\s-]+motu(?:[._\s-]|$)", lowered)
    )


def normalize_endpoint(name: str | None) -> str | None:
    if not name:
        return None
    lowered = name.lower()
    if is_motu_m2_endpoint(name):
        return "motu-m2"
    if "roland" in lowered or "digital_piano" in lowered:
        return "roland-fp-30x"
    if "hdmi" in lowered:
        return "hdmi"
    if "iec958" in lowered:
        return "spdif"
    return "other"


def parse_pactl_default(text: str, kind: str) -> str | None:
    labels = {
        "sink": ("Default Sink", "Standard-Ziel"),
        "source": ("Default Source", "Standard-Quelle"),
    }
    for line in text.splitlines():
        if any(line.startswith(f"{label}:") for label in labels[kind]):
            return line.split(":", 1)[1].strip()
    return None


def contains_device(text: str, device: str) -> bool:
    if device == "motu-m2":
        return any(
            is_motu_m2_endpoint(line)
            or re.search(r"\bM2\s*\[M2\]", line, re.IGNORECASE) is not None
            for line in text.splitlines()
        )
    if device == "roland-fp-30x":
        return bool(
            re.search(
                r"Roland Digital Piano|Roland.*Piano|FP[- ]?30X",
                text,
                re.IGNORECASE,
            )
        )
    raise ValueError(f"unknown device: {device}")


def read_bounded_text_files(
    paths: Iterable[pathlib.Path],
    *,
    max_files: int,
    max_bytes: int,
) -> BoundedText:
    if max_files < 1 or max_bytes < 1:
        raise ValueError("bounded text limits must be positive")
    retained = bytearray()
    observed_items = 0
    truncated = False
    for path in paths:
        if observed_items >= max_files:
            truncated = True
            break
        observed_items += 1
        separator_bytes = 1 if retained else 0
        remaining = max_bytes - len(retained) - separator_bytes
        if remaining <= 0:
            truncated = True
            break
        try:
            with path.open("rb") as handle:
                chunk = handle.read(remaining + 1)
        except OSError:
            continue
        if retained:
            retained.extend(b"\n")
        if len(chunk) > remaining:
            retained.extend(chunk[:remaining])
            truncated = True
            break
        retained.extend(chunk)
    retained_bytes = len(retained)
    value, decode_truncated = decode_bounded_utf8(bytes(retained), max_bytes)
    return BoundedText(
        value,
        truncated=truncated or decode_truncated,
        observed_items=observed_items,
        max_items=max_files,
        max_bytes=max_bytes,
        retained_bytes=retained_bytes,
    )


def read_eld_text() -> BoundedText:
    paths = itertools.islice(
        pathlib.Path("/proc/asound").glob("card*/eld#*"), MAX_ELD_FILES + 1
    )
    return read_bounded_text_files(
        paths, max_files=MAX_ELD_FILES, max_bytes=MAX_ELD_BYTES
    )


def physical_unknowns(contract_path: pathlib.Path | None = None) -> list[str]:
    path = contract_path or (
        pathlib.Path(__file__).resolve().parents[1]
        / "inventory"
        / "physical-verification.v1.json"
    )
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"physical verification contract unavailable: {path}"
        ) from exc
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        raise RuntimeError("physical verification contract has no facts object")
    non_null = [name for name, value in facts.items() if value is not None]
    if non_null:
        raise RuntimeError(
            "doctor accepts only the unverified template; verified physical facts require "
            "a separately signed observation"
        )
    return sorted(facts)


def desired_hardware() -> list[str]:
    profile_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "profiles"
        / "audio-profiles.v1.json"
    )
    try:
        payload = json.loads(profile_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"audio profile catalog unavailable: {profile_path}"
        ) from exc
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        raise RuntimeError("audio profile catalog has no profiles object")
    devices: set[str] = set()
    for profile in profiles.values():
        if not isinstance(profile, dict):
            raise RuntimeError("audio profile catalog contains a non-object profile")
        required = profile.get("required_hardware", [])
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            raise RuntimeError("audio profile required_hardware must be a string array")
        devices.update(required)
    return sorted(devices)


def qbzd_reference_proof_snapshot(observation: object) -> dict[str, object]:
    """Return only safe QBZD fields whose change invalidates a joint rate proof."""
    if not isinstance(observation, dict):
        return {"valid": False}
    audio = observation.get("audio")
    qconnect = observation.get("qconnect")
    if not isinstance(audio, dict):
        audio = {}
    if not isinstance(qconnect, dict):
        qconnect = {}
    return {
        "valid": True,
        "api_reachable": observation.get("api_reachable"),
        "status": observation.get("status"),
        "reason": observation.get("reason"),
        "reference_provider_ready": observation.get("reference_provider_ready"),
        "auth_state": observation.get("auth_state"),
        "audio_backend": audio.get("backend"),
        "configured_device": audio.get("configured_device"),
        "device_present": audio.get("device_present"),
        "device_open": audio.get("device_open"),
        "sample_rate_hz": audio.get("sample_rate_hz"),
        "bit_depth": audio.get("bit_depth"),
        "bit_perfect_mode": audio.get("bit_perfect_mode"),
        "qconnect_state": qconnect.get("state"),
        "qconnect_session_active": qconnect.get("session_active"),
    }


def classify_qbzd_rate_proof_state(
    *,
    qbzd_ready: bool,
    motu_playback: dict[str, object],
    qbzd_snapshot_observed_twice: bool,
    qbzd_snapshot_consistent: bool,
    direct_hardware_reported: bool,
    qbzd_device_open: object,
    qbzd_rate: object,
    motu_rate: object,
) -> str:
    """Classify one current-track proof attempt from strongest failure to PASS."""
    if not qbzd_ready:
        return "blocked"
    if motu_playback.get("snapshot_consistent") is not True:
        return "hardware-snapshot-unstable"
    if motu_playback.get("open") is not True:
        return "ready-awaiting-playback"
    pcm_state = motu_playback.get("pcm_state")
    if pcm_state in {"SETUP", "PREPARED"}:
        return "hardware-preparing"
    if pcm_state != "RUNNING":
        return "hardware-not-running"
    if not qbzd_snapshot_observed_twice:
        return "qbzd-snapshot-unavailable"
    if not qbzd_snapshot_consistent:
        return "qbzd-snapshot-unstable"
    owner_class = motu_playback.get("owner_class")
    if (
        owner_class == "pipewire"
        and qbzd_device_open is False
        and qbzd_rate is None
        and not direct_hardware_reported
    ):
        # PipeWire legitimately keeps the MOTU PCM running in the shared 48 kHz
        # desktop graph while QBZD is idle. This is not a failed Qobuz proof
        # attempt and can never establish track-native playback.
        return "desktop-mixed-active"
    if owner_class != "qbzd":
        return "hardware-owner-unverified"
    if not direct_hardware_reported:
        return "direct-hardware-not-reported"
    if not isinstance(motu_rate, int) or isinstance(motu_rate, bool) or motu_rate <= 0:
        return "motu-rate-unreadable"
    if not isinstance(qbzd_rate, int) or isinstance(qbzd_rate, bool) or qbzd_rate <= 0:
        return "qbzd-rate-unreadable"
    if qbzd_rate != motu_rate:
        return "rate-mismatch"
    return "verified-current-track"


def build_report(
    results: Iterable[CommandResult],
    eld_text: str = "",
    mopidy_qobuz: dict[str, object] | None = None,
    qbzd_qobuz: dict[str, object] | None = None,
    motu_playback: dict[str, object] | None = None,
    qbzd_qobuz_before: dict[str, object] | None = None,
) -> dict[str, object]:
    result_list = list(results)
    by_command = {result.argv: result for result in result_list}
    aplay = by_command.get(("aplay", "-l"), CommandResult((), 127, "", ""))
    arecord = by_command.get(("arecord", "-l"), CommandResult((), 127, "", ""))
    wpctl = by_command.get(("wpctl", "status"), CommandResult((), 127, "", ""))
    metadata = by_command.get(
        ("pw-metadata", "-n", "settings", "0"), CommandResult((), 127, "", "")
    )
    pactl_info = by_command.get(("pactl", "info"), CommandResult((), 127, "", ""))
    sinks = by_command.get(
        ("pactl", "list", "short", "sinks"), CommandResult((), 127, "", "")
    )
    sources = by_command.get(
        ("pactl", "list", "short", "sources"), CommandResult((), 127, "", "")
    )
    aconnect = by_command.get(("aconnect", "-l"), CommandResult((), 127, "", ""))
    amidi = by_command.get(("amidi", "-l"), CommandResult((), 127, "", ""))
    bluetooth = by_command.get(
        ("systemctl", "is-active", "bluetooth"), CommandResult((), 127, "", "")
    )

    alsa_audio_text = "\n".join((aplay.stdout, arecord.stdout))
    pipewire_text = wpctl.stdout
    midi_text = "\n".join((aconnect.stdout, amidi.stdout))
    sink_name = parse_pactl_default(pactl_info.stdout, "sink")
    source_name = parse_pactl_default(pactl_info.stdout, "source")
    rate = parse_setting(metadata.stdout, "clock.force-rate") or parse_setting(
        metadata.stdout, "clock.rate"
    )
    quantum = parse_setting(metadata.stdout, "clock.force-quantum") or parse_setting(
        metadata.stdout, "clock.quantum"
    )
    buffer_period_ms = round(quantum / rate * 1000, 3) if rate and quantum else None
    motu_alsa = contains_device(alsa_audio_text, "motu-m2")
    motu_pipewire = contains_device(pipewire_text, "motu-m2")
    roland_alsa_audio = contains_device(alsa_audio_text, "roland-fp-30x")
    roland_pipewire = contains_device(pipewire_text, "roland-fp-30x")
    roland_midi = contains_device(midi_text, "roland-fp-30x")
    # Physical presence is fail-closed: defaults and PipeWire labels are configuration/graph
    # evidence, not proof that a USB device is currently attached.
    motu = motu_alsa
    roland = roland_midi
    sink = normalize_endpoint(sink_name)
    source = normalize_endpoint(source_name)
    pioneer_observed = bool(re.search(r"Pioneer|VSX-?830", eld_text, re.IGNORECASE))
    bluetooth_active = bluetooth.stdout.strip() == "active"
    qobuz_observation = mopidy_qobuz or {
        "rpc_reachable": None,
        "backend_registered": None,
        "status": "not-observed",
        "reason": "mopidy-qobuz-not-observed",
    }
    qbzd_observation = qbzd_qobuz or {
        "api_reachable": None,
        "status": "not-observed",
        "reason": "qbzd-qobuz-not-observed",
        "reference_provider_ready": False,
        "track_native_proven": False,
        "rate_proof_state": "blocked",
    }
    qbzd_before_observation = qbzd_qobuz_before
    qbzd_snapshot_observed_twice = isinstance(qbzd_before_observation, dict)
    qbzd_snapshot_consistent = (
        qbzd_snapshot_observed_twice
        and qbzd_reference_proof_snapshot(qbzd_before_observation)
        == qbzd_reference_proof_snapshot(qbzd_observation)
    )
    qbzd_ready = qbzd_observation.get("reference_provider_ready") is True
    legacy_mopidy_ready = qobuz_observation.get("backend_registered") is True
    selected_qobuz_provider = (
        "qbzd-qconnect" if qbzd_ready else "mopidy-legacy" if legacy_mopidy_ready else None
    )
    motu_playback_observation = motu_playback or {
        "observed": False,
        "card_id": None,
        "open": False,
        "rate_hz": None,
        "format": None,
        "channels": None,
        "pcm_state": "CLOSED",
        "owner_class": "unknown",
        "snapshot_consistent": True,
        "reason": "motu-playback-not-observed",
    }
    qbzd_audio = qbzd_observation.get("audio")
    if not isinstance(qbzd_audio, dict):
        qbzd_audio = {}
    qbzd_rate = qbzd_audio.get("sample_rate_hz")
    motu_rate = motu_playback_observation.get("rate_hz")
    direct_hardware_reported = qbzd_audio.get("bit_perfect_mode") == "DirectHardware"
    qbzd_rate_proof_state = classify_qbzd_rate_proof_state(
        qbzd_ready=qbzd_ready,
        motu_playback=motu_playback_observation,
        qbzd_snapshot_observed_twice=qbzd_snapshot_observed_twice,
        qbzd_snapshot_consistent=qbzd_snapshot_consistent,
        direct_hardware_reported=direct_hardware_reported,
        qbzd_device_open=qbzd_audio.get("device_open"),
        qbzd_rate=qbzd_rate,
        motu_rate=motu_rate,
    )
    track_native_proven = qbzd_rate_proof_state == "verified-current-track"

    warnings: list[dict[str, str]] = []
    if source != "motu-m2":
        warnings.append(
            {
                "code": "voice-source-not-motu",
                "severity": "high",
                "detail": "Default capture source is not the MOTU M2 microphone input.",
            }
        )
    configured_endpoints = {"default_sink": sink, "default_source": source}
    observed_presence = {"motu-m2": motu, "roland-fp-30x": roland}
    for role, endpoint in configured_endpoints.items():
        if endpoint in observed_presence and not observed_presence[endpoint]:
            warnings.append(
                {
                    "code": "configured-default-device-absent",
                    "severity": "high",
                    "detail": f"{role} names {endpoint}, but current physical observation does not confirm it.",
                }
            )
    if quantum and quantum >= 1024:
        warnings.append(
            {
                "code": "high-live-quantum",
                "severity": "medium",
                "detail": "The current quantum favors stability over low-latency live monitoring.",
            }
        )
    if "44100Hz" in sources.stdout or "44100Hz" in sinks.stdout:
        warnings.append(
            {
                "code": "mixed-sample-rates",
                "severity": "medium",
                "detail": "At least one endpoint runs at 44.1 kHz inside the 48 kHz graph.",
            }
        )
    if not bluetooth_active:
        warnings.append(
            {
                "code": "bluetooth-service-inactive",
                "severity": "info",
                "detail": "The system Bluetooth service is inactive; an external transmitter remains unobservable.",
            }
        )
    qobuz_status = qobuz_observation.get("status")
    if qobuz_status == "backend-unavailable":
        warnings.append(
            {
                "code": "qobuz-mopidy-backend-unavailable",
                "severity": "medium",
                "detail": "Legacy Mopidy is reachable, but its Qobuz backend is not registered; the Mopidy rate probe cannot run.",
            }
        )
    elif qobuz_status == "rpc-unavailable":
        warnings.append(
            {
                "code": "qobuz-mopidy-rpc-unavailable",
                "severity": "medium",
                "detail": "The legacy Mopidy RPC is unavailable; its Qobuz backend state is unknown.",
            }
        )
    elif qobuz_status == "rpc-invalid":
        warnings.append(
            {
                "code": "qobuz-mopidy-probe-invalid",
                "severity": "medium",
                "detail": "The legacy Mopidy RPC probe returned an invalid or error response; its Qobuz backend state is unknown.",
            }
        )
    qbzd_status = qbzd_observation.get("status")
    if qbzd_status == "api-unavailable":
        warnings.append({"code": "qobuz-qbzd-api-unavailable", "severity": "medium", "detail": "The loopback QBZD status API is unavailable; reference-provider readiness is unknown."})
    elif qbzd_status == "api-invalid":
        warnings.append({"code": "qobuz-qbzd-probe-invalid", "severity": "medium", "detail": "The QBZD status response is invalid; reference-provider readiness is unknown."})
    elif qbzd_status == "not-ready":
        warnings.append({"code": "qobuz-qbzd-reference-not-ready", "severity": "medium", "detail": "QBZD is observable but the Qobuz reference route is not fully ready."})
    if qbzd_rate_proof_state == "rate-mismatch":
        warnings.append({"code": "qobuz-qbzd-motu-rate-mismatch", "severity": "high", "detail": "QBZD and the currently open MOTU hardware PCM report different sample rates; track-native playback is not established."})
    elif qbzd_rate_proof_state == "motu-rate-unreadable":
        warnings.append({"code": "qobuz-qbzd-motu-rate-unreadable", "severity": "high", "detail": "The MOTU playback PCM is running, but its hardware sample rate could not be read; track-native playback is not established."})
    elif qbzd_rate_proof_state == "qbzd-rate-unreadable":
        warnings.append({"code": "qobuz-qbzd-rate-unreadable", "severity": "high", "detail": "QBZD is running the reference route, but its current sample rate is unreadable; track-native playback is not established."})
    elif qbzd_rate_proof_state == "hardware-owner-unverified":
        warnings.append({"code": "qobuz-qbzd-motu-owner-unverified", "severity": "high", "detail": "The MOTU playback PCM is running, but its owner is not verified as QBZD; track-native playback is not established."})
    elif qbzd_rate_proof_state == "hardware-snapshot-unstable":
        warnings.append({"code": "qobuz-qbzd-motu-snapshot-unstable", "severity": "high", "detail": "The MOTU hardware snapshot changed while it was being read; track-native playback is not established."})
    elif qbzd_rate_proof_state == "qbzd-snapshot-unstable":
        warnings.append({"code": "qobuz-qbzd-snapshot-unstable", "severity": "high", "detail": "QBZD proof-relevant status changed around the MOTU observation; track-native playback is not established."})

    return {
        "schema_version": 1,
        "kind": "audio_doctor_report",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "read_only_contract": True,
        # Backward-compatible physical-presence projection used by profile planning.
        "hardware": {"motu_m2": motu, "roland_fp_30x": roland},
        "device_truth": {
            "observed": {
                "motu_m2": {
                    "present": motu,
                    "alsa_audio": motu_alsa,
                    "pipewire_graph": motu_pipewire,
                },
                "roland_fp_30x": {
                    "present": roland,
                    "alsa_audio": roland_alsa_audio,
                    "pipewire_graph": roland_pipewire,
                    "alsa_midi": roland_midi,
                },
            },
            "configured_defaults": configured_endpoints,
            "desired": {device: True for device in desired_hardware()},
        },
        "graph": {
            "default_sink": sink,
            "default_source": source,
            "force_rate_hz": rate,
            "force_quantum_frames": quantum,
            "single_buffer_period_ms": buffer_period_ms,
            "round_trip_latency_ms": None,
        },
        "streaming_sources": {
            "qobuz": {
                "selected_reference_provider": selected_qobuz_provider,
                "reference_provider_ready": qbzd_ready or legacy_mopidy_ready,
                "rate_probe_backend_ready": qbzd_ready or legacy_mopidy_ready,
                "track_native_proven": track_native_proven,
                "rate_proof_state": (qbzd_rate_proof_state if qbzd_ready else "legacy-mopidy-ready" if legacy_mopidy_ready else "blocked"),
                "direct_hardware_reported": direct_hardware_reported,
                "qbzd_snapshot_observed_twice": qbzd_snapshot_observed_twice,
                "qbzd_snapshot_consistent": qbzd_snapshot_consistent,
                "motu_hardware_playback": motu_playback_observation,
                "qbzd": qbzd_observation,
                "mopidy_legacy": qobuz_observation,
                # Deprecated compatibility alias; `mopidy_legacy` is canonical.
                "mopidy": qobuz_observation,
                "browser_quality_boundary": "shared-pipewire-mixed-path; track-native-not-established",
            }
        },
        "external_endpoints": {
            "pioneer_vsx_830_k": {
                "software_observed": pioneer_observed,
                "physical_connection": None,
            },
            "transmitter_1mii_b03_pro": {
                "software_observed": False,
                "bluetooth_service_active": bluetooth_active,
                "physical_connection": None,
                "codec": None,
            },
        },
        "profiles": {
            "headphone_listening": {
                "software_ready": motu and sink == "motu-m2",
                "physical_ready": None,
            },
            "voice_recording": {
                "software_ready": motu and source == "motu-m2",
                "physical_ready": None,
            },
            "piano_software_monitoring": {
                "software_ready": motu and roland and sink == "motu-m2",
                "physical_ready": None,
            },
        },
        "physical_unknowns": physical_unknowns(),
        "warnings": warnings,
        "input_health": {
            "eld": {
                "truncated": bool(getattr(eld_text, "truncated", False)),
                "observed_files": getattr(eld_text, "observed_items", None),
                "max_files": getattr(eld_text, "max_items", MAX_ELD_FILES),
                "max_bytes": getattr(eld_text, "max_bytes", MAX_ELD_BYTES),
                "retained_bytes": getattr(
                    eld_text, "retained_bytes", len(eld_text.encode("utf-8"))
                ),
            }
        },
        "command_health": [
            {
                "command": " ".join(result.argv),
                "available": result.error is None,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
                "stdout_retained_bytes": (
                    result.stdout_retained_bytes
                    if result.stdout_retained_bytes is not None
                    else len(result.stdout.encode("utf-8"))
                ),
                "stderr_retained_bytes": (
                    result.stderr_retained_bytes
                    if result.stderr_retained_bytes is not None
                    else len(result.stderr.encode("utf-8"))
                ),
                "max_output_bytes_per_stream": MAX_COMMAND_OUTPUT_BYTES,
            }
            for result in result_list
        ],
    }


def absolute_without_resolution(path: pathlib.Path) -> pathlib.Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = pathlib.Path.cwd() / expanded
    if any(part in {"", ".", ".."} for part in expanded.parts[1:]):
        raise OSError(f"unsafe output path component: {path}")
    return expanded


def open_directory_chain(
    path: pathlib.Path, *, create: bool = False
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


def atomic_write_output(path: pathlib.Path, payload: str) -> None:
    absolute = absolute_without_resolution(path)
    encoded = payload.encode("utf-8")
    _, parent_fd = open_directory_chain(absolute.parent, create=True)
    temporary_name = f".{absolute.name}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    try:
        mode = 0o600
        try:
            existing = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                raise OSError(f"output path is not a regular file: {path}")
            mode = stat.S_IMODE(existing.st_mode)

        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    results = [run_read_only(command) for command in READ_ONLY_COMMANDS]
    qbzd_before = observe_qbzd_qobuz()
    motu_playback = observe_motu_playback_hw_params()
    qbzd_after = observe_qbzd_qobuz()
    report = build_report(
        results,
        read_eld_text(),
        observe_mopidy_qobuz(),
        qbzd_after,
        motu_playback,
        qbzd_before,
    )
    encoded = (
        json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None) + "\n"
    )
    encoded = redact(encoded)
    if args.output:
        atomic_write_output(args.output, encoded)
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
