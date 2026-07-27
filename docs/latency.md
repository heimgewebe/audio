# Round-Trip-Latenz

Der Doctor berechnet nur die Dauer eines einzelnen PipeWire-Puffers. Das ist
**keine** gemessene Round-Trip-Latenz.

Für eine belastbare Messung:

1. `generate-audio-reference` erzeugt einen begrenzten Impuls als WAV.
2. Der Impuls wird in einer DAW über einen MOTU-Ausgang ausgegeben.
3. Ein physisches Kabel führt diesen Ausgang auf einen MOTU-Eingang zurück.
4. Die Aufnahme wird als mono PCM16-WAV exportiert.
5. `analyze-loopback-latency` vergleicht Referenz und Aufnahme offline.

Der Analysator verweigert Dateien oberhalb von 2.000.000 PCM-Frames.
Die Messung verändert keine dauerhafte PipeWire-Konfiguration. Ohne bestätigtes
Loopback-Kabel wird keine Zahl als Round-Trip-Latenz veröffentlicht.
