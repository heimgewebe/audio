#!/usr/bin/env python3
"""Read-only QBZD queue/playback identity consistency observer."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

QBZD_BASE_URL = "http://127.0.0.1:8182"
QUEUE_URL = f"{QBZD_BASE_URL}/api/queue?offset=0&limit=64"
NOW_PLAYING_URL = f"{QBZD_BASE_URL}/api/now-playing"
MAX_RESPONSE_BYTES = 128 * 1024
REQUEST_TIMEOUT_SECONDS = 1.5


class ObservationError(RuntimeError):
    """A bounded observation could not be classified safely."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class QueueSnapshot:
    current_track_id: int | None
    current_index: int | None
    total_tracks: int
    upcoming_track_ids: tuple[int, ...]
    history_len: int
    shuffle: bool
    repeat: str

    def signature(self) -> tuple[object, ...]:
        return (
            self.current_track_id,
            self.current_index,
            self.total_tracks,
            self.upcoming_track_ids,
            self.history_len,
            self.shuffle,
            self.repeat,
        )


@dataclass(frozen=True)
class NowPlayingSnapshot:
    queue_track_id: int | None
    playback_track_id: int | None
    is_playing: bool


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ObservationError(f"{label} is invalid")
    return value


def _optional_integer(
    value: object, *, label: str, minimum: int = 0
) -> int | None:
    if value is None:
        return None
    return _integer(value, label=label, minimum=minimum)


def _track_id(track: object, *, label: str) -> int | None:
    if track is None:
        return None
    if not isinstance(track, dict):
        raise ObservationError(f"{label} is invalid")
    return _integer(track.get("id"), label=f"{label}.id", minimum=1)


def classify_queue_payload(payload: object) -> QueueSnapshot:
    if not isinstance(payload, dict):
        raise ObservationError("queue payload is invalid")

    current_track_id = _track_id(payload.get("current_track"), label="current_track")
    current_index = _optional_integer(
        payload.get("current_index"), label="current_index"
    )
    total_tracks = _integer(payload.get("total_tracks"), label="total_tracks")
    history_len = _integer(payload.get("history_len"), label="history_len")
    shuffle = payload.get("shuffle")
    repeat = payload.get("repeat")
    upcoming = payload.get("upcoming")

    if not isinstance(shuffle, bool):
        raise ObservationError("shuffle is invalid")
    if repeat not in {"off", "all", "one"}:
        raise ObservationError("repeat is invalid")
    if not isinstance(upcoming, list):
        raise ObservationError("upcoming is invalid")

    upcoming_track_ids = tuple(
        _track_id(track, label=f"upcoming[{index}]")
        for index, track in enumerate(upcoming)
    )
    if any(track_id is None for track_id in upcoming_track_ids):
        raise ObservationError("upcoming track is null")

    if current_track_id is None and current_index is not None:
        raise ObservationError("queue index exists without current track")
    if current_track_id is not None and current_index is None:
        raise ObservationError("current track exists without queue index")
    if current_index is not None and current_index >= total_tracks:
        raise ObservationError("current queue index is out of range")
    if total_tracks == 0 and (current_track_id is not None or upcoming_track_ids):
        raise ObservationError("empty queue has active track state")

    return QueueSnapshot(
        current_track_id=current_track_id,
        current_index=current_index,
        total_tracks=total_tracks,
        upcoming_track_ids=tuple(int(track_id) for track_id in upcoming_track_ids),
        history_len=history_len,
        shuffle=shuffle,
        repeat=repeat,
    )


def classify_now_playing_payload(payload: object) -> NowPlayingSnapshot:
    if not isinstance(payload, dict):
        raise ObservationError("now-playing payload is invalid")
    playback = payload.get("playback")
    if not isinstance(playback, dict):
        raise ObservationError("playback payload is invalid")

    queue_track_id = _track_id(payload.get("track"), label="track")
    raw_playback_track_id = _integer(
        playback.get("track_id"), label="playback.track_id"
    )
    playback_track_id = raw_playback_track_id or None
    is_playing = playback.get("is_playing")
    if not isinstance(is_playing, bool):
        raise ObservationError("playback.is_playing is invalid")

    if is_playing and playback_track_id is None:
        raise ObservationError("playing state has no playback track")

    return NowPlayingSnapshot(
        queue_track_id=queue_track_id,
        playback_track_id=playback_track_id,
        is_playing=is_playing,
    )


def classify_consistency(
    queue_before: QueueSnapshot,
    now_playing: NowPlayingSnapshot,
    queue_after: QueueSnapshot,
) -> dict[str, object]:
    queue_stable = queue_before.signature() == queue_after.signature()
    base: dict[str, object] = {
        "schema_version": 1,
        "kind": "qbzd_playback_identity_observation",
        "read_only": True,
        "persistence_contract": "no-track-metadata-or-track-ids",
        "queue_samples_equal": queue_stable,
        "sampled_identity_match": None,
        "authoritative_identity_proof": False,
        "playing": now_playing.is_playing,
        "queue_index": queue_after.current_index if queue_stable else None,
        "queue_total_tracks": queue_after.total_tracks if queue_stable else None,
    }

    if not queue_stable:
        return {
            **base,
            "status": "sample-window-changed",
            "reason": "queue-samples-differ-around-now-playing-read",
        }

    if now_playing.queue_track_id != now_playing.playback_track_id:
        return {
            **base,
            "status": "sampled-mismatch",
            "reason": "now-playing-internal-mismatch",
            "sampled_identity_match": False,
        }

    if queue_after.current_track_id != now_playing.queue_track_id:
        return {
            **base,
            "status": "sampled-mismatch",
            "reason": "queue-playback-mismatch",
            "sampled_identity_match": False,
        }

    if queue_after.current_track_id is None:
        return {
            **base,
            "status": "sampled-idle",
            "reason": "unversioned-api-aba-not-excluded",
            "sampled_identity_match": True,
        }

    return {
        **base,
        "status": "sampled-match",
        "reason": "unversioned-api-aba-not-excluded",
        "sampled_identity_match": True,
    }


def fetch_json(url: str) -> dict[str, object]:
    if url not in {QUEUE_URL, NOW_PLAYING_URL}:
        raise ObservationError("URL is outside the fixed QBZD loopback contract")
    request = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirectHandler()
    )
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status = getattr(response, "status", 200)
            final_url = response.geturl()
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
        raise ObservationError("QBZD identity endpoint is unavailable") from error

    if status != 200 or final_url != url:
        raise ObservationError("QBZD identity endpoint response is invalid")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ObservationError("QBZD identity endpoint response is too large")
    return decode_json_object(body)


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON constant {value}")


def decode_json_object(body: bytes) -> dict[str, object]:
    try:
        text = body.decode("utf-8", errors="strict")
        payload = json.loads(text, parse_constant=_reject_non_json_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ObservationError("QBZD identity endpoint JSON is invalid") from error
    if not isinstance(payload, dict):
        raise ObservationError("QBZD identity endpoint payload is invalid")
    return payload


def observe(
    reader: Callable[[str], dict[str, object]] = fetch_json,
) -> dict[str, object]:
    try:
        queue_before = classify_queue_payload(reader(QUEUE_URL))
        now_playing = classify_now_playing_payload(reader(NOW_PLAYING_URL))
        queue_after = classify_queue_payload(reader(QUEUE_URL))
        return classify_consistency(queue_before, now_playing, queue_after)
    except ObservationError as error:
        return {
            "schema_version": 1,
            "kind": "qbzd_playback_identity_observation",
            "read_only": True,
            "persistence_contract": "no-track-metadata-or-track-ids",
            "status": "unavailable",
            "reason": str(error),
            "queue_samples_equal": False,
            "sampled_identity_match": None,
            "authoritative_identity_proof": False,
            "playing": None,
            "queue_index": None,
            "queue_total_tracks": None,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = observe()
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
        )
    )
    return 1 if report["status"] == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
