#!/usr/bin/env python3
"""Fail-closed read-only projection bridge for the Audiozentrale.

The canonical Audio Control service remains loopback-only on 127.0.0.1:8765.
This separate loopback service exposes only an explicit read-only projection for
an independently authenticated HTTPS frontend. It is not an open proxy and it
never gains audio, device, profile-transition or other effect authority.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import re
import socket
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CONTRACT_ID = "audiozentrale-remote-bridge-v1"
BRIDGE_HEADER = "read-only-v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8765
BACKEND_TIMEOUT_SECONDS = 6.0
REQUEST_IO_TIMEOUT_SECONDS = 6.0
MAX_REQUEST_LINE_BYTES = 2048
MAX_HEADER_BYTES = 16_384
MAX_BACKEND_HEADER_BYTES = 32_768
MAX_RESPONSE_BYTES = 1_048_576
MAX_CONCURRENT_REQUESTS = 8
MAX_CONDITIONAL_HEADER_BYTES = 4096

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
FIXED_API_ROUTES = frozenset(
    {
        "/api/v1/health",
        "/api/v1/telemetry",
        "/api/v1/replay",
        "/api/v1/whale/lesson",
    }
)
PROFILE_PLAN_RE = re.compile(r"^/api/v1/profiles/([^/]+)/plan$")
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
        "content-security-policy",
        "referrer-policy",
        "x-content-type-options",
        "x-frame-options",
        "cross-origin-opener-policy",
        "cross-origin-resource-policy",
        "permissions-policy",
    }
)


class BridgeError(RuntimeError):
    """Controlled fail-closed bridge failure."""


class RouteDenied(BridgeError):
    """Client target is outside the explicit projection contract."""


class RequestRejected(BridgeError):
    """Client request violates the bounded request contract."""


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


def backend_request_headers(headers: Any) -> dict[str, str]:
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
    return forwarded


def read_backend_response(target: str, incoming_headers: Any) -> tuple[int, list[tuple[str, str]], bytes, int]:
    connection = http.client.HTTPConnection(
        BACKEND_HOST,
        BACKEND_PORT,
        timeout=BACKEND_TIMEOUT_SECONDS,
    )
    try:
        connection.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
        for name, value in backend_request_headers(incoming_headers).items():
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
        self.send_header("X-Audio-Remote-Bridge", BRIDGE_HEADER)
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
        payload = (
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_remote_bridge_health",
                    "contract_id": CONTRACT_ID,
                    "status": "serving",
                    "projection": "read-only",
                    "effect_authority": False,
                    "allowed_methods": ["GET", "HEAD"],
                    "json_sensitive_key_redaction": True,
                    "backend": {
                        "authority": "local-loopback-only",
                        "remote_exposure": False,
                    },
                    "runtime_acceptance": {
                        "bridge_service_verified": False,
                        "tailnet_https_verified": False,
                        "ipad_https_reachability_verified": False,
                        "pwa_installation_verified": False,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self._send_headers(HTTPStatus.OK, content_length=len(payload))
        if not head_only:
            self.wfile.write(payload)

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
        self._method_not_allowed()

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST
    do_CONNECT = do_POST
    do_TRACE = do_POST


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
                    "effect_authority": False,
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
