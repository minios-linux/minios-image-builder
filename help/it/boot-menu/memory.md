# Memoria

Gestisci lo swap zRAM compresso creato per questa voce.

## Stato zRAM

**Automatico** mantiene le impostazioni di memoria predefinite di MiniOS. **Disabilitato** aggiunge `nozram` e
disabilita i controlli di compressione e dimensione perché non sono più applicabili.

## Compressione

`zramcomp=` seleziona l'algoritmo di compressione. Le scelte disponibili sono `lzo`,
`lzo-rle`, `lz4`, `lz4hc` e `zstd`. La disponibilità degli algoritmi dipende anche
dal kernel in esecuzione.

## Dimensione

`zramsize=` imposta la dimensione dello zRAM in MiB. Lascia il campo vuoto per lasciare che MiniOS
calcoli automaticamente la dimensione. Un valore maggiore non corrisponde a memoria fisica gratuita:
le pagine compresse occupano comunque RAM.
