# Wissen über Buckelwalstimme und Buckelwalgesang

Stand: 1. August 2026

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
`assets/whale-sources/processed/manifest.json` gebunden.

Der v5-Analyse-Builder teilt ganze Quellfamilien, nicht einzelne Ausschnitte.

### Training

- `humpback-moo-nps`
- `humpback-wheezeblow-nps`
- `song-antarctic-area-v-2010`
- `song-foraging-mn132a`
- `song-new-caledonia-2010`

### Holdout

- `humpback-song-cc0`
- `song-eastern-australia-2010`
- `song-foraging-mn133a`

Damit kann keine Quellfamilie zugleich Live-Trajektorien liefern und den
Holdout-Erfolg bestimmen.

## Lokal gemessene Muster

Der Builder analysiert jeden Clip als unveränderlichen, hashgeprüften
Byte-Snapshot. Er erzeugt 48 relative Kontrollpunkte mit Hüllkurve,
Periodizität, Rauigkeit, Hochfrequenzanteil, spektraler Neigung, achtteiliger
Obertonverteilung, zwei Resonanzverhältnissen, Pulsrate, Subharmonik und einer
sekundären Frequenzspur.

Im Trainingskorpus zeigt der Medianverlauf:

| relative Rufphase | Periodizität | Rauigkeit | Pulsstärke | Interpretation |
|---:|---:|---:|---:|---|
| 0,00 | 0,236 | 0,764 | 0,167 | unruhiger Einsatz |
| 0,09 | 0,330 | 0,670 | 0,173 | frühe Entwicklung |
| 0,26 | 0,208 | 0,792 | 0,319 | raues Übergangsfenster |
| 0,51 | 0,827 | 0,173 | 0,697 | geordneter, gepulster Rufkörper |
| 0,77 | 0,522 | 0,478 | 0,137 | Auflösung des Körpers |
| 0,91 | 0,272 | 0,728 | 0,141 | rauer Abschluss |
| 1,00 | 0,233 | 0,767 | 0,179 | Endtransiente |

Diese Werte beschreiben nur das kleine lokale Korpus. Aufnahmegeräusch, Art des
Lauts und Quellbedingungen wirken mit; sie sind keine biologischen Konstanten.

## Technischer Zielzustand der Grundengine

### Schallquelle

Die bandbegrenzte, phasenkontinuierliche Morphquelle bleibt erhalten. Ihr
Hauptgrundton folgt der gespielten Taste.

### Zeitvariables Source-Filter-Modell

Quelltrajektorien steuern relativ zur Rufphase:

- dunkle beziehungsweise helle spektrale Gewichtung;
- bewegliche Resonanzen;
- Periodizität und signalgebundene Rauigkeit;
- Pulsgruppen;
- schwache Subharmonik;
- seltene sekundäre Frequenzspur;
- asymmetrische Hüllkurvenentwicklung.

Lange Töne spielen keinen Audioloop. Nach einer relativen Rufeinheit folgt eine
verwandte nächste Steuertrajektorie mit Kreuzblende.

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

### Stille und Determinismus

- im Leerlauf exakt null Ausgabe;
- nach endlichem Ausklang exakt null Ausgabe;
- keine permanente Rauschschicht;
- gleiche Gesten und Ausgangszustände liefern bitidentische Ausgabe;
- Render-Chunkgrößen verändern Ausgabe und Zustand nicht.

## Bewertung

Kein einzelner Gesamtwert darf die Entwicklung steuern. Nötig sind getrennte
Prüfungen für Hauptton, zeitliche und spektrale Natürlichkeit, Tiefbass, Pegel,
Phrasenvariation, Echtzeitreserve, Holdout-Generalisierung und die subjektive
Hörprüfung über Roland FP-30X, MOTU M2 und Focal Clear MG.

Ein objektiver Vergleich ist ein Regressionsindikator. Er beweist weder
biologische Identität noch menschlich wahrgenommene Echtheit.

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
- Formantbewegung kann Walcharakter erhöhen, ohne die Tastaturtonhöhe zu
  zerstören;
- gestengebundene Varianten wirken organischer als identische Wiederholungen;
- relatives Phrasengedächtnis kann lange Spielpassagen kohärenter machen.

### Noch offen

- welche Einzelmerkmale subjektiv den größten Anteil am Walcharakter haben;
- wie stark Biphonation sein darf, bevor sie als Akkord wahrgenommen wird;
- wie weit der Eindruck über acht chromatische Oktaven trägt;
- ob v5 über die reale Wiedergabekette weniger nach UFO, Orgel oder Buzz klingt;
- ob optionaler Unterwasserraum Immersion erhöht oder nur Fehler verdeckt.

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
