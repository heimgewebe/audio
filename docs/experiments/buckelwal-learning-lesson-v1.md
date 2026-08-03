# Buckelwal-Lernlektion v1: Vom reinen Ton zur Buckelwaleinheit

Stand: 3. August 2026
Status: read-only Produkt- und Lernprototyp

## Entscheidung

Die erste Lernfunktion ist bewusst kein vollständiges Curriculum und keine
Forschungsplattform. Sie beantwortet eine einzelne Frage:

> Welche zeitlichen Merkmale machen aus einem stabilen Ton einen
> walähnlicheren Ruf?

Dafür werden eine echte Aufnahme und vier reproduzierbare Modellstufen
pegelbegrenzt gegenübergestellt. Die Oberfläche startet weder die Liveengine
noch ein Gerät und verändert keinen Audiozustand.

## Drei Wahrheitsebenen

1. **Beobachtung:** eine lizenzierte reale Aufnahme mit ihren
   Aufnahmebedingungen;
2. **Modell:** Morph und drei einzeln benannte Erweiterungsstufen;
3. **Extrapolation:** die 88-Tasten-Spielbarkeit ist musikalisch, nicht
   biologisch.

Diese Ebenen dürfen in Oberfläche, API und Manifest nicht zusammenfallen.
Die Oberfläche zeigt außerdem Attribution und Lizenz der echten Referenz sowie
aller tatsächlich verwendeten Morph-Anker. Damit bleiben auch die
CC-BY-2.5-Modellquellen im ausgelieferten Lernfokus sichtbar.

## Hörstufen

| ID | Inhalt | Aussage |
|---|---|---|
| `reference` | echter Ausschnitt | Beobachtung, keine Engineleistung |
| `morph` | aktuelle Grundengine | sichere periodische Basis |
| `envelope` | nur Quellhüllkurve | zeitliche Lautstärkeentwicklung |
| `periodicity` | zusätzlich Periodizität/Rauigkeit | stärkste isolierte, aber nicht robuste v5.1-Schicht |
| `articulation` | zusätzlich Modellzustände | didaktische Hypothese, keine Biomechanik |

Alle Modellhörproben verwenden dieselbe feste MIDI-Geste. Auch der echte
Ausschnitt wird auf 3,2 Sekunden gekürzt und an den Rändern weich ausgeblendet.
Der Builder normalisiert alle fünf Hörproben auf dasselbe begrenzte aktive
RMS-Ziel und eine Peakgrenze, damit Lautheit nicht allein über das Urteil
entscheidet.

## Merkmalsansicht

Für jede Hörprobe werden 48 Punkte ausgegeben:

- Hüllkurve;
- Periodizität;
- komplementäre Rauigkeit.

F0 und Voicing stammen aus Evaluator v2. Unsichere Frames bleiben unsicher.
Die Kurven begründen weder biologische Äquivalenz noch wahrgenommene
Überlegenheit.

## Lokaler Blindvergleich

Die Oberfläche mischt `morph` und `articulation` als A/B. Das Ergebnis:

- bleibt ausschließlich im Browserzustand;
- wird nicht gespeichert oder übertragen;
- ist Selbstbeobachtung, keine Studie.

## Reproduktion

```bash
python3 scripts/build_whale_learning_lesson.py --check
python3 scripts/whale_learning_lesson.py
python3 scripts/audio_control.py check
```

Kanonische Artefakte:

- `inventory/buckelwal-learning-lesson.v1.json`
- `schemas/buckelwal-learning-lesson.v1.schema.json`
- `ui/whale-learning-*.wav`
- `scripts/whale_learning_lesson.py`
- `scripts/build_whale_learning_lesson.py`

## Nicht belegt

Die Lektion belegt keine biologische Stimmgleichheit, Artenklassifikation,
Rufbedeutung, perzeptive Überlegenheit ohne verblindete Teilnehmer oder
natürliche Echtheit über 88 Klaviertasten.
