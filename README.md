# audio

Kanonisches Repository für die Audio-Konfiguration des Heim-PC, Aufnahme,
Wiedergabe, Instrumente und experimentelle Musiksysteme.

## Aktueller Zustand

Dieses Repository beginnt bewusst mit **Planung, Sicherheitsregeln und
Betriebsverträgen**. Es verändert noch keine produktive Audiokonfiguration.

Der Altbestand `heimgewebe/hausKI-audio` bleibt zunächst unverändert und dient
nur als geprüfte Spenderquelle. Code wird später selektiv und mit Herkunft
übernommen; seine Historie wird nicht blind zur Grundlage des neuen Systems.

Als erstes ausführbares Klangexperiment enthält das Repository
**Buckelwal Live Voice**. Standard ist nun eine lokal gespeicherte,
lizenzdokumentierte Samplebank aus echten Buckelwalaufnahmen. 19 natürliche
Phrasen werden über 27 Tastaturzonen mit höchstens vier Halbtönen Resampling
spielbar gemacht. Der frühere synthetische Klang bleibt ausdrücklich als
separater `ufo`-Modus erhalten.

## Bereiche

- reproduzierbare Audio- und MIDI-Profile
- MOTU M2, Roland FP-30X, Rode NT1-A und Wiedergabeketten
- Qobuz: gemischter und exklusiver Betrieb
- Aufnahme und Live-Monitoring
- Dauersong und spätere Klangexperimente
- Doctor, Diff, Apply, Rollback und Recovery
- harte Grenzen für Logs, Prozesse, große Assets und Geheimnisse

## Dokumente

- [Repository-Entscheidung](docs/decisions/0001-new-audio-repository.md)
- [sfizz-Störfall](docs/incidents/2026-07-26-sfizz-stdin-eof-loop.md)
- [Buckelwal Live Voice](docs/experiments/buckelwal-live-voice-v1.md)
- [Buckelwal-Samplequellen und Lizenzen](assets/whale-sources/README.md)
- [Bewertung von hausKI-audio](docs/migration/hauski-audio-assessment.md)
- [Plan zur Audio-Neukonfiguration](docs/plans/audio-configuration-redesign-v1.md)
- [Audio-Sicherheitsregeln](policy/audio-safety.md)
- [Read-only Baselines](docs/baselines/README.md)
- [Signalweg und physische Wahrheit](docs/signal-path.md)
- [Referenzpegel](docs/reference-levels.md)
- [Round-Trip-Latenz](docs/latency.md)
- [Physische Verifikation](docs/physical-verification-workflow.md)
- [Kalibrier- und Messworkflow](docs/calibration-workflow.md)
- [Read-only Profilplanung](docs/profile-planning.md)
- [Aktuelle Heim-PC-Baseline](baselines/heim-pc/2026-07-27/README.md)

## Prüfung

```bash
bash tests/test-audio-safety.sh
bash scripts/check-audio-safety .
python3 -m unittest discover -s tests -v
./scripts/audio-doctor --pretty
./scripts/audio-physical status
./scripts/audio-plan desktop-mixed
python3 scripts/whale_live.py doctor
python3 scripts/whale_live.py start --voice-mode realistic
python3 scripts/whale_live.py mode ufo
python3 scripts/whale_live.py toggle
python3 scripts/whale_live.py demo /tmp/buckelwal-live-voice-v1-demo.wav
```
