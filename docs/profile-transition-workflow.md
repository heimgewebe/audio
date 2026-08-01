# Profiltransition: `desktop-mixed`

## Zweck

`audio-transition` ist der erste wirkende Profilvertrag des Repositories. Er ist
absichtlich auf `desktop-mixed` begrenzt und darf nur drei Zustandsfelder
verändern:

- exakte PipeWire/PulseAudio-Standardsenke auf den eindeutig erkannten MOTU-M2-Pfad;
- `clock.force-rate` auf 48.000 Hz;
- `clock.force-quantum` auf 1.024 Frames.

Damit kann ein abweichender Desktop-, Mixed- oder Recording-Vorzustand sicher
in den gemeinsamen Desktopzustand zurückgeführt werden. Die Recording-Profile
selbst bleiben weiterhin an ihre eigenen Plan-, Gate- und Session-Verträge
gebunden und werden von diesem Werkzeug nicht aktiviert.

**Für Dummies:** `audio-plan` prüft die Profilreife. `diff` ist der reine
Dry-Run und schaut nur nach. `apply` darf nur genau den zuvor
angezeigten Plan ausführen. Vor jeder Änderung wird ein privates Rückfahrtticket
gespeichert. Nach jeder Änderung wird der Rechner erneut gefragt, ob der
beabsichtigte Zustand wirklich erreicht wurde.

## Ablauf

```bash
./scripts/audio-plan desktop-mixed
./scripts/audio-transition diff desktop-mixed
./scripts/audio-transition apply desktop-mixed --plan-sha256 <SHA-256-aus-diff>
./scripts/audio-transition status
./scripts/audio-transition rollback --operation-id <operation-id>
```

Ein zweites `apply` mit demselben Planhash und bereits erreichtem Zielzustand
ist wirkungsfrei. Hat sich der Livezustand zwischen `diff` und `apply` geändert,
wird die Ausführung mit `plan-changed` abgewiesen.

`audio-plan`, `diff` und `status` ändern weder Standardsenke noch PipeWire-
Metadaten. Nur `apply`, `rollback` und ein tatsächlich erforderliches `recover`
dürfen die drei gebundenen Felder verändern.

## Kontrollierte Fehlerzustände

- `profile-readiness-blocked`: Hardware, physische Fakten oder erforderliche
  Gates reichen nicht aus;
- `plan-changed`: Profilvertrag oder Livezustand weicht vom bestätigten Hash ab;
- `transition-busy`: das exklusive Lock blieb länger als zwei Sekunden belegt;
- `metadata-invalid` beziehungsweise `sink-inventory-invalid`: der Live-Readback
  ist mehrdeutig oder unzulässig;
- `apply-failed-rolled-back`: Apply scheiterte und die gebundenen Felder wurden
  wiederhergestellt;
- `rollback-drift-conflict`: fremder Drift wurde erkannt und bewusst nicht
  überschrieben; `status` verlangt Aufmerksamkeit und Recovery.

## Recovery

```bash
./scripts/audio-transition recover
```

`recover` behandelt einen unterbrochenen Lauf nach frischem Readback:

- Ist der vollständig gebundene Zielzustand bereits erreicht, wird der Lauf als
  angewendet abgeschlossen und jeder geplante Schritt für ein späteres
  vollständiges Rollback gebunden.
- Bei einem Teilzustand werden nur die betroffenen, nachweislich noch zum
  Vorgang gehörenden Felder zurückgesetzt.
- Hat ein anderer Prozess eines dieser Felder auf einen dritten Wert geändert,
  bleibt die Rücksetzung fail-closed blockiert. Dieser Drift wird nicht
  überschrieben.

## Private Zustandsdaten

Die Journale liegen standardmäßig unter
`~/.local/state/audio/profile-transitions-v1/` mit Verzeichnisrechten `0700` und
Dateirechten `0600`. Sie enthalten die exakten lokalen PipeWire-Namen, weil nur
so ein belastbares Rollback möglich ist. Öffentliche JSON-Ausgaben projizieren
Geräte dagegen auf stabile Namen wie `motu-m2` und geben keine USB-Identität aus.

## Sicherheitsgrenzen

- keine Shellausführung; nur feste Argumentvektoren für die gebundenen
  `/usr/bin/pactl`- und `/usr/bin/pw-metadata`-Programme;
- exklusives lokales Transitions-Lock mit begrenzter Wartezeit;
- atomare, hashgebundene Journale;
- feste Kommando-, bereits beim Einlesen durchgesetzte Ausgabe- und
  Readback-Zeitgrenzen;
- keine Mutation ohne exakten, frisch neu berechneten Planhash;
- kein Überschreiben fremden Drifts beim Rollback;
- keine Freigabe weiterer Profile durch diesen Vertrag.

Der Vertrag belegt weder subjektive Klangqualität noch Bitgenauigkeit,
sicheren Hörpegel oder Aufnahmebereitschaft. Die Audiozentrale bietet deshalb
noch keinen Profil-Apply-Knopf; die UI-Integration folgt erst nach separater
Browser-, Recovery- und Laborabnahme.

## Aktueller Heim-PC-Readback

Am 1. August 2026 ergaben Doctor und realer `diff`, beide ausschließlich
lesend:

- MOTU M2 und Roland FP-30X: nicht im aktuellen Livegraph beobachtet;
- Standardsenke: `spdif`;
- Force-Rate: 48.000 Hz;
- Force-Quantum: 1.024 Frames;
- Ergebnis: `profile-readiness-blocked` mit Blocker `motu_m2`;
- Planhash und Apply: nicht freigegeben.

Der fehlende Ziel-Sink wurde nicht ersetzt oder simuliert. Es wurde kein
Live-Audiozustand verändert.
