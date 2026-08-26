# Boot menu constructor

Build the visible startup menu from independent entries. Choose a template, then refine that entry with the typed controls. Disabled entries remain in the project but are omitted from the generated menu.

## Existing source menu

Before customization, the constructor reads recognized MiniOS entries, their default and timeout, and supported parameters from the effective GRUB or native SYSLINUX menu. Editing an imported entry replaces only parameters represented by typed controls. Other source arguments remain in the boot template. In a multilingual menu, each language keeps its source locale, timezone, and keyboard arguments unless you explicitly override them.

## How an entry is assembled

The template supplies the base MiniOS behavior. Global expert kernel arguments
are applied next, followed by the typed options and Additional parameters
for this entry. For repeated MiniOS key-value options, the last value wins.

## Session templates

Resume uses perchdir=resume. New uses perchdir=new. Choose uses perchdir=ask.
Fresh has no persistence selector. Copy to RAM uses toram.

You can create multiple entries from the same template.

## Persistence types

Native stores changes in a directory. Dynfilefs uses an expandable container,
Raw uses a fixed-size image, and LUKS uses an encrypted container. SquashFS
resumes an existing compressed session. The current
initramfs cannot create a new SquashFS session.

## Dependent settings

Controls become unavailable when they do not apply. Disabling zRAM disables
its compression and size controls. Native and SquashFS persistence do not use
the container-size field.

## Completion and expert input

Module filters complete from modules detected in the selected source. Locale,
timezone, and keyboard fields complete from installed system data. Use
Additional parameters only for options without a typed control. Unknown
arguments loaded from an older project are preserved there.

## Defaults and names

A customized menu has exactly one default entry. Disabling the default selects
another enabled entry automatically. An empty name keeps the source or template
title. ASCII custom names work in multilingual menus; a single-language menu
can use characters supported by that bootloader menu encoding.
