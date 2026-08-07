# Medienprovider- und Playlistvertrag v1

## Zweck

T005 ergänzt den Audio-Kern um providerneutrale Wiedergabe- und
Playlistverträge. Qobuz bleibt gemäß T002 ausschließlich über Mopidy als
`provider-and-comfort-adapter` gebunden; weder Mopidy noch Qobuz wird zum
allgemeinen Audiokern.

Die erste Version besitzt **keine produktive Providerwirkung**. Sie verändert
kein Konto, keine Playlist, keine Wiedergaberoute und startet keine Wiedergabe.
Der einzige mitgelieferte schreibfähige Provider ist ein deterministischer
In-Memory-Simulator für Offline-Tests.

Der maschinenlesbare Katalog liegt unter
`profiles/media-providers.v1.json`; sein Schema unter
`schemas/media-provider-catalog.v1.schema.json`.

## T002-Bindung

Der Providerkatalog bildet die akzeptierten Entscheidungen aus
`inventory/audio-architecture-decisions.v1.json` ab:

- Mopidy ist Qobuz-Provider- und Komfortadapter,
- der allgemeine Audiokern bleibt providerneutral,
- der bestehende Fallback läuft über PipeWire-Pulse,
- Exklusiv- oder Bitperfect-Wiedergabe wird nicht behauptet,
- `qobuz-exclusive` benötigt weiterhin einen aktuellen `qobuz-rate-proof`.

Providerdetails stehen nur im Katalog beziehungsweise hinter dem schmalen
`PlaylistPort`. `scripts/media_provider.py` implementiert weder Mopidy- noch
Qobuz-Transport, Netzwerkzugriff, frei parametrisierbare RPC-Aufrufe oder einen
allgemeinen Kommandoendpunkt.

## Track- und Formatbeleg

Ein `track_format_proof` bindet die normalisierte Titelidentität samt SHA-256 an:

- Container und Codec,
- Track-, Graph- und Endpunktrate,
- Resamplingklassifikation,
- den Zustand paralleler Mischpfade.

`resampling=none` ist nur zulässig, wenn Track-, Graph- und Endpunktrate exakt
gleich sind. Ein abweichender oder nachträglich manipulierter Beleg wird
abgewiesen. Der Vertrag belegt dadurch technische Beobachtungen, behauptet aber
keine Exklusivität oder Bitperfect-Wiedergabe ohne die separaten Labor-Gates.

## Providerneutraler Import

`normalize_import` akzeptiert drei Eingabeformen:

- Text mit einer Providerreferenz pro Zeile,
- JSON-Liste beziehungsweise `{ "tracks": [...] }`,
- bereits typisierte Providerreferenzen.

Die kanonische Referenzform ist `provider:track:item-id`. Der Import unterstützt
`add` und `replace`, Dry-Run, Duplikaterkennung, bereits vorhandene Einträge und
eine strukturierte Fehlerliste. Fehlerhafte Einträge bleiben sichtbar; ein
Manifest mit Fehlern darf keinen Schreibplan erzeugen.

## Playlistidentität und Schreibplan

Vor jedem Plan wird die Playlist vollständig über den injizierten Provider-Port
exportiert. Der Plan bindet:

- Provider,
- Konto,
- Playlist-ID,
- exakte Providerrevision,
- vollständiges Preimage-Exportmanifest,
- Preimage- und Import-SHA-256,
- SHA-256 des kanonischen Providerkatalogs,
- gewünschte vollständige Trackliste,
- Inhaltsdigest,
- Add-/Replace-Semantik,
- Dry-Run-Zustand,
- den eigenen Plan-SHA-256.

Ein veraltetes Importmanifest, eine fremde Providerreferenz oder eine seit dem
Plan geänderte Playlist blockiert vor dem Schreiben.

## Apply und Readback

`apply_write_plan` benötigt den exakten erneut übergebenen, reviewgebundenen
Plan-SHA-256. Ein Dry-Run-Plan kann nicht angewendet werden.

Bei einem zulässigen Apply wird nur die schmale Methode `replace_playlist` des
injizierten `PlaylistPort` verwendet. Danach wird die Playlist vollständig neu
exportiert und gegen die gewünschte Trackliste und deren Inhaltsdigest geprüft.
Der Beleg enthält Ziel, Planbindung, Vorzustand, Nachzustand, Readbackdigest und
die Zahl der tatsächlich ausgeführten Schreiboperationen.

Ein erneuter Apply gegen den bereits erreichten Inhalt ist idempotent und führt
keinen weiteren Write aus.

T005 liefert absichtlich **keine** produktive Implementierung dieses Ports. Die
Offline-Tests verwenden ausschließlich `SimulatedPlaylistProvider`.

## Vollständiger Inhaltsrollback

Rollback bindet den exakten Schreibplan und den exakten Write-Receipt. Vor dem
Rückbau werden zusätzlich Ziel, Operation, vollständiges Preimage und der
gebundene Postzustand semantisch gegengeprüft; ein lediglich neu gehashter,
inhaltlich umgebogener Receipt ist keine Rollback-Autorität.

Wenn die Playlist seit dem Write abgewichen ist, wird Rollback verweigert. Ein
Write-Receipt mit `operations_applied=0` besitzt keine Rückbauautorität für einen
extern erreichten Wunschzustand; er kann nur bestätigen, dass das Preimage
bereits wieder vorliegt. Bei unverändertem, tatsächlich durch den Plan erzeugtem
Postzustand wird die vollständige Preimage-Trackliste wiederhergestellt und
vollständig zurückgelesen. Ein zweiter Rollback ist idempotent.

## Sicherheitsgrenzen

T005 erzeugt:

- keine Raw-RPC- oder freie Skriptfassade,
- keinen Netzwerk- oder Providertransport im Audio-Kern,
- keine Kontomutation,
- keine produktive Playlistmutation,
- keine Wiedergabe- oder Routingwirkung,
- keine Bitperfect- oder Exklusivitätsbehauptung.

Eine spätere echte Qobuz-/Mopidy-Anbindung muss den `PlaylistPort` hinter einem
eigenen revisionsgebundenen Live-Gate implementieren und weiterhin die T002-
und Laborverträge erfüllen.

## Testvertrag

`tests/test_media_provider.py` prüft unter anderem:

- exakte T002-Konformität und fehlende Providerdetails im Kern,
- Track-/Formatbelege und Manipulationserkennung,
- Text-, JSON- und Providerreferenz-Importe,
- Dry-Run, Add, Replace, Duplikate und Fehlerlisten,
- Provider-, Konto-, Playlist-, Revisions- und Preimage-Bindung,
- exakten reviewgebundenen Planhash,
- vollständigen Readback und Inhaltsdigest,
- idempotenten zweiten Apply,
- vollständigen Inhaltsrollback und Rollbackdrift,
- neu gehashte, aber semantisch gefälschte Receipts,
- die vollständige Abwesenheit produktiver Providerwirkung in T005.

Reale Qobuz-Verfügbarkeit, reale Playlistwirkung und reale Wiedergabequalität
werden durch diesen Repositorytask ausdrücklich nicht belegt.
