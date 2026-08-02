# Buckelwal Organic Voice v5 – auditierte Fassung

## Status

> **Nachfolgende Entscheidung vom 2. August 2026:** Die revisionsgebundene
> Ablations- und Generalisierungsstudie in
> `docs/experiments/buckelwal-organic-v51-ablation.md` qualifiziert keine
> Organic-Variante als robuste neue Grundengine. Morph bleibt der kanonische
> Standard. Vollständiges Organic bleibt eine experimentelle Referenz; es wird
> weder weiter nach externen Ergebnissen abgestimmt noch als v5.1 veröffentlicht.
> Die v5.1-Studie wurde nach externem Review mit Sinc statt linearer
> Interpolation neu ausgeführt; ihre korrigierten Werte ändern diese Entscheidung
> nicht.

Die erste v5-Fassung wurde nach dem Merge erneut unabhängig gegen ihren
Quellcode, das Modellformat und die Bewertungsmethodik auditiert. Dabei wurden
konkrete Fehler in Sekundärfrequenzextraktion, Modellhashbindung,
Downsampling, Auswahlgewichtung, Trajektorienübergang und Bewertung gefunden.
Diese Datei beschreibt ausschließlich die korrigierte Fassung.

Die physische Hörprüfung bleibt offen. Die Stimme wird nicht automatisch
gestartet oder abgespielt.

## Wissensgrundlage

Die biologische und akustische Referenz steht in
`docs/knowledge/buckelwal-stimme-und-gesang.md`. Die Engine bleibt ein
musikalisch spielbares, quellengestütztes Instrument und kein vollständiges
Stimmapparatmodell.

## Modellbank v2

`scripts/build_whale_voice_model.py` analysiert 19 verarbeitete Clips aus exakt
acht gebundenen Quellfamilien. Jeder Clip wird aus einem einzigen
hashgeprüften Byte-Snapshot gelesen.

Vor der Reduktion von 48 auf 4 kHz wird ein achtpoliger Butterworth-Tiefpass mit
1.650 Hz Grenzfrequenz angewendet. Erst danach wird um Faktor zwölf dezimiert.
Die frühere Boxmittelung mit messbarem Aliasing ist entfernt.

Je Clip entstehen 48 Zeitpunkte mit:

- Hüllkurve;
- Periodizität und komplementärer Rauigkeit;
- bandbegrenztem Hochfrequenzanteil und spektraler Neigung;
- achtteiliger harmonischer Energieverteilung;
- zwei groben harmonischen Resonanzschwerpunkten;
- Pulsrate und Pulsstärke;
- Subharmonik;
- getrenntem Verhältnis und Stärke einer sekundären Frequenzspur.

Die Resonanzschwerpunkte werden nicht als biologisch gemessene Formanten
bezeichnet.

| Eigenschaft | Wert |
|---|---:|
| Modellgröße | 650.191 Byte |
| SHA-256 | `1bbd10566bbfc9ee9159c994de456d408ed003cea65602faee8076b308d0ee8a` |
| Trajektorien | 19 |
| Quellfamilien | 8 |
| Kontrollpunkte je Trajektorie | 48 |
| Harmonische Bänder | 8 |

Der Loader, der Live-Doctor, das Profil und die Audiozentrale verlangen exakt
diesen Modellhash. Auch eine schema-gültige Änderung eines einzelnen
Steuerwerts blockiert die Modellbank.

## Korrigierte Sekundärfrequenz

Die erste Fassung speicherte Verhältnis und Stärke irrtümlich als Produkt und
versuchte beide Werte daraus wiederherzustellen. Dadurch kollabierte der größte
Teil aktiver Sekundärkomponenten auf Verhältnis 1,0.

Die korrigierte Analyse führt beide Größen getrennt. Im neu gebauten Modell
besitzen 449 Kontrollpunkte eine aktive Sekundärkomponente; alle 449 liegen mehr
als 0,05 vom Unisonoverhältnis entfernt. Die Verhältnisse reichen im gebundenen
Korpus von ungefähr 0,605 bis 2,375. Die Liveamplitude bleibt weiterhin stark
begrenzt.

## Quellfamilienbalancierte Laufzeitauswahl

Die Auswahl erfolgt zweistufig:

1. gleichgewichtete Wahl einer zur Registerkategorie passenden Quellfamilie;
2. Wahl eines Clips innerhalb dieser Familie.

Familien mit drei Clips erhalten dadurch nicht mehr automatisch mehr Gewicht
als Familien mit zwei Clips.

## Rufeinheiten und Übergänge

Jede Trajektorie behält ihre eigene auf 1,45 bis 4,80 Sekunden begrenzte
Einheitsdauer. Die erste Fassung verwendete irrtümlich die Dauer der ersten
Trajektorie für alle folgenden Einheiten.

Beim Wechsel beginnt die neue Einheit am Endzustand der vorherigen. Über die
ersten 14 Prozent der neuen Einheit wird kontinuierlich in deren eigenen
Anfangsverlauf überblendet. Der frühere Rücksprung von einer vorab
überblendeten Phase auf Phase null entfällt.

## Nutzung des vollständigen Obertonprofils

Alle acht gespeicherten Bänder beeinflussen nun die Laufzeit:

- tiefe, mittlere und obere harmonische Energie;
- geradzahlige Harmonischenbalance;
- harmonischer Schwerpunkt;
- spektrale Tief-/Hochgewichtung;
- Resonanzmischung und signalgebundene Kantenstruktur.

Die erste Fassung verwendete faktisch nur das zweite und dritte Band.

## Bewertung

### Zurückgezogene Aussage

Der frühere Wert `0,1177` gegenüber `0,0993` war kein unabhängiger
Generalisierungsnachweis. Die sogenannten Holdoutfamilien wurden während der
Entwicklung wiederholt betrachtet; außerdem reduzierte der Prüfer Clips auf
Medianwerte und gewichtete Periodizität sowie `1 − Periodizität` doppelt.

### Korrigierte Cross-Validation

`scripts/evaluate_whale_voice_model.py` verwendet Leave-one-source-family-out-
Cross-Validation:

- acht Außenfolds, je Quellfamilie einer;
- die bewertete Familie ist im jeweiligen Organic-Render vollständig aus der
  Liveauswahl entfernt;
- alle Familien besitzen dasselbe Gewicht;
- verglichen werden geordnete 48-Punkt-Verläufe;
- Periodizität wird nur einmal gewichtet;
- Hüllkurve, Spektrum, Resonanzschwerpunkte, Puls, Subharmonik,
  Sekundärkomponente und vollständiges Obertonprofil fließen ein;
- Modellmanifest und ausgewertete Trajektorien stammen aus demselben gebundenen
  Bankobjekt, ohne zweite Pfadöffnung.

Abschließende Messung:

| Engine | mittlere Ähnlichkeit | Median | mittlere Distanz | Peak |
|---|---:|---:|---:|---:|
| Morph | 0,1447 | 0,1526 | 1,9968 | 0,0903 |
| Organic | **0,1487** | 0,1496 | **1,9531** | 0,2078 |

Organic verbessert fünf der acht Familienfolds und verschlechtert drei leicht.
Der kleine Mittelwertvorteil ist ein Regressionsergebnis innerhalb des
bekannten Korpus. Er ist kein unabhängiger Fremddatensatztest, kein biologischer
Identitätsbeleg und kein subjektiver Hörnachweis.

### Gesperrter unabhängiger Fremdtest

Nach Abschluss aller DSP- und Modellreparaturen wurde eine bislang nicht im
Repository verwendete NOAA-PMEL-Aufnahme als unveränderlicher Fremdtest
festgeschrieben. Die Quelle ist eine Buckelwalaufnahme aus Alaska vom Winter
1999; die veröffentlichte Datei ist zehnfach beschleunigt. Der gebundene
Evaluationsausschnitt stellt die Nominalgeschwindigkeit wieder her und wird
weder für Modellbau, Laufzeitauswahl noch Parameterabstimmung verwendet.

| Engine | Ähnlichkeit | zeitliche Distanz | Peak |
|---|---:|---:|---:|
| Morph | **0,1707** | **1,7676** | 0,0903 |
| Organic | 0,1538 | 1,8722 | 0,2078 |

Organic ist diesem einzelnen unabhängigen Ruf weniger ähnlich als Morph. Das
Ergebnis wurde nach seiner ersten Erhebung nicht zum Nachstimmen verwendet.
Ein einzelner Ruf belegt keine Populationsgeneralisation; er widerlegt aber die
Annahme, dass die Organic-Erweiterung außerhalb des Entwicklungskorpus bereits
allgemein überlegen sei.

Gebundene Evaluationsartefakte:

- `assets/whale-sources/evaluation/manifest.json`;
- Rohdatei SHA-256
  `54a91b3c3e488941697acdf01face985ad149ca91e5d85af0f3ec8b1ad00ab42`;
- Evaluations-WAV SHA-256
  `1a38ba45c88e3cabbf72ffc50026bdfb4fe9882018cebd5e7f3a658497484822`;
- Evaluationsmanifest SHA-256
  `9bdcf78fdc4d0f1fce77d4e2defa877910ea5102e66e515704fceaee123dd39d`.

### Ergänzende Ausgangsmetriken

Der ältere globale Prüfer liefert für die korrigierte Organic-Fassung:

- globaler Vergleich `0,3203`;
- zeitlicher Vergleich `0,3969`;
- Pulsindex `0,4931`;
- Zustandsentropie `0,6785`;
- Tonumfang `11,53` Halbtöne;
- Vergleichsphrase Peak `0,2312`.

Diese Werte bleiben ergänzende Regressionen und sind dem familiengewichteten
Zeitvergleich und dem unabhängigen Fremdtest methodisch nachgeordnet.

### Tiefbass und Laufzeitreserve

| Taste | Energie unter 120 Hz gegenüber Morph | Ein-Sekunden-Peak |
|---|---:|---:|
| A0 / MIDI 21 | 1,59× | 0,093 |
| A1 / MIDI 33 | 2,36× | 0,109 |
| A2 / MIDI 45 | 1,38× | 0,109 |

Der schlechteste gemessene Ein-Sekunden-Lauf benötigt `0,566 s` CPU pro
Audiosekunde. Vollständige Acht-Sekunden-Zyklen benötigen höchstens `0,546 s`
CPU pro Audiosekunde, entsprechend mindestens `1,83×` Echtzeitreserve; deren
maximaler Peak liegt bei `0,151`.

## Erhaltene Produktverträge

- Hauptgrundton bleibt an die gespielte Taste gebunden;
- zusätzliche Organic-Tonhöhenbewegung bleibt unter 20 Cent;
- Organic-Legato bleibt bei höchstens 180 Millisekunden;
- keine Aufnahmephrase, Samplezone oder permanente Rauschschicht;
- exakte digitale Stille nach endlichem Ausklang;
- bitidentische Ausgabe und gleicher Zustand bei beliebigen Render-Chunkgrößen;
- A0/A1 behalten den materiellen Tiefbassvertrag;
- Ausgabe bleibt unter 0,25;
- Echtzeitreserve bleibt automatisiert geprüft.

## Bedienung

Der sichtbare Modus bleibt `organic`; es entsteht kein weiteres Preset.

```bash
python3 scripts/build_whale_voice_model.py --check
python3 scripts/evaluate_whale_voice_model.py \
  --engine organic \
  --output /tmp/buckelwal-v5-cross-validation.json
python3 scripts/evaluate_whale_voice_model.py \
  --engine organic --external \
  --output /tmp/buckelwal-v5-external-evaluation.json
python3 scripts/whale_live.py doctor
python3 scripts/whale_live.py mode organic
```

Die letzte Zeile ist erst für die physische Hörprüfung mit verfügbarem Roland,
MOTU und Nutzer vorgesehen.

## Noch offen

- A/B-Hörtest über MOTU M2 und Focal Clear MG;
- Vergleich auf UFO-, Orgel-, Buzz- und Filtereffektcharakter;
- der zusätzliche externe Test ist abgeschlossen; offen bleibt ein größerer,
  nach Rufart und Aufnahmebedingung kontrollierter Datensatz mit wirklich
  unabhängigen Aufnahmen statt mehrerer Segmente derselben Feldaufnahmen;
- mögliche Erweiterung um ein Phrasengedächtnis, falls lange Spielpassagen trotz
  kontinuierlicher Einheiten noch unverbunden wirken.
