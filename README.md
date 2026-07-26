# audio

Kanonisches Repository für die Audio-Konfiguration des Heim-PC, Aufnahme,
Wiedergabe, Instrumente und experimentelle Musiksysteme.

## Aktueller Zustand

Dieses Repository beginnt bewusst mit **Planung, Sicherheitsregeln und
Betriebsverträgen**. Es verändert noch keine produktive Audiokonfiguration.

Der Altbestand `heimgewebe/hausKI-audio` bleibt zunächst unverändert und dient
nur als geprüfte Spenderquelle. Code wird später selektiv und mit Herkunft
übernommen; seine Historie wird nicht blind zur Grundlage des neuen Systems.

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
- [Bewertung von hausKI-audio](docs/migration/hauski-audio-assessment.md)
- [Plan zur Audio-Neukonfiguration](docs/plans/audio-configuration-redesign-v1.md)
- [Audio-Sicherheitsregeln](policy/audio-safety.md)

## Prüfung

```bash
bash tests/test-audio-safety.sh
bash scripts/check-audio-safety .
```
