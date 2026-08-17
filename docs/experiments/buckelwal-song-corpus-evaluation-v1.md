# Buckelwal-Songkorpus und mehrskalige Struktur-Evaluation v1

Stand: 17. August 2026

## Urteil

**These:** Die `WhaleSongGrammar` kann nun gegen ein echtes, menschlich annotiertes Songkorpus auf Phrase-/Theme-/Song-Skala geprüft werden. Eine ausschließlich aus 2012–2016 abgeleitete Projektion senkt den mittleren Fehler auf einem eingefrorenen 2017–2019-Holdout über sieben deklarierte Strukturmerkmale von `0.403603` auf `0.270037` (Delta `-0.133566`, relativ rund **33.09 %**).

**Gegenthese:** Der Gesamtgewinn rechtfertigt keinen Produkt- oder Defaultwechsel. Ein Merkmal wird im Holdout schlechter: die Interphrasenlücke. Die Run-Länge desselben Phrasentyps verbessert sich nach vollständiger Klassifikation der gültigen Codes. Außerdem enthalten die veröffentlichten Raven-Tabellen weder individuelle Unit-Zeitgrenzen noch explizite Songgrenzen.

**Synthese:** Das Korpus ist ein belastbarer study-only Evidenzlayer. Es zeigt, dass die Makrostruktur der bisherigen Default-Grammar messbar verbesserbar ist und dass die 2012–2016-Projektion in einem späteren Songstil generalisiert. Es etabliert noch keine biologisch endgültigen Parameter oder menschliche Hörpräferenz.

## Primärquelle und Rechte

Primär verwendet wird:

- Elena Schall, Javier Oña, Judith Denkinger (2024), *Humpback Whale Song Recordings Ecuador 2012-2019*, Figshare, DOI `10.6084/m9.figshare.25259947.v1`.
- Zugehörige Studie: Javier Oña, Judith Denkinger, Elena Schall (2025), *Acoustic richness and composition changes of humpback whale (Megaptera novaeangliae) songs on breeding grounds off the coast of Ecuador*, *Marine Mammal Science* 41(2), e13208, DOI `10.1111/mms.13208`.

Der Figshare-Datensatz ist als **CC BY 4.0** ausgewiesen und enthält 26 WAV-Aufnahmen mit zugehörigen Raven-Pro-Selection-Tables. In Git liegen nur die 26 kleinen `.txt`-Tabellen mit zusammen 272681 Bytes. Die ungefähr 6.13 GB Audiodaten werden nicht vendort. `source-manifest.json` bindet jede Tabelle an Figshare-Datei-ID, Bytezahl und veröffentlichtes MD5 sowie an ein lokal berechnetes SHA-256. Die externen WAVs bleiben nur über Figshare-ID, Bytezahl und MD5 referenziert.

### Bewusst ausgeschlossene Quellen

Der Allen-et-al.-Workbook-Datensatz 2002–2014 (Dryad DOI `10.5061/dryad.69161bg`) wurde bei der Quellensuche geprüft, aber nicht übernommen. Die Landingpage-Metadaten wirken offen, der XLSX-Inhalt selbst enthält jedoch einen restriktiveren Wiederverwendungshinweis für neue Studien. Der strengere eingebettete Hinweis wird respektiert.

Das öffentliche Autorenskript `HumpbackWhaleSongStructureAnalysis_Ecuador.m` wurde am GitLab-Commit `fcf49e4abe3ff4544c6400758c0d1fa32ff76996` als Methoden-Cross-Check geprüft. Dort existieren ein Phrasenkatalog und spezielle Regeln für mehrstellige Wiederholungscodes. Gleichzeitig markiert der Quelltext den Phrase→Unit-Transkriptionspfad ausdrücklich als noch nicht korrekt fixiert. Für dieses GitLab-Repository wurde keine explizite Code-Lizenz beobachtet. Daher werden weder Quellcode noch Phrasenkatalog kopiert oder als kanonischer Decoder verwendet.

## Wahrheitsvertrag

### Direkt beobachtet in den Raven-Tabellen

- Beginn und Ende jeder veröffentlichten Phrase;
- Phrasendauer;
- untere und obere Frequenzgrenze;
- originale Category;
- originale Selection-ID und Quellzeile.

### Sicher geparst, aber nicht als Unit-Decodierung behandelt

Aus einer Category wie `Ii11312` werden nur extrahiert:

```text
phrase_type = Ii
repetition_code = 11312
```

Die Ziffernfolge bleibt unverändert gespeichert. Sie wird **nicht** zeichenweise in Wiederholungszahlen zerlegt. Der Grund ist materiell: Die Autorenlogik benötigt phrase-spezifische Sonderregeln, insbesondere für mehrstellige Wiederholungszahlen und spezielle Phrasenfamilien.

### Direkt aus der peer-reviewten Tabelle übernommen

Pro Aufnahme werden revisionsgebunden übernommen:

- Anzahl analysierter Songs;
- mittlere Unit-Zahl pro Song;
- repräsentative/mediane Theme-Sequenz.

Damit stammt die Unit-Zahl/Song nicht aus unserem Decoder, sondern unmittelbar aus der publizierten Studienauswertung.

### Abgeleitete Aggregate

Da die Release-Tabellen keine individuellen Songgrenzen markieren, werden zwei Größen ausdrücklich nur als Aufnahmeaggregate verwendet:

- `phrases_per_published_song = veröffentlichte Phrasenzeilen / publizierte Songzahl`;
- `analyzed_span_per_published_song = Zeitspanne erste bis letzte veröffentlichte Phrase / publizierte Songzahl`.

Diese Werte sind keine behaupteten Einzel-Song-Messungen.

### Unbekannt / nicht behauptet

- einzelne Unit-Zeitgrenzen innerhalb der Phrasen;
- vollständige Unit-Sequenz und Unit-Anzahl jeder einzelnen Phrase aus dem rohen Wiederholungscode;
- individuelle Songgrenzen in den veröffentlichten Raven-Tabellen;
- biologisch separat gelabelte Hybrid-/Transition-Phrasen.

## Normalisierung der Release-Tabellen

Alle 26 Tabellen enthalten zusammen **2312** Phrasenzeilen.

Eine einzige Tabelle, `HS140716-1131-ESM.txt`, enthält eine Reihenfolge-Inversion. Der Normalizer sortiert deterministisch nach beobachteter Start-/Endzeit, behält aber die originale `source_row`. Nach dieser Sortierung gibt es dort weder Überlappungen noch exakte Dubletten. `source_table_reordered=true` macht den Eingriff explizit sichtbar.

Unklassifizierte oder nur teilweise klassifizierte Categories bleiben ehrlich erhalten:

- `Ja31` → Phrasentyp `Ja`, roher Wiederholungscode `31`;
- `Ha` → Phrasentyp bekannt, kein Wiederholungscode;
- `?` → unklassifiziert.

## Eingefrorener zeitlicher Split

Der Split ist Bestandteil des Source-Manifests und wird bei jedem Build geprüft:

- **Development/Fit:** 2012–2016, 15 Aufnahmen;
- **Holdout:** 2017–2019, 11 Aufnahmen.

Der Fit-Code erhält nur den Development-Split. Der Holdout wird erst nach der Parameterauswahl ausgewertet. Das ist bewusst strenger als ein zufälliger Zeilen-Split, weil die Studie starke Songentwicklung und Revolutionen über die Jahre beschreibt.

## Empirische Struktur

### Development 2012–2016

- Aufnahmen: **15**
- veröffentlichte Phrasen: **1605**
- mittlere Phrasendauer: **8.687608 s**
- mittlere Interphrasenlücke: **1.037118 s**
- mittlere Run-Länge desselben Phrasentyps: **6.162162**
- mittlere Länge der publizierten medianen Theme-Sequenz: **7.4**
- Phrasen pro publiziertem Song (gepoolt): **42.236842**
- publizierte mittlere Units pro Song (nach Songzahl gewichtet): **158.382105**
- analysierte Zeitspanne pro publiziertem Song (gepoolt): **412.585699 s**

### Holdout 2017–2019

- Aufnahmen: **11**
- veröffentlichte Phrasen: **707**
- mittlere Phrasendauer: **12.245461 s**
- mittlere Interphrasenlücke: **1.087752 s**
- mittlere Run-Länge desselben Phrasentyps: **5.160584**
- mittlere Länge der publizierten medianen Theme-Sequenz: **5.363636**
- Phrasen pro publiziertem Song (gepoolt): **30.739130**
- publizierte mittlere Units pro Song (nach Songzahl gewichtet): **184.772174**
- analysierte Zeitspanne pro publiziertem Song (gepoolt): **409.331153 s**

Der spätere Holdout ist damit materiell anders: längere Phrasen und mehr Units pro Song, aber kürzere Runs desselben Phrasentyps und kürzere Theme-Sequenzen.

Aggregationsvertrag: Phrasen- und Gap-Metriken sind phrase-/gapgewichtet; Phrasentyp-Runs werden innerhalb jeder Aufnahme gebildet, weil individuelle Songgrenzen fehlen. Phrasen/Song und analysierte Zeit/Song werden aus gepoolten Summen durch die gepoolte publizierte Songzahl geteilt. Die peer-reviewten Units/Song werden mit der jeweiligen publizierten Songzahl gewichtet. Nur die publizierte mediane Theme-Sequenz bleibt aufnahmegewichtet, weil sie selbst bereits eine Aufnahme-/Sänger-Zusammenfassung ist.

## Development-only Projektion in die aktuelle Grammar

Empirische Einzelquantile dürfen nicht blind nach `SongGrammarConfig` kopiert werden. Die vorhandenen Bounds wirken gemeinsam, vor allem `MAX_SESSION_UNITS=512`, maximal sechs Themes und maximal acht Wiederholungen.

`training_recommendations()` durchsucht deshalb alle **127 aktuell gültigen** Kombinationen aus:

- `theme_count` 2–6;
- `phrase_repeats_min` 2–8;
- `phrase_repeats_max` min–8;
- Development-Median der Phrasenpause, begrenzt durch den bestehenden Pause-/Jitter-Vertrag.

Der Selektionsscore benutzt nur fünf direkt vergleichbare Development-Größen:

- Interphrasenlücke;
- Run-Länge desselben Phrasentyps;
- Theme-Sequenzlänge;
- Phrasen pro publiziertem Song;
- peer-reviewte Units pro Song.

Gewinner innerhalb der **bestehenden sicheren Grammar**:

```text
theme_count = 6
phrase_repeats_min = 6
phrase_repeats_max = 6
phrase_pause_seconds = 0.716662
```

Unverändert bleiben:

```text
cycles = 2
transition_pause_seconds = 1.35
cycle_pause_seconds = 2.60
base_note = 45
seed = 0xB0A7
```

Der direkte Development-Fit über die fünf Selektionsmerkmale beträgt `0.124392`. Jede der 127 Konfigurationen wird über acht feste, datenunabhängig aus `whale-song-model-seed-v1:<index>` abgeleitete PRNG-Seeds bewertet; die fünf Modellmerkmale werden vor der Distanzberechnung arithmetisch gemittelt. Der empirische Theme-Median von 8 kann wegen der aktuellen Sechs-Theme-Grenze nicht direkt übernommen werden. Ebenso wird das Wiederholungs-P75 von 8 nicht mechanisch gesetzt, weil die gemeinsame Unit-Budget-Grenze die zulässigen Kombinationen beschränkt.

Nicht gefittet werden Transition- oder Zykluspausen, Motif-Pitches/Timbre und Unit-internes Timing. Dafür fehlt in diesem Korpus passende direkte Evidenz.

## Eingefrorene Holdout-Evaluation

Der technische Distanzwert ist der Mittelwert des relativen absoluten Fehlers über **sieben** deklarierte Strukturmerkmale. Niedriger ist besser. Er ist kein biologischer oder perceptueller Realismus-Score.

| Modell | Development | Holdout |
|---|---:|---:|
| bestehender Grammar-Default | 0.435504 | 0.403603 |
| Development-Projektion | 0.213331 | 0.270037 |

Holdout-Delta: **-0.133566**, relativ rund **33.09 %** niedriger als der Default. Die gefittete Projektion schlägt den Default zusätzlich bei **8/8** einzeln ausgewerteten festen Modell-Seeds; ihr schlechtester Seed (`0.270918`) bleibt klar unter dem besten Default-Seed (`0.386912`).

### Holdout-Einzelbefunde

Verbessert:

- Phrasendauer: `0.615001 -> 0.602048`;
- Theme-Sequenzlänge: `0.254237 -> 0.118644`;
- Phrasen/publizierter Song: `0.379862 -> 0.333805`;
- publizierte Units/Song: `0.539298 -> 0.017469`;
- Phrasentyp-Run-Länge: `0.200672 -> 0.162659`;
- analysierte Zeitspanne/publizierter Song: `0.733944 -> 0.427768`.

Verschlechtert:

- Interphrasenlücke: `0.081012 -> 0.227868`.

Damit liegt ein **begrenzter, aber klarer Generalisierungserfolg** vor. Die Projektion trifft die grobe Songmenge, Theme-Länge und Phrasentyp-Run-Länge besser; nur die Interphrasenlücke des späteren Songstils wird schlechter. Ein pauschaler Produkt-Defaultwechsel wäre daher zu stark.

## Größte verbleibende Modelllücke

Die bessere Projektion ist zeitlich weiterhin zu kurz:

- Holdout Phrasendauer: `12.245461 s` empirisch vs. `4.873101 s` Modell;
- Holdout analysierte Zeitspanne/publizierter Song: `409.331153 s` empirisch vs. `234.232524 s` Modell.

Bei der Unit-Zahl ist die Projektion hingegen deutlich näher: `188` Modell-Units/Song gegenüber `184.772174` publizierten Units/Song. Das spricht gegen weiteres bloßes Hinzufügen von Units und eher für eine getrennte study-only Untersuchung **längerer Unit-/Phrasendauern**. Ohne echte Unit-Zeitgrenzen wird daraus hier noch kein biologischer Timing-Vertrag.

## Blind-/Hörvergleich

`build_whale_song_blind_test.py` erzeugt ein anonymes Paar `sample-A.wav` / `sample-B.wav` plus getrenntes `answer-key.json`.

Kontrolliert gleich bleiben:

- `WhaleMorphVoice`;
- vollständiges Unit-Inventar der synthetischen Ausgangssession;
- Note, Dauer, Velocity, Pitch-Bend und Pulszahl jeder Unit;
- Unit-Reihenfolge innerhalb ihrer ursprünglichen Phrase.

Ablatiert werden nur:

- Reihenfolge der Phrasenblöcke;
- hierarchische Phrase-/Transition-/Zykluspausen.

Beide Samples werden zunächst mit identischem Gain gerendert und anschließend **nur abwärts** auf den niedrigeren RMS-Pegel angeglichen. Kein Sample wird zur Lautheitsangleichung hochverstärkt.

Der Renderer bleibt bei höchstens 30 Sekunden. Das Paar prüft damit lokale Phrase-/Theme-Kohärenz, nicht die Wahrnehmung eines vollständigen mehrminütigen Songzyklus.
Da die Makro-Ablation Phrasenblöcke umordnet, enthalten zwei zeitlich auf 30 Sekunden abgeschnittene Samples nicht zwingend exakt dieselben Units. Der Blindtest ist deshalb **explorativ** und etabliert keine kausale Präferenz für Hierarchie unabhängig von der konkreten Unit-Auswahl im Ausschnitt.

## Kanonische Artefakte

- Die vollständige normalisierte 26-Aufnahmen-/2312-Phrasen-Projektion wird deterministisch **on demand** gebaut und nicht als redundante 1.7-MB-Datei in Git gespeichert.
- `empirical-structure.json`: Development/Holdout und Development-only Empfehlung;
- `evaluation.json`: Default-vs.-Projection mit eingefrorenem Holdout.

Aktuelle interne Identitäten:

```text
corpus_sha256    eaf884d3c11d3a63d134d0f4d4544093b1772f09e493893013c2bebda9e83426
empirical_sha256 6f31dd7bc3ae63a510ef528502e0d92e2b6c28d74ac3c786b0dffde239038ac7
evaluation_sha256 6a0c2d28622cca037af106d2c553fc9d3d0133e947b0af7f42518324a11ed36d
```

Reproduzierbare Builds:

```bash
python3 scripts/build_whale_song_corpus.py \
  --output /tmp/whale-song-corpus-v1.json

python3 scripts/evaluate_whale_song_grammar_structure.py \
  --empirical-output assets/whale-sources/song-corpus-v1/empirical-structure.json \
  --evaluation-output assets/whale-sources/song-corpus-v1/evaluation.json

python3 scripts/build_whale_song_blind_test.py \
  --output-dir /tmp/whale-song-blind-v1 \
  --seconds 30
```

Die WAV-Blindartefakte sind Laufzeitergebnisse und werden nicht in Git versioniert.

## Nichtbehauptungen

Diese Arbeit etabliert nicht:

- exakte Unit-Zeitstempel;
- eine verlässliche per-Phrase Unit-Sequenz aus dem rohen Wiederholungscode allein;
- individuelle Songgrenzen in den Release-Tabellen;
- populationsweite biologische Optimalparameter;
- biologisch gelabelte Hybrid-/Transition-Phrasen;
- akustische Timbre-Überlegenheit;
- menschliche Hörpräferenz;
- einen neuen Live-Modus;
- eine Änderung des `morph`-Defaults oder der Roland-Spielbarkeit.

Akustische Mikrotreue bleibt in den vorhandenen unabhängigen NOAA-/Morph-Evaluatoren. Diese Arbeit misst gezielt die zuvor fehlende Makrostruktur.
