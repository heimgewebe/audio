"""Contract tests for the iPad/PWA v1 surface.

They prove the specified boundary of the static surface: install metadata,
fail-closed feature detection without user-agent evaluation, a local-device
mode that issues no backend request, a service worker that caches only a fixed
static app shell and treats same-origin ``/api/`` as strictly network-only,
and a preserved loopback and microphone-deny boundary.

They prove nothing about a physical iPad, a remote transport or any audio or
MIDI hardware.
"""

import hashlib
import http.client
import importlib.util
import json
import pathlib
import re
import struct
import sys
import threading
import unittest

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
CONTRACT_PATH = ROOT / "inventory" / "audiozentrale-ipad-pwa.v1.json"
SCHEMA_PATH = ROOT / "schemas" / "audiozentrale-ipad-pwa.v1.schema.json"

SPEC = importlib.util.spec_from_file_location(
    "audio_control_ipad_pwa", ROOT / "scripts" / "audio_control.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def read(relative: str) -> str:
    return (UI / relative).read_text(encoding="utf-8")


def png_dimensions(payload: bytes) -> tuple[int, int]:
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG signature")
    if payload[12:16] != b"IHDR":
        raise AssertionError("first chunk is not IHDR")
    width, height = struct.unpack(">II", payload[16:24])
    return width, height


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_contract_validates_against_its_bound_schema(self):
        jsonschema.Draft202012Validator.check_schema(self.schema)
        jsonschema.Draft202012Validator(self.schema).validate(self.contract)
        binding = self.contract["schema_binding"]
        self.assertEqual(binding["path"], "schemas/audiozentrale-ipad-pwa.v1.schema.json")
        self.assertEqual(
            binding["sha256"],
            hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
        )

    def test_physical_acceptance_is_false_everywhere(self):
        acceptance = self.contract["physical_acceptance"]
        self.assertEqual(
            set(acceptance),
            {
                "ipad_installation_verified",
                "ipad_standalone_launch_verified",
                "offline_app_shell_verified",
                "remote_bridge_verified",
                "local_audio_hardware_verified",
                "local_midi_hardware_verified",
            },
        )
        for name, value in acceptance.items():
            with self.subTest(field=name):
                self.assertIs(value, False)

    def test_contract_records_two_modes_with_separated_authority(self):
        modes = self.contract["runtime_modes"]
        self.assertEqual(modes["default"], "remote-audiozentrale")
        self.assertEqual(modes["storage_key"], "audio-ui-runtime-mode")
        self.assertEqual(set(modes["modes"]), {"local-device", "remote-audiozentrale"})
        local = modes["modes"]["local-device"]
        remote = modes["modes"]["remote-audiozentrale"]
        self.assertIs(local["backend_requests_allowed"], False)
        self.assertEqual(local["backend_authority"], "none")
        self.assertIs(remote["backend_requests_allowed"], True)
        self.assertEqual(remote["backend_authority"], "heim-pc-backend")
        for mode in (local, remote):
            self.assertIs(mode["native_hardware_authority"], False)
        self.assertTrue(
            any("private Tailscale Serve" in item for item in remote["requires"])
        )
        self.assertTrue(
            any("identity-bound short-lived bridge session" in item for item in remote["requires"])
        )
        self.assertIn("Buckelwal start/mode/stop", remote["boundary"])
        self.assertIn("recorder plan/start/stop/recover", remote["boundary"])
        self.assertIn("recording-library categorize/trash/restore", remote["boundary"])
        self.assertIn("profiles, routing, devices and system effects remain remote-denied", remote["boundary"])

    def test_contract_records_loopback_is_not_a_remote_transport(self):
        transport = self.contract["transport"]
        self.assertIs(transport["loopback_control_service_is_remote_transport"], False)
        self.assertIs(transport["loopback_boundary_preserved"], True)
        self.assertIs(transport["remote_bridge_proven"], False)
        self.assertEqual(transport["microphone_permissions_policy"], "denied")
        self.assertEqual(transport["api_cache_control"], "no-store")

    def test_contract_records_feature_detection_policy(self):
        detection = self.contract["feature_detection"]
        self.assertEqual(detection["method"], "api-presence-only")
        self.assertIs(detection["user_agent_sniffing"], False)
        self.assertIs(detection["automatic_permission_requests"], False)
        self.assertIs(detection["fail_closed"], True)
        self.assertIs(detection["distinguishes_api_presence_from_proof"], True)
        probes = {probe["id"] for probe in detection["probes"]}
        self.assertEqual(
            probes,
            {
                "secure-context",
                "service-worker",
                "web-audio",
                "media-capture",
                "web-midi",
            },
        )
        for probe in detection["probes"]:
            with self.subTest(probe=probe["id"]):
                self.assertIs(probe["invoked_automatically"], False)

    def test_contract_records_network_only_api_rule(self):
        worker = self.contract["service_worker"]
        self.assertEqual(worker["route"], "/sw.js")
        self.assertEqual(worker["registration"], "secure-contexts-only")
        self.assertEqual(worker["cache_prefix"], "audiozentrale-app-shell-")
        self.assertTrue(worker["cache_name"].startswith(worker["cache_prefix"]))
        rule = worker["api_rule"]
        self.assertEqual(rule["path_prefix"], "/api/")
        self.assertEqual(rule["behaviour"], "network-only")
        for flag in (
            "cache_read",
            "cache_write",
            "cache_fallback",
            "replay",
            "queue",
            "background_sync",
            "stale_state",
        ):
            with self.subTest(flag=flag):
                self.assertIs(rule[flag], False)
        self.assertNotIn(
            True,
            [route.startswith("/api") for route in worker["app_shell_routes"]],
        )
        self.assertFalse([route for route in worker["app_shell_routes"] if ".wav" in route])

    def test_contract_declares_no_second_app_and_no_framework(self):
        surface = self.contract["surface"]
        self.assertEqual(surface["frontend_framework_dependencies"], [])
        self.assertIs(surface["second_application_created"], False)
        for relative in surface["canonical_frontend"] + surface["added_assets"]:
            with self.subTest(path=relative):
                self.assertTrue((ROOT / relative).is_file())

    def test_contract_binds_icon_hashes(self):
        for icon in self.contract["install_metadata"]["icons"]:
            with self.subTest(icon=icon["path"]):
                payload = (ROOT / icon["path"]).read_bytes()
                self.assertEqual(icon["sha256"], hashlib.sha256(payload).hexdigest())
                width, height = png_dimensions(payload)
                self.assertEqual((width, height), (icon["pixel_size"],) * 2)


class InstallMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = read("index.html")
        self.manifest = json.loads(read("manifest.webmanifest"))

    def test_index_declares_standalone_and_apple_metadata(self):
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', self.html)
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', self.html)
        self.assertIn('name="mobile-web-app-capable" content="yes"', self.html)
        self.assertIn('name="apple-mobile-web-app-title" content="Audiozentrale"', self.html)
        self.assertIn(
            'name="apple-mobile-web-app-status-bar-style" content="black-translucent"',
            self.html,
        )
        self.assertIn('rel="apple-touch-icon" sizes="180x180" href="/icon-180.png"', self.html)
        self.assertIn(
            'name="audiozentrale-ipad-pwa-contract" content="audiozentrale-ipad-pwa-v1"',
            self.html,
        )

    def test_index_uses_viewport_fit_cover(self):
        match = re.search(r'<meta name="viewport" content="([^"]+)">', self.html)
        self.assertIsNotNone(match)
        content = match.group(1)
        self.assertIn("width=device-width", content)
        self.assertIn("viewport-fit=cover", content)

    def test_manifest_is_standards_compliant_and_standalone(self):
        self.assertEqual(self.manifest["start_url"], "/")
        self.assertEqual(self.manifest["scope"], "/")
        self.assertEqual(self.manifest["id"], "/")
        self.assertEqual(self.manifest["display"], "standalone")
        self.assertEqual(self.manifest["name"], "Audiozentrale")
        self.assertEqual(self.manifest["theme_color"], "#0d1414")
        self.assertEqual(self.manifest["background_color"], "#0b1111")
        self.assertIs(self.manifest["prefer_related_applications"], False)
        sizes = {icon["sizes"] for icon in self.manifest["icons"]}
        self.assertEqual(sizes, {"192x192", "512x512"})
        purposes = {icon["purpose"] for icon in self.manifest["icons"]}
        self.assertIn("maskable", purposes)
        for icon in self.manifest["icons"]:
            with self.subTest(icon=icon["src"]):
                self.assertEqual(icon["type"], "image/png")
                self.assertTrue((UI / icon["src"].lstrip("/")).is_file())

    def test_app_icons_exist_in_all_required_sizes(self):
        for size in (180, 192, 512):
            with self.subTest(size=size):
                payload = (UI / f"icon-{size}.png").read_bytes()
                self.assertEqual(png_dimensions(payload), (size, size))

    def test_surface_adds_no_external_dependency(self):
        for relative in ("index.html", "app.js", "sw.js", "manifest.webmanifest"):
            with self.subTest(file=relative):
                source = read(relative)
                self.assertNotIn("http://", source.replace("http://127.0.0.1", ""))
                self.assertNotIn("https://", source)
                self.assertNotIn("cdn", source.lower())


class RuntimeModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = read("app.js")
        self.html = read("index.html")

    def test_default_home_start_disables_browser_scroll_restoration(self):
        self.assertIn('window.history.scrollRestoration = isHomeStartRoute() ? "manual" : "auto";', self.app)
        self.assertIn('window.addEventListener("pageshow"', self.app)
        self.assertIn("restoreHomeStartPosition();", self.app)
        self.assertIn('routeFromHash() === "home" && !ROUTE_TARGETS[routeKey]', self.app)
        self.assertIn('route: "home"', self.app)
        self.assertIn('window.requestAnimationFrame(restoreHomeStartPosition)', self.app)
        self.assertIn('document.body.classList.add("workspace-focus-open")', self.app)
        self.assertIn('panel.classList.add("is-workspace-focused")', self.app)

    def test_modes_are_declared_and_persisted_locally(self):
        self.assertIn('const REMOTE_MODE = "remote-audiozentrale";', self.app)
        self.assertIn('const LOCAL_MODE = "local-device";', self.app)
        self.assertIn("const DEFAULT_RUNTIME_MODE = REMOTE_MODE;", self.app)
        self.assertIn(
            'const RUNTIME_MODE_STORAGE_KEY = "audio-ui-runtime-mode";', self.app
        )
        self.assertIn("window.localStorage.getItem(RUNTIME_MODE_STORAGE_KEY)", self.app)
        self.assertIn("savePreference(RUNTIME_MODE_STORAGE_KEY", self.app)

    def test_mode_choice_is_reachable_and_labelled_in_the_surface(self):
        self.assertIn('id="runtime-mode-remote"', self.html)
        self.assertIn('id="runtime-mode-local"', self.html)
        self.assertIn('value="remote-audiozentrale"', self.html)
        self.assertIn('value="local-device"', self.html)
        self.assertIn('aria-describedby="runtime-mode-remote-description"', self.html)
        self.assertIn('aria-describedby="runtime-mode-local-description"', self.html)
        self.assertIn("<legend class=\"sr-only\">Laufzeitmodus wählen</legend>", self.html)

    def test_remote_mode_requires_scoped_bridge_and_keeps_loopback_boundary(self):
        self.assertIn("kein Ferntransport", self.html)
        self.assertIn("Loopback-only", self.app)
        self.assertIn('response.headers.get("X-Audio-Remote-Bridge")', self.app)
        self.assertIn('bridgeMarker === "read-only-v1"', self.app)
        self.assertIn('bridgeMarker === "whale-action-v1"', self.app)
        self.assertIn('bridgeMarker === "recording-action-v1"', self.app)
        self.assertIn('response.headers.get("X-Audio-Remote-Effects") === "whale-v1"', self.app)
        self.assertIn('fetchJson("/bridge/v1/session"', self.app)
        session_loader = self.app.split('fetchJson("/bridge/v1/session"', 1)[1].split("});", 1)[0]
        self.assertIn('method: "POST"', session_loader)
        self.assertIn('body: "{}"', session_loader)
        self.assertIn('fetchJson("/bridge/v1/actions/whale"', self.app)
        self.assertIn('fetchJson("/bridge/v1/actions/recording"', self.app)
        self.assertIn('fetchJson("/api/v1/recordings"', self.app)
        self.assertIn("audio.src = item.audio_url", self.app)
        self.assertIn('"X-Audio-Bridge-Session": state.remoteWhaleSessionToken', self.app)
        self.assertIn("Recorderaktionen dürfen wirken", self.app)

    def test_local_mode_denies_native_hardware_authority_in_the_surface(self):
        for token in ("MOTU", "ALSA", "PipeWire", "Roland"):
            with self.subTest(token=token):
                self.assertIn(token, self.html)
        self.assertIn("keine native Autorität", self.html)


class RecordingMutationBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = read("app.js")

    def test_recording_actions_require_direct_http_loopback_origin(self):
        helper = self.app.split("function directLoopbackControlOrigin() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn('window.location.protocol === "http:"', helper)
        self.assertIn('window.location.hostname === "127.0.0.1"', helper)
        self.assertIn('window.location.hostname === "localhost"', helper)
        for broadened in ("https:", "::1", "endsWith", "isSecureContext"):
            with self.subTest(token=broadened):
                self.assertNotIn(broadened, helper)

        local_gate = self.app.split("function localRecordingActionsAllowed() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("directLoopbackControlOrigin() &&", local_gate)
        self.assertIn("backendAllowed() &&", local_gate)
        self.assertIn("state.remoteBridgeProjection !== true", local_gate)
        self.assertIn("recording_control === true", local_gate)
        self.assertIn("recording?.actionable === true", local_gate)
        self.assertIn("action_token.length >= 16", local_gate)

        remote_gate = self.app.split("function remoteRecordingActionsAllowed() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("remoteWhaleSessionFresh()", remote_gate)
        self.assertIn('state.remoteActionScopes.includes("recording")', remote_gate)
        self.assertNotIn("action_token", remote_gate)

    def test_remote_bridge_observation_stays_sticky_for_page_lifetime(self):
        fetch_block = self.app.split("async function fetchJson(url, options = {}) {", 1)[1].split(
            "\nfunction showNotice", 1
        )[0]
        self.assertIn('const bridgeMarker = response.headers.get("X-Audio-Remote-Bridge")', fetch_block)
        self.assertIn('bridgeMarker === "read-only-v1"', fetch_block)
        self.assertIn('bridgeMarker === "whale-action-v1"', fetch_block)
        self.assertIn('bridgeMarker === "recording-action-v1"', fetch_block)
        self.assertIn("state.remoteBridgeProjection = true;", fetch_block)
        self.assertIn('response.headers.get("X-Audio-Remote-Effects") === "whale-v1"', fetch_block)
        self.assertIn("state.remoteWhaleActionObserved = true;", fetch_block)
        self.assertIn("else if (state.remoteBridgeProjection === null)", fetch_block)
        self.assertIn("state.remoteBridgeProjection = false;", fetch_block)
        self.assertLess(
            fetch_block.index("state.remoteBridgeProjection = true;"),
            fetch_block.index("state.remoteBridgeProjection = false;"),
        )

    def test_recording_post_rechecks_the_central_mutation_gate(self):
        post = self.app.split("async function postRecordingAction(payload) {", 1)[1].split(
            "\nasync function runRecordingAction", 1
        )[0]
        self.assertIn("RECORDING_LIBRARY_ACTIONS.has(payload?.operation)", post)
        self.assertIn("recordingLibraryActionsAllowed()", post)
        self.assertIn("recordingActionsAllowed()", post)
        self.assertIn("if (!allowed)", post)
        self.assertIn('return fetchJson("/api/v1/actions/recording"', post)
        self.assertIn('return fetchJson("/bridge/v1/actions/recording"', post)
        self.assertIn('"X-Audio-Bridge-Session"', post)
        self.assertLess(
            post.index("if (!allowed)"),
            post.index('return fetchJson("/api/v1/actions/recording"'),
        )

    def test_whale_and_recording_actions_gain_scoped_remote_effect_authority(self):
        local_gate = self.app.split("function localWhaleActionsAllowed() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("directLoopbackControlOrigin() &&", local_gate)
        self.assertIn("state.remoteBridgeProjection !== true", local_gate)
        self.assertIn("action_token.length >= 16", local_gate)

        remote_gate = self.app.split("function remoteWhaleActionsAllowed() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("remoteWhaleSessionFresh()", remote_gate)
        self.assertIn("whale_control === true", remote_gate)
        self.assertNotIn("action_token", remote_gate)

        router = self.app.split("async function postWhaleAction(payload) {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn('fetchJson("/api/v1/actions/whale"', router)
        self.assertIn('fetchJson("/bridge/v1/actions/whale"', router)
        self.assertIn('"X-Audio-Bridge-Session"', router)
        self.assertNotIn("/api/v1/actions/recording", router)

        local_recorder_gate = self.app.split("function localRecordingActionsAllowed() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("directLoopbackControlOrigin() &&", local_recorder_gate)
        self.assertIn("state.remoteBridgeProjection !== true", local_recorder_gate)

        remote_recorder_gate = self.app.split("function remoteRecordingActionsAllowed() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("remoteWhaleSessionFresh()", remote_recorder_gate)
        self.assertIn('state.remoteActionScopes.includes("recording")', remote_recorder_gate)
        self.assertNotIn("action_token", remote_recorder_gate)

        recorder_router = self.app.split("async function postRecordingAction(payload) {", 1)[1].split(
            "\nasync function runRecordingAction", 1
        )[0]
        self.assertIn('/api/v1/actions/recording', recorder_router)
        self.assertIn('/bridge/v1/actions/recording', recorder_router)
        self.assertIn('"X-Audio-Bridge-Session"', recorder_router)

    def test_performance_hint_never_blocks_planning_or_substitutes_for_a_plan(self):
        controls = self.app.split("function renderRecordingControls(", 1)[1].split(
            "\nasync function loadRecordingLibrary", 1
        )[0]
        plan_disable = controls.split("planButton.disabled =", 1)[1].split(
            ";", 1
        )[0]
        start_disable = controls.split("startButton.disabled =", 1)[1].split(
            ";", 1
        )[0]
        self.assertIn("!writable", plan_disable)
        self.assertNotIn("actionable", plan_disable)
        self.assertNotIn("selectedRecordingModeActionable", self.app)
        self.assertNotIn("recordingPlanMatchesDraft()", start_disable)
        self.assertIn("runRecordingStart();", controls)
        self.assertIn("await requestRecordingPlan({ autoRenameCollision: true })", self.app)
        self.assertIn("RECORDING_COLLISION_BLOCKERS", self.app)
        self.assertIn("const attemptedNames = new Set();", self.app)
        self.assertIn("attemptedNames.add(input.name);", self.app)
        self.assertIn("nextAutomaticTakeName(input.mode, attemptedNames)", self.app)
        self.assertIn("modeButton.disabled = active || state.recordingActionPending", controls)
        mode_change = controls.split('modeButton.addEventListener("click"', 1)[1].split(
            "});", 1
        )[0]
        self.assertIn("state.recordingPlan = null", mode_change)
        self.assertIn("state.recordingPlanInput = null", mode_change)

        binding = self.app.split("function recordingPlanBindsDraft()", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("input?.mode === state.recordingDraft.mode", binding)
        self.assertIn("plan.mode === state.recordingDraft.mode", binding)
        matcher = self.app.split("function recordingPlanMatchesDraft()", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("recordingPlanBindsDraft()", matcher)
        self.assertIn("state.recordingPlan?.ready === true", matcher)


class FeatureDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = read("app.js")
        self.worker = read("sw.js")

    def test_detection_never_evaluates_the_user_agent(self):
        for token in (
            "userAgent",
            "userAgentData",
            "navigator.platform",
            "navigator.vendor",
            "appVersion",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, self.app)
                self.assertNotIn(token, self.worker)

    def test_permission_and_device_apis_are_never_invoked(self):
        for pattern in (
            r"getUserMedia\s*\(",
            r"requestMIDIAccess\s*\(",
            r"new\s+AudioContext",
            r"navigator\.permissions\.query",
        ):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, self.app), pattern)

    def test_media_playback_is_only_user_initiated_take_playback(self):
        capability_block = self.app.split("function detectCapabilities() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIsNone(re.search(r"\.play\s*\(\s*\)", capability_block))
        self.assertEqual(len(re.findall(r"\.play\s*\(\s*\)", self.app)), 1)
        playback = self.app.split("function playRecordingTake(item, trigger = null) {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("const playback = audio.play();", playback)
        listener = self.app.split("function appendTakeListenButton(parent, item) {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn('listen.addEventListener("click", (event) =>', listener)
        self.assertIn("playRecordingTake(item, event.currentTarget)", listener)

    def test_detection_probes_every_required_capability(self):
        self.assertIn("window.isSecureContext === true", self.app)
        self.assertIn('typeof window.AudioContext === "function"', self.app)
        self.assertIn(
            'typeof navigator.mediaDevices?.getUserMedia === "function"', self.app
        )
        self.assertIn('typeof navigator.requestMIDIAccess === "function"', self.app)
        self.assertIn('"serviceWorker" in navigator', self.app)
        self.assertIn("permissionsPolicyState", self.app)
        self.assertIn('permissionsPolicyState("microphone")', self.app)

    def test_detection_is_fail_closed(self):
        block = self.app.split("function capabilityState(probe) {", 1)[1].split("\n}", 1)[0]
        self.assertIn('return probe() ? "present" : "absent";', block)
        self.assertIn("} catch (_error) {", block)
        self.assertIn('return "unknown";', block)

    def test_surface_separates_api_presence_from_proof(self):
        self.assertIn('present: "Schnittstelle vorhanden"', self.app)
        self.assertIn("unbelegt", self.app)
        self.assertIn("Keine Anfrage gestellt, keine Aufnahme belegt.", self.app)


class LocalModeBackendSuppressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = read("app.js")

    def test_runtime_bootstrap_installs_a_same_origin_api_fetch_guard(self):
        self.assertIn("function installBackendFetchGuard()", self.app)
        self.assertIn("window.fetch = function guardedFetch(input, init)", self.app)
        self.assertIn("!backendAllowed() && sameOriginApiTarget(input)", self.app)
        self.assertIn("Promise.reject(new Error(LOCAL_MODE_API_BLOCK_MESSAGE))", self.app)
        bootstrap = self.app.rsplit("\n}\n", 1)[-1]
        self.assertIn("installBackendFetchGuard();", bootstrap)

    def test_api_target_detection_is_same_origin_and_fail_closed(self):
        block = self.app.split("function sameOriginApiTarget(input) {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("target.origin !== window.location.origin", block)
        self.assertIn('target.pathname.startsWith("/api/")', block)
        self.assertIn('target.pathname.startsWith("/bridge/v1/")', block)
        self.assertIn("return true;", block)

    def test_every_backend_reader_is_guarded(self):
        guarded = (
            "async function fetchJson(url, options = {}) {\n"
            "  if (!backendAllowed() && sameOriginApiTarget(url)) {",
            "async function refreshSnapshot(force = false) {\n"
            "  if (\n"
            "    state.loading ||\n"
            "    state.recordingActionPending ||\n"
            "    state.dauersongActionPending ||\n"
            "    state.operatingModeActionPending ||\n"
            "    state.whaleActionPending ||\n"
            "    !backendAllowed()\n"
            "  ) return;",
            "async function loadReplay() {\n  if (!backendAllowed()) return;",
            "function requestTelemetry() {\n  if (!backendAllowed()) return Promise.resolve();",
            "async function telemetryPollTick() {\n"
            "  state.telemetryTimer = null;\n"
            "  if (!backendAllowed()) return;",
            "async function openProfilePlan(profile, trigger) {\n"
            "  if (!backendAllowed()) return;",
            "function autoRefreshTick() {\n  if (!backendAllowed()) return;",
            "async function ensureRemoteWhaleSession({ force = false } = {}) {\n"
            "  if (state.remoteBridgeProjection !== true || !backendAllowed()) return false;",
        )
        for fragment in guarded:
            with self.subTest(fragment=fragment.splitlines()[0]):
                self.assertIn(fragment, self.app)

    def test_no_timer_is_scheduled_without_backend_authority(self):
        schedule = self.app.split("function scheduleAutoRefresh() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("if (!backendAllowed()) return;", schedule)
        polling = self.app.split(
            "function scheduleTelemetryPolling(delayMs = TELEMETRY_POLL_MS) {", 1
        )[1].split("\n}", 1)[0]
        self.assertIn("document.hidden || !backendAllowed()", polling)

    def test_local_mode_hides_backend_bound_surfaces(self):
        styles = read("styles.css")
        self.assertIn(
            'html[data-runtime-mode="local-device"] .depth-panel:not(.runtime-mode-panel)',
            styles,
        )
        self.assertIn("function stopRemoteActivity()", self.app)
        self.assertIn("state.snapshot = null;", self.app)
        self.assertIn('byId("local-device-boundary").hidden = backendAllowed();', self.app)


class ServiceWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = read("sw.js")
        self.app = read("app.js")

    def test_registration_happens_only_in_secure_contexts(self):
        block = self.app.split("function registerServiceWorker() {", 1)[1].split(
            "\nasync function", 1
        )[0]
        self.assertIn('if (!("serviceWorker" in navigator))', block)
        self.assertIn("if (window.isSecureContext !== true)", block)
        secure_gate = block.index("window.isSecureContext !== true")
        registration = block.index('navigator.serviceWorker\n    .register("/sw.js"')
        self.assertLess(secure_gate, registration)

    def test_app_shell_cache_is_a_fixed_static_list(self):
        block = self.worker.split("const APP_SHELL = Object.freeze([", 1)[1].split("]);", 1)[0]
        entries = re.findall(r'"([^"]+)"', block)
        self.assertEqual(
            entries,
            [
                "/",
                "/index.html",
                "/app.js",
                "/whale-lesson.js",
                "/styles.css",
                "/manifest.webmanifest",
                "/icon-180.png",
                "/icon-192.png",
                "/icon-512.png",
            ],
        )
        for entry in entries:
            with self.subTest(entry=entry):
                self.assertFalse(entry.startswith("/api"))
                self.assertFalse(entry.endswith(".wav"))
                self.assertNotEqual(entry, "/sw.js")

    def test_api_requests_are_strictly_network_only(self):
        branch = self.worker.split("if (isApiPath(url.pathname)) {", 1)[1].split(
            "return;", 1
        )[0]
        self.assertIn("event.respondWith(fetch(request));", branch)
        for forbidden in ("caches", "cache", "catch", "match", "put"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, branch)

    def test_bridge_requests_are_strictly_network_only(self):
        self.assertIn("function isBridgePath(pathname)", self.worker)
        branch = self.worker.split("if (isBridgePath(url.pathname)) {", 1)[1].split(
            "return;", 1
        )[0]
        self.assertIn("event.respondWith(fetch(request));", branch)
        for forbidden in ("caches", "cache", "catch", "match", "put"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, branch)

    def test_non_get_and_cross_origin_traffic_keeps_normal_behaviour(self):
        handler = self.worker.split('self.addEventListener("fetch", (event) => {', 1)[1]
        self.assertIn('if (request.method !== "GET") return;', handler)
        self.assertIn("if (url.origin !== self.location.origin) return;", handler)
        method_gate = handler.index('request.method !== "GET"')
        api_gate = handler.index("isApiPath(url.pathname)")
        self.assertLess(method_gate, api_gate)

    def test_only_basic_successful_responses_enter_the_cache(self):
        block = self.worker.split("function isCacheableShellResponse(response) {", 1)[
            1
        ].split("\n}", 1)[0]
        self.assertIn("response.status === 200", block)
        self.assertIn('response.type === "basic"', block)

    def test_worker_declares_no_replay_queue_or_background_capability(self):
        listeners = set(re.findall(r'addEventListener\(\s*"([a-z]+)"', self.worker))
        self.assertEqual(listeners, {"install", "activate", "fetch"})
        for forbidden in (
            "backgroundFetch",
            "registerSync",
            "SyncManager",
            "showNotification",
            "IndexedDB",
            "indexedDB",
        ):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, self.worker)

    def test_activation_removes_only_owned_stale_caches_and_claims_clients(self):
        block = self.worker.split('self.addEventListener("activate", (event) => {', 1)[1]
        self.assertIn("caches.keys()", block)
        self.assertIn('const CACHE_PREFIX = "audiozentrale-app-shell-";', self.worker)
        self.assertIn("name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME", block)
        self.assertNotIn(".filter((name) => name !== CACHE_NAME)", block)
        self.assertIn("caches.delete(name)", block)
        self.assertIn("self.clients.claim()", block)
        self.assertIn("self.skipWaiting()", self.worker)

    def test_controller_change_never_forces_an_automatic_reload(self):
        self.assertNotIn("location.reload", self.app)
        self.assertIn("const hadController = Boolean(navigator.serviceWorker.controller)", self.app)
        self.assertIn("Neuladen empfohlen", self.app)


class ControlServiceBoundaryTests(unittest.TestCase):
    def test_new_pwa_assets_are_allowlisted_with_correct_types(self):
        expected = {
            "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
            "/manifest.webmanifest": (
                "manifest.webmanifest",
                "application/manifest+json",
            ),
            "/icon-180.png": ("icon-180.png", "image/png"),
            "/icon-192.png": ("icon-192.png", "image/png"),
            "/icon-512.png": ("icon-512.png", "image/png"),
        }
        for route, entry in expected.items():
            with self.subTest(route=route):
                self.assertEqual(MODULE.STATIC_FILES[route], entry)

    def test_static_allowlist_stays_closed(self):
        for filename in ("../README.md", "sw.js.map", "service-worker.js"):
            with self.subTest(filename=filename):
                with self.assertRaises(MODULE.ControlError):
                    MODULE.read_static_file(filename)

    def test_every_allowlisted_ui_file_is_readable(self):
        for entry in MODULE.STATIC_FILES.values():
            with self.subTest(filename=entry[0]):
                self.assertTrue(MODULE.read_static_file(entry[0]))

    def test_csp_allows_a_same_origin_worker_and_nothing_broader(self):
        source = (ROOT / "scripts" / "audio_control.py").read_text(encoding="utf-8")
        self.assertIn("worker-src 'self'", source)
        self.assertNotIn("worker-src 'none'", source)
        self.assertIn("manifest-src 'self'", source)
        for directive in (
            "default-src 'self'",
            "script-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "media-src 'self'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        ):
            with self.subTest(directive=directive):
                self.assertIn(directive, source)
        for broadened in ("'unsafe-inline'", "'unsafe-eval'", "connect-src *", "worker-src *"):
            with self.subTest(token=broadened):
                self.assertNotIn(broadened, source)

    def test_microphone_stays_denied_and_api_stays_no_store(self):
        source = (ROOT / "scripts" / "audio_control.py").read_text(encoding="utf-8")
        self.assertIn(
            "camera=(), microphone=(), geolocation=(), display-capture=()", source
        )
        self.assertIn('cache_control: str = "no-store"', source)

    def test_loopback_boundary_is_unchanged(self):
        self.assertEqual(MODULE.DEFAULT_HOST, "127.0.0.1")
        self.assertTrue(MODULE.is_loopback_host("127.0.0.1"))
        self.assertFalse(MODULE.is_loopback_host("192.168.1.10"))
        self.assertFalse(MODULE.request_host_is_local("audio.example:8765", 8765))
        self.assertTrue(MODULE.request_host_is_local("127.0.0.1:8765", 8765))
        self.assertFalse(MODULE.origin_is_local("https://audio.example", 8765))


class InertRunner:
    """Static asset serving must not shell out at all."""

    def run(self, argv, *, timeout):
        raise AssertionError(f"unexpected subprocess during static serving: {argv}")


class ServedAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = MODULE.AudioControl(
            runner=InertRunner(),
            action_token="ipad-pwa-token",
            host="127.0.0.1",
            port=0,
            cache_seconds=0,
        )
        try:
            self.server = MODULE.AudioControlHTTPServer(
                ("127.0.0.1", 0), self.controller
            )
        except PermissionError as error:  # pragma: no cover - sandbox dependent
            self.skipTest(f"Loopback-Sockets sind in dieser Sandbox gesperrt: {error}")
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def get(self, path: str, *, host: str | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Host": host} if host else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    def test_pwa_assets_are_served_with_the_declared_types(self):
        expected = {
            "/sw.js": "application/javascript; charset=utf-8",
            "/manifest.webmanifest": "application/manifest+json",
            "/icon-180.png": "image/png",
            "/icon-192.png": "image/png",
            "/icon-512.png": "image/png",
        }
        for route, content_type in expected.items():
            with self.subTest(route=route):
                status, headers, payload = self.get(route)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], content_type)
                self.assertTrue(payload)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("worker-src 'self'", headers["Content-Security-Policy"])
                self.assertIn(
                    "microphone=()",
                    headers["Permissions-Policy"],
                )

    def test_manifest_response_parses_as_a_standalone_manifest(self):
        _status, _headers, payload = self.get("/manifest.webmanifest")
        manifest = json.loads(payload)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["scope"], "/")

    def test_worker_and_manifest_stay_behind_the_loopback_host_gate(self):
        for route in ("/sw.js", "/manifest.webmanifest", "/icon-512.png"):
            with self.subTest(route=route):
                status, _headers, _payload = self.get(route, host="audio.example")
                self.assertEqual(status, 421)

    def test_service_worker_route_is_root_scoped_and_unmapped_paths_stay_closed(self):
        status, _headers, _payload = self.get("/ui/sw.js")
        self.assertEqual(status, 404)
        status, _headers, _payload = self.get("/service-worker.js")
        self.assertEqual(status, 404)


class TouchAndSafeAreaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.styles = read("styles.css")

    def test_safe_area_insets_are_declared_and_applied(self):
        for variable in ("--safe-top", "--safe-right", "--safe-bottom", "--safe-left"):
            with self.subTest(variable=variable):
                self.assertIn(f"{variable}: env(safe-area-inset-", self.styles)
        self.assertIn("padding-top: calc(12px + var(--safe-top));", self.styles)
        self.assertIn("padding-bottom: calc(30px + var(--safe-bottom));", self.styles)
        self.assertIn("padding-right: calc(20px + var(--safe-right));", self.styles)
        self.assertIn("padding-top: calc(10px + var(--safe-top));", self.styles)

    def test_coarse_pointer_targets_reach_forty_four_pixels(self):
        block = self.styles.split("@media (pointer: coarse) {", 1)[1]
        self.assertIn("min-height: 44px;", block)
        self.assertIn("min-width: 44px;", block)
        for selector in ("button,", "select,", ".primary-nav a,", ".runtime-mode-option"):
            with self.subTest(selector=selector):
                self.assertIn(selector, block)
        self.assertIn(".icon-button,\n  .dialog-close {\n    width: 44px;\n    height: 44px;", block)
        self.assertIn("height: 44px;", block.split('input[role="switch"] {', 1)[1])

    def test_phone_navigation_keeps_labels_visible(self):
        phone = self.styles.split("@media (max-width: 620px) {", 1)[-1].split("/* Listening topology v3", 1)[0]
        self.assertIn(".primary-nav a > span:not(.nav-icon):not(.nav-badge)", phone)
        self.assertIn("display: inline;", phone)
        self.assertIn("min-height: 44px;", phone)

    def test_coarse_pointer_rules_never_shrink_existing_targets(self):
        block = self.styles.split("@media (pointer: coarse) {", 1)[1]
        brand = block.split(".brand {", 1)[1].split("}", 1)[0]
        self.assertIn("min-height: 48px;", brand)
        row = block.split(".setting-row {", 1)[1].split("}", 1)[0]
        self.assertIn("min-height: 56px;", row)


if __name__ == "__main__":
    unittest.main()
