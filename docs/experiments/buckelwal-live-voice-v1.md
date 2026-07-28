# Buckelwal Live Voice

## Produktgrenze

`Buckelwal Live Voice` macht das Roland FP-30X zu einem monophonen
Gesteninstrument für eine einzelne Buckelwalstimme. Standard ist der Modus
`realistic`: Er spielt lokal gespeicherte echte Buckelwalaufnahmen mit eng
begrenzter Tonhöhenverschiebung. Der frühere Oszillatorsynthesizer bleibt als
separater Modus `ufo` erhalten und wird nicht als realistischer Walgesang
bezeichnet.

Die realistische Engine ist keine Rekonstruktion biologischer Stimmerzeugung.
Sie erhält den natürlichen Charakter der Ausgangsaufnahmen, indem sie viele
Originalphrasen über eng gestaffelte Tastaturzonen verteilt, statt ein einziges
Signal über mehrere Oktaven zu verbiegen.

## Samplebank und Rechte

Die Bank wird durch `scripts/build_whale_sample_bank.py` deterministisch aus
acht dokumentierten Quellen gebaut:

- CC0 1.0;
- Public Domain, U.S. National Park Service;
- CC BY 2.5, PLOS ONE.

Quellseiten, Attributionen, Lizenzen und SHA-256-Werte stehen in
`assets/whale-sources/SOURCES.json` und
`assets/whale-sources/processed/manifest.json`. Die Rohdateien bleiben
unverändert erhalten. Der Builder erzeugt 19 mono PCM16-Phrasen bei 48 kHz,
normalisiert sie konservativ und versieht sie mit geloopten Mittelbereichen.

## Spielmodell

Das Instrument erzeugt bewusst keine Klavierakkorde. Die zuletzt angeschlagene
gehaltene Taste steuert eine einzige Stimme.

- **Anschlagstärke:** Lautheit und Einsatz der Originalphrase.
- **Haltezeit:** Der natürliche Mittelteil wird mit Equal-Power-Crossfade
  geloopt.
- **Überlappte Tasten:** Wechsel zur passenden Originalphrase über einen
  90-ms-Equal-Power-Crossfade.
- **Abgesetzte Taste:** Start einer neuen Originalphrase.
- **CC64 / Haltepedal:** Hält die Phrase; beim Freigeben beginnt ein natürlicher
  Ausklang.
- **CC1:** höchstens 2,5 Cent langsame Schwankung; kein Synthesizer-Vibrato.
- **CC11:** Expression.
- **CC67:** Entfernung beziehungsweise Tiefe.
- **CC120:** sofortige Stummschaltung.
- **CC123:** normaler Ausklang aller Noten.
- **Pitch Bend:** höchstens ±120 Cent zusätzlich zur Tastenzone.

## Nutzung der 88 Tasten

Die Samplebank besitzt 27 Zonen für A0 bis C8. Jede Taste liegt höchstens vier
Halbtöne vom Wurzelton ihrer Originalphrase entfernt.

| MIDI-Noten | Klangfamilie | Ausgangsmaterial |
|---:|---|---|
| 21–48 | tief | Moo-, Stöhn- und Körperlaute |
| 49–84 | mittel | Gesangsphrasen verschiedener Aufnahmen und Populationen |
| 85–108 | hoch | Wheeze-, Atem- und helle Ruflaute |

Die Zonierung begrenzt den typischen Theremin-/UFO-Effekt großer
Tonhöhenverschiebungen. Sie garantiert nicht, dass jede Taste wie eine
musikalisch temperierte Tonhöhe wahrgenommen wird; Walrufe bleiben komplexe
Geräusch- und Gesangsereignisse.

## Bedienung

### Desktop

Im GNOME-Anwendungsmenü sind installiert:

- `Buckelwal – An/Aus`;
- `Buckelwal – Realistisch`;
- `Buckelwal – UFO-Modus`;
- `Buckelwal – Aus`;
- `Buckelwal – Status`.

`Super+Alt+W` schaltet die realistische Stimme ein oder aus. Desktopaktionen
zeigen ihren Ausgang per Systembenachrichtigung.

### Kommandozeile und Chat-Operator

```bash
python3 scripts/whale_live.py start --voice-mode realistic
python3 scripts/whale_live.py stop
python3 scripts/whale_live.py toggle
python3 scripts/whale_live.py mode realistic
python3 scripts/whale_live.py mode ufo
python3 scripts/whale_live.py status
```

Die entsprechenden `just`-Ziele heißen `whale-start`, `whale-stop`,
`whale-toggle`, `whale-realistic`, `whale-ufo` und `whale-status`.

## Betriebs- und Audiovertrag

Der Doctor verlangt PipeWire, `aseqdump`, `pw-cat`, `systemctl`, `systemd-run`,
genau einen Roland-artigen MIDI-Port sowie eine vollständig hashgeprüfte
Samplebank. Fehlende oder veränderte Samples blockieren den realistischen
Start.

Der verwaltete Start erzeugt
`audio-buckelwal-live-voice-v1.service` als `Type=notify` mit:

- höchstens sechs Stunden Laufzeit;
- 256 MiB Speichergrenze;
- 80 Prozent CPU-Quote;
- 32 Tasks;
- begrenzter Journalrate;
- READY erst nach mindestens 100 ms erfolgreichem MIDI-/PCM-Verbrauch.

MIDI-Ereignisse werden nicht protokolliert. Es gibt kein `sfizz_jack` und keine
unbeschränkte Logdatei.

Audioformat:

- 48.000 Hz;
- Stereo Float32 an `pw-cat`;
- Standardblock 128 Frames;
- 4.096-Byte-PCM-Pipe auf dem aktuellen Host;
- nichtblockierende, abbrechbare Schreibschleife;
- Standard-Master-Gain 0,16;
- harter Maximalwert 0,25;
- Standardausgabe aktuelles PipeWire-Ziel, derzeit MOTU M2.

Der globale PipeWire-Graph läuft weiterhin mit Quantum 1.024. Die interne
128-Frame-Berechnung ist deshalb keine gemessene Hardwarelatenz. Eine
Round-Trip-Angabe bleibt bis zur physischen Loopback-Messung unzulässig.

## Abnahme am 28. Juli 2026

### Automatisiert und offline belegt

- 114/114 Repositorytests bestanden.
- 8 Quellen, 19 Phrasen, 27 Zonen; alle 88 Tasten höchstens vier Halbtöne vom
  Zonenwurzelton entfernt.
- deterministischer Bank-Neubau: Gesamt-Hash
  `da6bb02ab1d7c6d35356116efb752c961a6326ad9190d61bacbb0b1be2872537`.
- Bankladezeit: 388,438 ms.
- 128-Frame-Block, realistische Stimme aktiv: Median 223,936 µs,
  p99 344,762 µs, Maximum 511,373 µs bei 2.666,667 µs Frist.
- stiller Block: Median 0,370 µs, p99 0,650 µs.
- 12-Sekunden-Demo: Peak −25,137 dBFS, RMS ungefähr −41,37 dBFS, kein Clipping.
- Sustain, Legato-Crossfade, Release, Panic, deterministische Ausgabe,
  Samplehashes und Float32-Stereoformat sind getestet.

### Live belegt

- Roland als ALSA-MIDI-Quelle erkannt; die Portnummer darf sich nach USB-Neustart
  ändern und wird bei `auto` erneut aufgelöst.
- realistische Engine als `active/running` mit `voice_mode=realistic`.
- PipeWire-Ausgabe auf MOTU M2.
- 40,7 MiB Service-Speicher, 46,8 MiB Prozess-Höchstwert, sechs Tasks,
  null Neustarts.
- stiller Livebetrieb ungefähr 1,33 Prozent eines CPU-Kerns.
- stabiler Beobachtungsabschnitt: null neue PipeWire-Fehler beziehungsweise
  XRuns.
- Desktop-Umschaltung aus/an und Moduswechsel UFO/realistisch wurden mit
  Status-Readback ausgeführt.

### Noch subjektiv beziehungsweise physisch offen

Der Nutzer muss Klangwirkung, Tastenzuordnung, Schleifenübergänge und Pedalgefühl
am tatsächlichen Instrument beurteilen. Die Software kann belegen, dass echte
Aufnahmen statt der Oszillatorbank laufen; sie kann nicht automatisch belegen,
dass jede Phrase musikalisch überzeugend wirkt. Die physische
Round-Trip-Latenz bleibt ebenfalls ungemessen.

## Historischer UFO-Modus

`--voice-mode ufo` enthält weiterhin den deterministischen Synthesekern mit
integrierten Phasen, 88-Tasten-Frequenzabbildung, Legato und Retrigger-Fades.
Er dient als experimenteller Klangmodus und Regressionstest, nicht als
Buckelwal-Referenz.

## Nächste Entwicklungsstufe

1. subjektiv schwache oder auffällige Phrasen anhand realer Spieltests markieren;
2. Loopgrenzen und Zonen nach diesen Belegen neu kuratieren;
3. physische Loopback-Latenz am MOTU messen;
4. danach optional Phrasen-, Themen- und Variationsgrammatik ergänzen.
