# Audiozentrale iPad Remote Bridge v1

## Ziel

Die iPad-/PWA-Oberfläche darf den Heim-PC lesen, ohne den lokalen Audio-Control-Dienst selbst ins Netz zu stellen. Deshalb bleibt `audio_control.py` unverändert auf `127.0.0.1:8765`; davor sitzt ein eigener, eng begrenzter Read-only-Bridge auf `127.0.0.1:8766`. Tailscale Serve darf ausschließlich diesen Bridge über HTTPS-Port `9443` im privaten Tailnet veröffentlichen.

Der bestehende Tailscale-Serve-Eintrag auf HTTPS 443 gehört einer anderen Anwendung und ist ausdrücklich fremdes Eigentum. Der Audiozentrale-Controller darf ihn weder ersetzen noch zurücksetzen noch löschen.

## Sicherheitsgrenzen

`audio_remote_bridge.py` ist kein allgemeiner Reverse Proxy. Backendhost und -port sind Konstanten. Akzeptiert werden nur `GET` und `HEAD`, eine feste statische App-/Lektions-Allowlist sowie die read-only API-Endpunkte Health, Telemetrie, Replay, Buckelwal-Lektion, Snapshot und genau ein typisierter Profilplanpfad. Statische und feste API-Pfade akzeptieren keine Query; der Snapshot akzeptiert nur keine Query oder `refresh=1`. Kodierte Slash-/Backslash-Umgehungen werden fail-closed abgewiesen.

Zum Backend werden ausschließlich ein synthetischer `Host: 127.0.0.1:8765`, `Connection: close` und optional `If-None-Match` gesendet. Andere eingehende Header, Cookies, Autorisierungsdaten und Bodies werden nicht weitergereicht. Von Backendantworten übernimmt der Bridge nur die für Darstellung, Cache und Browser-Sandbox benötigten Header; `Content-Length` wird aus der tatsächlich ausgelieferten Darstellung neu berechnet. Jede Antwort trägt `X-Audio-Remote-Bridge: read-only-v1`.

JSON-Antworten werden vor der Auslieferung vollständig geparst, rekursiv nach lokal-only bzw. sicherheitsrelevanten Schlüsseln gefiltert, erneut geprüft und deterministisch kodiert. Insbesondere darf die lokale Aktionsauthentisierung des Control-Dienstes niemals die Remoteprojektion erreichen. Ungültiges oder zu großes JSON wird nicht transparent weitergereicht.

Der Bridge besitzt keine Audio-, Aufnahme-, Profil-, Geräte- oder Operatorwirkung. Ein Request ist niemals ein Beleg für Geräteanwesenheit oder eine erfolgreiche Audiowirkung.

## Tailscale Serve

`scripts/audio_remote_bridge_tailscale.py` verwaltet ausschließlich HTTPS-Port `9443` mit Ziel `http://127.0.0.1:8766`. Es benutzt nur `tailscale serve`; Funnel, `reset` und `clear` sind außerhalb des Vertrags. Vor einer Änderung wird die komplette Serve-Konfiguration gelesen. Eine Belegung von 9443, die nicht exakt dem Audiozentrale-Vertrag entspricht, blockiert die Änderung.

Nach `apply` wird geprüft, dass 9443 exakt auf den Bridge zeigt und die komplette Serve-Konfiguration nach Entfernung des eigenen 9443-Anteils semantisch identisch zum Vorzustand ist. Bei Abweichung wird ausschließlich 9443 wieder abgeschaltet und der Vorzustand erneut geprüft. `remove` greift nur dann ein, wenn 9443 exakt dem eigenen Vertrag entspricht. Andere Ports und Handler bleiben immer fremdes Eigentum.

## Deployment

`systemd/user/audio-remote-bridge-v1.service` wird releasegebunden installiert, aber nicht automatisch aktiviert oder gestartet. Ebenso verändert das normale Audio-Control-Deployment niemals die Tailscale-Konfiguration. Repositorylieferung und Runtimeaktivierung sind getrennte Stufen.

Die spätere Runtimeabnahme benötigt mindestens:

- gemergten, hashgebunden deployten Release,
- laufenden lokalen Control-Dienst auf `127.0.0.1:8765`,
- laufenden Bridge auf `127.0.0.1:8766`,
- revisionsgebundenen Serve-Readback für HTTPS 9443 bei unverändertem übrigen Serve-Zustand,
- HTTPS-Readback aus dem Tailnet,
- physischen Safari-/PWA-Readback auf dem vorgesehenen iPad.

Bis diese Belege vorliegen, bleiben sämtliche `runtime_acceptance`-Felder in `inventory/audiozentrale-remote-bridge.v1.json` auf `false`.

## Rückbau

Der Rückbau erfolgt in umgekehrter Reihenfolge: den exakt eigenen Serve-9443-Eintrag entfernen, Bridge-Dienst stoppen/deaktivieren, danach bei Bedarf den Repository-Commit revertieren. Der lokale Audio-Control-Dienst auf 8765 und fremde Serve-Konfigurationen werden dabei nicht verändert.
