# Buckelwal-Grundengine: Organic-v5.1-Ablations- und Generalisierungsstudie

Stand: 2. August 2026

## Entscheidung

**Morph bleibt die kanonische Grundengine.**

Keine Organic-Variante erfüllt die vorab festgelegten akustischen,
familienübergreifenden und Tiefbass-Kriterien. Der eingefrorene Kandidat besitzt
keine aktivierte Organic-Komponente und ist damit bitgenau Morph.

Die externen Auswertungen rechtfertigen keine nachträgliche Änderung:
Vollständiges Organic hilft reproduzierbar in der einen Stellwagen-Aufnahme mit
Schiffslärm, verliert jedoch im hochperiodischen Alaska-Ruf deutlich und ist in
der bandbegrenzten Amerikanisch-Samoa-Aufnahme im Mittel knapp schlechter als
Morph. Eine neue v5.1-Laufzeitengine wird daher nicht veröffentlicht.

## Review-Amendment

Zwei externe Reviews fanden vor dem Merge einen echten Methodikfehler und
mehrere Architekturprobleme. Daraufhin wurde die Studie korrigiert und
vollständig neu ausgeführt.

### Korrigiert

- Die lineare 5/44,1-kHz-zu-48-kHz-Interpolation wurde durch eine deterministische
  32-Tap-Lanczos-Fenster-Sinc-Interpolation ersetzt.
- Die Randfades erreichen nun exakt null.
- Die früheren linearen v2-Derivate und alle daraus berechneten externen Werte
  sind ausdrücklich **ungültig**.
- Rohdateien und Segmentgrenzen blieben unverändert; es gab keine Hörprobe,
  Neu-Auswahl oder Anpassung an Engine-Ergebnisse.
- Der eingefrorene Morph-Kandidat wird extern als Alias auf das bereits
  berechnete Morph-Ergebnis geführt statt nochmals gerendert.
- Hostabhängige Wandzeitwerte wurden aus den kanonischen Berichten und aus der
  Kandidatenwahl entfernt. Laufzeitreserve bleibt ein separater Produktvertrag.
- Produktverträge werden pro Variante zusammengefasst statt vollständig in
  jedem Fold wiederholt.
- Unabhängige Folds können über höchstens vier Prozesse ausgewertet werden.
- Relative Repositorypfade für Kandidat und Zusatzmanifest werden unterstützt.
- Alaska besitzt nun Population, Rufklassifikation, Aufnahmebedingungen und
  eine Aufnahme-ID.
- Distanzskalierung, Feature-Cap und Aggregation sind im Bericht explizit.

### Nicht geändert

- die zehn Organic-Komponenten;
- die acht gleichgewichteten internen Quellfamilien;
- die festen Spielgesten und 48 Kontrollpunkte;
- Feature-Skalen und Distanzfunktion;
- Schwellen für Mittelwertgewinn, Worst-Fold, Familienzahl und Tiefbass;
- Segmentgrenzen der externen Aufnahmen;
- die Engineparameter;
- der nach externer Auswertung nicht mehr abstimmbare Kandidatensatz.

Das Entfernen des Wandzeitkriteriums ist entscheidungsneutral: Alle Varianten
hatten den bisherigen Laufzeitgrenzwert bestanden. Morph bleibt auch ohne dieses
hostabhängige Kriterium der ausgewählte Kandidat.

## Wahrheitsgrenze

Die Studie vergleicht eine feste musikalische Spielgeste mit extrahierten
akustischen Trajektorien. Sie prüft technische Reproduzierbarkeit, Robustheit
über bekannte Quellfamilien und begrenzte externe Übertragbarkeit.

Sie beweist nicht:

- biologische Identität;
- menschlich wahrgenommene Echtheit;
- Generalisierung über die Art oder alle Populationen;
- statistische Unabhängigkeit mehrerer Segmente derselben Aufnahme;
- dass Störgeräuschähnlichkeit Walstimmenähnlichkeit bedeutet;
- gleiche Laufzeit auf anderen Rechnern.

Keine Aufnahme wurde physisch wiedergegeben. Der Wal-Dienst blieb inaktiv.

## Revisions- und Hashbindung

| Gegenstand | Bindung |
|---|---|
| Studienquellstand | `9fe1238d5a669cad350fdc893734c6689bfe4f65` |
| Modellbank | `1bbd10566bbfc9ee9159c994de456d408ed003cea65602faee8076b308d0ee8a` |
| Studiendefinition | `6266cafa0ed2ebb79081669963b704dc71632957964d46165b996e4de8dafeba` |
| interner Bericht | `ff99cec2d834372d2f586cd8a628200055c4ceb0d624c10ced7b34381752d7c6` |
| interne CSV | `bf36640b99c8325979d81deb84d4652e37bd19d3bfed919cee0103d1ceb7dd05` |
| eingefrorener Kandidat | `73558c8a080e978c98705f19d498d415704e9841456ed2f8f14907b7ae6e236f` |
| Alaska-Manifest | `9bdcf78fdc4d0f1fce77d4e2defa877910ea5102e66e515704fceaee123dd39d` |
| Zusatzmanifest v2 | `e42272161dd3864950b7b17f1cecce810b20ab83fec1a71ab763b10c9c07166f` |
| Alaska-Bericht | `a14fefcf49e1c96bd5dd8c8f30e13fd1d49e762f610e0e05b6499fe584b9517a` |
| externer Gesamtbericht | `d5d8408cbf3ae6724c49fd5fb7b04744e940c67f843ac6d46cff5817c30edb0d` |
| externe CSV | `a47192302cc8ba868cad3c2505eebcf6051e5eade9989d445990ad5b04df6985` |

Der Kandidat wurde aus dem internen Bericht erzeugt und vor externer
Auswertung festgeschrieben. Die externen Berichte deklarieren
`parameters_changed_after_external_results: false` und enthalten keine
Wandzeitwerte.

## Interne Methodik

### Matrix

Die deterministische Basismatrix enthält 22 Varianten:

- Morph;
- vollständiges Organic;
- zehn Leave-one-component-out-Varianten;
- zehn isolierte Komponenten über Morph.

Kombinationen werden nur erzeugt, wenn Komponenten sowohl isoliert als auch
beim Entfernen aus vollständigem Organic vorab definierte Evidenzgrenzen
bestehen. Keine Komponente qualifizierte sich; deshalb entstand keine
nachträglich optimierte Kombination.

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

Alle Schalter aus ergeben bitgenau Morph. Alle Schalter an ergeben bitgenau das
bisherige vollständige Organic.

### Distanzmodell

Jeder Fold vergleicht geordnete 48-Punkt-Trajektorien. Hüllkurve,
Periodizität, Hochbandanteil, spektrale Neigung, zwei Resonanzverhältnisse,
Pulsrate, Pulsstärke, Subharmonik, sekundäre Stärke, sekundäres Verhältnis und
das achtteilige Obertonprofil werden durch festgelegte Feature-Skalen
normalisiert. Jede Feature-Distanz pro Kontrollpunkt wird bei `8,0` gekappt;
anschließend wird gleichgewichtet gemittelt.

Periodizität wird einmal gewichtet. Rauigkeit wird nicht nochmals als
`1 − Periodizität` in die Distanz aufgenommen.

### Produktverträge

Die Studie bindet pro Variante:

- Peak höchstens `0,25`;
- exakte Tonhöhenbindung über 88 Tasten;
- exakte digitale Stille im Leerlauf;
- bitgenaue Chunk-Invarianz;
- Tiefbasswerte für A0, A1 und A2.

Laufzeitmessungen sind nicht Teil des eingefrorenen Wissenschaftsberichts. Die
bestehenden Runtime-Tests prüfen Echtzeitreserve separat, weil Wandzeit keine
bitreproduzierbare Eigenschaft eines Artefakts ist.

## Interne Ergebnisse

### Baselines

| Variante | Mittel | Median | schlechtester Fold | verbessert / verschlechtert | Peak |
|---|---:|---:|---:|---:|---:|
| Morph | 0,144732 | 0,152634 | 0,080041 | 0 / 0 | 0,0903 |
| vollständiges Organic | 0,148678 | 0,149587 | 0,088430 | 5 / 3 | 0,2078 |

Die akustischen Ergebnisse sind gegenüber der ersten Studienfassung bitgleich.
Organic gewinnt im Mittel, verletzt aber in mindestens einem Familienfold die
vorab erlaubte Verlustgrenze. Der Mittelwert allein reicht nicht zur Freigabe.

### Einzelkomponenten

| Komponente | isolierter Befund | Herausnahme aus Organic | Urteil |
|---|---|---|---|
| Periodizität/Rauigkeit | Mittel 0,152953; 5/8 Familien besser; schlechtester Fold 0,099566 | Organic ohne sie fällt auf 0,146895 | intern stärkste Komponente, aber ein Familienverlust ist zu groß |
| Registerbass | Mittel 0,146458; isolierter schlechtester Fold 0,103980; 3/8 besser | Organic ohne ihn fällt auf 0,146533 | für Tiefe funktional wichtig, kein allgemeines Ähnlichkeitsmodul |
| Resonanzfokus | 0/8 besser; Mittel 0,137513 | Herausnahme verbessert Organic auf 0,150935 | stärkster Rückbaukandidat in heutiger Dauerform |
| Obertonprofil | Mittel 0,142653; 2/8 besser | Herausnahme verbessert Organic leicht auf 0,149236 | überwiegend Färbung, keine robuste Generalisierung |
| Hüllkurve | Mittel 0,142560; 2/8 besser | Herausnahme nahezu neutral | neutral bis leicht schädlich in heutiger Stärke |
| Puls | Mittel 0,144176; 4/8 besser | Herausnahme verschlechtert Organic leicht | kleine kontextabhängige Wirkung |
| Artikulationszustände | Mittel 0,144343; 4/8 besser | Herausnahme verschlechtert Organic leicht | kleine kontextabhängige Wirkung |
| Pitchkontur | Mittel 0,143515; 2/8 besser | Herausnahme verschlechtert Organic leicht | kein objektiver Generalisierungsvorteil |
| Subharmonik | 0/8 besser; Mittel 0,142771 | Herausnahme nahezu neutral | in Daueraktivierung statistisch unnötig bis schädlich |
| sekundäre Frequenzspur | 0/8 besser; Mittel 0,142811 | Herausnahme nahezu neutral | in Daueraktivierung statistisch unnötig bis schädlich |

### Pareto-Front

Die interne Pareto-Front verwendet mittlere Ähnlichkeit, schlechtesten Fold,
Komponentenanzahl und Produktverträge. Sie enthält:

- Morph;
- nur Periodizität/Rauigkeit;
- nur Registerbass.

Die beiden Einzelvarianten scheitern dennoch an den vorab festgelegten
Freigabekriterien. Morph bleibt der robuste Rückfallpunkt.

## Berichtshygiene und Durchsatz

Der interne JSON-Bericht schrumpfte durch Normalisierung von `359.148` auf
`309.168` Byte und von `9.067` auf `7.681` Zeilen. Alle `220`
Wandzeitfelder wurden entfernt. Produktverträge stehen pro Variante; nur
bankabhängige Bass- und Chunk-Werte verbleiben auf Fold-Ebene.

Der interne Lauf kann die acht unabhängigen Folds pro Variante über höchstens
vier Prozesse verteilen. Die Reihenfolge der Ergebnisse bleibt deterministisch.
Ein kompiliertes Backend ist nicht Teil dieser Änderung.

## Externe Generalisierung

### Gesperrter Alaska-Test

Die Audiodateien und akustischen Werte bleiben unverändert. Nur Metadaten,
Aliasdarstellung und Berichtsschema wurden verbessert.

| Variante | Ähnlichkeit | Distanz | Peak |
|---|---:|---:|---:|
| Morph / eingefrorener Kandidat | **0,170738** | **1,767625** | 0,0903 |
| vollständiges Organic | 0,153790 | 1,872165 | 0,2078 |

Organic verliert in diesem stark periodischen Ruf deutlich.

### Zusatzsatz v2

Der Zusatzsatz enthält zwei unabhängige Rohaufnahmen:

- Stellwagen Bank, 44,1 kHz, Rufe mit Schiffslärm;
- Amerikanisch-Samoa, 5 kHz, Rufe mit Schnappkrebsen.

Aus jeder Aufnahme stammen vier vorab fixierte, nicht überlappende
Zwei-Sekunden-Segmente. Zusammen mit Alaska sind das neun Segmente aus drei
unabhängigen Feldaufnahmen. Die acht Zusatzsegmente sind untereinander nicht
unabhängig, weil je vier aus derselben Aufnahme stammen.

Die 5-kHz-Samoa-Aufnahme besitzt eine Nyquist-Grenze von 2,5 kHz. Der
Analyse-Tiefpass liegt bei 1,65 kHz und damit unter dieser Grenze; trotzdem kann
dieser Datensatz keine Information über höhere Quellanteile liefern. Er ist ein
bandbegrenzter Robustheitstest, kein vollbandiger Stimmvergleich.

### Korrigierte Sinc-Ergebnisse

| Aufnahme/Satz | Morph | vollständiges Organic | Einordnung |
|---|---:|---:|---|
| Alaska, 1 Segment | **0,170738** | 0,153790 | Morph klar besser |
| Stellwagen, 4 Segmente | 0,066128 | **0,076069** | Organic in allen vier Segmenten besser |
| Amerikanisch-Samoa, 4 Segmente | **0,133911** | 0,133594 | Morph im Mittel knapp besser; Organic verbessert den schlechtesten Fall |
| alle 9 Segmente | 0,107877 | **0,110271** | Organic leicht besser, geringere Varianz |

Über alle neun Segmente verbessert Organic den schlechtesten Wert von
`0,063110` auf `0,074427` und senkt die Varianz von `0,002081` auf
`0,001394`. Der Gesamtvorteil beträgt jedoch nur ungefähr `0,002394` und wird
praktisch vollständig von Stellwagen getragen.

In Amerikanisch-Samoa gewinnt Organic nur eines von vier Segmenten. Die
frühere Aussage „je zwei Segmentgewinne“ beruhte auf den ungültigen linear
interpolierten Derivaten und ist zurückgezogen.

### Interpretation

**Belegt:**

- Morph bleibt für den Alaska-Ruf deutlich näher.
- Organic ist in allen vier Stellwagen-Segmenten näher.
- Amerikanisch-Samoa zeigt keinen mittleren Organic-Vorteil.
- Die externe Wirkung ist aufnahmeabhängig und nicht allgemein stabil.

**Plausibel:**

Organic kann in stark gestörten, wenig periodischen Zielsignalen einige
Merkmaldistanzen reduzieren. Dass dies eine bessere Walstimme statt nur eine
bessere Anpassung an Störstruktur bedeutet, ist nicht belegt.

**Spekulativ:**

Eine adaptive Aktivierung von Periodizitäts-/Rauigkeitsformung könnte sinnvoll
sein. Diese Hypothese darf nicht aus dem externen Satz parametrisiert werden,
weil Population, Rufart und Aufnahmebedingung gleichzeitig variieren.

## Architekturentscheidung

### Jetzt

- Morph bleibt Standard und gemeinsamer Grundsignalpfad.
- Vollständiges Organic bleibt experimentelle Referenz.
- Es entstehen keine neuen sichtbaren Presets oder Modi.
- Aktives Profil und Laufzeitvertrag bleiben unverändert.
- Externe Ergebnisse fließen nicht in Parameter oder Schwellen zurück.

### Nächster belastbarer Pfad

Eine spätere Studie sollte:

- mehr unabhängige Rohaufnahmen statt mehr Segmente derselben Dateien verwenden;
- Rufart, Population, Störbedingung und Bandbreite getrennt kontrollieren;
- einen neuen Entwicklungsdatensatz für adaptive Aktivierungslogik reservieren;
- den hier verwendeten externen Satz dauerhaft als Testmenge sperren;
- Periodizitäts-/Rauigkeitsformung und Registerbass getrennt prüfen;
- Resonanzfokus, Subharmonik und sekundäre Spur nicht pauschal dauerhaft
  aktivieren;
- subjektive Natürlichkeit in einem geblendeten A/B-Hörtest separat messen.

Eine Lockerung der bestehenden Worst-Fold-Schwelle nach Sichtung der Ergebnisse
ist ausdrücklich kein zulässiger Pfad.

## Reproduktion

```bash
python3 scripts/build_whale_external_evaluation_v2.py --check
python3 scripts/summarize_whale_organic_external.py \
  --report assets/whale-sources/studies/organic-ablation-v51/external-report-all.json \
  --output assets/whale-sources/studies/organic-ablation-v51/external-summary.csv \
  --check
python3 scripts/study_whale_organic_ablation.py external \
  --candidate assets/whale-sources/studies/organic-ablation-v51/frozen-candidate.json \
  --additional-manifest assets/whale-sources/evaluation-v2/manifest.json \
  --output /tmp/buckelwal-organic-v51-external.json
```

## Kanonische Artefakte

- `assets/whale-sources/studies/organic-ablation-v51/definition.json`
- `assets/whale-sources/studies/organic-ablation-v51/internal-report.json`
- `assets/whale-sources/studies/organic-ablation-v51/internal-summary.csv`
- `assets/whale-sources/studies/organic-ablation-v51/frozen-candidate.json`
- `assets/whale-sources/studies/organic-ablation-v51/external-report-noaa.json`
- `assets/whale-sources/studies/organic-ablation-v51/external-report-all.json`
- `assets/whale-sources/studies/organic-ablation-v51/external-summary.csv`
- `assets/whale-sources/evaluation/manifest.json`
- `assets/whale-sources/evaluation-v2/manifest.json`
- `assets/whale-sources/evaluation-v2/NOTICE.md`

Tests verhindern den Eintritt externer Audiodateien in Modellbuilder,
Laufzeitauswahl und Live-Engine.