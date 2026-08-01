#!/usr/bin/env python3
"""Fail-closed, reversible audio profile transitions.

The first executable transition is intentionally limited to ``desktop-mixed``.
It changes only the PipeWire default sink and the explicit force-rate/quantum
metadata keys. Every mutation is bound to a freshly recomputed plan hash,
recorded in a private atomic journal, verified by live readback, and reversible.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "audio-profiles.v1.json"
PLANNER_PATH = ROOT / "scripts" / "profile_planner.py"
DOCTOR_PATH = ROOT / "scripts" / "audio_doctor.py"
MAX_COMMAND_OUTPUT_BYTES = 65_536
MAX_JOURNAL_BYTES = 65_536
COMMAND_TIMEOUT_SECONDS = 5.0
READBACK_TIMEOUT_SECONDS = 3.0
LOCK_TIMEOUT_SECONDS = 2.0
POST_KILL_WAIT_SECONDS = 1.0
MAX_SINK_NAME_BYTES = 1_024
MAX_METADATA_DIGITS = 12
MOTU_M2_VENDOR_ID = "07fd"
MOTU_M2_PRODUCT_ID = "0008"
MOTU_M2_SERIAL_PREFIX = "MOTU_M2_"
MOTU_M2_NODE_PREFIX = "alsa_output.usb-MOTU_M2_"
COMMAND_PATHS = {
    "pactl": pathlib.Path("/usr/bin/pactl"),
    "pw-metadata": pathlib.Path("/usr/bin/pw-metadata"),
}
SUPPORTED_PROFILE = "desktop-mixed"
EXPECTED_APPLY_AUTHORITY = "desktop-mixed-transition-v1"
OPERATION_ID = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{16}$")
STATE_FIELDS = ("default_sink", "force_rate_hz", "force_quantum_frames")
TERMINAL_STATES = frozenset({"applied", "rolled-back", "failed-rolled-back"})
UNRESOLVED_STATES = frozenset(
    {"applying", "rollback-needed", "rolling-back", "rollback-blocked"}
)
JOURNAL_STATES = TERMINAL_STATES | UNRESOLVED_STATES
DEFAULT_STATE_ROOT = (
    pathlib.Path(os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state"))
    / "audio"
    / "profile-transitions-v1"
)


class TransitionError(RuntimeError):
    """Controlled public transition error."""

    def __init__(self, code: str, detail: str, exit_code: int = 2):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.exit_code = exit_code


Runner = Callable[[tuple[str, ...]], str]


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise TransitionError(
            "module-load-failed", "A required local audio module cannot be loaded."
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PLANNER = load_module("profile_transition_planner", PLANNER_PATH)
DOCTOR = load_module("profile_transition_doctor", DOCTOR_PATH)


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_payload(payload: Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def new_operation_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{secrets.token_hex(8)}"


def command_label(argv: tuple[str, ...]) -> str:
    if argv[:2] == ("pactl", "info"):
        return "read-default-sink"
    if argv[:4] == ("pactl", "--format=json", "list", "sinks"):
        return "read-sink-inventory"
    if argv[:2] == ("pactl", "set-default-sink"):
        return "set-default-sink"
    if argv[:4] == ("pw-metadata", "-n", "settings", "0"):
        return "set-or-read-pipewire-metadata"
    if argv[:5] == ("pw-metadata", "-n", "settings", "-d", "0"):
        return "delete-pipewire-metadata"
    return "audio-transition-command"


def valid_sink_argument(value: str) -> bool:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return bool(value) and (
        not value.startswith("-")
        and len(encoded) <= MAX_SINK_NAME_BYTES
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def valid_positive_decimal(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= MAX_METADATA_DIGITS
        and value.isascii()
        and value.isdigit()
        and int(value) > 0
    )


def validate_command_argv(argv: tuple[str, ...]) -> None:
    if argv in {
        ("pactl", "info"),
        ("pactl", "--format=json", "list", "sinks"),
        ("pw-metadata", "-n", "settings", "0"),
    }:
        return
    if (
        len(argv) == 3
        and argv[:2] == ("pactl", "set-default-sink")
        and valid_sink_argument(argv[2])
    ):
        return
    metadata_keys = {"clock.force-rate", "clock.force-quantum"}
    if (
        len(argv) == 6
        and argv[:4] == ("pw-metadata", "-n", "settings", "0")
        and argv[4] in metadata_keys
        and valid_positive_decimal(argv[5])
    ):
        return
    if (
        len(argv) == 6
        and argv[:5] == ("pw-metadata", "-n", "settings", "-d", "0")
        and argv[5] in metadata_keys
    ):
        return
    raise TransitionError(
        "command-not-allowed", "The requested audio transition command is not allowed."
    )


def run_command(argv: tuple[str, ...]) -> str:
    validate_command_argv(argv)
    executable = COMMAND_PATHS[argv[0]]
    resolved_argv = (str(executable), *argv[1:])
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - validated fixed argv contract
            resolved_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except FileNotFoundError as exc:
        raise TransitionError(
            "command-unavailable",
            f"Required command is unavailable: {command_label(argv)}.",
        ) from exc
    except OSError as exc:
        raise TransitionError(
            "command-unavailable",
            f"Required command cannot be started: {command_label(argv)}.",
        ) from exc

    assert process.stdout is not None and process.stderr is not None
    streams = {
        process.stdout: bytearray(),
        process.stderr: bytearray(),
    }
    selector = selectors.DefaultSelector()
    for stream in streams:
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS

    def kill_process_group() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            try:
                process.kill()
            except ProcessLookupError:
                return

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kill_process_group()
                raise TransitionError(
                    "command-timeout",
                    f"Command exceeded its fixed timeout: {command_label(argv)}.",
                    3,
                )
            events = selector.select(timeout=min(remaining, 0.1))
            if not events and process.poll() is not None:
                events = [
                    (key, selectors.EVENT_READ) for key in selector.get_map().values()
                ]
            for key, _mask in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 65_536)
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if len(streams[stream]) + len(chunk) > MAX_COMMAND_OUTPUT_BYTES:
                    kill_process_group()
                    raise TransitionError(
                        "command-output-limit",
                        f"Command exceeded its output limit: {command_label(argv)}.",
                        3,
                    )
                streams[stream].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            kill_process_group()
            raise TransitionError(
                "command-timeout",
                f"Command exceeded its fixed timeout: {command_label(argv)}.",
                3,
            )
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            kill_process_group()
            raise TransitionError(
                "command-timeout",
                f"Command exceeded its fixed timeout: {command_label(argv)}.",
                3,
            ) from exc
    finally:
        selector.close()
        for stream in streams:
            if not stream.closed:
                stream.close()
        if process.poll() is None:
            kill_process_group()
        try:
            process.wait(timeout=POST_KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            kill_process_group()
    if returncode != 0:
        raise TransitionError(
            "command-failed",
            f"Command failed with exit {returncode}: {command_label(argv)}.",
            3,
        )
    return bytes(streams[process.stdout]).decode("utf-8", errors="replace")


def load_catalog() -> dict[str, Any]:
    try:
        raw = PROFILE_PATH.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise TransitionError(
            "profile-catalog-unavailable",
            "The audio profile catalog cannot be read and validated.",
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise TransitionError(
            "profile-catalog-invalid",
            "The audio profile catalog has an unsupported root contract.",
        )
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        raise TransitionError(
            "profile-catalog-invalid",
            "The audio profile catalog has no profiles object.",
        )
    return payload


def validate_profile_contract(profile: str, catalog: dict[str, Any]) -> dict[str, Any]:
    if profile != SUPPORTED_PROFILE:
        raise TransitionError(
            "profile-not-executable",
            f"Only {SUPPORTED_PROFILE} has an executable transition contract.",
        )
    profiles = catalog["profiles"]
    spec = profiles.get(profile)
    if not isinstance(spec, dict):
        raise TransitionError(
            "profile-catalog-invalid", "The executable profile is missing."
        )
    desired = spec.get("desired")
    expected_desired = {
        "default_sink": "motu-m2",
        "rate_hz": 48_000,
        "quantum_frames": 1_024,
        "resampling": "allowed",
    }
    if desired != expected_desired:
        raise TransitionError(
            "profile-contract-drift",
            "The desktop-mixed desired state changed; review the transition engine first.",
        )
    if spec.get("apply_authority") != EXPECTED_APPLY_AUTHORITY:
        raise TransitionError(
            "profile-authority-mismatch",
            "The profile does not delegate apply authority to this transition engine.",
        )
    return spec


def parse_default_sink(text: str) -> str:
    labels = ("Default Sink", "Standard-Ziel")
    for line in text.splitlines():
        if any(line.startswith(f"{label}:") for label in labels):
            value = line.split(":", 1)[1].strip()
            if value:
                return value
    raise TransitionError(
        "default-sink-unreadable",
        "PipeWire/PulseAudio did not report an exact default sink.",
    )


def normalize_usb_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold().removeprefix("0x")
    return normalized if re.fullmatch(r"[0-9a-f]{4}", normalized) else None


def parse_sink_inventory(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransitionError(
            "sink-inventory-invalid",
            "PipeWire/PulseAudio reported an invalid JSON sink inventory.",
        ) from exc
    if not isinstance(payload, list):
        raise TransitionError(
            "sink-inventory-invalid",
            "PipeWire/PulseAudio reported a non-array sink inventory.",
        )
    sinks: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise TransitionError(
                "sink-inventory-invalid",
                "PipeWire/PulseAudio reported a non-object sink entry.",
            )
        name = item.get("name")
        properties = item.get("properties")
        if (
            not isinstance(name, str)
            or not valid_sink_argument(name)
            or not isinstance(properties, dict)
            or name in names
        ):
            raise TransitionError(
                "sink-inventory-invalid",
                "PipeWire/PulseAudio reported an invalid or duplicate sink identity.",
            )
        names.add(name)
        sinks.append({"name": name, "properties": properties})
    if not sinks:
        raise TransitionError(
            "sink-inventory-empty",
            "PipeWire/PulseAudio did not report any sinks.",
        )
    return sinks


def motu_m2_sink_projection(item: dict[str, Any]) -> dict[str, str] | None:
    properties = item["properties"]
    vendor_id = normalize_usb_id(properties.get("device.vendor.id"))
    product_id = normalize_usb_id(properties.get("device.product.id"))
    if vendor_id != MOTU_M2_VENDOR_ID or product_id != MOTU_M2_PRODUCT_ID:
        return None
    name = item["name"]
    serial = properties.get("device.serial")
    bus_path = properties.get("device.bus_path")
    if (
        not name.startswith(MOTU_M2_NODE_PREFIX)
        or not isinstance(serial, str)
        or not serial.startswith(MOTU_M2_SERIAL_PREFIX)
        or f"usb-{serial}-00" not in name
        or not isinstance(bus_path, str)
        or not bus_path
    ):
        raise TransitionError(
            "motu-sink-invalid",
            "A USB device with the MOTU M2 IDs has an invalid bound sink identity.",
        )
    identity = {
        "vendor_id": vendor_id,
        "product_id": product_id,
        "serial_sha256": hashlib.sha256(serial.encode("utf-8")).hexdigest(),
        "node_name_sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
        "bus_path_sha256": hashlib.sha256(bus_path.encode("utf-8")).hexdigest(),
    }
    return {"node_name": name, "identity_sha256": sha256_payload(identity)}


def parse_metadata_value(text: str, key: str) -> int | None:
    matches = re.findall(
        rf"key:'{re.escape(key)}'\s+value:'([^']*)'",
        text,
    )
    if not matches and f"key:'{key}'" not in text:
        return None
    if len(matches) != 1 or not valid_positive_decimal(matches[0]):
        raise TransitionError(
            "metadata-invalid",
            f"PipeWire reported an ambiguous or invalid value for {key}.",
        )
    return int(matches[0])


def read_live_state(runner: Runner = run_command) -> dict[str, Any]:
    default_sink = parse_default_sink(runner(("pactl", "info")))
    if not valid_sink_argument(default_sink):
        raise TransitionError(
            "default-sink-unreadable",
            "PipeWire/PulseAudio reported an invalid default sink identity.",
        )
    inventory = parse_sink_inventory(
        runner(("pactl", "--format=json", "list", "sinks"))
    )
    sink_names = [item["name"] for item in inventory]
    if default_sink not in sink_names:
        raise TransitionError(
            "default-sink-not-in-inventory",
            "The exact default sink is absent from the current sink inventory.",
        )
    metadata = runner(("pw-metadata", "-n", "settings", "0"))
    return {
        "default_sink": default_sink,
        "sinks": sink_names,
        "sink_inventory": inventory,
        "force_rate_hz": parse_metadata_value(metadata, "clock.force-rate"),
        "force_quantum_frames": parse_metadata_value(metadata, "clock.force-quantum"),
    }


def normalize_sink(name: str | None) -> str | None:
    return DOCTOR.normalize_endpoint(name)


def resolve_motu_sink(live: dict[str, Any]) -> dict[str, str]:
    candidates: list[dict[str, str]] = []
    for item in live["sink_inventory"]:
        candidate = motu_m2_sink_projection(item)
        if candidate is not None:
            candidates.append(candidate)
    if len(candidates) != 1:
        raise TransitionError(
            "motu-sink-ambiguous",
            "Exactly one USB-ID-, serial- and bus-bound MOTU M2 sink is required for desktop-mixed.",
        )
    return candidates[0]


def validate_target_sink_identity(plan: dict[str, Any], live: dict[str, Any]) -> None:
    candidate = resolve_motu_sink(live)
    if (
        candidate["node_name"] != plan["target"]["default_sink"]
        or candidate["identity_sha256"] != plan["target_sink_identity_sha256"]
    ):
        raise TransitionError(
            "motu-sink-changed",
            "The exact MOTU M2 sink identity changed after the transition plan was approved.",
            3,
        )


def readiness_projection(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile": plan.get("profile"),
        "profile_executable": plan.get("profile_executable"),
        "ready_for_laboratory_apply": plan.get("ready_for_laboratory_apply"),
        "apply_authority": plan.get("apply_authority"),
        "missing_hardware": plan.get("missing_hardware"),
        "missing_physical_facts": plan.get("missing_physical_facts"),
        "mismatched_physical_facts": plan.get("mismatched_physical_facts"),
        "unresolved_laboratory_gates": plan.get("unresolved_laboratory_gates"),
        "invalidated_laboratory_gates": plan.get("invalidated_laboratory_gates"),
        "incompatible_laboratory_gates": plan.get("incompatible_laboratory_gates"),
        "planned_graph_fingerprint": plan.get("planned_graph_fingerprint"),
    }


def read_readiness(
    profile: str,
    physical_state: pathlib.Path,
    gates_state: pathlib.Path,
) -> dict[str, Any]:
    try:
        plan = PLANNER.plan(profile, physical_state, gates_state)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TransitionError(
            "readiness-unavailable",
            "The read-only profile readiness plan could not be produced.",
        ) from exc
    projection = readiness_projection(plan)
    if not projection["profile_executable"]:
        raise TransitionError(
            "profile-not-executable",
            "The selected profile is not executable.",
        )
    if not projection["ready_for_laboratory_apply"]:
        blockers: list[str] = []
        for field in (
            "missing_hardware",
            "missing_physical_facts",
            "unresolved_laboratory_gates",
        ):
            value = projection.get(field)
            if isinstance(value, list):
                blockers.extend(str(item) for item in value)
        raise TransitionError(
            "profile-readiness-blocked",
            "The profile readiness gate is blocked"
            + (f": {', '.join(sorted(set(blockers)))}." if blockers else "."),
        )
    return projection


def private_state_projection(live: dict[str, Any]) -> dict[str, Any]:
    return {
        "default_sink": live["default_sink"],
        "force_rate_hz": live["force_rate_hz"],
        "force_quantum_frames": live["force_quantum_frames"],
    }


def field_value(state: dict[str, Any], field: str) -> Any:
    return state[field]


def metadata_set_argv(key: str, value: int) -> tuple[str, ...]:
    return ("pw-metadata", "-n", "settings", "0", key, str(value))


def metadata_rollback_argv(key: str, value: int | None) -> tuple[str, ...]:
    if value is None:
        return ("pw-metadata", "-n", "settings", "-d", "0", key)
    return metadata_set_argv(key, value)


def build_operations(
    before: dict[str, Any], target: dict[str, Any]
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    if before["force_rate_hz"] != target["force_rate_hz"]:
        operations.append(
            {
                "field": "force_rate_hz",
                "before": before["force_rate_hz"],
                "target": target["force_rate_hz"],
                "apply_argv": list(
                    metadata_set_argv("clock.force-rate", target["force_rate_hz"])
                ),
                "rollback_argv": list(
                    metadata_rollback_argv("clock.force-rate", before["force_rate_hz"])
                ),
            }
        )
    if before["force_quantum_frames"] != target["force_quantum_frames"]:
        operations.append(
            {
                "field": "force_quantum_frames",
                "before": before["force_quantum_frames"],
                "target": target["force_quantum_frames"],
                "apply_argv": list(
                    metadata_set_argv(
                        "clock.force-quantum", target["force_quantum_frames"]
                    )
                ),
                "rollback_argv": list(
                    metadata_rollback_argv(
                        "clock.force-quantum", before["force_quantum_frames"]
                    )
                ),
            }
        )
    if before["default_sink"] != target["default_sink"]:
        operations.append(
            {
                "field": "default_sink",
                "before": before["default_sink"],
                "target": target["default_sink"],
                "apply_argv": [
                    "pactl",
                    "set-default-sink",
                    target["default_sink"],
                ],
                "rollback_argv": [
                    "pactl",
                    "set-default-sink",
                    before["default_sink"],
                ],
            }
        )
    return operations


def build_plan(
    profile: str,
    physical_state: pathlib.Path,
    gates_state: pathlib.Path,
    runner: Runner = run_command,
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    catalog = load_catalog()
    spec = validate_profile_contract(profile, catalog)
    readiness_value = readiness or read_readiness(profile, physical_state, gates_state)
    if readiness_value.get("apply_authority") != EXPECTED_APPLY_AUTHORITY:
        raise TransitionError(
            "profile-authority-mismatch",
            "The read-only planner and transition authority disagree.",
        )
    live = read_live_state(runner)
    target_sink = resolve_motu_sink(live)
    before = private_state_projection(live)
    target = {
        "default_sink": target_sink["node_name"],
        "force_rate_hz": spec["desired"]["rate_hz"],
        "force_quantum_frames": spec["desired"]["quantum_frames"],
    }
    operations = build_operations(before, target)
    plan = {
        "schema_version": 1,
        "kind": "audio_profile_transition_plan_private",
        "profile": profile,
        "catalog_sha256": sha256_file(PROFILE_PATH),
        "readiness_sha256": sha256_payload(readiness_value),
        "target_sink_identity_sha256": target_sink["identity_sha256"],
        "before": before,
        "target": target,
        "operations": operations,
    }
    plan["plan_sha256"] = sha256_payload(plan)
    return plan


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "default_sink": normalize_sink(state["default_sink"]),
        "force_rate_hz": state["force_rate_hz"],
        "force_quantum_frames": state["force_quantum_frames"],
    }


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    changes = []
    for operation in plan["operations"]:
        before = operation["before"]
        target = operation["target"]
        if operation["field"] == "default_sink":
            before = normalize_sink(before)
            target = normalize_sink(target)
        changes.append(
            {
                "field": operation["field"],
                "from": before,
                "to": target,
            }
        )
    return {
        "schema_version": 1,
        "kind": "audio_profile_transition_diff",
        "profile": plan["profile"],
        "read_only": True,
        "plan_sha256": plan["plan_sha256"],
        "idempotent": not changes,
        "before": public_state(plan["before"]),
        "target": public_state(plan["target"]),
        "changes": changes,
        "confirmation_contract": (
            "Apply requires this exact plan_sha256 and recomputes the live plan "
            "under an exclusive transition lock."
        ),
        "does_not_establish": [
            "subjective-audio-quality",
            "bit-perfect-playback",
            "safe-listening-level",
            "recording-readiness",
        ],
    }


def ensure_state_root(root: pathlib.Path) -> pathlib.Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = root.lstat()
    except OSError as exc:
        raise TransitionError(
            "state-root-unavailable",
            "The private transition state root cannot be inspected.",
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TransitionError(
            "state-root-invalid",
            "The private transition state root must be a real directory.",
        )
    if info.st_uid != os.getuid():
        raise TransitionError(
            "state-root-invalid",
            "The private transition state root must belong to the current user.",
        )
    root.chmod(0o700)
    operations = root / "operations"
    operations.mkdir(mode=0o700, exist_ok=True)
    op_info = operations.lstat()
    if stat.S_ISLNK(op_info.st_mode) or not stat.S_ISDIR(op_info.st_mode):
        raise TransitionError(
            "state-root-invalid",
            "The private operation state path must be a real directory.",
        )
    if op_info.st_uid != os.getuid():
        raise TransitionError(
            "state-root-invalid",
            "The private operation state path must belong to the current user.",
        )
    operations.chmod(0o700)
    return operations


@contextlib.contextmanager
def transition_lock(root: pathlib.Path) -> Iterator[None]:
    ensure_state_root(root)
    lock_path = root / "transition.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise TransitionError(
            "transition-lock-unavailable",
            "The exclusive transition lock cannot be opened.",
        ) from exc
    acquired = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise TransitionError(
                "transition-lock-invalid",
                "The transition lock must be a current-user regular file.",
            )
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TransitionError(
                        "transition-busy",
                        "Another audio profile transition holds the local lock.",
                        3,
                    ) from None
                time.sleep(0.05)
        yield
    except OSError as exc:
        raise TransitionError(
            "transition-lock-unavailable",
            "The exclusive transition lock cannot be used safely.",
        ) from exc
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def journal_path(root: pathlib.Path, operation_id: str) -> pathlib.Path:
    if not OPERATION_ID.fullmatch(operation_id):
        raise TransitionError(
            "operation-id-invalid", "The transition operation ID is invalid."
        )
    return root / "operations" / f"{operation_id}.json"


def atomic_write_private(path: pathlib.Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_JOURNAL_BYTES:
        raise TransitionError(
            "journal-too-large", "The private transition journal exceeded its limit."
        )
    if path.is_symlink():
        raise TransitionError(
            "journal-path-invalid",
            "A private transition journal must not be a symlink.",
        )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".journal-", delete=False
        ) as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = pathlib.Path(handle.name)
        temporary.replace(path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def validate_private_state(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(STATE_FIELDS):
        raise TransitionError(
            "journal-invalid", f"The private {label} state contract is invalid."
        )
    sink = value.get("default_sink")
    if not isinstance(sink, str) or not valid_sink_argument(sink):
        raise TransitionError(
            "journal-invalid", f"The private {label} sink identity is invalid."
        )
    for field in ("force_rate_hz", "force_quantum_frames"):
        item = value.get(field)
        if item is not None and (type(item) is not int or item <= 0):
            raise TransitionError(
                "journal-invalid", f"The private {label} metadata value is invalid."
            )
    return value


def validate_journal_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise TransitionError(
            "journal-invalid", "The private transition journal is not a regular file."
        )
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise TransitionError(
            "journal-invalid", "The private transition journal must have mode 0600."
        )


def validate_journal_header(
    payload: dict[str, Any], operation_id: str
) -> dict[str, Any]:
    if payload.get("schema_version") != 1:
        raise TransitionError(
            "journal-invalid", "The private transition journal schema is unsupported."
        )
    if payload.get("kind") != "audio_profile_transition_journal":
        raise TransitionError(
            "journal-invalid", "The private transition journal kind is invalid."
        )
    if payload.get("operation_id") != operation_id:
        raise TransitionError(
            "journal-invalid", "The private transition journal identity does not match."
        )
    plan = payload.get("plan")
    if not isinstance(plan, dict):
        raise TransitionError(
            "journal-invalid", "The private transition journal has no bound plan."
        )
    return plan


def validate_plan_header(payload: dict[str, Any], plan: dict[str, Any]) -> None:
    expected_fields = {
        "schema_version",
        "kind",
        "profile",
        "catalog_sha256",
        "readiness_sha256",
        "target_sink_identity_sha256",
        "before",
        "target",
        "operations",
        "plan_sha256",
    }
    if set(plan) != expected_fields:
        raise TransitionError(
            "journal-invalid", "The private transition plan fields are invalid."
        )
    if plan.get("schema_version") != 1:
        raise TransitionError(
            "journal-invalid", "The private transition plan schema is invalid."
        )
    if plan.get("kind") != "audio_profile_transition_plan_private":
        raise TransitionError(
            "journal-invalid", "The private transition plan kind is invalid."
        )
    if plan.get("profile") != SUPPORTED_PROFILE:
        raise TransitionError(
            "journal-invalid", "The private transition profile is invalid."
        )
    if payload.get("profile") != plan.get("profile"):
        raise TransitionError(
            "journal-invalid", "The journal and transition profile disagree."
        )


def validate_plan_bindings(plan: dict[str, Any]) -> None:
    for field in (
        "catalog_sha256",
        "readiness_sha256",
        "target_sink_identity_sha256",
    ):
        value = plan.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise TransitionError(
                "journal-invalid", "The private transition plan binding is invalid."
            )


def validate_plan_operations(plan: dict[str, Any]) -> list[dict[str, Any]]:
    before = validate_private_state(plan.get("before"), "before")
    target = validate_private_state(plan.get("target"), "target")
    operations = plan.get("operations")
    expected = build_operations(before, target)
    if not isinstance(operations, list) or operations != expected:
        raise TransitionError(
            "journal-invalid", "The private transition operation contract is invalid."
        )
    return operations


def validate_plan_digest(payload: dict[str, Any], plan: dict[str, Any]) -> None:
    claimed = plan.get("plan_sha256")
    without_hash = dict(plan)
    without_hash.pop("plan_sha256", None)
    if not isinstance(claimed, str) or claimed != sha256_payload(without_hash):
        raise TransitionError(
            "journal-invalid", "The private transition plan digest does not verify."
        )
    if payload.get("plan_sha256") != claimed:
        raise TransitionError(
            "journal-invalid", "The journal and plan digests disagree."
        )


def validate_journal_progress(payload: dict[str, Any], operation_count: int) -> None:
    status = payload.get("status")
    if status not in JOURNAL_STATES:
        raise TransitionError(
            "journal-invalid", "The private transition journal status is invalid."
        )
    completed = payload.get("completed_indices")
    if not isinstance(completed, list) or len(completed) != len(set(completed)):
        raise TransitionError(
            "journal-invalid", "The private transition completion set is invalid."
        )
    if any(
        type(index) is not int or index < 0 or index >= operation_count
        for index in completed
    ):
        raise TransitionError(
            "journal-invalid", "The private transition completion set is invalid."
        )
    if completed != list(range(len(completed))):
        raise TransitionError(
            "journal-invalid",
            "The private transition completion set must be a contiguous prefix.",
        )
    active = payload.get("active_index")
    if active is not None and (
        type(active) is not int or active < 0 or active >= operation_count
    ):
        raise TransitionError(
            "journal-invalid", "The private transition active operation is invalid."
        )
    if status == "applying" and active is not None and active != len(completed):
        raise TransitionError(
            "journal-invalid",
            "The active apply operation does not follow completed operations.",
        )
    if status == "applied" and completed != list(range(operation_count)):
        raise TransitionError(
            "journal-invalid",
            "An applied transition must bind every planned operation.",
        )


def validate_journal(
    info: os.stat_result, payload: dict[str, Any], operation_id: str
) -> None:
    validate_journal_file(info)
    plan = validate_journal_header(payload, operation_id)
    validate_plan_header(payload, plan)
    validate_plan_bindings(plan)
    operations = validate_plan_operations(plan)
    validate_plan_digest(payload, plan)
    validate_journal_progress(payload, len(operations))


def stat_fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def open_journal_descriptor(path: pathlib.Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except FileNotFoundError as exc:
        raise TransitionError(
            "operation-not-found", "The requested transition operation was not found."
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise TransitionError(
                "operation-not-found",
                "The requested transition operation was not found.",
            ) from exc
        raise TransitionError(
            "journal-invalid", "The private transition journal cannot be opened."
        ) from exc


def read_descriptor_bytes(descriptor: int) -> tuple[bytes, os.stat_result]:
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_JOURNAL_BYTES:
            raise TransitionError(
                "journal-invalid", "The private transition journal file is invalid."
            )
        chunks: list[bytes] = []
        remaining = MAX_JOURNAL_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise TransitionError(
            "journal-invalid", "The private transition journal cannot be read."
        ) from exc
    if len(encoded) > MAX_JOURNAL_BYTES:
        raise TransitionError(
            "journal-invalid", "The private transition journal is too large."
        )
    if stat_fingerprint(before) != stat_fingerprint(after):
        raise TransitionError(
            "journal-changed-during-read",
            "The private transition journal changed while it was being read.",
        )
    return encoded, after


def decode_journal(encoded: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransitionError(
            "journal-invalid", "The private transition journal is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise TransitionError(
            "journal-invalid", "The private transition journal root is invalid."
        )
    return payload


def read_journal(root: pathlib.Path, operation_id: str) -> dict[str, Any]:
    descriptor = open_journal_descriptor(journal_path(root, operation_id))
    try:
        encoded, info = read_descriptor_bytes(descriptor)
    finally:
        os.close(descriptor)
    payload = decode_journal(encoded)
    validate_journal(info, payload, operation_id)
    return payload


def write_journal(root: pathlib.Path, journal: dict[str, Any]) -> None:
    journal["updated_at"] = utc_now()
    path = journal_path(root, journal["operation_id"])
    atomic_write_private(path, journal)


def list_journal_ids(root: pathlib.Path) -> list[str]:
    if not root.exists():
        return []
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise TransitionError(
            "state-root-invalid",
            "The private transition state root must be a real directory.",
        )
    operations = root / "operations"
    if not operations.exists():
        return []
    operations_info = operations.lstat()
    if stat.S_ISLNK(operations_info.st_mode) or not stat.S_ISDIR(
        operations_info.st_mode
    ):
        raise TransitionError(
            "state-root-invalid",
            "The private operation state path must be a real directory.",
        )
    result: list[str] = []
    for path in operations.iterdir():
        if path.suffix != ".json":
            continue
        if OPERATION_ID.fullmatch(path.stem):
            result.append(path.stem)
    return sorted(result, reverse=True)


def latest_journal(
    root: pathlib.Path, statuses: set[str] | frozenset[str] | None = None
) -> dict[str, Any] | None:
    for operation_id in list_journal_ids(root):
        journal = read_journal(root, operation_id)
        if statuses is None or journal["status"] in statuses:
            return journal
    return None


def fields_match(
    actual: dict[str, Any],
    expected: dict[str, Any],
    fields: list[str] | tuple[str, ...],
) -> bool:
    return all(actual[field] == expected[field] for field in fields)


def state_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return fields_match(actual, expected, STATE_FIELDS)


def wait_for_fields(
    expected: dict[str, Any],
    fields: list[str] | tuple[str, ...],
    runner: Runner,
    timeout: float = READBACK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        latest = private_state_projection(read_live_state(runner))
        if fields_match(latest, expected, fields):
            return latest
        if time.monotonic() >= deadline:
            raise TransitionError(
                "postcondition-failed",
                "The live audio state did not reach the expected transition state.",
                3,
            )
        time.sleep(0.05)


def wait_for_state(
    expected: dict[str, Any],
    runner: Runner,
    timeout: float = READBACK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return wait_for_fields(expected, STATE_FIELDS, runner, timeout)


def affected_fields(plan: dict[str, Any]) -> list[str]:
    return [operation["field"] for operation in plan["operations"]]


def safe_rollback_preflight(
    current: dict[str, Any],
    before: dict[str, Any],
    target: dict[str, Any],
    fields: list[str],
) -> None:
    drifted = [
        field
        for field in fields
        if field_value(current, field)
        not in {field_value(before, field), field_value(target, field)}
    ]
    if drifted:
        raise TransitionError(
            "rollback-drift-conflict",
            "Rollback is blocked because affected live fields changed outside this operation: "
            + ", ".join(sorted(drifted))
            + ".",
            3,
        )


def rollback_indices(journal: dict[str, Any]) -> list[int]:
    values = set(journal["completed_indices"])
    if journal.get("active_index") is not None:
        values.add(journal["active_index"])
    return sorted(values, reverse=True)


def mark_rollback_blocked(
    root: pathlib.Path, journal: dict[str, Any], error: TransitionError
) -> None:
    journal["status"] = "rollback-blocked"
    journal["active_index"] = None
    journal["error"] = {"code": error.code, "detail": error.detail}
    write_journal(root, journal)


def rollback_journal(
    root: pathlib.Path,
    journal: dict[str, Any],
    runner: Runner,
    final_status: str,
) -> dict[str, Any]:
    plan = journal["plan"]
    current = private_state_projection(read_live_state(runner))
    indices = rollback_indices(journal)
    fields = [plan["operations"][index]["field"] for index in indices]
    try:
        safe_rollback_preflight(current, plan["before"], plan["target"], fields)
    except TransitionError as exc:
        mark_rollback_blocked(root, journal, exc)
        raise
    journal["status"] = "rolling-back"
    journal["active_index"] = None
    write_journal(root, journal)
    for index in indices:
        operation = plan["operations"][index]
        current = private_state_projection(read_live_state(runner))
        try:
            safe_rollback_preflight(
                current,
                plan["before"],
                plan["target"],
                [operation["field"]],
            )
        except TransitionError as exc:
            mark_rollback_blocked(root, journal, exc)
            raise
        if field_value(current, operation["field"]) == operation["before"]:
            continue
        journal["active_index"] = index
        write_journal(root, journal)
        runner(tuple(operation["rollback_argv"]))
        expected = dict(current)
        expected[operation["field"]] = operation["before"]
        wait_for_state(expected, runner)
        journal["active_index"] = None
        write_journal(root, journal)
    wait_for_fields(plan["before"], fields, runner)
    journal["status"] = final_status
    journal["active_index"] = None
    journal["rolled_back_at"] = utc_now()
    write_journal(root, journal)
    return journal


def ensure_no_unresolved(root: pathlib.Path) -> None:
    pending = latest_journal(root, UNRESOLVED_STATES)
    if pending is not None:
        raise TransitionError(
            "recovery-required",
            "An unresolved transition exists; run recover before a new apply.",
            3,
        )


def validate_requested_plan_sha256(value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise TransitionError(
            "plan-digest-invalid", "The required plan SHA-256 is invalid."
        )


def already_applied_result(
    state_root: pathlib.Path,
    profile: str,
    expected_plan_sha256: str,
    runner: Runner,
) -> dict[str, Any] | None:
    previous = latest_journal(state_root, {"applied"})
    if previous is None or previous["profile"] != profile:
        return None
    if previous["plan_sha256"] != expected_plan_sha256:
        return None
    if previous["plan"]["catalog_sha256"] != sha256_file(PROFILE_PATH):
        return None
    current = private_state_projection(read_live_state(runner))
    if not state_matches(current, previous["plan"]["target"]):
        return None
    return {
        "schema_version": 1,
        "kind": "audio_profile_transition_result",
        "profile": profile,
        "operation_id": previous["operation_id"],
        "status": "already-applied",
        "plan_sha256": expected_plan_sha256,
        "mutated": False,
        "state": public_state(previous["plan"]["target"]),
        "rollback_available": True,
    }


def validate_fresh_plan(
    plan: dict[str, Any], expected_plan_sha256: str, runner: Runner
) -> None:
    if plan["plan_sha256"] != expected_plan_sha256:
        raise TransitionError(
            "plan-changed",
            "The live transition plan changed; inspect a fresh diff before applying.",
            3,
        )
    live = read_live_state(runner)
    validate_target_sink_identity(plan, live)
    current = private_state_projection(live)
    if not state_matches(current, plan["before"]):
        raise TransitionError(
            "plan-changed",
            "The live audio state changed immediately before mutation.",
            3,
        )


def unchanged_result(profile: str, plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "audio_profile_transition_result",
        "profile": profile,
        "status": "unchanged",
        "plan_sha256": plan["plan_sha256"],
        "mutated": False,
        "state": public_state(plan["target"]),
    }


def new_journal(profile: str, plan: dict[str, Any]) -> dict[str, Any]:
    timestamp = utc_now()
    return {
        "schema_version": 1,
        "kind": "audio_profile_transition_journal",
        "operation_id": new_operation_id(),
        "profile": profile,
        "plan_sha256": plan["plan_sha256"],
        "plan": plan,
        "created_at": timestamp,
        "updated_at": timestamp,
        "status": "applying",
        "completed_indices": [],
        "active_index": None,
        "error": None,
    }


def expected_apply_state(journal: dict[str, Any]) -> dict[str, Any]:
    plan = journal["plan"]
    expected = dict(plan["before"])
    for index in journal["completed_indices"]:
        operation = plan["operations"][index]
        expected[operation["field"]] = operation["target"]
    return expected


def apply_operation(
    root: pathlib.Path,
    journal: dict[str, Any],
    index: int,
    runner: Runner,
) -> None:
    plan = journal["plan"]
    operation = plan["operations"][index]
    expected = expected_apply_state(journal)
    live = read_live_state(runner)
    validate_target_sink_identity(plan, live)
    current = private_state_projection(live)
    if not state_matches(current, expected):
        raise TransitionError(
            "concurrent-audio-drift",
            "The live audio state changed between transition steps.",
            3,
        )
    journal["active_index"] = index
    write_journal(root, journal)
    runner(tuple(operation["apply_argv"]))
    expected[operation["field"]] = operation["target"]
    wait_for_state(expected, runner)
    validate_target_sink_identity(plan, read_live_state(runner))
    journal["completed_indices"].append(index)
    journal["active_index"] = None
    write_journal(root, journal)


def execute_apply(root: pathlib.Path, journal: dict[str, Any], runner: Runner) -> None:
    operations = journal["plan"]["operations"]
    for index in range(len(operations)):
        apply_operation(root, journal, index, runner)
    wait_for_state(journal["plan"]["target"], runner)


def rollback_failed_apply(
    root: pathlib.Path,
    journal: dict[str, Any],
    runner: Runner,
    cause: TransitionError,
) -> None:
    journal["status"] = "rollback-needed"
    journal["error"] = {"code": cause.code, "detail": cause.detail}
    write_journal(root, journal)
    rollback_journal(
        root,
        journal,
        runner,
        final_status="failed-rolled-back",
    )


def complete_apply(root: pathlib.Path, journal: dict[str, Any]) -> dict[str, Any]:
    plan = journal["plan"]
    journal["status"] = "applied"
    journal["active_index"] = None
    journal["applied_at"] = utc_now()
    write_journal(root, journal)
    return {
        "schema_version": 1,
        "kind": "audio_profile_transition_result",
        "profile": journal["profile"],
        "operation_id": journal["operation_id"],
        "status": "applied",
        "plan_sha256": plan["plan_sha256"],
        "mutated": True,
        "state": public_state(plan["target"]),
        "rollback_available": True,
    }


def apply_plan(
    profile: str,
    expected_plan_sha256: str,
    physical_state: pathlib.Path,
    gates_state: pathlib.Path,
    state_root: pathlib.Path,
    runner: Runner = run_command,
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_requested_plan_sha256(expected_plan_sha256)
    with transition_lock(state_root):
        ensure_no_unresolved(state_root)
        validate_profile_contract(profile, load_catalog())
        previous = already_applied_result(
            state_root, profile, expected_plan_sha256, runner
        )
        if previous is not None:
            return previous
        plan = build_plan(
            profile,
            physical_state,
            gates_state,
            runner,
            readiness=readiness,
        )
        validate_fresh_plan(plan, expected_plan_sha256, runner)
        if not plan["operations"]:
            return unchanged_result(profile, plan)
        journal = new_journal(profile, plan)
        write_journal(state_root, journal)
        try:
            execute_apply(state_root, journal, runner)
        except TransitionError as exc:
            rollback_failed_apply(state_root, journal, runner, exc)
            raise TransitionError(
                "apply-failed-rolled-back",
                "Apply failed; the bound affected fields were restored.",
                3,
            ) from exc
        return complete_apply(state_root, journal)


def rollback_operation(
    state_root: pathlib.Path,
    operation_id: str | None,
    runner: Runner = run_command,
) -> dict[str, Any]:
    with transition_lock(state_root):
        latest_applied = latest_journal(state_root, {"applied"})
        journal = (
            read_journal(state_root, operation_id) if operation_id else latest_applied
        )
        if journal is None:
            raise TransitionError(
                "rollback-unavailable",
                "No applied transition is available to roll back.",
            )
        if journal["status"] != "applied":
            raise TransitionError(
                "rollback-unavailable",
                "Only an applied transition can be explicitly rolled back.",
            )
        if (
            latest_applied is None
            or latest_applied["operation_id"] != journal["operation_id"]
        ):
            raise TransitionError(
                "rollback-superseded",
                "The requested transition was superseded; roll back the latest applied operation first.",
                3,
            )
        starting = private_state_projection(read_live_state(runner))
        rollback_journal(state_root, journal, runner, final_status="rolled-back")
        ending = private_state_projection(read_live_state(runner))
        changed = any(
            starting[field] != ending[field]
            for field in affected_fields(journal["plan"])
        )
        return {
            "schema_version": 1,
            "kind": "audio_profile_transition_result",
            "profile": journal["profile"],
            "operation_id": journal["operation_id"],
            "status": "rolled-back",
            "mutated": changed,
            "state": public_state(ending),
        }


def recover(
    state_root: pathlib.Path,
    runner: Runner = run_command,
) -> dict[str, Any]:
    with transition_lock(state_root):
        journal = latest_journal(state_root, UNRESOLVED_STATES)
        if journal is None:
            return {
                "schema_version": 1,
                "kind": "audio_profile_transition_recovery",
                "status": "nothing-to-recover",
                "mutated": False,
            }
        current = private_state_projection(read_live_state(runner))
        plan = journal["plan"]
        if journal["status"] == "applying" and state_matches(current, plan["target"]):
            journal["status"] = "applied"
            journal["completed_indices"] = list(range(len(plan["operations"])))
            journal["active_index"] = None
            journal["applied_at"] = utc_now()
            write_journal(state_root, journal)
            return {
                "schema_version": 1,
                "kind": "audio_profile_transition_recovery",
                "operation_id": journal["operation_id"],
                "status": "recovered-as-applied",
                "mutated": False,
                "state": public_state(plan["target"]),
            }
        starting = private_state_projection(read_live_state(runner))
        rollback_journal(
            state_root,
            journal,
            runner,
            final_status="failed-rolled-back",
        )
        ending = private_state_projection(read_live_state(runner))
        changed = any(
            starting[field] != ending[field]
            for field in affected_fields(journal["plan"])
        )
        return {
            "schema_version": 1,
            "kind": "audio_profile_transition_recovery",
            "operation_id": journal["operation_id"],
            "status": "recovered-by-rollback",
            "mutated": changed,
            "state": public_state(ending),
        }


def status(state_root: pathlib.Path) -> dict[str, Any]:
    journal = latest_journal(state_root)
    if journal is None:
        return {
            "schema_version": 1,
            "kind": "audio_profile_transition_status",
            "status": "no-operations",
            "recovery_required": False,
        }
    return {
        "schema_version": 1,
        "kind": "audio_profile_transition_status",
        "operation_id": journal["operation_id"],
        "profile": journal["profile"],
        "status": journal["status"],
        "plan_sha256": journal["plan_sha256"],
        "created_at": journal["created_at"],
        "updated_at": journal["updated_at"],
        "recovery_required": journal["status"] in UNRESOLVED_STATES,
        "attention_required": journal["status"] == "rollback-blocked",
        "rollback_available": journal["status"] == "applied",
        "private_journal": True,
    }


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--state-root", type=pathlib.Path, default=DEFAULT_STATE_ROOT)
    result.add_argument(
        "--physical-state",
        type=pathlib.Path,
        default=PLANNER.PHYSICAL.DEFAULT_STATE,
    )
    result.add_argument(
        "--gates",
        type=pathlib.Path,
        default=PLANNER.LABORATORY.DEFAULT_STATE,
    )
    sub = result.add_subparsers(dest="command", required=True)
    diff_parser = sub.add_parser("diff")
    diff_parser.add_argument("profile")
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("profile")
    apply_parser.add_argument("--plan-sha256", required=True)
    rollback_parser = sub.add_parser("rollback")
    rollback_parser.add_argument("--operation-id")
    sub.add_parser("recover")
    sub.add_parser("status")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "diff":
            emit(
                public_plan(
                    build_plan(
                        args.profile,
                        args.physical_state,
                        args.gates,
                    )
                )
            )
        elif args.command == "apply":
            emit(
                apply_plan(
                    args.profile,
                    args.plan_sha256,
                    args.physical_state,
                    args.gates,
                    args.state_root,
                )
            )
        elif args.command == "rollback":
            emit(
                rollback_operation(
                    args.state_root,
                    args.operation_id,
                )
            )
        elif args.command == "recover":
            emit(recover(args.state_root))
        elif args.command == "status":
            emit(status(args.state_root))
        else:
            raise AssertionError("unreachable command")
    except TransitionError as exc:
        emit(
            {
                "schema_version": 1,
                "kind": "audio_profile_transition_error",
                "error_code": exc.code,
                "detail": exc.detail,
            }
        )
        return exc.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
