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
disc asynchronously. The page
shows backend, media category, source path, release, version, architecture,
bootloader, size, module counts, and diagnostics.

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
  Configuration values, raw kernel arguments, private customization paths, and
  selected capture paths are never shown. An existing output is replaced
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

Boot controls preserve or set a 0-300 second timeout and default
resume/new/choose/fresh/toram session. Expert kernel arguments are validated for
bootloader-safe syntax. Boot backgrounds must be valid PNG files.

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

**Current composition**
: Use selected source modules and current configuration without writable-session
capture. Composition is unprivileged; a root-only current configuration may
require authorization for the fixed packaged reader.

**Exact session**
: Preserve writable changes supported by the detected OverlayFS or AUFS
provider. This union-specific mode may preserve secrets, identities, personal
files, and logs, and requires explicit acknowledgement.

**Privacy-cleaned**
: Use the strict software and safe-default allowlist. Broad system, identity,
cache, log, and user state is intentionally omitted. This is not a guarantee
that the result is shareable.

**Selected changes**
: Capture at least one normalized path selected from an analyzed inventory.
Selecting a directory represents descendants; **savechanges** enforces actual
matching. Advanced include/exclude editors expose all loaded rules. Exact and
ancestor exclusions override matching row selection.

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
