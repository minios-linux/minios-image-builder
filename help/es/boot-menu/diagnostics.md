# Diagnóstico y opciones avanzadas

Ajusta el registro de inicio o agrega parámetros que no estén representados por los controles disponibles.

## Mensajes de arranque

**Ocultar mensajes rutinarios de arranque** agrega `quiet`. **Habilitar registro de diagnóstico** agrega
`debug`. Ambos pueden habilitarse de forma independiente. Desactiva `quiet` cuando los mensajes de arranque detallados
sean más útiles que una pantalla de inicio limpia.

## Parámetros adicionales

Ingresa solo parámetros de Linux, initramfs de MiniOS o live-config que no tengan un
control tipado. Los parámetros desconocidos existentes se conservan aquí, y la autocompletación
continúa sugiriendo opciones comunes.

Los parámetros específicos de la entrada se agregan después de los argumentos de plantilla y del kernel global.
Para una opción MiniOS `key=value` repetida, normalmente prevalece el último valor.
Parámetros expertos inválidos o en conflicto pueden impedir el arranque.
