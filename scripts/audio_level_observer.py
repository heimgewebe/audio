#!/usr/bin/env python3
"""Publish bounded live Peak/RMS observations from a shared PipeWire capture."""

from __future__ import annotations

import argparse
import array
import contextlib
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

OBSERVER_ID = "audio-control-level-observer-v1"
OBSERVER_MODE = "active-pipewire-shared-capture"
PW_CAT = pathlib.Path("/usr/bin/pw-cat")
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


def linear_to_dbfs(level: float) -> float:
    """Convert a non-negative linear full-scale ratio to bounded dBFS."""

    if not math.isfinite(level) or level < 0.0:
        raise ValueError("linear level must be finite and non-negative")
    if level == 0.0:
        return SILENCE_FLOOR_DBFS
    return round(max(SILENCE_FLOOR_DBFS, min(0.0, 20.0 * math.log10(level))), 3)


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


def build_pw_cat_command(target: str = "auto") -> list[str]:
    """Build the one permitted active PipeWire capture command."""

    if not isinstance(target, str) or not target or len(target) > 255:
        raise ValueError("PipeWire target must be a non-empty bounded string")
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
    target: str = "auto",
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
        "capture_transport": "pipewire-native-shared-stream",
        "source_selection": (
            "pipewire-default-source" if target == "auto" else "explicit-pipewire-target"
        ),
        "target": target,
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
        for key in ("HOME", "XDG_RUNTIME_DIR", "PIPEWIRE_REMOTE")
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


def run_observer(output: pathlib.Path, *, target: str = "auto") -> int:
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
    sequence = 0
    last_audio_at = time.monotonic()
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
                            target=target,
                        ),
                    )
            returncode = process.poll()
            if returncode is not None:
                detail = stderr_tail.decode("utf-8", errors="replace").strip()
                suffix = f": {detail[-240:]}" if detail else ""
                raise ObserverError(f"pw-cat exited unexpectedly with status {returncode}{suffix}")
            if time.monotonic() - last_audio_at > CAPTURE_IDLE_TIMEOUT_SECONDS:
                raise ObserverError("PipeWire capture produced no audio within its time bound")
        return 0
    finally:
        selector.close()
        _terminate_process(process)
        with contextlib.suppress(ObserverError, OSError):
            remove_output(output)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def check_report() -> dict[str, Any]:
    measurement = analyze_f32le(array.array("f", [0.5, -0.5, 0.0, 0.0]).tobytes())
    payload = observation_payload(measurement, sequence=1, observed_at=1.0)
    encode_observation(payload)
    return {
        "schema_version": 1,
        "kind": "audio_level_observer_check",
        "status": "pass",
        "observer": OBSERVER_ID,
        "observer_mode": OBSERVER_MODE,
        "capture_transport": "pipewire-native-shared-stream",
        "source_selection": "pipewire-default-source",
        "opens_active_capture_stream": True,
        "uses_direct_alsa_capture": False,
        "changes_pipewire_defaults": False,
        "output_bound_bytes": MAX_JSON_BYTES,
        "window_seconds": WINDOW_SECONDS,
        "command": build_pw_cat_command(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="start the active PipeWire observer")
    run_parser.add_argument("--output", type=pathlib.Path, required=True)
    run_parser.add_argument("--target", default="auto")
    subparsers.add_parser("check", help="validate the bounded observer contract")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "check":
            print(json.dumps(check_report(), ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        return run_observer(args.output, target=args.target)
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
