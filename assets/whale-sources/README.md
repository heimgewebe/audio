# Buckelwal-Quellen und abgeleitete Klangmodelle

Die Rohdateien bleiben unverändert erhalten. Zwei deterministische Ableitungen
nutzen denselben lizenz- und hashgebundenen Quellenbestand:

- `scripts/build_whale_sample_bank.py` erzeugt unter `processed/` 19 kurze
  Aufnahmephrasen für den Vergleichsmodus `realistic`;
- `scripts/build_whale_morph_bank.py` erzeugt unter `morph/` periodengemittelte,
  bandbegrenzte Einzelzyklus-Tabellen für den Standardmodus `morph`.

Der Morphmodus spielt keine fertigen Aufnahmephrasen ab. Er übernimmt nur die
wiederkehrende periodische Klangstruktur ausgewählter Buckelwalstimmen. Dadurch
werden nichtperiodisches Meeresrauschen und die ursprüngliche Melodie nicht als
fortlaufende Ebene in das Instrument übernommen.

## Quellen und Rechte

- `humpback-song-cc0.ogg`: Spyrogumas, *Song of Humpback Whales*, CC0 1.0.
- `humpback-moo-nps.ogg`: U.S. National Park Service, Glacier Bay,
  Public Domain in den Vereinigten Staaten.
- `humpback-wheezeblow-nps.ogg`: U.S. National Park Service, Glacier Bay,
  Public Domain in den Vereinigten Staaten.
- `song-antarctic-area-v-2010-ccby25.oga`,
  `song-new-caledonia-2010-ccby25.oga` und
  `song-eastern-australia-2010-ccby25.oga`: Garland et al. (2013),
  *Humpback Whale Song on the Southern Ocean Feeding Grounds: Implications
  for Cultural Transmission*, PLOS ONE, CC BY 2.5.
- `song-foraging-mn132a-ccby25.oga` und
  `song-foraging-mn133a-ccby25.oga`: Stimpert et al. (2012),
  *Humpback Whale Song and Foraging Behavior on an Antarctic Feeding Ground*,
  PLOS ONE, CC BY 2.5.

Die kanonischen Beschreibungsseiten, vollständigen Urheber, Lizenz-URIs,
Bearbeitungshinweise, erwarteten Rohdateigrößen und SHA-256-Werte stehen in
`SOURCES.json`. `NOTICE.md` stellt dieselben Angaben menschenlesbar neben die
Audioassets. `processed/manifest.json` bindet die Aufnahmephrasen;
`morph/manifest.json` bindet die daraus extrahierten periodischen Tabellen.
Es wird keine Unterstützung oder Empfehlung durch Urheber, NPS, PLOS oder
Wikimedia behauptet.

## Samplebank-Vertrag

- 19 extrahierte Originalphrasen;
- 27 Tastaturzonen über A0 bis C8;
- höchstens vier Halbtöne gesamte Tonhöhenverschiebung je Taste;
- geloopte Originalphrasen und 90-ms-Crossfades;
- ausschließlich Vergleichsmodus `realistic`.

## Morphbank-Vertrag

- sieben interne Quellanker;
- vollständige chromatische Klaviatur MIDI 21 bis 108;
- A4 = 440 Hz in gleichstufiger Zwölftonteilung;
- keine Samplezonen, Presets oder reservierten Tasten;
- keine lange Aufnahmephrase und keine permanente Rauschschicht;
- phasengleich gemittelte Stimmzyklen;
- mehrere harmonisch bandbegrenzte Tabellenstufen mit SHA-256;
- kontinuierliche Überblendung der Klangfarben und Bandbegrenzungsstufen.
