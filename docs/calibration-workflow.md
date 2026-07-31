# Kalibrier- und Messworkflow

`create-calibration-pack` bereitet physische Pegel-, Stimm- und
Loopbackmessungen vor. Das Werkzeug startet **weder Wiedergabe noch Aufnahme**.
Es erzeugt nur begrenzte WAV-Dateien, Checklisten und ein prüfbares Manifest.

## Vertrauensmodell

Ein Paket ist nur am exakt gebundenen, sauberen Git-Stand verwendbar. Das
Manifest bindet außerdem die Bytes der folgenden Repositoryverträge:

- Profilkatalog und Referenzpegel;
- physische Fakten und Verifikationsschema;
- Signalpfad und Systemwahrheit;
- Labor-Gates;
- Kalibrierpaket-Katalog, Generator und Referenzsignalgenerator.

`validate` vergleicht diese Bindungen mit dem aktuellen Repository. Änderungen
an Code, Katalogen, Profilen oder Messverträgen machen ein altes Paket bewusst
ungültig. Ebenso blockieren manipulierte WAV-Dateien, Symlinks, Spezialdateien,
Pfadtraversal, unerwartete Dateien und Größenüberschreitungen.

Der Zeitstempel gehört nicht zur reproduzierbaren Paketidentität. Gleiche
Eingaben am gleichen Git- und Vertragsstand ergeben denselben `pack_sha256` und
dieselben Signalbytes.

## Bedienung

Neue Form:

```text
scripts/create-calibration-pack create PACK AUSGABEVERZEICHNIS
scripts/create-calibration-pack validate AUSGABEVERZEICHNIS --pack PACK
```

Die bisherige Erzeugungssyntax bleibt gültig:

```text
scripts/create-calibration-pack PACK AUSGABEVERZEICHNIS
```

Erzeugung scheitert fail-closed bei einem schmutzigen Git-Checkout. Validierung
ist vollständig read-only.

## Pakete

### `headphone-reference`

Erzeugt einen mono 48-kHz-/16-Bit-Ton mit 1 kHz, −20 dBFS und fünf Sekunden
Dauer. Er dient nur zur kontrollierten Referenzstellung für MOTU M2, Lake People
G111 Mk2 und Focal Clear MG.

Vor einer manuellen Wiedergabe müssen der Lake-People-Regler auf Minimum stehen,
nur ein analoger Regler zugleich angehoben werden und unerwartete Lautstärke zum
sofortigen Abbruch führen. Zu dokumentieren sind Verkabelung, Gain-Stellung,
Referenzstellung und angeschlossener Kopfhörerausgang.

### `voice-gain`

Enthält absichtlich keine WAV-Datei. Die Checkliste bereitet die physische
Prüfung von RØDE NT1-A, MOTU-Eingang, 48 V und Gain-Stellung vor. Erst eine
anschließende gebundene Live-Aufnahme kann das Gate
`voice-level-measurement` erfüllen. Zielbereich der lautesten realistischen
Stimme: Spitzen zwischen −12 und −6 dBFS ohne Clipping.

### `receiver-reference`

Erzeugt denselben begrenzten Referenzton für den Pioneer-Pfad. Vor manueller
Wiedergabe stehen Receiverlautstärke auf Minimum und Hörmodus auf Stereo, solange
der Mehrkanalpfad nicht belegt ist. Eingang, Hörmodus, Verbindung und
Referenzlautstärke müssen physisch dokumentiert werden.

### `motu-loopback`

Erzeugt einen mono 48-kHz-/16-Bit-Impuls mit −20 dBFS und einer Sekunde Dauer.
Erst Ausgabe, physisches Kabel, getrennte Aufnahme und anschließende Analyse mit
`analyze-loopback-latency` können das Gate
`loopback-latency-measurement` erfüllen. Kopfhörer und Receiver bleiben dabei
stumm oder abgesenkt; am Loopback-Eingang sind 48 V und Mikrofon verboten.

## Was ein Paket nicht beweist

Ein erfolgreich erzeugtes oder validiertes Paket beweist keine sichere
Hörlautstärke, keine richtige Verkabelung, keine Reglerstellung, kein
Messergebnis und kein abgeschlossenes Labor-Gate. Diese Aussagen entstehen erst
durch explizite physische Beobachtung und private, validierte Messbelege.

`analyze-audio-level` bewertet vorhandene PCM-WAV-Dateien offline. Eine echte
Round-Trip-Latenz entsteht nur durch Ausgabe, physisches Loopback, Aufnahme und
`analyze-loopback-latency`. Der Pegelanalysator verarbeitet höchstens 2.000.000
Samples pro Datei.
