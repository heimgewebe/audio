# Buckelwal-Interphrase-Pause: Default-Entscheid v1

Stand: 2026-08-21

## Entscheidung

**RETAIN** – die vollständige eingefrorene T044-Konfiguration bleibt revisionsgebundene Studienevidenz. `phrase_pause_seconds=0.947703` wird insbesondere **nicht isoliert** als neuer Pause-Default freigegeben. Der aktuelle Grammar-Default bleibt `phrase_pause_seconds=0.82`; T045 autorisiert keinen Defaultwechsel.

Der T044-Kandidat verbessert die eingefrorene strukturelle Holdout-Evidenz deutlich. Diese Metriken wurden jedoch mit der vollständigen T044-Konfiguration erzeugt, darunter `theme_count=6` und Phrase-Repeats `6–6`, nicht mit dem aktuellen Default `theme_count=4` und `3–5`. Sie können deshalb keinen isolierten Austausch von `0.82` gegen `0.947703` begründen. Zusätzlich bindet das revisionsgebundene T043-Completion-Receipt keine realen, vor dem Entblinden eingefrorenen menschlichen Antworten; sein perceptuelles Ergebnis bleibt `indeterminate`.

Maschinenlesbare Entscheidungswahrheit:

`assets/whale-sources/song-corpus-v1/pause-calibration-v1/default-decision-v1.json`

## Gebundene T044-Evidenz

Der Entscheid verwendet ausschließlich bereits eingefrorene T044-Evidenz; der 2017–2019-Holdout wird nicht erneut für Tuning, Schwellenwahl oder Kandidatenselektion verwendet.

- Kandidat: `f343b326b149f0ea4b6c76bb0a0db0123eb8a7e91ca0c84a500f05a0ca22521e`
- Kandidat-Datei-SHA-256: `23ed10b77c5488d901f57a6845392c7b7515ecdc5c6f38ff05669b3b521666dd`
- T044 Final-Head: `fa261af856745da0ace132dbb1114ce17e3e38ee`
- T044 Merge: `ddbefe74bb6935acaf02982467964592f2cb0ab6`
- Holdout-Evaluation intern: `dd1f4f916dee9210006eb38a08a393bec7f29cecd55e26ae46bac5f60c8ac691`
- Holdout-Evaluation Datei: `27525a9b08e1db0ad3e594fe3a1a56a5bb9edef2b1cff22e0f9a8c902a9ea5d6`
- Holdout-Evaluationsordinal: `1`
- Baseline-Datei-SHA-256: `d617953522890c4668885bc38e0bcd6ece17eb2295b6a9bdaa63b56655f8e6f4`

### Konfigurationsscope der T044-Metriken

Die positive T044-Holdout-Evidenz gehört zur **vollständigen** eingefrorenen `final_config`:

| Parameter | aktueller Grammar-Default | eingefrorener T044-Kandidat |
| --- | ---: | ---: |
| `seed` | 45223 | 45223 |
| `base_note` | 45 | 45 |
| `cycles` | 2 | 2 |
| `theme_count` | **4** | **6** |
| `phrase_repeats_min` | **3** | **6** |
| `phrase_repeats_max` | **5** | **6** |
| `phrase_pause_seconds` | **0.82** | **0.947703** |
| `transition_pause_seconds` | 1.35 | 1.35 |
| `cycle_pause_seconds` | 2.6 | 2.6 |

Damit sind mindestens vier Strukturparameter zwischen aktuellem Default und T044-Kandidat verschieden. Die T044-Metriken isolieren den kausalen Beitrag von `phrase_pause_seconds` nicht. Sie dürfen nur als Evidenz für die vollständige eingefrorene Konfiguration gelesen werden und stützen **keine** isolierte Pause-Defaultänderung.

### Strukturmetriken

Niedriger ist jeweils besser.

| Metrik | Default | historische Projektion | kalibriert |
| --- | ---: | ---: | ---: |
| Gesamtmittel RAE | 0.403603 | 0.270037 | **0.241312** |
| Interphrase-Gap RAE | 0.081012 | 0.227868 | **0.046549** |
| analysierter Song-Span RAE | 0.733944 | 0.427768 | **0.408012** |
| Phrase-Dauer RAE | 0.615001 | 0.602048 | **0.602048** |
| Phrase-Typ-Run-Length RAE | 0.221866 | 0.162659 | **0.162659** |
| Phrasen pro Song RAE | 0.379862 | 0.333805 | **0.333805** |
| Units pro Song RAE | 0.539298 | 0.017469 | **0.017469** |
| Theme-Sequenzlänge RAE | 0.254237 | 0.118644 | **0.118644** |

Damit ist die strukturelle Evidenz für die vollständige T044-Konfiguration positiv. Sie ist weder eine isolierte Pause-Ablation noch menschliche Präferenz, Walähnlichkeit oder ein Nachweis, dass ein Hörer die Hierarchiewirkung bevorzugt oder zuverlässig erkennt.

## Gebundene T043-Evidenz

- T043 Final-Head: `272c7a6c1625c2804f914a6e1c6f39647ce83aad`
- T043 Merge: `dab041e5cc5d8b656fe41d56c325d44af9b1b548`
- T043 Completion-Receipt: `30fb7ef05ccee41d26cd8037b53677afbbed6162d57764ed5c7f18305f944251`

T043 liefert einen kontrollierten, anonymisierten und gegenbalancierten Hörtestvertrag. Der Builder erzeugt jedoch ausdrücklich eine leere `response-template.json`. Der verifizierte T043-Closeout hält für **dieses Receipt** fest:

- gebundene reale, vor Entblindung eingefrorene menschliche Antworten: `0`;
- perceptuelles Ergebnis: `indeterminate`;
- automatisierte Tests dürfen keine Präferenz, Walähnlichkeit oder Erkennbarkeit erzeugen.

Das ist kein negatives Hörurteil und keine Aussage, dass außerhalb des T043-Receipts niemals Hörantworten existiert haben. Es ist **fehlende revisionsgebundene perceptuelle Evidenz für diesen Entscheid**.

## Warum RETAIN statt PROMOTE

Für eine Default-Promotion müssten die verfügbaren Evidenzklassen zusammenpassen. Aktuell gilt:

1. **Strukturelle Holdout-Evidenz:** positiv, aber nur für die vollständige eingefrorene T044-Konfiguration.
2. **Isolierte Pause-Evidenz:** nicht vorhanden; T044 unterscheidet sich zugleich bei `theme_count` und den Repeat-Limits vom aktuellen Default.
3. **Perceptuelle Evidenz:** im gebundenen T043-Evidenzstand unbestimmt.
4. **Regressionslage:** T044 ist technisch grün und verändert keine Voice-/Timbre-/Livepfade.
5. **Methodische Grenze:** Der bereits einmal verwendete T044-Holdout darf nicht für eine neue Tuning- oder Auswahlrunde benutzt werden.

Die strukturelle Verbesserung darf deshalb weder als isolierte Pause-Wirkung noch als Ersatz für einen Hörnachweis verwendet werden. Nach dem T045-Rollbackvertrag ist `RETAIN` der sichere terminale Ausgang.

## Default- und Wirkungssperre

T045 ändert weder Live-Modi noch Defaultparameter. Beim Entscheidungszeitpunkt gilt die vollständige `SongGrammarConfig()` mit `theme_count=4`, Phrase-Repeats `3–5` und `phrase_pause_seconds=0.82`; `scripts/whale_song_grammar.py` ist an Datei-SHA-256 `49a3a23fa3c3a34ed2f3baff313c4d023e03a6fc8ae58646221343b456dbec71` gebunden. `profiles/buckelwal-live-voice-v1.json` bleibt bei `default_voice_mode = "morph"`.

Weder der vollständige T044-Kandidat noch `0.947703` allein ersetzen diese Defaults.

Ein zukünftiger Promote-Entscheid wäre ein **neuer** revisionsgebundener Entscheid und dessen technische Umsetzung wiederum ein **getrennter** Implementierungsauftrag. T045 selbst erteilt keine solche Autorität.

## Bedingungen für eine spätere Neubewertung

Eine neue Entscheidung benötigt mindestens:

- echte menschliche Antworten, die vor dem Entblinden unter einem revisionsgebundenen kontrollierten Protokoll eingefroren wurden;
- eine explizite Bindung des perceptuellen Stimulus an die **vollständige** tatsächlich bewertete Kandidatenkonfiguration;
- für eine isolierte Änderung von `phrase_pause_seconds` eine neue vorregistrierte Evaluation, die die übrigen Grammar-Parameter auf den beabsichtigten Zieldefaults festhält;
- keine erneute Nutzung des 2017–2019-Holdouts für Parameter-, Schwellen- oder Kandidatentuning.

Ohne diese neue Evidenz bleibt die T044-Konfiguration Studienkandidat und `phrase_pause_seconds=0.82` unveränderter Default.
