import importlib.util
import pathlib
import struct
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "roland_midi_capture_test_target", ROOT / "scripts" / "roland_midi_capture.py"
)
assert SPEC and SPEC.loader
MIDI = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MIDI
SPEC.loader.exec_module(MIDI)


def smf(track: bytes, *, division: int = MIDI.SMPTE_DIVISION, declared: int | None = None) -> bytes:
    length = len(track) if declared is None else declared
    return (
        b"MThd"
        + struct.pack(">IHHH", 6, 0, 1, division)
        + b"MTrk"
        + struct.pack(">I", length)
        + track
    )


def smf_tracks(*tracks: bytes) -> bytes:
    return (
        b"MThd"
        + struct.pack(">IHHH", 6, 1, len(tracks), MIDI.SMPTE_DIVISION)
        + b"".join(b"MTrk" + struct.pack(">I", len(track)) + track for track in tracks)
    )


class RolandMidiCaptureTests(unittest.TestCase):
    def test_capture_argv_is_exact_arecordmidi_smpte_contract(self):
        self.assertEqual(
            MIDI.arecordmidi_capture_argv(
                pathlib.Path("/usr/bin/arecordmidi"),
                "24:0",
                pathlib.Path("/private/.song.partial.mid"),
            ),
            [
                "/usr/bin/arecordmidi",
                "-p",
                "24:0",
                "-f",
                "25",
                "-t",
                "40",
                "/private/.song.partial.mid",
            ],
        )
        with self.assertRaisesRegex(MIDI.MidiCaptureError, "address"):
            MIDI.arecordmidi_capture_argv(pathlib.Path("x"), "Roland", pathlib.Path("x"))

    def test_smpte_scanner_preserves_notes_velocity_cc64_pitchbend_and_running_status(self):
        track = (
            b"\x00\x90\x3c\x64"  # note on, velocity 100
            b"\x01\x3e\x40"  # running-status note on, velocity 64
            b"\x00\xb0\x40\x7f"  # sustain CC64
            b"\x00\xe0\x00\x40"  # centered pitch bend
            b"\x01\x80\x3c\x20"  # note off, release velocity 32
            b"\x00\x90\x3e\x00"  # velocity-zero note off
            b"\x00\xff\x2f\x00"
        )
        report = MIDI.validate_smf_bytes(smf(track))
        self.assertEqual(report["division"], 0xE728)
        self.assertEqual(
            report["timing"],
            {"basis": "SMPTE", "fps": 25, "ticks_per_frame": 40, "nominal_resolution_ms": 1},
        )
        self.assertEqual(report["event_counts"]["note_on"], 2)
        self.assertEqual(report["event_counts"]["note_off"], 2)
        self.assertEqual(report["event_counts"]["control_change"], 1)
        self.assertEqual(report["event_counts"]["sustain_cc64"], 1)
        self.assertEqual(report["event_counts"]["pitch_bend"], 1)
        self.assertEqual(report["note_velocity"], {"minimum": 0, "maximum": 100})

    def test_intentionally_silent_take_is_structurally_valid(self):
        report = MIDI.validate_smf_bytes(smf(b"\x00\xff\x2f\x00"))
        self.assertEqual(report["event_counts"]["note_on"], 0)
        self.assertEqual(report["event_counts"]["note_off"], 0)
        self.assertEqual(report["note_velocity"], {"minimum": None, "maximum": None})

    def test_event_bound_is_global_across_all_tracks(self):
        two_events = b"\x00\x90\x3c\x40\x00\xff\x2f\x00"
        with mock.patch.object(MIDI, "MAX_EVENTS", 3):
            with self.assertRaisesRegex(MIDI.MidiCaptureError, "event count"):
                MIDI.validate_smf_bytes(smf_tracks(two_events, two_events))

    def test_malformed_truncated_and_running_status_boundaries_fail_closed(self):
        cases = {
            "wrong division": smf(b"\x00\xff\x2f\x00", division=480),
            "declared track": smf(b"\x00\xff\x2f\x00", declared=99),
            "truncated channel": smf(b"\x00\x90\x3c"),
            "overlong vlq": smf(b"\x81\x80\x80\x80\x00\xff\x2f\x00"),
            "running after meta": smf(
                b"\x00\x90\x3c\x64\x00\xff\x01\x00\x00\x3d\x40\x00\xff\x2f\x00"
            ),
            "running after sysex": smf(
                b"\x00\x90\x3c\x64\x00\xf0\x00\x00\x3d\x40\x00\xff\x2f\x00"
            ),
            "truncated sysex": smf(b"\x00\xf0\x05\x01\x02\x00\xff\x2f\x00"),
            "trailing bytes": smf(b"\x00\xff\x2f\x00") + b"x",
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(MIDI.MidiCaptureError):
                    MIDI.validate_smf_bytes(payload)

    def test_unique_missing_and_ambiguous_kernel_bound_roland_source(self):
        clients = (
            'Client 24 : "FP-30X" [Kernel,Card=2]\n'
            '  Port 0 : "FP-30X MIDI" (RWe-)\n'
            '  Port 1 : "FP-30X Extra" (RWe-)\n'
        )
        listing_one = " Port    Client name                      Port name\n 24:0    FP-30X                           FP-30X MIDI\n"
        usb = {
            "vendor_id": "0582",
            "product_id": "01b1",
            "identity_strength": "model-usb-port",
            "bus_number": "1",
            "port_path": "2.3",
        }
        usb["fingerprint"] = MIDI.canonical_sha256(usb)
        with tempfile.TemporaryDirectory() as temp:
            clients_path = pathlib.Path(temp) / "clients"
            clients_path.write_text(clients, encoding="utf-8")
            with mock.patch.object(MIDI, "_usb_identity_for_card", return_value=usb):
                match = MIDI.discover_unique_roland_port(
                    arecordmidi_listing=listing_one, clients_path=clients_path
                )
                self.assertEqual(match["address"], "24:0")
                self.assertEqual(match["identity"]["kernel_card"], 2)
                self.assertNotIn("FP-30X", repr(match["identity"]))
                with self.assertRaisesRegex(MIDI.MidiCaptureError, "observed 0"):
                    MIDI.discover_unique_roland_port(
                        arecordmidi_listing=" Port Client name Port name\n",
                        clients_path=clients_path,
                    )
                with self.assertRaisesRegex(MIDI.MidiCaptureError, "observed 2"):
                    MIDI.discover_unique_roland_port(
                        arecordmidi_listing=(
                            listing_one
                            + " 24:1    FP-30X                           FP-30X Extra\n"
                        ),
                        clients_path=clients_path,
                    )

    def test_kernel_legacy_client_is_bound_through_unique_roland_rawmidi_card(self):
        clients = (
            'Client 24 : "Roland Digital Piano" [Kernel Legacy]\n'
            '  Port 0 : "Roland Digital Piano MIDI 1" (RWeX) [In/Out]\n'
        )
        listing = (
            " Port    Client name                      Port name\n"
            " 24:0    Roland Digital Piano             Roland Digital Piano MIDI 1\n"
        )
        usb = {
            "vendor_id": "0582",
            "product_id": "01b1",
            "identity_strength": "model-usb-port",
            "bus_number": "1",
            "port_path": "9.3",
        }
        usb["fingerprint"] = MIDI.canonical_sha256(usb)
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            clients_path = root / "clients"
            clients_path.write_text(clients, encoding="utf-8")
            proc_root = root / "asound"
            (proc_root / "card2").mkdir(parents=True)
            (proc_root / "card2" / "midi0").write_text(
                "Roland Digital Piano\n\nType: Legacy\n", encoding="utf-8"
            )
            with mock.patch.object(MIDI, "_usb_identity_for_card", return_value=usb):
                match = MIDI.discover_unique_roland_port(
                    arecordmidi_listing=listing,
                    clients_path=clients_path,
                    sound_class_root=root / "sound",
                    proc_asound_root=proc_root,
                )
        self.assertEqual(match["address"], "24:0")
        self.assertEqual(match["identity"]["kernel_card"], 2)

    def test_kernel_legacy_mapping_rejects_user_client_spoof(self):
        clients = (
            'Client 24 : "Roland Digital Piano" [User Legacy]\n'
            '  Port 0 : "Roland Digital Piano MIDI 1" (RWeX) [In/Out]\n'
        )
        listing = (
            " Port    Client name                      Port name\n"
            " 24:0    Roland Digital Piano             Roland Digital Piano MIDI 1\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            clients_path = root / "clients"
            clients_path.write_text(clients, encoding="utf-8")
            with mock.patch.object(MIDI, "_usb_identity_for_card") as usb_lookup:
                with self.assertRaisesRegex(MIDI.MidiCaptureError, "observed 0"):
                    MIDI.discover_unique_roland_port(
                        arecordmidi_listing=listing,
                        clients_path=clients_path,
                        sound_class_root=root / "sound",
                        proc_asound_root=root / "asound",
                    )
            usb_lookup.assert_not_called()

    def test_kernel_legacy_mapping_fails_closed_when_two_roland_cards_match(self):
        clients = (
            'Client 24 : "Roland Digital Piano" [Kernel Legacy]\n'
            '  Port 0 : "Roland Digital Piano MIDI 1" (RWeX) [In/Out]\n'
        )
        listing = (
            " Port    Client name                      Port name\n"
            " 24:0    Roland Digital Piano             Roland Digital Piano MIDI 1\n"
        )
        usb = {
            "vendor_id": "0582",
            "product_id": "01b1",
            "identity_strength": "model-usb-port",
            "bus_number": "1",
            "port_path": "9.3",
        }
        usb["fingerprint"] = MIDI.canonical_sha256(usb)
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            clients_path = root / "clients"
            clients_path.write_text(clients, encoding="utf-8")
            proc_root = root / "asound"
            for card in (2, 4):
                (proc_root / f"card{card}").mkdir(parents=True)
                (proc_root / f"card{card}" / "midi0").write_text(
                    "Roland Digital Piano\n", encoding="utf-8"
                )
            with mock.patch.object(MIDI, "_usb_identity_for_card", return_value=usb):
                with self.assertRaisesRegex(MIDI.MidiCaptureError, "multiple USB cards"):
                    MIDI.discover_unique_roland_port(
                        arecordmidi_listing=listing,
                        clients_path=clients_path,
                        sound_class_root=root / "sound",
                        proc_asound_root=proc_root,
                    )


if __name__ == "__main__":
    unittest.main()
