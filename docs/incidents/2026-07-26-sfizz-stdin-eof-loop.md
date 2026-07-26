# Störfall: sfizz-Standardinput-EOF-Schleife

- Datum: 2026-07-26
- Status: Ursache reproduziert; betroffener Bestand entfernt
- Betroffen: Walgesang, Buckelwal-Stimme, Tierklavier

## Auswirkung

Drei separat gestartete `sfizz_jack`-Sitzungen belegten jeweils praktisch einen
CPU-Kern und erzeugten zusammen 447.004.229.059 Byte Logdaten:

| Sitzung | Loggröße |
|---|---:|
| Walgesang | 249.116.128.847 Byte |
| Buckelwal-Stimme | 145.657.324.633 Byte |
| Tierklavier | 52.230.775.579 Byte |

## Ursache

Die Launcher starteten den interaktiven JACK-Client im Hintergrund, ohne einen
lebenden Standardinput bereitzustellen, und leiteten Standardausgabe und
Fehlerausgabe unbegrenzt in `session.log` um.

Ein begrenzter Reproduktionstest zeigte:

- Standardinput `/dev/null`: 43.537 Prompts `> ` innerhalb von höchstens
  131.072 Ausgabebyte;
- dauerhaft offengehaltener Standardinput: ein Prompt und insgesamt 491 Byte.

Damit ist die Fehlerkette belegt:

1. der Prozess liest seine Textschnittstelle über Standardinput;
2. EOF wird nicht terminal behandelt;
3. der Prompt wird ohne Blockierung erneut ausgegeben;
4. die unbeschränkte Dateiumleitung macht die Schleife zum Speicherstörfall.

## Entfernte Angriffsfläche

- drei Launcher
- drei Desktop-Launcherfamilien
- drei Sample-/SFZ-Bestände
- drei Laufzeitzustände einschließlich Logs
- verwaiste Staging-Verzeichnisse
- private Installation `sfizz-1.2.3`

## Dauerhafte Regeln

1. `sfizz_jack` ist in versionierten produktiven Launchern verboten.
2. Sampler werden künftig als verwaltetes Plugin in einem Host oder über eine
   eigens geprüfte, nichtinteraktive Laufzeit betrieben.
3. Kein Audio-Prozess darf unbeschränkt in eine reguläre Logdatei schreiben.
4. Langläufer benötigen Prozessidentität, Ressourcenlimits, Rate-Limits,
   Größenlimits und einen belegten Stop-/Recovery-Pfad.
5. Negativtests müssen EOF, Logflut, 100-Prozent-CPU, fehlendes Gerät und
   abgebrochene Sitzungen abdecken.
