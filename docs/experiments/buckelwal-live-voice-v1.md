# Buckelwal Live Voice v1

## Produktgrenze

`Buckelwal Live Voice v1` macht das Roland FP-30X zu einem monophonen
Gesteninstrument für eine einzelne walartige Stimme.

Der aktuelle Klangkern ist eine abhängigkeitsfreie, deterministische Synthese.
Er ist ein spielbarer technischer Unterbau, aber noch kein biologisch
realistisches Buckelwalmodell. Echte, rechtlich nutzbare Buckelwalaufnahmen und
eine Sample-/Resynthese-Schicht sollen später denselben Gestenvertrag nutzen.

## Spielmodell

Das Instrument erzeugt bewusst keine Klavierakkorde. Die zuletzt angeschlagene
gehaltene Taste ist das aktuelle Tonziel einer einzigen Stimme.

- **Anschlagstärke:** Lautheit, Einsatz, Rauheit und Obertöne.
- **Haltezeit:** Der Laut läuft weiter und verändert Tonkontur und Formanten;
  es gibt keine kurze feste Samplelänge.
- **Überlappte Tasten:** Tonhöhe, Register und Timbre gleiten sampleweise ohne
  Hüllkurven-Neustart zum neuen Ziel. Wird die neueste Taste losgelassen, kehrt
  die Stimme zur zuvor gehaltenen Taste zurück.
- **Abgesetzte neue Taste:** Sobald keine Taste mehr gehalten und das Pedal frei
  ist, wird der alte Restklang über sechs Millisekunden auf null ausgeblendet;
  anschließend beginnt der nächste Anschlag als neue Phrase.
- **CC64 / Haltepedal:** Hält eine Phrase nach dem Loslassen der letzten Taste.
  Beim Freigeben beginnt ein von Haltezeit und Pedalwert abhängiger Ausklang.
- **CC1:** optional zusätzliche Rauheit und schnelleres Flattern.
- **CC11:** Expression.
- **CC67 / Soft-Pedal:** Entfernung beziehungsweise Tiefe.
- **CC120 / All Sound Off:** sofortige, harte Stummschaltung für MIDI-Panic.
- **CC123 / All Notes Off:** beendet alle gehaltenen Noten mit dem normalen
  Ausklang.

## Nutzung der 88 Tasten

Alle Tasten von A0 bis C8 werden monoton in einen Walstimmraum von 34 bis
1.850 Hz abgebildet. Die Abbildung ist nicht gleichstufig temperiert, sondern
im tiefen Bereich weiter aufgefächert.

| MIDI-Noten | Bereich | Klangrolle |
|---:|---|---|
| 21–35 | A0–H1 | Körperresonanz und Groans |
| 36–59 | C2–H3 | dunkle Moans und Pulse |
| 60–83 | C4–H5 | zentrale Wails und Rufbögen |
| 84–95 | C6–H6 | helle Calls |
| 96–108 | C7–C8 | Squeals und Flourishes |

Die Grenzen sind weich. Registerposition verändert gleichzeitig
Grundfrequenz, Subanteil, Formantgewicht, Pfeifanteil und Geräuschspektrum.
Fractionale Formantmodulation und der leicht verstimmte Anteil besitzen eigene
integrierte Phasenakkumulatoren; lange Holds und Phasen-Wraps erzeugen dadurch
keine altersabhängigen Sprünge.

## Bedienung

Read-only prüfen:

```bash
python3 scripts/whale_live.py doctor
```

Der Doctor verlangt PipeWire, `aseqdump`, `pw-cat`, `systemctl`, `systemd-run`
und genau einen Roland-artigen ALSA-MIDI-Eingang. Null oder mehrere passende
Ports blockieren den Start. Auch eine explizite Portnummer muss zu einem
Roland-artigen Port gehören; es wird niemals auf `Midi Through` ausgewichen.

Offline-Demo erzeugen, ohne etwas abzuspielen:

```bash
python3 scripts/whale_live.py demo /tmp/buckelwal-live-voice-v1-demo.wav
```

Verwaltet starten, prüfen und stoppen:

```bash
python3 scripts/whale_live.py start
python3 scripts/whale_live.py status
python3 scripts/whale_live.py stop
```

Der Start erzeugt die transiente User-Systemd-Unit
`audio-buckelwal-live-voice-v1.service` mit maximal sechs Stunden Laufzeit,
256 MiB Speichergrenze, 80 Prozent CPU-Quote, 32 Tasks und begrenzter
Journalrate. MIDI-Ereignisse werden nicht protokolliert.
Es gibt kein `sfizz_jack` und keine reguläre unbeschränkte Logdatei.

## Audiovertrag

- 48.000 Hz, Stereo, Float32 an `pw-cat`
- Standardblock: 128 Frames; der einheitenlose `pw-cat --latency`-Wert wird
  laut lokaler `pw-cat --help`-Schnittstelle als direkte Samplezahl interpretiert
- PCM-Zuleitung auf die nächste Zweierpotenz aus 4-KiB-Seiten begrenzt; auf dem
  aktuellen Host sind es bei 128 Frames 4.096 Byte beziehungsweise 512 Frames
- Systeme mit Speicherseiten über 4.096 Byte werden für diesen Niedriglatenzmodus
  fail-closed abgewiesen
- nichtblockierende, alle 50 ms abbrechbare PCM-Schreibschleife; zwei Sekunden
  ohne Fortschritt gelten als gestörter Audioverbraucher
- monotone Echtzeittaktung verhindert schneller-als-Echtzeit-Pufferung und setzt
  bereits bei genau einem Block Verspätung zurück, ohne Aufhol-Burst
- Standard-Master-Gain: 0,16
- harter Maximalwert für Master-Gain und Samples: 0,25
- Standardausgabe: aktuelles PipeWire-Ziel; für Referenzbetrieb soll dies das
  MOTU M2 sein

Der aktuelle globale PipeWire-Quantum von 1.024 Frames ist nicht freigegeben.
Vor einer Latenzfreigabe bleiben Loopback- und XRun-Messung erforderlich.

## Abnahme am 27. Juli 2026

### Belegt

- Synthese und MIDI-Gestenparser kompilieren ohne Zusatzpakete.
- 88-Tasten-Abbildung, Halten, Legato, Rückkehr zur vorherigen Taste, CC64,
  deterministische Ausgabe und Pegelgrenze sind automatisiert getestet.
- 12-Sekunden-Demo bei 48 kHz Stereo: Median-Renderzeit 1,514 Sekunden aus
  drei Läufen.
- Peak −20,387 dBFS; RMS −31,163 dBFS je Kanal; 0 Clipping-Samples.

### Blockiert

Bei der letzten Live-Prüfung waren weder Roland FP-30X noch MOTU M2 in
`aplay -l` sichtbar; `amidi -l` enthielt kein MIDI-Gerät. Deshalb konnte kein
realer Tastendruck bis zur PipeWire-Ausgabe end-to-end verifiziert werden.

Fehlt: eingeschaltetes und vom Kernel erkanntes Roland mit ALSA-MIDI-Port;
nötig für die reale Spiel-, Pedal-, Latenz- und XRun-Abnahme.

## Nächste Entwicklungsstufe

1. Reale MIDI-Nachrichten und das vorhandene Pedal am FP-30X aufzeichnen.
2. Live-Latenz bei 128, 256 und 512 Frames messen und den kleinsten stabilen
   Wert mit null XRuns wählen.
3. Rechtlich nutzbare Buckelwal-Einheiten kuratieren und Attack, tragfähigen
   Mittelteil und Ausklang trennen.
4. Den Synthesekern hinter derselben Gestenschnittstelle durch
   Sample-/Spektralresynthese ergänzen.
5. Danach Phrasen-, Themen- und Variationsgrammatik hinzufügen.
