# Buckelwal-Songgrammatik v1

Stand: 17. August 2026

## Ziel

Die bisherige Buckelwal-Engine modelliert vor allem den **Klang und die zeitliche
Entwicklung einzelner Wal-Laute**. Ein echter Buckelwalgesang organisiert solche
Laute zusätzlich auf höheren Ebenen:

```text
Einheit → Phrase → Thema → Songzyklus → wiederholte Songzyklen
```

`whale_song_grammar.py` ergänzt deshalb einen **separaten Offline-/Study-Layer**.
Er verändert weder `WhaleMorphVoice` noch `OrganicWhaleMorphVoice`, den Live-
Service, das Roland-Mapping oder den Defaultmodus `morph`.

Der Zweck ist zunächst methodisch: Wir können erstmals prüfen, ob eine
hierarchisch organisierte Sequenz aus derselben vorhandenen Morph-Stimme über
mehrere Minuten überzeugender wirkt als eine handkomponierte Kurzphrase.

## Produktgrenze

Die Songgrammatik ist **kein fünfter Live-Modus**. Sie erzeugt einen
reproduzierbaren Strukturplan und kann einen höchstens 30 Sekunden langen
Ausschnitt über die bestehende `WhaleMorphVoice` rendern.

Damit bleiben zwei Aufgaben getrennt:

- **Instrument:** Der Spieler bestimmt Melodie und Form; `morph` bleibt Default.
- **Naturgesang-Studie:** Die Grammar bestimmt Phrase, Thema, Übergang und
  Songzyklus; die Klangquelle bleibt die bestehende Morph-Stimme.

Diese Trennung verhindert, dass eine biologische Formhypothese die Spielbarkeit
des FP-30X verändert.

## Was wissenschaftlich belegt ist

Die fachliche Grundlage steht in
`docs/knowledge/buckelwal-stimme-und-gesang.md` und den dort gebundenen
Primär-/Fachquellen. Für v1 werden nur folgende strukturelle Aussagen als
**belegt** behandelt:

- Buckelwalgesang ist hierarchisch aus Einheiten, Phrasen und Themen aufgebaut;
- Phrasen werden innerhalb eines Themas wiederholt;
- Wiederholungen sind Varianten einer erkennbaren Familie und nicht
  bitidentische Kopien;
- längere Pausen können Phrasengrenzen markieren;
- mehrere Zeitskalen strukturieren längere Gesänge.

## Was v1 nur als technische Hypothese setzt

Die vorhandenen Quellen liefern noch **kein revisionsgebunden annotiertes
Unit-/Phrase-/Theme-Korpus**, aus dem wir alle Parameter statistisch schätzen
könnten. Deshalb sind folgende Werte bewusst Engineering-Hypothesen:

- die sechs konkreten Motive A–F;
- ihre Tonabstände, Dauern, Dynamik und Pitch-Bend-Werte;
- standardmäßig 3–5 Phrasenwiederholungen pro Thema;
- vier Themen pro Zyklus und zwei Zyklen pro Standardsession;
- die Stärke und Richtung der kleinen Entwicklung zwischen Wiederholungen;
- die Regel für optionale terminale Flourishes;
- die genaue Konstruktion einer Übergangsphrase aus je zwei Units des alten und
  des neuen Themas.

Diese Werte dürfen nicht als gemessene Eigenschaften einer Population, Saison
oder eines konkreten Songs zitiert werden.

## Modell

### Unit

Eine `UnitPlan` enthält nur die musikalische Geste:

- Unit-Typ;
- tastengebundene MIDI-Tonhöhe;
- Dauer und Binnenpause;
- Dynamik;
- begrenztes Pitch-Bend;
- optional wiederholte Pulse.

Die eigentliche Klangfarbe kommt weiterhin aus der source-derived Morph-Bank.

### Phrase

Eine Phrase instanziert ein wiedererkennbares Motiv. Wiederholungen entwickeln
sich **gerichtet und begrenzt**:

- kleine Dauervariation;
- kleine Dynamik-/Bend-Entwicklung;
- gelegentlich eine Änderung um höchstens einen Halbton an einer Fokus-Unit;
- optional ein vom eigenen Motivende abgeleitetes Flourish.

Die Kernfolge der Unit-Typen bleibt innerhalb einer Phrasenfamilie stabil. Das
verhindert unabhängiges Zufallswürfeln von Lauten.

### Thema

Ein `ThemePlan` wiederholt genau eine Phrasenfamilie mehrmals. Die
Wiederholungszahl ist seedgebunden und begrenzt. Die Reihenfolge der Themen ist
in v1 stabil A→B→C→D (bei anderen `theme_count` entsprechend ein Präfix davon).

### Übergang / Hybridphrase

Zwischen zwei Themen steht eine explizite `TransitionPlan`-Phrase. Sie besteht
aus den letzten zwei Units der alten und den ersten zwei Units der neuen
Familie. Damit ist der Übergang strukturell sichtbar und testbar, statt nur eine
lange Pause zwischen unabhängigen Blöcken zu sein.

Das ist eine technische Umsetzung der Idee gerichteter Songveränderung, **keine
Behauptung**, dass echte Buckelwale Übergänge genau nach dieser 2+2-Regel
bilden.

### Songzyklus und Session

Ein `SongCyclePlan` enthält die geordnete Theme-Folge samt Übergängen. Eine
`SongSessionPlan` kann bis zu vier Zyklen enthalten. Harte Grenzen verhindern
ungeprüft wachsende Pläne:

- höchstens 6 Themen;
- höchstens 8 Wiederholungen je Thema;
- höchstens 4 Songzyklen;
- höchstens 512 Units pro Session.

Mit Standardparametern und Seed `0xB0A7` entsteht im aktuellen Stand eine
Session von ungefähr **219 Sekunden** mit 39 Phrasen und 172 Units. Diese Zahlen
sind ein deterministischer Produkt-Smoke, kein biologischer Sollwert.

## Pausenvertrag

Binnenpausen gehören zu Units **zwischen** zwei Units. Die letzte Unit einer
Phrase trägt keine eigene Nachpause. Die größere Pause ist ausschließlich im
Phrase-Objekt gebunden.

Dadurch gilt strukturell:

```text
kleine Binnenpause
< Phrasengrenze
< Übergangsgrenze
< Songzyklusgrenze
```

Die konkreten Defaultwerte (`0,82 s`, `1,35 s`, `2,60 s`) sind Engineering-
Parameter. Nur die qualitative Rolle längerer Pausen an Strukturgrenzen ist
wissenschaftlich motiviert.

## Determinismus

Die Grammar benutzt einen kleinen eigenen 32-Bit-PRNG. Damit hängt ein
revisionsgebundener Plan nicht von Implementierungsdetails des Python-Moduls
`random` ab.

Gleiche Konfiguration + gleicher Seed ergeben:

- dieselbe Hierarchie;
- dieselben Zeitstempel;
- dieselben Varianten;
- denselben kanonischen JSON-Plan;
- denselben `plan_sha256`.

Ein anderer Seed darf Varianten und Wiederholungszahlen ändern, aber nicht die
Grundordnung der Theme-Familien.

## Study-Runner

Strukturbericht ohne Audio:

```bash
python3 scripts/study_whale_song_grammar.py
```

Kleine kontrollierte Strukturprobe:

```bash
python3 scripts/study_whale_song_grammar.py \
  --cycles 1 --themes 2 \
  --phrase-repeats-min 2 --phrase-repeats-max 2
```

Optional kann ausschließlich ein kurzer Präfix über die unveränderte
`WhaleMorphVoice` gerendert werden:

```bash
python3 scripts/study_whale_song_grammar.py \
  --report /tmp/whale-song-grammar.json \
  --render-wav /tmp/whale-song-grammar.wav \
  --render-seconds 12
```

Audio-Rendering ist auf 30 Sekunden begrenzt. Die mehrminütige Struktur wird
primär als Plan evaluiert, damit Tests schnell und deterministisch bleiben.

## Bericht und Quellbindung

Jeder Study-Report enthält SHA-256-Bindungen auf:

- `assets/whale-sources/SOURCES.json`;
- `assets/whale-sources/morph/manifest.json`;
- `docs/knowledge/buckelwal-stimme-und-gesang.md`;
- `scripts/whale_song_grammar.py`;
- `scripts/study_whale_song_grammar.py`.

Zusätzlich trennt der Report explizit:

- `evidence_backed`;
- `engineering_hypotheses`;
- `open_questions`;
- `does_not_establish`.

Ein Report aus einem Dirty-Worktree markiert `git.dirty=true`; ein Commit allein
wird dann nicht fälschlich als vollständige Codeidentität ausgegeben.

## Tests

`tests/test_whale_song_grammar.py` prüft insbesondere:

- exakten Seed-Determinismus;
- Hierarchie und Theme-Reihenfolge;
- Wiederholungen derselben Phrasenfamilie;
- gerichtete Varianten statt unabhängiger Unit-Familien;
- echte Zwei-Familien-Übergänge;
- Phrasengrenzen ohne zeitlich überhängende Unit-Pause;
- mehrminütige, aber begrenzte Standardsessions;
- bounded/sortierte MIDI-Übersetzung mit sicherem All-notes-off;
- unveränderten Live-Produktvertrag (`morph`, `organic`, `realistic`, `ufo`).

## Noch nicht gelöst

v1 löst die **Softwarestruktur**, nicht die wissenschaftliche Parametrisierung.
Der größte nächste Evidenzhebel ist ein revisionsgebundenes, lizenziertes
Referenzset mit Annotationen für Units, Phrasen, Themen, Wiederholungen und
Übergänge. Erst damit sollten wir beispielsweise Verteilungen für
Phrasengröße, Wiederholungszahl, Übergangstypen und gerichtete Varianten aus
realen Songs fitten.

Ebenso offen bleibt eine mehrskalige Blind-/Ähnlichkeitsprüfung gegen echte
Songs. Ein subjektiv überzeugender Klang nach 10 Sekunden beweist noch keine
plausible Songform nach drei Minuten.
