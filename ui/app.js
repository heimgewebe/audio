"use strict";

const ROUTES = {
  start: { title: "Start", eyebrow: "Dein Audio heute" },
  spielen: { title: "Spielen", eyebrow: "Instrumente & Gesten" },
  aufnehmen: { title: "Aufnehmen", eyebrow: "Quellen & Sicherheit" },
  hoeren: { title: "Hören", eyebrow: "Wiedergabewege" },
  klaenge: { title: "Klänge", eyebrow: "Klanglabor" },
  verbindungen: { title: "Verbindungen", eyebrow: "Geräte & Signalweg" },
  diagnose: { title: "Diagnose", eyebrow: "Beobachteter Zustand" },
  einstellungen: { title: "Einstellungen", eyebrow: "Dienst & Darstellung" },
};

const MODE_LABELS = {
  morph: "88-Tasten-Morph",
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

const state = {
  snapshot: null,
  route: "start",
  loading: false,
  actionPending: false,
  autoRefresh: true,
  timer: null,
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

async function fetchJson(url, options = {}) {
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
  notice.hidden = false;
}

function clearNotice() {
  const notice = byId("global-notice");
  notice.hidden = true;
  notice.textContent = "";
  notice.classList.remove("success");
}

function setLoading(loading) {
  state.loading = loading;
  const button = byId("refresh-button");
  button.disabled = loading;
  button.classList.toggle("is-loading", loading);
  byId("diagnostic-refresh").disabled = loading;
  if (state.snapshot) renderWhale();
}

async function refreshSnapshot(force = false) {
  if (state.loading || state.actionPending) return;
  setLoading(true);
  try {
    const suffix = force ? "?refresh=1" : "";
    const snapshot = await fetchJson(`/api/v1/snapshot${suffix}`, {
      timeoutMs: 50000,
    });
    state.snapshot = snapshot;
    clearNotice();
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

function renderAll() {
  if (!state.snapshot) return;
  renderAuthority("ready");
  byId("updated-at").textContent = formatTimestamp(state.snapshot.generated_at);
  byId("mobile-updated-at").textContent = formatTimestamp(
    state.snapshot.generated_at,
  ).replace("Stand ", "");
  renderHome();
  renderWhale();
  renderProfiles();
  renderSounds();
  renderConnections();
  renderDiagnostics();
  renderSettings();
}

function renderHome() {
  const snapshot = state.snapshot;
  const summary = snapshot.summary;
  const card = byId("home-state-card");
  card.replaceChildren();

  const statusSymbol = element(
    "span",
    `state-symbol${summary.state === "stable" ? "" : " attention"}`,
    summary.state === "stable" ? "✓" : "!",
  );
  statusSymbol.setAttribute("aria-hidden", "true");
  card.append(statusSymbol);

  const copy = element("div");
  appendText(
    copy,
    "h2",
    "",
    summary.state === "stable" ? "Der Grundzustand ist ruhig." : "Ein Blick lohnt sich.",
  );
  let detail;
  if (snapshot.doctor.status !== "ok") {
    detail = "Der Audio-Doctor ist gerade nicht vollständig lesbar.";
  } else if (summary.high_warning_count > 0) {
    detail = `${summary.high_warning_count} wichtiger Befund braucht Aufmerksamkeit.`;
  } else if (summary.warning_count > 0) {
    detail = `${summary.warning_count} Hinweise sind dokumentiert, ohne automatische Reparatur.`;
  } else {
    detail = "Keine Doctor-Warnung im zuletzt gelesenen Zustand.";
  }
  appendText(copy, "p", "", detail);
  card.append(copy);

  const foot = element("div", "state-foot");
  appendText(
    foot,
    "span",
    "",
    summary.active_whale ? "Walstimme läuft" : "Walstimme ruht",
  );
  const diagnosis = element("a", "text-link", "Diagnose →");
  diagnosis.href = "#diagnose";
  foot.append(diagnosis);
  card.append(foot);

  const tasks = [
    {
      route: "spielen",
      glyph: "♬",
      title: summary.active_whale ? "Walstimme steuern" : "Walstimme spielen",
      detail: summary.active_whale
        ? `Aktiv: ${displayMode(snapshot.whale.service.voice_mode)}`
        : `Inaktiv · Startmodus ${displayMode(snapshot.whale.contract.default_mode)}`,
    },
    {
      route: "hoeren",
      glyph: "◖",
      title: "Bewusst hören",
      detail: `${profilesFor("listening").length} Wiedergabewege vergleichen`,
    },
    {
      route: "aufnehmen",
      glyph: "●",
      title: "Aufnahme planen",
      detail: "Quelle, Gate und Headroom prüfen",
    },
    {
      route: "verbindungen",
      glyph: "⌁",
      title: "Signalweg ansehen",
      detail: `${summary.physical_unknown_count} physische Fakten offen`,
    },
  ];
  const taskGrid = byId("home-tasks");
  taskGrid.replaceChildren(
    ...tasks.map((task) => {
      const cardLink = element("a", "task-card");
      cardLink.href = `#${task.route}`;
      appendText(cardLink, "span", "task-icon", task.glyph).setAttribute(
        "aria-hidden",
        "true",
      );
      appendText(cardLink, "span", "task-arrow", "↗").setAttribute("aria-hidden", "true");
      appendText(cardLink, "h3", "", task.title);
      appendText(cardLink, "p", "", task.detail);
      return cardLink;
    }),
  );

  const insightGrid = byId("home-insights");
  const warnings = Array.isArray(snapshot.doctor.warnings)
    ? snapshot.doctor.warnings.slice(0, 3)
    : [];
  if (warnings.length === 0) {
    insightGrid.replaceChildren(
      insightCard("Doctor", "Keine gemeldeten Warnungen.", "ok"),
      insightCard(
        "Physische Wahrheit",
        `${summary.physical_unknown_count} Fakten bleiben explizit offen.`,
        summary.physical_unknown_count ? "medium" : "ok",
      ),
      insightCard(
        "Browsergrenze",
        "Kein kritisches Audio wird hier verarbeitet.",
        "ok",
      ),
    );
    return;
  }
  insightGrid.replaceChildren(
    ...warnings.map((warning) =>
      insightCard(
        warning.code || "Hinweis",
        warning.detail || "Doctor-Hinweis ohne Detail.",
        warning.severity || "medium",
      ),
    ),
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
  const whale = state.snapshot.whale;
  const service = whale.service || {};
  const active = Boolean(service.active);
  const currentMode = service.voice_mode || whale.contract.default_mode;
  const statusReadable = whale.status === "ok";
  const keyboard = whale.contract.keyboard;

  const intro = byId("play-intro-stat");
  intro.replaceChildren();
  appendText(
    intro,
    "strong",
    "",
    active ? "Aktiv" : statusReadable ? "Inaktiv" : "Offen",
  );
  appendText(
    intro,
    "span",
    "",
    active
      ? displayMode(currentMode)
      : statusReadable
        ? "Start prüft alle Voraussetzungen"
        : "Status nicht lesbar",
  );

  const wrapper = element("div", "whale-panel");
  wrapper.setAttribute("aria-busy", String(state.actionPending));
  const main = element("article", "whale-main");
  const title = element("div", "whale-title");
  const copy = element("div");
  appendText(copy, "p", "eyebrow", "Buckelwal Live Voice");
  appendText(copy, "h2", "", active ? "Die Walstimme ist wach." : "Eine Stimme, ein Instrument.");
  appendText(
    copy,
    "p",
    "",
    active
      ? `Der Backend-Readback meldet ${displayMode(currentMode)} als laufenden Modus.`
      : `${keyboard.key_count} Tasten von ${keyboard.lowest_key} bis ${keyboard.highest_key}; der Start bleibt bis zum Backend-Readback offen.`,
  );
  title.append(copy);
  const statusPill = element(
    "span",
    `status-pill ${statusReadable ? (active ? "ready" : "") : "unavailable"}`,
    statusReadable ? (active ? "läuft" : "inaktiv") : "unbekannt",
  );
  title.append(statusPill);
  main.append(title);

  const modes = whale.contract.modes || [];
  const picker = element("fieldset", "mode-picker");
  appendText(picker, "legend", "", "Klangcharakter");
  for (const mode of modes) {
    const label = element("label", "mode-choice");
    const input = element("input");
    input.type = "radio";
    input.name = "whale-mode";
    input.value = mode.id;
    input.checked = mode.id === currentMode;
    input.disabled = state.loading || state.actionPending || !statusReadable;
    label.append(input, element("span", "", displayMode(mode.id)));
    picker.append(label);
  }
  main.append(picker);

  const actions = element("div", "card-actions");
  const actionButton = element(
    "button",
    active ? "primary-button danger" : "primary-button",
    active ? "Walstimme beenden" : "Walstimme starten",
  );
  actionButton.type = "button";
  actionButton.id = "whale-primary-action";
  actionButton.disabled = state.loading || state.actionPending || !statusReadable;
  actionButton.addEventListener("click", () => {
    if (active) {
      runWhaleAction("stop");
    } else {
      runWhaleAction("start", selectedWhaleMode());
    }
  });
  actions.append(actionButton);

  if (active) {
    const modeButton = element("button", "secondary-button", "Modus übernehmen");
    modeButton.type = "button";
    modeButton.id = "whale-mode-action";
    modeButton.disabled =
      state.loading || state.actionPending || selectedWhaleMode() === currentMode;
    picker.addEventListener("change", () => {
      modeButton.disabled =
        state.loading || state.actionPending || selectedWhaleMode() === currentMode;
    });
    modeButton.addEventListener("click", () =>
      runWhaleAction("mode", selectedWhaleMode()),
    );
    actions.append(modeButton);
  }
  main.append(actions);

  const details = element("aside", "whale-details");
  appendText(details, "p", "eyebrow", "Autoritativer Dienst");
  appendText(details, "h2", "", statusReadable ? "Laufzeit" : "Nicht lesbar");
  const list = element("dl");
  detailRow(list, "Modus", active ? displayMode(currentMode) : "—");
  detailRow(list, "MIDI-Port", service.midi_port || "automatisch");
  detailRow(list, "Ausgabe", service.target || "PipeWire-Standard");
  detailRow(
    list,
    "Block",
    service.latency_frames
      ? `${service.latency_frames} Frames`
      : `${whale.contract.audio.block_frames || "—"} Frames`,
  );
  detailRow(
    list,
    "Laufzeitgrenze",
    service.runtime_max_seconds
      ? `${service.runtime_max_seconds} s`
      : `${whale.contract.runtime.maximum_runtime_seconds || "—"} s`,
  );
  details.append(list);
  if (whale.error) appendText(details, "p", "dialog-message", whale.error);

  wrapper.append(main, details);
  byId("whale-control").replaceChildren(wrapper);
}

function selectedWhaleMode() {
  const checked = document.querySelector('input[name="whale-mode"]:checked');
  return checked ? checked.value : state.snapshot.whale.contract.default_mode;
}

function setWhalePending(pending) {
  const panel = byId("whale-control").querySelector(".whale-panel");
  if (!panel) return;
  panel.setAttribute("aria-busy", String(pending));
  for (const control of panel.querySelectorAll("button, input")) {
    control.disabled = pending;
  }
}

async function runWhaleAction(operation, mode) {
  if (!state.snapshot || state.loading || state.actionPending) return;
  state.actionPending = true;
  clearNotice();
  setWhalePending(true);
  try {
    const payload = { operation };
    if (mode) payload.mode = mode;
    const result = await fetchJson("/api/v1/actions/whale", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Audio-Control-Token": state.snapshot.service.action_token,
      },
      body: JSON.stringify(payload),
      timeoutMs: 70000,
    });
    state.snapshot = result.snapshot;
    renderAll();
    const confirmation =
      operation === "stop"
        ? "Walstimme wurde beendet und als inaktiv zurückgelesen."
        : `Walstimme wurde als ${displayMode(mode)} aktiv zurückgelesen.`;
    showNotice(confirmation, "success");
  } catch (error) {
    showNotice(error instanceof Error ? error.message : "Audioaktion wurde blockiert.");
  } finally {
    state.actionPending = false;
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
  appendText(boundary, "strong", "", "Sicherheitsgrenze dieser Stufe");
  appendText(boundary, "p", "", recording.detail);

  const listening = profilesFor("listening");
  const listeningStat = byId("listen-intro-stat");
  listeningStat.replaceChildren();
  appendText(listeningStat, "strong", "", String(listening.length));
  appendText(listeningStat, "span", "", "explizite Hörwege");
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
  const top = element("div", "card-topline");
  appendText(top, "span", "card-glyph", PROFILE_GLYPHS[profile.id] || "◇").setAttribute(
    "aria-hidden",
    "true",
  );
  const status = profile.operational_status === "planned" ? "geplant" : "read-only";
  appendText(
    top,
    "span",
    `status-pill ${profile.operational_status === "planned" ? "" : "ready"}`,
    status,
  );
  card.append(top);
  appendText(card, "h3", "", displayProfile(profile.id));
  appendText(card, "p", "", profile.purpose);
  const meta = element("div", "card-meta");
  const hardwareCount = Array.isArray(profile.required_hardware)
    ? profile.required_hardware.length
    : 0;
  const gateCount = Array.isArray(profile.required_laboratory_gates)
    ? profile.required_laboratory_gates.length
    : 0;
  appendText(meta, "span", "", `${hardwareCount} Geräte`);
  appendText(meta, "span", "", `${gateCount} Labor-Gates`);
  card.append(meta);
  const actions = element("div", "card-actions");
  const button = element("button", "secondary-button", "Plan prüfen");
  button.type = "button";
  button.addEventListener("click", (event) => openProfilePlan(profile, event.currentTarget));
  actions.append(button);
  card.append(actions);
  return card;
}

async function openProfilePlan(profile, trigger) {
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

function renderSounds() {
  const target = byId("sound-library");
  const whale = state.snapshot.whale;
  const activeMode = whale.service.voice_mode;
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
    const link = element("a", "secondary-button", "Unter Spielen öffnen");
    link.href = "#spielen";
    actions.append(link);
    card.append(actions);
    return card;
  });

  const song = element("article", "sound-card");
  const songTop = element("div", "card-topline");
  appendText(songTop, "span", "card-glyph", "∞").setAttribute("aria-hidden", "true");
  appendText(songTop, "span", "status-pill", "geplant");
  song.append(songTop);
  appendText(song, "h3", "", "Dauersong");
  appendText(song, "p", "", state.snapshot.dauersong.detail);
  const songMeta = element("div", "card-meta");
  appendText(songMeta, "span", "", "Profil experimental");
  appendText(songMeta, "span", "", "fail-closed");
  song.append(songMeta);
  cards.push(song);
  target.replaceChildren(...cards);
}

function soundModeDescription(mode) {
  if (mode.id === "morph") {
    return "Quellengestützte, chromatisch spielbare Morph-Stimme ohne Samplezonen.";
  }
  if (mode.id === "realistic") {
    return "Lizenzierte Naturaufnahmen mit begrenzter Tonhöhenverschiebung.";
  }
  if (mode.id === "ufo") {
    return "Historischer synthetischer Vergleichsmodus, ausdrücklich kein Realismusbeleg.";
  }
  return `Backend: ${formatEndpoint(mode.backend)}.`;
}

function renderConnections() {
  const doctor = state.snapshot.doctor;
  const graph = doctor.graph || {};
  const hardware = doctor.hardware || {};
  const external = doctor.external_endpoints || {};
  const observedInputs = [
    hardware.motu_m2 ? "MOTU M2" : null,
    hardware.roland_fp_30x ? "Roland FP-30X" : null,
  ].filter(Boolean);
  const nodes = [
    {
      eyebrow: "Eingaben",
      title: observedInputs.length ? observedInputs.join(" · ") : "keine belegt",
      detail: observedInputs.length
        ? `${observedInputs.length} Geräte beobachtet`
        : "physisch nicht bestätigt",
    },
    {
      eyebrow: "Control",
      title: "PipeWire",
      detail: `${graph.force_rate_hz || "—"} Hz · ${graph.force_quantum_frames || "—"} Frames`,
    },
    {
      eyebrow: "Standardziel",
      title: formatEndpoint(graph.default_sink),
      detail: `Quelle: ${formatEndpoint(graph.default_source)}`,
    },
    {
      eyebrow: "Außenwelt",
      title: external.pioneer_vsx_830_k?.software_observed
        ? "Pioneer beobachtet"
        : "physisch offen",
      detail: "Focal · Pioneer · 1MII",
    },
  ];
  const flow = element("div", "connection-flow");
  for (const node of nodes) {
    const card = element("article", "connection-node");
    appendText(card, "p", "eyebrow", node.eyebrow);
    appendText(card, "strong", "", node.title);
    appendText(card, "small", "", node.detail);
    flow.append(card);
  }
  byId("connection-map").replaceChildren(flow);

  const facts = Array.isArray(doctor.physical_unknowns)
    ? doctor.physical_unknowns
    : [];
  const factList = byId("physical-facts");
  if (!facts.length) {
    factList.replaceChildren(
      element("div", "empty-state", "Keine offenen physischen Fakten gemeldet."),
    );
    return;
  }
  factList.replaceChildren(
    ...facts.map((fact) => {
      const row = element("div", "fact-row");
      appendText(row, "code", "", fact);
      appendText(row, "span", "", "nicht verifiziert");
      return row;
    }),
  );
}

function renderDiagnostics() {
  const doctor = state.snapshot.doctor;
  const graph = doctor.graph || {};
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
        appendText(
          row,
          "span",
          `status-dot ${warning.severity || "medium"}`,
        ).setAttribute("aria-hidden", "true");
        const copy = element("div");
        appendText(copy, "strong", "", warning.code || "Doctor-Hinweis");
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
  const count = warnings.length + (doctor.status === "ok" ? 0 : 1);
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

function routeFromHash() {
  const candidate = window.location.hash.slice(1);
  return ROUTES[candidate] ? candidate : "start";
}

function prefersReducedMotion() {
  return (
    document.documentElement.classList.contains("reduced-motion") ||
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function applyRoute(event) {
  const route = routeFromHash();
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
  byId("view-title").textContent = ROUTES[route].title;
  byId("view-eyebrow").textContent = ROUTES[route].eyebrow;
  document.title = `${ROUTES[route].title} · Audiozentrale`;
  if (event?.type === "hashchange") {
    byId("view-title").focus({ preventScroll: true });
  }
  window.scrollTo({
    top: 0,
    behavior: prefersReducedMotion() ? "auto" : "smooth",
  });
}

function loadPreferences() {
  try {
    const reduceMotion =
      window.localStorage.getItem("audio-ui-reduce-motion") === "true";
    const autoRefresh =
      window.localStorage.getItem("audio-ui-auto-refresh") !== "false";
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

function scheduleAutoRefresh() {
  if (state.timer) window.clearInterval(state.timer);
  state.timer = window.setInterval(() => {
    const active = document.activeElement;
    const interactionInProgress =
      !byId("dialog-backdrop").hidden ||
      active?.matches("a[href], button, input, select, textarea") ||
      Boolean(active?.closest("[role='dialog']"));
    if (state.autoRefresh && !document.hidden && !interactionInProgress) {
      refreshSnapshot(false);
    }
  }, 8000);
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
  byId("refresh-button").addEventListener("click", () => refreshSnapshot(true));
  byId("diagnostic-refresh").addEventListener("click", () => refreshSnapshot(true));
  byId("dialog-close").addEventListener("click", closeDialog);
  byId("dialog-backdrop").addEventListener("click", (event) => {
    if (event.target === byId("dialog-backdrop")) closeDialog();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !byId("dialog-backdrop").hidden) closeDialog();
    keepDialogFocus(event);
  });
  byId("motion-toggle").addEventListener("change", (event) => {
    document.documentElement.classList.toggle("reduced-motion", event.target.checked);
    savePreference("audio-ui-reduce-motion", event.target.checked);
  });
  byId("auto-refresh-toggle").addEventListener("change", (event) => {
    state.autoRefresh = event.target.checked;
    savePreference("audio-ui-auto-refresh", event.target.checked);
  });
}

loadPreferences();
wireEvents();
applyRoute();
scheduleAutoRefresh();
refreshSnapshot(true);
