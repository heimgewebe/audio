#!/usr/bin/env python3
"""Managed controller for the existing Grabowski Dauersong v9 service.

The musical runtime remains the proven v9 host installation for now.  This
module makes it a governed Audiozentrale component: exact source/soundfont
verification, effective systemd hardening readback, bounded start readiness,
stream-volume verification, stop and recovery.  It never creates a second
Dauersong performer identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shlex
import shutil
import stat
import subprocess
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "inventory" / "dauersong-v9-legacy.v1.json"
UNIT_NAME = "grabowski-dauersong.service"
MANAGED_BY = "audio-control-v1"
MAX_JSON_BYTES = 1_048_576
MAX_RUNTIME_SECONDS = 21_600
START_TIMEOUT_SECONDS = 15
EXPECTED_DROPIN_SUFFIX = "grabowski-dauersong.service.d/zz-audio-control-v1.conf"


def run_capture(argv: list[str], *, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
        env={**os.environ, "LC_ALL": "C.UTF-8"},
    )


def read_json_object(path: pathlib.Path, *, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_JSON_BYTES:
            raise RuntimeError(f"{label} has an invalid type or size")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(MAX_JSON_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_JSON_BYTES:
        raise RuntimeError(f"{label} exceeds the size limit")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} is not a JSON object")
    return value


def load_manifest() -> dict[str, Any]:
    manifest = read_json_object(MANIFEST_PATH, label="Dauersong runtime binding")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "dauersong_v9_legacy_runtime_binding"
        or manifest.get("legacy_unit") != UNIT_NAME
        or manifest.get("managed_unit") != UNIT_NAME
    ):
        raise RuntimeError("Dauersong runtime binding has a foreign schema")
    if not isinstance(manifest.get("required_files"), dict) or not manifest["required_files"]:
        raise RuntimeError("Dauersong runtime binding has no required files")
    return manifest


def legacy_root(manifest: dict[str, Any]) -> pathlib.Path:
    if manifest.get("legacy_root") != "~/.local/state/grabowski-music":
        raise RuntimeError("Dauersong runtime binding has an unexpected host root")
    return pathlib.Path.home() / ".local" / "state" / "grabowski-music"


def regular_file_fingerprint(path: pathlib.Path) -> tuple[int, str, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"not a regular file: {path}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 131_072)
            if not chunk:
                break
            digest.update(chunk)
        return metadata.st_size, digest.hexdigest(), metadata
    finally:
        os.close(descriptor)


def validate_runtime_sources(manifest: dict[str, Any]) -> dict[str, Any]:
    base = legacy_root(manifest)
    try:
        metadata = base.lstat()
    except OSError:
        return {"ready": False, "checked_files": [], "errors": ["legacy-root-unavailable"]}
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        return {"ready": False, "checked_files": [], "errors": ["legacy-root-unsafe"]}

    checked: list[dict[str, Any]] = []
    errors: list[str] = []
    for name, binding in sorted(manifest["required_files"].items()):
        if not isinstance(name, str) or pathlib.PurePosixPath(name).name != name:
            errors.append("invalid-runtime-filename")
            continue
        if not isinstance(binding, dict):
            errors.append(f"invalid-binding:{name}")
            continue
        try:
            size, digest, _ = regular_file_fingerprint(base / name)
        except FileNotFoundError:
            errors.append(f"missing:{name}")
            continue
        except (OSError, RuntimeError):
            errors.append(f"unsafe-type:{name}")
            continue
        verified = size == binding.get("bytes") and digest == binding.get("sha256")
        checked.append({"file": name, "bytes": size, "sha256": digest, "verified": verified})
        if not verified:
            errors.append(f"source-drift:{name}")
    return {"ready": not errors, "checked_files": checked, "errors": errors}


def validate_soundfont(manifest: dict[str, Any]) -> dict[str, Any]:
    binding = manifest.get("soundfont_binding")
    if not isinstance(binding, dict):
        return {"ready": False, "error": "soundfont-binding-missing"}
    configured = pathlib.Path(str(binding.get("configured_path", "")))
    if str(configured) != str(manifest.get("soundfont", "")):
        return {"ready": False, "error": "soundfont-config-mismatch"}
    try:
        resolved = configured.resolve(strict=True)
    except OSError:
        return {"ready": False, "error": "soundfont-unavailable"}
    if resolved != pathlib.Path(str(binding.get("resolved_path", ""))):
        return {"ready": False, "error": "soundfont-target-drift", "resolved_path": str(resolved)}
    try:
        size, digest, metadata = regular_file_fingerprint(resolved)
    except (OSError, RuntimeError):
        return {"ready": False, "error": "soundfont-target-unsafe"}
    ownership_ready = (
        type(binding.get("owner_uid")) is int
        and metadata.st_uid == binding["owner_uid"]
        and (
            binding.get("forbid_group_or_other_write") is not True
            or metadata.st_mode & 0o022 == 0
        )
    )
    ready = ownership_ready and size == binding.get("bytes") and digest == binding.get("sha256")
    return {
        "ready": ready,
        "error": None if ready else "soundfont-content-or-owner-drift",
        "configured_path": str(configured),
        "resolved_path": str(resolved),
        "bytes": size,
        "sha256": digest,
        "owner_uid": metadata.st_uid,
        "group_or_other_writable": bool(metadata.st_mode & 0o022),
    }


def parse_environment(text: str) -> dict[str, str]:
    try:
        items = shlex.split(text)
    except ValueError:
        return {}
    return dict(item.split("=", 1) for item in items if "=" in item)


def service_status() -> dict[str, Any]:
    properties = (
        "LoadState,ActiveState,SubState,Result,MainPID,Environment,Restart,MemoryMax,"
        "TasksMax,LimitNOFILE,RuntimeMaxUSec,CPUQuotaPerSecUSec,DropInPaths"
    )
    result = run_capture(
        ["systemctl", "--user", "show", UNIT_NAME, f"--property={properties}", "--no-pager"]
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if result.returncode != 0 and values.get("LoadState") != "not-found":
        raise RuntimeError(result.stderr.strip() or "systemctl show failed")
    environment = parse_environment(values.get("Environment", ""))
    try:
        main_pid = int(values.get("MainPID", "0") or "0")
    except ValueError:
        main_pid = 0
    try:
        runtime_seconds = int(environment.get("AUDIO_DAUERSONG_RUNTIME_MAX_SECONDS", ""))
    except ValueError:
        runtime_seconds = None
    try:
        volume = int(environment.get("GRABOWSKI_STREAM_VOLUME", ""))
    except ValueError:
        volume = None
    active = values.get("ActiveState") == "active" and values.get("SubState") == "running"
    dropins = values.get("DropInPaths", "").split()
    hardening = {
        "managed_by": environment.get("AUDIO_DAUERSONG_MANAGED_BY"),
        "stream_volume_percent": volume,
        "runtime_max_seconds": runtime_seconds,
        "restart": values.get("Restart"),
        "memory_max": values.get("MemoryMax"),
        "tasks_max": values.get("TasksMax"),
        "limit_nofile": values.get("LimitNOFILE"),
        "runtime_max_usec": values.get("RuntimeMaxUSec"),
        "cpu_quota_per_sec_usec": values.get("CPUQuotaPerSecUSec"),
        "dropin_present": any(path.endswith(EXPECTED_DROPIN_SUFFIX) for path in dropins),
    }
    hardening_ready = (
        hardening["managed_by"] == MANAGED_BY
        and hardening["stream_volume_percent"] == 100
        and hardening["runtime_max_seconds"] == MAX_RUNTIME_SECONDS
        and hardening["restart"] == "no"
        and hardening["memory_max"] == "536870912"
        and hardening["tasks_max"] == "32"
        and hardening["limit_nofile"] == "1024"
        and hardening["runtime_max_usec"] != "infinity"
        and hardening["cpu_quota_per_sec_usec"] != "infinity"
        and hardening["dropin_present"] is True
    )
    return {
        "unit": UNIT_NAME,
        "load_state": values.get("LoadState", "unknown"),
        "active_state": values.get("ActiveState", "unknown"),
        "sub_state": values.get("SubState", "unknown"),
        "result": values.get("Result", "unknown"),
        "main_pid": main_pid,
        "active": active,
        "hardening": hardening,
        "hardening_ready": hardening_ready,
    }


def descendant_pids(main_pid: int) -> set[int]:
    if main_pid <= 1:
        return set()
    found: set[int] = set()
    pending = [main_pid]
    while pending and len(found) < 64:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        try:
            children = pathlib.Path(f"/proc/{current}/task/{current}/children").read_text().split()
        except OSError:
            continue
        for child in children:
            try:
                pending.append(int(child))
            except ValueError:
                pass
    return found


def stream_status(main_pid: int) -> dict[str, Any]:
    pids = descendant_pids(main_pid)
    if not pids or not shutil.which("pactl"):
        return {"found": False, "indexes": [], "max_volume_percent": None}
    result = run_capture(["pactl", "--format=json", "list", "sink-inputs"])
    if result.returncode != 0:
        return {"found": False, "indexes": [], "max_volume_percent": None}
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"found": False, "indexes": [], "max_volume_percent": None}
    matches: list[dict[str, Any]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("properties"), dict):
            continue
        properties = entry["properties"]
        try:
            process_id = int(properties.get("application.process.id", "0"))
        except (TypeError, ValueError):
            continue
        if process_id not in pids or properties.get("application.process.binary") != "fluidsynth":
            continue
        percentages: list[int] = []
        for channel in (entry.get("volume") or {}).values():
            text = channel.get("value_percent") if isinstance(channel, dict) else None
            if isinstance(text, str) and text.endswith("%"):
                try:
                    percentages.append(int(text[:-1]))
                except ValueError:
                    pass
        matches.append(
            {
                "index": entry.get("index"),
                "process_id": process_id,
                "volume_percent": max(percentages) if percentages else None,
            }
        )
    volumes = [item["volume_percent"] for item in matches if type(item["volume_percent"]) is int]
    return {
        "found": bool(matches),
        "indexes": [item["index"] for item in matches],
        "max_volume_percent": max(volumes) if volumes else None,
        "streams": matches,
    }


def enforce_stream_volume(main_pid: int, percent: int = 100) -> dict[str, Any]:
    for _ in range(40):
        current = stream_status(main_pid)
        if current.get("found"):
            for index in current["indexes"]:
                result = run_capture(["pactl", "set-sink-input-volume", str(index), f"{percent}%"])
                if result.returncode != 0:
                    raise RuntimeError("could not enforce Dauersong stream volume")
            verified = stream_status(main_pid)
            maximum = verified.get("max_volume_percent")
            if verified.get("found") and type(maximum) is int and maximum <= percent:
                return verified
        time.sleep(0.05)
    raise RuntimeError("Dauersong stream did not become verifiably bounded")


def live_status_snapshot(manifest: dict[str, Any]) -> dict[str, Any] | None:
    path = legacy_root(manifest) / "ecosystem" / "live-status.json"
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_size > MAX_JSON_BYTES:
        return None
    try:
        value = read_json_object(path, label="Dauersong live status")
    except (OSError, RuntimeError):
        return None
    return {
        "updated_at_unix": metadata.st_mtime,
        "section": value.get("section"),
        "name": value.get("name"),
        "cycle": value.get("cycle"),
        "effective_bpm": value.get("effective_bpm"),
        "melody_notes": value.get("melody_notes"),
    }


def host_verification() -> dict[str, Any]:
    manifest = load_manifest()
    sources = validate_runtime_sources(manifest)
    soundfont = validate_soundfont(manifest)
    return {
        "ready": sources["ready"] and soundfont["ready"],
        "source_binding": sources,
        "soundfont": soundfont,
    }


def runtime_doctor() -> dict[str, Any]:
    commands = {name: bool(shutil.which(name)) for name in ("fluidsynth", "pactl", "systemctl")}
    blockers = [f"{name}-not-installed" for name, present in commands.items() if not present]
    host = host_verification()
    if not host["source_binding"]["ready"]:
        blockers.extend(host["source_binding"]["errors"])
    if not host["soundfont"]["ready"]:
        blockers.append(str(host["soundfont"].get("error") or "soundfont-unavailable"))
    service = service_status() if commands["systemctl"] else {}
    if service and not service.get("hardening_ready"):
        blockers.append("managed-dropin-not-effective")
    pipewire = run_capture(["systemctl", "--user", "is-active", "pipewire.service"])
    pipewire_active = pipewire.returncode == 0 and pipewire.stdout.strip() == "active"
    if not pipewire_active:
        blockers.append("pipewire-inactive")
    return {
        "schema_version": 1,
        "kind": "dauersong_live_doctor",
        "ready": not blockers,
        "blocking_reasons": blockers,
        "software": commands,
        "pipewire_active": pipewire_active,
        "host": host,
        "service": service,
    }


def full_status() -> dict[str, Any]:
    manifest = load_manifest()
    service = service_status()
    host = host_verification()
    stream = stream_status(service["main_pid"]) if service["active"] else {
        "found": False,
        "indexes": [],
        "max_volume_percent": None,
    }
    runtime_safe = (
        not service["active"]
        or (
            service["hardening_ready"]
            and host["ready"]
            and stream.get("found") is True
            and type(stream.get("max_volume_percent")) is int
            and stream["max_volume_percent"] <= 100
        )
    )
    return {
        "schema_version": 1,
        "kind": "dauersong_live_status",
        **service,
        "configured_stream_volume_percent": service["hardening"]["stream_volume_percent"],
        "runtime_max_seconds": service["hardening"]["runtime_max_seconds"],
        "managed_by": service["hardening"]["managed_by"],
        "stream": stream,
        "live": live_status_snapshot(manifest),
        "source_binding_ready": host["source_binding"]["ready"],
        "source_binding_errors": host["source_binding"]["errors"],
        "soundfont_ready": host["soundfont"]["ready"],
        "runtime_safe": runtime_safe,
    }


def verify_host_command() -> int:
    report = host_verification()
    if not report["ready"]:
        raise RuntimeError("Dauersong host verification failed: " + json.dumps(report, sort_keys=True))
    print(json.dumps({"state": "verified", "unit": UNIT_NAME}))
    return 0


def start_service() -> int:
    doctor = runtime_doctor()
    if not doctor["ready"]:
        raise RuntimeError("Dauersong start blocked: " + ", ".join(doctor["blocking_reasons"]))
    current = full_status()
    if current["active"]:
        raise RuntimeError("Dauersong is already active")
    manifest = load_manifest()
    live_path = legacy_root(manifest) / "ecosystem" / "live-status.json"
    try:
        before_mtime = live_path.lstat().st_mtime_ns
    except OSError:
        before_mtime = 0
    result = run_capture(["systemctl", "--user", "start", UNIT_NAME], timeout=START_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Dauersong systemd start failed")
    last: dict[str, Any] | None = None
    for _ in range(80):
        last = full_status()
        if last["active"] and last["hardening_ready"] and last["source_binding_ready"]:
            try:
                live_mtime = live_path.lstat().st_mtime_ns
            except OSError:
                live_mtime = 0
            if live_mtime > before_mtime:
                bounded = enforce_stream_volume(last["main_pid"], 100)
                confirmed = full_status()
                if confirmed["runtime_safe"]:
                    print(json.dumps({"state": "ready", "unit": UNIT_NAME, "stream": bounded}))
                    return 0
        if last.get("active_state") in {"failed", "inactive"}:
            break
        time.sleep(0.05)
    run_capture(["systemctl", "--user", "stop", UNIT_NAME])
    raise RuntimeError("Dauersong did not reach safe readiness: " + json.dumps(last or {}, sort_keys=True))


def stop_service() -> int:
    status = service_status()
    if not status["active"]:
        print(json.dumps({"state": "inactive", "unit": UNIT_NAME}))
        return 0
    result = run_capture(["systemctl", "--user", "stop", UNIT_NAME])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Dauersong stop failed")
    for _ in range(40):
        current = service_status()
        if not current["active"]:
            print(json.dumps({"state": "stopped", "unit": UNIT_NAME}))
            return 0
        time.sleep(0.05)
    raise RuntimeError("Dauersong did not stop cleanly")


def recover_service() -> int:
    status = service_status()
    if status["active"]:
        raise RuntimeError("Dauersong recovery requires an inactive service")
    result = run_capture(["systemctl", "--user", "reset-failed", UNIT_NAME])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Dauersong reset-failed failed")
    print(json.dumps({"state": "recovered", "unit": UNIT_NAME}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("doctor", "read-only host and effective service readiness"),
        ("status", "read current Dauersong state"),
        ("start", "start existing v9 through the hardened service"),
        ("stop", "stop the Dauersong service"),
        ("recover", "clear terminal failed state while inactive"),
        ("verify-host", "verify exact v9 sources and soundfont for ExecStartPre"),
    ):
        parser.add_parser(command, help=help_text)
    return parser


def main() -> int:
    command = build_parser().parse_args().command
    try:
        if command == "doctor":
            print(json.dumps(runtime_doctor(), indent=2, sort_keys=True))
            return 0
        if command == "status":
            print(json.dumps(full_status(), indent=2, sort_keys=True))
            return 0
        if command == "start":
            return start_service()
        if command == "stop":
            return stop_service()
        if command == "recover":
            return recover_service()
        if command == "verify-host":
            return verify_host_command()
        raise RuntimeError(f"unsupported command: {command}")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error), "unit": UNIT_NAME}), file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
