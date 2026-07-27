# Kalibrier- und Messworkflow

`create-calibration-pack` erzeugt begrenzte WAV-Dateien und ein artefakt- sowie quellgebundenes
Manifest. Es findet keine automatische Wiedergabe statt.

Verfügbare Pakete:

- `headphone-reference`: −20-dBFS-Ton für MOTU, Lake People und Focal;
- `voice-gain`: Sicherheits- und Aufnahmecheckliste ohne Testton;
- `receiver-reference`: −20-dBFS-Ton für den Pioneer-Pfad;
- `motu-loopback`: −20-dBFS-Impuls für eine physische Loopback-Messung.

`analyze-audio-level` bewertet eine vorhandene PCM-WAV-Datei offline. Für Stimme
wird der Spitzenbereich −12 bis −6 dBFS als Ziel verwendet. Eine echte
Round-Trip-Latenz entsteht erst durch Ausgabe, physisches Loopback, Aufnahme und
anschließende Auswertung mit `analyze-loopback-latency`.

Der Pegelanalysator verarbeitet höchstens 2.000.000 Samples pro Datei.
