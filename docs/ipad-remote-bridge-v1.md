# Audiozentrale iPad Remote Bridge v1

## Ziel

Die iPad-/PWA-Oberfläche darf den Heim-PC über den privaten Tailnet-Zugang lesen und zwei eng begrenzte Wirkbereiche steuern: die Buckelwal-Stimme sowie den gehärteten Recorder für `Nur Gesang` und `Klavier + Gesang`. `audio_control.py` bleibt unverändert auf `127.0.0.1:8765`; davor sitzt die eng begrenzte Bridge auf `127.0.0.1:8766`. Tailscale Serve veröffentlicht ausschließlich diese Bridge über HTTPS-Port `9443` im privaten Tailnet.

Der bestehende Tailscale-Serve-Eintrag auf HTTPS 443 gehört einer anderen Anwendung und ist ausdrücklich fremdes Eigentum. Der Audiozentrale-Controller darf ihn weder ersetzen noch zurücksetzen noch löschen.

## Sicherheitsgrenzen

`audio_remote_bridge.py` ist kein allgemeiner Reverse Proxy. Backendhost und -port sind Konstanten. Die normale Projektion bleibt auf `GET`/`HEAD`, eine feste statische App-/Lektions-Allowlist sowie die read-only API-Endpunkte Health, Telemetrie, Replay, Buckelwal-Lektion, Snapshot, Recorderbibliothek, verifizierte Take-Wiedergabe/-export und genau einen typisierten Profilplanpfad beschränkt. Statische und feste API-Pfade akzeptieren keine Query; der Snapshot akzeptiert nur keine Query oder `refresh=1`. Kodierte Slash-/Backslash-Umgehungen werden fail-closed abgewiesen.

Es existieren genau zwei wirkende Fernpfade:

- `POST /bridge/v1/actions/whale` akzeptiert ausschließlich `start`, `mode` und `stop`; `start` und `mode` sind auf `morph`, `organic`, `realistic` und `ufo` begrenzt.
- `POST /bridge/v1/actions/recording` akzeptiert ausschließlich `plan`, `start`, `stop`, `recover`, `categorize`, `trash` und `restore`. `plan`/`start` sind auf `voice` und `piano-vocal`, sichere einzelne `.wav`-Namen und eine begrenzte Dauer beschränkt. `start` verlangt den exakten, zuvor vom Recorder gelieferten Plan-SHA-256. `stop`, `recover`, `categorize`, `trash` und `restore` verlangen remote immer eine explizite 24-stellige Session-ID; `categorize` akzeptiert zusätzlich nur die feste Bibliotheks-Kategorienliste. `trash` ist ein wiederherstellbarer Bibliothekszustand und vernichtet keine WAV-, MIDI- oder Manifestbytes.

Profile, Routing, Geräte-, Lautstärke- und Systemaktionen bleiben remote gesperrt. Wal- und Recorderwirkungen teilen sich einen nichtblockierenden Bridge-Effekt-Lock; parallele Wirkung wird vor Backendkontakt abgewiesen.

Beide Fernpfade sind an den exakten HTTPS-Host `heim-pc.tail6dbb90.ts.net:9443`, eine passende Same-Origin-`Origin`, eine von Tailscale Serve verifizierte `Tailscale-User-Login`-Identität und einen kurzlebigen Bridge-Sessionnachweis gebunden. Auch die Ausgabe dieses Sessionnachweises erfolgt ausschließlich per Same-Origin-`POST /bridge/v1/session` mit JSON; ein `GET` erzeugt keinen Capability-Zustand. Tailscale Serve entfernt eingehend gefälschte Identitätsheader und setzt sie für Tailnet-Traffic selbst; die Bridge läuft deshalb weiterhin ausschließlich auf Loopback. Die Bridge speichert vom Sessionnachweis nur SHA-256 und Ablaufzeit und bindet ihn an den Hash der Tailscale-Identität.

Der lokale Backend-`action_token` verlässt den Heim-PC niemals. Vor jeder Wirkung liest die Bridge einen frischen lokalen Snapshot und prüft den passenden lokalen Capability-/Statusvertrag. Sie übernimmt den lokalen Aktionstoken nur intern und sendet die streng typisierte Aktion an `127.0.0.1:8765`. Beim Recorder bleiben damit insbesondere Hardware-/Labor-Gates, Plan-Hash, Quellenidentitäten, Prozessbindung, Dateigrenzen, atomare Veröffentlichung und Recovery vollständig autoritativ im lokalen Recorder. Die Bridge umgeht oder lockert keinen dieser Verträge.

Antworten werden vor der Auslieferung rekursiv von lokalen beziehungsweise sicherheitsrelevanten Schlüsseln bereinigt. Eine erfolgreiche Walwirkung muss einen autoritativen `audio_control_action_result` mit Snapshot-Readback liefern. Eine erfolgreiche Recorderaktion muss einen `audio_control_recording_action_result` liefern; ein Plan muss `ready`, Modus und exakten Plan-SHA binden, wirkende Recorderoperationen einen autoritativen Snapshot-Readback.

Zum Backend werden bei normalen Leseanfragen ausschließlich ein synthetischer `Host: 127.0.0.1:8765`, `Connection: close` und optional `If-None-Match` beziehungsweise `Range` gesendet. Recorder-WAV und Roland-MIDI sind nur über `/api/v1/recordings/<24-hex>/audio` beziehungsweise `/midi` lesbar; die Bridge streamt ausschließlich den vom lokalen Backend zuvor vollständig verifizierten `audio/wav`- beziehungsweise `audio/midi`-Take und puffert große Dateien nicht vollständig im Speicher. Diese GET/HEAD-Routen schaffen keine zusätzliche Wirk- oder Sessionautorität. Andere eingehende Header, Cookies, Autorisierungsdaten und Bodies werden nicht weitergereicht. Jede normale Projektionsantwort trägt `X-Audio-Remote-Bridge: read-only-v1`; erfolgreiche Walaktionen tragen `X-Audio-Remote-Bridge: whale-action-v1` und `X-Audio-Remote-Effects: whale-v1`; erfolgreiche Recorderaktionen `X-Audio-Remote-Bridge: recording-action-v1` und `X-Audio-Remote-Effects: recording-v1`.

JSON-Antworten der lokalen API werden vollständig geparst, rekursiv gefiltert, erneut geprüft und deterministisch kodiert. Ungültiges oder zu großes JSON wird nicht transparent weitergereicht.

Ein Request allein ist weiterhin kein Beleg für Geräteanwesenheit oder eine erfolgreiche Audiowirkung. Nur der autoritative Backend-/Recorder-Readback nach der typisierten Aktion belegt die konkrete Wirkung.

## Tailscale Serve

`scripts/audio_remote_bridge_tailscale.py` verwaltet ausschließlich HTTPS-Port `9443` mit Ziel `http://127.0.0.1:8766`. Es benutzt nur `tailscale serve`; Funnel, `reset` und `clear` sind außerhalb des Vertrags. Vor einer Änderung wird die komplette Serve-Konfiguration gelesen. Eine Belegung von 9443, die nicht exakt dem Audiozentrale-Vertrag entspricht, blockiert die Änderung.

Nach `apply` wird geprüft, dass 9443 exakt auf die Bridge zeigt und die komplette Serve-Konfiguration nach Entfernung des eigenen 9443-Anteils semantisch identisch zum Vorzustand ist. Bei Abweichung wird ausschließlich 9443 wieder abgeschaltet und der Vorzustand erneut geprüft. `remove` greift nur dann ein, wenn 9443 exakt dem eigenen Vertrag entspricht. Andere Ports und Handler bleiben immer fremdes Eigentum.

## Deployment und Abnahme

`systemd/user/audio-remote-bridge-v1.service` wird releasegebunden installiert. Die Tailscale-Konfiguration wird durch das normale Audio-Control-Deployment weiterhin nicht verändert.

Die Runtimeabnahme benötigt mindestens:

- gemergten, hashgebunden deployten Release,
- laufenden lokalen Control-Dienst auf `127.0.0.1:8765`,
- laufende Bridge auf `127.0.0.1:8766`,
- revisionsgebundenen Serve-Readback für HTTPS 9443 bei unverändertem übrigen Serve-Zustand,
- HTTPS-Readback aus dem Tailnet,
- Sessionnachweis mit verifizierter Tailscale-Identität und den Scopes `whale` und `recording`,
- ein Recorder-Backend-Zeitbudget der Bridge, das größer als der gebundene Worst-Case aus Recorderaktion plus WAV- und optionaler MIDI-Nachprüfung ist; ein bereits erfolgreicher Stop darf nicht allein durch einen kürzeren Bridge-Timeout als Remote-Fehler erscheinen,
- negative, wirkungsfreie Abweisung ungültiger Wal- und Recorderpayloads,
- einen erfolgreichen echten Recorder-`plan` über die Tailnet-Bridge mit lokal autoritativem Plan-Hash,
- für eine vollständige Wirkungsabnahme zusätzlich einen kontrollierten Recorder-Start/Stop mit realem Take sowie den bestehenden Wal-Readback,
- separaten Safari-/PWA-Bediennachweis für die sichtbare Oberfläche.

Die allgemeinen `runtime_acceptance`-Felder bleiben revisions- und TTL-gebunden. Ein alter Read-only- oder Wal-only-Abnahmebeleg darf einen neuen Recorder-fähigen Bridge-Release nicht automatisch freigeben.

## Rückbau

Der Rückbau erfolgt in umgekehrter Reihenfolge: die neue Bridge-Revision zurücknehmen beziehungsweise Dienst auf den vorherigen Release setzen und bei Bedarf den exakt eigenen Serve-9443-Eintrag entfernen. Der lokale Audio-Control-Dienst auf 8765 und fremde Serve-Konfigurationen werden dabei nicht verändert.
