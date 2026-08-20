% MINIOS-IMAGE-COMPOSE(1) MiniOS Live Manual

# NAME

minios-image-compose - generate MiniOS ISO image with specified modules

# SYNOPSIS

**minios-image-compose** [**-e** *REGEX*] [**-n** *NAME*] [**--source** *MINIOS_DIR*] [**--config** *FILE*] [**--volume-label** *LABEL*] [**--manifest** *FILE*] [**--capture-changes** *MODE*] [**--capture-selection** *FILE*] [**--capture-compression** *TYPE*] [**--boot-timeout** *SECONDS*] [**--default-boot** *MODE*] [**--kernel-args** *TEXT*] [**--boot-background** *PNG*] [**--overlay-directory** *DIR*] [**--menu** *TYPE*] [**--overwrite**] [**--no-color**] [**--help**] [**--version**] [*MODULE*...]

# DESCRIPTION

**minios-image-compose** generates a MiniOS ISO image and optionally adds specified SquashFS modules. The tool detects the bootloader type (SYSLINUX, GRUB, or mixed) and supports customizable menu types with localization support.

Input identities, modes, mtimes, and SHA256 digests are recorded before the build. Before **xorriso** starts, the backend attempts to bind mutable regular graft inputs to private, metadata-preserving reflinks. When a reflink is unavailable, the original path remains the graft source and strict identity and digest checks before and after **xorriso** detect mutation. Hardlinks are not used. Read-only mounted inputs may remain directly bound. The destination free-space preflight reserves the estimated ISO size and additional headroom. Graft points are passed to **xorriso** as separate arguments, and the completed image is checked for its filesystem tree, volume label, BIOS boot record, UEFI boot record, and system area. Rock Ridge modes and symbolic links are preserved in no-customization builds without chmod or writes against the source.

Positional modules must be readable, non-symlink regular files with portable **.sb** basenames and valid SquashFS superblocks. Names beginning with two digits and a hyphen are placed at the MiniOS top level; other added modules are placed below **minios/modules/**. Destination collisions and duplicate case-folded basenames are rejected across source top-level modules, **modules/**, positional modules, image overlays, and session captures because the runtime flattens those layers by basename.

ISO preparation, writing, verification, and publication run with the caller's EUID. For optional session capture, a non-root invocation runs only trusted **/usr/bin/savechanges** through trusted **/usr/bin/pkexec**; it does not elevate **minios-image-compose**, **xorriso**, or later phases. A private cancellation marker in the mode 0700 build directory lets the unprivileged parent request root-side process-group cleanup even when direct signals fail with EPERM. Callers should invoke **minios-image-compose** normally rather than prefixing the complete command with **pkexec**.

Image customization is limited to boot configuration, boot artwork, and one declarative filesystem overlay. It never chroots, installs packages, executes project scripts, or modifies the source tree. Effective boot configs and overlay files are copied into private storage through no-follow descriptors before tools consume them. Only the optional running-session capture can cross the pkexec boundary.

# OPTIONS

**-e**, **--exclude** *REGEX*
: Exclude source paths matching the POSIX extended regular expression *REGEX*. Newlines and invalid expressions are rejected. An exclusion may not remove required boot files, kernel/initramfs files, core or kernel modules, the selected menu, the configuration, or the requested manifest.

**-n**, **--name** *NAME*
: Publish to this exact output pathname. The default is minios-YYYYMMDD_HHMM.iso in the current directory. The image is built in a private mode 0700 directory on the destination filesystem and atomically published through retained directory descriptors. An existing pathname is refused unless **--overwrite** is present.

**--source** *MINIOS_DIR*
: Use the canonical MiniOS content root containing **boot/** and MiniOS module files. Modules may be top-level or below **modules/**. The source directory itself must not be a symbolic link and is never modified. Without this option, the current livekit/dracut source discovery is used. Session capture compares the effective source-module fingerprint with the running mounted base. If reliable module binding is unavailable, an explicit source is accepted only when it is the discovered running source.

**--config** *FILE*
: Graft the readable, non-symlink regular file as **minios/config.conf**. The default is **/etc/live/config.conf**.

**--volume-label** *LABEL*
: Set a 1 to 32 character printable ASCII ISO volume label. The default is **MINIOS**. Labels outside the strict ISO 9660 uppercase, digit, and underscore set are accepted by xorriso with a warning.

**--manifest** *FILE*
: Validate a readable, non-symlink JSON object and graft it as **minios/build-manifest.json**.

**--capture-changes** *MODE*
: Capture the running writable session with the savechanges **exact**, **clean**, or **selected** profile. The module is assigned the first safe numeric order after every included source and additional module, then grafted as **minios/ORDER-session-changes.sb**. Orders through 999998 are supported; collisions and an exhausted range are rejected. Capture metadata binds the module to the running boot ID and mounted base-module fingerprint. **clean** is a narrow path-based privacy reduction, not proof that no allowed software file contains a secret.

**--capture-selection** *FILE*
: Use this readable, non-symlink strict JSON selection with **--capture-changes selected**. It is required for selected capture and forbidden for exact or clean capture. The selection is copied to private build storage before it is hashed or consumed.

**--capture-compression** *TYPE*
: Compress the captured module with **zstd**, **gzip**, **lzo**, or **xz**. The default is **zstd**. This option is invalid without **--capture-changes**.

**--boot-timeout** *SECONDS*
: Set the timeout in every authoritative GRUB and native SYSLINUX configuration reached from the active bootloader roots. The value is an integer from 0 through 300. GRUB receives seconds and SYSLINUX receives deciseconds. Without this option, source timeout directives are preserved. In a multilingual layout, both the language menu and each reachable session menu are updated. The minimal MiniOS SYSLINUX-to-GRUB chainloader remains unchanged because GRUB is authoritative after its fixed immediate handoff.

**--default-boot** *MODE*
: Select **resume**, **new**, **choose**, **fresh**, or **toram** as the default MiniOS session. GRUB entries are identified by the MiniOS semantic classes **resume**, **new**, **switch**, **live**, and **ram**, with kernel session arguments used as a consistency check or fallback. Native SYSLINUX entries are identified from **APPEND** session arguments and checked against known **LABEL** values. Translated **MENU LABEL** and GRUB title text are never used. Every authoritative reachable session menu must prove the requested mapping or the build fails. A multilingual first-stage language menu keeps its language default; its reachable **main.cfg** or `lang/*.cfg` session menus are changed instead. The exact MiniOS `LINUX /minios/boot/grub/i386-pc/lnxboot.img` and `INITRD /minios/boot/grub/i386-pc/core.img` chain delegates session semantics to the reachable GRUB graph; malformed or mixed chainloader/native layouts are rejected.

**--kernel-args** *TEXT*
: Append *TEXT* unchanged to every actual GRUB **linux**, **linuxefi**, or **linux16** command and every SYSLINUX kernel stanza **APPEND** line in the effective config graph. The UTF-8 encoding must be 1 to 4096 bytes, printable, use ordinary spaces only, and avoid quoting, expansion, comment, redirection, grouping, wildcard, and command-separator characters that cannot have identical GRUB and SYSLINUX meaning. The text is never evaluated or passed through shell substitution. Logs and **image-customization.json** contain only its UTF-8 byte count and SHA256.

**--boot-background** *PNG*
: Replace **minios/boot/bootlogo791.png** and existing source `bootlogo*.png` variants with a private snapshot of *PNG*. The input must be a readable non-symlink regular file no larger than 16 MiB and 1 through 8192 pixels on each axis. Validation checks bounded chunks, CRCs, one leading IHDR, a GRUB/SYSLINUX-compatible non-interlaced RGB, indexed, or RGBA format, palette ordering, consecutive IDAT chunks, the zlib stream and scanline filters, one final IEND, and absence of trailing data. The source is not modified. Every embedded target is extracted and compared with the recorded digest after ISO creation.

**--overlay-directory** *DIR*
: Package one real, readable directory, given as an absolute path outside the MiniOS source, the output, and the work directory, as a rootless **ORDER-image-overlay.sb** module. The overlay is resolved independently of the current working directory, so the frontend may run **minios-image-compose** from a private job directory. Safe directories, regular files, empty directories, executable bits, mtimes, and relative symlinks that remain inside the overlay root are supported. Device nodes, sockets, FIFOs, filesystem crossings, control characters in names, absolute links, and escaping links are rejected. Privilege bits are cleared and module ownership is normalized with **mksquashfs -all-root**. The tree is copied through retained no-follow descriptors, rescanned for mutation, and only the private copy is passed to mksquashfs. The generated and ISO-extracted SquashFS images are each unpacked privately and compared against the captured path, type, mode, symlink-target, regular-size, and SHA256 manifest. Missing, extra, or changed objects fail publication. The dynamic order follows every source and additional module and precedes a session-capture module when both are requested.

**--menu** *TYPE*
: Set menu type for both GRUB and SYSLINUX. *TYPE* can be:
  **multilang** (default) - menu with language selection
  *LANG* - specific language code (en_US, ru_RU, de_DE, es_ES, it_IT, id_ID, pt_BR, pt_PT, fr_FR)

**--overwrite**
: Explicitly allow replacement of an existing output file. This is a safety change from older releases, which could let xorriso replace the target implicitly.

**--no-color**
: Disable terminal colors. Output is also color-free when stdout is not a terminal or the **NO_COLOR** environment variable is set.

**--help**
: Display help message and exit.

**--version**
: Display version information and exit.

# BOOTLOADER SUPPORT

The tool automatically detects and supports three bootloader configurations:

**syslinux-grub** (most common)
: Uses the exact minimal MiniOS SYSLINUX **lnxboot.img/core.img** stanza to hand legacy BIOS execution to GRUB; GRUB is also used directly for UEFI. Detection requires both chainloader images, and customization validates the stanza before treating the reachable GRUB graph as authoritative.

**grub-only**
: Uses GRUB directly for all boot scenarios. Detected when only GRUB BIOS components are present.

**syslinux-native**
: Uses SYSLINUX natively without GRUB components. Detected when only SYSLINUX files are present.

The bootloader type is automatically detected based on the boot files present in the source MiniOS system.

# MENU TYPES

**multilang** (default)
: Multi-language menu with language selection screen.

*Language codes*
: Fully localized menus with translated text and language-specific themes. Supported languages: en_US (English), ru_RU (Russian), de_DE (German), es_ES (Spanish), it_IT (Italian), id_ID (Indonesian), pt_BR (Portuguese Brazil), pt_PT (Portuguese Portugal), fr_FR (French).

# EXAMPLES

Create basic ISO from current live system:

    minios-image-compose

Create ISO with custom name:

    minios-image-compose --name my_custom_minios.iso

Create an ISO from an explicit source and configuration:

    minios-image-compose --source /media/minios --config ./config.conf --name ./custom.iso

Include a GUI-generated build manifest and custom label:

    minios-image-compose --manifest ./build.json --volume-label 'MINIOS LAB' --name ./custom.iso

Create an ISO using the narrow software-only session policy:

    minios-image-compose --capture-changes clean --name ./software-session.iso

Create an image with a five-second session menu, a fresh-session default, and additional safe kernel arguments:

    minios-image-compose --boot-timeout 5 --default-boot fresh \
        --kernel-args 'audit=1 mitigations=auto' --name ./custom-boot.iso

Add a project filesystem overlay and boot artwork without changing the source tree:

    minios-image-compose --overlay-directory "$PWD/image-overlay" \
        --boot-background ./art/boot.png --name ./branded.iso

Capture only paths chosen by a frontend:

    minios-image-compose --capture-changes selected \
        --capture-selection ./session-selection.json --name ./selected.iso

Exclude heavy applications:

    minios-image-compose --exclude 'firefox|libreoffice|gimp' --name minios_lite.iso

Create minimal text-mode ISO:

    minios-image-compose --exclude 'desktop|xorg|apps' --name minios_minimal.iso

Add extra modules to current system:

    minios-image-compose development_tools.sb games.sb --name minios_extended.iso

Create localized Russian ISO:

    minios-image-compose --menu ru_RU --name minios_ru.iso

Combine exclusions with additions:

    minios-image-compose --exclude 'games' extra_productivity.sb --name minios_work.iso

# FILES

Without **--source**, the script discovers MiniOS loaded from live media or RAM. With explicit **--source** and **--config** paths, source discovery is not required. Session capture still requires a running MiniOS session whose mounted base-module fingerprint matches the selected source. Required system tools are resolved from **/usr/sbin:/usr/bin:/sbin:/bin**, independently of the caller's PATH; **mkfs.ext2** is provided by **e2fsprogs**. Default boot files are located in:

*/run/initramfs/memory/data/minios/boot/*
: Boot files location when running from live system

*/lib/live/mount/data/minios/boot/*
: Boot files location with dracut-based live media

*/etc/live/config.conf*
: Default configuration input

# OUTPUT PROTOCOL

Human-readable messages retain the stable **I:**, **W:**, and **E:** prefixes. Major phases are additionally emitted as untranslated identifiers: **P:prepare**, **P:customize** before customization work, **P:capture** when enabled, **P:boot-copy**, **P:persistence**, **P:iso-write**, **P:verify**, **P:complete**, and **P:cancelled** during privileged cancellation. The nested **savechanges** phases **P:capture-inventory**, **P:capture-copy**, **P:capture-compress**, **P:capture-complete**, and **P:cancelled** pass through unchanged. Frontends should consume these identifiers rather than translated prose.

Capture logs state the profile, union backend, boot ID, source and base-module fingerprints, dynamic order and ISO target, nonzero module size, module SHA256, and selection SHA256 when applicable. Selection include and exclude paths are never logged; invalid or unmatched selections report only generic classes or counts. **minios/session-capture.json** embeds the same digest-only schema-3 report without changing the GUI-authored **build-manifest.json**.

After structural ISO checks, **minios-image-compose** extracts the embedded capture module and report, plus the build manifest when present. It rehashes their actual bytes, validates the report against privileged capture metadata and expected order, performs full SquashFS checks on the extracted module, and rejects any mismatch before atomic publication.

When image customization is enabled, **minios/image-customization.json** is embedded with product kind **minios-image-customization** and schema version 1. It records transformed config targets and digests, timeout and semantic default values, kernel-argument byte count and SHA256, background dimensions/targets/digest, and image-overlay target, order, size, SHA256, entry count, and input-tree fingerprint. It contains no kernel argument text or host input path. Verification rejects duplicate fields, non-finite numbers, unknown fields, incorrect types, and incorrect optional-section presence. The expected report is reconstructed independently from retained CLI intent, the byte-bound boot plan and its source digests, retained target-set identities, the original background and overlay identities, and bytes extracted from the ISO. It does not trust or compare against the mutable local report. Every customized config transformation is recomputed, every extracted background is parsed and hashed, and the generated and embedded overlay modules must match the original retained module size and SHA256. Both overlay modules are then checked against the captured filesystem manifest.

Boot transformation intentionally supports direct GRUB **configfile** or **source** references and direct native SYSLINUX **CONFIG** or **INCLUDE** references whose normalized targets remain below **minios/boot/**, plus the exact MiniOS SYSLINUX-GRUB chainloader described above. Dynamic references, shell-generated config names, GRUB submenus, SYSLINUX nested menus, commented or multiline kernel commands, ambiguous session arguments, missing semantic entries, mixed chainloader/native layouts, and config graph cycles are rejected rather than guessed. Existing native SYSLINUX **MENU DEFAULT** directives are removed and **ONTIMEOUT** is aligned with the requested semantic default. Source layouts using unsupported forms require normalization before customization; builds without boot customization continue to preserve them unchanged.

Private reflink snapshots close pathname and ancestor replacement races for mutable xorriso inputs when the filesystem supports reflinks. Otherwise, the original graft remains identity-bound and any metadata or content change detected around xorriso fails the build. Read-only mounted sources are treated as immutable. Every supervised external command must also finish its process group; a leader that exits while descendants remain causes bounded TERM/KILL cleanup and build failure.

# EXIT STATUS

**0**
: Successful completion.

**1**
: General, validation, tool, destination, write, or verification error.

**2**
: Invalid source tree, required modules, menu, kernel/initramfs, or boot files.

**3**
: Invalid, unsafe, colliding, unreadable, or non-SquashFS additional module.

**4**
: Cannot create or prepare secure temporary build data.

**130**
: Interrupted by INT or TERM. Rootless overlay mksquashfs and its process group are terminated directly. During privileged capture, the marker is created atomically before direct signal attempts, then the parent waits a bounded interval for savechanges to terminate, reap, and clean its root process group. Private temporary output is removed through retained descriptors.

# SEE ALSO

**sb**(1), **chroot2sb**(1), **apt2sb**(1)
