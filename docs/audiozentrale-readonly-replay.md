# Audiozentrale v2 – read-only Produktoberfläche und Replay

## Produktorte

Die Browseransicht und eine spätere App-Shell verwenden dasselbe statische Frontendpaket aus `ui/index.html`, `ui/app.js` und `ui/styles.css`. Es besitzt genau vier stabile Orte:

- **Jetzt**: autoritativer Readback des aktuellen Arbeitsstands und der aktiven Signalbahnen,
- **Setups**: Hör-, Spiel- und Aufnahmevorlagen sowie der beobachtete Instrumentzustand,
- **Bibliothek**: unveränderliche Takes, Klangzustände und Telemetrie-Replay,
- **System**: Geräte, Signalweg, Doctor, Deployment und lokale Ansichtseinstellungen.

Alte Hashrouten werden ausschließlich als kompatible Weiterleitung gelesen. Sie bilden keine zweite Navigationswahrheit.

## Darstellungstiefen

Jede Produktbahn beginnt kompakt. `Erweitern` öffnet nur die zugehörigen Details inline. `Fokus` verwendet den einen globalen Dialog und zeigt ausschließlich eine textuelle read-only Projektion; es werden keine aktiven Controls, IDs oder Audioelemente geklont. Tastatur, Touch und `prefers-reduced-motion` bleiben unterstützt.

## Wahrheitsebenen

Die vier Ebenen sind an jedem Ort sichtbar und textlich unterscheidbar:

1. **Beobachtet** – aktueller Backend- oder Doctor-Readback,
2. **Konfiguriert** – Sollzustand und Produktvertrag,
3. **Physisch offen** – noch nicht vor Ort belegte Hardwaretatsachen,
4. **Ausführbar** – in T020 ausdrücklich read-only; keine Audioaktion wird aus dem Frontend gesendet.

Farbe ist nie die einzige Zustandsinformation.

## Replay-Vertrag

`inventory/audiozentrale-telemetry-replay.v1.json` enthält sechs deterministische Szenarien mit insgesamt 48 Frames:

- Normalbetrieb,
- Clipping,
- XRun,
- Geräteverlust,
- veraltete Telemetrie,
- Recovery.

Der Katalog bindet das Audiozentrale-Produktmodell und `schemas/audiozentrale-telemetry-replay.v1.schema.json` per SHA-256. Der Validator liest Katalog, Schema und Produktmodell jeweils aus einem einzigen begrenzten, symlinkfreien Dateisnapshot. Doppelte JSON-Schlüssel, nichtendliche Zahlen, Drift während des Lesens, nichtmonotone XRun-Zähler und unplausible Ereignisse blockieren.

Der GET-Endpunkt `/api/v1/replay` liefert:

- `authority: synthetic-replay`,
- `authoritative: false`,
- Katalog-, Schema- und Produktmodellhash.

Er akzeptiert keine Query, startet keinen Subprozess und verändert weder Snapshot noch Gerät, Route, Profil, Instrument oder Aufnahme. Das Replay läuft nur im Browser über einen lokalen Timer.

## Wirkungsgrenze

T020 entfernt alle `/api/v1/actions/*`-Aufrufe aus dem gemeinsamen Frontend. Der bestehende Backend-Aktionsvertrag bleibt für getrennte spätere Aufgaben erhalten, wird von dieser Produktoberfläche aber nicht genutzt. Passive echte Telemetrie folgt in T021; Aufnahme folgt in T022.
