# Buckelwal-Grundengine: Organic-v5.1-Ablations- und Generalisierungsstudie

Stand: 2. August 2026

## Entscheidung

**Morph bleibt vorerst die kanonische Grundengine.**

Die vorab festgelegte interne Auswahlregel hat keine Organic-Variante als
hinreichend robust qualifiziert. Der eingefrorene Kandidat ist deshalb Morph.
Die danach ausgeführten Fremdtests wurden nicht zum Nachstimmen verwendet.

Eine neue v5.1-Laufzeitengine ist durch diese Studie **nicht** gerechtfertigt.
Die Studie liefert aber konkrete Richtungen für eine spätere adaptive Engine:
Periodizität beziehungsweise signalgebundene Rauigkeit ist intern am stärksten,
während Resonanzfokus, Subharmonik und sekundäre Frequenzspur in ihrer heutigen
Daueraktivierung zurückgebaut oder nur ereignisgebunden verwendet werden
sollten.

## Wahrheitsgrenze

Die Messungen vergleichen eine feste musikalische Spielgeste mit extrahierten
akustischen Trajektorien. Sie prüfen Reproduzierbarkeit, Robustheit über
Quellfamilien und begrenzte externe Generalisierung. Sie beweisen weder
biologische Identität noch menschlich wahrgenommene Echtheit.

Keine Aufnahme wurde physisch wiedergegeben. Der Wal-Dienst blieb während der
Studie inaktiv.

## Revisions- und Hashbindung

| Gegenstand | Bindung |
|---|---|
| Auswahlquellstand | `82e679c79d247a304568689355c22b29b2fa14fe` |
| Kandidaten-Freeze-Commit | `7ee05d2` |
| Modellbank | `1bbd10566bbfc9ee9159c994de456d408ed003cea65602faee8076b308d0ee8a` |
| Studiendefinition | `e22e73d711e17fe0949e89d1ff387e30714931af342d122158db178a25fde7e6` |
| interner Bericht | `95d1b46290057fcccc1aad2f66db302a7f85e4eb3aff318fc16db01651636912` |
| eingefrorener Kandidat | `bf93de7f8c127876cc4b546b2e1317f6cdfd31d418ca73ec73d2487b071f1101` |
| externer Gesamtbericht | `ce33d96c90f73ad132dc53ba23949b0573fb3e3615678a032ceaf87f7c0a4e2b` |
| externe CSV | `3cf6a4b02a0ebe88c2f29b1a243fd6d54ead6735c7779902be529a69565f904a` |

Der Kandidat wurde vor jeder externen Auswertung festgeschrieben. Sein
Komponentensatz ist leer, also bitgenau Morph. Der Bericht deklariert
`parameters_changed_after_external_results: false`.

## Methodik

### Tuningseite

Die Auswahl verwendet ausschließlich:

- acht gleichgewichtete Leave-one-source-family-out-Folds;
- den bestehenden Modellbestand aus 19 Clips und acht Familien;
- Peak-, CPU-, Bass-, Pitch-, Stille- und Chunk-Verträge;
- eine vorab definierte Robustheitsregel.

Periodizität wird nur einmal gewichtet. Rauigkeit ist kein zweites Gewicht für
`1 − Periodizität`.

### Ablationsmatrix

Die deterministische Basismatrix enthält 22 Varianten:

- Morph;
- vollständiges Organic;
- zehn Varianten mit jeweils einer deaktivierten Organic-Komponente;
- zehn Varianten mit jeweils nur einer Organic-Komponente über Morph.

Kombinationen wären nur aus Komponenten erzeugt worden, die sowohl isoliert als
auch beim Herausnehmen aus vollständigem Organic robust positiv erscheinen.
Keine Komponente erfüllte diese vorab festgelegte Bedingung; deshalb wurde keine
nachträgliche Kombination zusammengestellt.

### Komponenten

1. quellabgeleitete Hüllkurve;
2. Periodizität und signalgebundene Rauigkeit;
3. Pulsrate und Pulsstärke;
4. Subharmonik;
5. sekundäre Frequenzspur;
6. harmonische Resonanzschwerpunkte;
7. achtteiliges Obertonprofil;
8. registerabhängiger Tiefbasskörper;
9. zeitliche Artikulationszustände;
10. Organic-Pitchkontur.

Alle Schalter sind Morph-neutral definiert. Alle Schalter aus ergeben bitgenau
Morph; alle Schalter an ergeben bitgenau das bisherige vollständige Organic.

## Interne Ergebnisse

### Baselines

| Variante | Mittel | Median | schlechtester Fold | verbessert / verschlechtert | CPU je Audiosekunde | Peak |
|---|---:|---:|---:|---:|---:|---:|
| Morph | 0,144732 | 0,152634 | 0,080041 | 0 / 0 | 0,3019 | 0,0903 |
| vollständiges Organic | 0,148678 | 0,149587 | 0,088430 | 5 / 3 | 0,5532 | 0,2078 |

Organic gewinnt im Mittel, kostet aber ungefähr 83 Prozent mehr CPU und
verschlechtert drei Familien. Mindestens eine dieser Verschlechterungen
überschreitet die vorab erlaubte Worst-Fold-Grenze. Der globale Mittelwert
reicht daher nicht für eine Freigabe.

### Einzelkomponenten

| Komponente | isolierter Befund | Herausnahme aus Organic | Urteil |
|---|---|---|---|
| Periodizität/Rauigkeit | Mittel 0,152953; 5/8 Familien besser; schlechtester Fold 0,099566 | Organic ohne sie fällt auf 0,146895 | **intern nützlich, aber instabil**: ein Familienverlust ist zu groß; Peak erreicht 0,25 |
| Registerbass | Mittel 0,146458; bester isolierter schlechtester Fold 0,103980; nur 3/8 Familien besser | Organic ohne ihn fällt auf 0,146533 | **funktional wichtig für Tiefe, statistisch nicht allgemein nützlich**; nicht als Ähnlichkeitsmodul behandeln |
| Resonanzfokus | 0/8 Familien besser; Mittel 0,137513 | Herausnahme verbessert Organic auf 0,150935 | **schädlich in heutiger Dauerform**; stärkster Rückbaukandidat |
| Obertonprofil | Mittel 0,142653; 2/8 besser | Herausnahme verbessert Organic leicht auf 0,149236 | überwiegend Färbung; keine robuste Generalisierung |
| Hüllkurve | Mittel 0,142560; 2/8 besser | Herausnahme nahezu neutral | neutral bis leicht schädlich in heutiger Stärke |
| Puls | Mittel 0,144176; 4/8 besser | Herausnahme verschlechtert Organic leicht | kleine kontextabhängige Wirkung, allein nicht tragfähig |
| Artikulationszustände | Mittel 0,144343; 4/8 besser | Herausnahme verschlechtert Organic leicht | kleine kontextabhängige Wirkung, allein nicht tragfähig |
| Pitchkontur | Mittel 0,143515; 2/8 besser | Herausnahme verschlechtert Organic leicht | kein objektiver Generalisierungsvorteil; Hörfrage bleibt offen |
| Subharmonik | 0/8 besser; Mittel 0,142771 | Herausnahme nahezu neutral und in allen acht Folds nicht schlechter | **statistisch schädlich beziehungsweise unnötig** in Daueraktivierung |
| sekundäre Frequenzspur | 0/8 besser; Mittel 0,142811 | Herausnahme nahezu neutral | **statistisch schädlich beziehungsweise unnötig** in Daueraktivierung |

### Pareto-Front

Nur drei Varianten liegen auf der internen Pareto-Front aus Ähnlichkeit,
schlechtestem Fold, CPU und Produktverträgen:

- Morph;
- nur Periodizität/Rauigkeit;
- nur Registerbass.

Die beiden Organic-Einzelvarianten scheitern trotzdem an den vorab definierten
Freigabekriterien. Morph bleibt der stabile Rückfallpunkt.

## Externe Generalisierung

### Gesperrter Alaska-Fremdtest

Der bestehende NOAA-PMEL-Test blieb byte- und hashidentisch.

| Variante | Ähnlichkeit | Distanz | Peak |
|---|---:|---:|---:|
| Morph | **0,170738** | **1,767625** | 0,0903 |
| vollständiges Organic | 0,153790 | 1,872165 | 0,2078 |
| eingefrorener Kandidat | **0,170738** | **1,767625** | 0,0903 |

Der negative Organic-Befund wird bestätigt.

### Zusätzlicher NOAA-PMEL-Satz

Der zusätzliche Satz besteht aus zwei unabhängigen Feldaufnahmen:

- Stellwagen Bank, Nordatlantik, Buckelwalrufe mit Schiffslärm;
- Amerikanisch-Samoa, Südpazifik, Buckelwalrufe mit Schnappkrebsen.

Aus jeder Aufnahme wurden vor der Engine-Auswertung vier nicht überlappende,
gleichmäßig verteilte Zwei-Sekunden-Intervalle festgelegt. Es handelt sich also
um acht Segmente, aber nur zwei unabhängige Aufnahmen. Die Segmentgrenzen wurden
ohne Hörprobe, Normalisierung, Entrauschung oder ergebnisabhängige Auswahl
bestimmt.

| Satz | Morph | vollständiges Organic | Ergebnis |
|---|---:|---:|---|
| acht Zusatzsegmente gesamt | 0,107306 | **0,113359** | Organic leicht besser |
| Stellwagen, 4 Segmente | 0,066165 | **0,076096** | Organic in allen vier Segmenten besser |
| Amerikanisch-Samoa, 4 Segmente | 0,148447 | **0,150622** | gemischt: je zwei Segmentgewinne |
| alle 9 externen Segmente | 0,114354 | **0,117852** | Organic leicht besser, geringere Varianz |

Über alle neun Segmente verbessert Organic den schlechtesten Wert von 0,063164
auf 0,074487 und senkt die Varianz von 0,002531 auf 0,001871. Der Vorteil wird
jedoch stark von den vier Stellwagen-Segmenten mit Schiffslärm und geringer
gemessener Periodizität getragen. Im hochperiodischen Alaska-Ruf verliert
Organic deutlich.

### Interpretation

**Belegt:** Organic ist nicht allgemein schlechter. Es hilft in den vier
Stellwagen-Segmenten und teilweise in Amerikanisch-Samoa.

**Plausibel:** Organic wirkt besonders bei rauen, gestörten oder wenig
periodischen Zielsignalen günstiger, während Morph für stark tonale,
hochperiodische Rufe stabiler ist.

**Nicht belegt:** Dass Rauigkeit selbst die Ursache ist. Population,
Aufnahmegerät, Schiffslärm, Schnappkrebse, Pegel und Rufart ändern sich zugleich.
Eine datenabhängige Aktivierung darf deshalb noch nicht aus diesen Fremddaten
abgeleitet oder abgestimmt werden.

## Architekturentscheidung

### Jetzt

- Morph bleibt die gemeinsame Grundengine.
- Vollständiges Organic wird nicht zum neuen Standard erklärt.
- Es entstehen keine neuen sichtbaren Presets oder Spielmodi.
- Der Laufzeitvertrag und das aktive Profil bleiben unverändert.

### Nächster belastbarer Entwicklungspfad

Eine spätere adaptive Engine sollte **nicht** den vollständigen Organic-Modus
pauschal einschalten. Zu prüfen ist stattdessen:

- schwache, signalgebundene Periodizitäts-/Rauigkeitsformung;
- Registerbass weiterhin ausschließlich an den gespielten Grundton gebunden;
- Resonanzfokus in heutiger Form entfernen oder wesentlich schwächen;
- Subharmonik und Sekundärspur nur bei eindeutig quellseitig belegten
  Ereignissen aktivieren;
- Puls und Artikulation als schwache, gemessene Trajektorien statt dauerhafter
  Klangmodus;
- Intensität aus tatsächlichen Kontrollmerkmalen ableiten, nicht aus dem Namen
  `organic`.

Dieser Pfad gilt nur, wenn ein neuer Entwicklungsdatensatz oder eine sauber
verschachtelte Cross-Validation die Aktivierungslogik bestimmt. Der vorliegende
externe Satz darf dafür nicht verwendet werden.

## Zurückgezogene oder korrigierte Aussagen

- „Organic ist die bessere Grundengine“ ist zurückgezogen.
- „Fünf von acht interne Folds reichen als Generalisierungsbeleg“ ist
  zurückgezogen.
- „Der einzelne negative NOAA-Test widerlegt Organic insgesamt“ ist ebenfalls
  zu stark. Die Zusatzsegmente zeigen kontextabhängige Vorteile.
- Korrekte Aussage: **Morph ist derzeit der robustere Standard; Organic enthält
  einzelne nützliche, aber nicht stabil generalisierende Schichten.**

## Offene Hörfragen

Objektive Metriken beantworten nicht:

- ob Periodizitäts-/Rauigkeitsformung subjektiv natürlicher oder nur körniger
  klingt;
- ob der Registerbass körperhaft oder synthetisch-suboszillatorisch wirkt;
- ob Pitchkontur den UFO-Eindruck tatsächlich reduziert;
- ob der Resonanzrückbau Walcharakter entfernt, obwohl die Metrik steigt;
- wie sich Morph und Organic über MOTU M2 und Focal Clear MG verhalten.

Diese Fragen benötigen später einen geblendeten A/B-Hörtest. Er war nicht Teil
dieser Aufgabe.

## Reproduktion

Die Kandidatendatei erwartet wegen der strikten Repository-Bindung derzeit
einen absoluten Pfad. Die folgenden Befehle erzeugen beziehungsweise prüfen
die kanonischen Artefakte ohne Wiedergabe oder Dienststart:

```bash
python3 scripts/build_whale_external_evaluation_v2.py --check
python3 scripts/summarize_whale_organic_external.py \
  --report assets/whale-sources/studies/organic-ablation-v51/external-report-all.json \
  --output assets/whale-sources/studies/organic-ablation-v51/external-summary.csv \
  --check
python3 scripts/study_whale_organic_ablation.py external \
  --candidate "$(realpath assets/whale-sources/studies/organic-ablation-v51/frozen-candidate.json)" \
  --additional-manifest "$(realpath assets/whale-sources/evaluation-v2/manifest.json)" \
  --output /tmp/buckelwal-organic-v51-external.json
```

Die relative-Pfad-Einschränkung ist eine CLI-Ergonomielücke, kein Fehler der
Studienwerte. Sie darf erst nach dem Kandidatenfreeze separat behoben werden.

## Kanonische Artefakte

- `assets/whale-sources/studies/organic-ablation-v51/definition.json`
- `assets/whale-sources/studies/organic-ablation-v51/internal-report.json`
- `assets/whale-sources/studies/organic-ablation-v51/internal-summary.csv`
- `assets/whale-sources/studies/organic-ablation-v51/frozen-candidate.json`
- `assets/whale-sources/studies/organic-ablation-v51/external-report-noaa.json`
- `assets/whale-sources/studies/organic-ablation-v51/external-report-all.json`
- `assets/whale-sources/studies/organic-ablation-v51/external-summary.csv`
- `assets/whale-sources/evaluation-v2/manifest.json`

Die Roh- und Verarbeitungsdateien des Zusatzsatzes sind ausschließlich externe
Evaluation. Tests verhindern ihren Eintritt in Modellbuilder, Laufzeitauswahl
und Live-Engine.
