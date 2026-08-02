# Wissen über Buckelwalstimme und Buckelwalgesang

Stand: 2. August 2026

## Zweck und Wahrheitsgrenze

Diese Datei ist die fachliche Referenz für die Buckelwal-Klangarbeit im
Audio-Repository. Sie trennt wissenschaftlich belegte Merkmale, Messungen am
lokal gebundenen Korpus, technische Folgerungen und noch offene Annahmen.

Die Software ist kein biologisch vollständiges Stimmapparatmodell und kein
Artenklassifikator. Sie erzeugt ein musikalisch spielbares, quellengestütztes
Instrument. Die Erweiterung auf A0 bis C8 ist eine musikalische Extrapolation
und keine Behauptung über den natürlichen Stimmumfang eines Buckelwals.

## Stimmerzeugung

### Quelle, Filter und Kopplung

Bartenwale erzeugen Schall mit spezialisierten Strukturen im Kehlkopf. Direkte
Experimente, Anatomie und numerische Modelle zeigen einen eigenständigen
Phonationsmechanismus mit U-förmigem Gewebe beziehungsweise homologen
Stimmfalten und gekoppelten Luft- und Gewebestrukturen.

Für die Modellierung sind drei Ebenen zu trennen:

- **Quelle:** schwingendes Gewebe erzeugt Grundton und harmonische Energie;
- **Filter:** Luftsäcke und Hohlräume formen Resonanzen beziehungsweise
  formantähnliche Verstärkungen;
- **Kopplung:** Quelle und Filter beeinflussen sich und können instabile
  Zustände hervorrufen.

Eine Walstimme ist daher nicht angemessen als einzelner Oszillator mit Vibrato
und statischem Filter beschrieben.

### Physiologische Grenzen

Die natürliche Stimme besitzt anatomisch bedingte Grenzen für Frequenz und
Tauchtiefe. Das 88-Tasten-Instrument hält dagegen die musikalische Tonhöhe der
gespielten Taste fest und überträgt vor allem Klangfarbe, Artikulation und
zeitliche Organisation.

## Akustische Bausteine einzelner Rufe

### Kontinuum zwischen Puls und Ton

Buckelwalrufe können tonal und harmonisch, gepulst, knarrend oder breitbandig
wirken. Ein einzelner Ruf kann sich zwischen diesen Zuständen bewegen.

### Periodizität, Rauigkeit und Chaos

Periodische Abschnitte zeigen einen stabileren Grundton und geordnete Obertöne.
Raue oder chaotische Abschnitte besitzen unregelmäßigere Schwingungen. Echte
deterministische chaotische Abschnitte sind nicht dasselbe wie unabhängiges
Zufallsrauschen: Sie können Reste harmonischer und subharmonischer Struktur
enthalten.

Technische Folgen:

- keine permanente weiße oder rosa Rauschschicht;
- Rauigkeit entsteht aus dem aktiven Quellsignal;
- chaotische Fenster bleiben zeitlich begrenzt;
- nach dem Ausklang ist die Ausgabe exakt digital still.

### Frequenzsprünge

Frequenzsprünge sind abrupte Änderungen einer spektralen Bahn. Sie können durch
einen Zustandswechsel der Quelle, geänderte Quelle-Filter-Kopplung oder das
Kreuzen einer stabilen Grundtonbahn durch eine verschobene Resonanz entstehen.

Für das Instrument ist die letzte Möglichkeit besonders wertvoll: Ein
Formantsprung kann einen deutlichen Walbruch erzeugen, während die gespielte
Grundtonhöhe stabil bleibt. Große zufällige Sprünge des Haupttons erzeugen
dagegen leicht Theremin- oder UFO-Charakter.

### Subharmoniken

Subharmoniken sind periodische Komponenten unterhalb der Hauptfrequenz. Sie
können einen Ruf körperhafter oder gebrochener wirken lassen. Dauerhafte starke
Subharmonik klingt jedoch schnell wie ein synthetischer Suboszillator.

Daher bleibt der Hauptgrundton an die Taste gebunden; Subharmonik tritt nur
schwach und zeitlich begrenzt auf.

### Biphonation und sekundäre Frequenzspuren

Bei Biphonation können zwei teilweise unabhängige Frequenzverläufe gleichzeitig
auftreten. In der spielbaren Umsetzung bleibt die Hauptstimme dominant und
tastengebunden. Die zweite Frequenzspur ist leise, begrenzt und durch
Quelltrajektorien gesteuert, damit sie nicht wie ein Akkord oder
Science-Fiction-Effekt wirkt.

### Hüllkurve und Transienten

Einsätze und Abschlüsse tragen viel Identität. Natürliche Rufe besitzen keine
bloß symmetrische ADSR-Hüllkurve. Druckaufbau, Periodizität, Resonanzöffnung und
Rauigkeit können sich unterschiedlich entwickeln.

Ein plausibles Rufmodell lautet:

```text
instabiler oder dunkler Einsatz
→ Entwicklung von Pegel und Obertonordnung
→ periodischer beziehungsweise gepulster Rufkörper
→ optionales nichtlineares Ereignis
→ asymmetrischer, teilweise wieder rauer Abschluss
```

## Nichtlineare Ereignisse im Gesang

Eine Untersuchung von Buckelwalgesängen aus Madagaskar fand Frequenzsprünge in
im Mittel 35 Prozent und chaotische Abschnitte in im Mittel 41 Prozent der
untersuchten Lautäußerungen. Diese Werte sind korpusabhängig und kein direktes
Soll für die Engine. Entscheidend ist die zeitliche Verteilung:

- Chaos trat besonders häufig am Anfang eines Lauts auf;
- Frequenzsprünge lagen häufiger im mittleren oder letzten Drittel;
- Ereignisse waren nicht zufällig gleichmäßig über den Gesang verteilt;
- mehrere nichtlineare Merkmale konnten im selben Ruf auftreten.

Daraus folgt kein Auftrag zu möglichst viel Chaos. Eine organische Stimme
benötigt kurze, kausal zusammenhängende Zustandswechsel.

## Hierarchie des Buckelwalgesangs

Die übliche Struktur lautet:

```text
Einheit → Phrase → Thema → Folge von Themen beziehungsweise Songzyklus
```

- **Einheit:** einzelner unterscheidbarer Laut;
- **Phrase:** geordnete Folge von Einheiten;
- **Thema:** mehrfache Wiederholung eines Phrasentyps;
- **Songzyklus:** geordnete Folge mehrerer Themen.

Phrasewiederholungen sind nicht bitidentisch. Varianten können sich in
Einheitentypen, Dauer, Intensität, Klangfarbe und Reihenfolge unterscheiden.
Untersuchungen zeigen zugleich starke strukturelle Beschränkungen. Gemessene
Periodizitäten von ungefähr 6 bis 8 sowie 180 bis 400 Einheiten weisen auf
mehrere wirksame Zeitskalen hin.

Thema, Phrase und vorangehende Einheit können die nächste Einheit
mitbestimmen. Längere Pausen markieren oft Phrasengrenzen. Phrasen mit
unterschiedlicher Zahl von Einheiten können dennoch ähnliche Gesamtdauern
besitzen.

## Veränderung und Wiederholung

Buckelwalgesänge verändern sich fortlaufend:

- wiederholte Phrasen bleiben als Familie erkennbar;
- Varianten sind gerichtet statt beliebig zufällig;
- Veränderungen können über mehrere Zeitskalen ähnlich verlaufen;
- innerhalb eines Themas existieren stereotype und komplexere Phrasentypen;
- Populationen können gemeinsame Songmuster besitzen, während individuelle und
  saisonale Varianten fortbestehen.

Technische Folgen:

- derselbe Tastendruck darf nicht immer dieselbe Mikrotrajektorie erzeugen;
- Wiederholungen dürfen aber nicht unverbunden zufällig klingen;
- ein Phrasengedächtnis soll Variantenfamilien, Intensität und bereits benutzte
  Ereignisse berücksichtigen;
- die Engine spielt niemals selbstständig eine fertige Melodie anstelle des
  Spielers.

## Lokales, quellengebundenes Korpus

Das Repository enthält 19 verarbeitete Clips aus acht Quellfamilien. Quellen
und Lizenzen sind in `assets/whale-sources/SOURCES.json`,
`assets/whale-sources/NOTICE.md` und
`assets/whale-sources/processed/manifest.json` gebunden. Der Builder verlangt
exakt diese acht Familien. Eine fehlende oder unerwartete Familie verändert
nicht stillschweigend den Bewertungs- oder Laufzeitbestand, sondern blockiert
den Modellbau.

## Lokal extrahierte Steuermerkmale

`scripts/build_whale_voice_model.py` liest jeden Clip einmal als unveränderlichen,
hashgeprüften Byte-Snapshot. Vor der Reduktion von 48 auf 4 kHz begrenzt ein
achtpoliger Tiefpass das Signal auf 1.650 Hz; erst danach wird um Faktor zwölf
dezimiert. Damit werden hohe Aufnahmeanteile nicht wie in der ersten v5-Fassung
in den Analysebereich zurückgefaltet.

Jeder Clip liefert 48 normalisierte Zeitpunkte mit:

- Hüllkurve;
- Periodizität und ihrem komplementären Rauigkeitswert;
- Hochfrequenzanteil innerhalb des analysierten Bandes;
- spektraler Neigung;
- achtteiliger harmonischer Energieverteilung;
- zwei groben harmonischen Resonanzschwerpunkten;
- Pulsrate und Pulsstärke;
- Subharmonik;
- getrenntem Verhältnis und Stärke einer sekundären Frequenzspur.

Die Resonanzschwerpunkte sind ausdrücklich **keine biologisch identifizierten
Formanten**. Sie sind eine robuste, begrenzte Heuristik aus betonten
Harmonischen. Aussagen über echte Resonanzräume bleiben wissenschaftliche
Motivation, nicht Messbehauptung dieses Builders.

## Aktueller Grundengine-Befund

Die revisionsgebundene Ablationsstudie vom 2. August 2026 trennt zehn
Organic-Schichten. Keine Variante erfüllt die vorab festgelegten internen
Robustheitskriterien; Morph bleibt deshalb die kanonische Grundengine.

Gut belegt ist innerhalb des lokalen Korpus:

- Periodizitäts-/Rauigkeitsformung besitzt den größten isolierten
  Ähnlichkeitsgewinn, ist aber über Familien instabil;
- der registergebundene Bass verbessert einzelne tiefe beziehungsweise
  schwierige Folds, ist jedoch kein allgemeiner Ähnlichkeitsgewinn;
- Resonanzfokus, Subharmonik und sekundäre Frequenzspur sind in ihrer heutigen
  dauerhaften Aktivierung überwiegend neutral bis schädlich.

Laufzeitreserve wird in separaten Produkt- und Runtime-Tests geprüft.
Hostabhängige Wandzeitwerte gehören nicht in den eingefrorenen
Wissenschaftsbericht und beeinflussen die Kandidatenwahl nicht.

Die externe Evidenz ist widersprüchlich: Im gesperrten hochperiodischen
Alaska-Ruf gewinnt Morph klar. In vier Stellwagen-Segmenten mit Schiffslärm und
sehr niedriger Periodizität gewinnt Organic. In der bandbegrenzten
Amerikanisch-Samoa-Aufnahme gewinnt Morph im Mittel knapp; Organic verbessert
nur eines von vier Segmenten, aber den schlechtesten Fall. Die linear
interpolierte Vorfassung dieses Zusatztests wurde nach externem Review
verworfen und mit Lanczos-Sinc neu berechnet. Zugleich sättigt der einfache
Autokorrelations-Pitchtracker in drei Stellwagen-Segmenten überwiegend am
kürzesten Lag und meldet 1.333,33 Hz bei nur ungefähr 0,33 bis 0,36
Periodizität. Dieser Wert ist als Störgeräusch- beziehungsweise Suchrandartefakt
zu behandeln. Das legt eine aufnahmeabhängige Wirkung nahe, belegt aber keine
kausale adaptive Schaltung, weil Population, Rufart und Aufnahmebedingung
gleichzeitig variieren. Der externe Satz bleibt für Tuning gesperrt.

Die vollständige Methodik und alle Hashbindungen stehen in
`docs/experiments/buckelwal-organic-v51-ablation.md`.

## Historischer technischer v5-Zielzustand

### Schallquelle

Die bandbegrenzte, phasenkontinuierliche Morphquelle bleibt erhalten. Ihr
Hauptgrundton folgt der gespielten Taste.

### Zeitvariables Source-Filter-Modell

Quelltrajektorien steuern relativ zur Rufphase:

- dunkle beziehungsweise helle spektrale Gewichtung;
- harmonische Energie über alle acht gespeicherten Bänder;
- zwei bewegliche Resonanzschwerpunkte;
- Periodizität und signalgebundene Rauigkeit;
- Pulsgruppen;
- schwache Subharmonik;
- eine getrennt geschätzte sekundäre Frequenzspur;
- asymmetrische Hüllkurvenentwicklung.

Die Laufzeitauswahl ist zweistufig: zuerst wird eine Quellfamilie gleichgewichtet
gewählt, anschließend ein Clip innerhalb dieser Familie. Unterschiedliche
Clipzahlen geben einer Familie damit kein höheres Gewicht.

Lange Töne spielen keinen Audioloop. Jede ausgewählte Trajektorie behält ihre
eigene begrenzte Dauer. Am Beginn der folgenden Einheit wird der Endzustand der
vorherigen Einheit über 14 Prozent der neuen Einheit in deren Anfangszustand
überführt; es gibt keinen Rücksprung von einer vorab eingeblendeten Phase auf
Phase null.

### Tiefbass

A0 und A1 erhalten einen grundtongebundenen Basskörper aus tiefpassgefiltertem
Quellsignal, Grundwelle und schwacher zweiter Harmonischer. Er muss die
Tastaturtonhöhe erhalten, darf nicht wie ein dauerhafter Suboszillator wirken
und bleibt unter der harten Pegelgrenze.

### Anti-UFO-Vertrag

- zusätzliche Organic-Tonhöhenbewegung unter 20 Cent;
- Organic-Legato höchstens 180 Millisekunden;
- größere Brüche primär in Resonanzen und Sekundärkomponenten;
- keine langsame zweite Glissandokurve über der Morphquelle;
- keine zufälligen großen Sprünge des Hauptgrundtons.

### Stille, Determinismus und Integrität

- im Leerlauf exakt null Ausgabe;
- nach endlichem Ausklang exakt null Ausgabe;
- keine permanente Rauschschicht;
- gleiche Gesten und Ausgangszustände liefern bitidentische Ausgabe;
- Render-Chunkgrößen verändern Ausgabe und Zustand nicht;
- der Loader und der Live-Doctor verlangen den im Profil festgeschriebenen
  SHA-256 der vollständigen Modellbank;
- auch eine schema-gültige Änderung eines einzelnen Steuerwerts blockiert den
  Start.

## Bewertung

Die erste v5-Bewertung wurde nachträglich als methodisch zu stark bezeichnet:
Sie reduzierte jeden Clip auf Medianwerte, gewichtete Periodizität und deren
Komplement Rauigkeit doppelt und verwendete die als Holdout bezeichneten
Familien während der Entwicklung zur Abstimmung. Ihr früherer Wert ist daher
kein unabhängiger Generalisierungsnachweis.

Die korrigierte Prüfung verwendet Leave-one-source-family-out-Cross-Validation:

1. Eine vollständige Quellfamilie wird aus der Organic-Liveauswahl entfernt.
2. Dieselbe feste Spielgeste wird gerendert.
3. Das Ergebnis wird als geordnete 48-Punkt-Trajektorie analysiert.
4. Hüllkurve, Periodizität, Spektrum, Resonanzschwerpunkte, Puls,
   Subharmonik, Sekundärspur und vollständiges Obertonprofil werden zeitpunktweise
   verglichen.
5. Jede der acht Familien bildet genau einen gleichgewichteten Außenfold.

Diese Cross-Validation ist ein ehrlicher Regressionstest innerhalb des kleinen
bekannten Korpus. Sie ist weiterhin kein Artenklassifikator und kein Beleg
menschlich wahrgenommener Echtheit.

Zusätzlich ist unter `assets/whale-sources/evaluation/` eine zuvor unbenutzte,
nach Abschluss der DSP-Reparaturen gesperrte NOAA-PMEL-Aufnahme aus Alaska
gebunden. Sie darf weder Modellbau noch Parameterabstimmung beeinflussen. Der
Vergleich ergibt für diesen einzelnen Ruf eine Ähnlichkeit von `0,1707` für
Morph und `0,1538` für Organic. Organic ist diesem Fremdruf also weniger
ähnlich. Unter `assets/whale-sources/evaluation-v2/` liegen zwei weitere
Rohaufnahmen mit acht vorab festgelegten Segmenten. Nach Sinc-korrigierter
Aufbereitung gewinnt Organic in Stellwagen, verliert im Mittel knapp in
Amerikanisch-Samoa und liegt über alle neun Segmente nur leicht vorn. Diese
Aufnahmen sind ein begrenzter externer Robustheitstest, aber kein Beleg über
Populationen, Rufarten oder menschliche Wahrnehmung.

## Evidenzstufen

### Gut belegt

- laryngeale Schallproduktion mit spezialisierten Strukturen;
- Quelle-Filter-Kopplung;
- tonale, gepulste und nichtlineare Klangzustände;
- Frequenzsprünge und deterministisches Chaos;
- hierarchische Organisation aus Einheiten, Phrasen und Themen;
- strukturelle Beschränkungen und Wiederholung mit Varianten.

### Plausible technische Übertragung

- quellabgeleitete Kontrolltrajektorien wirken natürlicher als frei erfundene
  Zufallsmodulation;
- bewegliche Resonanzschwerpunkte können Walcharakter erhöhen, ohne die
  Tastaturtonhöhe zu zerstören;
- gestengebundene Varianten wirken organischer als identische Wiederholungen;
- relatives Phrasengedächtnis kann lange Spielpassagen kohärenter machen.

### Noch offen

- welche Einzelmerkmale subjektiv den größten Anteil am Walcharakter haben;
- wie stark Biphonation sein darf, bevor sie als Akkord wahrgenommen wird;
- wie weit der Eindruck über acht chromatische Oktaven trägt;
- ob die korrigierte v5-Fassung über die reale Wiedergabekette weniger nach UFO,
  Orgel oder Buzz klingt;
- ob optionaler Unterwasserraum Immersion erhöht oder nur Fehler verdeckt;
- wie sich Morph und schwache adaptive Komponenten auf einem größeren, nach
  Rufart und Aufnahmebedingung kontrollierten Satz unabhängiger Aufnahmen
  verhalten;
- ob die in geräuschreichen Segmenten gemessenen Organic-Vorteile Walmerkmale
  oder lediglich Aufnahme- und Störgeräuschähnlichkeit abbilden.

## Primär- und Fachquellen

- Elemans, C. P. H. et al. (2024): *Evolutionary novelties underlie sound
  production in baleen whales*. DOI: https://doi.org/10.1038/s41586-024-07080-1
- Cazau, D. et al. (2016): *A study of vocal nonlinearities in humpback whale
  songs*. DOI: https://doi.org/10.1038/srep31660
- Suzuki, R., Buck, J. R. und Tyack, P. L. (2006): *Information entropy of
  humpback whale songs*. DOI: https://doi.org/10.1121/1.2161827
- Handel, S., Todd, S. K. und Zoidis, A. M. (2012): *Hierarchical and rhythmic
  organization in the songs of humpback whales*.
  DOI: https://doi.org/10.1080/09524622.2012.668324
- Murray, A. et al. (2018): *Stereotypic and complex phrase types provide
  structural evidence for a multi-message display in humpback whales*.
  DOI: https://doi.org/10.1121/1.5023680
- Schneider, J. N. und Mercado, E. (2019): *Characterizing the rhythm and tempo
  of sound production by singing whales*.
  DOI: https://doi.org/10.1080/09524622.2018.1428827
- Mercado, E. (2021): *Song Morphing by Humpback Whales*.
  DOI: https://doi.org/10.3389/fpsyg.2020.574403

## Repositorybezug

- Quellen: `assets/whale-sources/SOURCES.json`
- Lizenzen: `assets/whale-sources/NOTICE.md`
- Verarbeitete Clips: `assets/whale-sources/processed/manifest.json`
- Periodische Morphquelle: `assets/whale-sources/morph/manifest.json`
- Zeitmodell: `assets/whale-sources/voice-model/manifest.json`
- Modell-Builder: `scripts/build_whale_voice_model.py`
- Source-Filter-Engine: `scripts/whale_source_filter_engine.py`
- Holdout-Prüfung: `scripts/evaluate_whale_voice_model.py`
- Ablationsstudie: `docs/experiments/buckelwal-organic-v51-ablation.md`
- externer Zusatzsatz: `assets/whale-sources/evaluation-v2/manifest.json`
