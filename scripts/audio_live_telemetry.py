#!/usr/bin/env python3
"""Passive, bounded, crash-isolated live audio and MIDI telemetry.

The module is dependency-free on purpose: it is imported by the local control
service, exercised by the deterministic soak harness and used standalone.

Safety boundary
---------------
Every collector observes only. The module never changes defaults, routes,
profiles, volumes or links, it never opens a capture or playback stream and it
never subscribes to an ALSA sequencer port. Only the argument vectors in
``PASSIVE_COMMANDS`` may be executed, always without a shell.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import errno
import hashlib
import json
import os
import pathlib
import re
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable, Sequence

SCHEMA_VERSION = 1
SNAPSHOT_KIND = "audio_live_telemetry_snapshot"
CHECK_KIND = "audio_live_telemetry_check"
AUTHORITY = "passive-observation"

STREAM_AUDIO_LEVELS = "audio-levels"
STREAM_MIDI_ACTIVITY = "midi-activity"
STREAM_TRANSPORT = "transport"
STREAM_CPU_LOAD = "cpu-load"
STREAM_XRUNS = "xruns"
STREAM_DEVICE_GRAPH = "device-graph"

STREAM_IDS: tuple[str, ...] = (
    STREAM_AUDIO_LEVELS,
    STREAM_MIDI_ACTIVITY,
    STREAM_TRANSPORT,
    STREAM_CPU_LOAD,
    STREAM_XRUNS,
    STREAM_DEVICE_GRAPH,
)

DEFAULT_STREAM_CAPACITY = 32
MAX_STREAM_CAPACITY = 256
DEFAULT_CONTROL_CAPACITY = 64
MAX_CONTROL_CAPACITY = 1024
MAX_PAYLOAD_BYTES = 16_384
MAX_ERROR_CHARACTERS = 240
MAX_OBSERVED_ITEMS = 32
MAX_COLLECTOR_RESTARTS = 8
OBSERVER_ID = "audio-control-telemetry-v1"
MAX_PASSIVE_COMMAND_TIMEOUT_SECONDS = 6.0
PROCESS_KILL_GRACE_SECONDS = 1.0
DEFAULT_STOP_TIMEOUT_SECONDS = (
    MAX_PASSIVE_COMMAND_TIMEOUT_SECONDS + PROCESS_KILL_GRACE_SECONDS + 1.0
)

MAX_COMMAND_OUTPUT_BYTES = 2_000_000
MAX_PROC_BYTES = 262_144
DEFAULT_COMMAND_TIMEOUT_SECONDS = 4.0

#: The complete allowlist of external programs this module may execute.
#: Each entry is matched exactly; nothing else is ever spawned.
PASSIVE_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("pw-dump",),
    ("pw-top", "-b", "-n", "1"),
)

#: Verbs that would leave the passive boundary. They must never appear in an
#: argument vector, not even inside an allowlisted command.
FORBIDDEN_ARGUMENTS: frozenset[str] = frozenset(
    {
        "set-default",
        "set-volume",
        "set-mute",
        "set-sink-volume",
        "set-source-volume",
        "set-card-profile",
        "set-port",
        "set-param",
        "link",
        "unlink",
        "connect",
        "disconnect",
        "load-module",
        "unload-module",
        "suspend",
        "move-sink-input",
        "move-source-output",
        "record",
        "play",
    }
)

PROC_LOADAVG = pathlib.Path("/proc/loadavg")
PROC_SELF_STAT = pathlib.Path("/proc/self/stat")
PROC_SEQ_CLIENTS = pathlib.Path("/proc/asound/seq/clients")
PROC_ASOUND = pathlib.Path("/proc/asound")

LEVEL_SOURCE_ENVIRONMENT = "AUDIO_TELEMETRY_LEVEL_SOURCE"
ACTIVE_LEVEL_OBSERVER_ID = "audio-control-level-observer-v1"
ACTIVE_LEVEL_OBSERVER_MODE = "active-pipewire-shared-capture"
MAX_ACTIVE_LEVEL_SOURCE_AGE_SECONDS = 3.0
MAX_ACTIVE_LEVEL_SOURCE_FUTURE_SECONDS = 1.0

_SEQ_CLIENT_RE = re.compile(r'^Client\s+(\d+)\s*:\s*"(.*?)"')
_SEQ_PORT_RE = re.compile(r"^\s+Port\s+(\d+)\s*:")
_RAWMIDI_BYTES_RE = re.compile(r"^\s*Bytes transferred:\s*(\d+)", re.MULTILINE)


class TelemetryError(RuntimeError):
    """A bounded, expected telemetry failure that stays inside one stream."""


class ControlChannelFull(TelemetryError):
    """The lossless command channel rejected a new command explicitly."""


def _clip(text: str, limit: int = MAX_ERROR_CHARACTERS) -> str:
    raw = str(text)
    scan_limit = max(1024, limit * 4)
    compact = " ".join(raw[:scan_limit].split())
    return compact[:limit]


def utc_iso(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat()


def assert_passive_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Reject anything that is not an exact passive, read-only command."""

    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise TelemetryError("telemetry command vector is invalid")
    candidate = tuple(argv)
    for item in candidate:
        if item.strip().lower() in FORBIDDEN_ARGUMENTS:
            raise TelemetryError(f"telemetry command is not passive: {item}")
    if candidate not in PASSIVE_COMMANDS:
        raise TelemetryError(f"telemetry command is not allowlisted: {candidate[0]}")
    return candidate


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=PROCESS_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_passive_command(
    argv: Sequence[str],
    *,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    maximum_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> str:
    """Run one allowlisted read-only command with a hard time and size bound."""

    command = assert_passive_argv(argv)
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise TelemetryError(f"program is unavailable: {command[0]}") from error
    except OSError as error:
        raise TelemetryError(f"program cannot be started: {command[0]}") from error

    assert process.stdout is not None
    collected = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                raise TelemetryError(f"{command[0]} exceeded its time bound")
            for _key, _mask in selector.select(timeout=min(remaining, 0.1)):
                chunk = os.read(process.stdout.fileno(), 65_536)
                if not chunk:
                    selector.unregister(process.stdout)
                    break
                collected.extend(chunk)
                if len(collected) > maximum_bytes:
                    _kill_process_group(process)
                    raise TelemetryError(f"{command[0]} exceeded its output bound")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_group(process)
            raise TelemetryError(f"{command[0]} exceeded its time bound")
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as error:
        _kill_process_group(process)
        raise TelemetryError(f"{command[0]} exceeded its time bound") from error
    finally:
        selector.close()
        if not process.stdout.closed:
            process.stdout.close()
    if returncode != 0:
        raise TelemetryError(f"{command[0]} returned {returncode}")
    return collected.decode("utf-8", errors="replace")


def read_bounded_text(
    path: pathlib.Path,
    *,
    label: str,
    maximum_bytes: int = MAX_PROC_BYTES,
) -> str:
    """Read a bounded amount of text without following symlinks on the leaf."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise TelemetryError(f"{label} is unavailable") from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise TelemetryError(f"{label} is a symlink") from error
        raise TelemetryError(f"{label} cannot be read") from error
    try:
        metadata = os.fstat(descriptor)
        # /proc entries report st_size 0, so only reject oversized regular files.
        if stat.S_ISREG(metadata.st_mode) and metadata.st_size > maximum_bytes:
            raise TelemetryError(f"{label} exceeds its size bound")
        payload = bytearray()
        while len(payload) <= maximum_bytes:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum_bytes:
            raise TelemetryError(f"{label} exceeds its size bound")
        return payload.decode("utf-8", errors="replace")
    except OSError as error:
        raise TelemetryError(f"{label} cannot be read") from error
    finally:
        os.close(descriptor)


class ControlChannel:
    """Bounded, lossless channel for commands and state transitions.

    Telemetry may be dropped; accepted commands and state transitions may not.
    The channel therefore rejects new submissions explicitly once it is full
    instead of evicting anything that was already accepted.
    """

    lossy = False

    def __init__(self, *, capacity: int = DEFAULT_CONTROL_CAPACITY) -> None:
        if not 1 <= capacity <= MAX_CONTROL_CAPACITY:
            raise TelemetryError("control channel capacity is outside the contract")
        self.capacity = capacity
        self._lock = threading.Lock()
        self._items: collections.deque[dict[str, Any]] = collections.deque()
        self.accepted_total = 0
        self.rejected_total = 0
        self.delivered_total = 0

    def submit(self, kind: str, detail: Any = None, *, wall_now: float | None = None) -> int:
        if not isinstance(kind, str) or not kind.strip():
            raise TelemetryError("control command kind must be a non-empty string")
        encoded = _encode_payload({"kind": kind, "detail": detail})
        with self._lock:
            if len(self._items) >= self.capacity:
                self.rejected_total += 1
                raise ControlChannelFull(
                    "control channel is full; the command was rejected, not dropped"
                )
            self.accepted_total += 1
            sequence = self.accepted_total
            self._items.append(
                {
                    "sequence": sequence,
                    "at": utc_iso(time.time() if wall_now is None else wall_now),
                    "command": encoded,
                }
            )
            return sequence

    def drain(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            count = len(self._items) if limit is None else max(0, min(limit, len(self._items)))
            drained = [self._items.popleft() for _index in range(count)]
            self.delivered_total += len(drained)
            return drained

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "capacity": self.capacity,
                "depth": len(self._items),
                "accepted_total": self.accepted_total,
                "delivered_total": self.delivered_total,
                "rejected_total": self.rejected_total,
                "dropped_total": 0,
                "lossless": True,
                "shares_telemetry_queue": False,
            }


def _encode_payload(payload: Any) -> Any:
    """Guarantee a bounded, JSON-safe telemetry or command payload."""

    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise TelemetryError("payload is not canonical JSON") from error
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise TelemetryError("payload exceeds the per-sample size bound")
    return json.loads(encoded)


class TelemetryStream:
    """One bounded, lossy observation stream with its own accounting."""

    lossy = True

    def __init__(
        self,
        stream_id: str,
        label: str,
        *,
        capacity: int = DEFAULT_STREAM_CAPACITY,
        stale_after_ms: int = 4000,
        unit: str | None = None,
    ) -> None:
        if not 1 <= capacity <= MAX_STREAM_CAPACITY:
            raise TelemetryError(f"{stream_id} buffer capacity is outside the contract")
        if not 100 <= stale_after_ms <= 600_000:
            raise TelemetryError(f"{stream_id} stale threshold is outside the contract")
        self.stream_id = stream_id
        self.label = label
        self.capacity = capacity
        self.stale_after_ms = stale_after_ms
        self.unit = unit
        self._lock = threading.Lock()
        self._buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=capacity)
        self.sequence = 0
        self.published_total = 0
        self.dropped_total = 0
        self.rejected_total = 0
        self.error_total = 0
        self.consecutive_error_count = 0
        self.last_error: str | None = None
        self.last_error_at: str | None = None
        self._latest: dict[str, Any] | None = None
        self._updated_monotonic: float | None = None

    def begin_lifecycle(self) -> None:
        """Invalidate prior samples before a new collector lifecycle starts."""

        with self._lock:
            self._latest = None
            self._updated_monotonic = None
            self.consecutive_error_count = 0
            self.last_error = None
            self.last_error_at = None

    def publish(self, payload: Any, *, monotonic_now: float, wall_now: float) -> int:
        try:
            encoded = _encode_payload(payload)
        except TelemetryError as error:
            with self._lock:
                self.rejected_total += 1
            self.fail(str(error), wall_now=wall_now)
            raise
        with self._lock:
            self.sequence += 1
            self.published_total += 1
            if len(self._buffer) == self._buffer.maxlen:
                self.dropped_total += 1
            sample = {
                "sequence": self.sequence,
                "at": utc_iso(wall_now),
                "value": encoded,
            }
            self._buffer.append(sample)
            self._latest = sample
            self._updated_monotonic = monotonic_now
            self.consecutive_error_count = 0
            self.last_error = None
            self.last_error_at = None
            return self.sequence

    def fail(self, message: str, *, wall_now: float) -> None:
        with self._lock:
            self.error_total += 1
            self.consecutive_error_count += 1
            self.last_error = _clip(message)
            self.last_error_at = utc_iso(wall_now)

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._buffer)

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return None if self._latest is None else dict(self._latest)

    def snapshot(self, *, monotonic_now: float, running: bool) -> dict[str, Any]:
        with self._lock:
            latest = self._latest
            age_ms: int | None = None
            if latest is not None and self._updated_monotonic is not None:
                age_ms = max(0, int(round((monotonic_now - self._updated_monotonic) * 1000)))
            if latest is None:
                availability = "unavailable" if self.last_error else "starting"
            elif not running:
                availability = "stale"
            elif age_ms is not None and age_ms > self.stale_after_ms:
                availability = "stale"
            else:
                availability = "live"
            return {
                "id": self.stream_id,
                "label": self.label,
                "unit": self.unit,
                "availability": availability,
                "lossy": True,
                "sequence": self.sequence,
                "published_total": self.published_total,
                "dropped_total": self.dropped_total,
                "rejected_total": self.rejected_total,
                "buffer_capacity": self.capacity,
                "buffer_depth": len(self._buffer),
                "stale_after_ms": self.stale_after_ms,
                "age_ms": age_ms,
                "updated_at": None if latest is None else latest["at"],
                "error": self.last_error,
                "error_at": self.last_error_at,
                "error_total": self.error_total,
                "consecutive_error_count": self.consecutive_error_count,
                "value": None if latest is None else latest["value"],
            }


class Collector:
    """Base class for a passive observation source bound to one stream."""

    #: Human readable collector identity, used in reports and error text.
    name = "collector"
    #: The stream this collector feeds.
    stream_id = "unknown"
    #: Nominal sample interval; the hub sleeps for the remainder of it.
    interval_seconds = 1.0
    label = "Stream"
    unit: str | None = None
    stale_after_ms = 4000

    def sample(self, context: "CollectorContext") -> Any:
        raise NotImplementedError

    def reset(self) -> None:
        """Drop derived state so a restarted collector starts clean."""


class CollectorContext:
    """Read-only view a collector may use while sampling."""

    def __init__(self, hub: "TelemetryHub", monotonic_now: float, wall_now: float) -> None:
        self.hub = hub
        self.monotonic_now = monotonic_now
        self.wall_now = wall_now

    def latest_value(self, stream_id: str) -> Any:
        stream = self.hub.stream(stream_id)
        latest = stream.latest()
        return None if latest is None else latest["value"]


class _CollectorEntry:
    def __init__(self, collector: Collector, stream: TelemetryStream) -> None:
        self.collector = collector
        self.stream = stream
        self.thread: threading.Thread | None = None
        self.sample_attempts = 0
        self.restart_count = 0
        self.running = False


class TelemetryHub:
    """Owns bounded streams, isolated collectors and the lossless command channel."""

    def __init__(
        self,
        collectors: Iterable[Collector],
        *,
        stream_capacity: int = DEFAULT_STREAM_CAPACITY,
        control_capacity: int = DEFAULT_CONTROL_CAPACITY,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.control = ControlChannel(capacity=control_capacity)
        self._entries: dict[str, _CollectorEntry] = {}
        self._order: list[str] = []
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._stop_event.set()
        self._running = False
        self._started_monotonic: float | None = None
        for collector in collectors:
            if collector.stream_id in self._entries:
                raise TelemetryError(f"duplicate telemetry stream: {collector.stream_id}")
            stream = TelemetryStream(
                collector.stream_id,
                collector.label,
                capacity=stream_capacity,
                stale_after_ms=collector.stale_after_ms,
                unit=collector.unit,
            )
            self._entries[collector.stream_id] = _CollectorEntry(collector, stream)
            self._order.append(collector.stream_id)
        if not self._entries:
            raise TelemetryError("a telemetry hub needs at least one collector")

    # ---------------------------------------------------------------- streams

    def stream(self, stream_id: str) -> TelemetryStream:
        entry = self._entries.get(stream_id)
        if entry is None:
            raise TelemetryError(f"unknown telemetry stream: {stream_id}")
        return entry.stream

    @property
    def stream_ids(self) -> tuple[str, ...]:
        return tuple(self._order)

    @property
    def running(self) -> bool:
        return self._running

    # -------------------------------------------------------------- lifecycle

    def start(self, *, threads: bool = True) -> dict[str, Any]:
        """Start the hub. ``threads=False`` keeps it caller-driven via ``pump``."""

        with self._lifecycle_lock:
            if self._running:
                return {"state": "already-running", "collectors": len(self._entries)}
            self._stop_event = threading.Event()
            self._running = True
            self._started_monotonic = self.monotonic()
            for stream_id in self._order:
                entry = self._entries[stream_id]
                entry.stream.begin_lifecycle()
                entry.collector.reset()
                entry.running = True
                if not threads:
                    continue
                thread = threading.Thread(
                    target=self._collector_loop,
                    args=(entry, self._stop_event),
                    name=f"audio-telemetry-{stream_id}",
                    daemon=True,
                )
                entry.thread = thread
                thread.start()
            return {
                "state": "running",
                "collectors": len(self._entries),
                "threaded": threads,
            }

    def stop(self, *, timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> dict[str, Any]:
        with self._lifecycle_lock:
            if not self._running:
                return {
                    "state": "already-stopped",
                    "collectors": len(self._entries),
                    "joined": 0,
                    "timed_out": 0,
                }
            self._stop_event.set()
            self._running = False
            joined = 0
            timed_out = 0
            deadline = time.monotonic() + max(0.0, timeout)
            for stream_id in self._order:
                entry = self._entries[stream_id]
                thread = entry.thread
                if thread is None:
                    continue
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    timed_out += 1
                else:
                    joined += 1
                entry.thread = None
                entry.running = False
            return {
                "state": "stopped",
                "collectors": len(self._entries),
                "joined": joined,
                "timed_out": timed_out,
            }

    def __enter__(self) -> "TelemetryHub":
        self.start()
        return self

    def __exit__(self, *_exception: object) -> None:
        self.stop()

    # -------------------------------------------------------------- collection

    def _collect_once(self, entry: _CollectorEntry) -> bool:
        """Sample one collector. A collector failure never leaves this method."""

        entry.sample_attempts += 1
        monotonic_now = self.monotonic()
        wall_now = self.wall_clock()
        context = CollectorContext(self, monotonic_now, wall_now)
        try:
            payload = entry.collector.sample(context)
        except TelemetryError as error:
            entry.stream.fail(str(error), wall_now=wall_now)
            return False
        except Exception as error:  # collector isolation, by contract
            entry.stream.fail(
                f"{type(error).__name__}: {_clip(str(error))}", wall_now=wall_now
            )
            return False
        if payload is None:
            entry.stream.fail("collector produced no observation", wall_now=wall_now)
            return False
        try:
            entry.stream.publish(payload, monotonic_now=monotonic_now, wall_now=wall_now)
        except TelemetryError:
            return False
        return True

    def pump(self, stream_id: str | None = None) -> dict[str, bool]:
        """Run collectors once synchronously; used by tests and the soak harness."""

        targets = self._order if stream_id is None else [stream_id]
        results: dict[str, bool] = {}
        for target in targets:
            entry = self._entries.get(target)
            if entry is None:
                raise TelemetryError(f"unknown telemetry stream: {target}")
            results[target] = self._collect_once(entry)
        return results

    def _collector_loop(self, entry: _CollectorEntry, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                while not stop_event.is_set():
                    started = self.monotonic()
                    self._collect_once(entry)
                    elapsed = self.monotonic() - started
                    stop_event.wait(max(0.0, entry.collector.interval_seconds - elapsed))
                return
            except BaseException as error:  # noqa: BLE001 - isolation is the point
                entry.restart_count += 1
                entry.stream.fail(
                    f"collector loop restarted after {type(error).__name__}: "
                    f"{_clip(str(error))}",
                    wall_now=self.wall_clock(),
                )
                if entry.restart_count > MAX_COLLECTOR_RESTARTS:
                    entry.running = False
                    entry.stream.fail(
                        "collector stopped after exceeding its restart bound",
                        wall_now=self.wall_clock(),
                    )
                    return
                stop_event.wait(min(1.0, entry.collector.interval_seconds))

    # ---------------------------------------------------------------- snapshot

    def submit_command(self, kind: str, detail: Any = None) -> int:
        return self.control.submit(kind, detail, wall_now=self.wall_clock())

    def snapshot(self) -> dict[str, Any]:
        monotonic_now = self.monotonic()
        streams = []
        for stream_id in self._order:
            entry = self._entries[stream_id]
            projection = entry.stream.snapshot(
                monotonic_now=monotonic_now,
                running=self._running and entry.running,
            )
            projection["collector"] = {
                "name": entry.collector.name,
                "interval_ms": int(round(entry.collector.interval_seconds * 1000)),
                "running": bool(self._running and entry.running),
                "sample_attempts": entry.sample_attempts,
                "restart_count": entry.restart_count,
            }
            streams.append(projection)
        counts: dict[str, int] = {}
        for projection in streams:
            counts[projection["availability"]] = counts.get(projection["availability"], 0) + 1
        uptime = (
            0.0
            if self._started_monotonic is None
            else max(0.0, monotonic_now - self._started_monotonic)
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": SNAPSHOT_KIND,
            "authority": AUTHORITY,
            "authoritative": False,
            "read_only": True,
            "generated_at": utc_iso(self.wall_clock()),
            "running": self._running,
            "uptime_seconds": round(uptime, 3),
            "safety": safety_boundary(),
            "control_channel": self.control.snapshot(),
            "summary": {
                "stream_count": len(streams),
                "live_count": counts.get("live", 0),
                "stale_count": counts.get("stale", 0),
                "starting_count": counts.get("starting", 0),
                "unavailable_count": counts.get("unavailable", 0),
                "error_stream_count": sum(1 for item in streams if item["error"]),
                "dropped_total": sum(item["dropped_total"] for item in streams),
                "restart_total": sum(item["collector"]["restart_count"] for item in streams),
            },
            "streams": streams,
        }


def safety_boundary() -> dict[str, Any]:
    return {
        "mode": "passive-observation",
        "observer_id": OBSERVER_ID,
        "owned_nodes": [],
        "owned_links": [],
        "identifies_nodes_and_links": True,
        "modifies_defaults": False,
        "modifies_routes": False,
        "modifies_profiles": False,
        "modifies_volumes": False,
        "modifies_links": False,
        "opens_audio_streams": False,
        "subscribes_midi_ports": False,
        "uses_shell": False,
        "reversible": True,
        "rollback": {
            "scope": "observer-owned-resources-only",
            "requires_identity_match": True,
            "action_when_no_owned_resources": "no-op",
        },
        "maximum_passive_command_seconds": MAX_PASSIVE_COMMAND_TIMEOUT_SECONDS,
        "stop_timeout_seconds": DEFAULT_STOP_TIMEOUT_SECONDS,
        "allowed_commands": [" ".join(command) for command in PASSIVE_COMMANDS],
    }


# ---------------------------------------------------------------- collectors


class PipeWireGraphCollector(Collector):
    """Identify observed PipeWire nodes and links without touching them."""

    name = "pipewire-graph"
    stream_id = STREAM_DEVICE_GRAPH
    label = "Geräte und Graph"
    interval_seconds = 2.0
    stale_after_ms = 8000

    def __init__(self, runner: Callable[..., str] | None = None) -> None:
        self._runner = runner or run_passive_command
        self._previous_graph_sha256: str | None = None
        self._event_sequence = 0

    def reset(self) -> None:
        self._previous_graph_sha256 = None
        self._event_sequence = 0

    def sample(self, context: CollectorContext) -> Any:
        raw = self._runner(
            ("pw-dump",),
            timeout_seconds=MAX_PASSIVE_COMMAND_TIMEOUT_SECONDS,
        )
        try:
            objects = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise TelemetryError("pw-dump did not produce readable JSON") from error
        if not isinstance(objects, list):
            raise TelemetryError("pw-dump did not produce an object list")
        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        object_digests: list[str] = []
        node_count = 0
        link_count = 0
        device_count = 0
        running_nodes = 0

        def bind_object(identity: dict[str, Any]) -> None:
            object_digests.append(
                hashlib.sha256(
                    json.dumps(
                        identity,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )

        for item in objects:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            info = item.get("info") if isinstance(item.get("info"), dict) else {}
            props = info.get("props") if isinstance(info.get("props"), dict) else {}
            if item_type == "PipeWire:Interface:Node":
                node_count += 1
                node_state = info.get("state")
                if node_state == "running":
                    running_nodes += 1
                node = {
                    "id": item.get("id"),
                    "name": _clip(str(props.get("node.name", "unbekannt")), 80),
                    "media_class": _clip(str(props.get("media.class", "")), 40) or None,
                    "state": node_state if isinstance(node_state, str) else None,
                }
                bind_object({"id": item.get("id"), "type": item_type, "info": info})
                if len(nodes) < MAX_OBSERVED_ITEMS:
                    nodes.append(node)
            elif item_type == "PipeWire:Interface:Link":
                link_count += 1
                link = {
                    "id": item.get("id"),
                    "output_node": info.get("output-node-id"),
                    "input_node": info.get("input-node-id"),
                    "state": info.get("state") if isinstance(info.get("state"), str) else None,
                }
                bind_object({"id": item.get("id"), "type": item_type, "info": info})
                if len(links) < MAX_OBSERVED_ITEMS:
                    links.append(link)
            elif item_type == "PipeWire:Interface:Device":
                device_count += 1
                bind_object({"id": item.get("id"), "type": item_type, "info": info})
        if not node_count and not link_count and not device_count:
            raise TelemetryError("pw-dump exposed no graph objects")
        graph_identity = {
            "node_count": node_count,
            "link_count": link_count,
            "device_count": device_count,
            "running_node_count": running_nodes,
            "object_digests": sorted(object_digests),
        }
        graph_sha256 = hashlib.sha256(
            json.dumps(graph_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self._previous_graph_sha256 is None:
            event = "baseline"
        elif graph_sha256 != self._previous_graph_sha256:
            self._event_sequence += 1
            event = "changed"
        else:
            event = "none"
        self._previous_graph_sha256 = graph_sha256
        content_truncated = node_count > len(nodes) or link_count > len(links)
        return {
            "node_count": node_count,
            "link_count": link_count,
            "device_count": device_count,
            "running_node_count": running_nodes,
            "observed_node_count": len(nodes),
            "observed_link_count": len(links),
            "observed_nodes": nodes,
            "observed_links": links,
            "truncated": content_truncated,
            "content_truncated": content_truncated,
            "observed_item_limit": MAX_OBSERVED_ITEMS,
            "hashed_object_count": len(object_digests),
            "event": event,
            "event_sequence": self._event_sequence,
            "graph_sha256": graph_sha256,
            "observer_id": OBSERVER_ID,
            "modified": False,
        }


class TransportCollector(Collector):
    """Derive transport state from the already observed graph, without new I/O."""

    name = "graph-transport"
    stream_id = STREAM_TRANSPORT
    label = "Transportzustand"
    interval_seconds = 1.0
    stale_after_ms = 6000

    def sample(self, context: CollectorContext) -> Any:
        graph = context.latest_value(STREAM_DEVICE_GRAPH)
        if not isinstance(graph, dict):
            raise TelemetryError("transport needs a readable graph observation")
        running = graph.get("running_node_count")
        node_count = graph.get("node_count")
        if not isinstance(running, int) or not isinstance(node_count, int):
            raise TelemetryError("graph observation lacks transport counters")
        if running > 0:
            state = "running"
        elif node_count > 0:
            state = "idle"
        else:
            state = "unknown"
        return {
            "state": state,
            "running_node_count": running,
            "node_count": node_count,
            "derived_from": STREAM_DEVICE_GRAPH,
        }


class XrunCollector(Collector):
    """Read XRun counters from a single bounded pw-top batch snapshot."""

    name = "pw-top-xruns"
    stream_id = STREAM_XRUNS
    label = "XRuns"
    interval_seconds = 5.0
    stale_after_ms = 20_000

    def __init__(self, runner: Callable[..., str] | None = None) -> None:
        self._runner = runner or run_passive_command
        self._previous_total: int | None = None
        self._reset_count = 0

    def reset(self) -> None:
        self._previous_total = None
        self._reset_count = 0

    @staticmethod
    def parse(text: str) -> tuple[int, list[dict[str, Any]]]:
        header: dict[str, int | None] | None = None
        total = 0
        per_node: list[dict[str, Any]] = []
        for line in text.splitlines():
            matches = list(re.finditer(r"\S+", line))
            fields = [match.group(0) for match in matches]
            if not fields:
                continue
            if header is None:
                if "ERR" not in fields or "NAME" not in fields:
                    continue
                error_index = fields.index("ERR")
                name_index = fields.index("NAME")
                if name_index <= error_index:
                    raise TelemetryError("pw-top XRun header has an invalid column order")
                header = {
                    "error_index": error_index,
                    "error_start": matches[error_index].start(),
                    "error_end": matches[error_index + 1].start(),
                    "name_start": matches[name_index].start(),
                    "name_index": name_index,
                    "format_index": fields.index("FORMAT") if "FORMAT" in fields else None,
                    "id_index": fields.index("ID") if "ID" in fields else None,
                }
                continue
            error_start = int(header["error_start"])
            error_end = int(header["error_end"])
            raw = line[error_start:error_end].strip()
            error_index = int(header["error_index"])
            if not raw.isdigit():
                if len(fields) <= error_index or not fields[error_index].isdigit():
                    continue
                raw = fields[error_index]
            name_start = int(header["name_start"])
            while name_start > 0 and name_start <= len(line) and not line[name_start - 1].isspace():
                name_start -= 1
            name = line[name_start:].strip() if len(line) > name_start else ""
            if not name:
                name_index = int(header["name_index"])
                format_index = header["format_index"]
                if isinstance(format_index, int) and format_index < name_index:
                    tail = fields[error_index + 1 :]
                    if tail and tail[0] == "-":
                        name_fields = tail[1:]
                    elif len(tail) >= 4 and tail[1].isdigit() and tail[2].isdigit():
                        name_fields = tail[3:]
                    else:
                        name_fields = tail[-1:]
                else:
                    name_fields = fields[name_index:]
                name = " ".join(name_fields).strip()
            if not name:
                continue
            node_id: int | None = None
            id_index = header["id_index"]
            if isinstance(id_index, int) and len(fields) > id_index and fields[id_index].isdigit():
                node_id = int(fields[id_index])
            count = int(raw)
            total += count
            if len(per_node) < MAX_OBSERVED_ITEMS:
                per_node.append({"id": node_id, "name": _clip(name, 80), "xruns": count})
        if header is None:
            raise TelemetryError("pw-top produced no XRun column")
        return total, per_node

    def sample(self, context: CollectorContext) -> Any:
        raw = self._runner(
            ("pw-top", "-b", "-n", "1"),
            timeout_seconds=MAX_PASSIVE_COMMAND_TIMEOUT_SECONDS,
        )
        total, per_node = self.parse(raw)
        previous = self._previous_total
        if previous is None:
            delta = 0
        elif total < previous:
            self._reset_count += 1
            delta = 0
        else:
            delta = total - previous
        self._previous_total = total
        return {
            "total": total,
            "delta": delta,
            "counter_reset_count": self._reset_count,
            "per_node": per_node,
            "source": "pw-top -b -n 1",
        }


class CpuLoadCollector(Collector):
    """Observe system load and this process' own CPU time from /proc only."""

    name = "proc-cpu-load"
    stream_id = STREAM_CPU_LOAD
    label = "CPU-Last"
    interval_seconds = 2.0
    stale_after_ms = 8000
    unit = "percent"

    def __init__(
        self,
        *,
        loadavg_path: pathlib.Path = PROC_LOADAVG,
        self_stat_path: pathlib.Path = PROC_SELF_STAT,
    ) -> None:
        self._loadavg_path = loadavg_path
        self._self_stat_path = self_stat_path
        self._previous: tuple[float, float] | None = None

    def reset(self) -> None:
        self._previous = None

    def sample(self, context: CollectorContext) -> Any:
        text = read_bounded_text(self._loadavg_path, label="load average")
        fields = text.split()
        if len(fields) < 3:
            raise TelemetryError("load average is malformed")
        try:
            load_1m, load_5m, load_15m = (float(value) for value in fields[:3])
        except ValueError as error:
            raise TelemetryError("load average is not numeric") from error
        process_seconds = self._process_cpu_seconds()
        percent: float | None = None
        if self._previous is not None:
            previous_seconds, previous_monotonic = self._previous
            window = context.monotonic_now - previous_monotonic
            if window > 0:
                percent = round(
                    max(0.0, (process_seconds - previous_seconds) / window * 100.0), 3
                )
        self._previous = (process_seconds, context.monotonic_now)
        return {
            "load_1m": load_1m,
            "load_5m": load_5m,
            "load_15m": load_15m,
            "cpu_count": os.cpu_count(),
            "service_cpu_seconds": round(process_seconds, 3),
            "service_cpu_percent": percent,
        }

    def _process_cpu_seconds(self) -> float:
        text = read_bounded_text(self._self_stat_path, label="process stat")
        closing = text.rfind(")")
        if closing < 0:
            raise TelemetryError("process stat is malformed (missing closing parenthesis)")
        fields = text[closing + 1 :].split()
        # utime and stime are fields 14 and 15 of /proc/<pid>/stat (1-based).
        if len(fields) < 13:
            raise TelemetryError("process stat is malformed")
        try:
            ticks = int(fields[11]) + int(fields[12])
        except ValueError as error:
            raise TelemetryError("process stat is not numeric") from error
        hertz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        if not isinstance(hertz, int) or hertz <= 0:
            hertz = 100
        return ticks / hertz


class MidiActivityCollector(Collector):
    """Observe ALSA sequencer clients and rawmidi byte counters passively.

    Reading ``/proc`` never subscribes to a port. ``aseqdump`` is deliberately
    not used: it would create a sequencer subscription and therefore change the
    observed MIDI graph.
    """

    name = "proc-midi-activity"
    stream_id = STREAM_MIDI_ACTIVITY
    label = "MIDI-Aktivität"
    interval_seconds = 1.0
    stale_after_ms = 6000

    def __init__(
        self,
        *,
        clients_path: pathlib.Path = PROC_SEQ_CLIENTS,
        asound_root: pathlib.Path = PROC_ASOUND,
    ) -> None:
        self._clients_path = clients_path
        self._asound_root = asound_root
        self._previous_bytes: int | None = None

    def reset(self) -> None:
        self._previous_bytes = None

    def sample(self, context: CollectorContext) -> Any:
        text = read_bounded_text(self._clients_path, label="ALSA sequencer clients")
        clients: list[dict[str, Any]] = []
        client_count = 0
        port_count = 0
        current: dict[str, Any] | None = None
        for line in text.splitlines():
            client_match = _SEQ_CLIENT_RE.match(line)
            if client_match:
                client_count += 1
                current = {
                    "id": int(client_match.group(1)),
                    "name": _clip(client_match.group(2), 80),
                    "port_count": 0,
                }
                if len(clients) < MAX_OBSERVED_ITEMS:
                    clients.append(current)
                continue
            if current is not None and _SEQ_PORT_RE.match(line):
                port_count += 1
                current["port_count"] += 1
        if client_count == 0:
            raise TelemetryError("ALSA sequencer exposed no clients")
        total_bytes = self._rawmidi_bytes()
        delta = None
        if total_bytes is not None and self._previous_bytes is not None:
            delta = max(0, total_bytes - self._previous_bytes)
        if total_bytes is not None:
            self._previous_bytes = total_bytes
        return {
            "client_count": client_count,
            "observed_client_count": len(clients),
            "port_count": port_count,
            "clients": clients,
            "truncated": client_count > len(clients),
            "rawmidi_bytes_total": total_bytes,
            "rawmidi_bytes_delta": delta,
            "active": bool(delta),
            "subscribed": False,
        }

    def _rawmidi_bytes(self) -> int | None:
        try:
            entries = sorted(self._asound_root.glob("card*/midi*"))
        except OSError:
            return None
        total: int | None = None
        for index, entry in enumerate(entries):
            if index >= MAX_OBSERVED_ITEMS:
                break
            try:
                text = read_bounded_text(entry, label="rawmidi counters")
            except TelemetryError:
                continue
            for match in _RAWMIDI_BYTES_RE.finditer(text):
                total = (total or 0) + int(match.group(1))
        return total


class LevelSourceCollector(Collector):
    """Passively read peak/RMS from an explicitly configured level file.

    Peak and RMS cannot be observed without a signal tap. Opening a capture
    stream would create a link and leave the passive boundary, so this collector
    only reads a bounded JSON file that an external observer may write. The
    deployed producer identifies itself honestly as an active PipeWire shared-
    capture observer. Without that file the stream stays explicitly unavailable
    instead of inventing data.
    """

    name = "passive-level-source"
    stream_id = STREAM_AUDIO_LEVELS
    label = "Pegel (Peak/RMS)"
    interval_seconds = 1.0
    stale_after_ms = 3000
    unit = "dBFS"

    def __init__(self, source_path: pathlib.Path | None = None) -> None:
        self._explicit_path = source_path
        self._last_active_observation: tuple[int, float] | None = None

    def reset(self) -> None:
        self._last_active_observation = None

    def _path(self) -> pathlib.Path:
        if self._explicit_path is not None:
            return self._explicit_path
        configured = os.environ.get(LEVEL_SOURCE_ENVIRONMENT, "").strip()
        if not configured:
            raise TelemetryError(
                "no passive level source is configured "
                f"({LEVEL_SOURCE_ENVIRONMENT} is unset); peak/RMS stays unavailable"
            )
        return pathlib.Path(configured)

    def sample(self, context: CollectorContext) -> Any:
        text = read_bounded_text(self._path(), label="level source", maximum_bytes=65_536)
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise TelemetryError("level source is not readable JSON") from error
        if not isinstance(value, dict):
            raise TelemetryError("level source is not a JSON object")
        observer = value.get("observer")
        observer_mode = value.get("observer_mode")
        active_observer = observer is not None or observer_mode is not None
        observation_identity: tuple[int, float] | None = None
        if active_observer:
            if (
                value.get("kind") != "audio_level_observation"
                or observer != ACTIVE_LEVEL_OBSERVER_ID
                or observer_mode != ACTIVE_LEVEL_OBSERVER_MODE
                or value.get("capture_transport") != "pipewire-native-shared-stream"
                or value.get("source_selection")
                not in {"pipewire-default-source", "explicit-pipewire-target"}
            ):
                raise TelemetryError("active level source identity is invalid")
            observed_at = value.get("observed_at_unix")
            if (
                isinstance(observed_at, bool)
                or not isinstance(observed_at, (int, float))
                or not float("-inf") < float(observed_at) < float("inf")
            ):
                raise TelemetryError("active level source timestamp is invalid")
            wall_now = time.time() if context is None else context.wall_now
            source_age = wall_now - float(observed_at)
            if source_age > MAX_ACTIVE_LEVEL_SOURCE_AGE_SECONDS:
                raise TelemetryError("active level source observation is stale")
            if source_age < -MAX_ACTIVE_LEVEL_SOURCE_FUTURE_SECONDS:
                raise TelemetryError("active level source observation is from the future")
            sequence = value.get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                raise TelemetryError("active level source sequence is invalid")
            observation_identity = (sequence, float(observed_at))
            if observation_identity == self._last_active_observation:
                raise TelemetryError("active level source observation has not advanced")
        peak = value.get("peak_dbfs")
        rms = value.get("rms_dbfs")
        for name, candidate in (("peak_dbfs", peak), ("rms_dbfs", rms)):
            if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
                raise TelemetryError(f"level source {name} is not numeric")
            if not -160.0 <= float(candidate) <= 0.0:
                raise TelemetryError(f"level source {name} is outside the dBFS range")
        if float(rms) > float(peak):
            raise TelemetryError("level source RMS exceeds its peak")
        if observation_identity is not None:
            self._last_active_observation = observation_identity
        channel = value.get("channel")
        return {
            "peak_dbfs": round(float(peak), 3),
            "rms_dbfs": round(float(rms), 3),
            "channel": _clip(str(channel), 40) if isinstance(channel, str) else None,
            "source": (
                "active-pipewire-shared-capture"
                if active_observer
                else "external-passive-level-file"
            ),
            "observer": observer if active_observer else None,
            "source_selection": value.get("source_selection") if active_observer else None,
            "clipping": float(peak) >= -0.1,
        }


def default_collectors() -> list[Collector]:
    # The graph is collected before the transport so a single pump already has
    # the observation the derived transport stream depends on.
    return [
        PipeWireGraphCollector(),
        LevelSourceCollector(),
        MidiActivityCollector(),
        TransportCollector(),
        CpuLoadCollector(),
        XrunCollector(),
    ]


def build_default_hub(**kwargs: Any) -> TelemetryHub:
    return TelemetryHub(default_collectors(), **kwargs)


def contract_report() -> dict[str, Any]:
    """Validate the static telemetry contract without running any collector."""

    hub = build_default_hub()
    observed = set(hub.stream_ids)
    if observed != set(STREAM_IDS):
        raise TelemetryError("the default hub does not cover the contracted streams")
    for command in PASSIVE_COMMANDS:
        assert_passive_argv(command)
    for forbidden in ("wpctl", "pactl", "aseqdump", "pw-cli", "pw-link", "pw-metadata"):
        if any(command[0] == forbidden for command in PASSIVE_COMMANDS):
            raise TelemetryError(f"a mutating-capable program is allowlisted: {forbidden}")
    snapshot = hub.snapshot()
    if snapshot["running"]:
        raise TelemetryError("a fresh hub must not report itself as running")
    if any(stream["availability"] != "starting" for stream in snapshot["streams"]):
        raise TelemetryError("a fresh hub must report every stream as starting")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": CHECK_KIND,
        "status": "ok",
        "streams": list(hub.stream_ids),
        "stream_capacity": DEFAULT_STREAM_CAPACITY,
        "control_capacity": DEFAULT_CONTROL_CAPACITY,
        "max_payload_bytes": MAX_PAYLOAD_BYTES,
        "safety": safety_boundary(),
    }


def observe_once(*, timeout: float = 20.0) -> dict[str, Any]:
    """Start, sample every collector twice and stop again, deterministically."""

    hub = build_default_hub()
    deadline = time.monotonic() + timeout
    hub.start(threads=False)
    try:
        hub.pump()
        if time.monotonic() < deadline:
            hub.pump()
        return hub.snapshot()
    finally:
        hub.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audio-live-telemetry", description=__doc__)
    parser.add_argument(
        "command",
        choices=("check", "show", "safety"),
        nargs="?",
        default="check",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            report: dict[str, Any] = contract_report()
        elif args.command == "safety":
            report = {
                "schema_version": SCHEMA_VERSION,
                "kind": "audio_live_telemetry_safety",
                "safety": safety_boundary(),
            }
        else:
            report = observe_once()
    except TelemetryError as error:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "audio_live_telemetry_error",
                    "error": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
