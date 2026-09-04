# Sesión y almacenamiento

Elige cómo esta entrada encuentra y almacena los cambios persistentes.

## Selección de sesión

La plantilla de entrada controla si MiniOS reanuda la última sesión, crea una nueva o pregunta al iniciar. El tipo de almacenamiento no reemplaza esa elección de plantilla.

## Tipo de almacenamiento

- **Automático** mantiene el modo de plantilla o sesión guardada.
- **Nativo** almacena los cambios directamente en un directorio del sistema de archivos con permisos Unix, como ext4, XFS o Btrfs.
- **Dynfilefs** utiliza un contenedor expandible.
- **Raw** utiliza una imagen de tamaño fijo.
- **LUKS** utiliza un contenedor cifrado.
- **SquashFS** reanuda una sesión comprimida existente.

El initramfs actual puede reanudar pero no crear sesiones SquashFS.

## Capacidad

**Tamaño del contenedor** solo aplica para sesiones respaldadas por contenedor, por lo que está deshabilitado para Nativo y SquashFS. **Espacio libre a reservar** guarda espacio en el dispositivo de persistencia para que los cambios guardados no lo llenen completamente.

Las opciones de arranque correspondientes son `perchmode=`, `perchsize=` y `perchreserve=`.

## Copiar a RAM

`toram=full` copia todo el sistema a la memoria. `toram=trim` copia solo el conjunto de módulos filtrados. Esto puede permitir quitar el dispositivo de arranque, pero requiere suficiente RAM para los datos copiados.
