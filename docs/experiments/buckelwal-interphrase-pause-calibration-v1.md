# Buckelwal-Interphrase-Pausenkalibrierung v1

Status: **T044 Holdout einmalig ausgewertet; kein Live-/Defaultwechsel**

Bureau-Task: `AUDIO-CONTROL-PLANE-V1-T044`

## Fragestellung

Die in der strukturellen Buckelwal-Songprojektion verwendete Interphrase-Pause von `0.716662 s` verbesserte die meisten Strukturmetriken, verschlechterte im historischen 2017–2019-Holdout aber den Interphrase-Gap-Fehler. T044 prüft deshalb ausschließlich, ob sich dieser eine Zeitparameter auf Development-Daten besser kalibrieren lässt, ohne Stimme, Unit-Synthese, Theme-Zahl, Phrase-Wiederholungen oder Live-Defaults anzufassen.

Die im Bureau-Task gebundenen historischen Gap-RAE-Werte sind:

- pausierte Development-Projektion: `0.22786836858667633`
- damaliger Default/Baseline: `0.08101189807106207`

Diese Werte sind Ausgangsevidenz und **keine nachträglich gesetzte Acceptance-Schwelle**.

## Daten- und Zugriffsvertrag

Der Garland-2013-Legacy-Korpus bleibt revisionsgebunden über `assets/whale-sources/song-corpus-v1/source-manifest.json`.

Der Split ist unverändert:

- Development: 2012–2016, 15 Recordings, 1605 Phrasen
- Holdout: 2017–2019, 11 Recordings

Für T044 wurde `build_split_corpus(...)` eingeführt. Im Unterschied zum bisherigen Vollkorpus-Builder öffnet und hash-verifiziert diese Funktion nur die Annotationen des explizit angeforderten Splits. Der Kandidaten-Freeze fordert ausschließlich `development` an. Das Baseline-Evaluationsartefakt wird während des Freezes ebenfalls nicht gelesen, weil es bereits Holdout-Ergebnisse enthält; seine Validierung ist bis nach dem Freeze verschoben.

Automatisierte Tests überwachen diese Grenze: Ein Development-Freeze schlägt fehl, falls er versucht, einen Holdout-Payload zu öffnen oder den Holdout-Split anzufordern.

## Definition der Interphrase-Lücke

Empirisch wird die Lücke als Abstand zwischen aufeinanderfolgenden veröffentlichten Raven-Phrasenfenstern innerhalb einer kontinuierlichen Recording-Session bestimmt:

1. `current_phrase_begin - previous_phrase_end`
2. negative Überlappung wird auf `0` geklemmt
3. Lücken über `60 s` markieren eine neue Session und gehen nicht in die Interphrase-Verteilung ein

Damit ist die Größe ausdrücklich **keine** Unit- oder Intra-Phrase-Lücke. Die veröffentlichten Raven-Tabellen markieren Theme-Grenzen nicht separat; T044 erfindet daher keine empirische Theme-Gap-Population. Auf Modellseite bleibt die bereits im kanonischen Evaluator deklarierte Engineering-Proxy-Größe erhalten: die mittlere hierarchische Phrase-Boundary-Pause. Empirie und Modellproxy werden nicht als biologisch identische Populationen behauptet.

Development-Referenz:

- mittlere Interphrase-Lücke: `1.037118 s`
- beobachtete Gap-Anzahl: `1589`

## Development-only Kalibrierung

Die vor T044 bereits Development-fit gewählte Makrostruktur bleibt unverändert:

- `theme_count = 6`
- `phrase_repeats_min = 6`
- `phrase_repeats_max = 6`
- `transition_pause_seconds = 1.35`
- `cycle_pause_seconds = 2.6`

Nur `phrase_pause_seconds` wird optimiert. Unter dem bestehenden Jitter-/Ordering-Vertrag liegt der gültige Bereich bei `0.45–1.19 s`.

Der deterministische 8-Seed-Modellmittelwert ist über diesen eindimensionalen Parameter monoton und affin. T044 bestimmt deshalb aus den beiden gültigen Randpunkten die Development-Zielkreuzung, rundet auf Mikrosekunden und prüft zusätzlich die direkten Mikrosekunden-Nachbarn. Auswahlkriterium ist ausschließlich der Development-RAE der Interphrase-Lücke.

Final eingefrorener Kandidat:

- `phrase_pause_seconds = 0.947703`
- modellierter Development-Gap: `1.037118 s`
- Development-Gap-RAE: `0.0` auf sechs Nachkommastellen
- mittlerer Development-RAE über alle aktuellen Strukturmetriken: `0.183364`
- Candidate SHA-256: `f343b326b149f0ea4b6c76bb0a0db0123eb8a7e91ca0c84a500f05a0ca22521e`

Der Kandidat bindet Development-Quellen, Split-Korpus, Code-Dateien, Modellseeds und Parameter kryptographisch. Nach dem Freeze wurde die gebundene Kalibrierungs-/Evaluatorlogik nicht mehr verändert.

## Einmalige Holdout-Auswertung

Der eingefrorene Kandidat wurde anschließend genau einmal mit `2017–2019` ausgewertet. Das kanonische Output-Artefakt ist `assets/whale-sources/song-corpus-v1/pause-calibration-v1/holdout-evaluation.json`. Die CLI verweigert eine zweite Auswertung auf denselben kanonischen Output-Pfad.

Evaluation SHA-256: `dd1f4f916dee9210006eb38a08a393bec7f29cecd55e26ae46bac5f60c8ac691`

### Ergebnis

| Metrik | Default RAE | alte Dev-Projektion | T044 kalibriert |
| --- | ---: | ---: | ---: |
| Phrase-Dauer | 0.615001 | 0.602048 | 0.602048 |
| Interphrase-Gap | 0.081012 | 0.227868 | **0.046549** |
| Phrase-Type-Run | 0.221866 | 0.162659 | 0.162659 |
| Theme-Sequenz | 0.254237 | 0.118644 | 0.118644 |
| Phrasen pro Song | 0.379862 | 0.333805 | 0.333805 |
| publizierte Units pro Song | 0.539298 | 0.017469 | 0.017469 |
| analysierter Span pro Song | 0.733944 | 0.427768 | **0.408012** |
| **Mittel über alle 7** | **0.403603** | **0.270037** | **0.241312** |

Der Interphrase-Gap-Fehler fällt damit gegenüber der alten Development-Projektion um rund 79,6 % und liegt auch unter dem bisherigen Default. Gegen die alte Development-Projektion regressiert keine der übrigen sechs Metriken: fünf bleiben unverändert, der analysierte Span verbessert sich zusätzlich. Gegen den Default ist T044 auf allen sieben aktuellen Strukturmetriken besser.

## Widerspruch „sechs“ vs. „sieben“ Strukturmetriken

Der historische T044-Auftrag spricht von sechs Strukturmetriken. Der aktuelle, revisionsgebundene kanonische Evaluator enthält inzwischen sieben; zusätzlich vorhanden ist `mean_analyzed_span_per_published_song_seconds`.

T044 löst diesen Widerspruch fail-open **nicht** durch Weglassen einer Metrik. Stattdessen wird die aktuelle Siebenermenge vollständig berichtet und die historische Sechserformulierung im Candidate-Manifest als Altvertrag gebunden. Dadurch wird keine schlechte Metrik selektiv versteckt.

## Keine Timbre- oder Live-Wirkung

T044 ändert keine Unit-Synthese, keinen Voice-Mode, keine Roughness-/Harmonikparameter, kein PipeWire-/MIDI-/Geräteverhalten und keinen Produktionsdefault. Die umfangreichen Voice-, Organic-, Harmonik- und Source-Filter-Regressionstests bleiben grün.

Insbesondere bleibt `profiles/buckelwal-live-voice-v1.json` beim bestehenden Default `morph`.

## Entscheidung

**Belegt:** Die Interphrase-Pausenkalibrierung `0.947703 s` verbessert im einmaligen Holdout sowohl den gezielt adressierten Gap-Fehler als auch den mittleren Fehler über alle sieben aktuellen Strukturmetriken, ohne strukturelle Nebenregression gegenüber der alten Development-Projektion.

**Nicht belegt:** biologische Optimalität, akustische Timbre-Realität, menschliche Präferenz oder die Berechtigung, den Produktionsdefault automatisch zu ändern.

Daher endet T044 mit positiver Holdout-Evidenz, aber ohne Defaultmutation. Ein Defaultwechsel ist eine separate revisionsgebundene Entscheidung und muss zusätzlich die relevante Regressionsevidenz berücksichtigen.

## Reproduzierbarkeit

Development-Freeze:

```bash
python3 scripts/calibrate_whale_song_interphrase_pause.py freeze \
  --output assets/whale-sources/song-corpus-v1/pause-calibration-v1/candidate.json
```

Der kanonische Holdout-Output ist absichtlich single-use. Eine erneute `evaluate`-Ausführung auf denselben Output-Pfad wird verweigert. Für die verifizierte T044-Evidenz ist das bereits committed Holdout-Artefakt maßgeblich; es darf nicht durch nachträgliches Tuning ersetzt werden.
