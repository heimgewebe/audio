#!/usr/bin/env python3
"""Canonical physical-iPad acceptance probe for the scoped Audiozentrale bridge."""

from __future__ import annotations

import ipaddress
import json
import platform
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any

HOST = "heim-pc.tail6dbb90.ts.net"
PORT = 9443
BASE_URL = f"https://{HOST}:{PORT}"
MAX_RESPONSE_BYTES = 200_000
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def is_tailnet_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address in TAILNET_V4 or address in TAILNET_V6


def fetch(
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        BASE_URL + path, data=body, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            require(len(payload) <= MAX_RESPONSE_BYTES, f"response too large: {path}")
            return {
                "status": response.status,
                "headers": {name.lower(): value for name, value in response.headers.items()},
                "body": payload,
            }
    except urllib.error.HTTPError as error:
        payload = error.read(MAX_RESPONSE_BYTES + 1)
        require(len(payload) <= MAX_RESPONSE_BYTES, f"error response too large: {path}")
        return {
            "status": error.code,
            "headers": {name.lower(): value for name, value in error.headers.items()},
            "body": payload,
        }


def run_probe() -> dict[str, Any]:
    resolved = socket.getaddrinfo(HOST, PORT, type=socket.SOCK_STREAM)
    resolved_ips = sorted({entry[4][0] for entry in resolved})
    require(resolved_ips, "MagicDNS returned no address")
    require(all(is_tailnet_address(value) for value in resolved_ips), "MagicDNS resolved outside the tailnet")

    context = ssl.create_default_context()
    with socket.create_connection((HOST, PORT), timeout=8) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=HOST) as tls_socket:
            certificate = tls_socket.getpeercert()
            sans = [value for kind, value in certificate.get("subjectAltName", ()) if kind == "DNS"]
            require(HOST in sans, "TLS certificate SAN does not bind the tailnet host")
            tls_version = tls_socket.version()
            cipher = tls_socket.cipher()[0]

    health_response = fetch("/bridge/v1/health")
    require(health_response["status"] == 200, "bridge health did not return HTTP 200")
    require(
        health_response["headers"].get("x-audio-remote-bridge") == "read-only-v1",
        "bridge marker is missing or wrong",
    )
    health = json.loads(health_response["body"].decode("utf-8"))
    require(health.get("kind") == "audio_remote_bridge_health", "wrong bridge health identity")
    require(
        health.get("projection") == "read-only-plus-whale-actions",
        "bridge projection does not expose the scoped whale contract",
    )
    require(health.get("effect_authority") is True, "scoped whale authority is missing")
    require(
        health.get("effect_scope") == ["whale:start", "whale:mode", "whale:stop"],
        "remote effect scope is broader or incomplete",
    )
    remote_action = health.get("remote_action")
    require(isinstance(remote_action, dict), "remote action contract is missing")
    require(remote_action.get("backend_token_exposed") is False, "backend token exposure is enabled")
    require(remote_action.get("tailscale_identity_required") is True, "Tailscale identity is not required")
    require(remote_action.get("session_identity_bound") is True, "session is not identity-bound")
    backend = health.get("backend")
    require(isinstance(backend, dict) and backend.get("remote_exposure") is False, "backend is remotely exposed")
    bridge_runtime_sha256 = health.get("runtime_sha256")
    require(
        isinstance(bridge_runtime_sha256, str)
        and len(bridge_runtime_sha256) == 64
        and all(character in "0123456789abcdef" for character in bridge_runtime_sha256),
        "bridge runtime SHA-256 is missing or invalid",
    )

    manifest_response = fetch("/manifest.webmanifest")
    require(manifest_response["status"] == 200, "manifest is unavailable")
    manifest = json.loads(manifest_response["body"].decode("utf-8"))
    require(manifest.get("start_url") == "/", "manifest start_url is not canonical")
    require(manifest.get("scope") == "/", "manifest scope is not canonical")
    require(manifest.get("display") == "standalone", "manifest is not standalone-installable")

    worker_response = fetch("/sw.js")
    require(worker_response["status"] == 200, "service worker is unavailable")
    worker_type = worker_response["headers"].get("content-type", "")
    require("javascript" in worker_type, "service worker content type is not JavaScript")

    snapshot_response = fetch("/api/v1/snapshot", method="HEAD")
    require(snapshot_response["status"] == 200, "snapshot HEAD is unavailable")
    require(
        snapshot_response["headers"].get("x-audio-remote-bridge") == "read-only-v1",
        "snapshot did not traverse the read-only bridge",
    )
    redactions = int(snapshot_response["headers"].get("x-audio-remote-redactions", "0"))
    require(redactions >= 1, "snapshot did not prove sensitive-field redaction")

    session_response = fetch(
        "/bridge/v1/session",
        method="POST",
        headers={"Origin": BASE_URL, "Content-Type": "application/json"},
        body=b"{}",
    )
    require(session_response["status"] == 200, "remote whale session is unavailable")
    require(
        session_response["headers"].get("x-audio-remote-effects") == "whale-v1",
        "remote whale capability marker is missing",
    )
    session = json.loads(session_response["body"].decode("utf-8"))
    require(session.get("kind") == "audio_remote_bridge_session", "wrong session identity")
    session_token = session.get("session_token")
    require(isinstance(session_token, str) and len(session_token) >= 32, "session token is missing")
    require("action_token" not in session, "backend-style action token leaked into session")

    invalid_action = json.dumps({"operation": "start", "mode": "not-allowed"}).encode("utf-8")
    negative_action_response = fetch(
        "/bridge/v1/actions/whale",
        method="POST",
        headers={
            "Origin": BASE_URL,
            "Content-Type": "application/json",
            "X-Audio-Bridge-Session": session_token,
        },
        body=invalid_action,
    )
    require(
        negative_action_response["status"] == 400,
        "invalid scoped whale action did not fail before effect dispatch",
    )

    post_response = fetch("/api/v1/health", method="POST")
    require(post_response["status"] == 405, "generic POST is not blocked by the bridge")

    return {
        "schema_version": 1,
        "kind": "audio_remote_bridge_ipad_probe",
        "platform": platform.platform(),
        "tailnet": {
            "host": HOST,
            "port": PORT,
            "resolved_ips": resolved_ips,
        },
        "bridge_runtime_sha256": bridge_runtime_sha256,
        "tls": {
            "version": tls_version,
            "cipher": cipher,
            "san_verified": True,
        },
        "pwa_surface": {
            "manifest_status": manifest_response["status"],
            "service_worker_status": worker_response["status"],
            "standalone_manifest": True,
        },
        "security": {
            "bridge_marker": "read-only-v1",
            "remote_effects_marker": "whale-v1",
            "snapshot_redactions": redactions,
            "generic_post_status": post_response["status"],
            "invalid_whale_action_status": negative_action_response["status"],
            "effect_authority": True,
            "effect_scope": ["whale:start", "whale:mode", "whale:stop"],
            "session_identity_bound": True,
            "backend_action_token_exposed": False,
            "backend_remote_exposure": False,
        },
        "runtime_acceptance": {
            "bridge_service_verified": True,
            "tailscale_serve_verified": True,
            "ipad_https_reachability_verified": True,
            "ipad_safari_verified": False,
            "pwa_installation_verified": False,
        },
        "does_not_establish": [
            "successful remote whale effect",
            "Safari renderer behavior",
            "PWA home-screen installation",
        ],
    }


def main() -> int:
    print(json.dumps(run_probe(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
