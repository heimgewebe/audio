# Plan: Durchgehend spielbare Buckelwalstimme über 88 Tasten

- Version: 1
- Datum: 2026-07-29
- Status: **Repositoryumsetzung und Offlineprüfung aktiv; physische Klangabnahme offen**
- Übergeordneter Plan: `docs/plans/audio-configuration-redesign-v1.md`
- Übergeordnetes Bureau-Programm: `AUDIO-CONTROL-PLANE-V1`

## Dialektischer Ausgangspunkt

Echte Aufnahmephrasen liefern unmittelbar glaubwürdige Einzelklänge, machen das
Roland aber zu einem zonierten Sample-Abspieler. Ein frei erfundener
Oszillatorsynthesizer ist dagegen vollständig spielbar, klingt jedoch schnell
nach Theremin oder UFO.

Der realistische Mittelweg ist eine quellengestützte Resynthese: kurze,
periodische Stimmzyklen werden aus lizenzierten Buckelwalaufnahmen gewonnen,
phasengleich gemittelt und bandbegrenzt. Zur Laufzeit spielt ein kontinuierlicher
Oszillator diese Klangfarben auf der exakten Tonhöhe jeder Taste. Fertige
Melodien, Samplezonen und stationäres Meeresrauschen werden nicht übernommen.

## Produktentscheidung

Das Produkt ist **ein einziges monophones Instrument**.

- Alle 88 Tasten von A0 bis C8 spielen ihre normalen chromatischen Töne.
- A4 ist 440 Hz; die Stimmung folgt gleichstufiger Zwölftonteilung.
- Keine Taste schaltet Presets, Modi oder Steuerfunktionen.
- Es gibt keine hörbaren oder logischen Samplezonen.
- Die interne Klangfarbe wandert stufenlos zwischen quellengestützten Ankern.
- Der zuletzt angeschlagene gehaltene Ton führt dieselbe Walstimme.
- `realistic` und `ufo` bleiben nur als Vergleichsmodi erhalten.

## Realistische Grenze

Der Modus ist kein biologisches Modell des Stimmapparats eines Buckelwals. Die
vorhandenen Aufnahmen reichen dafür weder methodisch noch akustisch aus. Vor
allem an den extremen Klaviertönen ist der Klang eine musikalische,
quellengestützte Extrapolation.

Der Plan behauptet daher nur:

- Tonhöhen sind musikalisch exakt und vollständig spielbar;
- Klangtabellen stammen nachweisbar aus Buckelwalaufnahmen;
- keine Aufnahmephrase wird als solche abgespielt;
- nichtperiodisches Umgebungsrauschen wird durch Periodenmittelung strukturell
  unterdrückt;
- die endgültige subjektive Walähnlichkeit erfordert einen physischen Hörtest.

## Architektur

### Offline-Builder

`scripts/build_whale_morph_bank.py` erzeugt deterministisch das Modell
`assets/whale-sources/morph/manifest.json`.

Der Builder:

1. prüft den bestehenden Samplebankvertrag und alle Quellhashes;
2. sucht in fest gebundenen Quellclips stabile periodische Fenster;
3. verfeinert die Periodenlänge auf 48 kHz;
4. richtet mehrere Zyklen phasengleich aus;
5. mittelt die Zyklen und entfernt Gleichanteile;
6. erzeugt bandbegrenzte harmonische Stufen;
7. quantisiert die Tabellen als hashgebundenes PCM16 im Manifest.

Nichtperiodisches Meeresrauschen und lange Aufnahmephrasen überstehen diese
Extraktion nicht als fortlaufende Ebene.

### Laufzeitbank

`WhaleMorphBank` validiert:

- Manifesttyp und Version;
- 48-kHz-Vertrag;
- vollständigen Bereich 21 bis 108;
- Quellmanifest-Hash;
- Tabellenhashes und Framezahlen;
- eindeutige, sortierte interne Anker;
- konsistente bandbegrenzte Stufen.

Die Anker sind keine Presets oder Tastaturzonen. Zwischen je zwei Ankern wird
für jeden Zwischenwert mit einer weichen, gleichleistungsgewichteten Überblendung
interpoliert.

### Stimme

`WhaleMorphVoice` ist monophon und phasenkontinuierlich.

| Eingabe | Wirkung |
|---|---|
| Taste | exakte chromatische Zieltonhöhe |
| Anschlagstärke | Einsatzzeit, Pegel und geringe Helligkeitsverschiebung |
| kurze Haltedauer | kurzer, geschlossener Ruf |
| lange Haltedauer | zunehmende spektrale und amplitudenbezogene Entwicklung |
| Legato | Gleitbewegung derselben Stimme ohne Phasenreset |
| gleiche Taste erneut | neuer Impuls ohne Wechsel des Grundtons |
| Haltepedal | Fortsetzung derselben Phrase |
| Modulation CC1 | begrenzte zusätzliche Mikromodulation |
| Expression CC11 | Pegel |
| Softpedal CC67 | Distanz und Tiefe |
| Pitch Bend | höchstens zwei Halbtöne |
| CC120 | sofortige Stille |
| CC123 | natürlicher Ausklang |

Die Artikulation ist kausal. Die Engine versucht nicht vorherzususehen, wie
lange eine Taste künftig gehalten wird, sondern entwickelt den Klang während
des tatsächlichen Haltens.

## Phasen

### Phase 1 – Zielvertrag und Vergleichsgrundlage

- vollständige 88-Tasten-Stimmung festlegen;
- Monophonie und Last-Note-Priority festlegen;
- alte Modi als Vergleich erhalten;
- kurze, lange, wiederholte und legatogespielte Testgesten definieren.

**Gate:** Alle Tonhöhen und Gesten sind maschinenlesbar getestet.

### Phase 2 – Quellmodell

- geeignete periodische Quellfenster bestimmen;
- Periodizitätsuntergrenze erzwingen;
- nichtperiodische Anteile durch phasengleiche Mittelung reduzieren;
- bandbegrenzte Tabellen und Provenienzmanifest erzeugen.

**Gate:** Kein fehlender oder geänderter Quellhash wird akzeptiert; mindestens
drei brauchbare Anker müssen beide Endpunkte der Klaviatur abdecken.

### Phase 3 – Kontinuierlicher Synthesekern

- exakte 12-TET-Tonhöhen;
- phasenkontinuierliches Wavetable-Morphing;
- weiche Registerfärbung ohne Zonen;
- vollständige Stille ohne Note;
- begrenzte Pegel und Ressourcen.

**Gate:** Alle 88 Tasten sind hörbar, chromatisch und frei von Steuerbelegung.

### Phase 4 – Artikulation

- Anschlagstärke;
- kausale Langtonentwicklung;
- abgesetzter neuer Ruf;
- Legato;
- Wiederholungsimpuls;
- Sustain und Panic.

**Gate:** identische Ereignisfolgen erzeugen reproduzierbare Ergebnisse;
Chunkgrößen ändern weder Ausgabe noch Endzustand.

### Phase 5 – Offline- und Laufzeitprüfung

- vollständige Unit- und Integrationssuite;
- deterministischer Builder-Neulauf;
- Demo-WAV für kurze, lange und legatogespielte Sequenzen;
- CPU-Echtzeitreserve;
- Alias- und Rauschgrenzen;
- Doctor- und Desktopintegration.

**Gate:** keine Regression der bisherigen Audio-Sicherheitsverträge.

### Phase 6 – Physische Klangabnahme

Diese Phase darf nicht durch Offline-Metriken ersetzt werden.

- Roland FP-30X als MIDIquelle;
- MOTU M2 und reale Wiedergabekette;
- chromatische Tonleiter über den gesamten Bereich;
- kurze und lange Rufe;
- Legato, Wiederholung und Pedal;
- Vergleich gegen `realistic` und `ufo`;
- Prüfung auf UFO-, Orgel-, Buzz-, Alias- und Rauschcharakter.

**Gate:** Der Nutzer bestätigt, dass der neue Modus als spielbare Walstimme
brauchbar ist. Misslingt dies, bleiben die technische Grundlage und die
Vergleichsmodi erhalten; der Modus wird nicht als klanglich abgeschlossen
bezeichnet.

### Phase 7 – Gemeinsame Kreativlaufzeit

Nach physischer Abnahme wird der Modus in die übergeordnete Aufgabe
`AUDIO-CONTROL-PLANE-V1-T006` übernommen. Die endgültige Live-Aktivierung und
Rückkehrprüfung zum Referenzprofil bleiben `AUDIO-CONTROL-PLANE-V1-T007`
vorbehalten.

## Abnahmekriterien

| Kriterium | Vertrag |
|---|---|
| MIDI-Bereich | 21–108, genau 88 Tasten |
| Stimmung | 12-TET, A4 = 440 Hz |
| Samplezonen | 0 |
| Presets | 0 |
| reservierte Tasten | 0 |
| gleichzeitig aktive Walstimmen | 1 |
| Dauerrauschen ohne Note | exakt 0 |
| Langphrasenwiedergabe | verboten |
| Tabellenprovenienz | Quell- und Tabellen-SHA-256 |
| Aliasing-Schutz | harmonische, frequenzabhängig überblendete Bandbegrenzung |
| Echtzeitreserve | mindestens Faktor 3 im Offlinebenchmark |
| Chunk-Invarianz | bitidentische Ausgabe und gleicher Endzustand |
| Live-XRuns | 0 im späteren Dauertest |

## Risiken und Alternativpfad

| Risiko | Gewicht | Behandlung |
|---|---:|---|
| Wavetable klingt nach Orgel oder Buzz | hoch | mehrere Quellanker, zeitliche Morphbewegung, physisches Gate |
| tiefe Quellen enthalten wenig klare Periodizität | mittel | harte Periodizitätsgrenze; bessere Quelle statt Schönrechnen |
| hohe Töne aliasen | mittel | harmonische Mip-Stufen und weiche Stufenüberblendung |
| Klang wird zwar sauber, aber nicht walartig | mittel | Vergleichsmodi erhalten; weitere quellengestützte Transienten erst nach Hörbefund |
| Python reicht live nicht | niedrig bis mittel | Echtzeitmessung; DSP-Kern erst bei belegtem Bedarf nach Rust verlagern |

Der Alternativpfad ist ein granularer Sampleplayer mit besserer
Rauschunterdrückung. Er kann natürlicher klingen, erfüllt aber nicht das
Hauptziel einer frei spielbaren Stimme und bleibt daher nur Rückfalloption.

## Noch offene Belege

- subjektive Walähnlichkeit über Focal und Receiver;
- Wahrnehmung der extremen Tasten A0 und C8;
- reale XRun-Zahl bei 48 kHz und 128 Frames;
- angemessene Gesamtlautstärke gegenüber den Vergleichsmodi;
- mögliche Notwendigkeit kurzer quellengestützter Einsatzstransienten.

Diese Punkte gehören in einen gesonderten physischen Abnahmetask und dürfen
nicht aus Offlineergebnissen interpoliert werden.
