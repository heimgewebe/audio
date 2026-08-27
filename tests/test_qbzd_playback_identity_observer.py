import http.client
import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock

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
    def test_equal_samples_report_only_diagnostic_match(self):
        queue = MODULE.classify_queue_payload(queue_payload())
        now = MODULE.classify_now_playing_payload(now_payload())
        report = MODULE.classify_consistency(queue, now, queue)
        self.assertEqual(report["status"], "sampled-match")
        self.assertTrue(report["sampled_identity_match"])
        self.assertTrue(report["queue_samples_equal"])
        self.assertFalse(report["authoritative_identity_proof"])
        self.assertEqual(report["reason"], "unversioned-api-aba-not-excluded")
        self.assertTrue(report["read_only"])

    def test_queue_playback_mismatch_is_detected_without_emitting_ids_or_metadata(self):
        before = MODULE.classify_queue_payload(queue_payload())
        now = MODULE.classify_now_playing_payload(now_payload(12345678))
        after = MODULE.classify_queue_payload(queue_payload())
        report = MODULE.classify_consistency(before, now, after)
        self.assertEqual(report["status"], "sampled-mismatch")
        self.assertEqual(report["reason"], "queue-playback-mismatch")
        self.assertFalse(report["sampled_identity_match"])
        self.assertFalse(report["authoritative_identity_proof"])
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
        self.assertEqual(report["status"], "sampled-mismatch")
        self.assertEqual(report["reason"], "now-playing-internal-mismatch")
        self.assertFalse(report["sampled_identity_match"])
        self.assertFalse(report["authoritative_identity_proof"])

    def test_queue_change_around_now_playing_read_reports_changed_window(self):
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
        self.assertEqual(report["status"], "sample-window-changed")
        self.assertEqual(
            report["reason"], "queue-samples-differ-around-now-playing-read"
        )
        self.assertIsNone(report["sampled_identity_match"])
        self.assertFalse(report["queue_samples_equal"])
        self.assertFalse(report["authoritative_identity_proof"])
        self.assertIsNone(report["queue_index"])

    def test_idle_queue_and_player_are_only_sampled_idle(self):
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
        self.assertEqual(report["status"], "sampled-idle")
        self.assertTrue(report["sampled_identity_match"])
        self.assertFalse(report["authoritative_identity_proof"])
        self.assertEqual(report["reason"], "unversioned-api-aba-not-excluded")

    def test_observe_reads_queue_now_queue_in_that_order(self):
        calls = []
        queue = queue_payload()
        now = now_payload()

        def reader(url):
            calls.append(url)
            return queue if url == MODULE.QUEUE_URL else now

        report = MODULE.observe(reader)
        self.assertEqual(report["status"], "sampled-match")
        self.assertFalse(report["authoritative_identity_proof"])
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
        self.assertFalse(report["authoritative_identity_proof"])
        self.assertNotIn("very secret", json.dumps(report))

    def test_same_track_snapshot_does_not_claim_expected_queue_advance(self):
        # Detecting qbzd #699 requires temporal transition evidence. Equal
        # unversioned samples can only be a diagnostic match, never a proof.
        queue = MODULE.classify_queue_payload(queue_payload())
        now = MODULE.classify_now_playing_payload(now_payload())
        report = MODULE.classify_consistency(queue, now, queue)
        self.assertEqual(report["status"], "sampled-match")
        self.assertFalse(report["authoritative_identity_proof"])
        self.assertEqual(report["reason"], "unversioned-api-aba-not-excluded")
        self.assertNotIn("expected_next", report)

    def test_aba_return_to_same_sample_is_never_called_authoritative(self):
        # A -> B -> A between the two queue reads is indistinguishable from no
        # transition because QBZD exposes no monotonic queue generation here.
        # The report must advertise that limitation even when both samples match.
        before = MODULE.classify_queue_payload(queue_payload())
        now = MODULE.classify_now_playing_payload(now_payload())
        after = MODULE.classify_queue_payload(queue_payload())
        report = MODULE.classify_consistency(before, now, after)
        self.assertTrue(report["queue_samples_equal"])
        self.assertTrue(report["sampled_identity_match"])
        self.assertFalse(report["authoritative_identity_proof"])
        self.assertEqual(report["status"], "sampled-match")
        self.assertEqual(report["reason"], "unversioned-api-aba-not-excluded")

    def test_strict_json_rejects_nonfinite_constants_and_huge_integer(self):
        for raw in (b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}'):
            with self.subTest(raw=raw):
                with self.assertRaises(MODULE.ObservationError):
                    MODULE.decode_json_object(raw)

        huge = b'{"value":' + (b"9" * 5000) + b"}"
        with self.assertRaises(MODULE.ObservationError):
            MODULE.decode_json_object(huge)

    def test_strict_json_rejects_invalid_utf8_and_non_object(self):
        with self.assertRaises(MODULE.ObservationError):
            MODULE.decode_json_object(b'{"x":"\xff"}')
        with self.assertRaises(MODULE.ObservationError):
            MODULE.decode_json_object(b"[]")

    def test_strict_json_normalizes_recursion_limit_failure(self):
        deep = (b"[" * 2000) + b"0" + (b"]" * 2000)
        with self.assertRaises(MODULE.ObservationError):
            MODULE.decode_json_object(deep)

    def test_http_framing_failure_is_normalized_to_observation_error(self):
        class BrokenResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def geturl(self):
                return MODULE.QUEUE_URL

            def read(self, _limit):
                raise http.client.IncompleteRead(b"partial", 10)

        opener = mock.Mock()
        opener.open.return_value = BrokenResponse()
        with mock.patch.object(MODULE.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(MODULE.ObservationError):
                MODULE.fetch_json(MODULE.QUEUE_URL)

    def test_fetch_json_rejects_any_url_outside_fixed_loopback_contract(self):
        with self.assertRaises(MODULE.ObservationError):
            MODULE.fetch_json("http://127.0.0.1:8182/api/status")
        with self.assertRaises(MODULE.ObservationError):
            MODULE.fetch_json("https://example.invalid/api/queue")


if __name__ == "__main__":
    unittest.main()
