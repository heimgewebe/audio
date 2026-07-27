# Audio-Baselines

Baselines trennen zwei Belege:

1. Ein privater, auf dem Heim-PC gespeicherter Rohbeleg mit begrenzten
   Kommandoausgaben und Konfigurationshashes.
2. Eine öffentliche Projektion ohne Hostname, Nutzername, Geräte-Seriennummern,
   Netzwerkadressen, Zugangsdaten oder vollständige Prozess- und Logausgaben.

`capture-baseline` ist ausschließlich lesend. Neue Kommandos werden durch Tests
gegen bekannte Mutationsverben geprüft. Eine Baseline autorisiert weder
`audio apply` noch irgendeine produktive Audioänderung.
