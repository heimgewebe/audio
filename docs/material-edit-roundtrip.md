# H2-Material: nichtdestruktiver Editor-Roundtrip (lokaler CLI-Schnitt)

## Zweck und Status

V1 ergänzt den vorhandenen unveränderlichen H2-Ingest um genau einen lokalen
Arbeitsablauf für ein ausgewähltes WAV-Mastersegment. Die Audiozentrale
erhält damit einen Bearbeitungsbaustein, aber noch keine Browser-Schaltfläche,
keinen Pluginhost und keinen automatischen DAW-Start.

Ardour und Audacity wurden auf dem Heim-PC als installiert beobachtet.
Eine bestimmte DAW oder deren Projektformat wird nicht zur Datenautorität.

## Ablauf

1. H2-Material zuerst archivieren. Material-ID und Originaldateinamen lassen sich
   über die Bibliotheksoperation des bestehenden Audio-H2-Ingest ermitteln.
2. Eine separate Arbeitskopie vorbereiten:

   python3 scripts/audio-material-edit prepare MATERIAL-ID 170926_191401_MIX.WAV

   Die JSON-Antwort enthält working_copy und expected_render. Der
   H2-Master unter H2-Material/MATERIAL-ID/master bleibt unverändert.

3. working_copy als Spur in Audacity oder Ardour importieren und das Projekt im
   Editor selbst speichern. Das Ergebnis ausdrücklich als Stereo- oder Mono-
   RIFF-WAV namens render.wav in denselben Arbeitsordner exportieren.
   Zugelassen sind PCM 16/24/32 oder Float32, 8–192 kHz.
   input.wav muss unverändert bleiben.
4. Die abgeleitete WAV in der privaten Ablage archivieren:

   python3 scripts/audio-material-edit finish EDIT-ID

   Die Antwort nennt derived_id, den archivierten Audiopfad und SHA-256.

## Daten und Sicherheitsgrenzen

Standardroot: ~/Music/Audio-Aufnahmen/H2-Bearbeitungen/

    H2-Material/MATERIAL-ID/master/...          # niemals bearbeiten
    H2-Bearbeitungen/working/EDIT-ID/
      input.wav                                  # geprüfte separate Arbeitskopie
      render.wav                                 # vom externen Editor erzeugt
      manifest.json                              # eindeutige Quellenbindung
    H2-Bearbeitungen/renders/DERIVED-ID/
      audio.wav                                  # hashgebundenes Rendering
      manifest.json                              # Herkunft inkl. Originalmaster

- Kein Eingriff in H2-SD-Karte, Originalmaster, Annotationen, PipeWire oder Qobuz.
- Vor `prepare` und `finish` verifiziert der bestehende H2-Ingest das
  gesamte Master-Set. Anschließend werden **alle Masterdateien** über den
  geprüften Archiv-Verzeichnisdeskriptor erneut vollständig gehasht;
  die Arbeitskopie und das Rendering erhalten eigene Hashprüfungen.
  Bei vielen oder mehrgigabytegroßen H2-Mastern verursacht das zusätzliche I/O.
- Keine Symlinks, keine beliebige Remote-Dateipfadübergabe, keine Überschreibung
  eines schon archivierten Ergebnisses.
- Verzeichniszugriffe verwenden unter Linux geprüfte `O_NOFOLLOW`-Deskriptoren;
  die Veröffentlichung nutzt `renameat2(RENAME_NOREPLACE)` und bricht ab, falls
  die atomare No-Replace-Operation nicht verfügbar ist. Nach Directory-Swap
  wird ein nicht mehr erreichbares Ergebnis nicht als erfolgreich ausgegeben.
- Wiederholtes finish mit identischem Inhalt ist idempotent. Neue Renderingbytes
  erzeugen einen neuen, an die Ursprungsdatei gebundenen Renderdatensatz.
- Änderungen der Arbeitskopie, ungültiges WAV oder modifizierte Originale
  blockieren die Archivierung.
- Bei **jeder fehlgeschlagenen Vorbereitung oder Veröffentlichung** bleibt das
  private `.audio-edit-staging-*`-Verzeichnis absichtlich erhalten. Auch ein
  Prozessabbruch kann solche Reste hinterlassen. Eine sichere automatische
  Identitätsprüfung bereits vor dem ersten Öffnen ist nicht möglich; daher
  findet keine destruktive Fehlerbereinigung statt.
- Stagingreste können bis zur Größe des kopierten WAVs Speicher belegen.
  Freien Speicher beobachten; Reste nur nach separater Identitäts- und
  Inhaltsprüfung manuell bereinigen. Die CLI löscht keine Nutzerdateien.
- Nur eine lokal explizit ausgeführte CLI darf diesen Dateisystempfad benutzen.
  Die Audiozentrale-Remote-Bridge erhält keinerlei neue Schreibrechte.

## Grenzen / späterer Produkt-Schnitt

Bearbeitungsergebnisse erscheinen noch nicht automatisch in der Browser-
Bibliothek. Dafür braucht es einen getrennten read-only Bibliotheksvertrag,
sicheres Media-Streaming, Sichtbarkeitsregeln für Remote-Clients und
Abnahmetests. Dieser Ausbau rechtfertigt noch keine eigene Audioengine.

Hardware-/GUI-Abnahme mit einer bewusst ausgewählten Testaufnahme bleibt
von synthetischen Regressionstests getrennt.