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

Quellseiten, vollständige Urheber, Lizenz-URIs, Bearbeitungshinweise,
Rohdateigrößen und erwartete SHA-256-Werte stehen in
`assets/whale-sources/SOURCES.json`; `assets/whale-sources/NOTICE.md` begleitet
die weitergegebenen Dateien menschenlesbar. Die Rohdateien bleiben unverändert
erhalten. Der Builder prüft sie vor FFmpeg, baut pfad- und symlinkgesichert in
einem privaten Staging-Verzeichnis und ersetzt die produktive Bank erst nach
vollständiger Validierung atomar. Er erzeugt 19 mono PCM16-Phrasen bei 48 kHz,
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
- **Pitch Bend:** bis ±120 Cent Steuerweg, jedoch gemeinsam mit Tastenzone und
  CC1-Flutter hart auf insgesamt ±4 Halbtöne begrenzt.

## Nutzung der 88 Tasten

Die Samplebank besitzt 27 Zonen für A0 bis C8. Jede Taste liegt höchstens vier
Halbtöne vom Wurzelton ihrer Originalphrase entfernt.

| MIDI-Noten | nominelle Präferenz | Ausgangsmaterial |
|---:|---|---|
| 21–48 | tief | Moo-, Stöhn- und Körperlaute |
| 49–84 | mittel | Gesangsphrasen verschiedener Aufnahmen und Populationen |
| 85–108 | hoch | Wheeze-, Atem- und helle Ruflaute |

Die tatsächliche Auswahl folgt zuerst dem nächsten Zonenwurzelton und nutzt die
Registerfamilie nur als Gleichstandsentscheidung. Daher liegen die belegten
Grenzzuweisungen 42/46 im Gesangsregister, 84 im hohen Register sowie 86/90
wieder im Gesangsregister. Alle 27 Zonen und 19 Clips bleiben dadurch erreichbar.

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
- MIDI-Queue maximal 256 Ereignisse, pro Audioblock höchstens 64 Dispatches;
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

- 141/141 Repositorytests, beide Safety-Gates und Compileall bestanden.
- 8 Quellen, 19 Phrasen, 27 Zonen; alle 88 Tasten höchstens vier Halbtöne vom
  Zonenwurzelton entfernt.
- deterministischer Schema-2-Bank-Neubau: Gesamt-Hash
  `82f77f68f7fbce6e0f6b3f00805f57a1128df88bfda3bb6bcba9eb1b8eae1a0f`;
  die 19 WAV-Dateien blieben gegenüber dem vorherigen Build bitidentisch.
- Katalog-Hash `d5a9dad7f56ea9893c1d1c458578447721529b032c8c4c006eea245ef43687b8`;
  Manifest-Hash `cdea5da13edf435a631043459eef2687cc973386dd28c287c33efbf13dc6fd67`.
- Bankladezeit einschließlich Katalog-, Rohdatei- und Clipprüfung: 368,522 ms.
- Sturmprobe mit 600 schnellen Wechseln, maximal drei ausblendenden
  Altschichten und jüngstem koalesziertem Ziel: Median 379,457 µs, p99
  729,814 µs, Maximum 1.233,397 µs bei 2.666,667 µs Frist.
- stiller Block: Median 0,370 µs, p99 0,650 µs.
- 12-Sekunden-Demo: Peak −25,137 dBFS, RMS ungefähr −41,37 dBFS, kein Clipping.
- Sustain, Legato-Crossfade, Release, Panic, deterministische Ausgabe,
  Samplehashes und Float32-Stereoformat sind getestet.

### Live belegt

- Roland als ALSA-MIDI-Quelle erkannt; die Portnummer darf sich nach USB-Neustart
  ändern und wird bei `auto` erneut aufgelöst.
- realistische Engine als `active/running` mit `voice_mode=realistic`.
- PipeWire-Ausgabe auf MOTU M2.
- 40,9 MiB Service-Speicher, 47,1 MiB Prozess-Höchstwert, sechs Tasks,
  null Neustarts.
- stiller Livebetrieb ungefähr 1,08 Prozent eines CPU-Kerns.
- stabiler Beobachtungsabschnitt: MOTU `3 → 3`, Fluidsynth `0 → 0`; keine
  neuen PipeWire-Fehler beziehungsweise XRuns. Die drei absoluten MOTU-Zähler
  entstanden vor dem beobachteten stabilen Abschnitt und werden nicht der
  Engine zugerechnet.
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
