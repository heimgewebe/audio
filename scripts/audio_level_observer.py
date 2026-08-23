#!/usr/bin/env python3
"""Publish bounded Peak/RMS from the exact MOTU source used by the recorder."""

from __future__ import annotations

import argparse
import array
import contextlib
import importlib.util
import json
import math
import os
import pathlib
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOTU_CAPTURE_IDENTITY_PATH = ROOT / "scripts" / "motu_capture_identity.py"


def load_motu_capture_identity() -> Any:
    spec = importlib.util.spec_from_file_location(
        "motu_capture_identity_for_audio_level_observer", MOTU_CAPTURE_IDENTITY_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("MOTU capture identity helper cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MOTU_CAPTURE_IDENTITY = load_motu_capture_identity()

OBSERVER_ID = "audio-control-level-observer-v1"
OBSERVER_MODE = "active-recorder-source-capture"
PW_CAT = pathlib.Path("/usr/bin/pw-cat")
PACTL = pathlib.Path("/usr/bin/pactl")
SAMPLE_RATE_HZ = 48_000
CHANNEL_NAMES = ("FL", "FR")
CHANNEL_COUNT = len(CHANNEL_NAMES)
SAMPLE_WIDTH_BYTES = 4
WINDOW_FRAMES = 24_000
WINDOW_SECONDS = WINDOW_FRAMES / SAMPLE_RATE_HZ
WINDOW_BYTES = WINDOW_FRAMES * CHANNEL_COUNT * SAMPLE_WIDTH_BYTES
READ_BYTES = 65_536
MAX_PENDING_BYTES = WINDOW_BYTES + READ_BYTES
MAX_STDERR_BYTES = 16_384
MAX_JSON_BYTES = 8_192
SILENCE_FLOOR_DBFS = -160.0
CAPTURE_IDLE_TIMEOUT_SECONDS = 5.0
SELECT_TIMEOUT_SECONDS = 1.0
PROCESS_STOP_TIMEOUT_SECONDS = 3.0


class ObserverError(RuntimeError):
    """A controlled observer failure that systemd may restart."""


class SourceUnavailable(ObserverError):
    """The exact recorder source is absent; the observer may wait for hotplug."""


SOURCE_RETRY_SECONDS = 2.0
SOURCE_REVALIDATION_SECONDS = 1.0


def linear_to_dbfs(level: float) -> float:
    """Convert a non-negative linear full-scale ratio to bounded dBFS."""

    if not math.isfinite(level) or level < 0.0:
        raise ValueError("linear level must be finite and non-negative")
    if level == 0.0:
        return SILENCE_FLOOR_DBFS
    return round(max(SILENCE_FLOOR_DBFS, min(0.0, 20.0 * math.log10(level))), 3)


def _motu_source_binding(source: Any) -> dict[str, Any] | None:
    identity = MOTU_CAPTURE_IDENTITY.source_identity(source)
    if identity is None:
        return None
    if (
        identity["sample_format"] != "s32le"
        or identity["sample_rate_hz"] != SAMPLE_RATE_HZ
        or identity["channels"] != CHANNEL_COUNT
        or identity["muted"] is not False
        or identity["unity_volume"] is not True
    ):
        raise ValueError("MOTU source does not satisfy the recorder capture contract")
    name = source.get("name")
    if not isinstance(name, str):
        raise ValueError("MOTU source node name is invalid")
    return {
        "target": name,
        "source_identity_sha256": MOTU_CAPTURE_IDENTITY.canonical_value_sha256(identity),
        "channel_map": "front-left,front-right",
    }


def resolve_recorder_source() -> dict[str, Any]:
    if not PACTL.is_file() or not os.access(PACTL, os.X_OK):
        raise ObserverError(f"PulseAudio/PipeWire query executable is unavailable: {PACTL}")
    try:
        completed = subprocess.run(
            [str(PACTL), "--format=json", "list", "sources"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_capture_environment(),
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ObserverError("MOTU recorder source query failed") from error
    if completed.returncode != 0 or len(completed.stdout) > 262_144:
        raise ObserverError("MOTU recorder source query failed or exceeded its bound")
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ObserverError("MOTU recorder source query returned invalid JSON") from error
    if not isinstance(payload, list):
        raise ObserverError("MOTU recorder source query returned a foreign contract")
    matches: list[dict[str, Any]] = []
    errors: list[str] = []
    for source in payload:
        try:
            binding = _motu_source_binding(source)
        except ValueError as error:
            errors.append(str(error))
            continue
        if binding is not None:
            matches.append(binding)
    if errors:
        raise ObserverError("MOTU recorder source identity is invalid")
    if not matches:
        raise SourceUnavailable("MOTU recorder source is currently unavailable")
    if len(matches) != 1:
        raise ObserverError("MOTU recorder source is ambiguous")
    return matches[0]


def recorder_source_binding_is_current(expected: dict[str, Any]) -> bool:
    current = resolve_recorder_source()
    return (
        current["source_identity_sha256"] == expected.get("source_identity_sha256")
        and current["target"] == expected.get("target")
        and current["channel_map"] == expected.get("channel_map")
    )


def revalidate_active_source(output: pathlib.Path, expected: dict[str, Any]) -> bool:
    """Fail closed when an active source drifts, disappears, or stops validating."""
    try:
        current = recorder_source_binding_is_current(expected)
    except ObserverError:
        current = False
    if not current:
        with contextlib.suppress(ObserverError, OSError):
            remove_output(output)
    return current


def analyze_f32le(payload: bytes, *, channels: int = CHANNEL_COUNT) -> dict[str, Any]:
    """Calculate real sample peak and RMS for interleaved little-endian f32 PCM."""

    frame_bytes = channels * SAMPLE_WIDTH_BYTES
    if channels < 1 or channels > 8:
        raise ValueError("channel count must be between 1 and 8")
    if not payload or len(payload) % frame_bytes:
        raise ValueError("PCM payload must contain complete non-empty frames")
    samples = array.array("f")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    if len(samples) != len(payload) // SAMPLE_WIDTH_BYTES:
        raise ValueError("decoded PCM sample count is inconsistent")

    peaks = [0.0] * channels
    square_sums = [0.0] * channels
    clipped_samples = [0] * channels
    for index, sample in enumerate(samples):
        value = float(sample)
        if not math.isfinite(value):
            raise ValueError("PCM payload contains a non-finite sample")
        channel = index % channels
        magnitude = abs(value)
        peaks[channel] = max(peaks[channel], magnitude)
        square_sums[channel] += value * value
        if magnitude >= 1.0:
            clipped_samples[channel] += 1

    frames = len(samples) // channels
    channel_analysis: list[dict[str, Any]] = []
    for channel in range(channels):
        rms = math.sqrt(square_sums[channel] / frames)
        channel_analysis.append(
            {
                "channel": CHANNEL_NAMES[channel] if channels == CHANNEL_COUNT else channel + 1,
                "peak_dbfs": linear_to_dbfs(peaks[channel]),
                "rms_dbfs": linear_to_dbfs(rms),
                "clipped_samples": clipped_samples[channel],
            }
        )

    overall_peak = max(peaks)
    overall_rms = math.sqrt(sum(square_sums) / len(samples))
    return {
        "frames": frames,
        "peak_dbfs": linear_to_dbfs(overall_peak),
        "rms_dbfs": linear_to_dbfs(overall_rms),
        "clipping": any(clipped_samples),
        "clipped_samples": sum(clipped_samples),
        "channels_analysis": channel_analysis,
    }


def build_pw_cat_command(target: str) -> list[str]:
    """Build the one permitted exact recorder-source PipeWire capture command."""

    if (
        not isinstance(target, str)
        or not target
        or len(target) > 255
        or target == "auto"
        or not target.startswith("alsa_input.usb-MOTU_M2_")
    ):
        raise ValueError("PipeWire recorder target must be the exact bounded MOTU source")
    properties = json.dumps(
        {
            "media.name": "Audio Control Level Observer",
            "node.description": "Audio Control Level Observer",
            "node.name": OBSERVER_ID,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return [
        str(PW_CAT),
        "--record",
        "--target",
        target,
        "--rate",
        str(SAMPLE_RATE_HZ),
        "--channels",
        str(CHANNEL_COUNT),
        "--channel-map",
        "stereo",
        "--format",
        "f32",
        "--latency",
        "100ms",
        "--media-role",
        "Production",
        "--properties",
        properties,
        "-",
    ]


def observation_payload(
    measurement: dict[str, Any],
    *,
    sequence: int,
    observed_at: float,
    source_binding: dict[str, Any],
) -> dict[str, Any]:
    if sequence < 1:
        raise ValueError("observation sequence must be positive")
    if not math.isfinite(observed_at) or observed_at <= 0.0:
        raise ValueError("observation timestamp must be finite and positive")
    return {
        "schema_version": 1,
        "kind": "audio_level_observation",
        "observer": OBSERVER_ID,
        "observer_mode": OBSERVER_MODE,
        "capture_transport": "pipewire-native-exact-source-stream",
        "source_selection": "recorder-bound-motu-source",
        "source_identity_sha256": source_binding["source_identity_sha256"],
        "channel_map": source_binding["channel_map"],
        "sample_format": "f32le",
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "channels": CHANNEL_COUNT,
        "channel": "/".join(CHANNEL_NAMES),
        "window_frames": measurement["frames"],
        "window_seconds": round(measurement["frames"] / SAMPLE_RATE_HZ, 6),
        "floor_dbfs": SILENCE_FLOOR_DBFS,
        "sequence": sequence,
        "observed_at_unix": round(observed_at, 6),
        "peak_dbfs": measurement["peak_dbfs"],
        "rms_dbfs": measurement["rms_dbfs"],
        "clipping": measurement["clipping"],
        "clipped_samples": measurement["clipped_samples"],
        "channels_analysis": measurement["channels_analysis"],
    }


def encode_observation(payload: dict[str, Any]) -> bytes:
    try:
        encoded = (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ObserverError("level observation is not finite JSON") from error
    if len(encoded) > MAX_JSON_BYTES:
        raise ObserverError("level observation exceeds its output bound")
    return encoded


def atomic_write_observation(path: pathlib.Path, payload: dict[str, Any]) -> None:
    """Replace one private regular JSON file without exposing a partial write."""

    if not path.is_absolute():
        raise ObserverError("level output path must be absolute")
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as error:
        raise ObserverError("level output directory is unavailable") from error
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
        raise ObserverError("level output directory is not a trusted directory")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or stat.S_ISLNK(existing.st_mode)
    ):
        raise ObserverError("level output must be absent or a regular file")

    encoded = encode_observation(payload)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = pathlib.Path(temporary_name)
    directory_descriptor = -1
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(directory_descriptor)
    finally:
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def remove_output(path: pathlib.Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ObserverError("refusing to remove an untrusted level output")
    path.unlink()


def _capture_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("HOME", "XDG_RUNTIME_DIR", "PIPEWIRE_REMOTE", "PULSE_SERVER", "PULSE_COOKIE")
        if key in os.environ
    }
    environment.update({"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
    return environment


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)


def run_observer(output: pathlib.Path) -> int:
    if not PW_CAT.is_file() or not os.access(PW_CAT, os.X_OK):
        raise ObserverError(f"PipeWire capture executable is unavailable: {PW_CAT}")
    remove_output(output)
    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    sequence = 0
    try:
        while not stop_requested:
            try:
                source_binding = resolve_recorder_source()
            except SourceUnavailable:
                with contextlib.suppress(ObserverError, OSError):
                    remove_output(output)
                time.sleep(SOURCE_RETRY_SECONDS)
                continue
            target = source_binding["target"]
            process = subprocess.Popen(
                build_pw_cat_command(target),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_capture_environment(),
                start_new_session=True,
                close_fds=True,
            )
            assert process.stdout is not None
            assert process.stderr is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            pending = bytearray()
            stderr_tail = bytearray()
            last_source_validation_at = time.monotonic()
            last_audio_at = time.monotonic()
            source_changed_or_absent = False
            try:
                while not stop_requested:
                    events = selector.select(SELECT_TIMEOUT_SECONDS)
                    for key, _mask in events:
                        try:
                            chunk = os.read(key.fileobj.fileno(), READ_BYTES)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            with contextlib.suppress(KeyError):
                                selector.unregister(key.fileobj)
                            continue
                        if key.data == "stderr":
                            stderr_tail.extend(chunk)
                            if len(stderr_tail) > MAX_STDERR_BYTES:
                                del stderr_tail[:-MAX_STDERR_BYTES]
                            continue
                        pending.extend(chunk)
                        last_audio_at = time.monotonic()
                        if len(pending) > MAX_PENDING_BYTES:
                            raise ObserverError("PipeWire PCM buffer exceeded its bound")
                        while len(pending) >= WINDOW_BYTES:
                            window = bytes(pending[:WINDOW_BYTES])
                            del pending[:WINDOW_BYTES]
                            measurement = analyze_f32le(window)
                            sequence += 1
                            atomic_write_observation(
                                output,
                                observation_payload(
                                    measurement,
                                    sequence=sequence,
                                    observed_at=time.time(),
                                    source_binding=source_binding,
                                ),
                            )
                    now = time.monotonic()
                    if now - last_source_validation_at >= SOURCE_REVALIDATION_SECONDS:
                        last_source_validation_at = now
                        if not revalidate_active_source(output, source_binding):
                            source_changed_or_absent = True
                            break
                    returncode = process.poll()
                    idle = time.monotonic() - last_audio_at > CAPTURE_IDLE_TIMEOUT_SECONDS
                    if returncode is not None or idle:
                        try:
                            current = recorder_source_binding_is_current(source_binding)
                        except ObserverError:
                            current = False
                        if not current:
                            source_changed_or_absent = True
                            break
                        if returncode is not None:
                            detail = stderr_tail.decode("utf-8", errors="replace").strip()
                            suffix = f": {detail[-240:]}" if detail else ""
                            raise ObserverError(
                                f"pw-cat exited unexpectedly with status {returncode}{suffix}"
                            )
                        raise ObserverError("PipeWire capture produced no audio within its time bound")
            finally:
                selector.close()
                _terminate_process(process)
                with contextlib.suppress(ObserverError, OSError):
                    remove_output(output)
            if source_changed_or_absent and not stop_requested:
                time.sleep(SOURCE_RETRY_SECONDS)
        return 0
    finally:
        with contextlib.suppress(ObserverError, OSError):
            remove_output(output)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def check_report() -> dict[str, Any]:
    measurement = analyze_f32le(array.array("f", [0.5, -0.5, 0.0, 0.0]).tobytes())
    source_binding = {
        "target": "alsa_input.usb-MOTU_M2_CHECK-00.analog-stereo",
        "source_identity_sha256": "0" * 64,
        "channel_map": "front-left,front-right",
    }
    payload = observation_payload(
        measurement, sequence=1, observed_at=1.0, source_binding=source_binding
    )
    encode_observation(payload)
    return {
        "schema_version": 1,
        "kind": "audio_level_observer_check",
        "status": "pass",
        "observer": OBSERVER_ID,
        "observer_mode": OBSERVER_MODE,
        "capture_transport": "pipewire-native-exact-source-stream",
        "source_selection": "recorder-bound-motu-source",
        "source_identity_sha256": source_binding["source_identity_sha256"],
        "opens_active_capture_stream": True,
        "uses_direct_alsa_capture": False,
        "changes_pipewire_defaults": False,
        "output_bound_bytes": MAX_JSON_BYTES,
        "window_seconds": WINDOW_SECONDS,
        "command": build_pw_cat_command(source_binding["target"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="start the active PipeWire observer")
    run_parser.add_argument("--output", type=pathlib.Path, required=True)
    subparsers.add_parser("check", help="validate the bounded observer contract")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "check":
            print(json.dumps(check_report(), ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        return run_observer(args.output)
    except (ObserverError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "audio_level_observer_error",
                    "error": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
