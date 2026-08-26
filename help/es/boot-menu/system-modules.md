# Sistema y módulos

Selecciona los módulos y el entorno de inicio para esta entrada.

## Filtros de módulos

**Cargar módulos** restringe la carga a nombres de módulos o rangos coincidentes.
**Omitir módulos** excluye los módulos que coincidan. Las sugerencias provienen de los módulos
detectados en la fuente MiniOS seleccionada.

Estos controles generan `load=FILTER` y `noload=FILTER`. Utilice solo los formatos de filtro
soportados por el initramfs de MiniOS.

## Entorno de inicio

Mantenga la imagen predeterminada, inicie el escritorio gráfico, use una consola de texto o
entre en modo de rescate. El modo texto y el modo de rescate están pensados para administración
y solución de problemas en lugar de uso normal del escritorio.

## Compatibilidad gráfica

El modo de compatibilidad añade la opción del kernel de Linux `nomodeset`. Úselo cuando el modo
normal de configuración del kernel impida que el sistema gráfico se inicie. Puede reducir
la resolución y la aceleración de pantalla.

## Montaje automático de discos

Active el montaje automático solo cuando la sesión deba exponer otros sistemas de archivos
adjuntos después del inicio.
