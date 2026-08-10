# Audiozentrale iPad Recording Actions v1

Status: vom Nutzer am 10. August 2026 nach erfolgreichem lokalem Klavier-und-Gesang-E2E-Take zur Umsetzung freigegeben.

## Ziel

Die private Tailnet-Audiozentrale auf HTTPS 9443 darf vom vorgesehenen iPad neben den bereits freigegebenen Buckelwal-Aktionen genau die typisierten Recorderoperationen `plan`, `start`, `stop` und `recover` ausführen. Der autoritative Audio-Control-Dienst bleibt ausschließlich auf `127.0.0.1:8765`; sein Aktionstoken verlässt den Heim-PC niemals.

## Harte Invarianten

1. Kein Backend-`action_token` wird an das iPad oder über Tailscale ausgeliefert.
2. Remote-Recorderwirkung existiert ausschließlich auf `/bridge/v1/actions/recording`; die Bridge bleibt kein allgemeiner Reverse Proxy.
3. Jede Wirkung verlangt die bestehende kurzlebige, an die verifizierte Tailscale-Identität und den exakten HTTPS-Origin gebundene Bridge-Session.
4. Recorderpayloads werden vor Backendkontakt streng typisiert: nur `voice` oder `piano-vocal`, sichere einzelne `.wav`-Namen, gebundene Plan-SHA-256 und gebundene 24-stellige Session-ID.
5. `stop` und `recover` verlangen remote immer eine explizite Session-ID; es gibt keine implizite Fernwirkung auf „irgendeine aktive Sitzung“.
6. Der lokale Recorder validiert weiterhin alle eigenen Verträge, Hardware-/Labor-Gates, Plan-Hashes, Prozessidentitäten, Dateigrenzen und den autoritativen Readback. Die Bridge lockert keinen dieser Verträge.
7. Buckelwal- und Recorderwirkungen teilen sich einen nichtblockierenden Effekt-Lock. Parallele Wirkung wird mit Konflikt abgewiesen.
8. Profile, Routing, Geräte-, Lautstärke- und Systemaktionen bleiben remote ausgeschlossen.
9. Das Backend bleibt loopback-only; Tailscale Serve veröffentlicht weiterhin nur die Bridge auf HTTPS 9443 und verändert keine fremde Serve-Konfiguration.
10. Antworten werden wie bisher rekursiv von sensitiven JSON-Schlüsseln bereinigt; insbesondere darf kein Backend-Aktionstoken zurückkehren.

## Bedienvertrag

- `Plan prüfen` ist auf dem iPad aktiv, sobald Recorderfähigkeit, Backendzustand und Bridge-Session belegt sind.
- `Aufnahme starten` wird erst nach einem zur aktuellen Eingabe passenden, `ready=true` Recorderplan aktiviert.
- `Stop` wirkt nur auf die im autoritativen Snapshot gebundene aktive Session.
- `Recovery` wirkt nur auf die im Snapshot gebundene Recovery-/Cleanup-Session.
- `Klavier + Gesang` darf vor der Planprüfung darauf hinweisen, dass exakter Roland-Port und `arecordmidi` im Plan gebunden werden; dieser Hinweis darf die Planprüfung nicht sperren.

## Abnahme

- Bridge-Unit- und Sicherheitstests beweisen Positiv- und Negativpfade für Recorderaktionen, Identitäts-/Origin-/Sessionbindung, Payloadgrenzen, Backend-Token-Geheimhaltung und Parallelitätsabweisung.
- UI-Tests beweisen lokale und entfernte Recorderautorität getrennt sowie die korrekte Wahl zwischen `/api/v1/actions/recording` und `/bridge/v1/actions/recording`.
- Vollständiger Repository-Check muss grün sein.
- Nach Merge muss die Deployment-Receipt den exakten Main-Commit melden, Bridge und Audio-Control-Dienst müssen aktiv sein, und die iPad-Oberfläche muss die Recorderbuttons über die private Bridge freischalten.
