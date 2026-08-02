# Audiozentrale v2: Produkt- und Zustandsmodell

Dieses Dokument bindet den freigegebenen Produktplan
`docs/plans/audiozentrale-product-v2.md` an einen maschinenlesbaren, zunächst
rein lesenden Vertrag. Der Vertrag verändert weder Audio noch Geräte und
begründet keine Apply-, Aufnahme- oder Instrumentenautorität.

## Kanonische Dateien

- `inventory/audiozentrale-product-model.v1.json`: Objekt-, Navigations-,
  Signaltyp-, Modul- und Modulationsvertrag
- `schemas/audiozentrale-product-model.v1.schema.json`: strukturelles
  JSON-Schema des Produktmodells
- `schemas/audiozentrale-workspace-state.v1.schema.json`: strukturelles
  JSON-Schema eines gespeicherten Workspace
- `profiles/audiozentrale-workspace.example.v1.json`: gültiges, nicht
  ausführbares Beispiel
- `scripts/audio-product-model`: fail-closed Prüfung und Projektion

Das Modell bindet den freigegebenen Plan, beide Schemata und den vorhandenen
Profilkatalog jeweils an Pfad und SHA-256. Der Python-Validator ist die
semantische Autorität. Die JSON-Schemata beschreiben dieselbe versionierte Form,
verwerfen unbekannte Wurzeleigenschaften und werden selbst auf Draft, ID,
Pflichtfelder und Digest geprüft.

## Objektgrenzen

Ein **Setup** ist der vollständige Zusammenhang einer Aufgabe. Höchstens ein
Setup darf `active` sein. Entwürfe und Vorlagen können keinen aktiven Zustand
vortäuschen. Ein aktives Setup benötigt mindestens eine Signalbahn.

Eine **Signalbahn** ist eine geordnete Folge aus Quelle, internen Modulen und
Ziel. Quelle und Ziel tragen einen Signaltyp: `audio-mono`, `audio-stereo` oder
`midi`. Der Validator führt diesen Typ durch jedes Modul. Ein Modul muss den
aktuellen Typ akzeptieren; sein Ausgang bestimmt den nächsten Typ. Die Bahn ist
nur gültig, wenn ihr Endtyp dem Zieltyp entspricht. Freie Ports, Kanten,
Skripte, beliebige Sidechains und Zyklen sind nicht Teil des Vertrags.

Ein **Modul** verweist auf den internen Katalog. Es besitzt stabile Ein- und
Ausgangstypen, Parameter, Latenz- und CPU-Klassen sowie festgelegtes Verhalten
bei internem Fehler und Geräteverlust. Parameter haben stabile IDs, Einheiten,
Grenzen und eine explizite Modulierbarkeit. Ein externer Pluginhost wird damit
nicht behauptet.

Eine **Verknüpfung** ist eine typisierte Modulationsbeziehung. Erlaubte Ziele
sind modulierbare Modulparameter und ausdrücklich modulierbare Bahnenmakros.
Aufnahme, Transport, Ausgabeauswahl, Masterpegel, Panic/Mute und
Sicherheitsaktionen sind ausgeschlossen. Tiefe, Glättung und Anzahl sind
begrenzt.

Eine **Szene** überschreibt nur vorhandene Parameter oder Makros desselben
Setups. Sie kann keine Quelle, Route, Hardware oder Ausgabe erfinden. Doppelte
Overrides werden abgewiesen.

Ein **Take** ist unveränderlich und an eine vorhandene Setup-Signalbahn, deren
Quellenreferenz, ein Format, Zeitstempel und eine explizite Monitoringart
gebunden. Ein finalisiertes Take benötigt Endzeit und Artefaktdigest; ein noch
laufendes Take darf beides nicht tragen. Timeline, Comping und Clipbearbeitung
sind ausgeschlossen. Eine spätere Ableitung erzeugt ein neues Objekt und erhält
das ursprüngliche Take.

## Zwei orthogonale Achsen

Die vier Wahrheitsebenen bleiben vollständig erhalten:

1. `observed` – aktueller Laufzeit-Readback
2. `configured` – versionierter Sollvertrag
3. `physical-open` – menschlich zu bestätigende physische Tatsache
4. `executable` – vorhandene Backendautorität und bestandene Gates

Davon unabhängig sind die Darstellungstiefen `compact`, `expanded` und
`focus`. Kompakt zeigt höchstens zwei Primäraktionen, erweitert höchstens acht;
es darf nur einen Fokus geben. Eine tiefere Ansicht macht eine Aussage nicht
wahrer oder ausführbarer.

## Migration der bisherigen Oberfläche

| bisher | primäres Ziel | zusätzliche Zielorte |
|---|---|---|
| Übersicht (`start`) | Jetzt | – |
| Hören | Jetzt | Setups |
| Spielen | Jetzt | Setups |
| Aufnehmen | Jetzt | Bibliothek |
| System | System | – |

Die Migration betrifft Navigation und Darstellung, nicht die Backendautorität.
Browser-Fallback und spätere App-Shell sollen dieselbe Frontendbasis und denselben
Zustandsvertrag verwenden.

## Fail-closed Lesen

JSON-Dateien werden mit fester Größenobergrenze über eine symlinkfreie
Verzeichniskette geöffnet und aus demselben Dateideskriptor gelesen. Der Reader
verwirft Änderungen während des Lesens, doppelte Schlüssel, nichtendliche
Zahlen, ungültiges UTF-8 und Nicht-Objekt-Wurzeln. Unbekannte Felder,
ungültige Typketten oder nicht auflösbare Referenzen führen zu einem
kontrollierten Vertragsfehler.

## Prüfung

```bash
./scripts/audio-product-model check
./scripts/audio-product-model validate profiles/audiozentrale-workspace.example.v1.json
```

Diese Prüfung ist offline und read-only. Sie startet keinen Dienst, verändert
keinen PipeWire-Graphen und aktiviert weder Aufnahme noch Instrument.
