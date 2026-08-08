#!/usr/bin/env python3
"""Bounded product projection for hardened recording sessions.

This module deliberately does not capture audio and does not mutate routing.  It
projects the recorder's private state into a path-free product contract and
performs full artifact verification only when media playback is requested.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import stat
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
RECORDING_SESSION_PATH = ROOT / "scripts" / "recording_session.py"
SESSION_ID_RE = re.compile(r"[0-9a-f]{24}")
MAX_LIBRARY_ITEMS = 64
MAX_SCAN_ITEMS = 512


def _load_recording_session() -> Any:
    spec = importlib.util.spec_from_file_location(
        "audio_recording_product_session", RECORDING_SESSION_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Recorder-Modul kann nicht geladen werden.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REC = _load_recording_session()
DEFAULT_STATE_ROOT = REC.DEFAULT_STATE_ROOT


class RecordingProductError(RuntimeError):
    """Expected product-projection error."""


def _valid_hex(value: Any, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def _private_state_root(path: pathlib.Path) -> pathlib.Path | None:
    root = REC.lexical_absolute(path)
    if not root.exists() and not root.is_symlink():
        return None
    REC._check_directory_chain(root)
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise RecordingProductError("Recorderzustand ist nicht lesbar.") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RecordingProductError("Recorderzustand verletzt die private Zustandsgrenze.")
    return root


def _result_projection(result: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    if (
        result.get("schema_version") != 1
        or result.get("kind") != "audio_recording_result"
        or result.get("session_id") != spec.get("session_id")
        or result.get("plan_sha256") != spec.get("plan_sha256")
        or result.get("status") not in {"completed", "failed-preserved"}
        or not isinstance(result.get("reason"), str)
        or not result["reason"]
    ):
        raise RecordingProductError("Recordergebnis ist nicht revisionsgebunden lesbar.")
    projection: dict[str, Any] = {
        "status": result["status"],
        "reason": result["reason"],
    }
    if result["status"] == "completed":
        started_at = result.get("started_at")
        completed_at = result.get("completed_at")
        artifact = result.get("artifact")
        final_path = pathlib.Path(spec["paths"]["final"])
        try:
            REC._validate_binding_shape(
                artifact,
                expected_path=final_path,
                require_identity=True,
                detail_fields=REC.ARTIFACT_DETAIL_FIELDS,
            )
        except REC.RecordingError as exc:
            raise RecordingProductError("Take-Beleg ist strukturell ungültig.") from exc
        if (
            not REC._valid_timestamp(started_at)
            or not REC._valid_timestamp(completed_at)
            or not isinstance(artifact, dict)
            or artifact.get("channels") != spec["plan_identity"]["capture"]["channels"]
            or artifact.get("sample_rate_hz")
            != spec["plan_identity"]["capture"]["sample_rate_hz"]
            or artifact.get("bit_depth_container") != 32
            or isinstance(artifact.get("duration_seconds"), bool)
            or not isinstance(artifact.get("duration_seconds"), (int, float))
            or artifact["duration_seconds"] <= 0
        ):
            raise RecordingProductError("Take-Beleg enthält unplausible Metadaten.")
        projection.update(
            {
                "started_at": started_at,
                "completed_at": completed_at,
                "artifact": {
                    "sha256": artifact["sha256"],
                    "bytes": artifact["bytes"],
                    "channels": artifact["channels"],
                    "bit_depth_container": artifact["bit_depth_container"],
                    "sample_rate_hz": artifact["sample_rate_hz"],
                    "frames": artifact["frames"],
                    "duration_seconds": artifact["duration_seconds"],
                    "receipt_bound": True,
                    "current_bytes_verified": False,
                },
            }
        )
    else:
        timestamp = result.get("failed_at") or result.get("recovered_at")
        if timestamp is not None and not REC._valid_timestamp(timestamp):
            raise RecordingProductError("Recovery-Beleg enthält keinen gültigen Zeitpunkt.")
        projection["at"] = timestamp
    return projection


def _session_projection(
    state_root: pathlib.Path,
    session_id: str,
    *,
    active_session_id: str | None,
) -> dict[str, Any]:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise RecordingProductError("Ungültige Recorder-Sitzungs-ID.")
    try:
        paths, spec, state = REC._read_session(state_root, session_id)
    except REC.RecordingError as exc:
        raise RecordingProductError("Recorder-Sitzung ist nicht gebunden lesbar.") from exc
    result: dict[str, Any] | None = None
    projected_result: dict[str, Any] | None = None
    if paths["result"].exists() or paths["result"].is_symlink():
        try:
            result = REC._safe_json_read(paths["result"], require_private=True)
            projected_result = _result_projection(result, spec)
        except (REC.RecordingError, RecordingProductError) as exc:
            raise RecordingProductError("Recordergebnis ist nicht sicher lesbar.") from exc
    process = state.get("process")
    exact_alive = isinstance(process, dict) and REC._identity_matches(process)
    pid_alive = isinstance(process, dict) and REC._proc_identity(process.get("pid")) is not None
    if projected_result is not None:
        status = projected_result["status"]
        recovery_required = False
    elif exact_alive:
        status = "running"
        recovery_required = False
    elif pid_alive:
        status = "identity-mismatch"
        recovery_required = True
    else:
        status = "recovery-required"
        recovery_required = True

    identity = spec["plan_identity"]
    capture = identity["capture"]
    monitoring = identity["monitoring"]
    source = identity["source"]
    physical = identity["physical"]
    laboratory = identity["laboratory"]
    facts = physical.get("facts") if isinstance(physical, dict) else None
    facts = facts if isinstance(facts, dict) else {}
    resolved = laboratory.get("resolved") if isinstance(laboratory, dict) else None
    resolved = resolved if isinstance(resolved, list) else []
    final_name = pathlib.Path(spec["paths"]["final"]).name
    active_pointer = active_session_id == session_id
    return {
        "session_id": session_id,
        "session_type": identity["session_type"],
        "profile": identity["profile"],
        "name": final_name,
        "created_at": spec["created_at"],
        "started_at": state.get("started_at"),
        "status": status,
        "active": exact_alive,
        "active_pointer": active_pointer,
        "recovery_required": recovery_required,
        "cleanup_required": bool(active_pointer and projected_result is not None),
        "plan_sha256": spec["plan_sha256"],
        "capture": {
            "sample_rate_hz": capture["sample_rate_hz"],
            "sample_format": capture["sample_format"],
            "channels": capture["channels"],
            "maximum_duration_seconds": capture["maximum_duration_seconds"],
        },
        "monitoring": {
            "mode": monitoring["mode"],
            "endpoint": monitoring["endpoint"],
            "software_loopback": monitoring["software_loopback"],
            "level_claim": monitoring["level_claim"],
        },
        "source": {
            "bound": isinstance(source.get("identity"), dict),
            "identity_sha256": source.get("identity_sha256"),
            "sample_rate_hz": (source.get("identity") or {}).get("sample_rate_hz"),
            "sample_format": (source.get("identity") or {}).get("sample_format"),
            "channels": (source.get("identity") or {}).get("channels"),
        },
        "physical": {
            "rode_nt1a_connected": facts.get("rode_nt1a_connected"),
            "rode_nt1a_motu_input": facts.get("rode_nt1a_motu_input"),
            "motu_phantom_48v": facts.get("motu_phantom_48v"),
            "motu_input_gain_reference": bool(facts.get("motu_input_gain_reference")),
        },
        "laboratory": {
            "voice_level_measurement": "voice-level-measurement" in resolved,
        },
        "result": projected_result,
    }


def probe(
    *, state_root: pathlib.Path = DEFAULT_STATE_ROOT, session_id: str | None = None
) -> dict[str, Any]:
    root = _private_state_root(state_root)
    if root is None:
        if session_id is not None:
            raise RecordingProductError("Recorderzustand ist noch nicht angelegt.")
        return {
            "schema_version": 1,
            "kind": "audio_recording_product_probe",
            "status": "idle",
            "active_session_id": None,
            "session": None,
            "read_only": True,
        }
    active_session_id: str | None = None
    active = root / "active.json"
    if active.exists() or active.is_symlink():
        try:
            active_session_id = REC._read_active(root)
        except REC.RecordingError as exc:
            raise RecordingProductError("Aktiver Recorderzeiger ist nicht sicher lesbar.") from exc
    resolved_id = session_id or active_session_id
    if resolved_id is None:
        return {
            "schema_version": 1,
            "kind": "audio_recording_product_probe",
            "status": "idle",
            "active_session_id": None,
            "session": None,
            "read_only": True,
        }
    session = _session_projection(root, resolved_id, active_session_id=active_session_id)
    return {
        "schema_version": 1,
        "kind": "audio_recording_product_probe",
        "status": session["status"],
        "active_session_id": active_session_id,
        "session": session,
        "read_only": True,
    }


def library(
    *, state_root: pathlib.Path = DEFAULT_STATE_ROOT, limit: int = 24
) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIBRARY_ITEMS:
        raise RecordingProductError("Bibliothekslimit ist ungültig.")
    root = _private_state_root(state_root)
    if root is None:
        return {
            "schema_version": 1,
            "kind": "audio_recording_product_library",
            "items": [],
            "count": 0,
            "skipped_invalid": 0,
            "truncated": False,
            "read_only": True,
        }
    active_session_id: str | None = None
    active = root / "active.json"
    if active.exists() or active.is_symlink():
        try:
            active_session_id = REC._read_active(root)
        except REC.RecordingError as exc:
            raise RecordingProductError("Aktiver Recorderzeiger ist nicht sicher lesbar.") from exc
    candidates: list[tuple[int, str]] = []
    for entry in os.scandir(root):
        match = re.fullmatch(r"([0-9a-f]{24})\.spec\.json", entry.name)
        if match is None:
            continue
        if not entry.is_file(follow_symlinks=False):
            continue
        try:
            modified = entry.stat(follow_symlinks=False).st_mtime_ns
        except OSError:
            continue
        candidates.append((modified, match.group(1)))
        if len(candidates) > MAX_SCAN_ITEMS:
            raise RecordingProductError("Recorderzustand überschreitet das Scan-Limit.")
    candidates.sort(reverse=True)
    items: list[dict[str, Any]] = []
    skipped = 0
    more_eligible = False
    for _modified, candidate_id in candidates:
        try:
            item = _session_projection(
                root, candidate_id, active_session_id=active_session_id
            )
        except RecordingProductError:
            skipped += 1
            continue
        if item["session_type"] != "voice-recording":
            continue
        if len(items) < limit:
            items.append(item)
            continue
        more_eligible = True
        break
    return {
        "schema_version": 1,
        "kind": "audio_recording_product_library",
        "items": items,
        "count": len(items),
        "skipped_invalid": skipped,
        "truncated": more_eligible,
        "read_only": True,
    }


def verified_media(
    session_id: str, *, state_root: pathlib.Path = DEFAULT_STATE_ROOT
) -> dict[str, Any]:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise RecordingProductError("Ungültige Recorder-Sitzungs-ID.")
    root = _private_state_root(state_root)
    if root is None:
        raise RecordingProductError("Recorderzustand ist noch nicht angelegt.")
    try:
        status = REC.session_status(state_root=root, session_id=session_id)
    except REC.RecordingError as exc:
        raise RecordingProductError("Take ist nicht vollständig verifiziert lesbar.") from exc
    result = status.get("result")
    final = status.get("final")
    if (
        status.get("session_type") != "voice-recording"
        or status.get("status") != "completed"
        or not isinstance(result, dict)
        or result.get("status") != "completed"
        or not isinstance(final, dict)
        or "error" in final
        or not isinstance(result.get("artifact"), dict)
        or result["artifact"].get("sha256") != final.get("sha256")
        or result["artifact"].get("bytes") != final.get("bytes")
    ):
        raise RecordingProductError("Take ist nicht als finalisierte Sprachaufnahme gebunden.")
    return {
        "schema_version": 1,
        "kind": "audio_recording_product_media_binding",
        "session_id": session_id,
        "path": final["path"],
        "sha256": final["sha256"],
        "bytes": final["bytes"],
        "mode": final["mode"],
        "device": final["device"],
        "inode": final["inode"],
        "channels": result["artifact"]["channels"],
        "sample_rate_hz": result["artifact"]["sample_rate_hz"],
        "duration_seconds": result["artifact"]["duration_seconds"],
        "verified_current": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("probe", "library", "media"):
        item = sub.add_parser(name)
        item.add_argument("--state-root", type=pathlib.Path, default=DEFAULT_STATE_ROOT)
        if name in {"probe", "media"}:
            item.add_argument("--session-id")
        if name == "library":
            item.add_argument("--limit", type=int, default=24)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "probe":
            result = probe(state_root=args.state_root, session_id=args.session_id)
        elif args.command == "library":
            result = library(state_root=args.state_root, limit=args.limit)
        else:
            if not args.session_id:
                raise RecordingProductError("Media-Readback benötigt eine Sitzungs-ID.")
            result = verified_media(args.session_id, state_root=args.state_root)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, RecordingProductError, REC.RecordingError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_recording_product_error",
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
