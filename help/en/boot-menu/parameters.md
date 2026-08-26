# Boot and kernel parameters

Type parameters separated by spaces. Completion suggests common MiniOS and Linux options. Global parameters apply to every constructed or preserved MiniOS session entry. Entry-specific parameters are appended later and can override a repeated MiniOS key-value option.

## Session and persistence

These options enable persistent changes, resume the latest compatible session,
create a new session, ask at startup, or select a numbered session directly:
perch, perchdir=resume, perchdir=new, perchdir=ask, and perchdir=NUMBER.

Storage modes are native, dynfilefs, raw, luks, and squashfs. SquashFS can
resume an existing compressed session, but the current initramfs cannot create
a new one. Container sizes accept MB, GB, or TB suffixes. The reserved free
space is measured in MiB; its default is 256 and its maximum is 4096.

The corresponding options are perchmode=MODE, perchsize=SIZE, and
perchreserve=MIB.

## Copy to RAM

The toram, toram=full, and toram=trim options copy the default, complete, or
filtered system to RAM.

## Modules

The load filter loads only matching modules; the noload filter excludes
matching modules. Filters may contain module names, lists, or MiniOS ranges
supported by the initramfs. The options are load=FILTER and noload=FILTER.

## Memory and graphics

The memory options disable zRAM, choose lzo, lzo-rle, lz4, lz4hc, or zstd
compression, and set the zRAM size in MiB. Text mode starts without the
graphical desktop. Nomodeset disables normal kernel mode setting and is useful
for graphics troubleshooting. The options are nozram, zramcomp=ALGORITHM,
zramsize=MIB, text, and nomodeset.

## Source and localization

These options select the MiniOS data source and override language, timezone,
and keyboard settings for the entry. The options are from=SOURCE, from=askdisk,
locales=LOCALE, timezone=ZONE, and keyboard-layouts=LAYOUT.

## Diagnostics

Quiet reduces boot messages. Debug enables additional diagnostics. Use only
parameters understood by Linux, the MiniOS initramfs, or live-config. The
options are quiet and debug.
