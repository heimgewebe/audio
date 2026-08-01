# Audio-Systemwahrheit und Drift

Der Audio-Doctor zeigt eine einzelne read-only Momentaufnahme. `audio-truth`
bindet diese Beobachtung zusätzlich an die kanonischen Signalweg-, Profil-,
Pegel-, Labor- und physischen Faktenverträge sowie an den validierten privaten
Laborzustand.

## Wahrheitsordnung

1. Aktuelle read-only Softwarebeobachtung.
2. Explizite menschliche Beobachtung physischer Kabel, Regler und Schalter.
3. Validierte private Laborbelege aus realen Aufnahmen oder Testläufen.
4. Repositorypläne und Standardwerte.

Ein tieferer Rang darf einen höheren Rang nicht überschreiben. Insbesondere gilt:

- `null` bedeutet unbekannt, nicht ausgeschaltet oder sicher.
- Eine PipeWire-Pufferdauer ist keine Round-Trip-Latenz.
- Keine jüngsten XRun-Zeilen beweisen keinen XRun-freien Betrieb.
- Ein aktiver Dienst beweist weder korrektes Routing noch Profilbereitschaft.
- Ein 48-V-, Gain-, Lautstärke- oder Codecwert darf nicht aus Software geraten
  werden.
- Ein lokaler SHA-256-Report prüft Integrität und innere Konsistenz, aber keine
  Urheberschaft. Für Authentizität muss der Capture-Receipt-Digest außerhalb des
  Reports festgehalten oder signiert werden.

## Momentaufnahme erzeugen

```text
just truth
```

Der Standardpfad ist privat:

```text
~/.local/state/audio/truth/latest.v1.json
```

Das Schreiben erfolgt atomar mit Modus `0600` über no-follow
Verzeichnisdeskriptoren. Der Report enthält:

- SHA-256-Bindungen aller Wahrheitsverträge,
- den normalisierten Doctor-Zustand und den kanonischen Labor-Graphfingerprint,
- nur Digest und Status der privaten physischen Beobachtungen,
- Digest, aufgelöste, invalidierte und offene private Laborgates,
- Dienstzustände und vollständige Pflicht-Limitfelder einschließlich `LimitNOFILE`,
- alle im bounded `ps`-Fenster klassifizierten Prozesse ausschließlich über
  Befehls- und Argument-Digests,
- Anzahl und Digest jüngster XRun-ähnlicher Journalzeilen ohne Rohlogs,
- freien Speicher und zeit-/eintragsbegrenzte Größen der Audio-Zustände,
- ausschließlich Digest, Verfügbarkeit und Zeilenzahl der Kernel-, PipeWire-, WirePlumber- und Mopidy-Versionsausgaben,
- den Status jedes T001-Abnahmegates.

Kommandos werden mit begrenztem Speicher und einer eigenen Prozessgruppe gelesen.
Bei Timeout wird die gesamte Gruppe beendet und nur innerhalb eines festen
Nachlaufbudgets geleert. Der Report akzeptiert ausschließlich den kanonischen
read-only Befehlsvektor. Im Report stehen nur Hashes, Byte- und Zeilenzahlen,
nie stdout, stderr, Prozessnamen, Prozessargumente oder Journalzeilen.

Ein Qobuz-Beleg wird nur dann als aktuell gewertet, wenn beim Capture sowohl der
Fingerprint als auch die Rate des gerade betrachteten Tracks angegeben werden
und mit dem validierten Beleg übereinstimmen. Ohne aktuellen Trackkontext bleibt
`qobuz-rate-proof` offen beziehungsweise invalidiert.

## Report prüfen

```text
just truth-verify ~/.local/state/audio/truth/latest.v1.json
```

Die Prüfung berechnet Vertragsaggregate, Graph-, Prozess- und Wahrheitsfingerprints
neu und deckt auch den Zeitstempel über `report_sha256` ab. Sie besagt nicht,
dass alle Messgates bestanden sind. Gegen gezielte vollständige Neuerzeugung
schützt nur der extern festgehaltene Capture-Digest oder eine Signatur.

## Drift vergleichen

Vor einem Kernel-, PipeWire-, WirePlumber-, Mopidy- oder Plugin-Update wird eine
Momentaufnahme archiviert. Danach wird eine zweite erzeugt:

```text
just truth-drift before.json after.json drift.json
```

Der Driftbericht vergleicht unter anderem:

- Vertragsaggregate,
- physischen und Labor-Zustandsdigest,
- kanonischen Graphfingerprint,
- MOTU- und Roland-Präsenz,
- Standardquelle und Standardsenke,
- Rate und Quantum,
- Dienstzustände, Dienstlimits und gehashte Softwareversionsprojektionen,
- relevante Prozessfingerprints,
- XRun-ähnliche Zeilenanzahl und -digest.

Er nennt unter `required_remeasurements` ausschließlich exakte IDs des
Laborkatalogs, beispielsweise `loopback-latency-measurement`,
`xrun-stability-test` und `qobuz-rate-proof`. Nicht katalogisierte Arbeiten wie
Hörpegelkalibrierung oder Geräteverlustübungen stehen getrennt unter
`required_followups`. Prozessänderungen gelten als materieller Drift. Der
Vergleich ändert niemals Profile oder Dienste.

## Geräteverlust und Wiederkehr

Die Software trennt MOTU und Roland nicht über flüchtige ALSA-Kartennummern,
sondern über USB-Hersteller- und Modellkennung. Beim MOTU bindet sie zusätzlich
die Geräteseriennummer. Das Roland FP-30X stellt keine geräteeindeutige
Serienkennung bereit; seine stärkste beobachtbare Identität ist deshalb
Modell plus USB-Port. Ein identisches Ersatzgerät am selben Port kann damit
nicht erkannt werden und bleibt ausdrücklich außerhalb des Belegs.

Eine Übung verändert selbst nichts. Während des begrenzten Beobachtungsfensters
muss das jeweilige Gerät physisch getrennt und wieder verbunden werden:

```bash
python3 scripts/audio-device-loss observe motu_m2 \
  --loss-timeout-seconds 60 \
  --recovery-timeout-seconds 60 \
  --output /tmp/motu-device-loss.json
python3 scripts/audio-device-loss record motu_m2 \
  /tmp/motu-device-loss.json --replace

python3 scripts/audio-device-loss observe roland_fp_30x \
  --loss-timeout-seconds 60 \
  --recovery-timeout-seconds 60 \
  --output /tmp/roland-device-loss.json
python3 scripts/audio-device-loss record roland_fp_30x \
  /tmp/roland-device-loss.json --replace
```

Ein positiver Receipt verlangt eine eindeutige Ausgangspräsenz, zwei
vollständige Abwesenheitsbeobachtungen, zwei übereinstimmende
Wiederkehrbeobachtungen und eine unveränderte beobachtbare Identität. State und
Belege werden privat mit Modus `0600` gespeichert. `device-loss-baseline` wird
erst dann `pass`, wenn beide aktuellen Receipts gültig sind. Ändert sich der
Beobachter- oder Systemwahrheitscode, werden ältere Receipts invalidiert und
müssen erneut ausgeführt werden. Dasselbe gilt, wenn das aktuell beobachtete
Gerät fehlt, nicht mehr eindeutig ist oder nicht mehr die im Receipt gebundene
Identität besitzt. Beim Roland zählt wegen der fehlenden Serienkennung auch ein
Wechsel des USB-Ports als Identitätsänderung.

## T001-Abschlussgrenze

Die Werkzeuge schließen die maschinelle Wahrheits- und Driftinfrastruktur. Die
folgenden Punkte bleiben blockiert, bis reale Evidenz vorliegt:

- sichere Referenz- und Maximalpegel für Lake People, Focal und Pioneer,
- Rode-NT1-A-Eingang, 48 V, Gain, Rauschabstand und reale Zielspitzen,
- physische Round-Trip-Latenz,
- begrenzter XRun-Stabilitätstest,
- MOTU- und Roland-Verlust-/Wiederkehrtest,
- Qobuz-Track-, Graph- und Endpunktrate samt Resampling,
- validierte Rate-/Resamplingentscheidung und Plugin-Host-Nachweis.

Diese Grenze verhindert, dass technische Bereitschaft mit gemessener
Audioqualität verwechselt wird.

## Ungültiger oder unlesbarer Labor-, Physik- oder Geräteübungszustand

Gespeicherte Labor-, Physik- und Geräteübungsbelege werden an den
Gate-Katalog, den Profilkatalog, die Beobachterimplementierung und die physische
Wahrheit gebunden. Ändert sich eine dieser Bindungen, ist die gespeicherte
Evidenz nicht mehr gültig — das ist gewollt und
erzwingt eine Nachmessung.

Der Bericht bricht in diesem Fall nicht ab. Ungültige Zustände, beispielsweise
Katalogdrift, und technisch unlesbare Zustände erhalten verschiedene stabile
Status- und Fehlercodes. Das gilt auch für Geräteverlust-Receipts. Die
betroffene Projektion wird mit `state_readable: false`,
`state_status: invalid|unreadable`, einem pfadfreien `state_issue_code` und ohne
`state_sha256` ausgewiesen. Abhängige Gates bleiben unaufgelöst oder werden bei
belegter Vertragsinvalidität invalidiert; `single-truth-model` meldet `unknown`
statt `pass`. `audio-truth verify` akzeptiert den fehlenden Digest ausschließlich
mit diesen expliziten Feldern und erzwingt eine leere Projektion. Unbekanntes
bleibt so ausdrücklich unbekannt, statt den gesamten Bericht unverfügbar zu
machen.


### Report-Schema und Verifikationsgrenze

Neue Truth-Reports verwenden `schema_version: 2`, weil `state_sha256` bei einem
nicht auswertbaren privaten Zustand ausdrücklich `null` sein kann. Der aktuelle
Verifier akzeptiert weiterhin unveränderte Schema-v1-Reports, verlangt dort aber
für jede Legacy-Projektion einen gültigen Zustandsdigest. Schema-v2-Projektionen
müssen die Lesbarkeit explizit ausweisen.

`verify` bestätigt strukturelle Integrität, Bindungen und die neu berechnete
Gate-Projektion. Das Verifikationsreceipt weist zusätzlich
`operationally_ready` und alle `nonpassing_gates` aus; ein strukturell gültiger
Report ist damit nicht automatisch eine Freigabe für Messung, Profilanwendung
oder Betrieb.
