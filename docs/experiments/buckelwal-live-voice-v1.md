# Buckelwal Live Voice

## Produktgrenze

`Buckelwal Live Voice` macht das Roland FP-30X zu einem monophonen Instrument
für eine einzelne, durchgehend spielbare Walstimme. Standard ist `morph`.

Jede Taste von A0 bis C8 spielt ihren normalen chromatischen Ton in
gleichstufiger Stimmung mit A4 = 440 Hz. Keine Taste wählt ein Sample, Preset
oder Steuerkommando. Die zuletzt angeschlagene gehaltene Taste führt dieselbe
phasenkontinuierliche Stimme.

Der Modus ist kein biologisches Modell des Stimmapparats eines Buckelwals.
Er resynthetisiert periodische Klangstrukturen realer Buckelwalaufnahmen und
überträgt sie auf ein musikalisch vollständig spielbares Instrument.

## Vier Spiel- und Vergleichsmodi

| Modus | Rolle | Grenze |
|---|---|---|
| `morph` | Standard und kontrollierte Ausgangsstimme | quellengestützte Resynthese, keine fertige Aufnahmephrase |
| `organic` | organischer Spielmodus | Morphbasis mit kurzem Legato, begrenzter Zusatztonhöhenbewegung, gestengebundener Artikulation und starkem Tiefbass in A0–A1 |
| `realistic` | Vergleich mit echten Aufnahmephrasen | 19 Clips und 27 Tastaturzonen, daher kein frei geformter Walgesang |
| `ufo` | historischer Synthesevergleich | vollständig spielbar, aber nicht aus Walstimmen abgeleitet |

Die Vergleichsmodi bleiben erhalten, damit Klangfortschritt nicht nur behauptet,
sondern mit identischen MIDI-Gesten beurteilt werden kann.

## Quellmodell

`scripts/build_whale_morph_bank.py` erzeugt aus sieben fest gebundenen
Quellclips das Modell
`assets/whale-sources/morph/manifest.json`.

Der Builder:

1. prüft das bestehende Samplemanifest und jeden Quellhash;
2. sucht periodische, phasenstabile Fenster;
3. richtet mehrere Stimmzyklen aus und mittelt sie;
4. entfernt Gleichanteile und normalisiert konservativ;
5. erzeugt mehrere harmonisch bandbegrenzte Tabellen;
6. bettet jede PCM16-Tabelle mit eigenem SHA-256 in das Manifest ein.

Die Periodenmittelung übernimmt die wiederkehrende Walstimmenstruktur, nicht
aber eine lange Melodie oder eine fortlaufende Unterwasser-Rauschschicht.

Die sieben internen Anker sind **keine Presets und keine Tastaturzonen**. Die
Laufzeit überblendet für jeden Zwischenwert kontinuierlich zwischen zwei
benachbarten Klangfarben. Auch die bandbegrenzten Tabellenstufen werden weich
überblendet, um harte Register- und Aliasgrenzen zu vermeiden.

## Spielmodell

- **Taste:** exakte 12-TET-Zieltonhöhe.
- **Anschlagstärke:** Einsatzzeit, Pegel und geringe spektrale Helligkeit.
- **Kurzer Anschlag:** kurzer, geschlossener Ruf.
- **Langer Anschlag:** zunehmend bewegte Klangfarbe und begrenzte Mikromodulation.
- **Legato:** Gleitbewegung derselben Stimme ohne Phasenreset.
- **Gleiche Taste erneut:** neuer Impuls bei gleichbleibender Tonhöhe.
- **CC64:** erhält die Phrase; Pedalloslassen beginnt den Ausklang.
- **CC1:** zusätzliche begrenzte Mikromodulation.
- **CC11:** Expression.
- **CC67:** Entfernung beziehungsweise Tiefe.
- **CC120:** sofortige Stille.
- **CC123:** natürlicher Ausklang aller Noten.
- **Pitch Bend:** maximal zwei Halbtöne.

Die Haltedauer wird nicht vorhergesagt. Die Engine entwickelt den Klang kausal,
während die Taste tatsächlich gehalten wird. Ein Langton enthält deshalb keine
wiederholte Aufnahmephrase und keine Loopnaht.

## Nutzung aller 88 Tasten

| Taste | Frequenz |
|---|---:|
| A0 / MIDI 21 | 27,5 Hz |
| A4 / MIDI 69 | 440 Hz |
| C8 / MIDI 108 | ungefähr 4.186,01 Hz |

Alle Halbtonschritte besitzen dasselbe Frequenzverhältnis
`2^(1/12)`. Die Klangfarbe bewegt sich stufenlos von körperhafteren zu
obertonreicheren Quellankern, ohne die musikalische Stimmung zu verändern.

Die extremen Tasten sind eine musikalische Extrapolation. Der Vertrag behauptet
nicht, dass ein realer Buckelwal genau diese gesamte Acht-Oktaven-Spanne
biologisch erzeugt.

## Bedienung

### Desktop

- `Buckelwal – An/Aus` startet standardmäßig `morph`.
- `Buckelwal – Spielbar` wählt `morph`.
- `Buckelwal – Organisch` wählt `organic`.
- `Buckelwal – Sample-Vergleich` wählt `realistic`.
- `Buckelwal – UFO-Modus` wählt `ufo`.
- `Buckelwal – Aus` beendet die Stimme.
- `Buckelwal – Status` zeigt den autoritativen Dienstzustand.

### Kommandozeile

```bash
python3 scripts/build_whale_morph_bank.py
python3 scripts/whale_live.py doctor
python3 scripts/whale_live.py start --voice-mode morph
python3 scripts/whale_live.py mode organic
python3 scripts/whale_live.py mode realistic
python3 scripts/whale_live.py mode ufo
python3 scripts/whale_live.py stop
python3 scripts/whale_live.py status
```

Die entsprechenden `just`-Ziele heißen unter anderem `whale-morph`,
`whale-organic`, `whale-realistic`, `whale-ufo`, `whale-morph-bank-build`, `whale-toggle` und
`whale-status`.

## Betriebs- und Sicherheitsvertrag

Der Doctor verlangt PipeWire, `aseqdump`, `pw-cat`, `systemctl`, `systemd-run`,
einen eindeutig erkannten Roland-Port sowie gültige Morph- und Samplebanken.
Fehlende oder veränderte Quell- beziehungsweise Tabellenhashes blockieren den
Start.

Der verwaltete Dienst behält die bisherigen Grenzen:

- Laufzeit 60 bis 21.600 Sekunden;
- 256 MiB Speicher;
- 80 Prozent eines CPU-Kerns;
- höchstens 32 Tasks;
- begrenzte Journalrate;
- MIDI-Queue höchstens 256 Ereignisse;
- höchstens 64 MIDI-Dispatches pro Audioblock;
- READY erst nach belegtem MIDI- und PCM-Start;
- kein `sfizz_jack`, keine unbegrenzte Logdatei und kein MIDI-Eventlogging.

Audioformat:

- 48.000 Hz;
- Stereo Float32;
- Standardblock 128 Frames;
- Master-Gain 0,16, harte Obergrenze 0,25;
- aktuelles PipeWire-Standardziel, sofern kein Ziel ausdrücklich gesetzt ist.

## Automatisiert belegt

- Morphmodell mit sieben periodischen Quellankern;
- vollständiger Bereich MIDI 21 bis 108;
- exakt null Samplezonen, Presets und Steuertasten;
- exakt null Ausgabe und kein Zustandsfortschritt im Leerlauf;
- alle 88 Tasten zielen auf ihre korrekte chromatische Frequenz;
- phasenkontinuierliches Legato;
- getrennte abgesetzte und wiederholte Artikulation;
- Sustain, Pitch Bend, Panic und Release;
- bitidentische Ausgabe und gleicher Zustand unabhängig von Render-Chunkgrößen;
- frequenzabhängige harmonische Bandbegrenzung;
- ausreichende Offline-Echtzeitreserve im Testvertrag;
- Organic-Modus mit exakter Leerlaufstille, chromatischer 88-Tasten-Abbildung,
  deterministischer Gestenreaktion und endlicher modaler Nachspur;
- reproduzierbarer A/B-Merkmalsvergleich gegen sechs quellengebundene echte
  Buckelwalclips; Details stehen in `buckelwal-organic-v3.md`.

## Physisch noch offen

Softwaretests können nicht belegen, dass das Ergebnis subjektiv überzeugend
nach einem Buckelwal klingt. Offen bleiben:

- Hörtest über Roland, MOTU M2, Focal und Receiver;
- Wahrnehmung von A0 und C8;
- Prüfung auf Orgel-, Buzz-, UFO- oder Aliascharakter;
- Pegelabgleich gegen die Vergleichsmodi;
- reale XRun- und Latenzmessung bei 48 kHz und 128 Frames.

Diese Punkte werden nicht schöngerechnet. Sie bilden einen eigenen physischen
Abnahmetask. Der vollständige Plan steht in
`docs/plans/buckelwal-continuous-voice-v1.md`.
