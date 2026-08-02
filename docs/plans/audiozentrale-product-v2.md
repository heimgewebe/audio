# Produktplan: Audiozentrale v2

- Version: 2
- Datum: 2026-08-02
- Status: **Produkt- und Architekturplan; keine Audio- oder Systemmutation**
- Basisrevision: `origin/main@17c316d8acbeba41e085e6a422192fda5548fc47`
- Arbeitsname: **Audiozentrale**
- Geltungsbereich: Produktoberfläche, Echtzeitdarstellung, Klangbearbeitung,
  Aufnahme, Wiedergabe, Instrumente und spätere Hardwareerweiterung
- Nicht ersetzt: aktuelle Sicherheits-, Profil-, Aufnahme-, Deployment- und
  Systemwahrheitsverträge

## 1. Ergebnis der vollständigen Planrevision

Der frühere Masterplan war als Ideensammlung nützlich, aber als Produktplan zu
breit, zu begriffslastig und zu früh technisch festgelegt. Er vermischte eine
DAW, einen modularen Synthesizer, eine Systemsteuerung, eine Pluginplattform,
KI-Werkzeuge und eine neue Produktkategorie. Das hätte sehr wahrscheinlich zu
einem eindrucksvollen, aber schwer bedienbaren und kaum fertigstellbaren System
geführt.

Die neue Entscheidung lautet:

> Die Audiozentrale wird eine adaptive, lokale Live-Audioumgebung, in der ein
> Mensch einen realen Signalweg sofort versteht, hört, verändert, aufnimmt und
> zuverlässig wiederherstellt.

Sie ist weder bloß Dashboard noch vollständige DAW. Sie beginnt mit wenigen
vollständigen Abläufen und wächst nur dort in die Tiefe, wo eine echte Aufgabe
dies verlangt.

## 2. Audit des früheren Plans

| Frühere Annahme | Kritik | Neue Entscheidung |
|---|---|---|
| „Klangbetriebssystem“ als Produktkategorie | groß, abstrakt und nicht selbsterklärend | Arbeitsname Audiozentrale; Nutzen wird über konkrete Abläufe erklärt |
| „für jedermann“ als unmittelbare Zielgruppe | nicht testbar; Hörer, Gitarristen, Produzenten und Live-Musiker brauchen unterschiedliche Dinge | zuerst Heimmusiker mit Interface, Instrument oder Mikrofon; einfache Bedienung bleibt allgemeines Prinzip |
| „Klangwelt“ als zentrales Objekt | poetisch, aber im Alltag mehrdeutig | **Setup** als verständliche Einheit aus Quellen, Bearbeitung, Zielen und Szenen |
| vier feste Aufgabenbereiche Hören, Spielen, Aufnehmen, Formen | dieselbe Quelle taucht mehrfach auf; Wechsel zerreißt den Zusammenhang | ein aktives Setup; Hören, Spielen und Aufnehmen sind Aktionen innerhalb desselben Signalwegs |
| vier Tiefen Kapsel, Arbeitsfläche, Werkstatt, Graph | zu viele Zustandswechsel und schwer lernbar | drei Ebenen: kompakt, inline erweitert, fokussiert |
| freier Graph als höchste Wahrheit | Kabelgewirr, schlechte Touch-Bedienung, hohe Fehlerrate | geordnete Signalbahnen sind primär; Routinggraph zunächst nur read-only und diagnostisch |
| „Reality Engine“ | unbelegtes Versprechen und unnötig mystisch | deterministische **Ableitungen und Verknüpfungen** mit sichtbarer Ursache |
| sechs Grundoperationen Formen, Kombinieren, Ableiten, Morphen, Verzweigen, Einfrieren | zu viele neue Verben vor einem bewiesenen Kernablauf | vier Grundhandlungen: **Hören, Spielen, Aufnehmen, Verknüpfen**; Speichern und Varianten sind Systemverhalten |
| KI- und ML-Stufen im Produktplan | lenkt vom echtzeitfähigen Kern ab, erzeugt Datenschutz-, Qualitäts- und Lizenzfragen | keine KI- oder ML-Funktion im v2-Plan; spätere Aufnahme nur durch eigene Entscheidung und belegten Nutzen |
| externe Plugins früh hosten | vervielfacht Crash-, UI-, Lizenz- und Kompatibilitätsprobleme | v1 nur interne, kontrollierte Module; Pluginhosting ist ein späteres, eigenes Produktgate |
| CLAP als frühe Implementierungsentscheidung | sinnvoller Standard, aber noch kein belegter Bedarf für v1 | Parameter- und Ereignismodell bleibt kompatibel denkbar; Host erst nach internem Modulvertrag |
| MIDI 2.0 sofort umsetzen | aktuelles Roland-Gerät und Marktbestand nutzen überwiegend MIDI 1.0 | internes Modell erlaubt hochauflösende und notenbezogene Daten; erster Adapter bleibt MIDI 1.0 |
| Tauri als fertige Zielentscheidung | gute Kandidatenlösung, aber WebView-, Packaging- und IPC-Fragen sind ungemessen | Tauri ist bevorzugter Shell-Kandidat; ein Spike entscheidet gegen native Alternativen |
| eigener vollständiger Audioengine-Kern sofort | größter Kosten- und Risikoblock | zuerst Metering- und Ein-Signalweg-Spike; Engineumfang wächst nur mit bewiesenen vertikalen Schnitten |
| freie Oberflächengestaltung | macht Support, Dokumentation und Konsistenz schwierig | feste Bediengrammatik; Nutzer dürfen später relevante Kapseln anheften, nicht die gesamte App neu bauen |
| klassische DAW-Timeline | wäre ein zweites Produkt | v1 besitzt Takes, Loops und einfache Regionen, aber kein vollständiges Arrangement |
| Szenen | für Live-Spiel und reproduzierbare Zustände tatsächlich wertvoll | bleibt Kernfunktion, aber erst nach clickfreien Übergängen und Recovery |
| Variantenbaum mit Provenienz | wertvoll, aber für v1 überdimensioniert | v1 speichert benannte Snapshots und A/B; Baumdarstellung erst bei realem Bedarf |
| permanente spektakuläre Visualisierung | Gefahr dekorativer Last und schlechter Übersicht | Bewegung zeigt ausschließlich Signal, Modulation, Transport oder Fehlerzustand |
| Cross-Plattform ab Beginn | verlangsamt die erste belastbare Audioengine | Linux und PipeWire zuerst; Plattformadapter werden architektonisch getrennt |
| Marketplace, Sharing und Cloud | kein Beleg für frühen Produktnutzen | außerhalb des Plans; lokaler Betrieb ohne Konto ist Grundvertrag |
| generative Musik | verändert Produktidentität und Vertrauensmodell | ausgeschlossen |

## 3. Produktvertrag

### 3.1 Drei Garantien

1. **Unmittelbar:** Ein unterstütztes Setup erzeugt nach klarer Auswahl ohne
   Routingarbeit einen hörbaren oder aufnahmebereiten Zustand.
2. **Erklärbar:** Jeder hörbare Eingriff, jede Modulation und jede Route ist in
   der Oberfläche sichtbar und auf eine Ursache zurückführbar.
3. **Wiederherstellbar:** Geräteverlust, App-Neustart und abgebrochene Aktionen
   hinterlassen einen lesbaren Zustand und keinen überraschenden Audioweg.

### 3.2 Erster Marktkeil

Der erste zahlende Kernmarkt sind allein oder in kleinen Räumen arbeitende
Heimmusiker, die ein Instrument oder Mikrofon ohne DAW-Konfiguration sofort
hören, bearbeiten und sicher aufnehmen wollen. Hochwertiges Musikhören ist ein
wichtiger Begleitmodus desselben Systems, aber keine zweite gleichrangige
Markteintrittsthese.

Die erste öffentliche Hardwarefreigabe bleibt auf konkret verifizierte Geräte
und Signalwege begrenzt. Generische class-compliant Interfaces folgen erst,
wenn Fähigkeiten, Kanalrollen, Pegelgrenzen und Geräteverlustverhalten
zuverlässig erkannt werden. Zum Zielbestand gehören schrittweise:

- Mikrofon;
- MIDI-Keyboard oder Digitalpiano;
- E-Gitarre oder E-Bass;
- hochwertige Kopfhörer oder Lautsprecher.

Das erste Produkt muss nicht jeden Produktionsstil abdecken. Es muss die
folgenden vollständigen Wege außergewöhnlich gut beherrschen:

1. hochwertige Wiedergabe;
2. Stimme oder Instrument sicher aufnehmen;
3. Piano oder Gitarre latenzarm spielen;
4. Signale musikalisch miteinander verknüpfen;
5. einen Zustand als Setup und Szene zuverlässig wieder öffnen.

### 3.3 Nichtziele bis zum bewiesenen Kern

- keine vollständige DAW;
- keine generative KI und kein Chat als Bedienform;
- kein Cloudkonto;
- kein Plugin-Marktplatz;
- kein beliebig programmierbarer Modulargraph;
- keine soziale Plattform;
- kein automatisches Masteringversprechen;
- keine behauptete Hardwarekontrolle, wenn ein physischer Regler nicht
  softwareseitig lesbar oder steuerbar ist.

## 4. Das mentale Modell

### 4.1 Setup

Ein **Setup** enthält genau den Zusammenhang, den ein Mensch für eine Aufgabe
braucht. Zu jedem Zeitpunkt ist höchstens ein Setup als hörbarer und
wirkungsfähiger Zustand aktiv; andere Setups sind Vorlagen oder gespeicherte
Entwürfe.

Ein Setup enthält:

- erkannte oder erwartete Geräte;
- eine oder mehrere Signalbahnen;
- Bearbeitungsmodule;
- typisierte Modulationsverknüpfungen;
- Ausgänge und Monitoring;
- Aufnahmeziele und zugehörige Takes;
- Szenen;
- physisch noch zu bestätigende Fakten.

Beispiele:

- `Referenzhören · Focal`
- `Stimme · trocken aufnehmen`
- `Roland · Piano und MIDI`
- `Roland · Buckelwal`
- `Gitarre · Clean und Raum`
- `Pioneer · Wohnzimmer`

### 4.2 Signalbahn

Eine Signalbahn ist die primäre visuelle und technische Einheit:

```text
Quelle → Bearbeitung → Ziel
```

Sie kann intern mehrere Module enthalten, bleibt in der Übersicht aber eine
lesbare Zeile. Mehrere Bahnen münden in einen benannten Ausgangsbus oder Master.
Seitliche Abzweigungen, Sidechains und Modulationen werden erst bei Bedarf
sichtbar. Flüchtige PipeWire-Knoten werden dabei nicht als stabile
Produktobjekte ausgegeben.

### 4.3 Modul

Ein Modul ist eine begrenzte Funktion mit:

- klaren Ein- und Ausgängen;
- typisierten Parametern;
- Latenz- und CPU-Angabe;
- Bypass;
- gespeichertem Zustand;
- sichtbaren Modulationsquellen;
- definiertem Verhalten bei Fehler oder Geräteverlust.

Bis einschließlich der kommerziellen Version 1 werden ausschließlich interne
Module genutzt. Sie liegen in einer linearen, typgeprüften Kette mit wenigen
klaren Einfügepositionen. Nutzer können passende Module ergänzen, umgehen und
umordnen; Zyklen, freie Ports und beliebige Sidechains entstehen daraus nicht.
Der erste Satz umfasst Gain, Filter, Kompressor, Gate, Verzerrung, Delay, Hall,
Looper, Tuner, Meter und Walstimme. Ein allgemeiner Sampler gehört erst zur
Ableitungsphase nach Version 1.

### 4.4 Verknüpfung

Eine **Verknüpfung** ist eine typisierte, allowlistete Steuerbeziehung. Sie
lässt eine messbare oder gespielte Eigenschaft einen anderen Parameter
beeinflussen, erzeugt aber keine beliebige Audio- oder MIDI-Route:

```text
Lautstärke der Stimme → Hallgröße
Gitarrentransienten   → Delay-Feedback
Sustainpedal          → Wal-Artikulation
Piano-Velocity        → Filteröffnung
LFO                    → Stereobewegung
```

Jede Verknüpfung zeigt Quelle, Ziel, Richtung, Tiefe, Glättung und aktuellen
Einfluss. Ein bewegter Regler muss jederzeit erklären können, warum er sich
bewegt. In der ersten kommerziellen Fassung dürfen Verknüpfungen weder
Aufnahme, Transport, Ausgabeziel, Masterpegel noch Sicherheitsfunktionen
steuern. Freie Kabel, Skripte und nicht typisierte Ziele sind ausgeschlossen.

### 4.5 Szene

Eine Szene ist ein benannter, vorbereiteter Zustand desselben Setups. Sie darf
keine neue Geräte- oder Routingwahrheit erfinden. Übergänge sind clickfrei,
vorher prüfbar und bei unvereinbaren Änderungen explizit blockiert.

### 4.6 Aufnahme

Eine Aufnahme ist kein bloßer Dateipfad, sondern ein **Take** mit:

- eindeutig gebundener Quelle;
- Rate, Kanälen und Format;
- Start- und Endzeit;
- Monitoringart;
- verwendetem Setup und Szene;
- finalisiertem oder wiederherstellbarem Dateistatus.

Ein Take ist zunächst eine unveränderliche, einzeln abspielbare Aufnahme. Die
erste kommerzielle Fassung besitzt keine Timeline, kein Comping und keine
Clipbearbeitung. Schneiden, Loopen oder Ableiten erzeugt später ein neues
Objekt; das ursprüngliche Take bleibt erhalten.

## 5. Informationsarchitektur

Die Produktoberfläche besitzt vier stabile Hauptorte:

| Ort | Zweck |
|---|---|
| **Jetzt** | aktives Setup, Live-Signal, Transport, Aufnahme, Szenen und direkte Bearbeitung |
| **Setups** | Vorlagen, eigene Setups, Hardwarevarianten und sichere Vorschau |
| **Bibliothek** | Aufnahmen, Takes, Loops, eingefrorene Klänge und gespeicherte Zustände |
| **System** | Geräte, Signalwahrheit, Latenz, XRuns, Deployment, Speicher, Diagnose und physische Belege |

Die bisherige Audiozentrale bleibt als System- und Fallbackoberfläche erhalten.
Sie wird nicht parallel als zweites Produkt weitergebaut. Dieselbe
Frontend-Codebasis wird schrittweise migriert und kann sowohl in der App-Shell
als auch im lokalen Browser laufen. Ihr Kontroll- und Wahrheitsmodell speist
die Produktoberfläche; technische Details wandern unter `System`.

Die heutige Fünferstruktur wird ausdrücklich abgebildet:

| Heutiger Bereich | Ziel in v2 |
|---|---|
| Übersicht | Statusleiste und Zusammenfassung unter `Jetzt` |
| Hören | hörbezogene Setups und Aktionen unter `Jetzt` und `Setups` |
| Spielen | instrumentbezogene Setups und Aktionen unter `Jetzt` und `Setups` |
| Aufnehmen | Aufnahmeaktion im aktiven Setup sowie Takes in `Bibliothek` |
| System | bleibt `System` und übernimmt die technische Tiefe |

Die vier Wahrheitsebenen `beobachtet`, `konfiguriert`, `physisch offen` und
`ausführbar` bleiben unverändert erhalten. Sie beschreiben die Belegart eines
Zustands. Die drei Darstellungsebenen beschreiben dagegen nur, wie viel Detail
zu sehen ist. Beide Achsen dürfen niemals ineinander umgedeutet werden.

## 6. Bedienoberfläche

### 6.1 Grundlayout auf dem Desktop

```text
┌──────────────────────────────────────────────────────────────────┐
│ Setup       Szene     Transport       ● Aufnahme       Systemzustand │
├──────────┬──────────────────────────────────────────┬────────────┤
│ Jetzt    │ Roland  → Piano → Raum        ▂▅▇▆▃     │ Kontext     │
│ Setups   │ Stimme  → Gate  → Hall        ▂▃▅▂      │ Inspektor   │
│ Biblioth.│ Gitarre → Amp   → Delay       ▂▆▇▅      │ oder Fokus  │
│ System   │──────────────── Master ────────────────  │             │
│          │ Szenen: [Nah] [Groß] [Wal] [Stumm]      │             │
└──────────┴──────────────────────────────────────────┴────────────┘
```

Auf Tablet und kleinen Displays werden Navigation und Inspektor zu festen
Sheets. Der aktive Signalweg bleibt stets sichtbar.

### 6.2 Drei Darstellungsebenen

#### Ebene 1 – kompakt

Eine Signalbahn zeigt Name, Quelle, Ziel, Livepegel, Status und höchstens zwei
wichtige Makros.

#### Ebene 2 – inline erweitert

Ein Klick erweitert genau diese Bahn an Ort und Stelle. Sichtbar werden Module,
größere Meter, Verknüpfungen, Monitoring und aufgabenbezogene Aktionen. Andere
Bahnen bleiben als Kontext erhalten.

#### Ebene 3 – Fokus

Ein Modul oder eine Aufgabe übernimmt die Hauptfläche: Wellenform,
Spektrumanzeige, Parameter, Aufnahme-Takes oder Modulationsdetails. Ein
Breadcrumb hält Setup und Signalbahn sichtbar. Es gibt immer nur einen Fokus.

Ein frei editierbarer Graph ist **keine vierte Ebene**. Eine automatisch
geordnete Graphansicht kann später unter System oder Expertenwerkzeugen
erscheinen.

### 6.3 Bedienregeln

- Kein wichtiger Zustand wird nur durch Farbe vermittelt.
- Jeder Parameter besitzt Wert, Einheit und Rücksetzpunkt.
- Einfache Makros sind sichtbar; technische Einzelparameter liegen eine Ebene
  tiefer.
- Aufklappen verändert nicht unbemerkt den Klang.
- Direkte Manipulation ist nur dort erlaubt, wo sie semantisch eindeutig ist.
- Strukturänderungen wie Route, Modul oder Szene benötigen Backendbestätigung
  und einen autoritativen Folgezustand. Kontinuierliche Parameter erhalten
  dagegen einen von der Engine bestätigten angewendeten Wert; ein vollständiger
  Systemscan pro Reglerbewegung wäre falsch.
- Undo gilt für kreative und strukturelle Änderungen, nicht für bereits
  geschehenes Audio, physische Handlungen oder Sicherheitsaktionen.
- `Panic/Mute` bleibt in jeder Produktoberfläche erreichbar; die Wirkung gehört
  dem Backend und darf nicht vom Überleben des UI-Threads abhängen. Der physische
  Lautstärkeregler bleibt der letzte unabhängige Rückfallweg.
- Die Oberfläche zeigt keine Regler für physische Hardwarewerte, die sie nicht
  tatsächlich lesen oder verändern kann.

### 6.4 Livevisualisierung

Die gesamte Anwendung wird nicht periodisch neu aufgebaut. Es gibt drei
getrennte Aktualisierungsklassen:

| Klasse | Inhalt | Verhalten |
|---|---|---|
| Systemwahrheit | Geräte, Profile, Prozesse, Deployment | ereignisgesteuert plus langsamer Reconciliation-Fallback |
| Audiotelemetrie | Peak, RMS, MIDI, Transport, XRuns, CPU | begrenzter Stream mit etwa 20 bis 60 Aktualisierungen pro Sekunde |
| Darstellung | Meter, Kurven und Übergänge | bildschirmgebunden über Canvas oder GPU-Zeichnung |

Pegel, Modulationsringe und Signalaktivität werden gezielt aktualisiert. Ein
langsam erneuerter System-Snapshot bleibt unabhängig davon bestehen.

Jede Animation muss eine echte Bedeutung besitzen. Dekorative Dauerbewegung,
Pseudo-Wellenformen und erfundene Aktivität sind unzulässig.

### 6.5 Einstieg und leere Zustände

Die Anwendung beginnt nie mit einem leeren Graphen. Beim ersten Start:

1. werden Geräte und belegbare Fähigkeiten read-only erkannt;
2. erscheinen höchstens drei passende Setup-Vorschläge;
3. werden physische Schritte wie Hi-Z, 48 V, Kabel und analoge Lautstärke
   getrennt und verständlich bestätigt;
4. führt ein kurzer Pegel- oder Funktionstest zum ersten hörbaren Ergebnis;
5. wird das bestätigte Setup lokal gespeichert.

Unbekannte Hardware erhält keine erfundene Automatik. Sie kann zunächst
beobachtet und manuell beschrieben werden, bleibt aber bis zu einem bestandenen
Profilvertrag nicht ausführbar. Konto, Cloud und Produktführungstour sind keine
Voraussetzung für den ersten Klang.

## 7. Gestufter Produktumfang

Die folgenden Fähigkeiten beschreiben das Produktprogramm, nicht den Umfang
einer einzigen ersten Lieferung. Die kommerzielle Version 1 umfasst erst nach
den Gates der Phasen 1 bis 6: Livewahrnehmung, Referenzhören, einen vollständigen
Stimm- und Rolandweg, eine begrenzte interne Modulation, E-Gitarre sowie
Produktabnahme. Weitergehende Ableitungen folgen nach Version 1.

### 7.1 Hören

- tatsächlicher Wiedergabeweg und Ziel;
- Stereo-Peak und RMS;
- Rate, Format und Resamplingstatus, soweit belegbar;
- Referenzmodus ohne unerwartete Bearbeitung;
- A/B zwischen vorbereiteten, kompatiblen Zielen;
- konfigurierbare digitale Pegelobergrenze und sofortiges Mute; sie garantiert
  wegen analoger Verstärkung und Kopfhörerwirkungsgrad keinen sicheren
  Schalldruck;
- Focal-, Pioneer- und Bluetooth-Setups mit klar unterschiedlichem Anspruch.

### 7.2 Spielen

- MIDI- und Audioaktivität;
- Velocity, Sustain und Pitch Bend;
- Tuner für Gitarre;
- interne Instrumente und Effekte;
- Looper;
- Szenen;
- CPU-, Quantum-, Latenz- und XRun-Anzeige;
- Walstimme als erstes besonderes Instrument, aber nicht als Sonderarchitektur.

### 7.3 Aufnehmen

- große, gut lesbare Eingangspegel;
- Zielbereich und Clip-Hold;
- eindeutige Quelle und Kanalbindung;
- sichtbare Monitoringart;
- ein Aufnahmebefehl und ein Stopbefehl;
- laufende Wellenform ohne Audiobearbeitung im UI;
- Takes, Benennung, Anhören und sichere Finalisierung;
- Recovery nach App-, Prozess- oder Geräteabbruch;
- Audio und MIDI beim Roland gemeinsam, sofern beide Verträge bestanden sind.

### 7.4 Verknüpfen und Formen

V1 unterstützt eine kleine, hochwertige Auswahl:

**Quellen**

- LFO;
- Hüllkurve;
- Envelope Follower;
- Transientenimpuls;
- MIDI CC, Velocity, Sustain und Pitch Bend;
- Schrittfolge;
- begrenzter Zufall.

**Ziele der ersten kommerziellen Fassung**

- Parameter interner Module;
- begrenzte Makros einer Signalbahn.

Aufnahme, Transport, Ausgabeauswahl, Masterpegel, Panic/Mute und andere
sicherheitskritische Aktionen sind keine Modulationsziele. Szenenwechsel bleiben
explizite Nutzer- oder Controlleraktionen mit eigener Transitionsemantik.

**Ableitungen nach Version 1, weiterhin ohne KI**

- Aufnahme in Segmente schneiden;
- Loopregion bilden;
- Transienten als Trigger verwenden;
- Tonhöhen- oder Lautstärkekontur als Modulationsquelle nutzen;
- Sample chromatisch oder perkussiv spielbar machen;
- spektrales Material in Wavetable oder Filterprofil überführen;
- komplexen Zustand als Audio oder internes Instrument einfrieren.

## 8. Technische Architektur

### 8.1 Stabile Prozessgrenzen

```text
Produktoberfläche / App-Shell
        │ Befehle, Zustand, Telemetrie
        ▼
Control Plane
        │ autorisierte Transitionen, Geräte- und Recoverywahrheit
        ├──────────────────────────────┐
        ▼                              ▼
Echtzeit-Engine                  Offline-/Analyseworker
Audio, MIDI, DSP, Metering       Wellenform, Export, Ableitungen
        │
        ▼
PipeWire-/ALSA-/MIDI-Adapter
```

- Die UI verarbeitet keine Audiopuffer.
- Die Echtzeit-Engine läuft getrennt von der UI; ein UI-Absturz darf den Ton
  nicht sofort beenden.
- Der bestehende Python-Control-Dienst bleibt zunächst Autorität für
  Systemwahrheit, Profile, Gates und Recovery.
- Die Echtzeit-Engine erhält nur typisierte, versionierte Befehle.
- Analyse und Dateioperationen laufen nicht im Echtzeitthread.

### 8.2 Noch nicht festgelegte Technik

Folgende Entscheidungen werden durch kleine Spikes und Messungen getroffen:

| Frage | Kandidaten | Entscheidungsbeleg |
|---|---|---|
| App-Shell | Tauri 2, native Linux-Shell | Packaging, Barrierefreiheit, Startzeit, IPC und WebView-Stabilität |
| UI-Framework | TypeScript mit kleinem Komponentenmodell | Telemetrie-Replay, Fokusführung, Testbarkeit und Renderbudget |
| Engine-Sprache | Rust oder C++ | PipeWire-Integration, Echtzeitverhalten, DSP-Ökosystem und Wartbarkeit |
| Telemetrie | binärer WebSocket, später Shared Memory | CPU, Latenz, Verlustverhalten und Browser-Fallback |
| Pluginstandard | zunächst keiner; später CLAP prüfen | interner Modulvertrag, Crashisolation und Nachfrage |

Tauri ist aktuell ein plausibler Kandidat, weil seine Capability- und
Permissionmodelle die IPC-Oberfläche begrenzen können. Dies ist ein Argument
für einen Spike, keine vorweggenommene Produktentscheidung.

PipeWire bleibt der erste Linux-Adapter. Sein Knoten-, Port- und Linkmodell darf
jedoch nicht ungefiltert zum Nutzerobjektmodell werden. Die Audiozentrale zeigt
stabile Signalbahnen und übersetzt flüchtige Systemobjekte in verständliche,
identitätsgebundene Zustände.

### 8.3 Echtzeitvertrag

- keine blockierende I/O, Speicherallokation oder Protokollserialisierung im
  Audiocallback;
- begrenzte, vorallokierte Queues;
- Telemetriedaten dürfen veralten und verworfen werden, Audio- und
  Zustandsbefehle nicht;
- clickfreie Parameter- und Szenenübergänge;
- deterministische Reihenfolge gleichzeitiger Ereignisse;
- bekannte und sichtbare Modullatenz;
- bei unauflösbarem Geräte- oder Graphfehler veranlasst ein getrennter
  Supervisor kontrolliertes Mute oder den Abbau des betroffenen Pfads;
- kein automatisches Wiederverbinden auf einen unerwarteten Ausgang.

### 8.4 Zukunftskompatibilität ohne Vorabkomplexität

- Parameter besitzen stabile IDs, Einheiten, Bereiche und Modulationsvertrag.
- Ereignisse können notenbezogene Identität tragen, auch wenn der erste Adapter
  MIDI 1.0 ist.
- Module geben Latenz, Audioports, Notenports und gespeicherten Zustand an.
- Diese Eigenschaften erleichtern später CLAP- oder MIDI-2.0-Anbindung, ohne sie
  in v1 zu implementieren.

## 9. Hardwarestrategie

### 9.1 Unterstützungsstufen

| Stufe | Bedeutung |
|---|---|
| **verifiziert** | exakte Geräteidentität, getestetes Setup, bekannte Grenzen und Laborbeleg |
| **kompatibel** | standardkonformes Gerät mit generischem Profil, aber ohne vollständige Produktabnahme |
| **beobachtet** | Gerät sichtbar, Fähigkeiten oder physische Schaltung unvollständig |
| **unbekannt** | keine sichere automatische Konfiguration |

Die App darf aus `beobachtet` oder `unbekannt` keinen verifizierten Signalweg
ableiten. Jedes neue Hardwareprofil benötigt vor seiner Aufnahme in eine
Produktphase Baseline, Geräteidentität, Kanal- und Pegelvertrag,
Geräteverlustverhalten, Rückfallweg und Negativtests. Das gilt ausdrücklich für
die E-Gitarre.

### 9.2 Erste Hardwareprofile

- MOTU M2 als Ein- und Ausgang;
- RØDE NT1-A als physisch zu bestätigender Mikrofonweg mit 48-V-Gate;
- Roland FP-30X für MIDI und später gemeinsam gebundenes USB-Audio;
- Lake People G111 Mk2 und Focal Clear MG als dokumentierte analoge Hörkette;
- Pioneer VSX-830-K als separates Wiedergabeprofil;
- 1MII B03 Pro als Komfortweg mit ausdrücklich nicht behaupteter
  Referenzqualität;
- E-Gitarre über MOTU-Hi-Z mit physischer Eingangs-, Gain- und
  Rückkopplungsprüfung.

## 10. Sicherheit und Verlässlichkeit als Produktfunktion

- backendgebundenes Master-Mute und definierte digitale Pegelrampe bei Start,
  Stop und Gerätewechsel; dies ist keine Garantie für den analogen Schalldruck;
- bekannte Softwarezyklen werden blockiert; bei möglicher akustischer
  Rückkopplung warnt das System und hält Panic/Mute bereit, behauptet aber keine
  allgemeine Feedbackerkennung;
- keine automatisch erzeugten Zyklen im Signalgraphen;
- Ausgabeziel muss vor Wirkung eindeutig gebunden sein;
- Hardwareverlust stoppt oder mutet den betroffenen Pfad kontrolliert;
- Aufnahmen werden atomar finalisiert oder als recoverable markiert;
- automatische lokale Zustandsicherung ohne Audio- oder MIDI-Inhalte in Logs;
- keine Cloudübertragung;
- keine Änderungen aus einem veralteten UI-Snapshot;
- physische Tatsachen bleiben getrennt von beobachteter Softwarewahrheit;
- Produktionsupdates verändern keinen laufenden Performancezustand.

## 11. Umsetzungsplan und Verhältnis zu bestehenden Gates

Dieser Plan ersetzt die produktseitige Informationsarchitektur und die noch
nicht umgesetzte kreative Ausbaufolge der bisherigen UI-Spezifikation. Er
ersetzt **nicht** deren bereits implementierte Stufe 1, den
Profiltransitionvertrag, Recordervertrag, Produktionsgraph, Labor-Gates oder
die Audio-Sicherheitsregeln. Eine Funktion schreitet nur fort, wenn sowohl ihr
Produktgate dieses Plans als auch ihr bestehendes operatives Gate bestanden
sind. Damit existieren keine zwei konkurrierenden Wahrheiten über
Ausführbarkeit.

### Phase 0 – Produktbeweis ohne Audiomutation

**Ergebnis**

- klickbarer Prototyp für `Jetzt`, `Setups`, `Bibliothek` und `System`;
- vollständige Primärabläufe für Referenzhören, Stimme und Roland;
- Wal und Gitarre als gezielte Belastungsproben für ungewöhnliche Instrumente
  und spätere Erweiterbarkeit;
- getestete Bediengrammatik mit drei Darstellungsebenen;
- Telemetrie-Replay mit realistischen Peak-, MIDI-, XRun- und
  Geräteverlustdaten;
- Architekturspikes für App-Shell, Telemetrie und Engine-Sprache.

**Gate**

Mindestens fünf externe Testpersonen können ohne Erklärung ein Setup öffnen,
eine Quelle erkennen, Aufnahmebereitschaft beurteilen und eine Verknüpfung
zurückverfolgen. Diese formative Zahl entscheidet über Bedienprobleme, nicht
über Marktfit. Vor einer kommerziellen Freigabe werden Nachfrage,
Nutzungshäufigkeit und Zahlungsbereitschaft separat mit Menschen außerhalb des
Projekts belegt. Technikentscheidungen sind durch Messprotokolle statt
Vorlieben gebunden.

### Phase 1 – Lebendige read-only Audiooberfläche

**Ergebnis**

- echte Livepegel für ausgewählte PipeWire-Quellen und Ziele; der passive
  Beobachter darf einen eigenen reversiblen PipeWire-Knoten oder Link anlegen,
  verändert aber keine Defaults oder produktiven Routen;
- MIDI-Aktivität des Roland;
- Graph- und Geräteereignisse ohne 8-Sekunden-Neurendering;
- Canvas-basierte Meter und Wellenform-Replay;
- App-Shell-Prototyp und Browser-Fallback aus demselben gebauten
  Frontendpaket;
- keine neue wirkende Audioaktion.

**Gate**

Ein achtstündiger passiver Soak sowie eine Stunde aktiver Last bestehen mit
begrenzten Queues, ohne anwendungsverursachte XRuns, ohne vollständigen
DOM-Neuaufbau pro Telemetrieframe und mit korrekter Degradation bei
Verbindungsverlust.

### Phase 2 – Ein vollständiger Aufnahmeweg

Der Stimmweg kommt zuerst, weil dafür bereits der am weitesten gehärtete
Recorder- und Recoveryvertrag existiert und weil er Aufnahme, Pegel,
Monitoring, Datei und Geräteverlust in einem überschaubaren vertikalen Schnitt
prüft.

**Ergebnis**

- Voice-Setup mit MOTU und RØDE;
- Livepegel, Monitoringwahrheit, Start, Stop, Take und Wiedergabe;
- Recorder-Recovery in der Produktoberfläche;
- sichere Pegel- und Speichergrenzen.

**Gate**

Quelle, Rate, Kanal, Format und Datei sind gebunden; regulärer Stop, Abbruch,
App-Neustart und Geräteverlust liefern jeweils einen eindeutigen Zustand.

### Phase 3 – Ein vollständiger Spielweg

**Ergebnis**

- Roland-Piano und Buckelwal als zwei Szenen oder verwandte Setups;
- MIDI- und Audiotelemetrie;
- clickfreie Szenenwechsel;
- Sustain, Velocity und Pitch Bend sichtbar;
- einfacher Looper.

**Gate**

Gemessene Latenz liegt im festgelegten Spielbudget, der Dauertest erzeugt keine
XRuns, und Rückkehr zum Referenzhörprofil hinterlässt keinen niedrigen
Quantum- oder Routingzustand.

### Phase 4 – Interne Modulation

**Ergebnis**

- kleiner Satz interner Effekte und Instrumentmodule;
- LFO, Envelope Follower, MIDI und Schrittfolge;
- Drag- oder Auswahlworkflow zum Verknüpfen;
- sichtbare Einflusszerlegung pro Parameter;
- A/B und Undo.

**Gate**

Jede hörbare Parameterbewegung ist erklärbar, begrenzt und reproduzierbar. Kein
Modulator kann unbemerkt Masterpegel, Ausgabeziel oder sicherheitskritische
Aktion übernehmen.

### Phase 5 – E-Gitarre

**Ergebnis**

- Hi-Z-Onboarding, Tuner, Gate, Kompressor, Verzerrung, Amp/Cab-ähnliche interne
  Verarbeitung, Delay und Hall;
- Fuß- oder MIDI-Steuerung;
- Szenen für Clean, Drive und Raum;
- Blockade bekannter Softwarezyklen, Pegelrampe und stets erreichbares
  Panic/Mute; keine Behauptung allgemeiner akustischer Feedbackerkennung.

**Gate**

Physische Eingangsart und Gain sind bestätigt, Latenz und Rauschen gemessen,
Szenenwechsel clickfrei und der digitale Ausgangspegel begrenzt. Akustische
Rückkopplung wird durch Testaufbau und Bedienvertrag minimiert, nicht als
vollständig automatisch erkennbar behauptet.

### Phase 6 – Produktabnahme und kommerzielle Version 1

**Ergebnis**

- signiertes Paket und sichere Updates;
- Onboarding für verifizierte Hardware und klar gekennzeichnete generische
  Kompatibilität nur nach bestandenem Adaptergate;
- Crash- und Recoverytests;
- Barrierefreiheits- und Touchabnahme;
- lokaler Diagnoseexport; eine Übermittlung verlässt das Gerät nur nach einer
  späteren, ausdrücklichen Produkt- und Datenschutzentscheidung;
- öffentliche Hardware-Kompatibilitätsmatrix.

**Gate**

Kein offener Datenverlust-, digitaler Pegel-, bekannter Feedbackpfad-,
Gerätewechsel- oder Recoveryblocker. Der Kernablauf funktioniert ohne Konto und
Internet. Erst dieses Gate begründet eine kommerzielle Version 1.

### Phase 7 – Ableitung und Bibliothek nach Version 1

**Ergebnis**

- Segmentieren, Loopen, Samplen und Einfrieren;
- Tonhöhe, Hüllkurve und Transienten als steuerbare Daten;
- reproduzierbare Snapshots und benannte Varianten;
- Herkunft jedes abgeleiteten Materials.

**Gate**

Originalmaterial bleibt unverändert, jeder Export ist reproduzierbar, und eine
Ableitung kann ohne versteckten Modell- oder Cloudzustand erneut erzeugt werden.

## 12. Messbare Produktkriterien

Die konkreten Schwellen werden in Phase 0 und 1 kalibriert. Folgende Kriterien
sind verbindlich:

### Bedienung

- unterstütztes Setup ohne manuelles Patchen öffnen;
- erste Aufnahme mit höchstens einer vorbereitenden Bestätigung und einem
  Startbefehl;
- aktive Quelle, Ziel und Monitoringart jederzeit sichtbar;
- technischer Graph für den normalen Ablauf nicht erforderlich;
- alle Kernaktionen mit Tastatur und Touch erreichbar.

### Reaktion

- lokale Eingaben erhalten im Zielsystem bei mindestens 95 Prozent der Fälle
  innerhalb von 100 Millisekunden eine sichtbare Annahmebestätigung; Abschluss
  und autoritativer Folgezustand bleiben davon getrennt;
- Pegel und MIDI reagieren visuell kontinuierlich; das Zielbudget für
  Messwert-zu-Anzeige liegt bei höchstens 100 Millisekunden im 95. Perzentil und
  wird vor Festschreibung auf Zielhardware gemessen;
- Telemetrieverlust blockiert keine Audioverarbeitung und wird sichtbar;
- Systemwahrheit wird nach jeder strukturellen Mutation autoritativ neu
  gelesen; kontinuierliche Parameter werden über enginebestätigte Werte
  synchronisiert.

### Audio

- null der Anwendung zurechenbare XRuns im festgelegten Dauertest des
  jeweiligen verifizierten Profils;
- clickfreie sichere Parameter- und Szenenübergänge;
- gemessene statt geschätzte Latenz;
- keine unbeabsichtigte Resampling-, Defaultgerät- oder Quantum-Drift;
- definierter Pegel bei Start, Stop, Fehler und Recovery.

### Zuverlässigkeit

- UI-Neustart ohne Verlust eines laufenden, sicheren Enginezustands;
- ein vom Supervisor erkannter Enginefehler führt zu kontrolliertem Mute oder
  zum Abbau des betroffenen Pfads und zu einem lesbaren Recoveryzustand;
- Geräteverlust und Wiederkehr erzeugen keine unerwartete Route;
- Aufnahme bleibt finalisiert oder ausdrücklich recoverable;
- jeder gespeicherte Zustand ist versions- und migrationsfähig.

## 13. Unmittelbare nächste Arbeitspakete

1. Produktbegriffe, Wahrheitsebenen, Migrationsabbildung und Objektmodell als
   maschinenlesbaren Entwurf festhalten: Setup, Signalbahn, Modul,
   Verknüpfung, Szene und Take.
2. Einen klickbaren, noch read-only Prototyp der neuen `Jetzt`-Ansicht bauen.
3. Eine deterministische Telemetrie-Replaydatei aus synthetischen Daten
   definieren, bevor echte Audioframes angebunden werden.
4. Einen isolierten PipeWire-Meter-Spike für einen Stereoausgang und einen
   Eingang erstellen; eigene Beobachtungsknoten sind vollständig reversibel und
   verändern weder Defaults noch produktive Routen.
5. Tauri gegen eine minimale native Shell anhand eines festen Kriterienbogens
   messen.
6. Voice-Recording als ersten vollständigen vertikalen Schnitt auswählen;
   Roland und Gitarre folgen erst nach dessen Recoverybeleg.
7. Die bestehende Dashboard-Spezifikation von Produktoberfläche und
   Systemfallback klar abgrenzen.

## 14. Forschungs- und Entscheidungsgrundlage

- PipeWire modelliert Audio als Knoten, Ports und Links und kennzeichnet
  Echtzeit-Callbacks ausdrücklich als RT-sicher zu behandeln:
  <https://docs.pipewire.org/group__pw__stream.html>
- Tauri 2 begrenzt Frontendzugriff über Permissions und window-/webviewgebundene
  Capabilities; dies muss im Shell-Spike praktisch geprüft werden:
  <https://v2.tauri.app/security/permissions/>
- CLAP besitzt stabile Plugin-/Host-ABI-Verträge sowie Erweiterungen für
  Parameter, Ports, Zustand, Latenz und Stimmen. Diese Eigenschaften informieren
  das interne Modulmodell, ohne einen frühen Pluginhost zu begründen:
  <https://github.com/free-audio/clap>
- MIDI 2.0 erweitert MIDI 1.0 und priorisiert Rückwärtskompatibilität. Deshalb
  bleibt der erste Adapter MIDI 1.0, während das interne Ereignismodell spätere
  Auflösung und notenbezogene Steuerung nicht verbaut:
  <https://midi.org/midi-2-0>

## 15. Schlussentscheidung

Die Audiozentrale wird nicht durch maximale Funktionszahl besonders. Ihr
Alleinstellungsmerkmal ist die Verbindung aus unmittelbarem Klang, sichtbarer
Ursache, sicherer Hardwarewahrheit und progressiv zugänglicher Tiefe.

Der erste Beweis ist daher nicht ein freier Graph oder eine spektakuläre
Mutation. Der erste Beweis lautet:

```text
Setup öffnen
→ echten Signalweg live sehen
→ sicher hören oder aufnehmen
→ eine verständliche Verknüpfung herstellen
→ Zustand speichern
→ nach Fehler oder Neustart exakt wiederfinden
```

Erst wenn dieser Ablauf überzeugt, darf die Produktfläche breiter werden.
