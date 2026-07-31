# Buckelwal Organic Voice v3

## Anlass

Der erste physische Hörtest des Organic-Modus am Roland FP-30X über die MOTU M2
ergab am 31. Juli 2026 zwei klare Befunde:

- die Stimme wirkte weiterhin stark nach UFO beziehungsweise Theremin;
- die tiefen Tasten nutzten ihr Basspotenzial nicht aus.

Dieses Nutzerurteil ist für das Instrument höherrangig als der bisherige
Rohclip-Ähnlichkeitsskalar. Version 3 optimiert deshalb nicht weiter auf große
Tonhöhensweeps und maximale Rauigkeit.

## Produktgrenze

`organic` bleibt eine monophone, quellengestützte Stimme über alle 88 normalen
chromatischen Tasten. Es gibt keine Samplezonen, Presets, Steuertasten,
permanente Rauschschicht oder abgespielte Langphrase. A4 bleibt 440 Hz; Pitch
Bend, Sustain, Expression, Modulation, Distanz und Panic bleiben erhalten.

Der Modus ist ein Musikinstrument und weder ein biologisches Stimmapparatmodell
noch ein Artenklassifikator.

## Anti-UFO-Entscheidung

Die Morph-Engine besitzt bereits Einsatzkontur, Vibrato und langsame Bögen.
Version 2 modulierte zusätzlich die Zieltonhöhe um bis zu deutlich mehr als
einen Halbton und verlängerte Legato. Diese doppelte Bewegung erzeugte den
Theremincharakter.

Version 3:

- begrenzt die zusätzliche Organic-Tonhöhenbewegung auf weniger als 20 Cent;
- verkürzt Organic-Legato auf höchstens 180 Millisekunden;
- ersetzt gestenabhängige Frequenzsprünge durch kurze Pegel- und
  Klangfarbenimpulse;
- reduziert Kantenfaltung, Hochfrequenzrauigkeit und metallische Resonanzzeiten;
- nutzt zwei kürzere, tiefer abgestimmte Resonanzmoden;
- bleibt nach dem Ausklang exakt digital still.

## Tiefbassvertrag

Unterhalb ungefähr G2 wird ein eigener Basskörper stufenlos eingeblendet. Seine
stärkste Wirkung liegt in A0 bis A1 und läuft bis ungefähr G3 vollständig aus.

Der Basskörper besteht aus:

- dem tiefpassgefilterten, quellengestützten Morphsignal;
- einer an die tatsächlich gespielte Grundfrequenz gebundenen Grundwelle;
- einer schwachen zweiten Harmonischen für Wahrnehmbarkeit auf kleineren
  Wiedergabesystemen;
- einem sehr kleinen Subharmonikanteil, der den Grundton nicht ersetzt.

Er ist weder ein frei laufender Suboszillator noch eine Oktavverschiebung der
Tastatur. Jede Taste behält ihre normale musikalische Tonhöhe.

Gemessen gegen den unveränderten Morph-Modus bei gleicher Anschlagstärke und
Masterverstärkung:

| Taste | Frequenz | Energie unter 120 Hz gegenüber `morph` |
|---|---:|---:|
| A0 / MIDI 21 | 27,5 Hz | 1,78× |
| A1 / MIDI 33 | 55 Hz | 2,27× |
| A2 / MIDI 45 | 110 Hz | 1,12× |

Mittel- und Hochregister werden nicht bassverstärkt.

## Vergleich mit echten Quellen

`scripts/compare_whale_organic.py` rendert weiterhin dieselbe 17-sekündige
Spielphrase mit `morph` und `organic` und vergleicht sie mit sechs
hashgebundenen echten Buckelwalclips.

| Merkmal | `morph` | `organic` v2 | `organic` v3 |
|---|---:|---:|---:|
| Rohclip-Ähnlichkeitsindikator | 0,314 | 0,387 | 0,308 |
| Konturspanne, Halbtöne | 11,41 | 20,23 | 11,41 |
| Periodizität | 0,970 | 0,950 | 0,969 |
| Rauigkeit | 0,030 | 0,050 | 0,031 |
| Hochfrequenzanteil | 0,00285 | 0,00997 | 0,00342 |
| Hüllkurven-CV | 0,593 | 0,575 | 0,540 |
| mediane Einheitsdauer, s | 2,875 | 2,925 | 2,900 |

Der niedrigere v3-Skalar ist erwartet: Das einfache Maß belohnt große
Konturspanne und Rohmaterial-Rauigkeit. Der physische Test zeigte, dass diese
Optimierung beim spielbaren Instrument als UFO wahrgenommen wurde. Der Skalar
bleibt als Diagnose erhalten, ist aber kein Freigabegate mehr.

## Automatisierte Gates

- alle 88 Zieltonhöhen bleiben exakt chromatisch;
- zusätzliche Organic-Pitchbewegung bleibt unter 20 Cent;
- Organic-Legato bleibt bei höchstens 180 Millisekunden;
- A0 besitzt mindestens 1,55× und A1 mindestens 1,80× die Morph-Energie unter
  120 Hz;
- Mittelregister bleibt leicht texturiert, aber nicht stark buzzy;
- Ausgabe bleibt auf 0,25 begrenzt;
- Renderausgabe und Zustand bleiben unabhängig von Chunkgrößen bitidentisch;
- Controller können keine modulierte Zwischenfrequenz als neuen Grundton
  übernehmen;
- Leerlauf und beendeter Ausklang sind exakt still;
- eine Sekunde Audio bleibt mit deutlicher Offline-Echtzeitreserve renderbar.

## Reproduktion

```bash
python3 scripts/compare_whale_organic.py \
  --engine morph \
  --output /tmp/buckelwal-morph.wav \
  --report /tmp/buckelwal-morph.json

python3 scripts/compare_whale_organic.py \
  --engine organic \
  --output /tmp/buckelwal-organic-v3.wav \
  --report /tmp/buckelwal-organic-v3.json

python3 scripts/whale_live.py mode organic
```

## Physisches Gate

Die nächste Abnahme bewertet primär:

- deutlich weniger UFO/Theremin als v2;
- körperhaften, aber nicht dröhnenden Bass in A0 bis A1;
- saubere Tonhöhenwahrnehmung trotz Basskörper;
- kurze, natürliche Übergänge statt langer Sweeps;
- Wal statt Orgel, Buzz oder Synthesizer;
- kein Dauerrauschen und keine hängenbleibende Resonanzspur.
