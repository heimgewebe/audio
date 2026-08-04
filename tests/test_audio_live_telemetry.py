import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load("audio_live_telemetry_under_test", "audio_live_telemetry.py")
SOAK = _load("audio_telemetry_soak_under_test", "audio_telemetry_soak.py")


class ManualClock:
    """Deterministic monotonic and wall clock pair."""

    def __init__(self, start=1000.0):
        self.value = start

    def advance(self, seconds):
        self.value += seconds

    def monotonic(self):
        return self.value

    def wall(self):
        return 1_700_000_000.0 + (self.value - 1000.0)


class CountingCollector(MODULE.Collector):
    name = "counting"
    stream_id = "counting"
    label = "Zähler"
    interval_seconds = 0.01

    def __init__(self, stream_id="counting"):
        self.stream_id = stream_id
        self.calls = 0
        self.resets = 0

    def reset(self):
        self.resets += 1

    def sample(self, context):
        self.calls += 1
        return {"tick": self.calls}


class RaisingCollector(MODULE.Collector):
    name = "raising"
    stream_id = "raising"
    label = "Absturz"
    interval_seconds = 0.01

    def __init__(self, exception=None):
        self.exception = exception or RuntimeError("collector exploded")
        self.calls = 0

    def sample(self, context):
        self.calls += 1
        raise self.exception


class NoneCollector(MODULE.Collector):
    name = "none"
    stream_id = "none-stream"
    label = "Ohne Beobachtung"
    interval_seconds = 0.01

    def sample(self, context):
        return None


class OversizedCollector(MODULE.Collector):
    name = "oversized"
    stream_id = "oversized"
    label = "Zu groß"
    interval_seconds = 0.01

    def sample(self, context):
        return {"blob": "x" * (MODULE.MAX_PAYLOAD_BYTES + 32)}


class UnserializableCollector(MODULE.Collector):
    name = "unserializable"
    stream_id = "unserializable"
    label = "Nicht serialisierbar"
    interval_seconds = 0.01

    def sample(self, context):
        return {"value": object()}


class StreamContractTests(unittest.TestCase):
    def test_sequence_is_monotonic_and_matches_published_total(self):
        clock = ManualClock()
        stream = MODULE.TelemetryStream("s", "S", capacity=4, stale_after_ms=1000)
        sequences = [
            stream.publish({"index": index}, monotonic_now=clock.value, wall_now=clock.wall())
            for index in range(10)
        ]
        self.assertEqual(sequences, list(range(1, 11)))
        self.assertEqual(stream.sequence, 10)
        self.assertEqual(stream.published_total, 10)
        self.assertTrue(all(b > a for a, b in zip(sequences, sequences[1:])))

    def test_errors_never_rewind_or_advance_the_sequence(self):
        clock = ManualClock()
        stream = MODULE.TelemetryStream("s", "S", capacity=4, stale_after_ms=1000)
        stream.publish({"a": 1}, monotonic_now=clock.value, wall_now=clock.wall())
        stream.fail("collector failed", wall_now=clock.wall())
        stream.fail("collector failed again", wall_now=clock.wall())
        projection = stream.snapshot(monotonic_now=clock.value, running=True)
        self.assertEqual(projection["sequence"], 1)
        self.assertEqual(projection["error_total"], 2)
        self.assertEqual(projection["consecutive_error_count"], 2)
        self.assertEqual(projection["error"], "collector failed again")
        stream.publish({"a": 2}, monotonic_now=clock.value, wall_now=clock.wall())
        recovered = stream.snapshot(monotonic_now=clock.value, running=True)
        self.assertEqual(recovered["sequence"], 2)
        self.assertIsNone(recovered["error"])
        self.assertEqual(recovered["consecutive_error_count"], 0)
        self.assertEqual(recovered["error_total"], 2)

    def test_buffer_is_bounded_and_drops_are_counted_explicitly(self):
        clock = ManualClock()
        stream = MODULE.TelemetryStream("s", "S", capacity=8, stale_after_ms=1000)
        for index in range(200):
            stream.publish({"index": index}, monotonic_now=clock.value, wall_now=clock.wall())
        projection = stream.snapshot(monotonic_now=clock.value, running=True)
        self.assertEqual(projection["buffer_depth"], 8)
        self.assertEqual(projection["buffer_capacity"], 8)
        self.assertEqual(projection["published_total"], 200)
        self.assertEqual(projection["dropped_total"], 192)
        self.assertEqual(len(stream.history()), 8)
        self.assertEqual(stream.history()[-1]["value"]["index"], 199)

    def test_freshness_moves_from_live_to_stale_without_new_samples(self):
        clock = ManualClock()
        stream = MODULE.TelemetryStream("s", "S", capacity=4, stale_after_ms=2000)
        stream.publish({"a": 1}, monotonic_now=clock.value, wall_now=clock.wall())
        self.assertEqual(
            stream.snapshot(monotonic_now=clock.value, running=True)["availability"],
            "live",
        )
        clock.advance(1.5)
        live = stream.snapshot(monotonic_now=clock.value, running=True)
        self.assertEqual(live["availability"], "live")
        self.assertEqual(live["age_ms"], 1500)
        clock.advance(1.0)
        stale = stream.snapshot(monotonic_now=clock.value, running=True)
        self.assertEqual(stale["availability"], "stale")
        self.assertEqual(stale["age_ms"], 2500)

    def test_availability_states_cover_starting_and_unavailable(self):
        clock = ManualClock()
        stream = MODULE.TelemetryStream("s", "S", capacity=4, stale_after_ms=1000)
        self.assertEqual(
            stream.snapshot(monotonic_now=clock.value, running=True)["availability"],
            "starting",
        )
        stream.fail("nothing to read", wall_now=clock.wall())
        self.assertEqual(
            stream.snapshot(monotonic_now=clock.value, running=True)["availability"],
            "unavailable",
        )
        stream.publish({"a": 1}, monotonic_now=clock.value, wall_now=clock.wall())
        self.assertEqual(
            stream.snapshot(monotonic_now=clock.value, running=False)["availability"],
            "stale",
        )

    def test_oversized_payload_is_rejected_without_touching_the_sequence(self):
        clock = ManualClock()
        stream = MODULE.TelemetryStream("s", "S", capacity=4, stale_after_ms=1000)
        with self.assertRaises(MODULE.TelemetryError):
            stream.publish(
                {"blob": "x" * (MODULE.MAX_PAYLOAD_BYTES + 1)},
                monotonic_now=clock.value,
                wall_now=clock.wall(),
            )
        projection = stream.snapshot(monotonic_now=clock.value, running=True)
        self.assertEqual(projection["sequence"], 0)
        self.assertEqual(projection["rejected_total"], 1)
        self.assertEqual(projection["error_total"], 1)

    def test_stream_construction_rejects_contract_violations(self):
        for capacity in (0, MODULE.MAX_STREAM_CAPACITY + 1):
            with self.assertRaises(MODULE.TelemetryError):
                MODULE.TelemetryStream("s", "S", capacity=capacity, stale_after_ms=1000)
        with self.assertRaises(MODULE.TelemetryError):
            MODULE.TelemetryStream("s", "S", capacity=4, stale_after_ms=10)


class ControlChannelTests(unittest.TestCase):
    def test_commands_are_never_dropped_and_rejection_is_explicit(self):
        channel = MODULE.ControlChannel(capacity=3)
        for index in range(3):
            channel.submit("transition", {"index": index})
        with self.assertRaises(MODULE.ControlChannelFull):
            channel.submit("transition", {"index": 3})
        projection = channel.snapshot()
        self.assertEqual(projection["depth"], 3)
        self.assertEqual(projection["accepted_total"], 3)
        self.assertEqual(projection["rejected_total"], 1)
        self.assertEqual(projection["dropped_total"], 0)
        self.assertTrue(projection["lossless"])
        self.assertFalse(projection["shares_telemetry_queue"])
        drained = channel.drain()
        self.assertEqual([item["command"]["detail"]["index"] for item in drained], [0, 1, 2])
        self.assertEqual(channel.snapshot()["depth"], 0)

    def test_telemetry_overflow_never_touches_the_command_channel(self):
        hub = MODULE.TelemetryHub([CountingCollector()], stream_capacity=4)
        hub.start(threads=False)
        hub.submit_command("service-start", {"port": 8765})
        for _index in range(200):
            hub.pump()
        snapshot = hub.snapshot()
        hub.stop()
        stream = snapshot["streams"][0]
        self.assertGreater(stream["dropped_total"], 0)
        self.assertEqual(snapshot["control_channel"]["dropped_total"], 0)
        self.assertEqual(snapshot["control_channel"]["accepted_total"], 1)
        self.assertEqual(snapshot["control_channel"]["depth"], 1)

    def test_channel_rejects_invalid_kinds_and_capacities(self):
        with self.assertRaises(MODULE.TelemetryError):
            MODULE.ControlChannel(capacity=0)
        with self.assertRaises(MODULE.TelemetryError):
            MODULE.ControlChannel(capacity=MODULE.MAX_CONTROL_CAPACITY + 1)
        channel = MODULE.ControlChannel(capacity=2)
        with self.assertRaises(MODULE.TelemetryError):
            channel.submit("  ")
        with self.assertRaises(MODULE.TelemetryError):
            channel.submit("transition", object())
        self.assertEqual(channel.snapshot()["accepted_total"], 0)


class CollectorIsolationTests(unittest.TestCase):
    def test_a_raising_collector_only_degrades_its_own_stream(self):
        healthy = CountingCollector()
        hub = MODULE.TelemetryHub([healthy, RaisingCollector()], stream_capacity=4)
        hub.start(threads=False)
        for _index in range(5):
            hub.pump()
        snapshot = hub.snapshot()
        hub.stop()
        streams = {stream["id"]: stream for stream in snapshot["streams"]}
        self.assertEqual(streams["counting"]["sequence"], 5)
        self.assertEqual(streams["counting"]["availability"], "live")
        self.assertEqual(streams["raising"]["sequence"], 0)
        self.assertEqual(streams["raising"]["availability"], "unavailable")
        self.assertEqual(streams["raising"]["error_total"], 5)
        self.assertIn("RuntimeError", streams["raising"]["error"])

    def test_base_exceptions_from_a_collector_do_not_escape_the_hub(self):
        hub = MODULE.TelemetryHub(
            [CountingCollector(), RaisingCollector(exception=MODULE.TelemetryError("gone"))],
            stream_capacity=4,
        )
        hub.start(threads=False)
        results = hub.pump()
        hub.stop()
        self.assertTrue(results["counting"])
        self.assertFalse(results["raising"])

    def test_none_and_malformed_observations_become_stream_errors(self):
        hub = MODULE.TelemetryHub(
            [NoneCollector(), OversizedCollector(), UnserializableCollector()],
            stream_capacity=4,
        )
        hub.start(threads=False)
        hub.pump()
        snapshot = hub.snapshot()
        hub.stop()
        for stream in snapshot["streams"]:
            with self.subTest(stream=stream["id"]):
                self.assertEqual(stream["sequence"], 0)
                self.assertEqual(stream["availability"], "unavailable")
                self.assertTrue(stream["error"])

    def test_threaded_collectors_publish_and_stop_deterministically(self):
        healthy = CountingCollector()
        hub = MODULE.TelemetryHub([healthy, RaisingCollector()], stream_capacity=8)
        started = hub.start()
        self.assertEqual(started["state"], "running")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if hub.stream("counting").latest() is not None:
                break
            time.sleep(0.01)
        stop_report = hub.stop()
        self.assertEqual(stop_report["state"], "stopped")
        self.assertEqual(stop_report["timed_out"], 0)
        self.assertEqual(stop_report["joined"], 2)
        self.assertFalse(hub.running)
        alive = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("audio-telemetry-")
        ]
        self.assertEqual(alive, [])
        self.assertIsNotNone(hub.stream("counting").latest())
        self.assertEqual(hub.stream("raising").snapshot(
            monotonic_now=time.monotonic(), running=False
        )["sequence"], 0)

    def test_stop_and_start_are_idempotent_and_restartable(self):
        collector = CountingCollector()
        hub = MODULE.TelemetryHub([collector], stream_capacity=4)
        self.assertEqual(hub.stop()["state"], "already-stopped")
        hub.start(threads=False)
        self.assertEqual(hub.start(threads=False)["state"], "already-running")
        hub.pump()
        first = hub.stop()
        self.assertEqual(first["state"], "stopped")
        self.assertEqual(hub.stop()["state"], "already-stopped")
        hub.start(threads=False)
        hub.pump()
        hub.stop()
        self.assertEqual(collector.calls, 2)
        self.assertEqual(collector.resets, 2)
        self.assertEqual(hub.stream("counting").sequence, 2)

    def test_snapshot_is_stable_while_collectors_publish_concurrently(self):
        hub = MODULE.TelemetryHub([CountingCollector()], stream_capacity=16)
        hub.start(threads=False)
        errors = []
        stop = threading.Event()

        def writer():
            try:
                while not stop.is_set():
                    hub.pump()
            except Exception as error:  # pragma: no cover - defensive
                errors.append(error)

        threads = [threading.Thread(target=writer) for _index in range(4)]
        for thread in threads:
            thread.start()
        try:
            sequences = [hub.snapshot()["streams"][0]["sequence"] for _index in range(200)]
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=3)
        hub.stop()
        self.assertEqual(errors, [])
        self.assertEqual(sequences, sorted(sequences))
        self.assertGreater(sequences[-1], 0)

    def test_hub_construction_rejects_duplicate_or_empty_stream_sets(self):
        with self.assertRaises(MODULE.TelemetryError):
            MODULE.TelemetryHub([])
        with self.assertRaises(MODULE.TelemetryError):
            MODULE.TelemetryHub([CountingCollector(), CountingCollector()])
        hub = MODULE.TelemetryHub([CountingCollector()])
        with self.assertRaises(MODULE.TelemetryError):
            hub.stream("missing")
        with self.assertRaises(MODULE.TelemetryError):
            hub.pump("missing")


class SafetyBoundaryTests(unittest.TestCase):
    def test_only_passive_commands_are_allowlisted(self):
        for command in MODULE.PASSIVE_COMMANDS:
            self.assertEqual(MODULE.assert_passive_argv(command), tuple(command))
        for rejected in (
            ("wpctl", "set-default", "42"),
            ("pw-cli", "set-param", "1"),
            ("pw-link", "a", "b"),
            ("aseqdump", "-p", "24:0"),
            ("pactl", "set-sink-volume", "0", "50%"),
            ("pw-dump", "--monitor"),
            (),
            ("",),
        ):
            with self.subTest(command=rejected):
                with self.assertRaises(MODULE.TelemetryError):
                    MODULE.assert_passive_argv(rejected)

    def test_safety_boundary_declares_no_mutation(self):
        safety = MODULE.safety_boundary()
        self.assertEqual(safety["mode"], "passive-observation")
        self.assertTrue(safety["identifies_nodes_and_links"])
        self.assertTrue(safety["reversible"])
        self.assertEqual(safety["observer_id"], MODULE.OBSERVER_ID)
        self.assertEqual(safety["owned_nodes"], [])
        self.assertEqual(safety["owned_links"], [])
        self.assertEqual(safety["rollback"]["scope"], "observer-owned-resources-only")
        self.assertTrue(safety["rollback"]["requires_identity_match"])
        self.assertGreater(
            safety["stop_timeout_seconds"],
            safety["maximum_passive_command_seconds"]
            + MODULE.PROCESS_KILL_GRACE_SECONDS,
        )
        for key in (
            "modifies_defaults",
            "modifies_routes",
            "modifies_profiles",
            "modifies_volumes",
            "modifies_links",
            "opens_audio_streams",
            "subscribes_midi_ports",
            "uses_shell",
        ):
            with self.subTest(key=key):
                self.assertFalse(safety[key])
        self.assertEqual(safety["allowed_commands"], ["pw-dump", "pw-top -b -n 1"])

    def test_module_source_contains_no_shell_execution(self):
        source = (ROOT / "scripts" / "audio_live_telemetry.py").read_text()
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)
        soak_source = (ROOT / "scripts" / "audio_telemetry_soak.py").read_text()
        self.assertNotIn("shell=True", soak_source)
        self.assertNotIn("os.system", soak_source)

    def test_contract_report_is_static_and_complete(self):
        report = MODULE.contract_report()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(set(report["streams"]), set(MODULE.STREAM_IDS))
        self.assertEqual(report["safety"]["mode"], "passive-observation")
        self.assertEqual(report["max_payload_bytes"], MODULE.MAX_PAYLOAD_BYTES)


class RealCollectorTests(unittest.TestCase):
    def test_pipewire_graph_collector_parses_a_bounded_dump(self):
        dump = json.dumps(
            [
                {
                    "id": 40,
                    "type": "PipeWire:Interface:Node",
                    "info": {
                        "state": "running",
                        "props": {"node.name": "motu", "media.class": "Audio/Sink"},
                    },
                },
                {
                    "id": 41,
                    "type": "PipeWire:Interface:Node",
                    "info": {"state": "idle", "props": {"node.name": "roland"}},
                },
                {
                    "id": 60,
                    "type": "PipeWire:Interface:Link",
                    "info": {"output-node-id": 40, "input-node-id": 41, "state": "active"},
                },
                {"id": 12, "type": "PipeWire:Interface:Device", "info": {}},
                "not-an-object",
            ]
        )
        collector = MODULE.PipeWireGraphCollector(runner=lambda argv, **kwargs: dump)
        value = collector.sample(None)
        self.assertEqual(value["node_count"], 2)
        self.assertEqual(value["link_count"], 1)
        self.assertEqual(value["device_count"], 1)
        self.assertEqual(value["running_node_count"], 1)
        self.assertEqual(value["observed_nodes"][0]["name"], "motu")
        self.assertEqual(value["event"], "baseline")
        self.assertEqual(value["event_sequence"], 0)
        self.assertEqual(value["observer_id"], MODULE.OBSERVER_ID)
        self.assertFalse(value["modified"])

    def test_pipewire_graph_events_are_monotone_and_content_bound(self):
        baseline = json.dumps(
            [
                {
                    "id": 1,
                    "type": "PipeWire:Interface:Node",
                    "info": {"state": "running", "props": {"node.name": "motu"}},
                }
            ]
        )
        changed = json.dumps(
            [
                {
                    "id": 1,
                    "type": "PipeWire:Interface:Node",
                    "info": {"state": "running", "props": {"node.name": "motu"}},
                },
                {
                    "id": 2,
                    "type": "PipeWire:Interface:Node",
                    "info": {"state": "idle", "props": {"node.name": "roland"}},
                },
            ]
        )
        outputs = iter([baseline, baseline, changed])
        collector = MODULE.PipeWireGraphCollector(
            runner=lambda argv, **kwargs: next(outputs)
        )
        first = collector.sample(None)
        second = collector.sample(None)
        third = collector.sample(None)
        self.assertEqual(
            [first["event"], second["event"], third["event"]],
            ["baseline", "none", "changed"],
        )
        self.assertEqual(
            [first["event_sequence"], second["event_sequence"], third["event_sequence"]],
            [0, 0, 1],
        )
        self.assertEqual(first["graph_sha256"], second["graph_sha256"])
        self.assertNotEqual(second["graph_sha256"], third["graph_sha256"])
        collector.reset()
        self.assertEqual(collector._event_sequence, 0)
        self.assertIsNone(collector._previous_graph_sha256)

    def test_malformed_or_missing_pipewire_output_becomes_a_stream_error(self):
        cases = {
            "not json": lambda argv, **kwargs: "<html>nope</html>",
            "not a list": lambda argv, **kwargs: '{"id": 1}',
            "empty graph": lambda argv, **kwargs: "[]",
            "absent tool": lambda argv, **kwargs: (_ for _ in ()).throw(
                MODULE.TelemetryError("program is unavailable: pw-dump")
            ),
        }
        for label, runner in cases.items():
            with self.subTest(case=label):
                hub = MODULE.TelemetryHub(
                    [MODULE.PipeWireGraphCollector(runner=runner)], stream_capacity=4
                )
                hub.start(threads=False)
                self.assertEqual(hub.pump(), {MODULE.STREAM_DEVICE_GRAPH: False})
                snapshot = hub.snapshot()
                hub.stop()
                stream = snapshot["streams"][0]
                self.assertEqual(stream["availability"], "unavailable")
                self.assertTrue(stream["error"])

    def test_transport_is_derived_from_the_observed_graph_only(self):
        graph = MODULE.PipeWireGraphCollector(
            runner=lambda argv, **kwargs: json.dumps(
                [
                    {
                        "id": 1,
                        "type": "PipeWire:Interface:Node",
                        "info": {"state": "running", "props": {"node.name": "a"}},
                    }
                ]
            )
        )
        hub = MODULE.TelemetryHub([graph, MODULE.TransportCollector()], stream_capacity=4)
        hub.start(threads=False)
        hub.pump()
        snapshot = hub.snapshot()
        hub.stop()
        transport = snapshot["streams"][1]
        self.assertEqual(transport["value"]["state"], "running")
        self.assertEqual(transport["value"]["derived_from"], MODULE.STREAM_DEVICE_GRAPH)

    def test_transport_without_a_graph_observation_reports_an_error(self):
        hub = MODULE.TelemetryHub([MODULE.TransportCollector()], stream_capacity=4)
        hub.start(threads=False)
        hub.pump()
        snapshot = hub.snapshot()
        hub.stop()
        self.assertEqual(snapshot["streams"][0]["availability"], "unavailable")
        self.assertIn("graph", snapshot["streams"][0]["error"])

    def test_xrun_parser_sums_the_error_column_and_tracks_deltas(self):
        first = (
            "S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME\n"
            "R   40   1024  48000  1.1us   2.2us  0.00  0.00    3    S32LE 2 48000 motu\n"
            "S   41      0      0  0.0us   0.0us  0.00  0.00    1        - roland\n"
        )
        second = first.replace("    3    S32LE", "    9    S32LE")
        outputs = iter([first, second, "no header here"])
        collector = MODULE.XrunCollector(runner=lambda argv, **kwargs: next(outputs))
        initial = collector.sample(None)
        self.assertEqual(initial["total"], 4)
        self.assertEqual(initial["delta"], 0)
        second_sample = collector.sample(None)
        self.assertEqual(second_sample["total"], 10)
        self.assertEqual(second_sample["delta"], 6)
        with self.assertRaises(MODULE.TelemetryError):
            collector.sample(None)

    def test_xrun_counter_reset_never_produces_a_negative_delta(self):
        outputs = iter(
            [
                "S ID ERR NAME\nR 1 7 motu\n",
                "S ID ERR NAME\nR 1 2 motu\n",
            ]
        )
        collector = MODULE.XrunCollector(runner=lambda argv, **kwargs: next(outputs))
        collector.sample(None)
        after_reset = collector.sample(None)
        self.assertEqual(after_reset["total"], 2)
        self.assertEqual(after_reset["delta"], 0)
        self.assertEqual(after_reset["counter_reset_count"], 1)

    def test_cpu_collector_reads_proc_and_rejects_malformed_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            loadavg = root / "loadavg"
            stat = root / "stat"
            loadavg.write_text("0.50 0.40 0.30 1/900 12345\n")
            # /proc/<pid>/stat: utime and stime are the 14th and 15th fields.
            fields = ["0"] * 9 + ["40", "20"] + ["0"] * 20
            stat.write_text("12 (audio control) S " + " ".join(fields) + "\n")
            collector = MODULE.CpuLoadCollector(loadavg_path=loadavg, self_stat_path=stat)
            context = MODULE.CollectorContext(None, 100.0, 1_700_000_000.0)
            value = collector.sample(context)
            self.assertEqual(value["load_1m"], 0.50)
            self.assertIsNone(value["service_cpu_percent"])
            later = MODULE.CollectorContext(None, 101.0, 1_700_000_001.0)
            second = collector.sample(later)
            self.assertEqual(second["service_cpu_percent"], 0.0)
            loadavg.write_text("broken\n")
            with self.assertRaises(MODULE.TelemetryError):
                collector.sample(later)
            collector = MODULE.CpuLoadCollector(
                loadavg_path=root / "absent", self_stat_path=stat
            )
            with self.assertRaises(MODULE.TelemetryError):
                collector.sample(context)

    def test_midi_collector_parses_sequencer_clients_without_subscribing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            clients = root / "clients"
            clients.write_text(
                'Client   0 : "System" [Kernel]\n'
                "  Port   0 : \"Timer\" (Rwe-)\n"
                "  Port   1 : \"Announce\" (R-e-)\n"
                'Client  24 : "FP-30X" [Kernel Card=1]\n'
                "  Port   0 : \"FP-30X MIDI 1\" (RWeX)\n"
            )
            collector = MODULE.MidiActivityCollector(
                clients_path=clients, asound_root=root / "asound"
            )
            value = collector.sample(None)
            self.assertEqual(value["client_count"], 2)
            self.assertEqual(value["port_count"], 3)
            self.assertEqual(value["clients"][1]["name"], "FP-30X")
            self.assertFalse(value["subscribed"])
            self.assertIsNone(value["rawmidi_bytes_total"])
            clients.write_text("no clients here\n")
            with self.assertRaises(MODULE.TelemetryError):
                collector.sample(None)

    def test_level_stream_is_unavailable_without_a_configured_passive_source(self):
        collector = MODULE.LevelSourceCollector(source_path=None)
        original = MODULE.os.environ.pop(MODULE.LEVEL_SOURCE_ENVIRONMENT, None)
        try:
            with self.assertRaises(MODULE.TelemetryError) as caught:
                collector.sample(None)
            self.assertIn("no passive level source", str(caught.exception))
        finally:
            if original is not None:
                MODULE.os.environ[MODULE.LEVEL_SOURCE_ENVIRONMENT] = original

    def test_level_source_is_validated_before_it_becomes_a_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "levels.json"
            collector = MODULE.LevelSourceCollector(source_path=path)
            path.write_text(json.dumps({"peak_dbfs": -6.0, "rms_dbfs": -12.0, "channel": "L"}))
            value = collector.sample(None)
            self.assertEqual(value["peak_dbfs"], -6.0)
            self.assertEqual(value["channel"], "L")
            self.assertFalse(value["clipping"])
            for payload in (
                "not json",
                json.dumps([1, 2]),
                json.dumps({"peak_dbfs": "loud", "rms_dbfs": -12.0}),
                json.dumps({"peak_dbfs": 12.0, "rms_dbfs": -12.0}),
                json.dumps({"peak_dbfs": -12.0, "rms_dbfs": -6.0}),
            ):
                with self.subTest(payload=payload[:40]):
                    path.write_text(payload)
                    with self.assertRaises(MODULE.TelemetryError):
                        collector.sample(None)

    def test_bounded_reader_rejects_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "big"
            path.write_text("x" * 4096)
            self.assertEqual(
                len(MODULE.read_bounded_text(path, label="big", maximum_bytes=8192)), 4096
            )
            with self.assertRaises(MODULE.TelemetryError):
                MODULE.read_bounded_text(path, label="big", maximum_bytes=1024)
            with self.assertRaises(MODULE.TelemetryError):
                MODULE.read_bounded_text(
                    pathlib.Path(directory) / "absent", label="absent"
                )

    def test_default_hub_covers_the_contracted_streams_in_graph_first_order(self):
        hub = MODULE.build_default_hub()
        self.assertEqual(set(hub.stream_ids), set(MODULE.STREAM_IDS))
        self.assertEqual(hub.stream_ids[0], MODULE.STREAM_DEVICE_GRAPH)
        self.assertLess(
            hub.stream_ids.index(MODULE.STREAM_DEVICE_GRAPH),
            hub.stream_ids.index(MODULE.STREAM_TRANSPORT),
        )


class SnapshotContractTests(unittest.TestCase):
    def test_snapshot_carries_the_full_per_stream_contract(self):
        hub = MODULE.TelemetryHub(
            [CountingCollector(), RaisingCollector()], stream_capacity=4
        )
        hub.start(threads=False)
        hub.pump()
        snapshot = hub.snapshot()
        hub.stop()
        self.assertEqual(snapshot["kind"], MODULE.SNAPSHOT_KIND)
        self.assertEqual(snapshot["authority"], "passive-observation")
        self.assertTrue(snapshot["read_only"])
        self.assertFalse(snapshot["authoritative"])
        self.assertTrue(snapshot["running"])
        for stream in snapshot["streams"]:
            with self.subTest(stream=stream["id"]):
                for key in (
                    "id",
                    "availability",
                    "sequence",
                    "published_total",
                    "dropped_total",
                    "buffer_capacity",
                    "buffer_depth",
                    "stale_after_ms",
                    "age_ms",
                    "error",
                    "error_total",
                    "collector",
                    "value",
                ):
                    self.assertIn(key, stream)
                self.assertTrue(stream["lossy"])
        self.assertEqual(snapshot["summary"]["stream_count"], 2)
        self.assertEqual(snapshot["summary"]["live_count"], 1)
        self.assertEqual(snapshot["summary"]["unavailable_count"], 1)
        self.assertEqual(snapshot["summary"]["error_stream_count"], 1)

    def test_snapshot_is_json_serialisable_and_bounded(self):
        hub = MODULE.TelemetryHub([CountingCollector()], stream_capacity=4)
        hub.start(threads=False)
        for _index in range(20):
            hub.pump()
        payload = json.dumps(hub.snapshot(), ensure_ascii=False, allow_nan=False)
        hub.stop()
        self.assertLess(len(payload.encode("utf-8")), 262_144)

    def test_cli_check_and_safety_do_not_touch_the_system(self):
        for command in ("check", "safety"):
            with self.subTest(command=command):
                stream = io.StringIO()
                with contextlib.redirect_stdout(stream):
                    self.assertEqual(MODULE.main([command]), 0)
                report = json.loads(stream.getvalue())
                self.assertEqual(report["safety"]["mode"], "passive-observation")


class SoakHarnessTests(unittest.TestCase):
    def report(self, argv):
        args = SOAK.build_parser().parse_args(argv)
        return SOAK.run(args)

    def test_synthetic_soak_proves_bounds_and_isolation_quickly(self):
        started = time.monotonic()
        report = self.report(["--mode", "synthetic", "--iterations", "120"])
        self.assertLess(time.monotonic() - started, 20.0)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["failed_checks"], [])
        self.assertEqual(report["evidence_class"], "synthetic-accelerated")
        self.assertFalse(report["live_proof"])
        self.assertEqual(report["iterations"], 120)
        by_id = {item["id"]: item for item in report["checks"]}
        for identifier in (
            "queue-depth-bounded",
            "drop-accounting-consistent",
            "control-channel-lossless",
            "control-channel-separate",
            "collector-isolation",
            "malformed-payload-rejected",
            "shutdown-deterministic",
            "threaded-collector-isolation",
            "memory-growth-bounded",
        ):
            with self.subTest(check=identifier):
                self.assertEqual(by_id[identifier]["status"], "pass")
        self.assertLessEqual(
            report["queue_bounds"]["max_buffer_depth"],
            report["queue_bounds"]["stream_capacity"],
        )
        self.assertGreater(report["queue_bounds"]["dropped_total"], 0)
        self.assertEqual(report["control_channel"]["dropped_total"], 0)
        malformed = next(
            stream for stream in report["streams"] if stream["id"] == SOAK.SOAK_MALFORMED_STREAM
        )
        self.assertEqual(malformed["sequence"], 0)
        self.assertGreater(malformed["rejected_total"], 0)
        self.assertGreater(malformed["error_total"], 0)

    def test_memory_sample_retention_is_hard_bounded_and_preserves_endpoints(self):
        samples = []
        for index in range(10_000):
            SOAK.append_bounded_sample(samples, (float(index), index), limit=16)
        self.assertLessEqual(len(samples), 16)
        self.assertEqual(samples[0], (0.0, 0))
        self.assertEqual(samples[-1], (9999.0, 9999))
        projection = SOAK.memory_projection(samples, 10_000.0)
        self.assertEqual(
            projection["retention"]["in_memory_limit"], SOAK.MAX_MEMORY_SAMPLES
        )
        self.assertLessEqual(len(projection["samples"]), SOAK.MAX_REPORT_SAMPLES)

    def test_short_synthetic_runs_refuse_to_extrapolate_a_memory_trend(self):
        report = self.report(["--mode", "synthetic", "--iterations", "20"])
        memory = report["memory"]
        if memory["available"]:
            self.assertIsNone(memory["growth_per_hour_kib"])
            self.assertEqual(memory["trend"], "not-extrapolated")
            self.assertLessEqual(len(memory["samples"]), SOAK.MAX_REPORT_SAMPLES)

    def test_synthetic_mode_never_claims_live_or_hardware_xrun_evidence(self):
        report = self.report(["--mode", "synthetic", "--iterations", "40"])
        self.assertFalse(report["xruns"]["live_counter"])
        self.assertEqual(report["xruns"]["authority"], "synthetic")
        self.assertIn("synthetic", report["live_proof_reason"])

    def test_live_mode_reports_unavailable_tools_without_claiming_proof(self):
        def failing_hub(**kwargs):
            return MODULE.TelemetryHub(
                [RaisingCollector(), MODULE.TransportCollector()], **kwargs
            )

        original = SOAK.TELEMETRY.build_default_hub
        SOAK.TELEMETRY.build_default_hub = failing_hub
        try:
            report = self.report(
                [
                    "--mode",
                    "live",
                    "--duration-seconds",
                    "0.3",
                    "--sample-interval-seconds",
                    "0.05",
                ]
            )
        finally:
            SOAK.TELEMETRY.build_default_hub = original
        self.assertEqual(report["evidence_class"], "live-observed")
        self.assertFalse(report["live_proof"])
        self.assertIn("proves nothing", report["live_proof_reason"])
        self.assertFalse(report["xruns"]["available"])
        self.assertIsNone(report["xruns"]["delta"])
        by_id = {item["id"]: item for item in report["checks"]}
        self.assertEqual(by_id["xrun-delta"]["status"], "skipped")
        self.assertEqual(by_id["some-stream-observed"]["status"], "fail")
        self.assertEqual(by_id["service-survived"]["status"], "pass")
        self.assertEqual(by_id["snapshot-load-exercised"]["status"], "pass")
        self.assertEqual(report["snapshot_reads"], report["observations"])
        self.assertEqual(report["status"], "fail")

    def test_live_mode_reports_xrun_deltas_when_a_counter_is_readable(self):
        state = {"count": 0}

        def runner(argv, **kwargs):
            state["count"] += 1
            return f"S ID ERR NAME\nR 1 {state['count']} motu\n"

        def xrun_hub(**kwargs):
            collector = MODULE.XrunCollector(runner=runner)
            collector.interval_seconds = 0.05
            return MODULE.TelemetryHub([collector], **kwargs)

        original = SOAK.TELEMETRY.build_default_hub
        SOAK.TELEMETRY.build_default_hub = xrun_hub
        try:
            report = self.report(
                [
                    "--mode",
                    "live",
                    "--duration-seconds",
                    "0.6",
                    "--sample-interval-seconds",
                    "0.05",
                    "--load-factor",
                    "3",
                ]
            )
        finally:
            SOAK.TELEMETRY.build_default_hub = original
        xruns = report["xruns"]
        self.assertTrue(xruns["available"])
        self.assertTrue(xruns["live_counter"])
        self.assertGreaterEqual(xruns["start_total"], 1)
        self.assertGreater(xruns["end_total"], xruns["start_total"])
        self.assertEqual(xruns["delta"], xruns["end_total"] - xruns["start_total"])
        self.assertFalse(report["live_proof"])
        self.assertIn("blocks clean evidence", report["live_proof_reason"])
        self.assertEqual(report["load_factor"], 3)
        self.assertEqual(report["snapshot_reads"], report["observations"] * 3)
        by_id = {item["id"]: item for item in report["checks"]}
        self.assertEqual(by_id["snapshot-load-exercised"]["status"], "pass")
        self.assertEqual(by_id["xrun-delta"]["status"], "fail")
        self.assertEqual(report["status"], "fail")

    def test_live_mode_accepts_a_readable_zero_xrun_delta(self):
        def runner(argv, **kwargs):
            return "S ID ERR NAME\nR 1 7 motu\n"

        def xrun_hub(**kwargs):
            collector = MODULE.XrunCollector(runner=runner)
            collector.interval_seconds = 0.05
            return MODULE.TelemetryHub([collector], **kwargs)

        original = SOAK.TELEMETRY.build_default_hub
        SOAK.TELEMETRY.build_default_hub = xrun_hub
        try:
            report = self.report(
                [
                    "--mode",
                    "live",
                    "--duration-seconds",
                    "0.4",
                    "--sample-interval-seconds",
                    "0.05",
                    "--load-factor",
                    "2",
                ]
            )
        finally:
            SOAK.TELEMETRY.build_default_hub = original
        self.assertEqual(report["xruns"]["delta"], 0)
        self.assertTrue(report["live_proof"])
        self.assertEqual(report["status"], "pass")
        by_id = {item["id"]: item for item in report["checks"]}
        self.assertEqual(by_id["xrun-delta"]["status"], "pass")
        self.assertEqual(by_id["snapshot-load-exercised"]["status"], "pass")

    def test_memory_growth_and_child_cpu_are_bounded_explicitly(self):
        passed = SOAK.memory_growth_check(
            {"available": True, "growth_kib": 1024, "growth_per_hour_kib": None}
        )
        failed = SOAK.memory_growth_check(
            {
                "available": True,
                "growth_kib": SOAK.MAX_SHORT_RUN_MEMORY_GROWTH_KIB + 1,
                "growth_per_hour_kib": None,
            }
        )
        hourly_failed = SOAK.memory_growth_check(
            {
                "available": True,
                "growth_kib": 1,
                "growth_per_hour_kib": SOAK.MAX_HOURLY_MEMORY_GROWTH_KIB + 1,
            }
        )
        self.assertEqual(passed["status"], "pass")
        self.assertEqual(failed["status"], "fail")
        self.assertEqual(hourly_failed["status"], "fail")

        class Times:
            user = 1.0
            system = 2.0
            children_user = 3.0
            children_system = 4.0

        original = SOAK.os.times
        SOAK.os.times = lambda: Times()
        try:
            self.assertEqual(SOAK.process_cpu_seconds(), 10.0)
        finally:
            SOAK.os.times = original

    def test_report_arguments_are_bounded(self):
        parser = SOAK.build_parser()
        for argv in (
            ["--duration-seconds", "0"],
            ["--duration-seconds", str(SOAK.MAX_DURATION_SECONDS + 1)],
            ["--iterations", "0"],
            ["--load-factor", str(SOAK.MAX_LOAD_FACTOR + 1)],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    parser.parse_args(argv)
        self.assertEqual(
            parser.parse_args(["--duration-seconds", str(SOAK.MAX_DURATION_SECONDS)])
            .duration_seconds,
            float(SOAK.MAX_DURATION_SECONDS),
        )
        with self.assertRaises(SOAK.SoakError):
            SOAK.run(parser.parse_args(["--mode", "live", "--iterations", "5"]))
        with self.assertRaises(SOAK.SoakError):
            SOAK.run(parser.parse_args(["--sample-interval-seconds", "0.0001"]))

    def test_report_is_written_bounded_and_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "soak.json"
            exit_code = SOAK.main(
                [
                    "--mode",
                    "synthetic",
                    "--iterations",
                    "30",
                    "--quiet",
                    "--report",
                    str(path),
                ]
            )
            self.assertEqual(exit_code, 0)
            written = json.loads(path.read_text())
            self.assertEqual(written["kind"], SOAK.REPORT_KIND)
            self.assertEqual(written["status"], "pass")
            self.assertLessEqual(path.stat().st_size, SOAK.MAX_REPORT_BYTES)
            self.assertEqual(written["safety"]["mode"], "passive-observation")
            with self.assertRaises(SOAK.SoakError):
                SOAK.write_report(written, pathlib.Path(directory) / "absent" / "x.json")
            target = pathlib.Path(directory) / "target.json"
            target.write_text("preserve")
            link = pathlib.Path(directory) / "report-link.json"
            link.symlink_to(target)
            with self.assertRaises(SOAK.SoakError):
                SOAK.write_report(written, link)
            self.assertEqual(target.read_text(), "preserve")


class DocumentationTests(unittest.TestCase):
    def test_documentation_states_boundary_commands_and_rollback(self):
        text = (ROOT / "docs" / "audio-live-telemetry-v1.md").read_text()
        for needle in (
            "--duration-seconds 28800",
            "--duration-seconds 3600",
            "pw-dump",
            "pw-top -b -n 1",
            "aseqdump",
            "/api/v1/telemetry",
            "Rollback",
            "Grenzen",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        for stream_id in MODULE.STREAM_IDS:
            with self.subTest(stream=stream_id):
                self.assertIn(stream_id, text)

    def test_justfile_exposes_bounded_telemetry_targets(self):
        text = (ROOT / "Justfile").read_text()
        for needle in (
            "telemetry-live-check:",
            "telemetry-live-safety:",
            "telemetry-live-show:",
            "\ntelemetry-soak-fast ",
            "\ntelemetry-soak-1h ",
            "\ntelemetry-soak-8h ",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        self.assertIn("--duration-seconds 28800", text)
        self.assertIn("--duration-seconds 3600", text)
        self.assertIn("--sample-interval-seconds 0.25 --load-factor 8", text)
        self.assertIn("--sample-interval-seconds 30 --load-factor 1", text)
        self.assertIn("python3 scripts/audio_live_telemetry.py check", text)
        self.assertIn("r.get(k)", text)


if __name__ == "__main__":
    unittest.main()
