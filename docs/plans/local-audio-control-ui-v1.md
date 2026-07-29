# Spezifikation: Lokale Audiozentrale v1

- Version: 1
- Datum: 2026-07-29
- Status: **Stufe 1 im Repository umgesetzt; weitere Audiolaufzeiten bleiben
  gate-gebunden**
- Integrierte Basisrevision:
  `origin/main@0fb490acd39b4dfb50e7d33109c346536f873446`
- Der unveröffentlichte Erstentwurf entstand auf
  `9ddefba90679adaf20be2e7e152f4d4aea068e77` und wurde vor der Integration
  bytegenau extern gesichert sowie lokal als Checkpoint committed.
- Übergeordnete Verträge:
  [Audio-Neukonfiguration](audio-configuration-redesign-v1.md),
  [Buckelwal Live Voice](../experiments/buckelwal-live-voice-v1.md),
  [Altbestandsbewertung](../migration/hauski-audio-assessment.md) und
  [Audio-Sicherheitsregeln](../../policy/audio-safety.md)

## Revisionsbindung und Arbeitsgrenze

Diese Spezifikation und ihre Implementierung sind auf die integrierte
Basisrevision gebunden. Andere Worktrees und ihre Branches wurden nur als
vorhanden erfasst und nicht verändert. Die drei vorausliegenden Änderungen aus
#12 bis #14 sind über `origin/main` enthalten: die Doctor-Fixturekorrektur, die
durchgehend spielbare 88-Tasten-Wal-Morph-Stimme und die immutable
Quellsnapshot-Härtung des Builders.

Die UI liest die Details der drei fest erlaubten Modi `morph`, `realistic` und
`ufo` aus dem Buckelwalprofil. Backend und Profil müssen dieselbe vollständige
Allowlist mit `morph` als Standard bestätigen. Die UI übernimmt oder verändert
keinen Morph-Builder und erfindet keine weiteren Modi.

## Produktentscheidung

Die Audiozentrale ist eine **lokale, aufgabenorientierte Fernbedienung**. Sie ist
ausdrücklich keine Digital Audio Workstation.

Menschen wählen ein Ziel wie Spielen, Aufnehmen oder Hören. Die Oberfläche
zeigt danach wenige sichere Entscheidungen, den autoritativen Istzustand und
offene Belege. Sie bietet insbesondere nicht:

- keine Spuren, Timeline oder Clipbearbeitung;
- keinen Browsermixer und keine Plugin-Kette;
- kein browserseitiges Monitoring, Resampling oder Recording;
- keinen generischen Kommandoendpunkt;
- keinen Profil-Apply, solange Apply und Rollback nicht implementiert sind;
- keine scheinbar ausführbaren Funktionen für Dauersong oder Aufnahme.

## Kernbereiche und Aufgaben

| Bereich | Primäre Frage | Vertrag in Stufe 1 |
|---|---|---|
| Start | Was kann ich jetzt sicher tun? | Zusammenfassung, häufige Aufgaben, wichtigste Doctor-Hinweise |
| Spielen | Welches Instrument soll laufen? | Buckelwal starten, Modus wechseln, stoppen; Dienstzustand zurücklesen |
| Aufnehmen | Ist Quelle und Aufnahmevertrag bereit? | Voice-, Piano- und Produktionsplan read-only prüfen; kein Recorderstart |
| Hören | Welcher Wiedergabeweg passt? | Hörprofile vergleichen und jeweils read-only planen |
| Klänge | Welche Klanglaufzeit existiert? | Buckelwalmodi aus Profil; Dauersong sichtbar als nicht ausführbar |
| Verbindungen | Was ist beobachtet, was physisch offen? | Graph, Hardware, Defaults und physische Unbekannte getrennt |
| Diagnose | Warum ist etwas nicht bereit? | Doctor-Warnungen und Command-Health; keine automatische Reparatur |
| Einstellungen | Wem gehört welcher Zustand? | Dienstbindung und Revision; nur nichtkritische Browserdarstellung lokal |

Die Navigation und alle Zustandsbegriffe sind auf Deutsch. Status wird nie nur
durch Farbe vermittelt. Die Ansicht ist mit Tastatur bedienbar, besitzt
erkennbare Fokusringe, hält Dialogfokus innerhalb des Modals, stellt den Fokus
nach Audioaktionen wieder her und berücksichtigt `prefers-reduced-motion`.

## Architektur

```text
Browser auf 127.0.0.1
        │
        │ versioniertes HTTP/JSON, gleicher Ursprung
        ▼
audio-control-ui-v1.service
        │
        ├── read-only: audio-doctor
        ├── read-only: audio-plan PROFILE
        ├── read-only: Buckelwal-Dienststatus
        └── Allowlist: Buckelwal start | mode | stop
                       │
                       ▼
             verwalteter systemd-Userdienst
             MIDI und PCM außerhalb des Browsers
```

### Zustandsautorität

Der Control-Dienst ist die einzige Zustandsautorität der UI:

1. Der Browser lädt einen Snapshot.
2. Eine Bedienhandlung sendet nur eine kleine Absicht wie
   `{"operation":"start","mode":"morph"}`.
3. Das Backend validiert Aktion und Modus gegen eine feste dreiteilige
   Allowlist und das versionierte Buckelwalprofil.
4. Das bestehende Laufzeitskript führt die Aktion aus.
5. Erst ein neuer Backend-Snapshot bestimmt den sichtbaren Folgezustand.

Optimistisches Umschalten von Audiozustand ist unzulässig. Browserseitig
gespeichert werden ausschließlich die Darstellungswünsche „Bewegung reduzieren“
und „automatisch aktualisieren“. Sie besitzen keine Audiosemantik.

### Control-Dienst

`scripts/audio-control` verwaltet `audio-control-ui-v1.service`.

- Standardbindung ausschließlich `127.0.0.1:8765`;
- zufälliger Aktionstoken pro Dienstlauf;
- exakte Prüfung von Host, Origin, JSON-Content-Type, Transfer-Encoding,
  Header- und Anfragegröße;
- keine CORS-Freigabe;
- kein Shellaufruf und keine frei wählbaren Programme oder Argumente;
- serialisierte Audioaktionen und Profilpläne sowie genau ein laufender
  Snapshot-Build; parallele Refreshes starten keine Subprozesskaskade;
- während Mutation und Readback wird auch kein älterer Snapshotcache als
  aktueller Zwischenzustand ausgeliefert;
- höchstens zwölf gleichzeitige HTTP-Handler mit fünf Sekunden
  Socket-I/O-Timeout;
- feste Subprozess-Timeouts, begrenzte Gesamtausgabe und Kill der Prozessgruppe
  bei Überschreitung;
- Content Security Policy und weitere Browser-Sicherheitsheader;
- Snapshotcache standardmäßig vier Sekunden, vor jeder Aktion invalidiert und
  auf jedem Abschlussweg frisch zurückgelesen;
- maximal 128 MiB Speicher, 50 Prozent eines CPU-Kerns, 32 Tasks;
- maximal 21.600 Sekunden Laufzeit und begrenzte Journalrate;
- Ownership-Marker mit Start-Readback; `stop` verändert keine gleichnamige
  aktive Unit ohne diesen Marker;
- `Type=notify`: Dienstbereitschaft erst nach geprüftem Repositoryvertrag und
  erfolgreichem Socket-Bind; dies behauptet keine Audio- oder Hardwarebereitschaft.

Ein fremder lokaler Prozess desselben Unix-Nutzers ist keine getrennte
Sicherheitsdomäne: Er kann bereits die Audio-CLI ausführen. Der Token schützt
zusammen mit Same-Origin- und Hostprüfung vor unbeabsichtigten
Cross-Site-Aktionen, ist aber kein Mehrbenutzer-Authentisierungssystem.

### API v1

| Methode und Pfad | Wirkung |
|---|---|
| `GET /api/v1/health` | leichte Dienstbereitschaft, keine Audioabfrage |
| `GET /api/v1/snapshot` | gebündelter autoritativer Zustand |
| `GET /api/v1/snapshot?refresh=1` | Cache umgehen und Zustand neu lesen |
| `GET /api/v1/profiles/{id}/plan` | bestehende read-only Profilplanung |
| `POST /api/v1/actions/whale` | ausschließlich `start`, `mode` oder `stop` |

Der Snapshot trägt `schema_version`, `api_version`, Erzeugungszeit,
Runtime-HEAD und die revisionsgebundene Spezifikationsbasis. Fehler des Doctors
oder Dienst-Readbacks werden als `unavailable` beziehungsweise `degraded`
sichtbar; unvollständige Erfolgsschemata und systemd-Übergangszustände werden
nicht als „inaktiv“ oder „gesund“ umgedeutet.
API-Fehler tragen ein einheitliches `audio_control_error`-Objekt mit stabilem
Code und menschenlesbarer Nachricht.

## Konsistenz mit bestehenden Plänen

### Walgesang

Die ausführbare Ausnahme in Stufe 1 ist der bereits verwaltete Buckelwaldienst.
Die UI übernimmt keine Audioengine. Start, Stop und Moduswechsel gehen durch
`scripts/whale_live.py`; MIDI-Port, PipeWire-Ziel, Gain, Blockgröße,
Laufzeitgrenze und systemd-Readiness bleiben in dessen Vertrag.

Die Modusdetails werden aus `profiles/buckelwal-live-voice-v1.json` gelesen.
Damit bleiben die 88-Tasten-Morph-Stimme, die realistische Samplebank und der
historische UFO-Vergleich sauber getrennt. Die UI behauptet weder biologische
Stimmerzeugung noch abgeschlossene physische Klangabnahme.

### Dauersong

Dauersong ist im Repositorybereich und im Profil `experimental` genannt, aber
es gibt auf der integrierten Basis keinen eigenständigen ausführbaren
Start-/Stop-/Statusvertrag. Die UI zeigt deshalb:

- Status `planned-not-executable`;
- Isolation und Ressourcenvertrag als Voraussetzung;
- keine Schaltfläche, die einen Prozess vortäuscht oder generisch startet.

Vor Freischaltung sind mindestens eindeutige Prozessidentität, atomarer
Zustand, Stop- und Recoverysemantik, Laufzeit-, CPU-, Speicher- und Loggrenzen
sowie Rückkehrprüfung zum vorherigen Audioprofil erforderlich.

### Aufnahme

Die Profile `voice-recording`, `piano-digital-recording` und `production`
werden angezeigt. Der Profilplaner darf read-only Hardware, physische Fakten,
Labor-Gates und vorgeschlagene Graphänderungen bestimmen.

Die Aufnahmebewertung des Alt-Repositories bleibt verbindlich: Ein ausführbarer
Recorder benötigt Prozessidentität aus PID, Startzeit und Executable, atomaren
Zustand, belegte Quelle und Kanalzuordnung, Abbruch- und
Dateifinalisierungsvertrag, Speichergrenze und Recovery-Test. Bis dahin gibt es
keinen Aufnahmebutton. Auch ein laborbereiter Profilplan begründet keine
Apply-Autorität.

### Audio-Konfiguration und Hören

Die UI liest den kanonischen Profilkatalog und kopiert keine Profilwerte nach
JavaScript. `desktop-mixed`, `reference-listening`, `qobuz-exclusive`,
`receiver` und `bluetooth-convenience` bleiben getrennte Aufgaben. Der
vorhandene `audio plan` ist ausführbar; `apply`, `diff` und `rollback` sind es
noch nicht. Deshalb wird kein Profilwechsel angeboten.

Gerätebeobachtung, gespeicherte Defaults und physische Verbindung werden
getrennt dargestellt. Qobuz-Exklusivität, Bitgenauigkeit, Round-Trip-Latenz und
Bluetooth-Codec dürfen nicht aus einem UI-Status abgeleitet werden.

## Umsetzungsstufen

### Stufe 1 – Lokale Zentrale und sichere Walsteuerung

In dieser Änderung umgesetzt:

- responsiver App-Rahmen und alle acht Kernbereiche;
- lokaler, versionierter Control-Dienst;
- autoritativer Snapshot mit Doctor, Profilkatalog und Buckelwaldienst;
- read-only Profilplanung;
- allowlistete Buckelwalaktionen mit anschließendem Readback;
- verwalteter systemd-Start, Stop und Status des Control-Dienstes;
- fail-closed Platzhalter für Aufnahme, Dauersong und Profil-Apply;
- Unit-, HTTP-, Vertrags- und statische UI-Prüfung.

### Stufe 2 – Hörprofil-Transitionsvertrag

- `audio diff`, `apply` und `rollback` implementieren;
- atomare Transition mit vorherigem Zustand und eindeutiger Operation-ID;
- zunächst ausschließlich `desktop-mixed`;
- UI-Aktion erst nach erfolgreichem Plan und expliziter Bestätigung;
- während der Transition Zwischenzustand nur vom Backend;
- Rollback und Recovery in der gleichen Aufgabenansicht.

**Gate:** deterministischer Apply, idempotenter zweiter Lauf, vollständiger
Rollback und kein zurückgelassener globaler PipeWire-Zustand.

### Stufe 3 – Aufnahme

- gehärteten Recordervertrag neu implementieren;
- verfügbare Zeit und Plattenbudget vor Start;
- Quelle, Kanal, Rate, Format, Zielpfad und Monitoringart sichtbar;
- atomarer Aufnahmezustand und verwertbare WAV-Datei nach Stop und Abbruch;
- Voice-Recording zuerst, Piano erst nach Resamplingentscheidung.

**Gate:** Dateifinalisierung, Recovery, Quellenbindung, 24-Bit-Beleg und
Speichergrenze bestehen ihre Negativtests.

### Stufe 4 – Gemeinsame Kreativlaufzeit

- physisch abgenommene Walstimme integrieren;
- verwalteten Dauersongvertrag definieren;
- gegenseitige Exklusivität und Rückkehr zum vorherigen Hörprofil;
- CPU-, Speicher-, Task-, Laufzeit- und Journalbudgets zentral beobachten.

**Gate:** null XRuns im geforderten Dauertest, keine verwaisten Prozesse und
belegte Rückkehr zum Referenzzustand.

### Stufe 5 – Betriebsreife

- installierbarer Desktopstarter;
- versionierte Ereignis- und Operationshistorie ohne MIDI- oder
  Aufnahmeinhalte;
- Recoveryansicht für unterbrochene Transitionen;
- Browser- und Tastaturabnahme auf Desktop, Tablet und Mobilgerät;
- optional lokale TLS- oder Unix-Socket-Fassade nur bei belegtem Bedarf.

## Akzeptanzkriterien der aktuellen Stufe

### Funktion

- Alle acht Bereiche sind direkt navigierbar und auf 320 Pixel Breite nutzbar.
- Start zeigt Backendzeit, Doctorzustand, Warnungszahl und Wal-Laufzustand.
- Buckelwalmodi stammen aus dem Profil und nicht aus einer UI-Konstante.
- Start, Moduswechsel und Stop liefern erst nach Backend-Readback Erfolg.
- Jeder Profilplan bleibt read-only und zeigt Apply-Autorität sowie Blocker.
- Aufnahme, Dauersong und Profil-Apply besitzen keine wirkende Aktion.
- Ein Doctorfehler wird nicht als gesunder oder inaktiver Audioweg dargestellt.

### Architektur und Sicherheit

- Der Dienst weist Nicht-Loopback-Binds und nichtlokale Hostheader ab.
- Mutationen benötigen Same-Origin, JSON und den laufzeitgebundenen Token.
- Die POST-API akzeptiert ausschließlich drei Walaktionen und bekannte Modi.
- Subprozesse werden ohne Shell, mit festen Pfaden, Timeouts und Ausgabelimit
  gestartet.
- Gleichzeitige Mutationen und Snapshot-Builds werden abgewiesen; eine
  Audioaktion reserviert ihren Status-Readback vor der Mutation.
- HTTP-Threads, Request-Line, Header, Body und Socket-I/O sind begrenzt.
- Statische Pfade und MIME-Typen sind allowlistet; Symlinks und Pfadtraversal
  sind nicht möglich.
- Browser-Sicherheitsheader sind auf statischen und API-Antworten gesetzt.
- Der verwaltete Dienst besitzt Readiness-, Stop-, Laufzeit- und
  Ressourcenvertrag.

### Qualität

- Python-Unit- und HTTP-Tests bestehen offline.
- `python3 scripts/audio_control.py check` bestätigt Bereiche und Verträge.
- JavaScript besteht einen Syntaxcheck, sofern Node lokal verfügbar ist.
- Repository-Safety-Gates und die vollständige bestehende Testsuite bleiben
  grün.
- README und Just-Ziele beschreiben Start, Status, Stop und Prüfung.

### Upstream-Integration

Die frühere revisionsbedingte Prüflücke ist geschlossen: Der Branch basiert auf
`origin/main@0fb490a…` und enthält damit die Doctor-Fixturekorrektur aus #12
sowie die Wal-Morph-Verträge und Builder-Härtung aus #13 und #14. Die UI prüft
den dreiteiligen Modusvertrag, `morph` als Standard sowie Quellbackend,
Anchorzahl, 88-Tasten-Abbildung und Spielgesten in ihren Regressionstests.

## Risiken und offene Folgetasks

| Risiko | Behandlung jetzt | Folgetask |
|---|---|---|
| Doctor kann auf defektem Host langsam sein | Cache, Single-Flight, Timeout, Output- und Handlergrenzen | parallele, generationsgebundene Beobachtung nur bei messbarem Bedarf |
| Moduswechsel stoppt vor erneutem Start | bestehende Laufzeitsemantik sichtbar lassen | transaktionaler Wechsel mit Recovery |
| lokaler Nutzerprozess kann API oder CLI bedienen | Loopback, Same-Origin, Token; keine Mehrnutzerbehauptung | nur bei Mehrnutzerbedarf Peer-Credentials/Socket |
| Aufnahme wirkt in UI „nah“, ist aber nicht ausführbar | klare Grenztexte, kein Button | Recordervertrag aus Stufe 3 |
| physische Signalwege bleiben unbekannt | Unknowns sichtbar, keine Ableitung | geführte physische Verifikation |

Offen bleiben außerdem die subjektive Wal-Klangabnahme, physische
Round-Trip-Messung, Qobuz-Ratenbeleg, kanonische Ardour-Form, echter
Dauersong-Laufzeitvertrag und alle Profil-Apply-/Rollback-Operationen.
