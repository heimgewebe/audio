#!/usr/bin/env python3
"""Capture a read-only Heim-PC audio baseline.

Private evidence stays outside Git. The public projection contains normalized,
non-secret facts suitable for the public repository.
"""
from __future__ import annotations

import argparse
import configparser
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_OUTPUT_BYTES = 262144
COMMAND_TIMEOUT = 12

READ_ONLY_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("uname", "-a"),
    ("lsusb",),
    ("aplay", "-l"),
    ("arecord", "-l"),
    ("amidi", "-l"),
    ("pactl", "info"),
    ("pactl", "list", "short", "cards"),
    ("pactl", "list", "short", "sinks"),
    ("pactl", "list", "short", "sources"),
    ("pactl", "list", "short", "sink-inputs"),
    ("pactl", "list", "short", "source-outputs"),
    ("wpctl", "status", "--name"),
    ("pw-metadata", "-n", "settings"),
    ("pw-top", "-b", "-n", "1"),
    ("systemctl", "--user", "is-active", "pipewire", "pipewire-pulse", "wireplumber", "mopidy", "easyeffects"),
    ("systemctl", "--user", "list-unit-files", "--no-pager", "--no-legend"),
    ("systemctl", "--user", "list-timers", "--all", "--no-pager", "--no-legend"),
    ("ps", "-eo", "pid,ppid,stat,etimes,%cpu,%mem,comm,args"),
    ("flatpak", "list", "--app", "--columns=application,version,runtime"),
    ("dpkg-query", "-W", "-f=${binary:Package}\\t${Version}\\n"),
    ("df", "-B1", "/"),
    ("journalctl", "--user", "-u", "pipewire", "-u", "wireplumber", "-u", "mopidy", "--since", "30 days ago", "--no-pager", "-n", "1200"),
    ("journalctl", "-k", "--since", "30 days ago", "--no-pager", "-n", "2500"),
)

FORBIDDEN_MUTATION_TOKENS = {
    "start", "stop", "restart", "enable", "disable", "mask", "unmask",
    "set-default-sink", "set-default-source", "pw-link", "rm", "mv", "cp",
    "install", "remove", "purge", "truncate", "tee",
}
AUDIO_KEYWORDS = (
    "audio", "alsa", "ardour", "bluetooth", "carla", "dauersong", "easyeffects",
    "fluidsynth", "jack", "midi", "mopidy", "motu", "pipewire", "pioneer",
    "qobuz", "qsynth", "record", "roland", "rode", "sfizz", "xrun",
)
CONFIG_ROOTS = (
    Path.home() / ".config" / "pipewire",
    Path.home() / ".config" / "wireplumber",
    Path.home() / ".config" / "easyeffects",
    Path.home() / ".config" / "mopidy",
    Path.home() / ".config" / "systemd" / "user",
)
SELECTED_PACKAGES = (
    "alsa", "ardour", "carla", "easyeffects", "fluidsynth", "jack", "mopidy",
    "pipewire", "pulseaudio", "qsynth", "wireplumber",
)
SECRET_LINE = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|client[_-]?secret|refresh[_-]?token)\s*[:=]")
URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.I)
MAC_ADDRESS = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I)
IPV4_ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SERIAL_ASSIGNMENT = re.compile(r"(?i)(serial(?:number)?|device\.serial|object\.serial)\s*[:=]\s*\S+")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def redact_text(text: str, *, hostname: str, username: str, redact_network: bool = True) -> str:
    text = text.replace(str(Path.home()), "~")
    if username:
        text = re.sub(rf"(?<![A-Za-z0-9_-]){re.escape(username)}(?![A-Za-z0-9_-])", "<user>", text)
    if hostname:
        text = re.sub(rf"(?<![A-Za-z0-9_.-]){re.escape(hostname)}(?![A-Za-z0-9_.-])", "<host>", text)
    text = URL_CREDENTIALS.sub(r"\g<scheme><redacted>@", text)
    if redact_network:
        text = MAC_ADDRESS.sub("<mac>", text)
        text = IPV4_ADDRESS.sub("<ipv4>", text)
    text = SERIAL_ASSIGNMENT.sub(lambda match: match.group(1) + "=<redacted>", text)
    lines: list[str] = []
    for line in text.splitlines():
        if SECRET_LINE.search(line):
            key = re.split(r"[:=]", line, maxsplit=1)[0]
            lines.append(f"{key}=<redacted>")
        else:
            lines.append(line)
    return "\n".join(lines)


def run_command(argv: tuple[str, ...], *, hostname: str, username: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=COMMAND_TIMEOUT,
            check=False,
            env={**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "LANGUAGE": "C"},
        )
    except FileNotFoundError:
        return {"argv": list(argv), "unavailable": True, "reason": "command-not-found"}
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        return {
            "argv": list(argv),
            "timed_out": True,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "stdout": redact_text(stdout[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"), hostname=hostname, username=username, redact_network=argv[0] != "dpkg-query"),
            "stderr": redact_text(stderr[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"), hostname=hostname, username=username, redact_network=argv[0] != "dpkg-query"),
            "stdout_truncated": len(stdout) > MAX_OUTPUT_BYTES,
            "stderr_truncated": len(stderr) > MAX_OUTPUT_BYTES,
        }
    return {
        "argv": list(argv),
        "returncode": result.returncode,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout": redact_text(result.stdout[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"), hostname=hostname, username=username, redact_network=argv[0] != "dpkg-query"),
        "stderr": redact_text(result.stderr[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"), hostname=hostname, username=username, redact_network=argv[0] != "dpkg-query"),
        "stdout_sha256_full": sha256_bytes(result.stdout),
        "stderr_sha256_full": sha256_bytes(result.stderr),
        "stdout_truncated": len(result.stdout) > MAX_OUTPUT_BYTES,
        "stderr_truncated": len(result.stderr) > MAX_OUTPUT_BYTES,
    }


def assert_read_only_commands() -> None:
    violations: list[str] = []
    for command in READ_ONLY_COMMANDS:
        hit = sorted({token.lower() for token in command} & FORBIDDEN_MUTATION_TOKENS)
        if hit:
            violations.append(f"{shlex.join(command)}: {', '.join(hit)}")
    if violations:
        raise RuntimeError("mutation-capable baseline command: " + "; ".join(violations))


def command_by_prefix(commands: list[dict[str, Any]], prefix: tuple[str, ...]) -> dict[str, Any]:
    for record in commands:
        argv = tuple(record.get("argv", []))
        if argv[: len(prefix)] == prefix:
            return record
    return {}


def normalize_device_name(value: str) -> str:
    lower = value.lower()
    if "motu" in lower or re.search(r"(?:^|[_. -])m2(?:$|[_. -])", lower):
        return "motu-m2"
    if "roland" in lower or "digital_piano" in lower or "digital piano" in lower or "fp-30" in lower:
        return "roland-fp-30x"
    if "blue" in lower or "bluetooth" in lower:
        return "bluetooth-audio"
    return re.sub(r"[0-9a-f]{8,}", "<id>", value, flags=re.I)[:160] if value else "unknown"


def parse_pactl_defaults(text: str) -> dict[str, str | None]:
    """Parse pactl defaults independent of the installed translation."""
    values: dict[str, str | None] = {"default_sink": None, "default_source": None}
    sink_labels = ("Default Sink:", "Standard Sink:", "Standard-Ziel:")
    source_labels = ("Default Source:", "Standard Source:", "Standard-Quelle:")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(sink_labels):
            values["default_sink"] = normalize_device_name(line.split(":", 1)[1].strip())
        elif line.startswith(source_labels):
            values["default_source"] = normalize_device_name(line.split(":", 1)[1].strip())
    return values


def parse_wpctl_configured_defaults(text: str) -> dict[str, str | None]:
    """Read WirePlumber's configured default targets from wpctl status."""
    values: dict[str, str | None] = {
        "configured_default_sink": None,
        "configured_default_source": None,
    }
    for line in text.splitlines():
        sink = re.search(r"Audio/Sink\s+(\S+)", line)
        source = re.search(r"Audio/Source\s+(\S+)", line)
        if sink:
            values["configured_default_sink"] = normalize_device_name(sink.group(1))
        if source:
            values["configured_default_source"] = normalize_device_name(source.group(1))
    return values


def parse_metadata(text: str) -> dict[str, int | str | None]:
    result: dict[str, int | str | None] = {"force_rate": None, "force_quantum": None}
    for line in text.splitlines():
        for key, output_key in (("clock.force-rate", "force_rate"), ("clock.force-quantum", "force_quantum")):
            if key in line:
                match = re.search(r"value:'([^']*)'", line)
                if match:
                    value = match.group(1)
                    result[output_key] = int(value) if value.isdigit() else value
    return result


def detect_hardware(*texts: str) -> dict[str, bool]:
    joined = "\n".join(texts).lower()
    return {
        "motu_m2": bool(re.search(r"\bmotu\b|\bm2\b|motu_m2", joined)),
        "roland_fp_30x": bool(re.search(r"\broland\b|digital[ _-]piano|fp-30", joined)),
    }


def filtered_lines(text: str, limit: int) -> list[str]:
    result: list[str] = []
    for line in text.splitlines():
        lower = line.lower()
        if any(keyword in lower for keyword in AUDIO_KEYWORDS):
            result.append(line[:1000])
            if len(result) >= limit:
                break
    return result


def mopidy_projection() -> dict[str, Any]:
    path = Path.home() / ".config" / "mopidy" / "mopidy.conf"
    result: dict[str, Any] = {"present": path.is_file(), "audio_output": None, "sections": []}
    if not path.is_file():
        return result
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError) as exc:
        result["parse_error"] = type(exc).__name__
        return result
    result["sections"] = sorted(parser.sections())
    output = parser.get("audio", "output", fallback="")
    if "pulse" in output.lower():
        result["audio_output"] = "pulse-mixed"
    elif "alsa" in output.lower():
        result["audio_output"] = "alsa-direct"
    elif output:
        result["audio_output"] = "other"
    return result


def config_manifest() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for root in CONFIG_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                metadata = path.stat()
                data = path.read_bytes()
            except OSError:
                continue
            if len(data) > 10 * 1024 * 1024:
                continue
            records.append({
                "path": str(path).replace(str(Path.home()), "~"),
                "bytes": len(data),
                "mode": stat.filemode(metadata.st_mode),
                "sha256": sha256_bytes(data),
            })
    return records


def package_projection(text: str) -> dict[str, str]:
    packages: dict[str, str] = {}
    for line in text.splitlines():
        if "\t" not in line:
            continue
        name, version = line.split("\t", 1)
        if any(token in name.lower() for token in SELECTED_PACKAGES):
            packages[name] = version
    return dict(sorted(packages.items()))


def pipewire_projection(hostname: str, username: str) -> list[dict[str, Any]]:
    try:
        result = subprocess.run(("pw-dump",), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=COMMAND_TIMEOUT, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    items: list[dict[str, Any]] = []
    for obj in payload:
        props = ((obj.get("info") or {}).get("props") or {})
        haystack = " ".join(str(props.get(key, "")) for key in (
            "node.name", "node.description", "device.name", "device.description",
            "media.class", "object.path", "api.alsa.card.name",
        )).lower()
        if not any(token in haystack for token in ("motu", "roland", "digital piano", "m2", "midi")):
            continue
        selected: dict[str, Any] = {}
        for key in (
            "media.class", "node.name", "node.description", "device.name",
            "device.description", "api.alsa.card.name", "audio.rate",
            "audio.channels", "port.name", "port.direction",
        ):
            if key in props:
                value = redact_text(str(props[key]), hostname=hostname, username=username)
                selected[key] = normalize_device_name(value) if key.endswith(("name", "description")) else value
        items.append({"type": obj.get("type"), "properties": selected})
    return items[:200]


def write_json(path: Path, payload: dict[str, Any], mode: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256_bytes(encoded)


def render_summary(public: dict[str, Any]) -> str:
    services = "\n".join(f"| `{name}` | {state} |" for name, state in public["services"].items())
    findings = "\n".join(f"- **{item['severity']}**: {item['message']}" for item in public["findings"])
    return f"""# Heim-PC Audio-Baseline – 2026-07-27

- Status: read-only erfasst
- Schema: {public['schema_version']}
- Privater Beleg-SHA-256: `{public['private_receipt_sha256']}`

## Hardwareerkennung

| Gerät | Digital erkannt |
|---|---|
| MOTU M2 | {str(public['hardware']['motu_m2']).lower()} |
| Roland FP-30X | {str(public['hardware']['roland_fp_30x']).lower()} |

Physisch nicht softwareseitig prüfbar bleiben Rode NT1-A, Lake People G111 Mk 2,
Focal Clear MG, Pioneer VSX-830-K und 1MII B03 Pro samt Verkabelung und
Schalterstellungen.

## PipeWire

- Aktive Standardausgabe: `{public['pipewire']['default_sink']}`
- Aktive Standardaufnahme: `{public['pipewire']['default_source']}`
- Gespeicherte Standardausgabe: `{public['pipewire']['configured_default_sink']}`
- Gespeicherte Standardaufnahme: `{public['pipewire']['configured_default_source']}`
- Erzwungene Rate: `{public['pipewire']['force_rate']}`
- Erzwungenes Quantum: `{public['pipewire']['force_quantum']}`
- Qobuz/Mopidy-Ausgabe: `{public['qobuz']['mopidy_audio_output']}`

## Dienste

| Dienst | Zustand |
|---|---|
{services}

## Befunde

{findings or '- Keine automatisch abgeleiteten Befunde.'}

## Offene Messungen vor einer Neukonfiguration

- physischer Signalweg und Gain-Stellungen
- tatsächlicher 1MII-Bluetooth-Codec
- Round-Trip-Latenz und XRuns je Profil
- reproduzierbarer MOTU-USB-Abbruchtest
- bitgenauer Qobuz-Nachweis
- kanonische Ardour- und Plugin-Auswahl

Diese Baseline autorisiert und vollzieht **keine** produktive Audioänderung.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-output", required=True, type=Path)
    parser.add_argument("--public-output", required=True, type=Path)
    parser.add_argument("--summary-output", required=True, type=Path)
    args = parser.parse_args()

    assert_read_only_commands()
    hostname = socket.gethostname()
    username = os.environ.get("USER", "")
    commands = [run_command(command, hostname=hostname, username=username) for command in READ_ONLY_COMMANDS]

    pactl_info = command_by_prefix(commands, ("pactl", "info"))
    metadata_record = command_by_prefix(commands, ("pw-metadata", "-n", "settings"))
    source_texts = [
        command_by_prefix(commands, ("lsusb",)).get("stdout", ""),
        command_by_prefix(commands, ("aplay", "-l")).get("stdout", ""),
        command_by_prefix(commands, ("arecord", "-l")).get("stdout", ""),
        command_by_prefix(commands, ("amidi", "-l")).get("stdout", ""),
    ]
    defaults = parse_pactl_defaults(str(pactl_info.get("stdout", "")))
    metadata = parse_metadata(str(metadata_record.get("stdout", "")))
    configured_defaults = parse_wpctl_configured_defaults(
        str(command_by_prefix(commands, ("wpctl", "status", "--name")).get("stdout", ""))
    )
    hardware = detect_hardware(*map(str, source_texts))
    mopidy = mopidy_projection()
    config_files = config_manifest()
    packages = package_projection(str(command_by_prefix(commands, ("dpkg-query", "-W")).get("stdout", "")))
    service_record = command_by_prefix(commands, ("systemctl", "--user", "is-active"))
    service_names = ("pipewire", "pipewire-pulse", "wireplumber", "mopidy", "easyeffects")
    service_lines = str(service_record.get("stdout", "")).splitlines()
    services = {name: (service_lines[index] if index < len(service_lines) else "unknown") for index, name in enumerate(service_names)}
    user_audio_log = filtered_lines(str(command_by_prefix(commands, ("journalctl", "--user")).get("stdout", "")), 300)
    kernel_audio_log = filtered_lines(str(command_by_prefix(commands, ("journalctl", "-k")).get("stdout", "")), 400)

    private_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "heim_pc_audio_baseline_private",
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "read_only_contract": True,
        "platform": {
            "system": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "python": platform.python_version(),
            "hostname_sha256": sha256_bytes(hostname.encode()),
        },
        "commands": commands,
        "pipewire_objects": pipewire_projection(hostname, username),
        "config_manifest": config_files,
        "mopidy": mopidy,
        "audio_log_projection": {"user": user_audio_log, "kernel": kernel_audio_log},
    }
    private_sha = write_json(args.private_output, private_payload, 0o600)

    findings: list[dict[str, str]] = []
    if defaults["default_source"] == "roland-fp-30x":
        findings.append({"severity": "medium", "message": "Die globale Standardaufnahmequelle ist das Roland statt des MOTU; dies ist nur profilabhängig sinnvoll."})
    if configured_defaults["configured_default_sink"] == "motu-m2" and not hardware["motu_m2"]:
        findings.append({"severity": "high", "message": "WirePlumber erwartet das MOTU als Standardausgabe, das Gerät ist aktuell aber abwesend; PipeWire nutzt deshalb einen Fallback."})
    if configured_defaults["configured_default_source"] == "roland-fp-30x" and not hardware["roland_fp_30x"]:
        findings.append({"severity": "medium", "message": "WirePlumber erwartet das Roland als Standardaufnahmequelle, das Gerät ist aktuell aber abwesend."})
    if mopidy.get("audio_output") == "pulse-mixed":
        findings.append({"severity": "medium", "message": "Mopidy/Qobuz nutzt den gemischten Pulse-Pfad; Bitgenauigkeit ist damit nicht belegt."})
    if metadata.get("force_quantum") == 1024:
        findings.append({"severity": "info", "message": "Das aktuelle globale Quantum 1024 priorisiert Robustheit, nicht niedrige Live-Latenz."})
    if any("motu" in line.lower() and any(word in line.lower() for word in ("error", "failed", "not found", "disconnect")) for line in kernel_audio_log + user_audio_log):
        findings.append({"severity": "high", "message": "In den letzten 30 Tagen wurden MOTU-bezogene Fehler protokolliert; die Ursache bleibt zu reproduzieren."})
    if not hardware["motu_m2"]:
        findings.append({"severity": "high", "message": "MOTU M2 war bei der Baseline nicht digital erkennbar."})
    if not hardware["roland_fp_30x"]:
        findings.append({"severity": "high", "message": "Roland FP-30X war bei der Baseline nicht digital erkennbar."})

    public_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "heim_pc_audio_baseline_public",
        "captured_at": private_payload["captured_at"],
        "read_only_contract": True,
        "private_receipt_sha256": private_sha,
        "hardware": hardware,
        "pipewire": {**defaults, **configured_defaults, **metadata, "relevant_object_count": len(private_payload["pipewire_objects"])},
        "services": services,
        "qobuz": {"mopidy_present": mopidy.get("present"), "mopidy_audio_output": mopidy.get("audio_output")},
        "software_versions": packages,
        "config_manifest": {
            "file_count": len(config_files),
            "aggregate_sha256": sha256_bytes(json.dumps(config_files, sort_keys=True).encode()),
        },
        "log_projection": {"user_audio_lines": len(user_audio_log), "kernel_audio_lines": len(kernel_audio_log)},
        "physical_signal_path": {
            "software_observable": False,
            "known_components": ["Rode NT1-A", "MOTU M2", "Lake People G111 Mk 2", "Focal Clear MG", "Pioneer VSX-830-K", "1MII B03 Pro"],
            "unresolved": ["cabling", "gain positions", "phantom power state", "receiver input and mode", "bluetooth codec"],
        },
        "findings": findings,
        "does_not_establish": [
            "audio_redesign_applied", "physical_signal_path_verified", "bit_perfect_qobuz",
            "round_trip_latency", "xrun_free_live_performance", "motu_usb_root_cause",
        ],
    }
    write_json(args.public_output, public_payload, 0o644)
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    args.summary_output.write_text(render_summary(public_payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
