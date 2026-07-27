import copy
import json
import math
import pathlib
import sys
import tempfile
import threading
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

    def test_legato_glides_pitch_and_timbre_without_sample_jump(self):
        voice = engine.WhaleVoice(self.config, seed=1234)
        voice.note_on(45, 55)
        voice.render(self.config.sample_rate // 2)
        envelope_before = voice.envelope
        old_frequency = voice.current_frequency
        old_register = voice.current_register
        baseline_next = copy.deepcopy(voice).render(1)[0]

        voice.note_on(69, 90)
        target_frequency = voice.target_frequency
        target_register = voice.target_register
        changed_next = voice.render(1)[0]
        voice.render(self.config.block_frames - 1)

        self.assertLess(abs(changed_next - baseline_next), 0.005)
        self.assertGreater(voice.envelope, envelope_before * 0.95)
        self.assertGreater(voice.current_frequency, old_frequency)
        self.assertLess(voice.current_frequency, target_frequency)
        self.assertGreater(voice.current_register, old_register)
        self.assertLess(voice.current_register, target_register)
        self.assertEqual(voice.active_note, 69)
        self.assertEqual(len(voice.held_notes), 2)

    def test_detached_note_crossfades_before_new_phrase_onset(self):
        voice = engine.WhaleVoice(self.config, seed=1234)
        voice.note_on(45, 70)
        voice.render(self.config.sample_rate // 2)
        voice.note_off(45)
        before = voice.render(self.config.sample_rate // 20)
        baseline_next = copy.deepcopy(voice).render(1)[0]
        self.assertGreater(voice.envelope, 0.12)
        self.assertFalse(voice.gate)
        self.assertFalse(voice.held_notes)

        voice.note_on(69, 90)
        first_fade_sample = voice.render(1)[0]
        remaining = voice.retrigger_fade_remaining
        transition = voice.render(remaining)

        self.assertLess(abs(first_fade_sample - baseline_next), 0.005)
        self.assertLess(
            max(
                abs(right - left)
                for left, right in zip(
                    before[-1:] + [first_fade_sample] + transition[:-1],
                    [first_fade_sample] + transition,
                )
            ),
            0.01,
        )
        self.assertEqual(transition[-1], 0.0)
        self.assertTrue(voice.gate)
        self.assertEqual(voice.retrigger_fade_remaining, 0)
        self.assertEqual(voice.note_age_frames, 0)
        self.assertEqual(voice.hold_frames, 0)
        self.assertAlmostEqual(voice.current_frequency, voice.target_frequency)
        self.assertAlmostEqual(voice.current_register, voice.target_register)
        self.assertEqual(voice.glide_seconds, 0.02)
        self.assertEqual(voice.envelope, 0.0)

        onset_sample = voice.render(1)[0]
        self.assertEqual(voice.note_age_frames, 1)
        self.assertEqual(voice.hold_frames, 1)
        self.assertGreater(voice.envelope, 0.0)
        self.assertLess(abs(onset_sample), 0.01)

    def test_detached_retrigger_is_chunk_invariant(self):
        def prepared_voice():
            voice = engine.WhaleVoice(self.config, seed=1234)
            voice.note_on(45, 90)
            voice.render(self.config.sample_rate // 2)
            voice.note_off(45)
            voice.render(self.config.sample_rate // 20)
            voice.note_on(69, 100)
            return voice

        whole = prepared_voice()
        total_frames = whole.retrigger_fade_total + 257
        expected = whole.render(total_frames)

        chunked = prepared_voice()
        actual = []
        fixed_chunks = (17, 31, 3, 64)
        for frames in (*fixed_chunks, total_frames - sum(fixed_chunks)):
            actual.extend(chunked.render(frames))

        self.assertEqual(actual, expected)
        self.assertEqual(chunked.__dict__, whole.__dict__)

    def test_low_release_tail_does_not_reattack_during_retrigger_fade(self):
        voice = engine.WhaleVoice(self.config, seed=1234)
        voice.note_on(45, 70)
        voice.render(self.config.sample_rate // 2)
        voice.note_off(45)
        voice.render(self.config.sample_rate // 20)
        voice.envelope = 2e-5

        voice.note_on(69, 90)
        first_chunk = voice.render(17)
        self.assertEqual(voice.envelope, 2e-5)
        second_chunk = voice.render(voice.retrigger_fade_remaining)

        self.assertLess(max(abs(sample) for sample in first_chunk + second_chunk), 1e-4)
        self.assertEqual(voice.retrigger_fade_remaining, 0)
        self.assertEqual(voice.envelope, 0.0)
        onset = voice.render(1)[0]
        self.assertGreater(voice.envelope, 0.0)
        self.assertLess(abs(onset), 0.01)

    def test_six_hour_contour_phase_states_cover_all_legato_pairs(self):
        runtime_seconds = 21_600
        maximum_delta = 0.0
        worst_pair = None
        for old_note in range(self.config.min_note, self.config.max_note + 1):
            base = engine.WhaleVoice(self.config, seed=1234)
            base.note_on(old_note, 127)
            base.render(self.config.sample_rate)
            register = base.target_register
            base.current_register = register
            base.current_frequency = base.target_frequency
            base.velocity = base.target_velocity
            base.note_age_frames = self.config.sample_rate * runtime_seconds
            base.hold_frames = base.note_age_frames
            base.slow_arc_phase = (
                0.7 + math.tau * (0.071 + register * 0.023) * runtime_seconds
            ) % math.tau
            base.second_arc_phase = (
                2.1 + math.tau * (0.193 - register * 0.041) * runtime_seconds
            ) % math.tau
            base.flutter_phase = (
                math.tau * (1.7 + register * 2.8) * runtime_seconds
            ) % math.tau
            baseline_next = copy.deepcopy(base).render(1)[0]

            for new_note in range(self.config.min_note, self.config.max_note + 1):
                if new_note == old_note:
                    continue
                changed = copy.deepcopy(base)
                changed.note_on(new_note, 127)
                delta = abs(changed.render(1)[0] - baseline_next)
                if delta > maximum_delta:
                    maximum_delta = delta
                    worst_pair = (old_note, new_note)

        self.assertLess(maximum_delta, 0.005, worst_pair)

    def test_fractional_formant_modulator_uses_its_own_integrated_phase(self):
        voice = engine.WhaleVoice(self.config, seed=1234)
        voice.note_on(60, 80)
        voice.render(64)
        voice.formant_phase = math.tau - 1e-6
        voice.carrier_mod_phase = 1.25
        before = voice.carrier_mod_phase

        voice.render(1)

        delta = (voice.carrier_mod_phase - before) % math.tau
        self.assertGreater(delta, 0.0)
        self.assertLess(delta, 0.5)

    def test_detuned_partial_phase_is_integrated_after_a_long_hold(self):
        voice = engine.WhaleVoice(self.config, seed=1234)
        voice.note_on(45, 100)
        voice.render(self.config.sample_rate)
        voice.note_age_frames = self.config.sample_rate * 3_600
        voice.hold_frames = voice.note_age_frames
        baseline_next = copy.deepcopy(voice).render(1)[0]
        phase_before = voice.detuned_phase

        voice.note_on(84, 100)
        changed_next = voice.render(1)[0]

        self.assertLess(abs(changed_next - baseline_next), 0.005)
        self.assertNotEqual(voice.detuned_phase, phase_before)

    def test_cc120_is_immediate_silence_while_cc123_releases(self):
        panic = engine.WhaleVoice(self.config)
        panic.note_on(55, 100)
        panic.render(self.config.sample_rate * 2)
        panic.control_change(120, 0)

        self.assertFalse(panic.active)
        self.assertFalse(panic.gate)
        self.assertFalse(panic.held_notes)
        self.assertIsNone(panic.active_note)
        self.assertEqual(panic.envelope, 0.0)
        self.assertEqual(panic.render(128), [0.0] * 128)

        fading = engine.WhaleVoice(self.config)
        fading.note_on(45, 90)
        fading.render(self.config.sample_rate // 2)
        fading.note_off(45)
        fading.render(self.config.sample_rate // 20)
        fading.note_on(69, 100)
        self.assertGreater(fading.retrigger_fade_remaining, 0)
        fading.control_change(120, 0)
        self.assertEqual(fading.retrigger_fade_remaining, 0)
        self.assertEqual(fading.render(128), [0.0] * 128)

        release = engine.WhaleVoice(self.config)
        release.note_on(55, 100)
        release.render(self.config.sample_rate * 2)
        release.control_change(123, 0)
        self.assertFalse(release.gate)
        self.assertGreater(release.envelope, 0.0)
        self.assertGreater(max(abs(sample) for sample in release.render(128)), 0.0)

        during_fade = engine.WhaleVoice(self.config, seed=1234)
        during_fade.note_on(45, 100)
        during_fade.render(self.config.sample_rate // 2)
        during_fade.note_off(45)
        during_fade.render(self.config.sample_rate // 20)
        during_fade.note_on(69, 100)
        during_fade.render(max(1, during_fade.retrigger_fade_remaining // 4))
        self.assertGreater(during_fade.retrigger_fade_remaining, 0)
        baseline_next = copy.deepcopy(during_fade).render(1)[0]
        during_fade.control_change(123, 0)
        released_next = during_fade.render(1)[0]
        self.assertEqual(during_fade.retrigger_fade_remaining, 0)
        self.assertFalse(during_fade.gate)
        self.assertIsNone(during_fade.active_note)
        self.assertGreater(during_fade.envelope, 0.0)
        self.assertLess(abs(released_next - baseline_next), 0.005)
        self.assertGreater(max(abs(sample) for sample in during_fade.render(128)), 0.0)

        baseline_after_release = copy.deepcopy(during_fade).render(1)[0]
        during_fade.note_on(80, 100)
        restarted = during_fade.render(1)[0]
        self.assertGreater(during_fade.retrigger_fade_remaining, 0)
        self.assertLess(abs(restarted - baseline_after_release), 0.005)

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

    def test_generic_digital_pianos_are_not_roland_like(self):
        ports = [
            whale_live.MidiPort("24:0", "Yamaha Digital Piano", "MIDI 1"),
            whale_live.MidiPort("25:0", "Kawai Digital Piano", "MIDI 2"),
        ]
        with mock.patch.object(whale_live, "list_midi_ports", return_value=ports):
            with self.assertRaisesRegex(
                RuntimeError, "Roland FP-30X MIDI port not found"
            ):
                whale_live.resolve_midi_port("auto")
            with self.assertRaisesRegex(RuntimeError, "is not Roland-like"):
                whale_live.resolve_midi_port("24:0")

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

    def test_pcm_pipe_is_page_and_power_of_two_bounded(self):
        with mock.patch.object(whale_live.os, "sysconf", return_value=4_096):
            self.assertEqual(whale_live.pcm_pipe_size_bytes(128), 4_096)
            self.assertEqual(whale_live.pcm_pipe_size_bytes(1_000), 8_192)
            self.assertEqual(whale_live.pcm_pipe_size_bytes(2_048), 16_384)
        with self.assertRaises(ValueError):
            whale_live.pcm_pipe_size_bytes(8)
        with mock.patch.object(whale_live.os, "sysconf", return_value=65_536):
            with self.assertRaisesRegex(RuntimeError, "page size exceeds"):
                whale_live.pcm_pipe_size_bytes(128)

    def test_pipe_capacity_readback_is_fail_closed(self):
        stream = mock.Mock()
        stream.fileno.return_value = 7
        with mock.patch.object(whale_live.fcntl, "fcntl", return_value=4_096):
            self.assertEqual(
                whale_live.verified_pipe_capacity_bytes(stream, 4_096), 4_096
            )
        with mock.patch.object(whale_live.fcntl, "fcntl", return_value=65_536):
            with self.assertRaisesRegex(RuntimeError, "exceeds configured maximum"):
                whale_live.verified_pipe_capacity_bytes(stream, 4_096)

    def test_realtime_pacer_waits_and_drops_catch_up_bursts(self):
        now = [0]
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            now[0] += round(seconds * 1_000_000_000)

        pacer = whale_live.RealtimeBlockPacer(
            48_000, 128, now_ns=lambda: now[0], sleeper=fake_sleep
        )
        block_ns = pacer.block_duration_ns
        pacer.wait()
        self.assertEqual(sleeps, [block_ns / 1_000_000_000])
        self.assertEqual(pacer.next_deadline_ns, block_ns * 2)

        now[0] = pacer.next_deadline_ns + block_ns
        pacer.wait()
        self.assertEqual(pacer.next_deadline_ns, now[0] + block_ns)
        self.assertEqual(len(sleeps), 1)

    def test_pcm_write_can_be_cancelled_while_pipe_is_full(self):
        stop_event = threading.Event()
        process = mock.Mock(returncode=None)
        process.poll.return_value = None
        stream = mock.Mock()
        stream.fileno.return_value = 7

        def stop_during_wait(*_args):
            stop_event.set()
            return ([], [], [])

        with mock.patch.object(whale_live.os, "write", side_effect=BlockingIOError):
            self.assertFalse(
                whale_live.write_pcm_block(
                    stream,
                    b"audio",
                    stop_event,
                    process,
                    wait_for_write=stop_during_wait,
                )
            )

    def test_pcm_write_handles_partial_nonblocking_writes(self):
        stop_event = threading.Event()
        process = mock.Mock(returncode=None)
        process.poll.return_value = None
        stream = mock.Mock()
        stream.fileno.return_value = 7
        with mock.patch.object(whale_live.os, "write", side_effect=[2, 3]):
            self.assertTrue(
                whale_live.write_pcm_block(stream, b"audio", stop_event, process)
            )

    def test_service_status_fails_closed_on_systemctl_error(self):
        failed = mock.Mock(returncode=1, stdout="", stderr="bus unavailable")
        with mock.patch.object(whale_live, "run_capture", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "systemctl show failed"):
                whale_live.service_status()
            with self.assertRaisesRegex(RuntimeError, "systemctl show failed"):
                whale_live.stop_service()

    def test_short_demo_truncates_the_fixed_phrase(self):
        with mock.patch.object(whale_live, "write_stereo_wav") as writer:
            result = whale_live.create_demo(pathlib.Path("unused.wav"), 1.0, 0.16)
        self.assertEqual(result["frames"], 48_000)
        writer.assert_called_once()

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

    def test_doctor_rejects_unsupported_system_page_size(self):
        completed = mock.Mock(returncode=0, stdout="active\n", stderr="")
        port = whale_live.MidiPort(
            "24:0", "Roland Digital Piano", "Roland Digital Piano MIDI 1"
        )
        with (
            mock.patch.object(whale_live.shutil, "which", return_value="/usr/bin/tool"),
            mock.patch.object(whale_live, "list_midi_ports", return_value=[port]),
            mock.patch.object(whale_live, "run_capture", return_value=completed),
            mock.patch.object(whale_live.os, "sysconf", return_value=65_536),
        ):
            report = whale_live.runtime_doctor()

        self.assertFalse(report["ready"])
        self.assertIn("pcm-pipe-contract-unavailable", report["blocking_reasons"])
        self.assertEqual(report["system_page_size_bytes"], 65_536)
        self.assertIsNone(report["pcm_pipe_capacity_bytes"])
        self.assertIn("65536 > 4096", report["pcm_pipe_error"])

    def test_profile_sink_policy_matches_runtime_default_target(self):
        profile = json.loads(
            (ROOT / "profiles/buckelwal-live-voice-v1.json").read_text()
        )
        audio = profile["audio"]
        command = whale_live.build_pw_cat_command(target=None, latency_frames=128)

        self.assertNotIn("default_sink", audio)
        self.assertEqual(
            audio["default_sink_policy"],
            "current-pipewire-default-unless-explicit-target",
        )
        self.assertEqual(audio["reference_sink"], "motu-m2")
        self.assertNotIn("--target", command)

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
