# System and modules

Select the modules and startup environment for this entry.

## Module filters

**Load modules** restricts loading to matching module names or ranges.
**Skip modules** excludes matching modules. Suggestions come from modules
detected in the selected MiniOS source.

These controls generate `load=FILTER` and `noload=FILTER`. Use only filter
forms supported by the MiniOS initramfs.

## Startup environment

Keep the image default, start the graphical desktop, use a text console, or
enter rescue mode. Text mode and rescue mode are intended for administration
and troubleshooting rather than normal desktop use.

## Graphics compatibility

Compatibility mode adds the Linux kernel option `nomodeset`. Use it when normal
kernel mode setting prevents the graphical system from starting. It can reduce
display resolution and acceleration.

## Disk automounting

Enable automatic mounting only when the session should expose other attached
filesystems after startup.
