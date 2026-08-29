# Buckelwal Two-Clock Low-Register Probe v1

Stand: 29. August 2026

## Ausgangsbefund

Im tiefen Register klingt das periodische Knattern sowohl in `morph` als auch in
`organic` hörbar schneller und heller als in den gebundenen realen Quellen.
Der gemeinsame Ursprung liegt in der Morph-Anregung: mehrere phasengleiche
Quellperioden werden zu genau einer Wavetable verdichtet und diese vollständige
reiche Quellperiode wird anschließend mit der MIDI-Frequenz wiederholt.

Das ist unkritisch, wenn gemessene Quellfrequenz und Tastenton praktisch
übereinstimmen. Es ist problematisch, wenn die zugewiesene Taste deutlich über
der gemessenen Quellfrequenz liegt, weil dann gleichzeitig

- die Wiederholungsrate der Quellstruktur und
- sämtliche darin enthaltenen harmonischen Texturanteile

nach oben transponiert werden.

## Falsifikationsfall C2

Der Morph-Anchor auf MIDI 36 stammt aus `humpback-moo-nps-03`.

- MIDI-C2: 65.40639132514966 Hz
- gemessene Quellfrequenz: 33.264033264033266 Hz
- Verhältnis Pitch / Source-Clock: rund 1.9663

Der Legacy-Pfad spielt deshalb die gesamte Quellperiode fast doppelt so schnell
ab wie sie im gebundenen Quellfenster gemessen wurde.

A0 dient als Negativkontrolle:

- MIDI-A0: 27.5 Hz
- Quellfrequenz: 28.38557066824364 Hz
- Verhältnis: rund 0.9688

Dort besteht kein Overclocking und der Probe darf den Render nicht verändern.

## Experiment

`scripts/whale_two_clock_probe.py` trennt ausschließlich für den Offline-Test
zwei Uhren:

1. Ein sauberer, fundamental-only Carrier bleibt exakt an der MIDI-Frequenz.
2. Die reiche source-derived Wavetable läuft auf einer separat aus dem
   Morph-Manifest gelesenen und logarithmisch interpolierten Source-Clock.

Die Trennung wird nur eingeblendet, wenn die MIDI-Frequenz die Source-Clock um
mehr als zehn Prozent überschreitet. Zwischen Verhältnis 1.10 und 2.00 steigt
die Einblendung kontinuierlich von null auf eins. Dadurch bleibt A0 exakt
unverändert und C2 bildet den starken Falsifikationsfall.

Dies ist keine produktive Engine und keine Default-Änderung.

## Reproduzierbare Evidenz

Der PR-Workflow `Whale Two Clock Probe` erzeugt jeweils Legacy- und Kandidaten-
WAVs für MIDI 21, 36 und 48 sowie `report.json` als kurzlebiges Actions-Artefakt.

Für C2 ergab der zweite Probe-Render:

- `decoupling_amount`: 0.9625329324581233
- Legacy-Differenzenergie: 0.0028247915150145506
- Two-Clock-Differenzenergie: 0.0001370242109141062
- Kandidat / Legacy: **0.0485077253262**

Der dependency-freie Differenzenergie-Proxy fällt damit auf rund **4.85 %** des
Legacy-Werts. Er ist kein psychoakustisches Wahrnehmungsmaß, aber ein direktes,
reproduzierbares Maß dafür, dass die schnelle Sample-zu-Sample-Struktur im
kritischen C2-Fall stark reduziert wurde.

A0 bleibt im selben Report bitidentisch (`render_changed=false`, Verhältnis
1.0). Bei C3 ist die Änderung mit rund 0.953 des Legacy-Proxys absichtlich klein,
weil dort Pitch und Source-Clock wesentlich näher beieinander liegen.

## Zusätzliche Diagnose

Eine separate FFT-Prüfung des unveränderten CI-Artefakts zeigte für C2 im Band
1–1000 Hz einen Spektralschwerpunkt von ungefähr 291 Hz im Legacy-Render und
ungefähr 74 Hz im Two-Clock-Render. Diese Zahl ist ergänzende Diagnose und kein
Release-Gate; das Repository-Gate bleibt der dependency-freie Proxy-Test.

## Interpretation

Die Hypothese ist damit technisch stark gestützt:

> Das tiefe Knattern wird nicht primär durch zu hohe 2–8-Hz-Pulsparameter
> verursacht. Ein wesentlicher Anteil entsteht, weil die vollständige reiche
> Morph-Quellperiode mit der MIDI-Tonhöhe beschleunigt wird.

`organic` erbt diesen Fehler, weil sein Source-Filter- und Artikulationspfad auf
der Morph-Anregung aufsetzt. Eine alleinige Reduktion von Organic-Pulsraten wäre
daher kein kausaler Fix.

## Noch nicht bewiesen

Der Probe belegt nicht:

- dass das Mischungsverhältnis 0.55 perceptuell optimal ist;
- dass die gleiche Strategie in jedem Register oder für jede Quellfamilie gilt;
- dass die produktive Morph-Engine bereits umgestellt werden darf;
- biologische Gleichheit zur Walstimme.

Vor einer Runtime-Änderung ist ein echter Hörvergleich der erzeugten A/B-Dateien
auf dem Referenzpfad sowie ein Integrationsdesign nötig, das `morph` und
`organic` gemeinsam korrigiert, ohne den 88-Tasten-Pitch-Vertrag zu brechen.
