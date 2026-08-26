% MINIOS-IMAGE-BUILDER(1) minios-image-builder
% MiniOS Linux
% 2026

# NAME

minios-image-builder - customize MiniOS as a verified image

# USAGE

**minios-image-builder**

# DESCRIPTION

**minios-image-builder** launches MiniOS Image Builder, a GTK project workspace
for remastering MiniOS through **minios-image-compose**(1). It selects and
fingerprints a MiniOS source, lets the user select source and additional
**.sb** modules, creates a secure build plan, and structurally verifies the
private ISO before publishing it.

The application runs inside MiniOS and never source-builds MiniOS or modifies
source media. The source can be the current session, a MiniOS ISO file, or an
optical disc. ISO and optical-disc sources are mounted read-only through
**udisksctl**. The application and composition remain unprivileged. A fixed
path-bound helper reads **/etc/live/config.conf** only when it is root-only;
optional writable-session capture authorizes only trusted **savechanges**(1).

# WORKFLOW

**Source**
: Select the current livekit or dracut session, a MiniOS ISO file, or an optical
disc asynchronously. The page shows the source path, release, version,
architecture, bootloader support, size, module counts, and diagnostics.

**Content**
: Select source modules and add external **.sb** files. Required core and kernel
modules remain selected and locked. Active modules outside the source media are
shown separately and are not included automatically. Basename and target
collisions block continuation.

**Settings**
: Select output identity, allowlisted system defaults, boot behavior and PNG
  appearance, an optional root-relative project filesystem layer, and optional
  session capture. Security, user-data, and expert kernel controls remain in
  compact expanders. Empty or Keep current values preserve source behavior.
  Inclusion of **/etc/live/config.conf** is currently required. Its bytes are
  read and copied verbatim, frozen during planning, and staged mode 0600 in the
  private build job. Its values are not interpreted, displayed, or logged. A loaded
  `include_current_config=false` value is preserved but blocks Review.

**Review**
: Create a fresh build plan and show selected, deselected, and additional
  modules, output defaults, path-free customization intent, capture mode and
  privilege boundary, size estimates, warnings, and stable diagnostic codes.
  Configuration values, raw global or per-entry kernel arguments, private
  customization paths, and selected capture paths are never shown. An existing output is replaced
only after confirmation and a new backend plan bound to the same observed file
identity. Cancellation, failure, or an identity mismatch clears approval, so
every retry asks again.

**Build**
: Revalidate every effective input, execute **minios-image-compose** with list-based argv in
  the backend's descriptor-bound working directory, capture writable changes
  when requested, structurally verify customization and capture attestations,
  and publish atomically only after verification succeeds.

# IMAGE CUSTOMIZATION

System controls cover hostname, timezone, default target, and enabled or
disabled services. Security and access controls expose only backend-allowlisted
sudo, polkit, SSH, XRDP, X11, lock-screen, and issue-hint modes. User data can
link or bind user directories, but not both, and accepts a validated
root-relative user-directory path.

Boot controls preserve or set a 0-300 second timeout and can preserve the
source menu/default or switch to the boot-menu constructor. The constructor
starts from the five MiniOS session templates: resume (`perchdir=resume`), new
(`perchdir=new`), choose (`perchdir=ask`), fresh (no persistence selector), and
copy-to-RAM (`toram`). Existing entries can be hidden or reordered, and any
template can be duplicated to create additional entries with a unique internal
identifier and custom visible title. Each entry exposes typed controls for the
persistence backend (`native`, `dynfilefs`, `raw`, `luks`, or compressed
`squashfs` sessions) and size, RAM-copy scope, module filters, graphical, text,
or rescue startup, compatible graphics, disk automounting, zRAM, locale,
timezone, keyboard layout, quiet startup, and diagnostics. Settings understood
by LiveKit and live-config are compiled to the existing per-entry kernel
argument format, so saved schema-1 projects and the composition CLI remain
compatible. Before customization, the constructor recognizes the effective
GRUB and native SYSLINUX entries, titles, default, timeout, and typed parameters
from the selected source. Editing a recognized entry replaces only parameters
owned by its typed controls; unknown and required source arguments remain in
the bootloader template. Multilingual source locale arguments remain per-language
unless the user explicitly overrides them. Unknown arguments from an existing
project remain in the Additional parameters field. A custom menu has exactly
one enabled default entry. ASCII
custom titles are portable across a multilingual menu; native single-language
SYSLINUX additionally permits characters representable by that menu's legacy
encoding. Module filters complete from the detected source modules, while
locale, timezone, and keyboard fields complete from the current system data.
Choosing disabled zRAM makes its compression and size fields insensitive;
directory and SquashFS persistence likewise disable the inapplicable container
size field. The constructor's general help explains entry assembly, templates,
parameter precedence, persistence types, dependencies, and compatibility. Each
option section also has contextual help for its own controls.

Kernel-parameter completion and contextual help document the commonly useful
MiniOS controls: `perch`, `perchdir=`, `perchmode=`, `perchsize=`,
`perchreserve=`, `toram`, `toram=full`, `toram=trim`, `load=`, `noload=`,
`from=`, `text`, `automount`, zRAM settings, locale/timezone/keyboard settings,
and common Linux diagnostic options such as `nomodeset`, `quiet`, and `debug`.
Compiled per-entry parameters are appended after the selected template and
global expert kernel arguments; MiniOS key=value parameters therefore follow
the initramfs last-value rule for legacy project entries. Recognized source
entries use replacement semantics for typed controls to avoid duplicate options
and permit flags to be disabled. Expert global and per-entry arguments remain
available separately for options not represented by typed controls.

General, section, and parameter help is installed as localized Markdown under
`/usr/share/minios-image-builder/help`. `python3-minios-gui` renders it natively
in a read-only GTK text view without HTML or WebKit. Lookup tries the complete
interface locale, then its language, and finally English.
Boot backgrounds must be valid PNG files.

The project filesystem layer is an existing canonical real directory, or a
mode-0700 directory created by the backend beneath the project directory. The
tree is copied relative to image root. No scripts, raw shell, chroot, or package
commands are exposed, and the source directory is never deleted. Reusable
modules belong in Module Manager.

Review presents only override key names, boot settings, kernel byte count and
SHA-256, background basename/digest, and overlay basename/fingerprint/counts.
Successful verification presents the corresponding attested summary and overlay
module size/hash. Composition is unprivileged; authorization is limited to the
fixed config reader and optional session capture.

# CANCELLATION

Inventory, build, and verification subprocesses run in dedicated process
groups. Inventory cancellation requests an identity-bound private backend
marker before sending **SIGTERM**, then escalates to **SIGKILL** after a short
grace period. Build cancellation signals **minios-image-compose**, whose trap owns its internal
capture marker. Pure Python hashing may finish its current
pass before cancellation reaches a safe checkpoint; stale results are discarded
and close waits for tracked work. Atomic publication is not cancelled after it
starts, so the destination is never intentionally left half-written.

# REQUIREMENTS

The composition backend is provided by the matching **minios-image-compose**
package, guaranteed by an exact package dependency. Session capture additionally
uses **savechanges** from **minios-tools** 1.5.0 or newer. **pkexec** is required;
a desktop polkit agent is recommended for root-only current configurations and
non-root session capture. **udisksctl** from **udisks2** is required for ISO-file
and optical-disc sources. Capture is enabled only after a successful savechanges
version probe. The launcher prefers distribution-owned **/usr/bin** tools over local shadows.

# PROJECTS

Projects are JSON documents written through the ImageProject backend. Use the
header actions or **Ctrl+N**, **Ctrl+O**, **Ctrl+S**, and **Ctrl+Shift+S** to
create, open, save, or save as. Unsaved changes require confirmation before
closing or replacing the current project.

Capture mode, normalized selected paths, compression, and sensitive-capture
acknowledgement are project intent. Session inventories are runtime-only and are
never written to project files. Exact acknowledgement is stored in the project
and remains in effect until revoked.

Customization fields are also project intent. Older projects without them load
Preserve source defaults. Save As preserves absolute output, background,
overlay, and module targets while invalidating plans when project base changes.

# CAPTURE MODES

The Settings page asks which changes made after the current MiniOS session
started should be copied into the new image. Source modules and the current
configuration are independent of this choice. The no-capture option is the
recommended default when only modules, configuration, boot settings, or image
customization are being changed.

**Do not include session changes**
: Use selected source modules and current configuration without capturing the
writable session layer.

**Include all session changes**
: Preserve every writable change supported by the detected OverlayFS or AUFS
provider. This may include secrets, identities, personal files, and logs, and
requires explicit acknowledgement.

**Include reusable changes only**
: Use the strict software and safe-default allowlist. Personal data, identity,
cache, log, and other broad state is intentionally omitted. This is not a
guarantee that the result is shareable.

**Choose session changes manually**
: Analyze the current session and capture at least one normalized path selected
from the in-memory inventory. Selecting a directory represents descendants;
**savechanges** enforces actual matching. Advanced include/exclude editors expose
all loaded rules, and exact or ancestor exclusions override matching selection.

Administrator authorization is a capability detail rather than a capture mode.
The first option does not capture the writable layer. The other modes use trusted
**savechanges** and may request administrator authorization while the image
builder itself remains unprivileged.

# SESSION INVENTORY

Analyze session changes is an explicit action. It creates a private mode-0700
workspace, obtains narrow savechanges --inventory-json argv from the backend,
uses pkexec only when authorization is needed, validates the mode-0600
result, and removes the workspace by device and inode identity. Command output
and private paths are not logged.

The inventory contains metadata, not file content, but filenames are sensitive.
It remains in memory only. The interface shows aggregate union, entry, byte,
category, sensitive, exact-default, and privacy-cleaned-default counts. Selected
paths appear only in the in-memory selector; Review shows count and digest. The
selector materializes 500 rows at a time with a 2,000-row hard display cap and
reports displayed, matching, and total counts. Search is debounced and filters
the complete inventory. Reanalysis, Source refresh, cancellation/failure, and
new/open project transitions clear inventory, search, filters, and displayed
rows while preserving loaded selection rules. Successful Selected-changes
attestation repeats the Review selection count and verified digest.

# BOOT TESTING

After a successful build, the application can report that external boot testing
is available when an installed VirtualBox or QEMU executable is detected. It
does not install, recommend, configure, or invoke a hypervisor.

# RELATED COMMANDS

**minios-image-compose**(1), **sb**(1), **savechanges**(1)

# AUTHOR

crims0n (crims0n@minios.dev)
