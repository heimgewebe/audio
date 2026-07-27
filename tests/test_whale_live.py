import math
import pathlib
import sys
import tempfile
import unittest
import wave
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import whale_live  # noqa: E402
import whale_live_engine as engine  # noqa: E402


class WhaleMidiParserTests(unittest.TestCase):
    def test_parses_note_control_and_pitch_events(self):
        note_on = engine.parse_aseqdump_line(
            " 24:0   Note on                 0, note 60, velocity 97"
        )
        note_off = engine.parse_aseqdump_line(
            " 24:0   Note off                0, note 60, velocity 64"
        )
        zero_velocity = engine.parse_aseqdump_line(
            " 24:0   Note on                 0, note 61, velocity 0"
        )
        pedal = engine.parse_aseqdump_line(
            " 24:0   Control change          0, controller 64, value 127"
        )
        bend = engine.parse_aseqdump_line(
            " 24:0   Pitch bend              0, value -1024"
        )

        self.assertEqual(note_on, engine.MidiEvent("note_on", 0, 60, 97))
        self.assertEqual(note_off, engine.MidiEvent("note_off", 0, 60, 64))
        self.assertEqual(zero_velocity, engine.MidiEvent("note_off", 0, 61, 0))
        self.assertEqual(
            pedal,
            engine.MidiEvent("control_change", channel=0, controller=64, value=127),
        )
        self.assertEqual(bend, engine.MidiEvent("pitch_bend", channel=0, value=-1024))
        self.assertIsNone(
            engine.parse_aseqdump_line("Waiting for data. Press Ctrl+C to end.")
        )

    def test_parses_aseqdump_port_table(self):
        ports = whale_live.parse_aseqdump_ports(
            """ Port    Client name                      Port name
  0:0    System                           Timer
 24:0    Roland Digital Piano             Roland Digital Piano MIDI 1
"""
        )
        self.assertEqual(
            ports,
            [
                whale_live.MidiPort("0:0", "System", "Timer"),
                whale_live.MidiPort(
                    "24:0", "Roland Digital Piano", "Roland Digital Piano MIDI 1"
                ),
            ],
        )


class WhaleVoiceTests(unittest.TestCase):
    def setUp(self):
        self.config = engine.WhaleVoiceConfig(
            sample_rate=8_000,
            block_frames=64,
            master_gain=0.16,
            max_frequency_hz=2_800.0,
        )

    def test_all_88_keys_map_monotonically_and_reach_endpoints(self):
        frequencies = [
            engine.note_to_whale_hz(note, self.config) for note in range(21, 109)
        ]
        self.assertEqual(len(frequencies), 88)
        self.assertAlmostEqual(frequencies[0], self.config.min_frequency_hz)
        self.assertAlmostEqual(frequencies[-1], self.config.max_frequency_hz)
        self.assertTrue(
            all(left < right for left, right in zip(frequencies, frequencies[1:]))
        )

    def test_held_key_keeps_an_evolving_audible_voice(self):
        voice = engine.WhaleVoice(self.config)
        voice.note_on(52, 72)
        samples = voice.render(self.config.sample_rate * 3)
        first_rms = self._rms(
            samples[self.config.sample_rate : self.config.sample_rate * 2]
        )
        last_rms = self._rms(samples[-self.config.sample_rate :])

        self.assertTrue(voice.gate)
        self.assertGreater(first_rms, 0.005)
        self.assertGreater(last_rms, 0.005)
        self.assertNotAlmostEqual(first_rms, last_rms, places=5)

    def test_note_release_decays_after_a_hold(self):
        voice = engine.WhaleVoice(self.config)
        voice.note_on(48, 64)
        voice.render(self.config.sample_rate)
        voice.note_off(48)
        release = voice.render(self.config.sample_rate * 4)

        early = self._rms(release[: self.config.sample_rate // 2])
        late = self._rms(release[-self.config.sample_rate // 2 :])
        self.assertFalse(voice.gate)
        self.assertGreater(early, late * 4)
        self.assertLess(late, 0.001)

    def test_legato_glides_one_voice_without_retriggering_from_zero(self):
        voice = engine.WhaleVoice(self.config)
        voice.note_on(45, 55)
        voice.render(self.config.sample_rate // 2)
        envelope_before = voice.envelope
        old_frequency = voice.current_frequency

        voice.note_on(69, 90)
        target = voice.target_frequency
        voice.render(self.config.block_frames)

        self.assertGreater(voice.envelope, envelope_before * 0.95)
        self.assertGreater(voice.current_frequency, old_frequency)
        self.assertLess(voice.current_frequency, target)
        self.assertEqual(voice.active_note, 69)
        self.assertEqual(len(voice.held_notes), 2)

    def test_releasing_latest_legato_note_returns_to_previous_held_note(self):
        voice = engine.WhaleVoice(self.config)
        voice.note_on(40, 50)
        voice.note_on(60, 80)
        voice.note_off(60)

        self.assertTrue(voice.gate)
        self.assertEqual(voice.active_note, 40)
        self.assertAlmostEqual(
            voice.target_frequency, engine.note_to_whale_hz(40, self.config)
        )

    def test_sustain_defers_and_then_releases_the_phrase(self):
        voice = engine.WhaleVoice(self.config)
        voice.note_on(55, 70)
        voice.render(self.config.sample_rate // 2)
        voice.control_change(64, 110)
        voice.note_off(55)
        voice.render(self.config.sample_rate // 2)
        self.assertTrue(voice.gate)

        voice.control_change(64, 0)
        self.assertFalse(voice.gate)
        self.assertGreater(voice.release_seconds, 1.4)
        voice.render(self.config.sample_rate * 5)
        self.assertLess(voice.envelope, 0.001)

    def test_output_is_deterministic_and_bounded(self):
        def render_once():
            voice = engine.WhaleVoice(self.config, seed=1234)
            voice.note_on(90, 127)
            voice.control_change(1, 127)
            return voice.render(self.config.sample_rate)

        first = render_once()
        second = render_once()
        self.assertEqual(first, second)
        self.assertLessEqual(
            max(abs(sample) for sample in first), engine.MAX_MASTER_GAIN
        )
        self.assertGreater(max(abs(sample) for sample in first), 0.01)

    def test_f32_stereo_has_exact_bounded_shape(self):
        voice = engine.WhaleVoice(self.config)
        voice.note_on(60, 60)
        payload = voice.render_f32_stereo(64)
        self.assertEqual(len(payload), 64 * 2 * 4)

    @staticmethod
    def _rms(samples):
        return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


class WhaleTimelineTests(unittest.TestCase):
    def test_demo_writes_safe_stereo_wave(self):
        config = engine.WhaleVoiceConfig(sample_rate=8_000, max_frequency_hz=2_800.0)
        samples = engine.render_timeline(engine.default_demo_events(), 12.0, config)
        metrics = engine.signal_metrics(samples)
        self.assertEqual(metrics["frames"], 96_000)
        self.assertLessEqual(metrics["peak"], engine.MAX_MASTER_GAIN)
        self.assertGreater(metrics["rms"], 0.005)

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "buckelwal.wav"
            engine.write_stereo_wav(path, samples, config.sample_rate)
            with wave.open(str(path), "rb") as handle:
                self.assertEqual(handle.getframerate(), 8_000)
                self.assertEqual(handle.getnchannels(), 2)
                self.assertEqual(handle.getsampwidth(), 2)
                self.assertEqual(handle.getnframes(), 96_000)

    def test_timeline_rejects_unbounded_duration(self):
        with self.assertRaises(ValueError):
            engine.render_timeline([], 31.0)


class WhaleRuntimeTests(unittest.TestCase):
    def test_auto_port_resolution_is_fail_closed(self):
        ports = [whale_live.MidiPort("14:0", "Midi Through", "Port-0")]
        with mock.patch.object(whale_live, "list_midi_ports", return_value=ports):
            with self.assertRaisesRegex(
                RuntimeError, "Roland FP-30X MIDI port not found"
            ):
                whale_live.resolve_midi_port("auto")

    def test_explicit_port_resolution_rejects_non_roland_port(self):
        ports = [whale_live.MidiPort("14:0", "Midi Through", "Port-0")]
        with mock.patch.object(whale_live, "list_midi_ports", return_value=ports):
            with self.assertRaisesRegex(RuntimeError, "is not Roland-like"):
                whale_live.resolve_midi_port("14:0")

    def test_explicit_port_resolution_accepts_roland_port(self):
        expected = whale_live.MidiPort(
            "24:0", "Roland Digital Piano", "Roland Digital Piano MIDI 1"
        )
        with mock.patch.object(whale_live, "list_midi_ports", return_value=[expected]):
            self.assertEqual(whale_live.resolve_midi_port("24:0"), expected)

    def test_auto_port_resolution_rejects_ambiguity(self):
        ports = [
            whale_live.MidiPort("24:0", "Roland Digital Piano", "MIDI 1"),
            whale_live.MidiPort("25:0", "Roland FP-30X", "MIDI 2"),
        ]
        with mock.patch.object(whale_live, "list_midi_ports", return_value=ports):
            with self.assertRaisesRegex(
                RuntimeError, "multiple Roland-like MIDI ports"
            ):
                whale_live.resolve_midi_port("auto")

    def test_pw_cat_command_is_explicit_and_bounded(self):
        command = whale_live.build_pw_cat_command(
            target="alsa_output.test", latency_frames=128
        )
        self.assertEqual(command[0], "pw-cat")
        self.assertIn("f32", command)
        self.assertIn("48000", command)
        self.assertIn("128", command)
        self.assertEqual(command[-1], "-")
        with self.assertRaises(ValueError):
            whale_live.build_pw_cat_command(target=None, latency_frames=8)

    def test_doctor_reports_missing_roland_without_claiming_readiness(self):
        completed = mock.Mock(returncode=0, stdout="active\n", stderr="")
        with (
            mock.patch.object(whale_live.shutil, "which", return_value="/usr/bin/tool"),
            mock.patch.object(
                whale_live,
                "list_midi_ports",
                return_value=[whale_live.MidiPort("14:0", "Midi Through", "Port-0")],
            ),
            mock.patch.object(whale_live, "run_capture", return_value=completed),
        ):
            report = whale_live.runtime_doctor()
        self.assertFalse(report["ready"])
        self.assertEqual(report["blocking_reason"], "roland-midi-not-found")
        self.assertEqual(report["blocking_reasons"], ["roland-midi-not-found"])
        self.assertIsNone(report["roland_midi_port"])


if __name__ == "__main__":
    unittest.main()
