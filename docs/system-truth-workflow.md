# Audio-Systemwahrheit und Drift

Der Audio-Doctor zeigt eine einzelne read-only Momentaufnahme. `audio-truth`
bindet diese Beobachtung zusätzlich an die kanonischen Signalweg-, Profil-,
Pegel-, Labor- und physischen Faktenverträge.

## Wahrheitsordnung

1. Aktuelle read-only Softwarebeobachtung.
2. Explizite menschliche Beobachtung physischer Kabel, Regler und Schalter.
3. Validierte Laborbelege aus realen Aufnahmen oder Testläufen.
4. Repositorypläne und Standardwerte.

Ein tieferer Rang darf einen höheren Rang nicht überschreiben. Insbesondere gilt:

- `null` bedeutet unbekannt, nicht ausgeschaltet oder sicher.
- Eine PipeWire-Pufferdauer ist keine Round-Trip-Latenz.
- Keine jüngsten XRun-Zeilen beweisen keinen XRun-freien Betrieb.
- Ein aktiver Dienst beweist weder korrektes Routing noch Profilbereitschaft.
- Ein 48-V-, Gain-, Lautstärke- oder Codecwert darf nicht aus Software geraten
  werden.

## Momentaufnahme erzeugen

```text
just truth
```

Der Standardpfad ist privat:

```text
~/.local/state/audio/truth/latest.v1.json
```

Das Schreiben erfolgt atomar mit Modus `0600`. Der Report enthält:

- SHA-256-Bindungen aller Wahrheitsverträge,
- den normalisierten Doctor-Zustand und Graph-Fingerprint,
- nur den Digest und Status der privaten physischen Beobachtungen,
- Dienstzustände und Ressourcenlimits,
- relevante Aufnahme-, Plugin-, Wiedergabe- und Kreativprozesse,
- jüngste XRun-ähnliche Journalzeilen,
- freien Speicher und begrenzte Größen der Audio-Zustandsverzeichnisse,
- Kernel-, PipeWire-, WirePlumber- und Mopidy-Versionen,
- den Status jedes T001-Abnahmegates.

## Report prüfen

```text
just truth-verify ~/.local/state/audio/truth/latest.v1.json
```

Die Prüfung erkennt nachträgliche Änderungen am Report. Sie besagt nicht, dass
alle Messgates bestanden sind.

## Drift vergleichen

Vor einem Kernel-, PipeWire-, WirePlumber-, Mopidy- oder Plugin-Update wird eine
Momentaufnahme archiviert. Danach wird eine zweite erzeugt:

```text
just truth-drift before.json after.json drift.json
```

Der Driftbericht vergleicht unter anderem:

- Vertragsaggregate,
- physischen Zustandsdigest,
- Graph-Fingerprint,
- MOTU- und Roland-Präsenz,
- Standardquelle und Standardsenke,
- Rate und Quantum,
- Dienstzustände,
- Softwareversionen,
- relevante Prozessklassen.

Er nennt erforderliche Nachmessungen. Er ändert niemals Profile oder Dienste.

## T001-Abschlussgrenze

Die Werkzeuge schließen die maschinelle Wahrheits- und Driftinfrastruktur. Die
folgenden Punkte bleiben blockiert, bis reale Evidenz vorliegt:

- sichere Referenz- und Maximalpegel für Lake People, Focal und Pioneer,
- Rode-NT1-A-Eingang, 48 V, Gain, Rauschabstand und reale Zielspitzen,
- physische Round-Trip-Latenz,
- begrenzter XRun-Stabilitätstest,
- MOTU- und Roland-Verlust-/Wiederkehrtest,
- Qobuz-Track-, Graph- und Endpunktrate samt Resampling.

Diese Grenze verhindert, dass technische Bereitschaft mit gemessener
Audioqualität verwechselt wird.
