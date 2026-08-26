# Arbeitsspeicher

Steuern Sie den komprimierten zRAM-Swap, der für diesen Eintrag erstellt wurde.

## zRAM-Status

**Automatisch** behält die MiniOS-Standardeinstellungen für den Speicher bei. **Deaktiviert** fügt `nozram` hinzu und deaktiviert die Komprimierungs- und Größenkontrollen, da sie nicht mehr anwendbar sind.

## Komprimierung

`zramcomp=` wählt den Komprimierungsalgorithmus aus. Verfügbare Optionen sind `lzo`,
`lzo-rle`, `lz4`, `lz4hc` und `zstd`. Die Verfügbarkeit des Algorithmus hängt auch vom laufenden Kernel ab.

## Größe

`zramsize=` legt die zRAM-Größe in MiB fest. Lassen Sie das Feld leer, damit MiniOS
die Größe automatisch berechnet. Ein größerer Wert bedeutet keinen freien physischen Speicher:
komprimierte Seiten belegen weiterhin RAM.
