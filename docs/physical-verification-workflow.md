# Physische Audioverifikation

Der Heim-PC kann analoge Kabel, 48 V und Reglerstellungen nicht zuverlässig
rücklesen. Diese Fakten werden deshalb ausschließlich als explizite menschliche
Beobachtungen in einer privaten Datei unter
`~/.local/state/audio/physical/latest.v1.json` gespeichert.

`audio-physical` akzeptiert nur katalogisierte Werte und eine zulässige
Evidenzart. Die Datei wird atomar mit Modus 0600 geschrieben und an die Hashes
des Faktenkatalogs sowie der unverifizierten Vorlage gebunden.

Beispiele:

```bash
./scripts/audio-physical init
./scripts/audio-physical status
./scripts/audio-physical record rode_nt1a_connected true --evidence visual
./scripts/audio-physical record rode_nt1a_motu_input input-1 --evidence visual
./scripts/audio-physical record motu_phantom_48v on --evidence visual
```

Eine Beobachtung ist kein automatischer Apply-Befehl und kein Beleg für sichere
Hörlautstärke oder gemessene Latenz.

Bestehende Fakten werden nicht still überschrieben. Eine Korrektur erfordert `--replace`; ein unbekannter Schlüssel und eine Zustandsdatei mit falschen Rechten, fremden Fakten oder ungültiger Evidenz werden fail-closed abgewiesen.
