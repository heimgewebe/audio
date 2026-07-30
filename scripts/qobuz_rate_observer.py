#!/usr/bin/env python3
"""Create a bounded, read-only Mopidy-Qobuz rate observation."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAB_PATH = ROOT / "scripts" / "laboratory_gate.py"
SYSTEM_TRUTH_PATH = ROOT / "scripts" / "system_truth.py"
MOPIDY_RPC_URL = "http://127.0.0.1:6680/mopidy/rpc"
MAX_RPC_BYTES = 131_072
RPC_TIMEOUT_SECONDS = 5.0
POLL_SECONDS = 1.0
QOBUZ_URI_RE = re.compile(r"^qobuz:track:(?P<track_id>[0-9]+)$")
RATE_RE = re.compile(r"(?P<rate>[0-9]+)Hz$")
DOWNLOADABLE_RE = re.compile(
    r"Valid track found: <DownloadableTrack "
    r"(?P<track_id>[0-9]+)@(?P<extension>[A-Za-z0-9]+) "
    r"\[(?P<bit_depth>[0-9]+)/(?P<rate_khz>[0-9]+(?:\.[0-9]+)?)\]>"
)


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LAB = load_module("laboratory_gate_for_qobuz_observer", LAB_PATH)
SYSTEM_TRUTH = load_module("system_truth_for_qobuz_observer", SYSTEM_TRUTH_PATH)
MAX_QOBUZ_JOURNAL_LINES = LAB.MAX_QOBUZ_JOURNAL_LINES
PACTL_INFO_ARGV = LAB.QOBUZ_PACTL_INFO_ARGV
PACTL_SINKS_ARGV = LAB.QOBUZ_PACTL_SINKS_ARGV
PACTL_INPUTS_ARGV = LAB.QOBUZ_PACTL_INPUTS_ARGV


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def monotonic_now() -> float:
    return time.monotonic()


def sleep_for(seconds: float) -> None:
    time.sleep(seconds)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8", errors="strict"))


def _run_read_only(argv: tuple[str, ...]):
    SYSTEM_TRUTH.assert_read_only_commands((argv,))
    result = SYSTEM_TRUTH.run_read_only(argv)
    if result.argv != argv:
        raise ValueError("read-only result is bound to another command")
    if result.error is not None or result.returncode != 0:
        raise ValueError(f"read-only command failed: {argv[0]}")
    if result.stdout_truncated or result.stderr_truncated:
        raise ValueError(f"read-only command output is truncated: {argv[0]}")
    return result


def qobuz_journal_argv(started_at: str, ended_at: str) -> tuple[str, ...]:
    return LAB.qobuz_journal_argv(started_at, ended_at)


def _rpc_payload() -> list[dict[str, Any]]:
    return [dict(item) for item in LAB.QOBUZ_RPC_PAYLOAD]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _local_rpc_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )


def _rpc_read() -> tuple[Any, dict[str, Any]]:
    request_body = json.dumps(
        _rpc_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    request = urllib.request.Request(
        MOPIDY_RPC_URL,
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _local_rpc_opener().open(
            request, timeout=RPC_TIMEOUT_SECONDS
        ) as response:
            body = response.read(MAX_RPC_BYTES + 1)
            status = getattr(response, "status", 200)
            final_url = response.geturl()
    except (OSError, urllib.error.URLError) as exc:
        raise ValueError("Mopidy RPC query failed") from exc
    if final_url != MOPIDY_RPC_URL:
        raise ValueError("Mopidy RPC left the fixed loopback endpoint")
    if status != 200:
        raise ValueError("Mopidy RPC returned a non-200 status")
    if len(body) > MAX_RPC_BYTES:
        raise ValueError("Mopidy RPC response exceeds the byte limit")
    try:
        payload = json.loads(body.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Mopidy RPC response is not valid UTF-8 JSON") from exc
    binding = {
        "endpoint": "mopidy-loopback-json-rpc",
        "request_sha256": _sha256_bytes(request_body),
        "response_sha256": _sha256_bytes(body),
        "response_bytes": len(body),
    }
    return payload, binding


def _hash_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("track text metadata has the wrong type")
    return _sha256_text(value)


def _track_identity(track: Any) -> dict[str, Any] | None:
    if track is None:
        return None
    if not isinstance(track, dict):
        raise ValueError("Mopidy current track is not an object")
    uri = track.get("uri")
    if not isinstance(uri, str):
        return None
    match = QOBUZ_URI_RE.fullmatch(uri)
    if match is None:
        return None
    artists = track.get("artists", [])
    if not isinstance(artists, list):
        raise ValueError("Mopidy track artists are not a list")
    artist_hashes: list[str] = []
    for artist in artists:
        if not isinstance(artist, dict):
            raise ValueError("Mopidy track artist is not an object")
        artist_hash = _hash_optional_text(artist.get("name"))
        if artist_hash is not None:
            artist_hashes.append(artist_hash)
    album = track.get("album")
    if album is not None and not isinstance(album, dict):
        raise ValueError("Mopidy track album is not an object")
    length_ms = track.get("length")
    if length_ms is not None:
        length_ms = _positive_int(length_ms, "track length_ms")
    identity = {
        "uri": uri,
        "track_id": match.group("track_id"),
        "name_sha256": _hash_optional_text(track.get("name")),
        "album_sha256": _hash_optional_text(album.get("name") if album else None),
        "artists_sha256": LAB.canonical_value_sha256(artist_hashes),
        "artist_count": len(artist_hashes),
        "length_ms": length_ms,
    }
    identity["fingerprint"] = LAB.canonical_value_sha256(identity)
    return identity


def playback_snapshot() -> dict[str, Any]:
    payload, binding = _rpc_read()
    if not isinstance(payload, list):
        raise ValueError("Mopidy RPC batch response is not a list")
    responses: dict[int, Any] = {}
    for item in payload:
        if not isinstance(item, dict) or item.get("jsonrpc") != "2.0":
            raise ValueError("Mopidy RPC batch item is invalid")
        identifier = item.get("id")
        if identifier not in {1, 2, 3} or identifier in responses:
            raise ValueError("Mopidy RPC batch identifiers are invalid")
        if "error" in item:
            raise ValueError("Mopidy RPC batch contains an error")
        responses[identifier] = item.get("result")
    if set(responses) != {1, 2, 3}:
        raise ValueError("Mopidy RPC batch response is incomplete")
    state = responses[1]
    if state not in {"playing", "paused", "stopped"}:
        raise ValueError("Mopidy playback state is invalid")
    position_ms = responses[3]
    if position_ms is not None:
        position_ms = _nonnegative_int(position_ms, "Mopidy position_ms")
    identity = _track_identity(responses[2])
    return {
        "state": state,
        "position_ms": position_ms,
        "track": identity,
        "rpc": binding,
    }


def _json_command(argv: tuple[str, ...]) -> tuple[Any, dict[str, Any]]:
    result = _run_read_only(argv)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{argv[0]} JSON output is invalid") from exc
    binding = {
        "query_argv": list(argv),
        "query_argv_sha256": LAB.canonical_value_sha256(list(argv)),
        "stdout_sha256": result.stdout_sha256,
        "stdout_total_bytes": result.stdout_total_bytes,
        "stderr_sha256": result.stderr_sha256,
        "stderr_total_bytes": result.stderr_total_bytes,
        "complete": True,
    }
    return payload, binding


def _sample_rate(sample_specification: Any, label: str) -> int:
    if not isinstance(sample_specification, str):
        raise ValueError(f"{label} sample specification is missing")
    match = RATE_RE.search(sample_specification)
    if match is None:
        raise ValueError(f"{label} sample specification has no rate")
    return _positive_int(int(match.group("rate")), f"{label} rate_hz")


def pulse_snapshot() -> dict[str, Any]:
    info, info_binding = _json_command(PACTL_INFO_ARGV)
    sinks, sinks_binding = _json_command(PACTL_SINKS_ARGV)
    sink_inputs, inputs_binding = _json_command(PACTL_INPUTS_ARGV)
    if not isinstance(info, dict) or not isinstance(sinks, list):
        raise ValueError("Pulse sink projection has the wrong shape")
    if not isinstance(sink_inputs, list):
        raise ValueError("Pulse sink-input projection has the wrong shape")
    default_sink_name = info.get("default_sink_name")
    if not isinstance(default_sink_name, str) or not default_sink_name:
        raise ValueError("Pulse default sink name is missing")
    matching_sinks = [
        item
        for item in sinks
        if isinstance(item, dict) and item.get("name") == default_sink_name
    ]
    if len(matching_sinks) != 1:
        raise ValueError("Pulse default sink is not uniquely observable")
    sink = matching_sinks[0]
    sink_index = _positive_int(sink.get("index"), "default sink index")
    endpoint_rate_hz = _sample_rate(
        sink.get("sample_specification"), "default sink"
    )

    active_mopidy: list[dict[str, Any]] = []
    for item in sink_inputs:
        if not isinstance(item, dict) or item.get("corked") is not False:
            continue
        properties = item.get("properties")
        if not isinstance(properties, dict):
            continue
        identity_values = [
            properties.get("application.name"),
            properties.get("application.process.binary"),
            properties.get("node.name"),
        ]
        if not any(
            isinstance(value, str) and "mopidy" in value.casefold()
            for value in identity_values
        ):
            continue
        active_mopidy.append(item)
    blockers: list[str] = []
    stream: dict[str, Any] | None = None
    if not active_mopidy:
        blockers.append("active-mopidy-pulse-stream-missing")
    elif len(active_mopidy) > 1:
        blockers.append("active-mopidy-pulse-stream-ambiguous")
    else:
        item = active_mopidy[0]
        stream_sink = _positive_int(item.get("sink"), "Mopidy stream sink")
        if stream_sink != sink_index:
            blockers.append("mopidy-stream-not-on-default-sink")
        properties = item.get("properties", {})
        stream = {
            "index": _positive_int(item.get("index"), "Mopidy sink-input index"),
            "sink_index": stream_sink,
            "rate_hz": _sample_rate(
                item.get("sample_specification"), "Mopidy stream"
            ),
            "sample_specification_sha256": _sha256_text(
                str(item.get("sample_specification"))
            ),
            "application_name_sha256": _hash_optional_text(
                properties.get("application.name")
            ),
            "application_binary_sha256": _hash_optional_text(
                properties.get("application.process.binary")
            ),
            "media_name_sha256": _hash_optional_text(properties.get("media.name")),
        }
    return {
        "default_sink": {
            "name": default_sink_name,
            "index": sink_index,
            "rate_hz": endpoint_rate_hz,
            "sample_specification_sha256": _sha256_text(
                str(sink.get("sample_specification"))
            ),
        },
        "mopidy_stream": stream,
        "blockers": blockers,
        "queries": {
            "info": info_binding,
            "sinks": sinks_binding,
            "sink_inputs": inputs_binding,
        },
    }


def truth_binding() -> dict[str, Any]:
    report = SYSTEM_TRUTH.build_report()
    SYSTEM_TRUTH.verify_report(report)
    runtime = report.get("runtime")
    doctor = report.get("doctor")
    if not isinstance(runtime, dict) or not isinstance(doctor, dict):
        raise ValueError("system-truth report has no runtime or doctor projection")
    graph = doctor.get("graph")
    if not isinstance(graph, dict):
        raise ValueError("system-truth report has no graph projection")
    return {
        "report_sha256": str(report["report_sha256"]),
        "truth_chain_sha256": str(report["truth_chain_sha256"]),
        "graph_fingerprint": str(runtime["graph_fingerprint"]),
        "rate_hz": _positive_int(graph.get("force_rate_hz"), "graph rate_hz"),
        "quantum_frames": _positive_int(
            graph.get("force_quantum_frames"), "graph quantum_frames"
        ),
    }


def _journal_event_time(record: dict[str, Any]) -> dt.datetime:
    raw = record.get("__REALTIME_TIMESTAMP")
    if raw is None:
        raw = record.get("_SOURCE_REALTIME_TIMESTAMP")
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    try:
        microseconds = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Qobuz journal event has no realtime timestamp") from exc
    if microseconds <= 0:
        raise ValueError("Qobuz journal timestamp must be positive")
    seconds, remaining_microseconds = divmod(microseconds, 1_000_000)
    return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc) + dt.timedelta(
        microseconds=remaining_microseconds
    )


def _downloadable_events(
    journal_text: str,
    track_id: str,
    started_at: str,
    ended_at: str,
) -> list[dict[str, Any]]:
    started = LAB.parse_timestamp(started_at, "Qobuz wait start")
    ended = LAB.parse_timestamp(ended_at, "Qobuz observation end")
    matching: list[dict[str, Any]] = []
    for raw in journal_text.splitlines():
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Qobuz journal output is not JSON lines") from exc
        if not isinstance(record, dict):
            raise ValueError("Qobuz journal record is not an object")
        message = record.get("MESSAGE")
        if not isinstance(message, str):
            continue
        match = DOWNLOADABLE_RE.search(message)
        if match is None or match.group("track_id") != track_id:
            continue
        observed_at = _journal_event_time(record)
        if observed_at < started or observed_at > ended:
            continue
        rate_khz = float(match.group("rate_khz"))
        rate_hz = int(round(rate_khz * 1000))
        event = {
            "track_id": track_id,
            "extension": match.group("extension").upper(),
            "bit_depth": _positive_int(int(match.group("bit_depth")), "bit_depth"),
            "rate_hz": _positive_int(rate_hz, "track rate_hz"),
            "observed_at": observed_at.isoformat(),
            "message_sha256": _sha256_text(message),
        }
        matching.append(event)
    return sorted(matching, key=lambda item: item["observed_at"])


def journal_binding(
    wait_started_at: str, ended_at: str, track_id: str
) -> tuple[dict[str, Any], list[str]]:
    argv = qobuz_journal_argv(wait_started_at, ended_at)
    result = _run_read_only(argv)
    lines = result.stdout.splitlines()
    blockers: list[str] = []
    if len(lines) > MAX_QOBUZ_JOURNAL_LINES:
        blockers.append("qobuz-journal-line-limit-exceeded")
    events = _downloadable_events(
        result.stdout, track_id, wait_started_at, ended_at
    )
    if not events:
        blockers.append("qobuz-downloadable-event-missing")
    if len(events) > 1:
        blockers.append("qobuz-downloadable-event-ambiguous")
    event = events[0] if len(events) == 1 else None
    if event is not None and event["extension"] != "FLAC":
        blockers.append("qobuz-downloadable-is-not-flac")
    binding = {
        "source": "mopidy-service-qobuz-downloadable-event",
        "query_argv": list(argv),
        "query_argv_sha256": LAB.canonical_value_sha256(list(argv)),
        "returncode": result.returncode,
        "stdout_sha256": result.stdout_sha256,
        "stdout_total_bytes": result.stdout_total_bytes,
        "stdout_truncated": result.stdout_truncated,
        "line_count": len(lines),
        "max_lines": MAX_QOBUZ_JOURNAL_LINES,
        "matching_event_count": len(events),
        "matching_events_sha256": LAB.canonical_value_sha256(events),
        "event": event,
        "complete": True,
    }
    return binding, blockers


def _same_track(snapshot: dict[str, Any], fingerprint: str) -> bool:
    track = snapshot.get("track")
    return (
        snapshot.get("state") == "playing"
        and isinstance(track, dict)
        and track.get("fingerprint") == fingerprint
    )


def _fail_without_track(
    wait_started_at: dt.datetime,
    wait_started_monotonic: float,
    duration_seconds: int,
    start_timeout_seconds: int,
    last_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    ended_at = utc_now()
    elapsed = max(0.0, monotonic_now() - wait_started_monotonic)
    return {
        "schema_version": 1,
        "kind": "qobuz_rate_observation",
        "gate": "qobuz-rate-proof",
        "result": "fail",
        "measured_at": ended_at.isoformat(),
        "physical_state_sha256": None,
        "requested_duration_seconds": duration_seconds,
        "start_timeout_seconds": start_timeout_seconds,
        "wait_started_at": wait_started_at.isoformat(),
        "observation_started_at": None,
        "observation_ended_at": ended_at.isoformat(),
        "duration_seconds": 0.0,
        "wait_duration_seconds": round(elapsed, 3),
        "track_fingerprint": None,
        "track_rate_hz": None,
        "stream_rate_hz": None,
        "graph_rate_hz": None,
        "endpoint_rate_hz": None,
        "graph_fingerprint": None,
        "resampling_observed": None,
        "method": LAB.QOBUZ_METHOD,
        "blockers": ["new-playing-mopidy-qobuz-track-not-observed"],
        "last_playback": last_snapshot,
        "does_not_establish": [
            "Qobuz rate without a newly started playing Mopidy track",
            "browser Qobuz playback rate",
            "profile apply authority",
        ],
    }


def qobuz_rate_observation(
    duration_seconds: int = 60, start_timeout_seconds: int = 60
) -> dict[str, Any]:
    duration_seconds = _positive_int(duration_seconds, "duration_seconds")
    start_timeout_seconds = _nonnegative_int(
        start_timeout_seconds, "start_timeout_seconds"
    )
    if duration_seconds < 60 or duration_seconds > 3_600:
        raise ValueError("Qobuz observation must cover 60 to 3600 seconds")
    if start_timeout_seconds > 3_600:
        raise ValueError("Qobuz start timeout must not exceed 3600 seconds")

    wait_started_at = utc_now()
    wait_started_monotonic = monotonic_now()
    initial_snapshot = playback_snapshot()
    initial_track = initial_snapshot.get("track")
    baseline_fingerprint = (
        str(initial_track.get("fingerprint"))
        if initial_snapshot.get("state") == "playing"
        and isinstance(initial_track, dict)
        and QOBUZ_URI_RE.fullmatch(str(initial_track.get("uri"))) is not None
        else None
    )
    departed_baseline = baseline_fingerprint is None
    baseline = {
        "state": initial_snapshot["state"],
        "position_ms": initial_snapshot["position_ms"],
        "track_fingerprint": baseline_fingerprint,
        "rpc_response_sha256": initial_snapshot["rpc"]["response_sha256"],
    }
    last_snapshot: dict[str, Any] | None = initial_snapshot
    selected: dict[str, Any] | None = None
    while True:
        elapsed = monotonic_now() - wait_started_monotonic
        if elapsed >= start_timeout_seconds:
            return _fail_without_track(
                wait_started_at,
                wait_started_monotonic,
                duration_seconds,
                start_timeout_seconds,
                last_snapshot,
            )
        sleep_for(min(POLL_SECONDS, max(0.0, start_timeout_seconds - elapsed)))
        last_snapshot = playback_snapshot()
        observed_elapsed = monotonic_now() - wait_started_monotonic
        if observed_elapsed > start_timeout_seconds:
            return _fail_without_track(
                wait_started_at,
                wait_started_monotonic,
                duration_seconds,
                start_timeout_seconds,
                last_snapshot,
            )
        track = last_snapshot.get("track")
        playing_qobuz = (
            last_snapshot.get("state") == "playing"
            and isinstance(track, dict)
            and QOBUZ_URI_RE.fullmatch(str(track.get("uri"))) is not None
        )
        current_fingerprint = (
            str(track.get("fingerprint")) if playing_qobuz else None
        )
        if playing_qobuz and (
            baseline_fingerprint is None
            or current_fingerprint != baseline_fingerprint
            or departed_baseline
        ):
            selected = last_snapshot
            break
        if baseline_fingerprint is not None and not playing_qobuz:
            departed_baseline = True

    track_identity = selected["track"]
    track_fingerprint = str(track_identity["fingerprint"])
    observation_started_at = utc_now()
    observation_started_monotonic = monotonic_now()
    wait_duration = observation_started_monotonic - wait_started_monotonic
    truth_before = truth_binding()
    pulse_before = pulse_snapshot()
    samples = [selected]
    blockers = list(pulse_before["blockers"])

    while True:
        elapsed = monotonic_now() - observation_started_monotonic
        if elapsed >= duration_seconds:
            break
        sleep_for(min(POLL_SECONDS, duration_seconds - elapsed))
        sample = playback_snapshot()
        samples.append(sample)
        if not _same_track(sample, track_fingerprint):
            blockers.append("qobuz-playback-not-continuous-or-track-changed")
            break

    observation_ended_monotonic = monotonic_now()
    observation_ended_at = utc_now()
    actual_duration = observation_ended_monotonic - observation_started_monotonic
    if actual_duration < duration_seconds:
        blockers.append("qobuz-observation-ended-before-requested-duration")
    pulse_after = pulse_snapshot()
    blockers.extend(pulse_after["blockers"])
    truth_after = truth_binding()

    before_stream = pulse_before.get("mopidy_stream")
    after_stream = pulse_after.get("mopidy_stream")
    if before_stream != after_stream:
        blockers.append("mopidy-pulse-stream-changed")
    if pulse_before.get("default_sink") != pulse_after.get("default_sink"):
        blockers.append("qobuz-default-sink-changed")
    for field in ("graph_fingerprint", "rate_hz", "quantum_frames"):
        if truth_before.get(field) != truth_after.get(field):
            blockers.append(f"qobuz-graph-changed:{field}")

    journal, journal_blockers = journal_binding(
        wait_started_at.isoformat(),
        observation_ended_at.isoformat(),
        str(track_identity["track_id"]),
    )
    blockers.extend(journal_blockers)
    journal_event = journal.get("event")
    track_rate_hz = journal_event.get("rate_hz") if journal_event else None
    stream_rate_hz = before_stream.get("rate_hz") if before_stream else None
    graph_rate_hz = truth_before.get("rate_hz")
    endpoint_rate_hz = pulse_before["default_sink"]["rate_hz"]
    observed_rates = [
        rate
        for rate in (
            track_rate_hz,
            stream_rate_hz,
            graph_rate_hz,
            endpoint_rate_hz,
        )
        if isinstance(rate, int)
    ]
    if len(observed_rates) != 4 or len(set(observed_rates)) != 1:
        blockers.append("qobuz-track-stream-graph-endpoint-rate-mismatch")
    positions = [
        item.get("position_ms")
        for item in samples
        if isinstance(item.get("position_ms"), int)
    ]
    position_monotonic = all(
        later >= earlier for earlier, later in zip(positions, positions[1:])
    )
    if len(positions) != len(samples) or not position_monotonic:
        blockers.append("qobuz-playback-position-not-monotonic")

    blockers = sorted(set(blockers))
    payload = {
        "schema_version": 1,
        "kind": "qobuz_rate_observation",
        "gate": "qobuz-rate-proof",
        "result": "pass" if not blockers else "fail",
        "measured_at": observation_ended_at.isoformat(),
        "physical_state_sha256": None,
        "requested_duration_seconds": duration_seconds,
        "start_timeout_seconds": start_timeout_seconds,
        "wait_started_at": wait_started_at.isoformat(),
        "wait_duration_seconds": round(wait_duration, 3),
        "baseline": baseline,
        "baseline_departure_observed": departed_baseline,
        "observation_started_at": observation_started_at.isoformat(),
        "observation_ended_at": observation_ended_at.isoformat(),
        "duration_seconds": round(actual_duration, 3),
        "track_identity": track_identity,
        "track_fingerprint": track_fingerprint,
        "track_rate_hz": track_rate_hz,
        "stream_rate_hz": stream_rate_hz,
        "graph_rate_hz": graph_rate_hz,
        "endpoint_rate_hz": endpoint_rate_hz,
        "graph_fingerprint": truth_before["graph_fingerprint"],
        "resampling_observed": len(observed_rates) != 4
        or len(set(observed_rates)) != 1,
        "method": LAB.QOBUZ_METHOD,
        "blockers": blockers,
        "implementation": {
            "qobuz_rate_observer_sha256": LAB.sha256_file(pathlib.Path(__file__)),
            "laboratory_gate_sha256": LAB.sha256_file(LAB_PATH),
            "system_truth_sha256": LAB.sha256_file(SYSTEM_TRUTH_PATH),
        },
        "truth_before": truth_before,
        "truth_after": truth_after,
        "pulse_before": pulse_before,
        "pulse_after": pulse_after,
        "journal": journal,
        "playback": {
            "sample_count": len(samples),
            "first_position_ms": positions[0] if positions else None,
            "last_position_ms": positions[-1] if positions else None,
            "position_monotonic": position_monotonic,
            "rpc_request_sha256": selected["rpc"]["request_sha256"],
            "rpc_response_chain_sha256": LAB.canonical_value_sha256(
                [item["rpc"]["response_sha256"] for item in samples]
            ),
        },
        "criteria": {
            "minimum_duration_seconds": 60,
            "requires_new_qobuz_start_after_wait_begins": True,
            "requires_continuous_playback": True,
            "requires_flac_downloadable_event": True,
            "requires_equal_track_stream_graph_endpoint_rates": True,
        },
        "does_not_establish": [
            "browser Qobuz playback rate",
            "audible quality or subjective transparency",
            "stability outside the bounded observation window",
            "profile apply authority",
        ],
    }
    return payload
