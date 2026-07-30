# Buckelwal Organic Voice v2

## Ziel

Der Modus `organic` soll die quellengestützte, chromatisch spielbare
88-Tasten-Morph-Stimme weniger orgelhaft und weniger statisch wirken lassen,
ohne fertige Aufnahmephrasen, Samplezonen, Presets oder eine permanente
Rauschschicht einzuführen.

Er bleibt ein Musikinstrument und ist weder ein biologisches Stimmapparatmodell
noch ein Artenklassifikator.

## Akustische Ableitung

Die Umsetzung folgt vier belegten Eigenschaften echter Buckelwalgesänge:

1. Einheiten werden zu Phrasen und wiederholten Themen organisiert; innerhalb
   derselben Phrasenart existieren Varianten.
2. Sänger verändern Einheiten und Konturen auch innerhalb einer Sitzung, statt
   nur identische Tonbausteine abzuspielen.
3. Buckelwalvokalisationen enthalten neben harmonischen Anteilen abrupte
   Frequenzsprünge, Subharmonik und deterministisch-chaotische Abschnitte.
4. Das Repertoire reicht von diskreten Pulsen bis zu kontinuierlichen tonalen
   Signalen; Resonanzräume tragen zur spektralen Form bei.

Primärliteratur:

- Cazau et al. (2016), *A study of vocal nonlinearities in humpback whale songs*,
  Scientific Reports 6:31660, DOI 10.1038/srep31660.
- Mercado et al. (2010), *Sound production by singing humpback whales*,
  Journal of the Acoustical Society of America 127, DOI 10.1121/1.3309453.
- Cholewiak et al. (2018), *Stereotypic and complex phrase types provide
  structural evidence for a multi-message display in humpback whales*,
  Animal Behaviour 137.
- Mercado (2022), *Cognitive control of song production by humpback whales*,
  Learning & Behavior, PMID 36058997.

## Signalweg

`OrganicWhaleMorphVoice` übernimmt die vorhandene `WhaleMorphVoice` und ergänzt
nur deterministische, signalgebundene Prozesse:

- Formantträgheit gegenüber der gespielten Tonhöhe;
- eine schwache Subharmonik für Körperanteil;
- gestenabhängige, rasch abklingende Frequenzsprünge;
- geglättete logistische Mikroinstabilität;
- signalgebundene Kantenfaltung statt unabhängig erzeugtem Rauschen;
- zwei gedämpfte, vom Quellsignal angeregte Resonanzmoden;
- eine endliche modale Nachspur, danach wieder exakte digitale Stille.

Alle Tasten bleiben Standardnoten in 12-TET mit A4 = 440 Hz. Der Modus ist
monophon und behält Last-Note-Priority, Sustain, Expression, Modulation,
Soft-Pedal/Distanz, Pitch Bend und Panic bei.

## Selbst gespielte Vergleichsphrase

`scripts/compare_whale_organic.py` erzeugt eine 17-sekündige Phrase nur aus
Roland-artigen MIDI-Gesten:

- weicher tiefer Ruf;
- kontinuierlicher Auf- und Abstieg;
- wiederholte gepulste Einheit;
- längerer modulierter Ruf mit Sustain und Legato;
- leiser Schlussgroan.

Es wird keine echte Phrase in die Synthese kopiert. Dieselbe Geste wird einmal
mit `morph` und einmal mit `organic` gerendert.

## Vergleich mit echten Quellen

Der dependency-freie Prüfer vergleicht die Spielphrase mit sechs bereits im
Repository quellen- und hashgebundenen Buckelwalclips. Er misst Konturspanne,
Periodizität, Rauigkeit, Hüllkurvenvariation, hochfrequenten Strukturanteil und
Einheitsdauer. Der Wert ist nur ein Regressionsindikator, kein Wahrheits- oder
Realismusbeweis. Die degenerierte Bewegungsrate der einfachen Pitchanalyse wird
weiterhin berichtet, aber nicht in den Skalar einbezogen.

| Merkmal | `morph` | `organic` v2 | Referenzmedian |
|---|---:|---:|---:|
| Ähnlichkeitsindikator 0–1 | 0,314 | 0,387 | – |
| Periodizität | 0,970 | 0,950 | 0,717 |
| Rauigkeit | 0,030 | 0,050 | 0,283 |
| Hochfrequenzanteil | 0,00285 | 0,00997 | 0,01431 |
| Konturspanne, Halbtöne | 11,41 | 20,23 | 43,40 |
| Hüllkurven-CV | 0,593 | 0,575 | 0,660 |
| mediane Einheitsdauer, s | 2,875 | 2,925 | 4,575 |

Der Organic-Modus verbessert den Skalar relativ um rund 23 Prozent. Die
größten Fortschritte liegen beim spektralen Strukturanteil, bei der
Konturspanne und bei der signalgebundenen Rauigkeit. Die Periodizität bleibt absichtlich höher als im Rohmaterial, um
die Stimme tonal und auf allen 88 Tasten kontrollierbar zu halten. Die einfache
Pitchanalyse kann bei rauen Mehrkomponentensignalen Oktav- und
Subharmonikfehler enthalten; absolute Referenzwerte dürfen daher nicht als
biologische Zielnorm interpretiert werden.

## Reproduktion

```bash
python3 scripts/compare_whale_organic.py \
  --engine morph \
  --output /tmp/buckelwal-morph.wav \
  --report /tmp/buckelwal-morph.json

python3 scripts/compare_whale_organic.py \
  --engine organic \
  --output /tmp/buckelwal-organic.wav \
  --report /tmp/buckelwal-organic.json

python3 scripts/whale_live.py mode organic
```

## Physisches Gate

Die Messung ersetzt nicht die Abnahme am Roland FP-30X über die MOTU M2.
Bewertet werden müssen insbesondere:

- Wal statt Orgel, Buzz oder UFO;
- organische, aber nicht kratzige Rauigkeit;
- plausibles Verhalten bei kurzen, langen und wiederholten Tönen;
- glaubwürdige Sprünge bei Legato und Anschlagsdynamik;
- kein Dauerrauschen und keine hängenbleibende modale Nachspur.
