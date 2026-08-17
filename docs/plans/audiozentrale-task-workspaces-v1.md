# Audiozentrale Task Workspaces v1

Status: vom Nutzer am 9. August 2026 als Bedienziel freigegeben.

Dieses Dokument ergänzt `audiozentrale-product-v2.md` für die sichtbare Bedienoberfläche. Es ändert **nicht** die bestehenden vier Produkt-/State-Domänen des maschinenlesbaren Produktmodells. Diese bleiben interne Zustands- und Migrationsgrenzen. Die primäre Navigation wird dagegen tätigkeitsorientiert.

## Harte UI-Invarianten

1. Die sichtbaren Hauptorte sind exakt **Home, Hören, Aufnehmen, Spielen, Material, System**.
2. Die Hierarchie lautet **Home → Arbeitsbereich → Fokus**.
3. `Home` ist Einstieg und Zusammenfassung. Signalweg-, Recorder- und Diagnosetiefe gehören in ihre Arbeitsbereiche.
4. `Aufnehmen` ist ein eigener Arbeitsbereich. Der vorhandene Recorder-DOM wird dorthin verschoben, nicht dupliziert.
5. Jeder sinnvolle `data-depth-panel`-Arbeitsbereich kann die gesamte Browserfläche übernehmen.
6. Fokus verändert ausschließlich die Darstellung. Er klont, ersetzt oder serialisiert den Arbeitsbereich nicht und darf Eingabewerte, Fokus, Recorderentwurf oder Backendzustand nicht verlieren.
7. Es gibt höchstens einen Fokus gleichzeitig. `Escape` und `Zurück` verlassen ihn deterministisch.
8. Kritische Backendgrenzen bleiben unverändert. Fokus verleiht keine zusätzliche Audio-, Routing-, Geräte- oder Qobuz-Autorität.
9. Technische Wahrheit, Doctor und passive Live-Telemetrie liegen unter `System`.
10. Die Oberfläche bleibt auf iPad und Desktop dieselbe App-Shell; Vollbild respektiert Safe-Area-Inset und Touchziele.
11. Die Buckelwal-Stimme ist im lokalen Loopback-Betrieb unter `Spielen` wirklich steuerbar: Modus wählen, starten, wechseln, stoppen. Die Fern-Audiozentrale bleibt dafür read-only; die bestehende Token-/Origin-Grenze wird nicht aufgeweicht.

## Sichtbare Arbeitsbereiche

| Route | Aufgabe | Primärinhalt |
|---|---|---|
| `#home` | orientieren | Bereitschaft, vier Schnellzugriffe, Aufmerksamkeit |
| `#hoeren` | Musik hören | Referenzweg, Hörprofile, Samplerate/Interface |
| `#aufnehmen` | aufnehmen | derselbe Live-Recorder, Recorderstatus, Aufnahmeprofile |
| `#spielen` | Instrument spielen | Roland/Buckelwal, Spielprofile, Lernbereich |
| `#material` | Ergebnisse nutzen | Takes, Klänge, Replay |
| `#system` | Technik prüfen | Wahrheitsebenen, Live-Telemetrie, Geräte, Doctor, PWA, Konfiguration |

## Kompatibilität

Alte Links werden nur als Eingangsaliase erhalten: `#start` und `#now` → `#home`, `#setups` → `#home`, `#library` und `#klaenge` → `#material`. `#verbindungen`, `#diagnose` und `#einstellungen` bleiben Systemaliase. Neue Hauptnavigation verwendet ausschließlich die sechs Task-Workspace-Routen.

## Fokusvertrag

Der Fokus wird durch eine CSS-Zustandsklasse auf **dem existierenden Panel** realisiert. Der Knoten bleibt Teil desselben Dokuments und behält seine untergeordneten Eingaben, Player, Recorderaktionen und Live-Readbacks. Der alte Text-Zusammenfassungsdialog ist für normale Arbeitsbereiche ausdrücklich kein Fokusmodus mehr. Spezialisierte Dialoge wie die geführte Wal-Lektion dürfen ihren eigenen Dialogvertrag behalten.

## Abnahme

- genau sechs Hauptnavigationsziele und sechs `data-view`-Arbeitsbereiche;
- `recorder-workspace` existiert genau einmal und wird in den Aufnahme-Host umgehängt;
- Hören und System erhalten bestehende Signal-/Wahrheitsknoten durch DOM-Reparenting, nicht durch Kopien;
- Fokus setzt `is-workspace-focused` auf denselben Knoten und ist per `Escape`/Button reversibel;
- alte Deep-Links bleiben auf definierte neue Arbeitsbereiche abbildbar;
- Buckelwal `start`/`mode`/`stop` sind aus `#spielen` an `/api/v1/actions/whale` gebunden und nur bei direkter lokaler Loopback-Autorität bedienbar;
- bestehende Recorder-, Telemetrie-, PWA- und Sicherheitsverträge bleiben grün.

## Visuelles Zonensystem v2

Die Oberfläche übernimmt für alle Arbeitsbereiche ein funktionales Farbsystem, ohne
Zustandswahrheit aus Farbe abzuleiten. Farbe dient ausschließlich der räumlichen
Orientierung: Hören ist kühl türkis, Receiver-/Lautsprecherwege sind warm amber,
Aufnehmen ist korallfarben, Spielen violett/blau, Bibliothek blau und System neutral
slate. Statusfarben für beobachtet, vor Ort, Labor und Fehler bleiben davon getrennt.

Auf iPad und kleineren Viewports dürfen zentrale Gerätenamen, Signalwegwerte und
Handlungsbeschreibungen nicht durch `text-overflow: ellipsis` zur Unkenntlichkeit
verkürzt werden. Signalpfade brechen responsiv um; Text darf mehrzeilig werden.
Die Informationshierarchie lautet sichtbar: Seitenkopf → Funktionszone → Status →
Profile/Aktionen → optionale technische Tiefe.
