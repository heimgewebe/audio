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
   konfigurierbar. Dieselbe Datei bindet
   `AUDIO_TELEMETRY_LEVEL_SOURCE` an die private JSON-Ausgabe des
   Pegelbeobachters unter `$XDG_RUNTIME_DIR/audio-control-level-observer`.
7. Deployskript und systemd-Units werden bei jedem Lauf gegen den gebundenen
   Release abgeglichen. Byte- oder Modusdrift wird auch dann repariert, wenn der
   Zielcommit unverändert ist; ein bereits identischer Stand bleibt ohne
   Unit-Prüfung und ohne Neustart wirkungsfrei.
8. Der persistente Dienst `audio-control-ui-v1.service` wird bei einem neuen
   Release, geänderter Laufzeitkonfiguration oder geänderter UI-/Observer-Unit
   neu gestartet. Seine `Wants=`-/`PartOf=`-Kopplung startet dabei auch
   `audio-control-level-observer-v1.service`,
   `audio-qobuz-desktop-recovery-v1.service` und
   `audio-qbzd-qconnect-recovery-v1.service` revisionsgebunden und beendet sie
   zusammen mit der UI. Der erste Recovery-Dienst darf ausschließlich
   WirePlumber nach dem ALSA-Direct-Handoff neu enumerieren. Der zweite darf
   ausschließlich einen nach mindestens fünf Minuten stabil belegten, inaktiven
   QConnect-Reconnect-Fehler durch Neustart von `qbzd.service` reparieren; ein
   aktiver QBZD-PCM-Besitz blockiert ihn zusätzlich direkt über `/proc/asound`.
   Die vollständigen Fail-closed-Gates stehen in
   `docs/qobuz-desktop-recovery.md` und `docs/qbzd-qconnect-recovery.md`.
9. Das Deployment gilt erst als erfolgreich, wenn HTML, JavaScript und CSS
   bytegenau zum Release passen und `/api/v1/health` zusätzlich exakt den
   Zielcommit als laufende Backendrevision bestätigt. Releases mit einem der
   Recovery-Verträge müssen die jeweilige Unit außerdem als installiert und
   aktiv zurücklesen; dies gilt auch bei unverändertem Release.

Bei der einmaligen Einführung einer neuen Runtime-Dateizuordnung läuft der
erste Timer-Pass zwangsläufig noch mit dem alten, bereits geladenen Deployer.
Dieser Pass kann eine neue Recovery-Unit noch nicht kennen und sein Receipt
beweist ihre Installation ausdrücklich nicht. Der unmittelbar folgende Pass
mit dem neuen Deployer installiert, startet und verifiziert sie. Für die
Einführung ist deshalb dessen abschließender Readback operativ erforderlich.
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

`control-deploy-status` ist strikt read-only: der Befehl legt keine Runtime-Roots
an und repariert keine Rechte. Die Ausgabe projiziert unter `runtime_roots` für
Deploy- und State-Root, ob der jeweilige Pfad vorhanden und vertrauenswürdig ist.
Inhalte eines fehlenden, unsicheren oder nicht privaten Roots werden nicht als
Deploymentwahrheit gelesen; der unabhängige systemd-Servicezustand bleibt dennoch
sichtbar. `control-deploy-sync` bleibt der mutierende Pfad, der die kanonischen
Runtime-Roots auf `0700` konvergiert.

Belege liegen unter `~/.local/state/audio-control-deploy/receipts`. Der jüngste
Beleg ist zusätzlich als `latest.json` verfügbar. Änderungen am Deployskript und
an den systemd-Units werden nach ihrem Merge ebenfalls aus dem geprüften Release
übernommen.

## Dashboard-Projektion

Die Audiozentrale liest `latest.json` über einen symlinkfreien,
größenbegrenzten Dateideskriptor. An den Browser gelangen ausschließlich
Deploymodus, Quelle, Runtime- und Belegcommit, Synchronität, letzter
Abgleichzeitpunkt und Dienstgesundheit. Interne Quell-, State- und Releasepfade
sowie übrige Receipt-Daten bleiben privat. Ein fehlender, fremder oder
widersprüchlicher Beleg wird als `nicht lesbar` beziehungsweise `Abweichung`
dargestellt; die UI erfindet keinen erfolgreichen Deployzustand.
