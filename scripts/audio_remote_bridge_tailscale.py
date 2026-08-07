#!/usr/bin/env python3
"""Scoped Tailscale Serve administration for the Audiozentrale remote bridge.

Only HTTPS port 9443 belongs to this component. Every mutation is surrounded by
full Serve-state readback and fails if unrelated configuration changes.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

HTTPS_PORT = 9443
TARGET_URL = "http://127.0.0.1:8766"
MAX_OUTPUT_BYTES = 1_048_576


class AdminError(RuntimeError):
    """Fail-closed Serve administration error."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(self, argv: list[str]) -> CommandResult: ...


class SubprocessRunner:
    def run(self, argv: list[str]) -> CommandResult:
        allowed = {tuple(status_argv()), tuple(apply_argv()), tuple(remove_argv())}
        if tuple(argv) not in allowed:
            raise AdminError("command is outside the exact Audiozentrale Serve allowlist")
        completed = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        result = CommandResult(
            tuple(argv),
            completed.returncode,
            completed.stdout[-MAX_OUTPUT_BYTES:],
            completed.stderr[-MAX_OUTPUT_BYTES:],
        )
        if result.returncode != 0:
            raise AdminError(result.stderr.strip() or result.stdout.strip() or "tailscale serve failed")
        return result


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def status_argv() -> list[str]:
    return ["tailscale", "serve", "status", "--json"]


def apply_argv() -> list[str]:
    return ["tailscale", "serve", "--bg", f"--https={HTTPS_PORT}", TARGET_URL]


def remove_argv() -> list[str]:
    return ["tailscale", "serve", f"--https={HTTPS_PORT}", "off"]


def read_status(runner: Runner) -> dict[str, Any]:
    result = runner.run(status_argv())
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise AdminError("tailscale serve status returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise AdminError("tailscale serve status returned an invalid shape")
    return payload


def authority_uses_port(authority: str, port: int) -> bool:
    host, separator, raw_port = authority.rpartition(":")
    return bool(host and separator and raw_port.isdigit() and int(raw_port) == port)


def strip_owned_port(status: dict[str, Any], port: int = HTTPS_PORT) -> dict[str, Any]:
    stripped = copy.deepcopy(status)
    tcp = stripped.get("TCP")
    if isinstance(tcp, dict):
        tcp.pop(str(port), None)
        if not tcp:
            stripped.pop("TCP", None)
    web = stripped.get("Web")
    if isinstance(web, dict):
        for key in list(web):
            if isinstance(key, str) and authority_uses_port(key, port):
                web.pop(key, None)
        if not web:
            stripped.pop("Web", None)
    allow_funnel = stripped.get("AllowFunnel")
    if isinstance(allow_funnel, dict):
        for key in list(allow_funnel):
            if isinstance(key, str) and authority_uses_port(key, port):
                allow_funnel.pop(key, None)
        if not allow_funnel:
            stripped.pop("AllowFunnel", None)
    return stripped


def owned_projection(status: dict[str, Any], port: int = HTTPS_PORT) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    tcp = status.get("TCP")
    if isinstance(tcp, dict) and str(port) in tcp:
        projection["TCP"] = copy.deepcopy(tcp[str(port)])
    web = status.get("Web")
    if isinstance(web, dict):
        matches = {
            key: copy.deepcopy(value)
            for key, value in web.items()
            if isinstance(key, str) and authority_uses_port(key, port)
        }
        if matches:
            projection["Web"] = matches
    allow_funnel = status.get("AllowFunnel")
    if isinstance(allow_funnel, dict):
        matches = {
            key: copy.deepcopy(value)
            for key, value in allow_funnel.items()
            if isinstance(key, str) and authority_uses_port(key, port)
        }
        if matches:
            projection["AllowFunnel"] = matches
    return projection


def projection_absent(status: dict[str, Any]) -> bool:
    return not owned_projection(status)


def projection_is_expected(status: dict[str, Any]) -> bool:
    projection = owned_projection(status)
    if projection.get("TCP") != {"HTTPS": True}:
        return False
    web = projection.get("Web")
    if not isinstance(web, dict) or len(web) != 1:
        return False
    if next(iter(web.values())) != {"Handlers": {"/": {"Proxy": TARGET_URL}}}:
        return False
    return "AllowFunnel" not in projection


def status_report(status: dict[str, Any]) -> dict[str, Any]:
    projection = owned_projection(status)
    return {
        "schema_version": 1,
        "kind": "audio_remote_bridge_tailscale_status",
        "https_port": HTTPS_PORT,
        "target": TARGET_URL,
        "tailnet_only": True,
        "public_exposure": bool(projection.get("AllowFunnel")),
        "owned_projection_present": projection_is_expected(status),
        "port_conflict": bool(projection) and not projection_is_expected(status),
        "state_sha256": sha256_json(status),
        "unowned_state_sha256": sha256_json(strip_owned_port(status)),
    }


def plan(runner: Runner) -> dict[str, Any]:
    status = read_status(runner)
    if projection_absent(status):
        action = "apply"
    elif projection_is_expected(status):
        action = "already-applied"
    else:
        action = "blocked-conflict"
    return {
        **status_report(status),
        "kind": "audio_remote_bridge_tailscale_plan",
        "action": action,
        "apply_argv": apply_argv(),
        "rollback_argv": remove_argv(),
        "preserve_other_config": True,
    }


def run_effect(runner: Runner, argv: list[str]) -> None:
    result = runner.run(argv)
    if result.returncode != 0:
        raise AdminError("tailscale serve mutation failed")


def rollback_failed_apply(runner: Runner, before: dict[str, Any], reason: str) -> None:
    rollback_problem: str | None = None
    try:
        run_effect(runner, remove_argv())
        rollback = read_status(runner)
        if rollback != before:
            rollback_problem = "scoped rollback did not restore the complete pre-state"
    except AdminError as error:
        rollback_problem = str(error)
    if rollback_problem:
        raise AdminError(f"{reason}; rollback failed: {rollback_problem}")
    raise AdminError(f"{reason}; scoped rollback restored the pre-state")


def apply(runner: Runner) -> dict[str, Any]:
    before = read_status(runner)
    if projection_is_expected(before):
        return {**status_report(before), "kind": "audio_remote_bridge_tailscale_apply", "changed": False, "reason": "already-applied", "preserved_unowned_state": True}
    if not projection_absent(before):
        raise AdminError("HTTPS 9443 is already occupied by another Serve configuration")
    run_effect(runner, apply_argv())
    after = read_status(runner)
    if not projection_is_expected(after):
        rollback_failed_apply(runner, before, "postflight lacks exact Audio projection")
    if strip_owned_port(after) != before:
        rollback_failed_apply(runner, before, "postflight changed unrelated Serve state")
    return {**status_report(after), "kind": "audio_remote_bridge_tailscale_apply", "changed": True, "reason": "applied", "preserved_unowned_state": True}


def remove(runner: Runner) -> dict[str, Any]:
    before = read_status(runner)
    if projection_absent(before):
        return {**status_report(before), "kind": "audio_remote_bridge_tailscale_remove", "changed": False, "reason": "already-absent", "preserved_unowned_state": True}
    if not projection_is_expected(before):
        raise AdminError("HTTPS 9443 does not match the Audiozentrale bridge contract")
    expected_after = strip_owned_port(before)
    run_effect(runner, remove_argv())
    after = read_status(runner)
    if owned_projection(after):
        raise AdminError("scoped removal left an HTTPS 9443 projection")
    if after != expected_after:
        raise AdminError("Serve removal changed unrelated configuration")
    return {**status_report(after), "kind": "audio_remote_bridge_tailscale_remove", "changed": True, "reason": "removed", "preserved_unowned_state": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audio-remote-bridge-tailscale")
    parser.add_argument("command", choices=("status", "plan", "apply", "remove"))
    args = parser.parse_args(argv)
    runner = SubprocessRunner()
    try:
        if args.command == "status":
            output = status_report(read_status(runner))
        elif args.command == "plan":
            output = plan(runner)
        elif args.command == "apply":
            output = apply(runner)
        else:
            output = remove(runner)
    except AdminError as error:
        print(json.dumps({"schema_version": 1, "kind": "audio_remote_bridge_tailscale_error", "error": str(error)}, sort_keys=True, separators=(",", ":")))
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
