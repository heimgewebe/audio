"""Security and contract tests for the scoped Audiozentrale remote bridge."""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "audio_remote_bridge.py"
SPEC = importlib.util.spec_from_file_location("audio_remote_bridge", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ADMIN_PATH = ROOT / "scripts" / "audio_remote_bridge_tailscale.py"
ADMIN_SPEC = importlib.util.spec_from_file_location("audio_remote_bridge_tailscale", ADMIN_PATH)
assert ADMIN_SPEC and ADMIN_SPEC.loader
ADMIN = importlib.util.module_from_spec(ADMIN_SPEC)
sys.modules[ADMIN_SPEC.name] = ADMIN
ADMIN_SPEC.loader.exec_module(ADMIN)

CONTRACT_PATH = ROOT / "inventory" / "audiozentrale-remote-bridge.v1.json"
SCHEMA_PATH = ROOT / "schemas" / "audiozentrale-remote-bridge.v1.schema.json"


class ContractTests(unittest.TestCase):
    def test_contract_validates_and_binds_schema(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(contract)
        self.assertEqual(
            contract["schema_binding"]["sha256"],
            hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(contract["bridge"]["listen_port"], 8766)
        self.assertEqual(contract["bridge"]["backend_port"], 8765)
        self.assertEqual(contract["tailscale_serve"]["https_port"], 9443)
        self.assertIs(contract["bridge"]["effect_authority"], True)
        self.assertEqual(
            contract["bridge"]["effect_scope"],
            ["whale:start", "whale:mode", "whale:stop"],
        )
        self.assertEqual(
            contract["bridge"]["effect_exclusions"],
            ["recording", "profiles", "routing", "devices", "system"],
        )
        self.assertIs(contract["bridge"]["backend_remote_exposure"], False)
        for name, value in contract["runtime_acceptance"].items():
            with self.subTest(name=name):
                self.assertIs(value, False)

        state_contract = contract["runtime_acceptance_state"]
        self.assertEqual(state_contract["path"], "~/.local/state/audio-remote-bridge-v1/acceptance.json")
        self.assertEqual(state_contract["mode"], "0600")
        self.assertEqual(state_contract["max_ttl_seconds"], MODULE.ACCEPTANCE_MAX_TTL_SECONDS)
        self.assertIs(state_contract["fail_closed"], True)
        self.assertIs(state_contract["binds_bridge_sha256"], True)

    def test_user_service_keeps_supported_hardening_only(self):
        unit = (ROOT / "systemd" / "user" / "audio-remote-bridge-v1.service").read_text(encoding="utf-8")
        for unsupported in (
            "PrivateDevices=yes",
            "ProtectKernelModules=yes",
            "ProtectClock=yes",
        ):
            with self.subTest(unsupported=unsupported):
                self.assertNotIn(unsupported, unit)
        for required in (
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "ProtectControlGroups=yes",
            "ProtectKernelTunables=yes",
            "RestrictNamespaces=yes",
            "RestrictAddressFamilies=AF_UNIX AF_INET",
        ):
            with self.subTest(required=required):
                self.assertIn(required, unit)

    def test_contract_matches_runtime_allowlist(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(contract["bridge"]["static_routes"]), MODULE.STATIC_ROUTES)
        self.assertEqual(set(contract["bridge"]["fixed_api_routes"]), MODULE.FIXED_API_ROUTES)
        self.assertEqual(contract["bridge"]["methods"], ["GET", "HEAD", "POST"])
        self.assertEqual(
            {name.lower() for name in contract["bridge"]["response_header_forward"]},
            MODULE.FORWARDED_RESPONSE_HEADERS,
        )
        self.assertEqual(
            {name.lower() for name in contract["bridge"]["request_header_forward"]},
            {"if-none-match", "range"},
        )
        self.assertIn("action_token", contract["bridge"]["json_scrubbing"]["must_remove"])
        remote_action = contract["bridge"]["remote_action"]
        self.assertEqual(remote_action["session_route"], MODULE.REMOTE_SESSION_ROUTE)
        self.assertEqual(remote_action["session_method"], "POST")
        self.assertEqual(remote_action["action_route"], MODULE.REMOTE_WHALE_ACTION_ROUTE)
        self.assertEqual(remote_action["tailnet_host"], MODULE.REMOTE_TAILNET_HOST)
        self.assertEqual(remote_action["session_header"], MODULE.REMOTE_ACTION_TOKEN_HEADER)
        self.assertIs(remote_action["tailscale_identity_required"], True)
        self.assertIs(remote_action["session_identity_bound"], True)
        self.assertIs(remote_action["backend_action_token_exposed"], False)
        self.assertEqual(set(contract["runtime_acceptance"]), set(MODULE.ACCEPTANCE_KEYS))


class RuntimeAcceptanceTests(unittest.TestCase):
    def write_state(self, path: pathlib.Path, *, now: int, values: dict[str, bool] | None = None, expires_offset: int = 3600) -> None:
        acceptance = MODULE.runtime_acceptance_defaults() if values is None else dict(values)
        payload = {
            "schema_version": 1,
            "kind": "audio_remote_bridge_runtime_acceptance",
            "contract_id": MODULE.CONTRACT_ID,
            "bridge_sha256": MODULE.BRIDGE_RUNTIME_SHA256,
            "recorded_at_unix": now,
            "expires_at_unix": now + expires_offset,
            "runtime_acceptance": acceptance,
            "evidence": {"source": "test", "evidence_sha256": "a" * 64},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)

    def test_missing_state_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            values, evidence = MODULE.load_runtime_acceptance(pathlib.Path(directory) / "missing.json", now_unix=1000)
        self.assertEqual(values, MODULE.runtime_acceptance_defaults())
        self.assertEqual(evidence, {"state": "unverified"})

    def test_valid_state_can_bind_only_proven_runtime_gates(self):
        now = 10_000
        values = MODULE.runtime_acceptance_defaults()
        values.update({
            "bridge_service_verified": True,
            "tailscale_serve_verified": True,
            "ipad_https_reachability_verified": True,
        })
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "acceptance.json"
            self.write_state(path, now=now, values=values)
            observed, evidence = MODULE.load_runtime_acceptance(path, now_unix=now + 1)
        self.assertEqual(observed, values)
        self.assertEqual(evidence["state"], "verified")
        self.assertEqual(evidence["source"], "test")
        self.assertEqual(evidence["evidence_sha256"], "a" * 64)
        self.assertEqual(evidence["bridge_sha256"], MODULE.BRIDGE_RUNTIME_SHA256)

    def test_expired_insecure_or_logically_impossible_state_fails_closed(self):
        now = 20_000
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            expired = root / "expired.json"
            self.write_state(expired, now=now - 100, expires_offset=50)
            values, evidence = MODULE.load_runtime_acceptance(expired, now_unix=now)
            self.assertEqual(values, MODULE.runtime_acceptance_defaults())
            self.assertEqual(evidence["state"], "expired")

            insecure = root / "insecure.json"
            self.write_state(insecure, now=now)
            insecure.chmod(0o644)
            values, evidence = MODULE.load_runtime_acceptance(insecure, now_unix=now)
            self.assertEqual(values, MODULE.runtime_acceptance_defaults())
            self.assertEqual(evidence["state"], "invalid")

            impossible = root / "impossible.json"
            bad = MODULE.runtime_acceptance_defaults()
            bad["ipad_https_reachability_verified"] = True
            self.write_state(impossible, now=now, values=bad)
            values, evidence = MODULE.load_runtime_acceptance(impossible, now_unix=now)
            self.assertEqual(values, MODULE.runtime_acceptance_defaults())
            self.assertEqual(evidence["state"], "invalid")

            stale_bridge = root / "stale-bridge.json"
            self.write_state(stale_bridge, now=now)
            payload = json.loads(stale_bridge.read_text(encoding="utf-8"))
            payload["bridge_sha256"] = "b" * 64
            stale_bridge.write_text(json.dumps(payload), encoding="utf-8")
            stale_bridge.chmod(0o600)
            values, evidence = MODULE.load_runtime_acceptance(stale_bridge, now_unix=now)
            self.assertEqual(values, MODULE.runtime_acceptance_defaults())
            self.assertEqual(evidence["state"], "invalid")


class TargetValidationTests(unittest.TestCase):
    def test_allowlisted_targets_are_canonicalized(self):
        self.assertEqual(MODULE.validate_request_target("/"), ("/", False))
        self.assertEqual(
            MODULE.validate_request_target("/api/v1/snapshot?refresh=1"),
            ("/api/v1/snapshot?refresh=1", True),
        )
        self.assertEqual(
            MODULE.validate_request_target("/api/v1/profiles/desktop-mixed/plan"),
            ("/api/v1/profiles/desktop-mixed/plan", True),
        )

    def test_unknown_queries_and_separator_bypasses_fail_closed(self):
        rejected = (
            "/unknown",
            "/app.js?x=1",
            "/api/v1/health?x=1",
            "/api/v1/snapshot?refresh=2",
            "/api/v1/profiles/a%2Fb/plan",
            "/api/v1/profiles/a%5Cb/plan",
            "/api/v1/profiles/a\\b/plan",
            "http://example.invalid/app.js",
        )
        for target in rejected:
            with self.subTest(target=target), self.assertRaises(MODULE.BridgeError):
                MODULE.validate_request_target(target)

    def test_json_scrubbing_is_recursive_and_fail_closed(self):
        payload = json.dumps(
            {
                "service": {"action_token": "local-only", "state": "ok"},
                "nested": [{"api_key": "hidden", "value": 1}],
            }
        ).encode()
        encoded, removed = MODULE.encode_scrubbed_json(payload)
        scrubbed = json.loads(encoded)
        self.assertEqual(scrubbed["service"], {"state": "ok"})
        self.assertEqual(scrubbed["nested"], [{"value": 1}])
        self.assertEqual(removed, 2)
        self.assertFalse(MODULE.contains_sensitive_json_key(scrubbed))
        variants, removed = MODULE.encode_scrubbed_json(
            json.dumps({"Auth-Token-Value": "x", "mySecretThing": "y", "apiKeyMaterial": "z", "ok": 1}).encode()
        )
        self.assertEqual(json.loads(variants), {"ok": 1})
        self.assertEqual(removed, 3)
        with self.assertRaises(MODULE.BridgeError):
            MODULE.encode_scrubbed_json(b"not-json")

    def test_configuration_is_fixed_loopback_only(self):
        MODULE.validate_configuration("127.0.0.1", 8766, "127.0.0.1", 8765)
        for args in (
            ("0.0.0.0", 8766, "127.0.0.1", 8765),
            ("127.0.0.1", 9000, "127.0.0.1", 8765),
            ("127.0.0.1", 8766, "192.168.1.2", 8765),
            ("127.0.0.1", 8766, "127.0.0.1", 9999),
        ):
            with self.subTest(args=args), self.assertRaises(MODULE.BridgeError):
                MODULE.validate_configuration(*args)


class FakeBackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    records: list[dict[str, object]] = []
    routes: dict[str, tuple[int, list[tuple[str, str]], bytes]] = {}

    def do_GET(self) -> None:
        type(self).records.append(
            {
                "method": "GET",
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
            }
        )
        status, headers, body = type(self).routes.get(
            self.path,
            (404, [("Content-Type", "application/json; charset=utf-8")], b"{}\n"),
        )
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).records.append(
            {
                "method": "POST",
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body,
            }
        )
        if self.path != "/api/v1/actions/whale":
            status = 404
            response = b"{}\n"
        else:
            action = json.loads(body.decode("utf-8"))
            operation = action["operation"]
            mode = action.get("mode", "organic")
            status = 200
            response = json.dumps(
                {
                    "kind": "audio_control_action_result",
                    "operation": operation,
                    "mode": mode if operation != "stop" else None,
                    "snapshot": {
                        "kind": "audio_control_snapshot",
                        "schema_version": 1,
                        "capabilities": {"whale_control": True},
                        "service": {
                            "action_token": "local-secret-value",
                            "authority": "local-backend",
                        },
                        "whale": {
                            "status": "ok",
                            "service": {
                                "active": operation != "stop",
                                "voice_mode": mode,
                            },
                        },
                    },
                },
                sort_keys=True,
            ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class BridgeHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.acceptance_directory = tempfile.TemporaryDirectory()
        self.acceptance_path_patch = mock.patch.object(
            MODULE,
            "ACCEPTANCE_STATE_PATH",
            pathlib.Path(self.acceptance_directory.name) / "missing-acceptance.json",
        )
        self.acceptance_path_patch.start()
        FakeBackendHandler.records = []
        FakeBackendHandler.routes = {
            "/api/v1/snapshot": (
                200,
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Cache-Control", "no-store"),
                    ("Set-Cookie", "forbidden=1"),
                    ("X-Unsafe", "drop-me"),
                ],
                json.dumps(
                    {
                        "kind": "audio_control_snapshot",
                        "schema_version": 1,
                        "capabilities": {"whale_control": True},
                        "service": {
                            "action_token": "local-secret-value",
                            "authority": "local-backend",
                        },
                        "whale": {"status": "ok", "service": {"active": False}},
                        "nested": {"private_key": "hidden", "ok": True},
                    }
                ).encode(),
            ),
            "/api/v1/snapshot?refresh=1": (
                200,
                [("Content-Type", "application/json; charset=utf-8")],
                json.dumps(
                    {
                        "kind": "audio_control_snapshot",
                        "schema_version": 1,
                        "capabilities": {"whale_control": True},
                        "service": {
                            "action_token": "local-secret-value",
                            "authority": "local-backend",
                        },
                        "whale": {"status": "ok", "service": {"active": False}},
                        "refresh": True,
                    }
                ).encode(),
            ),
            "/api/v1/health": (
                200,
                [("Content-Type", "application/json; charset=utf-8")],
                b'{"status":"serving"}',
            ),
            "/api/v1/replay": (
                200,
                [("Content-Type", "application/json; charset=utf-8")],
                b"not-json",
            ),
            "/app.js": (
                200,
                [
                    ("Content-Type", "application/javascript; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    ("ETag", '"abc"'),
                    ("Set-Cookie", "drop=1"),
                ],
                b'"use strict";\n',
            ),
            "/whale-learning-reference.wav": (
                206,
                [
                    ("Content-Type", "audio/wav"),
                    ("Cache-Control", "no-cache"),
                    ("Accept-Ranges", "bytes"),
                    ("Content-Range", "bytes 0-3/16"),
                    ("Set-Cookie", "drop=1"),
                ],
                b"RIFF",
            ),
        }
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackendHandler)
        self.backend_thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.backend_thread.start()
        self.backend_port_patch = mock.patch.object(MODULE, "BACKEND_PORT", self.backend.server_port)
        self.backend_port_patch.start()
        self.bridge = MODULE.AudioRemoteBridgeHTTPServer(("127.0.0.1", 0), test_mode=True)
        self.bridge_thread = threading.Thread(target=self.bridge.serve_forever, daemon=True)
        self.bridge_thread.start()

    def tearDown(self) -> None:
        self.bridge.shutdown()
        self.bridge_thread.join(timeout=2)
        self.bridge.server_close()
        self.backend_port_patch.stop()
        self.backend.shutdown()
        self.backend_thread.join(timeout=2)
        self.backend.server_close()
        self.acceptance_path_patch.stop()
        self.acceptance_directory.cleanup()

    def request(self, method: str, path: str, *, headers: dict[str, str] | None = None, body: bytes | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.bridge.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = response.status, {name: value for name, value in response.getheaders()}, payload
        connection.close()
        return result

    def test_snapshot_is_scrubbed_and_headers_are_narrowed(self):
        status, headers, payload = self.request(
            "GET",
            "/api/v1/snapshot",
            headers={
                "If-None-Match": '"abc"',
                "Authorization": "drop-this",
                "Cookie": "drop-this-too",
                "Range": "bytes=0-3",
                "X-Unrelated": "drop",
            },
        )
        self.assertEqual(status, 200)
        decoded = json.loads(payload)
        self.assertEqual(decoded["service"], {"authority": "local-backend"})
        self.assertEqual(decoded["nested"], {"ok": True})
        self.assertNotIn(b"local-secret-value", payload)
        self.assertEqual(headers["X-Audio-Remote-Bridge"], "read-only-v1")
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn("X-Unsafe", headers)
        self.assertGreaterEqual(int(headers["X-Audio-Remote-Redactions"]), 2)
        self.assertEqual(int(headers["Content-Length"]), len(payload))

        record = FakeBackendHandler.records[-1]
        backend_headers = record["headers"]
        self.assertEqual(record["path"], "/api/v1/snapshot")
        self.assertEqual(backend_headers["host"], f"127.0.0.1:{self.backend.server_port}")
        self.assertEqual(backend_headers["connection"], "close")
        self.assertEqual(backend_headers["if-none-match"], '"abc"')
        self.assertNotIn("authorization", backend_headers)
        self.assertNotIn("cookie", backend_headers)
        self.assertNotIn("range", backend_headers)
        self.assertNotIn("x-unrelated", backend_headers)

    def test_head_uses_the_scrubbed_representation_without_a_body(self):
        get_status, get_headers, get_payload = self.request("GET", "/api/v1/snapshot?refresh=1")
        head_status, head_headers, head_payload = self.request("HEAD", "/api/v1/snapshot?refresh=1")
        self.assertEqual((get_status, head_status), (200, 200))
        self.assertEqual(head_payload, b"")
        self.assertEqual(int(head_headers["Content-Length"]), len(get_payload))
        self.assertEqual(get_headers["X-Audio-Remote-Bridge"], "read-only-v1")
        self.assertEqual(head_headers["X-Audio-Remote-Bridge"], "read-only-v1")

    def test_static_response_keeps_only_safe_backend_headers(self):
        status, headers, payload = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(payload, b'"use strict";\n')
        self.assertEqual(headers["ETag"], '"abc"')
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertNotIn("Set-Cookie", headers)

    def test_audio_range_is_forwarded_and_partial_headers_survive(self):
        status, headers, payload = self.request(
            "GET",
            "/whale-learning-reference.wav",
            headers={
                "Range": "bytes=0-3",
                "Authorization": "drop-this",
            },
        )
        self.assertEqual(status, 206)
        self.assertEqual(payload, b"RIFF")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["Content-Range"], "bytes 0-3/16")
        self.assertEqual(int(headers["Content-Length"]), 4)
        self.assertNotIn("Set-Cookie", headers)

        record = FakeBackendHandler.records[-1]
        backend_headers = record["headers"]
        self.assertEqual(record["path"], "/whale-learning-reference.wav")
        self.assertEqual(backend_headers["range"], "bytes=0-3")
        self.assertNotIn("authorization", backend_headers)

    def test_invalid_backend_json_is_not_forwarded(self):
        status, headers, payload = self.request("GET", "/api/v1/replay")
        self.assertEqual(status, 502)
        self.assertEqual(headers["X-Audio-Remote-Bridge"], "read-only-v1")
        self.assertNotIn(b"not-json", payload)

    def test_non_scoped_mutations_and_request_bodies_are_rejected(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
            with self.subTest(method=method):
                status, headers, payload = self.request(method, "/api/v1/health")
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "GET, HEAD")
                self.assertEqual(payload, b"")
        status, _headers, _payload = self.request(
            "GET", "/api/v1/health", headers={"Content-Length": "1"}, body=b"x"
        )
        self.assertEqual(status, 400)

    def test_unknown_and_encoded_targets_never_reach_backend(self):
        before = len(FakeBackendHandler.records)
        for path in ("/unknown", "/api/v1/profiles/a%2Fb/plan", "/api/v1/health?x=1"):
            with self.subTest(path=path):
                status, _headers, _payload = self.request("GET", path)
                self.assertEqual(status, 404)
        self.assertEqual(len(FakeBackendHandler.records), before)

    def remote_headers(self, *, identity: str = "owner@example.test") -> dict[str, str]:
        return {
            "Host": MODULE.REMOTE_TAILNET_HOST,
            "Tailscale-User-Login": identity,
        }

    def issue_remote_session(self, *, identity: str = "owner@example.test") -> str:
        status, headers, payload = self.request(
            "POST",
            MODULE.REMOTE_SESSION_ROUTE,
            headers={
                **self.remote_headers(identity=identity),
                "Origin": f"https://{MODULE.REMOTE_TAILNET_HOST}",
                "Content-Type": "application/json",
            },
            body=b"{}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Audio-Remote-Bridge"], "read-only-v1")
        self.assertEqual(headers[MODULE.REMOTE_EFFECTS_HEADER], MODULE.REMOTE_EFFECTS_VALUE)
        session = json.loads(payload)
        self.assertEqual(session["kind"], "audio_remote_bridge_session")
        self.assertEqual(session["effect_scope"], ["whale"])
        self.assertEqual(set(session["allowed_operations"]), {"start", "mode", "stop"})
        self.assertNotIn("action_token", session)
        self.assertGreaterEqual(len(session["session_token"]), 32)
        return session["session_token"]

    def test_remote_session_requires_post_exact_origin_host_and_verified_identity(self):
        status, _response_headers, _payload = self.request(
            "GET", MODULE.REMOTE_SESSION_ROUTE, headers=self.remote_headers()
        )
        self.assertEqual(status, 404)

        cases = (
            {
                "Host": "other.tail6dbb90.ts.net:9443",
                "Origin": "https://other.tail6dbb90.ts.net:9443",
                "Tailscale-User-Login": "owner@example.test",
                "Content-Type": "application/json",
            },
            {
                "Host": MODULE.REMOTE_TAILNET_HOST,
                "Origin": f"https://{MODULE.REMOTE_TAILNET_HOST}",
                "Content-Type": "application/json",
            },
            {
                **self.remote_headers(),
                "Origin": "https://evil.example",
                "Content-Type": "application/json",
            },
        )
        for headers in cases:
            with self.subTest(headers=headers):
                status, _response_headers, _payload = self.request(
                    "POST", MODULE.REMOTE_SESSION_ROUTE, headers=headers, body=b"{}"
                )
                self.assertEqual(status, 403)

    def test_remote_sessions_are_bounded_expiring_and_identity_bound(self):
        owner_identity = hashlib.sha256(b"owner@example.test").hexdigest()
        other_identity = hashlib.sha256(b"other@example.test").hexdigest()
        tokens = []
        for _index in range(MODULE.REMOTE_ACTION_SESSION_CAPACITY + 2):
            token, _expires = self.bridge.issue_action_session(owner_identity)
            tokens.append(token)
        self.assertEqual(
            len(self.bridge._action_sessions), MODULE.REMOTE_ACTION_SESSION_CAPACITY
        )
        self.assertFalse(self.bridge.action_session_valid(tokens[0], owner_identity))
        self.assertTrue(self.bridge.action_session_valid(tokens[-1], owner_identity))
        self.assertFalse(self.bridge.action_session_valid(tokens[-1], other_identity))

        latest_digest = hashlib.sha256(tokens[-1].encode("utf-8")).hexdigest()
        self.bridge._action_sessions[latest_digest] = (
            int(MODULE.time.time()) - 1,
            owner_identity,
        )
        self.assertFalse(self.bridge.action_session_valid(tokens[-1], owner_identity))
        self.assertNotIn(latest_digest, self.bridge._action_sessions)

    def test_remote_whale_action_rejects_parallel_effect_before_backend_dispatch(self):
        token = self.issue_remote_session()
        headers = {
            **self.remote_headers(),
            "Origin": f"https://{MODULE.REMOTE_TAILNET_HOST}",
            "Content-Type": "application/json",
            MODULE.REMOTE_ACTION_TOKEN_HEADER: token,
        }
        body = json.dumps({"operation": "start", "mode": "morph"}).encode("utf-8")
        before = len(FakeBackendHandler.records)
        self.assertTrue(self.bridge._action_lock.acquire(blocking=False))
        try:
            status, _response_headers, _payload = self.request(
                "POST", MODULE.REMOTE_WHALE_ACTION_ROUTE, headers=headers, body=body
            )
        finally:
            self.bridge._action_lock.release()
        self.assertEqual(status, 409)
        self.assertEqual(len(FakeBackendHandler.records), before)

    def test_remote_whale_action_is_identity_origin_session_and_type_bound(self):
        token = self.issue_remote_session()
        before = len(FakeBackendHandler.records)
        body = json.dumps({"operation": "start", "mode": "morph"}).encode("utf-8")
        headers = {
            **self.remote_headers(),
            "Origin": f"https://{MODULE.REMOTE_TAILNET_HOST}",
            "Content-Type": "application/json",
            MODULE.REMOTE_ACTION_TOKEN_HEADER: token,
        }
        status, response_headers, payload = self.request(
            "POST", MODULE.REMOTE_WHALE_ACTION_ROUTE, headers=headers, body=body
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["X-Audio-Remote-Bridge"], "whale-action-v1")
        self.assertEqual(
            response_headers[MODULE.REMOTE_EFFECTS_HEADER], MODULE.REMOTE_EFFECTS_VALUE
        )
        decoded = json.loads(payload)
        self.assertEqual(decoded["kind"], "audio_control_action_result")
        self.assertTrue(decoded["snapshot"]["whale"]["service"]["active"])
        self.assertNotIn(b"local-secret-value", payload)
        self.assertNotIn("action_token", decoded["snapshot"]["service"])

        records = FakeBackendHandler.records[before:]
        self.assertEqual([record["method"] for record in records], ["GET", "POST"])
        self.assertEqual(records[0]["path"], "/api/v1/snapshot?refresh=1")
        self.assertEqual(records[1]["path"], "/api/v1/actions/whale")
        backend_headers = records[1]["headers"]
        self.assertEqual(backend_headers["x-audio-control-token"], "local-secret-value")
        self.assertEqual(
            backend_headers["origin"], f"http://127.0.0.1:{self.backend.server_port}"
        )
        self.assertEqual(json.loads(records[1]["body"]), {"operation": "start", "mode": "morph"})

    def test_remote_whale_action_rejects_wrong_identity_origin_session_and_payload(self):
        token = self.issue_remote_session(identity="owner@example.test")
        base = {
            **self.remote_headers(identity="owner@example.test"),
            "Origin": f"https://{MODULE.REMOTE_TAILNET_HOST}",
            "Content-Type": "application/json",
            MODULE.REMOTE_ACTION_TOKEN_HEADER: token,
        }
        cases = (
            ({**base, "Origin": "https://evil.example"}, {"operation": "start", "mode": "morph"}, 403),
            ({**base, "Tailscale-User-Login": "other@example.test"}, {"operation": "start", "mode": "morph"}, 403),
            ({**base, MODULE.REMOTE_ACTION_TOKEN_HEADER: "x" * 40}, {"operation": "start", "mode": "morph"}, 403),
            (base, {"operation": "start", "mode": "unknown"}, 400),
            (base, {"operation": "stop", "mode": "morph"}, 400),
            (base, {"operation": "start", "mode": "morph", "extra": True}, 400),
        )
        for headers, action, expected_status in cases:
            before = len(FakeBackendHandler.records)
            with self.subTest(action=action, status=expected_status):
                status, _response_headers, _payload = self.request(
                    "POST",
                    MODULE.REMOTE_WHALE_ACTION_ROUTE,
                    headers=headers,
                    body=json.dumps(action).encode("utf-8"),
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(len(FakeBackendHandler.records), before)

        status, _headers, _payload = self.request(
            "POST",
            "/api/v1/actions/recording",
            headers=base,
            body=json.dumps({"operation": "start"}).encode("utf-8"),
        )
        self.assertEqual(status, 405)
        status, _headers, _payload = self.request(
            "PUT", MODULE.REMOTE_WHALE_ACTION_ROUTE, headers=base, body=b"{}"
        )
        self.assertEqual(status, 405)

    def test_bridge_health_is_local_contract_truth_only(self):
        status, headers, payload = self.request("GET", "/bridge/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Audio-Remote-Bridge"], "read-only-v1")
        health = json.loads(payload)
        self.assertEqual(health["contract_id"], "audiozentrale-remote-bridge-v1")
        self.assertEqual(health["projection"], "read-only-plus-whale-actions")
        self.assertIs(health["effect_authority"], True)
        self.assertEqual(
            health["effect_scope"], ["whale:start", "whale:mode", "whale:stop"]
        )
        self.assertIn("recording", health["effect_exclusions"])
        self.assertIs(health["remote_action"]["backend_token_exposed"], False)
        self.assertIs(health["remote_action"]["tailscale_identity_required"], True)
        self.assertIs(health["remote_action"]["session_identity_bound"], True)
        self.assertIs(health["backend"]["remote_exposure"], False)
        self.assertEqual(health["runtime_sha256"], MODULE.BRIDGE_RUNTIME_SHA256)
        self.assertEqual(set(health["runtime_acceptance"]), set(MODULE.ACCEPTANCE_KEYS))
        self.assertEqual(set(health["runtime_acceptance"].values()), {False})
        self.assertEqual(health["runtime_acceptance_evidence"]["state"], "unverified")


class FakeServeRunner:
    def __init__(self, state, *, drift_on_apply=False):
        self.state = json.loads(json.dumps(state))
        self.calls = []
        self.drift_on_apply = drift_on_apply

    def run(self, argv):
        self.calls.append(tuple(argv))
        if argv == ADMIN.status_argv():
            return ADMIN.CommandResult(tuple(argv), 0, json.dumps(self.state), "")
        if argv == ADMIN.apply_argv():
            host = "heim-pc.example.ts.net"
            self.state.setdefault("TCP", {})[str(ADMIN.HTTPS_PORT)] = {"HTTPS": True}
            self.state.setdefault("Web", {})[f"{host}:{ADMIN.HTTPS_PORT}"] = {
                "Handlers": {"/": {"Proxy": ADMIN.TARGET_URL}}
            }
            if self.drift_on_apply:
                self.state["Web"][f"{host}:443"]["Handlers"]["/"]["Proxy"] = "http://127.0.0.1:9999"
            return ADMIN.CommandResult(tuple(argv), 0, "", "")
        if argv == ADMIN.remove_argv():
            self.state = ADMIN.strip_owned_port(self.state)
            return ADMIN.CommandResult(tuple(argv), 0, "", "")
        raise AssertionError(f"unexpected argv: {argv}")


class TailscaleAdminTests(unittest.TestCase):
    def base_state(self):
        return {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                "heim-pc.example.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:18082"}}
                }
            },
        }

    def test_apply_and_remove_preserve_unrelated_443_state(self):
        before = self.base_state()
        runner = FakeServeRunner(before)
        applied = ADMIN.apply(runner)
        self.assertTrue(applied["changed"])
        self.assertTrue(ADMIN.projection_is_expected(runner.state))
        self.assertEqual(ADMIN.strip_owned_port(runner.state), before)
        removed = ADMIN.remove(runner)
        self.assertTrue(removed["changed"])
        self.assertEqual(runner.state, before)
        self.assertIn(tuple(ADMIN.apply_argv()), runner.calls)
        self.assertIn(tuple(ADMIN.remove_argv()), runner.calls)

    def test_existing_9443_conflict_and_public_projection_block_mutation(self):
        state = self.base_state()
        state["TCP"]["9443"] = {"HTTPS": True}
        state["Web"]["heim-pc.example.ts.net:9443"] = {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}
        runner = FakeServeRunner(state)
        self.assertEqual(ADMIN.plan(runner)["action"], "blocked-conflict")
        with self.assertRaises(ADMIN.AdminError):
            ADMIN.apply(runner)
        self.assertNotIn(tuple(ADMIN.apply_argv()), runner.calls)

        public = self.base_state()
        public["TCP"]["9443"] = {"HTTPS": True}
        public["Web"]["heim-pc.example.ts.net:9443"] = {"Handlers": {"/": {"Proxy": ADMIN.TARGET_URL}}}
        public["AllowFunnel"] = {"heim-pc.example.ts.net:9443": True}
        self.assertFalse(ADMIN.projection_is_expected(public))
        self.assertTrue(ADMIN.status_report(public)["public_exposure"])

    def test_postflight_drift_triggers_only_scoped_rollback(self):
        runner = FakeServeRunner(self.base_state(), drift_on_apply=True)
        with self.assertRaises(ADMIN.AdminError):
            ADMIN.apply(runner)
        self.assertIn(tuple(ADMIN.remove_argv()), runner.calls)
        forbidden = {"reset", "clear", "funnel"}
        self.assertFalse(any(forbidden.intersection(call) for call in runner.calls))

    def test_subprocess_runner_rejects_broad_or_public_operations_before_exec(self):
        runner = ADMIN.SubprocessRunner()
        for argv in (
            ["tailscale", "serve", "reset"],
            ["tailscale", "serve", "clear"],
            ["tailscale", "serve", "funnel"],
            ["tailscale", "serve", "1234"],
            ["tailscale", "status"],
        ):
            with self.subTest(argv=argv), self.assertRaises(ADMIN.AdminError):
                runner.run(argv)


if __name__ == "__main__":
    unittest.main()
