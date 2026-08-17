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

Der Pioneer VSX-830-K nutzt ebenfalls den MOTU M2. Der nutzerdeklarierte
Wiedergabeweg lautet damit `Heim-PC / Qobuz → PipeWire → MOTU M2 → Pioneer
VSX-830-K → Lautsprecher`. Die beiden Hauptwege verzweigen sich hinter dem MOTU:
zum Lake People/Focal einerseits und zum Pioneer/Lautsprechersystem andererseits.
Als Lautsprecherbestand sind 2× ELAC FS 109.2, ein Canton Center und vier Canton
Satelliten ohne Subwoofer deklariert. Welcher MOTU-Ausgang und Kabeltyp zum
Pioneer genutzt werden, welcher Receiver-Eingang aktiv ist, wie die Lautsprecher
zugeordnet und verkabelt sind sowie Hörmodus und Referenzpegel bleiben bis zur
Vor-Ort-Prüfung ausdrücklich offen.

## Physisch zu bestätigen

Die Vorlage `inventory/physical-verification.v1.json` nennt die noch offenen
Regler, Eingänge, Kabel und Betriebsarten. `null` ist ein Schutzwert: unbekannt,
nicht automatisch aus oder sicher.
