# Buckelwal-Interphrase-Pause: Default-Entscheid v1

Stand: 2026-08-21

## Entscheidung

**RETAIN** – `phrase_pause_seconds=0.947703` bleibt ein revisionsgebundener Studienkandidat. Der gebundene aktuelle Grammar-Default bleibt `phrase_pause_seconds=0.82`; T045 autorisiert keinen Defaultwechsel.

Der T044-Kandidat verbessert die eingefrorene strukturelle Holdout-Evidenz deutlich. Das reicht jedoch nicht für eine Default-Promotion, weil das revisionsgebundene T043-Completion-Receipt keine realen, vor dem Entblinden eingefrorenen menschlichen Antworten bindet. Sein verifizierter Vertrag setzt das perceptuelle Ergebnis in diesem Evidenzstand zwingend auf `indeterminate`.

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

Damit ist die strukturelle Evidenz positiv. Sie ist aber weder menschliche Präferenz noch Walähnlichkeit noch ein Nachweis, dass ein Hörer die Hierarchiewirkung bevorzugt oder zuverlässig erkennt.

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

1. **Strukturelle Holdout-Evidenz:** positiv.
2. **Perceptuelle Evidenz:** im gebundenen T043-Evidenzstand unbestimmt.
3. **Regressionslage:** T044 ist technisch grün und verändert keine Voice-/Timbre-/Livepfade.
4. **Methodische Grenze:** Der bereits einmal verwendete T044-Holdout darf nicht für eine neue Tuning- oder Auswahlrunde benutzt werden.

Die strukturelle Verbesserung darf deshalb nicht als Ersatz für einen Hörnachweis verwendet werden. Nach dem T045-Rollbackvertrag ist bei unzureichender perceptueller Evidenz `RETAIN` der sichere terminale Ausgang.

## Default- und Wirkungssperre

T045 ändert weder Live-Modi noch Defaultparameter. Beim Entscheidungszeitpunkt gilt:

- `scripts/whale_song_grammar.py`: `SongGrammarConfig().phrase_pause_seconds = 0.82`, Datei-SHA-256 `49a3a23fa3c3a34ed2f3baff313c4d023e03a6fc8ae58646221343b456dbec71`;
- `profiles/buckelwal-live-voice-v1.json`: `default_voice_mode = "morph"`.

Der Studienkandidat `0.947703` ersetzt den Default `0.82` ausdrücklich **nicht**.

Ein zukünftiger Promote-Entscheid wäre ein **neuer** revisionsgebundener Entscheid und dessen technische Umsetzung wiederum ein **getrennter** Implementierungsauftrag. T045 selbst erteilt keine solche Autorität.

## Bedingungen für eine spätere Neubewertung

Eine neue Entscheidung benötigt mindestens:

- echte menschliche Antworten, die vor dem Entblinden unter einem revisionsgebundenen kontrollierten Protokoll eingefroren wurden;
- eine explizite Bindung des perceptuellen Stimulus an den tatsächlich neu bewerteten Kandidaten;
- keine erneute Nutzung des 2017–2019-Holdouts für Parameter-, Schwellen- oder Kandidatentuning.

Ohne diese neue Evidenz bleibt `0.947703` Studienkandidat und kein Default.
