# Live-Telemetrie v1 – begrenzt und absturzisoliert

Dieses Dokument beschreibt die Live-Telemetrie der lokalen Audiozentrale. Der
Telemetriekern bleibt passiv; für echte Peak-/RMS-Werte läuft davor ein klar
getrennter **aktiver** PipeWire-Beobachter. Beschrieben werden die exakten
Sicherheitsgrenzen, die Ströme und ihr Schema, der Endpunkt, die Belegläufe über
eine und acht Stunden, die Interpretation der Belege, die Grenzen und der
Rollback.

Beteiligte Dateien:

- `scripts/audio_live_telemetry.py` – abhängigkeitsfreier Telemetriekern,
- `scripts/audio_level_observer.py` – aktiver PipeWire-Pegelbeobachter,
- `scripts/audio_telemetry_soak.py` – begrenzte Soak- und Lastprüfung,
- `scripts/audio_control.py` – read-only Endpunkt und Dienstlebenszyklus,
- `ui/index.html`, `ui/app.js`, `ui/styles.css` – kompaktes Telemetriepanel.

## 1. Sicherheitsgrenze (exakt)

Der in `audio_live_telemetry.py` laufende Telemetriekern **beobachtet
ausschließlich**. Er identifiziert beobachtete Knoten und Verbindungen,
verändert aber nichts.

Erlaubt ist genau:

| erlaubte Aktion | Werkzeug |
| --- | --- |
| Graph- und Geräteobjekte lesen | `pw-dump` |
| XRun-Zähler lesen | `pw-top -b -n 1` |
| Systemlast lesen | `/proc/loadavg` |
| eigene Prozess-CPU-Zeit lesen | `/proc/self/stat` |
| ALSA-Sequencer-Clients und -Ports lesen | `/proc/asound/seq/clients` |
| Rawmidi-Bytezähler lesen | `/proc/asound/card*/midi*` |
| Pegel lesen, falls extern bereitgestellt | begrenzte Datei aus `AUDIO_TELEMETRY_LEVEL_SOURCE` |

Ausdrücklich verboten und im Code hart abgewiesen:

- Standardgeräte, Routen, Profile, Lautstärken oder Links ändern
  (`wpctl set-*`, `pactl set-*`, `pw-cli set-param`, `pw-link`, `pw-metadata`),
- einen Aufnahme- oder Wiedergabestrom öffnen (`pw-record`, `pw-cat`, `arecord`);
  der nachfolgend beschriebene separate Pegelbeobachter ist nicht Teil dieser
  passiven Grenze,
- eine ALSA-Sequencer-Subskription anlegen. **`aseqdump` wird bewusst nicht
  verwendet**: es abonniert einen Port und verändert damit den beobachteten
  MIDI-Graphen. Die MIDI-Aktivität kommt deshalb nur aus `/proc`.
- eine Shell starten. `shell=True` existiert im Kern nicht; jedes Kommando läuft
  als Argumentvektor mit eigener Prozessgruppe, Zeit- und Ausgabegrenze.

Die Allowlist `PASSIVE_COMMANDS` ist die einzige Stelle, an der externe
Programme entstehen. `assert_passive_argv()` weist alles andere ab, auch wenn
ein verbotenes Verb in einem sonst erlaubten Vektor auftaucht.

Die passive Kernbeobachtung ist vollständig reversibel: sie hinterlässt keinen
Zustand. Sie trägt die feste Identität `audio-control-telemetry-v1`;
`owned_nodes` und `owned_links` bleiben für diesen Kern leer. Das Beenden des
Kerns ist ein reiner Threadstopp. Sein Zeitbudget liegt oberhalb des längsten
passiven Subprozess-Timeouts einschließlich Kill-Grace.

### 1.1 Aktiver Pegelbeobachter

Peak und RMS benötigen echte Samples. Der separate Dienst
`audio-control-level-observer-v1.service` öffnet deshalb bewusst einen aktiven
nativen PipeWire-Capture-Stream. PipeWire/WirePlumber erzeugen für diesen Stream
einen flüchtigen Knoten und mindestens einen flüchtigen Link zur Quelle. Der
gesamte Pegelmesspfad ist damit **nicht passiv**. Der Observer ändert aber weder
Defaultquelle noch Profile, Lautstärken oder produktive Routen und hinterlegt
keine dauerhafte PipeWire-Konfiguration.

Der Stream verwendet **kein** `target=auto` mehr. Vor jedem Capture wird über
`pactl --format=json list sources` genau eine MOTU-M2-Quelle mit USB-Vendor/Product
`07fd:0008`, Serienbindung, Buspfad, 48 kHz, Stereo `s32le`, ungemutetem Zustand
und Unity-Source-Volume verlangt. Recorder und Pegelobserver benutzen für diese
Identität denselben wirkungsfreien Helper `scripts/motu_capture_identity.py`.
Nur der daraus gelesene exakte `node.name` wird intern an `pw-cat --target`
übergeben; der Rohname erscheint nicht im Telemetriesnapshot. Öffentlich wird
nur der kanonische Quellen-Hash plus `front-left,front-right` projiziert. Der
Observer greift nie direkt oder exklusiv auf ALSA zu und ändert insbesondere
keine Defaultquelle.

`pw-cat` liefert 48 kHz, Stereo `FL/FR` und `f32`. Der Observer berechnet pro
500-ms-Fenster Sample-Peak und RMS aus den tatsächlich empfangenen Samples. Bei
exakter digitaler Null wird der dokumentierte Darstellungsboden `-160 dBFS`
ausgegeben; Übersteuerungen werden bei `0 dBFS` begrenzt und zusätzlich über
`clipping` und `clipped_samples` kenntlich gemacht. Es werden weder Zufalls-,
Fallback- noch aus Graphdaten abgeleitete Pegel erzeugt.

Jedes Fenster ersetzt atomar eine private, auf 8 KiB begrenzte JSON-Datei unter
`$XDG_RUNTIME_DIR/audio-control-level-observer/levels.json`. Der Deployer trägt
diesen absoluten Pfad in `runtime.env` als `AUDIO_TELEMETRY_LEVEL_SOURCE` ein.
Der passive Dateicollector prüft Schema, Observeridentität, fortschreitende
Sequenz und Zeitstempel; eine unveränderte, mehr als drei Sekunden alte oder aus
der Zukunft datierte Beobachtung wird abgewiesen. `RuntimeDirectory=` entfernt
die Datei bei Dienstende. Fehlt das MOTU beim Boot oder nach Hotplug, bleibt der
Observer ohne Pegelbeleg aktiv und sucht begrenzt weiter; bei Wiederkehr bindet
er ausschließlich die erneut verifizierte Recorderquelle. Ein echter
`pw-cat`-Fehler bei weiterhin identischer Quelle bleibt dagegen ein Fehler, den
systemd neu startet. Während Quelle oder Capture fehlen, fällt das Panel auf
`stale`/`unavailable`, statt den letzten Wert als neu auszugeben.

Der Observer ist mit `Wants=` und `PartOf=` an den UI-Dienst gekoppelt, läuft
mit `Restart=on-failure`, 64 MiB Speichergrenze, 10 % CPU-Quota und 100 ms
PipeWire-Latenz. Der Deployer installiert beide Units revisionsgebunden und
startet die UI auch bei reparierter Observer-Unit neu. Der vorhandene
`level_analyzer.py` bleibt der Offline-WAV-Analysator; der vorhandene
`voice_capture_observer.py` erzeugt kurze gebundene Mess-WAVs. Beide sind
absichtlich keine wiederverwendbaren Dauer-Meter und würden unnötige Dateien
beziehungsweise einen zweiten Lifecycle einführen.

## 2. Ströme und Schema

Sechs Ströme, jeder mit eigener Sequenz, Frische, Verfügbarkeit, Fehlerlage und
eigenem begrenztem Puffer:

| Strom-ID | Inhalt | Quelle | Intervall | Stale ab |
| --- | --- | --- | --- | --- |
| `device-graph` | Knoten-, Link- und Gerätezahl, Inhaltsdigest sowie `baseline`/`none`/`changed`-Ereignis | `pw-dump` | 2 s | 8000 ms |
| `audio-levels` | Peak und RMS in dBFS, FL/FR-Kanalwerte und recordergebundener Quellen-Hash | exakter MOTU-Recorder-Capture, passiv aus JSON gelesen | 1 s | 3000 ms |
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
`summary` (Zähler über alle Ströme) und `control_channel`. Der Graphsammler
bildet aus Knoten, Links und Geräten einen stabilen SHA-256. Der erste gültige
Stand heißt `baseline`, unveränderte Folgestände `none`; nur eine echte
Inhaltsänderung erhöht `event_sequence` und liefert `changed`. Für den Digest
werden alle relevanten Graphobjekte berücksichtigt; nur die angezeigten
Detaillisten werden auf je 32 Einträge gekürzt. Änderungen außerhalb dieses
Ausschnitts bleiben dadurch als Graphereignis sichtbar. `midi-activity` zählt
`client_count` und
`port_count` über die gesamte gelesene Population. Nur die Detailliste
`clients` ist auf 32 Einträge begrenzt; `observed_client_count` und
`truncated: true` machen diesen Darstellungs-Ausschnitt ausdrücklich sichtbar.

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
`finally`-Zweig deterministisch gestoppt. Das optionale Telemetriemodul wird
erst beim Aufbau des Hubs geladen. Ein Syntax-, Import- oder Startfehler macht
den Control-Dienst daher nicht unbrauchbar: er läuft mit
`live_telemetry: unavailable` weiter. Der explizite Repositorycheck bleibt
streng und weist ein defektes Modul ab. Bei jedem neuen Sammlerlebenszyklus
wird die alte aktuelle Sampleprojektion invalidiert. Der begrenzte
Historienpuffer sowie Sequenz- und Gesamtzähler bleiben erhalten; bis zum ersten
neuen Sample steht der Strom wieder auf `starting` und liefert keinen alten
Wert als live. Die bestehenden Endpunkte, Limits und Aktionsregeln bleiben
unverändert.

## 5. UI

Das Panel `Live-Telemetrie` in *Jetzt* zeigt pro Strom eine kompakte Karte mit
Beschriftung, Zustandschip (`live`, `veraltet`, `startet`, `nicht verfügbar`),
Wert, Sequenz, Alter, Verlustzähler und Fehlertext. Der Zustand steht immer als
Text da; Farbe ist nie die einzige Information.

Die UI pollt alle zwei Sekunden getrennt vom Zustandssnapshot. Das Polling
erfolgt sequenziell: Erst nach Abschluss oder Abbruch einer Anfrage wird der nächste
Timer geplant; ein In-Flight-Promise verhindert auch bei Sichtbarkeitswechseln
parallele Readbacks. Im verborgenen Tab läuft kein Telemetrie-Timer, beim
Sichtbarwerden startet sofort ein Readback. Ein
Telemetriefehler setzt nur den Panel-Text und berührt weder die globale
Meldung, den Aktualisierungsknopf noch den Snapshotzyklus. Verspätete Antworten
dürfen einen neueren Stand nicht überschreiben: jede Anfrage besitzt eine
monotone Request-ID, nur die jüngste Antwort wird präsentiert.

Die drei Ebenen sind ausdrücklich getrennt:

- `/api/v1/snapshot` liefert `truth_stream.sequence`, Cache-Alter, Frische und
  Teilfehler der langsamen Systemwahrheit. Cache-Readbacks behalten die Sequenz
  und erhöhen nur das Alter; ein echter Refresh erhöht die Sequenz.
- `/api/v1/telemetry` liefert die unabhängigen Sequenzen, Frische- und
  Fehlerzustände jedes Telemetriestroms.
- Die UI führt zusätzlich `telemetryPresentationSequence` und die angenommene
  Request-ID. Diese Darstellungssequenz erteilt weder Audio- noch
  Systemwahrheitsautorität.

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
| `evidence_class` | `synthetic-accelerated` = beschleunigte Simulation, `live-observed` = echte Laufzeitbeobachtung; das allein behauptet keinen vollständig passiven Pegelpfad. |
| `live_proof` | Nur `true`, wenn die Laufzeit vollständig war, mindestens ein Strom `live` war, kein Sicherheitscheck fehlschlug und der globale XRun-Delta exakt null blieb. |
| `live_proof_reason` | Klartext, warum ein Lauf etwas beweist oder eben nicht. |
| `queue_bounds.max_buffer_depth` | Muss `<= stream_capacity` sein. |
| `queue_bounds.dropped_total` | Erwartete, gezählte Telemetrieverluste unter Last. |
| `control_channel.dropped_total` | Muss immer `0` sein. |
| `load_factor` / `snapshot_reads` | Angeforderte Snapshot-Leselast und tatsächlich ausgeführte Lesevorgänge. |
| `memory.growth_kib` | RSS-Zuwachs über den Lauf. |
| `memory.retention` | Harte interne Stichprobengrenze und Kompaktierungsmethode der Harness. |
| `memory.growth_per_hour_kib` | Nur bei Läufen ab 60 s gesetzt, sonst `null`. |
| `memory.trend` | `flat`, `rising` oder `not-extrapolated`. |
| `cpu.process_cpu_percent` | CPU-Anteil des Harness einschließlich beendeter Kindprozesse über die Laufzeit. |
| `xruns.available` | `false`, wenn kein echter Zähler lesbar war. |
| `xruns.delta` | XRun-Zuwachs über den Lauf; `null`, wenn nicht messbar. |
| `checks[]` | `pass`, `fail` oder `skipped`, jeweils mit Begründung. |
| `status` | `fail`, sobald eine Prüfung fehlschlägt; der Exitcode ist dann 1. |

Ehrlichkeitsregeln, die die Harness einhält:

- Der synthetische Modus setzt `live_proof: false` und markiert seine
  XRun-Zahlen als `authority: synthetic`.
- Ein Livelauf ohne lesbaren XRun-Zähler setzt `xruns.available: false`,
  `delta: null` und markiert `xrun-delta` als `skipped` statt eine Null zu
  behaupten. Ein positiver globaler Delta wird der Anwendung nicht automatisch
  zugerechnet, blockiert aber einen sauberen Beleg.
- Läufe unter 60 s extrapolieren keinen Stundentrend. Kurze Läufe begrenzen den
  absoluten RSS-Zuwachs auf 32 MiB; längere Läufe den beobachteten Trend auf
  64 MiB pro Stunde.
- Befehle und Zieldefinitionen sind kein Laufbeleg. Ohne erzeugten Bericht ist
  weder die Einstundenlast noch der Achtstunden-Soak ausgeführt oder bestanden.

## 8. Grenzen

- **Peak und RMS sind ohne aktive Samplequelle nicht beobachtbar.** Der
  ausgelieferte PipeWire-Observer stellt den erforderlichen flüchtigen Stream
  und Link bereit. Ohne laufenden Observer oder ohne
  `AUDIO_TELEMETRY_LEVEL_SOURCE` bleibt der Strom ausdrücklich `unavailable`
  mit Begründung, statt Werte zu erfinden.
- `transport` ist aus dem Graphen abgeleitet (`running_node_count > 0`), nicht
  aus einem globalen Transportobjekt; PipeWire hat keines.
- XRun-Zähler stammen aus einem Batch-Snapshot von `pw-top`. Der Parser bindet
  `ERR`, `ID`, `FORMAT` und `NAME` an den beobachteten Header und erhält Namen
  mit Leerzeichen. Fehlt das Werkzeug oder eine Pflichtspalte, wird der Strom
  zum Fehler – nie zum Dienstausfall.
- `pw-dump`-Ausgaben werden auf 2 MB und 6 s begrenzt; beobachtete Knoten und
  Links werden auf je 32 Einträge gekürzt (`truncated: true`). Der
  Telemetrie-Stopp wartet länger als dieses Subprozessbudget.
- Die Telemetrie liest den *beobachteten* Zustand. Sie ist keine physische
  Wahrheit über Kabel, Phantomspeisung oder Reglerstellungen.
- Der Speicherbedarf des Kerns ist hart begrenzt: 6 Ströme × 32 Samples ×
  16 KiB Payload. Die Harness hält zusätzlich höchstens 256 RSS-Stichproben im
  Speicher und verdichtet sie periodisch unter Erhalt der ersten und neuesten
  Messung; im Bericht erscheinen höchstens 64.
- Die Harness startet keinen dauerhaften Dienst. Ein Achtstundenlauf ist ein
  Vordergrundprozess und endet mit seinem Zeitlimit. Reportdateien werden mit
  `O_NOFOLLOW` geöffnet; Symlink-Ziele werden nicht überschrieben.
- Die vorhandenen Befehle definieren Belegläufe, führen sie aber nicht aus. Für
  T021 liegen repositoryseitige Kurztests vor; der echte Achtstunden-Soak und
  die echte Einstunden-Lastprobe bleiben bis zu ihren erzeugten Berichten
  ausdrücklich unbewiesen.

## 9. Rollback

Die Telemetrie ist additiv. Der Pegelbeobachter hinterlässt keinen dauerhaften
PipeWire-Zustand, erzeugt während seiner Laufzeit aber ehrlich sichtbar einen
eigenen Capture-Knoten und dessen Link.

1. **Zur Laufzeit abschalten:** `systemctl --user stop
   audio-control-ui-v1.service`. Durch `PartOf=` endet auch der Pegelbeobachter,
   PipeWire entfernt Stream und Link, und `RuntimeDirectory=` entfernt die
   Pegeldatei. Der UI-Kern joint zusätzlich alle Sammlerthreads.
2. **Nur Pegel abschalten:** `systemctl --user stop
   audio-control-level-observer-v1.service`. Der Strom fällt auf
   `stale`/`unavailable` zurück; die übrige UI bleibt verfügbar. Ein späterer
   UI-Neustart startet den per `Wants=` gekoppelten Observer wieder.
3. **Panel entfernen:** Das Panel `id="live-telemetry"` aus `ui/index.html`
   entfernen. Dann schlägt `python3 scripts/audio_control.py check` bewusst an,
   solange die Bindung in `validate_repository_contract()` nicht mit entfernt
   wird.
4. **Vollständig zurücknehmen:** Zusätzlich zum Pegelobserver und seiner Unit
   die Dateien `scripts/audio_live_telemetry.py` und
   `scripts/audio_telemetry_soak.py` löschen, den Endpunkt
   `/api/v1/telemetry`, den `LIVE_TELEMETRY`-Import, den Lebenszyklusaufruf in
   `serve()` und die Panelbindung aus `scripts/audio_control.py` entfernen sowie
   die Telemetrieteile aus `ui/`. Es sind keine Migrationen, Zustandsdateien
   oder Systemänderungen rückabzuwickeln.

Ein Rollback verändert keine Standardgeräte, Profile, Lautstärken oder
produktiven Routen. Er beendet lediglich den identifizierten Observerprozess;
PipeWire nimmt dessen eigenen flüchtigen Stream und Link zurück.
