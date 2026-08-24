#!/usr/bin/env python3
"""Fail-closed remote projection with narrowly scoped audio action channels.

The canonical Audio Control service remains loopback-only on 127.0.0.1:8765.
This separate loopback service exposes a read-only projection plus exactly the
typed Buckelwal and recorder actions for the private Tailnet frontend. It is
not an open proxy and never exposes the backend action token.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import pathlib
import re
import secrets
import socket
import stat
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CONTRACT_ID = "audiozentrale-remote-bridge-v1"
BRIDGE_HEADER = "read-only-v1"
BRIDGE_WHALE_ACTION_HEADER = "whale-action-v1"
BRIDGE_RECORDING_ACTION_HEADER = "recording-action-v1"
REMOTE_EFFECTS_HEADER = "X-Audio-Remote-Effects"
REMOTE_WHALE_EFFECTS_VALUE = "whale-v1"
REMOTE_RECORDING_EFFECTS_VALUE = "recording-v1"
REMOTE_SESSION_ROUTE = "/bridge/v1/session"
REMOTE_WHALE_ACTION_ROUTE = "/bridge/v1/actions/whale"
REMOTE_RECORDING_ACTION_ROUTE = "/bridge/v1/actions/recording"
REMOTE_ACTION_TOKEN_HEADER = "X-Audio-Bridge-Session"
REMOTE_ACTION_SESSION_TTL_SECONDS = 15 * 60
REMOTE_ACTION_SESSION_CAPACITY = 8
MAX_ACTION_BODY_BYTES = 512
MAX_RECORDING_ACTION_BODY_BYTES = 1024
WHALE_ACTION_MODES = frozenset({"morph", "organic", "realistic", "ufo"})
WHALE_ACTION_OPERATIONS = frozenset({"start", "mode", "stop"})
RECORDING_ACTION_MODES = frozenset({"voice", "piano-vocal"})
RECORDING_ACTION_OPERATIONS = frozenset(
    {"plan", "prepare", "start", "stop", "recover", "categorize", "trash", "restore"}
)
RECORDING_LIBRARY_CATEGORIES = frozenset(
    {"unsorted", "song", "practice", "idea", "test", "finished"}
)
RECORDING_SESSION_ID_RE = re.compile(r"^[0-9a-f]{24}$")
REMOTE_TAILNET_HOST = "heim-pc.tail6dbb90.ts.net:9443"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8765
BACKEND_TIMEOUT_SECONDS = 6.0
# Recorder stop may spend up to 45 s in the bounded recorder action and then
# verify the final WAV plus optional Roland MIDI, each with a 30 s bound.
# The bridge must outlive that backend contract so a successful stop cannot be
# misreported remotely merely because post-stop verification is still running.
RECORDING_BACKEND_TIMEOUT_SECONDS = 120.0
# Recorder prepare may run two bounded 30 s plans plus service-state checks,
# controlled stops/restores and one 20 s PipeWire core restart.  Keep the
# bridge beyond that complete backend bound so successful convergence is not
# misreported as a remote timeout.
RECORDING_PREPARE_BACKEND_TIMEOUT_SECONDS = 240.0
REQUEST_IO_TIMEOUT_SECONDS = 6.0
MAX_REQUEST_LINE_BYTES = 2048
MAX_HEADER_BYTES = 16_384
MAX_BACKEND_HEADER_BYTES = 32_768
MAX_RESPONSE_BYTES = 1_048_576
MAX_RECORDING_AUDIO_STREAM_BYTES = 6_000_000_000
MAX_CONCURRENT_REQUESTS = 8
MAX_CONDITIONAL_HEADER_BYTES = 4096
MAX_RANGE_HEADER_BYTES = 128


def recording_backend_timeout_seconds(operation: str) -> float:
    return (
        RECORDING_PREPARE_BACKEND_TIMEOUT_SECONDS
        if operation == "prepare"
        else RECORDING_BACKEND_TIMEOUT_SECONDS
    )
ACCEPTANCE_STATE_PATH = (
    pathlib.Path.home() / ".local" / "state" / "audio-remote-bridge-v1" / "acceptance.json"
)
ACCEPTANCE_STATE_MAX_BYTES = 32_768
ACCEPTANCE_MAX_TTL_SECONDS = 7 * 24 * 60 * 60
ACCEPTANCE_KEYS = (
    "bridge_service_verified",
    "tailscale_serve_verified",
    "ipad_https_reachability_verified",
    "ipad_safari_verified",
    "pwa_installation_verified",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BRIDGE_RUNTIME_SHA256 = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()

STATIC_ROUTES = frozenset(
    {
        "/",
        "/index.html",
        "/app.js",
        "/whale-lesson.js",
        "/sw.js",
        "/styles.css",
        "/manifest.webmanifest",
        "/icon-180.png",
        "/icon-192.png",
        "/icon-512.png",
        "/whale-learning-reference.wav",
        "/whale-learning-morph.wav",
        "/whale-learning-envelope.wav",
        "/whale-learning-periodicity.wav",
        "/whale-learning-articulation.wav",
    }
)
AUDIO_STATIC_ROUTES = frozenset(
    route for route in STATIC_ROUTES if route.endswith(".wav")
)
FIXED_API_ROUTES = frozenset(
    {
        "/api/v1/health",
        "/api/v1/telemetry",
        "/api/v1/replay",
        "/api/v1/whale/lesson",
        "/api/v1/recordings",
    }
)
PROFILE_PLAN_RE = re.compile(r"^/api/v1/profiles/([^/]+)/plan$")
RECORDING_MEDIA_RE = re.compile(r"^/api/v1/recordings/([0-9a-f]{24})/(audio|midi)$")
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FORBIDDEN_ENCODED_PATH_RE = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
SENSITIVE_KEY_TERMS = (
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "credential",
    "apikey",
    "privatekey",
)
FORWARDED_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "cache-control",
        "etag",
        "accept-ranges",
        "content-range",
        "content-security-policy",
        "referrer-policy",
        "x-content-type-options",
        "x-frame-options",
        "cross-origin-opener-policy",
        "cross-origin-resource-policy",
        "permissions-policy",
    }
)


def runtime_acceptance_defaults() -> dict[str, bool]:
    return {name: False for name in ACCEPTANCE_KEYS}


def _acceptance_evidence(state: str, **extra: Any) -> dict[str, Any]:
    return {"state": state, **extra}


def load_runtime_acceptance(
    path: pathlib.Path | None = None,
    *,
    now_unix: int | None = None,
) -> tuple[dict[str, bool], dict[str, Any]]:
    target = path if path is not None else ACCEPTANCE_STATE_PATH
    defaults = runtime_acceptance_defaults()
    now = int(time.time()) if now_unix is None else now_unix
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return defaults, _acceptance_evidence("unverified")
    except OSError:
        return defaults, _acceptance_evidence("invalid")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return defaults, _acceptance_evidence("invalid")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        return defaults, _acceptance_evidence("invalid")
    if metadata.st_size <= 0 or metadata.st_size > ACCEPTANCE_STATE_MAX_BYTES:
        return defaults, _acceptance_evidence("invalid")
    try:
        raw = target.read_bytes()
    except OSError:
        return defaults, _acceptance_evidence("invalid")
    state_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    required = {
        "schema_version",
        "kind",
        "contract_id",
        "bridge_sha256",
        "recorded_at_unix",
        "expires_at_unix",
        "runtime_acceptance",
        "evidence",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    if payload.get("schema_version") != 1 or payload.get("kind") != "audio_remote_bridge_runtime_acceptance":
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    if payload.get("contract_id") != CONTRACT_ID:
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    bridge_sha256 = payload.get("bridge_sha256")
    if (
        not isinstance(bridge_sha256, str)
        or not SHA256_RE.fullmatch(bridge_sha256)
        or bridge_sha256 != BRIDGE_RUNTIME_SHA256
    ):
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    recorded = payload.get("recorded_at_unix")
    expires = payload.get("expires_at_unix")
    if (
        not isinstance(recorded, int)
        or isinstance(recorded, bool)
        or not isinstance(expires, int)
        or isinstance(expires, bool)
        or recorded > now + 60
        or expires <= recorded
        or expires - recorded > ACCEPTANCE_MAX_TTL_SECONDS
    ):
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    if expires <= now:
        return defaults, _acceptance_evidence(
            "expired",
            recorded_at_unix=recorded,
            expires_at_unix=expires,
            state_sha256=state_sha256,
        )
    values = payload.get("runtime_acceptance")
    if not isinstance(values, dict) or set(values) != set(ACCEPTANCE_KEYS):
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    if any(not isinstance(values[name], bool) for name in ACCEPTANCE_KEYS):
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    if values["tailscale_serve_verified"] and not values["bridge_service_verified"]:
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    if values["ipad_https_reachability_verified"] and not (
        values["bridge_service_verified"] and values["tailscale_serve_verified"]
    ):
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    if values["ipad_safari_verified"] and not values["ipad_https_reachability_verified"]:
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    if values["pwa_installation_verified"] and not values["ipad_safari_verified"]:
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    source = evidence.get("source")
    evidence_sha256 = evidence.get("evidence_sha256")
    if (
        not isinstance(source, str)
        or not source.strip()
        or len(source) > 128
        or not isinstance(evidence_sha256, str)
        or not SHA256_RE.fullmatch(evidence_sha256)
    ):
        return defaults, _acceptance_evidence("invalid", state_sha256=state_sha256)
    return (
        {name: values[name] for name in ACCEPTANCE_KEYS},
        _acceptance_evidence(
            "verified",
            source=source,
            evidence_sha256=evidence_sha256,
            bridge_sha256=bridge_sha256,
            state_sha256=state_sha256,
            recorded_at_unix=recorded,
            expires_at_unix=expires,
        ),
    )


class BridgeError(RuntimeError):
    """Controlled fail-closed bridge failure."""


class RouteDenied(BridgeError):
    """Client target is outside the explicit projection contract."""


class RequestRejected(BridgeError):
    """Client request violates the bounded request contract."""


class ActionDenied(BridgeError):
    """Remote effect request lacks the exact scoped authorization."""


class ActionBusy(BridgeError):
    """Another scoped remote effect is already being processed."""


class BackendFailure(BridgeError):
    """Local backend response cannot be forwarded safely."""


class BoundedHeaderReader:
    """Wrap the request stream with an aggregate header-byte limit."""

    def __init__(self, stream: Any, limit: int) -> None:
        self.stream = stream
        self.remaining = limit

    def readline(self, size: int = -1) -> bytes:
        allowed = self.remaining + 1
        if size >= 0:
            allowed = min(allowed, size)
        line = self.stream.readline(allowed)
        self.remaining -= len(line)
        if self.remaining < 0:
            raise http.client.LineTooLong("HTTP header section")
        return line

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


def is_loopback_host(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def normalize_sensitive_key(key: object) -> str:
    if not isinstance(key, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def is_sensitive_json_key(key: object) -> bool:
    normalized = normalize_sensitive_key(key)
    return bool(normalized) and any(term in normalized for term in SENSITIVE_KEY_TERMS)


def scrub_json(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        removed = 0
        for key, child in value.items():
            if is_sensitive_json_key(key):
                removed += 1
                continue
            scrubbed, nested = scrub_json(child)
            result[str(key)] = scrubbed
            removed += nested
        return result, removed
    if isinstance(value, list):
        result_list: list[Any] = []
        removed = 0
        for child in value:
            scrubbed, nested = scrub_json(child)
            result_list.append(scrubbed)
            removed += nested
        return result_list, removed
    return value, 0


def contains_sensitive_json_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            is_sensitive_json_key(key) or contains_sensitive_json_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(contains_sensitive_json_key(child) for child in value)
    return False


def encode_scrubbed_json(payload: bytes) -> tuple[bytes, int]:
    if len(payload) > MAX_RESPONSE_BYTES:
        raise BackendFailure("backend response exceeds bridge limit")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackendFailure("backend JSON is invalid") from error
    scrubbed, removed = scrub_json(decoded)
    if contains_sensitive_json_key(scrubbed):
        raise BackendFailure("sensitive JSON key survived scrubbing")
    encoded = (
        json.dumps(scrubbed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise BackendFailure("scrubbed response exceeds bridge limit")
    return encoded, removed


def validate_request_target(raw_target: str) -> tuple[str, bool]:
    if not raw_target or len(raw_target.encode("utf-8", errors="ignore")) > MAX_REQUEST_LINE_BYTES:
        raise RequestRejected("invalid request target")
    if "\\" in raw_target or FORBIDDEN_ENCODED_PATH_RE.search(raw_target):
        raise RouteDenied("encoded path separator is forbidden")
    try:
        parsed = urllib.parse.urlsplit(raw_target)
    except ValueError as error:
        raise RequestRejected("invalid request target") from error
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
        raise RouteDenied("only origin-form request targets are allowed")
    path = parsed.path
    query = parsed.query
    if path in STATIC_ROUTES:
        if query:
            raise RouteDenied("static routes accept no query")
        return path, False
    if path in FIXED_API_ROUTES:
        if query:
            raise RouteDenied("fixed API routes accept no query")
        return path, True
    if path == "/api/v1/snapshot":
        if query not in {"", "refresh=1"}:
            raise RouteDenied("snapshot query is not allowed")
        return path + ("?refresh=1" if query else ""), True
    recording_media = RECORDING_MEDIA_RE.fullmatch(path)
    if recording_media:
        if query:
            raise RouteDenied("recording media accepts no query")
        return (
            f"/api/v1/recordings/{recording_media.group(1)}/{recording_media.group(2)}",
            True,
        )
    match = PROFILE_PLAN_RE.fullmatch(path)
    if match:
        if query:
            raise RouteDenied("profile plan accepts no query")
        encoded_id = match.group(1)
        if "%" in encoded_id and re.search(r"%(?![0-9A-Fa-f]{2})", encoded_id):
            raise RouteDenied("profile id has invalid percent encoding")
        try:
            profile_id = urllib.parse.unquote(encoded_id, errors="strict")
        except UnicodeDecodeError as error:
            raise RouteDenied("profile id is invalid") from error
        if not PROFILE_ID_RE.fullmatch(profile_id):
            raise RouteDenied("profile id is invalid")
        canonical_id = urllib.parse.quote(profile_id, safe="-._~")
        return f"/api/v1/profiles/{canonical_id}/plan", True
    raise RouteDenied("route is not allowlisted")



def validate_remote_tailnet_host(headers: Any) -> str:
    values = headers.get_all("Host", []) if headers is not None else []
    if len(values) != 1:
        raise ActionDenied("remote action requires one Host header")
    host = values[0].strip().lower()
    if (
        not host
        or len(host) > 255
        or any(character in host for character in "\r\n\0")
        or host != REMOTE_TAILNET_HOST
    ):
        raise ActionDenied("remote action host is outside the private Tailnet HTTPS contract")
    return host


def validate_remote_tailnet_origin(headers: Any) -> str:
    host = validate_remote_tailnet_host(headers)
    values = headers.get_all("Origin", []) if headers is not None else []
    if len(values) != 1 or values[0] != f"https://{host}":
        raise ActionDenied("remote action Origin does not match the Tailnet HTTPS host")
    return host



def validated_tailscale_identity(headers: Any) -> str:
    values = headers.get_all("Tailscale-User-Login", []) if headers is not None else []
    if len(values) != 1:
        raise ActionDenied("remote action requires one verified Tailscale identity")
    value = values[0].strip()
    if (
        not value
        or len(value.encode("utf-8", errors="replace")) > 512
        or any(character in value for character in "\r\n\0")
    ):
        raise ActionDenied("remote Tailscale identity is invalid")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def validate_whale_action_payload(payload: bytes) -> dict[str, str]:
    if not payload or len(payload) > MAX_ACTION_BODY_BYTES:
        raise RequestRejected("remote whale action body is outside the size contract")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestRejected("remote whale action body is invalid JSON") from error
    if not isinstance(decoded, dict):
        raise RequestRejected("remote whale action body must be an object")
    operation = decoded.get("operation")
    if operation not in WHALE_ACTION_OPERATIONS:
        raise RequestRejected("remote whale action operation is not allowlisted")
    if operation == "stop":
        if set(decoded) != {"operation"}:
            raise RequestRejected("remote stop accepts no additional fields")
        return {"operation": "stop"}
    if set(decoded) != {"operation", "mode"}:
        raise RequestRejected("remote start/mode requires exactly operation and mode")
    mode = decoded.get("mode")
    if mode not in WHALE_ACTION_MODES:
        raise RequestRejected("remote whale mode is not allowlisted")
    return {"operation": str(operation), "mode": str(mode)}


def _validated_recording_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value in {".", ".."}
        or pathlib.PurePath(value).name != value
        or "/" in value
        or "\\" in value
        or not value.lower().endswith(".wav")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise RequestRejected("remote recording name is not a safe WAV filename")
    return value


def _validated_recording_duration(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 14_400:
        raise RequestRejected("remote recording duration is outside the size contract")
    return value


def validate_recording_action_payload(payload: bytes) -> dict[str, Any]:
    if not payload or len(payload) > MAX_RECORDING_ACTION_BODY_BYTES:
        raise RequestRejected("remote recording action body is outside the size contract")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestRejected("remote recording action body is invalid JSON") from error
    if not isinstance(decoded, dict):
        raise RequestRejected("remote recording action body must be an object")
    operation = decoded.get("operation")
    if operation not in RECORDING_ACTION_OPERATIONS:
        raise RequestRejected("remote recording operation is not allowlisted")
    if operation in {"plan", "prepare"}:
        required = {"operation", "mode", "name", "maximum_seconds"}
    elif operation == "start":
        required = {"operation", "mode", "name", "maximum_seconds", "expected_plan_sha256"}
    elif operation == "categorize":
        required = {"operation", "session_id", "category"}
    else:
        required = {"operation", "session_id"}
    if set(decoded) != required:
        raise RequestRejected("remote recording action fields do not match the operation contract")
    if operation in {"plan", "prepare", "start"}:
        mode = decoded.get("mode")
        if mode not in RECORDING_ACTION_MODES:
            raise RequestRejected("remote recording mode is not allowlisted")
        result: dict[str, Any] = {
            "operation": str(operation),
            "mode": str(mode),
            "name": _validated_recording_name(decoded.get("name")),
            "maximum_seconds": _validated_recording_duration(decoded.get("maximum_seconds")),
        }
        if operation == "start":
            plan_sha256 = decoded.get("expected_plan_sha256")
            if not isinstance(plan_sha256, str) or SHA256_RE.fullmatch(plan_sha256) is None:
                raise RequestRejected("remote recording start plan SHA-256 is invalid")
            result["expected_plan_sha256"] = plan_sha256
        return result
    session_id = decoded.get("session_id")
    if not isinstance(session_id, str) or RECORDING_SESSION_ID_RE.fullmatch(session_id) is None:
        raise RequestRejected("remote recording session id is invalid")
    result = {"operation": str(operation), "session_id": session_id}
    if operation == "categorize":
        category = decoded.get("category")
        if category not in RECORDING_LIBRARY_CATEGORIES:
            raise RequestRejected("remote recording category is not allowlisted")
        result["category"] = str(category)
    return result


def backend_request_headers(
    headers: Any,
    *,
    allow_range: bool = False,
) -> dict[str, str]:
    forwarded: dict[str, str] = {
        "Host": f"{BACKEND_HOST}:{BACKEND_PORT}",
        "Connection": "close",
    }
    values = headers.get_all("If-None-Match", []) if headers is not None else []
    if len(values) > 1:
        raise RequestRejected("duplicate conditional header is forbidden")
    if values:
        value = values[0]
        if len(value.encode("latin-1", errors="replace")) > MAX_CONDITIONAL_HEADER_BYTES:
            raise RequestRejected("conditional header is too large")
        if any(character in value for character in "\r\n\0"):
            raise RequestRejected("conditional header contains control characters")
        forwarded["If-None-Match"] = value

    range_values = (
        headers.get_all("Range", [])
        if headers is not None and allow_range
        else []
    )
    if len(range_values) > 1:
        raise RequestRejected("duplicate range header is forbidden")
    if range_values:
        range_value = range_values[0]
        if len(range_value.encode("latin-1", errors="replace")) > MAX_RANGE_HEADER_BYTES:
            raise RequestRejected("range header is too large")
        if any(character in range_value for character in "\r\n\0"):
            raise RequestRejected("range header contains control characters")
        forwarded["Range"] = range_value
    return forwarded


def read_backend_response(target: str, incoming_headers: Any) -> tuple[int, list[tuple[str, str]], bytes, int]:
    connection = http.client.HTTPConnection(
        BACKEND_HOST,
        BACKEND_PORT,
        timeout=BACKEND_TIMEOUT_SECONDS,
    )
    try:
        connection.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
        for name, value in backend_request_headers(
            incoming_headers,
            allow_range=target in AUDIO_STATIC_ROUTES,
        ).items():
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        headers = response.getheaders()
        header_bytes = sum(
            len(name.encode("latin-1", errors="replace"))
            + len(value.encode("latin-1", errors="replace"))
            + 4
            for name, value in headers
        )
        if header_bytes > MAX_BACKEND_HEADER_BYTES:
            raise BackendFailure("backend headers exceed bridge limit")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise BackendFailure("backend response exceeds bridge limit")
        content_type = next(
            (value for name, value in headers if name.lower() == "content-type"), ""
        )
        redactions = 0
        if (
            content_type.lower().split(";", 1)[0].strip() == "application/json"
            and response.status != HTTPStatus.NOT_MODIFIED
        ):
            payload, redactions = encode_scrubbed_json(payload)
        filtered = [
            (name, value)
            for name, value in headers
            if name.lower() in FORWARDED_RESPONSE_HEADERS
        ]
        return response.status, filtered, payload, redactions
    except RequestRejected:
        raise
    except BackendFailure:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise BackendFailure("backend is unavailable") from error
    finally:
        connection.close()


def stream_backend_recording_artifact(
    handler: "AudioRemoteBridgeHandler",
    target: str,
    incoming_headers: Any,
    *,
    head_only: bool,
) -> None:
    media = RECORDING_MEDIA_RE.fullmatch(target)
    if media is None:
        raise RequestRejected("recording media target is invalid")
    expected_content_type = "audio/wav" if media.group(2) == "audio" else "audio/midi"
    connection = http.client.HTTPConnection(
        BACKEND_HOST, BACKEND_PORT, timeout=BACKEND_TIMEOUT_SECONDS
    )
    response_started = False
    try:
        connection.putrequest(
            "HEAD" if head_only else "GET",
            target,
            skip_host=True,
            skip_accept_encoding=True,
        )
        for name, value in backend_request_headers(
            incoming_headers, allow_range=True
        ).items():
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        headers = response.getheaders()
        header_bytes = sum(
            len(name.encode("latin-1", errors="replace"))
            + len(value.encode("latin-1", errors="replace"))
            + 4
            for name, value in headers
        )
        if header_bytes > MAX_BACKEND_HEADER_BYTES:
            raise BackendFailure("backend headers exceed bridge limit")
        content_type = next(
            (value for name, value in headers if name.lower() == "content-type"), ""
        ).lower().split(";", 1)[0].strip()
        filtered = [
            (name, value)
            for name, value in headers
            if name.lower() in FORWARDED_RESPONSE_HEADERS
        ]
        if response.status in {
            HTTPStatus.OK,
            HTTPStatus.PARTIAL_CONTENT,
            HTTPStatus.NOT_MODIFIED,
            HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
        }:
            if content_type != expected_content_type:
                raise BackendFailure("backend recording media response has an invalid type")
            length_values = [
                value for name, value in headers if name.lower() == "content-length"
            ]
            if len(length_values) != 1 or not length_values[0].isdigit():
                raise BackendFailure("backend recording media length is invalid")
            length = int(length_values[0], 10)
            if not 0 <= length <= MAX_RECORDING_AUDIO_STREAM_BYTES:
                raise BackendFailure("backend recording media exceeds stream contract")
            response_started = True
            handler._send_headers(
                response.status,
                content_length=length,
                backend_headers=filtered,
            )
            if head_only or length == 0:
                return
            remaining = length
            while remaining:
                chunk = response.read(min(64 * 1024, remaining))
                if not chunk:
                    raise BackendFailure("backend recording media ended early")
                handler.wfile.write(chunk)
                remaining -= len(chunk)
            return
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise BackendFailure("backend recording error response exceeds bridge limit")
        redactions = 0
        if content_type == "application/json":
            payload, redactions = encode_scrubbed_json(payload)
        elif payload:
            raise BackendFailure("backend recording error response has unsafe content type")
        response_started = True
        handler._send_headers(
            response.status,
            content_length=len(payload),
            backend_headers=filtered,
            redactions=redactions,
        )
        if not head_only:
            handler.wfile.write(payload)
    except (RequestRejected, BackendFailure):
        if response_started:
            handler.close_connection = True
            return
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        if response_started:
            handler.close_connection = True
            return
        raise BackendFailure("backend recording media is unavailable") from error
    finally:
        connection.close()


def _bounded_backend_payload(response: http.client.HTTPResponse) -> tuple[list[tuple[str, str]], bytes]:
    headers = response.getheaders()
    header_bytes = sum(
        len(name.encode("latin-1", errors="replace"))
        + len(value.encode("latin-1", errors="replace"))
        + 4
        for name, value in headers
    )
    if header_bytes > MAX_BACKEND_HEADER_BYTES:
        raise BackendFailure("backend headers exceed bridge limit")
    payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise BackendFailure("backend response exceeds bridge limit")
    return headers, payload


def read_backend_action_token(effect: str) -> str:
    connection = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=BACKEND_TIMEOUT_SECONDS)
    try:
        connection.putrequest("GET", "/api/v1/snapshot?refresh=1", skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", f"{BACKEND_HOST}:{BACKEND_PORT}")
        connection.putheader("Connection", "close")
        connection.endheaders()
        response = connection.getresponse()
        _headers, payload = _bounded_backend_payload(response)
        if response.status != HTTPStatus.OK:
            raise BackendFailure("backend snapshot is unavailable for remote action")
        try:
            snapshot = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BackendFailure("backend snapshot for remote action is invalid") from error
        if not isinstance(snapshot, dict) or snapshot.get("kind") != "audio_control_snapshot":
            raise BackendFailure("backend snapshot identity is invalid")
        capabilities = snapshot.get("capabilities")
        service = snapshot.get("service")
        if not isinstance(capabilities, dict):
            raise BackendFailure("backend capabilities are unavailable")
        if effect == "whale":
            whale = snapshot.get("whale")
            if capabilities.get("whale_control") is not True:
                raise BackendFailure("backend whale control is not actionable")
            if not isinstance(whale, dict) or whale.get("status") != "ok":
                raise BackendFailure("backend whale status is not actionable")
        elif effect == "recording":
            recording = snapshot.get("recording")
            if capabilities.get("recording_control") is not True:
                raise BackendFailure("backend recording control is not actionable")
            if not isinstance(recording, dict) or recording.get("actionable") is not True:
                raise BackendFailure("backend recorder is not actionable")
        else:
            raise BackendFailure("backend effect is not allowlisted")
        token = service.get("action_token") if isinstance(service, dict) else None
        if not isinstance(token, str) or not 16 <= len(token) <= 512:
            raise BackendFailure("backend action token is unavailable")
        return token
    except BackendFailure:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise BackendFailure("backend is unavailable") from error
    finally:
        connection.close()


def write_backend_whale_action(action: dict[str, str]) -> tuple[int, bytes, int]:
    token = read_backend_action_token("whale")
    body = json.dumps(action, sort_keys=True, separators=(",", ":")).encode("utf-8")
    connection = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=70.0)
    try:
        connection.putrequest("POST", "/api/v1/actions/whale", skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", f"{BACKEND_HOST}:{BACKEND_PORT}")
        connection.putheader("Connection", "close")
        connection.putheader("Origin", f"http://{BACKEND_HOST}:{BACKEND_PORT}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        connection.putheader("X-Audio-Control-Token", token)
        connection.endheaders(body)
        response = connection.getresponse()
        headers, payload = _bounded_backend_payload(response)
        content_type = next((value for name, value in headers if name.lower() == "content-type"), "")
        if content_type.lower().split(";", 1)[0].strip() != "application/json":
            raise BackendFailure("backend whale action response is not JSON")
        scrubbed, redactions = encode_scrubbed_json(payload)
        if response.status == HTTPStatus.OK:
            decoded = json.loads(scrubbed.decode("utf-8"))
            if (
                not isinstance(decoded, dict)
                or decoded.get("kind") != "audio_control_action_result"
                or not isinstance(decoded.get("snapshot"), dict)
                or decoded["snapshot"].get("kind") != "audio_control_snapshot"
            ):
                raise BackendFailure("backend whale action lacks authoritative readback")
        return response.status, scrubbed, redactions
    except BackendFailure:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise BackendFailure("backend whale action is unavailable") from error
    finally:
        connection.close()


def write_backend_recording_action(action: dict[str, Any]) -> tuple[int, bytes, int]:
    token = read_backend_action_token("recording")
    body = json.dumps(action, sort_keys=True, separators=(",", ":")).encode("utf-8")
    backend_timeout = recording_backend_timeout_seconds(str(action.get("operation", "")))
    connection = http.client.HTTPConnection(
        BACKEND_HOST, BACKEND_PORT, timeout=backend_timeout
    )
    try:
        connection.putrequest("POST", "/api/v1/actions/recording", skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", f"{BACKEND_HOST}:{BACKEND_PORT}")
        connection.putheader("Connection", "close")
        connection.putheader("Origin", f"http://{BACKEND_HOST}:{BACKEND_PORT}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        connection.putheader("X-Audio-Control-Token", token)
        connection.endheaders(body)
        response = connection.getresponse()
        headers, payload = _bounded_backend_payload(response)
        content_type = next((value for name, value in headers if name.lower() == "content-type"), "")
        if content_type.lower().split(";", 1)[0].strip() != "application/json":
            raise BackendFailure("backend recording action response is not JSON")
        scrubbed, redactions = encode_scrubbed_json(payload)
        if response.status == HTTPStatus.OK:
            decoded = json.loads(scrubbed.decode("utf-8"))
            if (
                not isinstance(decoded, dict)
                or decoded.get("kind") != "audio_control_recording_action_result"
                or decoded.get("operation") != action["operation"]
            ):
                raise BackendFailure("backend recording action lacks bound result identity")
            if action["operation"] in {"plan", "prepare"}:
                plan = decoded.get("plan")
                if (
                    not isinstance(plan, dict)
                    or not isinstance(plan.get("ready"), bool)
                    or not isinstance(plan.get("plan_sha256"), str)
                    or SHA256_RE.fullmatch(plan["plan_sha256"]) is None
                    or plan.get("mode") != action["mode"]
                ):
                    raise BackendFailure("backend recording plan lacks bound plan readback")
            else:
                snapshot = decoded.get("snapshot")
                if not isinstance(snapshot, dict) or snapshot.get("kind") != "audio_control_snapshot":
                    raise BackendFailure("backend recording action lacks authoritative readback")
                if action["operation"] in {"categorize", "trash", "restore"}:
                    library = decoded.get("library")
                    if (
                        not isinstance(library, dict)
                        or library.get("kind") != "audio_recording_library_metadata"
                        or library.get("session_id") != action["session_id"]
                        or library.get("category") not in RECORDING_LIBRARY_CATEGORIES
                        or not isinstance(library.get("trashed"), bool)
                    ):
                        raise BackendFailure("backend recording library action lacks bound metadata")
        return response.status, scrubbed, redactions
    except BackendFailure:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise BackendFailure("backend recording action is unavailable") from error
    finally:
        connection.close()


class AudioRemoteBridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = MAX_CONCURRENT_REQUESTS

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        test_mode: bool = False,
    ) -> None:
        host, port = server_address
        if host != DEFAULT_HOST or not is_loopback_host(host):
            raise BridgeError("bridge bind must stay on canonical loopback")
        if not test_mode and port != DEFAULT_PORT:
            raise BridgeError("bridge port differs from fixed contract")
        if test_mode and port != 0 and not 1024 <= port <= 65535:
            raise BridgeError("test bridge port is invalid")
        self._request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self._action_lock = threading.Lock()
        self._action_session_lock = threading.Lock()
        self._action_sessions: dict[str, tuple[int, str]] = {}
        super().__init__(server_address, AudioRemoteBridgeHandler)

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(REQUEST_IO_TIMEOUT_SECONDS)
        return request, client_address

    def verify_request(self, request: Any, client_address: Any) -> bool:
        del request
        return bool(client_address) and is_loopback_host(str(client_address[0]))

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 0\r\n"
                    b"Cache-Control: no-store\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def issue_action_session(self, identity_sha256: str) -> tuple[str, int]:
        now = int(time.time())
        expires = now + REMOTE_ACTION_SESSION_TTL_SECONDS
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._action_session_lock:
            self._action_sessions = {
                key: value for key, value in self._action_sessions.items() if value[0] > now
            }
            if len(self._action_sessions) >= REMOTE_ACTION_SESSION_CAPACITY:
                oldest = min(self._action_sessions, key=lambda key: self._action_sessions[key][0])
                del self._action_sessions[oldest]
            self._action_sessions[digest] = (expires, identity_sha256)
        return token, expires

    def action_session_valid(self, token: str, identity_sha256: str) -> bool:
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            return False
        now = int(time.time())
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._action_session_lock:
            expired = [key for key, value in self._action_sessions.items() if value[0] <= now]
            for key in expired:
                del self._action_sessions[key]
            for digest, (expires, bound_identity) in self._action_sessions.items():
                if (
                    expires > now
                    and hmac.compare_digest(digest, candidate)
                    and hmac.compare_digest(bound_identity, identity_sha256)
                ):
                    return True
        return False

    def execute_whale_action(self, action: dict[str, str]) -> tuple[int, bytes, int]:
        if not self._action_lock.acquire(blocking=False):
            raise ActionBusy("another remote whale action is already in progress")
        try:
            return write_backend_whale_action(action)
        finally:
            self._action_lock.release()

    def execute_recording_action(self, action: dict[str, Any]) -> tuple[int, bytes, int]:
        if not self._action_lock.acquire(blocking=False):
            raise ActionBusy("another remote audio action is already in progress")
        try:
            return write_backend_recording_action(action)
        finally:
            self._action_lock.release()


class AudioRemoteBridgeHandler(BaseHTTPRequestHandler):
    server: AudioRemoteBridgeHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "AudioRemoteBridge/1"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(MAX_REQUEST_LINE_BYTES + 1)
            if len(self.raw_requestline) > MAX_REQUEST_LINE_BYTES:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            method = getattr(self, "do_" + self.command, None)
            if method is None:
                self._method_not_allowed()
                return
            method()
            self.wfile.flush()
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def parse_request(self) -> bool:
        stream = self.rfile
        self.rfile = BoundedHeaderReader(stream, MAX_HEADER_BYTES)
        try:
            return super().parse_request()
        finally:
            self.rfile = stream

    def handle_expect_100(self) -> bool:
        self.close_connection = True
        self._send_error(HTTPStatus.EXPECTATION_FAILED, "Expect is not supported")
        return False

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        del explain
        self.close_connection = True
        try:
            status = HTTPStatus(code)
        except ValueError:
            status = HTTPStatus.BAD_REQUEST
        self._send_error(status, message or status.phrase)

    def log_message(self, _format_string: str, *_args: object) -> None:
        return

    def _send_headers(
        self,
        status: int,
        *,
        content_length: int,
        backend_headers: list[tuple[str, str]] | None = None,
        content_type: str = "application/json; charset=utf-8",
        redactions: int = 0,
        bridge_marker: str = BRIDGE_HEADER,
        remote_effect: str | None = None,
    ) -> None:
        self.send_response(status)
        supplied = {name.lower() for name, _value in backend_headers or []}
        for name, value in backend_headers or []:
            self.send_header(name, value)
        if "content-type" not in supplied:
            self.send_header("Content-Type", content_type)
        if "cache-control" not in supplied:
            self.send_header("Cache-Control", "no-store")
        if "x-content-type-options" not in supplied:
            self.send_header("X-Content-Type-Options", "nosniff")
        if "referrer-policy" not in supplied:
            self.send_header("Referrer-Policy", "no-referrer")
        if "permissions-policy" not in supplied:
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), display-capture=()",
            )
        self.send_header("Content-Length", str(content_length))
        self.send_header("X-Audio-Remote-Bridge", bridge_marker)
        if remote_effect:
            self.send_header(REMOTE_EFFECTS_HEADER, remote_effect)
        if redactions:
            self.send_header("X-Audio-Remote-Redactions", str(redactions))
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_error(self, status: HTTPStatus, message: str, *, head_only: bool = False) -> None:
        payload = (
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_remote_bridge_error",
                    "error": {"code": status.name.lower(), "message": message},
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self._send_headers(status, content_length=len(payload))
        if not head_only:
            self.wfile.write(payload)

    def _method_not_allowed(self) -> None:
        self.close_connection = True
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Audio-Remote-Bridge", BRIDGE_HEADER)
        self.send_header("Connection", "close")
        self.end_headers()

    def _bridge_health(self, *, head_only: bool) -> None:
        runtime_acceptance, runtime_acceptance_evidence = load_runtime_acceptance()
        payload = (
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_remote_bridge_health",
                    "contract_id": CONTRACT_ID,
                    "status": "serving",
                    "projection": "read-only-plus-scoped-actions",
                    "effect_authority": True,
                    "effect_scope": [
                        "whale:start",
                        "whale:mode",
                        "whale:stop",
                        "recording:plan",
                        "recording:start",
                        "recording:stop",
                        "recording:recover",
                        "recording:categorize",
                        "recording:trash",
                        "recording:restore",
                    ],
                    "effect_exclusions": ["profiles", "routing", "devices", "system"],
                    "allowed_methods": ["GET", "HEAD", "POST"],
                    "remote_action": {
                        "session_route": REMOTE_SESSION_ROUTE,
                        "action_route": REMOTE_WHALE_ACTION_ROUTE,
                        "recording_action_route": REMOTE_RECORDING_ACTION_ROUTE,
                        "session_ttl_seconds": REMOTE_ACTION_SESSION_TTL_SECONDS,
                        "token_header": REMOTE_ACTION_TOKEN_HEADER,
                        "backend_token_exposed": False,
                        "tailscale_identity_required": True,
                        "session_identity_bound": True,
                    },
                    "json_sensitive_key_redaction": True,
                    "backend": {
                        "authority": "local-loopback-only",
                        "remote_exposure": False,
                    },
                    "runtime_sha256": BRIDGE_RUNTIME_SHA256,
                    "runtime_acceptance": runtime_acceptance,
                    "runtime_acceptance_evidence": runtime_acceptance_evidence,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self._send_headers(HTTPStatus.OK, content_length=len(payload))
        if not head_only:
            self.wfile.write(payload)

    def _remote_session(self) -> None:
        try:
            validate_remote_tailnet_origin(self.headers)
            identity_sha256 = validated_tailscale_identity(self.headers)
        except ActionDenied as error:
            self._send_error(HTTPStatus.FORBIDDEN, str(error))
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self._send_error(HTTPStatus.BAD_REQUEST, "chunked remote session bodies are forbidden")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote session requires one Content-Length")
            return
        try:
            length = int(lengths[0], 10)
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote session Content-Length is invalid")
            return
        if not 1 <= length <= 16:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote session body is outside the size contract")
            return
        content_types = self.headers.get_all("Content-Type", [])
        if (
            len(content_types) != 1
            or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
        ):
            self._send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "remote session requires application/json")
            return
        request_payload = self.rfile.read(length)
        if len(request_payload) != length:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote session body is incomplete")
            return
        try:
            session_request = json.loads(request_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error(HTTPStatus.BAD_REQUEST, "remote session body is invalid JSON")
            return
        if session_request != {}:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote session body must be an empty object")
            return
        token, expires = self.server.issue_action_session(identity_sha256)
        payload = (
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_remote_bridge_session",
                    "effect_scope": ["whale", "recording"],
                    "allowed_operations": {
                        "whale": sorted(WHALE_ACTION_OPERATIONS),
                        "recording": sorted(RECORDING_ACTION_OPERATIONS),
                    },
                    "session_token": token,
                    "expires_at_unix": expires,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self._send_headers(
            HTTPStatus.OK,
            content_length=len(payload),
            remote_effect=REMOTE_WHALE_EFFECTS_VALUE,
        )
        self.wfile.write(payload)

    def _serve_whale_action(self) -> None:
        try:
            validate_remote_tailnet_origin(self.headers)
            identity_sha256 = validated_tailscale_identity(self.headers)
        except ActionDenied as error:
            self._send_error(HTTPStatus.FORBIDDEN, str(error))
            return
        token_values = self.headers.get_all(REMOTE_ACTION_TOKEN_HEADER, [])
        if (
            len(token_values) != 1
            or not self.server.action_session_valid(token_values[0], identity_sha256)
        ):
            self._send_error(HTTPStatus.FORBIDDEN, "remote whale action session is invalid or expired")
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self._send_error(HTTPStatus.BAD_REQUEST, "chunked remote action bodies are forbidden")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote whale action requires one Content-Length")
            return
        try:
            length = int(lengths[0], 10)
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote whale action Content-Length is invalid")
            return
        if not 1 <= length <= MAX_ACTION_BODY_BYTES:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote whale action body is outside the size contract")
            return
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
            self._send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "remote whale action requires application/json")
            return
        payload = self.rfile.read(length)
        if len(payload) != length:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote whale action body is incomplete")
            return
        try:
            action = validate_whale_action_payload(payload)
            status, response_payload, redactions = self.server.execute_whale_action(action)
        except RequestRejected as error:
            self._send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        except ActionBusy as error:
            self._send_error(HTTPStatus.CONFLICT, str(error))
            return
        except BackendFailure as error:
            self._send_error(HTTPStatus.BAD_GATEWAY, str(error))
            return
        self._send_headers(
            status,
            content_length=len(response_payload),
            redactions=redactions,
            bridge_marker=BRIDGE_WHALE_ACTION_HEADER,
            remote_effect=REMOTE_WHALE_EFFECTS_VALUE,
        )
        self.wfile.write(response_payload)

    def _serve_recording_action(self) -> None:
        try:
            validate_remote_tailnet_origin(self.headers)
            identity_sha256 = validated_tailscale_identity(self.headers)
        except ActionDenied as error:
            self._send_error(HTTPStatus.FORBIDDEN, str(error))
            return
        token_values = self.headers.get_all(REMOTE_ACTION_TOKEN_HEADER, [])
        if (
            len(token_values) != 1
            or not self.server.action_session_valid(token_values[0], identity_sha256)
        ):
            self._send_error(HTTPStatus.FORBIDDEN, "remote recording action session is invalid or expired")
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self._send_error(HTTPStatus.BAD_REQUEST, "chunked remote recording bodies are forbidden")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote recording action requires one Content-Length")
            return
        try:
            length = int(lengths[0], 10)
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote recording Content-Length is invalid")
            return
        if not 1 <= length <= MAX_RECORDING_ACTION_BODY_BYTES:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote recording body is outside the size contract")
            return
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
            self._send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "remote recording action requires application/json")
            return
        payload = self.rfile.read(length)
        if len(payload) != length:
            self._send_error(HTTPStatus.BAD_REQUEST, "remote recording action body is incomplete")
            return
        try:
            action = validate_recording_action_payload(payload)
            status, response_payload, redactions = self.server.execute_recording_action(action)
        except RequestRejected as error:
            self._send_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        except ActionBusy as error:
            self._send_error(HTTPStatus.CONFLICT, str(error))
            return
        except BackendFailure as error:
            self._send_error(HTTPStatus.BAD_GATEWAY, str(error))
            return
        self._send_headers(
            status,
            content_length=len(response_payload),
            redactions=redactions,
            bridge_marker=BRIDGE_RECORDING_ACTION_HEADER,
            remote_effect=REMOTE_RECORDING_EFFECTS_VALUE,
        )
        self.wfile.write(response_payload)

    def _serve(self, *, head_only: bool) -> None:
        if self.headers.get_all("Transfer-Encoding", []):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "request body is forbidden",
                head_only=head_only,
            )
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or (lengths and lengths[0] not in {"0", ""}):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "request body is forbidden",
                head_only=head_only,
            )
            return
        if self.path == "/bridge/v1/health":
            self._bridge_health(head_only=head_only)
            return
        try:
            target, _is_api = validate_request_target(self.path)
            if RECORDING_MEDIA_RE.fullmatch(target):
                stream_backend_recording_artifact(
                    self, target, self.headers, head_only=head_only
                )
                return
            status, headers, payload, redactions = read_backend_response(target, self.headers)
        except RouteDenied as error:
            self._send_error(HTTPStatus.NOT_FOUND, str(error), head_only=head_only)
            return
        except RequestRejected as error:
            self._send_error(HTTPStatus.BAD_REQUEST, str(error), head_only=head_only)
            return
        except BackendFailure as error:
            self._send_error(HTTPStatus.BAD_GATEWAY, str(error), head_only=head_only)
            return
        self._send_headers(
            status,
            content_length=len(payload),
            backend_headers=headers,
            redactions=redactions,
        )
        if not head_only:
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._serve(head_only=False)

    def do_HEAD(self) -> None:
        self._serve(head_only=True)

    def do_POST(self) -> None:
        if self.path == REMOTE_SESSION_ROUTE:
            self._remote_session()
            return
        if self.path == REMOTE_WHALE_ACTION_ROUTE:
            self._serve_whale_action()
            return
        if self.path == REMOTE_RECORDING_ACTION_ROUTE:
            self._serve_recording_action()
            return
        self._method_not_allowed()

    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_CONNECT = _method_not_allowed
    do_TRACE = _method_not_allowed


def validate_configuration(host: str, port: int, backend_host: str, backend_port: int) -> None:
    if (host, port) != (DEFAULT_HOST, DEFAULT_PORT):
        raise BridgeError("bridge address differs from fixed contract")
    if (backend_host, backend_port) != (BACKEND_HOST, BACKEND_PORT):
        raise BridgeError("backend address differs from fixed contract")
    if not is_loopback_host(host) or not is_loopback_host(backend_host):
        raise BridgeError("bridge and backend must stay on loopback")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audio-remote-bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--backend-host", default=BACKEND_HOST)
    serve.add_argument("--backend-port", type=int, default=BACKEND_PORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        validate_configuration(DEFAULT_HOST, DEFAULT_PORT, BACKEND_HOST, BACKEND_PORT)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_remote_bridge_check",
                    "contract_id": CONTRACT_ID,
                    "status": "ok",
                    "effect_authority": True,
                    "effect_scope": [
                        "whale:start",
                        "whale:mode",
                        "whale:stop",
                        "recording:plan",
                        "recording:start",
                        "recording:stop",
                        "recording:recover",
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    validate_configuration(args.host, args.port, args.backend_host, args.backend_port)
    server = AudioRemoteBridgeHTTPServer((args.host, args.port))
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
