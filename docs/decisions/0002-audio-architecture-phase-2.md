# ADR 0002: Audio-Architekturentscheidungen der Phase 2

- Status: angenommen
- Datum: 2026-08-06
- Aufgabe: `AUDIO-CONTROL-PLANE-V1-T002`
- Maschinenvertrag: `inventory/audio-architecture-decisions.v1.json`

## Ausgangslage

Die Audiozentrale besitzt bereits einen sicheren, read-only Wahrheits- und
Messvertrag. Reale MOTU- und Roland-Belege fehlen weiterhin; sie gehören zu
T032 und werden hier nicht ersetzt. Phase 2 darf deshalb Architektur festlegen,
aber weder Routing, Dienste, Geräte, Pegel noch Profile verändern.

## Dialektische Prüfung

**These:** Ein gemeinsamer PipeWire-Kern ist die robusteste Grundlage. Er ist
aktuell aktiv, beobachtbar und verbindet Desktop, Mopidy, Aufnahme,
Softwareinstrumente und die geplante Produktoberfläche.

**Gegenthese:** Für Qobuz könnte ein direkter ALSA-Pfad Umschaltungen und
Resampling vermeiden. Ein solcher Pfad wäre jedoch eine zweite Betriebsart mit
Geräteeigentum, Rückkehrlogik und Ausfallrisiko. Ohne titelgebundenen
Ratenbeleg wäre der Qualitätsgewinn nur behauptet.

**Alternative Sinnachse:** Wird maximale theoretische Signaltreue höher als
Betriebsstabilität gewichtet, lohnt ein begrenztes ALSA-Experiment. Wird sichere
Alltagsnutzung höher gewichtet, bleibt der 48-kHz-PipeWire-Graph der richtige
Standard. Die Entscheidung erlaubt deshalb das Experiment, macht es aber nicht
zum Grundsystem.

## Frische Belege

### Belegt

- PipeWire, PipeWire-Pulse und WirePlumber sind aktiv; Rate und Quantum stehen
  auf 48 kHz und 1024 Frames.
- Mopidy 3.4.2 ist aktiv. Mit exakt derselben zweifachen Konfigurationsfolge wie
  der Benutzerdienst ist Mopidy-Qobuz-Hires 0.1.1 aktiviert. Der Ausgang ist
  `pulsesink`; damit ist ein funktionaler Mischpfad, aber keine Exklusivität oder
  Bitgenauigkeit belegt. Distribution 0.1.1 und Modulversion 0.1.0
  widersprechen sich; die Paketidentität muss vor einer Übernahme frisch geprüft werden.
- Ardour ist nativ als 6.9.0 und als Flatpak 9.7.0 installiert. Das Flatpak sieht
  Home, den PipeWire-Socket, PulseAudio und Geräte. Das beweist noch keinen
  erfolgreichen Scan hostlokaler LV2- oder VST3-Pfade.
- Der breite Pluginbestand ist LV2. Nutzerlokal existieren zusätzlich LV2 und
  VST3. `jalv` und eine Windows-Plugin-Bridge sind nicht vorhanden.
- EasyEffects 8.2.7 ist als Flatpak installiert. Ein nativer Benutzerdienst
  ist nicht aktiv; ob ein Flatpak-Prozess läuft, folgt daraus nicht. Der ältere
  Plan verlangt ausdrücklich Hör- und Aufnahmeprüfung.

### Plausibel

- Flatpak-Ardour ist wegen der erheblich neueren Version und ausreichender
  Hostsicht die sinnvollere Kandidatin; Plugin-Scan und Session-Wiederöffnung
  müssen die Eignung noch belegen.
- Ein fester 48-kHz-Kern reduziert Zustandswechsel für Aufnahme, Desktop,
  Softwareinstrumente und Produktion.
- Mopidy ist als Qobuz- und Komfortadapter nützlich, sollte aber nicht den
  allgemeinen Audiokern definieren.

### Spekulativ und deshalb gegatet

- Hörbarer oder messbarer Vorteil eines direkten Qobuz-ALSA-Pfads.
- Stabilität von 512 Frames für Aufnahme und 128 Frames für Livespiel.
- Subjektiver Nutzen konkreter EasyEffects-Profile.

## Entscheidungen

### 1. PipeWire und ALSA

PipeWire bleibt Graph-, Routing- und Beobachtungskern. PipeWire-Pulse bleibt als
Kompatibilitätsschicht erhalten. Ein exklusiver ALSA-Pfad ist ausschließlich als
profilgebundenes Qobuz-Experiment erlaubt, wenn ein aktueller
`qobuz-rate-proof`, exklusives Geräteeigentum, ausgeschlossener Parallelmix und
ein begrenzter Rückkehrpfad vorliegen. Globales Stoppen von PipeWire bleibt
verboten.

### 2. Sampleraten

Gemischte Wiedergabe, Referenzhören, Aufnahme, Softwareinstrumente und
Produktion verwenden 48 kHz. Das Roland FP-30X liefert digitales Audio mit
44,1 kHz und wird genau einmal auf 48 kHz umgesetzt. Weitere absichtliche
Resamplingstufen sind verboten. Qobuz bleibt ohne titelgebundenen Beleg auf dem
48-kHz-Fallback; tracknative Wiedergabe ist nur im exklusiven Profil zulässig.

### 3. Quantums

1024 Frames sind der stabile Hör- und Desktopstandard. 512 Frames für Aufnahme
und 128 Frames für Livespiel bleiben Kandidaten, keine Zusagen. Freigabe verlangt
gebundene Latenz-, CPU-, XRun- und Recoverybelege am realen Zielgraphen.

### 4. Qobuz und Mopidy

Mopidy bleibt Provider- und Komfortadapter. Es ist weder allgemeiner Audiokern
noch Beleg für einen exklusiven oder bitgenauen Pfad. Der bestehende
`pulsesink`-Weg bleibt der sichere Fallback. Ein späterer exklusiver Pfad muss
Mopidy nicht zwingend umgehen, darf aber nur aus Messung entschieden werden.

### 5. Ardour

`org.ardour.Ardour` als Flatpak ist die kanonische Produktionsruntime. Die
native Ardour-Version bleibt installiert, ist aber nicht kanonisch und wird in
dieser Aufgabe nicht entfernt. Nur das Flatpak definiert Session-, Plugin- und
Wiederöffnungsverträge. Projekte und Templates liegen in einem manifestierten
Nutzerpfad, nicht in flüchtigen Sandboxdaten. Produktive Freigabe verlangt einen
belegten Plugin-Scan und eine erfolgreiche Session-Wiederöffnung.

### 6. Pluginruntime

Produktion hostet Plugins in Ardour. Live- und Experimentalprofile verwenden
nur einen begrenzten systemd-Benutzerhost. LV2 hat Priorität. VST3 ist nur
über Flatpak-Erweiterungen oder einen frisch belegten Benutzerpfad zulässig. Eine
Windows-Plugin-Bridge bleibt ohne eigene Entscheidung verboten. Eigenständiges
`sfizz_jack` bleibt verboten.

### 7. EasyEffects

EasyEffects ist ein optionaler profilgebundener Randprozessor, kein globaler
Audiokern. Standard ist inaktiv. `reference-listening`, `qobuz-exclusive` und
`production` umgehen EasyEffects. `desktop-mixed` und `voice-recording` dürfen
es erst nach Hör- beziehungsweise Aufnahmeprüfung verwenden. Der ältere Plan
wird als Designinput importiert, nicht als Aktivierungsautorität.

## Folgen und Risiken

Der Vertrag reduziert konkurrierende Wahrheiten, lässt aber zwei Installationen
von Ardour vorübergehend bestehen. Das ist Wartungsschuld, jedoch risikoärmer
als eine Entfernung ohne Session- und Plugin-Abnahme. Der 48-kHz-Standard kann
44,1-kHz-Musik resampeln; dafür bleibt der exklusive Qobuz-Pfad als gegatete
Alternative offen. Niedrige Quantums und DSP-Vorteile werden nicht vorweggenommen.

## Fehlende Belege

Fehlt: reale MOTU-/Roland-Sichtbarkeit, titelgebundener Qobuz-Ratenbeleg,
Round-Trip-Latenz, XRun-Lauf, Flatpak-Plugin-Scan, Session-Wiederöffnung und
subjektive EasyEffects-Abnahme. Nötig für: jede spätere Profilaktivierung, Exklusivroute,
Low-Latency-Freigabe oder Bereinigung der doppelten Ardour-Installation.

## Rückbau

Diese ADR und ihr Maschinenvertrag können gemeinsam zurückgenommen werden.
Hostzustand, Dienste, Konfigurationen, Secrets und Audio-Routing wurden nicht
verändert.
