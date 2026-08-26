# Diagnostics and advanced options

Adjust boot logging or add parameters not represented by the typed controls.

## Boot messages

**Hide routine boot messages** adds `quiet`. **Enable diagnostic logging** adds
`debug`. They can be enabled independently. Disable `quiet` when detailed boot
messages are more useful than a clean startup screen.

## Additional parameters

Enter only Linux, MiniOS initramfs, or live-config parameters that do not have a
typed control. Existing unknown parameters are preserved here, and completion
continues to suggest common options.

Entry-specific parameters are appended after the template and global kernel
arguments. For a repeated MiniOS `key=value` option, the last value normally
wins. Invalid or conflicting expert parameters can prevent startup.
