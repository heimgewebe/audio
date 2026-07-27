#!/usr/bin/env python3
"""Managed live runtime for Buckelwal Live Voice v1."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import queue
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass

from whale_live_engine import (
    DEFAULT_BLOCK_FRAMES,
    MAX_MASTER_GAIN,
    MAX_OFFLINE_DURATION_SECONDS,
    MidiEvent,
    WhaleVoice,
    WhaleVoiceConfig,
    default_demo_events,
    parse_aseqdump_line,
    render_timeline,
    signal_metrics,
    write_stereo_wav,
)

UNIT_NAME = "audio-buckelwal-live-voice-v1.service"
ROLAND_PATTERN = re.compile(r"\broland\b|\bfp[- ]?30x?\b", re.IGNORECASE)
PORT_LINE_RE = re.compile(r"^\s*(\d+):(\d+)\s{2,}(.+?)\s{2,}(.+?)\s*$")
MAX_MANAGED_RUNTIME_SECONDS = 21_600
BYTES_PER_STEREO_F32_FRAME = 8
MAX_LOW_LATENCY_PAGE_BYTES = 4_096
PCM_WRITE_POLL_SECONDS = 0.05
PCM_WRITE_STALL_SECONDS = 2.0
LIVE_INITIALIZATION_SECONDS = 0.1
MANAGED_NOTIFY_REQUIRED_ENV = "AUDIO_BUCKELWAL_REQUIRE_NOTIFY"
SERVICE_START_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class MidiPort:
    address: str
    client_name: str
    port_name: str

    @property
    def label(self) -> str:
        return f"{self.client_name} {self.port_name}".strip()


def run_capture(
    argv: list[str], *, timeout: float = 10
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def parse_aseqdump_ports(text: str) -> list[MidiPort]:
    ports: list[MidiPort] = []
    for line in text.splitlines():
        match = PORT_LINE_RE.match(line)
        if not match:
            continue
        client, port, client_name, port_name = match.groups()
        ports.append(
            MidiPort(f"{client}:{port}", client_name.strip(), port_name.strip())
        )
    return ports


def list_midi_ports() -> list[MidiPort]:
    result = run_capture(["aseqdump", "-l"])
    if result.returncode != 0:
        raise RuntimeError(f"aseqdump -l failed: {result.stderr.strip()}")
    return parse_aseqdump_ports(result.stdout)


def resolve_midi_port(requested: str) -> MidiPort:
    ports = list_midi_ports()
    if requested != "auto":
        for port in ports:
            if port.address != requested:
                continue
            if not ROLAND_PATTERN.search(port.label):
                raise RuntimeError(
                    f"requested MIDI port {requested!r} is not Roland-like: {port.label}"
                )
            return port
        raise RuntimeError(f"requested MIDI port {requested!r} is not present")
    matches = [port for port in ports if ROLAND_PATTERN.search(port.label)]
    if not matches:
        visible = ", ".join(f"{port.address} {port.label}" for port in ports) or "none"
        raise RuntimeError(
            "Roland FP-30X MIDI port not found; visible input ports: " + visible
        )
    if len(matches) > 1:
        labels = ", ".join(f"{port.address} {port.label}" for port in matches)
        raise RuntimeError(
            "multiple Roland-like MIDI ports found; select one explicitly: " + labels
        )
    return matches[0]


def runtime_doctor() -> dict[str, object]:
    commands = {
        command: shutil.which(command)
        for command in ("aseqdump", "pw-cat", "systemctl", "systemd-run")
    }
    ports: list[MidiPort] = []
    port_error: str | None = None
    roland_port: MidiPort | None = None
    if commands["aseqdump"]:
        try:
            ports = list_midi_ports()
            matches = [port for port in ports if ROLAND_PATTERN.search(port.label)]
            if len(matches) == 1:
                roland_port = matches[0]
            elif not matches:
                port_error = "roland-midi-not-found"
            else:
                port_error = "roland-midi-ambiguous"
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            port_error = str(error)
    else:
        port_error = "aseqdump-not-installed"

    blocking_reasons = [
        f"{name}-not-installed" for name, path in commands.items() if not path
    ]
    if port_error:
        blocking_reasons.append(port_error)

    system_page_size_bytes: int | None = None
    pcm_pipe_capacity_bytes: int | None = None
    pcm_pipe_error: str | None = None
    try:
        system_page_size_bytes = int(os.sysconf("SC_PAGESIZE"))
        pcm_pipe_capacity_bytes = pcm_pipe_size_bytes(DEFAULT_BLOCK_FRAMES)
    except (OSError, RuntimeError, ValueError) as error:
        pcm_pipe_error = str(error)
        blocking_reasons.append("pcm-pipe-contract-unavailable")

    pipewire_active = False
    if commands["systemctl"]:
        try:
            pipewire = run_capture(
                ["systemctl", "--user", "is-active", "pipewire.service"]
            )
            pipewire_active = (
                pipewire.returncode == 0 and pipewire.stdout.strip() == "active"
            )
        except (OSError, subprocess.TimeoutExpired):
            pipewire_active = False
    if not pipewire_active:
        blocking_reasons.append("pipewire-inactive")

    report = {
        "schema_version": 1,
        "kind": "buckelwal_live_voice_doctor",
        "software": {name: bool(path) for name, path in commands.items()},
        "pipewire_active": pipewire_active,
        "system_page_size_bytes": system_page_size_bytes,
        "pcm_pipe_capacity_bytes": pcm_pipe_capacity_bytes,
        "pcm_pipe_error": pcm_pipe_error,
        "midi_ports": [asdict(port) for port in ports],
        "roland_midi_port": asdict(roland_port) if roland_port else None,
        "ready": not blocking_reasons,
        "blocking_reason": blocking_reasons[0] if blocking_reasons else None,
        "blocking_reasons": blocking_reasons,
        "audio_contract": {
            "sample_rate_hz": 48_000,
            "channels": 2,
            "format": "f32",
            "latency_frames": DEFAULT_BLOCK_FRAMES,
            "maximum_supported_page_size_bytes": MAX_LOW_LATENCY_PAGE_BYTES,
            "maximum_master_gain": MAX_MASTER_GAIN,
        },
    }
    return report


def build_pw_cat_command(*, target: str | None, latency_frames: int) -> list[str]:
    if not 32 <= latency_frames <= 2_048:
        raise ValueError("latency_frames must be between 32 and 2048")
    command = [
        "pw-cat",
        "--playback",
        "--rate",
        "48000",
        "--channels",
        "2",
        "--channel-map",
        "stereo",
        "--format",
        "f32",
        "--latency",
        # pw-cat accepts a unitless value as direct samples; this is not milliseconds.
        str(latency_frames),
        "--media-role",
        "Music",
    ]
    if target:
        command.extend(["--target", target])
    command.append("-")
    return command


def pcm_pipe_size_bytes(block_frames: int) -> int:
    if not 16 <= block_frames <= 4_096:
        raise ValueError("block_frames must be between 16 and 4096")
    page_size = int(os.sysconf("SC_PAGESIZE"))
    if page_size <= 0 or page_size > MAX_LOW_LATENCY_PAGE_BYTES:
        raise RuntimeError(
            "system page size exceeds the low-latency PCM pipe contract: "
            f"{page_size} > {MAX_LOW_LATENCY_PAGE_BYTES}"
        )
    minimum_bytes = max(page_size, block_frames * BYTES_PER_STEREO_F32_FRAME)
    required_pages = (minimum_bytes + page_size - 1) // page_size
    rounded_pages = 1 << (required_pages - 1).bit_length()
    return page_size * rounded_pages


def live_initialization_block_count(sample_rate: int, block_frames: int) -> int:
    if sample_rate <= 0 or block_frames <= 0:
        raise ValueError("sample_rate and block_frames must be positive")
    initialization_frames = max(
        block_frames, round(sample_rate * LIVE_INITIALIZATION_SECONDS)
    )
    return (initialization_frames + block_frames - 1) // block_frames


class RealtimeBlockPacer:
    """Clock one rendered block per real audio block without catch-up bursts."""

    def __init__(
        self,
        sample_rate: int,
        block_frames: int,
        *,
        now_ns=time.monotonic_ns,
        sleeper=time.sleep,
    ) -> None:
        if sample_rate <= 0 or block_frames <= 0:
            raise ValueError("sample_rate and block_frames must be positive")
        self.block_duration_ns = round(block_frames * 1_000_000_000 / sample_rate)
        self._now_ns = now_ns
        self._sleeper = sleeper
        self.next_deadline_ns = now_ns() + self.block_duration_ns

    def wait(self) -> None:
        now = self._now_ns()
        if now < self.next_deadline_ns:
            self._sleeper((self.next_deadline_ns - now) / 1_000_000_000)
            now = self._now_ns()
        if now - self.next_deadline_ns >= self.block_duration_ns:
            self.next_deadline_ns = now + self.block_duration_ns
        else:
            self.next_deadline_ns += self.block_duration_ns


def verified_pipe_capacity_bytes(stream: object, maximum_bytes: int) -> int:
    fileno = getattr(stream, "fileno", None)
    if not callable(fileno):
        raise RuntimeError("PCM pipe has no file descriptor")
    capacity = int(fcntl.fcntl(fileno(), fcntl.F_GETPIPE_SZ))
    if capacity > maximum_bytes:
        raise RuntimeError(
            f"PCM pipe capacity {capacity} exceeds configured maximum {maximum_bytes}"
        )
    return capacity


def write_pcm_block(
    stream: object,
    payload: bytes,
    stop_event: threading.Event,
    process: subprocess.Popen[bytes],
    *,
    stall_timeout_seconds: float = PCM_WRITE_STALL_SECONDS,
    now=time.monotonic,
    wait_for_write=select.select,
) -> bool:
    """Write one PCM block without hiding shutdown behind a full pipe."""

    fileno = getattr(stream, "fileno", None)
    if not callable(fileno):
        raise RuntimeError("PCM pipe has no file descriptor")
    if stall_timeout_seconds <= 0:
        raise ValueError("stall_timeout_seconds must be positive")
    descriptor = fileno()
    view = memoryview(payload)
    offset = 0
    last_progress = now()
    while offset < len(view):
        if stop_event.is_set():
            return False
        if process.poll() is not None:
            raise RuntimeError(f"pw-cat exited with status {process.returncode}")
        try:
            written = os.write(descriptor, view[offset:])
        except BlockingIOError:
            written = 0
        except BrokenPipeError as error:
            raise RuntimeError("PipeWire audio stream closed unexpectedly") from error
        current = now()
        if written > 0:
            offset += written
            last_progress = current
            continue
        remaining = stall_timeout_seconds - (current - last_progress)
        if remaining <= 0:
            raise RuntimeError("PCM pipe stalled without consuming audio")
        try:
            wait_for_write([], [descriptor], [], min(PCM_WRITE_POLL_SECONDS, remaining))
        except InterruptedError:
            continue
    return True


def _midi_reader(
    process: subprocess.Popen[str],
    event_queue: queue.SimpleQueue[MidiEvent | BaseException],
    stop_event: threading.Event,
) -> None:
    assert process.stdout is not None
    try:
        for line in process.stdout:
            if stop_event.is_set():
                break
            event = parse_aseqdump_line(line)
            if event is not None:
                event_queue.put(event)
        if not stop_event.is_set():
            event_queue.put(RuntimeError("aseqdump ended unexpectedly"))
    except BaseException as error:  # forward the exact reader failure to the audio loop
        event_queue.put(error)


def terminate_process(process: subprocess.Popen[object] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def notify_systemd_ready(status: str) -> bool:
    if "\n" in status or "\r" in status:
        raise ValueError("systemd readiness status must be one line")
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    payload = f"READY=1\nSTATUS={status}".encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
        sent = notifier.sendto(payload, address)
    if sent != len(payload):
        raise RuntimeError("systemd readiness notification was truncated")
    return True


def _dispatch_pending_events(
    voice: WhaleVoice,
    event_queue: queue.SimpleQueue[MidiEvent | BaseException],
) -> None:
    while True:
        try:
            item = event_queue.get_nowait()
        except queue.Empty:
            return
        if isinstance(item, BaseException):
            raise item
        voice.dispatch(item)


def _require_live_children(
    midi_process: subprocess.Popen[str], audio_process: subprocess.Popen[bytes]
) -> None:
    if midi_process.poll() is not None:
        raise RuntimeError(f"aseqdump exited with status {midi_process.returncode}")
    if audio_process.poll() is not None:
        raise RuntimeError(f"pw-cat exited with status {audio_process.returncode}")


def run_live(
    *,
    midi_port: str,
    target: str | None,
    gain: float,
    latency_frames: int,
) -> int:
    port = resolve_midi_port(midi_port)
    config = WhaleVoiceConfig(master_gain=gain, block_frames=latency_frames)
    voice = WhaleVoice(config)
    stop_event = threading.Event()
    event_queue: queue.SimpleQueue[MidiEvent | BaseException] = queue.SimpleQueue()
    midi_process: subprocess.Popen[str] | None = None
    audio_process: subprocess.Popen[bytes] | None = None

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    old_sigint = signal.signal(signal.SIGINT, request_stop)
    old_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        midi_process = subprocess.Popen(
            ["aseqdump", "-p", port.address],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
        )
        maximum_pipe_bytes = pcm_pipe_size_bytes(config.block_frames)
        audio_process = subprocess.Popen(
            build_pw_cat_command(target=target, latency_frames=latency_frames),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=None,
            bufsize=0,
            pipesize=maximum_pipe_bytes,
        )
        assert audio_process.stdin is not None
        pipe_capacity_bytes = verified_pipe_capacity_bytes(
            audio_process.stdin, maximum_pipe_bytes
        )
        os.set_blocking(audio_process.stdin.fileno(), False)
        pacer = RealtimeBlockPacer(config.sample_rate, config.block_frames)
        reader = threading.Thread(
            target=_midi_reader,
            args=(midi_process, event_queue, stop_event),
            name="buckelwal-midi-reader",
            daemon=True,
        )
        reader.start()

        required_initialization_blocks = live_initialization_block_count(
            config.sample_rate, config.block_frames
        )
        initialization_blocks = 0
        for _block in range(required_initialization_blocks):
            if stop_event.is_set():
                raise RuntimeError("live initialization stopped before readiness")
            _dispatch_pending_events(voice, event_queue)
            _require_live_children(midi_process, audio_process)
            payload = voice.render_f32_stereo(config.block_frames)
            if not write_pcm_block(
                audio_process.stdin, payload, stop_event, audio_process
            ):
                raise RuntimeError("live initialization stopped before readiness")
            initialization_blocks += 1
            pacer.wait()
        _dispatch_pending_events(voice, event_queue)
        _require_live_children(midi_process, audio_process)

        notify_required = os.environ.get(MANAGED_NOTIFY_REQUIRED_ENV) == "1"
        if notify_required and not notify_systemd_ready(
            "Buckelwal Live Voice MIDI and PipeWire initialized"
        ):
            raise RuntimeError("managed runtime has no systemd notification socket")
        print(
            json.dumps(
                {
                    "state": "running",
                    "midi_port": asdict(port),
                    "sample_rate_hz": config.sample_rate,
                    "block_frames": config.block_frames,
                    "master_gain": config.master_gain,
                    "pcm_pipe_capacity_bytes": pipe_capacity_bytes,
                    "initialization_blocks": initialization_blocks,
                    "realtime_pacing": True,
                },
                sort_keys=True,
            ),
            flush=True,
        )

        while not stop_event.is_set():
            _dispatch_pending_events(voice, event_queue)
            _require_live_children(midi_process, audio_process)
            payload = voice.render_f32_stereo(config.block_frames)
            if not write_pcm_block(
                audio_process.stdin, payload, stop_event, audio_process
            ):
                break
            pacer.wait()
        return 0
    finally:
        stop_event.set()
        if audio_process and audio_process.stdin:
            try:
                audio_process.stdin.close()
            except BrokenPipeError:
                pass
        terminate_process(midi_process)
        terminate_process(audio_process)
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)


def service_active() -> bool:
    status = service_status()
    if status["load_state"] == "not-found":
        return False
    active_state = status["active_state"]
    if active_state in {"active", "activating", "reloading"}:
        return True
    if active_state in {"inactive", "failed", "deactivating"}:
        return False
    raise RuntimeError(f"unknown systemd active state: {active_state}")


def service_ready(status: dict[str, object]) -> bool:
    return (
        status.get("load_state") == "loaded"
        and status.get("active_state") == "active"
        and status.get("sub_state") == "running"
    )


def start_service(args: argparse.Namespace) -> int:
    if service_active():
        raise RuntimeError(f"{UNIT_NAME} is already active")
    port = resolve_midi_port(args.midi_port)
    script = pathlib.Path(__file__).resolve()
    command = [
        "systemd-run",
        "--user",
        "--collect",
        "--quiet",
        "--service-type",
        "notify",
        "--setenv",
        f"{MANAGED_NOTIFY_REQUIRED_ENV}=1",
        "--unit",
        UNIT_NAME.removesuffix(".service"),
        "--property",
        "Description=Buckelwal Live Voice v1",
        "--property",
        "Restart=no",
        "--property",
        "NotifyAccess=main",
        "--property",
        "TimeoutStartSec=10s",
        "--property",
        "MemoryMax=268435456",
        "--property",
        "CPUQuota=80%",
        "--property",
        "TasksMax=32",
        "--property",
        "LogRateLimitIntervalSec=30s",
        "--property",
        "LogRateLimitBurst=100",
        "--property",
        f"RuntimeMaxSec={args.runtime_max_seconds}",
        sys.executable,
        str(script),
        "run",
        "--midi-port",
        port.address,
        "--gain",
        str(args.gain),
        "--latency-frames",
        str(args.latency_frames),
    ]
    if args.target:
        command.extend(["--target", args.target])
    result = run_capture(command, timeout=SERVICE_START_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or "systemd-run failed"
        )
    last_status: dict[str, object] | None = None
    for _attempt in range(20):
        last_status = service_status()
        if service_ready(last_status):
            print(
                json.dumps(
                    {"state": "ready", "unit": UNIT_NAME, "midi_port": port.address}
                )
            )
            return 0
        if last_status["active_state"] in {"failed", "inactive"}:
            break
        time.sleep(0.05)
    raise RuntimeError(
        "service did not report runtime readiness: "
        + json.dumps(last_status or {}, sort_keys=True)
    )


def stop_service() -> int:
    if not service_active():
        print(json.dumps({"state": "inactive", "unit": UNIT_NAME}))
        return 0
    result = run_capture(["systemctl", "--user", "stop", UNIT_NAME])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "systemctl stop failed")
    print(json.dumps({"state": "stopped", "unit": UNIT_NAME}))
    return 0


def service_status() -> dict[str, object]:
    result = run_capture(
        [
            "systemctl",
            "--user",
            "show",
            UNIT_NAME,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=Result",
            "--property=ExecMainStatus",
        ]
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if result.returncode != 0 and values.get("LoadState") != "not-found":
        detail = (
            result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        )
        raise RuntimeError(f"systemctl show failed: {detail}")
    if "LoadState" not in values or "ActiveState" not in values:
        raise RuntimeError("systemctl show returned incomplete service state")
    return {
        "unit": UNIT_NAME,
        "load_state": values.get("LoadState", "unknown"),
        "active_state": values.get("ActiveState", "unknown"),
        "sub_state": values.get("SubState", "unknown"),
        "result": values.get("Result", "unknown"),
        "exec_main_status": values.get("ExecMainStatus", "unknown"),
    }


def create_demo(
    path: pathlib.Path, duration_seconds: float, gain: float
) -> dict[str, object]:
    config = WhaleVoiceConfig(master_gain=gain)
    events = [event for event in default_demo_events() if event[0] <= duration_seconds]
    samples = render_timeline(events, duration_seconds, config)
    write_stereo_wav(path, samples, config.sample_rate)
    metrics = signal_metrics(samples)
    return {
        "output": str(path),
        "sample_rate_hz": config.sample_rate,
        "channels": 2,
        "duration_seconds": duration_seconds,
        **metrics,
    }


def bounded_gain(value: str) -> float:
    gain = float(value)
    if not 0 < gain <= MAX_MASTER_GAIN:
        raise argparse.ArgumentTypeError(
            f"gain must be positive and at most {MAX_MASTER_GAIN}"
        )
    return gain


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="read-only runtime and MIDI readiness report")

    demo = subparsers.add_parser("demo", help="render a safe offline demonstration WAV")
    demo.add_argument("output", type=pathlib.Path)
    demo.add_argument("--duration", type=float, default=12.0)
    demo.add_argument("--gain", type=bounded_gain, default=0.16)

    run = subparsers.add_parser("run", help="run in foreground until SIGINT/SIGTERM")
    run.add_argument("--midi-port", default="auto")
    run.add_argument("--target")
    run.add_argument("--gain", type=bounded_gain, default=0.16)
    run.add_argument("--latency-frames", type=int, default=128)

    start = subparsers.add_parser(
        "start", help="start a bounded transient user service"
    )
    start.add_argument("--midi-port", default="auto")
    start.add_argument("--target")
    start.add_argument("--gain", type=bounded_gain, default=0.16)
    start.add_argument("--latency-frames", type=int, default=128)
    start.add_argument(
        "--runtime-max-seconds",
        type=int,
        default=MAX_MANAGED_RUNTIME_SECONDS,
        choices=range(60, MAX_MANAGED_RUNTIME_SECONDS + 1),
        metavar=f"60..{MAX_MANAGED_RUNTIME_SECONDS}",
    )

    subparsers.add_parser("stop", help="stop the managed user service")
    subparsers.add_parser("status", help="read managed service state")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "doctor":
            report = runtime_doctor()
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if report["ready"] else 2
        if args.command == "demo":
            if not 1.0 <= args.duration <= MAX_OFFLINE_DURATION_SECONDS:
                raise ValueError(
                    "demo duration must be between 1 and "
                    f"{MAX_OFFLINE_DURATION_SECONDS:g} seconds"
                )
            print(
                json.dumps(
                    create_demo(args.output, args.duration, args.gain),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "run":
            return run_live(
                midi_port=args.midi_port,
                target=args.target,
                gain=args.gain,
                latency_frames=args.latency_frames,
            )
        if args.command == "start":
            if not 32 <= args.latency_frames <= 2_048:
                raise ValueError("latency_frames must be between 32 and 2048")
            return start_service(args)
        if args.command == "stop":
            return stop_service()
        if args.command == "status":
            print(json.dumps(service_status(), indent=2, sort_keys=True))
            return 0
        raise AssertionError("unreachable command")
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(
            json.dumps({"state": "blocked", "error": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
