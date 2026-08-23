# Audiozentrale operating modes v1

Stand: 23. August 2026

## Entscheidung

Die Produktoberfläche führt einen kleinen Betriebsmodusvertrag oberhalb der
bestehenden Audioautoritäten ein. Der Vertrag ist eine Orchestrierungs- und
Wahrheitsschicht, keine neue Audioengine und keine zweite Routingwahrheit.

Ausführbar sind zunächst:

- `desktop-listening`: Desktop, Spotify und Browser über den gemeinsamen
  PipeWire-/MOTU-Pfad. Jede erforderliche Wirkung wird ausschließlich an
  `desktop-mixed-transition-v1` delegiert.
- `qobuz-reference`: der vorhandene QBZD-/QConnect-/ALSA-Direct-Pfad. Die
  Audiozentrale startet, stoppt oder simuliert keinen Qobuz-Player. Sie bindet
  den Sollmodus nur, wenn der aktuelle Doctor-Readback den QBZD-Referenzprovider
  positiv als bereit bestätigt.

`recording` und `performance` sind deklarierte spätere Modi ohne Wirkung. Ihre
bestehenden Recorder-, Profil- und Instrumentautoritäten werden nicht
übernommen.

## Wahrheitsebenen

Der Snapshot projiziert vier orthogonale Ebenen:

- `configured`: expliziter Sollmodus aus einem privaten Zustandsbeleg;
- `observed`: aktuell beobachteter Signalbesitzer und Signalweg;
- `physical`: aktuelle MOTU-Anwesenheit aus dem Audio-Doctor;
- `executable`: ob der jeweilige bestehende Wirkvertrag jetzt sicher aufgerufen
  werden darf.

Diese Ebenen werden nicht ineinander umgedeutet. Insbesondere belegt ein
konfigurierter Qobuz-Modus keine laufende Wiedergabe, eine verbundene
QConnect-Sitzung kein Track-Native und ein PipeWire-Standardziel keine physische
MOTU-Anwesenheit.

Die Produktzustände sind `ready`, `transitioning`, `attention`, `blocked` und
`recovering`. QConnect `retrying` oder `reconnecting` wird sichtbar als
`recovering` projiziert. Ein unlesbarer QBZD-Readback bleibt `attention` oder
`blocked`; er wird niemals als Bereitschaft angenommen.

## Transition

Die lokale API akzeptiert ausschließlich einen expliziten `POST` auf
`/api/v1/actions/operating-mode` mit `request_id` und `target_mode`, lokaler
Origin-Prüfung und dem vorhandenen Aktionstoken. `GET`, `HEAD`, Seitenaufruf und
Refresh lesen nur und schreiben weder den Modusbeleg noch Audiozustand.

Vor jeder Wirkung werden Doctor, Recorder, Buckelwal und Dauersong frisch
gelesen. Aktive oder zu bergende Aufnahmen sowie aktive Wal-/Dauersong-Pfade
blockieren die Hörmodustransition. Laufende, QBZD-eigene MOTU-Wiedergabe
blockiert den Wechsel zu Desktop: Die Audiozentrale stoppt keinen fremden
Player.

Für Desktop wird zunächst der bestehende private, hashgebundene
`desktop-mixed`-Plan erzeugt und anschließend über dessen typed Apply-Vertrag
ausgeführt. Sein kanonisches privates Journal bleibt mit CLI und UI gemeinsam
unter `~/.local/state/audio/profile-transitions-v1/`; der Modusbeleg selbst liegt
weiter im von systemd verwalteten `StateDirectory`. Die statische UI-Unit behält
`ProtectHome=read-only` und erlaubt nur die drei exakten Schreibwurzeln für
Recorder-Ausgabe, Recorder-State und dieses Transition-Journal. Ein
releasegebundener `ExecStartPre` in `audio_control.py` legt diese Pfade vor dem
Sandbox-Start sicher und ohne Audioeffekt an; abweichende Laufzeitpfade werden
fail-closed abgewiesen.

Die Transition-Laufzeitclosure aus Transition, Planner, Doctor, Physical-/Labor-
Vertrag, Profilkatalog und den dafür gelesenen Inventaren ist für neue Releases
hashgebunden. Beim ersten Upgrade bleibt ein alter Releasebeleg lesbar, aber
Desktop-Routing ist so lange blockiert, bis der neue Deployer diese vollständige
Closure revisionsgebunden in den Releasebeleg aufgenommen hat. Kandidaten dürfen
vor Marker-Erzeugung nur im kanonischen privaten Deployment-Validation-Tree
getestet werden; ein aktivierter markerloser Release bleibt blockiert.

Der Betriebsmodus gilt erst als erfolgreich, wenn ein neuer
Audio-Doctor-Readback MOTU als anwesend sowie `motu-m2`, 48 kHz und 1024 Frames
als Desktopzustand bestätigt und kein QBZD-PCM mehr läuft. Für Qobuz ist ein
aktueller, bereiter `qbzd-qconnect`-Provider die Postcondition; Wiedergabe und
Track-Native sind dafür ausdrücklich nicht erforderlich.

## Antwortverlust und Wiederholung

Vor einer möglichen Desktopwirkung wird die Request-ID atomar mit Zustand
`transitioning` persistiert. Fehlt danach die Postcondition, bleibt der Beleg
`recovering`. Eine Wiederholung mit derselben Request-ID führt die Wirkung
nicht erneut aus, sondern liest nur den autoritativen Zustand zurück. Eine neue
Request-ID wird blockiert, bis der unklare Vorgang mit seiner ursprünglichen ID
aufgelöst ist.

Ist die Postcondition trotz verlorener oder fehlerhafter Wirkantwort erreicht,
wird der Vorgang als abgeglichen abgeschlossen. Ob dabei Audio tatsächlich
mutiert wurde, bleibt in diesem Fall `null` statt unbelegt `true` oder `false`
zu behaupten. Ein bereits erfolgreich belegter Request ist idempotent und
verursacht bei Wiederholung keine Wirkung.

## Track-Native-Grenze

`TRACK-NATIVE ✓` darf nur aus dem bestehenden Current-Track-Gate des Doctors
projiziert werden: laufender, QBZD-eigener MOTU-PCM, stabile QBZD-/ALSA-Snapshots,
DirectHardware und exakt passende Rate. Die Modusprojektion verschärft dies
zusätzlich, indem sie Rate und Kennzeichnung nur bei aktuell beobachteter
QBZD-Wiedergabe freigibt. Connected, ready, prepared oder pausiert reichen
nicht.

Pioneer und Bluetooth bleiben read-only. Fehlende physische Fakten werden als
offen gezeigt und nicht aus Softwarebeobachtung abgeleitet.
