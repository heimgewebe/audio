"use strict";

/*
 * Audiozentrale App-Shell-Service-Worker v1.
 *
 * Autoritätsgrenze:
 *   - Der Worker cached ausschließlich eine feste, statische App-Shell.
 *   - Gleichherkunfts-Anfragen unter /api/ sind strikt network-only: kein
 *     Cache, kein Cache-Fallback, kein Replay, keine Queue, kein Background
 *     Sync, kein stale state.
 *   - Mutationen (nicht-GET), Anmeldedaten, Qobuz-/Provider-Zustand,
 *     Wiedergabe-, Routing- und Gerätesteuerungsantworten sowie WAV-/Audiodaten
 *     werden niemals gecached.
 *   - Alles, was nicht exakt zur App-Shell gehört, läuft unverändert über das
 *     normale Netzwerkverhalten des Browsers.
 *
 * Der Worker verschafft keiner Ansicht Hardware-, Geräte- oder Backendautorität.
 */

const CACHE_PREFIX = "audiozentrale-app-shell-";
const CACHE_NAME = `${CACHE_PREFIX}v1`;

/* Feste Liste. Sie enthält bewusst keine WAV-Datei und keinen /api/-Pfad. */
const APP_SHELL = Object.freeze([
  "/",
  "/index.html",
  "/app.js",
  "/whale-lesson.js",
  "/styles.css",
  "/manifest.webmanifest",
  "/icon-180.png",
  "/icon-192.png",
  "/icon-512.png",
]);

const APP_SHELL_PATHS = new Set(APP_SHELL);

function isApiPath(pathname) {
  return pathname === "/api" || pathname.startsWith("/api/");
}

/*
 * Nur exakte, query-freie App-Shell-Pfade sind cachefähig. Ein Suffix, eine
 * Query oder ein unbekannter Pfad fällt auf normales Netzwerkverhalten zurück.
 */
function appShellKey(url, request) {
  if (url.search) return null;
  if (request.mode === "navigate") {
    return url.pathname === "/" || url.pathname === "/index.html" ? "/" : null;
  }
  return APP_SHELL_PATHS.has(url.pathname) ? url.pathname : null;
}

function isCacheableShellResponse(response) {
  return Boolean(
    response &&
      response.status === 200 &&
      response.type === "basic" &&
      !response.bodyUsed,
  );
}

/*
 * Netzwerk zuerst, Cache nur als Offline-Rückfall. Damit kann eine neu
 * ausgelieferte App-Shell nie dauerhaft von einer alten Kopie verdeckt werden.
 */
async function appShellResponse(key, request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (isCacheableShellResponse(response)) {
      await cache.put(key, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await cache.match(key);
    if (cached) return cached;
    throw error;
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      await cache.addAll(APP_SHELL);
      /* Sofortige Ablösung verhindert einen dauerhaft veralteten Controller. */
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      /* Nur eigene alte Audiozentrale-Caches dürfen entfernt werden. */
      await Promise.all(
        names
          .filter(
            (name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME,
          )
          .map((name) => caches.delete(name)),
      );
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;

  /* Mutationen und andere Nicht-GET-Methoden bleiben unangetastet. */
  if (request.method !== "GET") return;

  let url;
  try {
    url = new URL(request.url);
  } catch (_error) {
    return;
  }

  /* Fremdherkunft bleibt normales Netzwerkverhalten. */
  if (url.origin !== self.location.origin) return;

  if (isApiPath(url.pathname)) {
    /*
     * Strikt network-only: keine Cache-Öffnung, kein Fallback, kein Replay.
     * Ein Netzwerkfehler bleibt ein Netzwerkfehler.
     */
    event.respondWith(fetch(request));
    return;
  }

  const key = appShellKey(url, request);
  if (!key) return;

  event.respondWith(appShellResponse(key, request));
});

/*
 * Bewusst nicht implementiert: "sync", "periodicsync", "push",
 * "backgroundfetchsuccess" und "message"-getriebene Wiederholungen. Es gibt
 * keinen Pfad, auf dem dieser Worker eine Anfrage später erneut absendet.
 */
