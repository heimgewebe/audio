# Plan: Audio-Konfiguration vollständig neu entwerfen

- Version: 1
- Datum: 2026-07-26
- Status: **Planung – keine Anwendung auf dem Heim-PC**

## Dialektischer Ausgangspunkt

Eine einzige universelle Konfiguration wäre einfach zu bedienen, kann aber
nicht gleichzeitig bitgenaue Wiedergabe, gemischtes Desktop-Audio, sehr niedrige
Live-Latenz und robuste Aufnahme optimal erfüllen.

Das Gegenextrem – für jeden Fall ein unabhängiger Stack – maximiert lokale
Optimierung, erhöht aber Drift, Fehlbedienung und Recoveryaufwand.

Der geplante Mittelweg ist ein gemeinsames Systemmodell mit wenigen expliziten,
messbaren Profilen. Profile dürfen sich unterscheiden, aber nicht heimlich
gegenseitig Zustand hinterlassen.

## Ziele

1. höchste nachvollziehbare Qualität beim Kopfhörerhören;
2. reproduzierbare Sprach-, Instrument- und Klavieraufnahme;
3. stabiles, latenzarmes Roland-Spiel durch Softwareinstrumente;
4. sichere Nutzung des Pioneer-Receivers und des Bluetooth-Senders;
5. experimentelle Klangerzeugung ohne Gefährdung des Referenzbetriebs;
6. vollständig prüfbarer Zustand mit Doctor, Diff, Apply und Rollback.

## Nichtziele dieses Plans

- noch keine Änderung an PipeWire, WirePlumber, Mopidy, Ardour oder Geräten;
- noch keine Paketentfernung;
- noch keine Festlegung auf eine einzige Samplerate für alle Zwecke;
- keine Behauptung von Bitgenauigkeit ohne Messung.

## Zielprofile

| Profil | Zweck | Vorrang |
|---|---|---|
| `reference-listening` | Focal Clear MG über MOTU und Lake People | Transparenz, keine DSP-Überraschungen |
| `qobuz-exclusive` | exklusive Musikwiedergabe | kein unbelegtes Resampling |
| `desktop-mixed` | Browser, Systemton, Qobuz parallel | Komfort und stabile Mischung |
| `voice-recording` | Rode NT1-A über MOTU | Gain, Rauschabstand, sichere 48 V |
| `piano-digital-recording` | Roland-Audio und MIDI | Synchronität und Wiederholbarkeit |
| `piano-software-live` | Roland als MIDI-Controller | niedrige Latenz ohne XRuns |
| `production` | Ardour, Plugins und Overdubs | reproduzierbare Sessions |
| `receiver` | Ausgabe an Pioneer | definierter Pegel und Kanalmodus |
| `bluetooth-convenience` | 1MII B03 Pro | Komfort, ausdrücklich nicht Referenz |
| `experimental` | Dauersong und Klanglabore | Isolation und harte Ressourcenlimits |

## Phase 0 – Veränderungsstopp und Baseline

**Für Dummies:** Erst wird fotografiert und gemessen, bevor etwas umgebaut wird.

### Arbeit

- vollständige Geräte-, Node-, Port-, MIDI- und Dienstinventur;
- physischer Signalplan mit Kabeln, Eingängen und Schalterstellungen;
- aktuelle Sample-Raten, Quantums, Standardgeräte und Resampler erfassen;
- Qobuz-, Aufnahme- und Live-Latenzpfade separat messen;
- bestehende Konfigurationen und relevante Paketstände hashbinden;
- Recovery- und Rückfallzustand dokumentieren.

### Gate

Kein Apply, solange Baseline, physischer Readback oder Rückfallpfad fehlen.

## Phase 1 – Anforderungen und Referenzpegel

### Arbeit

- maximale sichere Hörlautstärke und Referenzstellung festlegen;
- MOTU-Ausgang, Lake-People-Verstärkung und Receiver-Pegel aufeinander beziehen;
- Rode-Gain für leise, normale und laute Stimme messen;
- direkte gegen softwareseitige Abhörung vergleichen;
- zulässige Latenz je Profil festlegen.

### Messgrößen

- Peak und RMS/LUFS, soweit sinnvoll;
- Rauschabstand und Grundrauschen;
- Round-Trip-Latenz;
- XRun-Zahl;
- Resamplingstatus;
- Geräte-Recovery nach Aus- und Einstecken.

## Phase 2 – Architekturentscheidungen

Folgende Fragen werden durch Messung, nicht durch Vorliebe entschieden:

1. Bleibt PipeWire die gemeinsame Koordinationsschicht?
2. Benötigt `qobuz-exclusive` einen separaten ALSA-Pfad?
3. Soll der Graph dynamisch zwischen 44,1- und 48-kHz-Familien wechseln oder
   dauerhaft auf 48 kHz laufen?
4. Welche Quantums gelten für Hören, Aufnahme und Live-Spiel?
5. Bleibt Mopidy der Qobuz-Kern oder nur eine Komfortoberfläche?
6. Wird Ardour nativ oder als Flatpak kanonisch?
7. Welche Plugin-Runtime bleibt erhalten?
8. Welche Aufgaben übernimmt EasyEffects, und in welchen Profilen ist es
   garantiert umgangen?

### Vorläufige Hypothese

PipeWire bleibt die gemeinsame Schalt- und Beobachtungsebene. Ein exklusiver
ALSA-Pfad wird nur eingeführt, wenn er messbar einen relevanten Vorteil liefert
und sauber zurück in den Mischbetrieb wechseln kann.

## Phase 3 – Deklaratives Profilmodell

Jedes Profil beschreibt mindestens:

- erwartete Geräteidentitäten statt flüchtiger Nummern;
- Quelle, Senke, MIDI- und Monitorpfad;
- Sample-Rate und zulässiges Resampling;
- Quantum und Latenzziel;
- Kanalzahl und Format;
- DSP-/Plugin-Vertrag;
- Standardquelle und Standardsenke;
- Start-, Stop-, Apply- und Rollbackreihenfolge;
- Zeit-, CPU-, Speicher- und Loglimits;
- Abnahmemessungen.

Secrets, volatile IDs und große Assets werden nicht eingecheckt.

## Phase 4 – Doctor, Plan und Diff

Ein read-only `audio doctor` erzeugt:

- beobachteten Istzustand;
- erwarteten Profilzustand;
- Abweichungen mit Schweregrad;
- unerwartete Resampler, Defaultgeräte und globale Overrides;
- Geräte- und USB-Fehler;
- offene Aufnahme- oder Pluginprozesse;
- unbegrenzte Logs und Laufzeitzustände;
- einen maschinenlesbaren, hashgebundenen Bericht.

`audio plan PROFILE` berechnet Änderungen ohne Wirkung. `audio diff` zeigt die
exakten Dateien, Dienste, Metadaten und Routen. Erst `audio apply` darf ändern.

## Phase 5 – Implementierung in Isolation

- Konfigurationen zunächst in einem isolierten Testprofil erzeugen;
- keine manuelle Änderung als einzige Quelle der Wahrheit;
- atomare Dateien und explizite Eigentümerschaft;
- Systemd-Einheiten mit Ressourcen- und Loggrenzen;
- Hardwareabhängigkeiten fail-closed behandeln;
- keine globalen Audio-Dienste stoppen, wenn ein profilgebundener Mechanismus
  genügt.

## Phase 6 – Profilweise Laborprüfung

Für jedes Profil:

1. Kaltstart;
2. 30-Minuten-Dauerlauf;
3. Gerät aus- und einstecken;
4. Wechsel zu einem anderen Profil und zurück;
5. Abbruch während Start und Aufnahme;
6. Plattenplatz- und Logflut-Negativtest;
7. Messung von Rate, Format, Latenz, XRuns und Routing;
8. subjektiver Hörvergleich erst nach technischer Blindkontrolle.

## Phase 7 – Gestufte Anwendung

Reihenfolge nach Risiko:

1. read-only Doctor;
2. `desktop-mixed`;
3. `reference-listening`;
4. `voice-recording`;
5. `piano-digital-recording`;
6. `piano-software-live`;
7. `qobuz-exclusive`;
8. Receiver und Bluetooth;
9. Produktion und Experimente.

Nach jeder Stufe folgt ein Beobachtungsfenster. Kein nächstes Profil bei
unaufgelöster Regression.

## Phase 8 – Betrieb und Wartung

- monatlicher read-only Doctor;
- Driftbericht nach Paket- oder Kernelupdates;
- Versions- und Assetmanifest;
- begrenzte Logs und Speicherbudgets;
- Backup- und Restore-Probe der Konfiguration;
- neue Experimente beginnen in isolierten Profilen;
- Dauersong-Audits bleiben nebenwirkungsfrei.

## Abnahmekriterien

### Für alle Profile

- deterministisches Apply und idempotenter zweiter Lauf;
- vollständiger Rollback;
- keine unbekannten globalen PipeWire-Overrides;
- keine unbegrenzten Logs;
- keine verwaisten Prozesse oder PID-Verwechslungen;
- Wiederanlauf nach Geräteverlust dokumentiert.

### `qobuz-exclusive`

- tatsächliche Rate und Format sind für den Titel belegt;
- kein Resampling, sofern das Profil dies behauptet;
- kein paralleler unbeabsichtigter Mischpfad.

### `piano-software-live`

- gemessene statt geschätzte Round-Trip-Latenz;
- null XRuns im 30-Minuten-Test;
- CPU- und Loggrenzen greifen;
- Wechsel zurück zum Hörprofil hinterlässt keine niedrige Quantum-Einstellung.

### Aufnahme

- korrekte Quelle und Kanalzuordnung;
- 24-Bit-Vertrag technisch belegt;
- Datei wird bei regulärem Stop und Abbruch verwertbar finalisiert;
- Speichergrenze und Aufnahmezeit sichtbar;
- direkte und Software-Abhörung sind eindeutig gekennzeichnet.

## Risiken und Trade-offs

| Entscheidung | Nutzen | Risiko |
|---|---|---|
| ein gemeinsamer PipeWire-Kern | beobachtbar und flexibel | exklusive Pfade komplexer |
| dynamische Rate | weniger Resampling | Umschalt- und Gerätefehler |
| feste 48 kHz | robust für Aufnahme/Video | Qobuz-44,1-kHz-Titel werden gemischt resampelt |
| sehr kleines Quantum | geringe Latenz | höhere CPU- und XRun-Gefahr |
| getrennte Profile | klare Optimierung | mehr Übergänge zu testen |
| Plugin-Isolation | sichere Experimente | zusätzlicher Betriebsaufwand |

## Fehlende Belege vor Ausführung

- Fotos oder direkter Readback der analogen Verkabelung;
- Gain-Stellungen an MOTU, Lake People und Pioneer;
- tatsächlicher Bluetooth-Codec des 1MII-Pfads;
- reproduzierbare USB-Fehlerursache des MOTU;
- objektive Latenz- und XRun-Messungen;
- Entscheidung über den kanonischen Ardour- und Pluginbestand.

Ohne diese Punkte darf der Plan nicht als fertige Zielkonfiguration gelten.
