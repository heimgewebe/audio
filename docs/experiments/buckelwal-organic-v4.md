# Buckelwal Organic Voice v4

## Anlass

Version 3 schloss die großen UFO-/Theremin-Sweeps und ergänzte den Tiefbass.
Eine anschließend selbst komponierte, 34-sekündige Phrase zeigte objektiv eine
brauchbare Makrostruktur, aber weiterhin eine zu gleichmäßige Stimme:

- lange Abschnitte blieben im selben tonalen Zustand;
- die Periodizität war höher als in den echten Quellen;
- kurzzeitige raue und gebrochene Einheiten fehlten weitgehend;
- die Hüllkurve war weniger gepulst als der Referenzmedian.

Da der Nutzer am 1. August 2026 nicht am Heim-PC, sondern am iPad arbeitete,
erfolgte keine physische Wiedergabe. Version 4 wird ausschließlich durch
reproduzierbare Offline- und Repositoryprüfungen freigegeben. Eine spätere
Hörabnahme bleibt ein separates Gate.

## Produktgrenze

`organic` bleibt eine monophone, quellengestützte Stimme über 88 normale
chromatische Tasten. Es gibt weiterhin keine Samplezonen, Presets, Steuertasten,
permanente Rauschschicht oder abgespielte Langphrase. A4 bleibt 440 Hz. Die
zusätzliche Tonhöhenbewegung bleibt unter 20 Cent; Organic-Legato bleibt auf
höchstens 180 Millisekunden begrenzt.

## Zeitliche Artikulationszustände

Jeder neu angeschlagene, abgesetzte Ruf erhält aus Note, Anschlag und
Phrasenzähler reproduzierbar eines von vier achtteiligen Mustern. Die Segmente
sind 0,70 bis 0,96 Sekunden lang und werden über 140 Millisekunden
übergeblendet.

Die vier Zustände sind:

- **tonal:** unveränderte quellengestützte Morphstimme;
- **gepulst:** begrenzte Hüllkurvenbewegung ohne neue Tonhöhenkontur;
- **rau:** stärker signalgekoppelte Kanten und kurze knarrende Anteile;
- **gebrochen:** gepulste Subharmonik und unregelmäßiger Quellanteil.

Alle Zusätze werden aus dem vorhandenen Audiosignal angeregt. Es existiert kein
unabhängiger Rauschgenerator. Rauere Zustände ersetzen einen Teil des glatten
Quellsignals, statt nur zusätzliche Energie aufzuschichten.

## Akustischer Zustandsprüfer

`scripts/compare_whale_organic.py` wertet neben globalen Mittelwerten nun
140-Millisekunden-Fenster im Abstand von 50 Millisekunden aus. Die Zustände
werden aus Periodizität und hochfrequenter Differenzenergie des resultierenden
Audios klassifiziert. Interne Synthesizerzustände fließen nicht in die
Bewertung ein.

Gemessen wird:

- Anteil tonal, gemischt und rau;
- Zustandsentropie und beobachtete Wechselrate;
- 10-, 50- und 90-Prozent-Quantil der Periodizität;
- Periodizitätsspanne;
- mediane und obere hochfrequente Struktur;
- kurzzeitige Hüllkurvenvariation als Pulsindex.

Die sechs Referenzclips und ihr Manifest bleiben byte- und hashgebunden.

## Ergebnis

Dieselbe 17-sekündige Spielphrase wurde mit `morph` und `organic` v4 gerendert.

| Merkmal | `morph` | `organic` v4 | Referenzmedian |
|---|---:|---:|---:|
| globaler Vergleichswert | 0,314 | **0,341** | – |
| zeitlicher Vergleichswert | 0,226 | **0,355** | – |
| raue Fenster | 0,000 | **0,063** | 0,222 |
| Zustandsentropie | 0,586 | **0,794** | 0,721 |
| beobachtete Wechselrate, 1/s | 0,659 | 0,662 | 1,739 |
| Periodizitätsspanne | 0,078 | 0,069 | 0,374 |
| obere Hochfrequenzstruktur | 0,429 | **0,651** | 1,286 |
| Pulsindex | 0,328 | **0,482** | 0,535 |
| globaler Hochfrequenzanteil | 0,00285 | **0,01063** | 0,01431 |
| Tonumfang, Halbtöne | 11,41 | 11,41 | 43,40 |

Der zeitliche Vergleich verbessert sich relativ um rund 57 Prozent. Der
Tonumfang wächst nicht; die Verbesserung stammt daher nicht aus erneut
eingeführten Sweeps. Die Periodizität bleibt bewusst höher als im Rohmaterial,
weil eine stärkere Zerstörung der Grundtonstruktur dem früheren Buzz-/UFO-Fehler
ähneln würde.

## Sicherheit und Leistung

- stärkster Peak der Vergleichsphrase: 0,190 bei harter Grenze 0,25;
- Median aus neun Läufen für eine Sekunde Audio: 0,472 Sekunden CPU-Zeit;
- 90-Prozent-Wert derselben Messung: 0,486 Sekunden;
- Offline-Echtzeitreserve: rund 2,12×;
- drei vollständige Acht-Sekunden-Zustandszyklen über Tief-, Mittel- und
  Hochregister: schlechtester Median 0,492 Sekunden CPU je Audiosekunde,
  entsprechend 2,03× Echtzeitreserve;
- stärkster Peak dieser vollständigen Zustandszyklen: 0,195;
- A0-Energie unter 120 Hz gegenüber `morph`: 1,76×;
- A1-Energie unter 120 Hz gegenüber `morph`: 2,25×;
- A2-Energie unter 120 Hz gegenüber `morph`: 1,11×;
- Ausgabe und interner Zustand bleiben unabhängig von Chunkgrößen bitidentisch;
- Leerlauf und beendeter Ausklang bleiben exakt digital still.

## Grenzen

Die Vergleichswerte sind Diagnosegrößen, keine Wahrnehmungs- oder
Artenklassifikatoren. Noch offen bleiben insbesondere:

- die zu geringe Periodizitätsspanne gegenüber vielen Naturaufnahmen;
- ein geringerer Anteil rauer Fenster als im Referenzmedian;
- die physische Bewertung von Wal, Buzz, Orgel und UFO;
- die Wirkung des Tiefbasses über MOTU M2 und Focal Clear MG.

Diese offenen Punkte dürfen nicht durch erneute große Pitch-Sweeps oder
ungekoppeltes Rauschen geschlossen werden.

## Reproduktion

```bash
python3 scripts/compare_whale_organic.py \
  --engine morph \
  --output /tmp/buckelwal-morph.wav \
  --report /tmp/buckelwal-morph.json

python3 scripts/compare_whale_organic.py \
  --engine organic \
  --output /tmp/buckelwal-organic-v4.wav \
  --report /tmp/buckelwal-organic-v4.json
```
