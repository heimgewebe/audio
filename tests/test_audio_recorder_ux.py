import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class AudioRecorderUXTests(unittest.TestCase):
    def setUp(self):
        self.app = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.styles = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")

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


    def test_take_playback_uses_one_persistent_native_player(self):
        self.assertIn('id="global-take-player"', self.html)
        self.assertIn('id="global-take-player-audio"', self.html)
        self.assertIn('</main>\n\n      <section\n        class="global-take-player"', self.html)
        self.assertIn('</section>\n    </div>\n\n    <div class="dialog-backdrop"', self.html)
        self.assertIn('class="recording-player"', self.html)
        self.assertEqual(self.html.count('class="recording-player"'), 1)
        self.assertNotIn('element("audio", "recording-player")', self.app)
        self.assertEqual(self.app.count("appendTakeListenButton(card, item);"), 2)
        self.assertIn('"Anhören"', self.app)
        self.assertIn("function playRecordingTake(item, trigger = null)", self.app)

    def test_global_player_reuses_existing_playback_and_refresh_contracts(self):
        self.assertIn("function recordingPlaybackActive()", self.app)
        self.assertIn('document.querySelectorAll("audio.recording-player")', self.app)
        self.assertIn("function clearGlobalTakePlayer({ restoreFocus = false } = {})", self.app)
        self.assertIn('byId("global-take-player-close").addEventListener', self.app)
        self.assertIn('byId("global-take-player-audio").setAttribute("aria-label"', self.app)
        self.assertIn('audio.removeAttribute("src")', self.app)
        self.assertIn('audio.removeAttribute("aria-label")', self.app)
        self.assertIn("recordingPlayerReturnFocus: null", self.app)
        self.assertIn("function clearGlobalTakePlayer({ restoreFocus = false } = {})", self.app)
        self.assertIn('!returnFocus.closest("[hidden]")', self.app)
        self.assertIn('byId("main-content")', self.app)
        self.assertIn("returnTarget?.focus({ preventScroll: true })", self.app)
        self.assertIn("clearGlobalTakePlayer({ restoreFocus: true })", self.app)

    def test_global_player_is_cleared_only_on_proven_stale_or_trashed_take(self):
        self.assertIn("function reconcileGlobalTakePlayer()", self.app)
        self.assertIn("state.recordingLibrary.truncated !== true", self.app)
        self.assertIn('current.status !== "completed"', self.app)
        self.assertIn("current.library?.trashed === true", self.app)
        self.assertIn("current.audio_url !== state.recordingPlayerAudioUrl", self.app)
        self.assertIn('result.operation === "trash"', self.app)
        self.assertIn("payload.session_id === state.recordingPlayerSessionId", self.app)

    def test_global_player_reserves_safe_area_without_custom_audio_engine(self):
        self.assertIn("body.take-player-active main", self.styles)
        self.assertIn("var(--safe-bottom)", self.styles)
        self.assertIn(".global-take-player", self.styles)
        self.assertIn(".recording-playback-note", self.styles)
        self.assertNotIn("waveform", self.app.lower())
        self.assertNotIn("audioContext", self.app)


if __name__ == "__main__":
    unittest.main()
