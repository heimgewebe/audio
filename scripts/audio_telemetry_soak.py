#!/usr/bin/env python3
"""Bounded soak and load harness for the passive live telemetry core.

Two evidence classes exist and are never conflated:

``synthetic``
    Accelerated, deterministic in-process load. It proves queue bounds, drop
    accounting, control-channel losslessness, collector isolation and memory or
    CPU trends in seconds. It proves nothing about real hardware.

``live``
    A real passive observation run of the default collectors for a chosen wall
    clock duration (for example one or eight hours). It reports XRun deltas only
    when a real counter was readable and otherwise says so explicitly.

The harness never starts a persistent service and never changes audio state.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys
import time
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]
TELEMETRY_SCRIPT = ROOT / "scripts" / "audio_live_telemetry.py"

_SPEC = importlib.util.spec_from_file_location(
    "audio_live_telemetry_soak_core", TELEMETRY_SCRIPT
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - packaging failure
    raise RuntimeError("Telemetriekern kann nicht geladen werden.")
TELEMETRY = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = TELEMETRY
_SPEC.loader.exec_module(TELEMETRY)

SCHEMA_VERSION = 1
REPORT_KIND = "audio_telemetry_soak_report"

MAX_DURATION_SECONDS = 28_800  # eight hours
MAX_ITERATIONS = 2_000_000
MAX_LOAD_FACTOR = 64
MAX_REPORT_SAMPLES = 64
MAX_MEMORY_SAMPLES = MAX_REPORT_SAMPLES * 4
MAX_REPORT_BYTES = 1_048_576
MAX_SHORT_RUN_MEMORY_GROWTH_KIB = 32_768
MAX_HOURLY_MEMORY_GROWTH_KIB = 65_536
PROC_SELF_STATM = pathlib.Path("/proc/self/statm")

SOAK_CRASH_STREAM = "soak-crashing"
SOAK_MALFORMED_STREAM = "soak-malformed"


class SoakError(RuntimeError):
    """A bounded soak harness failure."""


# ------------------------------------------------------------------ resources


def resident_kib() -> int | None:
    """Current resident set size in KiB, or ``None`` where /proc is absent."""

    try:
        text = TELEMETRY.read_bounded_text(
            PROC_SELF_STATM, label="process memory", maximum_bytes=4096
        )
    except TELEMETRY.TelemetryError:
        return None
    fields = text.split()
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    return int(fields[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)


def process_cpu_seconds() -> float:
    times = os.times()
    return float(
        times.user + times.system + times.children_user + times.children_system
    )


def downsample(samples: list[Any], limit: int = MAX_REPORT_SAMPLES) -> list[Any]:
    if limit <= 0:
        return []
    if not samples:
        return []
    if limit == 1:
        return [samples[-1]]
    if len(samples) <= limit:
        return list(samples)
    step = len(samples) / float(limit - 1)
    picked = [
        samples[min(len(samples) - 1, int(round(index * step)))]
        for index in range(limit - 1)
    ]
    picked.append(samples[-1])
    return picked


def append_bounded_sample(
    samples: list[tuple[float, int]],
    sample: tuple[float, int],
    *,
    limit: int = MAX_MEMORY_SAMPLES,
) -> None:
    """Append while preserving first/latest evidence under a hard memory bound."""

    if limit < 2:
        raise SoakError("memory sample retention requires a limit of at least two")
    if len(samples) < limit:
        samples.append(sample)
        return
    previous = list(samples)
    if limit == 2:
        samples[:] = [previous[0], sample]
        return
    interior_limit = max(0, (limit - 3) // 2)
    interior = downsample(previous[1:-1], interior_limit)
    samples[:] = [previous[0], *interior, previous[-1], sample]
    if len(samples) > limit:  # pragma: no cover - defensive contract guard
        raise AssertionError("bounded memory sample retention exceeded its limit")


# --------------------------------------------------------- synthetic sources


class _SyntheticCollector(TELEMETRY.Collector):
    interval_seconds = 0.0

    def __init__(self) -> None:
        self.tick = 0

    def reset(self) -> None:
        self.tick = 0


class SyntheticGraphCollector(_SyntheticCollector):
    name = "synthetic-graph"
    stream_id = TELEMETRY.STREAM_DEVICE_GRAPH
    label = "Geräte und Graph (synthetisch)"

    def sample(self, context: Any) -> Any:
        self.tick += 1
        running = 2 if self.tick % 7 else 0
        return {
            "node_count": 8,
            "link_count": 4,
            "device_count": 2,
            "running_node_count": running,
            "observed_nodes": [{"id": 42, "name": "synthetic", "state": "running"}],
            "observed_links": [],
            "truncated": False,
            "modified": False,
        }


class SyntheticLevelCollector(_SyntheticCollector):
    name = "synthetic-level"
    stream_id = TELEMETRY.STREAM_AUDIO_LEVELS
    label = "Pegel (synthetisch)"
    unit = "dBFS"

    def sample(self, context: Any) -> Any:
        self.tick += 1
        peak = -60.0 + (self.tick % 60)
        return {
            "peak_dbfs": round(min(peak, -0.5), 3),
            "rms_dbfs": round(min(peak, -0.5) - 6.0, 3),
            "channel": "synthetic",
            "source": "synthetic",
            "clipping": False,
        }


class SyntheticMidiCollector(_SyntheticCollector):
    name = "synthetic-midi"
    stream_id = TELEMETRY.STREAM_MIDI_ACTIVITY
    label = "MIDI (synthetisch)"

    def sample(self, context: Any) -> Any:
        self.tick += 1
        return {
            "client_count": 3,
            "port_count": 4,
            "clients": [{"id": 24, "name": "synthetic", "port_count": 1}],
            "rawmidi_bytes_total": self.tick * 3,
            "rawmidi_bytes_delta": 3,
            "active": bool(self.tick % 2),
            "subscribed": False,
        }


class SyntheticTransportCollector(_SyntheticCollector):
    name = "synthetic-transport"
    stream_id = TELEMETRY.STREAM_TRANSPORT
    label = "Transport (synthetisch)"

    def sample(self, context: Any) -> Any:
        self.tick += 1
        graph = context.latest_value(TELEMETRY.STREAM_DEVICE_GRAPH)
        running = graph.get("running_node_count", 0) if isinstance(graph, dict) else 0
        return {
            "state": "running" if running else "idle",
            "running_node_count": running,
            "node_count": 8,
            "derived_from": TELEMETRY.STREAM_DEVICE_GRAPH,
        }


class SyntheticCpuCollector(_SyntheticCollector):
    name = "synthetic-cpu"
    stream_id = TELEMETRY.STREAM_CPU_LOAD
    label = "CPU-Last (synthetisch)"
    unit = "percent"

    def sample(self, context: Any) -> Any:
        self.tick += 1
        return {
            "load_1m": 0.5,
            "load_5m": 0.5,
            "load_15m": 0.5,
            "cpu_count": os.cpu_count(),
            "service_cpu_seconds": round(process_cpu_seconds(), 3),
            "service_cpu_percent": 1.0,
        }


class SyntheticXrunCollector(_SyntheticCollector):
    name = "synthetic-xruns"
    stream_id = TELEMETRY.STREAM_XRUNS
    label = "XRuns (synthetisch)"

    def __init__(self, *, every: int = 25) -> None:
        super().__init__()
        self.every = max(1, every)
        self.total = 0

    def reset(self) -> None:
        super().reset()
        self.total = 0

    def sample(self, context: Any) -> Any:
        self.tick += 1
        delta = 1 if self.tick % self.every == 0 else 0
        self.total += delta
        return {
            "total": self.total,
            "delta": delta,
            "counter_reset_count": 0,
            "per_node": [{"name": "synthetic", "xruns": self.total}],
            "source": "synthetic",
        }


class CrashingCollector(_SyntheticCollector):
    """Fails on every sample; proves a broken collector isolates itself."""

    name = "synthetic-crashing"
    stream_id = SOAK_CRASH_STREAM
    label = "Absturz-Kollektor (synthetisch)"

    def sample(self, context: Any) -> Any:
        self.tick += 1
        raise RuntimeError(f"deliberate collector crash #{self.tick}")


class MalformedCollector(_SyntheticCollector):
    """Returns values the payload contract must reject without killing the hub."""

    name = "synthetic-malformed"
    stream_id = SOAK_MALFORMED_STREAM
    label = "Fehlerhafter Kollektor (synthetisch)"

    def sample(self, context: Any) -> Any:
        self.tick += 1
        if self.tick % 2:
            return {"oversized": "x" * (TELEMETRY.MAX_PAYLOAD_BYTES + 64)}
        return {"unserializable": object()}


def synthetic_collectors() -> list[TELEMETRY.Collector]:
    return [
        SyntheticGraphCollector(),
        SyntheticLevelCollector(),
        SyntheticMidiCollector(),
        SyntheticTransportCollector(),
        SyntheticCpuCollector(),
        SyntheticXrunCollector(),
        CrashingCollector(),
        MalformedCollector(),
    ]


# --------------------------------------------------------------------- checks


def check(identifier: str, passed: bool, detail: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "status": "pass" if passed else "fail",
        "detail": detail,
    }


def skipped(identifier: str, detail: str) -> dict[str, Any]:
    return {"id": identifier, "status": "skipped", "detail": detail}


def compact_streams(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": stream["id"],
            "availability": stream["availability"],
            "sequence": stream["sequence"],
            "published_total": stream["published_total"],
            "dropped_total": stream["dropped_total"],
            "rejected_total": stream["rejected_total"],
            "buffer_depth": stream["buffer_depth"],
            "buffer_capacity": stream["buffer_capacity"],
            "age_ms": stream["age_ms"],
            "error": stream["error"],
            "error_total": stream["error_total"],
            "restart_count": stream["collector"]["restart_count"],
        }
        for stream in snapshot["streams"]
    ]


def queue_bound_checks(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    over_capacity = [
        stream["id"]
        for stream in snapshot["streams"]
        if stream["buffer_depth"] > stream["buffer_capacity"]
    ]
    results.append(
        check(
            "queue-depth-bounded",
            not over_capacity,
            "no stream buffer exceeded its capacity"
            if not over_capacity
            else f"buffers exceeded capacity: {', '.join(over_capacity)}",
        )
    )
    inconsistent = []
    for stream in snapshot["streams"]:
        retained = min(stream["published_total"], stream["buffer_capacity"])
        expected_drops = max(0, stream["published_total"] - retained)
        if stream["dropped_total"] != expected_drops or stream["buffer_depth"] != retained:
            inconsistent.append(stream["id"])
    results.append(
        check(
            "drop-accounting-consistent",
            not inconsistent,
            "published = retained + dropped for every stream"
            if not inconsistent
            else f"drop accounting differs: {', '.join(inconsistent)}",
        )
    )
    control = snapshot["control_channel"]
    results.append(
        check(
            "control-channel-lossless",
            control["dropped_total"] == 0 and control["lossless"] is True,
            f"control channel dropped {control['dropped_total']} commands "
            f"(accepted {control['accepted_total']}, rejected {control['rejected_total']})",
        )
    )
    results.append(
        check(
            "control-channel-separate",
            control["shares_telemetry_queue"] is False,
            "commands and state transitions do not share a telemetry queue",
        )
    )
    return results


#: Extrapolating a per-hour trend from a very short run would be an invented
#: claim, so the projection stays explicit about refusing it.
MIN_TREND_SECONDS = 60.0


def memory_projection(samples: list[tuple[float, int]], duration: float) -> dict[str, Any]:
    retention = {
        "in_memory_limit": MAX_MEMORY_SAMPLES,
        "reported_limit": MAX_REPORT_SAMPLES,
        "method": "first/latest-preserving periodic compaction",
    }
    if not samples:
        return {
            "available": False,
            "reason": "resident set size is not readable on this platform",
            "retention": retention,
        }
    values = [value for _elapsed, value in samples]
    growth = values[-1] - values[0]
    extrapolated = duration >= MIN_TREND_SECONDS
    return {
        "available": True,
        "unit": "KiB",
        "sample_count": len(samples),
        "start_kib": values[0],
        "end_kib": values[-1],
        "peak_kib": max(values),
        "growth_kib": growth,
        "growth_per_hour_kib": round(growth * 3600.0 / duration, 3)
        if extrapolated and duration > 0
        else None,
        "trend": ("rising" if growth > 0 else "flat") if extrapolated else "not-extrapolated",
        "trend_reason": (
            "hourly trend extrapolated from the observed run"
            if extrapolated
            else f"run shorter than {MIN_TREND_SECONDS:.0f} s; no hourly trend is claimed"
        ),
        "retention": retention,
        "samples": [
            {"elapsed_seconds": round(elapsed, 3), "resident_kib": value}
            for elapsed, value in downsample(samples)
        ],
    }


def memory_growth_check(projection: dict[str, Any]) -> dict[str, Any]:
    if projection.get("available") is not True:
        return skipped(
            "memory-growth-bounded",
            "resident set size is unavailable; no memory-growth claim is made",
        )
    growth = max(0, int(projection.get("growth_kib") or 0))
    hourly = projection.get("growth_per_hour_kib")
    if hourly is None:
        return check(
            "memory-growth-bounded",
            growth <= MAX_SHORT_RUN_MEMORY_GROWTH_KIB,
            f"short-run RSS growth {growth} KiB; limit {MAX_SHORT_RUN_MEMORY_GROWTH_KIB} KiB",
        )
    return check(
        "memory-growth-bounded",
        float(hourly) <= MAX_HOURLY_MEMORY_GROWTH_KIB,
        f"observed RSS trend {hourly} KiB/h; limit {MAX_HOURLY_MEMORY_GROWTH_KIB} KiB/h",
    )


def threaded_shutdown_probe(*, probe_seconds: float = 0.25) -> dict[str, Any]:
    """Prove threaded collector isolation and deterministic shutdown, bounded."""

    collectors = synthetic_collectors()
    for collector in collectors:
        collector.interval_seconds = 0.005
    hub = TELEMETRY.TelemetryHub(collectors, stream_capacity=8, control_capacity=8)
    hub.start()
    try:
        time.sleep(max(0.01, min(probe_seconds, 5.0)))
        snapshot = hub.snapshot()
    finally:
        stop_report = hub.stop()
    streams = {stream["id"]: stream for stream in snapshot["streams"]}
    healthy = [
        stream
        for identifier, stream in streams.items()
        if identifier not in {SOAK_CRASH_STREAM, SOAK_MALFORMED_STREAM}
    ]
    return {
        "probe_seconds": probe_seconds,
        "collectors": stop_report["collectors"],
        "joined": stop_report["joined"],
        "timed_out": stop_report["timed_out"],
        "crashing_stream_sequence": streams[SOAK_CRASH_STREAM]["sequence"],
        "crashing_stream_errors": streams[SOAK_CRASH_STREAM]["error_total"],
        "healthy_streams_publishing": sum(
            1 for stream in healthy if stream["sequence"] > 0
        ),
        "healthy_stream_count": len(healthy),
    }


# ------------------------------------------------------------------ soak runs


def run_synthetic(
    *,
    duration_seconds: float,
    iterations: int | None,
    load_factor: int,
    stream_capacity: int,
    control_capacity: int,
    sample_interval_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    hub = TELEMETRY.TelemetryHub(
        synthetic_collectors(),
        stream_capacity=stream_capacity,
        control_capacity=control_capacity,
    )
    hub.start(threads=False)
    started_wall = time.time()
    started = monotonic()
    started_cpu = process_cpu_seconds()
    memory_samples: list[tuple[float, int]] = []
    first_memory = resident_kib()
    if first_memory is not None:
        append_bounded_sample(memory_samples, (0.0, first_memory))
    next_memory_sample = started + sample_interval_seconds

    commands_submitted = 0
    commands_rejected = 0
    performed = 0
    deadline = started + duration_seconds
    memory_every = max(1, (iterations or MAX_REPORT_SAMPLES) // MAX_REPORT_SAMPLES)
    while True:
        if iterations is not None and performed >= iterations:
            break
        if iterations is None and monotonic() >= deadline:
            break
        for _repeat in range(load_factor):
            hub.pump()
        performed += 1
        # Commands and state transitions use the lossless channel only.
        try:
            hub.submit_command("soak-state-transition", {"iteration": performed})
            commands_submitted += 1
        except TELEMETRY.ControlChannelFull:
            commands_rejected += 1
            hub.control.drain(limit=control_capacity // 2)
        now = monotonic()
        if now >= next_memory_sample or performed % memory_every == 0:
            current = resident_kib()
            if current is not None:
                append_bounded_sample(memory_samples, (now - started, current))
            next_memory_sample = now + sample_interval_seconds

    elapsed = max(0.0, monotonic() - started)
    final_memory = resident_kib()
    if final_memory is not None:
        append_bounded_sample(memory_samples, (elapsed, final_memory))
    snapshot = hub.snapshot()
    stop_report = hub.stop()
    cpu_delta = max(0.0, process_cpu_seconds() - started_cpu)

    checks = queue_bound_checks(snapshot)
    streams = {stream["id"]: stream for stream in snapshot["streams"]}
    crash_stream = streams[SOAK_CRASH_STREAM]
    malformed_stream = streams[SOAK_MALFORMED_STREAM]
    healthy = [
        stream
        for identifier, stream in streams.items()
        if identifier not in {SOAK_CRASH_STREAM, SOAK_MALFORMED_STREAM}
    ]
    checks.append(
        check(
            "collector-isolation",
            crash_stream["error_total"] > 0
            and crash_stream["sequence"] == 0
            and all(stream["sequence"] > 0 for stream in healthy),
            f"the crashing collector failed {crash_stream['error_total']} times while "
            f"{len(healthy)} healthy streams kept publishing",
        )
    )
    checks.append(
        check(
            "malformed-payload-rejected",
            malformed_stream["sequence"] == 0
            and malformed_stream["rejected_total"] > 0
            and malformed_stream["error_total"] > 0,
            "malformed collector output became a stream error instead of a sample",
        )
    )
    probe = threaded_shutdown_probe()
    checks.append(
        check(
            "shutdown-deterministic",
            stop_report["state"] == "stopped"
            and probe["timed_out"] == 0
            and probe["joined"] == probe["collectors"],
            f"the threaded probe joined {probe['joined']} of {probe['collectors']} "
            f"collector threads, {probe['timed_out']} timed out",
        )
    )
    checks.append(
        check(
            "threaded-collector-isolation",
            probe["crashing_stream_sequence"] == 0
            and probe["crashing_stream_errors"] > 0
            and probe["healthy_streams_publishing"] == probe["healthy_stream_count"],
            f"under threads {probe['healthy_streams_publishing']} of "
            f"{probe['healthy_stream_count']} healthy streams kept publishing while the "
            f"crashing collector failed {probe['crashing_stream_errors']} times",
        )
    )
    xrun_stream = streams[TELEMETRY.STREAM_XRUNS]
    xrun_value = xrun_stream["value"] or {}
    xruns = {
        "available": bool(xrun_value),
        "authority": "synthetic",
        "live_counter": False,
        "end_total": xrun_value.get("total"),
        "delta": xrun_value.get("total"),
        "reason": "synthetic XRun generator; not evidence about real hardware",
    }
    memory = memory_projection(memory_samples, elapsed)
    checks.append(memory_growth_check(memory))
    return {
        "mode": "synthetic",
        "evidence_class": "synthetic-accelerated",
        "live_proof": False,
        "live_proof_reason": (
            "synthetic mode never observes real devices; use --mode live for hardware evidence"
        ),
        "started_at": TELEMETRY.utc_iso(started_wall),
        "finished_at": TELEMETRY.utc_iso(time.time()),
        "duration_seconds": round(elapsed, 3),
        "iterations": performed,
        "samples_per_iteration": load_factor * len(snapshot["streams"]),
        "load_factor": load_factor,
        "commands_submitted": commands_submitted,
        "commands_rejected": commands_rejected,
        "queue_bounds": {
            "stream_capacity": stream_capacity,
            "control_capacity": control_capacity,
            "max_buffer_depth": max(
                stream["buffer_depth"] for stream in snapshot["streams"]
            ),
            "dropped_total": snapshot["summary"]["dropped_total"],
        },
        "memory": memory,
        "cpu": {
            "process_cpu_seconds": round(cpu_delta, 3),
            "process_cpu_percent": round(cpu_delta / elapsed * 100.0, 3)
            if elapsed > 0
            else None,
        },
        "xruns": xruns,
        "control_channel": snapshot["control_channel"],
        "streams": compact_streams(snapshot),
        "checks": checks,
        "shutdown": stop_report,
        "threaded_probe": probe,
    }


def run_live(
    *,
    duration_seconds: float,
    sample_interval_seconds: float,
    load_factor: int,
    stream_capacity: int,
    control_capacity: int,
) -> dict[str, Any]:
    hub = TELEMETRY.build_default_hub(
        stream_capacity=stream_capacity,
        control_capacity=control_capacity,
    )
    started_wall = time.time()
    started = time.monotonic()
    started_cpu = process_cpu_seconds()
    hub.start()
    hub.submit_command("soak-live-start", {"duration_seconds": duration_seconds})
    memory_samples: list[tuple[float, int]] = []
    first_memory = resident_kib()
    if first_memory is not None:
        append_bounded_sample(memory_samples, (0.0, first_memory))
    first_xrun_total: int | None = None
    last_xrun_total: int | None = None
    availability_seen: dict[str, set[str]] = {}
    observations = 0
    snapshot_reads = 0
    try:
        deadline = started + duration_seconds
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            time.sleep(min(sample_interval_seconds, max(0.0, remaining)))
            snapshot = None
            for _read in range(load_factor):
                snapshot = hub.snapshot()
                snapshot_reads += 1
            assert snapshot is not None
            observations += 1
            elapsed = time.monotonic() - started
            current = resident_kib()
            if current is not None:
                append_bounded_sample(memory_samples, (elapsed, current))
            for stream in snapshot["streams"]:
                availability_seen.setdefault(stream["id"], set()).add(
                    stream["availability"]
                )
                if stream["id"] == TELEMETRY.STREAM_XRUNS and isinstance(
                    stream["value"], dict
                ):
                    total = stream["value"].get("total")
                    if isinstance(total, int):
                        if first_xrun_total is None:
                            first_xrun_total = total
                        last_xrun_total = total
        snapshot = hub.snapshot()
    finally:
        stop_report = hub.stop()
    elapsed = max(0.0, time.monotonic() - started)
    final_memory = resident_kib()
    if final_memory is not None:
        append_bounded_sample(memory_samples, (elapsed, final_memory))
    cpu_delta = max(0.0, process_cpu_seconds() - started_cpu)
    memory = memory_projection(memory_samples, elapsed)

    checks = queue_bound_checks(snapshot)
    checks.append(memory_growth_check(memory))
    checks.append(
        check(
            "service-survived",
            stop_report["state"] == "stopped" and stop_report["timed_out"] == 0,
            f"stop joined {stop_report['joined']} collector threads without timeout",
        )
    )
    expected_snapshot_reads = observations * load_factor
    checks.append(
        check(
            "snapshot-load-exercised",
            observations > 0 and snapshot_reads == expected_snapshot_reads,
            f"performed {snapshot_reads} snapshot reads over {observations} cycles "
            f"at load factor {load_factor}",
        )
    )
    live_streams = sorted(
        identifier
        for identifier, states in availability_seen.items()
        if "live" in states
    )
    unavailable_streams = sorted(
        stream["id"]
        for stream in snapshot["streams"]
        if stream["availability"] == "unavailable"
    )
    checks.append(
        check(
            "some-stream-observed",
            bool(live_streams),
            f"streams observed live: {', '.join(live_streams) or 'none'}",
        )
    )
    if first_xrun_total is None or last_xrun_total is None:
        xruns = {
            "available": False,
            "authority": "passive-observation",
            "live_counter": False,
            "start_total": None,
            "end_total": None,
            "delta": None,
            "reason": "no XRun counter was readable during this run; no XRun claim is made",
        }
        checks.append(
            skipped("xrun-delta", "pw-top exposed no XRun counter during this run")
        )
        xrun_clean = False
    else:
        xrun_delta = max(0, last_xrun_total - first_xrun_total)
        xruns = {
            "available": True,
            "authority": "passive-observation",
            "live_counter": True,
            "start_total": first_xrun_total,
            "end_total": last_xrun_total,
            "delta": xrun_delta,
            "reason": "observed with pw-top -b -n 1",
        }
        xrun_clean = xrun_delta == 0
        checks.append(
            check(
                "xrun-delta",
                xrun_clean,
                "no global XRun increase was observed"
                if xrun_clean
                else f"global XRun delta {xrun_delta}; attribution is unknown, so clean evidence is blocked",
            )
        )
    completed_duration = elapsed >= duration_seconds * 0.95
    failed_checks = [item["id"] for item in checks if item["status"] == "fail"]
    live_proof = completed_duration and bool(live_streams) and xrun_clean and not failed_checks
    if not live_streams:
        live_proof_reason = "no stream ever reached the live state; this run proves nothing about hardware"
    elif not xruns["available"]:
        live_proof_reason = "no readable XRun counter; no clean XRun proof is claimed"
    elif not xrun_clean:
        live_proof_reason = "a positive global XRun delta blocks clean evidence; attribution remains unknown"
    elif not completed_duration:
        live_proof_reason = "the requested wall-clock duration was not completed"
    elif failed_checks:
        live_proof_reason = "one or more bounded safety checks failed"
    else:
        live_proof_reason = f"{len(live_streams)} streams live, duration complete and XRun delta zero"
    return {
        "mode": "live",
        "evidence_class": "live-observed",
        "live_proof": live_proof,
        "live_proof_reason": live_proof_reason,
        "started_at": TELEMETRY.utc_iso(started_wall),
        "finished_at": TELEMETRY.utc_iso(time.time()),
        "duration_seconds": round(elapsed, 3),
        "planned_duration_seconds": duration_seconds,
        "observations": observations,
        "sample_interval_seconds": sample_interval_seconds,
        "load_factor": load_factor,
        "snapshot_reads": snapshot_reads,
        "snapshot_reads_per_second": round(snapshot_reads / elapsed, 3)
        if elapsed > 0
        else None,
        "live_streams": live_streams,
        "unavailable_streams": unavailable_streams,
        "queue_bounds": {
            "stream_capacity": stream_capacity,
            "control_capacity": control_capacity,
            "max_buffer_depth": max(
                stream["buffer_depth"] for stream in snapshot["streams"]
            ),
            "dropped_total": snapshot["summary"]["dropped_total"],
        },
        "memory": memory,
        "cpu": {
            "process_cpu_seconds": round(cpu_delta, 3),
            "process_cpu_percent": round(cpu_delta / elapsed * 100.0, 3)
            if elapsed > 0
            else None,
        },
        "xruns": xruns,
        "control_channel": snapshot["control_channel"],
        "streams": compact_streams(snapshot),
        "checks": checks,
        "shutdown": stop_report,
    }


def build_report(body: dict[str, Any]) -> dict[str, Any]:
    failed = [item["id"] for item in body["checks"] if item["status"] == "fail"]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "status": "fail" if failed else "pass",
        "failed_checks": failed,
        "safety": TELEMETRY.safety_boundary(),
        **body,
    }


def write_report(report: dict[str, Any], path: pathlib.Path) -> None:
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise SoakError("soak report exceeds its size bound")
    parent = path.parent
    if not parent.is_dir():
        raise SoakError(f"report directory does not exist: {parent}")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise SoakError(f"report path cannot be opened safely: {path}") from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)


def bounded_duration(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Dauer muss eine Zahl sein.") from error
    if not 0.0 < seconds <= MAX_DURATION_SECONDS:
        raise argparse.ArgumentTypeError(
            f"Dauer muss zwischen 0 und {MAX_DURATION_SECONDS} Sekunden liegen."
        )
    return seconds


def bounded_iterations(value: str) -> int:
    try:
        iterations = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Iterationen müssen ganzzahlig sein.") from error
    if not 1 <= iterations <= MAX_ITERATIONS:
        raise argparse.ArgumentTypeError(
            f"Iterationen müssen zwischen 1 und {MAX_ITERATIONS} liegen."
        )
    return iterations


def bounded_load_factor(value: str) -> int:
    try:
        factor = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Lastfaktor muss ganzzahlig sein.") from error
    if not 1 <= factor <= MAX_LOAD_FACTOR:
        raise argparse.ArgumentTypeError(
            f"Lastfaktor muss zwischen 1 und {MAX_LOAD_FACTOR} liegen."
        )
    return factor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audio-telemetry-soak", description=__doc__)
    parser.add_argument("--mode", choices=("synthetic", "live"), default="synthetic")
    parser.add_argument("--duration-seconds", type=bounded_duration, default=5.0)
    parser.add_argument(
        "--iterations",
        type=bounded_iterations,
        default=None,
        help="synthetic mode only: run an exact number of pump iterations",
    )
    parser.add_argument("--load-factor", type=bounded_load_factor, default=1)
    parser.add_argument("--sample-interval-seconds", type=float, default=1.0)
    parser.add_argument(
        "--stream-capacity",
        type=int,
        default=TELEMETRY.DEFAULT_STREAM_CAPACITY,
    )
    parser.add_argument(
        "--control-capacity",
        type=int,
        default=TELEMETRY.DEFAULT_CONTROL_CAPACITY,
    )
    parser.add_argument("--report", type=pathlib.Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not 0.01 <= args.sample_interval_seconds <= 600.0:
        raise SoakError("Abtastintervall muss zwischen 0.01 und 600 Sekunden liegen.")
    if not 1 <= args.stream_capacity <= TELEMETRY.MAX_STREAM_CAPACITY:
        raise SoakError("Streamkapazität liegt außerhalb des Vertrags.")
    if not 1 <= args.control_capacity <= TELEMETRY.MAX_CONTROL_CAPACITY:
        raise SoakError("Kommandokapazität liegt außerhalb des Vertrags.")
    if args.mode == "live" and args.iterations is not None:
        raise SoakError("--iterations gilt nur im synthetischen Modus.")
    if args.mode == "synthetic":
        body = run_synthetic(
            duration_seconds=args.duration_seconds,
            iterations=args.iterations,
            load_factor=args.load_factor,
            stream_capacity=args.stream_capacity,
            control_capacity=args.control_capacity,
            sample_interval_seconds=min(args.sample_interval_seconds, 1.0),
        )
    else:
        body = run_live(
            duration_seconds=args.duration_seconds,
            sample_interval_seconds=args.sample_interval_seconds,
            load_factor=args.load_factor,
            stream_capacity=args.stream_capacity,
            control_capacity=args.control_capacity,
        )
    return build_report(body)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run(args)
    except (SoakError, TELEMETRY.TelemetryError) as error:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "audio_telemetry_soak_error",
                    "error": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    if args.report is not None:
        write_report(report, args.report)
    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
