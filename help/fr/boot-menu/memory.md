# Mémoire

Contrôlez le swap zRAM compressé créé pour cette entrée.

## État de zRAM

**Automatique** conserve les valeurs par défaut de la mémoire MiniOS. **Désactivé** ajoute `nozram` et
désactive les contrôles de compression et de taille car ils ne s'appliquent plus.

## Compression

`zramcomp=` sélectionne l'algorithme de compression. Les choix disponibles sont `lzo`,
`lzo-rle`, `lz4`, `lz4hc` et `zstd`. La disponibilité de l'algorithme dépend également
du noyau en cours d'exécution.

## Taille

`zramsize=` définit la taille de la zRAM en Mio. Laissez le champ vide pour laisser MiniOS
calculer automatiquement la taille. Une valeur plus grande n'est pas de la mémoire physique gratuite :
les pages compressées consomment toujours de la RAM.
