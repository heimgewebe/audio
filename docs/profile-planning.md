# Read-only Profilplanung

`audio-plan PROFILE` verbindet den Live-Doctor, private physische Beobachtungen
und den deklarativen Profilkatalog. Das Ergebnis nennt fehlende Hardware,
fehlende oder widersprüchliche physische Fakten und die geplanten Änderungen.

Der Planer hat keine Apply-Funktion. Kandidaten für Quantum oder native
Sampleraten bleiben blockiert, bis Latenz-, XRun- und Resamplingmessungen
vorliegen.

Noch ausstehende Labor-Gates werden maschinenlesbar als `unresolved_laboratory_gates` ausgegeben und blockieren die Bereitschaft.


## Operativer Status

Jedes Profil ist entweder implizit `available` oder ausdrücklich `planned`. Ein
`planned`-Profil ist im Katalog auffindbar, wird aber mit
`profile_executable=false`, `ready_for_laboratory_apply=false` und einem
maschinenlesbaren `profile-planned-not-executable`-Blocker ausgegeben. So kann
Dokumentation kein ausführbares Profil vortäuschen. `production` ist derzeit
solch ein geplantes Profil, bis Ardour-Installationsform, Session-Template,
Plugininventar und Wiederöffnungsbeleg kanonisch festgelegt sind.

## Gerätewahrheit

Der Audio-Doctor trennt aktuelle physische Beobachtung, konfigurierte
Standardendpunkte und aus dem Profilkatalog abgeleitete Sollgeräte. Namen in
PipeWire oder `pactl` gelten nicht als Beleg, dass MOTU oder Roland aktuell
angeschlossen sind. MOTU-Präsenz wird fail-closed aus ALSA-Audio,
Roland-Präsenz aus ALSA-MIDI abgeleitet.
