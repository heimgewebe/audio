# Gehärtete Sprachaufnahme

## Zweck

`scripts/audio-record` verwaltet eine Sprachaufnahme vom RØDE NT1-A über die seriell gebundene MOTU-M2-Quelle. Der Ablauf trennt **prüfen**, **freigeben**, **aufnehmen**, **stoppen** und **wiederherstellen**.

Für Dummies: Das Programm drückt nicht sofort auf Aufnahme. Es prüft zuerst, ob Mikrofonzustand, Pegelnachweis, MOTU-Quelle, Speicherplatz, Zieldatei und Programmversion noch genau dem zuvor geprüften Plan entsprechen. Erst ein ausdrücklich bestätigter Plan-Hash darf die Aufnahme starten.

## Was der Vertrag garantiert

- 48 kHz, zwei Kanäle, 32-Bit-PCM-Container (`s32le`) und WAV.
- Die MOTU-M2-Quelle muss über Vendor-, Product-, Serien-, Bus-, Format-, Rate-, Kanal-, Mute- und Lautstärkebindung eindeutig sein.
- Die dokumentierten RØDE-/MOTU-Fakten und das Labor-Gate `voice-level-measurement` müssen aktuell aufgelöst sein.
- Nur eine verwaltete Aufnahme darf aktiv sein.
- Zieldateien werden niemals überschrieben.
- Eine unvollständige Datei bleibt bei Fehlern als private `.partial.wav` erhalten und wird nicht stillschweigend als fertige Aufnahme ausgegeben.
- Stop und Recovery vertrauen nicht nur einer PID, sondern prüfen zusätzlich Prozessstartzeit, ausführbare Datei, Kommandozeile und Prozessgruppe.
- Zustandsdateien sind privat (`0600`), Zustands- und Aufnahmeverzeichnisse privat (`0700`).

## Was der Vertrag nicht garantiert

- Die Stellung des analogen Gain-Reglers, Phantomspeisung und Verkabelung können softwareseitig nicht gemessen werden; sie werden über gebundene physische Beobachtungen abgesichert.
- Ein 32-Bit-Container beweist keine 32 oder 24 wirksamen Audiobits. Er erhält das vom MOTU/PipeWire-Pfad gelieferte Format ohne zusätzliche absichtliche Reduktion.
- Mikrofonposition, Raumakustik, Pop-Laute, Monitoring-Lautstärke und subjektive Klangqualität benötigen einen Hör- und Hardwaretest.
- Die Stereoaufnahme beweist noch nicht, welcher physische MOTU-Eingang später als gewünschter Monokanal extrahiert werden soll.

## Vorbereitung

Der Produktionsrecorder verlangt einen bestandenen Pegelnachweis. Dieser wird mit dem bereits vorhandenen, auf 8 bis 20 Sekunden begrenzten Kalibrierpfad erstellt:

```bash
scripts/create-audio-evidence voice-capture \
  --duration-seconds 15 \
  --wav-output "$HOME/Music/voice-level-reference.wav" \
  --output "$HOME/.local/state/audio/laboratory/voice-level-evidence.json"
```

Danach wird die Evidenz über den vorhandenen Labor-Gate-Ablauf geprüft und gespeichert. Der kurze Kalibrierpfad darf vor dem Gate aufnehmen; die normale Sitzung darf es nicht umgehen.

## Verzeichnisse anlegen

```bash
scripts/audio-record init
```

Standardpfade:

- Aufnahmen: `~/Music/Audio-Aufnahmen`
- Sitzungszustand: `~/.local/state/audio/recordings-v1`

Alternative Wurzeln können mit `--root` und `--state-root` angegeben werden. Symbolische Pfadkomponenten und fremde oder schreiboffene Zielverzeichnisse werden abgelehnt.

## Plan prüfen

```bash
scripts/audio-record plan "Stimme 01.wav" --maximum-seconds 3600
```

Wichtig sind:

- `ready: true`: alle maschinell prüfbaren Voraussetzungen gelten.
- `readiness.blockers`: konkrete Gründe, weshalb nicht gestartet wird.
- `plan_sha256`: die Freigabe für exakt diesen Zustand.
- `required_file_bytes` und `required_free_bytes`: Dateibudget plus Reserve.

Der Plan ist read-only. Er startet keinen Audioprozess und legt keinen Sitzungszustand an.

## Aufnahme starten

```bash
scripts/audio-record start "Stimme 01.wav" \
  --maximum-seconds 3600 \
  --expected-plan-sha256 '<HASH-AUS-PLAN>'
```

Vor dem Start wird derselbe Plan erneut vollständig berechnet. Ändert sich eine gebundene Voraussetzung, wird der Start verweigert und ein neuer Plan muss geprüft werden.

## Status, Stop und Recovery

```bash
scripts/audio-record status
scripts/audio-record stop
scripts/audio-record recover
```

- `status` beobachtet nur.
- `stop` sendet ein Signal ausschließlich an den exakt gebundenen Prozess.
- `recover` räumt einen terminalen Sitzungszeiger auf oder markiert eine verwaiste Teilaufnahme als `failed-preserved`.
- Bei PID-Wiederverwendung oder Identitätsabweichung bleibt Recovery absichtlich geschlossen.

Mit `--session-id <ID>` kann eine bestimmte Sitzung angesprochen werden.

## Speicherbedarf

Der rohe Datenstrom benötigt bei 48 kHz, zwei Kanälen und vier Byte je Sample:

- 384.000 Byte pro Sekunde
- etwa 23,0 MB pro Minute
- etwa 1,382 GB pro Stunde

Zusätzlich rechnet der Vertrag mit 1 MiB Header-/Metadatenreserve pro Datei und hält standardmäßig 1 GiB freien Speicher außerhalb des Aufnahmebudgets zurück. Die maximale Sitzungsdauer beträgt vier Stunden.

## Fehler- und Dateisemantik

Die Aufnahme entsteht zunächst als versteckte Teil-Datei im endgültigen Zielverzeichnis. Nur nach sauberem Prozessende und bestandener WAV-Prüfung wird sie per nicht überschreibendem Hardlink veröffentlicht. Dadurch gibt es keinen kopierenden Cross-Filesystem-Schritt und keinen stillen Austausch einer bereits vorhandenen Zieldatei.

Bei einem Fehler gilt:

1. Keine vorhandene Zieldatei wird verändert.
2. Eine vorhandene Teil-Datei bleibt für Diagnose oder bewusste Rettung erhalten.
3. Der Ergebnisbeleg nennt ausdrücklich, was nicht bewiesen ist.
4. Ein neuer Start bleibt blockiert, bis der aktive Zeiger geprüft oder recovered wurde.

## Noch notwendige Hardware-Abnahme

Am PC müssen später einmal real geprüft werden:

- RØDE am dokumentierten MOTU-Eingang und 48 V aktiv.
- Pegelziel zwischen −12 und −6 dBFS ohne Clipping.
- Start, Stop, Maximaldauer und Recovery mit echter MOTU-Aufnahme.
- Welcher der zwei aufgezeichneten Kanäle den gewählten Mikrofoneingang führt.
- Dateigröße, WAV-Header und tatsächliche Abspielbarkeit.
- Störfreiheit, Latenz, Raumklang und Monitoring-Pegel.
