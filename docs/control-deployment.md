# Deployment der Audiozentrale

Die laufende Audiozentrale wird nicht aus einem Entwicklungs-Worktree gestartet.
Der Deploypfad liest im Quellcheckout nur die konfigurierte Remote-Adresse. Alle
Fetch- und Referenzmutationen laufen in einem privaten Bare-Repository unter
`~/.local/state/audio-control-deploy/repository.git`.

Jeder auszuliefernde Stand wird unter
`~/.local/share/audio-control-ui/releases/<commit>` als unveränderlicher,
commitgebundener Release angelegt.

## Ablauf

1. Der Timer `audio-control-deploy.timer` prüft `origin/main` jede Minute.
2. Das private Bare-Repository holt den Zielbranch über einen auf SSH und HTTPS
   begrenzten Git-Protokollvertrag. Der Entwicklungscheckout bleibt unverändert.
3. Der Zielcommit wird mit `git archive` in ein privates Staging-Verzeichnis
   extrahiert. Traversal, Symlinks und nicht reguläre Archiveinträge werden
   abgewiesen.
4. Vor jeder Wirkung laufen Control-Vertrag, Audio-Control-Tests,
   Python-Kompilierung und JavaScript-Syntaxprüfung.
5. Der Zeiger `current` wird atomar auf den geprüften Release umgeschaltet.
6. Der persistente Dienst `audio-control-ui-v1.service` wird neu gestartet.
7. Das Deployment gilt erst als erfolgreich, wenn HTML, JavaScript und CSS
   bytegenau zum Release passen und `/api/v1/health` den lokalen Backendvertrag
   bestätigt.
8. Bei einem Fehler werden Releasezeiger, Deploymechanismus und Dienst auf den
   vorherigen Stand zurückgesetzt.
9. Der aktuelle und die zwei jüngsten gültigen Vorgängerreleases bleiben für
   Rollback und Diagnose erhalten; ältere gültige Releases werden entfernt.

Der normale Abstand zwischen Merge und beginnendem Deployment beträgt höchstens
etwa 60 Sekunden zuzüglich lokaler Prüfung. Ein unveränderter, gesunder Release
wird nicht jede Minute neu gestartet.

## Installation und Bedienung

```text
just control-deploy-install <vollständiger-main-commit>
just control-deploy-status
just control-deploy-sync
```

Belege liegen unter `~/.local/state/audio-control-deploy/receipts`. Der jüngste
Beleg ist zusätzlich als `latest.json` verfügbar. Änderungen am Deployskript und
an den systemd-Units werden nach ihrem Merge ebenfalls aus dem geprüften Release
übernommen.
