# audio

Kanonisches Repository für die Audio-Konfiguration des Heim-PC, Aufnahme,
Wiedergabe, Instrumente und experimentelle Musiksysteme.

## Aktueller Zustand

Dieses Repository entwickelt die Audiokonfiguration über **Planung,
Sicherheitsregeln und überprüfbare Betriebsverträge**. Produktiver Zustand darf
nur profilgebunden über `plan`, `diff`, `apply`, `rollback` und Recovery
verändert werden.

Der erste wirkende Profilvertrag ist auf `desktop-mixed` begrenzt. Er bindet die
MOTU-M2-Standardsenke mit USB-ID-, Serien-, Knoten- und Busbindung, 48 kHz und Quantum 1.024 an einen exakten Planhash, ein
privates atomares Journal und anschließenden Live-Readback. Ohne eine eindeutig
beobachtete MOTU M2 bleibt bereits der Dry-Run fail-closed; so ist es im
aktuellen Readback vom 1. August 2026. Weitere Profile bleiben gesperrt.

Der Altbestand `heimgewebe/hausKI-audio` bleibt zunächst unverändert und dient
nur als geprüfte Ideen- und Verhaltensquelle. Seine Implementierung und Historie
werden standardmäßig nicht übernommen; nützliche Anforderungen und Testabsichten
werden gegen die neuen Verträge neu programmiert.

Als erstes ausführbares Klangexperiment enthält das Repository
**Buckelwal Live Voice**. Standard ist `morph`: eine monophone,
quellengestützte Walstimme über alle 88 chromatischen Tasten von A0 bis C8.
Kurze periodische Stimmzyklen aus lizenzierten Buckelwalaufnahmen werden
phasengleich gemittelt, bandbegrenzt und stufenlos gemorpht. Es gibt keine
Samplezonen, Presets, Steuertasten oder permanente Rauschschicht. Der Modus
`organic` nutzt zusätzlich ein hashgebundenes zeitvariables Source-Filter-Modell:
Hüllkurve, Periodizität, signalgebundene Rauigkeit, spektrale Neigung,
Resonanzen, Pulsgruppen, Subharmonik und leise sekundäre Frequenzspuren stammen
aus normalisierten Trajektorien der Originalaufnahmen. Ein starker
Grundton-Tiefbass und der Anti-UFO-Vertrag bleiben erhalten. Globale,
zeitliche und ganze Quellfamilien zurückhaltende Holdout-Vergleiche prüfen die
reproduzierbare Spielphrase. Die Modi `realistic` (Aufnahmephrasen) und `ufo`
bleiben als Vergleiche erhalten.

## Buckelwal-Lernlektion

Die read-only Audiozentrale enthält eine erste geführte Lektion
`Vom reinen Ton zur Buckelwaleinheit`. Sie trennt echte Beobachtung,
reproduzierbares Modell und musikalische 88-Tasten-Extrapolation. Hörproben,
Merkmalskurven und ein lokaler A/B-Vergleich starten keine Liveengine und
speichern kein Urteil. Reproduktion: `just whale-learning-check`.

## Bereiche

- reproduzierbare Audio- und MIDI-Profile
- MOTU M2, Roland FP-30X, Rode NT1-A und Wiedergabeketten
- Qobuz: gemischter und exklusiver Betrieb
- Aufnahme und Live-Monitoring
- Dauersong und spätere Klangexperimente
- Doctor, Wahrheitskette, Drift, Diff, Apply, Rollback und Recovery
- harte Grenzen für Logs, Prozesse, große Assets und Geheimnisse

## Audiozentrale

Die lokale **Audiozentrale** ordnet den Bestand nach fünf Aufgabenbereichen:
Übersicht, Hören, Spielen, Aufnehmen und System. Klänge gehören zum Spielen;
Signalweg, Geräte, Diagnose, Deployment und Browserkonfiguration sind unter
System gebündelt. Ein loopback-gebundener Control-Dienst bleibt
Zustandsautorität; der Browser verarbeitet kein kritisches Audio.

Die Oberfläche trennt beobachtete Laufzeit, gewünschte Konfiguration, physisch
ungeprüfte Fakten und tatsächliche Ausführungsautorität. Ausgeschaltete Geräte
sind daher ein neutraler Vor-Ort-Zustand und kein pauschaler Systemfehler.
Profilplanung ist read-only; Dauersong und Profil-Apply bleiben sichtbar
fail-closed. Das Dashboard zeigt zusätzlich, ob die laufende, unveränderliche
Releasefassung mit dem letzten sicheren Auto-Deploy-Beleg synchron ist, ohne
private Deploypfade offenzulegen.

Der gehärtete Aufnahme-Kern ist per CLI vorhanden, aber weiterhin an die
physischen Mikrofonfakten, die eindeutige MOTU-Quelle und das bestandene
Stimmpegel-Gate gebunden. Er plant read-only, startet nur mit dem exakten
Planhash, identifiziert seinen Prozess über PID, Startzeit und Executable,
begrenzt Laufzeit und Dateigröße und veröffentlicht die WAV-Datei ohne
Überschreiben. Ein Abbruch oder Rechnerausfall bleibt über `status` und
`recover` nachvollziehbar; die Audiozentrale erhält erst nach physischer
Abnahme Aufnahme-Schaltflächen.

Der neue `desktop-mixed`-Transitionsvertrag ist zunächst nur per CLI
freigegeben, bis Browserbestätigung, Recoveryanzeige und Laborabnahme separat
bestehen. Er schaltet keine Aufnahme und keinen Produktions-Mixgraph.

Der [Produktplan Audiozentrale v2](docs/plans/audiozentrale-product-v2.md)
ordnet die längerfristige Produktoberfläche neu. Er ersetzt weder die heutige
System- und Fallbackoberfläche noch ihre Sicherheitsverträge. Der Plan setzt auf
ein aktives Setup mit geordneten Signalbahnen, echte Live-Telemetrie und drei
progressive Darstellungsebenen. KI, ein früher Pluginhost, ein freier
Modulargraph und eine vollständige DAW liegen ausdrücklich außerhalb des
bewiesenen Kerns.

Das [maschinenlesbare Produkt- und Zustandsmodell](docs/audiozentrale-product-model.md)
bindet diesen Plan an die Objekte Setup, Signalbahn, Modul, Verknüpfung, Szene
und Take. Es erzwingt höchstens ein aktives Setup, lineare typisierte
Modulketten, unveränderliche Takes und die Trennung von Wahrheitsebenen und
Darstellungstiefe. Freie Ports, Routingzyklen, Skripte, Timeline, Comping sowie
Modulation von Aufnahme, Ausgabe, Masterpegel oder Sicherheitsfunktionen werden
fail-closed abgewiesen. Der Vertrag ist noch read-only und erweitert keine
Audioautorität.

## iPad und PWA v1

Die [iPad- und PWA-Fläche v1](docs/ipad-pwa-v1.md) macht die bestehende
kanonische Oberfläche installierbar — ohne zweite App, ohne Framework, ohne
neue Abhängigkeit. Sie ergänzt Manifest, Apple-Metadaten, 180/192/512-Symbole,
sichere Bereiche, 44-px-Berührungsziele und einen App-Shell-Service-Worker, der
gleichherkünftige `/api/`-Anfragen strikt network-only behandelt: kein Cache,
kein Rückfall, kein Replay, keine Queue, kein Background Sync.

Zwei Laufzeitmodi sind ausdrücklich getrennt. `remote-audiozentrale` (Vorgabe)
behandelt das Heim-PC-Backend als autoritativ, setzt aber eine **separat zu
belegende, authentifizierte und gesicherte Fern-Oberfläche** voraus; der
heutige Loopback-Control-Dienst ist ausdrücklich **kein Ferntransport**.
`local-device` kennt nur Browserfähigkeiten, stellt keine einzige
Backendanfrage und hat keine native Autorität über MOTU, ALSA, PipeWire oder
Roland. Fähigkeiten werden fail-closed und ohne Kennungsauswertung erkannt;
`getUserMedia` und `requestMIDIAccess` werden nie automatisch aufgerufen.

Der Vertrag `inventory/audiozentrale-ipad-pwa.v1.json` hält alle
`physical_acceptance`-Felder auf `false`: weder iPad-Installation noch
Fernstrecke noch lokale Audio- oder MIDI-Hardware sind belegt.

## Dokumente

- [Repository-Entscheidung](docs/decisions/0001-new-audio-repository.md)
- [sfizz-Störfall](docs/incidents/2026-07-26-sfizz-stdin-eof-loop.md)
- [Buckelwal Live Voice](docs/experiments/buckelwal-live-voice-v1.md)
- [Buckelwal-Lernlektion v1](docs/experiments/buckelwal-learning-lesson-v1.md)
- [Wissen über Buckelwalstimme und Buckelwalgesang](docs/knowledge/buckelwal-stimme-und-gesang.md)
- [Organische Buckelwalstimme und A/B-Vergleich](docs/experiments/buckelwal-organic-v5.md)
- [Buckelwal-Samplequellen und Lizenzen](assets/whale-sources/README.md)
- [Unabhängiger Buckelwal-Evaluationssatz](assets/whale-sources/evaluation/NOTICE.md)
- [Bewertung von hausKI-audio](docs/migration/hauski-audio-assessment.md)
- [Plan zur Audio-Neukonfiguration](docs/plans/audio-configuration-redesign-v1.md)
- [Produktplan Audiozentrale v2](docs/plans/audiozentrale-product-v2.md)
- [Audiozentrale-Produkt- und Zustandsmodell](docs/audiozentrale-product-model.md)
- [Plan der durchgehend spielbaren Buckelwalstimme](docs/plans/buckelwal-continuous-voice-v1.md)
- [Spezifikation der lokalen Audiozentrale](docs/plans/local-audio-control-ui-v1.md)
- [iPad- und PWA-Fläche v1](docs/ipad-pwa-v1.md)
- [Deployment der Audiozentrale](docs/control-deployment.md)
- [Profiltransition und Recovery](docs/profile-transition-workflow.md)
- [Audio-Sicherheitsregeln](policy/audio-safety.md)
- [Read-only Baselines](docs/baselines/README.md)
- [Systemwahrheit und Drift](docs/system-truth-workflow.md)
- [Signalweg und physische Wahrheit](docs/signal-path.md)
- [Referenzpegel](docs/reference-levels.md)
- [Round-Trip-Latenz](docs/latency.md)
- [Physische Verifikation](docs/physical-verification-workflow.md)
- [Kalibrier- und Messworkflow](docs/calibration-workflow.md)
- [Gehärtete Aufnahmesitzungen](docs/recording-session-workflow.md)
- [Verwalteter Produktions-Mixgraph](docs/production-mix-graph.md)
- [Read-only Profilplanung](docs/profile-planning.md)
- [Aktuelle Heim-PC-Baseline](baselines/heim-pc/2026-07-27/README.md)

## Prüfung

```bash
just check
./scripts/audio-product-model check
./scripts/audio-product-model validate profiles/audiozentrale-workspace.example.v1.json
./scripts/audio-doctor --pretty
./scripts/audio-truth capture --output ~/.local/state/audio/truth/latest.v1.json
./scripts/audio-truth verify ~/.local/state/audio/truth/latest.v1.json
./scripts/audio-physical status
./scripts/audio-plan desktop-mixed
./scripts/audio-transition diff desktop-mixed
./scripts/audio-transition status
./scripts/audio-production-mix plan
./scripts/audio-record plan stimme-01.wav --session-type voice-recording --maximum-seconds 1800
./scripts/audio-record plan roland-01.wav --session-type roland-audio-recording --maximum-seconds 1800
./scripts/audio-record plan produktion-01.wav --session-type production-mix-recording --maximum-seconds 1800
# Mixgraph und Aufnahme starten jeweils erst nach Prüfung ihres eigenen Plan-Hashes.
./scripts/audio-record status
# Start erst nach bestandenem Plan mit identischem --session-type und --expected-plan-sha256.
python3 scripts/whale_live.py doctor
python3 scripts/build_whale_morph_bank.py
python3 scripts/whale_live.py start --voice-mode morph
python3 scripts/whale_live.py mode organic
python3 scripts/whale_live.py mode realistic
python3 scripts/whale_live.py mode ufo
python3 scripts/whale_live.py toggle
python3 scripts/whale_live.py demo /tmp/buckelwal-live-voice-v1-demo.wav
./scripts/audio-control check
./scripts/audio-control start
./scripts/audio-control status
# Oberfläche danach lokal unter http://127.0.0.1:8765/
./scripts/audio-control stop
```

## Audiozentrale v2 Produktoberfläche

Siehe [`docs/audiozentrale-readonly-replay.md`](docs/audiozentrale-readonly-replay.md).
