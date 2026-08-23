# Gehärtete Aufnahmesitzungen

## Zweck

`scripts/audio-record` verwaltet vier explizite Aufnahmetypen mit demselben fail-closed Sitzungsprotokoll:

| Sitzungstyp | Quelle | Voraussetzung | Zielformat |
| --- | --- | --- | --- |
| `voice-recording` | RØDE NT1-A über die seriell und busgebundene MOTU-M2-Quelle | physische RØDE-/MOTU-Fakten und `voice-level-measurement` | 48 kHz, Stereo, `s32le`, WAV |
| `piano-vocal-performance` | RØDE/MOTU und echter Roland-FP-30X-USB-Audioeingang parallel, plus genau ein kernel-/USB-gebundener Roland-Sequencer-Port | Voice- und Resampling-Gates, eindeutige MOTU-/Roland-Quellen sowie gebundenes `arecordmidi` und `ffmpeg` | finaler Stereo-Mix `name.wav`, Roland-MIDI `name.mid` und Commit-Beleg `name.take.json` |
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
- Der Performance-Plan bindet `/usr/bin/arecordmidi` und `/usr/bin/ffmpeg`, jeweils mit tatsächlich aufgelöstem Binary-Pfad und Host-SHA-256. Vor dem Start wird jede Bindung erneut gelesen; jede Änderung schließt den Start.
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
- `readiness.checks`: sieben kanonische Prüfgruppen (`output`, `physical`, `laboratory`, `source`, `tools`, `storage`, `session`). Jede Gruppe ist ausdrücklich `ready` oder `blocked` und führt ausschließlich ihre eigenen Blocker. Die flache Blockerliste muss exakt der Vereinigung dieser Gruppen entsprechen.
- `plan_sha256`: die Freigabe für exakt diesen Zustand und Sitzungstyp.
- `required_file_bytes` und `required_free_bytes`: Dateibudget plus Reserve.

## Gebundener Live-Pegel in der Audiozentrale

Der dauerhafte Pegelobserver öffnet einen eigenen PipeWire-Stream auf exakt der
MOTU-Quelle, deren Identitätsvertrag auch der Recorder verwendet. Er veröffentlicht
keinen Roh-Gerätenamen, sondern nur den kanonischen Quellen-Hash, FL/FR und die
zugehörigen Peak-/RMS-Werte. Ein MOTU-Hotplug erzeugt keinen Default-Source-Fallback.

Die Recorderoberfläche zeigt daraus einen Mikrofonpegel nur, wenn zusätzlich eine
der folgenden Autoritäten vorliegt:

- eine aktive Recorder-Session mit identischem `source.identity_sha256`, oder
- ein explizit angeforderter, noch zum aktuellen Entwurf passender Recorderplan
  mit identischem Quellen-Hash.

Der physisch bestätigte Wert `rode_nt1a_motu_input` wählt dabei ausschließlich
`input-1 → FL` oder `input-2 → FR`. Fehlen Plan/Session, Hashgleichheit oder diese
Inputbindung, wird kein aggregierter Stereo- oder Default-Source-Pegel als
Mikrofonpegel ausgegeben. Nach **Plan prüfen** kann der damit gebundene Vorabpegel
vor dem eigentlichen Record-Start genutzt werden; der Start bleibt trotzdem an
alle bestehenden Physical-, Laboratory-, Source-, Tool-, Storage- und
Session-Gates gebunden. Der UI-Pegel ersetzt insbesondere nicht den dauerhaften
Beleg `voice-level-measurement` und beweist weder 48 V noch Gainstellung oder
Mikrofonverkabelung aus sich selbst.

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
- `stop` signalisiert ausschließlich den exakt gebundenen Prozess. Die Audiozentrale liest danach den autoritativen Recorderzustand zurück. Ist der Take `completed`, wird zusätzlich die aktuell veröffentlichte WAV gegen den gebundenen Ergebnisbeleg geprüft; bei `piano-vocal-performance` wird auch die Roland-MIDI-Datei geprüft. Der Aktions-Readback gibt nur pfadfreie Prüfmetadaten weiter.
- Schlägt diese zusätzliche Medienprüfung nach einem bereits bestätigten Stop fehl, wird der Stop nicht rückwirkend als fehlgeschlagen ausgegeben: der Ergebnisstatus lautet ausdrücklich `unverified`. Wiedergabe und Export bleiben zusätzlich durch ihre eigene aktuelle Medienprüfung geschützt.
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

Beim Performance-Modus startet `arecordmidi` zuerst. Danach werden MOTU- und Roland-`parecord` ohne Monitoringgraph als zwei headless 48-kHz-Prozesse gestartet. Der monotone Abstand zwischen den beiden Spawn-Anforderungen darf höchstens 5 ms betragen; Überschreitung oder Quellidentitätsdrift schließen die Aufnahme fail-closed. Am Take-Ende darf die Länge der beiden unabhängig laufenden Captures um höchstens 4.800 Frames beziehungsweise 100 ms differieren; `amix=duration=shortest` schneidet ausschließlich diesen begrenzten Endüberhang ab, größere Abweichungen bleiben Fehler. Ready folgt erst nach beiden WAV-Stems und MIDI. Nach dem sauberen Stop erstellt das gebundene `ffmpeg` ohne Routing- oder Monitoringmutation deterministisch den 48-kHz-Stereo-Mix mit `amix`, `duration=shortest` und gleichgewichteter Eingangsnormalisierung (`normalize=1`) als statischer Headroom-Schutz. `ffmpeg` liefert dafür privates rohes `s32le`-PCM; der Recorder verpackt exakt diese erwartete Framezahl anschließend selbst in einen klassischen RIFF/PCM-WAV-Container, damit Validator und Browser keinen `WAVE_FORMAT_EXTENSIBLE`-Sonderpfad benötigen. Voice-/Roland-Stems und der Raw-Scratch bleiben temporär, privat und werden nach erfolgreichem Manifest-Commit entfernt. Die MIDI-Preroll, Spawn-Offets und der gemeinsame Ready-Zeitpunkt sind manifestiert; sie belegen keine sample-genaue WAV/MIDI-Synchronität.

Nach Stop prüft ein begrenzter read-only Scanner den SMF-Header, die SMPTE-Division `25/40`, sämtliche Trackgrenzen, Running Status, Meta- und SysEx-Längen. Er zählt unter anderem Note-on/off, Velocity-Grenzen, CC, CC64 und Pitchbend. Ein strukturell gültiger stiller MIDI-Take ist ausdrücklich gültig.

Der finale Mix-WAV und MIDI werden erst nach Validierung als nicht überschreibbare Geschwister verlinkt. Das finale `*.take.json` folgt zuletzt und bindet exakt deren SHA-256-, Größen-, Modus-, Device- und Inode-Belege. Nur dieses gültige finale Manifest ist die Commit-Wahrheit eines vollständigen Performance-Takes. Ein Fehler zwischen den Links lässt private Partials und eventuell bereits sichtbare, aber uncommitted Geschwister erhalten; Status, Library und Recovery melden daraus keinen vollständigen Take. Abgeschlossene Legacy-Takes mit `vocal_wav` und MIDI bleiben lesbar; sie zeigen ausdrücklich, dass das Klavier dort MIDI-only ist.

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
