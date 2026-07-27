#!/usr/bin/env python3
"""Managed live runtime for Buckelwal Live Voice v1."""

from __future__ import annotations

import argparse
import json
import pathlib
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass

from whale_live_engine import (
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
ROLAND_PATTERN = re.compile(r"roland|digital\s+piano|fp[- ]?30", re.IGNORECASE)
PORT_LINE_RE = re.compile(r"^\s*(\d+):(\d+)\s{2,}(.+?)\s{2,}(.+?)\s*$")
MAX_MANAGED_RUNTIME_SECONDS = 21_600


@dataclass(frozen=True)
class MidiPort:
    address: str
    client_name: str
    port_name: str

    @property
    def label(self) -> str:
        return f"{self.client_name} {self.port_name}".strip()


def run_capture(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
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
        "midi_ports": [asdict(port) for port in ports],
        "roland_midi_port": asdict(roland_port) if roland_port else None,
        "ready": not blocking_reasons,
        "blocking_reason": blocking_reasons[0] if blocking_reasons else None,
        "blocking_reasons": blocking_reasons,
        "audio_contract": {
            "sample_rate_hz": 48_000,
            "channels": 2,
            "format": "f32",
            "latency_frames": 128,
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
        audio_process = subprocess.Popen(
            build_pw_cat_command(target=target, latency_frames=latency_frames),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=None,
            bufsize=0,
        )
        reader = threading.Thread(
            target=_midi_reader,
            args=(midi_process, event_queue, stop_event),
            name="buckelwal-midi-reader",
            daemon=True,
        )
        reader.start()
        print(
            json.dumps(
                {
                    "state": "running",
                    "midi_port": asdict(port),
                    "sample_rate_hz": config.sample_rate,
                    "block_frames": config.block_frames,
                    "master_gain": config.master_gain,
                },
                sort_keys=True,
            ),
            flush=True,
        )

        assert audio_process.stdin is not None
        while not stop_event.is_set():
            while True:
                try:
                    item = event_queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, BaseException):
                    raise item
                voice.dispatch(item)
            if midi_process.poll() is not None:
                raise RuntimeError(
                    f"aseqdump exited with status {midi_process.returncode}"
                )
            if audio_process.poll() is not None:
                raise RuntimeError(
                    f"pw-cat exited with status {audio_process.returncode}"
                )
            audio_process.stdin.write(voice.render_f32_stereo(config.block_frames))
        return 0
    except BrokenPipeError as error:
        raise RuntimeError("PipeWire audio stream closed unexpectedly") from error
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
    result = run_capture(["systemctl", "--user", "is-active", UNIT_NAME])
    return result.returncode == 0 and result.stdout.strip() == "active"


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
        "--unit",
        UNIT_NAME.removesuffix(".service"),
        "--property",
        "Description=Buckelwal Live Voice v1",
        "--property",
        "Restart=no",
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
    result = run_capture(command)
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or "systemd-run failed"
        )
    for _attempt in range(20):
        if service_active():
            print(
                json.dumps(
                    {"state": "active", "unit": UNIT_NAME, "midi_port": port.address}
                )
            )
            return 0
        time.sleep(0.05)
    status = service_status()
    raise RuntimeError(
        "service did not become active: " + json.dumps(status, sort_keys=True)
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
    samples = render_timeline(default_demo_events(), duration_seconds, config)
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
