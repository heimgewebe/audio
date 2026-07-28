# Buckelwal-Samplequellen

Diese Dateien bilden den realistischen Standardklang von Buckelwal Live Voice.
Die Rohdateien bleiben unverändert erhalten; `scripts/build_whale_sample_bank.py`
erzeugt daraus deterministisch mono PCM16 bei 48 kHz, kurze normalisierte
Phrasen und ein hashgebundenes Manifest.

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
Audioassets; `processed/manifest.json` bindet den tatsächlich gebauten Stand.
Es wird keine Unterstützung oder Empfehlung durch Urheber, NPS, PLOS oder
Wikimedia behauptet.

Der Builder prüft sämtliche Rohdateien vor FFmpeg gegen den Katalog, akzeptiert
keine absoluten Pfade, Traversal oder Symlinks und baut in einem privaten
Staging-Verzeichnis. Erst eine vollständig validierte Bank ersetzt das bisherige
`processed/`-Verzeichnis atomar; ein Fehler lässt die alte Bank unverändert.

## Klangvertrag

- 19 extrahierte Originalphrasen;
- 27 Tastaturzonen über A0 bis C8;
- höchstens vier Halbtöne gesamte Tonhöhenverschiebung je Taste, Pitch Bend
  eingeschlossen;
- tiefe Tasten bevorzugen NPS-Moo-/Körperlaute;
- mittlere Tasten bevorzugen Gesangsphrasen;
- hohe Tasten bevorzugen Wheeze-/Atemlaute;
- gehaltene Töne verwenden geloopte Originalphrasen mit Crossfade;
- Legato wechselt Phrasen mit 90 ms Equal-Power-Crossfade.
