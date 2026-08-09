#!/usr/bin/env python3
"""Read-only audio signal-path doctor for the Heim-PC."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import itertools
import os
import pathlib
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Iterable

READ_ONLY_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("aplay", "-l"),
    ("arecord", "-l"),
    ("wpctl", "status"),
    ("pw-metadata", "-n", "settings", "0"),
    ("pactl", "info"),
    ("pactl", "list", "short", "sinks"),
    ("pactl", "list", "short", "sources"),
    ("aconnect", "-l"),
    ("amidi", "-l"),
    ("systemctl", "is-active", "bluetooth"),
)

MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
MAX_ELD_FILES = 32
MAX_ELD_BYTES = 256 * 1024
STREAM_CHUNK_BYTES = 8192

SERIAL_PATTERNS = (
    re.compile(r"usb-[^\s]+", re.IGNORECASE),
    re.compile(r"M2_[A-Z0-9-]+", re.IGNORECASE),
)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    error: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False


class BoundedText(str):
    truncated: bool
    observed_items: int
    max_items: int
    max_bytes: int

    def __new__(
        cls,
        value: str,
        *,
        truncated: bool,
        observed_items: int,
        max_items: int,
        max_bytes: int,
    ) -> "BoundedText":
        instance = str.__new__(cls, value)
        instance.truncated = truncated
        instance.observed_items = observed_items
        instance.max_items = max_items
        instance.max_bytes = max_bytes
        return instance


def _drain_bounded_stream(
    stream: object, limit: int, result: dict[str, object]
) -> None:
    kept = bytearray()
    truncated = False
    try:
        while True:
            chunk = stream.read(STREAM_CHUNK_BYTES)  # type: ignore[attr-defined]
            if not chunk:
                break
            remaining = max(0, limit - len(kept))
            if remaining:
                kept.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
    finally:
        stream.close()  # type: ignore[attr-defined]
    result["bytes"] = bytes(kept)
    result["truncated"] = truncated


def run_read_only(
    argv: tuple[str, ...],
    timeout: float = 4.0,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except FileNotFoundError as exc:
        return CommandResult(argv, 127, "", "", type(exc).__name__)

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_result: dict[str, object] = {}
    stderr_result: dict[str, object] = {}
    readers = [
        threading.Thread(
            target=_drain_bounded_stream,
            args=(process.stdout, max_output_bytes, stdout_result),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_bounded_stream,
            args=(process.stderr, max_output_bytes, stderr_result),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        returncode = process.wait()
    for reader in readers:
        reader.join()

    stdout_bytes = stdout_result.get("bytes", b"")
    stderr_bytes = stderr_result.get("bytes", b"")
    assert isinstance(stdout_bytes, bytes)
    assert isinstance(stderr_bytes, bytes)
    return CommandResult(
        argv,
        124 if timed_out else returncode,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
        "TimeoutExpired" if timed_out else None,
        bool(stdout_result.get("truncated", False)),
        bool(stderr_result.get("truncated", False)),
        timed_out,
    )


def redact(text: str) -> str:
    value = text
    username = os.environ.get("USER", "").strip()
    hostname = os.uname().nodename.strip()
    if username:
        value = value.replace(username, "<user>")
    if hostname:
        value = value.replace(hostname, "<host>")
    for pattern in SERIAL_PATTERNS:
        value = pattern.sub("<redacted-device-id>", value)
    return value


def parse_setting(text: str, key: str) -> int | None:
    match = re.search(rf"key:'{re.escape(key)}' value:'([0-9]+)'", text)
    return int(match.group(1)) if match else None


def is_motu_m2_endpoint(name: str | None) -> bool:
    if not name:
        return False
    lowered = name.casefold()
    return bool(
        re.search(r"(?:^|[._\s-])motu[._\s-]+m2(?:[._\s-]|$)", lowered)
        or re.search(r"(?:^|[._\s-])m2[._\s-]+motu(?:[._\s-]|$)", lowered)
    )


def normalize_endpoint(name: str | None) -> str | None:
    if not name:
        return None
    lowered = name.lower()
    if is_motu_m2_endpoint(name):
        return "motu-m2"
    if "roland" in lowered or "digital_piano" in lowered:
        return "roland-fp-30x"
    if "hdmi" in lowered:
        return "hdmi"
    if "iec958" in lowered:
        return "spdif"
    return "other"


def parse_pactl_default(text: str, kind: str) -> str | None:
    labels = {
        "sink": ("Default Sink", "Standard-Ziel"),
        "source": ("Default Source", "Standard-Quelle"),
    }
    for line in text.splitlines():
        if any(line.startswith(f"{label}:") for label in labels[kind]):
            return line.split(":", 1)[1].strip()
    return None


def contains_device(text: str, device: str) -> bool:
    if device == "motu-m2":
        return any(
            is_motu_m2_endpoint(line)
            or re.search(r"\bM2\s*\[M2\]", line, re.IGNORECASE) is not None
            for line in text.splitlines()
        )
    if device == "roland-fp-30x":
        return bool(
            re.search(
                r"Roland Digital Piano|Roland.*Piano|FP[- ]?30X",
                text,
                re.IGNORECASE,
            )
        )
    raise ValueError(f"unknown device: {device}")


def read_bounded_text_files(
    paths: Iterable[pathlib.Path],
    *,
    max_files: int,
    max_bytes: int,
) -> BoundedText:
    if max_files < 1 or max_bytes < 1:
        raise ValueError("bounded text limits must be positive")
    retained = bytearray()
    observed_items = 0
    truncated = False
    for path in paths:
        if observed_items >= max_files:
            truncated = True
            break
        observed_items += 1
        separator_bytes = 1 if retained else 0
        remaining = max_bytes - len(retained) - separator_bytes
        if remaining <= 0:
            truncated = True
            break
        try:
            with path.open("rb") as handle:
                chunk = handle.read(remaining + 1)
        except OSError:
            continue
        if retained:
            retained.extend(b"\n")
        if len(chunk) > remaining:
            retained.extend(chunk[:remaining])
            truncated = True
            break
        retained.extend(chunk)
    value = bytes(retained).decode("utf-8", errors="replace")
    return BoundedText(
        value,
        truncated=truncated,
        observed_items=observed_items,
        max_items=max_files,
        max_bytes=max_bytes,
    )


def read_eld_text() -> BoundedText:
    paths = itertools.islice(
        pathlib.Path("/proc/asound").glob("card*/eld#*"), MAX_ELD_FILES + 1
    )
    return read_bounded_text_files(
        paths, max_files=MAX_ELD_FILES, max_bytes=MAX_ELD_BYTES
    )


def physical_unknowns(contract_path: pathlib.Path | None = None) -> list[str]:
    path = contract_path or (
        pathlib.Path(__file__).resolve().parents[1]
        / "inventory"
        / "physical-verification.v1.json"
    )
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"physical verification contract unavailable: {path}"
        ) from exc
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        raise RuntimeError("physical verification contract has no facts object")
    non_null = [name for name, value in facts.items() if value is not None]
    if non_null:
        raise RuntimeError(
            "doctor accepts only the unverified template; verified physical facts require "
            "a separately signed observation"
        )
    return sorted(facts)


def desired_hardware() -> list[str]:
    profile_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "profiles"
        / "audio-profiles.v1.json"
    )
    try:
        payload = json.loads(profile_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"audio profile catalog unavailable: {profile_path}"
        ) from exc
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        raise RuntimeError("audio profile catalog has no profiles object")
    devices: set[str] = set()
    for profile in profiles.values():
        if not isinstance(profile, dict):
            raise RuntimeError("audio profile catalog contains a non-object profile")
        required = profile.get("required_hardware", [])
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            raise RuntimeError("audio profile required_hardware must be a string array")
        devices.update(required)
    return sorted(devices)


def build_report(
    results: Iterable[CommandResult], eld_text: str = ""
) -> dict[str, object]:
    result_list = list(results)
    by_command = {result.argv: result for result in result_list}
    aplay = by_command.get(("aplay", "-l"), CommandResult((), 127, "", ""))
    arecord = by_command.get(("arecord", "-l"), CommandResult((), 127, "", ""))
    wpctl = by_command.get(("wpctl", "status"), CommandResult((), 127, "", ""))
    metadata = by_command.get(
        ("pw-metadata", "-n", "settings", "0"), CommandResult((), 127, "", "")
    )
    pactl_info = by_command.get(("pactl", "info"), CommandResult((), 127, "", ""))
    sinks = by_command.get(
        ("pactl", "list", "short", "sinks"), CommandResult((), 127, "", "")
    )
    sources = by_command.get(
        ("pactl", "list", "short", "sources"), CommandResult((), 127, "", "")
    )
    aconnect = by_command.get(("aconnect", "-l"), CommandResult((), 127, "", ""))
    amidi = by_command.get(("amidi", "-l"), CommandResult((), 127, "", ""))
    bluetooth = by_command.get(
        ("systemctl", "is-active", "bluetooth"), CommandResult((), 127, "", "")
    )

    alsa_audio_text = "\n".join((aplay.stdout, arecord.stdout))
    pipewire_text = wpctl.stdout
    midi_text = "\n".join((aconnect.stdout, amidi.stdout))
    sink_name = parse_pactl_default(pactl_info.stdout, "sink")
    source_name = parse_pactl_default(pactl_info.stdout, "source")
    rate = parse_setting(metadata.stdout, "clock.force-rate") or parse_setting(
        metadata.stdout, "clock.rate"
    )
    quantum = parse_setting(metadata.stdout, "clock.force-quantum") or parse_setting(
        metadata.stdout, "clock.quantum"
    )
    buffer_period_ms = round(quantum / rate * 1000, 3) if rate and quantum else None
    motu_alsa = contains_device(alsa_audio_text, "motu-m2")
    motu_pipewire = contains_device(pipewire_text, "motu-m2")
    roland_alsa_audio = contains_device(alsa_audio_text, "roland-fp-30x")
    roland_pipewire = contains_device(pipewire_text, "roland-fp-30x")
    roland_midi = contains_device(midi_text, "roland-fp-30x")
    # Physical presence is fail-closed: defaults and PipeWire labels are configuration/graph
    # evidence, not proof that a USB device is currently attached.
    motu = motu_alsa
    roland = roland_midi
    sink = normalize_endpoint(sink_name)
    source = normalize_endpoint(source_name)
    pioneer_observed = bool(re.search(r"Pioneer|VSX-?830", eld_text, re.IGNORECASE))
    bluetooth_active = bluetooth.stdout.strip() == "active"

    warnings: list[dict[str, str]] = []
    if source != "motu-m2":
        warnings.append(
            {
                "code": "voice-source-not-motu",
                "severity": "high",
                "detail": "Default capture source is not the MOTU M2 microphone input.",
            }
        )
    configured_endpoints = {"default_sink": sink, "default_source": source}
    observed_presence = {"motu-m2": motu, "roland-fp-30x": roland}
    for role, endpoint in configured_endpoints.items():
        if endpoint in observed_presence and not observed_presence[endpoint]:
            warnings.append(
                {
                    "code": "configured-default-device-absent",
                    "severity": "high",
                    "detail": f"{role} names {endpoint}, but current physical observation does not confirm it.",
                }
            )
    if quantum and quantum >= 1024:
        warnings.append(
            {
                "code": "high-live-quantum",
                "severity": "medium",
                "detail": "The current quantum favors stability over low-latency live monitoring.",
            }
        )
    if "44100Hz" in sources.stdout or "44100Hz" in sinks.stdout:
        warnings.append(
            {
                "code": "mixed-sample-rates",
                "severity": "medium",
                "detail": "At least one endpoint runs at 44.1 kHz inside the 48 kHz graph.",
            }
        )
    if not bluetooth_active:
        warnings.append(
            {
                "code": "bluetooth-service-inactive",
                "severity": "info",
                "detail": "The system Bluetooth service is inactive; an external transmitter remains unobservable.",
            }
        )

    return {
        "schema_version": 1,
        "kind": "audio_doctor_report",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "read_only_contract": True,
        # Backward-compatible physical-presence projection used by profile planning.
        "hardware": {"motu_m2": motu, "roland_fp_30x": roland},
        "device_truth": {
            "observed": {
                "motu_m2": {
                    "present": motu,
                    "alsa_audio": motu_alsa,
                    "pipewire_graph": motu_pipewire,
                },
                "roland_fp_30x": {
                    "present": roland,
                    "alsa_audio": roland_alsa_audio,
                    "pipewire_graph": roland_pipewire,
                    "alsa_midi": roland_midi,
                },
            },
            "configured_defaults": configured_endpoints,
            "desired": {device: True for device in desired_hardware()},
        },
        "graph": {
            "default_sink": sink,
            "default_source": source,
            "force_rate_hz": rate,
            "force_quantum_frames": quantum,
            "single_buffer_period_ms": buffer_period_ms,
            "round_trip_latency_ms": None,
        },
        "external_endpoints": {
            "pioneer_vsx_830_k": {
                "software_observed": pioneer_observed,
                "physical_connection": None,
            },
            "transmitter_1mii_b03_pro": {
                "software_observed": False,
                "bluetooth_service_active": bluetooth_active,
                "physical_connection": None,
                "codec": None,
            },
        },
        "profiles": {
            "headphone_listening": {
                "software_ready": motu and sink == "motu-m2",
                "physical_ready": None,
            },
            "voice_recording": {
                "software_ready": motu and source == "motu-m2",
                "physical_ready": None,
            },
            "piano_software_monitoring": {
                "software_ready": motu and roland and sink == "motu-m2",
                "physical_ready": None,
            },
        },
        "physical_unknowns": physical_unknowns(),
        "warnings": warnings,
        "input_health": {
            "eld": {
                "truncated": bool(getattr(eld_text, "truncated", False)),
                "observed_files": getattr(eld_text, "observed_items", None),
                "max_files": getattr(eld_text, "max_items", MAX_ELD_FILES),
                "max_bytes": getattr(eld_text, "max_bytes", MAX_ELD_BYTES),
                "retained_bytes": len(eld_text.encode("utf-8")),
            }
        },
        "command_health": [
            {
                "command": " ".join(result.argv),
                "available": result.error is None,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
                "stdout_retained_bytes": len(result.stdout.encode("utf-8")),
                "stderr_retained_bytes": len(result.stderr.encode("utf-8")),
                "max_output_bytes_per_stream": MAX_COMMAND_OUTPUT_BYTES,
            }
            for result in result_list
        ],
    }


def absolute_without_resolution(path: pathlib.Path) -> pathlib.Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = pathlib.Path.cwd() / expanded
    if any(part in {"", ".", ".."} for part in expanded.parts[1:]):
        raise OSError(f"unsafe output path component: {path}")
    return expanded


def open_directory_chain(
    path: pathlib.Path, *, create: bool = False
) -> tuple[pathlib.Path, int]:
    absolute = absolute_without_resolution(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return absolute, descriptor
    except Exception:
        os.close(descriptor)
        raise


def atomic_write_output(path: pathlib.Path, payload: str) -> None:
    absolute = absolute_without_resolution(path)
    encoded = payload.encode("utf-8")
    _, parent_fd = open_directory_chain(absolute.parent, create=True)
    temporary_name = f".{absolute.name}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    try:
        mode = 0o600
        try:
            existing = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                raise OSError(f"output path is not a regular file: {path}")
            mode = stat.S_IMODE(existing.st_mode)

        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            absolute.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    results = [run_read_only(command) for command in READ_ONLY_COMMANDS]
    report = build_report(results, read_eld_text())
    encoded = (
        json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None) + "\n"
    )
    encoded = redact(encoded)
    if args.output:
        atomic_write_output(args.output, encoded)
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
