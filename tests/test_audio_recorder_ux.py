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

    def test_live_meter_is_bound_to_recorder_source_and_physical_mic_channel(self):
        self.assertIn('stream.value?.source !== "active-recorder-bound-capture"', self.app)
        self.assertIn("function recordingLevelBinding()", self.app)
        self.assertIn('binding.source.identity_sha256 !== observedSourceSha', self.app)
        self.assertIn('input === "input-1" ? "FL" : input === "input-2" ? "FR" : null', self.app)
        self.assertIn('stream.value.channels_analysis.find', self.app)
        self.assertIn('"front-left,front-right"', self.app)
        self.assertIn('"Pegel nicht an Aufnahme gebunden"', self.app)
        self.assertIn('"Mikrofonkanal nicht bestätigt"', self.app)
        self.assertIn('RØDE · MOTU ${input === "input-1" ? "Input 1" : "Input 2"}', self.app)
        self.assertNotIn('active-pipewire-shared-capture', self.app)
        self.assertNotIn('aggregierter Live-Pegel', self.app)

    def test_preflight_meter_requires_explicit_current_plan_binding(self):
        self.assertIn("function recordingPlanBindsDraft()", self.app)
        self.assertIn('if (recordingPlanBindsDraft() && plan?.source?.bound === true)', self.app)
        self.assertIn('phase: "preflight"', self.app)
        self.assertIn('if (recordingPlanBindsDraft()) {', self.app)
        self.assertIn('"Pegel vor Aufnahme"', self.app)
        self.assertIn('"Recorderplan gebunden · Vorabpegel wird verifiziert"', self.app)
        self.assertIn('"Vorabpegel des an den Recorderplan gebundenen RØDE-Kanals"', self.app)
        self.assertIn('binding.phase === "preflight" ? "Vorabpegel · " : ""', self.app)
        self.assertNotIn('runOperatingModeTransition("recording")', self.app)

    def test_level_acceptance_is_explicit_local_and_discards_measurement_audio(self):
        self.assertIn('async function runRecordingLevelAcceptance()', self.app)
        self.assertIn('"Pegelabnahme starten (10 s)"', self.app)
        self.assertIn('operation: "measure-level"', self.app)
        self.assertIn('!localRecordingActionsAllowed() || state.recordingActionPending', self.app)
        self.assertIn('Die Mess-WAV wird danach verworfen.', self.app)
        block = self.app.split('async function runRecordingLevelAcceptance()', 1)[1].split('function syncRecordingLibraryControls()', 1)[0]
        self.assertNotIn('/bridge/v1/actions/recording', block)

    def test_live_meter_exposes_stale_unavailable_unbound_and_incomplete_states(self):
        for message in (
            "Live-Pegel nicht verfügbar",
            "Pegel veraltet",
            "Pegel startet",
            "Pegel nicht verfügbar",
            "Pegelquelle nicht verifiziert",
            "Pegel nicht an Aufnahme gebunden",
            "Mikrofonkanal nicht bestätigt",
            "Pegel unvollständig",
        ):
            self.assertIn(f'"{message}"', self.app)
        self.assertNotIn('value.textContent = "Pegel wird gelesen"', self.app)

    def test_recorder_surface_reads_recording_operating_mode_without_second_mode_action(self):
        self.assertIn('const operatingRecordingMode = (state.snapshot?.operating_mode?.modes || []).find(', self.app)
        self.assertIn('mode?.id === "recording"', self.app)
        self.assertIn('"Betriebsmodus"', self.app)
        self.assertIn('"recording-preflight-required"', self.app)
        self.assertIn('"recording-recovery-required"', self.app)
        self.assertNotIn('runOperatingModeTransition("recording")', self.app)

    def test_known_mode_blocker_disables_start_without_blocking_automatic_preflight(self):
        self.assertIn("function recordingModeKnownHardBlocked(mode)", self.app)
        self.assertIn('mode.blocker !== "exact-midi-gate-requires-plan"', self.app)
        self.assertIn(
            "startButton.disabled = !writable || state.recordingActionPending || knownModeHardBlocked;",
            self.app,
        )
        self.assertIn('modeBlockerMessage.id = "recording-mode-blocker"', self.app)
        self.assertIn('startButton.setAttribute("aria-describedby", modeBlockerMessage.id)', self.app)
        self.assertIn('selectedMode.blocker === "exact-midi-gate-requires-plan"', self.app)
        self.assertIn('"Automatische Startprüfung"', self.app)

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
