# Constructor del menú de arranque

Crea el menú de inicio visible a partir de entradas independientes. Elige una plantilla y luego ajusta esa entrada con los controles disponibles. Las entradas deshabilitadas permanecen en el proyecto, pero se omiten del menú generado.

## Menú de origen existente

Antes de la personalización, el constructor lee las entradas reconocidas de MiniOS, su valor predeterminado y tiempo de espera, y los parámetros compatibles desde el menú efectivo de GRUB o SYSLINUX nativo. Al editar una entrada importada, solo se reemplazan los parámetros representados por controles tipados. Los demás argumentos de origen permanecen en la plantilla de arranque. En un menú multilingüe, cada idioma mantiene sus argumentos de idioma, zona horaria y teclado de origen, a menos que los sobrescribas explícitamente.

## Cómo se ensambla una entrada

La plantilla proporciona el comportamiento base de MiniOS. Luego se aplican los argumentos globales expertos del kernel, seguidos por las opciones tipadas y los parámetros adicionales para esta entrada. Para las opciones repetidas de MiniOS en formato clave-valor, prevalece el último valor.

## Plantillas de sesión

Resume utiliza perchdir=resume. New utiliza perchdir=new. Choose utiliza perchdir=ask.
Fresh no tiene selector de persistencia. Copiar a RAM utiliza toram.

Puedes crear múltiples entradas a partir de la misma plantilla.

## Tipos de persistencia

Native almacena los cambios en un directorio. Dynfilefs utiliza un contenedor expandible, Raw usa una imagen de tamaño fijo y LUKS utiliza un contenedor cifrado. SquashFS reanuda una sesión comprimida existente. El initramfs actual no puede crear una nueva sesión SquashFS.

## Configuraciones dependientes

Los controles se desactivan cuando no aplican. Si desactivas zRAM, también se desactivan sus controles de compresión y tamaño. La persistencia Native y SquashFS no utilizan el campo de tamaño de contenedor.

## Finalización e ingreso experto

Los filtros de módulos se completan a partir de los módulos detectados en la fuente seleccionada. Los campos de idioma, zona horaria y teclado se completan a partir de los datos instalados en el sistema. Utiliza Parámetros adicionales solo para opciones que no tengan un control tipado. Los argumentos desconocidos cargados desde un proyecto anterior se conservan allí.

## Valores predeterminados y nombres

Un menú personalizado tiene exactamente una entrada predeterminada. Si deshabilitas la predeterminada, se selecciona automáticamente otra entrada habilitada. Un nombre vacío mantiene el título de la fuente o plantilla. Los nombres personalizados en ASCII funcionan en menús multilingües; un menú de un solo idioma puede usar caracteres compatibles con la codificación de menú de ese gestor de arranque.
