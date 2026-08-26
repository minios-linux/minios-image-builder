# Parámetros de arranque y kernel

Escriba los parámetros separados por espacios. La función de autocompletado sugiere opciones comunes de MiniOS y Linux. Los parámetros globales se aplican a cada entrada de sesión de MiniOS creada o preservada. Los parámetros específicos de cada entrada se añaden después y pueden sobrescribir una opción de clave-valor de MiniOS repetida.

## Sesión y persistencia

Estas opciones permiten cambios persistentes, reanudar la última sesión compatible, crear una nueva sesión, preguntar al inicio o seleccionar directamente una sesión numerada: perch, perchdir=resume, perchdir=new, perchdir=ask y perchdir=NUMBER.

Los modos de almacenamiento son native, dynfilefs, raw, luks y squashfs. SquashFS puede reanudar una sesión comprimida existente, pero el initramfs actual no puede crear una nueva. Los tamaños de los contenedores aceptan sufijos MB, GB o TB. El espacio libre reservado se mide en MiB; su valor predeterminado es 256 y el máximo es 4096.

Las opciones correspondientes son perchmode=MODE, perchsize=SIZE y perchreserve=MIB.

## Copiar a RAM

Las opciones toram, toram=full y toram=trim copian el sistema predeterminado, completo o filtrado a la RAM.

## Módulos

El filtro load carga solo los módulos coincidentes; el filtro noload excluye los módulos coincidentes. Los filtros pueden contener nombres de módulos, listas o rangos de MiniOS soportados por el initramfs. Las opciones son load=FILTER y noload=FILTER.

## Memoria y gráficos

Las opciones de memoria desactivan zRAM, eligen compresión lzo, lzo-rle, lz4, lz4hc o zstd, y establecen el tamaño de zRAM en MiB. El modo texto inicia sin el escritorio gráfico. Nomodeset desactiva la configuración normal del modo del kernel y es útil para la solución de problemas gráficos. Las opciones son nozram, zramcomp=ALGORITHM, zramsize=MIB, text y nomodeset.

## Fuente y localización

Estas opciones seleccionan la fuente de datos de MiniOS y sobrescriben el idioma, la zona horaria y la configuración del teclado para la entrada. Las opciones son from=SOURCE, from=askdisk, locales=LOCALE, timezone=ZONE y keyboard-layouts=LAYOUT.

## Diagnóstico

Quiet reduce los mensajes de arranque. Debug habilita diagnósticos adicionales. Use solo parámetros entendidos por Linux, el initramfs de MiniOS o live-config. Las opciones son quiet y debug.
