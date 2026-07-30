import copy
import datetime as dt
import importlib.util
import json
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qobuz_rate_observer", ROOT / "scripts/qobuz_rate_observer.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class QobuzRateObserverTests(unittest.TestCase):
    def track_identity(self, track_id="123456"):
        return MODULE._track_identity(
            {
                "uri": f"qobuz:track:{track_id}",
                "name": "Bound test track",
                "album": {"name": "Bound test album"},
                "artists": [{"name": "Bound test artist"}],
                "length": 180_000,
            }
        )

    def playback(self, *, state="playing", position_ms=1_000, track=None, marker="a"):
        return {
            "state": state,
            "position_ms": position_ms,
            "track": track,
            "rpc": {
                "endpoint": "mopidy-loopback-json-rpc",
                "request_sha256": MODULE.LAB.canonical_value_sha256(
                    MODULE.LAB.QOBUZ_RPC_PAYLOAD
                ),
                "response_sha256": marker * 64,
                "response_bytes": 100,
            },
        }

    def query(self, argv, marker):
        return {
            "query_argv": list(argv),
            "query_argv_sha256": MODULE.LAB.canonical_value_sha256(list(argv)),
            "stdout_sha256": marker * 64,
            "stdout_total_bytes": 100,
            "stderr_sha256": "0" * 64,
            "stderr_total_bytes": 0,
            "complete": True,
        }

    def pulse(self, rate_hz=48_000):
        return {
            "default_sink": {
                "name": "alsa_output.usb-MOTU_M2.Direct_sink",
                "index": 6415,
                "rate_hz": rate_hz,
                "sample_specification_sha256": "2" * 64,
            },
            "mopidy_stream": {
                "index": 9001,
                "sink_index": 6415,
                "rate_hz": rate_hz,
                "sample_specification_sha256": "3" * 64,
                "application_name_sha256": "4" * 64,
                "application_binary_sha256": "5" * 64,
                "media_name_sha256": "6" * 64,
            },
            "blockers": [],
            "queries": {
                "info": self.query(MODULE.PACTL_INFO_ARGV, "7"),
                "sinks": self.query(MODULE.PACTL_SINKS_ARGV, "8"),
                "sink_inputs": self.query(MODULE.PACTL_INPUTS_ARGV, "9"),
            },
        }

    def truth(self, marker, rate_hz=48_000):
        return {
            "report_sha256": marker * 64,
            "truth_chain_sha256": "b" * 64,
            "graph_fingerprint": "c" * 64,
            "rate_hz": rate_hz,
            "quantum_frames": 1024,
        }

    def journal(self, wait_started, ended, track_id="123456", rate_hz=48_000):
        argv = MODULE.qobuz_journal_argv(
            wait_started.isoformat(), ended.isoformat()
        )
        message = (
            f"Valid track found: <DownloadableTrack {track_id}@FLAC "
            f"[24/{rate_hz / 1000:g}]>"
        )
        event = {
            "track_id": track_id,
            "extension": "FLAC",
            "bit_depth": 24,
            "rate_hz": rate_hz,
            "observed_at": (wait_started + dt.timedelta(seconds=30)).isoformat(),
            "message_sha256": MODULE._sha256_text(message),
        }
        return {
            "source": "mopidy-service-qobuz-downloadable-event",
            "query_argv": list(argv),
            "query_argv_sha256": MODULE.LAB.canonical_value_sha256(list(argv)),
            "returncode": 0,
            "stdout_sha256": "d" * 64,
            "stdout_total_bytes": 100,
            "stdout_truncated": False,
            "line_count": 1,
            "max_lines": MODULE.MAX_QOBUZ_JOURNAL_LINES,
            "matching_event_count": 1,
            "matching_events_sha256": MODULE.LAB.canonical_value_sha256([event]),
            "event": event,
            "complete": True,
        }

    def bound_evidence(self, *, stream_rate_hz=48_000):
        wait_started = dt.datetime(2026, 7, 30, 8, 0, tzinfo=dt.timezone.utc)
        observation_started = wait_started + dt.timedelta(seconds=60)
        ended = observation_started + dt.timedelta(seconds=60)
        identity = self.track_identity()
        initial = self.playback(state="stopped", position_ms=0, track=None, marker="a")
        selected = self.playback(track=identity, position_ms=1_000, marker="b")
        continued = self.playback(track=identity, position_ms=61_000, marker="c")
        pulse = self.pulse(stream_rate_hz)
        with (
            mock.patch.object(
                MODULE,
                "playback_snapshot",
                side_effect=[initial, selected, continued],
            ),
            mock.patch.object(
                MODULE,
                "truth_binding",
                side_effect=[self.truth("a"), self.truth("f")],
            ),
            mock.patch.object(
                MODULE,
                "pulse_snapshot",
                side_effect=[copy.deepcopy(pulse), copy.deepcopy(pulse)],
            ),
            mock.patch.object(
                MODULE,
                "journal_binding",
                return_value=(
                    self.journal(wait_started, ended),
                    [],
                ),
            ),
            mock.patch.object(
                MODULE,
                "utc_now",
                side_effect=[wait_started, observation_started, ended],
            ),
            mock.patch.object(
                MODULE,
                "monotonic_now",
                side_effect=[10.0, 10.0, 70.0, 70.0, 70.0, 130.0, 130.0],
            ),
            mock.patch.object(MODULE, "POLL_SECONDS", 60.0),
            mock.patch.object(MODULE, "sleep_for") as sleep,
        ):
            evidence = MODULE.qobuz_rate_observation(60, 60)
        self.assertEqual(sleep.call_count, 2)
        return evidence

    def test_bound_observation_passes_validator(self):
        evidence = self.bound_evidence()
        self.assertEqual(evidence["result"], "pass")
        self.assertEqual(evidence["blockers"], [])
        self.assertFalse(evidence["resampling_observed"])
        self.assertEqual(evidence["track_rate_hz"], 48_000)
        self.assertEqual(evidence["stream_rate_hz"], 48_000)
        MODULE.LAB.validate_evidence("qobuz-rate-proof", evidence)
        tampered = copy.deepcopy(evidence)
        tampered["implementation"]["system_truth_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "implementation binding changed"):
            MODULE.LAB.validate_evidence("qobuz-rate-proof", tampered)

    def test_rate_mismatch_returns_fail_receipt(self):
        evidence = self.bound_evidence(stream_rate_hz=44_100)
        self.assertEqual(evidence["result"], "fail")
        self.assertTrue(evidence["resampling_observed"])
        self.assertIn(
            "qobuz-track-stream-graph-endpoint-rate-mismatch",
            evidence["blockers"],
        )

    def test_no_new_track_returns_structured_fail_with_requested_duration(self):
        wait_started = dt.datetime(2026, 7, 30, 8, 0, tzinfo=dt.timezone.utc)
        ended = wait_started + dt.timedelta(seconds=1)
        stopped = self.playback(state="stopped", position_ms=0, track=None)
        with (
            mock.patch.object(MODULE, "playback_snapshot", return_value=stopped),
            mock.patch.object(MODULE, "utc_now", side_effect=[wait_started, ended]),
            mock.patch.object(
                MODULE, "monotonic_now", side_effect=[10.0, 10.0, 11.0]
            ),
        ):
            evidence = MODULE.qobuz_rate_observation(120, 0)
        self.assertEqual(evidence["result"], "fail")
        self.assertEqual(evidence["requested_duration_seconds"], 120)
        self.assertEqual(
            evidence["blockers"],
            ["new-playing-mopidy-qobuz-track-not-observed"],
        )

    def test_preexisting_playing_track_is_not_accepted_as_new(self):
        wait_started = dt.datetime(2026, 7, 30, 8, 0, tzinfo=dt.timezone.utc)
        ended = wait_started + dt.timedelta(seconds=1)
        identity = self.track_identity()
        baseline = self.playback(track=identity, position_ms=10_000, marker="a")
        continued = self.playback(track=identity, position_ms=11_000, marker="b")
        with (
            mock.patch.object(
                MODULE, "playback_snapshot", side_effect=[baseline, continued]
            ),
            mock.patch.object(MODULE, "utc_now", side_effect=[wait_started, ended]),
            mock.patch.object(
                MODULE,
                "monotonic_now",
                side_effect=[10.0, 10.0, 11.0, 11.0, 11.0],
            ),
            mock.patch.object(MODULE, "sleep_for") as sleep,
            mock.patch.object(MODULE, "truth_binding") as truth,
        ):
            evidence = MODULE.qobuz_rate_observation(60, 1)
        sleep.assert_called_once_with(1.0)
        truth.assert_not_called()
        self.assertEqual(evidence["result"], "fail")

    def test_rpc_contract_uses_getters_without_proxy_or_redirect(self):
        payload = MODULE._rpc_payload()
        self.assertEqual(payload, [dict(item) for item in MODULE.LAB.QOBUZ_RPC_PAYLOAD])
        self.assertTrue(
            all(
                item["method"].startswith("core.playback.get_")
                for item in payload
            )
        )
        opener = MODULE._local_rpc_opener()
        proxy_handlers = [
            handler
            for handler in opener.handlers
            if isinstance(handler, MODULE.urllib.request.ProxyHandler)
        ]
        self.assertEqual(proxy_handlers, [])
        self.assertTrue(
            any(
                isinstance(handler, MODULE._NoRedirectHandler)
                for handler in opener.handlers
            )
        )
        self.assertIsNone(
            MODULE._NoRedirectHandler().redirect_request(
                None,
                None,
                302,
                "redirect",
                {},
                "https://example.invalid/",
            )
        )

    def test_pulse_snapshot_selects_active_mopidy_stream(self):
        info = {"default_sink_name": "motu-sink"}
        sinks = [
            {
                "name": "motu-sink",
                "index": 5,
                "sample_specification": "s32le 2ch 48000Hz",
            }
        ]
        sink_inputs = [
            {
                "index": 9,
                "sink": 5,
                "corked": False,
                "sample_specification": "float32le 2ch 48000Hz",
                "properties": {
                    "application.name": "Mopidy",
                    "application.process.binary": "mopidy",
                    "media.name": "Music",
                },
            },
            {
                "index": 10,
                "sink": 5,
                "corked": False,
                "sample_specification": "float32le 2ch 96000Hz",
                "properties": {"application.name": "Firefox"},
            },
        ]
        with mock.patch.object(
            MODULE,
            "_json_command",
            side_effect=[
                (info, self.query(MODULE.PACTL_INFO_ARGV, "1")),
                (sinks, self.query(MODULE.PACTL_SINKS_ARGV, "2")),
                (sink_inputs, self.query(MODULE.PACTL_INPUTS_ARGV, "3")),
            ],
        ):
            snapshot = MODULE.pulse_snapshot()
        self.assertEqual(snapshot["blockers"], [])
        self.assertEqual(snapshot["default_sink"]["rate_hz"], 48_000)
        self.assertEqual(snapshot["mopidy_stream"]["index"], 9)
        self.assertEqual(snapshot["mopidy_stream"]["rate_hz"], 48_000)

    def test_legacy_receipt_is_readable_but_not_resolved(self):
        legacy = {
            "schema_version": 1,
            "kind": "qobuz_rate_observation",
            "gate": "qobuz-rate-proof",
            "result": "pass",
            "measured_at": "2026-07-30T08:00:00+00:00",
            "physical_state_sha256": None,
            "track_rate_hz": 48_000,
            "track_fingerprint": "a" * 64,
            "graph_rate_hz": 48_000,
            "endpoint_rate_hz": 48_000,
            "resampling_observed": False,
            "method": "manual legacy observation",
            "graph_fingerprint": "b" * 64,
        }
        MODULE.LAB.validate_evidence(
            "qobuz-rate-proof",
            legacy,
            allow_legacy_qobuz=True,
        )
        with self.assertRaisesRegex(ValueError, "legacy Qobuz"):
            MODULE.LAB.validate_evidence("qobuz-rate-proof", legacy)
        state = MODULE.LAB.empty_state()
        state["gates"]["qobuz-rate-proof"] = {
            "status": "passed",
            "recorded_at": "2026-07-30T08:00:00+00:00",
            "evidence_sha256": MODULE.LAB.canonical_sha256(legacy),
            "physical_state_sha256": None,
            "evidence": legacy,
        }
        resolved, invalidated = MODULE.LAB.gate_resolution(
            state, pathlib.Path("/missing-physical-state")
        )
        self.assertNotIn("qobuz-rate-proof", resolved)
        self.assertEqual(
            invalidated["qobuz-rate-proof"],
            "legacy-unbound-qobuz-evidence",
        )

    def test_downloadable_parser_binds_rate_format_and_exact_time(self):
        started = dt.datetime(2026, 7, 30, 8, 0, tzinfo=dt.timezone.utc)
        observed = started + dt.timedelta(seconds=1)
        ended = started + dt.timedelta(seconds=2)
        message = (
            "Valid track found: "
            "<DownloadableTrack 123456@FLAC [24/96.0]>"
        )
        record = json.dumps(
            {
                "__REALTIME_TIMESTAMP": str(int(observed.timestamp() * 1_000_000)),
                "MESSAGE": message,
            }
        )
        events = MODULE._downloadable_events(
            record,
            "123456",
            started.isoformat(),
            ended.isoformat(),
        )
        self.assertEqual(
            events,
            [
                {
                    "track_id": "123456",
                    "extension": "FLAC",
                    "bit_depth": 24,
                    "rate_hz": 96_000,
                    "observed_at": observed.isoformat(),
                    "message_sha256": MODULE._sha256_text(message),
                }
            ],
        )
        before = started - dt.timedelta(microseconds=1)
        old_record = json.dumps(
            {
                "__REALTIME_TIMESTAMP": str(int(before.timestamp() * 1_000_000)),
                "MESSAGE": message,
            }
        )
        self.assertEqual(
            MODULE._downloadable_events(
                old_record,
                "123456",
                started.isoformat(),
                ended.isoformat(),
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
