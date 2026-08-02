# Buckelwal-Evaluator v2: F0-Suchrand und Voicing

Stand: 2. August 2026  
Bureau-Aufgabe: `AUDIO-CONTROL-PLANE-V1-T030`  
Repositorybasis: `7f5de247683c394cc981f99ea2aa1c92f18b90f8`

## Urteil

**These:** Der bisherige Autokorrelations-Tracker meldet unter Rauschen häufig den kürzesten zulässigen Lag als exakten Grundton. Bei 4 kHz Analyserate entspricht Lag 3 ungefähr 1.333,33 Hz. Insbesondere die Stellwagen-Aufnahme mit Schiffslärm wird dadurch in vielen Frames falsch als hochfrequent und voiced klassifiziert.

**Gegenthese:** Ein strengerer Evaluator könnte echte hochfrequente Rufe oder schwache periodische Passagen pauschal verwerfen und dadurch nur scheinbar robuster wirken.

**Urteil:** Evaluator v2 darf Suchrandtreffer nicht als exakten F0 ausgeben. Er trennt F0, Voicing, Konfidenz, Suchranddiagnostik, Oktavkandidaten und Bandbreitenverfügbarkeit. Der externe Sensitivitätslauf bestätigt die Suchrandhärtung mit null voiced boundary hits. Gleichzeitig ist der Evaluator konservativ: Die Alaska-Aufnahme wird vollständig als Suchrand-unklar verworfen. Deshalb belegt T030 eine robustere Fehlerbehandlung, nicht biologische Grundtonwahrheit oder bessere klangliche Walähnlichkeit.

## Abgrenzung zu T029

T029 und Audio-PR #43 bleiben unverändert und reproduzierbar. Die dort verwendete Legacy-Auswertung wird weder ersetzt noch rückwirkend neu bewertet. Evaluator v2 ist eine getrennte Messgeneration für spätere Studien.

Nicht verändert wurden:

- Walengine, Morph- oder Organic-Parameter;
- T029-Definition, Kandidat, interne und externe Berichte;
- gesperrte NOAA-/PMEL-Audiodateien und Segmentgrenzen;
- Liveprofil, PipeWire-, MIDI- oder Gerätepfade;
- Standardengine und Produktfreigabe.

## Vorregistrierung

Die erste Definition wurde vor jeder externen v2-Auswertung eingecheckt:

- Commit: `a8d15d16e17ee8196e76aca39d17d530001ec435`
- SHA-256: `5d5d265cf11b18db708252767c60be927f7cf34d9fb42283355d106c72c26670`

Der kontrollierte Entwicklungskorpus zeigte danach zwei komplementäre Aliasfehler:

1. Blindes Bevorzugen längerer Perioden halbierte reine Töne.
2. Ausschließlich der global stärkste Peak ordnete den 520-Hz-Referenzton einer Dreifachperiode zu.

Vor jeglichem externen v2-Lauf wurde deshalb eine dokumentierte Methodikpräzisierung eingecheckt:

- Commit: `bfe237b4fa21a89a712ad49b4bde709ab46d6106`
- aktuelle Definitions-SHA-256: `c4dc3c5d579847d78799a95202616b6ce5a4cbf05941b681b7e4fc8378484045`

Die Präzisierung bestimmt zuerst die stärkste lokale Peakfamilie und wählt innerhalb ausreichend starker, annähernd ganzzahlig verwandter Perioden den kürzesten Lag. Andere Familien bleiben getrennte Kandidaten. Sämtliche numerischen Schwellen, kontrollierten Erwartungen, externen Sperrsätze und Erfolgsgates blieben unverändert.

## Fester Analysevertrag

| Merkmal | Wert |
|---|---:|
| Eingangsrate | 48.000 Hz |
| Analyserate | 4.000 Hz |
| Fenster | 180 ms |
| Kontrollpunkte | 48 |
| F0-Suchraum | 28–800 Hz |
| Mindestperiodizität | 0,38 |
| Mindestprominenz | 0,04 |
| Suchrandwache | 1 Lag |
| Oktavfamilienverhältnis | 0,88 |

Unvoiced wird ausgegeben bei:

- Stille beziehungsweise zu geringer Energie;
- Periodizität unter der fixierten Grenze;
- zu geringer lokaler Peakprominenz;
- ausgewähltem Lag am hohen oder tiefen Suchrand.

Suchrandtreffer erhalten eine explizite Diagnose, aber keinen F0. Oktav- und Mehrfachperiodenkandidaten werden pro Frame sichtbar berichtet.

## Kontrollierter Referenzkorpus

Der kontrollierte Report ist byte-reproduzierbar:

- `assets/whale-sources/studies/evaluator-v2/reference-corpus.json`
- SHA-256: `e2a3e01c9ec04d10588f43b7f8b9b976f04183525be920b889952f54af1c9a30`

| Fall | Erwartung | Ergebnis | Voiced-Anteil |
|---|---:|---:|---:|
| Sinus | 80 Hz | 80,00 Hz | 1,00 |
| Sinus | 220 Hz | 222,22 Hz | 1,00 |
| Sinus | 520 Hz | 500,00 Hz | 1,00 |
| Fehlender Grundton | 110 Hz | 111,11 Hz | 1,00 |
| Gepulster Ton | 150 Hz | 148,15 Hz | 1,00 |
| 180 Hz mit synthetischem Schiffslärm | 180 Hz | 181,82 Hz | 1,00 |
| Deterministisches weißes Rauschen | unvoiced | unvoiced | 0,00 |
| Suchrandton | 800 Hz | Suchrand, unvoiced | 0,00 |
| Vorher eingefrorene reale Walannotation | 105,4945 Hz | 105,2632 Hz | 1,00 |

Die reale Walannotation stammt aus dem bereits vor T030 erzeugten Morph-Manifest und ist quellengebunden. Sie ist eine unabhängige historische Referenz, keine biologische Ground Truth.

## Gesperrte externe Sensitivität

Verwendet wurden unverändert:

- Alaska: `assets/whale-sources/evaluation/manifest.json`
- Stellwagen und Amerikanisch-Samoa: `assets/whale-sources/evaluation-v2/manifest.json`

Der externe Report ist byte-reproduzierbar:

- `assets/whale-sources/studies/evaluator-v2/sensitivity-report.json`
- SHA-256: `8fc32f5cd60fd435e5b16d68771d1c2d86812fefba4c9baa9a64931f5e274f71`

### Suchrandsättigung

Die vorregistrierte Legacy-Reproduktion zählt Lag-3-Frames, die zugleich die alte Voiced-Grenze erfüllen:

| Stellwagen-Segment | Erwartet | Beobachtet |
|---|---:|---:|
| 1 | 36/48 | 36/48 |
| 2 | 40/48 | 40/48 |
| 3 | 34/48 | 34/48 |

Evaluator v2 erzeugt über alle neun externen Segmente **null voiced Suchrandtreffer**.

### Aufnahmeebene

| Aufnahme | Legacy mittlerer Voiced-Anteil | v2 mittlerer Voiced-Anteil | v2 voiced boundary hits |
|---|---:|---:|---:|
| Alaska 1999 | 1,0000 | 0,0000 | 0 |
| Stellwagen | 0,9375 | 0,1875 | 0 |
| Amerikanisch-Samoa | 0,9219 | 0,6615 | 0 |

Die starke Verringerung bei Stellwagen ist mit der bekannten Schiffslärmsättigung vereinbar. Der vollständige Alaska-Verlust ist dagegen ein Warnsignal: Die Aufnahme enthält unter dem fixierten Vertrag keine belastbar vom hohen Suchrand getrennten Frames. Das Ergebnis darf weder als Abwesenheit eines Walrufs noch als gesicherte Unvoiced-Wahrheit interpretiert werden.

### Bandbreitenschicht

Die Amerikanisch-Samoa-Quelle besitzt 5 kHz Abtastrate und 2,5 kHz Nyquist. F0/Voicing bleibt laut vorab fixiertem Vertrag verfügbar. Das Hochbandmerkmal wird dagegen explizit als `unavailable` ausgewiesen. Es findet weder Imputation noch stilles Umgewichten statt.

## Korrekturen nach dem ersten externen Lauf

Zwei Implementierungsfehler wurden nach dem ersten externen Bericht korrigiert. Beide Änderungen sind im finalen JSON-Report selbst aufgeführt. Es wurden keine Schwellen, Suchgrenzen, Audiodaten oder Erfolgsgates geändert.

1. Der erste Bericht verglich 42 rohe Lag-3-Auswahlen im zweiten Stellwagen-Segment mit dem vorregistrierten Wert 40. Der Wert 40 bezeichnet jedoch Lag-3-Frames, die zugleich die alte Voiced-Schwelle erfüllen. Die Berichtslogik verwendet nun `lag_3_voiced_hits`; rohe Treffer bleiben getrennt sichtbar. Audio- und Frameergebnisse änderten sich dadurch nicht.
2. Das Self-Review fand, dass die Implementierung neben den eingefrorenen Multiplikatoren 2 und 3 versehentlich auch 4 akzeptierte. Die Implementierung liest die zulässigen Multiplikatoren jetzt direkt aus der Definition. Der kontrollierte Vertrag, `36/40/34` und null voiced boundary hits bleiben gleich. Der mittlere Stellwagen-v2-Voiced-Anteil änderte sich nachvollziehbar von 0,18229167 auf 0,1875.

Der vor dieser zweiten Korrektur erzeugte kontrollierte Report hatte SHA-256 `5d88147f2c5a203bc22dd3b9ec14496b28439dbb85be661ee0a319c2fbe8f88c`, der Sensitivitätsreport `8913f7012c80028002cdfad292387080432fc1c4cdf3c8c409837ed64af83b46`. Diese Vorgängerhashes sind im finalen Sensitivitätsreport gebunden.

## Reproduzierbarkeit

Kanonische Implementierungsartefakte:

| Artefakt | SHA-256 |
|---|---|
| `scripts/evaluate_whale_f0_v2.py` | `a2270945d5d4a84d614111cd14d67571844e5a14fb0167ae01e93af7578c3025` |
| `scripts/study_whale_evaluator_v2.py` | `17df1b93928eb907b3044421b97c30d23fa071ee865bb654379cb1a4fcc142b1` |
| `tests/test_whale_f0_evaluator_v2.py` | `4fd674378f07fe7162c04e1fcd4aca22f5313e99897d41069c770e76d086822d` |

Die fokussierte Testmatrix erzeugt Kontroll- und Sensitivitätsbericht neu und verlangt Bytegleichheit mit den eingecheckten Dateien. Sie prüft zusätzlich Suchrand-Unvoicing, Oktavdiagnostik, Legacy-Reproduktion, Aufnahmeaggregation und Samoa-Bandbreitenverfügbarkeit.

## Nicht belegt

T030 belegt nicht:

- biologisch korrekten F0 jedes Buckelwalrufs;
- bessere subjektive Walähnlichkeit;
- eine neue Engine- oder Profilfreigabe;
- physische Qualität am Roland-, MOTU- oder Focal-Pfad;
- dass konservatives Unvoicing stets besser als ein unsicherer F0 ist;
- dass die in T029 beobachteten Distanzverschiebungen mit v2 identisch bleiben.

## Folgerung

Für zukünftige Walstudien muss Evaluator v2 Unsicherheit sichtbar machen. Eine spätere Engine- oder Organic-Studie darf externe Ergebnisse erst nach einer neuen, eigenen Vorregistrierung auswerten. Alaska benötigt gegebenenfalls eine getrennte, vorab definierte niedrigerbandige oder zeitlich längere Analyse – nicht eine nachträgliche Lockerung dieses Vertrags.
