#!/usr/bin/env python3
"""Read-only Roland source binding and bounded Standard MIDI File validation."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import struct
from typing import Any

MAX_MIDI_BYTES = 64_000_000
MAX_EVENTS = 2_000_000
MAX_TRACKS = 256
MAX_VLQ = 0x0FFFFFFF
SMPTE_FPS = 25
SMPTE_TICKS_PER_FRAME = 40
SMPTE_DIVISION = ((256 - SMPTE_FPS) << 8) | SMPTE_TICKS_PER_FRAME
ADDRESS_RE = re.compile(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)")
CLIENT_RE = re.compile(
    r'^Client\s+(\d+)\s*:\s*"([^"]*)"\s+\[([^\]]*)\]\s*$'
)
CARD_METADATA_RE = re.compile(r"\bCard=(\d+)\b")
PORT_RE = re.compile(
    r'^\s+Port\s+(\d+)\s*:\s*"([^"]*)"'
    r'(?:\s+\([^)]*\))?(?:\s+\[[^\]]*\])?\s*$'
)
LIST_PORT_RE = re.compile(r"^\s*((?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*))\s+(.+?)\s{2,}(.+?)\s*$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SPACE_RE = re.compile(r"\s+")
PROC_CARD_RE = re.compile(r"card(0|[1-9][0-9]*)")
PROC_MIDI_RE = re.compile(r"midi(?:0|[1-9][0-9]*)")

ROLAND_VENDOR_ID = "0582"
ROLAND_PRODUCT_ID = "01b1"


class MidiCaptureError(RuntimeError):
    """The MIDI source or SMF is ambiguous, malformed, or outside its bounds."""


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _normalized_label_hash(value: str) -> str:
    if CONTROL_RE.search(value):
        raise MidiCaptureError("MIDI port label contains control characters")
    normalized = SPACE_RE.sub(" ", value.strip()).casefold()
    if not normalized or len(normalized.encode("utf-8")) > 1024:
        raise MidiCaptureError("MIDI port label is invalid")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _read_small(path: pathlib.Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not metadata.st_size <= 4096:
            raise MidiCaptureError("sysfs identity attribute exceeds its bound")
        payload = os.read(descriptor, 4097)
        if len(payload) > 4096:
            raise MidiCaptureError("sysfs identity attribute exceeds its bound")
        return payload.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise MidiCaptureError("sysfs identity attribute cannot be read") from exc
    finally:
        os.close(descriptor)


def parse_seq_clients(text: str) -> list[dict[str, Any]]:
    """Parse the bounded kernel ALSA sequencer inventory."""

    clients: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        match = CLIENT_RE.fullmatch(line)
        if match:
            client, name, metadata = match.groups()
            card_match = CARD_METADATA_RE.search(metadata)
            current = {
                "client": int(client),
                "card": int(card_match.group(1)) if card_match else None,
                "kernel_legacy": metadata.strip() == "Kernel Legacy",
                "client_label": name,
                "ports": [],
            }
            clients.append(current)
            continue
        port_match = PORT_RE.fullmatch(line)
        if current is not None and port_match:
            port, name = port_match.groups()
            current["ports"].append({"port": int(port), "port_label": name})
    return clients


def parse_arecordmidi_ports(text: str) -> dict[str, dict[str, str]]:
    """Parse ``arecordmidi -l`` solely as a corroborating address/name view."""

    ports: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        match = LIST_PORT_RE.fullmatch(line)
        if not match:
            continue
        address, client_name, port_name = match.groups()
        if address in ports:
            raise MidiCaptureError("arecordmidi listed one address more than once")
        ports[address] = {
            "client_label_sha256": _normalized_label_hash(client_name),
            "port_label_sha256": _normalized_label_hash(port_name),
        }
    return ports


def _usb_identity_for_card(
    card: int,
    *,
    sound_class_root: pathlib.Path,
    sys_devices_root: pathlib.Path,
) -> dict[str, Any] | None:
    try:
        devices_root = sys_devices_root.resolve(strict=True)
        device = (sound_class_root / f"card{card}" / "device").resolve(strict=True)
        device.relative_to(devices_root)
    except (FileNotFoundError, OSError, ValueError):
        return None
    for candidate in (device, *device.parents):
        try:
            candidate.relative_to(devices_root)
        except ValueError:
            break
        try:
            vendor = _read_small(candidate / "idVendor").casefold()
            product = _read_small(candidate / "idProduct").casefold()
        except (FileNotFoundError, MidiCaptureError):
            continue
        if vendor != ROLAND_VENDOR_ID or product != ROLAND_PRODUCT_ID:
            return None
        try:
            bus_number = _read_small(candidate / "busnum")
            port_path = _read_small(candidate / "devpath")
        except (FileNotFoundError, MidiCaptureError) as exc:
            raise MidiCaptureError("Roland USB port identity is incomplete") from exc
        if not bus_number or not port_path or CONTROL_RE.search(bus_number + port_path):
            raise MidiCaptureError("Roland USB port identity is invalid")
        identity = {
            "vendor_id": vendor,
            "product_id": product,
            "identity_strength": "model-usb-port",
            "bus_number": bus_number,
            "port_path": port_path,
        }
        identity["fingerprint"] = canonical_sha256(identity)
        return identity
    return None


def _proc_midi_device_label(path: pathlib.Path) -> str:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError as exc:
        raise MidiCaptureError("ALSA raw-MIDI card metadata cannot be opened") from exc
    try:
        try:
            payload = os.read(descriptor, 4097)
        except OSError as exc:
            raise MidiCaptureError("ALSA raw-MIDI card metadata cannot be read") from exc
        if len(payload) > 4096:
            raise MidiCaptureError("ALSA raw-MIDI card metadata exceeds its bound")
    finally:
        os.close(descriptor)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MidiCaptureError("ALSA raw-MIDI card metadata is not UTF-8") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise MidiCaptureError("ALSA raw-MIDI card metadata has no device label")
    return lines[0]


def _legacy_client_card(
    client_label: str,
    *,
    sound_class_root: pathlib.Path,
    sys_devices_root: pathlib.Path,
    proc_asound_root: pathlib.Path,
) -> tuple[int, dict[str, Any]] | None:
    """Map one kernel-legacy sequencer label back to a unique Roland USB card."""

    try:
        card_roots = sorted(
            (item for item in proc_asound_root.iterdir() if PROC_CARD_RE.fullmatch(item.name)),
            key=lambda item: item.name,
        )
    except OSError as exc:
        raise MidiCaptureError("ALSA card inventory is unavailable") from exc
    if len(card_roots) > 256:
        raise MidiCaptureError("ALSA card inventory exceeds its bound")
    expected_label = _normalized_label_hash(client_label)
    matches: list[tuple[int, dict[str, Any]]] = []
    for card_root in card_roots:
        card_match = PROC_CARD_RE.fullmatch(card_root.name)
        assert card_match is not None
        card = int(card_match.group(1))
        usb = _usb_identity_for_card(
            card,
            sound_class_root=sound_class_root,
            sys_devices_root=sys_devices_root,
        )
        if usb is None:
            continue
        try:
            midi_entries = sorted(
                (item for item in card_root.iterdir() if PROC_MIDI_RE.fullmatch(item.name)),
                key=lambda item: item.name,
            )
        except OSError as exc:
            raise MidiCaptureError("Roland ALSA raw-MIDI inventory is unavailable") from exc
        if len(midi_entries) > 32:
            raise MidiCaptureError("Roland ALSA raw-MIDI inventory exceeds its bound")
        for item in midi_entries:
            if _normalized_label_hash(_proc_midi_device_label(item)) == expected_label:
                matches.append((card, usb))
                break
    if len(matches) > 1:
        raise MidiCaptureError("Roland legacy MIDI client maps to multiple USB cards")
    return matches[0] if matches else None


def discover_unique_roland_port(
    *,
    arecordmidi_listing: str,
    clients_path: pathlib.Path = pathlib.Path("/proc/asound/seq/clients"),
    sound_class_root: pathlib.Path = pathlib.Path("/sys/class/sound"),
    sys_devices_root: pathlib.Path = pathlib.Path("/sys/devices"),
    proc_asound_root: pathlib.Path = pathlib.Path("/proc/asound"),
) -> dict[str, Any]:
    """Bind one USB 0582:01b1 kernel port also present in ``arecordmidi -l``."""

    try:
        raw = clients_path.read_bytes()
    except OSError as exc:
        raise MidiCaptureError("ALSA sequencer inventory is unavailable") from exc
    if len(raw) > 262_144:
        raise MidiCaptureError("ALSA sequencer inventory exceeds its bound")
    try:
        clients = parse_seq_clients(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise MidiCaptureError("ALSA sequencer inventory is not UTF-8") from exc
    listing = parse_arecordmidi_ports(arecordmidi_listing)
    matches: list[dict[str, Any]] = []
    for client in clients:
        card = client["card"]
        if card is None:
            if client.get("kernel_legacy") is not True:
                continue
            legacy = _legacy_client_card(
                client["client_label"],
                sound_class_root=sound_class_root,
                sys_devices_root=sys_devices_root,
                proc_asound_root=proc_asound_root,
            )
            if legacy is None:
                continue
            card, usb = legacy
        else:
            usb = _usb_identity_for_card(
                card,
                sound_class_root=sound_class_root,
                sys_devices_root=sys_devices_root,
            )
            if usb is None:
                continue
        for port in client["ports"]:
            address = f"{client['client']}:{port['port']}"
            listed = listing.get(address)
            if listed is None:
                continue
            kernel_client_label_sha256 = _normalized_label_hash(client["client_label"])
            kernel_port_label_sha256 = _normalized_label_hash(port["port_label"])
            if (
                listed["client_label_sha256"] != kernel_client_label_sha256
                or listed["port_label_sha256"] != kernel_port_label_sha256
            ):
                continue
            identity = {
                "address": address,
                "client": client["client"],
                "port": port["port"],
                "kernel_card": card,
                "kernel_client_label_sha256": kernel_client_label_sha256,
                "kernel_port_label_sha256": kernel_port_label_sha256,
                "arecordmidi_client_label_sha256": listed["client_label_sha256"],
                "arecordmidi_port_label_sha256": listed["port_label_sha256"],
                "usb": usb,
            }
            identity["fingerprint"] = canonical_sha256(identity)
            matches.append({"address": address, "identity": identity})
    if len(matches) != 1:
        raise MidiCaptureError(
            f"expected exactly one USB-bound Roland FP-30X MIDI port, observed {len(matches)}"
        )
    return matches[0]


def arecordmidi_capture_argv(
    binary: pathlib.Path, address: str, output_path: pathlib.Path
) -> list[str]:
    if ADDRESS_RE.fullmatch(address) is None:
        raise MidiCaptureError("ALSA sequencer port address is invalid")
    return [
        str(binary),
        "-p",
        address,
        "-f",
        str(SMPTE_FPS),
        "-t",
        str(SMPTE_TICKS_PER_FRAME),
        str(output_path),
    ]


def _read_vlq(data: bytes, position: int, end: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if position >= end:
            raise MidiCaptureError("SMF variable-length quantity is truncated")
        byte = data[position]
        position += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, position
    raise MidiCaptureError("SMF variable-length quantity exceeds four bytes")


def _scan_track(
    data: bytes,
    start: int,
    end: int,
    counts: dict[str, int],
    *,
    remaining_events: int,
) -> dict[str, int | None]:
    position = start
    running_status: int | None = None
    saw_end = False
    velocity_min: int | None = None
    velocity_max: int | None = None
    event_count = 0
    while position < end:
        _delta, position = _read_vlq(data, position, end)
        if position >= end:
            raise MidiCaptureError("SMF event is truncated")
        lead = data[position]
        if lead < 0x80:
            if running_status is None:
                raise MidiCaptureError("SMF running status has no channel status")
            status = running_status
        else:
            position += 1
            status = lead
            if 0x80 <= status <= 0xEF:
                running_status = status
            else:
                running_status = None
        if 0x80 <= status <= 0xEF:
            event_type = status >> 4
            length = 1 if event_type in {0xC, 0xD} else 2
            if position + length > end:
                raise MidiCaptureError("SMF channel event is truncated")
            payload = data[position : position + length]
            if any(byte >= 0x80 for byte in payload):
                raise MidiCaptureError("SMF channel data byte has its status bit set")
            position += length
            class_name = {
                0x8: "note_off",
                0x9: "note_on",
                0xA: "poly_aftertouch",
                0xB: "control_change",
                0xC: "program_change",
                0xD: "channel_aftertouch",
                0xE: "pitch_bend",
            }[event_type]
            if event_type == 0x9 and payload[1] == 0:
                class_name = "note_off"
            counts[class_name] = counts.get(class_name, 0) + 1
            if event_type == 0xB and payload[0] == 64:
                counts["sustain_cc64"] = counts.get("sustain_cc64", 0) + 1
            if event_type in {0x8, 0x9}:
                velocity = payload[1]
                velocity_min = velocity if velocity_min is None else min(velocity_min, velocity)
                velocity_max = velocity if velocity_max is None else max(velocity_max, velocity)
        elif status in {0xF0, 0xF7}:
            length, position = _read_vlq(data, position, end)
            if length > end - position:
                raise MidiCaptureError("SMF SysEx payload exceeds its track")
            position += length
            counts["sysex"] = counts.get("sysex", 0) + 1
        elif status == 0xFF:
            if position >= end:
                raise MidiCaptureError("SMF meta event type is truncated")
            meta_type = data[position]
            position += 1
            length, position = _read_vlq(data, position, end)
            if length > end - position:
                raise MidiCaptureError("SMF meta payload exceeds its track")
            if meta_type == 0x2F:
                if length != 0 or position != end:
                    raise MidiCaptureError("SMF end-of-track is malformed or not final")
                saw_end = True
            position += length
            counts["meta"] = counts.get("meta", 0) + 1
        else:
            raise MidiCaptureError("SMF contains an unsupported system status")
        event_count += 1
        if event_count > remaining_events:
            raise MidiCaptureError("SMF event count exceeds its bound")
    if position != end or not saw_end:
        raise MidiCaptureError("SMF track has no final end-of-track event")
    return {"events": event_count, "velocity_min": velocity_min, "velocity_max": velocity_max}


def validate_smf_bytes(data: bytes) -> dict[str, Any]:
    """Validate a complete SMPTE SMF and return bounded event-class counts."""

    if len(data) < 14 or len(data) > MAX_MIDI_BYTES:
        raise MidiCaptureError("SMF size is outside its bound")
    if data[:4] != b"MThd":
        raise MidiCaptureError("SMF header chunk is missing")
    header_length = struct.unpack(">I", data[4:8])[0]
    if header_length != 6:
        raise MidiCaptureError("SMF header length is not six")
    smf_format, track_count, division = struct.unpack(">HHH", data[8:14])
    if smf_format not in {0, 1} or track_count < 1 or track_count > MAX_TRACKS:
        raise MidiCaptureError("SMF format or track count is invalid")
    if smf_format == 0 and track_count != 1:
        raise MidiCaptureError("SMF format zero must contain one track")
    if division != SMPTE_DIVISION:
        raise MidiCaptureError("SMF division is not SMPTE 25 fps / 40 ticks")
    position = 14
    counts: dict[str, int] = {}
    total_events = 0
    velocity_values: list[int] = []
    for _track in range(track_count):
        if position + 8 > len(data) or data[position : position + 4] != b"MTrk":
            raise MidiCaptureError("SMF track header is missing or truncated")
        length = struct.unpack(">I", data[position + 4 : position + 8])[0]
        position += 8
        if length > len(data) - position:
            raise MidiCaptureError("SMF track exceeds the file boundary")
        track = _scan_track(
            data,
            position,
            position + length,
            counts,
            remaining_events=MAX_EVENTS - total_events,
        )
        total_events += int(track["events"])
        if total_events > MAX_EVENTS:
            raise MidiCaptureError("SMF event count exceeds its bound")
        for key in ("velocity_min", "velocity_max"):
            value = track[key]
            if isinstance(value, int):
                velocity_values.append(value)
        position += length
    if position != len(data):
        raise MidiCaptureError("SMF has trailing bytes after its declared tracks")
    for name in (
        "note_on",
        "note_off",
        "control_change",
        "sustain_cc64",
        "pitch_bend",
        "poly_aftertouch",
        "program_change",
        "channel_aftertouch",
        "sysex",
        "meta",
    ):
        counts.setdefault(name, 0)
    return {
        "format": smf_format,
        "track_count": track_count,
        "division": division,
        "timing": {
            "basis": "SMPTE",
            "fps": SMPTE_FPS,
            "ticks_per_frame": SMPTE_TICKS_PER_FRAME,
            "nominal_resolution_ms": 1,
        },
        "event_count": total_events,
        "event_counts": counts,
        "note_velocity": {
            "minimum": min(velocity_values) if velocity_values else None,
            "maximum": max(velocity_values) if velocity_values else None,
        },
    }


def validate_smf(path: pathlib.Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not 1 <= metadata.st_size <= MAX_MIDI_BYTES:
            raise MidiCaptureError("SMF size is outside its bound")
        data = bytearray()
        while len(data) <= MAX_MIDI_BYTES:
            chunk = os.read(descriptor, min(65_536, MAX_MIDI_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != metadata.st_size:
            raise MidiCaptureError("SMF changed or exceeded its bound while being read")
        return validate_smf_bytes(bytes(data))
    except OSError as exc:
        raise MidiCaptureError("SMF cannot be read") from exc
    finally:
        os.close(descriptor)
