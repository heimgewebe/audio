# Audio-Sicherheitsregeln

## Prozessstart

- Keine unbeaufsichtigten interaktiven Programme ohne definierten Standardinput.
- `sfizz_jack` ist in produktiven Launchern verboten.
- Jeder Langläufer benötigt eindeutige Prozessidentität, Stop-Semantik und
  Ressourcenlimits.

## Logging

- Keine unbeschränkte Umleitung mit `>> ...log`.
- Produktive Langläufer schreiben in begrenztes Journald oder einen explizit
  rotierten Logpfad.
- Pro Prozess werden maximale Größe, Rate und Aufbewahrung geprüft.

## Konfiguration

- Änderungen erfolgen aus versionierten Profilen über `plan`, `diff`, `apply`
  und `rollback`.
- Der Doctor ist standardmäßig read-only.
- Kein Profil darf globale PipeWire-Einstellungen hinterlassen, ohne den
  vorherigen Zustand wiederherzustellen.

## Assets

Samples, Aufnahmen und Master liegen nicht in Git. Manifeste binden Quelle,
Lizenz, Größe und SHA-256.
