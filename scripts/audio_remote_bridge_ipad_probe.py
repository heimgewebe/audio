#!/usr/bin/env python3
"""Canonical read-only physical-iPad acceptance probe for Audiozentrale."""

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


def fetch(path: str, *, method: str = "GET") -> dict[str, Any]:
    request = urllib.request.Request(BASE_URL + path, method=method)
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
    require(health.get("projection") == "read-only", "bridge is not read-only")
    require(health.get("effect_authority") is False, "bridge unexpectedly has effect authority")
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

    post_response = fetch("/api/v1/health", method="POST")
    require(post_response["status"] == 405, "POST is not blocked by the bridge")

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
            "snapshot_redactions": redactions,
            "post_status": post_response["status"],
            "effect_authority": False,
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
            "Safari renderer behavior",
            "PWA home-screen installation",
        ],
    }


def main() -> int:
    print(json.dumps(run_probe(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
