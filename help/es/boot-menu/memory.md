# Memoria

Controla el swap comprimido zRAM creado para esta entrada.

## Estado de zRAM

**Automático** mantiene los valores predeterminados de memoria de MiniOS. **Deshabilitado** agrega `nozram` y desactiva los controles de compresión y tamaño porque ya no aplican.

## Compresión

`zramcomp=` selecciona el algoritmo de compresión. Las opciones disponibles son `lzo`, `lzo-rle`, `lz4`, `lz4hc` y `zstd`. La disponibilidad del algoritmo también depende del kernel en ejecución.

## Tamaño

`zramsize=` establece el tamaño de zRAM en MiB. Deja el campo vacío para que MiniOS calcule el tamaño automáticamente. Un valor mayor no es memoria física libre: las páginas comprimidas aún consumen RAM.
