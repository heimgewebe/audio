# ADR 0001: Neues kanonisches Audio-Repository

- Status: angenommen
- Datum: 2026-07-26
- Entscheidung: `heimgewebe/audio` wird neu angelegt

## Kontext

`heimgewebe/hausKI-audio` enthält brauchbaren Code, ist aber um Mopidy, eine
kleine HTTP-Fassade und ältere Aufnahmehelfer herum gebaut. Der heutige
Audio-Bestand umfasst zusätzlich PipeWire-Profile, MOTU M2, Roland FP-30X,
Rode NT1-A, verschiedene Wiedergabewege, Dauersong und experimentelle
Instrumente. Eine Umbenennung würde den alten Zuschnitt und seine historische
Semantik fälschlich zum Fundament des neuen Systems machen.

## Entscheidung

Ein neues Repository beginnt mit einem expliziten Systemmodell. Der Altbestand
wird nicht geforkt und nicht in voller Historie importiert. Einzelne Komponenten
dürfen später übernommen werden, wenn:

1. ihr Nutzen gegen den aktuellen Zielvertrag belegt ist;
2. Herkunft und ursprünglicher Commit dokumentiert sind;
3. Tests vor und nach der Übernahme bestehen;
4. kein veralteter Betriebsvertrag mitgeschleppt wird.

## Folgen

- `hausKI-audio` bleibt vorerst erhalten und unverändert.
- Das neue Repository verändert im ersten Schritt keine Livekonfiguration.
- Große Samples, Aufnahmen, Logs, Caches und Geheimnisse bleiben außerhalb von
  Git und werden später nur durch Manifeste und Hashes referenziert.
- Eine Archivierung des Alt-Repositories ist erst nach abgeschlossener,
  belegter Migration zulässig.
