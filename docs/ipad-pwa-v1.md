# iPad- und PWA-Fläche v1

Diese Stufe macht die **bestehende** kanonische Oberfläche (`ui/index.html`,
`ui/app.js`, `ui/styles.css`, ausgeliefert von `scripts/audio_control.py`)
installierbar. Es gibt keine zweite App, kein Framework und keine neue
Abhängigkeit.

Maschinenlesbarer Vertrag: `inventory/audiozentrale-ipad-pwa.v1.json`,
gebunden an `schemas/audiozentrale-ipad-pwa.v1.schema.json`. Der hashgebundene
Produktplan (`docs/plans/audiozentrale-product-v2.md`) bleibt unverändert.

## Was diese Stufe *nicht* belegt

`physical_acceptance` ist im Vertrag vollständig `false`:

| Feld | Wert |
| --- | --- |
| `ipad_installation_verified` | `false` |
| `ipad_standalone_launch_verified` | `false` |
| `offline_app_shell_verified` | `false` |
| `remote_bridge_verified` | `false` |
| `local_audio_hardware_verified` | `false` |
| `local_midi_hardware_verified` | `false` |

Es wurde kein iPad angefasst, keine Fernstrecke aufgebaut und kein Audio- oder
MIDI-Gerät geöffnet. Die Stufe ist eine Spezifikation mit Tests, kein
physischer Beleg.

## Installationsmetadaten

* `ui/manifest.webmanifest` (`/manifest.webmanifest`,
  `application/manifest+json`): `id`/`start_url`/`scope` = `/`,
  `display: standalone`, Theme `#0d1414`, Hintergrund `#0b1111`.
* Apple-Metadaten in `ui/index.html`: `apple-mobile-web-app-capable`,
  `apple-mobile-web-app-title`, `apple-mobile-web-app-status-bar-style`
  (`black-translucent`) und `apple-touch-icon` mit 180 px.
* Symbole: `ui/icon-180.png`, `ui/icon-192.png`, `ui/icon-512.png`
  (PNG, deckend, Marke im maskierbaren Sicherheitsbereich). Ihre SHA-256-Werte
  stehen im Vertrag.
* `viewport-fit=cover` plus `env(safe-area-inset-*)`-Polster in `styles.css`.
  Weil `black-translucent` den Inhalt unter die Statusleiste zieht, tragen der
  Seitenstreifen und die Kopfleiste den oberen sicheren Bereich.
* `@media (pointer: coarse)` hebt Schaltflächen, Auswahlfelder,
  Navigationsziele, Schalter und den Dialogschließer auf mindestens 44 px.

## Zwei Laufzeitmodi

Die Wahl liegt unter **System → Betriebsmodus**, wird in `localStorage`
(`audio-ui-runtime-mode`) gehalten und steuert kein Gerät. Vorgabe bleibt
`remote-audiozentrale`, damit sich der Desktopbetrieb nicht ändert.

### `remote-audiozentrale`

Das Heim-PC-Backend ist autoritativ für Zustand, Telemetrie, Profile und
Deployment. **Der aktuelle Control-Dienst ist kein Ferntransport.**
`AudioControlHTTPServer` bindet ausschließlich `127.0.0.1`, `verify_request`
lässt nur Loopback-Clients zu und der Handler weist nichtlokale `Host`- und
`Origin`-Kopfzeilen ab. Diese Grenze bleibt bestehen.

Ein echter iPad-Fernzugriff braucht deshalb eine **separat zu belegende,
authentifizierte und gesicherte Fern-Oberfläche**. Sie ist nicht Teil dieser
Stufe und im Vertrag als `remote_bridge_proven: false` festgehalten.

### `local-device`

Nur Browserfähigkeiten. Kein Heim-PC-Backend, keine `/api/`-Anfrage, kein
Telemetrie-Polling, kein automatisches Aktualisieren. Backendgebundene Flächen
werden **ausgeblendet** statt veraltet weitergezeigt; stattdessen erscheint ein
ausdrücklicher Hinweis. Dieser Modus hat **keine native Autorität über MOTU,
ALSA, PipeWire oder Roland**.

## Fähigkeitserkennung: fail-closed, ohne Kennungsauswertung

Erkannt werden ausschließlich Schnittstellen — sicherer Kontext, Service
Worker, Web Audio, `getUserMedia`, Web MIDI — und, sofern der Browser sie
exponiert, die Permissions-Policy. Regeln:

* Keine Auswertung von `navigator.userAgent`, `platform` oder `vendor`.
* `getUserMedia` und `requestMIDIAccess` werden **nie** automatisch aufgerufen;
  es wird nur auf ihr Vorhandensein geprüft. Es wird auch kein `AudioContext`
  konstruiert.
* Eine fehlgeschlagene Erkennung gilt als `unknown`, niemals als vorhanden.
* Die Oberfläche trennt sichtbar „Schnittstelle vorhanden“ von „Erlaubnis,
  Gerät und Hardware belegt“. Letzteres ist in dieser Stufe immer unbelegt.
* Die `Permissions-Policy` des Dienstes verweigert das Mikrofon weiterhin
  (`microphone=()`). Diese Stufe lockert sie nicht.

## Service Worker

`ui/sw.js` wird unter `/sw.js` mit Scope `/` ausgeliefert und **nur in sicheren
Kontexten** registriert (`http://127.0.0.1` gilt als sicher).

* Gecacht wird ausschließlich eine feste, statische App-Shell:
  `/`, `/index.html`, `/app.js`, `/whale-lesson.js`, `/styles.css`,
  `/manifest.webmanifest` und die drei Symbole. **Keine WAV-Datei.**
* Strategie: Netzwerk zuerst, Cache nur als Offline-Rückfall. Eine neue
  App-Shell kann so nie dauerhaft von einer alten Kopie verdeckt werden.
* Gleichherkunft `/api/` ist **strikt network-only**: kein Cache-Zugriff, kein
  Rückfall, kein Replay, keine Queue, kein Background Sync, kein stale state.
* Nicht-GET-Anfragen, Fremdherkunft und alles außerhalb der App-Shell laufen
  unverändert über das normale Netzwerkverhalten des Browsers.
* Es gibt keine `sync`-, `periodicsync`-, `push`- oder `backgroundfetch`-
  Behandlung und keinen nachrichtengesteuerten Wiederholungspfad.

### Umgang mit veralteter Kontrolle

`install` ruft `skipWaiting()`. `activate` löscht ausschließlich veraltete Caches
mit dem eigenen Präfix `audiozentrale-app-shell-` und lässt alle fremden
Same-Origin-Caches unangetastet; anschließend ruft es `clients.claim()`. Damit
kann ein alter Audiozentrale-Worker abgelöst werden, ohne Cache-Autorität über
andere Anwendungen zu beanspruchen.
Wechselt der Controller in einer bereits laufenden Ansicht, meldet die
Oberfläche das und **empfiehlt** ein Neuladen — es wird keines erzwungen, um
Reload-Schleifen zu vermeiden. Die Erstregistrierung löst diese Meldung nicht
aus.

## Sperre gegen Backendanfragen im lokalen Modus

Zwei Ebenen, bewusst redundant:

1. `fetchJson` und alle Aufrufstellen (`refreshSnapshot`, `loadReplay`,
   `requestTelemetry`, `telemetryPollTick`, `scheduleTelemetryPolling`,
   `scheduleAutoRefresh`, `autoRefreshTick`, `openProfilePlan`) prüfen
   `backendAllowed()`.
2. `installBackendFetchGuard()` umhüllt beim Start `window.fetch` und weist
   jede gleichherkünftige `/api/`-Anfrage im Modus `local-device` lokal ab —
   auch aus Code, der `fetchJson` nicht benutzt. Ein unlesbares Ziel gilt als
   gesperrt.

## Änderungen am Control-Dienst

Eng gehalten:

* `STATIC_FILES` erhält `/sw.js`, `/manifest.webmanifest` und die drei Symbole
  mit passenden Inhaltstypen. Die Allowlist und `read_static_file` bleiben
  sonst unverändert.
* CSP: `worker-src 'none'` → `worker-src 'self'` plus explizites
  `manifest-src 'self'`. Alle übrigen Direktiven bleiben unverändert eng.
* Unverändert: Loopback-Bindung, `verify_request`, Host- und Origin-Gates,
  `Permissions-Policy` mit `microphone=()`, `Cache-Control: no-store` für
  API-Antworten.

`scripts/audio_control_deploy.py` bindet Worker, Manifest, alle drei Symbole sowie
PWA-Vertrag und -Schema als release-kritische Dateien. Der Dienst-Readback prüft
zusätzlich die ausgelieferten Bytes und Inhaltstypen von Worker, Manifest und
Symbolen gegen genau diesen Release. `validate_release` führt die fokussierten
PWA-Vertragstests aus und prüft die Worker-Syntax mit Node, sofern Node
vorhanden ist. Ein Release kann die PWA-Fläche daher nicht stillschweigend
auslassen oder mit abweichenden statischen Bytes ausliefern.

## Offene Blockaden

* **HTTPS-Fernstrecke.** Ohne authentifizierte, gesicherte Fern-Oberfläche ist
  `remote-audiozentrale` vom iPad aus nicht erreichbar.
* **Physisches iPad.** Installierbarkeit, Standalone-Start und Offline-Verhalten
  sind nur spezifiziert und getestet, nicht auf Gerät belegt.
* **Audio- und MIDI-Hardware.** Web Audio und Web MIDI bleiben unbelegte
  Schnittstellen; native Autorität über MOTU, ALSA, PipeWire und Roland
  entsteht hier nicht.
