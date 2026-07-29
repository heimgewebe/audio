# audio

Kanonisches Repository für die Audio-Konfiguration des Heim-PC, Aufnahme,
Wiedergabe, Instrumente und experimentelle Musiksysteme.

## Aktueller Zustand

Dieses Repository beginnt bewusst mit **Planung, Sicherheitsregeln und
Betriebsverträgen**. Produktive Audiokonfiguration wird ausschließlich über
spätere profilgebundene Apply- und Rollback-Verträge verändert.

Der Altbestand `heimgewebe/hausKI-audio` bleibt zunächst unverändert und dient
nur als geprüfte Ideen- und Verhaltensquelle. Seine Implementierung und Historie
werden standardmäßig nicht übernommen; nützliche Anforderungen und Testabsichten
werden gegen die neuen Verträge neu programmiert.

Als erstes ausführbares Klangexperiment enthält das Repository
**Buckelwal Live Voice**. Standard ist `morph`: eine monophone,
quellengestützte Walstimme über alle 88 chromatischen Tasten von A0 bis C8.
Kurze periodische Stimmzyklen aus lizenzierten Buckelwalaufnahmen werden
phasengleich gemittelt, bandbegrenzt und stufenlos gemorpht. Es gibt keine
Samplezonen, Presets, Steuertasten oder permanente Rauschschicht. Die früheren
Modi `realistic` (Aufnahmephrasen) und `ufo` bleiben als Vergleiche erhalten.

## Bereiche

- reproduzierbare Audio- und MIDI-Profile
- MOTU M2, Roland FP-30X, Rode NT1-A und Wiedergabeketten
- Qobuz: gemischter und exklusiver Betrieb
- Aufnahme und Live-Monitoring
- Dauersong und spätere Klangexperimente
- Doctor, Wahrheitskette, Drift, Diff, Apply, Rollback und Recovery
- harte Grenzen für Logs, Prozesse, große Assets und Geheimnisse

## Dokumente

- [Repository-Entscheidung](docs/decisions/0001-new-audio-repository.md)
- [sfizz-Störfall](docs/incidents/2026-07-26-sfizz-stdin-eof-loop.md)
- [Buckelwal Live Voice](docs/experiments/buckelwal-live-voice-v1.md)
- [Buckelwal-Samplequellen und Lizenzen](assets/whale-sources/README.md)
- [Bewertung von hausKI-audio](docs/migration/hauski-audio-assessment.md)
- [Plan zur Audio-Neukonfiguration](docs/plans/audio-configuration-redesign-v1.md)
- [Plan der durchgehend spielbaren Buckelwalstimme](docs/plans/buckelwal-continuous-voice-v1.md)
- [Audio-Sicherheitsregeln](policy/audio-safety.md)
- [Read-only Baselines](docs/baselines/README.md)
- [Systemwahrheit und Drift](docs/system-truth-workflow.md)
- [Signalweg und physische Wahrheit](docs/signal-path.md)
- [Referenzpegel](docs/reference-levels.md)
- [Round-Trip-Latenz](docs/latency.md)
- [Physische Verifikation](docs/physical-verification-workflow.md)
- [Kalibrier- und Messworkflow](docs/calibration-workflow.md)
- [Read-only Profilplanung](docs/profile-planning.md)
- [Aktuelle Heim-PC-Baseline](baselines/heim-pc/2026-07-27/README.md)

## Prüfung

```bash
just check
./scripts/audio-doctor --pretty
./scripts/audio-truth capture --output ~/.local/state/audio/truth/latest.v1.json
./scripts/audio-truth verify ~/.local/state/audio/truth/latest.v1.json
./scripts/audio-physical status
./scripts/audio-plan desktop-mixed
python3 scripts/whale_live.py doctor
python3 scripts/build_whale_morph_bank.py
python3 scripts/whale_live.py start --voice-mode morph
python3 scripts/whale_live.py mode realistic
python3 scripts/whale_live.py mode ufo
python3 scripts/whale_live.py toggle
python3 scripts/whale_live.py demo /tmp/buckelwal-live-voice-v1-demo.wav
```
