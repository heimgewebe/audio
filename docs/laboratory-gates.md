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

## Gebundene Live-Stimmpegelmessung

Der kanonische Stimmpegelbeleg wird direkt von der seriengebundenen
MOTU-M2-Quelle aufgenommen:

```bash
./scripts/create-audio-evidence voice-capture \
  --duration-seconds 15 \
  --wav-output /tmp/voice-reference.wav \
  --output /tmp/voice-reference-evidence.json
./scripts/audio-lab-gate record voice-level-measurement \
  /tmp/voice-reference-evidence.json --replace
```

Während des 15-Sekunden-Fensters wird die lauteste realistische
Stimm-Darbietung gesprochen. Ein positiver Beleg verlangt eine eindeutige,
unveränderte MOTU-M2-Quelle mit Serienkennung, 48 kHz, Stereo, 32-Bit-PCM,
Software-Aufnahmepegel 0 dB, mindestens acht Sekunden Aufnahme, keine
geclippten Samples und Spitzen zwischen -12 und -6 dBFS. WAV und JSON werden
privat mit Modus `0600` geschrieben. Serienkennung und PipeWire-Knotenname
erscheinen im Beleg nur als SHA-256.

Verschwindet das MOTU, wechselt seine Identität, ist die Quelle stumm oder
nicht auf 0 dB, endet die Messung fail-closed. Der Befehl verändert weder
Routing noch Hardware-Gain noch Monitoring-Pegel.

## Offline-Belege

`create-audio-evidence voice-level AUFNAHME.wav` analysiert eine vorhandene
WAV-Datei weiterhin diagnostisch. Ein positives Analyseergebnis verlangt
Spitzen zwischen -12 und -6 dBFS und null geclippte Samples. Da eine beliebige
Datei jedoch keine laufende MOTU-Quelle beweist, kann dieser alte ungebundene
Beleg `voice-level-measurement` nicht mehr auflösen.

Der geplante Graph-Fingerprint wird vor der Messung aus dem Zielprofil gelesen:

```bash
graph_fingerprint=$(./scripts/audio-plan piano-software-live \
  | jq -r .planned_graph_fingerprint)
./scripts/create-audio-evidence loopback-latency \
  REFERENZ.wav AUFNAHME.wav \
  --quantum-frames 128 \
  --graph-fingerprint "$graph_fingerprint"
```

`create-audio-evidence loopback-latency` verwendet den bestehenden
Impulsanalysator. Ein positiver Beleg verlangt mindestens 0,8
Erkennungskonfidenz und 20 dB Peak-SNR. Referenz und Aufnahme müssen
unterschiedliche Bytes besitzen, und die erkannte Verzögerung muss mindestens
ein Sample betragen. Der Beleg wird zusätzlich an Samplerate, Quantum und den
geplanten Graph-Fingerprint gebunden; ein Profil akzeptiert ihn
nur bei Übereinstimmung mit seinem Zielkontext. Dasselbe gilt für XRun-Belege.
Stimmpegelbelege müssen zur Ziel-Samplerate des Profils passen.

Sampleratenentscheidungen werden mit `policy-decision` als ausdrückliche
Operatorentscheidung dokumentiert. Eine Entscheidung belegt weder
Bit-Perfect-Wiedergabe noch das Ausbleiben von Resampling.

## Gebundene XRun-Beobachtung

Eine XRun-Freigabe darf nicht aus einer manuell eingetragenen Zahl entstehen.
`xrun-observation` bindet deshalb eine mindestens 60 Sekunden lange Beobachtung
an zwei verifizierte Systemwahrheitsberichte, den unveränderten Graphen und ein
begrenztes Journalfenster.

Vor dem Lauf müssen Rate, Quantum und Graph-Fingerprint aus demselben aktuellen
Truth-Report gelesen werden:

```bash
./scripts/audio-truth capture --output /tmp/audio-truth.json
jq '.doctor.graph | {force_rate_hz, force_quantum_frames}' /tmp/audio-truth.json
jq -r '.runtime.graph_fingerprint' /tmp/audio-truth.json

./scripts/create-audio-evidence xrun-observation \
  --duration-seconds 60 \
  --expected-rate-hz 48000 \
  --expected-quantum-frames 1024 \
  --expected-graph-fingerprint SHA256_AUS_DEM_TRUTH_REPORT \
  --output /tmp/xrun-evidence.json
./scripts/audio-lab-gate record xrun-stability-test \
  /tmp/xrun-evidence.json --replace
```

Die Beispielwerte `48000` und `1024` dürfen nicht blind übernommen werden. Der
Lauf bricht ab, wenn sich Graph, Rate oder Quantum ändern, die Journalabfrage
fehlschlägt oder abgeschnitten wird, das Zeitfenster zu kurz ist oder neue
XRun-, Underrun-, Overrun- beziehungsweise Dropout-Meldungen auftreten. Er
startet weder Wiedergabe noch Routing. Alte ungebundene XRun-Belege bleiben zur
Migration lesbar, lösen das Gate aber nicht mehr und können nicht neu gespeichert
werden.

## Gebundene Plugin-Host-Beobachtung

`managed-plugin-host-observation` ermittelt aktive Plugin-Hosts aus einem
begrenzten Prozess-Snapshot, bindet jeden Prozess über PID, Kernel-Startzeit,
Befehlsdigest und cgroup an genau einen systemd-Benutzerdienst und beobachtet
diesen Zustand mindestens 60 Sekunden.

```bash
./scripts/create-audio-evidence managed-plugin-host-observation \
  --duration-seconds 60 \
  --output /tmp/plugin-host-evidence.json
./scripts/audio-lab-gate record managed-plugin-host-proof \
  /tmp/plugin-host-evidence.json --replace
```

Ein positiver Beleg verlangt für jeden beobachteten Host:

- unveränderte Prozessidentität und keinen Dienstneustart;
- einen geladenen und laufenden systemd-Benutzerdienst;
- `MemoryMax` bis 2 GiB, `TasksMax` bis 512 und `LimitNOFILE` bis 262.144;
- journalgebundene Standardausgabe sowie dienstseitige Logratenbegrenzung;
- ein vollständiges, auf die exakten Diensteinheiten begrenztes Journalfenster;
- keinen eigenständig laufenden `sfizz_jack`-Prozess.

Fehlende oder unendliche Grenzen erzeugen einen Fail-Beleg mit konkreten
Blockern; sie werden nicht als unbekannte oder implizit sichere Werte geglättet.
Der Beobachter startet, stoppt oder verändert keinen Dienst. Alte Belege mit
nur frei gesetzten Wahrheitswerten bleiben lesbar, lösen das Gate jedoch nicht
mehr und können nicht neu gespeichert werden.

## Gebundene Qobuz-Ratenbeobachtung

`qobuz-rate-observation` beobachtet ausschließlich den kanonischen
Mopidy-Qobuz-Pfad. Der Beobachter startet keine Wiedergabe. Während des
begrenzten Startfensters muss deshalb ein Qobuz-Titel in Mopidy neu begonnen
werden. Ein bereits vor dem Beobachter laufender Titel zählt erst nach Stop und
Neustart oder nach einem Trackwechsel.

```bash
./scripts/create-audio-evidence qobuz-rate-observation \
  --start-timeout-seconds 60 \
  --duration-seconds 60 \
  --output /tmp/qobuz-rate-evidence.json
./scripts/audio-lab-gate record qobuz-rate-proof \
  /tmp/qobuz-rate-evidence.json --replace
```

Ein positiver Beleg bindet aus demselben Beobachtungsfenster:

- die Qobuz-Track-ID und tatsächlich ausgelieferte FLAC-Rate aus einem neuen
  `Mopidy-Qobuz-Hires`-Journalereignis;
- eine datensparsame Trackidentität mit Hashes statt Titel- und Künstlertext;
- den aktiven Mopidy-Pulse-Stream und dessen PCM-Rate;
- PipeWire-Graphrate, Quantum und Graph-Fingerprint vor und nach dem Lauf;
- die Rate der aktuellen Standardsenke sowie die unveränderte Streamroute;
- mindestens 60 Sekunden kontinuierliche Wiedergabe mit monotoner Position;
- die exakten Bytes von Beobachter, Gate-Validator und Systemwahrheit.

Nur wenn Track-, Stream-, Graph- und Endpunktrate identisch sind, gilt
`resampling_observed` als falsch. Browser-Qobuz wird ausdrücklich nicht durch
diesen Beleg abgedeckt. Fehlt ein neuer Mopidy-Qobuz-Titel, ändert sich die
Route oder wird kein neues DownloadableTrack-Ereignis gefunden, entsteht ein
strukturierter Fail-Beleg. Alte frei eingetragene Qobuz-Raten bleiben zur
Migration lesbar, lösen das Gate aber nicht mehr.

## Gebundene Sampleratenentscheidungen

Die beiden Policy-Gates werden nicht mehr durch frei formulierte Texte
freigegeben. Der Beobachter bindet seine Entscheidung an den aktuellen
Systemwahrheitsbericht, die realen PipeWire/Pulse-Endpunkte, die relevanten
Profile und die exakten Implementierungsbytes:

```bash
./scripts/create-audio-evidence rate-policy-observation \
  rate-policy-decision \
  --output /tmp/rate-policy-evidence.json
./scripts/create-audio-evidence rate-policy-observation \
  resampling-decision \
  --output /tmp/resampling-evidence.json

./scripts/audio-lab-gate record rate-policy-decision \
  /tmp/rate-policy-evidence.json --replace
./scripts/audio-lab-gate record resampling-decision \
  /tmp/resampling-evidence.json --replace
```

Der kanonische Vertrag lautet:

- gemischte Wiedergabe, Aufnahme, Referenzhören und Softwareinstrumente nutzen
  einen stabilen 48-kHz-Graphen;
- das Roland FP-30X liefert digitales Audio mit 44,1 kHz und wird einmalig im
  PipeWire-Pfad auf 48 kHz umgesetzt;
- MIDI wird nicht resampelt;
- eine zusätzliche absichtliche Resampling-Stufe ist verboten;
- Qobuz darf nur im exklusiven Profil tracknativ laufen, wenn ein passender
  `qobuz-rate-proof` vorliegt und paralleles Mischen ausgeschlossen ist;
- ohne diesen Beleg bleibt auch Qobuz auf dem stabilen 48-kHz-Fallback.

Ein positiver Beleg verlangt echte MOTU-Ein-/Ausgänge ausschließlich bei
48 kHz, echte Roland-Ein-/Ausgänge ausschließlich bei 44,1 kHz, einen
48-kHz-Systemgraphen, MOTU als Standardquelle und Standardsenke sowie exakt
passende Profilverträge. Monitorquellen werden nicht als Eingänge gezählt.
Gerätekennungen und Knotennamen erscheinen nur als SHA-256.

Die Befehle lesen ausschließlich. Sie schalten weder die Graphsamplerate noch
Profile oder Routing um. Sie belegen auch keine Bitgenauigkeit, keine
Resamplertransparenz und keine Latenz- oder XRun-Eignung bei einem anderen
Quantum. Alte freie `policy-decision`-Belege bleiben zur Migration lesbar,
werden aber als `legacy-unbound-policy-evidence` entwertet.

## Speicherung

`audio-lab-gate` speichert validierte Belege atomar und privat mit Modus `0600`.
XRun-, Qobuz-, Voice-, Plugin-Host- und Sampleraten-Gates akzeptieren nur ihre
streng typisierten Belegformate. Passende aktive Beobachter bleiben getrennte
Arbeitsschritte.


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
