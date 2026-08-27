# QBZD playback identity observer

Stand: 27. August 2026

## Zweck

`scripts/qbzd_playback_identity_observer.py` beobachtet ausschließlich die
lokale QBZD-Queue-/Playback-Identität. Es beantwortet eine engere Frage als der
bestehende native-rate-Nachweis:

> Zeigen QBZDs Queue, `/api/now-playing`-Track und der Player-Track im selben
> stabilen lokalen Snapshot auf denselben Track?

Der Observer steuert weder Qobuz noch QBZD, verändert keine Queue und startet
keinen Dienst neu.

## Wahrheit und Race-Gate

Ein Einzelread ist wegen parallel möglicher QConnect-, Queue- und
Playback-Änderungen nicht hinreichend. Der Observer liest daher fest:

1. `GET http://127.0.0.1:8182/api/queue?offset=0&limit=64`
2. `GET http://127.0.0.1:8182/api/now-playing`
3. denselben Queue-Endpunkt erneut

Nur wenn beide Queue-Snapshots in den identitätsrelevanten Feldern identisch
sind, wird Queue gegen Now-Playing verglichen. Ändert sich die Queue dazwischen,
ist das Ergebnis `snapshot-raced`; es wird keine Konsistenz behauptet.

Der Netzpfad ist auf die beiden festen Loopback-URLs beschränkt. Pro Antwort
gelten ein 128-KiB-Limit, striktes UTF-8/JSON, ein kurzer Timeout, keine
Proxy-Nutzung und keine Redirects.

## Zustände

- `consistent`: stabiler Snapshot; Queue-Current-Track, Now-Playing-Track und
  Player-Track stimmen überein.
- `idle`: stabiler Snapshot ohne geladenen Current-Track.
- `mismatch` / `queue-playback-mismatch`: Queue-Current-Track und lokaler
  Now-Playing-Track stimmen nicht überein.
- `mismatch` / `now-playing-internal-mismatch`: QBZDs eigener `track` und
  `playback.track_id` widersprechen sich.
- `snapshot-raced`: die Queue änderte sich um den Now-Playing-Read herum.
- `unavailable`: Antwort, Form, Typ, Größe oder Transport konnten nicht sicher
  validiert werden.

## Datenschutzgrenze

Track-IDs werden ausschließlich im Prozess zum Vergleichen verwendet. Der
Report enthält weder Track-ID noch Titel, Künstler, Album, Artwork oder
Accountdaten und persistiert selbst nichts. Ausgegeben werden nur der
Konsistenzzustand, Queue-Index/-Länge und nicht-identifizierende Beweisflags.

## Abgrenzung zu `TRACK-NATIVE`

Der bestehende Hardware-/Ratenbeleg beweist, dass QBZD den laufenden MOTU-PCM
besitzt und QBZD-Rate und MOTU-Hardwarerate übereinstimmen. Das ist keine
Track-Identitätsaussage. Dieser Observer schafft die fehlende lokale
Queue↔Playback-Wahrheit, damit ein späterer Integrationsschritt den
`track_native_proven`-Anspruch zusätzlich an `identity_match=true` binden kann.

Er beobachtet **nicht** die Anzeige in einer Qobuz-App. Qobuz-Controllerzustand
außerhalb QBZD bleibt eine eigene Wahrheitsdomäne.

## QBZ #699

Der offene upstream QBZ-Bug #699 für `qbzd 2.0.2 + Qobuz Connect + ALSA Direct`
beschreibt einen anderen, zeitlichen Fehler: Nach einem fehlgeschlagenen
gapless Übergang kann derselbe Track erneut bei 0 beginnen, während der
Queue-Cursor nicht zum erwarteten Nachfolger weiterrückt. In einem einzelnen
Snapshot können Queue und Playback dabei trotzdem konsistent denselben Track
melden.

Deshalb behauptet dieser Observer bewusst **nicht**, aus einem Snapshot einen
erwarteten Queue-Fortschritt ableiten zu können. Eine Erkennung der #699-Signatur
benötigt separat gebundene Transitionsevidenz über Zeit; sie darf nicht durch
Raten-, Titel- oder UI-Heuristiken ersetzt werden.
