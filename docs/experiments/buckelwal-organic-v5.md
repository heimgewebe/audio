# Buckelwal Organic Voice v5

## Anlass

Organic v4 verbesserte zeitliche Zustandswechsel, blieb aber im Kern eine
periodische Morphstimme mit algorithmisch ergänzten Klangzuständen. Die nächste
Stufe sollte nicht mehr Rauigkeit oder Tonhöhenbewegung hinzufügen, sondern
Originalaufnahmen nach zeitlich gekoppelten Stimmmerkmalen auswerten und diese
als Grundengine verwenden.

Die physische Hörprüfung ist weiterhin offen, weil der Nutzer während der
Umsetzung nicht zuhause ist. V5 wird deshalb nur aus reproduzierbaren
Software-, Quellen- und Holdoutbelegen freigegeben. Die Walstimme wird nicht
automatisch gestartet oder abgespielt.

## Wissensgrundlage

Die fachliche Referenz steht in
`docs/knowledge/buckelwal-stimme-und-gesang.md`. Sie dokumentiert unter anderem:

- Quelle-Filter-Kopplung des Kehlkopfs;
- tonale, gepulste, raue und chaotische Zustände;
- Frequenzsprünge, Subharmonik und Biphonation;
- asymmetrische Einsätze und Abschlüsse;
- Hierarchie aus Einheit, Phrase, Thema und Songzyklus;
- gerichtete Varianten statt identischer oder beliebig zufälliger Wiederholung;
- Grenzen der Übertragung auf ein chromatisches 88-Tasten-Instrument.

## Quellengebundener Analyse-Builder

`scripts/build_whale_voice_model.py` analysiert alle 19 verarbeiteten Clips als
jeweils einen unveränderlichen, hashgeprüften Byte-Snapshot. Es entstehen pro
Clip 48 relative Kontrollpunkte mit:

- Hüllkurve;
- Periodizität und signalgebundener Rauigkeit;
- Hochfrequenzanteil und spektraler Neigung;
- achtteiliger Obertonverteilung;
- zwei Resonanzverhältnissen;
- Pulsrate und Pulsstärke;
- Subharmonik;
- sekundärer Frequenzspur.

Das resultierende Modell liegt unter
`assets/whale-sources/voice-model/manifest.json`.

| Eigenschaft | Wert |
|---|---:|
| Modellgröße | 644.837 Byte |
| SHA-256 | `c2f5a99f0d9c95f75830ba6f2122cfbdd12e847b54eca6bbee80b563d07a9541` |
| Trajektorien | 19 |
| Trainings-Trajektorien | 12 |
| Holdout-Trajektorien | 7 |
| Trainings-Quellfamilien | 5 |
| Holdout-Quellfamilien | 3 |
| Kontrollpunkte je Trajektorie | 48 |

Der Builder besitzt einen Byte-Reproduzierbarkeitscheck. Jede Veränderung am
Quellmanifest oder einem Clip blockiert Modellbau beziehungsweise Live-Start.

## Ganze Quellfamilien als Holdout

Nicht einzelne Ausschnitte, sondern vollständige Quellfamilien werden
zurückgehalten.

### Training und Live-Auswahl

- `humpback-moo-nps`
- `humpback-wheezeblow-nps`
- `song-antarctic-area-v-2010`
- `song-foraging-mn132a`
- `song-new-caledonia-2010`

### Ausschließlicher Holdout

- `humpback-song-cc0`
- `song-eastern-australia-2010`
- `song-foraging-mn133a`

Die Live-Engine kann keine Holdout-Trajektorie auswählen. Damit misst der
Holdout nicht bloß die Reproduktion bereits verwendeter Aufnahmefamilien.

## Source-Filter-Grundengine

`scripts/whale_source_filter_engine.py` trennt die Klangproduktion in:

1. bandbegrenzte, phasenkontinuierliche Morphquelle;
2. zeitvariable spektrale Gewichtung;
3. zwei bewegliche Resonanzen;
4. quellseitig gesteuerte Pulsierung;
5. signalgebundene Rauigkeit ohne unabhängigen Rauschgenerator;
6. schwache, begrenzte Subharmonik;
7. leise sekundäre Frequenzspur;
8. relative Rufeinheiten mit Übergang zur nächsten verwandten Trajektorie.

Der Hauptgrundton bleibt an die gespielte Taste gebunden. Es wird keine
aufgenommene Phrase abgespielt und kein Audioloop verwendet.

## Verschmelzung statt Effektestapel

Ein erster Prototyp legte die vollständige v4-Schicht über die neue
Source-Filter-Basis. Das war fachlich redundant und benötigte ungefähr 0,65
Sekunden CPU pro Audiosekunde. Die endgültige Fassung verschmilzt beide Ebenen:

- Source-Filter übernimmt Formanten, Spektralverlauf, Periodizität, Rauigkeit,
  Puls, Subharmonik und sekundäre Frequenz;
- Organic ergänzt nur Anti-UFO-Kontur, Tiefbass, Gestenreaktion und eine leichte
  Gewichtung kurzer `tonal`-, `pulsed`-, `rough`- und `broken`-Fenster;
- kräftigere Kanten entstehen ausschließlich, wenn sowohl der zeitliche Zustand
  als auch die Originaltrajektorie Rauigkeit anzeigen.

## Vergleichswerte

Die gleiche 17-sekündige Spielphrase wird mit identischen MIDI-Gesten
verglichen. Die Werte sind Engineeringindikatoren und kein biologischer oder
perzeptiver Echtheitsbeweis.

### Bisheriger globaler und zeitlicher Prüfer

| Merkmal | Morphbasis | Organic v4 | Organic v5 |
|---|---:|---:|---:|
| globaler Vergleich | 0,314 | 0,341 | **0,327** |
| zeitlicher Vergleich | 0,226 | 0,355 | **0,348** |
| Zustandsentropie | 0,586 | 0,794 | **0,659** |
| Pulsindex | 0,328 | 0,482 | **0,487** |
| obere Hochfrequenzstruktur | 0,429 | 0,651 | **0,563** |
| rauer Fensteranteil | 0,000 | 0,063 | **0,011** |
| Konturspanne | 11,41 HT | 11,41 HT | **11,41 HT** |

V5 hält den v4-Zeitwert bis auf rund 1,8 Prozent und verbessert den
Morph-Zeitwert um rund 54 Prozent. Es optimiert bewusst nicht auf maximalen
rauen Fensteranteil, weil dies in früheren physischen Tests Buzz- und
UFO-Eindruck verstärkte.

### Neue Quellfamilien-Holdout-Prüfung

| Engine | Holdoutwert |
|---|---:|
| Morphbasis | 0,0993 |
| Organic v5 | **0,1177** |

Das entspricht rund **18,5 Prozent relativer Verbesserung** auf vollständig
unbenutzten Quellfamilien. Das technische Mindestgate liegt bei 15 Prozent.

## Tiefbass und Pegel

Die Source-Filter-Basis besitzt eine registerabhängige Pegelkompensation, damit
der bewusste Organic-Basskörper nicht doppelt verstärkt wird.

| Taste | Energie unter 120 Hz gegenüber Morph | Ein-Sekunden-Peak |
|---|---:|---:|
| A0 / MIDI 21 | **1,85×** | 0,127 |
| A1 / MIDI 33 | **2,47×** | 0,135 |
| A2 / MIDI 45 | **1,39×** | 0,136 |

Die vollständige Vergleichsphrase erreicht Peak `0,232`; die harte Grenze
bleibt `0,25`.

## Echtzeitreserve

- Sieben Ein-Sekunden-Läufe im Mittelregister: Median ungefähr 0,58 Sekunden
  CPU pro Audiosekunde, entsprechend rund 1,72× Echtzeitreserve.
- Vollständige Acht-Sekunden-Zustandszyklen in Tief-, Mittel- und Hochregister:
  schlechtester Wert 0,532 Sekunden CPU pro Audiosekunde, entsprechend
  mindestens 1,88× Echtzeitreserve.
- Maximaler Peak dieser Vollzyklen: 0,214.

## Erhaltene Produktverträge

- alle 88 Zieltonhöhen bleiben chromatisch bei A4 = 440 Hz;
- zusätzliche Organic-Tonhöhenbewegung bleibt unter 20 Cent;
- Organic-Legato bleibt bei höchstens 180 Millisekunden;
- keine Samplezonen, Presets oder Steuertasten;
- keine permanente Rauschschicht;
- keine aufgenommene Langphrase und kein Audioloop;
- exakte digitale Stille im Leerlauf und nach dem Ausklang;
- bitidentische Ausgabe und gleicher Zustand bei beliebigen Render-Chunkgrößen;
- Holdout-Quellfamilien sind von der Live-Auswahl ausgeschlossen;
- Doctor blockiert bei fehlender oder veränderter Zeitmodellbank;
- Ausgabe bleibt unter 0,25;
- Offline-Echtzeitreserve bleibt größer als 1,5×.

## Bedienung

Der sichtbare Modus bleibt `organic`. Es entsteht kein neues Preset und kein
zusätzlicher Modusschalter. Die Grundengine des vorhandenen Organic-Modus wird
ausgetauscht.

```bash
python3 scripts/build_whale_voice_model.py --check
python3 scripts/evaluate_whale_voice_model.py \
  --engine organic \
  --output /tmp/buckelwal-v5-holdout.json
python3 scripts/whale_live.py doctor
python3 scripts/whale_live.py mode organic
```

Die letzte Zeile darf erst ausgeführt werden, wenn Roland, MOTU und Nutzer für
die physische Hörprüfung verfügbar sind.

## Noch offene Hörfragen

Software und Holdout können nicht entscheiden:

- ob die Resonanzbewegungen wie ein Wal oder wie ein Filtereffekt wirken;
- ob die sekundäre Frequenzspur als Biphonation oder als Akkord gehört wird;
- ob der Tiefbass auf Focal Clear MG körperhaft statt dröhnend erscheint;
- ob v5 gegenüber v4 subjektiv weniger Orgel, Buzz und UFO erzeugt;
- ob lange Spielpassagen bereits ausreichend kohärent sind oder zusätzliches
  Phrasengedächtnis benötigen.
