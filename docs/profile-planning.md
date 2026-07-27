# Read-only Profilplanung

`audio-plan PROFILE` verbindet den Live-Doctor, private physische Beobachtungen
und den deklarativen Profilkatalog. Das Ergebnis nennt fehlende Hardware,
fehlende oder widersprüchliche physische Fakten und die geplanten Änderungen.

Der Planer hat keine Apply-Funktion. Kandidaten für Quantum oder native
Sampleraten bleiben blockiert, bis Latenz-, XRun- und Resamplingmessungen
vorliegen.

Noch ausstehende Labor-Gates werden maschinenlesbar als `unresolved_laboratory_gates` ausgegeben und blockieren die Bereitschaft.
