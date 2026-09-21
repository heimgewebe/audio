# Zoom H2essential: sicherer Datei-Ingest v1

## Zweck

Der H2essential ist für die Audiozentrale im Datei-Transfer-Modus eine
**removable source** und kein neues PipeWire- oder Aufnahmeprofil. Der Ingest
übernimmt vorhandene Recorderdateien in die bestehende Materialdomäne, ohne
Audio auf der SD-Karte zu verändern.

## Beobachtete reale H2-Struktur

Am 21. September 2026 wurde der angeschlossene H2essential live als
USB-Massenspeicher `ZOOM H2e SD R&W` mit exFAT-Volume `ZOOM_H2E` beobachtet.
Eine Aufnahme ist auf der Karte ein Sessionordner wie:

```
170926_191401/
├── 170926_191401_FRONT.WAV
├── 170926_191401_REAR.WAV
└── 170926_191401_MIX.WAV
```

Je nach aktivierten H2-Spuren können REAR und MIX fehlen. Die reale Stichprobe
bestand aus neun Sessionordnern und 21 WAV-Dateien.

Die geprüften Dateien waren BWF/RIFF-WAV mit:

- PCM 32-bit float little-endian;
- Stereo;
- 44,1 kHz in der beobachteten Stichprobe;
- `bext`-Metadaten mit `ZOOM H2essential`, Aufnahmedatum/-zeit, `zSCENE`,
  `zTAKE` und Coding-History;
- H2-Spurrollen `TRK=1` (FRONT), `TRK=3` (REAR), `TRK=5` (MIX).

Die beobachteten Dateien enthielten keine `cue `-, `LIST`-, `iXML`- oder
`axml`-Chunks. Das belegt nur den aktuellen Kartenbestand und nicht, dass der
H2 niemals Marker in anderen Aufnahmen speichert.

## Sicherheitsvertrag

`scripts/audio-h2-ingest` besitzt vier Operationen:

- `scan`: read-only Erkennung der H2-Sessions;
- `import <scene>`: byteidentische Archivierung genau einer Session;
- `library`: flache Materialprojektion ohne aktuellen Vollhash-Claim;
- `verify <material-id>`: erneuter SHA-256-Vollcheck aller archivierten Master.

Eine Session wird als **eine Materialeinheit** behandelt. FRONT, REAR und MIX
bleiben getrennte Master innerhalb dieser Einheit. MIX wird nicht verworfen:
Auch wenn der Recorder ihn selbst erzeugt hat, ist er Bestandteil des
unveränderten H2-Quellbestands.

Der Import:

1. akzeptiert nur normale Dateien und Verzeichnisse ohne Symlink;
2. verlangt die H2-Kennung `ZOOM_H2essential.SYS`;
3. validiert RIFF/BWF, H2-Originator, Szene, Spurrolle und 32-bit-float Stereo;
4. kopiert jede Datei in ein privates Staging-Verzeichnis;
5. berechnet beim Lesen SHA-256 und prüft die Quellidentität vor/nach dem Kopieren;
6. führt `fsync` auf dem Ziel aus;
7. hasht das Ziel erneut und vergleicht es mit der Quelle;
8. veröffentlicht erst danach den vollständigen Materialordner atomar;\n9. meldet einen Wiederholungsimport nur dann als bereits vorhanden, wenn die archivierten Master aktuell erneut vollständig gehasht und gegen das Manifest verifiziert wurden.

Der Ingest schreibt niemals auf die H2-Karte. Ein Bibliotheksziel unterhalb des
H2-Quellpfads wird ausdrücklich abgewiesen.

## Zielstruktur

Standardziel:

```
~/Music/Audio-Material/H2/<material-id>/
├── manifest.json
├── annotations.json
└── master/
    ├── <scene>_FRONT.WAV
    ├── <scene>_REAR.WAV
    └── <scene>_MIX.WAV
```

`manifest.json` und die Master werden read-only veröffentlicht. Das Manifest
bindet die Originaldateinamen, Rollen, SHA-256, Größen, Audioformat und
BWF-Metadaten.

`annotations.json` ist getrennt veränderbar und ist für Titel, Notiz, Tags und
spätere Marker vorgesehen. Dadurch ändert eine Benennung nie das Masteraudio
oder seinen Provenienzbeleg.

## Nutzung

Nur prüfen:

```bash
./scripts/audio-h2-ingest scan --source-root /media/$USER/ZOOM_H2E
```

Eine explizit gewählte Session importieren:

```bash
./scripts/audio-h2-ingest import 170926_191401 \
  --source-root /media/$USER/ZOOM_H2E
```

Bibliothek lesen:

```bash
./scripts/audio-h2-ingest library
```

Ein bereits importiertes Materialobjekt vollständig neu verifizieren:

```bash
./scripts/audio-h2-ingest verify <material-id>
```

## Noch nicht behauptet

v1 belegt noch nicht:

- automatische Markerübernahme aus einer H2-Datei mit tatsächlich gesetzten Markern;
- Bitwig- oder Ardour-Übergabe;
- Backup auf ein zweites physisches Medium;
- Freigabe zum Löschen der SD-Karten-Originale;
- Audiozentrale-UI für den Import.

Diese Punkte sind getrennte Ausbau- und Acceptance-Schritte.
