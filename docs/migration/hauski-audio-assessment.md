# Bewertung des Alt-Repositories `hausKI-audio`

## Urteil

Der Code ist teilweise brauchbar, rechtfertigt aber keine Umbenennung des
Repos. Er wird als Spenderquelle behandelt.

## Belegt brauchbar

- Rust-Backend mit etwa 1.546 Quellzeilen
- abstrahierter Mopidy-Client und JSON-RPC-Zugriff
- HTTP-Endpunkte für Health, Modus, Playlists und ähnliche Titel
- Konfigurationsvalidierung und Tests
- Aufnahmehelfer für `pw-record`
- elf bestehende Python-Tests bestanden am 2026-07-26

## Nur nach Härtung übernehmbar

### Aufnahmehelfer

Das Grundprinzip ist brauchbar. Vor einer Übernahme fehlen insbesondere:

- PID plus Startzeit/Executable statt nur PID;
- atomarer Zustandsvertrag;
- belegte Zielnode und Kanalzuordnung;
- Abbruch-, Dateiintegritäts- und Speichergrenzen;
- WAV-Finalisierung und Recovery-Test.

### Mopidy-Backend

Die Trennung über ein Client-Trait und die Tests sind brauchbar. Vor einer
Übernahme ist zu entscheiden, ob Mopidy im künftigen Qobuz-Zielbild überhaupt
kanonisch bleibt.

## Nicht unverändert übernehmen

### `audio-mode`

Der Helfer stoppt für den ALSA-Modus PipeWire und PipeWire-Pulse global. Das
ist für ein profilbasiertes Gesamtsystem zu grob und kann Aufnahme, MIDI,
Desktop-Audio und Recoverypfade gleichzeitig beeinträchtigen.

### Alte Dokumentationsaussagen

Der behauptete ALSA-Standard und der tatsächliche Livepfad über `pulsesink`
widersprachen sich. Dokumentation wird nur zusammen mit einem messbaren
Doctor-Befund übernommen.

## Migrationsregel

Kein Copy-all. Jede Übernahme erhält einen eigenen Diff, Herkunfts-Commit,
Tests, Zielvertrag und Rückbaupfad.
