# Live-Telemetrie v1 – passiv, begrenzt, absturzisoliert

Dieses Dokument beschreibt die passive Live-Telemetrie der lokalen
Audiozentrale: die exakte Sicherheitsgrenze, die Ströme und ihr Schema, den
Endpunkt, die Belegläufe über eine und acht Stunden, die Interpretation der
Belege, die Grenzen und den Rollback.

Beteiligte Dateien:

- `scripts/audio_live_telemetry.py` – abhängigkeitsfreier Telemetriekern,
- `scripts/audio_telemetry_soak.py` – begrenzte Soak- und Lastprüfung,
- `scripts/audio_control.py` – read-only Endpunkt und Dienstlebenszyklus,
- `ui/index.html`, `ui/app.js`, `ui/styles.css` – kompaktes Telemetriepanel.

## 1. Sicherheitsgrenze (exakt)

Die Telemetrie **beobachtet ausschließlich**. Sie identifiziert beobachtete
Knoten und Verbindungen, verändert aber nichts.

Erlaubt ist genau:

| erlaubte Aktion | Werkzeug |
| --- | --- |
| Graph- und Geräteobjekte lesen | `pw-dump` |
| XRun-Zähler lesen | `pw-top -b -n 1` |
| Systemlast lesen | `/proc/loadavg` |
| eigene Prozess-CPU-Zeit lesen | `/proc/self/stat` |
| ALSA-Sequencer-Clients und -Ports lesen | `/proc/asound/seq/clients` |
| Rawmidi-Bytezähler lesen | `/proc/asound/card*/midi*` |
| Pegel lesen, falls extern bereitgestellt | Datei aus `AUDIO_TELEMETRY_LEVEL_SOURCE` |

Ausdrücklich verboten und im Code hart abgewiesen:

- Standardgeräte, Routen, Profile, Lautstärken oder Links ändern
  (`wpctl set-*`, `pactl set-*`, `pw-cli set-param`, `pw-link`, `pw-metadata`),
- einen Aufnahme- oder Wiedergabestrom öffnen (`pw-record`, `pw-cat`, `arecord`),
- eine ALSA-Sequencer-Subskription anlegen. **`aseqdump` wird bewusst nicht
  verwendet**: es abonniert einen Port und verändert damit den beobachteten
  MIDI-Graphen. Die MIDI-Aktivität kommt deshalb nur aus `/proc`.
- eine Shell starten. `shell=True` existiert im Kern nicht; jedes Kommando läuft
  als Argumentvektor mit eigener Prozessgruppe, Zeit- und Ausgabegrenze.

Die Allowlist `PASSIVE_COMMANDS` ist die einzige Stelle, an der externe
Programme entstehen. `assert_passive_argv()` weist alles andere ab, auch wenn
ein verbotenes Verb in einem sonst erlaubten Vektor auftaucht.

Die Beobachtung ist vollständig reversibel: sie hinterlässt keinen Zustand.
Das Beenden der Telemetrie ist ein reiner Threadstopp.

## 2. Ströme und Schema

Sechs Ströme, jeder mit eigener Sequenz, Frische, Verfügbarkeit, Fehlerlage und
eigenem begrenztem Puffer:

| Strom-ID | Inhalt | Quelle | Intervall | Stale ab |
| --- | --- | --- | --- | --- |
| `device-graph` | Knoten-, Link- und Gerätezahl, beobachtete Knoten und Links | `pw-dump` | 2 s | 8000 ms |
| `audio-levels` | Peak und RMS in dBFS | externe Pegelquelle | 1 s | 3000 ms |
| `midi-activity` | Sequencer-Clients, Ports, Rawmidi-Bytes und Delta | `/proc/asound` | 1 s | 6000 ms |
| `transport` | `running`, `idle` oder `unknown` | abgeleitet aus `device-graph` | 1 s | 6000 ms |
| `cpu-load` | Systemlast, CPU-Sekunden und -Prozent des Dienstes | `/proc` | 2 s | 8000 ms |
| `xruns` | Gesamtzähler, Delta, Zähler-Resets, pro Knoten | `pw-top -b -n 1` | 5 s | 20000 ms |

Jeder Stromabschnitt im Snapshot hat dieselbe Form:

```json
{
  "id": "xruns",
  "label": "XRuns",
  "unit": null,
  "availability": "live",
  "lossy": true,
  "sequence": 42,
  "published_total": 42,
  "dropped_total": 10,
  "rejected_total": 0,
  "buffer_capacity": 32,
  "buffer_depth": 32,
  "stale_after_ms": 20000,
  "age_ms": 1200,
  "updated_at": "2026-08-04T13:31:27.181766+00:00",
  "error": null,
  "error_at": null,
  "error_total": 0,
  "consecutive_error_count": 0,
  "collector": {
    "name": "pw-top-xruns",
    "interval_ms": 5000,
    "running": true,
    "sample_attempts": 42,
    "restart_count": 0
  },
  "value": {"total": 3, "delta": 0, "counter_reset_count": 0, "per_node": [], "source": "pw-top -b -n 1"}
}
```

Bedeutung der Felder:

- **`sequence`** steigt streng monoton und ausschließlich bei einer erfolgreich
  veröffentlichten Beobachtung. Fehler verändern sie nie.
- **`availability`** ist `starting` (noch keine Beobachtung, kein Fehler),
  `live` (Alter innerhalb der Stale-Grenze), `stale` (Alter überschritten oder
  Sammler gestoppt) oder `unavailable` (nie beobachtet, Fehler liegt vor).
- **`error`** ist orthogonal zur Verfügbarkeit: ein Strom kann `live` sein und
  trotzdem einen historischen Fehlerzähler tragen.
- **`dropped_total`** zählt verdrängte Telemetriesamples. Telemetrie darf
  verloren gehen; der Zähler macht es sichtbar.
- **`rejected_total`** zählt Samples, die die Payload-Grenze von 16 KiB oder den
  JSON-Vertrag verletzt haben.

Der Snapshotkopf enthält zusätzlich `safety` (die Grenze aus Abschnitt 1),
`summary` (Zähler über alle Ströme) und `control_channel`.

## 3. Kommandos teilen keine verlustbehaftete Warteschlange

Telemetrie und Kommandos sind getrennt:

- Telemetrie läuft über `TelemetryStream` – begrenzter Ringpuffer, verdrängt
  alte Samples, zählt jeden Verlust (`lossy: true`).
- Kommandos und Zustandsübergänge laufen über `ControlChannel` – begrenzt, aber
  **verlustfrei** (`lossless: true`, `dropped_total: 0`,
  `shares_telemetry_queue: false`). Ist der Kanal voll, wird die **neue**
  Einreichung mit `ControlChannelFull` ausdrücklich abgewiesen und in
  `rejected_total` gezählt. Bereits angenommene Kommandos werden nie verworfen.

Der Control-Dienst nutzt diesen Kanal für `service-start` und `service-stop`.

## 4. Endpunkt und Dienstlebenszyklus

`GET /api/v1/telemetry`

- read-only, ohne Aktionstoken, ohne Query (`?` ⇒ `400 invalid_query`),
- nur mit lokalem `Host` erreichbar, gleiche Sicherheitsheader wie die übrigen
  Endpunkte,
- startet keinen Subprozess: er liest nur den bereits gesammelten Zustand,
- liefert bei fehlendem Telemetriekern `503 telemetry_unavailable`,
- liefert bei gestoppten Sammlern `200` mit `running: false` und ausdrücklich
  veralteten Strömen statt eines Fehlers.

Die Sammler starten in `serve()` nach der Vertragsprüfung und werden im
`finally`-Zweig deterministisch gestoppt. Ein Fehler beim Start der Telemetrie
macht den Dienst nicht unbrauchbar: er läuft mit `live_telemetry: unavailable`
weiter. Die bestehenden Endpunkte, Limits und Aktionsregeln bleiben unverändert.

## 5. UI

Das Panel `Live-Telemetrie` in *Jetzt* zeigt pro Strom eine kompakte Karte mit
Beschriftung, Zustandschip (`live`, `veraltet`, `startet`, `nicht verfügbar`),
Wert, Sequenz, Alter, Verlustzähler und Fehlertext. Der Zustand steht immer als
Text da; Farbe ist nie die einzige Information.

Die UI pollt alle zwei Sekunden getrennt vom Zustandssnapshot. Ein
Telemetriefehler setzt nur den Panel-Text und berührt weder die globale
Meldung, den Aktualisierungsknopf noch den Snapshotzyklus.

## 6. Belegläufe

### Schnell und deterministisch (Sekunden, für Tests und CI)

```
just telemetry-soak-fast
python3 scripts/audio_telemetry_soak.py --mode synthetic --iterations 2000 --load-factor 4 --report /tmp/audio-telemetry-soak-fast.v1.json
```

Beweist mit synthetischen Sammlern in Sekunden:

- Puffergrenze (`queue-depth-bounded`),
- Verlustbuchhaltung `veröffentlicht = behalten + verworfen`
  (`drop-accounting-consistent`),
- Verlustfreiheit und Trennung des Kommandokanals,
- Kollektorisolation synchron und unter Threads,
- deterministisches Herunterfahren ohne Join-Timeout.

### Echte Einstunden-Lastmessung

```
just telemetry-soak-1h
python3 scripts/audio_telemetry_soak.py --mode live --duration-seconds 3600 --sample-interval-seconds 0.25 --load-factor 8 --report /tmp/audio-telemetry-soak-1h.v1.json
```

Der Lastfaktor bedeutet hier acht vollständige `hub.snapshot()`-Lesevorgänge pro
Beobachtungszyklus. Bei 0,25 s Zyklusabstand sind das nominell 32 Snapshot-Lesevorgänge
pro Sekunde. Der Bericht führt `load_factor`, `snapshot_reads`,
`snapshot_reads_per_second` und die Prüfung `snapshot-load-exercised`. Damit wird die
Telemetrieprojektion deutlich über der UI-Normallast beansprucht, ohne einen
Audio-, Routing- oder Steuerbefehl auszuführen.

### Echte Achtstundenmessung

```
just telemetry-soak-8h
python3 scripts/audio_telemetry_soak.py --mode live --duration-seconds 28800 --sample-interval-seconds 30 --load-factor 1 --report /tmp/audio-telemetry-soak-8h.v1.json
```

Zusammenfassung eines vorhandenen Berichts:

```
just telemetry-soak-summary /tmp/audio-telemetry-soak-8h.v1.json
```

## 7. Belege lesen

| Feld | Bedeutung |
| --- | --- |
| `evidence_class` | `synthetic-accelerated` = beschleunigte Simulation, `live-observed` = echte passive Beobachtung. |
| `live_proof` | Nur `true`, wenn im Lauf mindestens ein Strom tatsächlich `live` war. |
| `live_proof_reason` | Klartext, warum ein Lauf etwas beweist oder eben nicht. |
| `queue_bounds.max_buffer_depth` | Muss `<= stream_capacity` sein. |
| `queue_bounds.dropped_total` | Erwartete, gezählte Telemetrieverluste unter Last. |
| `control_channel.dropped_total` | Muss immer `0` sein. |
| `load_factor` / `snapshot_reads` | Angeforderte Snapshot-Leselast und tatsächlich ausgeführte Lesevorgänge. |
| `memory.growth_kib` | RSS-Zuwachs über den Lauf. |
| `memory.retention` | Harte interne Stichprobengrenze und Kompaktierungsmethode der Harness. |
| `memory.growth_per_hour_kib` | Nur bei Läufen ab 60 s gesetzt, sonst `null`. |
| `memory.trend` | `flat`, `rising` oder `not-extrapolated`. |
| `cpu.process_cpu_percent` | Eigener CPU-Anteil über die Laufzeit. |
| `xruns.available` | `false`, wenn kein echter Zähler lesbar war. |
| `xruns.delta` | XRun-Zuwachs über den Lauf; `null`, wenn nicht messbar. |
| `checks[]` | `pass`, `fail` oder `skipped`, jeweils mit Begründung. |
| `status` | `fail`, sobald eine Prüfung fehlschlägt; der Exitcode ist dann 1. |

Ehrlichkeitsregeln, die die Harness einhält:

- Der synthetische Modus setzt `live_proof: false` und markiert seine
  XRun-Zahlen als `authority: synthetic`.
- Ein Livelauf ohne lesbaren XRun-Zähler setzt `xruns.available: false`,
  `delta: null` und markiert `xrun-delta` als `skipped` statt eine Null zu
  behaupten.
- Läufe unter 60 s extrapolieren keinen Stundentrend.
- Befehle und Zieldefinitionen sind kein Laufbeleg. Ohne erzeugten Bericht ist
  weder die Einstundenlast noch der Achtstunden-Soak ausgeführt oder bestanden.

## 8. Grenzen

- **Peak und RMS sind ohne externe Quelle nicht beobachtbar.** Ein echter
  Pegelwert bräuchte einen Signalabgriff, also einen zusätzlichen Link – das
  verletzt die passive Grenze. Ohne `AUDIO_TELEMETRY_LEVEL_SOURCE` bleibt der
  Strom ausdrücklich `unavailable` mit Begründung, statt Werte zu erfinden.
- `transport` ist aus dem Graphen abgeleitet (`running_node_count > 0`), nicht
  aus einem globalen Transportobjekt; PipeWire hat keines.
- XRun-Zähler stammen aus einem Batch-Snapshot von `pw-top`. Fehlt das Werkzeug
  oder die Spalte, wird der Strom zum Fehler – nie zum Dienstausfall.
- `pw-dump`-Ausgaben werden auf 2 MB und 6 s begrenzt; beobachtete Knoten und
  Links werden auf je 32 Einträge gekürzt (`truncated: true`).
- Die Telemetrie liest den *beobachteten* Zustand. Sie ist keine physische
  Wahrheit über Kabel, Phantomspeisung oder Reglerstellungen.
- Der Speicherbedarf des Kerns ist hart begrenzt: 6 Ströme × 32 Samples ×
  16 KiB Payload. Die Harness hält zusätzlich höchstens 256 RSS-Stichproben im
  Speicher und verdichtet sie periodisch unter Erhalt der ersten und neuesten
  Messung; im Bericht erscheinen höchstens 64.
- Die Harness startet keinen dauerhaften Dienst. Ein Achtstundenlauf ist ein
  Vordergrundprozess und endet mit seinem Zeitlimit.

## 9. Rollback

Die Telemetrie ist additiv und hinterlässt keinen Systemzustand.

1. **Zur Laufzeit abschalten:** Control-Dienst beenden (`just control-stop`).
   Mit ihm endet die Telemetrie deterministisch; `stop()` joint alle
   Sammlerthreads. Es bleibt kein Prozess und keine Datei zurück.
2. **Pegelquelle entfernen:** `AUDIO_TELEMETRY_LEVEL_SOURCE` löschen. Der Strom
   fällt auf `unavailable` zurück, sonst ändert sich nichts.
3. **Panel entfernen:** Das Panel `id="live-telemetry"` aus `ui/index.html`
   entfernen. Dann schlägt `python3 scripts/audio_control.py check` bewusst an,
   solange die Bindung in `validate_repository_contract()` nicht mit entfernt
   wird.
4. **Vollständig zurücknehmen:** Die Dateien `scripts/audio_live_telemetry.py`
   und `scripts/audio_telemetry_soak.py` löschen, den Endpunkt
   `/api/v1/telemetry`, den `LIVE_TELEMETRY`-Import, den Lebenszyklusaufruf in
   `serve()` und die Panelbindung aus `scripts/audio_control.py` entfernen sowie
   die Telemetrieteile aus `ui/`. Es sind keine Migrationen, Zustandsdateien
   oder Systemänderungen rückabzuwickeln.

Zu keinem Zeitpunkt verändert ein Rollback Standardgeräte, Routen, Profile,
Lautstärken oder Links – weil die Telemetrie sie nie verändert hat.
