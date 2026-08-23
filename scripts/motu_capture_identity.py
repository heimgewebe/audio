#!/usr/bin/env python3
"""Pure identity contract for the MOTU M2 recorder capture source."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SOURCE_SPEC_RE = re.compile(
    r"^(?P<format>[A-Za-z0-9_-]+) (?P<channels>[0-9]+)ch (?P<rate>[0-9]+)Hz$"
)


def canonical_value_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def normalize_usb_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized[2:] if normalized.startswith("0x") else normalized


def source_volume_values(source: dict[str, Any]) -> list[int]:
    volume = source.get("volume")
    if not isinstance(volume, dict) or not volume:
        return []
    values: list[int] = []
    for item in volume.values():
        if not isinstance(item, dict):
            return []
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, int):
            return []
        values.append(value)
    return values


def source_identity(source: Any) -> dict[str, Any] | None:
    if not isinstance(source, dict):
        raise ValueError("pactl source item is not an object")
    properties = source.get("properties")
    if not isinstance(properties, dict):
        return None
    if source.get("monitor_source") not in {"", None}:
        return None
    if properties.get("device.class") != "sound":
        return None
    if properties.get("media.class") != "Audio/Source":
        return None
    vendor_id = normalize_usb_id(properties.get("device.vendor.id"))
    product_id = normalize_usb_id(properties.get("device.product.id"))
    if vendor_id != "07fd" or product_id != "0008":
        return None
    serial = properties.get("device.serial")
    name = source.get("name")
    bus_path = properties.get("device.bus_path")
    if not isinstance(serial, str) or not serial.startswith("MOTU_M2_"):
        raise ValueError("MOTU source lacks its serial-bound identity")
    if not isinstance(name, str) or not name.startswith("alsa_input.usb-MOTU_M2_"):
        raise ValueError("MOTU source node name is invalid")
    if f"usb-{serial}-00" not in name:
        raise ValueError("MOTU source node does not match its serial identity")
    if not isinstance(bus_path, str) or not bus_path:
        raise ValueError("MOTU source has no USB bus path")
    match = SOURCE_SPEC_RE.fullmatch(str(source.get("sample_specification", "")))
    if match is None:
        raise ValueError("MOTU source sample specification is invalid")
    volume_values = source_volume_values(source)
    identity = {
        "vendor_id": vendor_id,
        "product_id": product_id,
        "serial_sha256": sha256_text(serial),
        "node_name_sha256": sha256_text(name),
        "bus_path_sha256": sha256_text(bus_path),
        "sample_format": match.group("format"),
        "sample_rate_hz": int(match.group("rate")),
        "channels": int(match.group("channels")),
        "muted": source.get("mute"),
        "unity_volume": bool(volume_values)
        and all(value == 65_536 for value in volume_values),
    }
    identity["fingerprint"] = canonical_value_sha256(identity)
    return identity
