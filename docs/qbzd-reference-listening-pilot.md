# QBZD reference listening pilot

Stand: 21. August 2026

## Ziel

Der Referenzpfad für Qobuz umgeht den gemischten Browser-/PipeWire-Pfad und gibt direkt über QBZD, ALSA und den MOTU M2 aus. Die Audiozentrale bleibt Kontroll- und Diagnoseebene; sie implementiert keinen eigenen Qobuz-Client.

```text
Qobuz
  ↓ Qobuz Connect
QBZD 2.0.2
  ↓ ALSA DirectHardware
MOTU M2
  ├─ Lake People G111 Mk II → Focal Clear Mg
  └─ Pioneer VSX-830-K → Lautsprecher
```

Die lokale QBZD-Control-API ist ausschließlich an `127.0.0.1:8182` gebunden.

## Reale Ratenprüfung

Am Heim-PC wurden vier Quellraten mit laufender Wiedergabe gegen den tatsächlich geöffneten MOTU-ALSA-PCM geprüft:

| Quelle | QBZD | MOTU ALSA | Modus | Ergebnis |
| ---: | ---: | ---: | --- | --- |
| 16 Bit / 44,1 kHz | 44,1 kHz | 44,1 kHz | DirectHardware | PASS |
| 24 Bit / 48 kHz | 48 kHz | 48 kHz | DirectHardware | PASS |
| 24 Bit / 96 kHz | 96 kHz | 96 kHz | DirectHardware | PASS |
| 24 Bit / 192 kHz | 192 kHz | 192 kHz | DirectHardware | PASS |

Damit ist für diesen Pfad das automatische native Umschalten zwischen 44,1, 48, 96 und 192 kHz hardwareseitig belegt. Der beobachtete ALSA-PCM-Container war `S32_LE`; daraus folgt **nicht**, dass die native Hardware-Bittiefe 32 Bit beträgt oder dass jede künftige Quelle bitidentisch ausgegeben wird.

## Wahrheitshierarchie

Für den aktuellen Hardwarezustand gilt:

1. `/proc/asound/card*/pcm0p/sub0/hw_params` und `status` des über `card*/id == M2` identifizierten MOTU sind die unmittelbare Hardwarewahrheit.
2. Ein laufender PCM wird nur QBZD zugerechnet, wenn ALSA `owner_pid` meldet und dessen Prozessklasse lokal als `qbzd` aufgelöst wird. PID und fremde Prozessnamen werden nicht in den Doctor-Report übernommen.
3. QBZD `bit_perfect=DirectHardware` und die von QBZD gemeldete Rate sind notwendige, aber allein nicht hinreichende Belege.
4. QBZD-Felder wie `device_open`, `sample_rate`, `playback_state` oder `qconnect.enabled` können nach Zustandswechseln kurz veraltet sein und sind deshalb diagnostisch, nicht allein autoritativ.
5. Für Qobuz Connect gelten `state=connected` plus `session_active=true` als Laufzeitbeleg; ein veraltetes `enabled=false` überschreibt diese Sessionwahrheit nicht.

## Aktuelles `TRACK-NATIVE`-Gate

Die Oberfläche darf `TRACK-NATIVE ✓` nur anzeigen, wenn gleichzeitig gilt:

- QBZD-Referenzprovider ist bereit,
- QBZD meldet `DirectHardware`,
- der MOTU-Wiedergabe-PCM ist geöffnet,
- `hw_params` ist vor und nach dem zugehörigen ALSA-Statusread identisch,
- ALSA meldet `state: RUNNING`,
- der ALSA-Owner wird als `qbzd` klassifiziert,
- QBZD-Rate und MOTU-Hardwarerate sind exakt gleich.

`SETUP` und `PREPARED` bedeuten nur, dass der Pfad aufgebaut wird. Bei geschlossenem oder nicht laufendem PCM, instabilem Hardware-Snapshot, fremdem oder unbekanntem Owner sowie Ratenabweichung bleibt der Nachweis offen. QBZDs eigener `playback_state` wird wegen beobachteter Stale-Zustände nicht als Gate verwendet.

## Abgrenzung

Der Browser-/Desktop-Pfad und der bestehende Mopidy-Pfad bleiben Komfort- bzw. Legacy-Pfade über den gemischten Audiographen. Für sie wird kein Track-Native- oder Bitperfekt-Anspruch abgeleitet. Der QBZD-Referenzpfad fügt selbst kein EQ, Loudness-Normalizing oder bewusstes Resampling hinzu. DSP bleibt eine spätere, getrennt zu bewertende Option.

Die maschinenlesbare Prüfmatrix liegt in `inventory/qbzd-reference-rate-matrix.v1.json`. Sie enthält keine Qobuz-Accountdaten, Titel, Interpreten, Track-IDs oder Prozess-IDs.
