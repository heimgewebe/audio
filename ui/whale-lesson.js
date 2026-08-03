"use strict";

async function loadWhaleLesson() {
  try {
    const lesson = await fetchJson("/api/v1/whale/lesson", {
      timeoutMs: 12000,
    });
    if (
      lesson.authoritative !== false ||
      lesson.authority !== "educational-model" ||
      lesson.read_only !== true
    ) {
      throw new Error("Die Lektions-Wahrheitsgrenze ist ungültig.");
    }
    state.whaleLesson = lesson;
    state.whaleLessonError = null;
  } catch (error) {
    state.whaleLesson = null;
    state.whaleLessonError =
      error instanceof Error ? error.message : "Lektion ist nicht lesbar.";
  }
  renderWhaleLessonSummary();
}

function lessonVariant(id) {
  return (
    state.whaleLesson?.variants?.find((variant) => variant.id === id) || null
  );
}

function renderWhaleLessonSummary() {
  const target = byId("whale-lesson-summary");
  const stages = byId("whale-lesson-stages");
  if (!target || !stages) return;
  target.replaceChildren();
  stages.replaceChildren();
  if (!state.whaleLesson) {
    appendText(
      target,
      "strong",
      "",
      state.whaleLessonError ? "Lektion nicht verfügbar" : "Lektion wird geladen",
    );
    appendText(
      target,
      "span",
      "",
      state.whaleLessonError || "Revisionsgebundene Hörproben werden geprüft.",
    );
    return;
  }
  appendText(target, "strong", "", state.whaleLesson.title);
  appendText(
    target,
    "span",
    "",
    `${state.whaleLesson.variants.length} Hörproben · Beobachtung, Modell und Extrapolation getrennt`,
  );
  for (const step of state.whaleLesson.steps) {
    const row = element("article", "lesson-stage-row");
    appendText(row, "strong", "", step.title);
    appendText(row, "span", "", step.instruction);
    stages.append(row);
  }
}

function curvePath(values, width, height) {
  return values
    .map((value, index) => {
      const x = (index / Math.max(1, values.length - 1)) * width;
      const y = height - Math.max(0, Math.min(1, Number(value))) * height;
      return `${index ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

function lessonFeatureChart(variant) {
  const wrapper = element("div", "lesson-feature-chart");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 480 150");
  svg.setAttribute("role", "img");
  svg.setAttribute(
    "aria-label",
    `Merkmalsverläufe für ${variant.title}: Hüllkurve, Periodizität und Rauigkeit`,
  );
  const curves = [
    ["envelope", "Hüllkurve"],
    ["periodicity", "Periodizität"],
    ["roughness", "Rauigkeit"],
  ];
  for (const [name, label] of curves) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", curvePath(variant.features[name], 480, 150));
    path.setAttribute("class", `lesson-curve ${name}`);
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = label;
    path.append(title);
    svg.append(path);
  }
  wrapper.append(svg);
  const legend = element("div", "lesson-curve-legend");
  for (const [name, label] of curves) {
    legend.append(element("span", name, label));
  }
  wrapper.append(legend);
  return wrapper;
}

function stopLessonAudio() {
  if (state.lessonAudio) {
    state.lessonAudio.pause();
    state.lessonAudio.currentTime = 0;
    state.lessonAudio = null;
  }
  const content = byId("dialog-content");
  for (const audio of content ? content.querySelectorAll("audio") : []) {
    audio.pause();
    audio.currentTime = 0;
  }
}

function playLessonVariant(id, statusTarget = null) {
  const variant = lessonVariant(id);
  if (!variant) return;
  stopLessonAudio();
  const audio = new Audio(variant.audio_url);
  state.lessonAudio = audio;
  if (statusTarget) statusTarget.textContent = `Spielt: ${variant.title}`;
  audio.addEventListener(
    "ended",
    () => {
      if (state.lessonAudio === audio) state.lessonAudio = null;
      if (statusTarget) statusTarget.textContent = "Hörprobe beendet.";
    },
    { once: true },
  );
  audio.play().catch(() => {
    if (statusTarget) {
      statusTarget.textContent = "Der Browser hat die Hörprobe blockiert.";
    }
  });
}

function randomBlindOrder() {
  const candidates = [...state.whaleLesson.blind_comparison.candidate_ids];
  const values = new Uint32Array(1);
  window.crypto.getRandomValues(values);
  if (values[0] % 2) candidates.reverse();
  state.blindOrder = candidates;
  state.blindAnswered = false;
}

function revealBlindResult(choice, result) {
  state.blindAnswered = true;
  const chosenId = state.blindOrder[choice === "A" ? 0 : 1];
  const otherId = state.blindOrder[choice === "A" ? 1 : 0];
  result.replaceChildren();
  appendText(
    result,
    "strong",
    "",
    `${choice} war ${lessonVariant(chosenId).title}.`,
  );
  appendText(
    result,
    "span",
    "",
    `Die andere Probe war ${lessonVariant(otherId).title}. Dein Urteil bleibt lokal und ist kein Realismusnachweis.`,
  );
}

function openWhaleLesson(trigger) {
  state.dialogRequest += 1;
  state.lastDialogTrigger = trigger;
  byId("dialog-eyebrow").textContent =
    "Read-only Lernfokus · Beobachtung ist nicht Modell";
  byId("dialog-title").textContent =
    state.whaleLesson?.title || "Buckelwal-Lektion";
  const content = byId("dialog-content");
  content.replaceChildren();
  if (!state.whaleLesson) {
    appendText(
      content,
      "p",
      "dialog-message",
      state.whaleLessonError || "Die Lektion wird noch geprüft.",
    );
  } else {
    const truth = element("div", "lesson-truth-grid");
    for (const [layer, description] of Object.entries(
      state.whaleLesson.truth_layers,
    )) {
      const card = element("article", `lesson-truth-card ${layer}`);
      appendText(
        card,
        "strong",
        "",
        {
          observation: "Beobachtung",
          model: "Modell",
          extrapolation: "Extrapolation",
        }[layer],
      );
      appendText(card, "span", "", description);
      truth.append(card);
    }
    content.append(truth);

    const provenance = element("details", "lesson-provenance");
    provenance.append(element("summary", "", "Quellen und Lizenzen"));
    const referenceSource = element("div", "lesson-source-group");
    appendText(referenceSource, "strong", "", "Echte Referenz");
    appendText(
      referenceSource,
      "span",
      "",
      `${state.whaleLesson.reference_source.attribution} · ${state.whaleLesson.reference_source.license}`,
    );
    provenance.append(referenceSource);
    const modelSourceGroup = element("div", "lesson-source-group");
    appendText(modelSourceGroup, "strong", "", "Modellanker");
    for (const source of state.whaleLesson.model_sources.sources) {
      appendText(
        modelSourceGroup,
        "span",
        "",
        `${source.attribution} · ${source.license}`,
      );
    }
    provenance.append(modelSourceGroup);
    content.append(provenance);

    appendText(content, "p", "dialog-message", state.whaleLesson.question);
    const variants = element("div", "lesson-variant-grid");
    for (const variant of state.whaleLesson.variants) {
      const card = element(
        "article",
        `lesson-variant-card ${variant.truth_layer}`,
      );
      const heading = element("div", "lesson-variant-heading");
      appendText(heading, "h3", "", variant.title);
      appendText(
        heading,
        "span",
        "status-pill",
        variant.truth_layer === "observation" ? "Beobachtung" : "Modell",
      );
      card.append(heading);
      appendText(card, "p", "", variant.description);
      const audio = element("audio", "lesson-audio");
      audio.controls = true;
      audio.preload = "none";
      audio.src = variant.audio_url;
      audio.setAttribute("aria-label", `Hörprobe: ${variant.title}`);
      card.append(audio);
      appendText(card, "p", "listen-for", variant.listen_for);
      card.append(lessonFeatureChart(variant));
      const metrics = element("div", "lesson-metrics");
      appendText(
        metrics,
        "span",
        "",
        `Voiced: ${Math.round(Number(variant.summary.voiced_fraction) * 100)} %`,
      );
      appendText(
        metrics,
        "span",
        "",
        variant.summary.median_periodicity === null
          ? "Periodizität: unsicher"
          : `Median-Periodizität: ${Number(
              variant.summary.median_periodicity,
            ).toFixed(2)}`,
      );
      card.append(metrics);
      variants.append(card);
    }
    content.append(variants);

    const blind = element("section", "lesson-blind");
    appendText(blind, "p", "eyebrow", "Lokaler Blindvergleich");
    appendText(
      blind,
      "h3",
      "",
      state.whaleLesson.blind_comparison.prompt,
    );
    const blindStatus = appendText(
      blind,
      "p",
      "lesson-blind-status",
      "Die Zuordnung A/B wird erst nach deiner Wahl gezeigt.",
    );
    randomBlindOrder();
    const play = element("div", "lesson-blind-actions");
    const referenceButton = element("button", "secondary-button", "Referenz");
    referenceButton.type = "button";
    referenceButton.addEventListener("click", () =>
      playLessonVariant("reference", blindStatus),
    );
    const aButton = element("button", "secondary-button", "A");
    aButton.type = "button";
    aButton.addEventListener("click", () =>
      playLessonVariant(state.blindOrder[0], blindStatus),
    );
    const bButton = element("button", "secondary-button", "B");
    bButton.type = "button";
    bButton.addEventListener("click", () =>
      playLessonVariant(state.blindOrder[1], blindStatus),
    );
    play.append(referenceButton, aButton, bButton);
    blind.append(play);
    const choose = element("div", "lesson-blind-actions choose");
    const result = element("div", "lesson-blind-result");
    for (const choice of ["A", "B"]) {
      const button = element(
        "button",
        "primary-button",
        `${choice} wirkt weniger instrumentenartig`,
      );
      button.type = "button";
      button.addEventListener("click", () =>
        revealBlindResult(choice, result),
      );
      choose.append(button);
    }
    const remix = element("button", "secondary-button", "Neu mischen");
    remix.type = "button";
    remix.addEventListener("click", () => {
      stopLessonAudio();
      randomBlindOrder();
      result.replaceChildren();
      blindStatus.textContent = "A/B wurde neu gemischt.";
    });
    choose.append(remix);
    blind.append(choose, result);
    content.append(blind);

    appendText(
      content,
      "p",
      "read-only-boundary",
      "Keine Hörprobe startet die Liveengine. Keine Wahl wird gespeichert. Kurven und Hörproben belegen keine biologische Gleichheit oder Rufbedeutung.",
    );
  }
  content.setAttribute("aria-busy", "false");
  byId("dialog-backdrop").hidden = false;
  document.body.classList.add("dialog-open");
  byId("app-shell").setAttribute("inert", "");
  byId("dialog-close").focus();
}
