# MiniOS Image Builder

MiniOS Image Builder is a GTK3 workspace for customizing and remastering MiniOS. It uses the `minios-image-compose` backend to create a bootable ISO, structurally verifies the private result, and publishes it to the selected path only after verification succeeds.

The application runs inside MiniOS and never source-builds MiniOS or modifies source media. The source can be the current session, a MiniOS ISO file, or an optical disc. ISO and optical-disc sources are mounted read-only through `udisksctl`. Session capture from external media is available only when its base-module fingerprint matches the running MiniOS session.

## Workflow

1. **Source** selects and fingerprints the current session, a MiniOS ISO file, or an optical disc without blocking the GTK main loop.
2. **Content** shows every source module, keeps required core and kernel modules locked, and leaves active external modules opt-in.
3. **Settings** configures output, allowlisted system defaults, boot behavior and appearance, an optional project filesystem layer, and optional writable-session capture. Expert controls stay collapsed by default.
4. **Review** creates a fresh secure build plan with module, tool, source, destination, customization, capture, privilege, scratch-space, and sensitive-state diagnostics without listing private input paths or values.
5. **Build** revalidates inputs, runs `minios-image-compose`, structurally verifies the private ISO, customization report, filesystem overlay, and any captured session layer, and atomically publishes the verified output.

Projects are JSON documents created by `ImageProject.save()` and reopened by `ImageProject.load()`. Source paths and fingerprints remain explicit so a changed or unrelated selected source blocks a build rather than being silently accepted.

## Session capture

Four capture profiles are available when `minios-tools` exposes the required contract:

- **Do not include session changes** (`custom`) is the recommended default when only selected source modules, configuration, boot settings, or image customization should be used. The writable session layer is ignored.
- **Include all session changes** (`exact`) preserves writable changes supported by the detected OverlayFS or AUFS provider. It can contain passwords, tokens, machine identity, personal files, and logs, and therefore requires explicit acknowledgement.
- **Include reusable changes only** (`clean`) uses a strict software and safe-default allowlist. It intentionally omits personal data, identity, cache, log, and other broad state. It is not a guarantee that the result is shareable.
- **Choose session changes manually** (`selected`) captures normalized paths explicitly chosen from an analyzed inventory. Selecting a directory represents descendants; `savechanges` enforces actual matching. Persisted include and exclude rules are visible in an advanced editor.

`Analyze session changes` is never automatic. It creates a private mode-0700 workspace, invokes only trusted `/usr/bin/savechanges` through `pkexec` when authorization is needed, validates the mode-0600 inventory, and removes the workspace by identity. Filenames are sensitive metadata: inventory stays in memory, is not saved in the project, and is never copied into Review or logs. Selected paths are persisted in the project because they are explicit project intent, but Review exposes only their count and digest. Successful Selected-changes verification repeats the same include/exclude count and verified selection digest alongside the captured layer attestation.

The selector filters the complete validated inventory but materializes only 500 GTK rows at a time, up to a hard 2,000-row display cap. It reports displayed, matching, and total counts; use search or category filters to inspect entries beyond the cap. Aggregate inventory summaries are cached. Starting another analysis, refreshing or changing Source, cancelling or failing analysis, and opening or creating a project clear all runtime inventory metadata, filters, and displayed rows while preserving loaded include/exclude project intent.

The outer image command is plain `minios-image-compose`. A non-root application authorizes only nested `savechanges` and, when the current configuration is root-only, the fixed packaged live-config reader. The authorized bytes are staged mode 0600 in the private build job; composition remains unprivileged. Inclusion of `/etc/live/config.conf` remains required by the backend. A loaded project that sets `include_current_config=false` retains that intent but is blocked from Review instead of being silently rewritten.

Capture modes are enabled only after a successful `savechanges --version` probe. Completed failures show upgrade guidance rather than an indefinite checking state. The packaged launcher prefers distribution-owned `/usr/bin` tools over `/usr/local` shadows.

The application can detect an installed VirtualBox or QEMU provider and report that boot testing is available externally after a successful build. It does not install, recommend, configure, or invoke a hypervisor.

## Image customization

Settings exposes a compact set of backend-validated project intent:

- **System defaults** can override hostname, timezone, default systemd target, and comma-separated enabled or disabled service names. Empty fields mean preserve the source value.
- **Security & access** contains only backend-allowlisted sudo, polkit, SSH, XRDP, X11, lock-screen, and issue password-hint settings. `Keep current` creates no override.
- **User data** controls link or bind behavior and a validated root-relative user-directory path. Link and bind cannot both be enabled.
- **Boot behavior & appearance** controls a 0-300 second timeout, source-menu default selection, and a boot-menu constructor. The constructor can keep, hide, reorder, duplicate, or add entries based on the five MiniOS session templates (`resume`, `new`, `choose`, `fresh`, `toram`), choose one custom default, edit visible names, and append bootloader-safe kernel parameters to individual entries. Inline completion and contextual help cover common MiniOS options such as `perch*`, `toram*`, `load=`, `noload=`, zRAM, source, localization, and diagnostic parameters. Global expert kernel arguments and a validated PNG background remain available separately.
- **Project filesystem layer** selects an existing canonical real directory or asks the backend to create a private mode-0700 layer under the project directory. Its tree is interpreted relative to image root. The builder never executes scripts, opens a chroot, runs package commands, exposes raw shell, or deletes the selected directory. Reusable `.sb` modules belong in Module Manager.

Background and overlay paths remain canonical absolute runtime targets. Project serialization makes them relative when appropriate. Save As preserves the semantic target of absolute background, overlay, output, and module paths and invalidates any existing plan when the project base changes.

Review shows only customization override key names, boot timeout/default, the visible boot-menu composition and counts of custom/parameterized entries, global kernel-argument byte count and SHA-256, background basename and digest, and overlay basename, fingerprint, entry count, and regular-byte total. It never shows configuration values, raw global or per-entry kernel arguments, or private absolute background/overlay paths. Customization runs rootlessly; the separate session capture section is the only operation that may request `savechanges` authorization.

Build executes the backend argv in its descriptor-bound `execution_cwd` and logs only `display_argv`. Successful verification shows attested override key names, boot settings, kernel count/digest, background basename/digest, and overlay module basename, size, SHA-256, and input-tree fingerprint. The session capture attestation remains alongside it when both features are requested.

## Cancellation boundary

Inventory, build, and verification subprocesses run in dedicated process groups. Cancel sends `SIGTERM` and escalates to `SIGKILL` after a short grace period to the retained process-group ID even if the original leader has already exited, so descendants holding output pipes are terminated. Inventory cancel first asks the backend to create an identity-bound private cancel marker and then signals the process group as fallback. Build cancellation signals plain `minios-image-compose`; its own trap creates and handles the internal capture marker. Pure Python source hashing cannot be interrupted in the middle of one hashing pass; the result is discarded at the next safe checkpoint. Once atomic publication starts, it is allowed to finish so the destination cannot be left half-written.

Replacing an existing destination requires confirmation against its observed device, inode, size, timestamp, and SHA-256 identity. Cancellation, any build, verification, or publication failure, and an observation mismatch clear that approval. Every retry therefore requires a new confirmation and plan.

## Requirements

- Python 3.6 or newer
- GTK 3, PyGObject, GLib, and Gio introspection data
- Debian package `minios-tools` 1.5.0 or newer provides `savechanges` 1.3.0 for session capture. The matching `minios-image-compose` package provides the composition backend. Session inventory/capture contracts and stable `P:<id>` phase records remain required.
- `python3-minios-gui` 1.2.0 or newer
- `xorriso`, `squashfs-tools`, `e2fsprogs`, `gettext-base`, and `mawk`
- `udisks2` for ISO-file and optical-disc sources
- `pkexec`; a desktop polkit authentication agent is recommended for reading a root-only current config and for non-root session capture

The application and builds without session capture do not run with root privileges. Authorization is limited to the fixed config reader and trusted `savechanges`. The launcher augments a desktop session's `PATH` with standard `sbin` directories while preserving existing entries and preferring `/usr/bin` over local shadows. From a source checkout:

```sh
./bin/minios-image-builder
```

## Context help

Editable contextual-help sources live under `help/<locale>/`. They are Markdown
authoring files only; the application package ships the generated parser-free
`share/help/<locale>/*.json` documents. The shared Markdown compiler and its
pinned npm dependencies belong to the sibling `minios-gui` repository. Refresh
the bundle with:

```sh
../minios-gui/tools/npm-ci.sh
make compile-help
```

The generated bundle is committed, so normal Debian package builds do not need
Node.js, npm, Mermaid, or a browser. Mermaid blocks, if added later, are rendered
to static SVG by the shared compiler during this refresh step.

## Testing

The core controller tests have no GTK dependency; the runtime suite also checks the GTK command runner and launcher:

```sh
pytest -q -W error
```

Run the complete Python and Bats test suite with:

```sh
make test
```

Syntax and desktop metadata checks require `desktop-file-utils` and are available through:

```sh
make check
```

## Debian package

``` sh
dpkg-buildpackage -b -uc -us
```

## License

Distributed under the GNU General Public License v2 or later.
