# Signalweg und physische Wahrheit

`inventory/signal-path.v1.json` trennt vier Evidenzklassen:

- **observed/software-observable**: aktuell vom Heim-PC messbar;
- **human-visual**: durch ausdrückliche menschliche Sichtprüfung belegt;
- **partial-human-visual**: ein Teil der physischen Verbindung ist visuell belegt, offene Details bleiben unbekannt;
- **user-declared/unknown**: bekannt oder erwartet, aber nicht hinreichend technisch bzw. visuell belegt.

Der MOTU M2 exponiert unter Linux keine ALSA-Regler für Eingangsgain, Monitor-Mix
oder 48 V. Der Doctor darf diese Zustände daher nicht aus Gerätesichtbarkeit,
Aufnahmepegeln oder Prozesszuständen ableiten.

## Aktuell belegbare digitale Kette

Der Heim-PC läuft unter Linux mit PipeWire. Für den Referenz-Hörpfad gilt daher
nicht ein Windows-/WASAPI-/ASIO-Modell, sondern der tatsächlich beobachtete
`Qobuz/Mopidy → PipeWire → USB → MOTU M2`-Pfad. Qobuz über Mopidy und den
Pulse-kompatiblen PipeWire-Pfad belegt einen funktionalen Mischpfad, aber für sich
allein noch keine bitgenaue oder track-native Wiedergabe. Dafür gelten weiterhin
die separaten Gates `qobuz-rate-proof` und `rate-policy-decision`.

## Fotografisch geprüfte Ausgangstopologie vom 20. August 2026

Der Wiedergabepfad verzweigt physisch am MOTU M2:

```text
Heim-PC / Qobuz
      ↓ USB
    MOTU M2
      ├─ Monitor Out: MOTU-seitig 6,3-mm-TRS belegt → Lake People G111 Mk II → Focal Clear Mg
      └─ 2× RCA/Cinch fotografisch belegt → Pioneer VSX-830-K → Lautsprecher
```

Für den Lake-People-Zweig ist mindestens ein echter TRS-Stecker mit zwei
Isolierringen am MOTU fotografisch belegt. In diesem Ausgangskontext steht TRS
für eine symmetrische Mono-Line-Verbindung (Tip +, Ring −, Sleeve Schirm), nicht
für Stereo auf einem Kabel. Für Stereo werden zwei Kanäle benötigt. Noch nicht
separat visuell abgeschlossen sind der zweite MOTU-TRS-Ausgang, die exakte
Links/Rechts-Zuordnung, beide Verstärker-seitigen Anschlüsse sowie der
Input-Selector des G111. Es gibt aktuell keinen positiven Hinweis auf einen
Verkabelungsfehler.

Für den Pioneer-Zweig sind die beiden belegten RCA/Cinch-Ausgänge am MOTU und
deren vom Nutzer identifizierte Führung zum Pioneer belegt. Der Pioneer liegt
damit nicht im Focal-Kopfhörerpfad. Receiver-Eingang, Hörmodus, Referenzpegel und
die konkrete Lautsprecherzuordnung bleiben getrennte offene Fakten.

Der Herstellervertrag ergänzt eine wichtige Systemgrenze: Laut `MOTU M Series
User Guide` spiegeln die beiden RCA-Ausgänge des M2 das Signal der entsprechenden
6,3-mm-Monitor-Ausgänge. Der große `MONITOR`-Regler steuert die rückseitigen
Monitor-Ausgänge. Lake People und Pioneer sind daher zwei physische Abgriffe
desselben Monitorpaares, nicht zwei unabhängig adressierbare Software-Ausgänge.
Eine künftige Pegelkalibrierung muss beide angeschlossenen Ziele berücksichtigen;
die Audiozentrale darf aus der gezeichneten Verzweigung keinen separaten Mix oder
eine unabhängige Ausgangswahl ableiten.

Ein Kabelwechsel ist aus dieser Topologie allein kein begründeter Klanghebel.
Weitere Kabelarbeit ist erst bei einem realen Symptom wie Brummen,
Kanalunterbrechung oder Fehlkontakt relevant.

## Physisch zu bestätigen

Die Vorlage `inventory/physical-verification.v1.json` nennt die noch offenen
Regler, Eingänge, Kabel und Betriebsarten. `null` ist ein Schutzwert: unbekannt,
nicht automatisch aus oder sicher. Neue Fotoevidenz schließt nur die jeweils
beobachteten Teilfragen; sie darf insbesondere Gain-Stellung, Lake-Input-Selector
oder Referenzlautstärke nicht indirekt behaupten.
