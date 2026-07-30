# Deployment der Audiozentrale

Die laufende Audiozentrale wird nicht aus einem Entwicklungs-Worktree gestartet.
Der Deploypfad liest im Quellcheckout nur die konfigurierte Remote-Adresse. Alle
Fetch- und Referenzmutationen laufen in einem privaten Bare-Repository unter
`~/.local/state/audio-control-deploy/repository.git`.

Jeder auszuliefernde Stand wird unter
`~/.local/share/audio-control-ui/releases/<commit>` als unveränderlicher,
commitgebundener Release angelegt. Der Releasebeleg
`.audio-control-release.json` liefert dem laufenden Backend auch ohne `.git` die
autoritative Commitidentität.

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
6. Host, Port und Laufzeitverwalter werden atomar in
   `~/.local/state/audio-control-deploy/runtime.env` gebunden. Version 1 bleibt
   absichtlich auf `127.0.0.1` beschränkt; der Port ist zwischen 1024 und 65535
   konfigurierbar.
7. Deployskript und systemd-Units werden bei jedem Lauf gegen den gebundenen
   Release abgeglichen. Byte- oder Modusdrift wird auch dann repariert, wenn der
   Zielcommit unverändert ist; ein bereits identischer Stand bleibt ohne
   Unit-Prüfung und ohne Neustart wirkungsfrei.
8. Der persistente Dienst `audio-control-ui-v1.service` wird bei einem neuen
   Release, geänderter Laufzeitkonfiguration oder geänderter UI-Unit neu gestartet.
9. Das Deployment gilt erst als erfolgreich, wenn HTML, JavaScript und CSS
   bytegenau zum Release passen und `/api/v1/health` zusätzlich exakt den
   Zielcommit als laufende Backendrevision bestätigt.
10. Bei einem Fehler werden Laufzeitkonfiguration, Releasezeiger,
    Deploymechanismus und Dienst auf den vorherigen Stand zurückgesetzt.
11. Der aktuelle und die zwei jüngsten gültigen Vorgängerreleases bleiben für
    Rollback und Diagnose erhalten; ältere gültige Releases werden entfernt.

Beim ersten Selbstupdate einer älteren Installation kann `runtime.env` noch
fehlen. Der UI-Dienst startet dann mit sicheren Defaults und übernimmt nur den
bisher konfigurierten Port aus `~/.config/audio-control-deploy.env`. Die
Bind-Adresse bleibt fest auf `127.0.0.1`. Sobald der neue Deployer läuft,
erzeugt er `runtime.env`; diese releasegebundene Konfiguration hat anschließend
Vorrang vor dem Migrationsfallback. Die Datei liegt im bereits vorhandenen und
für den Deploydienst schreibbaren State-Verzeichnis; deshalb braucht der
Legacy-Upgradepfad weder einen neuen Konfigurationsordner noch eine breitere
Schreibfreigabe unter `~/.config`.

Die Startseite enthält zusätzlich den unsichtbaren statischen Vertrag
`audio-control-deployment-contract=revision-bound-v1`. Die Legacy-Version
`1e759e0` kennt noch keine Backendrevision und prüft beim ersten Selbstupdate nur
die drei Webdateien bytegenau. Der Marker macht diesen ersten Sprung auch dann
unterscheidbar, wenn ein fehlgeschlagener Dienststopp den alten Prozess auf dem
Port zurücklässt; der alte Deployer kann diesen Prozess dann nicht als neuen
Release bestätigen.

Version 1 bindet den persistenten Dienst und seine systemd-Sandbox fest an
`~/.local/share/audio-control-ui` und
`~/.local/state/audio-control-deploy`. Abweichende Werte für `--deploy-root`,
`--state-root`, `AUDIO_CONTROL_DEPLOY_ROOT` oder `AUDIO_CONTROL_STATE_ROOT`
werden vor jeder Installations- oder Deploymentwirkung abgewiesen. Damit kann
eine scheinbar erfolgreiche Installation nicht erst beim nächsten Timerlauf an
statischen Dienstpfaden oder fehlenden Schreibfreigaben scheitern.

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
