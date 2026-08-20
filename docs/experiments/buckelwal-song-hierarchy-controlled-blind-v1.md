# Kontrollierter Blindtest für Buckelwal-Songhierarchie

Stand: 2026-08-20

## Zweck

Dieser Test ergänzt den explorativen Blindvergleich aus `buckelwal-song-corpus-evaluation-v1.md` um einen engeren kausalen Stimulusvertrag.

Der ältere Vergleich mischt bei der Strukturablation Phraseblöcke. Dadurch kann ein gleich langer Audio-Prefix unterschiedliche konkrete Units enthalten. Das ist für eine explorative Hörprobe zulässig, reicht aber nicht aus, um den hörbaren Beitrag der Makrohierarchie von zufälliger Unit-Auswahl und Reihenfolge zu trennen.

Der kontrollierte Test in `scripts/build_whale_song_hierarchy_blind_test.py` ändert deshalb nur **eine** Achse:

- strukturierte Bedingung: die source-derived Zwischenpausen des ausgewählten Phrasefensters bleiben erhalten;
- strukturabgetragene Bedingung: dieselben Zwischenpausen werden auf einen einheitlichen Wert nivelliert.

Dabei bleiben erhalten:

- exakt dieselben Unit-IDs und dasselbe konkrete Unit-Inventar;
- exakt dieselbe Unit-Reihenfolge;
- Unit-Typ, Herkunftsthema, Dauer, interne Lücke, Note, Velocity, Bend, Pulse und Flourish;
- dasselbe gesamte Zwischenpausenbudget des Fensters;
- dieselbe Renderdauer;
- dieselbe `WhaleMorphVoice`, derselbe Renderer, dieselbe Sample-Rate und derselbe Ausgangsgain.

Damit isoliert der Test den hörbaren Beitrag der **Verteilung der Zwischenpausen über Phrase- und Transition-Grenzen**. Er entfernt absichtlich nicht alle anderen Sequenz- oder Inhaltsmerkmale der Hierarchie. Insbesondere bleiben Transition-Units und die konkrete Unit-Reihenfolge in beiden Bedingungen identisch.

## Deterministisches Stimulusfenster

Aus der auf dem Development-Split angepassten Grammatik wird deterministisch ein zusammenhängendes Fenster gewählt, das mindestens enthält:

1. eine normale Phrase vor einer Transition,
2. die Transition selbst,
3. eine normale Phrase danach.

Unter mehreren zulässigen Fenstern wird für die erste passende Transition das größte vollständige Phrasefenster innerhalb der vorgegebenen Höchstdauer verwendet. Es werden keine Phraseblöcke nachträglich nach Hörwirkung ausgewählt.

Die letzte äußere Pause des Fensters zählt nicht zum Stimulus. Nur Pausen **zwischen** den vollständig gebundenen Phraseblöcken werden verglichen.

## Pegel- und Dauerregel

Beide Bedingungen werden zunächst mit identischer Stimme und identischem Gain offline gerendert. Danach gilt:

1. beide Source-Renders müssen hörbar und clippingfrei sein;
2. Ziel-RMS ist der niedrigere der beiden Source-RMS-Werte;
3. nur die lautere Bedingung darf abgeschwächt werden;
4. Verstärkung ist verboten;
5. beide Dateien behalten dieselbe Renderdauer;
6. Source-Peak, Source-RMS, Matching-Faktor und Ergebniswerte stehen im öffentlichen Manifest.

Pegelangleichung darf damit weder Clipping erzeugen noch ein Stimulusmaterial über seinen sicher gerenderten Ausgangspegel anheben.

## Anonymisierung und Gegenbalancierung

Der Builder erzeugt standardmäßig vier Trial-Varianten. Jede Variante besitzt nur neutrale Dateien wie:

- `trial-01-A.wav`
- `trial-01-B.wav`

Die semantische Bedingungszuordnung steht ausschließlich in `answer-key.json`. Das öffentliche `blind-manifest.json` enthält keine A/B→Bedingungszuordnung.

Die vier Trials bilden die Kombinationen aus:

- strukturierte Bedingung als A oder B;
- Präsentationsreihenfolge A→B oder B→A.

Dadurch erscheinen beide Bedingungen gleich häufig unter A/B und gleich häufig zuerst. Die konkrete Zuordnung wird aus einem kryptographisch zufälligen 256-Bit-Seed bestimmt, der ausschließlich in `answer-key.json` steht. Das öffentliche Manifest enthält nur dessen SHA-256-Commitment; aus `pair_identity_sha256` oder dem öffentlichen Manifest lässt sich die A/B-Zuordnung daher nicht rekonstruieren. Nach dem Entblinden kann die Zuordnung mit dem im Answer-Key gespeicherten Seed reproduziert werden. Ein Trial ist für **eine** menschliche Hörsitzung gedacht. Die vier Varianten sind keine vier unabhängigen Wiederholungen desselben Hörers.

## Menschliche Antworten

`response-template.json` startet mit einer leeren Antwortliste. Der Builder erzeugt keine Wahrnehmungsdaten.

Für eine reale Hörsitzung wird vor dem Entblinden mindestens erfasst:

- pseudonyme `listener_id`;
- `trial_id`;
- `hierarchy_guess`: `A`, `B` oder `unsure`;
- `preference`: `A`, `B` oder `no_preference`.

Die Antwortdatei muss eingefroren werden, solange `answer-key.json` für den Hörer verborgen bleibt. Erst danach darf die Antwort gegen die Bedingungszuordnung aufgelöst werden.

Die Auswertungsregel ist rein deskriptiv:

- Hierarchieerkennung: `correct`, `incorrect` oder `unsure` gegenüber dem versteckten Label `structured_timing`; `unsure` wird nie zu `correct` umgedeutet.
- Präferenz: nach Entblindung `structured`, `flat` oder `no_preference`; `no_preference` bleibt als explizite Enthaltung sichtbar.
- Ohne echte eingefrorene Antworten ist das perceptuelle Ergebnis zwingend `indeterminate`.

Automatisierte Tests können ausschließlich Stimulus-, Identitäts-, Pegel-, Anonymisierungs- und Gegenbalancierungsverträge belegen. Sie können keine menschliche Präferenz, Walähnlichkeit oder Erkennbarkeit erzeugen.

## Offline ausführen

```text
python3 scripts/build_whale_song_hierarchy_blind_test.py \
  --output-dir /tmp/buckelwal-hierarchy-blind \
  --seconds 30 \
  --gain 0.16 \
  --trials 4
```

Der Builder erzeugt ausschließlich Dateien im angegebenen Output-Verzeichnis. Er startet keinen Wal-Dienst und verändert weder PipeWire noch MIDI, Geräte, Profile, Live-Modi oder Defaultparameter.

## Grenzen

Der Test belegt nicht:

- menschliche Präferenz ohne reale Antworten;
- menschliche Hierarchieerkennung ohne reale Antworten;
- populationsweite Walähnlichkeit;
- biologische Korrektheit;
- einen kausalen Effekt anderer Hierarchiemerkmale als der Zwischenpausenverteilung;
- die Berechtigung zu einem Live-, Service-, Geräte-, Profil- oder Defaultwechsel.

Die erhaltene Unit-Reihenfolge ist hier bewusst Stärke und Grenze zugleich: Sie beseitigt den Reihenfolgen-Konfundierer, lässt aber Sequenzinhalt als möglichen weiteren Hierarchiehinweis bestehen.
