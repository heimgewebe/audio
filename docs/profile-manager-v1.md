# Audio-Profilmanager v1

## Zweck

Der Profilmanager bildet die kanonischen Audio-Profile als vollständige,
typisierte Übergangsverträge ab. Er ergänzt den bestehenden Live-Doctor und den
bereits gehärteten `desktop-mixed`-Übergang, übernimmt aber **keine** produktive
Audioautorität.

Die erste Version ist absichtlich auf Repository- und Simulationswirkung
begrenzt. Sie kann weder PipeWire noch ALSA, MIDI, Dienste, Prozesse oder Geräte
auf dem Heim-PC verändern.

## Eine Profilwahrheit

`profiles/audio-profiles.v1.json` bleibt der kanonische Produktkatalog. Der
technische Übergangskatalog `profiles/audio-profile-contracts.v1.json` ist an
Pfad und SHA-256 dieses Katalogs gebunden. Er darf weder zusätzliche Profile
noch Aliase enthalten. Jede Abweichung blockiert `doctor`, `plan`, `diff` und
den simulierten Apply-Pfad.

Jedes Profil besitzt explizite Verträge für:

- Geräte, Quelle, Ziel und MIDI-Rolle,
- Monitoring, Rate, Quantum und Resampling,
- DSP, Kanäle und Grenzwerte,
- ausschließlich manager-eigene Start-/Stoppobjekte und Routen,
- Readback- und Rollbackfelder,
- Aufnahmeprotektion.

Das Schema liegt unter `schemas/audio-profile-catalog.v1.schema.json`.

## Reine Beobachtung

`scripts/profile_manager.py doctor`, `plan` und `diff` arbeiten ausschließlich
auf einem übergebenen JSON-Snapshot. Sie schreiben keine Datei und rufen keine
Audio- oder Prozesskommandos auf.

Ein Plan bindet:

- den exakten normalisierten Ausgangszustand,
- beide Kataloghashes,
- Quell- und Zielprofil,
- geordnete manager-eigene Operationen,
- die vollständige inverse Rollbackfolge,
- Blocker und Wirkungsausschlüsse,
- seinen eigenen SHA-256.

Unbekannte Profilnamen werden abgewiesen. Es gibt keinen zweiten Alias- oder
Fallbackpfad.

## Simulierter Apply

`apply-simulated` ist die einzige mutierende Oberfläche. Sie akzeptiert nur:

1. einen intern validen Plan,
2. dessen exakten, vom Aufrufer erneut übergebenen SHA-256,
3. einen unveränderten Ausgangssnapshot,
4. einen blockierungsfreien Übergang.

Die Wirkung beschränkt sich auf die angegebene Simulationsdatei. Der neue
Zustand wird in eine private temporäre Datei geschrieben, geflusht, atomar per
`replace` eingesetzt und anschließend vollständig zurückgelesen. Das Ergebnis
ist ein hashgebundener Beleg mit Vorzustand, Nachzustand, Planhash, Zahl der
Operationen und Readback.

Ein zweiter Aufruf desselben Plans gegen den bereits erreichten Zielzustand ist
idempotent und führt keine weitere Operation aus.

### Abbruch vor Apply

Ein noch nicht angewendeter Plan wird durch Verwerfen abgebrochen. Das ist
bewusst keine eigene mutierende Operation: `doctor`, `plan` und `diff` sind
rein, und ohne Aufruf von `apply-simulated` bleibt selbst die angegebene
Simulationsdatei bytegenau unverändert. Die Offline-Tests prüfen diesen
Abbruchpfad für jedes gerichtete Profilpaar. Nach einem Apply ist dagegen der
hashgebundene Rollback der einzige Rückweg; ein bloßes Verwerfen des alten
Plans behauptet keine Rücknahme.

## Rollback

`rollback-simulated` verlangt den exakten Apply-Beleg und dessen SHA-256. Es
verweigert den Rückbau, sobald der Nachzustand seit dem Apply abgewichen ist.
Bei unverändertem Zustand stellt es den vollständigen gebundenen Vorzustand
atomar wieder her und liest ihn erneut zurück.

## Aufnahme- und Fremdschutz

Jeder materielle Profilwechsel wird blockiert, wenn eine Aufnahme `active` oder
der Aufnahmezustand `unknown` ist. Ein bekannter inaktiver Zustand ist
Voraussetzung für simulierte Übergänge.

Operationen dürfen nur manager-eigene Felder, Dienste und Routen verändern.
Fremde Prozesse und fremde Routen sind Bestandteil des gebundenen Snapshots,
werden nicht in Operationen aufgenommen und bleiben bei Apply und Rollback
unverändert. Ein globaler PipeWire-/Audio-Stopp ist weder modelliert noch
zugelassen.

## Testvertrag

`tests/test_profile_manager.py` prüft alle gerichteten Profilpaare. Bei zehn
Profilen sind das 100 Übergänge einschließlich der zehn Identitätsübergänge.
Für jedes Paar werden Determinismus, vollständige Inversen, exakte
Hashbindung, atomarer Apply, Readback, Idempotenz, Rollback und Erhalt fremder
Zustände geprüft.

Zusätzliche Negativtests decken aktive und unbekannte Aufnahmen, geänderte
Ausgangszustände, falsche Planhashes, manipulierte Belege, Rollbackdrift,
Katalogdrift und unerlaubte Profilaliase ab.

## Abgrenzung zur produktiven Laufzeit

Dieser Vertrag belegt keine physische Gerätebereitschaft, keinen sicheren
Abhörpegel, keine niedrige Latenz, keine XRun-Freiheit und keine produktive
Profilumschaltung. Der bestehende `desktop-mixed`-Livepfad bleibt separat und
wird durch diese Aufgabe weder erweitert noch aufgerufen. Weitere produktive
Adapter benötigen einen eigenen revisionsgebundenen Task, frische Hardware- und
Messbelege sowie einen expliziten Wirkungsgate.
