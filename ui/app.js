"use strict";

const ROUTES = {
  home: {
    title: "Home",
    eyebrow: "Arbeitsbereiche",
    description: "Direkter Einstieg in Hören, Aufnehmen und Spielen mit aktuellem System-Readback.",
  },
  hoeren: {
    title: "Hören",
    eyebrow: "Wiedergabe und Referenzweg",
    description: "Desktop und Mobilgeräte können als Quellen dienen; der MOTU M2 ist der zentrale Knoten für Kopfhörer- und Receiverast.",
  },
  aufnehmen: {
    title: "Aufnehmen",
    eyebrow: "Recorder und Takes",
    description: "Gesang und Instrumente mit prüfbaren Start-Gates aufnehmen und Takes direkt weiterverwenden.",
  },
  spielen: {
    title: "Spielen",
    eyebrow: "Instrumente und Klang",
    description: "Roland, Softwareinstrumente und Klangmodelle in einem fokussierten Spielbereich.",
  },
  material: {
    title: "Bibliothek",
    eyebrow: "Aufnahmen, Klänge und Replay",
    description: "Eigene Takes, Klangmaterial und Replay getrennt organisieren und wiederfinden.",
  },
  system: {
    title: "System",
    eyebrow: "Betrieb und Integration",
    description: "Geräte, Wahrheitsebenen, Telemetrie und Deployment als technische Kontrollfläche.",
  },
};

// Alte Links bleiben lesbar, sind aber nicht mehr die sichtbare Informationsarchitektur.
const ROUTE_ALIASES = {
  start: "home",
  now: "home",
  setups: "home",
  library: "material",
  klaenge: "material",
  verbindungen: "system",
  diagnose: "system",
  einstellungen: "system",
};

const ROUTE_TARGETS = {};

const MODE_LABELS = {
  morph: "88-Tasten-Morph",
  organic: "Organischer Wal",
  realistic: "Naturaufnahmen",
  ufo: "UFO-Vergleich",
};

const PROFILE_LABELS = {
  "desktop-mixed": "Desktop gemischt",
  "reference-listening": "Referenz hören",
  "qobuz-exclusive": "Qobuz exklusiv",
  receiver: "Pioneer Receiver",
  "bluetooth-convenience": "Bluetooth komfortabel",
  "voice-recording": "Stimme aufnehmen",
  "piano-digital-recording": "Klavier aufnehmen",
  production: "Produktion",
  "piano-software-live": "Softwareinstrument",
  experimental: "Experimentell",
};

const PROFILE_GLYPHS = {
  "desktop-mixed": "◫",
  "reference-listening": "◖",
  "qobuz-exclusive": "Q",
  receiver: "⌂",
  "bluetooth-convenience": "⌁",
  "voice-recording": "●",
  "piano-digital-recording": "♩",
  production: "◇",
  "piano-software-live": "♬",
  experimental: "≋",
};

const PROFILE_STATE_LABELS = {
  executable: "anwendbar",
  "plan-ready": "Plan bereit",
  onsite: "vor Ort",
  laboratory: "Labor-Gate",
  planned: "geplant",
};

const PHYSICAL_FACT_LABELS = {
  focal_connected_output: "Focal Clear MG am vorgesehenen Ausgang",
  lake_people_gain_setting: "Lake People Gain-Schalter",
  lake_people_volume_reference: "Lake People Referenzlautstärke",
  motu_input_gain_reference: "MOTU Eingangsverstärkung",
  motu_output_to_lake_people: "MOTU-Ausgang zum Lake People",
  motu_phantom_48v: "MOTU 48-V-Phantomspeisung",
  pioneer_listening_mode: "Pioneer Hörmodus",
  pioneer_pc_connection: "Pioneer Verbindung vom MOTU M2",
  pioneer_reference_volume: "Pioneer Referenzlautstärke",
  pioneer_selected_input: "Pioneer ausgewählter Eingang",
  rode_nt1a_connected: "RØDE NT1-A angeschlossen",
  rode_nt1a_motu_input: "RØDE am vorgesehenen MOTU-Eingang",
  transmitter_codec: "1MII Bluetooth-Codec",
  transmitter_input: "1MII Eingang",
  transmitter_paired_target: "1MII gekoppeltes Ziel",
  transmitter_tx_mode: "1MII Sendemodus",
};

const WARNING_LABELS = {
  "voice-source-not-motu": "Mikrofonquelle ist nicht das MOTU M2",
  "high-live-quantum": "Großer Live-Puffer",
  "bluetooth-service-inactive": "System-Bluetooth ist inaktiv",
  "qobuz-qbzd-api-unavailable": "Qobuz-Referenzpfad nicht erreichbar",
  "qobuz-qbzd-probe-invalid": "QBZD-Status nicht verlässlich",
  "qobuz-qbzd-reference-not-ready": "Qobuz-Referenzpfad nicht bereit",
  "qobuz-qbzd-motu-rate-mismatch": "Qobuz- und MOTU-Samplerate stimmen nicht überein",
  "qobuz-qbzd-motu-owner-unverified": "MOTU-Wiedergabe nicht sicher QBZD zugeordnet",
};

const INTERACTION_GRACE_MS = 1200;
const TELEMETRY_POLL_MS = 2000;

let focusedDepthPanel = null;
let depthFocusReturn = null;
let depthFocusScrollY = 0;

const TELEMETRY_STREAM_LABELS = {
  "audio-levels": "Pegel",
  "midi-activity": "MIDI",
  transport: "Transport",
  "cpu-load": "CPU-Last",
  xruns: "XRuns",
  "device-graph": "Geräte/Graph",
};

const TELEMETRY_AVAILABILITY_LABELS = {
  live: "live",
  stale: "veraltet",
  starting: "startet",
  unavailable: "nicht verfügbar",
};

const TRANSPORT_STATE_LABELS = {
  running: "läuft",
  idle: "bereit",
  unknown: "unbekannt",
};

const RUNTIME_MODE_STORAGE_KEY = "audio-ui-runtime-mode";
const REMOTE_MODE = "remote-audiozentrale";
const LOCAL_MODE = "local-device";
const DEFAULT_RUNTIME_MODE = REMOTE_MODE;
const RUNTIME_MODES = {
  [REMOTE_MODE]: {
    label: "Fern-Audiozentrale",
    authority:
      "Das Heim-PC-Backend ist autoritativ. Fernzugriff ist ausschließlich über " +
      "eine separat verifizierte private Bridge zulässig; nur eng typisierte Wal- und " +
      "Recorderaktionen dürfen wirken. Der lokale Control-Dienst bleibt Loopback-only.",
    backend: true,
  },
  [LOCAL_MODE]: {
    label: "Lokales Gerät",
    authority:
      "Nur Browserfähigkeiten. Kein Heim-PC-Backend, keine Backendabfragen und " +
      "keine native Autorität über MOTU, ALSA, PipeWire oder Roland.",
    backend: false,
  },
};
const LOCAL_MODE_API_BLOCK_MESSAGE =
  "Im Modus „Lokales Gerät“ sind Backendanfragen gesperrt.";
const CAPABILITY_STATE_LABELS = {
  present: "Schnittstelle vorhanden",
  absent: "Schnittstelle fehlt",
  unknown: "nicht ermittelbar",
};

const RECORDING_LIBRARY_CATEGORIES = Object.freeze({
  unsorted: "Unsortiert",
  song: "Song",
  practice: "Übung",
  idea: "Idee",
  test: "Test",
  finished: "Fertig",
});
const LIBRARY_VIEWS = new Set(["active", "trash", "all"]);
const LIBRARY_SORTS = new Set(["newest", "oldest", "name", "duration", "category"]);
const RECORDING_LIBRARY_ACTIONS = new Set(["categorize", "trash", "restore"]);

const RECORDING_COLLISION_BLOCKERS = new Set([
  "output-already-exists",
  "midi-output-already-exists",
  "manifest-output-already-exists",
]);

const RECORDING_PREFLIGHT_LABELS = Object.freeze({
  output: "Zieldatei",
  physical: "Mikrofon und MOTU",
  laboratory: "Pegelabnahme",
  source: "Audio- und MIDI-Quellen",
  tools: "Recorder-Werkzeuge",
  storage: "Speicherplatz",
  session: "Recorder-Zustand",
});

const RECORDING_BLOCKER_LABELS = Object.freeze({
  "output-root-not-ready": "Aufnahmeordner ist nicht bereit",
  "output-already-exists": "WAV-Dateiname ist bereits belegt",
  "midi-output-already-exists": "MIDI-Dateiname ist bereits belegt",
  "manifest-output-already-exists": "Take-Manifest ist bereits belegt",
  "physical-state-invalid": "Vor-Ort-Zustand ist nicht sicher lesbar",
  "laboratory-state-invalid": "Pegelabnahme ist nicht sicher lesbar",
  "motu-source-not-unique": "MOTU-Mikrofonquelle fehlt oder ist nicht eindeutig",
  "roland-audio-source-not-unique": "Roland-Audioquelle fehlt oder ist nicht eindeutig",
  "roland-midi-source-not-unique": "Roland-MIDI-Port fehlt oder ist nicht eindeutig",
  "performance-source-invalid": "Klavier-und-Gesang-Quellen passen nicht zum Aufnahmevertrag",
  "production-mix-graph-not-ready": "Produktions-Mix ist noch nicht bereit",
  "parecord-unavailable": "Audio-Recorder parecord ist nicht verfügbar",
  "arecordmidi-unavailable": "MIDI-Recorder arecordmidi ist nicht verfügbar",
  "ffmpeg-unavailable": "Mix-Werkzeug ffmpeg ist nicht verfügbar",
  "free-space-unknown": "Freier Speicherplatz kann nicht sicher bestimmt werden",
  "free-space-insufficient": "Nicht genug freier Speicherplatz für diesen Take",
  "active-session-requires-status-or-recovery": "Eine frühere Aufnahme muss zuerst abgeschlossen oder wiederhergestellt werden",
});

const RECORDING_SOURCE_FIELD_LABELS = Object.freeze({
  sample_rate_hz: "Abtastrate",
  sample_format: "Sample-Format",
  channels: "Kanalzahl",
  muted: "Stummschaltung",
  unity_volume: "Quelllautstärke",
  vendor_id: "Herstellerkennung",
  product_id: "Gerätekennung",
  address: "MIDI-Portadresse",
  upstream_roles: "Quellrollen",
});

function automaticTakeName(mode, date = new Date(), suffix = 0) {
  const pad = (value) => String(value).padStart(2, "0");
  const stamp =
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-` +
    `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
  const prefix = mode === "piano-vocal" ? "klavier-gesang" : "gesang";
  const tail = Number.isInteger(suffix) && suffix > 1 ? `-${suffix}` : "";
  return `${prefix}-${stamp}${tail}.wav`;
}

function recordingUsedNames() {
  const names = new Set();
  for (const item of state.recordingLibrary?.items || []) {
    if (typeof item?.name === "string" && item.name) names.add(item.name);
  }
  const sessionName = state.snapshot?.recording?.session?.name;
  if (typeof sessionName === "string" && sessionName) names.add(sessionName);
  return names;
}

function nextAutomaticTakeName(mode, excludedNames = []) {
  const used = recordingUsedNames();
  for (const name of excludedNames) {
    if (typeof name === "string" && name) used.add(name);
  }
  const now = new Date();
  for (let suffix = 0; suffix < 1000; suffix += 1) {
    const candidate = automaticTakeName(mode, now, suffix < 2 ? 0 : suffix);
    if (!used.has(candidate)) return candidate;
  }
  return automaticTakeName(mode, now, 1000 + (Date.now() % 100000));
}

function ensureAutomaticTakeNameFree() {
  if (state.recordingDraft.automaticName !== true) return false;
  if (state.snapshot?.recording?.session?.active === true) return false;
  const used = recordingUsedNames();
  if (!used.has(state.recordingDraft.name)) return false;
  state.recordingDraft = {
    ...state.recordingDraft,
    name: nextAutomaticTakeName(state.recordingDraft.mode, [state.recordingDraft.name]),
  };
  state.recordingPlan = null;
  state.recordingPlanInput = null;
  return true;
}

function recordingCollisionOnly(blockers) {
  return (
    Array.isArray(blockers) &&
    blockers.length > 0 &&
    blockers.every((blocker) => RECORDING_COLLISION_BLOCKERS.has(blocker))
  );
}

function recordingModeKnownHardBlocked(mode) {
  return (
    mode?.actionable !== true &&
    typeof mode?.blocker === "string" &&
    mode.blocker.length > 0 &&
    mode.blocker !== "exact-midi-gate-requires-plan"
  );
}

function recordingBlockerLabel(blocker) {
  if (typeof blocker !== "string" || !blocker) return "Unbekanntes Start-Gate";
  if (RECORDING_BLOCKER_LABELS[blocker]) return RECORDING_BLOCKER_LABELS[blocker];
  if (blocker.startsWith("physical-fact:")) {
    const fact = blocker.slice("physical-fact:".length);
    return `${PHYSICAL_FACT_LABELS[fact] || fact} fehlt oder passt nicht`;
  }
  if (blocker.startsWith("laboratory-gate:")) {
    const gate = blocker.slice("laboratory-gate:".length);
    return gate === "voice-level-measurement"
      ? "Mikrofonpegel wurde noch nicht gültig abgenommen"
      : `Laborprüfung ${gate} ist noch offen`;
  }
  const sourceField = blocker.match(
    /^(motu|roland-audio|roland-midi|production-mix)-source:(.+)$/,
  );
  if (sourceField) {
    const source = {
      motu: "MOTU",
      "roland-audio": "Roland-Audio",
      "roland-midi": "Roland-MIDI",
      "production-mix": "Produktions-Mix",
    }[sourceField[1]];
    return `${source}: ${RECORDING_SOURCE_FIELD_LABELS[sourceField[2]] || sourceField[2]} passt nicht`;
  }
  if (blocker.endsWith("-source-query-failed")) {
    return "Aufnahmequelle konnte nicht sicher abgefragt werden";
  }
  if (blocker.endsWith("-source-not-unique")) {
    return "Aufnahmequelle fehlt oder ist nicht eindeutig";
  }
  return `Technisches Start-Gate: ${blocker}`;
}

const state = {
  snapshot: null,
  runtimeMode: DEFAULT_RUNTIME_MODE,
  capabilities: null,
  serviceWorkerState: "nicht geprüft",
  appShellReloadPending: false,
  remoteBridgeProjection: null,
  remoteWhaleActionObserved: false,
  remoteActionScopes: [],
  remoteWhaleSessionToken: null,
  remoteWhaleSessionExpiresAt: 0,
  remoteWhaleSessionError: null,
  recordingLibrary: null,
  recordingLibraryError: null,
  recordingPlayerSessionId: null,
  recordingPlayerAudioUrl: null,
  recordingPlayerReturnFocus: null,
  libraryView: "active",
  libraryCategory: "all",
  librarySort: "newest",
  recordingPlan: null,
  recordingPlanInput: null,
  recordingDraft: {
    mode: "voice",
    name: automaticTakeName("voice"),
    maximumSeconds: 600,
    automaticName: true,
  },
  recordingActionPending: false,
  dauersongActionPending: false,
  recordingDetailsOpen: false,
  recordingClockTimer: null,
  whaleActionPending: false,
  whaleModeDraft: null,
  replay: null,
  replayScenarioId: "normal",
  replayFrameIndex: 0,
  replayPlaying: false,
  replayTimer: null,
  whaleLesson: null,
  whaleLessonError: null,
  lessonAudio: null,
  blindOrder: [],
  blindAnswered: false,
  telemetry: null,
  telemetryError: null,
  telemetryTimer: null,
  telemetryInFlight: null,
  telemetryRequestSequence: 0,
  telemetryPresentationSequence: 0,
  telemetryPresentedRequest: 0,
  telemetryUpdatedAt: null,
  route: "home",
  loading: false,
  autoRefresh: true,
  timer: null,
  interactionUntil: 0,
  lastDialogTrigger: null,
  dialogRequest: 0,
};

const byId = (id) => document.getElementById(id);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function appendText(parent, tag, className, text) {
  const child = element(tag, className, text);
  parent.append(child);
  return child;
}

function displayMode(mode) {
  return MODE_LABELS[mode] || mode || "Unbekannt";
}

function displayProfile(profileId) {
  return PROFILE_LABELS[profileId] || profileId;
}

function formatEndpoint(value) {
  if (value === null || value === undefined || value === "") return "unbekannt";
  return String(value).replaceAll("-", " ");
}

function formatTimestamp(value) {
  if (!value) return "Zeit unbekannt";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Zeit unbekannt";
  return `Stand ${new Intl.DateTimeFormat("de-DE", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date)}`;
}

function formatDateTime(value) {
  if (!value) return "nicht verfügbar";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "nicht verfügbar";
  return new Intl.DateTimeFormat("de-DE", {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(date);
}

function shortRevision(value) {
  return typeof value === "string" && value.length >= 10
    ? value.slice(0, 10)
    : "nicht lesbar";
}

function hardwareStateLabel(stateName) {
  return (
    {
      online: "online",
      partial: "teilweise online",
      offline: "nicht beobachtet",
      "not-configured": "nicht konfiguriert",
      unavailable: "nicht lesbar",
    }[stateName] || "unbekannt"
  );
}

function deploymentStateLabel(status) {
  return (
    {
      current: "aktuell",
      drift: "Abweichung",
      unavailable: "nicht lesbar",
      "source-checkout": "Quellcheckout",
    }[status] || "unbekannt"
  );
}

function profileState(profile) {
  if (profile.dashboard_state) return profile.dashboard_state;
  if (profile.operational_status === "planned") return "planned";
  if ((profile.required_hardware || []).length) return "onsite";
  if ((profile.required_laboratory_gates || []).length) return "laboratory";
  return profile.actionable ? "executable" : "plan-ready";
}

function profileStateTone(stateName) {
  if (stateName === "executable" || stateName === "plan-ready") return "ready";
  if (stateName === "onsite") return "onsite";
  if (stateName === "planned") return "planned";
  return "laboratory";
}

function profilesByArea(area) {
  return profilesFor(area);
}

/*
 * Laufzeitmodus, Fähigkeitserkennung und Backendsperre.
 *
 * "remote-audiozentrale" behandelt das Heim-PC-Backend als autoritativ,
 * "local-device" kennt ausschließlich Browserfähigkeiten und darf keine
 * einzige Backendanfrage auslösen.
 */

function runtimeModeDefinition(mode) {
  return Object.hasOwn(RUNTIME_MODES, mode)
    ? RUNTIME_MODES[mode]
    : RUNTIME_MODES[DEFAULT_RUNTIME_MODE];
}

function backendAllowed() {
  return runtimeModeDefinition(state.runtimeMode).backend === true;
}

function sameOriginApiTarget(input) {
  let candidate = null;
  if (typeof input === "string") candidate = input;
  else if (input instanceof URL) candidate = input.href;
  else if (input && typeof input.url === "string") candidate = input.url;
  if (candidate === null) return true;
  let target;
  try {
    target = new URL(candidate, window.location.href);
  } catch (_error) {
    // Fail closed: Ein unlesbares Ziel gilt als backendverdächtig.
    return true;
  }
  if (target.origin !== window.location.origin) return false;
  return (
    target.pathname === "/api" ||
    target.pathname.startsWith("/api/") ||
    target.pathname.startsWith("/bridge/v1/")
  );
}

function installBackendFetchGuard() {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function guardedFetch(input, init) {
    if (!backendAllowed() && sameOriginApiTarget(input)) {
      return Promise.reject(new Error(LOCAL_MODE_API_BLOCK_MESSAGE));
    }
    return nativeFetch(input, init);
  };
}

function capabilityState(probe) {
  try {
    return probe() ? "present" : "absent";
  } catch (_error) {
    // Fail closed: Eine fehlgeschlagene Erkennung gilt niemals als vorhanden.
    return "unknown";
  }
}

function permissionsPolicyState(feature) {
  const policy = document.featurePolicy || document.permissionsPolicy || null;
  if (!policy || typeof policy.allowsFeature !== "function") {
    return "nicht exponiert";
  }
  try {
    return policy.allowsFeature(feature) ? "erlaubt" : "verweigert";
  } catch (_error) {
    return "nicht exponiert";
  }
}

/*
 * Reine Schnittstellenerkennung ohne Kennungsauswertung. Es wird nichts
 * geöffnet, nichts angefragt und nichts gestartet.
 */
function detectCapabilities() {
  return {
    secureContext: capabilityState(() => window.isSecureContext === true),
    standalone: capabilityState(
      () => window.matchMedia("(display-mode: standalone)").matches,
    ),
    webAudio: capabilityState(() => typeof window.AudioContext === "function"),
    mediaCapture: capabilityState(
      () => typeof navigator.mediaDevices?.getUserMedia === "function",
    ),
    webMidi: capabilityState(
      () => typeof navigator.requestMIDIAccess === "function",
    ),
    serviceWorker: capabilityState(() => "serviceWorker" in navigator),
    microphonePolicy: permissionsPolicyState("microphone"),
    midiPolicy: permissionsPolicyState("midi"),
  };
}

function capabilityRows(capabilities) {
  return [
    {
      id: "secure-context",
      label: "Sicherer Kontext",
      state: capabilities.secureContext,
      proof: "Transportbedingung, kein Geräte- oder Hardwarebeleg.",
    },
    {
      id: "service-worker",
      label: "Service Worker",
      state: capabilities.serviceWorker,
      proof: `Registrierung: ${state.serviceWorkerState}. Cacht nur die statische App-Shell.`,
    },
    {
      id: "web-audio",
      label: "Web Audio",
      state: capabilities.webAudio,
      proof: "Kein AudioContext geöffnet; Ausgabegerät und Route bleiben unbelegt.",
    },
    {
      id: "media-capture",
      label: "Mikrofonaufnahme",
      state: capabilities.mediaCapture,
      proof: `Permissions-Policy Mikrofon: ${capabilities.microphonePolicy}. Keine Anfrage gestellt, keine Aufnahme belegt.`,
    },
    {
      id: "web-midi",
      label: "Web MIDI",
      state: capabilities.webMidi,
      proof: `Permissions-Policy MIDI: ${capabilities.midiPolicy}. Kein Zugriff angefordert; Roland bleibt unbelegt.`,
    },
  ];
}

function renderRuntimeMode() {
  const definition = runtimeModeDefinition(state.runtimeMode);
  const capabilities = state.capabilities || detectCapabilities();
  document.documentElement.dataset.runtimeMode = state.runtimeMode;
  byId("local-device-boundary").hidden = backendAllowed();
  byId("runtime-mode-remote").checked = state.runtimeMode === REMOTE_MODE;
  byId("runtime-mode-local").checked = state.runtimeMode === LOCAL_MODE;
  byId("runtime-mode-authority").textContent =
    `${definition.label}: ${definition.authority}`;

  const list = byId("capability-list");
  list.replaceChildren(
    ...capabilityRows(capabilities).map((row) => {
      const card = element("article", "capability-card");
      card.dataset.capability = row.id;
      card.dataset.capabilityState = row.state;
      appendText(card, "span", "", row.label);
      appendText(card, "strong", "", CAPABILITY_STATE_LABELS[row.state]);
      appendText(card, "small", "", row.proof);
      return card;
    }),
  );

  const install = byId("pwa-install-state");
  install.replaceChildren();
  detailRow(
    install,
    "Anzeige",
    capabilities.standalone === "present" ? "standalone" : "Browserfenster",
  );
  detailRow(install, "Service Worker", state.serviceWorkerState);
  detailRow(install, "Backendabfragen", backendAllowed() ? "erlaubt" : "gesperrt");
  detailRow(
    install,
    "Ferntransport",
    state.remoteBridgeProjection === true
      ? remoteWhaleSessionFresh()
        ? "Private Bridge · Wal + Recorder eng freigegeben; sonst read-only"
        : "Private Bridge · scoped Session nicht belegt"
      : "nicht in aktueller Antwort belegt · Control bleibt Loopback-only",
  );
  detailRow(
    install,
    "Physisch belegt",
    "nein · iPad-Installation, Audio- und MIDI-Hardware sind unbelegt",
  );
  syncRemoteControls();
}

function syncRemoteControls() {
  const blocked = !backendAllowed();
  byId("refresh-button").disabled =
    blocked || state.loading || state.recordingActionPending || state.dauersongActionPending || state.whaleActionPending;
  byId("diagnostic-refresh").disabled =
    blocked || state.loading || state.recordingActionPending || state.dauersongActionPending || state.whaleActionPending;
  byId("auto-refresh-toggle").disabled = blocked;
}

function renderLocalDeviceAuthority() {
  const light = byId("authority-light");
  light.classList.remove("is-ready", "is-busy");
  byId("authority-label").textContent = "Lokales Gerät · kein Backend";
  const mobileLight = byId("mobile-authority-light");
  mobileLight.classList.remove("is-ready", "is-busy");
  byId("mobile-authority-label").textContent = "gerät";
  byId("mobile-authority").setAttribute(
    "aria-label",
    "Lokales Gerät ohne Backend- und Hardwareautorität",
  );
  byId("updated-at").textContent = "Kein Backendstand";
  byId("mobile-updated-at").textContent = "kein Stand";
  const truth = [
    ["truth-observed", "nicht gelesen", "kein Backend-Readback"],
    ["truth-configured", "nicht gelesen", "kein Sollzustand ohne Backend"],
    ["truth-physical", "nicht gelesen", "keine Vor-Ort-Belege"],
    ["truth-executable", "read-only", "keine Audiowirkung"],
  ];
  for (const [id, value, detail] of truth) {
    byId(id).textContent = value;
    byId(`${id}-detail`).textContent = detail;
  }
  showNotice(
    "Modus „Lokales Gerät“: keine Backend-, Telemetrie- oder Geräteabfragen. " +
      "Angezeigte Systemwahrheit stammt nicht von diesem Gerät.",
    "info",
  );
}

function stopRemoteActivity() {
  if (state.timer) {
    window.clearInterval(state.timer);
    state.timer = null;
  }
  if (state.recordingClockTimer) {
    window.clearInterval(state.recordingClockTimer);
    state.recordingClockTimer = null;
  }
  stopTelemetryPolling();
  stopReplay();
  // Laufende Antworten dürfen keine späte Backendwahrheit mehr einblenden.
  state.telemetryRequestSequence += 1;
  state.telemetry = null;
  state.telemetryError = "Im Modus „Lokales Gerät“ wird keine Telemetrie gelesen.";
  state.snapshot = null;
  state.recordingLibrary = null;
  state.recordingLibraryError = null;
  state.recordingPlan = null;
  state.recordingPlanInput = null;
  state.recordingActionPending = false;
  state.dauersongActionPending = false;
  state.whaleActionPending = false;
  state.remoteWhaleActionObserved = false;
  state.remoteActionScopes = [];
  state.remoteWhaleSessionToken = null;
  state.remoteWhaleSessionExpiresAt = 0;
  state.remoteWhaleSessionError = null;
  state.whaleModeDraft = null;
  clearGlobalTakePlayer();
  // Kein gelesener Zustand darf als Kennzahl stehen bleiben.
  byId("diagnostic-badge").hidden = true;
  byId("diagnostic-badge").textContent = "0";
  renderTelemetry();
}

function applyRuntimeMode({ persist = false } = {}) {
  if (persist) savePreference(RUNTIME_MODE_STORAGE_KEY, state.runtimeMode);
  if (backendAllowed()) {
    clearNotice();
    scheduleAutoRefresh();
    loadReplay();
    loadWhaleLesson();
    requestTelemetry().finally(() => scheduleTelemetryPolling());
    refreshSnapshot(true);
  } else {
    stopRemoteActivity();
    renderLocalDeviceAuthority();
  }
  renderRuntimeMode();
}

function loadRuntimeMode() {
  try {
    const stored = window.localStorage.getItem(RUNTIME_MODE_STORAGE_KEY);
    if (typeof stored === "string" && Object.hasOwn(RUNTIME_MODES, stored)) {
      return stored;
    }
  } catch (_error) {
    // Ohne lokalen Speicher gilt der Vorgabemodus.
  }
  return DEFAULT_RUNTIME_MODE;
}

/*
 * Registrierung ausschließlich in sicheren Kontexten. Ein Fehlschlag bleibt
 * folgenlos; die Ansicht funktioniert ohne Service Worker vollständig.
 */
function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) {
    state.serviceWorkerState = "nicht unterstützt";
    return;
  }
  if (window.isSecureContext !== true) {
    state.serviceWorkerState = "übersprungen · kein sicherer Kontext";
    return;
  }
  const hadController = Boolean(navigator.serviceWorker.controller);
  state.serviceWorkerState = "wird registriert";
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!hadController) {
      state.serviceWorkerState = "aktiv";
      renderRuntimeMode();
      return;
    }
    // Kein automatischer Reload: eine neue App-Shell wird angekündigt, nicht erzwungen.
    state.appShellReloadPending = true;
    state.serviceWorkerState = "neue App-Shell aktiv · Neuladen empfohlen";
    showNotice(
      "Eine neue App-Shell ist aktiv. Bitte die Ansicht neu laden.",
      "info",
    );
    renderRuntimeMode();
  });
  navigator.serviceWorker
    .register("/sw.js", { scope: "/" })
    .then(() => {
      state.serviceWorkerState = "registriert";
      renderRuntimeMode();
    })
    .catch(() => {
      state.serviceWorkerState = "Registrierung fehlgeschlagen";
      renderRuntimeMode();
    });
}

async function fetchJson(url, options = {}) {
  if (!backendAllowed() && sameOriginApiTarget(url)) {
    throw new Error(LOCAL_MODE_API_BLOCK_MESSAGE);
  }
  const { timeoutMs = 12000, ...fetchOptions } = options;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(url, {
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
      ...fetchOptions,
    });
  } catch (error) {
    window.clearTimeout(timeout);
    if (
      controller.signal.aborted ||
      (error instanceof DOMException && error.name === "AbortError")
    ) {
      throw new Error("Der Control-Dienst hat nicht rechtzeitig geantwortet.");
    }
    throw new Error("Der lokale Control-Dienst ist nicht erreichbar.");
  }
  const bridgeMarker = response.headers.get("X-Audio-Remote-Bridge");
  if (
    bridgeMarker === "read-only-v1" ||
    bridgeMarker === "whale-action-v1" ||
    bridgeMarker === "recording-action-v1"
  ) {
    // Fail closed for the page lifetime: once a remote projection was observed,
    // later responses may not silently turn the page into local authority.
    state.remoteBridgeProjection = true;
  } else if (state.remoteBridgeProjection === null) {
    state.remoteBridgeProjection = false;
  }
  if (response.headers.get("X-Audio-Remote-Effects") === "whale-v1") {
    state.remoteWhaleActionObserved = true;
  }
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    if (controller.signal.aborted) {
      throw new Error("Der Control-Dienst hat nicht rechtzeitig geantwortet.");
    }
    throw new Error(`Der Control-Dienst antwortet unlesbar (${response.status}).`);
  } finally {
    window.clearTimeout(timeout);
  }
  if (!response.ok) {
    const serviceCode =
      payload &&
      typeof payload.error === "object" &&
      typeof payload.error.code === "string"
        ? payload.error.code
        : null;
    const serviceMessage =
      payload &&
      typeof payload.error === "object" &&
      typeof payload.error.message === "string"
        ? payload.error.message
        : typeof payload?.error === "string"
          ? payload.error
          : null;
    const serviceError = new Error(
      serviceMessage || `Anfrage fehlgeschlagen (${response.status}).`,
    );
    serviceError.code = serviceCode;
    serviceError.status = response.status;
    throw serviceError;
  }
  return payload;
}

function showNotice(message, tone = "error") {
  const notice = byId("global-notice");
  notice.textContent = message;
  notice.classList.toggle("success", tone === "success");
  notice.classList.toggle("info", tone === "info");
  notice.hidden = false;
}

function clearNotice() {
  const notice = byId("global-notice");
  notice.hidden = true;
  notice.textContent = "";
  notice.classList.remove("success");
  notice.classList.remove("info");
}

function setLoading(loading) {
  state.loading = loading;
  const button = byId("refresh-button");
  button.classList.toggle("is-loading", loading);
  syncRemoteControls();
  if (state.snapshot) renderWhale();
}

async function refreshSnapshot(force = false) {
  if (
    state.loading ||
    state.recordingActionPending ||
    state.dauersongActionPending ||
    state.whaleActionPending ||
    !backendAllowed()
  ) return;
  setLoading(true);
  try {
    const suffix = force ? "?refresh=1" : "";
    const snapshot = await fetchJson(`/api/v1/snapshot${suffix}`, {
      timeoutMs: 50000,
    });
    state.snapshot = snapshot;
    clearNotice();
    if (state.remoteBridgeProjection === true) {
      await ensureRemoteWhaleSession();
    }
    await loadRecordingLibrary({ render: false });
    renderAll();
  } catch (error) {
    showNotice(error instanceof Error ? error.message : "Zustand konnte nicht gelesen werden.");
    renderAuthority(error?.code === "snapshot_busy" ? "busy" : "offline");
  } finally {
    setLoading(false);
  }
}

function renderAuthority(status = "ready") {
  const ready = status === "ready";
  const light = byId("authority-light");
  light.classList.toggle("is-ready", ready);
  light.classList.toggle("is-busy", status === "busy");
  const fullLabel =
    status === "ready"
      ? "Backend autoritativ"
      : status === "busy"
        ? "Backend beschäftigt"
        : "Backend nicht erreichbar";
  byId("authority-label").textContent = fullLabel;
  byId("mobile-authority-light").classList.toggle("is-ready", ready);
  byId("mobile-authority-light").classList.toggle("is-busy", status === "busy");
  byId("mobile-authority-label").textContent =
    status === "ready" ? "lokal" : status === "busy" ? "wartet" : "offline";
  byId("mobile-authority").setAttribute("aria-label", fullLabel);
}

function renderAll({ preserveRecorderDraft = true } = {}) {
  if (!state.snapshot) return;
  renderAuthority("ready");
  renderRuntimeMode();
  byId("updated-at").textContent = formatTimestamp(state.snapshot.generated_at);
  byId("mobile-updated-at").textContent = formatTimestamp(
    state.snapshot.generated_at,
  ).replace("Stand ", "");
  renderTruth();
  renderHome();
  renderActiveLanes({ preserveDraft: preserveRecorderDraft });
  renderRecordingRecentTakes();
  renderWhale();
  renderWhaleLessonSummary();
  renderProfiles();
  renderLibrary();
  renderSounds();
  renderConnections();
  renderDeployment();
  renderDiagnostics();
  renderSettings();
  renderReplay();
}

function renderTruth() {
  const snapshot = state.snapshot;
  const doctor = snapshot.doctor || {};
  const graph = doctor.graph || {};
  const presence = snapshot.presence || {};
  const summary = snapshot.summary || {};
  const truth = snapshot.truth_stream || {};
  const observed = presence.observed_count ?? 0;
  const desired = presence.desired_count ?? 0;
  byId("truth-observed").textContent = doctor.status === "ok" ? `${observed}/${desired} Geräte` : "nicht lesbar";
  byId("truth-observed-detail").textContent =
    doctor.status === "ok"
      ? `Wahrheit Seq ${truth.sequence ?? "—"} · ${truth.freshness || "offen"} · ${truth.age_ms ?? "—"} ms`
      : `kein positiver Zustand angenommen · Fehler ${truth.error_count ?? "—"}`;
  byId("truth-configured").textContent = graph.force_rate_hz ? `${graph.force_rate_hz} Hz` : "offen";
  byId("truth-configured-detail").textContent = graph.default_sink ? `Ziel: ${formatEndpoint(graph.default_sink)}` : "kein Ziel lesbar";
  const physicalCount = summary.physical_unknown_count || 0;
  byId("truth-physical").textContent = physicalCount ? `${physicalCount} offen` : "belegt";
  byId("truth-physical-detail").textContent = physicalCount ? "Vor-Ort-Nachweise fehlen" : "keine offenen Nachweise";
  const recordingWritable = recordingActionsAllowed();
  const whaleWritable = whaleActionsAllowed();
  const executable = [];
  if (recordingWritable) executable.push("Voice-Recorder");
  if (whaleWritable) executable.push("Walstimme");
  byId("truth-executable").textContent = executable.length ? executable.join(" + ") : "read-only";
  byId("truth-executable-detail").textContent = executable.length
    ? remoteWhaleActionsAllowed()
      ? "Walstimme und Recorder über private, eng begrenzte Tailnet-Bridge"
      : remoteRecordingActionsAllowed()
        ? "Recorder über private, eng begrenzte Tailnet-Bridge"
        : "Wirkende Aktionen nur über den lokalen Loopback-Dienst mit frischem Aktionstoken"
    : state.remoteBridgeProjection === true
      ? "Private Bridge erkannt; ohne scoped Session keine Audiowirkung"
      : "Replay lokal; wirkende Audioaktionen nicht autorisiert";

}

function directLoopbackControlOrigin() {
  return Boolean(
    window.location.protocol === "http:" &&
      (window.location.hostname === "127.0.0.1" ||
        window.location.hostname === "localhost"),
  );
}

function localRecordingActionsAllowed() {
  return Boolean(
    directLoopbackControlOrigin() &&
      backendAllowed() &&
      state.remoteBridgeProjection !== true &&
      state.snapshot?.capabilities?.recording_control === true &&
      state.snapshot?.recording?.actionable === true &&
      typeof state.snapshot?.service?.action_token === "string" &&
      state.snapshot.service.action_token.length >= 16
  );
}

function remoteRecordingActionsAllowed() {
  return Boolean(
    backendAllowed() &&
      state.snapshot?.capabilities?.recording_control === true &&
      state.snapshot?.recording?.actionable === true &&
      remoteWhaleSessionFresh() &&
      state.remoteActionScopes.includes("recording")
  );
}

function recordingActionsAllowed() {
  return localRecordingActionsAllowed() || remoteRecordingActionsAllowed();
}

function localRecordingLibraryActionsAllowed() {
  return Boolean(
    directLoopbackControlOrigin() &&
      backendAllowed() &&
      state.remoteBridgeProjection !== true &&
      state.snapshot?.capabilities?.recording_control === true &&
      typeof state.snapshot?.service?.action_token === "string" &&
      state.snapshot.service.action_token.length >= 16
  );
}

function remoteRecordingLibraryActionsAllowed() {
  return Boolean(
    backendAllowed() &&
      state.snapshot?.capabilities?.recording_control === true &&
      remoteWhaleSessionFresh() &&
      state.remoteActionScopes.includes("recording")
  );
}

function recordingLibraryActionsAllowed() {
  return (
    localRecordingLibraryActionsAllowed() || remoteRecordingLibraryActionsAllowed()
  );
}

function localWhaleActionsAllowed() {
  return Boolean(
    directLoopbackControlOrigin() &&
      backendAllowed() &&
      state.remoteBridgeProjection !== true &&
      state.snapshot?.capabilities?.whale_control === true &&
      state.snapshot?.whale?.status === "ok" &&
      typeof state.snapshot?.service?.action_token === "string" &&
      state.snapshot.service.action_token.length >= 16
  );
}

function localDauersongActionsAllowed() {
  return Boolean(
    directLoopbackControlOrigin() &&
      backendAllowed() &&
      state.remoteBridgeProjection !== true &&
      state.snapshot?.capabilities?.dauersong_control === true &&
      state.snapshot?.dauersong?.status === "ok" &&
      typeof state.snapshot?.service?.action_token === "string" &&
      state.snapshot.service.action_token.length >= 16
  );
}

async function postDauersongAction(payload) {
  if (!localDauersongActionsAllowed()) {
    throw new Error(
      state.remoteBridgeProjection === true
        ? "Dauersong-Steuerung ist derzeit nur lokal am Heim-PC freigegeben."
        : "Dauersong-Steuerung ist aktuell nicht sicher verfügbar.",
    );
  }
  return fetchJson("/api/v1/actions/dauersong", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Audio-Control-Token": state.snapshot.service.action_token,
    },
    body: JSON.stringify(payload),
    timeoutMs: 70000,
  });
}

function remoteWhaleSessionFresh() {
  return Boolean(
    state.remoteBridgeProjection === true &&
      state.remoteWhaleActionObserved === true &&
      typeof state.remoteWhaleSessionToken === "string" &&
      state.remoteWhaleSessionToken.length >= 32 &&
      Number.isInteger(state.remoteWhaleSessionExpiresAt) &&
      state.remoteWhaleSessionExpiresAt > Math.floor(Date.now() / 1000) + 30 &&
      state.remoteActionScopes.includes("whale")
  );
}

function remoteWhaleActionsAllowed() {
  return Boolean(
    backendAllowed() &&
      state.snapshot?.capabilities?.whale_control === true &&
      state.snapshot?.whale?.status === "ok" &&
      remoteWhaleSessionFresh()
  );
}

function whaleActionsAllowed() {
  return localWhaleActionsAllowed() || remoteWhaleActionsAllowed();
}

async function ensureRemoteWhaleSession({ force = false } = {}) {
  if (state.remoteBridgeProjection !== true || !backendAllowed()) return false;
  if (!force && remoteWhaleSessionFresh()) return true;
  state.remoteWhaleSessionToken = null;
  state.remoteWhaleSessionExpiresAt = 0;
  state.remoteActionScopes = [];
  try {
    const session = await fetchJson("/bridge/v1/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
      timeoutMs: 6000,
    });
    if (
      session?.kind !== "audio_remote_bridge_session" ||
      !Array.isArray(session.effect_scope) ||
      !session.effect_scope.includes("whale") ||
      !session.effect_scope.includes("recording") ||
      typeof session.session_token !== "string" ||
      session.session_token.length < 32 ||
      !Number.isInteger(session.expires_at_unix)
    ) {
      throw new Error("Die Wal-Fernsession ist unvollständig.");
    }
    state.remoteWhaleSessionToken = session.session_token;
    state.remoteWhaleSessionExpiresAt = session.expires_at_unix;
    state.remoteActionScopes = [...session.effect_scope];
    state.remoteWhaleSessionError = null;
    return remoteWhaleSessionFresh();
  } catch (error) {
    state.remoteWhaleSessionToken = null;
    state.remoteWhaleSessionExpiresAt = 0;
    state.remoteActionScopes = [];
    state.remoteWhaleSessionError =
      error instanceof Error ? error.message : "Wal-Fernsession ist nicht verfügbar.";
    return false;
  }
}

async function postWhaleAction(payload) {
  if (localWhaleActionsAllowed()) {
    return fetchJson("/api/v1/actions/whale", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Audio-Control-Token": state.snapshot.service.action_token,
      },
      body: JSON.stringify(payload),
      timeoutMs: 70000,
    });
  }
  if (remoteWhaleActionsAllowed()) {
    return fetchJson("/bridge/v1/actions/whale", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Audio-Bridge-Session": state.remoteWhaleSessionToken,
      },
      body: JSON.stringify(payload),
      timeoutMs: 70000,
    });
  }
  throw new Error(
    state.remoteBridgeProjection === true
      ? state.remoteWhaleSessionError || "Walsteuerung ist auf dieser Fernverbindung nicht autorisiert."
      : "Walsteuerung ist nur über eine autorisierte lokale oder private Fernverbindung möglich.",
  );
}

function recordingStatusLabel(value) {
  return (
    {
      idle: "bereit · keine aktive Aufnahme",
      running: "Aufnahme läuft",
      completed: "Take finalisiert",
      "failed-preserved": "fehlgeschlagen · Teilaufnahme erhalten",
      "recovery-required": "Recovery erforderlich",
      "identity-mismatch": "Prozessidentität unklar · Recovery erforderlich",
      unavailable: "nicht lesbar",
    }[value] || value || "unbekannt"
  );
}

function recordingPresentationState(recording, plan = null) {
  const session = recording?.session || null;
  if (
    session?.recovery_required === true ||
    session?.status === "recovery-required" ||
    session?.status === "identity-mismatch"
  ) {
    return "recovery-required";
  }
  if (session?.active === true || recording?.status === "running") return "recording";
  if (session?.status === "completed") return "completed";
  if (plan?.ready === true) return "ready";
  if (plan?.ready === false) return "blocked";
  return "idle";
}

function formatRecordingElapsed(startedAt, nowMs = Date.now()) {
  const startedMs = Date.parse(startedAt || "");
  if (!Number.isFinite(startedMs) || !Number.isFinite(nowMs)) return "00:00";
  const totalSeconds = Math.max(0, Math.floor((nowMs - startedMs) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function recordingLiveLevelState(telemetry = state.telemetry) {
  const stream = (telemetry?.streams || []).find((candidate) => candidate?.id === "audio-levels");
  if (!stream) {
    return {
      status: "unavailable",
      observation: null,
      label: "Live-Pegel nicht verfügbar",
      detail: "Kein aktueller Pegelbeleg",
    };
  }
  if (stream.availability !== "live") {
    const status = stream.availability || "unavailable";
    return {
      status,
      observation: null,
      label:
        status === "stale"
          ? "Pegel veraltet"
          : status === "starting"
            ? "Pegel startet"
            : "Pegel nicht verfügbar",
      detail:
        status === "stale"
          ? "Letzte Pegelmessung ist nicht mehr frisch"
          : status === "starting"
            ? "Live-Telemetrie baut die Pegelmessung auf"
            : "Live-Telemetrie meldet keinen aktuellen Pegel",
    };
  }
  if (stream.value?.source !== "active-pipewire-shared-capture") {
    return {
      status: "unverified-source",
      observation: null,
      label: "Pegelquelle nicht verifiziert",
      detail: "Kein belegter Shared-Capture-Pegel",
    };
  }
  const peak = finiteTelemetryNumber(stream.value?.peak_dbfs);
  const rms = finiteTelemetryNumber(stream.value?.rms_dbfs);
  if (peak === null || rms === null) {
    return {
      status: "incomplete",
      observation: null,
      label: "Pegel unvollständig",
      detail: "Peak und RMS sind nicht vollständig lesbar",
    };
  }
  return {
    status: "live",
    observation: {
      peak,
      rms,
      clipping: stream.value?.clipping === true,
    },
    label: null,
    detail:
      stream.value?.source_selection === "pipewire-default-source"
        ? "PipeWire-Standard-Eingang · aggregierter Live-Pegel"
        : "PipeWire-Eingang · aggregierter Live-Pegel",
  };
}

function recordingLiveLevelObservation(telemetry = state.telemetry) {
  return recordingLiveLevelState(telemetry).observation;
}

function updateRecordingLiveStage(nowMs = Date.now()) {
  const session = state.snapshot?.recording?.session || null;
  const timer = byId("recording-running-time");
  if (timer) timer.textContent = formatRecordingElapsed(session?.started_at, nowMs);

  const meter = byId("recording-live-level");
  const value = byId("recording-live-level-value");
  const source = byId("recording-live-level-source");
  const stage = byId("recording-live-stage");
  if (!meter || !value || !source || !stage) return;
  const levelState = recordingLiveLevelState();
  const observation = levelState.observation;
  if (!observation) {
    meter.removeAttribute("value");
    value.textContent = levelState.label;
    source.textContent = levelState.detail;
    stage.classList.remove("is-clipping");
    return;
  }
  meter.value = dbProgress(observation.peak);
  value.textContent = `Peak ${observation.peak.toFixed(1)} dBFS · RMS ${observation.rms.toFixed(1)} dBFS${observation.clipping ? " · Clipping" : ""}`;
  source.textContent = levelState.detail;
  stage.classList.toggle("is-clipping", observation.clipping);
}

function syncRecordingClock() {
  if (state.recordingClockTimer) window.clearInterval(state.recordingClockTimer);
  state.recordingClockTimer = null;
  updateRecordingLiveStage();
  if (state.snapshot?.recording?.session?.active === true) {
    state.recordingClockTimer = window.setInterval(updateRecordingLiveStage, 250);
  }
}

function recordingPlanMatchesDraft() {
  const plan = state.recordingPlan;
  const input = state.recordingPlanInput;
  return Boolean(
    plan?.ready === true &&
      typeof plan.plan_sha256 === "string" &&
      input?.name === state.recordingDraft.name &&
      input?.maximumSeconds === state.recordingDraft.maximumSeconds &&
      input?.mode === state.recordingDraft.mode &&
      plan.mode === state.recordingDraft.mode &&
      plan.session_type ===
        (state.recordingDraft.mode === "piano-vocal"
          ? "piano-vocal-performance"
          : "voice-recording")
  );
}

async function postRecordingAction(payload) {
  const libraryAction = RECORDING_LIBRARY_ACTIONS.has(payload?.operation);
  const allowed = libraryAction
    ? recordingLibraryActionsAllowed()
    : recordingActionsAllowed();
  if (!allowed) {
    throw new Error(
      state.remoteBridgeProjection === true
        ? state.remoteWhaleSessionError || "Recorder ist auf dieser privaten Fernverbindung nicht autorisiert."
        : libraryAction
          ? "Die lokale Aufnahmebibliothek ist nicht für Änderungen freigeschaltet."
          : "Der lokale Voice-Recorder ist nicht für Aktionen freigeschaltet.",
    );
  }
  if (
    libraryAction ? localRecordingLibraryActionsAllowed() : localRecordingActionsAllowed()
  ) {
    return fetchJson("/api/v1/actions/recording", {
      method: "POST",
      timeoutMs: 55000,
      headers: {
        "Content-Type": "application/json",
        "X-Audio-Control-Token": state.snapshot.service.action_token,
      },
      body: JSON.stringify(payload),
    });
  }
  if (
    libraryAction ? remoteRecordingLibraryActionsAllowed() : remoteRecordingActionsAllowed()
  ) {
    return fetchJson("/bridge/v1/actions/recording", {
      method: "POST",
      timeoutMs: 55000,
      headers: {
        "Content-Type": "application/json",
        "X-Audio-Bridge-Session": state.remoteWhaleSessionToken,
      },
      body: JSON.stringify(payload),
    });
  }
  throw new Error("Recorderaktion ist nicht autorisiert.");
}

async function requestRecordingPlan({ autoRenameCollision = true } = {}) {
  const attemptedNames = new Set();
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const input = {
      mode: state.recordingDraft.mode,
      name: state.recordingDraft.name,
      maximumSeconds: state.recordingDraft.maximumSeconds,
    };
    attemptedNames.add(input.name);
    const result = await postRecordingAction({
      operation: "plan",
      mode: input.mode,
      name: input.name,
      maximum_seconds: input.maximumSeconds,
    });
    if (result.operation !== "plan" || !result.plan) {
      throw new Error("Recorder lieferte keinen gebundenen Aufnahmeplan.");
    }
    const blockers = result.plan?.readiness?.blockers || [];
    if (
      result.plan?.ready !== true &&
      autoRenameCollision &&
      state.recordingDraft.automaticName === true &&
      recordingCollisionOnly(blockers) &&
      attempt < 2
    ) {
      state.recordingDraft = {
        ...state.recordingDraft,
        name: nextAutomaticTakeName(input.mode, attemptedNames),
      };
      state.recordingPlan = null;
      state.recordingPlanInput = null;
      continue;
    }
    state.recordingPlan = result.plan;
    state.recordingPlanInput = input;
    return { result, input };
  }
  throw new Error("Für den nächsten Take konnte kein freier Dateiname gefunden werden.");
}

function syncRecordingLibraryControls() {
  const target = byId("library-takes");
  if (!target) return;
  target.setAttribute("aria-busy", String(state.recordingActionPending));
  // renderLibrary baut den autorisierungsabhängigen Endzustand neu; alte
  // Controls werden hier deshalb nie pauschal wieder freigeschaltet.
  if (!state.recordingActionPending) return;
  for (const control of target.querySelectorAll(
    ".recording-category-select, .recording-take button",
  )) {
    control.disabled = true;
  }
}

async function runRecordingStart() {
  if (state.recordingActionPending) return;
  state.recordingActionPending = true;
  syncRemoteControls();
  syncRecordingLibraryControls();
  renderActiveLanes({ preserveDraft: false });
  try {
    let plan = state.recordingPlan;
    let input = state.recordingPlanInput;
    if (!recordingPlanMatchesDraft()) {
      const planned = await requestRecordingPlan({ autoRenameCollision: true });
      plan = planned.result.plan;
      input = planned.input;
    }
    const blockers = plan?.readiness?.blockers || [];
    if (plan?.ready !== true || typeof plan.plan_sha256 !== "string") {
      state.recordingDetailsOpen = true;
      showNotice(
        `Aufnahme kann noch nicht starten: ${blockers.join(" · ") || "Plan nicht startbereit"}.`,
        "info",
      );
      return;
    }
    const result = await postRecordingAction({
      operation: "start",
      mode: input.mode,
      name: input.name,
      maximum_seconds: input.maximumSeconds,
      expected_plan_sha256: plan.plan_sha256,
    });
    if (result.snapshot?.kind !== "audio_control_snapshot") {
      throw new Error("Recorderstart lieferte keinen autoritativen Readback.");
    }
    state.snapshot = result.snapshot;
    state.recordingPlan = null;
    state.recordingPlanInput = null;
    state.recordingDetailsOpen = false;
    await loadRecordingLibrary({ render: false });
    showNotice("Aufnahme läuft; Planprüfung und Start wurden bestätigt.", "success");
  } catch (error) {
    showNotice(error instanceof Error ? error.message : "Recorderstart wurde abgewiesen.");
  } finally {
    state.recordingActionPending = false;
    syncRemoteControls();
    if (state.snapshot) renderAll({ preserveRecorderDraft: false });
  }
}

async function runRecordingAction(payload) {
  if (state.recordingActionPending) return;
  state.recordingActionPending = true;
  syncRemoteControls();
  syncRecordingLibraryControls();
  renderActiveLanes({ preserveDraft: false });
  try {
    let result;
    if (payload.operation === "plan") {
      state.recordingDetailsOpen = true;
      const planned = await requestRecordingPlan({ autoRenameCollision: true });
      result = planned.result;
      const blockers = result.plan?.readiness?.blockers || [];
      showNotice(
        result.plan?.ready
          ? "Aufnahmeplan ist vollständig gebunden und startbereit."
          : `Aufnahmeplan bleibt blockiert (${blockers.length} Gate${blockers.length === 1 ? "" : "s"}).`,
        result.plan?.ready ? "success" : "info",
      );
    } else {
      result = await postRecordingAction(payload);
      if (result.snapshot?.kind !== "audio_control_snapshot") {
        throw new Error("Recorderaktion lieferte keinen autoritativen Readback.");
      }
      state.snapshot = result.snapshot;
      state.recordingPlan = null;
      state.recordingPlanInput = null;
      if (["stop", "recover"].includes(result.operation)) state.recordingDetailsOpen = false;
      await loadRecordingLibrary({ render: false });
      if (
        result.operation === "trash" &&
        payload.session_id === state.recordingPlayerSessionId
      ) {
        clearGlobalTakePlayer();
      }
      let actionMessage =
        {
          start: "Aufnahme läuft; der Start wurde durch Recorder-Readback bestätigt.",
          stop: "Take wurde gestoppt und Recorder-Readback bestätigt.",
          recover: "Recovery wurde ausgeführt und Recorder-Readback bestätigt.",
          categorize: "Kategorie wurde gespeichert und exakt zurückgelesen.",
          trash: "Take wurde in den Papierkorb verschoben.",
          restore: "Take wurde aus dem Papierkorb wiederhergestellt.",
        }[result.operation] || "Recorderaktion bestätigt.";
      let actionTone = "success";
      const verification = result.verification;
      if (result.operation === "stop" || result.operation === "recover") {
        if (verification?.status === "verified") {
          actionMessage = verification.midi
            ? "Take finalisiert; WAV und Roland-MIDI wurden als aktuelle Dateien verifiziert."
            : "Take finalisiert; die WAV-Datei wurde als aktuelle Datei verifiziert.";
        } else if (verification?.status === "unverified") {
          actionMessage =
            "Take wurde beendet, aber die aktuelle Ergebnisdatei konnte nicht vollständig verifiziert werden.";
          actionTone = "info";
        } else if (verification?.status === "not-completed") {
          actionMessage =
            "Recorder wurde beendet; es liegt noch kein vollständig finalisierter Take vor.";
          actionTone = "info";
        }
      }
      showNotice(actionMessage, actionTone);
    }
  } catch (error) {
    showNotice(
      error instanceof Error ? error.message : "Recorderaktion wurde abgewiesen.",
    );
  } finally {
    state.recordingActionPending = false;
    syncRemoteControls();
    if (state.snapshot) renderAll({ preserveRecorderDraft: false });
  }
}

function renderRecordingControls(card, recording) {
  const contract = recording.contract || {};
  const source = contract.source || {};
  const monitoring = contract.monitoring || {};
  const levels = contract.levels || {};
  const session = recording.session || null;
  const writable = recordingActionsAllowed();
  const active = session?.active === true;
  const activeMode =
    session?.session_type === "piano-vocal-performance" ? "piano-vocal" : "voice";
  const selectedMode = (recording.modes || []).find(
    (mode) => mode.id === (active ? activeMode : state.recordingDraft.mode),
  );
  const startActionLabel = "Aufnahme starten";
  const stopActionLabel = "Stop";
  const recoveryActionLabel = "Recovery";
  const pianoVocalProduct = "Stereo-Mix WAV: Gesang + echter Roland-Klang · MIDI zusätzlich";
  const controls = element("section", "recording-controls");
  controls.setAttribute("aria-label", "Aufnahme");
  controls.dataset.recordingState = recordingPresentationState(recording, state.recordingPlan);

  const productHead = element("div", "recording-product-head");
  const productCopy = element("div");
  appendText(productCopy, "p", "eyebrow", active ? "Aufnahme" : "Recorder");
  appendText(
    productCopy,
    "h3",
    "",
    active ? selectedMode?.label || "Aufnahme" : "Was möchtest du aufnehmen?",
  );
  appendText(
    productCopy,
    "p",
    "recording-product-hint",
    active
      ? "Der Recorder läuft. Technische Details bleiben darunter verfügbar."
      : "Startprüfung, Quellenbindung und Dateiname werden beim Start automatisch geprüft.",
  );
  productHead.append(productCopy);
  controls.append(productHead);

  const facts = element("dl", "truth-list recording-truth");
  detailRow(
    facts,
    "Quelle",
    `${source.interface || "MOTU M2"} + ${source.microphone || "RØDE NT1-A"} · ${source.sample_rate_hz || 48000} Hz`,
  );
  detailRow(
    facts,
    "Monitoring",
    monitoring.mode === "hardware-direct"
      ? "MOTU Hardware-Direct · Software-Loopback aus · Latenz minimal erwartet, nicht softwaregemessen"
      : "Monitoring nicht gebunden",
  );
  detailRow(
    facts,
    "Pegelziel",
    Array.isArray(levels.typical_average_dbfs_range) && Array.isArray(levels.peak_dbfs_range)
      ? `Ø ${levels.typical_average_dbfs_range.join("…")} dBFS · Peaks ${levels.peak_dbfs_range.join("…")} dBFS · Referenzziel, kein Live-Messwert`
      : "Referenzpegel nicht lesbar",
  );
  if (session) {
    const physical = session.physical || {};
    detailRow(
      facts,
      "Vor-Ort-Gates",
      `RØDE ${physical.rode_nt1a_connected === true ? "✓" : "offen"} · Eingang ${physical.rode_nt1a_motu_input || "offen"} · 48 V ${physical.motu_phantom_48v || "offen"} · Gain ${physical.motu_input_gain_reference ? "✓" : "offen"} · Pegel-Gate ${session.laboratory?.voice_level_measurement ? "✓" : "offen"}`,
    );
    detailRow(facts, "Plan", shortRevision(session.plan_sha256));
  }

  if (active) {
    const stage = element("section", "recording-live-stage");
    stage.id = "recording-live-stage";
    stage.setAttribute("aria-label", "Laufende Aufnahme");
    appendText(stage, "span", "recording-live-indicator", "● Aufnahme läuft");
    const elapsed = appendText(stage, "strong", "recording-running-time", "00:00");
    elapsed.id = "recording-running-time";
    const meter = element("progress", "recording-live-meter");
    meter.id = "recording-live-level";
    meter.max = 120;
    meter.setAttribute("aria-label", "Aggregierter Live-Eingangspegel");
    stage.append(meter);
    const meterCopy = element("div", "recording-live-meter-copy");
    const meterValue = appendText(meterCopy, "strong", "", "Pegel wird gelesen");
    meterValue.id = "recording-live-level-value";
    const meterSource = appendText(
      meterCopy,
      "small",
      "",
      "MOTU-Eingang · aggregierter Pegel",
    );
    meterSource.id = "recording-live-level-source";
    stage.append(meterCopy);
    const stopButton = element("button", "primary-button recording-stop-button", "■ Aufnahme stoppen");
    stopButton.type = "button";
    stopButton.setAttribute("aria-label", stopActionLabel);
    stopButton.dataset.control = "stop";
    stopButton.disabled = !writable || !active || state.recordingActionPending;
    stopButton.addEventListener("click", () =>
      runRecordingAction({ operation: "stop", session_id: session?.session_id }),
    );
    stage.append(stopButton);
    controls.append(stage);
  } else {
    const modeSwitch = element("div", "recording-mode-switch");
    modeSwitch.setAttribute("role", "group");
    modeSwitch.setAttribute("aria-label", "Aufnahmemodus");
    for (const mode of recording.modes || []) {
      const modeButton = element("button", "secondary-button", mode.label);
      modeButton.type = "button";
      modeButton.dataset.recordingMode = mode.id;
      modeButton.setAttribute(
        "aria-pressed",
        String(state.recordingDraft.mode === mode.id),
      );
      modeButton.disabled = active || state.recordingActionPending;
      modeButton.addEventListener("click", () => {
        const automaticName = state.recordingDraft.automaticName === true;
        state.recordingDraft = {
          ...state.recordingDraft,
          mode: mode.id,
          name: automaticName
            ? nextAutomaticTakeName(mode.id, [state.recordingDraft.name])
            : state.recordingDraft.name,
        };
        state.recordingPlan = null;
        state.recordingPlanInput = null;
        state.recordingDetailsOpen = false;
        renderActiveLanes({ preserveDraft: false });
      });
      modeSwitch.append(modeButton);
    }
    controls.append(modeSwitch);
    appendText(
      controls,
      "p",
      "recording-product-hint",
      state.recordingDraft.mode === "piano-vocal"
        ? `Ergebnis: ${pianoVocalProduct}`
        : "Ergebnis: Gesang als WAV",
    );
    let modeBlockerMessage = null;
    if (selectedMode?.actionable !== true && selectedMode?.blocker) {
      modeBlockerMessage = appendText(
        controls,
        "p",
        selectedMode.blocker === "exact-midi-gate-requires-plan"
          ? "recording-product-hint"
          : "recording-plan",
        `${selectedMode.blocker === "exact-midi-gate-requires-plan" ? "Automatische Startprüfung" : "Modus derzeit blockiert"}: ${
          selectedMode.blocker === "roland-midi-source-not-observed"
            ? "Roland-MIDI-Quelle nicht beobachtet"
            : selectedMode.blocker === "exact-midi-gate-requires-plan"
              ? "exakter MIDI-Port und arecordmidi werden beim Start automatisch gebunden"
              : selectedMode.blocker
        }`,
      );
    }
    const startButton = element("button", "primary-button recording-start-button", "● Aufnahme starten");
    startButton.type = "button";
    startButton.setAttribute("aria-label", startActionLabel);
    startButton.dataset.control = "start";
    const knownModeHardBlocked = recordingModeKnownHardBlocked(selectedMode);
    startButton.disabled = !writable || state.recordingActionPending || knownModeHardBlocked;
    if (knownModeHardBlocked && modeBlockerMessage) {
      modeBlockerMessage.id = "recording-mode-blocker";
      startButton.setAttribute("aria-describedby", modeBlockerMessage.id);
    }
    startButton.addEventListener("click", () => {
      runRecordingStart();
    });
    controls.append(startButton);
  }

  const advanced = element("details", "recording-advanced");
  advanced.open = state.recordingDetailsOpen;
  const advancedSummary = element("summary", "recording-advanced-summary", "Details und Startprüfung");
  advanced.append(advancedSummary, facts);
  advanced.addEventListener("toggle", () => {
    state.recordingDetailsOpen = advanced.open;
  });

  if (!active) {
    const draft = element("div", "recording-draft");
    const nameLabel = element("label", "recording-field");
    appendText(nameLabel, "span", "", "Take-Name");
    const nameInput = element("input", "recording-input");
    nameInput.dataset.control = "take-name";
    nameInput.type = "text";
    nameInput.autocomplete = "off";
    nameInput.spellcheck = false;
    nameInput.maxLength = 128;
    nameInput.value = state.recordingDraft.name;
    nameInput.disabled = !writable || state.recordingActionPending;
    nameLabel.append(nameInput);
    const durationLabel = element("label", "recording-field");
    appendText(durationLabel, "span", "", "Maximale Dauer (Sekunden)");
    const durationInput = element("input", "recording-input");
    durationInput.dataset.control = "maximum-seconds";
    durationInput.type = "number";
    durationInput.min = String(contract.capture?.minimum_duration_seconds || 1);
    durationInput.max = String(contract.capture?.maximum_duration_seconds || 14400);
    durationInput.step = "1";
    durationInput.value = String(state.recordingDraft.maximumSeconds);
    durationInput.disabled = !writable || state.recordingActionPending;
    durationLabel.append(durationInput);
    draft.append(nameLabel, durationLabel);
    advanced.append(draft);

    const invalidatePlan = ({ nameEdited = false } = {}) => {
      const parsedDuration = Number.parseInt(durationInput.value, 10);
      state.recordingDraft = {
        mode: state.recordingDraft.mode,
        name: nameInput.value,
        maximumSeconds: Number.isInteger(parsedDuration) ? parsedDuration : 0,
        automaticName: nameEdited ? false : state.recordingDraft.automaticName === true,
      };
      state.recordingPlan = null;
      state.recordingPlanInput = null;
    };
    nameInput.addEventListener("input", () => invalidatePlan({ nameEdited: true }));
    durationInput.addEventListener("input", () => invalidatePlan());

    const planButton = element("button", "secondary-button", "Plan prüfen");
    planButton.type = "button";
    planButton.dataset.control = "plan";
    planButton.disabled = !writable || state.recordingActionPending;
    planButton.addEventListener("click", () => {
      invalidatePlan();
      state.recordingDetailsOpen = true;
      runRecordingAction({
        operation: "plan",
        mode: state.recordingDraft.mode,
        name: state.recordingDraft.name,
        maximum_seconds: state.recordingDraft.maximumSeconds,
      });
    });
    advanced.append(planButton);
  }

  if (state.recordingPlan) {
    const plan = state.recordingPlan;
    appendText(
      advanced,
      "p",
      plan.ready ? "recording-plan ready" : "recording-plan",
      plan.ready
        ? `Startprüfung ${shortRevision(plan.plan_sha256)} vollständig · Aufnahme ist bereit`
        : "Startprüfung blockiert · offene Punkte stehen direkt darunter",
    );
    const checks = Array.isArray(plan.readiness?.checks) ? plan.readiness.checks : [];
    if (checks.length) {
      const checklist = element("ul", "recording-preflight");
      checklist.setAttribute("aria-label", "Startprüfung");
      for (const check of checks) {
        const ready = check?.status === "ready";
        const item = element(
          "li",
          `recording-preflight-item ${ready ? "ready" : "blocked"}`,
        );
        appendText(item, "span", "recording-preflight-mark", ready ? "✓" : "!").setAttribute(
          "aria-hidden",
          "true",
        );
        const copy = element("span", "recording-preflight-copy");
        appendText(
          copy,
          "strong",
          "",
          RECORDING_PREFLIGHT_LABELS[check.id] || check.id || "Startprüfung",
        );
        appendText(
          copy,
          "small",
          "",
          ready
            ? "bereit"
            : (check.blockers || []).map(recordingBlockerLabel).join(" · ") || "noch offen",
        );
        item.append(copy);
        checklist.append(item);
      }
      advanced.append(checklist);
    }
  }
  controls.append(advanced);

  if (session?.recovery_required === true || session?.cleanup_required === true) {
    const recovery = element("section", "recording-recovery");
    appendText(
      recovery,
      "strong",
      "",
      session.recovery_required === true
        ? "Recorder benötigt Wiederherstellung"
        : "Recorder kann freigegeben werden",
    );
    appendText(
      recovery,
      "p",
      "",
      session.recovery_required === true
        ? "Die vorhandene Aufnahme bleibt erhalten. Recovery prüft die gebundene Sitzung und räumt nur den Recorderzustand auf."
        : "Der Take ist abgeschlossen; der verbliebene Recorderzustand kann jetzt kontrolliert aufgeräumt werden.",
    );
    const recoverButton = element(
      "button",
      "secondary-button",
      session.recovery_required === true ? "Recovery ausführen" : "Recorder freigeben",
    );
    recoverButton.type = "button";
    recoverButton.setAttribute("aria-label", recoveryActionLabel);
    recoverButton.dataset.control = "recovery";
    recoverButton.disabled = !writable || state.recordingActionPending;
    recoverButton.addEventListener("click", () =>
      runRecordingAction({ operation: "recover", session_id: session?.session_id }),
    );
    recovery.append(recoverButton);
    controls.append(recovery);
  }

  if (!writable) {
    appendText(
      controls,
      "p",
      "read-only-boundary",
      state.remoteBridgeProjection === true
        ? "Private Bridge erkannt. Recorderaktionen benötigen eine gültige, identitätsgebundene Fernsession."
        : "Recorderaktionen sind nur am autoritativen lokalen Backend oder über die scoped private Bridge möglich.",
    );
  }
  card.append(controls);
  syncRecordingClock();
}

async function loadRecordingLibrary({ render = true } = {}) {
  if (!backendAllowed()) return;
  try {
    state.recordingLibrary = await fetchJson("/api/v1/recordings", {
      timeoutMs: 15000,
    });
    state.recordingLibraryError = null;
    reconcileGlobalTakePlayer();
    ensureAutomaticTakeNameFree();
  } catch (error) {
    state.recordingLibrary = null;
    state.recordingLibraryError =
      error instanceof Error ? error.message : "Recorderbibliothek ist nicht lesbar.";
  }
  if (render && state.snapshot) {
    renderLibrary();
    renderRecordingRecentTakes();
  }
}

function recordingTakeDurationSeconds(item) {
  const mix = item.result?.artifacts?.mix_wav;
  const vocal = item.result?.artifacts?.vocal_wav;
  const artifact = item.result?.artifact;
  const seconds = mix?.duration_seconds ?? vocal?.duration_seconds ?? artifact?.duration_seconds;
  return Number.isFinite(Number(seconds)) ? Number(seconds) : null;
}

function recordingTakeDuration(item) {
  const seconds = recordingTakeDurationSeconds(item);
  return seconds === null ? null : `${seconds.toFixed(2)} s`;
}

function recordingCategoryLabel(value) {
  return RECORDING_LIBRARY_CATEGORIES[value] || "Unsortiert";
}

function recordingTakeProduct(item) {
  if (item.session_type !== "piano-vocal-performance") return "Gesang · WAV";
  return item.result?.artifacts?.mix_wav
    ? "Klavier + Gesang · Stereo-Mix WAV + MIDI"
    : "Klavier: MIDI-only · Gesang WAV (Legacy-Take)";
}

function globalTakePlayerItemName(item) {
  return item.name || `Take ${shortRevision(item.session_id)}`;
}

function renderGlobalTakePlayer(item) {
  const player = byId("global-take-player");
  if (!player) return;
  const name = globalTakePlayerItemName(item);
  byId("global-take-player-title").textContent = name;
  byId("global-take-player-audio").setAttribute("aria-label", `${name} abspielen`);
  const duration = recordingTakeDuration(item);
  byId("global-take-player-meta").textContent =
    `${recordingTakeProduct(item)}${duration ? ` · ${duration}` : ""}`;
  player.hidden = false;
  document.body.classList.add("take-player-active");
}

function clearGlobalTakePlayer({ restoreFocus = false } = {}) {
  const returnFocus = state.recordingPlayerReturnFocus;
  const audio = byId("global-take-player-audio");
  if (audio) {
    audio.pause();
    audio.removeAttribute("src");
    audio.removeAttribute("aria-label");
    audio.load();
  }
  state.recordingPlayerSessionId = null;
  state.recordingPlayerAudioUrl = null;
  state.recordingPlayerReturnFocus = null;
  const player = byId("global-take-player");
  if (player) player.hidden = true;
  document.body.classList.remove("take-player-active");
  if (restoreFocus) {
    const returnTarget =
      returnFocus?.isConnected && !returnFocus.closest("[hidden]")
        ? returnFocus
        : byId("main-content");
    returnTarget?.focus({ preventScroll: true });
  }
}

function playRecordingTake(item, trigger = null) {
  if (
    item?.status !== "completed" ||
    item.library?.trashed === true ||
    typeof item.audio_url !== "string"
  ) {
    return;
  }
  const audio = byId("global-take-player-audio");
  if (!audio) return;
  if (trigger?.isConnected) state.recordingPlayerReturnFocus = trigger;
  const sameTake =
    state.recordingPlayerSessionId === item.session_id &&
    state.recordingPlayerAudioUrl === item.audio_url;
  if (!sameTake) {
    audio.pause();
    audio.src = item.audio_url;
    audio.load();
    state.recordingPlayerSessionId = item.session_id;
    state.recordingPlayerAudioUrl = item.audio_url;
  } else if (audio.ended) {
    audio.currentTime = 0;
  }
  renderGlobalTakePlayer(item);
  const playback = audio.play();
  if (playback && typeof playback.catch === "function") {
    playback.catch(() =>
      showNotice(
        "Wiedergabe konnte nicht gestartet werden; der Take bleibt im Player geöffnet.",
        "info",
      ),
    );
  }
}

function reconcileGlobalTakePlayer() {
  if (!state.recordingPlayerSessionId || !state.recordingLibrary) return;
  const items = Array.isArray(state.recordingLibrary.items)
    ? state.recordingLibrary.items
    : [];
  const current = items.find(
    (item) => item.session_id === state.recordingPlayerSessionId,
  );
  if (!current) {
    if (state.recordingLibrary.truncated !== true) clearGlobalTakePlayer();
    return;
  }
  if (
    current.status !== "completed" ||
    current.library?.trashed === true ||
    current.audio_url !== state.recordingPlayerAudioUrl
  ) {
    clearGlobalTakePlayer();
    return;
  }
  renderGlobalTakePlayer(current);
}

function appendTakeListenButton(parent, item) {
  if (item.status !== "completed" || typeof item.audio_url !== "string") return;
  const listen = element(
    "button",
    "secondary-button recording-listen-button",
    "Anhören",
  );
  listen.type = "button";
  listen.setAttribute("aria-label", `${globalTakePlayerItemName(item)} anhören`);
  listen.addEventListener("click", (event) =>
    playRecordingTake(item, event.currentTarget),
  );
  parent.append(listen);
}

function appendTakeExports(parent, item) {
  if (item.status !== "completed") return;
  const exports = element("div", "recording-take-exports");
  if (typeof item.audio_url === "string") {
    const wav = element("a", "secondary-button", "WAV sichern");
    wav.href = item.audio_url;
    wav.download = item.name || `take-${shortRevision(item.session_id)}.wav`;
    wav.setAttribute("aria-label", `WAV von ${item.name || shortRevision(item.session_id)} sichern`);
    exports.append(wav);
  }
  if (typeof item.midi_url === "string") {
    const midi = element("a", "secondary-button", "MIDI sichern");
    midi.href = item.midi_url;
    midi.download = typeof item.name === "string" && item.name.endsWith(".wav")
      ? `${item.name.slice(0, -4)}.mid`
      : `take-${shortRevision(item.session_id)}.mid`;
    midi.setAttribute("aria-label", `Roland-MIDI von ${item.name || shortRevision(item.session_id)} sichern`);
    exports.append(midi);
  }
  if (exports.childElementCount) parent.append(exports);
}

function renderRecordingRecentTakes() {
  const target = byId("recording-recent-takes");
  if (!target) return;
  const library = state.recordingLibrary;
  if (!library) {
    target.replaceChildren(
      element(
        "div",
        "empty-state",
        state.recordingLibraryError || "Aufnahmen werden geladen.",
      ),
    );
    return;
  }
  const items = Array.isArray(library.items)
    ? library.items
        .filter((item) => item.status === "completed" && item.library?.trashed !== true)
        .slice(0, 3)
    : [];
  if (!items.length) {
    target.replaceChildren(
      element("div", "empty-state", "Noch keine Aufnahme vorhanden."),
    );
    return;
  }
  target.replaceChildren(
    ...items.map((item) => {
      const card = element("article", "recording-recent-card");
      const head = element("div", "recording-recent-card-head");
      const copy = element("div");
      appendText(copy, "strong", "", item.name || `Take ${shortRevision(item.session_id)}`);
      const duration = recordingTakeDuration(item);
      appendText(
        copy,
        "small",
        "",
        `${recordingTakeProduct(item)}${duration ? ` · ${duration}` : ""} · ${formatDateTime(item.created_at)}`,
      );
      head.append(copy);
      appendText(
        head,
        "span",
        `status-pill ${item.status === "completed" ? "ready" : ""}`,
        recordingStatusLabel(item.status),
      );
      card.append(head);
      appendTakeListenButton(card, item);
      appendTakeExports(card, item);
      return card;
    }),
  );
}

function captureRecorderInteraction(workspace) {
  const active = document.activeElement;
  if (!workspace.contains(active)) return null;
  return {
    control: active.dataset.control || "recorder-workspace",
    value: "value" in active ? active.value : null,
    selectionStart: active.selectionStart,
    selectionEnd: active.selectionEnd,
    selectionDirection: active.selectionDirection,
  };
}

function restoreRecorderInteraction(workspace, interaction, { preserveDraft }) {
  if (!interaction) return;
  const target = [...workspace.querySelectorAll("[data-control]")].find(
    (control) => control.dataset.control === interaction.control,
  );
  const applicable = target && !target.disabled && !target.closest("[hidden]");
  const focusTarget = applicable ? target : workspace;
  if (applicable && preserveDraft && interaction.value !== null) {
    focusTarget.value = interaction.value;
  }
  focusTarget.focus({ preventScroll: true });
  if (
    applicable &&
    preserveDraft &&
    Number.isInteger(interaction.selectionStart) &&
    Number.isInteger(interaction.selectionEnd)
  ) {
    focusTarget.setSelectionRange(
      interaction.selectionStart,
      interaction.selectionEnd,
      interaction.selectionDirection || "none",
    );
  }
}

function reconcileKeyedChildren(parent, children) {
  const retained = new Set(children);
  for (const child of [...parent.children]) {
    if (!retained.has(child)) child.remove();
  }
  children.forEach((child, index) => {
    const current = parent.children[index] || null;
    if (current !== child) parent.insertBefore(child, current);
  });
}

function renderActiveLanes({ preserveDraft = true } = {}) {
  const snapshot = state.snapshot;
  const graph = snapshot.doctor?.graph || {};
  const whale = snapshot.whale || {};
  const recording = snapshot.recording || {};
  const container = byId("now-signal-lanes");
  const workspace = byId("recorder-workspace");
  const interaction = captureRecorderInteraction(workspace);
  const existingCards = new Map(
    [...container.children]
      .filter((card) => card.dataset.lane)
      .map((card) => [card.dataset.lane, card]),
  );
  const lanes = [
    {
      key: "listening",
      name: "Hören",
      path: `${formatEndpoint(graph.default_source)} → ${formatEndpoint(graph.default_sink)}`,
      observed: snapshot.doctor?.status === "ok" ? "Route gelesen" : "nicht lesbar",
      configured: graph.force_rate_hz ? `${graph.force_rate_hz} Hz` : "offen",
      physical: snapshot.summary?.physical_unknown_count ? "Belege offen" : "belegt",
      executable: "keine Apply-Aktion",
    },
    {
      key: "playing",
      name: "Spielen",
      path: `Roland → ${displayMode(whale.service?.voice_mode || whale.contract?.default_mode)}`,
      observed: whale.status === "ok" ? (whale.service?.active ? "aktiv" : "inaktiv") : "nicht lesbar",
      configured: `${whale.contract?.keyboard?.key_count || 88} Tasten`,
      physical: snapshot.presence?.observed?.roland_fp_30x ? "Roland beobachtet" : "Roland offen",
      executable: "in T020 gesperrt",
    },
    {
      key: "recording",
      name: "Aufnehmen",
      path: "MOTU M2 → unveränderlicher Take",
      observed: snapshot.presence?.observed?.motu_m2 ? "MOTU beobachtet" : "MOTU nicht beobachtet",
      configured: recordingStatusLabel(recording.status),
      physical: recording.session?.physical?.rode_nt1a_connected === true
        ? "RØDE + Voice-Gates im Recorderplan belegt"
        : "Mikrofon-/48-V-/Gain-/Pegel-Gates im Plan zu belegen",
      executable: recordingActionsAllowed()
        ? "Voice Plan/Start/Stop/Recovery"
        : state.remoteBridgeProjection === true
          ? "Read-only-Bridge"
          : "fail-closed",
      kind: "recording",
    },
  ];
  const cards = lanes.filter((lane) => lane.key === "recording").map((lane) => {
    const card = existingCards.get(lane.key) || element("article", "lane-card");
    card.dataset.lane = lane.key;
    card.replaceChildren();
    appendText(card, "p", "eyebrow", lane.name);
    appendText(card, "h3", "", lane.path);
    const list = element("dl", "truth-list");
    detailRow(list, "Beobachtet", lane.observed);
    detailRow(list, "Konfiguriert", lane.configured);
    detailRow(list, "Physisch offen", lane.physical);
    detailRow(list, "Ausführbar", lane.executable);
    card.append(list);
    if (lane.kind === "recording") renderRecordingControls(card, recording);
    return card;
  });
  reconcileKeyedChildren(container, cards);
  restoreRecorderInteraction(workspace, interaction, { preserveDraft });
}

function homeProfile(profileId) {
  return (state.snapshot?.profiles || []).find((profile) => profile.id === profileId) || null;
}

function homeProfileStatus(profileId) {
  const profile = homeProfile(profileId);
  if (!profile) return { label: "nicht katalogisiert", tone: "planned" };
  const stateName = profileState(profile);
  return {
    label: PROFILE_STATE_LABELS[stateName] || stateName,
    tone: profileStateTone(stateName),
  };
}

function homeActionCard({ href, glyph, eyebrow, title, status, tone, detail }) {
  const card = element("a", "home-action-card");
  card.href = href;
  appendText(card, "span", "home-action-glyph", glyph).setAttribute("aria-hidden", "true");
  const copy = element("span", "home-action-copy");
  appendText(copy, "span", "eyebrow", eyebrow);
  appendText(copy, "strong", "", title);
  appendText(copy, "small", "", detail);
  card.append(copy);
  appendText(card, "span", `home-action-state ${tone || "planned"}`, status);
  appendText(card, "span", "home-action-arrow", "→").setAttribute("aria-hidden", "true");
  return card;
}

const SIGNAL_GLYPHS = Object.freeze({
  Quelle: "≋",
  "Quelle · Desktop": "▱",
  "Quelle · Mobil": "▯",
  "Zentraler Knoten": "▣",
  Interface: "▣",
  Verstärker: "▥",
  Kopfhörer: "◖",
  "PC-Ausgang": "▱",
  Receiver: "▤",
  Lautsprecher: "◎",
});

function homeSignalNode(label, value, detail, tone = "configured") {
  const node = element("article", `home-signal-node ${tone}`);
  appendText(node, "span", "signal-node-glyph", SIGNAL_GLYPHS[label] || "◇").setAttribute(
    "aria-hidden",
    "true",
  );
  appendText(node, "span", "eyebrow", label);
  appendText(node, "strong", "", value);
  appendText(node, "small", "", detail);
  return node;
}

function listeningPathCard(title, subtitle, nodes, tone = "reference") {
  const path = element("section", `listening-path-card is-${tone}`);
  path.dataset.pathTone = tone;
  const heading = element("div", "listening-path-heading");
  appendText(heading, "strong", "", title);
  appendText(heading, "small", "", subtitle);
  path.append(heading);
  const flow = element("div", "listening-path-flow");
  flow.append(...nodes);
  path.append(flow);
  return path;
}

function listeningTopology(inputs, hub, branches) {
  const topology = element("section", "listening-topology");
  topology.setAttribute("aria-label", "Wiedergabetopologie");

  const inputStage = element("div", "listening-input-stage");
  appendText(inputStage, "span", "topology-stage-label", "Quelloptionen");
  const inputGrid = element("div", "listening-input-grid");
  inputGrid.append(...inputs);
  inputStage.append(inputGrid);

  const hubStage = element("div", "listening-hub-stage");
  hubStage.append(hub);

  const branchGrid = element("div", "listening-branch-grid");
  branchGrid.append(...branches);
  topology.append(inputStage, hubStage, branchGrid);
  return topology;
}

function renderHome() {
  const snapshot = state.snapshot;
  const summary = snapshot.summary || {};
  const doctor = snapshot.doctor || {};
  const graph = doctor.graph || {};
  const qobuz = doctor.streaming_sources?.qobuz || {};
  const qbzd = qobuz.qbzd || {};
  const qbzdAudio = qbzd.audio || {};
  const qobuzVerified = qobuz.track_native_proven === true;
  const qobuzReady = qobuz.reference_provider_ready === true;
  const qobuzProvider = qobuz.selected_reference_provider || null;
  const qobuzRate =
    qobuzVerified && Number.isFinite(qbzdAudio.sample_rate_hz)
      ? qbzdAudio.sample_rate_hz
      : null;
  let qobuzSourceTitle = "Heim-PC / Qobuz";
  let qobuzSourceDetail = "Qobuz-Pfad offen";
  let qobuzSourceTone = "onsite";
  if (qobuzProvider === "qbzd-qconnect") {
    qobuzSourceTitle = "Qobuz / QBZD";
    if (qobuzVerified) {
      const nativeRate = qobuzRate
        ? `${(qobuzRate / 1000).toFixed(qobuzRate % 1000 === 0 ? 0 : 1)} kHz · `
        : "";
      qobuzSourceDetail = `Qobuz Connect · ${nativeRate}TRACK-NATIVE ✓`;
      qobuzSourceTone = "observed";
    } else if (qobuzReady) {
      const proofStateDetail = {
        "hardware-preparing": "Qobuz Connect · Referenzpfad wird vorbereitet",
        "ready-awaiting-playback": "Qobuz Connect · Referenzpfad bereit · Ratebeleg bei Wiedergabe",
        "rate-mismatch": "Qobuz Connect · Rate-Mismatch · track-native nicht belegt",
        "motu-rate-unreadable": "Qobuz Connect · MOTU-Rate nicht lesbar · track-native nicht belegt",
        "qbzd-rate-unreadable": "Qobuz Connect · QBZD-Rate nicht lesbar · track-native nicht belegt",
        "hardware-owner-unverified": "Qobuz Connect · MOTU-Owner nicht QBZD · track-native nicht belegt",
        "hardware-snapshot-unstable": "Qobuz Connect · MOTU-Snapshot instabil · track-native nicht belegt",
        "qbzd-snapshot-unstable": "Qobuz Connect · QBZD-Snapshot instabil · track-native nicht belegt",
        "qbzd-snapshot-unavailable": "Qobuz Connect · QBZD-Doppelbeleg offen · track-native nicht belegt",
        "direct-hardware-not-reported": "Qobuz Connect · DirectHardware nicht belegt",
      };
      qobuzSourceDetail =
        proofStateDetail[qobuz.rate_proof_state] ||
        "Qobuz Connect · Referenzpfad bereit · aktueller Ratebeleg offen";
      qobuzSourceTone = "configured";
    }
  } else if (qobuzProvider === "mopidy-legacy") {
    qobuzSourceTitle = "Qobuz / Legacy-Mopidy";
    qobuzSourceDetail = "Legacy-Mopidy · gemischter Pfad · track-native nicht belegt";
    qobuzSourceTone = "configured";
  }
  const presence = snapshot.presence || {};
  const deployment = snapshot.deployment || {};
  const recording = snapshot.recording || {};
  const whaleStatusReadable = snapshot.whale.status === "ok";
  const activeWhale = whaleStatusReadable && summary.active_whale;
  const runtimeHealthy = summary.runtime_state === "healthy";
  const hardwareOffline = presence.state === "offline";
  const motuObserved = presence.observed?.motu_m2 === true;
  const rolandObserved = presence.observed?.roland_fp_30x === true;
  const rate = graph.force_rate_hz || graph.rate_hz || null;
  const quantum = graph.force_quantum_frames || graph.quantum_frames || null;
  const takeCount = Array.isArray(state.recordingLibrary?.items)
    ? state.recordingLibrary.items.length
    : null;
  const card = byId("home-state-card");
  card.replaceChildren();

  const statusSymbol = element(
    "span",
    `state-symbol${runtimeHealthy ? "" : " attention"}`,
    runtimeHealthy ? "✓" : "!",
  );
  statusSymbol.setAttribute("aria-hidden", "true");
  card.append(statusSymbol);

  const copy = element("div");
  appendText(
    copy,
    "h2",
    "",
    runtimeHealthy
      ? hardwareOffline
        ? "Zentrale bereit · Hardware nicht gesehen"
        : "Zentrale bereit · Geräte gelesen"
      : "Systemzustand prüfen",
  );
  appendText(
    copy,
    "p",
    "",
    runtimeHealthy
      ? `${formatEndpoint(graph.default_sink)}${rate ? ` · ${rate} Hz` : ""} · ${deployment.in_sync ? "Deployment synchron" : deploymentStateLabel(deployment.status)}`
      : "Mindestens eine Laufzeitwahrheit ist nicht verlässlich lesbar.",
  );
  card.append(copy);

  const foot = element("div", "state-foot home-state-foot");
  appendText(foot, "span", motuObserved ? "is-positive" : "", motuObserved ? "MOTU M2 beobachtet" : "MOTU M2 offen");
  appendText(foot, "span", rolandObserved ? "is-positive" : "", rolandObserved ? "Roland beobachtet" : "Roland offen");
  appendText(foot, "span", "", recordingStatusLabel(recording.status));
  card.append(foot);

  const listening = homeProfileStatus("reference-listening");
  const recordingProfile = homeProfileStatus("voice-recording");
  const playingProfile = homeProfileStatus("piano-software-live");
  const whaleMode = displayMode(snapshot.whale.service?.voice_mode || snapshot.whale.contract?.default_mode);
  const actions = [
    {
      href: "#hoeren",
      glyph: "◖",
      eyebrow: "Wiedergabe",
      title: "Hören",
      status: listening.label,
      tone: listening.tone,
      detail: `${formatEndpoint(graph.default_sink)}${rate ? ` · ${rate} Hz` : ""}`,
    },
    {
      href: "#aufnehmen",
      glyph: "●",
      eyebrow: "Recorder",
      title: "Aufnehmen",
      status: recording.status === "running" ? "läuft" : recordingProfile.label,
      tone: recording.status === "running" || recordingActionsAllowed() ? "ready" : recordingProfile.tone,
      detail: `${recordingStatusLabel(recording.status)} · ${motuObserved ? "MOTU da" : "MOTU offen"}`,
    },
    {
      href: "#spielen",
      glyph: "♬",
      eyebrow: "Instrumente",
      title: "Spielen",
      status: activeWhale ? `${whaleMode} aktiv` : playingProfile.label,
      tone: activeWhale ? "ready" : playingProfile.tone,
      detail: rolandObserved ? "Roland FP-30X beobachtet" : "Roland FP-30X nicht beobachtet",
    },
    {
      href: "#material",
      glyph: "≋",
      eyebrow: "Aufnahmen",
      title: "Bibliothek",
      status: whaleStatusReadable
        ? takeCount === null
          ? "Bibliothek"
          : `${takeCount} Take${takeCount === 1 ? "" : "s"}`
        : "Walstatus nicht lesbar",
      tone: "planned",
      detail: whaleStatusReadable
        ? activeWhale
          ? `${whaleMode} · Wal aktiv`
          : "Walstimmen, Takes und Replay"
        : "Replay verfügbar · Livezustand nicht lesbar",
    },
  ];
  byId("home-actions").replaceChildren(...actions.map(homeActionCard));

  const external = doctor.external_endpoints || {};
  const receiverProfile = homeProfile("receiver");
  const receiverPhysicalOpen = (receiverProfile?.unresolved_physical_fact_count ?? 0) > 0;
  const pioneerObserved = external.pioneer_vsx_830_k?.software_observed === true;
  const pioneerTone = pioneerObserved ? "observed" : receiverPhysicalOpen ? "onsite" : "configured";
  const pioneerDetail = pioneerObserved
    ? "softwareseitig beobachtet"
    : receiverPhysicalOpen
      ? "RCA/Cinch am MOTU belegt · Receiver-Eingang / Hörmodus / Pegel offen"
      : "physische Receiver-Gates belegt";

  byId("home-signal-caption").textContent =
    `${rate ? `${rate} Hz · ` : ""}2 Quellarten → MOTU M2 · Softwareziel ${formatEndpoint(graph.default_sink)}`;
  byId("home-signal-flow").replaceChildren(
    listeningTopology(
      [
        homeSignalNode("Quelle · Desktop", qobuzSourceTitle, qobuzSourceDetail, qobuzSourceTone),
        homeSignalNode(
          "Quelle · Mobil",
          "iPad / Handy",
          "mögliche Wiedergabequelle · Anschlussweg offen",
          "onsite",
        ),
      ],
      homeSignalNode(
        "Zentraler Knoten",
        "MOTU M2",
        motuObserved
          ? "aktuell beobachtet · DAC / Audiointerface"
          : "DAC / Audiointerface · aktuell nicht beobachtet",
        motuObserved ? "observed" : "onsite",
      ),
      [
        listeningPathCard("Kopfhörer · Referenz", "MOTU Monitor Out (MOTU-seitig TRS belegt) → Lake People → Focal", [
          homeSignalNode("Verstärker", "Lake People G111 Mk 2", "Kopfhörerverstärker", "configured"),
          homeSignalNode("Kopfhörer", "Focal Clear MG", "Referenzabhöre", "configured"),
        ], "reference"),
        listeningPathCard("Lautsprecher · Receiver", "MOTU RCA/Cinch (spiegelt Monitor 1/2) → Pioneer → ELAC/Canton", [
          homeSignalNode("Receiver", "Pioneer VSX-830-K", pioneerDetail, pioneerTone),
          homeSignalNode(
            "Lautsprecher",
            "ELAC + Canton",
            "2× ELAC FS 109.2 · Canton Center + 4 Satelliten · kein Subwoofer · aktive Zuordnung offen",
            "onsite",
          ),
        ], "receiver"),
      ],
    ),
  );

  const metrics = [
    ["Samplerate", rate ? `${rate / 1000} kHz` : "offen", "aktueller Graph"],
    ["Puffer", quantum ? `${quantum} Frames` : "offen", "aktueller Quantum"],
    ["MOTU M2", motuObserved ? "beobachtet" : "offen", "zentraler Hörknoten"],
    ["Ausgaben", "Focal + Pioneer", "Kopfhörer + Lautsprecher"],
  ];
  const metricGlyphs = { Samplerate: "≋", Puffer: "◫", "MOTU M2": "◖", Ausgaben: "◎" };
  byId("home-metrics").replaceChildren(
    ...metrics.map(([label, value, description]) => {
      const metric = element("article", "metric-card listen-metric-card");
      metric.dataset.metric = label.toLowerCase().replaceAll(" ", "-");
      appendText(metric, "span", "metric-glyph", metricGlyphs[label] || "◇").setAttribute(
        "aria-hidden",
        "true",
      );
      const copy = element("div", "metric-copy");
      appendText(copy, "p", "eyebrow", label);
      appendText(copy, "strong", "", value);
      appendText(copy, "span", "", description);
      metric.append(copy);
      return metric;
    }),
  );

  const readinessAreas = [
    ["Hören", "listening", "hoeren"],
    ["Spielen", "playing", "spielen"],
    ["Aufnehmen", "recording", "aufnehmen"],
  ];
  byId("home-readiness").replaceChildren(
    ...readinessAreas.map(([label, area, alias]) => {
      const profiles = profilesByArea(area);
      const readiness = element("a", "readiness-card");
      readiness.href = `#${alias}`;
      const heading = element("div", "readiness-heading");
      appendText(heading, "h3", "", label);
      appendText(heading, "span", "", `${profiles.length} Profile`);
      readiness.append(heading);
      const states = element("div", "readiness-states");
      for (const profile of profiles) {
        const stateName = profileState(profile);
        appendText(
          states,
          "span",
          `readiness-chip ${profileStateTone(stateName)}`,
          `${displayProfile(profile.id)} · ${PROFILE_STATE_LABELS[stateName] || stateName}`,
        );
      }
      readiness.append(states);
      return readiness;
    }),
  );

  const warnings = Array.isArray(doctor.warnings) ? doctor.warnings.slice(0, 3) : [];
  if (warnings.length === 0) {
    byId("home-insights").replaceChildren(
      insightCard("System", doctor.status === "ok" ? "Keine Doctor-Warnungen" : "Doctor nicht lesbar", doctor.status === "ok" ? "ok" : "high"),
      insightCard(
        "Vor Ort",
        `${summary.physical_unknown_count || 0} Belege offen`,
        summary.physical_unknown_count ? "onsite" : "ok",
      ),
      insightCard("Deployment", deployment.in_sync ? "Runtime und Quelle synchron" : deploymentStateLabel(deployment.status), deployment.in_sync ? "ok" : "medium"),
    );
    return;
  }
  byId("home-insights").replaceChildren(
    ...warnings.map((warning) => {
      const onsite =
        warning.code === "voice-source-not-motu" &&
        presence.observed?.motu_m2 !== true;
      return insightCard(
        WARNING_LABELS[warning.code] || warning.code || "Hinweis",
        warning.detail || "Doctor-Hinweis ohne Detail",
        onsite ? "onsite" : warning.severity || "medium",
      );
    }),
  );
}

function insightCard(title, detail, severity) {
  const card = element("article", "insight-card");
  const dot = element("span", `insight-dot ${severity}`);
  dot.setAttribute("aria-hidden", "true");
  card.append(dot);
  const copy = element("div");
  appendText(copy, "h3", "", title);
  appendText(copy, "p", "", detail);
  card.append(copy);
  return card;
}

function profilesFor(area) {
  if (!state.snapshot || !Array.isArray(state.snapshot.profiles)) return [];
  return state.snapshot.profiles.filter((profile) => profile.area === area);
}

function renderWhale() {
  const whale = state.snapshot.whale || {};
  const service = whale.service || {};
  const contract = whale.contract || {};
  const active = whale.status === "ok" && service.active === true;
  const currentMode = service.voice_mode || contract.default_mode;
  const statusReadable = whale.status === "ok";
  const keyboard = contract.keyboard || {};
  const writable = whaleActionsAllowed();
  const focusedMode =
    document.activeElement?.matches?.('input[name="whale-mode"]') === true
      ? document.activeElement.value
      : null;
  const modes = Array.isArray(contract.modes) ? contract.modes : [];
  const modeIds = new Set(modes.map((mode) => mode.id));
  if (
    !writable ||
    !modeIds.has(state.whaleModeDraft) ||
    state.whaleModeDraft === currentMode
  ) {
    state.whaleModeDraft = null;
  }
  const selectedMode = state.whaleModeDraft || currentMode;

  const intro = byId("play-intro-stat");
  intro.replaceChildren();
  appendText(intro, "strong", "", active ? "Aktiv" : statusReadable ? "Inaktiv" : "Nicht lesbar");
  appendText(
    intro,
    "span",
    "",
    writable
      ? active
        ? `${displayMode(currentMode)} · ${remoteWhaleActionsAllowed() ? "vom iPad steuerbar" : "lokal steuerbar"}`
        : remoteWhaleActionsAllowed()
          ? "Walstimme über private iPad-Bridge steuerbar"
          : "Walstimme direkt am Heim-PC steuerbar"
      : state.remoteBridgeProjection === true
        ? "Fernansicht read-only · Wal-Fernsession nicht belegt"
        : "Wirkende Steuerung nur über den lokalen Heim-PC",
  );

  const wrapper = element("div", `whale-panel${writable ? "" : " read-only"}`);
  wrapper.setAttribute("aria-busy", String(state.whaleActionPending));
  const main = element("article", "whale-main");
  const title = element("div", "whale-title");
  const copy = element("div");
  appendText(copy, "p", "eyebrow", "Buckelwal Live Voice");
  appendText(copy, "h3", "", active ? "Die Walstimme ist aktiv." : "Walstimme auswählen und starten");
  appendText(copy, "p", "", active
    ? `${service.midi_port || "MIDI automatisch"} → ${service.target || "PipeWire-Standard"}`
    : `${keyboard.key_count || 88} Tasten · ${keyboard.lowest_key || "A0"} bis ${keyboard.highest_key || "C8"}`);
  title.append(copy);
  appendText(title, "span", `status-pill ${active ? "ready" : statusReadable ? "" : "unavailable"}`, active ? "läuft" : statusReadable ? "inaktiv" : "unbekannt");
  main.append(title);

  const picker = element("fieldset", "mode-picker");
  appendText(picker, "legend", "", "Walstimme / Klangcharakter");
  for (const mode of modes) {
    const label = element("label", "mode-choice");
    const input = element("input");
    input.type = "radio";
    input.name = "whale-mode";
    input.value = mode.id;
    input.checked = mode.id === selectedMode;
    input.disabled = state.whaleActionPending || !statusReadable || !writable;
    label.append(input, element("span", "", displayMode(mode.id)));
    picker.append(label);
  }
  main.append(picker);
  picker.addEventListener("change", (event) => {
    if (event.target?.name !== "whale-mode") return;
    state.whaleModeDraft = event.target.value;
  });

  const actions = element("div", "card-actions");
  const actionButton = element("button", active ? "primary-button danger" : "primary-button", active ? "Walstimme beenden" : "Walstimme starten");
  actionButton.type = "button";
  actionButton.id = "whale-primary-action";
  actionButton.disabled = state.loading || state.whaleActionPending || !statusReadable || !writable;
  actionButton.addEventListener("click", () => active ? runWhaleAction("stop") : runWhaleAction("start", selectedWhaleMode()));
  actions.append(actionButton);
  if (active) {
    const modeButton = element("button", "secondary-button", "Modus übernehmen");
    modeButton.type = "button";
    modeButton.id = "whale-mode-action";
    const syncModeButton = () => {
      modeButton.disabled = state.loading || state.whaleActionPending || !writable || selectedWhaleMode() === currentMode;
    };
    syncModeButton();
    picker.addEventListener("change", syncModeButton);
    modeButton.addEventListener("click", () => runWhaleAction("mode", selectedWhaleMode()));
    actions.append(modeButton);
  }
  main.append(actions);

  if (!writable) appendText(main, "p", "read-only-boundary", state.remoteBridgeProjection === true
    ? state.remoteWhaleSessionError || "Die Fern-Audiozentrale bleibt außerhalb der eng begrenzten Wal-Session read-only."
    : "Start, Stop und Moduswechsel werden erst freigeschaltet, wenn die lokale Backend-Autorität samt Aktionstoken belegt ist.");

  const details = element("aside", "whale-details");
  appendText(details, "p", "eyebrow", "Instrumentbezug");
  appendText(details, "h3", "", statusReadable ? "Laufzeit" : "Nicht lesbar");
  const list = element("dl");
  detailRow(list, "Modus", active ? displayMode(currentMode) : displayMode(contract.default_mode));
  detailRow(list, "MIDI-Port", service.midi_port || "automatisch");
  detailRow(list, "Ausgabe", service.target || "PipeWire-Standard");
  detailRow(list, "Roland", state.snapshot.presence?.observed?.roland_fp_30x ? "FP-30X beobachtet" : "vor Ort nicht belegt");
  detailRow(
    list,
    "Ausführbar",
    writable
      ? remoteWhaleActionsAllowed()
        ? "Start / Modus / Stop · private iPad-Bridge"
        : "Start / Modus / Stop · lokal"
      : "keine wirkende Autorität belegt",
  );
  details.append(list);
  if (whale.error) appendText(details, "p", "dialog-message", whale.error);
  wrapper.append(main, details);
  const whaleControl = byId("whale-control");
  whaleControl.replaceChildren(wrapper);
  if (focusedMode && state.route === "spielen") {
    const replacement = [...whaleControl.querySelectorAll('input[name="whale-mode"]')].find(
      (input) => input.value === focusedMode,
    );
    if (replacement && !replacement.disabled) {
      replacement.focus({ preventScroll: true });
    }
  }
}

function selectedWhaleMode() {
  if (typeof state.whaleModeDraft === "string") return state.whaleModeDraft;
  const checked = document.querySelector('input[name="whale-mode"]:checked');
  return checked ? checked.value : state.snapshot?.whale?.contract?.default_mode;
}

function setWhalePending(pending) {
  const panel = byId("whale-control").querySelector(".whale-panel");
  if (!panel) return;
  panel.setAttribute("aria-busy", String(pending));
  for (const control of panel.querySelectorAll("button, input")) control.disabled = pending;
}

async function runWhaleAction(operation, mode) {
  if (!state.snapshot || state.loading || state.whaleActionPending) return;
  state.whaleActionPending = true;
  clearNotice();
  setWhalePending(true);
  syncRemoteControls();
  try {
    if (state.remoteBridgeProjection === true && !remoteWhaleSessionFresh()) {
      await ensureRemoteWhaleSession({ force: true });
    }
    if (!whaleActionsAllowed()) {
      throw new Error(
        state.remoteWhaleSessionError || "Walsteuerung ist aktuell nicht autorisiert.",
      );
    }
    const payload = { operation };
    if (mode) payload.mode = mode;
    const result = await postWhaleAction(payload);
    if (result?.kind !== "audio_control_action_result" || !result.snapshot) throw new Error("Buckelwal-Aktion kam ohne autoritativen Readback zurück.");
    state.whaleModeDraft = null;
    state.snapshot = result.snapshot;
    renderAll();
    const confirmedMode = result.mode || mode;
    showNotice(operation === "stop" ? "Walstimme wurde beendet und als inaktiv zurückgelesen." : `Walstimme wurde als ${displayMode(confirmedMode)} aktiv zurückgelesen.`, "success");
  } catch (error) {
    if (state.remoteBridgeProjection === true && [401, 403].includes(error?.status)) {
      state.remoteWhaleSessionToken = null;
      state.remoteWhaleSessionExpiresAt = 0;
    }
    showNotice(error instanceof Error ? error.message : "Walstimmen-Aktion wurde blockiert.");
  } finally {
    state.whaleActionPending = false;
    syncRemoteControls();
    if (state.snapshot) renderWhale();
    byId("whale-primary-action")?.focus({ preventScroll: true });
  }
}

function detailRow(list, term, detail) {
  const row = element("div");
  appendText(row, "dt", "", term);
  appendText(row, "dd", "", detail);
  list.append(row);
}

function renderProfiles() {
  renderProfileGrid("playing-profiles", profilesFor("playing"));
  renderProfileGrid("recording-profiles", profilesFor("recording"));
  renderProfileGrid("listening-profiles", profilesFor("listening"));
  const recording = state.snapshot.recording;
  const boundary = byId("recording-boundary");
  boundary.replaceChildren();
  appendText(boundary, "strong", "", "Aufnahmepfad");
  appendText(
    boundary,
    "p",
    "",
    `${recording.detail} Aktuell: ${recordingStatusLabel(recording.status)}.`,
  );

  const listening = profilesFor("listening");
  const listeningStat = byId("listen-intro-stat");
  listeningStat.replaceChildren();
  appendText(listeningStat, "strong", "", String(listening.length));
  appendText(listeningStat, "span", "", "Hörprofile");
}

function renderProfileGrid(targetId, profiles) {
  const target = byId(targetId);
  if (!profiles.length) {
    target.replaceChildren(element("div", "empty-state", "Keine Profile in diesem Bereich."));
    return;
  }
  target.replaceChildren(...profiles.map(profileCard));
}

function profileCard(profile) {
  const card = element("article", "profile-card");
  const stateName = profileState(profile);
  card.dataset.profile = profile.id;
  card.dataset.profileState = stateName;
  const top = element("div", "card-topline");
  appendText(top, "span", "card-glyph", PROFILE_GLYPHS[profile.id] || "◇").setAttribute(
    "aria-hidden",
    "true",
  );
  appendText(
    top,
    "span",
    `status-pill ${profileStateTone(stateName)}`,
    PROFILE_STATE_LABELS[stateName] || stateName,
  );
  card.append(top);
  appendText(card, "h3", "", displayProfile(profile.id));
  appendText(card, "p", "", profile.purpose);

  const meta = element("div", "card-meta");
  const missingHardware = profile.missing_hardware_count ?? 0;
  const physicalFacts = profile.unresolved_physical_fact_count ?? 0;
  const laboratoryGates = profile.laboratory_gate_count ?? 0;
  if (missingHardware) appendText(meta, "span", "", `${missingHardware} Gerät fehlt`);
  if (physicalFacts) appendText(meta, "span", "", `${physicalFacts} Vor-Ort-Belege`);
  if (laboratoryGates) appendText(meta, "span", "", `${laboratoryGates} Labor-Gates`);
  if (!missingHardware && !physicalFacts && !laboratoryGates) {
    appendText(meta, "span", "", "Repositoryseitig vollständig");
  }
  card.append(meta);

  const actions = element("div", "card-actions");
  const button = element(
    "button",
    "secondary-button",
    profile.plan_available ? "Voraussetzungen" : "Details nicht verfügbar",
  );
  button.type = "button";
  button.disabled = !profile.plan_available;
  if (profile.plan_available) {
    button.addEventListener("click", (event) =>
      openProfilePlan(profile, event.currentTarget),
    );
  }
  actions.append(button);
  card.append(actions);
  return card;
}

async function openProfilePlan(profile, trigger) {
  if (!backendAllowed()) return;
  const requestId = ++state.dialogRequest;
  state.lastDialogTrigger = trigger;
  const backdrop = byId("dialog-backdrop");
  byId("dialog-eyebrow").textContent = "Read-only Profilplan";
  byId("dialog-title").textContent = displayProfile(profile.id);
  byId("dialog-content").replaceChildren(
    element("p", "dialog-message", "Beobachteten Zustand und Gates werden geprüft …"),
  );
  byId("dialog-content").setAttribute("aria-busy", "true");
  backdrop.hidden = false;
  document.body.classList.add("dialog-open");
  byId("app-shell").setAttribute("inert", "");
  byId("dialog-close").focus();
  try {
    const plan = await fetchJson(
      `/api/v1/profiles/${encodeURIComponent(profile.id)}/plan`,
      { timeoutMs: 55000 },
    );
    if (requestId !== state.dialogRequest) return;
    renderProfilePlan(plan);
  } catch (error) {
    if (requestId !== state.dialogRequest) return;
    byId("dialog-content").replaceChildren(
      element(
        "p",
        "dialog-message",
        error instanceof Error ? error.message : "Profilplan ist nicht lesbar.",
      ),
    );
  } finally {
    if (requestId === state.dialogRequest) {
      byId("dialog-content").setAttribute("aria-busy", "false");
    }
  }
}

function renderProfilePlan(plan) {
  const content = byId("dialog-content");
  content.replaceChildren();
  const readiness = element(
    "span",
    `status-pill ${plan.ready_for_laboratory_apply ? "ready" : ""}`,
    plan.ready_for_laboratory_apply ? "Laborbereit, kein Apply" : "noch blockiert",
  );
  content.append(readiness);
  appendText(
    content,
    "p",
    "dialog-message",
    plan.ready_for_laboratory_apply
      ? "Die bekannten Laborvoraussetzungen sind erfüllt. Das begründet ausdrücklich noch keine Apply-Autorität."
      : "Der Backendplan bleibt nebenwirkungsfrei und zeigt, was vor einer späteren Anwendung fehlt.",
  );
  const list = element("dl", "plan-detail-list");
  detailRow(list, "Apply-Autorität", plan.apply_authority || "unbekannt");
  detailRow(
    list,
    "Fehlende Hardware",
    String((plan.missing_hardware || []).length),
  );
  detailRow(
    list,
    "Physische Fakten",
    String((plan.missing_physical_facts || []).length),
  );
  detailRow(
    list,
    "Offene Labor-Gates",
    String((plan.unresolved_laboratory_gates || []).length),
  );
  detailRow(
    list,
    "Vorgeschlagene Änderungen",
    String((plan.proposed_changes || []).length),
  );
  content.append(list);
  const blockers = [
    ...(plan.readiness_blockers || []),
    ...(plan.missing_hardware || []).map((item) => `Hardware: ${item}`),
    ...(plan.missing_physical_facts || []).map((item) => `Physischer Fakt: ${item}`),
    ...(plan.unresolved_laboratory_gates || []).map((item) => `Labor-Gate: ${item}`),
  ];
  if (plan.planned_blocker) blockers.unshift(plan.planned_blocker);
  if (blockers.length) {
    appendText(content, "p", "eyebrow", "Offene Punkte");
    const blockerList = element("ul", "blocker-list");
    blockerList.append(...blockers.map((blocker) => element("li", "", blocker)));
    content.append(blockerList);
  }
}

function closeDialog() {
  state.dialogRequest += 1;
  stopLessonAudio();
  byId("dialog-backdrop").hidden = true;
  document.body.classList.remove("dialog-open");
  byId("app-shell").removeAttribute("inert");
  if (state.lastDialogTrigger?.isConnected) {
    state.lastDialogTrigger.focus();
  } else {
    byId("main-content").focus();
  }
  state.lastDialogTrigger = null;
}

function renderLibrary() {
  const recording = state.snapshot.recording || {};
  const library = state.recordingLibrary;
  const target = byId("library-takes");
  const allItems = Array.isArray(library?.items) ? [...library.items] : [];
  const activeCount = allItems.filter((item) => item.library?.trashed !== true).length;
  const trashedCount = allItems.length - activeCount;

  if (byId("library-view")) byId("library-view").value = state.libraryView;
  if (byId("library-category-filter")) {
    byId("library-category-filter").value = state.libraryCategory;
  }
  if (byId("library-sort")) byId("library-sort").value = state.librarySort;

  let items = allItems.filter((item) => {
    const trashed = item.library?.trashed === true;
    if (state.libraryView === "active" && trashed) return false;
    if (state.libraryView === "trash" && !trashed) return false;
    if (state.libraryCategory !== "all" && item.library?.category !== state.libraryCategory) {
      return false;
    }
    return true;
  });
  const createdAt = (item) => Date.parse(item.created_at || "") || 0;
  items.sort((left, right) => {
    if (state.librarySort === "oldest") return createdAt(left) - createdAt(right);
    if (state.librarySort === "name") {
      return String(left.name || "").localeCompare(String(right.name || ""), "de");
    }
    if (state.librarySort === "duration") {
      return (recordingTakeDurationSeconds(right) || 0) - (recordingTakeDurationSeconds(left) || 0);
    }
    if (state.librarySort === "category") {
      const category = recordingCategoryLabel(left.library?.category).localeCompare(
        recordingCategoryLabel(right.library?.category),
        "de",
      );
      return category || createdAt(right) - createdAt(left);
    }
    return createdAt(right) - createdAt(left);
  });

  const cards = [];
  const summary = element("article", "metric-card");
  appendText(summary, "p", "eyebrow", "Takes");
  appendText(
    summary,
    "strong",
    "",
    library ? `${activeCount} aktiv · ${trashedCount} Papierkorb` : "nicht lesbar",
  );
  appendText(
    summary,
    "span",
    "",
    state.recordingLibraryError ||
      `${items.length} von ${allItems.length} Takes in der aktuellen Ansicht${library?.truncated ? " · weitere Takes vorhanden" : ""}.`,
  );
  cards.push(summary);

  const organization = element("article", "metric-card");
  appendText(organization, "p", "eyebrow", "Organisation");
  appendText(organization, "strong", "", "Kategorien + Sortierung");
  appendText(
    organization,
    "span",
    "",
    "Kategorie ist getrennte Bibliotheksmetadaten und verändert weder WAV noch MIDI noch Take-Manifest.",
  );
  cards.push(organization);

  const immutable = element("article", "metric-card");
  appendText(immutable, "p", "eyebrow", "Löschschutz");
  appendText(immutable, "strong", "", "Papierkorb + SHA-256");
  appendText(
    immutable,
    "span",
    "",
    "Löschen ist zunächst wiederherstellbar. Im Papierkorb werden Wiedergabe und Export gesperrt.",
  );
  cards.push(immutable);

  if (library && !items.length) {
    const empty = element("article", "metric-card");
    appendText(empty, "p", "eyebrow", "Ansicht");
    appendText(empty, "strong", "", "Keine passenden Takes");
    appendText(empty, "span", "", "Filter oder Ansicht ändern, um andere Aufnahmen zu sehen.");
    cards.push(empty);
  }

  for (const item of items) {
    const trashed = item.library?.trashed === true;
    const category = item.library?.category || "unsorted";
    const card = element("article", `profile-card recording-take${trashed ? " is-trashed" : ""}`);
    const top = element("div", "card-topline");
    appendText(top, "span", "card-glyph", "●").setAttribute("aria-hidden", "true");
    appendText(
      top,
      "span",
      `status-pill ${!trashed && item.status === "completed" ? "ready" : item.recovery_required ? "laboratory" : ""}`,
      trashed ? "Papierkorb" : recordingStatusLabel(item.status),
    );
    card.append(top);
    appendText(card, "h3", "", item.name || `Take ${shortRevision(item.session_id)}`);
    const meta = element("dl", "truth-list");
    detailRow(meta, "Kategorie", recordingCategoryLabel(category));
    detailRow(meta, "Erstellt", formatDateTime(item.created_at));
    if (trashed && item.library?.trashed_at) {
      detailRow(meta, "Papierkorb seit", formatDateTime(item.library.trashed_at));
    }
    detailRow(meta, "Session", shortRevision(item.session_id));
    detailRow(meta, "Plan", shortRevision(item.plan_sha256));
    detailRow(
      meta,
      "Quelle",
      item.source?.bound
        ? `gebunden · ${item.source.sample_rate_hz || 48000} Hz · ${item.source.channels || 2} Kanäle`
        : "nicht gebunden",
    );
    if (item.result?.artifact) {
      const artifact = item.result.artifact;
      detailRow(meta, "Dauer", `${Number(artifact.duration_seconds).toFixed(2)} s`);
      detailRow(meta, "Take-SHA", shortRevision(artifact.sha256));
    }
    if (item.result?.artifacts) {
      const artifacts = item.result.artifacts;
      const mix = artifacts.mix_wav;
      const vocal = artifacts.vocal_wav;
      detailRow(
        meta,
        "Produkt",
        mix ? "Stereo-Mix WAV + Roland MIDI" : "Klavier: MIDI-only + Gesang WAV (Legacy)",
      );
      if (mix || vocal) detailRow(meta, "Dauer", `${Number((mix || vocal).duration_seconds).toFixed(2)} s`);
      detailRow(
        meta,
        "Geschwister",
        artifacts.take_manifest?.commit_truth === true
          ? "WAV + MID · Manifest gültig"
          : "unvollständig",
      );
    }
    card.append(meta);

    if (!trashed && item.status === "completed" && typeof item.audio_url === "string") {
      appendTakeListenButton(card, item);
      appendText(
        card,
        "small",
        "recording-playback-note",
        "Wiedergabe: Der zentrale Player öffnet ausschließlich den vom Backend erneut verifizierten finalen Take; keine Browser-Mikrofonaufnahme.",
      );
    }
    if (!trashed) appendTakeExports(card, item);

    const terminal = item.status === "completed" || item.status === "failed-preserved";
    if (terminal) {
      const actions = element("div", "recording-library-actions");
      const categoryLabel = element("label", "recording-library-category");
      appendText(categoryLabel, "span", "", "Kategorie");
      const categorySelect = element("select", "recording-category-select");
      for (const [value, label] of Object.entries(RECORDING_LIBRARY_CATEGORIES)) {
        const option = element("option", "", label);
        option.value = value;
        option.selected = value === category;
        categorySelect.append(option);
      }
      categorySelect.disabled =
        trashed || !recordingLibraryActionsAllowed() || state.recordingActionPending;
      categorySelect.addEventListener("change", (event) =>
        runRecordingAction({
          operation: "categorize",
          session_id: item.session_id,
          category: event.target.value,
        }),
      );
      categoryLabel.append(categorySelect);
      actions.append(categoryLabel);

      if (trashed) {
        const restore = element("button", "secondary-button", "Wiederherstellen");
        restore.type = "button";
        restore.disabled =
          !recordingLibraryActionsAllowed() || state.recordingActionPending;
        restore.addEventListener("click", () =>
          runRecordingAction({ operation: "restore", session_id: item.session_id }),
        );
        actions.append(restore);
      } else {
        const remove = element("button", "secondary-button danger-button", "Löschen");
        remove.type = "button";
        remove.disabled =
          !recordingLibraryActionsAllowed() || state.recordingActionPending;
        remove.addEventListener("click", () => {
          const name = item.name || `Take ${shortRevision(item.session_id)}`;
          if (!window.confirm(`${name} in den Papierkorb verschieben? Der Take kann wiederhergestellt werden.`)) {
            return;
          }
          runRecordingAction({ operation: "trash", session_id: item.session_id });
        });
        actions.append(remove);
      }
      card.append(actions);
    }

    if (!trashed && (item.recovery_required === true || item.cleanup_required === true)) {
      const recover = element("button", "secondary-button", "Recovery");
      recover.type = "button";
      recover.disabled = !recordingActionsAllowed() || state.recordingActionPending;
      recover.addEventListener("click", () =>
        runRecordingAction({ operation: "recover", session_id: item.session_id }),
      );
      card.append(recover);
    }
    cards.push(card);
  }

  if (!library && !state.recordingLibraryError) {
    const pending = element("article", "metric-card");
    appendText(pending, "p", "eyebrow", "Recorderbibliothek");
    appendText(pending, "strong", "", recordingStatusLabel(recording.status));
    appendText(pending, "span", "", "Bibliothek wird beim nächsten Backend-Readback geladen.");
    cards.push(pending);
  }
  target.replaceChildren(...cards);
  syncRecordingLibraryControls();
}

async function loadReplay() {
  if (!backendAllowed()) return;
  try {
    const replay = await fetchJson("/api/v1/replay", { timeoutMs: 12000 });
    if (replay.authoritative !== false || replay.authority !== "synthetic-replay") {
      throw new Error("Replay-Autoritätsgrenze ist ungültig.");
    }
    state.replay = replay;
    const scenarios = replay.catalog?.scenarios || [];
    if (!scenarios.some((scenario) => scenario.id === state.replayScenarioId)) {
      state.replayScenarioId = scenarios[0]?.id || "normal";
    }
    state.replayFrameIndex = 0;
    renderReplaySelector();
    renderReplay();
  } catch (error) {
    state.replay = null;
    stopReplay();
    byId("replay-authority").textContent =
      error instanceof Error ? error.message : "Replay ist nicht lesbar.";
    for (const control of ["replay-scenario", "replay-play", "replay-step", "replay-reset"]) {
      byId(control).disabled = true;
    }
  }
}

function replayScenarios() {
  return state.replay?.catalog?.scenarios || [];
}

function currentReplayScenario() {
  return replayScenarios().find((scenario) => scenario.id === state.replayScenarioId) || null;
}

function currentReplayFrame() {
  const scenario = currentReplayScenario();
  if (!scenario?.frames?.length) return null;
  return scenario.frames[Math.min(state.replayFrameIndex, scenario.frames.length - 1)];
}

function renderReplaySelector() {
  const select = byId("replay-scenario");
  select.replaceChildren(
    ...replayScenarios().map((scenario) => {
      const option = element("option", "", scenario.label);
      option.value = scenario.id;
      option.selected = scenario.id === state.replayScenarioId;
      return option;
    }),
  );
  select.disabled = replayScenarios().length === 0;
}

function dbProgress(value) {
  return Math.max(0, Math.min(120, Number(value) + 120));
}

function renderReplay() {
  const scenario = currentReplayScenario();
  const frame = currentReplayFrame();
  const available = Boolean(state.replay && scenario && frame);
  for (const control of ["replay-play", "replay-step", "replay-reset"]) {
    byId(control).disabled = !available;
  }
  byId("replay-play").textContent = state.replayPlaying ? "Pause" : "Abspielen";
  if (!available) return;
  byId("replay-authority").textContent =
    `${scenario.label}: ${scenario.description} · synthetisch, nicht autoritativ`;
  byId("replay-peak").value = dbProgress(frame.peak_dbfs);
  byId("replay-rms").value = dbProgress(frame.rms_dbfs);
  byId("replay-peak-value").textContent = `${frame.peak_dbfs} dBFS`;
  byId("replay-rms-value").textContent = `${frame.rms_dbfs} dBFS`;
  byId("replay-midi").textContent =
    frame.midi_note === null
      ? "keine Note"
      : `Note ${frame.midi_note} · Velocity ${frame.midi_velocity}`;
  byId("replay-xruns").textContent = String(frame.xrun_total);
  byId("replay-device").textContent =
    { online: "online", lost: "verloren", recovering: "Recovery" }[frame.device_state] || frame.device_state;
  byId("replay-event").textContent =
    { none: "keins", midi: "MIDI", clip: "Clipping", xrun: "XRun", "device-loss": "Geräteverlust", stale: "veraltet", recovery: "Recovery" }[frame.event] || frame.event;
  const detail = byId("replay-detail");
  detail.replaceChildren();
  detailRow(detail, "Frame", `${frame.index + 1}/${scenario.frames.length}`);
  detailRow(detail, "Offset", `${frame.offset_ms} ms`);
  detailRow(detail, "Telemetriealter", `${frame.telemetry_age_ms} ms`);
  detailRow(detail, "Stale-Grenze", `${state.replay.catalog.stale_after_ms} ms`);
  detailRow(detail, "Katalog", shortRevision(state.replay.catalog_sha256));
  detailRow(detail, "Autorität", "synthetic-replay · false");
}

function stopReplay() {
  if (state.replayTimer) window.clearInterval(state.replayTimer);
  state.replayTimer = null;
  state.replayPlaying = false;
  if (byId("replay-play")) byId("replay-play").textContent = "Abspielen";
}

function stepReplay() {
  const scenario = currentReplayScenario();
  if (!scenario?.frames?.length) return;
  if (state.replayFrameIndex >= scenario.frames.length - 1) {
    stopReplay();
    return;
  }
  state.replayFrameIndex += 1;
  renderReplay();
}

function toggleReplay() {
  if (state.replayPlaying) {
    stopReplay();
    renderReplay();
    return;
  }
  const scenario = currentReplayScenario();
  if (!scenario?.frames?.length) return;
  if (state.replayFrameIndex >= scenario.frames.length - 1) state.replayFrameIndex = 0;
  state.replayPlaying = true;
  renderReplay();
  state.replayTimer = window.setInterval(
    stepReplay,
    Math.max(state.replay.catalog.sample_interval_ms, prefersReducedMotion() ? 500 : 100),
  );
}

function resetReplay() {
  stopReplay();
  state.replayFrameIndex = 0;
  renderReplay();
}

function hasActivePipeWireLevel(streams) {
  return streams.some(
    (stream) =>
      stream?.id === "audio-levels" &&
      stream.availability === "live" &&
      stream.value?.source === "active-pipewire-shared-capture",
  );
}

function telemetryObservationSummary(streams) {
  return hasActivePipeWireLevel(streams)
    ? "Pegel aktiv via PipeWire · Telemetriekern read-only/ohne Steuerwirkung"
    : "Telemetriekern read-only/ohne Steuerwirkung";
}

function telemetryLevelSourceLabel(stream) {
  if (stream?.id !== "audio-levels") return null;
  if (stream.value?.source === "active-pipewire-shared-capture") {
    return "Quelle: PipeWire Shared Capture";
  }
  if (stream.value?.source === "external-passive-level-file") {
    return "Quelle: externe Pegeldatei";
  }
  return null;
}

function finiteTelemetryNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function telemetryValueText(stream) {
  const value = stream.value;
  if (!value || typeof value !== "object") return "keine Beobachtung";
  switch (stream.id) {
    case "audio-levels": {
      const peak = finiteTelemetryNumber(value.peak_dbfs);
      const rms = finiteTelemetryNumber(value.rms_dbfs);
      return peak === null || rms === null
        ? "unvollständige Beobachtung"
        : `${peak} / ${rms} dBFS`;
    }
    case "midi-activity": {
      const clients = finiteTelemetryNumber(value.client_count);
      const ports = finiteTelemetryNumber(value.port_count);
      return clients === null || ports === null
        ? "unvollständige Beobachtung"
        : `${clients} Clients · ${ports} Ports`;
    }
    case "transport":
      return typeof value.state === "string" && value.state
        ? TRANSPORT_STATE_LABELS[value.state] || value.state
        : "unvollständige Beobachtung";
    case "cpu-load": {
      const load = finiteTelemetryNumber(value.load_1m);
      const service = finiteTelemetryNumber(value.service_cpu_percent);
      if (load === null) return "unvollständige Beobachtung";
      return service === null ? `Last ${load}` : `Last ${load} · Dienst ${service} %`;
    }
    case "xruns": {
      const total = finiteTelemetryNumber(value.total);
      const delta = finiteTelemetryNumber(value.delta);
      return total === null || delta === null
        ? "unvollständige Beobachtung"
        : `${total} gesamt · Δ ${delta}`;
    }
    case "device-graph": {
      const nodes = finiteTelemetryNumber(value.node_count);
      const links = finiteTelemetryNumber(value.link_count);
      return nodes === null || links === null
        ? "unvollständige Beobachtung"
        : `${nodes} Knoten · ${links} Links`;
    }
    default:
      return "beobachtet";
  }
}


function telemetryCard(stream) {
  const availability = stream.availability || "unavailable";
  const card = element("article", `telemetry-card is-${availability}`);
  appendText(card, "p", "eyebrow", TELEMETRY_STREAM_LABELS[stream.id] || stream.id);
  const chip = appendText(
    card,
    "span",
    "telemetry-chip",
    TELEMETRY_AVAILABILITY_LABELS[availability] || availability,
  );
  chip.setAttribute("data-availability", availability);
  appendText(card, "strong", "", telemetryValueText(stream));
  appendText(
    card,
    "small",
    "",
    `Seq ${stream.sequence} · Alter ${
      stream.age_ms === null || stream.age_ms === undefined ? "offen" : `${stream.age_ms} ms`
    } · verworfen ${stream.dropped_total}`,
  );
  const levelSource = telemetryLevelSourceLabel(stream);
  if (levelSource) appendText(card, "small", "", levelSource);
  if (stream.error) {
    appendText(card, "small", "telemetry-error", stream.error);
  }
  return card;
}

function renderTelemetry() {
  const grid = byId("telemetry-grid");
  const detail = byId("telemetry-detail");
  const authority = byId("telemetry-authority");
  const telemetry = state.telemetry;
  if (!telemetry) {
    grid.replaceChildren();
    detail.replaceChildren();
    authority.textContent =
      state.telemetryError ||
      "Live-Telemetrie ist nicht lesbar. Die Bedienung bleibt davon unberührt.";
    detailRow(
      detail,
      "Darstellung",
      `Seq ${state.telemetryPresentationSequence} · Anfrage ${state.telemetryPresentedRequest}`,
    );
    detailRow(detail, "Fehler", state.telemetryError || "nicht verfügbar");
    return;
  }
  const summary = telemetry.summary || {};
  const streams = telemetry.streams || [];
  authority.textContent = telemetry.running
    ? `${summary.live_count}/${summary.stream_count} Ströme live · ${summary.stale_count} veraltet · ${summary.unavailable_count} nicht verfügbar · ${telemetryObservationSummary(streams)}`
    : "Telemetriesammler laufen nicht; Ströme werden ausdrücklich als veraltet gezeigt.";
  grid.replaceChildren(...streams.map(telemetryCard));
  const control = telemetry.control_channel || {};
  detail.replaceChildren();
  detailRow(detail, "Kommandokanal", `${control.depth}/${control.capacity} · verlustfrei`);
  detailRow(detail, "Abgewiesene Kommandos", String(control.rejected_total));
  detailRow(detail, "Verworfene Telemetrie", String(summary.dropped_total));
  detailRow(detail, "Kollektor-Neustarts", String(summary.restart_total));
  detailRow(
    detail,
    "Darstellung",
    `Seq ${state.telemetryPresentationSequence} · Anfrage ${state.telemetryPresentedRequest}`,
  );
  detailRow(
    detail,
    "Darstellungsalter",
    state.telemetryUpdatedAt === null
      ? "offen"
      : `${Math.max(0, Date.now() - state.telemetryUpdatedAt)} ms`,
  );
  detailRow(detail, "Laufzeit", `${telemetry.uptime_seconds} s`);
  detailRow(
    detail,
    "Grenze",
    "Telemetriekern: passive-observation · read-only · keine Steuerwirkung",
  );
}

async function loadTelemetry() {
  const requestId = ++state.telemetryRequestSequence;
  try {
    const telemetry = await fetchJson("/api/v1/telemetry", { timeoutMs: 4000 });
    if (requestId !== state.telemetryRequestSequence) return;
    if (telemetry.read_only !== true || telemetry.authority !== "passive-observation") {
      throw new Error("Telemetrie-Autoritätsgrenze ist ungültig.");
    }
    state.telemetry = telemetry;
    state.telemetryError = null;
  } catch (error) {
    if (requestId !== state.telemetryRequestSequence) return;
    // A telemetry failure must never disturb the controls or the state view.
    state.telemetry = null;
    state.telemetryError =
      error instanceof Error
        ? `Live-Telemetrie nicht lesbar: ${error.message}`
        : "Live-Telemetrie nicht lesbar.";
  }
  state.telemetryPresentedRequest = requestId;
  state.telemetryPresentationSequence += 1;
  state.telemetryUpdatedAt = Date.now();
  renderTelemetry();
  updateRecordingLiveStage();
}

function requestTelemetry() {
  if (!backendAllowed()) return Promise.resolve();
  if (state.telemetryInFlight) return state.telemetryInFlight;
  const request = loadTelemetry().finally(() => {
    if (state.telemetryInFlight === request) state.telemetryInFlight = null;
  });
  state.telemetryInFlight = request;
  return request;
}

function stopTelemetryPolling() {
  if (!state.telemetryTimer) return;
  window.clearTimeout(state.telemetryTimer);
  state.telemetryTimer = null;
}

async function telemetryPollTick() {
  state.telemetryTimer = null;
  if (!backendAllowed()) return;
  if (!document.hidden) await requestTelemetry();
  scheduleTelemetryPolling();
}

function scheduleTelemetryPolling(delayMs = TELEMETRY_POLL_MS) {
  stopTelemetryPolling();
  if (document.hidden || !backendAllowed()) return;
  state.telemetryTimer = window.setTimeout(telemetryPollTick, delayMs);
}


async function runDauersongAction(operation) {
  if (!state.snapshot || state.loading || state.dauersongActionPending) return;
  if (!["start", "stop", "recover"].includes(operation)) return;
  state.dauersongActionPending = true;
  clearNotice();
  syncRemoteControls();
  renderSounds();
  try {
    const result = await postDauersongAction({ operation });
    if (
      result?.kind !== "audio_control_dauersong_action_result" ||
      result.operation !== operation ||
      !result.snapshot
    ) {
      throw new Error("Dauersong-Aktion kam ohne autoritativen Readback zurück.");
    }
    state.snapshot = result.snapshot;
    renderAll();
    showNotice(
      operation === "start"
        ? "Dauersong läuft mit bestätigter 100-%-Streamgrenze."
        : operation === "stop"
          ? "Dauersong wurde beendet und als inaktiv zurückgelesen."
          : "Dauersong-Recovery wurde abgeschlossen; der Dienst bleibt inaktiv.",
      "success",
    );
  } catch (error) {
    showNotice(error instanceof Error ? error.message : "Dauersong-Aktion wurde blockiert.");
  } finally {
    state.dauersongActionPending = false;
    syncRemoteControls();
    if (state.snapshot) renderSounds();
  }
}

function renderSounds() {
  const target = byId("sound-library");
  const whale = state.snapshot.whale;
  const activeMode =
    whale.status === "ok" && whale.service?.active === true
      ? whale.service.voice_mode
      : null;
  const cards = (whale.contract.modes || []).map((mode, index) => {
    const card = element("article", `sound-card${index === 0 ? " featured" : ""}`);
    const top = element("div", "card-topline");
    appendText(top, "span", "card-glyph", mode.id === "ufo" ? "⌁" : "≋").setAttribute(
      "aria-hidden",
      "true",
    );
    appendText(
      top,
      "span",
      `status-pill ${activeMode === mode.id ? "ready" : ""}`,
      activeMode === mode.id ? "aktiv" : "verfügbar",
    );
    card.append(top);
    appendText(card, "h3", "", `Buckelwal · ${displayMode(mode.id)}`);
    appendText(card, "p", "", soundModeDescription(mode));
    const meta = element("div", "card-meta");
    appendText(meta, "span", "", formatEndpoint(mode.backend));
    card.append(meta);
    const actions = element("div", "card-actions");
    const link = element("a", "secondary-button", "In Spielen öffnen");
    link.href = "#spielen";
    actions.append(link);
    card.append(actions);
    return card;
  });

  const songState = state.snapshot.dauersong || {};
  const songService = songState.service || {};
  const songActive = songState.status === "ok" && songService.active === true;
  const songRuntimeSafe = state.snapshot.summary?.dauersong_runtime_safe !== false;
  const songReady =
    songState.status === "ok" &&
    songService.hardening_ready === true &&
    songService.source_binding_ready === true;
  const song = element("article", "sound-card");
  song.setAttribute("aria-busy", String(state.dauersongActionPending));
  const songTop = element("div", "card-topline");
  appendText(songTop, "span", "card-glyph", "∞").setAttribute("aria-hidden", "true");
  appendText(
    songTop,
    "span",
    `status-pill ${songActive && songRuntimeSafe ? "ready" : songActive ? "unavailable" : songReady ? "" : "unavailable"}`,
    songActive
      ? songRuntimeSafe
        ? "aktiv"
        : "aktiv · prüfen"
      : songReady
        ? "bereit"
        : "nicht verfügbar",
  );
  song.append(songTop);
  appendText(song, "h3", "", "Dauersong");
  appendText(song, "p", "", songState.detail || "Dauersong-Zustand ist nicht verfügbar.");
  const songMeta = element("div", "card-meta");
  appendText(songMeta, "span", "", "v9 · quellgebunden");
  appendText(
    songMeta,
    "span",
    "",
    songActive && !songRuntimeSafe ? "Stream-/Laufzeitvertrag verletzt · Stop verfügbar" : "Stream max. 100 %",
  );
  if (songService.live?.name) {
    appendText(songMeta, "span", "", `Abschnitt ${songService.live.section ?? "–"} · ${songService.live.name}`);
  }
  song.append(songMeta);
  const songActions = element("div", "card-actions");
  const songButton = element(
    "button",
    songActive ? "primary-button danger" : "primary-button",
    songActive ? "Dauersong beenden" : "Dauersong starten",
  );
  songButton.type = "button";
  songButton.disabled =
    state.loading ||
    state.dauersongActionPending ||
    !localDauersongActionsAllowed() ||
    (!songActive && !songReady);
  songButton.addEventListener("click", () => runDauersongAction(songActive ? "stop" : "start"));
  songActions.append(songButton);
  if (!songActive && songService.active_state === "failed" && songReady) {
    const recoverButton = element("button", "secondary-button", "Recovery");
    recoverButton.type = "button";
    recoverButton.disabled = state.loading || state.dauersongActionPending || !localDauersongActionsAllowed();
    recoverButton.addEventListener("click", () => runDauersongAction("recover"));
    songActions.append(recoverButton);
  }
  song.append(songActions);
  cards.push(song);
  target.replaceChildren(...cards);
}

function soundModeDescription(mode) {
  if (mode.id === "morph") {
    return "Quellengestützte, chromatisch spielbare Morph-Stimme ohne Samplezonen.";
  }
  if (mode.id === "organic") {
    return "Quellengestützte Walstimme mit zeitvariablen Originaltrajektorien für Resonanzen, Puls, Rauigkeit und Nebenstimme sowie kräftigem Tiefbass.";
  }
  if (mode.id === "realistic") {
    return "Lizenzierte Naturaufnahmen mit begrenzter Tonhöhenverschiebung.";
  }
  if (mode.id === "ufo") {
    return "Historischer synthetischer Vergleichsmodus, ausdrücklich kein Realismusbeleg.";
  }
  return `Backend: ${formatEndpoint(mode.backend)}.`;
}

function signalNode(title, detail, stateName) {
  const node = element("article", `signal-node ${stateName}`);
  appendText(node, "strong", "", title);
  appendText(node, "small", "", detail);
  return node;
}

function signalStage(label, nodes) {
  const stage = element("section", "signal-stage");
  appendText(stage, "p", "eyebrow", label);
  const list = element("div", "signal-node-list");
  list.append(...nodes);
  stage.append(list);
  return stage;
}

function renderConnections() {
  const doctor = state.snapshot.doctor || {};
  const graph = doctor.graph || {};
  const hardware = doctor.hardware || {};
  const external = doctor.external_endpoints || {};
  const unknownFacts = new Set(doctor.physical_unknowns || []);

  const inputs = [
    signalNode(
      "MOTU M2",
      hardware.motu_m2 ? "Audiointerface beobachtet" : "aus oder nicht verbunden",
      hardware.motu_m2 ? "observed" : "onsite",
    ),
    signalNode(
      "Roland FP-30X",
      hardware.roland_fp_30x ? "Piano beobachtet" : "aus oder nicht verbunden",
      hardware.roland_fp_30x ? "observed" : "onsite",
    ),
  ];
  const control = [
    signalNode(
      "PipeWire",
      `${graph.force_rate_hz || "—"} Hz · ${graph.force_quantum_frames || "—"} Frames`,
      doctor.status === "ok" ? "configured" : "unavailable",
    ),
    signalNode(
      "Aktuelle Route",
      `${formatEndpoint(graph.default_source)} → ${formatEndpoint(graph.default_sink)}`,
      doctor.status === "ok" ? "configured" : "unavailable",
    ),
  ];
  const focalUnknown =
    unknownFacts.has("motu_output_to_lake_people") ||
    unknownFacts.has("focal_connected_output");
  const outputs = [
    signalNode(
      "Lake People · Focal",
      focalUnknown ? "physischer Weg nicht belegt" : "physischer Weg belegt",
      focalUnknown ? "onsite" : "observed",
    ),
    signalNode(
      "Pioneer VSX-830-K",
      external.pioneer_vsx_830_k?.software_observed
        ? "softwareseitig beobachtet"
        : "physischer Weg offen",
      external.pioneer_vsx_830_k?.software_observed ? "observed" : "onsite",
    ),
    signalNode(
      "Lautsprecher am Pioneer",
      "2× ELAC FS 109.2 · Canton Center + 4 Satelliten · kein Subwoofer · aktive Zuordnung offen",
      "onsite",
    ),
    signalNode(
      "1MII B03 Pro",
      external.transmitter_1mii_b03_pro?.software_observed
        ? "softwareseitig beobachtet"
        : "externer Sender nicht beobachtbar",
      external.transmitter_1mii_b03_pro?.software_observed ? "observed" : "onsite",
    ),
  ];

  const topology = element("div", "signal-topology");
  topology.append(
    signalStage("Quellen", inputs),
    signalStage("Routing", control),
    signalStage("Ausgaben", outputs),
  );
  byId("connection-map").replaceChildren(topology);

  const legend = [
    ["observed", "beobachtet"],
    ["configured", "konfiguriert"],
    ["onsite", "vor Ort"],
    ["unavailable", "nicht lesbar"],
  ];
  byId("connection-legend").replaceChildren(
    ...legend.map(([tone, label]) => {
      const item = element("span", "legend-item");
      appendText(item, "i", `legend-dot ${tone}`, "").setAttribute("aria-hidden", "true");
      appendText(item, "span", "", label);
      return item;
    }),
  );

  const facts = Array.isArray(doctor.physical_unknowns)
    ? doctor.physical_unknowns
    : [];
  if (!facts.length) {
    byId("physical-facts").replaceChildren(
      element("div", "empty-state", "Keine offenen physischen Belege gemeldet."),
    );
    return;
  }
  byId("physical-facts").replaceChildren(
    ...facts.map((fact) => {
      const row = element("div", "fact-row");
      const copy = element("div");
      appendText(copy, "strong", "", PHYSICAL_FACT_LABELS[fact] || fact);
      appendText(copy, "code", "", fact);
      row.append(copy);
      appendText(row, "span", "", "vor Ort");
      return row;
    }),
  );
}

function renderDeployment() {
  const deployment = state.snapshot.deployment || {};
  const panel = byId("deployment-status");
  panel.replaceChildren();
  const heading = element("div", "panel-heading");
  const copy = element("div");
  appendText(copy, "p", "eyebrow", "Automatische Auslieferung");
  appendText(copy, "h2", "", deploymentStateLabel(deployment.status));
  heading.append(copy);
  appendText(
    heading,
    "span",
    `status-pill ${deployment.in_sync ? "ready" : deployment.status === "drift" ? "unavailable" : ""}`,
    deployment.in_sync ? "synchron" : deploymentStateLabel(deployment.status),
  );
  panel.append(heading);
  const list = element("dl", "service-list");
  detailRow(list, "Modus", deployment.automatic ? "automatisch" : "Quellcheckout");
  detailRow(list, "Quelle", deployment.source_ref || "unbekannt");
  detailRow(list, "Runtime", shortRevision(deployment.runtime_commit));
  detailRow(list, "Beleg", shortRevision(deployment.receipt_commit));
  detailRow(list, "Letzter Sync", formatDateTime(deployment.last_sync_at));
  detailRow(list, "Dienst", deployment.service_health || "nicht lesbar");
  panel.append(list);
}

function renderDiagnostics() {
  const doctor = state.snapshot.doctor;
  const graph = doctor.graph || {};
  const summary = state.snapshot.summary || {};
  const presence = state.snapshot.presence || {};
  const deployment = state.snapshot.deployment || {};
  const systemMetrics = [
    ["Runtime", summary.runtime_state === "healthy" ? "bereit" : summary.runtime_state || "offen"],
    ["Hardware", hardwareStateLabel(presence.state)],
    ["Route", formatEndpoint(graph.default_sink)],
    ["Deployment", deploymentStateLabel(deployment.status)],
  ];
  byId("system-summary").replaceChildren(
    ...systemMetrics.map(([label, value]) => {
      const card = element("article", "metric-card");
      appendText(card, "p", "eyebrow", label);
      appendText(card, "strong", "", value);
      appendText(card, "span", "", "autoritativ beobachtet");
      return card;
    }),
  );
  const commands = Array.isArray(doctor.command_health) ? doctor.command_health : [];
  const available = commands.filter((item) => item.available).length;
  const metrics = [
    ["Doctor", doctor.status === "ok" ? "lesbar" : "offen"],
    ["Rate", graph.force_rate_hz ? `${graph.force_rate_hz} Hz` : "—"],
    ["Quantum", graph.force_quantum_frames ? `${graph.force_quantum_frames}` : "—"],
    ["Werkzeuge", `${available}/${commands.length}`],
  ];
  byId("diagnostic-metrics").replaceChildren(
    ...metrics.map(([label, value]) => {
      const card = element("article", "metric-card");
      appendText(card, "p", "eyebrow", label);
      appendText(card, "strong", "", value);
      appendText(
        card,
        "span",
        "",
        label === "Quantum" ? "Frames, beobachtet" : "Backend-Readback",
      );
      return card;
    }),
  );

  const warnings = Array.isArray(doctor.warnings) ? doctor.warnings : [];
  const warningList = byId("warning-list");
  if (warnings.length === 0) {
    warningList.replaceChildren(
      element(
        "div",
        "empty-state",
        doctor.status === "ok"
          ? "Der Doctor meldet keine Warnungen."
          : doctor.error || "Doctor-Zustand ist nicht lesbar.",
      ),
    );
  } else {
    warningList.replaceChildren(
      ...warnings.map((warning) => {
        const row = element("article", "warning-row");
        const onsite =
          warning.code === "voice-source-not-motu" &&
          state.snapshot.presence?.observed?.motu_m2 !== true;
        appendText(
          row,
          "span",
          `status-dot ${onsite ? "onsite" : warning.severity || "medium"}`,
        ).setAttribute("aria-hidden", "true");
        const copy = element("div");
        appendText(
          copy,
          "strong",
          "",
          WARNING_LABELS[warning.code] || warning.code || "Doctor-Hinweis",
        );
        appendText(copy, "p", "", warning.detail || "Kein Detail vorhanden.");
        row.append(copy);
        return row;
      }),
    );
  }

  const commandList = byId("command-health");
  if (!commands.length) {
    commandList.replaceChildren(
      element("div", "empty-state", "Keine Werkzeugabfrage verfügbar."),
    );
  } else {
    commandList.replaceChildren(
      ...commands.map((command) => {
        const row = element("div", "command-row");
        appendText(row, "code", "", command.command || "unbekannt");
        appendText(
          row,
          "span",
          `command-state${command.available ? "" : " bad"}`,
          command.available ? "bereit" : "fehlt",
        );
        return row;
      }),
    );
  }

  const badge = byId("diagnostic-badge");
  const count =
    (state.snapshot.summary?.runtime_high_warning_count || 0) +
    (doctor.status === "ok" ? 0 : 1);
  badge.textContent = String(count);
  badge.hidden = count === 0;
}

function renderSettings() {
  const service = state.snapshot.service;
  const repository = state.snapshot.repository;
  const panel = byId("service-settings");
  panel.replaceChildren();
  const heading = element("div", "panel-heading");
  const copy = element("div");
  appendText(copy, "p", "eyebrow", "Control-Dienst");
  appendText(copy, "h2", "", "Lokale Autorität");
  heading.append(copy);
  panel.append(heading);
  const list = element("dl", "service-list");
  detailRow(list, "Bind", `${service.bind}:${service.port}`);
  detailRow(list, "API", state.snapshot.api_version);
  detailRow(list, "Einheit", service.managed_unit);
  detailRow(list, "Browser-Audio", service.browser_audio_authority ? "ja" : "nein");
  detailRow(
    list,
    "Runtime-HEAD",
    repository.runtime_head === "unavailable"
      ? "nicht lesbar"
      : repository.runtime_head.slice(0, 10),
  );
  detailRow(list, "Spec-Basis", repository.spec_base_revision.slice(0, 10));
  panel.append(list);
}

function installTaskWorkspaceLayout() {
  const listeningView = byId("view-hoeren");
  const recordingView = byId("view-aufnehmen");
  const playingView = byId("view-spielen");

  byId("listening-signal-host").append(
    document.querySelector(".home-signal-block"),
    byId("home-metrics"),
  );
  byId("recording-live-host").append(byId("now-signal-lanes"));

  listeningView.append(byId("setup-listening"));
  recordingView.append(byId("setup-recording"));
  playingView.append(byId("setup-playing"), byId("whale-learning-lesson"));

  byId("system-truth-host").append(byId("truth-strip"));
  byId("system-live-host").append(byId("live-telemetry"));
}

function toggleDepth(panel, button) {
  const detail = panel.querySelector(":scope > .depth-detail");
  if (!detail) return;
  const expanded = button.getAttribute("aria-expanded") === "true";
  detail.hidden = expanded;
  panel.classList.toggle("is-expanded", !expanded);
  button.setAttribute("aria-expanded", String(!expanded));
  button.textContent = expanded ? "Erweitern" : "Reduzieren";
}

function focusableInDepthPanel(panel) {
  return [...panel.querySelectorAll(
    "button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
  )].filter((node) => !node.closest("[hidden]") && !node.closest('[aria-hidden="true"]'));
}

function closeDepthFocus({ restoreFocus = true, restoreScroll = true } = {}) {
  if (!focusedDepthPanel) return;
  const panel = focusedDepthPanel;
  const trigger = depthFocusReturn;
  panel.classList.remove("is-workspace-focused");
  document.body.classList.remove("workspace-focus-open");
  if (trigger) {
    trigger.textContent = trigger.dataset.focusLabel || "Vollbild";
    trigger.setAttribute("aria-pressed", "false");
  }
  focusedDepthPanel = null;
  depthFocusReturn = null;
  if (restoreScroll) {
    window.scrollTo({ top: depthFocusScrollY, left: 0, behavior: "auto" });
  }
  if (restoreFocus && trigger) trigger.focus({ preventScroll: true });
}

function openDepthFocus(panel, trigger) {
  if (panel.dataset.focusKind === "whale-learning") {
    openWhaleLesson(trigger);
    return;
  }
  if (focusedDepthPanel === panel) {
    closeDepthFocus();
    return;
  }
  if (focusedDepthPanel) {
    closeDepthFocus({ restoreFocus: false, restoreScroll: false });
  }
  focusedDepthPanel = panel;
  depthFocusReturn = trigger;
  depthFocusScrollY = window.scrollY;
  trigger.dataset.focusLabel ||= trigger.textContent.trim() || "Vollbild";
  trigger.textContent = "Zurück";
  trigger.setAttribute("aria-pressed", "true");
  panel.classList.add("is-workspace-focused");
  document.body.classList.add("workspace-focus-open");
  trigger.focus({ preventScroll: true });
}

function keepDepthFocus(event) {
  if (event.key !== "Tab" || !focusedDepthPanel) return;
  const focusable = focusableInDepthPanel(focusedDepthPanel);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function wireDepthPanels() {
  for (const panel of document.querySelectorAll("[data-depth-panel]")) {
    const toggle = panel.querySelector(":scope > .depth-heading .depth-toggle");
    const focus = panel.querySelector(":scope > .depth-heading .depth-focus");
    if (toggle) toggle.addEventListener("click", () => toggleDepth(panel, toggle));
    if (focus && panel.dataset.focusKind !== "whale-learning") {
      focus.dataset.focusLabel = "Vollbild";
      focus.textContent = "Vollbild";
      focus.setAttribute("aria-pressed", "false");
    }
    if (focus) focus.addEventListener("click", () => openDepthFocus(panel, focus));
  }
}

function routeFromHash() {
  const candidate = window.location.hash.slice(1);
  const resolved = ROUTE_ALIASES[candidate] || candidate;
  return ROUTES[resolved] ? resolved : "home";
}

function isHomeStartRoute() {
  const routeKey = window.location.hash.slice(1);
  return routeFromHash() === "home" && !ROUTE_TARGETS[routeKey];
}

function configureNativeScrollRestoration() {
  if ("scrollRestoration" in window.history) {
    window.history.scrollRestoration = isHomeStartRoute() ? "manual" : "auto";
  }
}

function restoreHomeStartPosition() {
  if (!isHomeStartRoute()) return;
  window.scrollTo({ top: 0, left: 0, behavior: "auto" });
}

function prefersReducedMotion() {
  return (
    document.documentElement.classList.contains("reduced-motion") ||
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function revealRouteTarget(target) {
  const detail = target.closest(".depth-detail");
  if (detail?.hidden) {
    detail.hidden = false;
    const panel = detail.closest("[data-depth-panel]");
    const toggle = panel?.querySelector(":scope > .depth-heading .depth-toggle");
    panel?.classList.add("is-expanded");
    if (toggle) {
      toggle.setAttribute("aria-expanded", "true");
      toggle.textContent = "Reduzieren";
    }
  }
  target.focus({ preventScroll: true });
  target.scrollIntoView({
    block: "start",
    behavior: prefersReducedMotion() ? "auto" : "smooth",
  });
}

function applyRoute(event) {
  const routeKey = window.location.hash.slice(1);
  const route = routeFromHash();
  configureNativeScrollRestoration();
  if (focusedDepthPanel) {
    const focusedView = focusedDepthPanel.closest("[data-view]");
    if (!focusedView || focusedView.dataset.view !== route) {
      closeDepthFocus({ restoreFocus: false, restoreScroll: false });
    }
  }
  const routeTarget = ROUTE_TARGETS[routeKey] || null;
  state.route = route;
  for (const view of document.querySelectorAll("[data-view]")) {
    const active = view.dataset.view === route;
    view.hidden = !active;
    view.classList.toggle("is-active", active);
  }
  for (const link of document.querySelectorAll("[data-route]")) {
    const active = link.dataset.route === route;
    link.classList.toggle("is-active", active);
    if (active) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  }
  document.documentElement.dataset.activeRoute = route;
  byId("view-title").textContent = ROUTES[route].title;
  byId("view-eyebrow").textContent = ROUTES[route].eyebrow;
  byId("view-description").textContent = ROUTES[route].description;
  document.title = `${ROUTES[route].title} · Audiozentrale`;
  if (routeTarget) {
    window.requestAnimationFrame(() => {
      const target = byId(routeTarget);
      if (!target || target.hidden) return;
      revealRouteTarget(target);
    });
  } else {
    if (event?.type === "hashchange") {
      byId("view-title").focus({ preventScroll: true });
    }
    window.scrollTo({
      top: 0,
      behavior: prefersReducedMotion() ? "auto" : "smooth",
    });
  }
}

function loadPreferences() {
  try {
    const reduceMotion =
      window.localStorage.getItem("audio-ui-reduce-motion") === "true";
    const autoRefresh =
      window.localStorage.getItem("audio-ui-auto-refresh") !== "false";
    const libraryView = window.localStorage.getItem("audio-ui-library-view");
    const libraryCategory = window.localStorage.getItem("audio-ui-library-category");
    const librarySort = window.localStorage.getItem("audio-ui-library-sort");
    if (LIBRARY_VIEWS.has(libraryView)) state.libraryView = libraryView;
    if (libraryCategory === "all" || Object.hasOwn(RECORDING_LIBRARY_CATEGORIES, libraryCategory)) {
      state.libraryCategory = libraryCategory;
    }
    if (LIBRARY_SORTS.has(librarySort)) state.librarySort = librarySort;
    byId("motion-toggle").checked = reduceMotion;
    byId("auto-refresh-toggle").checked = autoRefresh;
    document.documentElement.classList.toggle("reduced-motion", reduceMotion);
    state.autoRefresh = autoRefresh;
  } catch (_error) {
    state.autoRefresh = true;
  }
}

function savePreference(key, value) {
  try {
    window.localStorage.setItem(key, String(value));
  } catch (_error) {
    // Die UI bleibt ohne lokalen Speicher vollständig bedienbar.
  }
}

function markTransientInteraction() {
  state.interactionUntil = window.performance.now() + INTERACTION_GRACE_MS;
}

function recordingPlaybackActive() {
  return [...document.querySelectorAll("audio.recording-player")].some(
    (audio) => !audio.paused && !audio.ended,
  );
}

function autoRefreshBlocked() {
  return (
    !byId("dialog-backdrop").hidden ||
    state.loading ||
    state.recordingActionPending ||
    state.dauersongActionPending ||
    state.whaleActionPending ||
    state.replayPlaying ||
    recordingPlaybackActive() ||
    window.performance.now() < state.interactionUntil
  );
}

function autoRefreshTick() {
  if (!backendAllowed()) return;
  if (state.autoRefresh && !document.hidden && !autoRefreshBlocked()) {
    refreshSnapshot(false);
  }
}

function scheduleAutoRefresh() {
  if (state.timer) window.clearInterval(state.timer);
  state.timer = null;
  if (!backendAllowed()) return;
  state.timer = window.setInterval(autoRefreshTick, 8000);
}

function keepDialogFocus(event) {
  if (event.key !== "Tab" || byId("dialog-backdrop").hidden) return;
  const dialog = byId("dialog-backdrop").querySelector("[role='dialog']");
  const focusable = [...dialog.querySelectorAll("button:not([disabled]), a[href]")];
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function wireEvents() {
  window.addEventListener("hashchange", applyRoute);
  window.addEventListener("pageshow", () => {
    configureNativeScrollRestoration();
    // Safari/iPadOS may restore a previous scroll position after initial script
    // execution. Re-assert the Home position only for the default route;
    // explicit deep-route targets such as #aufnehmen remain untouched.
    restoreHomeStartPosition();
    window.requestAnimationFrame(restoreHomeStartPosition);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !backendAllowed()) {
      stopTelemetryPolling();
      return;
    }
    requestTelemetry().finally(() => scheduleTelemetryPolling());
  });
  document.addEventListener("pointerdown", markTransientInteraction, {
    passive: true,
  });
  byId("refresh-button").addEventListener("click", () => refreshSnapshot(true));
  byId("diagnostic-refresh").addEventListener("click", () => refreshSnapshot(true));
  byId("library-view").addEventListener("change", (event) => {
    if (!LIBRARY_VIEWS.has(event.target.value)) return;
    state.libraryView = event.target.value;
    savePreference("audio-ui-library-view", state.libraryView);
    renderLibrary();
  });
  byId("library-category-filter").addEventListener("change", (event) => {
    const next = event.target.value;
    if (next !== "all" && !Object.hasOwn(RECORDING_LIBRARY_CATEGORIES, next)) return;
    state.libraryCategory = next;
    savePreference("audio-ui-library-category", state.libraryCategory);
    renderLibrary();
  });
  byId("library-sort").addEventListener("change", (event) => {
    if (!LIBRARY_SORTS.has(event.target.value)) return;
    state.librarySort = event.target.value;
    savePreference("audio-ui-library-sort", state.librarySort);
    renderLibrary();
  });
  byId("replay-scenario").addEventListener("change", (event) => {
    stopReplay();
    state.replayScenarioId = event.target.value;
    state.replayFrameIndex = 0;
    renderReplay();
  });
  byId("replay-play").addEventListener("click", toggleReplay);
  byId("replay-step").addEventListener("click", stepReplay);
  byId("replay-reset").addEventListener("click", resetReplay);
  byId("dialog-close").addEventListener("click", closeDialog);
  byId("global-take-player-close").addEventListener("click", () =>
    clearGlobalTakePlayer({ restoreFocus: true }),
  );
  byId("dialog-backdrop").addEventListener("click", (event) => {
    if (event.target === byId("dialog-backdrop")) closeDialog();
  });
  document.addEventListener("keydown", (event) => {
    markTransientInteraction();
    if (event.key === "Escape" && !byId("dialog-backdrop").hidden) {
      closeDialog();
      return;
    }
    if (event.key === "Escape" && focusedDepthPanel) {
      event.preventDefault();
      closeDepthFocus();
      return;
    }
    keepDialogFocus(event);
    keepDepthFocus(event);
  });
  byId("motion-toggle").addEventListener("change", (event) => {
    document.documentElement.classList.toggle("reduced-motion", event.target.checked);
    savePreference("audio-ui-reduce-motion", event.target.checked);
  });
  byId("auto-refresh-toggle").addEventListener("change", (event) => {
    state.autoRefresh = event.target.checked;
    savePreference("audio-ui-auto-refresh", event.target.checked);
  });
  for (const input of document.querySelectorAll("input[name='runtime-mode']")) {
    input.addEventListener("change", (event) => {
      const next = event.target.value;
      if (!event.target.checked) return;
      if (!Object.hasOwn(RUNTIME_MODES, next) || next === state.runtimeMode) return;
      state.runtimeMode = next;
      state.capabilities = detectCapabilities();
      applyRuntimeMode({ persist: true });
    });
  }
}

installBackendFetchGuard();
configureNativeScrollRestoration();
state.runtimeMode = loadRuntimeMode();
state.capabilities = detectCapabilities();
loadPreferences();
installTaskWorkspaceLayout();
wireEvents();
wireDepthPanels();
applyRoute();
restoreHomeStartPosition();
registerServiceWorker();
applyRuntimeMode();
