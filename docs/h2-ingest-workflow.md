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

Die aktuelle Zoom-Bedienungsanleitung dokumentiert außerdem: Überschreitet eine
Aufnahmedatei 2 GB, setzt der H2essential die Aufnahme ohne Pause in einer neuen
Datei fort und ergänzt den ursprünglichen Dateinamen um `_001` (danach weitere
fortlaufende Segmente). Der Ingest behandelt solche lückenlosen Folgedateien
einer Spur als Segmente derselben Session. RF64 wird für den nativen
H2essential-Aufnahmepfad deshalb nicht vorausgesetzt und weiterhin fail-closed
abgewiesen.

## Sicherheitsvertrag

`scripts/audio-h2-ingest` besitzt zusätzlich zu den vier Kernoperationen Metadaten- und Medienbindungen für die Audiozentrale. Die Kernoperationen sind:

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
3. validiert RIFF/BWF, H2-Originator, Szene, Spurrolle, Segmentfolge und 32-bit-float Stereo;
4. validiert BWF/Audioformat und hasht jeden H2-Master auf derselben geöffneten Dateigeneration; Metadaten und Digest können dadurch nicht aus zwei verschiedenen Quellgenerationen stammen;
5. leitet daraus `master_set_sha256` und die content-addressierte `material_id` ab;
6. verifiziert bei einem bereits vorhandenen Materialobjekt dessen archivierte Master vollständig und beendet den Re-Import **ohne** Staging oder erneute Zielkopie;
7. erzeugt nur für neues Material ein privates Staging-Verzeichnis und kopiert jeden Master bytegenau, wobei der Copy-Hash exakt dem Vorhash entsprechen muss;
8. führt `fsync` auf den Zieldateien aus und hasht die Kopien erneut;
9. schreibt Manifest und getrennte mutable Annotationen erst nach erfolgreicher Kopierprüfung;
10. veröffentlicht den vollständigen Materialordner atomar und synchronisiert anschließend das Bibliotheksverzeichnis.

Der Ingest schreibt niemals auf die H2-Karte. Ein Bibliotheksziel unterhalb des
H2-Quellpfads wird ausdrücklich abgewiesen.

## Zielstruktur

Standardziel:

```
~/Music/Audio-Aufnahmen/H2-Material/<material-id>/
├── manifest.json
├── annotations.json
└── master/
    ├── <scene>_FRONT.WAV
    ├── <scene>_FRONT_001.WAV   # nur bei H2-2-GB-Folgesegmenten
    ├── <scene>_REAR.WAV
    └── <scene>_MIX.WAV
```

Für neue Installationen ist dieser Root autoritativ. Liegt im neuen Root noch
kein Material, aber im unmittelbar vorherigen Standard
`~/Music/Audio-Material/H2`, wird genau dieser bestehende Legacy-Root
weiterverwendet. Liegen in beiden Roots Materialobjekte, wird die automatische
Rootwahl fail-closed abgewiesen; es gibt keinen stillen Splitbrain-Pfad und
keine automatische Verschiebung oder Löschung. Ein explizites
`AUDIO_MATERIAL_ROOT` behält diesen historischen Material-Parent-Vertrag nur
für die direkte Standalone-Nutzung von `h2_ingest.py`; der gehärtete
Audio-Control-Dienst lehnt den Override ausdrücklich ab und schreibt nur in
seinen Primär- bzw. Legacy-Root.

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

Markerimport, kreative DAW-Übergabe, zweite Sicherung und eine spätere Löschfreigabe bleiben getrennte Ausbau- und Acceptance-Schritte. Neue Installationen legen den physischen H2-Materialroot bewusst unter den bereits gehärteten Audio-Aufnahmen-Schreibroot; bestehende Installationen mit dem vorherigen `Audio-Material/H2`-Root behalten genau diesen einen Root als Übergangspfad. Die Produktdomäne bleibt davon unabhängig „Material“.

## Remote-H2-Inbox

H2-Dateien, die unterwegs über ein iPad oder Smartphone angeliefert werden,
landen nicht direkt in der Materialbibliothek und werden auch nicht durch die
Audio-Remote-Bridge als Multi-GB-HTTP-Body transportiert. Die Audiozentrale
stellt dafür den privaten lokalen Drop bereit:

~~~
~/Music/Audio-Aufnahmen/H2-Remote-Inbox/<transfer-id>/
~~~

prepare-runtime-state legt H2-Remote-Inbox als privaten Root an. Der eigentliche
Netzwerktransport bleibt davon getrennt: SFTP, SMB oder ein späterer resumabler
Transport können einen neuen transfer-id befüllen. Dieser Transport erhält
dadurch keine Materialautorität und wird von diesem Vertrag nicht automatisch
eingerichtet.

Ein Transfer muss die H2-Quellstruktur erhalten, insbesondere
ZOOM_H2essential.SYS und die Sessionordner. Die Audiozentrale erzeugt keinen
Fake-Sentinel. Remote-Clients dürfen beim Import außerdem keinen freien
Serverpfad übergeben; akzeptiert werden ausschließlich eine validierte
transfer_id und scene.

Die Oberfläche liest die Remote-Inbox nur auf expliziten Klick. Der normale
8-Sekunden-Refresh des bestehenden H2-Arbeitsbereichs scannt sie nicht. Pro
Readback werden höchstens die zwei zuletzt geänderten sicheren Transferordner
geprüft. Für jede Session laufen dieselben H2-Scanner-, BWF-, Rollen-,
Segment- und Größenprüfungen wie beim direkt angeschlossenen Recorder.

Beim Archivieren bleibt h2_ingest.py import die einzige Autorität für
master_set_sha256, content-addressierte material_id, Copy-Hash, Re-Hash, fsync
und atomare Veröffentlichung. Ein erfolgreicher oder fehlgeschlagener Import
löscht den Remote-Transfer nicht. Eine spätere Inbox-Bereinigung braucht einen
eigenen verifizierten Vertrag.

Der Zielpfad lautet damit:

~~~
H2 -> iPad/Smartphone -> Tailnet-Dateitransport -> H2-Remote-Inbox
   -> bestehender H2-Ingest -> H2-Materialbibliothek
~~~

V1 behauptet für die Remote-Inbox ausdrücklich weder einen eingebauten
Dateiserver noch Resume-Semantik des Transportes noch eine kryptografische
Geräteattestierung des H2.
