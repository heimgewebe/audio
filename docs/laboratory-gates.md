# Labor-Gates und Messbelege

Die Audio-Profile enthalten physische Voraussetzungen und Labor-Gates. Ein
Gate ist nicht länger ein statischer Blocker: `audio-lab-gate` kann einen
privaten, validierten Beleg speichern. `audio-plan` liest diesen Zustand
read-only und zieht nur weiterhin gültige Belege von den offenen Gates ab.

Der Standardpfad lautet:

```text
~/.local/state/audio/laboratory/gates.v1.json
```

Die Datei wird atomar mit Modus `0600` geschrieben. Katalog- und Profilhash
sind Teil des Zustands. Belege für Stimmpegel und Loopback-Latenz sind außerdem
an den Hash des physischen Zustands gebunden. Ändert sich dieser Zustand,
werden die betreffenden Gates automatisch als ungültig ausgewiesen.

## Offline-Belege

`create-audio-evidence voice-level AUFNAHME.wav` analysiert eine vorhandene
WAV-Datei. Ein positiver Beleg verlangt Spitzen zwischen -12 und -6 dBFS und
null geclippte Samples.

`create-audio-evidence loopback-latency REFERENZ.wav AUFNAHME.wav
--quantum-frames 128` verwendet den bestehenden Impulsanalysator. Ein positiver
Beleg verlangt mindestens 0,8 Erkennungskonfidenz und 20 dB Peak-SNR. Der Beleg
wird zusätzlich an Samplerate und Quantum gebunden; ein Profil akzeptiert ihn
nur bei Übereinstimmung mit seinen Zielparametern. Dasselbe gilt für
XRun-Belege. Stimmpegelbelege müssen zur Ziel-Samplerate des Profils passen.

Sampleratenentscheidungen werden mit `policy-decision` als ausdrückliche
Operatorentscheidung dokumentiert. Eine Entscheidung belegt weder
Bit-Perfect-Wiedergabe noch das Ausbleiben von Resampling.

## Speicherung

```bash
./scripts/audio-lab-gate init
./scripts/create-audio-evidence policy-decision resampling-decision graph-48k \
  "Roland wird für den gemischten Betrieb kontrolliert auf 48 kHz umgesetzt." \
  > /tmp/resampling-evidence.json
./scripts/audio-lab-gate record resampling-decision /tmp/resampling-evidence.json
./scripts/audio-plan piano-digital-recording
```

Keiner dieser Befehle startet Wiedergabe, ändert Routing oder wendet ein Profil
an. XRun-, Qobuz- und Plugin-Host-Gates akzeptieren nur ihre streng typisierten
Belegformate. Das Qobuz-Gate wird nur bei übereinstimmender Track-, Graph- und
Endpunktrate ohne beobachtetes Resampling erfüllt. Passende aktive Beobachter
bleiben separate Arbeitsschritte.


## Graph- und Trackbindung

Der Profilplaner bildet aus Standardziel, Standardquelle, geplanter Samplerate
und geplantem Quantum einen kanonischen Graph-Fingerprint. Loopback- und
XRun-Belege werden nur akzeptiert, wenn ihr Fingerprint genau zu diesem
geplanten Kontext passt.

Ein Qobuz-Beleg enthält zusätzlich den SHA-256-Fingerprint einer stabilen
Track-Identität und die Track-Samplerate. `audio-plan qobuz-exclusive` verlangt
deshalb `--qobuz-track-fingerprint` und `--qobuz-track-rate-hz`; ohne aktuellen
Trackkontext bleibt das Gate blockiert. Damit kann ein Beleg weder auf einen
anderen Graphen noch auf einen anderen Titel übertragen werden.

WAV-Dateien werden vor der Analyse in eine private Momentaufnahme kopiert. Hash
und Analyse beziehen sich auf exakt dieselben Bytes. Ändert sich die Quelle
während der Momentaufnahme, wird kein Beleg erzeugt.
