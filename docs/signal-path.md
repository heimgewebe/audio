# Signalweg und physische Wahrheit

`inventory/signal-path.v1.json` trennt drei Evidenzklassen:

- **observed/software-observable**: aktuell vom Heim-PC messbar;
- **user-declared**: Gerät oder Verbindung ist bekannt, aber nicht technisch rückgelesen;
- **unknown**: darf weder als aktiv noch als sicher interpretiert werden.

Der MOTU M2 exponiert unter Linux keine ALSA-Regler für Eingangsgain, Monitor-Mix
oder 48 V. Der Doctor darf diese Zustände daher nicht aus Gerätesichtbarkeit,
Aufnahmepegeln oder Prozesszuständen ableiten.

## Aktuell belegbare digitale Kette

`Roland FP-30X → USB → PipeWire → MOTU M2`

Qobuz läuft derzeit über Mopidy und den Pulse-kompatiblen PipeWire-Pfad. Dies
belegt einen funktionalen Mischpfad, aber keine bitgenaue Wiedergabe.

## Physisch zu bestätigen

Die Vorlage `inventory/physical-verification.v1.json` nennt die noch offenen
Regler, Eingänge, Kabel und Betriebsarten. `null` ist ein Schutzwert: unbekannt,
nicht automatisch aus oder sicher.
