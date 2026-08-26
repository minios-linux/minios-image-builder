# Session and storage

Choose how this entry finds and stores persistent changes.

## Session selection

The entry template controls whether MiniOS resumes the latest session, creates
a new one, or asks at startup. The storage type does not replace that template
choice.

## Storage type

- **Automatic** keeps the template or saved-session mode.
- **Native** stores changes in a directory on a Linux filesystem.
- **Dynfilefs** uses an expandable container.
- **Raw** uses a fixed-size image.
- **LUKS** uses an encrypted container.
- **SquashFS** resumes an existing compressed session.

The current initramfs can resume but cannot create SquashFS sessions.

## Capacity

**Container size** applies only to container-backed sessions, so it is disabled
for Native and SquashFS. **Free space to keep** reserves room on the persistence
device so that saved changes do not fill it completely.

The corresponding boot options are `perchmode=`, `perchsize=`, and
`perchreserve=`.

## Copy to RAM

`toram=full` copies the entire system to memory. `toram=trim` copies only the
filtered module set. This may allow removal of the boot device, but requires
enough RAM for the copied data.
