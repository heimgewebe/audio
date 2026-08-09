# Gehärtete Aufnahmesitzungen

## Zweck

`scripts/audio-record` verwaltet vier explizite Aufnahmetypen mit demselben fail-closed Sitzungsprotokoll:

| Sitzungstyp | Quelle | Voraussetzung | Zielformat |
| --- | --- | --- | --- |
| `voice-recording` | RØDE NT1-A über die seriell und busgebundene MOTU-M2-Quelle | physische RØDE-/MOTU-Fakten und `voice-level-measurement` | 48 kHz, Stereo, `s32le`, WAV |
| `piano-vocal-performance` | dieselbe Gesangsquelle plus genau ein kernel-/USB-gebundener Roland-FP-30X-Sequencer-Port | Voice-Gates, eindeutige Roland-Quelle und gebundenes `arecordmidi` | Geschwister `name.wav`, `name.mid` und Commit-Beleg `name.take.json` |
| `roland-audio-recording` | eindeutige USB-Audioquelle des Roland FP-30X | gebundene Entscheidung `resampling-decision` | einmalige Umsetzung von 44,1 kHz auf 48 kHz, Stereo, `s32le`, WAV |
| `production-mix-recording` | exakt ein PipeWire-Quellknoten namens `audio-production-mix` | der Knoten muss bereits eindeutig und vertragskonform vorhanden sein | 48 kHz, Stereo, `s32le`, WAV |

Der zugehörige Quellknoten wird durch den getrennten, hashfreigegebenen Ablauf in [Verwalteter Produktions-Mixgraph](production-mix-graph.md) definiert. Recorder und Graph besitzen bewusst getrennte Plan-Hashes und Lebenszyklen.
Der Produktionsrecorder akzeptiert die Quelle nur, wenn der verwaltete Dienst, alle vier Kindprozesse und die aktuelle 48-kHz-Stereotopologie exakt bestätigt sind. Ein fremder oder lediglich namensgleicher Knoten bleibt blockiert.

Der Ablauf trennt **prüfen**, **freigeben**, **aufnehmen**, **stoppen** und **wiederherstellen**. Ein Plan ist read-only. Erst ein ausdrücklich bestätigter Plan-Hash darf eine Aufnahme starten.

## Gemeinsame Garantien

- Sitzungstyp, Profil, Quelle, Monitoringvertrag, Recorderprogramm und Prozessargumente sind im Plan gebunden.
- Nur eine verwaltete Aufnahme darf aktiv sein.
- Die Quellidentität muss beim Planen, Starten und unmittelbar vor dem Capture übereinstimmen.
- Roland wird über USB-Vendor/Product, Serienkennung, Buspfad, PipeWire-Knoten, Format, Rate, Kanalzahl, Mute und Lautstärke gebunden.
- Der Produktionspfad akzeptiert ausschließlich den eindeutigen Knoten `audio-production-mix`; er errät keine Monitorquelle und keinen wechselnden Upstream.
- Recorderprozesse verwenden typspezifische Client- und Streamnamen.
- Der Performance-Plan bindet `/usr/bin/arecordmidi`, den zur Planzeit tatsächlich aufgelösten Binary-Pfad und dessen auf dem aktuellen Host berechneten SHA-256 sowie die exakten Capture-Argumente `-p <client:port> -f 25 -t 40 <private-partial.mid>`. Vor dem Start wird diese Bindung erneut gelesen; jede Änderung schließt den Start.
- Der gebundene `arecordmidi`-Vertrag verwendet `-f 25` als SMPTE-25-fps und `-t 40` als 40 Ticks pro Frame. Fehlen Binary oder exakter Vertrag auf einem Host, bleibt der Plan geschlossen; es gibt keinen stillen Rückfall auf Event-Textparsing.
- Zieldateien werden niemals überschrieben.
- Eine unvollständige Datei bleibt bei Fehlern als private `.partial.wav` erhalten und wird nicht stillschweigend als fertig veröffentlicht.
- Stop und Recovery prüfen PID, Prozessstartzeit, ausführbare Datei, Kommandozeile und Prozessgruppe.
- Zustandsdateien sind privat (`0600`), Zustands- und Aufnahmeverzeichnisse privat (`0700`).

## Bewusste Grenzen

- Der Produktionsrecorder **erzeugt und verbindet den Mixbus nicht**. Fehlt `audio-production-mix` oder existiert er mehrfach, bleibt der Plan blockiert.
- Die im Produktionsvertrag genannten Upstream-Rollen `voice`, `roland` und `software-instrument` beschreiben den vorgesehenen Bus. Die Aufnahmesitzung beweist nicht, dass diese Rollen aktuell angeschlossen oder korrekt gepegelt sind.
- Der Roland-Vertrag belegt die freigegebene einstufige 44,1→48-kHz-Umsetzung. Er beweist nicht, dass außerhalb dieses Pfads keinerlei weitere Resamplingstufe existiert.
- Ein 32-Bit-Container beweist keine 32 oder 24 wirksamen Audiobits.
- Mikrofonposition, Raumakustik, Monitoring-Lautstärke, subjektive Klangqualität und physische Verkabelung benötigen eine reale Abnahme.
- Die Stereo-Sprachaufnahme beweist noch nicht, welcher physische MOTU-Eingang später als gewünschter Monokanal extrahiert werden soll.
- Die SMPTE-Division 25 fps × 40 Ticks ergibt 1.000 Ticks/s und damit 1 ms nominale Dateiauflösung. Sie ist weder ein BPM-/Taktclaim noch ein Beleg sample-genauer WAV/MIDI-Synchronität.
- Die manifestierten `CLOCK_MONOTONIC`-Offsets belegen nur Worker-Epoche, Spawn-/Running-Beobachtungen und den gemeinsamen Ready-Zeitpunkt. Scheduler-, ALSA-, USB- und Audiopufferlatenzen bleiben darin enthalten.

## Verzeichnisse anlegen

```bash
scripts/audio-record init
```

Standardpfade:

- Aufnahmen: `~/Music/Audio-Aufnahmen`
- Sitzungszustand: `~/.local/state/audio/recordings-v1`

Alternative Wurzeln können mit `--root` und `--state-root` angegeben werden. Symbolische Pfadkomponenten sowie fremde oder schreiboffene Zielverzeichnisse werden abgelehnt.

## Pläne prüfen

Stimme:

```bash
scripts/audio-record plan "Stimme 01.wav" \
  --session-type voice-recording \
  --maximum-seconds 3600
```

Klavier + Gesang:

```bash
scripts/audio-record plan "Song 01.wav" \
  --session-type piano-vocal-performance \
  --maximum-seconds 3600
```

Roland-Audio:

```bash
scripts/audio-record plan "Roland 01.wav" \
  --session-type roland-audio-recording \
  --maximum-seconds 3600
```

Produktions-Mixbus:

```bash
scripts/audio-record plan "Produktion 01.wav" \
  --session-type production-mix-recording \
  --maximum-seconds 3600
```

Wichtig sind:

- `ready: true`: alle maschinell prüfbaren Voraussetzungen gelten.
- `readiness.blockers`: konkrete Gründe, weshalb nicht gestartet wird.
- `plan_sha256`: die Freigabe für exakt diesen Zustand und Sitzungstyp.
- `required_file_bytes` und `required_free_bytes`: Dateibudget plus Reserve.

## Aufnahme starten

Der Sitzungstyp muss beim Start identisch angegeben werden:

```bash
scripts/audio-record start "Roland 01.wav" \
  --session-type roland-audio-recording \
  --maximum-seconds 3600 \
  --expected-plan-sha256 '<HASH-AUS-PLAN>'
```

Vor dem Start wird derselbe Plan vollständig neu berechnet. Ändert sich Quelle, Gate, physische Evidenz, Zielpfad, freier Speicher oder Programmvertrag, wird der Start verweigert.

## Status, Stop und Recovery

```bash
scripts/audio-record status
scripts/audio-record stop
scripts/audio-record recover
```

- `status` beobachtet nur und liest den Sitzungstyp aus dem privaten Spec.
- `stop` signalisiert ausschließlich den exakt gebundenen Prozess.
- `recover` räumt einen terminalen Sitzungszeiger auf oder markiert eine verwaiste Teilaufnahme als `failed-preserved`.
- Bei PID-Wiederverwendung oder Identitätsabweichung bleibt Recovery geschlossen.

Mit `--session-id <ID>` kann eine bestimmte Sitzung angesprochen werden.

## Speicherbedarf

Der gemeinsame Zieldatenstrom benötigt bei 48 kHz, zwei Kanälen und vier Byte je Sample:

- 384.000 Byte pro Sekunde
- etwa 23,0 MB pro Minute
- etwa 1,382 GB pro Stunde

Zusätzlich reserviert der Vertrag 1 MiB für Header und Metadaten sowie standardmäßig 1 GiB freien Speicher außerhalb des Aufnahmebudgets. Die maximale Sitzungsdauer beträgt vier Stunden.

## Fehler- und Dateisemantik

Die Aufnahme entsteht zunächst als versteckte Teil-Datei im endgültigen Zielverzeichnis. Nur nach sauberem Prozessende und bestandener WAV-Prüfung wird sie per nicht überschreibendem Hardlink veröffentlicht.

Beim Performance-Modus startet `arecordmidi` zuerst. Erst nachdem MIDI-Kind und private MID-Datei als laufend beobachtet wurden, startet `parecord`; Ready wird erst nach laufenden Kindern und begonnenem WAV geschrieben. Diese MIDI-Preroll verhindert verlorene frühe Tastenanschläge und wird über monotone Offsets dokumentiert. Beide Kinder laufen in eigenen, vom Worker gebundenen Prozessgruppen und erhalten unter Linux `PDEATHSIG=SIGKILL`. Der äußere Lifecycle signalisiert weiterhin ausschließlich die exakt gebundene Worker-PID; nur der Worker räumt seine Kinder auf.

Nach Stop prüft ein begrenzter read-only Scanner den SMF-Header, die SMPTE-Division `25/40`, sämtliche Trackgrenzen, Running Status, Meta- und SysEx-Längen. Er zählt unter anderem Note-on/off, Velocity-Grenzen, CC, CC64 und Pitchbend. Ein strukturell gültiger stiller MIDI-Take ist ausdrücklich gültig.

WAV und MID werden erst nach Validierung als nicht überschreibbare Geschwister verlinkt. Das finale `*.take.json` folgt zuletzt und bindet exakt deren SHA-256-, Größen-, Modus-, Device- und Inode-Belege. Nur dieses gültige finale Manifest ist die Commit-Wahrheit eines vollständigen Performance-Takes. Ein Fehler zwischen den Links lässt private Partials und eventuell bereits sichtbare, aber uncommitted Geschwister erhalten; Status, Library und Recovery melden daraus keinen vollständigen Take.

Bei einem Fehler gilt:

1. Keine vorhandene Zieldatei wird verändert.
2. Eine vorhandene Teil-Datei bleibt für Diagnose oder bewusste Rettung erhalten.
3. Der Ergebnisbeleg nennt ausdrücklich, was nicht bewiesen ist.
4. Ein neuer Start bleibt blockiert, bis der aktive Zeiger geprüft oder recovered wurde.

## Noch notwendige reale Abnahme

T007 muss später am Heim-PC belegen:

- RØDE am dokumentierten MOTU-Eingang, 48 V aktiv und Pegelziel zwischen −12 und −6 dBFS ohne Clipping.
- Roland-Verlust und Wiederkehr während realer Sitzungen sowie hörbarer und messbarer 44,1→48-kHz-Pfad.
- Erzeugung, Routing, Pegel und Upstream-Inhalt des Produktionsknotens `audio-production-mix`.
- Start, Stop, Maximaldauer, Dateigröße, WAV-/SMF-Header, CC64/Pitchbend, Abspielbarkeit und Recovery für alle vier Sitzungstypen.
- Reales grobes Alignment von Gesangs-WAV und Roland-SMF, MIDI-Preroll sowie Kind-Cleanup bei hartem Worker-Tod.
- Lokale Bedienung des Modusschalters und read-only Darstellung über die iPad-Bridge; das iPad öffnet weder MOTU noch Roland direkt.
- Störfreiheit, Latenz, Raumklang und sichere Monitoring-Lautstärke.
