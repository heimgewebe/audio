import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class AudioRecorderUXTests(unittest.TestCase):
    def setUp(self):
        self.app = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")

    def test_recorder_states_are_explicit(self):
        for state in ("idle", "ready", "blocked", "recording", "completed", "recovery-required"):
            self.assertIn(f'return "{state}"', self.app)
        self.assertIn("function recordingPresentationState(", self.app)

    def test_timer_uses_recorder_started_at_and_is_stopped_with_remote_activity(self):
        self.assertIn("function formatRecordingElapsed(", self.app)
        self.assertIn("session?.started_at", self.app)
        self.assertIn('elapsed.id = "recording-running-time"', self.app)
        self.assertIn("window.clearInterval(state.recordingClockTimer)", self.app)
        self.assertIn("state.recordingClockTimer = null", self.app)

    def test_live_meter_is_aggregated_and_never_claims_unbound_hardware(self):
        self.assertIn('stream.value?.source !== "active-pipewire-shared-capture"', self.app)
        self.assertIn('"PipeWire-Standard-Eingang · aggregierter Live-Pegel"', self.app)
        self.assertIn('"PipeWire-Eingang · aggregierter Live-Pegel"', self.app)
        self.assertIn('observation.clipping ? " · Clipping" : ""', self.app)
        self.assertNotIn("MOTU-Eingang · aggregierter Live-Pegel", self.app)
        self.assertNotIn("Stimme · aggregierter Live-Pegel", self.app)
        self.assertNotIn("Klavier · aggregierter Live-Pegel", self.app)

    def test_live_meter_exposes_stale_unavailable_and_unverified_states(self):
        for message in (
            "Live-Pegel nicht verfügbar",
            "Pegel veraltet",
            "Pegel startet",
            "Pegel nicht verfügbar",
            "Pegelquelle nicht verifiziert",
            "Pegel unvollständig",
        ):
            self.assertIn(f'"{message}"', self.app)
        self.assertNotIn('value.textContent = "Pegel wird gelesen"', self.app)

    def test_technical_controls_are_progressive(self):
        self.assertIn('element("details", "recording-advanced")', self.app)
        self.assertIn('"Details und Startprüfung"', self.app)
        self.assertIn('"● Aufnahme starten"', self.app)
        self.assertIn('"■ Aufnahme stoppen"', self.app)
        self.assertIn("session?.recovery_required === true || session?.cleanup_required === true", self.app)


if __name__ == "__main__":
    unittest.main()
