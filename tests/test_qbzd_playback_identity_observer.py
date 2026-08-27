import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "qbzd_playback_identity_observer.py"
SPEC = importlib.util.spec_from_file_location("qbzd_playback_identity_observer", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def track(track_id: int, title: str = "secret title") -> dict[str, object]:
    return {
        "id": track_id,
        "title": title,
        "artist": "secret artist",
        "album": "secret album",
    }


def queue_payload(
    current_id: int | None = 87654321,
    *,
    current_index: int | None = 4,
    upcoming_ids=(87654322, 87654323),
    total_tracks: int = 7,
    history_len: int = 4,
) -> dict[str, object]:
    return {
        "current_track": track(current_id) if current_id is not None else None,
        "current_index": current_index,
        "upcoming": [track(value) for value in upcoming_ids],
        "history": [],
        "history_len": history_len,
        "shuffle": False,
        "repeat": "off",
        "total_tracks": total_tracks,
        "offset": 0,
        "limit": 64,
    }


def now_payload(
    track_id: int | None = 87654321,
    *,
    playback_id: int | None = None,
    playing: bool = True,
) -> dict[str, object]:
    if playback_id is None:
        playback_id = track_id or 0
    return {
        "playback": {
            "is_playing": playing,
            "position": 12,
            "duration": 240,
            "track_id": playback_id,
            "volume": 1.0,
            "queue_len": 7,
        },
        "track": track(track_id) if track_id is not None else None,
    }


class PlaybackIdentityObserverTests(unittest.TestCase):
    def test_consistent_snapshot_proves_only_local_identity_match(self):
        queue = MODULE.classify_queue_payload(queue_payload())
        now = MODULE.classify_now_playing_payload(now_payload())
        report = MODULE.classify_consistency(queue, now, queue)
        self.assertEqual(report["status"], "consistent")
        self.assertTrue(report["identity_match"])
        self.assertTrue(report["snapshot_consistent"])
        self.assertTrue(report["read_only"])

    def test_queue_playback_mismatch_is_detected_without_emitting_ids_or_metadata(self):
        before = MODULE.classify_queue_payload(queue_payload())
        now = MODULE.classify_now_playing_payload(now_payload(12345678))
        after = MODULE.classify_queue_payload(queue_payload())
        report = MODULE.classify_consistency(before, now, after)
        self.assertEqual(report["status"], "mismatch")
        self.assertEqual(report["reason"], "queue-playback-mismatch")
        self.assertFalse(report["identity_match"])
        encoded = json.dumps(report)
        for forbidden in (
            "87654321",
            "12345678",
            "secret title",
            "secret artist",
            "secret album",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_now_playing_internal_mismatch_is_detected(self):
        queue = MODULE.classify_queue_payload(queue_payload())
        now = MODULE.classify_now_playing_payload(
            now_payload(87654321, playback_id=12345678)
        )
        report = MODULE.classify_consistency(queue, now, queue)
        self.assertEqual(report["status"], "mismatch")
        self.assertEqual(report["reason"], "now-playing-internal-mismatch")

    def test_queue_change_around_now_playing_read_fails_closed(self):
        before = MODULE.classify_queue_payload(queue_payload())
        after = MODULE.classify_queue_payload(
            queue_payload(
                87654322,
                current_index=5,
                upcoming_ids=(87654323,),
                history_len=5,
            )
        )
        now = MODULE.classify_now_playing_payload(now_payload())
        report = MODULE.classify_consistency(before, now, after)
        self.assertEqual(report["status"], "snapshot-raced")
        self.assertIsNone(report["identity_match"])
        self.assertFalse(report["snapshot_consistent"])
        self.assertIsNone(report["queue_index"])

    def test_idle_queue_and_player_are_consistent(self):
        queue = MODULE.classify_queue_payload(
            queue_payload(
                None,
                current_index=None,
                upcoming_ids=(),
                total_tracks=0,
                history_len=0,
            )
        )
        now = MODULE.classify_now_playing_payload(
            now_payload(None, playback_id=0, playing=False)
        )
        report = MODULE.classify_consistency(queue, now, queue)
        self.assertEqual(report["status"], "idle")
        self.assertTrue(report["identity_match"])

    def test_observe_reads_queue_now_queue_in_that_order(self):
        calls = []
        queue = queue_payload()
        now = now_payload()

        def reader(url):
            calls.append(url)
            return queue if url == MODULE.QUEUE_URL else now

        report = MODULE.observe(reader)
        self.assertEqual(report["status"], "consistent")
        self.assertEqual(
            calls,
            [MODULE.QUEUE_URL, MODULE.NOW_PLAYING_URL, MODULE.QUEUE_URL],
        )

    def test_malformed_payloads_fail_closed(self):
        bad_queues = [
            {
                "current_track": {"id": True},
                "current_index": 0,
                "upcoming": [],
                "history_len": 0,
                "shuffle": False,
                "repeat": "off",
                "total_tracks": 1,
            },
            {
                "current_track": track(1),
                "current_index": None,
                "upcoming": [],
                "history_len": 0,
                "shuffle": False,
                "repeat": "off",
                "total_tracks": 1,
            },
            {
                "current_track": None,
                "current_index": 0,
                "upcoming": [],
                "history_len": 0,
                "shuffle": False,
                "repeat": "off",
                "total_tracks": 1,
            },
        ]
        for payload in bad_queues:
            with self.subTest(payload=payload):
                with self.assertRaises(MODULE.ObservationError):
                    MODULE.classify_queue_payload(payload)

        with self.assertRaises(MODULE.ObservationError):
            MODULE.classify_now_playing_payload(
                {
                    "playback": {"track_id": 0, "is_playing": True},
                    "track": None,
                }
            )

    def test_observation_error_never_echoes_payload(self):
        def reader(_url):
            return {"private": "very secret"}

        report = MODULE.observe(reader)
        self.assertEqual(report["status"], "unavailable")
        self.assertNotIn("very secret", json.dumps(report))

    def test_same_track_snapshot_does_not_claim_expected_queue_advance(self):
        # This is intentionally consistent. Detecting qbzd #699 requires temporal
        # transition evidence; a single snapshot must not invent an expected next track.
        queue = MODULE.classify_queue_payload(queue_payload())
        now = MODULE.classify_now_playing_payload(now_payload())
        report = MODULE.classify_consistency(queue, now, queue)
        self.assertEqual(report["status"], "consistent")
        self.assertNotIn("expected_next", report)


if __name__ == "__main__":
    unittest.main()
