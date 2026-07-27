# Heim-PC Audio-Baseline – 2026-07-27

- Status: read-only erfasst
- Schema: 1
- Privater Beleg-SHA-256: `91959b9fc4871ae46aa9b398e7d2b193fdb50a9077afc6f3c418489993920420`

## Hardwareerkennung

| Gerät | Digital erkannt |
|---|---|
| MOTU M2 | true |
| Roland FP-30X | false |

Physisch nicht softwareseitig prüfbar bleiben Rode NT1-A, Lake People G111 Mk 2,
Focal Clear MG, Pioneer VSX-830-K und 1MII B03 Pro samt Verkabelung und
Schalterstellungen.

## PipeWire

- Aktive Standardausgabe: `motu-m2`
- Aktive Standardaufnahme: `motu-m2`
- Gespeicherte Standardausgabe: `motu-m2`
- Gespeicherte Standardaufnahme: `roland-fp-30x`
- Erzwungene Rate: `48000`
- Erzwungenes Quantum: `1024`
- Qobuz/Mopidy-Ausgabe: `pulse-mixed`

## Dienste

| Dienst | Zustand |
|---|---|
| `pipewire` | active |
| `pipewire-pulse` | active |
| `wireplumber` | active |
| `mopidy` | active |
| `easyeffects` | inactive |

## Befunde

- **medium**: WirePlumber erwartet das Roland als Standardaufnahmequelle, das Gerät ist aktuell aber abwesend.
- **medium**: Mopidy/Qobuz nutzt den gemischten Pulse-Pfad; Bitgenauigkeit ist damit nicht belegt.
- **info**: Das aktuelle globale Quantum 1024 priorisiert Robustheit, nicht niedrige Live-Latenz.
- **high**: Roland FP-30X war bei der Baseline nicht digital erkennbar.

## Offene Messungen vor einer Neukonfiguration

- physischer Signalweg und Gain-Stellungen
- tatsächlicher 1MII-Bluetooth-Codec
- Round-Trip-Latenz und XRuns je Profil
- reproduzierbarer MOTU-USB-Abbruchtest
- bitgenauer Qobuz-Nachweis
- kanonische Ardour- und Plugin-Auswahl

Diese Baseline autorisiert und vollzieht **keine** produktive Audioänderung.
