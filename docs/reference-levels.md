# Referenzpegel

Die kanonischen Zielbereiche stehen in `profiles/reference-levels.v1.json`.
Sie definieren Headroom und sichere Abläufe, aber keine erfundenen Knopfpositionen.

## Wiedergabe

- Kalibriersignal: 1 kHz bei **−20 dBFS**.
- Das Repository erzeugt keine Testsignale oberhalb **−12 dBFS**.
- Ein Testsignal wird nur als WAV-Datei erzeugt; keine Software spielt es automatisch ab.
- Der Generator ist auf 192 kHz und 2.000.000 Frames begrenzt.
- Vor einer Kalibrierung beginnen Lake People und Pioneer bei minimaler Lautstärke.

## Aufnahme

Für Stimme und Instrumente werden typische Spitzen zwischen **−12 und −6 dBFS**
angestrebt. Der Gain wird anhand der lautesten realistischen Darbietung gesetzt.
0 dBFS ist die Clippinggrenze, kein Zielwert.

## Rode NT1-A

48 V ist erforderlich, aber nur physisch verifizierbar. Vor dem Einschalten werden
Monitoring und Gain abgesenkt; danach wird der Gain schrittweise aufgebaut.
