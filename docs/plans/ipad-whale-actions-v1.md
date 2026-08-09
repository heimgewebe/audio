# Audiozentrale iPad Whale Actions v1

Status: vom Nutzer am 9. August 2026 durch „weiter“ zur Umsetzung freigegeben.

## Ziel

Die bestehende private Tailnet-Audiozentrale auf HTTPS 9443 bleibt überwiegend read-only, erlaubt dem vorgesehenen iPad aber genau die bereits lokal typisierten Buckelwal-Aktionen `start`, `mode` und `stop`. Der lokale Control-Dienst auf `127.0.0.1:8765` bleibt unverändert loopback-only.

## Harte Invarianten

1. Kein Backend-`action_token` verlässt den Heim-PC.
2. Remote-Wirkung ist ausschließlich auf `/bridge/v1/actions/whale` begrenzt; Recorder, Profile, Routing, Geräte, Lautstärke und Systemaktionen bleiben remote gesperrt.
3. Der Browser erhält nur per Same-Origin-JSON-`POST /bridge/v1/session` einen kurzlebigen, vom Bridge erzeugten Sessionnachweis; `GET` erzeugt keinen Capability-Zustand. Der Bridge speichert davon nur einen Hash, eine Ablaufzeit und den Hash der anfragenden Tailscale-Identität.
4. Remote-POST verlangt den exakten Host `heim-pc.tail6dbb90.ts.net:9443`, die passende HTTPS-Origin, eine von Tailscale Serve gesetzte Benutzeridentität, JSON, einen an dieselbe Identität gebundenen Sessionnachweis und einen streng typisierten Payload ohne Zusatzfelder.
5. `start` und `mode` akzeptieren nur `morph`, `organic`, `realistic` oder `ufo`; `stop` akzeptiert keinen Modus.
6. Der Bridge liest den lokalen Backend-Aktionstoken für jede Wirkung frisch, verwendet ihn nur intern und liefert ausschließlich geschrubbten Readback zurück.
7. Eine Remote-Aktion gilt nur bei autoritativem Backend-Readback als erfolgreich.
8. Generische GET/HEAD-Projektion und JSON-Scrubbing bleiben unverändert fail-closed.
9. Tailscale Serve bleibt privat auf 9443; Port 443 und alle anderen Handler sind fremdes Eigentum und werden nicht verändert.
10. Das iPad-UI unterscheidet lokale Aktionsautorität, read-only Fernprojektion und eng begrenzte Wal-Fernautorität sichtbar.

## Abnahme

- Bridge-Sicherheitstests beweisen Identitäts- und Sessionbindung, Same-Origin, Body-/Header-Grenzen, Modus-Allowlist, Token-Scrubbing und dass fremde POST-Pfade 405/404 bleiben.
- UI-Tests beweisen, dass Recorder weiterhin nur auf direktem HTTP-Loopback wirkt, während ausschließlich Walaktionen den Bridge-Pfad nutzen dürfen.
- `just check` ist grün.
- GitHub-Pflichtchecks sind grün.
- Nach Deployment beweist der echte iPad-Agent über `https://heim-pc.tail6dbb90.ts.net:9443` Sessionerhalt sowie `start -> mode -> stop` mit final inaktivem Readback.

## Alternativpfad, bewusst verworfen

Den lokalen Control-Dienst direkt per Tailnet zu exponieren oder die bestehende Bridge allgemein POST-fähig zu machen, reduziert Implementierungsaufwand, vergrößert aber die Wirkungsschnittstelle unnötig. Dieser Pfad wird nicht verwendet.
