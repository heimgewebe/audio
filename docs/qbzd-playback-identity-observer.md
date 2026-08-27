# QBZD playback identity observer

Stand: 27. August 2026

## Zweck

`scripts/qbzd_playback_identity_observer.py` beobachtet ausschließlich die
lokale QBZD-Queue-/Playback-Identität. Es beantwortet eine engere Frage als der
bestehende Hardware-/Raten-Nachweis:

> Zeigen QBZDs Queue, `/api/now-playing`-Track und der Player-Track in den
> genommenen Samples auf denselben Track?

Der Observer steuert weder Qobuz noch QBZD, verändert keine Queue und startet
keinen Dienst neu.

## Wahrheit und verbleibende Race-Grenze

Der Observer liest fest:

1. `GET http://127.0.0.1:8182/api/queue?offset=0&limit=64`
2. `GET http://127.0.0.1:8182/api/now-playing`
3. denselben Queue-Endpunkt erneut

Unterscheiden sich die beiden Queue-Samples, wird keine Identitätsaussage
gemacht. Sind sie gleich, kann der Observer häufige einfache Rennen ausschließen
und Queue gegen Now-Playing vergleichen.

**Gleiche Samples sind jedoch kein stabiler Snapshot-Beweis.** QBZD stellt an
diesen Endpunkten keine monotone Queue-Revision oder Generation bereit. Eine
ABA-Sequenz `A → B → A` zwischen den Reads kann deshalb wieder dasselbe Sample
ergeben. Positive Ergebnisse heißen aus diesem Grund bewusst nur
`sampled-match` bzw. `sampled-idle`; `authoritative_identity_proof` bleibt immer
`false`.

Der Netzpfad ist auf die beiden festen Loopback-URLs beschränkt. Pro Antwort
gelten ein 128-KiB-Limit, striktes UTF-8/JSON, ein kurzer Timeout, keine
Proxy-Nutzung und keine Redirects. Nicht-JSON-Konstanten wie `NaN` oder
`Infinity`, übergroße JSON-Integer und sonstige Decoderfehler werden fail-closed
als `unavailable` behandelt.

## Zustände

- `sampled-match`: beide Queue-Samples sind gleich und Queue-Current-Track,
  Now-Playing-Track sowie Player-Track stimmen in den genommenen Samples überein.
  Das ist diagnostische Evidenz, kein atomarer Identitätsbeweis.
- `sampled-idle`: beide Queue-Samples sind gleich und in den genommenen Samples
  ist kein Current-Track geladen. Ebenfalls nicht autoritativ.
- `sampled-mismatch` / `queue-playback-mismatch`: Queue-Current-Track und lokaler
  Now-Playing-Track widersprechen sich im Beobachtungsfenster.
- `sampled-mismatch` / `now-playing-internal-mismatch`: QBZDs eigener `track`
  und `playback.track_id` widersprechen sich.
- `sample-window-changed`: die beiden Queue-Samples unterscheiden sich; das
  Fenster war sichtbar in Bewegung und bleibt ohne Identitätsurteil.
- `unavailable`: Antwort, Form, Typ, Größe oder Transport konnten nicht sicher
  validiert werden.

`sampled_identity_match` beschreibt ausschließlich den Vergleich in den
genommenen Samples. `authoritative_identity_proof=false` ist eine feste
Vertragsaussage, solange die API keine ausreichend starke Generation oder
vergleichbare Autorität bietet.

## Datenschutzgrenze

Track-IDs werden ausschließlich im Prozess zum Vergleichen verwendet. Der
Report enthält weder Track-ID noch Titel, Künstler, Album, Artwork oder
Accountdaten und persistiert selbst nichts. Ausgegeben werden nur Zustand,
Queue-Index/-Länge und nicht-identifizierende Diagnoseflags.

## Abgrenzung zu `TRACK-NATIVE`

Der bestehende Hardware-/Ratenbeleg beweist, dass QBZD den laufenden MOTU-PCM
besitzt und QBZD-Rate und MOTU-Hardwarerate übereinstimmen. Das ist keine
Track-Identitätsaussage.

Dieser Observer liefert die fehlende **diagnostische** Queue↔Playback-Sicht,
aber noch keine hinreichende Autorität, um `track_native_proven` an ein positives
Ergebnis zu binden. Ein späterer echter Identitätsbeleg benötigt mindestens eine
monotone Queue-/Playback-Generation oder eine anderweitig validierte, lückenlos
gebundene Ereignisfolge, die ABA im Beobachtungsfenster ausschließt.

Er beobachtet außerdem **nicht** die Anzeige in einer Qobuz-App.
Qobuz-Controllerzustand außerhalb QBZD bleibt eine eigene Wahrheitsdomäne.

## QBZ #699

Der offene upstream QBZ-Bug #699 für `qbzd 2.0.2 + Qobuz Connect + ALSA Direct`
beschreibt einen zeitlichen Fehler: Nach einem fehlgeschlagenen gapless Übergang
kann derselbe Track erneut bei 0 beginnen, während der Queue-Cursor nicht zum
erwarteten Nachfolger weiterrückt. In einem einzelnen Sample können Queue und
Playback dabei trotzdem denselben Track melden.

Deshalb behauptet dieser Observer bewusst **nicht**, aus einem Snapshot oder aus
zwei gleichen unversionierten Queue-Samples einen erwarteten Fortschritt ableiten
zu können. Eine Erkennung der #699-Signatur benötigt separat gebundene
Transitionsevidenz über Zeit; sie darf nicht durch Raten-, Titel- oder
UI-Heuristiken ersetzt werden.
