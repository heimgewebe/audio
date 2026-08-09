# Audiozentrale iPad Remote Bridge v1

## Ziel

Die iPad-/PWA-Oberfläche darf den Heim-PC über den privaten Tailnet-Zugang lesen und genau die Buckelwal-Stimme steuern, ohne den lokalen Audio-Control-Dienst selbst ins Netz zu stellen. `audio_control.py` bleibt unverändert auf `127.0.0.1:8765`; davor sitzt der eng begrenzte Bridge auf `127.0.0.1:8766`. Tailscale Serve veröffentlicht ausschließlich diesen Bridge über HTTPS-Port `9443` im privaten Tailnet.

Der bestehende Tailscale-Serve-Eintrag auf HTTPS 443 gehört einer anderen Anwendung und ist ausdrücklich fremdes Eigentum. Der Audiozentrale-Controller darf ihn weder ersetzen noch zurücksetzen noch löschen.

## Sicherheitsgrenzen

`audio_remote_bridge.py` ist kein allgemeiner Reverse Proxy. Backendhost und -port sind Konstanten. Die normale Projektion bleibt auf `GET`/`HEAD`, eine feste statische App-/Lektions-Allowlist sowie die read-only API-Endpunkte Health, Telemetrie, Replay, Buckelwal-Lektion, Snapshot und genau einen typisierten Profilplanpfad beschränkt. Statische und feste API-Pfade akzeptieren keine Query; der Snapshot akzeptiert nur keine Query oder `refresh=1`. Kodierte Slash-/Backslash-Umgehungen werden fail-closed abgewiesen.

Zusätzlich existiert genau ein wirkender Fernpfad: `POST /bridge/v1/actions/whale`. Er akzeptiert ausschließlich die Operationen `start`, `mode` und `stop`; `start` und `mode` sind auf `morph`, `organic`, `realistic` und `ufo` begrenzt. Recorder, Profile, Routing, Geräte-, Lautstärke- und Systemaktionen bleiben remote gesperrt.

Der Fernpfad ist an den exakten HTTPS-Host `heim-pc.tail6dbb90.ts.net:9443`, eine passende Same-Origin-`Origin`, eine von Tailscale Serve verifizierte `Tailscale-User-Login`-Identität und einen kurzlebigen Bridge-Sessionnachweis gebunden. Auch die Ausgabe dieses Sessionnachweises erfolgt ausschließlich per Same-Origin-`POST /bridge/v1/session` mit JSON; ein `GET` erzeugt keinen Capability-Zustand. Tailscale Serve entfernt eingehend gefälschte Identitätsheader und setzt sie für Tailnet-Traffic selbst; der Bridge läuft deshalb weiterhin ausschließlich auf Loopback. Der Bridge speichert vom Sessionnachweis nur SHA-256 und Ablaufzeit und bindet ihn an den Hash der Tailscale-Identität.

Der lokale Backend-`action_token` verlässt den Heim-PC niemals. Für jede Walwirkung liest der Bridge zuerst einen frischen lokalen Snapshot, prüft `whale_control` und den Walstatus, übernimmt den lokalen Aktionstoken nur intern und sendet die streng typisierte Aktion an `127.0.0.1:8765`. Die Antwort wird vor der Auslieferung rekursiv geschrubbt. Erfolg gilt nur mit autoritativem `audio_control_action_result` samt Snapshot-Readback.

Zum Backend werden bei normalen Leseanfragen ausschließlich ein synthetischer `Host: 127.0.0.1:8765`, `Connection: close` und optional `If-None-Match` beziehungsweise `Range` gesendet. Andere eingehende Header, Cookies, Autorisierungsdaten und Bodies werden nicht weitergereicht. Jede normale Projektionsantwort trägt `X-Audio-Remote-Bridge: read-only-v1`; erfolgreiche Walaktionen tragen `X-Audio-Remote-Bridge: whale-action-v1` und `X-Audio-Remote-Effects: whale-v1`.

JSON-Antworten der lokalen API werden vollständig geparst, rekursiv nach lokal-only beziehungsweise sicherheitsrelevanten Schlüsseln gefiltert, erneut geprüft und deterministisch kodiert. Ungültiges oder zu großes JSON wird nicht transparent weitergereicht.

Ein Request allein ist weiterhin kein Beleg für Geräteanwesenheit oder eine erfolgreiche Audiowirkung. Nur der autoritative Backend-Readback nach der typisierten Walaktion belegt die konkrete Wirkung.

## Tailscale Serve

`scripts/audio_remote_bridge_tailscale.py` verwaltet ausschließlich HTTPS-Port `9443` mit Ziel `http://127.0.0.1:8766`. Es benutzt nur `tailscale serve`; Funnel, `reset` und `clear` sind außerhalb des Vertrags. Vor einer Änderung wird die komplette Serve-Konfiguration gelesen. Eine Belegung von 9443, die nicht exakt dem Audiozentrale-Vertrag entspricht, blockiert die Änderung.

Nach `apply` wird geprüft, dass 9443 exakt auf den Bridge zeigt und die komplette Serve-Konfiguration nach Entfernung des eigenen 9443-Anteils semantisch identisch zum Vorzustand ist. Bei Abweichung wird ausschließlich 9443 wieder abgeschaltet und der Vorzustand erneut geprüft. `remove` greift nur dann ein, wenn 9443 exakt dem eigenen Vertrag entspricht. Andere Ports und Handler bleiben immer fremdes Eigentum.

## Deployment und Abnahme

`systemd/user/audio-remote-bridge-v1.service` wird releasegebunden installiert. Die Tailscale-Konfiguration wird durch das normale Audio-Control-Deployment weiterhin nicht verändert.

Die Runtimeabnahme benötigt mindestens:

- gemergten, hashgebunden deployten Release,
- laufenden lokalen Control-Dienst auf `127.0.0.1:8765`,
- laufenden Bridge auf `127.0.0.1:8766`,
- revisionsgebundenen Serve-Readback für HTTPS 9443 bei unverändertem übrigen Serve-Zustand,
- HTTPS-Readback aus dem Tailnet,
- Sessionnachweis mit verifizierter Tailscale-Identität,
- kontrollierten iPad-Readback `start → mode → stop` mit final inaktiver Walstimme,
- separaten Safari-/PWA-Bediennachweis für die sichtbare Oberfläche.

Die allgemeinen `runtime_acceptance`-Felder bleiben revisions- und TTL-gebunden. Ein alter Read-only-Abnahmebeleg darf einen neuen wirkenden Bridge-Release nicht automatisch freigeben.

## Rückbau

Der Rückbau erfolgt in umgekehrter Reihenfolge: die neue Bridge-Revision zurücknehmen beziehungsweise Dienst auf den vorherigen Release setzen und bei Bedarf den exakt eigenen Serve-9443-Eintrag entfernen. Der lokale Audio-Control-Dienst auf 8765 und fremde Serve-Konfigurationen werden dabei nicht verändert.
