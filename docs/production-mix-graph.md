# Verwalteter Produktions-Mixgraph

## Zweck

`scripts/audio-production-mix` definiert und verwaltet den flüchtigen PipeWire-Graphen zwischen RØDE/MOTU, Roland FP-30X, Softwareinstrumenten, MOTU-Monitoring und dem Aufnahmeknoten `audio-production-mix`.

Der Manager verändert keine globalen PipeWire-Standards und schreibt keine dauerhafte PipeWire-Konfiguration. Er erzeugt den Graphen ausschließlich als gebundenen transienten Benutzerdienst. In diesem Repositorylauf wurde der Dienst nicht gestartet.

## Kanonischer Graph

| Rolle | Vertrag |
| --- | --- |
| Produktionsbus | virtueller Stereo-Sink `audio-production-bus`, 48 kHz, `s32le`, FL/FR |
| Aufnahmemix | virtuelle Stereo-Quelle `audio-production-mix`, gespeist aus dem Monitor des Produktionsbusses |
| Monitoring | Produktionsbus direkt zum eindeutig gebundenen MOTU-M2-Sink |
| Stimme | der physisch dokumentierte MOTU-Eingang wird als Monoquelle zum Produktionsbus geführt |
| Roland | eindeutige FP-30X-USB-Quelle, 44,1 kHz, wird einmal in den 48-kHz-Produktionsgraphen überführt |
| Softwareinstrument | muss seinen Ausgabestrom ausdrücklich auf `audio-production-bus` richten |

Der Bus ist kein globaler Standardsink. Browser, Qobuz und Systemtöne gelangen daher nicht unbeabsichtigt in eine Produktionsaufnahme.

## Sicherheits- und Identitätsvertrag

- Planen ist read-only und startet keinen Audioprozess.
- Start verlangt den exakten aktuellen Plan-Hash.
- RØDE-, Roland- und MOTU-Monitorquellen werden über Node, USB-Identität, Serien- und Busbindung, Format, Rate, Kanäle und Mute-Zustand gebunden.
- Der ausgewählte Mikrofoneingang bestimmt explizit `FL` oder `FR`; die Produktionsroute gibt ihn als Mono weiter.
- Der Dienst bindet systemd-Invocation, MainPID, Control Group, ExecStart und den privaten Spec-Hash.
- Alle vier `pw-loopback`-Kinder binden zusätzlich PID, Startzeit, Executable, Kommandozeilenhash und Prozessgruppe.
- Fällt ein Kind aus oder driftet die beobachtete Topologie dreimal in Folge, wird der gesamte Graph beendet.
- Stop greift nur bei exakter Dienstidentität. Ein fremder oder ausgetauschter Dienst bleibt unangetastet.
- Zustandsdateien sind privat und atomar; verwaiste Sitzungen benötigen explizite Recovery.

## Ressourcenvertrag

Der transiente Dienst begrenzt:

- Speicher auf 256 MiB,
- Tasks auf 64,
- offene Dateien auf 128,
- CPU auf 80 Prozent,
- Laufzeit auf zwölf Stunden,
- Journalrate auf 100 Meldungen je 30 Sekunden,
- erfassten Standardfehler auf 64 KiB je Kindprozess.

## Bedienung

Zustandsverzeichnis anlegen:

```bash
scripts/audio-production-mix init
```

Plan prüfen:

```bash
scripts/audio-production-mix plan
```

Erst nach Prüfung von `ready`, `readiness.blockers` und `plan_sha256` darf derselbe Zustand gestartet werden:

```bash
scripts/audio-production-mix start \
  --expected-plan-sha256 '<HASH-AUS-PLAN>'
```

Beobachten, stoppen oder verwaiste Zustände behandeln:

```bash
scripts/audio-production-mix status
scripts/audio-production-mix stop
scripts/audio-production-mix recover
```

Erst wenn `status` den exakten Dienst, alle vier Kindprozesse und eine vollständige Topologie als `ready` ausweist, kann die vorhandene Aufnahmesitzung die Quelle verwenden. Dieser Runtime-Beleg wird in den Recorder-Plan eingebunden und vor dem Capture erneut gelesen:

```bash
scripts/audio-record plan "Produktion 01.wav" \
  --session-type production-mix-recording \
  --maximum-seconds 3600
```

## Noch nicht bewiesen

Repositorytests beweisen keine reale Funktions- oder Klangqualität des PipeWire-Graphen. T007 muss später unter kontrollierter Lautstärke prüfen:

- reale Knoten- und Porterzeugung durch die installierte PipeWire-Version,
- korrekte Auswahl des physischen RØDE-Kanals,
- einmalige und störungsfreie Roland-Umsetzung von 44,1 auf 48 kHz,
- Monitoring zum MOTU ohne Rückkopplung oder doppelte Route,
- Softwareinstrument-Routing, XRuns, Latenz und Pegel,
- tatsächliche 24-Bit-Nutzinformation innerhalb des 32-Bit-Containers,
- Start, Driftabbruch, Stop, Recovery und Aufnahme über `audio-production-mix`.
