#!/usr/bin/env python3
"""Recover the serial-bound MOTU desktop sink after Qobuz ALSA Direct use."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

MOTU_USB_ID = "07fd:0008"
MOTU_VENDOR_ID = "07fd"
MOTU_PRODUCT_ID = "0008"
MOTU_SERIAL_PREFIX = "MOTU_M2_"
REQUIRED_SERVICES = (
    "pipewire.service",
    "pipewire-pulse.service",
    "wireplumber.service",
)
WIREPLUMBER_UNIT = "wireplumber.service"
LEVEL_OBSERVER_UNIT = "audio-control-level-observer-v1.service"
PIPEWIRE_EXECUTABLE = "/usr/bin/pipewire"
POLL_SECONDS = 10.0
READBACK_ATTEMPTS = 6
READBACK_INTERVAL_SECONDS = 1.0
ABSENCE_STABILIZATION_SECONDS = 1.0
QUIESCE_CLOSE_ATTEMPTS = 7
SUCCESS_COOLDOWN_SECONDS = 120.0
FAILURE_BACKOFF_BASE_SECONDS = 30.0
FAILURE_BACKOFF_MAX_SECONDS = 900.0
COMMAND_TIMEOUT_SECONDS = 5.0
MAX_COMMAND_OUTPUT_BYTES = 1_048_576
MAX_PROC_FILE_BYTES = 4_096
MAX_SYSFS_FILE_BYTES = 1_024
MAX_CARDS = 32
MAX_SUBSTREAMS = 64
UNITY_VOLUME = 65_536
STATE_SCHEMA_VERSION = 2


class RecoveryError(RuntimeError):
    """A fail-closed observation or bounded recovery failure."""


Runner = Callable[[tuple[str, ...]], str]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class PhysicalMotu:
    card: pathlib.Path
    usb_serial: str
    pipewire_serial: str
    bus_path: str

    @property
    def serial_sha256(self) -> str:
        return hashlib.sha256(self.usb_serial.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SinkIdentity:
    name: str
    serial: str
    bus_path: str


def _valid_sink_name(value: str) -> bool:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        bool(value)
        and len(encoded) <= 1_024
        and not value.startswith("-")
        and all(31 < ord(character) < 127 for character in value)
    )


def _validate_command(argv: tuple[str, ...]) -> None:
    if argv in {
        ("pactl", "--format=json", "list", "sinks"),
        ("pactl", "info"),
        ("systemctl", "--user", "restart", WIREPLUMBER_UNIT),
        ("systemctl", "--user", "stop", LEVEL_OBSERVER_UNIT),
        ("systemctl", "--user", "start", LEVEL_OBSERVER_UNIT),
        (
            "systemctl",
            "--user",
            "show",
            LEVEL_OBSERVER_UNIT,
            "--property=ActiveState",
            "--value",
        ),
    }:
        return
    if (
        len(argv) == 4
        and argv[:3] == ("systemctl", "--user", "is-active")
        and argv[3] in REQUIRED_SERVICES
    ):
        return
    if len(argv) == 4 and argv[:2] == ("pactl", "set-sink-volume"):
        if _valid_sink_name(argv[2]) and argv[3] == "100%":
            return
    if len(argv) == 4 and argv[:2] == ("pactl", "set-sink-mute"):
        if _valid_sink_name(argv[2]) and argv[3] == "0":
            return
    if len(argv) == 3 and argv[:2] == ("pactl", "set-default-sink"):
        if _valid_sink_name(argv[2]):
            return
    raise RecoveryError("command-not-allowed")


def run_command(argv: tuple[str, ...]) -> str:
    _validate_command(argv)
    executable = pathlib.Path("/usr/bin") / argv[0]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed, validated argv contract
            (str(executable), *argv[1:]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError(f"command-unavailable:{argv[0]}") from exc
    if (
        len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES
        or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES
    ):
        raise RecoveryError(f"command-output-limit:{argv[0]}")
    if completed.returncode != 0:
        raise RecoveryError(f"command-failed:{argv[0]}")
    try:
        return completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RecoveryError(f"command-output-invalid:{argv[0]}") from exc


def _read_bounded_text(path: pathlib.Path, limit: int, error: str) -> str:
    try:
        with path.open("rb") as handle:
            payload = handle.read(limit + 1)
    except OSError as exc:
        raise RecoveryError(error) from exc
    if len(payload) > limit:
        raise RecoveryError(error)
    try:
        return payload.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise RecoveryError(error) from exc


def _read_proc_text(path: pathlib.Path) -> str:
    return _read_bounded_text(path, MAX_PROC_FILE_BYTES, "alsa-proc-unreadable")


def _read_sysfs_text(path: pathlib.Path, *, optional: bool = False) -> str | None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if optional:
            return None
        raise RecoveryError("motu-sysfs-identity-unreadable") from None
    except OSError as exc:
        raise RecoveryError("motu-sysfs-identity-unreadable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RecoveryError("motu-sysfs-identity-unreadable")
    return _read_bounded_text(
        path, MAX_SYSFS_FILE_BYTES, "motu-sysfs-identity-unreadable"
    )


def _inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _pipewire_bus_path(
    device: pathlib.Path,
    usb_parent: pathlib.Path,
    devices_root: pathlib.Path,
) -> str:
    """Map one canonical PCI-backed USB sysfs path to PipeWire bus syntax."""
    interface_matches: list[re.Match[str]] = []
    interface_path: pathlib.Path | None = None
    for candidate in (device, *device.parents):
        if candidate == usb_parent:
            break
        match = re.fullmatch(
            r"([0-9]+)-([0-9]+(?:\.[0-9]+)*):([0-9]+)\.([0-9]+)",
            candidate.name,
        )
        if match is not None:
            interface_matches.append(match)
            interface_path = candidate
    if (
        len(interface_matches) != 1
        or interface_path is None
        or interface_path.parent != usb_parent
    ):
        raise RecoveryError("motu-usb-bus-path-invalid")
    interface = interface_matches[0]

    usb_device = re.fullmatch(
        r"([0-9]+)-([0-9]+(?:\.[0-9]+)*)", usb_parent.name
    )
    if (
        usb_device is None
        or interface.group(1) != usb_device.group(1)
        or interface.group(2) != usb_device.group(2)
    ):
        raise RecoveryError("motu-usb-bus-path-invalid")

    usb_roots = [
        candidate
        for candidate in usb_parent.parents
        if _inside(candidate, devices_root)
        and re.fullmatch(r"usb[0-9]+", candidate.name)
    ]
    if len(usb_roots) != 1:
        raise RecoveryError("motu-usb-bus-path-invalid")
    usb_root = usb_roots[0]
    if usb_root.name[3:] != usb_device.group(1):
        raise RecoveryError("motu-usb-bus-path-invalid")

    controller = re.fullmatch(
        r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]",
        usb_root.parent.name,
    )
    if controller is None:
        raise RecoveryError("motu-usb-bus-path-invalid")

    ports = usb_device.group(2).split(".")
    expected_chain = tuple(
        f"{usb_device.group(1)}-{'.'.join(ports[:index])}"
        for index in range(1, len(ports) + 1)
    )
    try:
        observed_chain = usb_parent.relative_to(usb_root).parts
    except ValueError as exc:
        raise RecoveryError("motu-usb-bus-path-invalid") from exc
    if observed_chain != expected_chain:
        raise RecoveryError("motu-usb-bus-path-invalid")

    return (
        f"pci-{controller.group(0).casefold()}-usb-0:{usb_device.group(2)}:"
        f"{interface.group(3)}.{interface.group(4)}"
    )


def _physical_identity(
    card: pathlib.Path,
    *,
    sound_class_root: pathlib.Path,
    sys_devices_root: pathlib.Path,
) -> PhysicalMotu | None:
    try:
        device = (sound_class_root / card.name / "device").resolve(strict=True)
        devices_root = sys_devices_root.resolve(strict=True)
    except OSError as exc:
        raise RecoveryError("motu-sysfs-binding-unreadable") from exc
    if not _inside(device, devices_root):
        raise RecoveryError("motu-sysfs-binding-invalid")

    usb_parent: pathlib.Path | None = None
    for candidate in (device, *device.parents):
        if not _inside(candidate, devices_root):
            break
        vendor = _read_sysfs_text(candidate / "idVendor", optional=True)
        product = _read_sysfs_text(candidate / "idProduct", optional=True)
        if vendor is not None or product is not None:
            if vendor is None or product is None:
                raise RecoveryError("motu-sysfs-binding-invalid")
            usb_parent = candidate
            break
    if usb_parent is None:
        return None
    vendor = (_read_sysfs_text(usb_parent / "idVendor") or "").casefold()
    product = (_read_sysfs_text(usb_parent / "idProduct") or "").casefold()
    if vendor != MOTU_VENDOR_ID or product != MOTU_PRODUCT_ID:
        return None
    serial = _read_sysfs_text(usb_parent / "serial") or ""
    if (
        not serial
        or len(serial.encode("utf-8")) > 256
        or any(ord(character) < 33 or ord(character) > 126 for character in serial)
    ):
        raise RecoveryError("motu-usb-serial-invalid")

    bus_path = _pipewire_bus_path(device, usb_parent, devices_root)
    return PhysicalMotu(
        card=card,
        usb_serial=serial,
        pipewire_serial=f"{MOTU_SERIAL_PREFIX}{serial}",
        bus_path=bus_path,
    )


def resolve_unique_motu_card(
    asound_root: pathlib.Path,
    *,
    sound_class_root: pathlib.Path,
    sys_devices_root: pathlib.Path,
) -> PhysicalMotu | None:
    try:
        cards = sorted(
            (
                path
                for path in asound_root.iterdir()
                if re.fullmatch(r"card[0-9]+", path.name) and path.is_dir()
            ),
            key=lambda path: int(path.name[4:]),
        )
    except OSError as exc:
        raise RecoveryError("alsa-cards-unreadable") from exc
    if len(cards) > MAX_CARDS:
        raise RecoveryError("alsa-cards-ambiguous")

    candidates: list[PhysicalMotu] = []
    for card in cards:
        physical = _physical_identity(
            card,
            sound_class_root=sound_class_root,
            sys_devices_root=sys_devices_root,
        )
        if physical is None:
            continue
        if _read_proc_text(card / "usbid").casefold() != MOTU_USB_ID:
            raise RecoveryError("motu-usb-identity-mismatch")
        candidates.append(physical)
    if not candidates:
        return None
    if len(candidates) != 1:
        raise RecoveryError("motu-card-ambiguous")
    return candidates[0]


def _pcm_substreams(card: pathlib.Path) -> tuple[tuple[str, pathlib.Path], ...]:
    try:
        pcm_dirs = sorted(
            path
            for path in card.iterdir()
            if path.is_dir() and re.fullmatch(r"pcm[0-9]+[pc]", path.name)
        )
        substreams = tuple(
            (pcm.name[-1], substream)
            for pcm in pcm_dirs
            for substream in sorted(pcm.iterdir())
            if substream.is_dir() and re.fullmatch(r"sub[0-9]+", substream.name)
        )
    except OSError as exc:
        raise RecoveryError("motu-pcm-unreadable") from exc
    if {direction for direction, _path in substreams} != {"p", "c"}:
        raise RecoveryError("motu-pcm-ambiguous")
    if len(substreams) > MAX_SUBSTREAMS:
        raise RecoveryError("motu-pcm-ambiguous")
    return substreams


def _stable_pcm_snapshot(
    card: pathlib.Path,
) -> tuple[tuple[str, pathlib.Path, str, str], ...]:
    def observe() -> tuple[tuple[str, pathlib.Path, str, str], ...]:
        return tuple(
            (
                direction,
                path,
                _read_proc_text(path / "hw_params"),
                _read_proc_text(path / "status"),
            )
            for direction, path in _pcm_substreams(card)
        )

    first = observe()
    second = observe()
    if first != second:
        raise RecoveryError("motu-pcm-snapshot-changed")
    return second


def all_pcm_definitely_closed(card: pathlib.Path) -> None:
    if any(
        hw_params.casefold() != "closed" or status.casefold() != "closed"
        for _direction, _path, hw_params, status in _stable_pcm_snapshot(card)
    ):
        raise RecoveryError("motu-pcm-not-closed")


def _stable_other_pcm_status_snapshot(
    asound_root: pathlib.Path, motu_card: pathlib.Path
) -> tuple[tuple[pathlib.Path, str], ...]:
    def observe() -> tuple[tuple[pathlib.Path, str], ...]:
        try:
            cards = sorted(
                (
                    path
                    for path in asound_root.iterdir()
                    if re.fullmatch(r"card[0-9]+", path.name)
                ),
                key=lambda path: int(path.name[4:]),
            )
        except OSError as exc:
            raise RecoveryError("host-pcm-unreadable") from exc
        if (
            len(cards) > MAX_CARDS
            or motu_card not in cards
            or any(not card.is_dir() for card in cards)
        ):
            raise RecoveryError("host-pcm-ambiguous")

        snapshot: list[tuple[pathlib.Path, str]] = []
        try:
            for card in cards:
                if card == motu_card:
                    continue
                pcm_dirs = sorted(
                    path
                    for path in card.iterdir()
                    if re.fullmatch(r"pcm[0-9]+[pc]", path.name)
                )
                if any(not pcm.is_dir() for pcm in pcm_dirs):
                    raise RecoveryError("host-pcm-ambiguous")
                card_substream_count = 0
                for pcm in pcm_dirs:
                    substreams = sorted(
                        path
                        for path in pcm.iterdir()
                        if re.fullmatch(r"sub[0-9]+", path.name)
                    )
                    if not substreams or any(
                        not substream.is_dir() for substream in substreams
                    ):
                        raise RecoveryError("host-pcm-ambiguous")
                    card_substream_count += len(substreams)
                    if card_substream_count > MAX_SUBSTREAMS:
                        raise RecoveryError("host-pcm-ambiguous")
                    for substream in substreams:
                        snapshot.append(
                            (substream, _read_proc_text(substream / "status"))
                        )
        except OSError as exc:
            raise RecoveryError("host-pcm-unreadable") from exc
        return tuple(snapshot)

    first = observe()
    second = observe()
    if first != second:
        raise RecoveryError("host-pcm-snapshot-changed")
    return second


def all_other_pcm_definitely_closed(
    asound_root: pathlib.Path, motu_card: pathlib.Path
) -> None:
    if any(
        status.casefold() != "closed"
        for _substream, status in _stable_other_pcm_status_snapshot(
            asound_root, motu_card
        )
    ):
        raise RecoveryError("host-pcm-not-closed")


def _owner_pid(status_text: str) -> int | None:
    matches = re.findall(r"(?m)^owner_pid\s*:\s*([0-9]+)\s*$", status_text)
    if len(matches) != 1:
        return None
    value = int(matches[0])
    return value if value > 0 else None


def _pipewire_owns(pid: int, proc_root: pathlib.Path) -> bool:
    try:
        executable = os.readlink(proc_root / str(pid) / "exe")
    except OSError:
        return False
    return executable == PIPEWIRE_EXECUTABLE


def sink_transition_pcm_safe(card: pathlib.Path, proc_root: pathlib.Path) -> None:
    """Allow restored playback only when the stable owner is exactly PipeWire."""
    for direction, _path, hw_params, status in _stable_pcm_snapshot(card):
        closed = hw_params.casefold() == "closed" and status.casefold() == "closed"
        if closed:
            continue
        if direction == "c":
            raise RecoveryError("motu-capture-not-closed")
        owner = _owner_pid(status)
        if owner is None or not _pipewire_owns(owner, proc_root):
            raise RecoveryError("motu-playback-owner-unproven")


def absence_observation_pcm_safe(card: pathlib.Path, proc_root: pathlib.Path) -> None:
    """Reject direct or ambiguous opens before waiting on a missing sink."""
    for _direction, _path, hw_params, status in _stable_pcm_snapshot(card):
        hw_closed = hw_params.casefold() == "closed"
        status_closed = status.casefold() == "closed"
        if hw_closed != status_closed:
            raise RecoveryError("motu-pcm-ambiguous")
        if hw_closed:
            continue
        owner = _owner_pid(status)
        if owner is None or not _pipewire_owns(owner, proc_root):
            raise RecoveryError("motu-pcm-owner-unproven")


def _wait_for_pcm_gate(
    gate: Callable[[], None], sleeper: Sleeper, retryable: set[str]
) -> None:
    for attempt in range(QUIESCE_CLOSE_ATTEMPTS):
        try:
            gate()
            return
        except RecoveryError as exc:
            if str(exc) not in retryable or attempt + 1 == QUIESCE_CLOSE_ATTEMPTS:
                raise
            sleeper(READBACK_INTERVAL_SECONDS)


def require_audio_services_active(runner: Runner) -> None:
    for unit in REQUIRED_SERVICES:
        if runner(("systemctl", "--user", "is-active", unit)).strip() != "active":
            raise RecoveryError("desktop-audio-service-inactive")


def _observer_state(runner: Runner) -> str:
    state = runner(
        (
            "systemctl",
            "--user",
            "show",
            LEVEL_OBSERVER_UNIT,
            "--property=ActiveState",
            "--value",
        )
    ).strip()
    if state not in {"active", "inactive", "failed"}:
        raise RecoveryError("level-observer-state-ambiguous")
    return state


def _create_quiesce_marker(path: pathlib.Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"level-observer-quiesced-v1\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RecoveryError("level-observer-marker-unwritable") from exc


def _restore_marked_observer(path: pathlib.Path, runner: Runner) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RecoveryError("level-observer-marker-unreadable") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RecoveryError("level-observer-marker-invalid")
    try:
        if path.read_bytes() != b"level-observer-quiesced-v1\n":
            raise RecoveryError("level-observer-marker-invalid")
    except OSError as exc:
        raise RecoveryError("level-observer-marker-unreadable") from exc
    runner(("systemctl", "--user", "start", LEVEL_OBSERVER_UNIT))
    if _observer_state(runner) != "active":
        raise RecoveryError("level-observer-restore-unproven")
    try:
        path.unlink()
    except OSError as exc:
        raise RecoveryError("level-observer-marker-unwritable") from exc


@contextlib.contextmanager
def quiesce_level_observer(runner: Runner, marker_path: pathlib.Path) -> Iterator[None]:
    if _observer_state(runner) != "active":
        raise RecoveryError("level-observer-not-active")
    _create_quiesce_marker(marker_path)
    try:
        runner(("systemctl", "--user", "stop", LEVEL_OBSERVER_UNIT))
        if _observer_state(runner) != "inactive":
            raise RecoveryError("level-observer-stop-unproven")
        yield
    finally:
        _restore_marked_observer(marker_path, runner)


def _normalize_usb_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold().removeprefix("0x")
    return normalized if re.fullmatch(r"[0-9a-f]{4}", normalized) else None


def _normalize_pipewire_bus_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > 1_024:
        return None
    match = re.fullmatch(
        r"pci-([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])"
        r"-usb-0:([0-9]+(?:\.[0-9]+)*):([0-9]+)\.([0-9]+)",
        value,
    )
    if match is None:
        return None
    return (
        f"pci-{match.group(1).casefold()}-usb-0:{match.group(2)}:"
        f"{match.group(3)}.{match.group(4)}"
    )


def read_sink_inventory(runner: Runner) -> list[dict[str, Any]]:
    try:
        payload = json.loads(runner(("pactl", "--format=json", "list", "sinks")))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RecoveryError("sink-inventory-invalid") from exc
    if not isinstance(payload, list):
        raise RecoveryError("sink-inventory-invalid")
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise RecoveryError("sink-inventory-invalid")
        name = item.get("name")
        properties = item.get("properties")
        if (
            not isinstance(name, str)
            or not _valid_sink_name(name)
            or not isinstance(properties, dict)
            or name in names
        ):
            raise RecoveryError("sink-inventory-invalid")
        names.add(name)
        result.append(item)
    return result


def _motu_looking(item: dict[str, Any], physical: PhysicalMotu) -> bool:
    properties = item["properties"]
    serial = properties.get("device.serial")
    return (
        _normalize_pipewire_bus_path(properties.get("device.bus_path"))
        == physical.bus_path
        or item["name"].startswith("alsa_output.usb-MOTU_M2")
        or _normalize_usb_id(properties.get("device.vendor.id")) == MOTU_VENDOR_ID
        or (isinstance(serial, str) and serial.startswith(MOTU_SERIAL_PREFIX))
    )


def _motu_sink_identity(
    item: dict[str, Any], physical: PhysicalMotu
) -> SinkIdentity | None:
    if not _motu_looking(item, physical):
        return None
    properties = item["properties"]
    name = item["name"]
    serial = properties.get("device.serial")
    bus_path = properties.get("device.bus_path")
    expected_name = f"alsa_output.usb-{physical.pipewire_serial}-00.Direct__hw_M2__sink"
    if (
        _normalize_usb_id(properties.get("device.vendor.id")) != MOTU_VENDOR_ID
        or _normalize_usb_id(properties.get("device.product.id")) != MOTU_PRODUCT_ID
        or name != expected_name
        or serial != physical.pipewire_serial
        or _normalize_pipewire_bus_path(bus_path) != physical.bus_path
    ):
        raise RecoveryError("motu-sink-identity-ambiguous")
    return SinkIdentity(name=name, serial=serial, bus_path=bus_path)


def resolve_motu_sink(
    inventory: list[dict[str, Any]], physical: PhysicalMotu
) -> tuple[dict[str, Any], SinkIdentity] | None:
    candidates: list[tuple[dict[str, Any], SinkIdentity]] = []
    for item in inventory:
        identity = _motu_sink_identity(item, physical)
        if identity is not None:
            candidates.append((item, identity))
    if not candidates:
        return None
    if len(candidates) != 1:
        raise RecoveryError("motu-sink-ambiguous")
    return candidates[0]


def _sink_is_unity_unmuted(item: dict[str, Any]) -> bool:
    volume = item.get("volume")
    if not isinstance(volume, dict) or not volume:
        return False
    values: list[int] = []
    for channel in volume.values():
        if not isinstance(channel, dict) or type(channel.get("value")) is not int:
            return False
        values.append(channel["value"])
    return item.get("mute") is False and all(value == UNITY_VOLUME for value in values)


def read_default_sink(runner: Runner) -> str:
    matches: list[str] = []
    for line in runner(("pactl", "info")).splitlines():
        if line.startswith(("Default Sink:", "Standard-Ziel:")):
            matches.append(line.split(":", 1)[1].strip())
    if len(matches) != 1 or not _valid_sink_name(matches[0]):
        raise RecoveryError("default-sink-unreadable")
    return matches[0]


def _default_state() -> dict[str, Any]:
    return {
        "failures": 0,
        "next_attempt_at": 0.0,
        "handoff_pending": False,
        "handoff_serial_sha256": None,
    }


def _load_state(path: pathlib.Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RecoveryError("recovery-state-invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_state()
    except RecoveryError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise RecoveryError("recovery-state-unreadable") from exc
    failures = payload.get("failures") if isinstance(payload, dict) else None
    next_attempt = payload.get("next_attempt_at") if isinstance(payload, dict) else None
    pending = payload.get("handoff_pending") if isinstance(payload, dict) else None
    serial_hash = (
        payload.get("handoff_serial_sha256") if isinstance(payload, dict) else None
    )
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != STATE_SCHEMA_VERSION
        or type(failures) is not int
        or not 0 <= failures <= 64
        or type(next_attempt) not in {int, float}
        or not math.isfinite(next_attempt)
        or next_attempt < 0
        or type(pending) is not bool
        or (
            pending
            and (
                not isinstance(serial_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", serial_hash) is None
            )
        )
        or (not pending and serial_hash is not None)
    ):
        raise RecoveryError("recovery-state-invalid")
    return {
        "failures": failures,
        "next_attempt_at": float(next_attempt),
        "handoff_pending": pending,
        "handoff_serial_sha256": serial_hash,
    }


def _store_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    next_attempt = state.get("next_attempt_at")
    if type(next_attempt) not in {int, float} or not math.isfinite(next_attempt):
        raise RecoveryError("recovery-state-invalid")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
    except OSError as exc:
        raise RecoveryError("recovery-state-unwritable") from exc
    payload = json.dumps(
        {"schema_version": STATE_SCHEMA_VERSION, **state},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
    except OSError as exc:
        raise RecoveryError("recovery-state-unwritable") from exc
    temporary = pathlib.Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise RecoveryError("recovery-state-unwritable") from exc
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()


def _armed_failure_state(
    state: dict[str, Any], physical: PhysicalMotu, now: float
) -> dict[str, Any]:
    failures = min(int(state["failures"]) + 1, 64)
    exponent = min(failures - 1, 20)
    delay = min(
        FAILURE_BACKOFF_BASE_SECONDS * (2**exponent), FAILURE_BACKOFF_MAX_SECONDS
    )
    return {
        "failures": failures,
        "next_attempt_at": now + delay,
        "handoff_pending": True,
        "handoff_serial_sha256": physical.serial_sha256,
    }


def _success_state(now: float) -> dict[str, Any]:
    return {
        "failures": 0,
        "next_attempt_at": now + SUCCESS_COOLDOWN_SECONDS,
        "handoff_pending": False,
        "handoff_serial_sha256": None,
    }


def _same_physical(first: PhysicalMotu, second: PhysicalMotu | None) -> bool:
    return second is not None and (
        first.card == second.card
        and first.usb_serial == second.usb_serial
        and first.bus_path == second.bus_path
    )


def _sink_after_absence_stabilization(
    *,
    physical: PhysicalMotu,
    asound_root: pathlib.Path,
    sound_class_root: pathlib.Path,
    sys_devices_root: pathlib.Path,
    runner: Runner,
    sleeper: Sleeper,
) -> tuple[dict[str, Any], SinkIdentity] | None:
    sleeper(ABSENCE_STABILIZATION_SECONDS)
    current = resolve_unique_motu_card(
        asound_root,
        sound_class_root=sound_class_root,
        sys_devices_root=sys_devices_root,
    )
    if not _same_physical(physical, current):
        raise RecoveryError("motu-card-changed")
    require_audio_services_active(runner)
    return resolve_motu_sink(read_sink_inventory(runner), physical)


def _normalize_exact_sink(
    *, physical: PhysicalMotu, runner: Runner, proc_root: pathlib.Path
) -> SinkIdentity:
    recovered = resolve_motu_sink(read_sink_inventory(runner), physical)
    if recovered is None:
        raise RecoveryError("motu-sink-disappeared")
    _item, identity = recovered
    sink_transition_pcm_safe(physical.card, proc_root)
    runner(("pactl", "set-sink-volume", identity.name, "100%"))
    runner(("pactl", "set-sink-mute", identity.name, "0"))

    # Rebind identity and PCM ownership immediately before changing the default.
    rebound = resolve_motu_sink(read_sink_inventory(runner), physical)
    if rebound is None or rebound[1] != identity:
        raise RecoveryError("motu-sink-changed")
    sink_transition_pcm_safe(physical.card, proc_root)
    runner(("pactl", "set-default-sink", identity.name))

    verified = resolve_motu_sink(read_sink_inventory(runner), physical)
    if (
        verified is None
        or verified[1] != identity
        or not _sink_is_unity_unmuted(verified[0])
        or read_default_sink(runner) != identity.name
    ):
        raise RecoveryError("desktop-sink-readback-failed")
    sink_transition_pcm_safe(physical.card, proc_root)
    return identity


def reconcile_once(
    *,
    asound_root: pathlib.Path,
    state_path: pathlib.Path,
    sound_class_root: pathlib.Path = pathlib.Path("/sys/class/sound"),
    sys_devices_root: pathlib.Path = pathlib.Path("/sys/devices"),
    proc_root: pathlib.Path = pathlib.Path("/proc"),
    runner: Runner = run_command,
    now: float | None = None,
    sleeper: Sleeper = time.sleep,
) -> str:
    observed_now = time.time() if now is None else now
    try:
        quiesce_marker = state_path.with_name("level-observer-quiesced")
        _restore_marked_observer(quiesce_marker, runner)
        physical = resolve_unique_motu_card(
            asound_root,
            sound_class_root=sound_class_root,
            sys_devices_root=sys_devices_root,
        )
        if physical is None:
            return "noop:m2-absent"
        require_audio_services_active(runner)
        state = _load_state(state_path)
        sink = resolve_motu_sink(read_sink_inventory(runner), physical)
        if sink is not None and not state["handoff_pending"]:
            return "noop:sink-present"
        if state["handoff_pending"] and (
            state["handoff_serial_sha256"] != physical.serial_sha256
        ):
            raise RecoveryError("handoff-physical-identity-changed")

        if sink is None:
            if observed_now < float(state["next_attempt_at"]):
                return "noop:backoff"
            absence_observation_pcm_safe(physical.card, proc_root)
            sink = _sink_after_absence_stabilization(
                physical=physical,
                asound_root=asound_root,
                sound_class_root=sound_class_root,
                sys_devices_root=sys_devices_root,
                runner=runner,
                sleeper=sleeper,
            )
            if sink is not None and not state["handoff_pending"]:
                return "noop:sink-present"
        if sink is not None:
            with quiesce_level_observer(runner, quiesce_marker):
                current = resolve_unique_motu_card(
                    asound_root,
                    sound_class_root=sound_class_root,
                    sys_devices_root=sys_devices_root,
                )
                if not _same_physical(physical, current):
                    raise RecoveryError("motu-card-changed")
                require_audio_services_active(runner)
                _wait_for_pcm_gate(
                    lambda: sink_transition_pcm_safe(physical.card, proc_root),
                    sleeper,
                    {"motu-capture-not-closed"},
                )
                _normalize_exact_sink(
                    physical=physical, runner=runner, proc_root=proc_root
                )
            _store_state(state_path, _success_state(observed_now))
            return "handoff-restored"

        all_other_pcm_definitely_closed(asound_root, physical.card)
        with quiesce_level_observer(runner, quiesce_marker):
            current = resolve_unique_motu_card(
                asound_root,
                sound_class_root=sound_class_root,
                sys_devices_root=sys_devices_root,
            )
            if not _same_physical(physical, current):
                raise RecoveryError("motu-card-changed")
            require_audio_services_active(runner)
            if resolve_motu_sink(read_sink_inventory(runner), physical) is not None:
                _wait_for_pcm_gate(
                    lambda: sink_transition_pcm_safe(physical.card, proc_root),
                    sleeper,
                    {"motu-capture-not-closed"},
                )
                _normalize_exact_sink(
                    physical=physical, runner=runner, proc_root=proc_root
                )
                recovered_without_restart = True
            else:
                _wait_for_pcm_gate(
                    lambda: all_pcm_definitely_closed(physical.card),
                    sleeper,
                    {"motu-pcm-not-closed"},
                )
                # Durable arming precedes the effect; a crash cannot lose handoff intent.
                _store_state(
                    state_path, _armed_failure_state(state, physical, observed_now)
                )

                # This stable all-playback-and-capture double observation is the
                # final sub-call before restart. Only a non-cooperating ALSA open
                # can race the following exec; that residual window is documented.
                current = resolve_unique_motu_card(
                    asound_root,
                    sound_class_root=sound_class_root,
                    sys_devices_root=sys_devices_root,
                )
                if not _same_physical(physical, current):
                    raise RecoveryError("motu-card-changed")
                require_audio_services_active(runner)
                returned = resolve_motu_sink(read_sink_inventory(runner), physical)
                if returned is not None:
                    _normalize_exact_sink(
                        physical=physical, runner=runner, proc_root=proc_root
                    )
                    recovered_without_restart = True
                else:
                    all_pcm_definitely_closed(physical.card)
                    all_other_pcm_definitely_closed(asound_root, physical.card)
                    runner(("systemctl", "--user", "restart", WIREPLUMBER_UNIT))
                    recovered_without_restart = False

                    recovered = None
                    for attempt in range(READBACK_ATTEMPTS):
                        current = resolve_unique_motu_card(
                            asound_root,
                            sound_class_root=sound_class_root,
                            sys_devices_root=sys_devices_root,
                        )
                        if not _same_physical(physical, current):
                            raise RecoveryError("motu-card-changed")
                        require_audio_services_active(runner)
                        recovered = resolve_motu_sink(
                            read_sink_inventory(runner), physical
                        )
                        if recovered is not None:
                            break
                        if attempt + 1 < READBACK_ATTEMPTS:
                            sleeper(READBACK_INTERVAL_SECONDS)
                    if recovered is None:
                        raise RecoveryError("motu-sink-readback-timeout")
                    _normalize_exact_sink(
                        physical=physical, runner=runner, proc_root=proc_root
                    )

        _store_state(state_path, _success_state(observed_now))
        return "handoff-restored" if recovered_without_restart else "recovered"
    except RecoveryError:
        return "blocked"


def check_contract() -> None:
    for argv in (
        ("systemctl", "--user", "restart", WIREPLUMBER_UNIT),
        ("systemctl", "--user", "stop", LEVEL_OBSERVER_UNIT),
        ("systemctl", "--user", "is-active", "pipewire.service"),
        ("pactl", "--format=json", "list", "sinks"),
    ):
        _validate_command(argv)
    if REQUIRED_SERVICES != (
        "pipewire.service",
        "pipewire-pulse.service",
        "wireplumber.service",
    ):
        raise RecoveryError("service-contract-drift")


def run_loop(
    asound_root: pathlib.Path,
    state_path: pathlib.Path,
    sound_class_root: pathlib.Path,
    sys_devices_root: pathlib.Path,
    proc_root: pathlib.Path,
) -> None:
    while True:
        result = reconcile_once(
            asound_root=asound_root,
            state_path=state_path,
            sound_class_root=sound_class_root,
            sys_devices_root=sys_devices_root,
            proc_root=proc_root,
        )
        if result in {"recovered", "handoff-restored", "blocked"}:
            print(json.dumps({"qobuz_desktop_recovery": result}), flush=True)
        time.sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument(
        "--asound-root", type=pathlib.Path, default=pathlib.Path("/proc/asound")
    )
    run_parser.add_argument(
        "--sound-class-root",
        type=pathlib.Path,
        default=pathlib.Path("/sys/class/sound"),
    )
    run_parser.add_argument(
        "--sys-devices-root", type=pathlib.Path, default=pathlib.Path("/sys/devices")
    )
    run_parser.add_argument(
        "--proc-root", type=pathlib.Path, default=pathlib.Path("/proc")
    )
    run_parser.add_argument("--state-file", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "check":
        check_contract()
        return 0
    run_loop(
        args.asound_root,
        args.state_file,
        args.sound_class_root,
        args.sys_devices_root,
        args.proc_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
