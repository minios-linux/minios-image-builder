#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Secure backend for customizing a selected MiniOS image.

The bounded product represented here remixes a selected MiniOS
source.  It never source-builds MiniOS and never modifies source media.

Builds use an explicit ``minios-image-compose`` source/config contract and a private,
mode-0700 job directory beside the destination.  Structural verification is
bound to the exact BuildPlan and publication repeats verification.  Python 3.6
does not expose Linux ``renameat2(RENAME_NOREPLACE)``: new outputs therefore use
an atomic hard-link publication, while explicitly approved replacement of an
existing output is identity-checked immediately before ``os.replace``.  A
process with the same uid (or root) can still race path operations; retaining a
private job directory and checking inode identities narrows that unavoidable
limit.

Only the Python standard library is used.  This module has no GTK dependency.
"""

from __future__ import absolute_import

import errno
import grp
import hashlib
import json
import os
import posixpath
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import zlib
from types import MappingProxyType


PROJECT_KIND = 'minios-image-project'
PROJECT_SCHEMA_VERSION = 1
BUILD_PLAN_KIND = 'minios-image-build-plan'
VERIFICATION_KIND = 'minios-image-verification'

MODULE_MANAGER_HANDOFF_KIND = 'minios-module-manager-handoff'
MODULE_MANAGER_HANDOFF_SCHEMA_VERSION = 1
STORE_INSTALL_INTENT_KIND = 'minios-store-application-install-intent'
STORE_INSTALL_INTENT_SCHEMA_VERSION = 1

SOURCE_KIND = 'running-minios'
SOURCE_SUPPORTED = 'supported'
SOURCE_UNSUPPORTED = 'unsupported'
SOURCE_ERROR = 'error'

CURRENT_COMPOSITION = 'custom'
NO_SESSION_CAPTURE = CURRENT_COMPOSITION
SUPPORTED_CAPTURE_MODE = CURRENT_COMPOSITION
CAPTURE_MODES = ('custom', 'exact', 'clean', 'selected')
SESSION_CAPTURE_MODES = ('exact', 'clean', 'selected')
CAPTURE_COMPRESSIONS = ('zstd', 'gzip', 'lzo', 'xz')
SESSION_SELECTION_KIND = 'minios-session-selection'
SESSION_SELECTION_SCHEMA_VERSION = 1
SESSION_INVENTORY_KIND = 'minios-session-inventory'
SESSION_INVENTORY_SCHEMA_VERSION = 2
SESSION_CAPTURE_REPORT_KIND = 'minios-session-capture-report'
SESSION_CAPTURE_REPORT_SCHEMA_VERSION = 3
MAX_SESSION_INVENTORY_BYTES = 64 * 1024 * 1024
IMAGE_CUSTOMIZATION_REPORT_KIND = 'minios-image-customization'
IMAGE_CUSTOMIZATION_REPORT_SCHEMA_VERSION = 1
MAX_CUSTOMIZATION_REPORT_BYTES = 1024 * 1024
MAX_LIVE_CONFIG_BYTES = 16 * 1024 * 1024
MAX_BUILD_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_KERNEL_ARGUMENT_BYTES = 4096

BUILD_PHASE_PREPARE = 'prepare'
BUILD_PHASE_CUSTOMIZE = 'customize'
BUILD_PHASE_CAPTURE = 'capture'
BUILD_PHASE_BOOT_COPY = 'boot-copy'
BUILD_PHASE_ORDER = (
    BUILD_PHASE_PREPARE, BUILD_PHASE_CUSTOMIZE, BUILD_PHASE_CAPTURE,
    BUILD_PHASE_BOOT_COPY,
    'persistence', 'iso-write', 'verify', 'complete',
)
CAPTURE_PHASE_IDS = (
    'capture-inventory', 'capture-copy', 'capture-compress',
    'capture-complete',
)
MENU_LOCALES = (
    'multilang', 'en_US', 'ru_RU', 'de_DE', 'es_ES', 'it_IT', 'id_ID',
    'pt_BR', 'pt_PT', 'fr_FR',
)

VERIFICATION_NOT_BUILT = 'not_built'
VERIFICATION_BUILT = 'built'
VERIFICATION_STRUCTURAL = 'structurally_verified'

SOURCE_FINGERPRINT_ALGORITHM = 'effective-content-sha256-v2'
HASH_CHUNK_SIZE = 1024 * 1024
MAX_PROJECT_BYTES = 2 * 1024 * 1024
MIN_DESTINATION_HEADROOM = 256 * 1024 * 1024
MIN_SCRATCH_HEADROOM = 512 * 1024 * 1024
# Fixed working-memory reserve for tool buffers, inventory, and framing that a
# RAM-backed workspace must hold on top of its staged bytes.
MIN_MEMORY_HEADROOM = 256 * 1024 * 1024
# Filesystem backing classes the resource planner reports for the destination
# and scratch work areas. RAM-backed selections are allowed but accounted for
# against available memory, not just free space.
RAM_BACKED_FSTYPES = ('tmpfs', 'ramfs')
FILESYSTEM_CLASS_PERSISTENT = 'persistent'
FILESYSTEM_CLASS_RAM_BACKED = 'ram-backed'
FILESYSTEM_CLASS_LIVE_OVERLAY = 'live-overlay-backed'
FILESYSTEM_CLASS_REMOVABLE = 'removable'
FILESYSTEM_CLASS_UNKNOWN = 'unknown'
DEFAULT_LIVE_CHANGES_ROOTS = (
    '/run/initramfs/memory/changes',
    '/lib/live/mount/changes',
)
_PERSISTENT_FSTYPES = (
    'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'f2fs', 'reiserfs', 'jfs',
    'vfat', 'exfat', 'ntfs', 'ntfs3', 'zfs', 'nilfs2',
)
_REMOVABLE_FSTYPES = ('iso9660', 'udf')

DEFAULT_RUNNING_ROOTS = (
    ('livekit', '/run/initramfs/memory'),
    ('dracut', '/lib/live/mount'),
)
SOURCE_SUBDIRECTORIES = ('data', 'medium', 'iso')
# Source backends that name a read-only mounted MiniOS medium (ISO image file
# or optical disc) whose minios/ tree sits at the mount root.
MOUNTED_SOURCE_BACKENDS = ('iso', 'optical')
REQUIRED_TOOL_NAMES = ('xorriso', 'mkfs.ext2', 'unsquashfs')
# Canonical composition backend. Its presence is guaranteed by an exact package
# dependency (minios-image-builder depends on the matching minios-image-compose
# version), so it is resolved at a fixed path and is never probed for version,
# advertised options, or executable basename. A missing fixed path is a broken
# installation, and any execution failure is reported normally.
COMPOSE_BACKEND_NAME = 'minios-image-compose'
COMPOSE_BACKEND_PATH = '/usr/bin/minios-image-compose'
SAVECHANGES_MIN_VERSION = (1, 3, 0)
COMPOSE_REQUIRED_OPTIONS = (
    '--source', '--config', '--name', '--menu', '--manifest',
)
COMPOSE_OPTIONAL_OPTIONS = ('--volume-label', '--exclude')
COMPOSE_CAPTURE_OPTIONS = (
    '--capture-changes', '--capture-selection', '--capture-compression',
)
COMPOSE_CUSTOMIZATION_OPTIONS = (
    '--boot-timeout', '--default-boot', '--boot-menu-json', '--kernel-args',
    '--boot-background', '--overlay-directory',
)
DEFAULT_BOOT_MODES = ('resume', 'new', 'choose', 'fresh', 'toram')
BOOT_MENU_TITLE_MAX_BYTES = 512
BOOT_MENU_MAX_ENTRIES = 32
BOOT_MENU_MAX_JSON_BYTES = 65536
_BOOT_MENU_ENTRY_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$')
LIVE_CONFIG_OVERRIDE_KEYS = (
    'DEFAULT_TARGET', 'DISABLE_SERVICES', 'ENABLE_SERVICES', 'EXPORT_LOGS',
    'LIVE_BIND_USER_DIRS', 'LIVE_CONFIG_CMDLINE', 'LIVE_CONFIG_DEBUG',
    'LIVE_CONFIG_NOROOT', 'LIVE_HOSTNAME', 'LIVE_ISSUE_PASSWORD_HINTS',
    'LIVE_KEYBOARD_LAYOUTS', 'LIVE_KEYBOARD_MODEL', 'LIVE_KEYBOARD_OPTIONS',
    'LIVE_KEYBOARD_VARIANTS', 'LIVE_LINK_USER_DIRS',
    'LIVE_LOCKSCREEN_MODE', 'LIVE_LOCALES', 'LIVE_MODULE_MODE',
    'LIVE_POLKIT_MODE', 'LIVE_SECURITY_PROFILE',
    'LIVE_SSH_PASSWORD_AUTHENTICATION', 'LIVE_SSH_PERMIT_ROOT_LOGIN',
    'LIVE_SUDO_MODE', 'LIVE_TIMEZONE', 'LIVE_USER_DEFAULT_GROUPS',
    'LIVE_USER_DIRS_PATH', 'LIVE_USER_FULLNAME', 'LIVE_USERNAME',
    'LIVE_X11_MODE', 'LIVE_XRDP_MODE',
)

_MODULE_ORDER_RE = re.compile(r'^(\d+)(?:[-_.]|$)')
_COMPOSE_TOP_LEVEL_RE = re.compile(r'^[0-9]{2}-')
_SAFE_MODULE_BASENAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]*\.sb$')
_APPLICATION_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$')
_OVERLAY_DIRECTORY_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
_SENSITIVE_CONFIG_KEY_RE = re.compile(
    r'(PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|API_KEY|PRIVATE_KEY|CREDENTIAL|PSK)',
    re.IGNORECASE)
# Already-hashed digests (LIVE_USER_PASSWORD_CRYPTED, *_HASH) are not plaintext
# secrets. The standard MiniOS live configuration ships them, so they must not
# be flagged as sensitive plaintext.
_HASHED_CONFIG_KEY_RE = re.compile(r'(_CRYPTED|_HASH|_HASHED)$', re.IGNORECASE)
_KERNEL_ARGUMENT_FORBIDDEN = frozenset('\\\"\'`$;&|<>(){}[]*?!#')
_LIVE_CONFIG_BOOLEAN_KEYS = frozenset((
    'EXPORT_LOGS', 'LIVE_BIND_USER_DIRS', 'LIVE_CONFIG_DEBUG',
    'LIVE_CONFIG_NOROOT', 'LIVE_ISSUE_PASSWORD_HINTS',
    'LIVE_LINK_USER_DIRS', 'LIVE_SSH_PASSWORD_AUTHENTICATION',
    'LIVE_SSH_PERMIT_ROOT_LOGIN',
))
_LIVE_CONFIG_ENUMS = {
    'DEFAULT_TARGET': frozenset((
        '', 'graphical', 'graphical.target', 'multi-user',
        'multi-user.target', 'rescue', 'rescue.target')),
    'LIVE_LOCKSCREEN_MODE': frozenset(('', 'relaxed', 'hardened')),
    'LIVE_MODULE_MODE': frozenset(('', 'simple', 'merged')),
    'LIVE_POLKIT_MODE': frozenset(('', 'passwordless', 'password', 'disabled')),
    'LIVE_SECURITY_PROFILE': frozenset(('', 'convenient', 'balanced', 'strict')),
    'LIVE_SUDO_MODE': frozenset(('', 'passwordless', 'password', 'disabled')),
    'LIVE_X11_MODE': frozenset(('', 'relaxed', 'hardened')),
    'LIVE_XRDP_MODE': frozenset(('', 'relaxed', 'hardened', 'disabled')),
}
_ARCHITECTURES = (
    'amd64', 'x86_64', 'i386', 'i686', 'arm64', 'aarch64', 'armhf',
)

_MODULE_ROLE_RULES = (
    (('core', 'minios'), 'core', 'Core system',
     'Base filesystem and essential system utilities.'),
    (('kernel',), 'kernel', 'Kernel and drivers',
     'Linux kernel and hardware drivers.'),
    (('firmware', 'ucode', 'microcode'), 'firmware', 'Hardware firmware',
     'Firmware for graphics, networking, and other devices.'),
    (('gui-base', 'guibase', 'xorg', 'x11', 'wayland'), 'gui-base',
     'Graphical base', 'Display server and desktop graphics stack.'),
    (('desktop', 'xfce', 'kde', 'plasma', 'gnome', 'lxqt', 'lxde', 'mate',
      'cinnamon', 'fluxbox', 'openbox'), 'desktop', 'Desktop environment',
     'Desktop shell, window manager, and supporting applications.'),
    (('firefox', 'chromium', 'chrome', 'browser'), 'browser', 'Web browser',
     'Web browser and its runtime.'),
    (('toolbox', 'tools'), 'toolbox', 'Toolbox utilities',
     'Additional command-line and system utilities.'),
    (('apps', 'application', 'software', 'office', 'ultra'), 'applications',
     'Application bundle', 'Bundled user applications.'),
)

_PLAN_TOKEN = object()
_VERIFICATION_TOKEN = object()


class ImageProjectError(Exception):
    """Base exception for backend failures."""


class ProjectFormatError(ImageProjectError):
    """A project or handoff document is malformed."""


class UnsupportedSchemaError(ProjectFormatError):
    """A document uses an unknown kind or schema version."""


class SourceInspectionError(ImageProjectError):
    """A source cannot be inspected without ambiguity."""


class OutputPublishError(ImageProjectError):
    """A verified output cannot be published safely."""


class _Immutable(object):
    """Plain Python record base that blocks mutation after construction."""

    __slots__ = ('_locked',)

    def __setattr__(self, name, value):
        if getattr(self, '_locked', False):
            raise AttributeError('{} is immutable'.format(
                self.__class__.__name__))
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if getattr(self, '_locked', False):
            raise AttributeError('{} is immutable'.format(
                self.__class__.__name__))
        object.__delattr__(self, name)

    def _lock(self):
        object.__setattr__(self, '_locked', True)


def _freeze(value):
    if isinstance(value, MappingProxyType):
        return value
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item)
                                 for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(_thaw(item) for item in value)
    return value


def _path_string(value, field='path'):
    if hasattr(os, 'fspath'):
        try:
            value = os.fspath(value)
        except TypeError:
            pass
    if not isinstance(value, str) or not value:
        raise ValueError('{} must be a non-empty string'.format(field))
    if '\x00' in value:
        raise ValueError('{} contains a NUL byte'.format(field))
    return os.path.normpath(value)


def _optional_string(value, field, allow_empty=False):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('{} must be a string or null'.format(field))
    if '\x00' in value:
        raise ValueError('{} contains a NUL byte'.format(field))
    if not allow_empty and not value:
        raise ValueError('{} must not be empty'.format(field))
    return value


def _require_bool(value, field):
    if not isinstance(value, bool):
        raise ValueError('{} must be a boolean'.format(field))
    return value


def _unique_strings(values, field):
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ValueError('{} must be a list of strings'.format(field))
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value or '\x00' in value:
            raise ValueError('{} must contain non-empty strings'.format(field))
        if value in seen:
            raise ValueError('{} contains duplicate value {!r}'.format(
                field, value))
        seen.add(value)
        result.append(value)
    return tuple(result)


def _resolve_path(path, base_dir, field='path'):
    path = _path_string(path, field)
    if not base_dir:
        raise ValueError('an explicit project base is required')
    if os.path.isabs(path):
        return os.path.abspath(path)
    return os.path.abspath(os.path.join(base_dir, path))


def _relative_path(path, base_dir):
    try:
        return os.path.relpath(path, base_dir)
    except (OSError, ValueError) as error:
        raise ValueError('path cannot be serialized relative to project: {}'.format(
            error))


def _candidate_real_path(path):
    path = os.path.abspath(path)
    parent = os.path.realpath(os.path.dirname(path) or os.curdir)
    return os.path.join(parent, os.path.basename(path))


def _is_within(path, directory):
    try:
        path = _candidate_real_path(path)
        directory = os.path.realpath(directory)
        return os.path.commonpath((path, directory)) == directory
    except (AttributeError, OSError, ValueError):
        directory = os.path.realpath(directory).rstrip(os.sep) + os.sep
        return _candidate_real_path(path).startswith(directory)


def _same_path(left, right):
    if not left or not right:
        return False
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
        os.path.realpath(right))


def _identity(file_stat):
    return (int(file_stat.st_dev), int(file_stat.st_ino))


def _stat_mtime_ns(file_stat):
    value = getattr(file_stat, 'st_mtime_ns', None)
    if value is not None:
        return int(value)
    return int(file_stat.st_mtime * 1000000000)


def _fsync_directory(directory):
    flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY'):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _mode_is_writable_directory(path):
    try:
        file_stat = os.stat(path)
    except OSError:
        return False
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    return (stat.S_ISDIR(file_stat.st_mode) and
            bool(file_stat.st_mode & write_bits) and
            bool(file_stat.st_mode & execute_bits) and
            os.access(path, os.W_OK | os.X_OK))


def _is_real_directory(path):
    try:
        file_stat = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(file_stat.st_mode) and not stat.S_ISLNK(file_stat.st_mode)


def _probe_private_workspace(directory):
    """Return an error if a secure child workspace cannot be created here."""
    parent_fd = None
    probe_fd = None
    probe_name = None
    error_message = None
    try:
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        parent_fd = os.open(directory, flags)
        for _unused in range(128):
            candidate = '.minios-image-builder-probe-{}'.format(
                os.urandom(12).hex())
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_fd)
                probe_name = candidate
                break
            except FileExistsError:
                continue
        if probe_name is None:
            raise OSError('cannot reserve a private workspace')
        probe_fd = os.open(probe_name, flags, dir_fd=parent_fd)
        os.fchmod(probe_fd, 0o700)
        file_stat = os.fstat(probe_fd)
        if (stat.S_ISLNK(file_stat.st_mode) or
                not stat.S_ISDIR(file_stat.st_mode) or
                stat.S_IMODE(file_stat.st_mode) != 0o700):
            raise OSError('filesystem cannot enforce mode 0700')
        if (hasattr(os, 'geteuid') and
                file_stat.st_uid != os.geteuid()):
            raise OSError('temporary directory has an unexpected owner')
    except (OSError, TypeError, ValueError) as error:
        error_message = str(error)
    if probe_fd is not None:
        os.close(probe_fd)
    if probe_name is not None and parent_fd is not None:
        try:
            os.rmdir(probe_name, dir_fd=parent_fd)
        except OSError as error:
            if error_message is None:
                error_message = 'cannot remove workspace probe: {}'.format(
                    error)
    if parent_fd is not None:
        os.close(parent_fd)
    return error_message


def _scratch_path_trust_error(directory):
    """Reject path components that another unprivileged user can replace."""
    current = os.path.realpath(directory)
    expected_owners = {0}
    if hasattr(os, 'geteuid'):
        expected_owners.add(os.geteuid())
    while True:
        try:
            file_stat = os.lstat(current)
        except OSError as error:
            return str(error)
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISDIR(file_stat.st_mode):
            return 'path contains a non-directory component'
        if file_stat.st_uid not in expected_owners:
            return 'path contains a directory owned by another user'
        shared_write = bool(file_stat.st_mode & stat.S_IWOTH)
        if file_stat.st_mode & stat.S_IWGRP:
            try:
                acl = os.getxattr(
                    current, 'system.posix_acl_access', follow_symlinks=False)
            except OSError as error:
                acl = None
                if error.errno not in (
                        errno.ENODATA, errno.ENOTSUP,
                        getattr(errno, 'EOPNOTSUPP', errno.ENOTSUP)):
                    shared_write = True
            if acl:
                shared_write = True
            elif not shared_write:
                try:
                    owner_name = pwd.getpwuid(os.geteuid()).pw_name
                    group = grp.getgrgid(file_stat.st_gid)
                    group_users = set(group.gr_mem)
                    group_users.update(
                        item.pw_name for item in pwd.getpwall()
                        if item.pw_gid == file_stat.st_gid)
                    group_users.discard('root')
                    group_users.discard(owner_name)
                    shared_write = bool(group_users)
                except (KeyError, OSError):
                    shared_write = True
        if shared_write and not (file_stat.st_mode & stat.S_ISVTX):
            return 'path contains a shared writable directory without sticky bit'
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _decode_output(value):
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


def _runner_result(result):
    if isinstance(result, dict):
        return (int(result.get('returncode', 0)),
                _decode_output(result.get('stdout', '')),
                _decode_output(result.get('stderr', '')))
    if isinstance(result, (tuple, list)) and len(result) == 3:
        return (int(result[0]), _decode_output(result[1]),
                _decode_output(result[2]))
    return (int(getattr(result, 'returncode', 0)),
            _decode_output(getattr(result, 'stdout', '')),
            _decode_output(getattr(result, 'stderr', '')))


def _run_command(runner, argv, input_data=None):
    if runner is None:
        if isinstance(input_data, str):
            input_data = input_data.encode('utf-8')
        kwargs = {
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'check': False,
        }
        if input_data is None:
            kwargs['stdin'] = subprocess.DEVNULL
        result = subprocess.run(argv, input=input_data, **kwargs)
    elif hasattr(runner, 'run'):
        if input_data is None:
            result = runner.run(argv)
        else:
            result = runner.run(argv, input_data=input_data)
    else:
        if input_data is None:
            result = runner(argv)
        else:
            result = runner(argv, input_data=input_data)
    return _runner_result(result)


def _json_digest(value):
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':')).encode('ascii')
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value):
    try:
        serialized = json.dumps(
            value, ensure_ascii=True, allow_nan=False, indent=2,
            sort_keys=True, separators=(',', ': ')) + '\n'
    except (TypeError, ValueError) as error:
        raise ImageProjectError(
            'Document is not serializable: {}'.format(error))
    return serialized.encode('ascii')


def _hash_descriptor(descriptor, chunk_size=HASH_CHUNK_SIZE):
    hasher = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, chunk_size)
        if not chunk:
            break
        hasher.update(chunk)
    return hasher.hexdigest()


def sha256_file(path, chunk_size=HASH_CHUNK_SIZE):
    """Hash a regular file while rejecting a final-component symlink."""
    path = _path_string(path)
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError(errno.EINVAL, 'not a regular file', path)
        return _hash_descriptor(descriptor, chunk_size)
    finally:
        os.close(descriptor)


def _metadata_snapshot(file_stat):
    return (
        int(file_stat.st_dev), int(file_stat.st_ino), int(file_stat.st_mode),
        int(file_stat.st_nlink), int(file_stat.st_uid), int(file_stat.st_gid),
        int(file_stat.st_rdev), int(file_stat.st_size),
        _stat_mtime_ns(file_stat),
        int(getattr(file_stat, 'st_ctime_ns',
                    file_stat.st_ctime * 1000000000)),
    )


def _matches_expected_identity(file_stat, expected_identity):
    expected = tuple(expected_identity)
    if len(expected) == 2:
        return _identity(file_stat) == expected
    if len(expected) == len(_metadata_snapshot(file_stat)):
        return _metadata_snapshot(file_stat) == expected
    raise ValueError('expected identity has an unsupported shape')


def _read_stable_regular_bytes(path, maximum_bytes):
    path = os.path.abspath(_path_string(path))
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ImageProjectError('input must be a non-symlink regular file')
    if before.st_size > maximum_bytes:
        raise ImageProjectError('input exceeds the supported size')
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or
                _identity(opened) != _identity(before) or
                opened.st_size > maximum_bytes):
            raise ImageProjectError('input identity changed while opening')
        blocks = []
        total = 0
        while True:
            block = os.read(descriptor, HASH_CHUNK_SIZE)
            if not block:
                break
            total += len(block)
            if total > maximum_bytes:
                raise ImageProjectError('input exceeds the supported size')
            blocks.append(block)
        after = os.fstat(descriptor)
        if (_metadata_snapshot(after) != _metadata_snapshot(opened) or
                total != opened.st_size):
            raise ImageProjectError('input changed while being read')
        return b''.join(blocks), after
    finally:
        os.close(descriptor)


def _png_metadata_from_payload(data):
    if len(data) < 57 or data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('boot background has an invalid PNG structure')
    offset = 8
    chunk_index = 0
    width = height = bit_depth = color_type = None
    seen_ihdr = False
    seen_plte = False
    seen_idat = False
    idat_closed = False
    idat_bytes = 0
    idat_parts = []
    seen_iend = False
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError('PNG has a truncated chunk header')
        length = int.from_bytes(data[offset:offset + 4], 'big')
        chunk_type = data[offset + 4:offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            raise ValueError('PNG chunk exceeds the input bounds')
        if (len(chunk_type) != 4 or
                any(value not in range(ord('A'), ord('Z') + 1) and
                    value not in range(ord('a'), ord('z') + 1)
                    for value in chunk_type) or
                not chr(chunk_type[2]).isupper()):
            raise ValueError('PNG chunk type is invalid')
        payload = data[offset + 8:offset + 8 + length]
        expected_crc = int.from_bytes(
            data[offset + 8 + length:chunk_end], 'big')
        if zlib.crc32(chunk_type + payload) & 0xffffffff != expected_crc:
            raise ValueError('PNG chunk CRC mismatch')
        if chunk_type == b'IHDR':
            if seen_ihdr or chunk_index != 0 or length != 13:
                raise ValueError('PNG must contain one leading IHDR')
            width = int.from_bytes(payload[0:4], 'big')
            height = int.from_bytes(payload[4:8], 'big')
            bit_depth, color_type, compression, filtering, interlace = payload[8:13]
            supported_depths = {2: {8}, 3: {1, 2, 4, 8}, 6: {8}}
            if (color_type not in supported_depths or
                    bit_depth not in supported_depths[color_type] or
                    compression != 0 or filtering != 0 or interlace != 0):
                raise ValueError('PNG pixel format is unsupported')
            seen_ihdr = True
        elif not seen_ihdr:
            raise ValueError('PNG IHDR is not the first chunk')
        elif chunk_type == b'PLTE':
            if seen_plte or seen_idat or not length or length > 768 or length % 3:
                raise ValueError('PNG palette has invalid order or size')
            if color_type == 3 and length // 3 > 2 ** bit_depth:
                raise ValueError('PNG palette exceeds its indexed bit depth')
            seen_plte = True
        elif chunk_type == b'IDAT':
            if idat_closed:
                raise ValueError('PNG IDAT chunks are not consecutive')
            seen_idat = True
            idat_bytes += length
            idat_parts.append(payload)
        elif chunk_type == b'IEND':
            if seen_iend or length != 0 or not seen_idat or not idat_bytes:
                raise ValueError('PNG IEND or IDAT structure is invalid')
            seen_iend = True
            offset = chunk_end
            if offset != len(data):
                raise ValueError('PNG has trailing data after IEND')
            break
        else:
            if seen_idat:
                idat_closed = True
            if chr(chunk_type[0]).isupper():
                raise ValueError('PNG contains an unknown critical chunk')
        offset = chunk_end
        chunk_index += 1
    if not seen_iend or (color_type == 3 and not seen_plte):
        raise ValueError('PNG is missing a required chunk')
    if not (1 <= width <= 8192 and 1 <= height <= 8192):
        raise ValueError('PNG dimensions must be between 1 and 8192 pixels')
    channels = {2: 3, 3: 1, 6: 4}[color_type]
    row_size = (width * channels * bit_depth + 7) // 8 + 1
    rows = 0
    pending = bytearray()
    decompressor = zlib.decompressobj()

    def consume_scanlines(block):
        nonlocal rows
        pending.extend(block)
        while len(pending) >= row_size:
            if pending[0] > 4:
                raise ValueError('PNG scanline uses an invalid filter')
            del pending[:row_size]
            rows += 1
            if rows > height:
                raise ValueError('PNG data exceeds its dimensions')

    try:
        for compressed in idat_parts:
            remaining = compressed
            while remaining:
                block = decompressor.decompress(remaining, 65536)
                remaining = decompressor.unconsumed_tail
                consume_scanlines(block)
        consume_scanlines(decompressor.flush())
    except zlib.error:
        raise ValueError('PNG IDAT data is not a valid zlib stream')
    if (not decompressor.eof or decompressor.unused_data or pending or
            rows != height):
        raise ValueError('PNG scanlines do not match its dimensions')
    return {
        'width': width,
        'height': height,
        'size': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
    }


def boot_background_metadata(path):
    """Validate a bootloader-compatible PNG and return safe metadata."""
    payload, unused_stat = _read_stable_regular_bytes(
        path, MAX_LIVE_CONFIG_BYTES)
    return _png_metadata_from_payload(payload)


def overlay_fingerprint(records):
    """Return the exact input-tree fingerprint used by minios-image-compose."""
    digest = hashlib.sha256()
    digest.update(b'minios-image-overlay-v1\x00')
    for record in sorted(records, key=lambda item: item['path']):
        for value in (
                record['path'], record['kind'], str(record['mode']),
                str(record.get('mtime_ns', 0)), str(record.get('size', 0)),
                record.get('digest', ''), record.get('target', '')):
            if not isinstance(value, str):
                value = str(value)
            digest.update(value.encode('utf-8', 'strict'))
            digest.update(b'\x00')
    return digest.hexdigest()


def _validate_overlay_component(name):
    try:
        name.encode('utf-8', 'strict')
    except UnicodeError:
        raise ImageProjectError('overlay path is not valid UTF-8')
    if (not name or name in ('.', '..') or
            any(not character.isprintable() for character in name)):
        raise ImageProjectError('overlay path contains an unsafe component')


def _hash_overlay_regular(directory_descriptor, name, expected_metadata):
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    if hasattr(os, 'O_CLOEXEC'):
        flags |= os.O_CLOEXEC
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or
                _metadata_snapshot(opened) !=
                _metadata_snapshot(expected_metadata)):
            raise ImageProjectError(
                'overlay regular file changed while opening')
        digest = _hash_descriptor(descriptor)
        final_metadata = os.fstat(descriptor)
        if _metadata_snapshot(final_metadata) != _metadata_snapshot(opened):
            raise ImageProjectError(
                'overlay regular file changed while hashing')
        return digest, final_metadata
    finally:
        os.close(descriptor)


def _overlay_tree_once(root_path):
    root_descriptor = _open_absolute_directory_nofollow(root_path)
    try:
        root_stat = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ImageProjectError('overlay source must be a real directory')
        records = [{
            'path': '.', 'kind': 'directory',
            'mode': stat.S_IMODE(root_stat.st_mode) & 0o777,
            'mtime_ns': _stat_mtime_ns(root_stat),
        }]
        identities = [{
            'path': '.', 'snapshot': _metadata_snapshot(root_stat),
        }]

        def walk(directory_descriptor, relative, expected_stat):
            try:
                names = os.listdir(directory_descriptor)
            except OSError as error:
                raise ImageProjectError(
                    'cannot list overlay directory: {}'.format(error))
            for name in names:
                _validate_overlay_component(name)
            names.sort(key=lambda value: value.encode('utf-8', 'strict'))
            for name in names:
                relative_path = name if not relative else relative + '/' + name
                metadata = os.stat(
                    name, dir_fd=directory_descriptor,
                    follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    if metadata.st_dev != root_stat.st_dev:
                        raise ImageProjectError(
                            'overlay directory crosses a filesystem boundary')
                    child_descriptor = os.open(
                        name, _directory_open_flags(),
                        dir_fd=directory_descriptor)
                    try:
                        opened = os.fstat(child_descriptor)
                        if (_metadata_snapshot(opened) !=
                                _metadata_snapshot(metadata)):
                            raise ImageProjectError(
                                'overlay directory changed while opening')
                        records.append({
                            'path': relative_path, 'kind': 'directory',
                            'mode': stat.S_IMODE(opened.st_mode) & 0o777,
                            'mtime_ns': _stat_mtime_ns(opened),
                        })
                        identities.append({
                            'path': relative_path,
                            'snapshot': _metadata_snapshot(opened),
                        })
                        walk(child_descriptor, relative_path, opened)
                        if (_metadata_snapshot(os.fstat(child_descriptor)) !=
                                _metadata_snapshot(opened)):
                            raise ImageProjectError(
                                'overlay directory changed while scanning')
                    finally:
                        os.close(child_descriptor)
                elif stat.S_ISREG(metadata.st_mode):
                    digest, opened = _hash_overlay_regular(
                        directory_descriptor, name, metadata)
                    records.append({
                        'path': relative_path, 'kind': 'regular',
                        'mode': stat.S_IMODE(opened.st_mode) & 0o777,
                        'mtime_ns': _stat_mtime_ns(opened),
                        'size': int(opened.st_size), 'digest': digest,
                    })
                    identities.append({
                        'path': relative_path,
                        'snapshot': _metadata_snapshot(opened),
                    })
                elif stat.S_ISLNK(metadata.st_mode):
                    target = os.readlink(name, dir_fd=directory_descriptor)
                    try:
                        encoded_target = target.encode('utf-8', 'strict')
                    except UnicodeError:
                        raise ImageProjectError(
                            'overlay symbolic-link target is not valid UTF-8')
                    after = os.stat(
                        name, dir_fd=directory_descriptor,
                        follow_symlinks=False)
                    if (_metadata_snapshot(after) !=
                            _metadata_snapshot(metadata)):
                        raise ImageProjectError(
                            'overlay symbolic link changed while being read')
                    if (not target or target.startswith('/') or
                            b'\x00' in encoded_target or
                            any(value < 32 or value == 127
                                for value in encoded_target)):
                        raise ImageProjectError(
                            'overlay symbolic-link target is unsafe')
                    resolved = posixpath.normpath(posixpath.join(
                        posixpath.dirname(relative_path), target))
                    if resolved == '..' or resolved.startswith('../'):
                        raise ImageProjectError(
                            'overlay symbolic link escapes the overlay root')
                    records.append({
                        'path': relative_path, 'kind': 'symlink', 'mode': 0o777,
                        'mtime_ns': _stat_mtime_ns(metadata), 'target': target,
                        'digest': hashlib.sha256(encoded_target).hexdigest(),
                    })
                    identities.append({
                        'path': relative_path,
                        'snapshot': _metadata_snapshot(metadata),
                    })
                else:
                    raise ImageProjectError(
                        'overlay tree contains an unsupported filesystem object')
            if (_metadata_snapshot(os.fstat(directory_descriptor)) !=
                    _metadata_snapshot(expected_stat)):
                raise ImageProjectError(
                    'overlay directory changed while scanning')

        walk(root_descriptor, '', root_stat)
        if (_metadata_snapshot(os.fstat(root_descriptor)) !=
                _metadata_snapshot(root_stat)):
            raise ImageProjectError('overlay root changed while scanning')
        verification_descriptor = _open_absolute_directory_nofollow(root_path)
        try:
            if (_identity(os.fstat(verification_descriptor)) !=
                    _identity(root_stat)):
                raise ImageProjectError(
                    'overlay root identity changed while scanning')
        finally:
            os.close(verification_descriptor)
        return tuple(records), tuple(identities)
    finally:
        os.close(root_descriptor)


def _overlay_inventory(path):
    path = os.path.abspath(_path_string(path, 'overlay_directory'))
    if os.path.realpath(path) != path:
        raise ImageProjectError('overlay directory path must be canonical')
    first_records, first_identities = _overlay_tree_once(path)
    second_records, second_identities = _overlay_tree_once(path)
    if (first_records != second_records or
            first_identities != second_identities):
        raise ImageProjectError('overlay tree changed during inventory')
    for record in first_records:
        _squashfs_mtime_seconds(record['mtime_ns'])
    return {
        'input_tree_fingerprint': overlay_fingerprint(first_records),
        'entry_count': len(first_records),
        'regular_bytes': sum(item.get('size', 0) for item in first_records),
        'records': first_records,
        'identities': first_identities,
    }


def inspect_overlay_directory(path):
    """Return a path-free summary of a stable, adapter-safe overlay tree."""
    inventory = _overlay_inventory(path)
    return {
        'input_tree_fingerprint': inventory['input_tree_fingerprint'],
        'entry_count': inventory['entry_count'],
        'regular_bytes': inventory['regular_bytes'],
    }


def create_project_overlay_directory(parent_directory, name='image-overlay'):
    """Atomically create one new private overlay directory under a parent.

    The parent must be an existing, canonical, real (non-symlink) directory
    owned by the current effective user where that is observable.  A brand new
    child directory is created with ``openat``/``mkdirat`` semantics (mode
    ``0700``, current-user owned, never a symlink).  When the preferred name is
    taken, a deterministic numeric suffix is chosen without following symlinks.
    An existing directory is never reused or returned, and nothing is ever
    deleted.  The absolute path of the freshly created directory is returned.
    """
    if not isinstance(name, str) or not _OVERLAY_DIRECTORY_NAME_RE.match(name):
        raise ValueError('overlay directory name must be a safe basename')
    parent = _path_string(parent_directory, 'parent_directory')
    if '\n' in parent or '\r' in parent:
        raise ValueError('parent_directory contains a line break')
    if not os.path.isabs(parent):
        raise ValueError('parent_directory must be an absolute path')
    parent = os.path.abspath(parent)
    if os.path.realpath(parent) != parent:
        raise ImageProjectError('parent_directory path must be canonical')
    parent_descriptor = _open_absolute_directory_nofollow(parent)
    try:
        parent_stat = os.fstat(parent_descriptor)
        if (stat.S_ISLNK(parent_stat.st_mode) or
                not stat.S_ISDIR(parent_stat.st_mode)):
            raise ImageProjectError(
                'parent_directory must be a real directory')
        if (hasattr(os, 'geteuid') and
                parent_stat.st_uid != os.geteuid()):
            raise ImageProjectError(
                'parent_directory must be owned by the current user')
        created_name = None
        for index in range(0, 4096):
            candidate = name if index == 0 else '{}-{}'.format(name, index)
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                continue
            except OSError as error:
                raise ImageProjectError(
                    'cannot create overlay directory: {}'.format(error))
            created_name = candidate
            break
        if created_name is None:
            raise ImageProjectError(
                'no available overlay directory name remains')
        child_descriptor = os.open(
            created_name, _directory_open_flags(), dir_fd=parent_descriptor)
        try:
            os.fchmod(child_descriptor, 0o700)
            child_stat = os.fstat(child_descriptor)
            entry_stat = os.stat(
                created_name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (stat.S_ISLNK(entry_stat.st_mode) or
                    not stat.S_ISDIR(entry_stat.st_mode) or
                    not stat.S_ISDIR(child_stat.st_mode) or
                    _identity(child_stat) != _identity(entry_stat) or
                    stat.S_IMODE(child_stat.st_mode) != 0o700):
                raise ImageProjectError(
                    'created overlay directory is not private')
            if (hasattr(os, 'geteuid') and
                    child_stat.st_uid != os.geteuid()):
                raise ImageProjectError(
                    'created overlay directory has an unexpected owner')
            os.fsync(child_descriptor)
        finally:
            os.close(child_descriptor)
        try:
            os.fsync(parent_descriptor)
        except OSError:
            pass
    except ImageProjectError:
        raise
    except OSError as error:
        raise ImageProjectError(
            'cannot create overlay directory: {}'.format(error))
    finally:
        os.close(parent_descriptor)
    child_path = os.path.join(parent, created_name)
    final_stat = os.lstat(child_path)
    if (stat.S_ISLNK(final_stat.st_mode) or
            not stat.S_ISDIR(final_stat.st_mode) or
            stat.S_IMODE(final_stat.st_mode) != 0o700 or
            _identity(final_stat) != _identity(child_stat) or
            (hasattr(os, 'geteuid') and final_stat.st_uid != os.geteuid())):
        raise ImageProjectError('created overlay directory identity changed')
    return child_path


def _squashfs_mtime_seconds(mtime_ns):
    if isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int):
        raise ImageProjectError('overlay mtime is invalid')
    seconds = mtime_ns // 1000000000
    if seconds < 0 or seconds > 0xffffffff:
        raise ImageProjectError(
            'overlay mtime is outside the SquashFS timestamp range')
    return seconds


def _overlay_content_records(records):
    result = []
    for record in records:
        item = {
            'path': record['path'], 'kind': record['kind'],
            'mode': record['mode'],
            'mtime_seconds': _squashfs_mtime_seconds(record['mtime_ns']),
        }
        if record['kind'] == 'regular':
            item.update(size=record['size'], digest=record['digest'])
        elif record['kind'] == 'symlink':
            item.update(target=record['target'], digest=record['digest'])
        result.append(item)
    return tuple(sorted(result, key=lambda item: item['path']))


class Diagnostic(_Immutable):
    """Actionable diagnostic with a stable code."""

    __slots__ = ('severity', 'code', 'message', 'path')

    def __init__(self, severity, code, message, path=None):
        if severity not in ('error', 'warning', 'info'):
            raise ValueError('invalid diagnostic severity')
        self.severity = _optional_string(severity, 'severity')
        self.code = _optional_string(code, 'code')
        self.message = _optional_string(message, 'message')
        self.path = (_optional_string(path, 'path')
                     if path is not None else None)
        self._lock()

    def to_dict(self):
        result = {
            'severity': self.severity,
            'code': self.code,
            'message': self.message,
        }
        if self.path is not None:
            result['path'] = self.path
        return result


def _add_diagnostic(collection, severity, code, message, path=None):
    collection.append(Diagnostic(severity, code, message, path))


def _public_diagnostic(diagnostic):
    value = diagnostic.to_dict()
    if (diagnostic.path is not None and
            os.path.isabs(diagnostic.path)):
        value['message'] = value['message'].replace(
            diagnostic.path, '<redacted-host-path>')
        value['path'] = '<redacted-host-path>'
    return value


def _normalized_session_path(value, field='session path'):
    if not isinstance(value, str):
        raise ValueError('{} must be a string'.format(field))
    try:
        value.encode('utf-8', 'strict')
    except UnicodeError:
        raise ValueError('{} must contain valid UTF-8 text'.format(field))
    if (not value or value.startswith('/') or '\x00' in value or
            '\n' in value or '\r' in value):
        raise ValueError('{} must be a normalized relative path'.format(field))
    components = value.split('/')
    if (any(component in ('', '.', '..') for component in components) or
            posixpath.normpath(value) != value):
        raise ValueError('{} must be a normalized relative path'.format(field))
    return value


def _is_sha256(value):
    return (isinstance(value, str) and len(value) == 64 and
            all(character in '0123456789abcdef' for character in value))


def _is_strict_int(value, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    if value < minimum:
        return False
    return maximum is None or value <= maximum


def customization_target_set_identity(targets):
    """Return minios-image-compose's count and digest for an immutable ISO target set."""
    targets = tuple(targets)
    if any(not isinstance(target, str) or not target for target in targets):
        raise ValueError('customization target set contains an invalid path')
    if len(set(targets)) != len(targets):
        raise ValueError('customization target set contains duplicate paths')
    digest = hashlib.sha256()
    digest.update(b'minios-image-target-set-v1\x00')
    for target in sorted(targets):
        try:
            encoded = target.encode('utf-8', 'strict')
        except UnicodeError:
            raise ValueError('customization target is not valid UTF-8')
        digest.update(encoded)
        digest.update(b'\x00')
    return len(targets), digest.hexdigest()


def validate_boot_menu_entries(entries):
    """Validate an optional boot-menu composition built from MiniOS templates."""
    if entries is None:
        return None
    if (not isinstance(entries, (list, tuple)) or not entries or
            len(entries) > BOOT_MENU_MAX_ENTRIES):
        raise ValueError('boot_menu_entries must contain 1 to 32 entries')
    normalized = []
    seen_ids = set()
    default_count = 0
    for item in entries:
        old_keys = {
            'id', 'base_mode', 'enabled', 'default', 'title', 'kernel_args'}
        new_keys = old_keys | {'kernel_args_schema'}
        if (not isinstance(item, (dict, MappingProxyType)) or
                set(item) not in (old_keys, new_keys)):
            raise ValueError('boot menu entry schema is invalid')
        arguments_schema = item.get('kernel_args_schema')
        if arguments_schema is not None and arguments_schema not in (2, 3):
            raise ValueError('boot menu entry kernel_args_schema is invalid')
        entry_id = item.get('id')
        if (not isinstance(entry_id, str) or
                not _BOOT_MENU_ENTRY_ID_RE.match(entry_id) or
                entry_id in seen_ids):
            raise ValueError('boot menu entry id is invalid or duplicated')
        seen_ids.add(entry_id)
        base_mode = item.get('base_mode')
        if base_mode not in DEFAULT_BOOT_MODES:
            raise ValueError('boot menu entry base mode is invalid')
        enabled = _require_bool(item.get('enabled'), 'boot menu entry enabled')
        is_default = _require_bool(item.get('default'), 'boot menu entry default')
        if is_default:
            default_count += 1
            if not enabled:
                raise ValueError('default boot menu entry must be enabled')
        title = item.get('title')
        if title is not None:
            if not isinstance(title, str):
                raise ValueError('boot menu entry title must be a string or null')
            title = title.strip()
            if not title:
                title = None
            else:
                try:
                    encoded = title.encode('utf-8', 'strict')
                except UnicodeError:
                    raise ValueError('boot menu entry title must be valid UTF-8')
                if len(encoded) > BOOT_MENU_TITLE_MAX_BYTES:
                    raise ValueError('boot menu entry title is too long')
                if (any(not character.isprintable() for character in title) or
                        any(character in '\"\\$`' for character in title)):
                    raise ValueError(
                        'boot menu entry title contains unsafe bootloader syntax')
        extra = item.get('kernel_args')
        if not isinstance(extra, str):
            raise ValueError('boot menu entry kernel_args must be a string')
        extra = extra.strip()
        if extra:
            validate_kernel_arguments(extra)
        normalized_item = {
            'id': entry_id, 'base_mode': base_mode, 'enabled': enabled,
            'default': is_default, 'title': title, 'kernel_args': extra,
        }
        if arguments_schema is not None:
            normalized_item['kernel_args_schema'] = arguments_schema
        normalized.append(normalized_item)
    if not any(item['enabled'] for item in normalized):
        raise ValueError('boot menu must keep at least one enabled entry')
    if default_count != 1:
        raise ValueError('boot menu must have exactly one default entry')
    serialized = json.dumps(
        normalized, ensure_ascii=True, allow_nan=False, sort_keys=True,
        separators=(',', ':')).encode('ascii')
    if len(serialized) > BOOT_MENU_MAX_JSON_BYTES:
        raise ValueError('boot menu customization exceeds 65536 JSON bytes')
    return tuple(_freeze(item) for item in normalized)


def _boot_menu_public(entries):
    if entries is None:
        return None
    keys = ('id', 'base_mode', 'enabled', 'default', 'title', 'kernel_args')
    result = []
    for item in entries:
        public = dict((key, item[key]) for key in keys)
        if 'kernel_args_schema' in item:
            public['kernel_args_schema'] = item['kernel_args_schema']
        result.append(public)
    return result

def _boot_menu_plan_summary(entries):
    if entries is None:
        return None
    result = []
    for item in entries:
        extra = item['kernel_args']
        if extra:
            count, digest = validate_kernel_arguments(extra)
            extra_summary = {'bytes': count, 'sha256': digest}
        else:
            extra_summary = None
        result.append({
            'id': item['id'], 'base_mode': item['base_mode'],
            'enabled': item['enabled'], 'default': item['default'],
            'title': item['title'], 'kernel_args': extra_summary,
        })
    return result


def validate_kernel_arguments(value):
    """Validate minios-image-compose-safe kernel text and return its byte count/digest."""
    if not isinstance(value, str):
        raise ValueError('kernel_args must be a string')
    try:
        encoded = value.encode('utf-8', 'strict')
    except UnicodeError:
        raise ValueError('kernel_args must contain valid UTF-8 text')
    if not encoded or len(encoded) > MAX_KERNEL_ARGUMENT_BYTES:
        raise ValueError('kernel_args must contain 1 to 4096 UTF-8 bytes')
    for character in value:
        if (not character.isprintable() or
                (character.isspace() and character != ' ') or
                character in _KERNEL_ARGUMENT_FORBIDDEN):
            raise ValueError('kernel_args contains bootloader-unsafe syntax')
    if value[0] == ' ' or value[-1] == ' ':
        raise ValueError('kernel_args must not begin or end with a space')
    return len(encoded), hashlib.sha256(encoded).hexdigest()


def _validate_live_config_value(key, value):
    try:
        encoded = value.encode('utf-8', 'strict')
    except UnicodeError:
        raise ValueError('live config override must contain valid UTF-8')
    if len(encoded) > 4096:
        raise ValueError('live config override exceeds 4096 UTF-8 bytes')
    if any(not character.isprintable() for character in value):
        raise ValueError('live config override contains a control character')
    if key in _LIVE_CONFIG_BOOLEAN_KEYS and value not in ('', 'true', 'false'):
        raise ValueError('live config boolean override is invalid')
    allowed = _LIVE_CONFIG_ENUMS.get(key)
    if allowed is not None and value not in allowed:
        raise ValueError('live config enum override is invalid')
    if (key == 'LIVE_HOSTNAME' and value and
            not re.match(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$', value)):
        raise ValueError('live config hostname override is invalid')
    if (key == 'LIVE_USERNAME' and value and
            not re.match(r'^[a-z_][a-z0-9_-]{0,31}$', value)):
        raise ValueError('live config username override is invalid')
    if key == 'LIVE_USER_DIRS_PATH' and value:
        relative = value.lstrip('/')
        components = relative.split('/')
        if (not relative or len(relative) > 240 or
                not re.match(r'^[A-Za-z0-9._ /-]+$', relative) or
                any(component in ('', '.', '..') for component in components)):
            raise ValueError('live config user-directory override is invalid')
    if key in ('ENABLE_SERVICES', 'DISABLE_SERVICES') and value:
        services = [item.strip() for item in value.split(',')]
        if (any(not item for item in services) or
                any(not re.match(r'^[A-Za-z0-9_.@+-]+$', item)
                    for item in services)):
            raise ValueError('live config service override is invalid')
    return encoded


def validate_live_config_overrides(overrides):
    """Validate the non-secret live-config allowlist without evaluating shell."""
    if overrides is None:
        return {}
    if not isinstance(overrides, (dict, MappingProxyType)):
        raise ValueError('live_config_overrides must be an object')
    allowed = frozenset(LIVE_CONFIG_OVERRIDE_KEYS)
    result = {}
    total_bytes = 0
    for key in sorted(overrides):
        value = overrides[key]
        if not isinstance(key, str) or key not in allowed:
            raise ValueError('live_config_overrides contains an unsupported key')
        if not isinstance(value, str):
            raise ValueError('live config override values must be strings')
        encoded = _validate_live_config_value(key, value)
        total_bytes += len(key.encode('ascii')) + len(encoded)
        result[key] = value
    if total_bytes > 64 * 1024:
        raise ValueError('live_config_overrides exceeds the supported size')
    if (result.get('LIVE_LINK_USER_DIRS') == 'true' and
            result.get('LIVE_BIND_USER_DIRS') == 'true'):
        raise ValueError('linked and bind-mounted user directories conflict')
    if (result.get('LIVE_CONFIG_NOROOT') == 'true' and
            result.get('LIVE_SSH_PERMIT_ROOT_LOGIN') == 'true'):
        raise ValueError('root SSH login conflicts with no-root mode')
    enabled = set(item.strip() for item in result.get(
        'ENABLE_SERVICES', '').split(',') if item.strip())
    disabled = set(item.strip() for item in result.get(
        'DISABLE_SERVICES', '').split(',') if item.strip())
    if enabled & disabled:
        raise ValueError('a live config service cannot be enabled and disabled')
    return result


def _shell_assignment(key, value):
    return "{}='{}'".format(key, value.replace("'", "'\\''"))


def _shell_code_without_comment(line):
    quote = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if character == "'":
                quote = None
            continue
        if character == '\\':
            escaped = True
            continue
        if quote == '"':
            if character == '"':
                quote = None
            continue
        if character in ("'", '"'):
            quote = character
        elif (character == '#' and
              (index == 0 or line[index - 1].isspace() or
               line[index - 1] in ';|&()')):
            return line[:index]
    return line


def _plain_shell_assignment_key(code):
    match = re.match(
        r'^[ \t]*(?:export[ \t]+)?([A-Z][A-Z0-9_]*)=', code)
    if not match:
        return None
    value = code[match.end():]
    quote = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if character == "'":
                quote = None
            continue
        if character == '\\':
            escaped = True
            continue
        if quote == '"':
            if character == '"':
                quote = None
            elif character in ('$', '`'):
                return None
            continue
        if character in ("'", '"'):
            quote = character
        elif character in '$`;|&<>()':
            return None
        elif character.isspace() and value[index:].strip():
            return None
    if quote is not None or escaped:
        return None
    return match.group(1)


def _validate_live_config_source(text, override_keys):
    key_patterns = dict(
        (key, re.compile(r'(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])'.format(
            re.escape(key)))) for key in override_keys)
    continued = False
    heredoc_delimiter = None
    for line in text.splitlines():
        if heredoc_delimiter is not None:
            if line.strip() == heredoc_delimiter:
                heredoc_delimiter = None
                continue
            if any(pattern.search(line) for pattern in key_patterns.values()):
                raise ValueError(
                    'live config override key occurs inside a here-document')
            continue
        code = _shell_code_without_comment(line)
        plain_key = _plain_shell_assignment_key(code)
        for key, pattern in key_patterns.items():
            occurrences = tuple(pattern.finditer(code))
            if not occurrences:
                continue
            if continued or plain_key != key or len(occurrences) != 1:
                raise ValueError(
                    'live config override key uses ambiguous shell syntax: '
                    '{}'.format(key))
        heredoc = re.search(
            r'<<-?[ \t]*([\'\"]?)([A-Za-z0-9_]+)\1', code)
        if heredoc:
            heredoc_delimiter = heredoc.group(2)
        stripped = code.rstrip()
        slash_count = len(stripped) - len(stripped.rstrip('\\'))
        continued = bool(slash_count % 2)
    if heredoc_delimiter is not None or continued:
        raise ValueError(
            'live config source ends in an incomplete shell construct')


def render_live_config(source_payload, overrides):
    """Append deterministic shell-quoted overrides to a safe UTF-8 config."""
    normalized = validate_live_config_overrides(overrides)
    if isinstance(source_payload, bytes):
        if len(source_payload) > MAX_LIVE_CONFIG_BYTES:
            raise ValueError('live config source is unexpectedly large')
        try:
            text = source_payload.decode('utf-8', 'strict')
        except UnicodeError:
            raise ValueError('live config source is not valid UTF-8')
    elif isinstance(source_payload, str):
        text = source_payload
        if len(text.encode('utf-8', 'strict')) > MAX_LIVE_CONFIG_BYTES:
            raise ValueError('live config source is unexpectedly large')
    else:
        raise ValueError('live config source must be text or bytes')
    if '\x00' in text:
        raise ValueError('live config source contains a NUL byte')
    source_bytes = text.encode('utf-8', 'strict')
    if not normalized:
        return source_bytes
    _validate_live_config_source(text, tuple(sorted(normalized)))
    block = [
        '# MiniOS Image Builder overrides',
    ]
    block.extend(_shell_assignment(key, normalized[key])
                 for key in sorted(normalized))
    block.append('# End MiniOS Image Builder overrides')
    separator = b'' if not source_bytes or source_bytes.endswith(b'\n') else b'\n'
    payload = source_bytes + separator + ('\n'.join(block) + '\n').encode(
        'utf-8', 'strict')
    if len(payload) > MAX_LIVE_CONFIG_BYTES:
        raise ValueError('rendered live config is unexpectedly large')
    return payload


render_live_config_overrides = render_live_config


def _strict_json_object(raw, context):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON field: {}'.format(key))
            result[key] = value
        return result

    if isinstance(raw, bytes):
        try:
            raw = raw.decode('utf-8', 'strict')
        except UnicodeError as error:
            raise ProjectFormatError('{} is not UTF-8: {}'.format(
                context, error))
    if isinstance(raw, str):
        try:
            value = json.loads(
                raw, object_pairs_hook=unique_object,
                parse_constant=lambda item: (_ for _ in ()).throw(
                    ValueError('invalid JSON constant: {}'.format(item))))
        except ValueError as error:
            raise ProjectFormatError('Invalid {} JSON: {}'.format(
                context, error))
    else:
        value = raw
    if not isinstance(value, dict):
        raise ProjectFormatError('{} must be a JSON object'.format(context))
    return value


class SessionEntry(_Immutable):
    """One metadata-only entry from a savechanges inventory."""

    __slots__ = (
        'path', 'type', 'category', 'sensitive', 'default_exact',
        'default_clean', 'size',
    )

    TYPES = frozenset(('regular', 'directory', 'symlink', 'whiteout',
                       'unsupported'))
    CATEGORIES = frozenset((
        'runtime', 'user-data', 'logs-cache', 'machine-identity',
        'network-identity', 'software', 'system-config', 'other',
    ))

    def __init__(self, path, entry_type, category, sensitive, default_exact,
                 default_clean, size=None):
        self.path = _normalized_session_path(path, 'inventory entry path')
        if entry_type not in self.TYPES:
            raise ValueError('invalid inventory entry type')
        self.type = entry_type
        if category not in self.CATEGORIES:
            raise ValueError('invalid inventory entry category')
        self.category = category
        self.sensitive = _require_bool(sensitive, 'sensitive')
        self.default_exact = _require_bool(default_exact, 'default_exact')
        self.default_clean = _require_bool(default_clean, 'default_clean')
        if self.default_clean and not self.default_exact:
            raise ValueError('clean inventory default requires exact default')
        if entry_type == 'unsupported' and self.default_exact:
            raise ValueError('unsupported inventory entries cannot be selected')
        if size is not None:
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError('inventory entry size must be nonnegative')
            if entry_type != 'regular':
                raise ValueError('only regular inventory entries have sizes')
        self.size = size
        self._lock()

    def to_dict(self):
        result = {
            'path': self.path,
            'type': self.type,
            'category': self.category,
            'sensitive': self.sensitive,
            'default_exact': self.default_exact,
            'default_clean': self.default_clean,
        }
        if self.size is not None:
            result['size'] = self.size
        return result


class SessionInventory(_Immutable):
    """Strict immutable savechanges inventory without file contents."""

    __slots__ = (
        'source_fingerprint', 'union_backend', 'entries',
        'document_sha256',
    )

    def __init__(self, source_fingerprint, union_backend, entries):
        if not _is_sha256(source_fingerprint):
            raise ValueError('invalid session inventory source fingerprint')
        self.source_fingerprint = source_fingerprint
        if union_backend not in ('overlayfs', 'aufs', 'unknown'):
            raise ValueError('invalid session inventory union backend')
        self.union_backend = union_backend
        entries = tuple(entries)
        if not all(isinstance(item, SessionEntry) for item in entries):
            raise ValueError('inventory entries must be SessionEntry objects')
        paths = [item.path for item in entries]
        if len(paths) != len(set(paths)):
            raise ValueError('session inventory contains duplicate paths')
        self.entries = entries
        document = {
            'product_kind': SESSION_INVENTORY_KIND,
            'schema_version': SESSION_INVENTORY_SCHEMA_VERSION,
            'source_fingerprint': source_fingerprint,
            'union_backend': union_backend,
            'entries': [item.to_dict() for item in entries],
        }
        self.document_sha256 = _json_digest(document)
        self._lock()

    def to_dict(self):
        return {
            'product_kind': SESSION_INVENTORY_KIND,
            'schema_version': SESSION_INVENTORY_SCHEMA_VERSION,
            'source_fingerprint': self.source_fingerprint,
            'union_backend': self.union_backend,
            'entries': [item.to_dict() for item in self.entries],
        }


def parse_session_inventory(payload):
    value = _strict_json_object(payload, 'session inventory')
    expected = set((
        'product_kind', 'schema_version', 'source_fingerprint',
        'union_backend', 'entries',
    ))
    _require_keys(value, expected, expected, 'session inventory')
    version = value.get('schema_version')
    if (value.get('product_kind') != SESSION_INVENTORY_KIND or
            isinstance(version, bool) or
            version != SESSION_INVENTORY_SCHEMA_VERSION):
        raise UnsupportedSchemaError(
            'Unsupported session inventory identity or schema')
    raw_entries = value.get('entries')
    if not isinstance(raw_entries, list):
        raise ProjectFormatError('session inventory entries must be an array')
    entries = []
    base_keys = set((
        'path', 'type', 'category', 'sensitive', 'default_exact',
        'default_clean',
    ))
    allowed_keys = base_keys | set(('size',))
    for index, raw_entry in enumerate(raw_entries):
        try:
            _require_keys(
                raw_entry, base_keys, allowed_keys,
                'session inventory entry {}'.format(index))
            entries.append(SessionEntry(
                raw_entry.get('path'), raw_entry.get('type'),
                raw_entry.get('category'), raw_entry.get('sensitive'),
                raw_entry.get('default_exact'),
                raw_entry.get('default_clean'), raw_entry.get('size')))
        except (TypeError, ValueError, ProjectFormatError) as error:
            raise ProjectFormatError(
                'Invalid session inventory entry {}: {}'.format(index, error))
    try:
        return SessionInventory(
            value.get('source_fingerprint'), value.get('union_backend'),
            entries)
    except ValueError as error:
        raise ProjectFormatError('Invalid session inventory: {}'.format(error))


def cleanup_session_inventory(path, expected_identity=None):
    """Unlink only the expected non-symlink regular inventory output."""
    path = os.path.abspath(_path_string(path, 'inventory path'))
    try:
        file_stat = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ImageProjectError('refusing to clean unsafe inventory output')
    if (expected_identity is not None and
            not _matches_expected_identity(file_stat, expected_identity)):
        raise ImageProjectError('inventory output identity changed')
    os.unlink(path)
    _fsync_directory(os.path.dirname(path) or os.curdir)
    return True


def load_session_inventory(path, cleanup=False):
    """Securely load a stable mode-0600 inventory, optionally removing it."""
    path = os.path.abspath(_path_string(path, 'inventory path'))
    file_stat = os.lstat(path)
    if (stat.S_ISLNK(file_stat.st_mode) or
            not stat.S_ISREG(file_stat.st_mode)):
        raise ProjectFormatError('inventory output must be a regular file')
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise ProjectFormatError('inventory output mode must be 0600')
    if file_stat.st_size > MAX_SESSION_INVENTORY_BYTES:
        raise ProjectFormatError('session inventory is too large')
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (_identity(opened) != _identity(file_stat) or
                not stat.S_ISREG(opened.st_mode)):
            raise ProjectFormatError('inventory identity changed while opening')
        chunks = []
        total = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_SESSION_INVENTORY_BYTES:
                raise ProjectFormatError('session inventory is too large')
            chunks.append(block)
        after = os.fstat(descriptor)
        if (_identity(after) != _identity(opened) or
                after.st_size != opened.st_size or
                _stat_mtime_ns(after) != _stat_mtime_ns(opened)):
            raise ProjectFormatError('inventory changed while being read')
        inventory = parse_session_inventory(b''.join(chunks))
    finally:
        os.close(descriptor)
    if cleanup:
        cleanup_session_inventory(path, _identity(file_stat))
    return inventory


class ModuleInfo(_Immutable):
    """Content-addressed metadata for one source or runtime module path."""

    __slots__ = (
        'path', 'real_path', 'relative_path', 'basename', 'size', 'sha256',
        'order_prefix', 'role', 'friendly_name', 'description',
        'source_category', 'required', 'core', 'active', 'architecture',
        'kernel_version', 'is_symlink', 'link_target',
    )

    def __init__(self, path, relative_path, size, sha256, order_prefix, role,
                 friendly_name, description, source_category, required=False,
                 core=False, active=None, architecture=None,
                 kernel_version=None, is_symlink=False, link_target=None):
        self.path = os.path.abspath(_path_string(path))
        self.real_path = os.path.realpath(self.path)
        self.relative_path = (_optional_string(relative_path, 'relative_path')
                              if relative_path is not None else None)
        self.basename = os.path.basename(self.path)
        self.size = size if isinstance(size, int) and size >= 0 else None
        self.sha256 = _optional_string(sha256, 'sha256')
        self.order_prefix = (order_prefix if isinstance(order_prefix, int)
                             and order_prefix >= 0 else None)
        self.role = _optional_string(role, 'role')
        self.friendly_name = _optional_string(friendly_name, 'friendly_name')
        self.description = _optional_string(description, 'description')
        self.source_category = _optional_string(
            source_category, 'source_category')
        self.required = bool(required)
        self.core = bool(core)
        if active not in (True, False, None):
            raise ValueError('active must be true, false, or null')
        self.active = active
        self.architecture = architecture
        self.kernel_version = kernel_version
        self.is_symlink = bool(is_symlink)
        self.link_target = link_target
        self._lock()

    @property
    def target_path(self):
        if self.relative_path is not None:
            return 'minios/' + self.relative_path.replace(os.sep, '/')
        return compose_module_target(self.basename)

    def to_dict(self):
        return {
            'path': self.path,
            'real_path': self.real_path,
            'relative_path': self.relative_path,
            'basename': self.basename,
            'size': self.size,
            'sha256': self.sha256,
            'order_prefix': self.order_prefix,
            'role': self.role,
            'friendly_name': self.friendly_name,
            'description': self.description,
            'source_category': self.source_category,
            'required': self.required,
            'core': self.core,
            'active': self.active,
            'architecture': self.architecture,
            'kernel_version': self.kernel_version,
            'is_symlink': self.is_symlink,
            'link_target': self.link_target,
            'target_path': self.target_path,
        }


class SourceInfo(_Immutable):
    """Content-addressed inspection result for a MiniOS source."""

    __slots__ = (
        'status', 'kind', 'backend', 'root_path', 'source_path',
        'media_category', 'fingerprint', 'fingerprint_algorithm', 'metadata',
        'modules', 'active_external_modules', 'diagnostics', 'collisions',
        'total_bytes', 'non_module_bytes', 'input_manifest',
    )

    def __init__(self, status, backend=None, root_path=None, source_path=None,
                 media_category=None, fingerprint=None,
                 fingerprint_algorithm=SOURCE_FINGERPRINT_ALGORITHM,
                 metadata=None, modules=(), active_external_modules=(),
                 diagnostics=(), collisions=(), total_bytes=0,
                 non_module_bytes=0, input_manifest=()):
        if status not in (SOURCE_SUPPORTED, SOURCE_UNSUPPORTED, SOURCE_ERROR):
            raise ValueError('invalid source status')
        self.status = status
        self.kind = SOURCE_KIND
        self.backend = backend
        self.root_path = root_path
        self.source_path = source_path
        self.media_category = media_category
        self.fingerprint = fingerprint
        self.fingerprint_algorithm = fingerprint_algorithm
        self.metadata = _freeze(dict(metadata or {}))
        self.modules = tuple(modules)
        self.active_external_modules = tuple(active_external_modules)
        self.diagnostics = tuple(diagnostics)
        self.collisions = tuple(collisions)
        self.total_bytes = int(total_bytes or 0)
        self.non_module_bytes = int(non_module_bytes or 0)
        self.input_manifest = tuple(_freeze(dict(item))
                                    for item in input_manifest)
        self._lock()

    @property
    def supported(self):
        return self.status == SOURCE_SUPPORTED

    @property
    def all_modules(self):
        return self.modules + self.active_external_modules

    def module_names(self):
        return tuple(module.basename for module in self.modules)

    def to_dict(self):
        return {
            'status': self.status,
            'kind': self.kind,
            'backend': self.backend,
            'root_path': self.root_path,
            'source_path': self.source_path,
            'media_category': self.media_category,
            'fingerprint': self.fingerprint,
            'fingerprint_algorithm': self.fingerprint_algorithm,
            'metadata': _thaw(self.metadata),
            'modules': [item.to_dict() for item in self.modules],
            'active_external_modules': [
                item.to_dict() for item in self.active_external_modules
            ],
            'diagnostics': [item.to_dict() for item in self.diagnostics],
            'collisions': [item.to_dict() for item in self.collisions],
            'total_bytes': self.total_bytes,
            'non_module_bytes': self.non_module_bytes,
            'input_manifest': [_thaw(item) for item in self.input_manifest],
        }


def parse_module_order(name):
    match = _MODULE_ORDER_RE.match(os.path.basename(name))
    return int(match.group(1)) if match else None


def _module_architecture(name):
    lower = os.path.basename(name).lower()
    stem = lower[:-3] if lower.endswith('.sb') else lower
    for architecture in _ARCHITECTURES:
        if stem == architecture or stem.endswith('-' + architecture):
            return architecture
    return None


def _kernel_module_version(name):
    lower = os.path.basename(name).lower()
    stem = lower[:-3] if lower.endswith('.sb') else lower
    architecture = _module_architecture(name)
    if architecture and stem.endswith('-' + architecture):
        stem = stem[:-(len(architecture) + 1)]
    marker = 'kernel-'
    index = stem.find(marker)
    if index < 0:
        return None
    value = stem[index + len(marker):]
    if not value or not value[0].isdigit():
        return None
    return value


def _normalized_architecture(value):
    if not value:
        return None
    value = value.lower()
    aliases = {'x86_64': 'amd64', 'i686': 'i386', 'aarch64': 'arm64'}
    return aliases.get(value, value)


def describe_module_name(name):
    basename = os.path.basename(name)
    lower = basename.lower()
    order = parse_module_order(basename)
    for keywords, role, friendly_name, description in _MODULE_ROLE_RULES:
        if any(keyword in lower for keyword in keywords):
            break
    else:
        if order == 0:
            role = 'core'
            friendly_name = 'Core system'
            description = 'Base filesystem and essential system utilities.'
        elif order == 1:
            role = 'kernel'
            friendly_name = 'Kernel and drivers'
            description = 'Linux kernel and hardware drivers.'
        else:
            role = 'custom'
            friendly_name = 'Custom module'
            description = 'A MiniOS SquashFS module.'
    core = role == 'core' or order == 0
    required = core or role == 'kernel' or order == 1
    return {
        'order_prefix': order,
        'role': role,
        'friendly_name': friendly_name,
        'description': description,
        'core': core,
        'required': required,
        'architecture': _module_architecture(basename),
        'kernel_version': (_kernel_module_version(basename)
                           if role == 'kernel' else None),
    }


def _source_path_is_ignored(relative_path):
    relative_path = relative_path.replace(os.sep, '/')
    return (relative_path == 'config.conf' or
            relative_path == 'boot/active-kernel' or
            relative_path == 'changes' or
            relative_path.startswith('changes/') or
            relative_path == 'kernels' or
            relative_path.startswith('kernels/'))


def _safe_source_symlink(source_path, path, link_target):
    if os.path.isabs(link_target):
        return False
    resolved = os.path.realpath(os.path.join(os.path.dirname(path), link_target))
    if not _is_within(resolved, source_path) or not os.path.exists(resolved):
        return False
    relative_target = os.path.relpath(
        resolved, source_path).replace(os.sep, '/')
    return not _source_path_is_ignored(relative_target)


def _secure_hash_regular(path, expected_lstat=None):
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourceInspectionError(
                'Input is not a regular file: {}'.format(path))
        if expected_lstat is not None and _identity(before) != _identity(expected_lstat):
            raise SourceInspectionError(
                'Input changed while opening: {}'.format(path))
        digest = _hash_descriptor(descriptor)
        after = os.fstat(descriptor)
        if (_identity(before) != _identity(after) or
                before.st_size != after.st_size or
                _stat_mtime_ns(before) != _stat_mtime_ns(after)):
            raise SourceInspectionError(
                'Input changed while hashing: {}'.format(path))
        return digest, after
    finally:
        os.close(descriptor)


def _build_source_manifest(source_path):
    """Hash every effective regular file and safe symlink in source."""
    source_path = os.path.abspath(_path_string(source_path, 'source_path'))
    root_stat = os.lstat(source_path)
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise SourceInspectionError(
            'MiniOS source must be a real directory: {}'.format(source_path))
    records = []
    total_bytes = 0
    stack = [(source_path, '')]
    while stack:
        directory, relative_directory = stack.pop()
        try:
            names = sorted(os.listdir(directory), reverse=True)
        except OSError as error:
            raise SourceInspectionError(
                'Cannot list source directory {}: {}'.format(directory, error))
        for name in names:
            if '\n' in name or '\r' in name:
                raise SourceInspectionError(
                    'Source names may not contain newlines: {}'.format(name))
            path = os.path.join(directory, name)
            relative = (os.path.join(relative_directory, name)
                        if relative_directory else name)
            normalized = relative.replace(os.sep, '/')
            if _source_path_is_ignored(normalized):
                continue
            try:
                file_stat = os.lstat(path)
            except OSError as error:
                raise SourceInspectionError(
                    'Cannot lstat source entry {}: {}'.format(path, error))
            if stat.S_ISLNK(file_stat.st_mode):
                link_target = os.readlink(path)
                if not _safe_source_symlink(source_path, path, link_target):
                    raise SourceInspectionError(
                        'Unsafe or dangling source symlink: {}'.format(path))
                digest = hashlib.sha256(
                    b'symlink\x00' + link_target.encode(
                        'utf-8', 'surrogateescape')).hexdigest()
                records.append({
                    'relative_path': normalized,
                    'path': path,
                    'type': 'symlink',
                    'size': len(link_target.encode('utf-8', 'surrogateescape')),
                    'sha256': digest,
                    'link_target': link_target,
                    'device': int(file_stat.st_dev),
                    'inode': int(file_stat.st_ino),
                    'mode': stat.S_IMODE(file_stat.st_mode),
                })
            elif stat.S_ISDIR(file_stat.st_mode):
                stack.append((path, relative))
            elif stat.S_ISREG(file_stat.st_mode):
                digest, opened_stat = _secure_hash_regular(path, file_stat)
                records.append({
                    'relative_path': normalized,
                    'path': path,
                    'type': 'file',
                    'size': int(opened_stat.st_size),
                    'sha256': digest,
                    'link_target': None,
                    'device': int(opened_stat.st_dev),
                    'inode': int(opened_stat.st_ino),
                    'mode': stat.S_IMODE(opened_stat.st_mode),
                })
                total_bytes += opened_stat.st_size
            else:
                # minios-image-compose's source traversal includes files/symlinks, not devices.
                continue
    records.sort(key=lambda item: item['relative_path'])
    fingerprint_records = [
        {
            'relative_path': item['relative_path'],
            'type': item['type'],
            'size': item['size'],
            'sha256': item['sha256'],
            'link_target': item['link_target'],
        }
        for item in records
    ]
    fingerprint = '{}:{}'.format(
        SOURCE_FINGERPRINT_ALGORITHM, _json_digest(fingerprint_records))
    return fingerprint, total_bytes, tuple(records)


def source_tree_inventory(source_path):
    fingerprint, total_bytes, unused_records = _build_source_manifest(source_path)
    return fingerprint, total_bytes


def source_tree_fingerprint(source_path):
    return source_tree_inventory(source_path)[0]


def _manifest_record_by_relative(records):
    return {item['relative_path']: item for item in records}


def _followed_module_digest(path):
    flags = os.O_RDONLY
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SourceInspectionError(
                'Module target is not a regular file: {}'.format(path))
        return _hash_descriptor(descriptor), file_stat
    finally:
        os.close(descriptor)


def _module_from_record(record, source_category, active=None):
    details = describe_module_name(record['relative_path'])
    digest, target_stat = _followed_module_digest(record['path'])
    return ModuleInfo(
        path=record['path'], relative_path=record['relative_path'],
        size=target_stat.st_size, sha256=digest,
        order_prefix=details['order_prefix'], role=details['role'],
        friendly_name=details['friendly_name'],
        description=details['description'], source_category=source_category,
        required=details['required'], core=details['core'], active=active,
        architecture=details['architecture'],
        kernel_version=details['kernel_version'],
        is_symlink=record['type'] == 'symlink',
        link_target=record['link_target'])


def inspect_source_modules(source_path, input_manifest=None):
    """Recursively inspect top-level and ``modules/**/*.sb`` entries."""
    source_path = os.path.abspath(_path_string(source_path, 'source_path'))
    if input_manifest is None:
        unused_fingerprint, unused_total, input_manifest = _build_source_manifest(
            source_path)
    modules = []
    diagnostics = []
    for frozen_record in input_manifest:
        record = _thaw(frozen_record) if isinstance(
            frozen_record, MappingProxyType) else dict(frozen_record)
        relative = record['relative_path']
        parts = relative.split('/')
        top_level = len(parts) == 1 and relative.endswith('.sb')
        nested = (len(parts) >= 2 and parts[0] == 'modules' and
                  relative.endswith('.sb'))
        if not top_level and not nested:
            continue
        category = 'source-top-level' if top_level else 'source-modules'
        modules.append(_module_from_record(record, category, active=None))
    modules.sort(key=lambda item: (
        item.order_prefix is None,
        item.order_prefix if item.order_prefix is not None else 0,
        item.basename.lower(), item.relative_path or '',
    ))
    by_real = {}
    by_basename = {}
    for module in modules:
        by_real.setdefault(module.real_path, []).append(module)
        by_basename.setdefault(module.basename, []).append(module)
    for entries in by_real.values():
        if len(entries) > 1:
            diagnostics.append(Diagnostic(
                'warning', 'module_real_path_alias',
                'Multiple source module paths resolve to the same content: '
                '{}.'.format(', '.join(item.relative_path for item in entries))))
    for basename, entries in sorted(by_basename.items()):
        if len(entries) > 1:
            diagnostics.append(Diagnostic(
                'error', 'source_module_basename_collision',
                'Different source paths share module basename {}: {}.'.format(
                    basename, ', '.join(item.relative_path for item in entries)),
                basename))
    return tuple(modules), tuple(diagnostics)


def _decode_mount_field(value):
    for encoded, decoded in (
            ('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'),
            ('\\134', '\\')):
        value = value.replace(encoded, decoded)
    return value


def _loop_backing_path(device, options, sys_block_root):
    for option in options.split(','):
        if option.startswith('loop=') and len(option) > 5:
            return _decode_mount_field(option[5:])
    if not device.startswith('/dev/loop'):
        return None
    path = os.path.join(
        sys_block_root, os.path.basename(device), 'loop', 'backing_file')
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            value = handle.readline().strip()
    except OSError:
        return None
    value = _decode_mount_field(value)
    if value.endswith(' (deleted)'):
        value = value[:-10]
    if value and not os.path.isabs(value):
        value = os.sep + value
    return value or None


def _active_module_backing_paths(mounts_path, sys_block_root):
    diagnostics = []
    paths = []
    try:
        with open(mounts_path, 'r', encoding='utf-8', errors='replace') as handle:
            lines = handle.readlines()
    except OSError as error:
        diagnostics.append(Diagnostic(
            'warning', 'mount_table_unavailable',
            'Cannot inspect active modules: {}'.format(error), mounts_path))
        return tuple(), tuple(diagnostics)
    for line in lines:
        columns = line.split()
        if len(columns) < 4:
            continue
        device = _decode_mount_field(columns[0])
        mountpoint = os.path.normpath(_decode_mount_field(columns[1]))
        filesystem = columns[2]
        options = columns[3]
        if 'bundles' not in mountpoint.split(os.sep):
            continue
        backing = None
        if device.endswith('.sb') and os.path.isabs(device):
            backing = device
        elif device.startswith('/dev/loop'):
            backing = _loop_backing_path(device, options, sys_block_root)
        if (not backing or not backing.lower().endswith('.sb') or
                filesystem != 'squashfs'):
            continue
        paths.append(os.path.abspath(backing))
    return tuple(paths), tuple(diagnostics)


def _copy_module_active(module, active):
    return ModuleInfo(
        path=module.path, relative_path=module.relative_path,
        size=module.size, sha256=module.sha256,
        order_prefix=module.order_prefix, role=module.role,
        friendly_name=module.friendly_name,
        description=module.description,
        source_category=module.source_category, required=module.required,
        core=module.core, active=active, architecture=module.architecture,
        kernel_version=module.kernel_version,
        is_symlink=module.is_symlink, link_target=module.link_target)


def _map_active_modules(source_modules, mounts_path, sys_block_root):
    backing_paths, mount_diagnostics = _active_module_backing_paths(
        mounts_path, sys_block_root)
    diagnostics = list(mount_diagnostics)
    backing_by_real = {}
    for path in backing_paths:
        backing_by_real.setdefault(os.path.realpath(path), path)
    source_real = set(item.real_path for item in source_modules)
    mapped_source = tuple(
        _copy_module_active(item, True)
        if item.real_path in backing_by_real else item
        for item in source_modules)
    external = []
    for real_path, path in sorted(backing_by_real.items()):
        if real_path in source_real:
            continue
        try:
            file_stat = os.lstat(path)
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            digest, opened_stat = _secure_hash_regular(path, file_stat)
        except (OSError, SourceInspectionError):
            continue
        details = describe_module_name(path)
        external.append(ModuleInfo(
            path=path, relative_path=None, size=opened_stat.st_size,
            sha256=digest, order_prefix=details['order_prefix'],
            role=details['role'], friendly_name=details['friendly_name'],
            description=details['description'],
            source_category='runtime-external', required=False, core=False,
            active=True, architecture=details['architecture'],
            kernel_version=details['kernel_version']))
    source_names = set(item.basename for item in source_modules)
    external_names = {}
    for item in external:
        external_names.setdefault(item.basename, []).append(item.path)
    for basename, paths in sorted(external_names.items()):
        if basename in source_names:
            diagnostics.append(Diagnostic(
                'warning', 'runtime_source_basename_collision',
                'Active external module shares a source basename and is not '
                'included automatically: {}.'.format(basename), basename))
        if len(paths) > 1:
            diagnostics.append(Diagnostic(
                'warning', 'runtime_module_basename_collision',
                'Active external modules share basename {}: {}.'.format(
                    basename, ', '.join(paths)), basename))
    return mapped_source, tuple(external), tuple(diagnostics)


def discover_active_external_modules(source_modules=(),
                                     mounts_path='/proc/mounts',
                                     sys_block_root='/sys/class/block'):
    unused_source, external, diagnostics = _map_active_modules(
        tuple(source_modules), mounts_path, sys_block_root)
    return external, diagnostics


def _kernel_version_from_name(name):
    if name.startswith('vmlinuz-'):
        return name[len('vmlinuz-'):]
    return None


def _initramfs_version_from_name(name):
    if name.startswith('initrfs-') and name.endswith('.img'):
        return name[len('initrfs-'):-len('.img')]
    if name.startswith('initrd.img-'):
        return name[len('initrd.img-'):]
    if name.startswith('initrd-'):
        value = name[len('initrd-'):]
        return value[:-4] if value.endswith('.img') else value
    return None


def _boot_inventory(source_path):
    boot_path = os.path.join(source_path, 'boot')
    try:
        names = sorted(os.listdir(boot_path))
    except OSError as error:
        raise SourceInspectionError(
            'Cannot inspect boot directory {}: {}'.format(boot_path, error))
    kernels = []
    initramfs = []
    for name in names:
        path = os.path.join(boot_path, name)
        try:
            file_stat = os.stat(path)
        except OSError:
            continue
        if not stat.S_ISREG(file_stat.st_mode):
            continue
        if name == 'vmlinuz' or name.startswith('vmlinuz-'):
            kernels.append(path)
        if (name == 'initrfs.img' or name == 'initrd.img' or
                name.startswith('initrfs-') or name.startswith('initrd-') or
                name.startswith('initrd.img-')):
            initramfs.append(path)
    has_syslinux = os.path.isdir(os.path.join(boot_path, 'syslinux'))
    grub_bios = os.path.join(boot_path, 'grub', 'i386-pc')
    has_grub_chain = all(os.path.isfile(os.path.join(grub_bios, name))
                         for name in ('lnxboot.img', 'core.img'))
    has_grub_native = all(os.path.isfile(os.path.join(grub_bios, name))
                          for name in ('eltorito.img', 'boot_hybrid.img'))
    if has_syslinux and has_grub_chain:
        bootloader = 'syslinux-grub'
    elif has_syslinux:
        bootloader = 'syslinux-native'
    elif has_grub_native:
        bootloader = 'grub-only'
    else:
        bootloader = 'unknown'
    kernel_versions = set(filter(None, (
        _kernel_version_from_name(os.path.basename(path)) for path in kernels)))
    initramfs_versions = set(filter(None, (
        _initramfs_version_from_name(os.path.basename(path))
        for path in initramfs)))
    generic_pair = (any(os.path.basename(path) == 'vmlinuz' for path in kernels)
                    and any(os.path.basename(path) in ('initrfs.img', 'initrd.img')
                            for path in initramfs))
    coherent_versions = sorted(kernel_versions & initramfs_versions)
    if kernel_versions or initramfs_versions:
        coherent = bool(kernel_versions) and kernel_versions == initramfs_versions
    else:
        coherent = generic_pair
    return {
        'bootloader': bootloader,
        'kernel_paths': tuple(kernels),
        'initramfs_paths': tuple(initramfs),
        'kernel_versions': tuple(sorted(kernel_versions)),
        'initramfs_versions': tuple(sorted(initramfs_versions)),
        'coherent_versions': tuple(coherent_versions),
        'version_coherent': coherent,
    }


def required_source_boot_files(source_path, bootloader=None):
    if bootloader is None:
        bootloader = _boot_inventory(source_path)['bootloader']
    boot = os.path.join(source_path, 'boot')
    if bootloader == 'grub-only':
        relative = (
            'grub/i386-pc/eltorito.img',
            'grub/i386-pc/boot_hybrid.img',
            'grub/efi.img', 'grub/grub.cfg',
        )
    elif bootloader == 'syslinux-native':
        relative = (
            'syslinux/isolinux.bin', 'syslinux/isohdpfx.bin',
            'syslinux/syslinux.cfg', 'grub/efi.img',
        )
    elif bootloader == 'syslinux-grub':
        relative = (
            'syslinux/isolinux.bin', 'syslinux/isohdpfx.bin',
            'syslinux/syslinux.cfg', 'grub/efi.img',
            'grub/grub.cfg',
        )
    else:
        return tuple()
    return tuple(os.path.join(boot, item) for item in relative)


def _menu_source_paths(source_path, bootloader, menu_locale):
    paths = []
    grub = os.path.join(source_path, 'boot', 'grub')
    if menu_locale == 'multilang':
        candidate = os.path.join(grub, 'grub.multilang.cfg')
        paths.append(candidate if os.path.isfile(candidate)
                     else os.path.join(grub, 'grub.cfg'))
    else:
        paths.append(os.path.join(grub, 'grub.cfg'))
    if bootloader == 'syslinux-native':
        syslinux = os.path.join(source_path, 'boot', 'syslinux')
        if menu_locale == 'multilang':
            candidate = os.path.join(syslinux, 'syslinux.multilang.cfg')
        else:
            candidate = os.path.join(syslinux, 'lang',
                                     '{}.cfg'.format(menu_locale))
        paths.append(candidate if os.path.isfile(candidate)
                     else os.path.join(syslinux, 'syslinux.cfg'))
    return tuple(paths)


def _effective_grub_menu_relative(menu_locale, included_relative):
    if (menu_locale == 'multilang' and
            'boot/grub/grub.multilang.cfg' in included_relative):
        return 'boot/grub/grub.multilang.cfg'
    return 'boot/grub/grub.cfg'


def _effective_syslinux_menu_relative(source_path, menu_locale,
                                      included_relative):
    if menu_locale == 'multilang':
        candidate = 'boot/syslinux/syslinux.multilang.cfg'
        return (candidate if candidate in included_relative
                else 'boot/syslinux/syslinux.cfg')
    candidate = 'boot/syslinux/lang/{}.cfg'.format(menu_locale)
    fallback = 'boot/syslinux/lang/en_US.cfg'
    if candidate in included_relative:
        return candidate
    if fallback in included_relative:
        return fallback
    return 'boot/syslinux/syslinux.cfg'


def _resolve_boot_config_reference(current, reference, kind):
    if any(character in reference for character in '"\'`$;|&<>(){}[]'):
        raise ImageProjectError('boot config reference uses unsafe syntax')
    if kind == 'grub' and reference.startswith('/'):
        resolved = posixpath.normpath(reference[1:])
    elif reference.startswith('/'):
        resolved = posixpath.normpath(
            'minios/boot/syslinux/' + reference.lstrip('/'))
    else:
        resolved = posixpath.normpath(posixpath.join(
            posixpath.dirname(current), reference))
    if (resolved == '..' or resolved.startswith('../') or
            not resolved.startswith('minios/boot/')):
        raise ImageProjectError('boot config reference escapes the boot tree')
    return resolved


def _boot_config_references_payload(payload, kind):
    try:
        text = payload.decode(
            'utf-8', 'strict' if kind == 'grub' else 'surrogateescape')
    except UnicodeError:
        raise ImageProjectError('GRUB configuration is not valid UTF-8')
    references = []
    if kind == 'grub':
        expression = re.compile(r'^\s*(?:configfile|source)\s+(\S+)\s*$')
        prefix = re.compile(r'^\s*(?:configfile|source)(?:\s|$)')
    else:
        expression = re.compile(
            r'^\s*(?:CONFIG|INCLUDE)\s+(\S+)\s*$', re.IGNORECASE)
        prefix = re.compile(
            r'^\s*(?:CONFIG|INCLUDE)(?:\s|$)', re.IGNORECASE)
    for line in text.splitlines():
        match = expression.match(line)
        if match:
            references.append(match.group(1))
        elif prefix.match(line):
            raise ImageProjectError('boot config reference syntax is unsupported')
    return tuple(references)


def _po_translations(path):
    try:
        payload, unused_stat = _read_stable_regular_bytes(path, 1024 * 1024)
        text = payload.decode('utf-8', 'strict')
    except (OSError, UnicodeError, ImageProjectError):
        return None
    translations = {}
    msgid = ''
    msgstr = ''
    state = None

    def decoded(fragment):
        fragment = fragment.strip()
        if len(fragment) >= 2 and fragment[0] == fragment[-1] == '"':
            try:
                return json.loads(fragment)
            except ValueError:
                return fragment[1:-1]
        return ''

    def flush():
        if msgid and msgstr and msgid not in translations:
            translations[msgid] = msgstr

    for line in text.splitlines():
        if line.startswith('msgid '):
            flush()
            msgid = decoded(line[6:])
            msgstr = ''
            state = 'msgid'
        elif line.startswith('msgstr '):
            msgstr = decoded(line[7:])
            state = 'msgstr'
        elif line.startswith('"'):
            if state == 'msgid':
                msgid += decoded(line)
            elif state == 'msgstr':
                msgstr += decoded(line)
        elif not line.strip():
            flush()
            msgid = ''
            msgstr = ''
            state = None
    flush()
    return translations or None


def _localized_grub_payload(source_path, language):
    translations = _po_translations(os.path.join(
        source_path, 'boot', 'grub', 'po', '{}.po'.format(language)))
    template_path = os.path.join(
        source_path, 'boot', 'grub', 'grub.template.cfg')
    if not translations:
        return None
    try:
        payload, unused_stat = _read_stable_regular_bytes(
            template_path, 4 * 1024 * 1024)
        content = payload.decode('utf-8', 'strict').rstrip('\n')
    except (OSError, UnicodeError, ImageProjectError):
        return None
    english_texts = (
        ('Resume previous session', 'resume'),
        ('Start a new session', 'newsession'),
        ('Choose session during startup', 'choosesession'),
        ('Fresh start', 'freshstart'),
        ('Copy to RAM', 'copyram'),
        ('Loading kernel and ramdisk...', 'loading'),
        ('MiniOS', 'OS'),
    )
    for english, variable in english_texts:
        localized = translations.get(english, english)
        if localized != english:
            content = content.replace(
                'menuentry "{}"'.format(english),
                'menuentry "{}"'.format(localized))
            content = content.replace(
                'set {}="{}"'.format(variable, english),
                'set {}="{}"'.format(variable, localized))
    theme = os.path.join(
        source_path, 'boot', 'grub', 'minios-theme',
        'theme_{}.txt'.format(language))
    if os.path.isfile(theme):
        content = content.replace(
            'set theme=/minios/boot/grub/minios-theme/theme.txt',
            'set theme=/minios/boot/grub/minios-theme/theme_{}.txt'.format(
                language))
    extra_by_language = {
        'ru_RU': 'timezone=Europe/Moscow keyboard-layouts=us,ru',
        'de_DE': 'timezone=Europe/Berlin keyboard-layouts=us,de',
        'es_ES': 'timezone=Europe/Madrid keyboard-layouts=us,es',
        'fr_FR': 'timezone=Europe/Paris keyboard-layouts=us,fr',
        'id_ID': 'timezone=Asia/Jakarta keyboard-layouts=id',
        'it_IT': 'timezone=Europe/Rome keyboard-layouts=us,it',
        'pt_BR': 'timezone=America/Sao_Paulo keyboard-layouts=us,br',
        'pt_PT': 'timezone=Europe/Lisbon keyboard-layouts=us,pt',
    }
    locale_parameters = 'locales={}.UTF-8'.format(language)
    extra = extra_by_language.get(language)
    if extra:
        locale_parameters += ' ' + extra
    lines = []
    expression = re.compile(r'(linux .*/vmlinuz[^ ]* .*)')
    for line in content.splitlines():
        lines.append(expression.sub(
            lambda match: match.group(1) + ' ' + locale_parameters, line))
    return ('\n'.join(lines) + '\n').encode('utf-8', 'strict')


def _effective_boot_config_mapping(source_path, bootloader, menu_locale,
                                   included_relative):
    included_relative = frozenset(included_relative)
    mapping = {}
    for relative in included_relative:
        if not relative.endswith('.cfg'):
            continue
        if relative.startswith('boot/grub/'):
            kind = 'grub'
        elif relative.startswith('boot/syslinux/'):
            kind = 'syslinux'
        else:
            continue
        payload, unused_stat = _read_stable_regular_bytes(
            os.path.join(source_path, relative), 4 * 1024 * 1024)
        mapping['minios/' + relative] = (payload, kind)
    grub_root = 'minios/boot/grub/grub.cfg'
    if menu_locale == 'multilang':
        grub_relative = _effective_grub_menu_relative(
            menu_locale, included_relative)
        if grub_relative not in included_relative:
            raise ImageProjectError('effective GRUB menu source is unavailable')
        grub_payload, unused_stat = _read_stable_regular_bytes(
            os.path.join(source_path, grub_relative), 4 * 1024 * 1024)
    else:
        grub_payload = _localized_grub_payload(source_path, menu_locale)
        if grub_payload is None:
            grub_payload = _localized_grub_payload(source_path, 'en_US')
        if grub_payload is None:
            grub_relative = 'boot/grub/grub.cfg'
            if grub_relative not in included_relative:
                raise ImageProjectError(
                    'effective GRUB menu source is unavailable')
            grub_payload, unused_stat = _read_stable_regular_bytes(
                os.path.join(source_path, grub_relative), 4 * 1024 * 1024)
    mapping[grub_root] = (grub_payload, 'grub')
    roots = [grub_root]
    if bootloader == 'syslinux-native':
        syslinux_root = 'minios/boot/syslinux/syslinux.cfg'
        syslinux_relative = _effective_syslinux_menu_relative(
            source_path, menu_locale, included_relative)
        if syslinux_relative not in included_relative:
            raise ImageProjectError(
                'effective SYSLINUX menu source is unavailable')
        syslinux_payload, unused_stat = _read_stable_regular_bytes(
            os.path.join(source_path, syslinux_relative), 4 * 1024 * 1024)
        mapping[syslinux_root] = (syslinux_payload, 'syslinux')
        roots.append(syslinux_root)
    return mapping, tuple(roots)


def _boot_line_body(line):
    if line.endswith('\r\n'):
        return line[:-2], '\r\n'
    if line.endswith('\n'):
        return line[:-1], '\n'
    return line, ''


def _boot_replace_or_prepend(lines, expression, replacement):
    found = False
    output = []
    for line in lines:
        body, ending = _boot_line_body(line)
        if expression.match(body):
            indent = body[:len(body) - len(body.lstrip())]
            output.append(indent + replacement + ending)
            found = True
        else:
            output.append(line)
    if not found:
        output.insert(0, replacement + '\n')
    return output


def _boot_semantic(arguments):
    tokens = arguments.split()
    modes = []
    if 'perchdir=resume' in tokens:
        modes.append('resume')
    if 'perchdir=new' in tokens:
        modes.append('new')
    if 'perchdir=ask' in tokens:
        modes.append('choose')
    if 'toram' in tokens:
        modes.append('toram')
    if len(modes) > 1:
        raise ImageProjectError(
            'boot entry has conflicting MiniOS session arguments')
    if modes:
        return modes[0]
    return 'fresh' if 'boot=live' in tokens else None


_GRUB_CLASS_MODES = {
    'resume': 'resume', 'new': 'new', 'switch': 'choose',
    'live': 'fresh', 'ram': 'toram',
}
_SYSLINUX_LABEL_MODES = {
    'default': 'resume', 'perch': 'new', 'asksession': 'choose',
    'live': 'fresh', 'toram': 'toram',
}


def _grub_menu_entries(lines):
    entries = []
    index = 0
    while index < len(lines):
        body, unused_ending = _boot_line_body(lines[index])
        if not re.match(r'^\s*menuentry(?:\s|$)', body):
            index += 1
            continue
        balance = body.count('{') - body.count('}')
        if balance <= 0:
            raise ImageProjectError('unsupported GRUB menuentry syntax')
        end = index
        while balance > 0:
            end += 1
            if end >= len(lines):
                raise ImageProjectError('unterminated GRUB menuentry')
            block_body, unused_block_ending = _boot_line_body(lines[end])
            balance += block_body.count('{') - block_body.count('}')
        entries.append((index, end, body))
        index = end + 1
    return entries


def _boot_menu_sequence(entries):
    normalized = validate_boot_menu_entries(entries)
    if normalized is None:
        return None
    return [dict(item) for item in normalized]


_GRUB_MODE_CLASSES_REVERSE = {
    'resume': 'resume', 'new': 'new', 'choose': 'switch',
    'fresh': 'live', 'toram': 'ram',
}
_BOOT_MODE_SELECTORS = {
    'resume': 'perchdir=resume',
    'new': 'perchdir=new',
    'choose': 'perchdir=ask',
    'fresh': None,
    'toram': 'toram',
}
_BOOT_MODE_FALLBACK_TITLES = {
    'resume': 'Resume previous session',
    'new': 'Start a new session',
    'choose': 'Choose session during startup',
    'fresh': 'Fresh start',
    'toram': 'Copy to RAM',
}
_SESSION_SELECTOR_TOKENS = frozenset((
    'perchdir=resume', 'perchdir=new', 'perchdir=ask', 'toram',
))

_MANAGED_BOOT_ARGUMENT_FLAGS = frozenset((
    'text', 'nomodeset', 'automount', 'nozram', 'quiet', 'debug',
))
_MANAGED_BOOT_ARGUMENT_KEYS = frozenset((
    'perchmode', 'perchsize', 'perchreserve', 'load', 'noload',
    'zramcomp', 'zramsize', 'locales', 'timezone', 'keyboard-layouts',
    'default-target', 'default_target',
))
_MANAGED_LOCALE_ARGUMENT_KEYS = frozenset((
    'locales', 'timezone', 'keyboard-layouts',
))


def _managed_boot_argument(token):
    if token in _MANAGED_BOOT_ARGUMENT_FLAGS:
        return True
    if token in ('toram=full', 'toram=trim'):
        return True
    if '=' not in token:
        return False
    name, value = token.split('=', 1)
    if not value or name not in _MANAGED_BOOT_ARGUMENT_KEYS:
        return False
    allowed = {
        'perchmode': frozenset((
            'native', 'dynfilefs', 'raw', 'luks', 'squashfs')),
        'zramcomp': frozenset(('lzo', 'lzo-rle', 'lz4', 'lz4hc', 'zstd')),
        'default-target': frozenset((
            'graphical', 'graphical.target', 'multi-user',
            'multi-user.target', 'rescue', 'rescue.target')),
        'default_target': frozenset((
            'graphical', 'graphical.target', 'multi-user',
            'multi-user.target', 'rescue', 'rescue.target')),
    }.get(name)
    return allowed is None or value in allowed


def _managed_locale_argument(token):
    return ('=' in token and
            token.split('=', 1)[0] in _MANAGED_LOCALE_ARGUMENT_KEYS)


def _constructor_arguments(arguments):
    return ' '.join(token for token in arguments.split()
                    if _managed_boot_argument(token))


def _kernel_arguments_for_base(arguments, base_mode, replace_managed=False,
                               preserve_locale=False):
    tokens = [token for token in arguments.split()
              if token not in _SESSION_SELECTOR_TOKENS and
              not (replace_managed and _managed_boot_argument(token) and
                   not (preserve_locale and
                        _managed_locale_argument(token)))]
    selector = _BOOT_MODE_SELECTORS[base_mode]
    if selector:
        tokens.append(selector)
    return ' '.join(tokens)


def _append_extra_arguments(arguments, *values):
    result = arguments.strip()
    for value in values:
        value = (value or '').strip()
        if value:
            result = (result + ' ' + value).strip()
    return result


def _rename_grub_menu_block(block, title):
    if not block:
        raise ImageProjectError('GRUB menu entry block is empty')
    body, ending = _boot_line_body(block[0])
    match = re.match(r'^(\s*menuentry\s+)"([^"]*)"(.*)$', body)
    if not match:
        raise ImageProjectError('GRUB menu entry title syntax is unsupported')
    block[0] = '{}"{}"{}{}'.format(
        match.group(1), title, match.group(3), ending)
    return block


def _set_grub_menu_block_mode(block, base_mode, replace_managed=False,
                              preserve_locale=False):
    body, ending = _boot_line_body(block[0])
    target_class = _GRUB_MODE_CLASSES_REVERSE[base_mode]
    replaced = [False]

    def class_replacement(match):
        value = match.group(2)
        if value in _GRUB_CLASS_MODES:
            replaced[0] = True
            return match.group(1) + target_class
        return match.group(0)

    body = re.sub(
        r'(--class(?:=|\s+))([A-Za-z0-9_-]+)', class_replacement, body)
    if not replaced[0]:
        brace = body.rfind('{')
        if brace < 0:
            raise ImageProjectError('GRUB menu entry declaration is unsupported')
        body = body[:brace].rstrip() + ' --class ' + target_class + ' ' + body[brace:]
    block[0] = body + ending
    kernel_count = 0
    for index in range(1, len(block) - 1):
        body, ending = _boot_line_body(block[index])
        match = re.match(
            r'^(\s*(?:linux|linuxefi|linux16)\s+\S+)(?:\s+(.*))?$', body)
        if not match:
            continue
        arguments = _kernel_arguments_for_base(
            match.group(2) or '', base_mode, replace_managed=replace_managed,
            preserve_locale=preserve_locale)
        block[index] = match.group(1) + (
            ' ' + arguments if arguments else '') + ending
        kernel_count += 1
    if kernel_count == 0:
        raise ImageProjectError('GRUB session template has no kernel command')
    return block


def _append_grub_block_arguments(block, *values):
    for index in range(1, len(block) - 1):
        body, ending = _boot_line_body(block[index])
        match = re.match(
            r'^(\s*(?:linux|linuxefi|linux16)\s+\S+)(?:\s+(.*))?$', body)
        if not match:
            continue
        arguments = _append_extra_arguments(match.group(2) or '', *values)
        block[index] = match.group(1) + (
            ' ' + arguments if arguments else '') + ending
    return block


def _syslinux_title_text(title, menu_locale):
    codec = 'cp866' if menu_locale == 'ru_RU' else 'iso-8859-1'
    try:
        return title.encode(codec, 'strict').decode('latin-1')
    except UnicodeError:
        raise ImageProjectError(
            'custom boot menu title cannot be represented by the selected '
            'SYSLINUX menu encoding')


def _rename_syslinux_menu_block(block, title, menu_locale):
    replacement = _syslinux_title_text(title, menu_locale)
    matches = []
    for index, line in enumerate(block):
        body, ending = _boot_line_body(line)
        if re.match(r'^\s*MENU\s+LABEL(?:\s|$)', body, re.IGNORECASE):
            matches.append((index, body, ending))
    if len(matches) != 1:
        raise ImageProjectError(
            'SYSLINUX session entry needs exactly one MENU LABEL directive')
    index, body, ending = matches[0]
    indent = body[:len(body) - len(body.lstrip())]
    block[index] = '{}MENU LABEL {}{}'.format(indent, replacement, ending)
    return block


def _set_syslinux_menu_block_mode(block, base_mode, entry_id,
                                  replace_managed=False,
                                  preserve_locale=False):
    label_count = 0
    append_count = 0
    output = []
    for line in block:
        body, ending = _boot_line_body(line)
        label_match = re.match(r'^(\s*LABEL\s+)\S+\s*$', body, re.IGNORECASE)
        if label_match:
            output.append(label_match.group(1) + entry_id + ending)
            label_count += 1
            continue
        if re.match(r'^\s*MENU\s+DEFAULT\s*$', body, re.IGNORECASE):
            continue
        append_match = re.match(r'^(\s*APPEND)(?:\s+(.*))?$', body, re.IGNORECASE)
        if append_match:
            arguments = _kernel_arguments_for_base(
                append_match.group(2) or '', base_mode,
                replace_managed=replace_managed,
                preserve_locale=preserve_locale)
            output.append(append_match.group(1) + (
                ' ' + arguments if arguments else '') + ending)
            append_count += 1
            continue
        output.append(line)
    if label_count != 1 or append_count != 1:
        raise ImageProjectError(
            'SYSLINUX session template needs one LABEL and one APPEND directive')
    return output


def _append_syslinux_block_arguments(block, *values):
    for index, line in enumerate(block):
        body, ending = _boot_line_body(line)
        match = re.match(r'^(\s*APPEND)(?:\s+(.*))?$', body, re.IGNORECASE)
        if not match:
            continue
        arguments = _append_extra_arguments(match.group(2) or '', *values)
        block[index] = match.group(1) + (
            ' ' + arguments if arguments else '') + ending
    return block


def _rebuild_semantic_menu_blocks(lines, blocks, boot_menu, kind,
                                  menu_locale='en_US', global_args=None):
    """Rebuild one contiguous MiniOS session menu from reusable templates."""
    if not blocks:
        return lines, None
    ordered = sorted(blocks, key=lambda item: item[0])
    for index in range(len(ordered) - 1):
        gap = lines[ordered[index][1] + 1:ordered[index + 1][0]]
        if any(_boot_line_body(line)[0].strip() for line in gap):
            raise ImageProjectError(
                'MiniOS session entries are not a contiguous boot-menu block')
    templates = {}
    for start, end, mode in ordered:
        templates.setdefault(mode, list(lines[start:end + 1]))
    generic = list(lines[ordered[0][0]:ordered[0][1] + 1])
    first_start = ordered[0][0]
    last_end = ordered[-1][1]
    gap_template = (lines[ordered[0][1] + 1:ordered[1][0]]
                    if len(ordered) > 1 else [])
    enabled_entries = []
    rebuilt = []
    for item in boot_menu:
        if not item['enabled']:
            continue
        base_mode = item['base_mode']
        arguments_schema = item.get('kernel_args_schema')
        replace_managed = arguments_schema in (2, 3)
        preserve_locale = arguments_schema == 3
        has_native_template = base_mode in templates
        block = list(templates.get(base_mode, generic))
        if kind == 'grub':
            block = _set_grub_menu_block_mode(
                block, base_mode, replace_managed=replace_managed,
                preserve_locale=preserve_locale)
            if item['title']:
                block = _rename_grub_menu_block(block, item['title'])
            elif not has_native_template:
                block = _rename_grub_menu_block(
                    block, _BOOT_MODE_FALLBACK_TITLES[base_mode])
            block = _append_grub_block_arguments(
                block, global_args, item['kernel_args'])
        else:
            block = _set_syslinux_menu_block_mode(
                block, base_mode, item['id'],
                replace_managed=replace_managed,
                preserve_locale=preserve_locale)
            if item['title']:
                block = _rename_syslinux_menu_block(
                    block, item['title'], menu_locale)
            elif not has_native_template:
                block = _rename_syslinux_menu_block(
                    block, _BOOT_MODE_FALLBACK_TITLES[base_mode], menu_locale)
            block = _append_syslinux_block_arguments(
                block, global_args, item['kernel_args'])
        if rebuilt and gap_template:
            rebuilt.extend(gap_template)
        rebuilt.extend(block)
        enabled_entries.append(item)
    if not enabled_entries:
        raise ImageProjectError('custom boot menu removed every session entry')
    return lines[:first_start] + rebuilt + lines[last_end + 1:], enabled_entries


def _transform_grub_payload(payload, timeout, default_boot, kernel_args,
                            boot_menu_entries=None):
    try:
        text = payload.decode('utf-8', 'strict')
    except UnicodeError:
        raise ImageProjectError(
            'effective GRUB configuration is not valid UTF-8')
    lines = text.splitlines(True)
    if text and not lines:
        lines = [text]
    if default_boot and any(
            re.match(r'^\s*submenu(?:\s|$)', _boot_line_body(line)[0])
            for line in lines):
        raise ImageProjectError(
            'GRUB submenu default selector is unsupported')
    all_menu_entries = _grub_menu_entries(lines)
    semantic_entries = []
    semantic_blocks = []
    kernel_indexes = []
    for menu_index, (start, end, declaration) in enumerate(all_menu_entries):
        classes = re.findall(
            r'--class(?:=|\s+)([A-Za-z0-9_-]+)', declaration)
        known = set(_GRUB_CLASS_MODES[value] for value in classes
                    if value in _GRUB_CLASS_MODES)
        if len(known) > 1:
            raise ImageProjectError(
                'GRUB menuentry has conflicting semantic classes')
        argument_modes = []
        entry_kernel_indexes = []
        for line_index in range(start + 1, end):
            body, unused_ending = _boot_line_body(lines[line_index])
            match = re.match(
                r'^\s*(?:linux|linuxefi|linux16)\s+\S+(?:\s+(.*))?$', body)
            if match:
                if '#' in (match.group(1) or '') or body.rstrip().endswith('\\'):
                    raise ImageProjectError(
                        'GRUB kernel command syntax is unsupported')
                entry_kernel_indexes.append(line_index)
                mode = _boot_semantic(match.group(1) or '')
                if mode is not None:
                    argument_modes.append(mode)
        if len(set(argument_modes)) > 1:
            raise ImageProjectError(
                'GRUB menuentry kernel lines disagree on session semantics')
        inferred = argument_modes[0] if argument_modes else None
        declared = next(iter(known)) if known else None
        if declared is not None and inferred is not None and declared != inferred:
            raise ImageProjectError(
                'GRUB semantic class conflicts with kernel arguments')
        semantic = declared or inferred
        if semantic is not None:
            if not entry_kernel_indexes:
                raise ImageProjectError(
                    'GRUB semantic entry has no kernel command')
            semantic_entries.append((menu_index, semantic))
            semantic_blocks.append((start, end, semantic))
        kernel_indexes.extend(entry_kernel_indexes)
    references = _boot_config_references_payload(payload, 'grub')
    session = bool(semantic_entries)
    boot_menu = _boot_menu_sequence(boot_menu_entries)
    enabled_entries = None
    if boot_menu is not None and session:
        if len(all_menu_entries) != len(semantic_entries):
            raise ImageProjectError(
                'custom GRUB session menu contains non-session entries')
        lines, enabled_entries = _rebuild_semantic_menu_blocks(
            lines, semantic_blocks, boot_menu, 'grub', global_args=kernel_args)
    elif kernel_args and session:
        for line_index in kernel_indexes:
            body, ending = _boot_line_body(lines[line_index])
            lines[line_index] = body + ' ' + kernel_args + ending
    if enabled_entries is not None:
        default_indexes = [index for index, item in enumerate(enabled_entries)
                           if item['default']]
        if len(default_indexes) != 1:
            raise ImageProjectError('custom GRUB menu has an invalid default entry')
        lines = _boot_replace_or_prepend(
            lines, re.compile(r'^\s*set\s+default\s*='),
            'set default={}'.format(default_indexes[0]))
    elif default_boot and session:
        matches = [menu_index for menu_index, semantic in semantic_entries
                   if semantic == default_boot]
        if len(matches) != 1:
            raise ImageProjectError(
                'effective GRUB menu cannot prove the requested default')
        lines = _boot_replace_or_prepend(
            lines, re.compile(r'^\s*set\s+default\s*='),
            'set default={}'.format(matches[0]))
    if timeout is not None:
        lines = _boot_replace_or_prepend(
            lines, re.compile(r'^\s*set\s+timeout\s*='),
            'set timeout={}'.format(timeout))
    if (default_boot or kernel_args or boot_menu is not None) and not session and not references:
        raise ImageProjectError(
            'effective GRUB config has no provable session menu or chain')
    return ''.join(lines).encode('utf-8'), references, session


def _transform_syslinux_payload(payload, timeout, default_boot, kernel_args,
                                boot_menu_entries=None, menu_locale='en_US'):
    text = payload.decode('latin-1')
    lines = text.splitlines(True)
    if default_boot and any(re.match(
            r'^\s*MENU\s+BEGIN(?:\s|$)', _boot_line_body(line)[0],
            re.IGNORECASE) for line in lines):
        raise ImageProjectError(
            'SYSLINUX nested-menu default selector is unsupported')
    label_starts = []
    references = _boot_config_references_payload(payload, 'syslinux')
    for index, line in enumerate(lines):
        body, unused_ending = _boot_line_body(line)
        match = re.match(r'^\s*LABEL\s+(\S+)\s*$', body, re.IGNORECASE)
        if match:
            label_starts.append((index, match.group(1)))
    semantic_entries = []
    semantic_blocks = []
    append_indexes = []
    for position, (start, label) in enumerate(label_starts):
        end = (label_starts[position + 1][0]
               if position + 1 < len(label_starts) else len(lines))
        kernel = False
        appends = []
        for line_index in range(start + 1, end):
            body, unused_ending = _boot_line_body(lines[line_index])
            kernel_match = re.match(
                r'^\s*(?:KERNEL|LINUX)\s+(\S+)\s*$', body,
                re.IGNORECASE)
            if kernel_match and 'vmlinuz' in kernel_match.group(1).lower():
                kernel = True
            append_match = re.match(
                r'^\s*APPEND(?:\s+(.*))?$', body, re.IGNORECASE)
            if append_match:
                if ('#' in (append_match.group(1) or '') or
                        body.rstrip().endswith('\\')):
                    raise ImageProjectError(
                        'SYSLINUX APPEND syntax is unsupported')
                appends.append((line_index, append_match.group(1) or ''))
        if not kernel:
            continue
        if len(appends) != 1:
            raise ImageProjectError(
                'SYSLINUX kernel entry needs exactly one APPEND directive')
        semantic = _boot_semantic(appends[0][1])
        label_semantic = _SYSLINUX_LABEL_MODES.get(label.lower())
        if semantic is None:
            raise ImageProjectError(
                'SYSLINUX kernel entry has no provable MiniOS session semantics')
        if label_semantic is not None and label_semantic != semantic:
            raise ImageProjectError(
                'SYSLINUX label conflicts with APPEND semantics')
        semantic_entries.append((label, semantic))
        semantic_blocks.append((start, end - 1, semantic))
        append_indexes.append(appends[0][0])
    session = bool(semantic_entries)
    boot_menu = _boot_menu_sequence(boot_menu_entries)
    enabled_entries = None
    if boot_menu is not None and session:
        if len(label_starts) != len(semantic_entries):
            raise ImageProjectError(
                'custom SYSLINUX session menu contains non-session entries')
        lines, enabled_entries = _rebuild_semantic_menu_blocks(
            lines, semantic_blocks, boot_menu, 'syslinux',
            menu_locale=menu_locale, global_args=kernel_args)
    elif kernel_args and session:
        for line_index in append_indexes:
            body, ending = _boot_line_body(lines[line_index])
            lines[line_index] = body + ' ' + kernel_args + ending
    if enabled_entries is not None:
        defaults = [item for item in enabled_entries if item['default']]
        if len(defaults) != 1:
            raise ImageProjectError('custom SYSLINUX menu has an invalid default entry')
        default_label = defaults[0]['id']
        lines = [line for line in lines if not re.match(
            r'^\s*MENU\s+DEFAULT\s*$', _boot_line_body(line)[0],
            re.IGNORECASE)]
        lines = _boot_replace_or_prepend(
            lines, re.compile(r'^\s*DEFAULT(?:\s|$)', re.IGNORECASE),
            'DEFAULT {}'.format(default_label))
        timeout_default = re.compile(
            r'^\s*ONTIMEOUT(?:\s|$)', re.IGNORECASE)
        replaced = []
        for line in lines:
            body, ending = _boot_line_body(line)
            if timeout_default.match(body):
                indent = body[:len(body) - len(body.lstrip())]
                replaced.append(indent + 'ONTIMEOUT ' + default_label + ending)
            else:
                replaced.append(line)
        lines = replaced
    elif default_boot and session:
        matches = [label for label, semantic in semantic_entries
                   if semantic == default_boot]
        if len(matches) != 1:
            raise ImageProjectError(
                'effective SYSLINUX menu cannot prove the requested default')
        default_label = matches[0]
        lines = [line for line in lines if not re.match(
            r'^\s*MENU\s+DEFAULT\s*$', _boot_line_body(line)[0],
            re.IGNORECASE)]
        lines = _boot_replace_or_prepend(
            lines, re.compile(r'^\s*DEFAULT(?:\s|$)', re.IGNORECASE),
            'DEFAULT {}'.format(default_label))
        timeout_default = re.compile(
            r'^\s*ONTIMEOUT(?:\s|$)', re.IGNORECASE)
        replaced = []
        for line in lines:
            body, ending = _boot_line_body(line)
            if timeout_default.match(body):
                indent = body[:len(body) - len(body.lstrip())]
                replaced.append(indent + 'ONTIMEOUT ' + default_label + ending)
            else:
                replaced.append(line)
        lines = replaced
    if timeout is not None:
        lines = _boot_replace_or_prepend(
            lines, re.compile(r'^\s*TIMEOUT(?:\s|$)', re.IGNORECASE),
            'TIMEOUT {}'.format(timeout * 10))
    if (default_boot or kernel_args or boot_menu is not None) and not session and not references:
        raise ImageProjectError(
            'effective SYSLINUX config has no provable session menu or chain')
    return ''.join(lines).encode('latin-1'), references, session


def _source_entry_id(mode, used):
    candidate = mode
    number = 2
    while candidate in used:
        candidate = '{}-{}'.format(mode, number)
        number += 1
    used.add(candidate)
    return candidate


def _grub_source_menu(payload):
    try:
        text = payload.decode('utf-8', 'strict')
    except UnicodeError:
        raise ImageProjectError(
            'effective GRUB configuration is not valid UTF-8')
    lines = text.splitlines(True)
    menu_blocks = _grub_menu_entries(lines)
    entries = []
    used = set()
    for menu_index, (start, end, declaration) in enumerate(menu_blocks):
        classes = re.findall(
            r'--class(?:=|\s+)([A-Za-z0-9_-]+)', declaration)
        known = set(_GRUB_CLASS_MODES[value] for value in classes
                    if value in _GRUB_CLASS_MODES)
        if len(known) > 1:
            raise ImageProjectError(
                'GRUB menuentry has conflicting semantic classes')
        arguments = []
        modes = []
        for line_index in range(start + 1, end):
            body, unused_ending = _boot_line_body(lines[line_index])
            match = re.match(
                r'^\s*(?:linux|linuxefi|linux16)\s+\S+(?:\s+(.*))?$', body)
            if not match:
                continue
            value = match.group(1) or ''
            if '#' in value or body.rstrip().endswith('\\'):
                raise ImageProjectError(
                    'GRUB kernel command syntax is unsupported')
            arguments.append(value)
            semantic = _boot_semantic(value)
            if semantic is not None:
                modes.append(semantic)
        if len(set(modes)) > 1:
            raise ImageProjectError(
                'GRUB menuentry kernel lines disagree on session semantics')
        inferred = modes[0] if modes else None
        declared = next(iter(known)) if known else None
        if declared is not None and inferred is not None and declared != inferred:
            raise ImageProjectError(
                'GRUB semantic class conflicts with kernel arguments')
        mode = declared or inferred
        if mode is None:
            continue
        if len(arguments) != 1:
            raise ImageProjectError(
                'recognized GRUB source entry needs exactly one kernel command')
        title_match = re.match(r'^\s*menuentry\s+"([^"]*)"', declaration)
        if not title_match:
            raise ImageProjectError(
                'GRUB menu entry title syntax is unsupported')
        entries.append({
            'id': _source_entry_id(mode, used), 'base_mode': mode,
            'enabled': True, 'default': False,
            'title': title_match.group(1).strip() or None,
            'kernel_args': _constructor_arguments(arguments[0]),
            'kernel_args_schema': 2, '_selector': menu_index,
        })
    default_index = None
    default_known = False
    default_values = []
    dynamic_default = False
    timeout_values = []
    dynamic_timeout = False
    for line in lines:
        body, unused_ending = _boot_line_body(line)
        match = re.match(r'^\s*set\s+default\s*=\s*([0-9]+)\s*$', body)
        if match:
            default_values.append(int(match.group(1)))
        elif re.match(r'^\s*set\s+default\s*=', body):
            dynamic_default = True
        match = re.match(r'^\s*set\s+timeout\s*=\s*([0-9]+)\s*$', body)
        if match:
            timeout_values.append(int(match.group(1)))
        elif re.match(r'^\s*set\s+timeout\s*=', body):
            dynamic_timeout = True
    if default_values and not dynamic_default and len(set(default_values)) == 1:
        default_index = default_values[0]
        matches = [item for item in entries if item['_selector'] == default_index]
        if len(matches) == 1:
            matches[0]['default'] = True
            default_known = True
    timeout = None
    timeout_known = False
    if timeout_values and not dynamic_timeout and len(set(timeout_values)) == 1:
        timeout = timeout_values[0]
        timeout_known = timeout <= 300
        if not timeout_known:
            timeout = None
    for item in entries:
        del item['_selector']
    return {
        'entries': entries, 'default_known': default_known,
        'timeout': timeout, 'timeout_known': timeout_known,
        'references': _boot_config_references_payload(payload, 'grub'),
    }


def _syslinux_source_menu(payload, menu_locale='en_US'):
    lines = payload.decode('latin-1').splitlines(True)
    labels = []
    for index, line in enumerate(lines):
        body, unused_ending = _boot_line_body(line)
        match = re.match(r'^\s*LABEL\s+(\S+)\s*$', body, re.IGNORECASE)
        if match:
            labels.append((index, match.group(1)))
    entries = []
    by_label = {}
    menu_defaults = []
    used = set()
    for position, (start, label) in enumerate(labels):
        end = labels[position + 1][0] if position + 1 < len(labels) else len(lines)
        kernel = False
        appends = []
        titles = []
        is_menu_default = False
        for line_index in range(start + 1, end):
            body, unused_ending = _boot_line_body(lines[line_index])
            kernel_match = re.match(
                r'^\s*(?:KERNEL|LINUX)\s+(\S+)\s*$', body, re.IGNORECASE)
            if kernel_match and 'vmlinuz' in kernel_match.group(1).lower():
                kernel = True
            append_match = re.match(
                r'^\s*APPEND(?:\s+(.*))?$', body, re.IGNORECASE)
            if append_match:
                value = append_match.group(1) or ''
                if '#' in value or body.rstrip().endswith('\\'):
                    raise ImageProjectError(
                        'SYSLINUX APPEND syntax is unsupported')
                appends.append(value)
            title_match = re.match(
                r'^\s*MENU\s+LABEL(?:\s+(.*))?$', body, re.IGNORECASE)
            if title_match:
                titles.append((title_match.group(1) or '').strip())
            if re.match(r'^\s*MENU\s+DEFAULT\s*$', body, re.IGNORECASE):
                is_menu_default = True
        if not kernel:
            continue
        if len(appends) != 1:
            raise ImageProjectError(
                'SYSLINUX kernel entry needs exactly one APPEND directive')
        mode = _boot_semantic(appends[0])
        label_mode = _SYSLINUX_LABEL_MODES.get(label.lower())
        if mode is None:
            raise ImageProjectError(
                'SYSLINUX kernel entry has no provable MiniOS session semantics')
        if label_mode is not None and label_mode != mode:
            raise ImageProjectError(
                'SYSLINUX label conflicts with APPEND semantics')
        if len(titles) > 1:
            raise ImageProjectError(
                'SYSLINUX source entry has multiple MENU LABEL directives')
        title = titles[0] if titles and titles[0] else None
        if title:
            codec = 'cp866' if menu_locale == 'ru_RU' else 'iso-8859-1'
            try:
                title = title.encode('latin-1').decode(codec)
            except UnicodeError:
                raise ImageProjectError(
                    'SYSLINUX menu title uses an invalid source encoding')
        entry = {
            'id': _source_entry_id(mode, used), 'base_mode': mode,
            'enabled': True, 'default': False,
            'title': title,
            'kernel_args': _constructor_arguments(appends[0]),
            'kernel_args_schema': 2,
        }
        entries.append(entry)
        by_label[label.lower()] = entry
        if is_menu_default:
            menu_defaults.append(entry)
    selected_defaults = list(menu_defaults)
    timeout_values = []
    dynamic_timeout = False
    for line in lines:
        body, unused_ending = _boot_line_body(line)
        match = re.match(r'^\s*(?:DEFAULT|ONTIMEOUT)\s+(\S+)\s*$', body,
                         re.IGNORECASE)
        if match:
            selected = by_label.get(match.group(1).lower())
            if selected is not None:
                selected_defaults.append(selected)
        match = re.match(r'^\s*TIMEOUT\s+([0-9]+)\s*$', body, re.IGNORECASE)
        if match:
            timeout_values.append(int(match.group(1)))
        elif re.match(r'^\s*TIMEOUT(?:\s|$)', body, re.IGNORECASE):
            dynamic_timeout = True
    default_ids = set(item['id'] for item in selected_defaults)
    default_known = len(default_ids) == 1
    if default_known:
        selected_defaults[0]['default'] = True
    timeout = None
    timeout_known = False
    if timeout_values and not dynamic_timeout and len(set(timeout_values)) == 1:
        deciseconds = timeout_values[0]
        if deciseconds % 10 == 0 and deciseconds // 10 <= 300:
            timeout = deciseconds // 10
            timeout_known = True
    return {
        'entries': entries, 'default_known': default_known,
        'timeout': timeout, 'timeout_known': timeout_known,
        'references': _boot_config_references_payload(payload, 'syslinux'),
    }


def inspect_source_boot_menu(source_info, menu_locale):
    """Read and recognize the effective MiniOS boot menu for a source."""
    if not isinstance(source_info, SourceInfo) or not source_info.supported:
        raise ValueError('a supported SourceInfo is required')
    if menu_locale not in MENU_LOCALES:
        raise ValueError('unsupported menu locale: {}'.format(menu_locale))
    bootloader = source_info.metadata.get('bootloader')
    included = tuple(item['relative_path'] for item in source_info.input_manifest)
    mapping, roots = _effective_boot_config_mapping(
        source_info.source_path, bootloader, menu_locale, included)
    candidates = []

    def visit(target, root, visiting, visited):
        if target in visited:
            return
        if target in visiting:
            raise SourceInspectionError('effective boot config graph has a cycle')
        item = mapping.get(target)
        if item is None:
            raise SourceInspectionError(
                'effective boot config references an unavailable config')
        visiting.add(target)
        result = (_grub_source_menu(item[0]) if item[1] == 'grub'
                  else _syslinux_source_menu(
                      item[0], _syslinux_menu_locale_for_target(
                          target, menu_locale)))
        if result['entries']:
            candidates.append((root, target, item[1], result))
        for reference in result['references']:
            visit(_resolve_boot_config_reference(target, reference, item[1]),
                  root, visiting, visited)
        visiting.remove(target)
        visited.add(target)

    try:
        for root in roots:
            visit(root, root, set(), set())
    except ImageProjectError as error:
        raise SourceInspectionError(str(error))
    if not candidates:
        raise SourceInspectionError(
            'effective boot config graph has no recognized MiniOS menu')

    if menu_locale == 'multilang':
        for unused_root, unused_target, unused_kind, result in candidates:
            for item in result['entries']:
                item['kernel_args'] = ' '.join(
                    token for token in item['kernel_args'].split()
                    if not _managed_locale_argument(token))
                item['kernel_args_schema'] = 3

    def structure(result):
        return (
            tuple((item['base_mode'], item['kernel_args'])
                  for item in result['entries']),
            result['default_known'],
            next((index for index, item in enumerate(result['entries'])
                  if item['default']), None),
            result['timeout_known'], result['timeout'],
        )

    structures = set(structure(item[3]) for item in candidates)
    if len(structures) != 1:
        raise SourceInspectionError(
            'effective boot config roots have conflicting MiniOS menu structures')
    preferred_kind = 'syslinux' if bootloader == 'syslinux-native' else 'grub'
    selected = sorted(
        candidates,
        key=lambda item: (item[2] != preferred_kind, item[1], item[0]))[0]
    result = selected[3]
    if menu_locale == 'multilang':
        # Each language menu owns its translated titles. Leaving them unset
        # preserves those source titles when a recognized menu is customized.
        result = dict(result)
        result['entries'] = [dict(item, title=None)
                             for item in result['entries']]
    diagnostics = []
    if not result['default_known']:
        diagnostics.append('source boot default is dynamic or unsupported')
    if not result['timeout_known']:
        diagnostics.append('source boot timeout is dynamic or unsupported')
    return {
        'entries': tuple(_freeze(dict(item)) for item in result['entries']),
        'default_known': result['default_known'],
        'timeout': result['timeout'], 'timeout_known': result['timeout_known'],
        'representative': selected[1], 'bootloader': selected[2],
        'diagnostics': tuple(diagnostics),
    }


def _syslinux_menu_locale_for_target(target, configured_locale):
    if configured_locale != 'multilang':
        return configured_locale
    match = re.match(r'^minios/boot/syslinux/lang/([A-Za-z0-9_-]+)[.]cfg$', target)
    return match.group(1) if match else 'en_US'


def _expected_boot_customization_records(
        source_path, bootloader, menu_locale, included_relative, timeout,
        default_boot, kernel_args, boot_menu_entries=None):
    mapping, roots = _effective_boot_config_mapping(
        source_path, bootloader, menu_locale, included_relative)
    visiting = set()
    visited = set()
    records = []
    transformed_payloads = {}

    def visit(target):
        if target in visited:
            return 0
        if target in visiting:
            raise ImageProjectError('effective boot config graph has a cycle')
        item = mapping.get(target)
        if item is None:
            raise ImageProjectError(
                'effective boot config references an unavailable config')
        visiting.add(target)
        if item[1] == 'grub':
            transformed, references, session = _transform_grub_payload(
                item[0], timeout, default_boot, kernel_args,
                boot_menu_entries=boot_menu_entries)
        else:
            transformed, references, session = _transform_syslinux_payload(
                item[0], timeout, default_boot, kernel_args,
                boot_menu_entries=boot_menu_entries,
                menu_locale=_syslinux_menu_locale_for_target(
                    target, menu_locale))
        records.append({
            'target': target, 'size': len(transformed),
            'sha256': hashlib.sha256(transformed).hexdigest(),
        })
        transformed_payloads[target] = transformed
        session_count = 1 if session else 0
        for reference in references:
            session_count += visit(_resolve_boot_config_reference(
                target, reference, item[1]))
        visiting.remove(target)
        visited.add(target)
        return session_count

    for root in roots:
        sessions = visit(root)
        if (default_boot or kernel_args or boot_menu_entries is not None) and sessions == 0:
            raise ImageProjectError(
                'effective boot root has no provable MiniOS session menu')
    return (tuple(sorted(records, key=lambda item: item['target'])),
            transformed_payloads)


def _runtime_release_metadata(path):
    if not path or not os.path.isfile(path):
        return {}
    allowed = {
        'VERSION': 'version', 'VERSION_ID': 'version_id',
        'EDITION': 'edition', 'ARCH': 'architecture',
        'DISTRIBUTION': 'distribution', 'BUILD_ID': 'build_id',
        'PRETTY_NAME': 'pretty_name',
    }
    try:
        if os.path.getsize(path) > 64 * 1024:
            return {}
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            lines = handle.readlines()
    except OSError:
        return {}
    result = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key not in allowed:
            continue
        value = value.strip()
        if (len(value) >= 2 and value[0] == value[-1] and
                value[0] in "'\""):
            value = value[1:-1]
        if value:
            result[allowed[key]] = value[:1024]
    return result


def _detect_initramfs_implementation(root_path, default_label):
    """Return the real MiniOS initramfs implementation for a mount root.

    livekit-mos and dracut-mos both mount the MiniOS medium at the same place
    (``/run/initramfs/memory``), so the mount path alone cannot distinguish
    them. Each initramfs runtime leaves an implementation directory next to the
    mount root (for example ``/run/initramfs/dracut-mos``); use it when present
    and otherwise fall back to the configured label.
    """
    parent = os.path.dirname(os.path.normpath(root_path))
    for marker, label in (('dracut-mos', 'dracut'), ('livekit-mos', 'livekit')):
        try:
            marker_stat = os.lstat(os.path.join(parent, marker))
        except OSError:
            continue
        if stat.S_ISDIR(marker_stat.st_mode) and not stat.S_ISLNK(
                marker_stat.st_mode):
            return label
    return default_label


def _normalize_roots(roots=None, livekit_root=None, dracut_root=None):
    if roots is None and (livekit_root is not None or dracut_root is not None):
        roots = []
        if livekit_root is not None:
            roots.append(('livekit', livekit_root))
        if dracut_root is not None:
            roots.append(('dracut', dracut_root))
    if roots is None:
        roots = DEFAULT_RUNNING_ROOTS
    if isinstance(roots, str):
        roots = (roots,)
    if isinstance(roots, dict):
        roots = list(roots.items())
    result = []
    for index, item in enumerate(roots):
        if isinstance(item, str):
            backend = 'livekit' if index == 0 else 'dracut'
            path = item
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            backend, path = item
        else:
            raise ValueError('invalid running root entry')
        result.append((backend, os.path.abspath(_path_string(path))))
    return tuple(result)


def discover_running_source(roots=None, livekit_root=None, dracut_root=None,
                            mounts_path='/proc/mounts',
                            sys_block_root='/sys/class/block',
                            runtime_release_path='/etc/minios-release',
                            subdirectories=None):
    """Discover livekit/dracut data, medium, or iso MiniOS source trees."""
    roots = _normalize_roots(roots, livekit_root, dracut_root)
    if subdirectories is None:
        subdirectories = SOURCE_SUBDIRECTORIES
    candidates = []
    diagnostics = []
    errors = []
    existing_roots = 0
    for backend, root_path in roots:
        try:
            root_stat = os.lstat(root_path)
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ENOTDIR):
                errors.append(Diagnostic(
                    'error', 'live_root_unreadable',
                    'Cannot inspect live root: {}'.format(error), root_path))
            continue
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            continue
        existing_roots += 1
        for category in subdirectories:
            source_path = os.path.join(root_path, category, 'minios')
            try:
                source_stat = os.lstat(source_path)
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTDIR):
                    errors.append(Diagnostic(
                        'error', 'source_candidate_unreadable',
                        'Cannot inspect source candidate: {}'.format(error),
                        source_path))
                continue
            if (not stat.S_ISDIR(source_stat.st_mode) or
                    stat.S_ISLNK(source_stat.st_mode)):
                diagnostics.append(Diagnostic(
                    'warning', 'source_candidate_not_directory',
                    'Source candidate is not a real directory.', source_path))
                continue
            boot_path = os.path.join(source_path, 'boot')
            try:
                boot_stat = os.lstat(boot_path)
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTDIR):
                    errors.append(Diagnostic(
                        'error', 'source_boot_directory_unreadable',
                        'Cannot inspect source boot directory: {}'.format(error),
                        boot_path))
                else:
                    diagnostics.append(Diagnostic(
                        'warning', 'source_boot_directory_missing',
                        'Source candidate has no boot directory.', source_path))
                continue
            if not stat.S_ISDIR(boot_stat.st_mode):
                diagnostics.append(Diagnostic(
                    'warning', 'source_boot_directory_missing',
                    'Source candidate has no real boot directory.', source_path))
                continue
            candidates.append((backend, root_path, category, source_path))
    if not candidates:
        if errors:
            return SourceInfo(SOURCE_ERROR,
                              diagnostics=tuple(errors + diagnostics))
        code = 'minios_source_not_found' if existing_roots else 'not_running_minios'
        message = ('Live roots contain no supported MiniOS source tree.'
                   if existing_roots else
                   'Neither a livekit nor dracut MiniOS root was found.')
        return SourceInfo(
            SOURCE_UNSUPPORTED,
            diagnostics=(Diagnostic('warning', code, message),) +
            tuple(diagnostics))
    backend, root_path, category, source_path = candidates[0]
    backend = _detect_initramfs_implementation(root_path, backend)
    if len(candidates) > 1:
        diagnostics.append(Diagnostic(
            'warning', 'multiple_minios_sources',
            'Multiple sources were found; using {}.'.format(source_path),
            source_path))
    diagnostics.extend(errors)
    try:
        fingerprint, total_bytes, source_manifest = _build_source_manifest(
            source_path)
        modules, module_diagnostics = inspect_source_modules(
            source_path, source_manifest)
        boot = _boot_inventory(source_path)
    except (OSError, SourceInspectionError) as error:
        diagnostics.append(Diagnostic(
            'error', 'source_inspection_failed', str(error), source_path))
        return SourceInfo(
            SOURCE_ERROR, backend=backend, root_path=root_path,
            source_path=source_path, media_category=category,
            diagnostics=tuple(diagnostics))
    diagnostics.extend(module_diagnostics)
    modules, external, active_diagnostics = _map_active_modules(
        modules, mounts_path, sys_block_root)
    diagnostics.extend(active_diagnostics)
    collisions = tuple(item for item in diagnostics if 'collision' in item.code)
    module_paths = set(item.relative_path for item in modules)
    module_bytes = sum(
        item['size'] for item in source_manifest
        if item['relative_path'] in module_paths and item['type'] == 'file')
    metadata = {
        'source_category': category,
        'bootloader': boot['bootloader'],
        'kernel_paths': [os.path.relpath(path, source_path).replace(os.sep, '/')
                         for path in boot['kernel_paths']],
        'initramfs_paths': [
            os.path.relpath(path, source_path).replace(os.sep, '/')
            for path in boot['initramfs_paths']],
        'kernel_versions': list(boot['kernel_versions']),
        'initramfs_versions': list(boot['initramfs_versions']),
        'coherent_kernel_versions': list(boot['coherent_versions']),
        'kernel_version_coherent': boot['version_coherent'],
        'source_module_count': len(modules),
        'active_external_module_count': len(external),
    }
    metadata.update(_runtime_release_metadata(runtime_release_path))
    if not metadata.get('architecture'):
        module_archs = [module.architecture for module in modules
                        if module.architecture]
        if module_archs:
            metadata['architecture'] = max(
                sorted(set(module_archs)), key=module_archs.count)
    return SourceInfo(
        SOURCE_SUPPORTED, backend=backend, root_path=root_path,
        source_path=source_path, media_category=category,
        fingerprint=fingerprint, metadata=metadata, modules=modules,
        active_external_modules=external, diagnostics=tuple(diagnostics),
        collisions=collisions, total_bytes=total_bytes,
        non_module_bytes=max(0, total_bytes - module_bytes),
        input_manifest=source_manifest)


discover_running_minios = discover_running_source


# Alternative source media --------------------------------------------------
# In addition to the running session the builder can remaster a MiniOS ISO
# image file or an optical disc. Those media are mounted read-only by the
# frontend (through udisks) and inspected here: the minios/ tree of a MiniOS
# ISO lives at the mount root, so discovery searches the root directly.

def list_optical_devices(sys_block_root='/sys/class/block', dev_root='/dev'):
    """Return ``[(device_path, label), ...]`` for optical (sr*) drives."""
    devices = []
    try:
        names = sorted(os.listdir(sys_block_root))
    except OSError:
        return devices
    for name in names:
        if not (name.startswith('sr') and name[2:].isdigit()):
            continue
        device = os.path.join(dev_root, name)
        vendor = _read_small_text(
            os.path.join(sys_block_root, name, 'device', 'vendor'))
        model = _read_small_text(
            os.path.join(sys_block_root, name, 'device', 'model'))
        description = ' '.join(part for part in (vendor, model) if part)
        label = ('{} ({})'.format(description, device)
                 if description else device)
        devices.append((device, label))
    return devices


def _read_small_text(path):
    try:
        if os.path.getsize(path) > 4096:
            return ''
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            return handle.read().strip()
    except OSError:
        return ''


def discover_mounted_source(mount_path, media_category='iso',
                            mounts_path='/proc/mounts',
                            sys_block_root='/sys/class/block',
                            runtime_release_path='/etc/minios-release'):
    """Inspect a MiniOS tree on a read-only mounted ISO image or optical disc.

    The mount root holds ``minios/`` directly, so discovery is pointed at the
    root subdirectory. The medium is read-only, so no module is reported active.
    """
    return discover_running_source(
        roots=((media_category, mount_path),),
        mounts_path=mounts_path, sys_block_root=sys_block_root,
        runtime_release_path=runtime_release_path,
        subdirectories=('',))


class ImageProject(_Immutable):
    """Strict schema-v1 project resolved against an explicit base directory."""

    __slots__ = (
        'project_base', 'source_backend', 'source_root_path', 'source_path',
        'source_fingerprint', 'source_fingerprint_algorithm',
        'selected_source_modules', 'additional_module_paths', 'menu_locale',
        'capture_mode', 'include_current_config', 'exclusions', 'output_path',
        'volume_label', 'notes', 'sensitive_config_acknowledged',
        'overwrite_output', 'capture_include_paths', 'capture_exclude_paths',
        'capture_compression', 'sensitive_capture_acknowledged',
        'live_config_overrides', 'boot_timeout', 'default_boot',
        'boot_menu_entries', 'kernel_args', 'boot_background_path',
        'overlay_directory',
        'project_path',
    )

    def __init__(self, project_base, source_backend, source_root_path,
                 source_path, source_fingerprint, selected_source_modules,
                 additional_module_paths=(), menu_locale='multilang',
                 capture_mode='custom', include_current_config=True,
                 exclusions=(), output_path='minios-custom.iso',
                 volume_label=None, notes=None,
                 sensitive_config_acknowledged=False, overwrite_output=False,
                 capture_include_paths=(), capture_exclude_paths=(),
                 capture_compression='zstd',
                  sensitive_capture_acknowledged=False,
                  source_fingerprint_algorithm=SOURCE_FINGERPRINT_ALGORITHM,
                  project_path=None, live_config_overrides=None,
                  boot_timeout=None, default_boot=None, boot_menu_entries=None,
                  kernel_args=None, boot_background_path=None,
                  overlay_directory=None):
        self.project_base = os.path.abspath(_path_string(
            project_base, 'project_base'))
        self.source_backend = _optional_string(
            source_backend, 'source_backend')
        self.source_root_path = _resolve_path(
            source_root_path, self.project_base, 'source_root_path')
        self.source_path = _resolve_path(
            source_path, self.project_base, 'source_path')
        self.source_fingerprint = _optional_string(
            source_fingerprint, 'source_fingerprint')
        self.source_fingerprint_algorithm = _optional_string(
            source_fingerprint_algorithm, 'source_fingerprint_algorithm')
        selected = _unique_strings(
            selected_source_modules, 'selected_source_modules')
        for name in selected:
            if os.path.basename(name) != name or not name.endswith('.sb'):
                raise ValueError('selected modules must be .sb basenames')
        self.selected_source_modules = selected
        paths = _unique_strings(
            additional_module_paths, 'additional_module_paths')
        self.additional_module_paths = tuple(
            _resolve_path(path, self.project_base, 'additional_module_path')
            for path in paths)
        if menu_locale not in MENU_LOCALES:
            raise ValueError('unsupported menu locale: {}'.format(menu_locale))
        self.menu_locale = menu_locale
        if capture_mode not in CAPTURE_MODES:
            raise ValueError('unsupported capture mode: {}'.format(capture_mode))
        self.capture_mode = capture_mode
        includes = _unique_strings(
            capture_include_paths, 'capture_include_paths')
        excludes = _unique_strings(
            capture_exclude_paths, 'capture_exclude_paths')
        includes = tuple(_normalized_session_path(
            path, 'capture include path') for path in includes)
        excludes = tuple(_normalized_session_path(
            path, 'capture exclude path') for path in excludes)
        if set(includes) & set(excludes):
            raise ValueError('capture excludes cannot duplicate includes')
        if capture_mode == 'selected' and not includes:
            raise ValueError('selected capture requires an include path')
        if capture_mode != 'selected' and (includes or excludes):
            raise ValueError(
                'capture include/exclude paths require selected mode')
        if capture_compression not in CAPTURE_COMPRESSIONS:
            raise ValueError('unsupported capture compression')
        self.capture_include_paths = includes
        self.capture_exclude_paths = excludes
        self.capture_compression = capture_compression
        self.sensitive_capture_acknowledged = _require_bool(
            sensitive_capture_acknowledged,
            'sensitive_capture_acknowledged')
        if (capture_mode == 'exact' and
                not self.sensitive_capture_acknowledged):
            raise ValueError(
                'exact capture requires sensitive capture acknowledgement')
        self.include_current_config = _require_bool(
            include_current_config, 'include_current_config')
        if isinstance(exclusions, str):
            exclusions = (exclusions,)
        self.exclusions = _unique_strings(exclusions, 'exclusions')
        if any('\n' in pattern or '\r' in pattern
               for pattern in self.exclusions):
            raise ValueError('exclusion regexes may not contain newlines')
        self.output_path = _resolve_path(
            output_path, self.project_base, 'output_path')
        self.volume_label = (_optional_string(volume_label, 'volume_label')
                             if volume_label is not None else None)
        self.notes = (_optional_string(notes, 'notes', allow_empty=True)
                      if notes is not None else None)
        self.sensitive_config_acknowledged = _require_bool(
            sensitive_config_acknowledged,
            'sensitive_config_acknowledged')
        self.overwrite_output = _require_bool(
            overwrite_output, 'overwrite_output')
        self.live_config_overrides = _freeze(
            validate_live_config_overrides(live_config_overrides))
        if boot_timeout is not None and not _is_strict_int(
                boot_timeout, 0, 300):
            raise ValueError('boot_timeout must be an integer from 0 to 300')
        self.boot_timeout = boot_timeout
        if default_boot is not None and default_boot not in DEFAULT_BOOT_MODES:
            raise ValueError('unsupported default_boot mode')
        self.default_boot = default_boot
        self.boot_menu_entries = validate_boot_menu_entries(boot_menu_entries)
        if self.boot_menu_entries is not None:
            if default_boot is not None:
                raise ValueError(
                    'default_boot and boot_menu_entries cannot be combined')
            if (menu_locale == 'multilang' and
                    any(item['title'] and any(ord(character) >= 128 for character in item['title'])
                        for item in self.boot_menu_entries)):
                raise ValueError(
                    'multilingual custom boot-menu titles must be ASCII')
        if kernel_args is not None:
            validate_kernel_arguments(kernel_args)
        self.kernel_args = kernel_args
        self.boot_background_path = (
            _resolve_path(
                boot_background_path, self.project_base,
                'boot_background_path')
            if boot_background_path is not None else None)
        self.overlay_directory = (
            _resolve_path(
                overlay_directory, self.project_base, 'overlay_directory')
            if overlay_directory is not None else None)
        self.project_path = (os.path.abspath(_path_string(project_path))
                             if project_path is not None else None)
        self._lock()

    @property
    def customization_requested(self):
        return bool(
            self.live_config_overrides or self.boot_timeout is not None or
            self.default_boot is not None or self.boot_menu_entries is not None or
            self.kernel_args is not None or
            self.boot_background_path is not None or
            self.overlay_directory is not None)

    @classmethod
    def from_source(cls, source_info, output_path, project_base, **kwargs):
        if not isinstance(source_info, SourceInfo) or not source_info.supported:
            raise ValueError('a supported SourceInfo is required')
        return cls(
            project_base=project_base,
            source_backend=source_info.backend,
            source_root_path=source_info.root_path,
            source_path=source_info.source_path,
            source_fingerprint=source_info.fingerprint,
            source_fingerprint_algorithm=source_info.fingerprint_algorithm,
            selected_source_modules=[item.basename
                                     for item in source_info.modules],
            output_path=output_path, **kwargs)

    def to_dict(self, base_dir=None):
        base_dir = os.path.abspath(base_dir or self.project_base)
        return {
            'product_kind': PROJECT_KIND,
            'schema_version': PROJECT_SCHEMA_VERSION,
            'source': {
                'kind': SOURCE_KIND,
                'backend': self.source_backend,
                'root_path': _relative_path(self.source_root_path, base_dir),
                'tree_path': _relative_path(self.source_path, base_dir),
                'fingerprint': self.source_fingerprint,
                'fingerprint_algorithm': self.source_fingerprint_algorithm,
            },
            'selected_source_modules': list(self.selected_source_modules),
            'additional_module_paths': [
                _relative_path(path, base_dir)
                for path in self.additional_module_paths],
            'menu_locale': self.menu_locale,
            'capture_mode': self.capture_mode,
            'include_current_config': self.include_current_config,
            'exclusions': list(self.exclusions),
            'output_path': _relative_path(self.output_path, base_dir),
            'volume_label': self.volume_label,
            'notes': self.notes,
            'sensitive_config_acknowledged':
                self.sensitive_config_acknowledged,
            'overwrite_output': self.overwrite_output,
            'capture_include_paths': list(self.capture_include_paths),
            'capture_exclude_paths': list(self.capture_exclude_paths),
            'capture_compression': self.capture_compression,
            'sensitive_capture_acknowledged':
                self.sensitive_capture_acknowledged,
            'live_config_overrides': _thaw(self.live_config_overrides),
            'boot_timeout': self.boot_timeout,
            'default_boot': self.default_boot,
            'boot_menu_entries': _boot_menu_public(self.boot_menu_entries),
            'kernel_args': self.kernel_args,
            'boot_background_path': (
                _relative_path(self.boot_background_path, base_dir)
                if self.boot_background_path is not None else None),
            'overlay_directory': (
                _relative_path(self.overlay_directory, base_dir)
                if self.overlay_directory is not None else None),
        }

    def save(self, path):
        path = os.path.abspath(_path_string(path, 'project_path'))
        atomic_write_json(path, self.to_dict(os.path.dirname(path)))

    @classmethod
    def load(cls, path):
        return load_image_project(path)


_PROJECT_BASE_KEYS = set((
    'product_kind', 'schema_version', 'source', 'selected_source_modules',
    'additional_module_paths', 'menu_locale', 'capture_mode',
    'include_current_config', 'exclusions', 'output_path', 'volume_label',
    'notes', 'sensitive_config_acknowledged', 'overwrite_output',
))
_PROJECT_CAPTURE_KEYS = set((
    'capture_include_paths', 'capture_exclude_paths',
    'capture_compression', 'sensitive_capture_acknowledged',
))
_PROJECT_CUSTOMIZATION_KEYS = set((
    'live_config_overrides', 'boot_timeout', 'default_boot', 'kernel_args',
    'boot_background_path', 'overlay_directory',
))
_PROJECT_OPTIONAL_KEYS = set(('boot_menu_entries',))
_PROJECT_KEYS = (
    _PROJECT_BASE_KEYS | _PROJECT_CAPTURE_KEYS | _PROJECT_CUSTOMIZATION_KEYS |
    _PROJECT_OPTIONAL_KEYS)
_SOURCE_KEYS = set((
    'kind', 'backend', 'root_path', 'tree_path', 'fingerprint',
    'fingerprint_algorithm',
))


def _require_keys(mapping, required, allowed, context):
    if not isinstance(mapping, dict):
        raise ProjectFormatError('{} must be an object'.format(context))
    missing = set(required) - set(mapping)
    unknown = set(mapping) - set(allowed)
    if missing:
        raise ProjectFormatError('{} is missing: {}'.format(
            context, ', '.join(sorted(missing))))
    if unknown:
        raise ProjectFormatError('{} has unknown fields: {}'.format(
            context, ', '.join(sorted(unknown))))


def _read_json_document(path, maximum_bytes=MAX_PROJECT_BYTES):
    try:
        if os.path.getsize(path) > maximum_bytes:
            raise ProjectFormatError('JSON document is too large')
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except ProjectFormatError:
        raise
    except (OSError, ValueError) as error:
        raise ProjectFormatError('Invalid JSON document: {}'.format(error))


def load_image_project(path):
    path = os.path.abspath(_path_string(path, 'project_path'))
    payload = _read_json_document(path)
    if not isinstance(payload, dict):
        raise ProjectFormatError('project must be an object')
    unknown = set(payload) - _PROJECT_KEYS
    missing_base = _PROJECT_BASE_KEYS - set(payload)
    present_capture = set(payload) & _PROJECT_CAPTURE_KEYS
    present_customization = set(payload) & _PROJECT_CUSTOMIZATION_KEYS
    if unknown:
        raise ProjectFormatError('project has unknown fields: {}'.format(
            ', '.join(sorted(unknown))))
    if missing_base:
        raise ProjectFormatError('project is missing: {}'.format(
            ', '.join(sorted(missing_base))))
    if present_capture and present_capture != _PROJECT_CAPTURE_KEYS:
        raise ProjectFormatError(
            'project has an incomplete session capture schema')
    if (present_customization and
            present_customization != _PROJECT_CUSTOMIZATION_KEYS):
        raise ProjectFormatError(
            'project has an incomplete image customization schema')
    if payload.get('product_kind') != PROJECT_KIND:
        raise UnsupportedSchemaError('Unsupported project kind')
    version = payload.get('schema_version')
    if isinstance(version, bool) or version != PROJECT_SCHEMA_VERSION:
        raise UnsupportedSchemaError(
            'Unsupported project schema: {!r}'.format(version))
    source = payload.get('source')
    _require_keys(source, _SOURCE_KEYS, _SOURCE_KEYS, 'source')
    if source.get('kind') != SOURCE_KIND:
        raise ProjectFormatError('Project source must be running-minios')
    base_dir = os.path.dirname(path)
    try:
        return ImageProject(
            project_base=base_dir,
            source_backend=source.get('backend'),
            source_root_path=source.get('root_path'),
            source_path=source.get('tree_path'),
            source_fingerprint=source.get('fingerprint'),
            source_fingerprint_algorithm=source.get('fingerprint_algorithm'),
            selected_source_modules=payload.get('selected_source_modules'),
            additional_module_paths=payload.get('additional_module_paths'),
            menu_locale=payload.get('menu_locale'),
            capture_mode=payload.get('capture_mode'),
            include_current_config=payload.get('include_current_config'),
            exclusions=payload.get('exclusions'),
            output_path=payload.get('output_path'),
            volume_label=payload.get('volume_label'),
            notes=payload.get('notes'),
            sensitive_config_acknowledged=payload.get(
                'sensitive_config_acknowledged'),
            overwrite_output=payload.get('overwrite_output'),
            capture_include_paths=payload.get('capture_include_paths', ()),
            capture_exclude_paths=payload.get('capture_exclude_paths', ()),
            capture_compression=payload.get('capture_compression', 'zstd'),
            sensitive_capture_acknowledged=payload.get(
                'sensitive_capture_acknowledged', False),
            live_config_overrides=payload.get('live_config_overrides', {}),
            boot_timeout=payload.get('boot_timeout'),
            default_boot=payload.get('default_boot'),
            boot_menu_entries=payload.get('boot_menu_entries'),
            kernel_args=payload.get('kernel_args'),
            boot_background_path=payload.get('boot_background_path'),
            overlay_directory=payload.get('overlay_directory'),
            project_path=path)
    except (TypeError, ValueError) as error:
        raise ProjectFormatError('Invalid project data: {}'.format(error))


def atomic_write_json(path, payload):
    path = os.path.abspath(_path_string(path, 'output_path'))
    directory = os.path.dirname(path) or os.curdir
    if not os.path.isdir(directory):
        raise ImageProjectError(
            'Output directory does not exist: {}'.format(directory))
    serialized = _canonical_json_bytes(payload)
    descriptor = None
    temporary_path = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix='.{}.'.format(os.path.basename(path)), suffix='.tmp',
            dir=directory)
        with os.fdopen(descriptor, 'wb') as handle:
            descriptor = None
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(directory)
    except OSError as error:
        raise ImageProjectError(
            'Cannot atomically write {}: {}'.format(path, error))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def compose_module_target(basename):
    basename = os.path.basename(basename)
    if _COMPOSE_TOP_LEVEL_RE.match(basename):
        return 'minios/{}'.format(basename)
    return 'minios/modules/{}'.format(basename)


def _escape_posix_ere_literal(value):
    result = []
    for character in value:
        if character in r'\.^$*+?()[]{}|':
            result.append('\\')
        result.append(character)
    return ''.join(result)


def module_exclusion_regex(basenames):
    names = []
    seen = set()
    for basename in basenames:
        basename = os.path.basename(basename)
        if basename not in seen:
            seen.add(basename)
            names.append(basename)
    if not names:
        return ''
    return r'(^|/)({})$'.format('|'.join(
        _escape_posix_ere_literal(name) for name in names))


def _tool_version_command(name, path):
    if name == 'mkfs.ext2':
        return [path, '-V']
    if name in ('xorriso', 'unsquashfs', 'mksquashfs'):
        return [path, '-version']
    return [path, '--version']


def _resolve_compose_backend_entry(resolver=None):
    """Resolve the fixed composition backend path without probing it.

    The backend is guaranteed by an exact package dependency, so it is not
    version, option, or basename probed. A caller-supplied resolver (used by
    tests) may redirect the fixed path or canonical name; otherwise the fixed
    installed path is checked for an executable regular file. A missing path is
    a broken installation surfaced by the build planner, not an optional
    capability.
    """
    path = None
    if resolver is not None:
        candidate = (resolver(COMPOSE_BACKEND_PATH) or
                     resolver(COMPOSE_BACKEND_NAME))
        path = candidate or None
    elif (os.path.isfile(COMPOSE_BACKEND_PATH) and
            os.access(COMPOSE_BACKEND_PATH, os.X_OK)):
        path = COMPOSE_BACKEND_PATH
    return {'available': bool(path), 'path': path}


def _resolve_trusted_contract_executable(path, resolver=None):
    """Resolve a fixed absolute privileged-contract executable."""
    if not os.path.isabs(path):
        raise ImageProjectError('trusted executable path must be absolute')
    if resolver is not None:
        candidate = resolver(path) or resolver(os.path.basename(path))
        if (not candidate or not os.path.isabs(candidate) or
                os.path.abspath(candidate) != path):
            raise ImageProjectError(
                'trusted executable is unavailable: {}'.format(path))
    try:
        file_stat = os.lstat(path)
    except OSError as error:
        if resolver is not None:
            return path
        raise ImageProjectError(
            'trusted executable is unavailable: {}'.format(error))
    if (stat.S_ISLNK(file_stat.st_mode) or
            not stat.S_ISREG(file_stat.st_mode) or
            not os.access(path, os.X_OK) or file_stat.st_uid != 0 or
            stat.S_IMODE(file_stat.st_mode) & 0o022):
        raise ImageProjectError(
            'trusted executable failed ownership or mode checks: {}'.format(
                path))
    if os.path.realpath(path) != path:
        raise ImageProjectError(
            'trusted executable path is not canonical: {}'.format(path))
    return path


def _strict_cancel_file_path(value):
    if hasattr(os, 'fspath'):
        try:
            value = os.fspath(value)
        except TypeError:
            pass
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('cancel_file must be a non-empty path')
    if '\n' in value or '\r' in value:
        raise ValueError('cancel_file contains a line break')
    if os.path.normpath(value) != value:
        raise ValueError('cancel_file must be a normalized path')
    path = os.path.abspath(value)
    if path.startswith(os.sep + os.sep):
        raise ValueError('cancel_file must use one absolute root')
    basename = os.path.basename(path)
    if basename in ('', '.', '..'):
        raise ValueError('cancel_file must have a safe basename')
    return path, os.path.dirname(path), basename


def _directory_open_flags():
    flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY'):
        flags |= os.O_DIRECTORY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    if hasattr(os, 'O_CLOEXEC'):
        flags |= os.O_CLOEXEC
    return flags


def _open_absolute_directory_nofollow(path):
    """Open an absolute directory without following any path component."""
    if (not isinstance(path, str) or not os.path.isabs(path) or
            os.path.normpath(path) != path or path.startswith(os.sep + os.sep)):
        raise ImageProjectError('directory path is not normalized and absolute')
    flags = _directory_open_flags()
    descriptor = None
    try:
        descriptor = os.open(os.sep, flags)
        for component in path.split(os.sep):
            if not component:
                continue
            if component in ('.', '..'):
                raise ImageProjectError('directory path contains traversal')
            before = os.stat(
                component, dir_fd=descriptor, follow_symlinks=False)
            if (stat.S_ISLNK(before.st_mode) or
                    not stat.S_ISDIR(before.st_mode)):
                raise ImageProjectError(
                    'directory path has a non-directory component')
            next_descriptor = None
            try:
                next_descriptor = os.open(
                    component, flags, dir_fd=descriptor)
                opened = os.fstat(next_descriptor)
                if (not stat.S_ISDIR(opened.st_mode) or
                        _identity(opened) != _identity(before)):
                    raise ImageProjectError(
                        'directory path changed while opening')
            except Exception:
                if next_descriptor is not None:
                    os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except ImageProjectError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise ImageProjectError(
            'cannot securely open directory: {}'.format(error))
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _validated_parent_identity(value):
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError('expected_parent_identity must contain device/inode')
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(
                'expected_parent_identity must contain nonnegative integers')
        result.append(int(item))
    return tuple(result)


def _current_user_id():
    if not hasattr(os, 'geteuid'):
        raise ImageProjectError('cannot determine the current user identity')
    return int(os.geteuid())


def _validate_cancel_parent(descriptor, expected_identity=None):
    metadata = os.fstat(descriptor)
    if (not stat.S_ISDIR(metadata.st_mode) or
            stat.S_IMODE(metadata.st_mode) != 0o700 or
            metadata.st_uid != _current_user_id()):
        raise ImageProjectError(
            'cancel marker parent must be mode 0700 and current-user-owned')
    identity = _identity(metadata)
    if expected_identity is not None and identity != expected_identity:
        raise ImageProjectError('cancel marker parent identity changed')
    return metadata


def _verify_cancel_parent_path(path, expected_identity):
    verification_descriptor = _open_absolute_directory_nofollow(path)
    try:
        _validate_cancel_parent(
            verification_descriptor, expected_identity=expected_identity)
    finally:
        os.close(verification_descriptor)


def _entry_metadata(descriptor, basename):
    try:
        return os.stat(
            basename, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ImageProjectError(
            'cannot inspect private directory entry: {}'.format(error))


def _validate_cancel_marker_metadata(metadata):
    if (metadata is None or stat.S_ISLNK(metadata.st_mode) or
            not stat.S_ISREG(metadata.st_mode) or
            stat.S_IMODE(metadata.st_mode) != 0o600 or
            metadata.st_uid != _current_user_id()):
        raise ImageProjectError(
            'cancel marker must be a current-user-owned mode-0600 regular file')


def _validate_existing_cancel_marker(parent_descriptor, basename):
    observed = _entry_metadata(parent_descriptor, basename)
    _validate_cancel_marker_metadata(observed)
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    if hasattr(os, 'O_NONBLOCK'):
        flags |= os.O_NONBLOCK
    if hasattr(os, 'O_CLOEXEC'):
        flags |= os.O_CLOEXEC
    marker_descriptor = None
    try:
        marker_descriptor = os.open(
            basename, flags, dir_fd=parent_descriptor)
        opened = os.fstat(marker_descriptor)
        _validate_cancel_marker_metadata(opened)
        if _identity(opened) != _identity(observed):
            raise ImageProjectError('cancel marker identity changed')
        os.fsync(marker_descriptor)
        final_metadata = _entry_metadata(parent_descriptor, basename)
        _validate_cancel_marker_metadata(final_metadata)
        if _identity(final_metadata) != _identity(opened):
            raise ImageProjectError('cancel marker identity changed')
    except ImageProjectError:
        raise
    except OSError as error:
        raise ImageProjectError(
            'cannot validate existing cancel marker: {}'.format(error))
    finally:
        if marker_descriptor is not None:
            os.close(marker_descriptor)


def build_session_inventory_command(output_path, changes_dir=None, euid=None,
                                    resolver=None, cancel_file=None):
    """Build the narrow savechanges inventory command without executing it."""
    output_path = os.path.abspath(_path_string(
        output_path, 'inventory output path'))
    if '\n' in output_path or '\r' in output_path:
        raise ValueError('inventory output path contains a newline')
    output_parent = os.path.dirname(output_path) or os.curdir
    if not _is_real_directory(output_parent):
        raise ImageProjectError(
            'inventory output parent must be a real directory')
    cancel_path = None
    if cancel_file is None:
        if os.path.lexists(output_path):
            raise ImageProjectError('inventory output already exists')
    else:
        cancel_path, cancel_parent, cancel_basename = (
            _strict_cancel_file_path(cancel_file))
        if cancel_parent != output_parent:
            raise ImageProjectError(
                'cancel marker must share the inventory output parent')
        output_basename = os.path.basename(output_path)
        if cancel_basename == output_basename:
            raise ImageProjectError(
                'cancel marker must differ from the inventory output')
        parent_descriptor = _open_absolute_directory_nofollow(output_parent)
        try:
            parent_metadata = _validate_cancel_parent(parent_descriptor)
            parent_identity = _identity(parent_metadata)
            _verify_cancel_parent_path(output_parent, parent_identity)
            if _entry_metadata(parent_descriptor, output_basename) is not None:
                raise ImageProjectError('inventory output already exists')
            if _entry_metadata(parent_descriptor, cancel_basename) is not None:
                raise ImageProjectError('cancel marker already exists')
            _validate_cancel_parent(
                parent_descriptor, expected_identity=parent_identity)
            _verify_cancel_parent_path(output_parent, parent_identity)
        finally:
            os.close(parent_descriptor)
    savechanges = _resolve_trusted_contract_executable(
        '/usr/bin/savechanges', resolver)
    command = [savechanges, '--inventory-json', output_path]
    if cancel_path is not None:
        command.extend(('--cancel-file', cancel_path))
    if changes_dir is not None:
        changes_dir = os.path.abspath(_path_string(
            changes_dir, 'changes directory'))
        if '\n' in changes_dir or '\r' in changes_dir:
            raise ValueError('changes directory contains a newline')
        if not _is_real_directory(changes_dir):
            raise ImageProjectError('changes directory must be a real directory')
        command.append(changes_dir)
    if euid is None:
        euid = os.geteuid() if hasattr(os, 'geteuid') else 1
    if isinstance(euid, bool) or not isinstance(euid, int) or euid < 0:
        raise ValueError('euid must be a nonnegative integer')
    if euid != 0:
        pkexec = _resolve_trusted_contract_executable(
            '/usr/bin/pkexec', resolver)
        command.insert(0, pkexec)
    return tuple(command)


def request_session_inventory_cancel(cancel_file,
                                     expected_parent_identity=None):
    """Atomically create a cancellation marker in its retained private parent."""
    cancel_path, parent_path, basename = _strict_cancel_file_path(cancel_file)
    expected_identity = _validated_parent_identity(expected_parent_identity)
    parent_descriptor = _open_absolute_directory_nofollow(parent_path)
    marker_descriptor = None
    try:
        parent_metadata = _validate_cancel_parent(
            parent_descriptor, expected_identity=expected_identity)
        parent_identity = _identity(parent_metadata)
        _verify_cancel_parent_path(parent_path, parent_identity)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
        try:
            marker_descriptor = os.open(
                basename, flags, 0o600, dir_fd=parent_descriptor)
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise ImageProjectError(
                    'cannot create cancel marker: {}'.format(error))
            _validate_existing_cancel_marker(parent_descriptor, basename)
        else:
            try:
                os.fchmod(marker_descriptor, 0o600)
                created = os.fstat(marker_descriptor)
                _validate_cancel_marker_metadata(created)
                os.fsync(marker_descriptor)
                current = _entry_metadata(parent_descriptor, basename)
                _validate_cancel_marker_metadata(current)
                if _identity(current) != _identity(created):
                    raise ImageProjectError('cancel marker identity changed')
            finally:
                descriptor_to_close = marker_descriptor
                marker_descriptor = None
                os.close(descriptor_to_close)
        os.fsync(parent_descriptor)
        _validate_cancel_parent(
            parent_descriptor, expected_identity=parent_identity)
        _verify_cancel_parent_path(parent_path, parent_identity)
        return True
    except ImageProjectError:
        raise
    except OSError as error:
        raise ImageProjectError(
            'cannot request session inventory cancellation: {}'.format(error))
    finally:
        if marker_descriptor is not None:
            os.close(marker_descriptor)
        os.close(parent_descriptor)


def probe_required_tools(resolver=None, runner=None, capture_requested=False,
                         euid=None, overlay_requested=False):
    """Resolve external tools, capture privilege, and the fixed backend path.

    The same-source composition backend is guaranteed by an exact package
    dependency, so it is resolved at a fixed path and never version, option, or
    basename probed. External helper tools are still discovered and version
    reported, and privileged capture tooling is checked only when capture is
    requested.
    """
    trusted_resolver = resolver
    resolver = resolver or shutil.which
    tools = {}
    tool_names = list(REQUIRED_TOOL_NAMES)
    if overlay_requested:
        tool_names.append('mksquashfs')
    for name in tool_names:
        path = resolver(name)
        entry = {'available': bool(path), 'path': path, 'version': None}
        if path:
            try:
                returncode, stdout, stderr = _run_command(
                    runner, _tool_version_command(name, path))
                text = (stdout + '\n' + stderr).strip()
                if text:
                    entry['version'] = text.splitlines()[0][:512]
                entry['version_probe_returncode'] = returncode
            except OSError:
                entry['available'] = False
        tools[name] = entry
    tools[COMPOSE_BACKEND_NAME] = _resolve_compose_backend_entry(
        trusted_resolver)
    capture_privilege = {
        'requested': bool(capture_requested),
        'euid': euid,
        'available': True,
        'pkexec': None,
    }
    if capture_requested:
        try:
            savechanges_path = _resolve_trusted_contract_executable(
                '/usr/bin/savechanges', trusted_resolver)
        except ImageProjectError:
            savechanges_path = None
        savechanges_entry = {
            'available': bool(savechanges_path),
            'path': savechanges_path,
            'version': None,
        }
        if savechanges_path:
            try:
                returncode, stdout, stderr = _run_command(
                    runner, [savechanges_path, '--version'])
                text = (stdout + '\n' + stderr).strip()
                if text:
                    savechanges_entry['version'] = text.splitlines()[0][:512]
                savechanges_entry['version_probe_returncode'] = returncode
                match = re.fullmatch(
                    r'savechanges ([0-9]+)\.([0-9]+)\.([0-9]+)',
                    savechanges_entry['version'] or '')
                if (returncode != 0 or match is None or
                        tuple(int(value) for value in match.groups()) <
                        SAVECHANGES_MIN_VERSION):
                    savechanges_entry['available'] = False
            except OSError:
                savechanges_entry['available'] = False
        tools['savechanges'] = savechanges_entry
        if euid is None:
            euid = os.geteuid() if hasattr(os, 'geteuid') else 1
            capture_privilege['euid'] = euid
        if isinstance(euid, bool) or not isinstance(euid, int) or euid < 0:
            raise ValueError('euid must be a nonnegative integer')
        if euid != 0:
            try:
                pkexec = _resolve_trusted_contract_executable(
                    '/usr/bin/pkexec', trusted_resolver)
            except ImageProjectError:
                pkexec = None
            capture_privilege['pkexec'] = pkexec
            capture_privilege['available'] = bool(pkexec)
    return {
        'tools': tools,
        'capture_privilege': capture_privilege,
    }


def validate_squashfs(path, runner=None, unsquashfs='unsquashfs'):
    """Validate a module using ``unsquashfs -s``."""
    try:
        returncode, stdout, stderr = _run_command(
            runner, [unsquashfs, '-s', path])
    except OSError as error:
        return False, str(error)
    detail = (stderr or stdout).strip()
    return returncode == 0, detail


def grep_ere_validate(pattern, relative_paths, grep='grep', runner=None):
    """Validate and evaluate one POSIX ERE without changing its grouping."""
    if any('\n' in path or '\r' in path for path in relative_paths):
        return False, tuple(), 'source paths contain newlines'
    payload = ''.join('{}\n'.format(path) for path in relative_paths)
    try:
        returncode, stdout, stderr = _run_command(
            runner, [grep, '-E', '-n', '--', pattern], input_data=payload)
    except (OSError, TypeError) as error:
        return False, tuple(), str(error)
    if returncode == 2:
        return False, tuple(), (stderr or stdout).strip()
    if returncode not in (0, 1):
        return False, tuple(), 'grep returned {}'.format(returncode)
    matched = []
    for line in stdout.splitlines():
        match = re.match(r'^(\d+):', line)
        if not match:
            continue
        index = int(match.group(1)) - 1
        if 0 <= index < len(relative_paths):
            matched.append(relative_paths[index])
    return True, tuple(matched), ''


def _disk_free_bytes(directory, disk_usage_func):
    usage = disk_usage_func(directory)
    if hasattr(usage, 'free'):
        return int(usage.free)
    if isinstance(usage, dict) and 'free' in usage:
        return int(usage['free'])
    if isinstance(usage, (tuple, list)) and len(usage) >= 3:
        return int(usage[2])
    raise ValueError('disk usage result has no free value')


def _read_mount_table(mounts_path):
    """Return (mountpoint, fstype, device) rows from a /proc/mounts-style file.

    Missing or unreadable tables yield an empty list so the planner degrades to
    an ``unknown`` classification rather than failing the build.
    """
    rows = []
    try:
        with open(mounts_path, 'r') as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 3:
                    continue
                device = fields[0]
                mountpoint = fields[1].replace('\\040', ' ').replace(
                    '\\011', '\t')
                fstype = fields[2]
                rows.append((mountpoint, fstype, device))
    except (OSError, UnicodeDecodeError):
        return []
    return rows


def _read_mountinfo(mountinfo_path):
    rows = []
    try:
        with open(mountinfo_path, 'r') as handle:
            for line in handle:
                fields = line.split()
                try:
                    separator = fields.index('-')
                except ValueError:
                    continue
                if len(fields) < 6 or separator + 2 >= len(fields):
                    continue
                rows.append((
                    fields[2],
                    _decode_mount_field(fields[3]),
                    _decode_mount_field(fields[4]),
                    fields[separator + 1],
                    _decode_mount_field(','.join(fields[separator + 3:])),
                ))
    except (OSError, UnicodeDecodeError):
        return []
    return rows


def _physical_mount_location(path, mountinfo_path):
    target = os.path.realpath(path)
    best = None
    best_length = -1
    for device, mount_root, mountpoint, _fstype, _options in _read_mountinfo(
            mountinfo_path):
        normalized = os.path.normpath(mountpoint)
        prefix = os.sep if normalized == os.sep else normalized + os.sep
        candidate = target if target.endswith(os.sep) else target + os.sep
        if candidate != prefix and not candidate.startswith(prefix):
            continue
        if len(normalized) <= best_length:
            continue
        relative = os.path.relpath(target, normalized)
        physical = os.path.normpath(os.path.join(mount_root, relative))
        best = (device, physical)
        best_length = len(normalized)
    return best


def _mountinfo_entry(path, mountinfo_path):
    target = os.path.realpath(path)
    best = None
    best_length = -1
    for entry in _read_mountinfo(mountinfo_path):
        normalized = os.path.normpath(entry[2])
        prefix = os.sep if normalized == os.sep else normalized + os.sep
        candidate = target if target.endswith(os.sep) else target + os.sep
        if candidate != prefix and not candidate.startswith(prefix):
            continue
        if len(normalized) > best_length:
            best = entry
            best_length = len(normalized)
    return best


def _uses_captured_union_storage(path, effective_changes_root,
                                 mountinfo_path):
    entry = _mountinfo_entry(path, mountinfo_path)
    root_entry = _mountinfo_entry(os.sep, mountinfo_path)
    if entry is None or root_entry is None:
        return False
    root_union = root_entry[3] in ('overlay', 'aufs')
    if root_union and entry[0] == root_entry[0]:
        return True
    if entry[3] != 'overlay' or not effective_changes_root:
        return False
    for option in entry[4].split(','):
        if not option.startswith('upperdir='):
            continue
        upper = option.split('=', 1)[1]
        if (_is_within(upper, effective_changes_root) or
                _physically_within(
                    upper, effective_changes_root, mountinfo_path)):
            return True
    return False


def _physically_within(path, directory, mountinfo_path):
    candidate = _physical_mount_location(path, mountinfo_path)
    root = _physical_mount_location(directory, mountinfo_path)
    if candidate is None or root is None or candidate[0] != root[0]:
        return False
    try:
        return os.path.commonpath((candidate[1], root[1])) == root[1]
    except (AttributeError, ValueError):
        prefix = root[1].rstrip(os.sep) + os.sep
        return candidate[1] == root[1] or candidate[1].startswith(prefix)


def _classify_directory_filesystem(path, mounts_path):
    """Classify the backing filesystem of ``path`` for the resource planner."""
    rows = _read_mount_table(mounts_path)
    if not rows:
        return FILESYSTEM_CLASS_UNKNOWN
    try:
        target = os.path.realpath(path)
    except OSError:
        target = os.path.abspath(path)
    best = None
    best_length = -1
    for mountpoint, fstype, device in rows:
        normalized = os.path.normpath(mountpoint)
        if normalized == os.sep:
            prefix = os.sep
        else:
            prefix = normalized + os.sep
        candidate = target if target.endswith(os.sep) else target + os.sep
        if candidate == prefix or candidate.startswith(prefix):
            if len(normalized) > best_length:
                best = (normalized, fstype, device)
                best_length = len(normalized)
    if best is None:
        return FILESYSTEM_CLASS_UNKNOWN
    mountpoint, fstype, device = best
    if fstype in RAM_BACKED_FSTYPES:
        return FILESYSTEM_CLASS_RAM_BACKED
    if mountpoint == os.sep and fstype in ('overlay', 'aufs'):
        return FILESYSTEM_CLASS_LIVE_OVERLAY
    if fstype in _REMOVABLE_FSTYPES or device.startswith('/dev/sr'):
        return FILESYSTEM_CLASS_REMOVABLE
    if fstype in _PERSISTENT_FSTYPES:
        return FILESYSTEM_CLASS_PERSISTENT
    return FILESYSTEM_CLASS_UNKNOWN


def _effective_live_changes_root(changes_roots=None):
    """Mirror savechanges' default writable-layer selection."""
    candidates = (DEFAULT_LIVE_CHANGES_ROOTS if changes_roots is None
                  else tuple(changes_roots))
    for candidate in candidates:
        if not os.path.isdir(candidate):
            continue
        root = candidate
        if (os.path.isdir(os.path.join(candidate, 'changes')) and
                os.path.isdir(os.path.join(candidate, 'workdir'))):
            root = os.path.join(candidate, 'changes')
        return os.path.realpath(root)
    return None


def _available_memory_bytes(meminfo_path):
    """Return MemAvailable in bytes from a /proc/meminfo-style file, or None."""
    try:
        with open(meminfo_path, 'r') as handle:
            for line in handle:
                if line.startswith('MemAvailable:'):
                    fields = line.split()
                    if len(fields) >= 2 and fields[1].isdigit():
                        unit = fields[2].lower() if len(fields) >= 3 else 'kb'
                        scale = 1024 if unit in ('kb', 'kib') else 1
                        return int(fields[1]) * scale
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return None


def resolve_device_mountpoint(device, mounts_path='/proc/mounts'):
    """Return the canonical mountpoint for a block device, or None.

    The device is matched by canonical path against the mount table so that
    human-readable udisks output is never parsed as state. The returned path is
    absolute and normalized.
    """
    if not device:
        return None
    try:
        target = os.path.realpath(device)
    except OSError:
        target = device
    for mountpoint, _fstype, dev in _read_mount_table(mounts_path):
        try:
            candidate = os.path.realpath(dev)
        except OSError:
            candidate = dev
        if candidate == target or dev == device:
            normalized = os.path.normpath(mountpoint)
            if os.path.isabs(normalized) and normalized != os.sep + os.sep:
                return normalized
    return None


def find_loop_backing_device(backing_file, sys_block_root='/sys/class/block'):
    """Return the ``/dev/loopN`` whose backing file is ``backing_file``.

    Loop devices are matched by their kernel-reported backing file so the
    frontend never parses ``udisksctl loop-setup`` output. Returns None when no
    loop device currently backs the file.
    """
    if not backing_file:
        return None
    try:
        target = os.path.realpath(backing_file)
    except OSError:
        target = backing_file
    try:
        names = sorted(os.listdir(sys_block_root))
    except OSError:
        return None
    for name in names:
        if not name.startswith('loop'):
            continue
        backing_path = os.path.join(
            sys_block_root, name, 'loop', 'backing_file')
        try:
            with open(backing_path, 'r') as handle:
                value = handle.read().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if value.endswith(' (deleted)'):
            value = value[:-len(' (deleted)')]
        try:
            resolved = os.path.realpath(value)
        except OSError:
            resolved = value
        if resolved == target or value == backing_file:
            return os.path.join('/dev', name)
    return None


def _scan_sensitive_config_keys(path):
    count = 0
    try:
        if os.path.getsize(path) > 16 * 1024 * 1024:
            raise ImageProjectError('configuration file is unexpectedly large')
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                stripped = line.lstrip()
                if not stripped or stripped.startswith('#') or '=' not in stripped:
                    continue
                key = stripped.split('=', 1)[0].strip()
                if (_SENSITIVE_CONFIG_KEY_RE.search(key) and
                        not _HASHED_CONFIG_KEY_RE.search(key)):
                    count += 1
    except OSError as error:
        raise ImageProjectError(
            'Cannot inspect configuration: {}'.format(error))
    return count


def _scan_sensitive_config_payload(payload):
    try:
        text = payload.decode('utf-8', 'strict')
    except UnicodeError as error:
        raise ImageProjectError(
            'Configuration is not valid UTF-8: {}'.format(error))
    count = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            continue
        key = stripped.split('=', 1)[0].strip()
        if (_SENSITIVE_CONFIG_KEY_RE.search(key) and
                not _HASHED_CONFIG_KEY_RE.search(key)):
            count += 1
    return count


def _validate_volume_label(label):
    if label is None:
        return True
    return (1 <= len(label) <= 32 and
            all(32 <= ord(character) < 127 for character in label))


def _existing_regular_identity(path):
    file_stat = os.lstat(path)
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ImageProjectError(
            'Path must be a non-symlink regular file: {}'.format(path))
    digest, opened_stat = _secure_hash_regular(path, file_stat)
    return {
        'device': int(opened_stat.st_dev),
        'inode': int(opened_stat.st_ino),
        'size': int(opened_stat.st_size),
        'mtime_ns': _stat_mtime_ns(opened_stat),
        'sha256': digest,
    }


def _allocate_job_directory(output_directory):
    job_directory = tempfile.mkdtemp(
        prefix='.minios-image-builder-', dir=output_directory)
    descriptor = None
    try:
        os.chmod(job_directory, 0o700)
        file_stat = os.lstat(job_directory)
        if (stat.S_ISLNK(file_stat.st_mode) or
                not stat.S_ISDIR(file_stat.st_mode) or
                stat.S_IMODE(file_stat.st_mode) != 0o700):
            raise ImageProjectError(
                'Failed to allocate a private job directory')
        if hasattr(os, 'geteuid') and file_stat.st_uid != os.geteuid():
            raise ImageProjectError(
                'Private job directory has an unexpected owner')
        descriptor = os.open(job_directory, _directory_open_flags())
        opened = os.fstat(descriptor)
        if (_identity(opened) != _identity(file_stat) or
                stat.S_IMODE(opened.st_mode) != 0o700):
            raise ImageProjectError(
                'Private job directory changed while opening')
        return job_directory, opened, descriptor
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.rmdir(job_directory)
        except OSError:
            pass
        raise


def _session_selection_payload(include_paths, exclude_paths):
    document = {
        'product_kind': SESSION_SELECTION_KIND,
        'schema_version': SESSION_SELECTION_SCHEMA_VERSION,
        'include_paths': list(include_paths),
        'exclude_paths': list(exclude_paths),
    }
    text = json.dumps(
        document, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(',', ':')) + '\n'
    payload = text.encode('utf-8', 'strict')
    return payload, hashlib.sha256(payload).hexdigest()


def _base_module_fingerprint(modules):
    digest = hashlib.sha256()
    digest.update(b'minios-base-modules-v2\x00')
    records = []
    for item in modules:
        if (isinstance(item.size, bool) or not isinstance(item.size, int) or
                item.size <= 0 or not _is_sha256(item.sha256)):
            raise ValueError('source module metadata is incomplete')
        order = re.match(r'^([0-9]+)', item.basename)
        records.append((int(order.group(1)) if order else 0,
                        item.basename, item.size, item.sha256))
    records.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _order, name, size, module_digest in records:
        digest.update(name.encode('ascii', 'strict'))
        digest.update(b'\x00')
        digest.update(str(size).encode('ascii'))
        digest.update(b'\x00')
        digest.update(module_digest.encode('ascii'))
        digest.update(b'\x00')
    return digest.hexdigest()


def _is_compose_source_module(module):
    relative = (module.relative_path or '').replace(os.sep, '/')
    return relative.endswith('.sb') and (
        '/' not in relative or relative.startswith('modules/'))


def _dynamic_module_orders(module_basenames, overlay_requested=False,
                           capture_requested=False):
    maximum = 0
    for basename in module_basenames:
        basename = os.path.basename(basename)
        match = re.match(r'^([0-9]+)', basename)
        if not match:
            continue
        digits = match.group(1)
        if len(digits) > 6:
            raise ValueError('numeric module order exceeds six digits')
        order = int(digits)
        maximum = max(maximum, order)
    overlay = (None, None)
    capture = (None, None)
    if overlay_requested:
        if maximum > 999998:
            raise ValueError('no safe image overlay order remains')
        order = maximum + 1
        overlay = (
            order, 'minios/{:02d}-image-overlay.sb'.format(order))
        maximum = order
    if capture_requested:
        if maximum > 999998:
            raise ValueError('no safe last-loaded capture order remains')
        order = maximum + 1
        capture = (
            order, 'minios/{:02d}-session-changes.sb'.format(order))
    return overlay, capture


def _capture_order_and_target(module_basenames, overlay_requested=False):
    unused_overlay, capture = _dynamic_module_orders(
        module_basenames, overlay_requested=overlay_requested,
        capture_requested=True)
    return capture


def _overlay_order_and_target(module_basenames):
    overlay, unused_capture = _dynamic_module_orders(
        module_basenames, overlay_requested=True)
    return overlay


def _session_path_at_or_below(path, parent):
    return parent == '' or path == parent or path.startswith(parent + '/')


def _session_paths_related(left, right):
    return (_session_path_at_or_below(left, right) or
            _session_path_at_or_below(right, left))


def _inventory_whiteout_target(entry, union_backend):
    if entry.type != 'whiteout' or union_backend != 'aufs':
        return entry.path
    parent, separator, basename = entry.path.rpartition('/')
    if basename == '.wh..wh..opq':
        return parent
    if basename.startswith('.wh.'):
        target = basename[4:]
        return parent + separator + target if parent else target
    return entry.path


def _estimate_session_capture(inventory, mode, includes, excludes):
    selected = []
    matched_includes = dict((path, False) for path in includes)
    for entry in inventory.entries:
        include = False
        if mode == 'exact':
            include = entry.default_exact
        elif mode == 'clean':
            include = entry.default_clean
        elif mode == 'selected':
            whiteout_target = _inventory_whiteout_target(
                entry, inventory.union_backend)
            for path in includes:
                if (_session_path_at_or_below(entry.path, path) or
                        (entry.type == 'directory' and
                         _session_path_at_or_below(path, entry.path)) or
                        (entry.type == 'whiteout' and
                         _session_paths_related(whiteout_target, path))):
                    matched_includes[path] = True
                    include = entry.default_exact
            if any(
                    _session_path_at_or_below(entry.path, path) or
                    (entry.type == 'whiteout' and
                     _session_paths_related(whiteout_target, path))
                    for path in excludes):
                include = False
        if include:
            selected.append(entry)
    unmatched = tuple(path for path, matched in matched_includes.items()
                      if not matched)
    unknown_size = any(item.type == 'regular' and item.size is None
                       for item in selected)
    if unknown_size:
        return None, len(selected), unmatched
    regular_bytes = sum(item.size or 0 for item in selected
                        if item.type == 'regular')
    estimate = regular_bytes + max(1024 * 1024, regular_bytes // 10)
    return estimate, len(selected), unmatched


class BuildPlan(_Immutable):
    """Attested preflight result for one private output job."""

    __slots__ = (
        'errors', 'warnings', 'estimated_input_bytes', 'argv', 'display_argv',
        'execution_cwd', 'output_path', 'scratch_directory',
        'partial_output_path', 'job_directory', 'adapter_manifest_path',
        'plan_id', '_manifest_json', '_manifest_payload', '_input_records',
        '_job_identity', '_job_descriptor', '_source_path',
        '_output_expectation', '_nonce', '_tool_capabilities',
        '_capture_selection_path', '_capture_selection_payload',
        '_capture_selection_digest', '_session_inventory',
        '_expected_base_module_fingerprint', '_capture_requested',
        '_live_config_path', '_live_config_payload', '_live_config_digest',
        '_customization_requested', '_adapter_customization_requested',
        '_overlay_directory', '_overlay_inventory', '_boot_config_payloads',
        '_scratch_identity',
    )

    def __init__(self, errors, warnings, estimated_input_bytes, argv,
                 output_path, partial_output_path, job_directory,
                 adapter_manifest_path, manifest, input_records,
                 job_identity, output_expectation, tool_capabilities,
                 capture_selection_path=None, capture_selection_payload=None,
                 capture_selection_digest=None, session_inventory=None,
                  expected_base_module_fingerprint=None,
                  capture_requested=False,
                  live_config_path=None, live_config_payload=None,
                  live_config_digest=None, customization_requested=False,
                  adapter_customization_requested=False,
                  overlay_directory=None, overlay_inventory=None,
                   boot_config_payloads=None,
                   job_descriptor=None, source_path=None, display_argv=None,
                   scratch_directory=None, scratch_identity=None,
                   _token=None):
        if _token is not _PLAN_TOKEN:
            raise TypeError('BuildPlan objects are created by preflight')
        self.errors = tuple(errors)
        self.warnings = tuple(warnings)
        self.estimated_input_bytes = int(estimated_input_bytes or 0)
        self.argv = tuple(argv)
        self.display_argv = tuple(
            display_argv if display_argv is not None else argv)
        self.execution_cwd = (
            '/proc/self/fd/{}'.format(job_descriptor)
            if job_descriptor is not None else None)
        self.output_path = output_path
        self.scratch_directory = scratch_directory
        self.partial_output_path = partial_output_path
        self.job_directory = job_directory
        self.adapter_manifest_path = adapter_manifest_path
        self.plan_id = manifest['plan_id']
        self._manifest_payload = _canonical_json_bytes(manifest)
        self._manifest_json = self._manifest_payload.decode('ascii')
        self._input_records = tuple(_freeze(dict(item))
                                    for item in input_records)
        self._job_identity = tuple(job_identity) if job_identity else None
        self._job_descriptor = job_descriptor
        self._source_path = source_path
        self._output_expectation = _freeze(output_expectation)
        self._nonce = object()
        self._tool_capabilities = _freeze(tool_capabilities)
        self._capture_selection_path = capture_selection_path
        self._capture_selection_payload = capture_selection_payload
        self._capture_selection_digest = capture_selection_digest
        self._session_inventory = session_inventory
        self._expected_base_module_fingerprint = (
            expected_base_module_fingerprint)
        self._capture_requested = bool(capture_requested)
        self._live_config_path = live_config_path
        self._live_config_payload = live_config_payload
        self._live_config_digest = live_config_digest
        self._customization_requested = bool(customization_requested)
        self._adapter_customization_requested = bool(
            adapter_customization_requested)
        self._overlay_directory = overlay_directory
        self._overlay_inventory = _freeze(overlay_inventory)
        self._boot_config_payloads = _freeze(dict(
            boot_config_payloads or {}))
        self._scratch_identity = (
            tuple(scratch_identity) if scratch_identity else None)
        self._lock()

    def __del__(self):
        descriptor = getattr(self, '_job_descriptor', None)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except (AttributeError, OSError):
                pass

    @property
    def buildable(self):
        return not self.errors and bool(self.argv)

    @property
    def blocking_errors(self):
        return self.errors

    @property
    def capture_requested(self):
        return self._capture_requested

    @property
    def customization_requested(self):
        return self._customization_requested

    @property
    def manifest(self):
        return json.loads(self._manifest_json)

    @property
    def manifest_payload(self):
        return self._manifest_payload

    def to_dict(self):
        return self.manifest


def _failed_plan(errors, warnings, estimated, output_path, manifest):
    manifest['buildable'] = False
    manifest['errors'] = [_public_diagnostic(item) for item in errors]
    manifest['warnings'] = [_public_diagnostic(item) for item in warnings]
    manifest['plan_id'] = _json_digest(manifest)
    return BuildPlan(
        errors, warnings, estimated, (), output_path, None, None, None,
        manifest, (), None, None, manifest.get('tools', {}),
        capture_requested=manifest.get('capture', {}).get('requested', False),
        customization_requested=manifest.get(
            'customization', {}).get('requested', False),
        adapter_customization_requested=manifest.get(
            'customization', {}).get('adapter_report_requested', False),
        _token=_PLAN_TOKEN)


def _tool_path(tool_capabilities, name):
    return tool_capabilities.get('tools', {}).get(name, {}).get('path')


def _public_module_record(module):
    value = module.to_dict()
    value.pop('path', None)
    value.pop('real_path', None)
    value.pop('link_target', None)
    return value


def _public_tool_capabilities(tool_capabilities):
    value = _thaw(_freeze(tool_capabilities))
    for name, entry in value.get('tools', {}).items():
        if isinstance(entry, dict) and entry.get('path'):
            entry['path'] = '<tool:{}>'.format(name)
    privilege = value.get('capture_privilege', {})
    if isinstance(privilege, dict) and privilege.get('pkexec'):
        privilege['pkexec'] = '<tool:pkexec>'
    return value


def _redacted_build_argv(argv, additional_paths=()):
    replacements = {
        '--source': '<running-minios-source>',
        '--config': '<live-config-input>',
        '--name': '<private-partial-output>',
        '--manifest': '<private-build-manifest>',
        '--kernel-args': '<redacted-kernel-arguments>',
        '--boot-menu-json': '<redacted-boot-menu-json>',
        '--boot-background': '<boot-background-input>',
        '--overlay-directory': '<overlay-directory-input>',
        '--capture-selection': '<private-capture-selection>',
    }
    additional_paths = frozenset(additional_paths)
    result = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if index == 0:
            result.append(os.path.basename(argument))
        elif argument in replacements and index + 1 < len(argv):
            result.extend((argument, replacements[argument]))
            index += 1
        elif argument in additional_paths:
            result.append('<additional-module-input>')
        else:
            result.append(argument)
        index += 1
    return tuple(result)


def _input_record(kind, path, digest, file_stat, relative_path=None,
                  target_path=None, record_type='file', link_target=None):
    return {
        'kind': kind,
        'path': path,
        'relative_path': relative_path,
        'target_path': target_path,
        'type': record_type,
        'link_target': link_target,
        'size': int(file_stat.st_size),
        'sha256': digest,
        'device': int(file_stat.st_dev),
        'inode': int(file_stat.st_ino),
        'mtime_ns': _stat_mtime_ns(file_stat),
    }


def _source_input_records(source_manifest):
    result = []
    for frozen in source_manifest:
        item = _thaw(frozen) if isinstance(frozen, MappingProxyType) else dict(frozen)
        result.append({
            'kind': 'source',
            'path': item['path'],
            'relative_path': item['relative_path'],
            'target_path': 'minios/' + item['relative_path'],
            'type': item['type'],
            'link_target': item['link_target'],
            'size': item['size'],
            'sha256': item['sha256'],
            'device': item['device'],
            'inode': item['inode'],
            'mtime_ns': None,
        })
    return result


def create_build_plan(project, source_info=None,
                      current_config_path='/etc/live/config.conf',
                      current_config_payload=None,
                      disk_usage_func=None, scratch_directory=None,
                      tool_capabilities=None, resolver=None,
                      command_runner=None, regex_validator=None,
                      regex_runner=None,
                       session_inventory=None, euid=None,
                       grep='grep', roots=None, mounts_path='/proc/mounts',
                       sys_block_root='/sys/class/block',
                       meminfo_path='/proc/meminfo', changes_roots=None,
                       mountinfo_path='/proc/self/mountinfo'):
    """Resolve, hash, validate, and allocate one secure minios-image-compose build job."""
    if not isinstance(project, ImageProject):
        raise TypeError('project must be an ImageProject')
    errors = []
    warnings = []
    disk_usage_func = disk_usage_func or shutil.disk_usage
    scratch_directory = os.path.abspath(
        scratch_directory or tempfile.gettempdir())
    config_path = _resolve_path(
        current_config_path, project.project_base, 'current_config_path')
    if source_info is None:
        discovery_roots = roots or ((project.source_backend,
                                     project.source_root_path),)
        discovery_subdirectories = (
            ('',) if project.source_backend in MOUNTED_SOURCE_BACKENDS
            else None)
        source_info = discover_running_source(
            roots=discovery_roots, mounts_path=mounts_path,
            sys_block_root=sys_block_root,
            subdirectories=discovery_subdirectories)
    if not isinstance(source_info, SourceInfo):
        raise TypeError('source_info must be a SourceInfo')
    output_path = os.path.abspath(project.output_path)
    output_directory = os.path.dirname(output_path) or os.curdir
    capture_requested = project.capture_mode in SESSION_CAPTURE_MODES
    boot_customization_requested = (
        project.boot_timeout is not None or project.default_boot is not None or
        project.boot_menu_entries is not None or project.kernel_args is not None)
    background_requested = project.boot_background_path is not None
    overlay_requested = project.overlay_directory is not None
    adapter_customization_requested = (
        boot_customization_requested or background_requested or
        overlay_requested)
    customization_requested = project.customization_requested
    kernel_argument_summary = None
    if project.kernel_args is not None:
        kernel_bytes, kernel_digest = validate_kernel_arguments(
            project.kernel_args)
        kernel_argument_summary = {
            'bytes': kernel_bytes, 'sha256': kernel_digest,
        }

    if tool_capabilities is None:
        tool_capabilities = probe_required_tools(
            resolver=resolver, runner=command_runner,
            capture_requested=capture_requested, euid=euid,
            overlay_requested=overlay_requested)
    tool_capabilities = _thaw(_freeze(tool_capabilities))
    for name in REQUIRED_TOOL_NAMES:
        entry = tool_capabilities.get('tools', {}).get(name, {})
        if not entry.get('available') or not entry.get('path'):
            _add_diagnostic(
                errors, 'error', 'required_tool_missing',
                'Required tool is unavailable: {}'.format(name), name)
    if overlay_requested:
        mksquashfs_entry = tool_capabilities.get('tools', {}).get(
            'mksquashfs', {})
        if (not mksquashfs_entry.get('available') or
                not mksquashfs_entry.get('path')):
            _add_diagnostic(
                errors, 'error', 'overlay_mksquashfs_unavailable',
                'Image overlays require mksquashfs.', 'mksquashfs')
    compose_entry = tool_capabilities.get('tools', {}).get(
        COMPOSE_BACKEND_NAME, {})
    compose_backend_path = compose_entry.get('path')
    if not compose_entry.get('available') or not compose_backend_path:
        _add_diagnostic(
            errors, 'error', 'compose_backend_missing',
            'The minios-image-compose backend is not installed; reinstall the '
            'minios-image-compose package.', COMPOSE_BACKEND_NAME)
    if capture_requested:
        savechanges_entry = tool_capabilities.get('tools', {}).get(
            'savechanges', {})
        if (not savechanges_entry.get('available') or
                not savechanges_entry.get('path')):
            _add_diagnostic(
                errors, 'error', 'capture_savechanges_unavailable',
                'Session capture requires trusted /usr/bin/savechanges.')
        privilege = tool_capabilities.get('capture_privilege', {})
        if not privilege.get('available'):
            _add_diagnostic(
                errors, 'error', 'capture_privilege_unavailable',
                'Session capture requires root or trusted /usr/bin/pkexec.')
    if not source_info.supported:
        _add_diagnostic(
            errors, 'error', 'source_not_supported',
            'A supported MiniOS source is required.')
    if source_info.source_path and not _same_path(
            project.source_path, source_info.source_path):
        _add_diagnostic(
            errors, 'error', 'source_path_changed',
            'Project and discovered source paths differ.', source_info.source_path)
    if source_info.root_path and not _same_path(
            project.source_root_path, source_info.root_path):
        _add_diagnostic(
            errors, 'error', 'source_root_changed',
            'Project and discovered live roots differ.', source_info.root_path)
    if project.source_fingerprint_algorithm != SOURCE_FINGERPRINT_ALGORITHM:
        _add_diagnostic(
            errors, 'error', 'source_fingerprint_algorithm_changed',
            'Project uses an unsupported source fingerprint algorithm.')

    current_source_manifest = ()
    current_fingerprint = None
    current_total_bytes = 0
    if source_info.source_path and os.path.isdir(source_info.source_path):
        try:
            current_fingerprint, current_total_bytes, current_source_manifest = (
                _build_source_manifest(source_info.source_path))
        except (OSError, SourceInspectionError) as error:
            _add_diagnostic(
                errors, 'error', 'source_fingerprint_failed', str(error),
                source_info.source_path)
    if current_fingerprint is not None:
        if source_info.fingerprint != current_fingerprint:
            _add_diagnostic(
                errors, 'error', 'source_changed_since_inspection',
                'Source content changed after discovery.', source_info.source_path)
        if project.source_fingerprint != current_fingerprint:
            _add_diagnostic(
                errors, 'error', 'source_drift',
                'Source content no longer matches the project fingerprint.',
                source_info.source_path)

    if (project.capture_mode == 'exact' and
            not project.sensitive_capture_acknowledged):
        _add_diagnostic(
            errors, 'error', 'sensitive_capture_acknowledgement_required',
            'Exact session capture requires explicit sensitive-data '
            'acknowledgement.')
    if project.capture_mode == 'clean':
        _add_diagnostic(
            warnings, 'warning', 'clean_capture_allowlist',
            'Clean capture is allowlist-based and intentionally omits broad '
            'system state, user data, identity, logs, and caches.')
    if session_inventory is not None and not isinstance(
            session_inventory, SessionInventory):
        _add_diagnostic(
            errors, 'error', 'invalid_session_inventory',
            'session_inventory must be a validated SessionInventory.')
        session_inventory = None
    if session_inventory is not None and not capture_requested:
        _add_diagnostic(
            errors, 'error', 'session_inventory_without_capture',
            'A session inventory is only valid for a requested capture.')
    if not project.include_current_config:
        _add_diagnostic(
            errors, 'error', 'adapter_requires_current_config',
            'The minios-image-compose contract requires a configuration file.')
    if not _validate_volume_label(project.volume_label):
        _add_diagnostic(
            errors, 'error', 'invalid_volume_label',
            'Volume label must contain 1-32 printable ASCII characters.')

    for diagnostic in source_info.diagnostics:
        if diagnostic.severity == 'error':
            errors.append(diagnostic)
        elif diagnostic.severity == 'warning':
            warnings.append(diagnostic)

    source_by_name = {}
    for module in source_info.modules:
        source_by_name.setdefault(module.basename, []).append(module)
    for basename, entries in source_by_name.items():
        if len(entries) > 1:
            _add_diagnostic(
                errors, 'error', 'ambiguous_source_module',
                'Module basename {} identifies multiple source paths.'.format(
                    basename), basename)
    selected_names = set(project.selected_source_modules)
    known_names = set(source_by_name)
    for name in sorted(selected_names - known_names):
        _add_diagnostic(
            errors, 'error', 'unknown_selected_module',
            'Selected source module is unavailable: {}'.format(name), name)
    selected_modules = []
    deselected_modules = []
    for module in source_info.modules:
        if module.basename in selected_names:
            if len(source_by_name[module.basename]) == 1:
                selected_modules.append(module)
        else:
            deselected_modules.append(module)
            if module.required:
                _add_diagnostic(
                    errors, 'error', 'required_module_deselected',
                    'Required module is deselected: {}'.format(module.basename),
                    module.path)
    if not selected_modules:
        _add_diagnostic(
            errors, 'error', 'no_source_modules_selected',
            'At least one source module must be selected.')
    else:
        selected_orders = [
            module.order_prefix for module in selected_modules
            if module.order_prefix is not None]
        if selected_orders:
            highest_selected = max(selected_orders)
            for module in deselected_modules:
                if (module.order_prefix is not None and
                        module.order_prefix < highest_selected):
                    _add_diagnostic(
                        errors, 'error', 'module_dependency_gap',
                        'Module {} is deselected but higher layers that '
                        'depend on it are still selected. Numbered layers '
                        'must form a gap-free stack.'.format(module.basename),
                        module.path)

    unsquashfs = _tool_path(tool_capabilities, 'unsquashfs') or 'unsquashfs'
    for module in selected_modules:
        if not _SAFE_MODULE_BASENAME_RE.match(module.basename):
            _add_diagnostic(
                errors, 'error', 'module_target_not_graft_safe',
                'Module basename is not safe for the adapter graft contract: '
                '{}'.format(module.basename), module.path)
        valid, detail = validate_squashfs(
            module.path, runner=command_runner, unsquashfs=unsquashfs)
        if not valid:
            _add_diagnostic(
                errors, 'error', 'selected_module_not_squashfs',
                'unsquashfs rejected source module {}{}.'.format(
                    module.basename, ': ' + detail if detail else ''),
                module.path)

    additional = []
    additional_records = []
    target_paths = {}
    for module in selected_modules:
        target_paths.setdefault(module.target_path, []).append(module.path)
    for path in project.additional_module_paths:
        basename = os.path.basename(path)
        target = compose_module_target(basename)
        entry = {'path': path, 'basename': basename, 'target_path': target,
                 'size': None, 'sha256': None}
        try:
            file_stat = os.lstat(path)
            if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
                raise ImageProjectError(
                    'Additional module must be a non-symlink regular file')
            digest, opened_stat = _secure_hash_regular(path, file_stat)
            entry['size'] = int(opened_stat.st_size)
            entry['sha256'] = digest
            additional_records.append(_input_record(
                'additional-module', path, digest, opened_stat,
                target_path=target))
        except (OSError, ImageProjectError, SourceInspectionError) as error:
            _add_diagnostic(
                errors, 'error', 'additional_module_invalid',
                'Invalid additional module {}: {}'.format(path, error), path)
            opened_stat = None
        if not _SAFE_MODULE_BASENAME_RE.match(basename):
            _add_diagnostic(
                errors, 'error', 'module_target_not_graft_safe',
                'Additional module basename is not graft-safe: {}'.format(
                    basename), path)
        if opened_stat is not None:
            valid, detail = validate_squashfs(
                path, runner=command_runner, unsquashfs=unsquashfs)
            if not valid:
                _add_diagnostic(
                    errors, 'error', 'additional_module_not_squashfs',
                    'unsquashfs rejected additional module {}{}.'.format(
                        basename, ': ' + detail if detail else ''), path)
        target_paths.setdefault(target, []).append(path)
        additional.append(entry)
    for target, paths in sorted(target_paths.items()):
        if len(paths) > 1:
            _add_diagnostic(
                errors, 'error', 'duplicate_module_target',
                'Multiple modules target {}: {}.'.format(
                    target, ', '.join(paths)), target)
    folded_targets = {}
    for target in target_paths:
        folded_targets.setdefault(target.lower(), set()).add(target)
    for targets in folded_targets.values():
        if len(targets) > 1:
            _add_diagnostic(
                errors, 'error', 'module_target_case_collision',
                'Module targets differ only by case: {}.'.format(
                    ', '.join(sorted(targets))))

    config_record = None
    rendered_config_payload = None
    rendered_config_digest = None
    rendered_config_size = None
    sensitive_count = 0
    try:
        if current_config_payload is None:
            config_payload, opened_config_stat = _read_stable_regular_bytes(
                config_path, MAX_LIVE_CONFIG_BYTES)
        else:
            if not isinstance(current_config_payload, bytes):
                raise TypeError('current_config_payload must be bytes')
            if len(current_config_payload) > MAX_LIVE_CONFIG_BYTES:
                raise ValueError('current configuration exceeds size limit')
            config_payload = current_config_payload
            opened_config_stat = None
        config_digest = hashlib.sha256(config_payload).hexdigest()
        if opened_config_stat is None:
            config_record = {
                'kind': 'config', 'path': None,
                'target_path': 'minios/config.conf',
                'type': 'file', 'link_target': None,
                'size': len(config_payload), 'sha256': config_digest,
                'device': None, 'inode': None, 'mtime_ns': None,
            }
        else:
            config_record = _input_record(
                'config', config_path, config_digest, opened_config_stat,
                target_path='minios/config.conf')
        if project.live_config_overrides or current_config_payload is not None:
            sensitive_count = _scan_sensitive_config_payload(config_payload)
            rendered_config_payload = render_live_config(
                config_payload, project.live_config_overrides)
            rendered_config_digest = hashlib.sha256(
                rendered_config_payload).hexdigest()
            rendered_config_size = len(rendered_config_payload)
        else:
            sensitive_count = _scan_sensitive_config_keys(config_path)
    except (OSError, ValueError, ImageProjectError,
            SourceInspectionError) as error:
        _add_diagnostic(
            errors, 'error', 'current_config_invalid',
            'Cannot use current configuration: {}'.format(error), config_path)
    if sensitive_count:
        _add_diagnostic(
            warnings, 'warning', 'sensitive_config_present',
            'Configuration contains {} plaintext setting(s) with a '
            'sensitive-looking key. Values are copied verbatim and are not '
            'interpreted, displayed, or logged by the builder.'.format(
                sensitive_count))

    background_record = None
    background_metadata_value = None
    if background_requested:
        try:
            background_payload, background_stat = _read_stable_regular_bytes(
                project.boot_background_path, MAX_LIVE_CONFIG_BYTES)
            background_metadata_value = _png_metadata_from_payload(
                background_payload)
            background_record = _input_record(
                'boot-background', project.boot_background_path,
                background_metadata_value['sha256'], background_stat)
        except (OSError, ValueError, ImageProjectError) as error:
            _add_diagnostic(
                errors, 'error', 'boot_background_invalid',
                'Boot background is not a supported stable PNG: {}'.format(
                    error))

    overlay_inventory_value = None
    if overlay_requested:
        overlay_path = project.overlay_directory
        project_base_real = os.path.realpath(project.project_base)
        overlay_real = os.path.realpath(overlay_path)
        if (overlay_real == project_base_real or
                not _is_within(overlay_path, project.project_base)):
            _add_diagnostic(
                errors, 'error', 'overlay_outside_project',
                'Overlay directory must be a child of the project directory.')
        if (_is_within(overlay_path, source_info.source_path or
                       project.source_path) or
                _is_within(source_info.source_path or project.source_path,
                           overlay_path)):
            _add_diagnostic(
                errors, 'error', 'overlay_overlaps_source',
                'Overlay directory and MiniOS source tree must not overlap.')
        try:
            overlay_inventory_value = _overlay_inventory(overlay_path)
        except (OSError, ImageProjectError) as error:
            _add_diagnostic(
                errors, 'error', 'overlay_directory_invalid',
                'Overlay directory cannot be inventoried safely: {}'.format(
                    error))
        else:
            if overlay_inventory_value['entry_count'] <= 1:
                _add_diagnostic(
                    warnings, 'warning', 'overlay_directory_empty',
                    'Overlay directory is empty; it will contribute no files '
                    'to the image.')

    boot = None
    required_boot_relative = []
    kernel_relative = []
    initramfs_relative = []
    menu_relative = []
    if source_info.source_path and os.path.isdir(source_info.source_path):
        try:
            boot = _boot_inventory(source_info.source_path)
        except SourceInspectionError as error:
            _add_diagnostic(
                errors, 'error', 'boot_inspection_failed', str(error),
                source_info.source_path)
    if boot is not None:
        if boot['bootloader'] == 'unknown':
            _add_diagnostic(
                errors, 'error', 'source_bootloader_unsupported',
                'Source has no supported BIOS bootloader layout.')
        if not boot['kernel_paths']:
            _add_diagnostic(errors, 'error', 'source_kernel_missing',
                            'Source has no kernel image.')
        if not boot['initramfs_paths']:
            _add_diagnostic(errors, 'error', 'source_initramfs_missing',
                            'Source has no initramfs image.')
        if not boot['version_coherent']:
            _add_diagnostic(
                errors, 'error', 'kernel_initramfs_version_mismatch',
                'No coherent vmlinuz/initramfs version pair was found.')
        required_boot = required_source_boot_files(
            source_info.source_path, boot['bootloader'])
        for path in required_boot:
            relative = os.path.relpath(
                path, source_info.source_path).replace(os.sep, '/')
            required_boot_relative.append(relative)
            if not os.path.isfile(path):
                _add_diagnostic(
                    errors, 'error', 'required_boot_file_missing',
                    'Required boot file is missing: {}'.format(relative), path)
        kernel_relative = [os.path.relpath(
            path, source_info.source_path).replace(os.sep, '/')
            for path in boot['kernel_paths']]
        initramfs_relative = [os.path.relpath(
            path, source_info.source_path).replace(os.sep, '/')
            for path in boot['initramfs_paths']]
        for path in _menu_source_paths(
                source_info.source_path, boot['bootloader'],
                project.menu_locale):
            relative = os.path.relpath(
                path, source_info.source_path).replace(os.sep, '/')
            menu_relative.append(relative)
            if not os.path.isfile(path):
                _add_diagnostic(
                    errors, 'error', 'menu_source_file_missing',
                    'Required menu source is missing: {}'.format(relative), path)

    kernel_modules = [item for item in selected_modules if item.role == 'kernel']
    if not kernel_modules:
        _add_diagnostic(
            errors, 'error', 'kernel_module_missing',
            'A selected kernel module is required.')
    runtime_arch = _normalized_architecture(
        source_info.metadata.get('architecture'))
    module_arches = set(filter(None, (
        _normalized_architecture(item.architecture)
        for item in selected_modules)))
    if len(module_arches) > 1:
        _add_diagnostic(
            errors, 'error', 'module_architecture_mismatch',
            'Selected source modules contain multiple architectures.')
    if runtime_arch and module_arches and runtime_arch not in module_arches:
        _add_diagnostic(
            errors, 'error', 'runtime_module_architecture_mismatch',
            'Module architecture does not match selected MiniOS metadata.')
    kernel_module_versions = set(filter(None, (
        item.kernel_version for item in kernel_modules)))
    if len(kernel_module_versions) > 1:
        _add_diagnostic(
            errors, 'error', 'kernel_module_version_conflict',
            'Selected kernel modules declare different kernel versions.')
    if boot is not None and kernel_module_versions:
        coherent_versions = set(boot['coherent_versions'])
        if coherent_versions:
            compatible = all(any(
                boot_version == module_version or
                boot_version.startswith(module_version + '.') or
                boot_version.startswith(module_version + '-')
                for module_version in kernel_module_versions)
                for boot_version in coherent_versions)
            if not compatible:
                _add_diagnostic(
                    errors, 'error', 'kernel_module_version_mismatch',
                    'Kernel module version does not match the coherent '
                    'vmlinuz/initramfs version.')
        else:
            _add_diagnostic(
                warnings, 'warning', 'kernel_module_version_unverifiable',
                'Kernel module declares a version, but boot files use only '
                'generic names.')

    capture_selection_payload = None
    capture_selection_digest = None
    if project.capture_mode == 'selected':
        capture_selection_payload, capture_selection_digest = (
            _session_selection_payload(
                project.capture_include_paths,
                project.capture_exclude_paths))
    expected_base_module_fingerprint = None
    overlay_order = None
    overlay_target = None
    capture_order = None
    capture_target = None
    capture_estimated_bytes = None
    capture_inventory_selected_count = None
    if capture_requested:
        capture_base_modules = [
            item for item in selected_modules
            if _is_compose_source_module(item)]
        if any(item.is_symlink for item in capture_base_modules):
            _add_diagnostic(
                errors, 'error', 'capture_source_module_symlink_unsupported',
                'Session base binding requires regular source module files.')
        if not capture_base_modules:
            _add_diagnostic(
                errors, 'error', 'capture_base_modules_missing',
                'Session capture requires at least one source module.')
        else:
            try:
                expected_base_module_fingerprint = _base_module_fingerprint(
                    capture_base_modules)
            except (UnicodeError, TypeError, ValueError) as error:
                _add_diagnostic(
                    errors, 'error', 'capture_base_fingerprint_failed',
                    'Cannot fingerprint effective base modules: {}'.format(
                        error))
        if session_inventory is None:
            _add_diagnostic(
                warnings, 'warning', 'capture_size_unknown',
                'No bound session inventory was supplied; capture size is '
                'unknown and is not included as a false estimate.')
        else:
            capture_estimated_bytes, capture_inventory_selected_count, unmatched = (
                _estimate_session_capture(
                    session_inventory, project.capture_mode,
                    project.capture_include_paths,
                    project.capture_exclude_paths))
            if unmatched:
                _add_diagnostic(
                    errors, 'error', 'capture_selection_unmatched',
                    'Selected include paths do not match the supplied session '
                    'inventory; unmatched count: {}.'.format(len(unmatched)))
            if capture_estimated_bytes is None:
                _add_diagnostic(
                    warnings, 'warning', 'capture_size_unknown',
                    'Selected inventory entries have unknown sizes; capture '
                    'size is not claimed.')

    source_records = _source_input_records(current_source_manifest)
    all_relative_paths = [item['relative_path'] for item in source_records]
    source_capture_paths = [
        item['relative_path'] for item in source_records
        if (item['relative_path'] == 'session-capture.json' or
            re.match(r'(^|/)[0-9]+-session-changes[.]sb$',
                     item['relative_path']))]
    additional_capture_paths = [
        item['target_path'] for item in additional
        if re.match(r'^minios/(?:modules/)?[0-9]+-session-changes[.]sb$',
                    item['target_path'])]
    if source_capture_paths:
        _add_diagnostic(
            warnings, 'warning', 'source_session_capture_artifact',
            'Source already contains saved session-change artifacts. They are '
            'treated as source content and do not block further customization.')
    if additional_capture_paths:
        _add_diagnostic(
            errors, 'error', 'reserved_session_capture_artifact',
            'Additional modules use names reserved for generated session '
            'capture layers; rename or remove them before building.')
    source_customization_paths = [
        item['relative_path'] for item in source_records
        if (item['relative_path'] == 'image-customization.json' or
            re.match(r'(^|/)[0-9]+-image-overlay[.]sb$',
                     item['relative_path']))]
    additional_customization_paths = [
        item['target_path'] for item in additional
        if re.match(
            r'^minios/(?:modules/)?[0-9]+-image-overlay[.]sb$',
            item['target_path'])]
    if source_customization_paths:
        _add_diagnostic(
            warnings, 'warning', 'source_image_customization_artifact',
            'Source contains artifacts from an earlier Image Builder '
            'customization. They are treated as source content and do not '
            'block further customization.')
    if additional_customization_paths:
        _add_diagnostic(
            errors, 'error', 'reserved_image_customization_artifact',
            'Additional modules use names reserved for generated image '
            'customization layers; rename or remove them before building.')
    mandatory_paths = set(
        item.relative_path for item in selected_modules)
    mandatory_paths.update(required_boot_relative)
    mandatory_paths.update(kernel_relative)
    mandatory_paths.update(initramfs_relative)
    mandatory_paths.update(menu_relative)
    deselected_names = [item.basename for item in deselected_modules]
    safe_regex = module_exclusion_regex(deselected_names)
    advanced_regex = None
    advanced_matches = ()
    if project.exclusions:
        if len(project.exclusions) != 1:
            _add_diagnostic(
                errors, 'error', 'multiple_advanced_regexes_unsupported',
                'Only one unchanged advanced POSIX ERE can be represented.')
        elif safe_regex:
            _add_diagnostic(
                errors, 'error', 'advanced_regex_cannot_combine_safely',
                'Advanced regex cannot be combined with generated module '
                'selection without changing backreference grouping.')
        else:
            advanced_regex = project.exclusions[0]
            validator = regex_validator or (
                lambda pattern, paths: grep_ere_validate(
                    pattern, paths, grep=grep, runner=regex_runner))
            try:
                valid, advanced_matches, detail = validator(
                    advanced_regex, tuple(all_relative_paths))
            except Exception as error:
                valid, advanced_matches, detail = False, (), str(error)
            if not valid:
                _add_diagnostic(
                    errors, 'error', 'advanced_regex_validation_failed',
                    'Advanced POSIX ERE could not be validated{}.'.format(
                        ': ' + detail if detail else ''))
            matched_mandatory = sorted(set(advanced_matches) & mandatory_paths)
            if matched_mandatory:
                _add_diagnostic(
                    errors, 'error', 'advanced_regex_matches_mandatory_input',
                    'Advanced regex matches mandatory or selected source '
                    'paths: {}.'.format(', '.join(matched_mandatory)))
            if valid:
                _add_diagnostic(
                    warnings, 'warning', 'advanced_exclusion_regex',
                    'Validated advanced POSIX ERE excludes {} source path(s).'.format(
                        len(advanced_matches)))
    adapter_regex = advanced_regex or safe_regex

    selected_relative = set(item.relative_path for item in selected_modules)
    deselected_relative = set(item.relative_path for item in deselected_modules)
    included_source_records = [
        item for item in source_records
        if item['relative_path'] not in deselected_relative and
        item['relative_path'] not in set(advanced_matches)
    ]
    included_relative_paths = tuple(
        item['relative_path'] for item in included_source_records)
    effective_module_targets = [
        item['target_path'] for item in included_source_records
        if item['target_path'].endswith('.sb')]
    effective_module_targets.extend(
        item['target_path'] for item in additional)
    try:
        overlay_value, capture_value = _dynamic_module_orders(
            [posixpath.basename(target) for target in effective_module_targets],
            overlay_requested=overlay_requested,
            capture_requested=capture_requested)
        overlay_order, overlay_target = overlay_value
        capture_order, capture_target = capture_value
    except ValueError as error:
        _add_diagnostic(
            errors, 'error', 'dynamic_module_order_unavailable', str(error))
    if overlay_target and overlay_target in effective_module_targets:
        _add_diagnostic(
            errors, 'error', 'overlay_module_target_collision',
            'Dynamic image overlay target collides with an input module.',
            overlay_target)
    if capture_target and capture_target in effective_module_targets:
        _add_diagnostic(
            errors, 'error', 'capture_module_target_collision',
            'Dynamic session capture target collides with an input module.',
            capture_target)
    boot_customization_targets = ()
    boot_expected_records = ()
    boot_expected_payloads = {}
    boot_target_count, boot_target_digest = (
        customization_target_set_identity(boot_customization_targets))
    if boot_customization_requested and boot is not None:
        try:
            (boot_expected_records,
             boot_expected_payloads) = _expected_boot_customization_records(
                 source_info.source_path, boot['bootloader'],
                 project.menu_locale, included_relative_paths,
                 project.boot_timeout, project.default_boot,
                 project.kernel_args, project.boot_menu_entries)
            boot_customization_targets = tuple(
                item['target'] for item in boot_expected_records)
            boot_target_count, boot_target_digest = (
                customization_target_set_identity(
                    boot_customization_targets))
        except (OSError, ValueError, ImageProjectError) as error:
            _add_diagnostic(
                errors, 'error', 'boot_customization_graph_invalid',
                'Effective boot configuration graph is invalid: {}'.format(
                    error))
    background_targets = ()
    background_target_count = 0
    background_target_digest = None
    if background_requested:
        background_targets = tuple(sorted(set(
            item['target_path'] for item in included_source_records
            if re.match(r'^minios/boot/bootlogo.*[.]png$',
                        item['target_path'])) | {
                            'minios/boot/bootlogo791.png'}))
        try:
            background_target_count, background_target_digest = (
                customization_target_set_identity(background_targets))
        except ValueError as error:
            _add_diagnostic(
                errors, 'error', 'background_target_set_invalid', str(error))
    # Ensure the selected module content in the current source still matches
    # the module objects captured by discovery.
    source_record_map = {item['relative_path']: item
                         for item in source_records}
    for module in selected_modules:
        record = source_record_map.get(module.relative_path)
        if record is None:
            _add_diagnostic(
                errors, 'error', 'selected_module_manifest_missing',
                'Selected module is absent from current source manifest.',
                module.path)
        elif module.is_symlink:
            # Its target content digest was captured separately by ModuleInfo.
            try:
                current_digest, unused_stat = _followed_module_digest(module.path)
            except (OSError, SourceInspectionError) as error:
                _add_diagnostic(errors, 'error', 'selected_module_hash_failed',
                                str(error), module.path)
            else:
                if current_digest != module.sha256:
                    _add_diagnostic(
                        errors, 'error', 'selected_module_content_drift',
                        'Selected symlinked module target changed.', module.path)

    if not output_path.lower().endswith('.iso'):
        _add_diagnostic(errors, 'error', 'output_extension',
                        'Output path must end with .iso.', output_path)
    if not os.path.isdir(output_directory):
        _add_diagnostic(
            errors, 'error', 'output_directory_missing',
            'Output directory does not exist.', output_directory)
    elif not _mode_is_writable_directory(output_directory):
        _add_diagnostic(
            errors, 'error', 'output_directory_unwritable',
            'Output directory is not writable.', output_directory)
    if os.path.isdir(output_directory) and not _is_real_directory(output_directory):
        _add_diagnostic(
            errors, 'error', 'output_directory_symlink',
            'Output directory must not be a symlink.', output_directory)
    if _is_within(output_path, source_info.source_path or project.source_path):
        _add_diagnostic(
            errors, 'error', 'output_within_source',
            'Output must not be inside the MiniOS source tree.', output_path)
    protected_input_paths = list(project.additional_module_paths) + [config_path]
    if project.boot_background_path is not None:
        protected_input_paths.append(project.boot_background_path)
    for input_path in protected_input_paths:
        if _same_path(output_path, input_path):
            _add_diagnostic(
                errors, 'error', 'output_overlaps_input',
                'Output must not replace a build input.',
                input_path)
    if overlay_requested and _is_within(
            output_path, project.overlay_directory):
        _add_diagnostic(
            errors, 'error', 'output_within_overlay',
            'Output must not be inside the overlay directory.', output_path)
    output_expectation = {'exists': False, 'identity': None}
    if os.path.lexists(output_path):
        try:
            identity = _existing_regular_identity(output_path)
        except (OSError, ImageProjectError, SourceInspectionError) as error:
            _add_diagnostic(
                errors, 'error', 'output_not_regular', str(error), output_path)
        else:
            output_expectation = {'exists': True, 'identity': identity}
            if not project.overwrite_output:
                _add_diagnostic(
                    errors, 'error', 'output_exists_overwrite_not_allowed',
                    'Output exists and overwrite policy is false.', output_path)
    scratch_identity = None
    scratch_usable = False
    if '\n' in scratch_directory or '\r' in scratch_directory:
        _add_diagnostic(
            errors, 'error', 'scratch_directory_invalid',
            'Scratch directory path must not contain line breaks.',
            scratch_directory)
    if not os.path.isdir(scratch_directory):
        _add_diagnostic(
            errors, 'error', 'scratch_directory_missing',
            'Scratch directory does not exist.', scratch_directory)
    elif not _mode_is_writable_directory(scratch_directory):
        _add_diagnostic(
            errors, 'error', 'scratch_directory_unwritable',
            'Scratch directory is not writable.', scratch_directory)
    else:
        scratch_usable = True
    if os.path.isdir(scratch_directory) and not _is_real_directory(
            scratch_directory):
        _add_diagnostic(
            errors, 'error', 'scratch_directory_symlink',
            'Scratch directory must not be a symlink.', scratch_directory)
        scratch_usable = False
    if scratch_usable:
        trust_error = _scratch_path_trust_error(scratch_directory)
        if trust_error:
            _add_diagnostic(
                errors, 'error', 'scratch_directory_untrusted',
                'Scratch directory path is not protected from replacement: '
                '{}.'.format(trust_error), scratch_directory)
        else:
            probe_error = _probe_private_workspace(scratch_directory)
            if probe_error:
                _add_diagnostic(
                    errors, 'error', 'scratch_directory_incompatible',
                    'Scratch directory cannot hold a secure private workspace: '
                    '{}.'.format(probe_error), scratch_directory)
            else:
                try:
                    scratch_identity = _identity(os.lstat(scratch_directory))
                except OSError as error:
                    _add_diagnostic(
                        errors, 'error', 'scratch_directory_unavailable',
                        str(error), scratch_directory)
    if _is_within(scratch_directory,
                  source_info.source_path or project.source_path):
        _add_diagnostic(
            errors, 'error', 'scratch_within_source',
            'Scratch directory must not be inside the source tree.',
            scratch_directory)
    if (project.overlay_directory and
            (_is_within(scratch_directory, project.overlay_directory) or
             _physically_within(scratch_directory, project.overlay_directory,
                                mountinfo_path))):
        _add_diagnostic(
            errors, 'error', 'scratch_within_project_overlay',
            'Scratch directory must not be inside the project filesystem '
            'layer.', scratch_directory)

    estimated_input_bytes = sum(item['size'] for item in included_source_records)
    estimated_input_bytes += sum(item.get('size') or 0 for item in additional)
    if config_record:
        estimated_input_bytes += (
            rendered_config_size if rendered_config_size is not None
            else config_record['size'])
    if background_record:
        estimated_input_bytes += background_record['size']
    if overlay_inventory_value is not None:
        overlay_bytes = overlay_inventory_value['regular_bytes']
        estimated_input_bytes += overlay_bytes + max(
            1024 * 1024, overlay_bytes // 10)
    if capture_estimated_bytes is not None:
        estimated_input_bytes += capture_estimated_bytes
    required_destination_bytes = (
        estimated_input_bytes +
        max(MIN_DESTINATION_HEADROOM, estimated_input_bytes // 2))
    required_scratch_bytes = max(
        MIN_SCRATCH_HEADROOM, estimated_input_bytes * 2)
    destination_free = None
    scratch_free = None
    shared_filesystem = False
    combined_required_bytes = None
    if os.path.isdir(output_directory):
        try:
            destination_free = _disk_free_bytes(
                output_directory, disk_usage_func)
            if destination_free < required_destination_bytes:
                _add_diagnostic(
                    errors, 'error', 'destination_space_insufficient',
                    'Destination has {} bytes free; {} are required.'.format(
                        destination_free, required_destination_bytes),
                    output_directory)
        except (OSError, TypeError, ValueError) as error:
            _add_diagnostic(
                errors, 'error', 'destination_space_unavailable', str(error),
                output_directory)
    if os.path.isdir(scratch_directory):
        try:
            scratch_free = _disk_free_bytes(
                scratch_directory, disk_usage_func)
            if scratch_free < required_scratch_bytes:
                _add_diagnostic(
                    errors, 'error', 'scratch_space_insufficient',
                    'Scratch has {} bytes free; {} are required.'.format(
                        scratch_free, required_scratch_bytes), scratch_directory)
        except (OSError, TypeError, ValueError) as error:
            _add_diagnostic(
                errors, 'error', 'scratch_space_unavailable', str(error),
                scratch_directory)
    if (os.path.isdir(output_directory) and os.path.isdir(scratch_directory)
            and destination_free is not None and scratch_free is not None):
        try:
            shared_filesystem = (
                os.stat(output_directory).st_dev ==
                os.stat(scratch_directory).st_dev)
        except OSError as error:
            _add_diagnostic(
                errors, 'error', 'space_filesystem_probe_failed', str(error))
        if shared_filesystem:
            combined_required_bytes = (
                required_destination_bytes + required_scratch_bytes)
            if min(destination_free, scratch_free) < combined_required_bytes:
                _add_diagnostic(
                    errors, 'error', 'combined_space_insufficient',
                    'Destination and scratch share a filesystem with {} bytes '
                    'free; {} concurrent bytes are required.'.format(
                        min(destination_free, scratch_free),
                        combined_required_bytes), output_directory)

    # Classify work filesystems and account RAM-backed workspaces against
    # available memory. RAM use remains advisory, but a live-overlay scratch
    # directory is unsafe while that writable layer is being captured.
    destination_filesystem_class = (
        _classify_directory_filesystem(output_directory, mounts_path)
        if os.path.isdir(output_directory) else FILESYSTEM_CLASS_UNKNOWN)
    scratch_filesystem_class = (
        _classify_directory_filesystem(scratch_directory, mounts_path)
        if os.path.isdir(scratch_directory) else FILESYSTEM_CLASS_UNKNOWN)
    effective_changes_root = _effective_live_changes_root(changes_roots)
    destination_uses_captured_union = _uses_captured_union_storage(
        output_directory, effective_changes_root, mountinfo_path)
    scratch_uses_captured_union = _uses_captured_union_storage(
        scratch_directory, effective_changes_root, mountinfo_path)
    if (capture_requested and
            (destination_filesystem_class == FILESYSTEM_CLASS_LIVE_OVERLAY or
             destination_uses_captured_union)):
        _add_diagnostic(
            errors, 'error', 'destination_on_captured_live_overlay',
            'Private output work files on the live writable layer could be '
            'included in the saved session changes.', output_directory)
    if (capture_requested and
            (scratch_filesystem_class == FILESYSTEM_CLASS_LIVE_OVERLAY or
             scratch_uses_captured_union)):
        _add_diagnostic(
            errors, 'error', 'scratch_on_captured_live_overlay',
            'Temporary build files on the live writable layer could be '
            'included in the saved session changes.', scratch_directory)
    if (capture_requested and effective_changes_root and
            (_is_within(scratch_directory, effective_changes_root) or
             _physically_within(scratch_directory, effective_changes_root,
                                mountinfo_path))):
        _add_diagnostic(
            errors, 'error', 'scratch_within_captured_changes',
            'Temporary work directory must be outside the changes directory '
            'that is being saved.', scratch_directory)
    if (capture_requested and effective_changes_root and
            (_is_within(output_directory, effective_changes_root) or
             _physically_within(output_directory, effective_changes_root,
                                mountinfo_path))):
        _add_diagnostic(
            errors, 'error', 'destination_within_captured_changes',
            'Output directory must be outside the changes directory that is '
            'being saved.', output_directory)
    available_memory_bytes = _available_memory_bytes(meminfo_path)
    ram_backed_bytes = 0
    if destination_filesystem_class == FILESYSTEM_CLASS_RAM_BACKED:
        ram_backed_bytes += required_destination_bytes
    if scratch_filesystem_class == FILESYSTEM_CLASS_RAM_BACKED:
        if shared_filesystem:
            ram_backed_bytes = max(ram_backed_bytes, combined_required_bytes)
        else:
            ram_backed_bytes += required_scratch_bytes
    peak_memory_bytes = (
        ram_backed_bytes + MIN_MEMORY_HEADROOM if ram_backed_bytes else None)
    if (peak_memory_bytes is not None and available_memory_bytes is not None
            and peak_memory_bytes > available_memory_bytes):
        _add_diagnostic(
            warnings, 'warning', 'ram_workspace_memory_pressure',
            'A RAM-backed work area needs about {} bytes but only {} bytes of '
            'memory are available. The build may exhaust memory. Consider the '
            'persistent MiniOS changes storage or another mounted filesystem '
            'for the destination or scratch directory, or free memory before '
            'building.'.format(peak_memory_bytes, available_memory_bytes),
            output_directory)

    expected_module_targets = [
        item.target_path for item in selected_modules]
    expected_module_targets.extend(item['target_path'] for item in additional)
    forbidden_module_targets = [item.target_path for item in deselected_modules]
    expected_boot_targets = [
        'minios/' + item for item in required_boot_relative]
    expected_kernel_targets = ['minios/' + item for item in kernel_relative]
    expected_initramfs_targets = ['minios/' + item
                                  for item in initramfs_relative]
    expected_menu_targets = ['minios/' + item for item in menu_relative]
    bios_target = None
    if boot:
        if boot['bootloader'] == 'grub-only':
            bios_target = 'minios/boot/grub/i386-pc/eltorito.img'
        elif boot['bootloader'] in ('syslinux-grub', 'syslinux-native'):
            bios_target = 'minios/boot/syslinux/isolinux.bin'
    base_manifest = {
        'product_kind': BUILD_PLAN_KIND,
        'schema_version': 1,
        'buildable': False,
        'source': {
            'kind': SOURCE_KIND,
            'backend': source_info.backend,
            'root_path': '<running-live-root>',
            'tree_path': '<running-minios-source>',
            'fingerprint': current_fingerprint,
            'fingerprint_algorithm': SOURCE_FINGERPRINT_ALGORITHM,
            'bootloader': boot['bootloader'] if boot else None,
        },
        'composition': {
            'selected_source_modules': [_public_module_record(item)
                                        for item in selected_modules],
            'deselected_source_modules': [_public_module_record(item)
                                          for item in deselected_modules],
            'additional_modules': [
                dict((key, item.get(key)) for key in (
                    'basename', 'target_path', 'size', 'sha256'))
                for item in additional],
            'active_external_modules_observed': [
                _public_module_record(item)
                for item in source_info.active_external_modules],
        },
        'config': {
            'include_current_config': project.include_current_config,
            'config_path': '<live-config-input>',
            'config_sha256': (config_record['sha256']
                              if config_record else None),
            'override_count': len(project.live_config_overrides),
            'override_keys': sorted(project.live_config_overrides),
            'rendered_size': rendered_config_size,
            'rendered_sha256': rendered_config_digest,
            'sensitive_key_setting_count': sensitive_count,
            'sensitive_config_acknowledged':
                project.sensitive_config_acknowledged,
        },
        'customization': {
            'requested': customization_requested,
            'adapter_report_requested': adapter_customization_requested,
            'live_config': {
                'requested': bool(project.live_config_overrides),
                'override_count': len(project.live_config_overrides),
                'rendered_size': rendered_config_size,
                'rendered_sha256': rendered_config_digest,
            },
            'boot': {
                'requested': boot_customization_requested,
                'timeout_seconds': project.boot_timeout,
                'default_boot': project.default_boot,
                'menu_entries': _boot_menu_plan_summary(
                    project.boot_menu_entries),
                'kernel_args': kernel_argument_summary,
                'config_target_count': boot_target_count,
                'config_target_set_sha256': boot_target_digest,
                'expected_configs': [dict(item)
                                     for item in boot_expected_records],
                'background': (
                    dict(background_metadata_value,
                         target_count=background_target_count,
                         target_set_sha256=background_target_digest)
                    if background_metadata_value is not None else None),
            },
            'overlay': ({
                'requested': True,
                'module_order': overlay_order,
                'module_target': overlay_target,
                'input_tree_fingerprint': overlay_inventory_value[
                    'input_tree_fingerprint'],
                'entry_count': overlay_inventory_value['entry_count'],
                'regular_bytes': overlay_inventory_value['regular_bytes'],
            } if overlay_inventory_value is not None else {
                'requested': overlay_requested,
                'module_order': overlay_order,
                'module_target': overlay_target,
                'input_tree_fingerprint': None,
                'entry_count': None,
                'regular_bytes': None,
            }),
        },
        'capture': {
            'requested': capture_requested,
            'mode': project.capture_mode,
            'compression': (project.capture_compression
                            if capture_requested else None),
            'sensitive_capture_acknowledged':
                project.sensitive_capture_acknowledged,
            'selection': {
                'include_count': len(project.capture_include_paths),
                'exclude_count': len(project.capture_exclude_paths),
                'sha256': capture_selection_digest,
            },
            'inventory': {
                'provided': session_inventory is not None,
                'entry_count': (len(session_inventory.entries)
                                if session_inventory else None),
                'document_sha256': (session_inventory.document_sha256
                                    if session_inventory else None),
                'source_fingerprint': (session_inventory.source_fingerprint
                                       if session_inventory else None),
                'union_backend': (session_inventory.union_backend
                                  if session_inventory else None),
                'selected_entry_count': capture_inventory_selected_count,
            },
            'estimated_bytes': capture_estimated_bytes,
            'expected_base_module_fingerprint':
                expected_base_module_fingerprint,
            'expected_module_order': capture_order,
            'expected_module_target': capture_target,
        },
        'menu_locale': project.menu_locale,
        'volume_label': project.volume_label or 'MINIOS',
        'exclusions': {
            'generated_module_regex': safe_regex or None,
            'advanced_regex': advanced_regex,
            'advanced_matched_paths': list(advanced_matches),
            'adapter_regex': adapter_regex or None,
        },
        'expected_iso': {
            'module_targets': expected_module_targets,
            'forbidden_module_targets': forbidden_module_targets,
            'config_target': 'minios/config.conf',
            'build_manifest_target': 'minios/build-manifest.json',
            'required_boot_targets': expected_boot_targets,
            'kernel_targets': expected_kernel_targets,
            'initramfs_targets': expected_initramfs_targets,
            'menu_targets': expected_menu_targets,
            'bios_required': bool(boot and boot['bootloader'] != 'unknown'),
            'bios_target': bios_target,
            'uefi_targets': [
                'minios/boot/grub/efi.img',
            ] if boot and boot['bootloader'] != 'unknown' else [],
            'session_capture': {
                'requested': capture_requested,
                'report_target': ('minios/session-capture.json'
                                  if capture_requested else None),
                'module_target': capture_target,
                'module_order': capture_order,
            },
            'image_customization': {
                'requested': customization_requested,
                'adapter_report_requested': adapter_customization_requested,
                'report_target': (
                    'minios/image-customization.json'
                    if adapter_customization_requested else None),
                'verify_config_digest': bool(project.live_config_overrides),
                'boot_config_targets': list(boot_customization_targets),
                'background_targets': list(background_targets),
                'overlay_requested': overlay_requested,
                'overlay_target': overlay_target,
                'overlay_order': overlay_order,
            },
        },
        'input_digests': {
            'source_files': [
                {key: item.get(key) for key in (
                    'relative_path', 'type', 'size', 'sha256', 'link_target')}
                for item in included_source_records],
            'additional_modules': [
                {key: item.get(key) for key in (
                    'target_path', 'size', 'sha256')}
                for item in additional],
            'config': ({key: config_record.get(key) for key in (
                'target_path', 'size', 'sha256')}
                       if config_record else None),
            'boot_background': ({
                'size': background_record['size'],
                'sha256': background_record['sha256'],
            } if background_record else None),
            'overlay': ({
                'entry_count': overlay_inventory_value['entry_count'],
                'regular_bytes': overlay_inventory_value['regular_bytes'],
                'input_tree_fingerprint': overlay_inventory_value[
                    'input_tree_fingerprint'],
            } if overlay_inventory_value is not None else None),
        },
        'estimate': {
            'input_bytes': estimated_input_bytes,
            'required_destination_bytes': required_destination_bytes,
            'required_scratch_bytes': required_scratch_bytes,
            'destination_free_bytes': destination_free,
            'scratch_free_bytes': scratch_free,
            'shared_destination_scratch_filesystem': shared_filesystem,
            'combined_required_bytes': combined_required_bytes,
            'destination_filesystem_class': destination_filesystem_class,
            'scratch_filesystem_class': scratch_filesystem_class,
            'available_memory_bytes': available_memory_bytes,
            'peak_memory_bytes': peak_memory_bytes,
        },
        'output': {
            'final_path': output_path,
            'overwrite_allowed': project.overwrite_output,
            'existing_output': output_expectation,
            'atomic_publish_required': True,
            'job_directory': None,
            'partial_path': None,
            'adapter_manifest_path': None,
        },
        'tools': _public_tool_capabilities(tool_capabilities),
        'adapter': {
            'name': 'minios-image-compose', 'argv': [], 'mutates_source': False,
            'must_revalidate_inputs_immediately_before_execution': True,
        },
        'errors': [], 'warnings': [],
    }
    if errors:
        return _failed_plan(
            errors, warnings, estimated_input_bytes, output_path, base_manifest)

    job_directory = None
    job_descriptor = None
    try:
        job_directory, job_stat, job_descriptor = _allocate_job_directory(
            output_directory)
        if _is_within(job_directory, source_info.source_path):
            raise ImageProjectError('job directory resolved inside source')
        partial_basename = 'image.partial.iso'
        adapter_manifest_basename = 'compose-manifest.json'
        partial_path = os.path.join(job_directory, partial_basename)
        adapter_manifest_path = os.path.join(
            job_directory, adapter_manifest_basename)
        capture_selection_path = (
            os.path.join(job_directory, 'session-selection.json')
            if project.capture_mode == 'selected' else None)
        live_config_path = (
            os.path.join(job_directory, 'live-config.conf')
            if rendered_config_payload is not None else None)
        private_basenames = [partial_basename, adapter_manifest_basename]
        if capture_selection_path:
            private_basenames.append(os.path.basename(capture_selection_path))
        if live_config_path:
            private_basenames.append(os.path.basename(live_config_path))
        if any(_entry_metadata(job_descriptor, name) is not None
               for name in private_basenames):
            raise ImageProjectError('private job paths unexpectedly exist')
    except (OSError, ImageProjectError) as error:
        if job_descriptor is not None:
            os.close(job_descriptor)
            job_descriptor = None
        if job_directory:
            try:
                os.rmdir(job_directory)
            except OSError:
                pass
        _add_diagnostic(
            errors, 'error', 'secure_job_allocation_failed', str(error),
            output_directory)
        return _failed_plan(
            errors, warnings, estimated_input_bytes, output_path, base_manifest)

    argv = [
        compose_backend_path,
        '--source', source_info.source_path,
        '--config', (os.path.basename(live_config_path)
                     if live_config_path else config_path),
        '--name', partial_basename,
        '--menu', project.menu_locale,
        '--manifest', adapter_manifest_basename,
    ]
    if project.volume_label is not None:
        argv.extend(('--volume-label', project.volume_label))
    if project.boot_timeout is not None:
        argv.extend(('--boot-timeout', str(project.boot_timeout)))
    if project.default_boot is not None:
        argv.extend(('--default-boot', project.default_boot))
    if project.boot_menu_entries is not None:
        boot_menu_json = json.dumps(
            _boot_menu_public(project.boot_menu_entries),
            ensure_ascii=True, allow_nan=False, sort_keys=True,
            separators=(',', ':'))
        argv.extend(('--boot-menu-json', boot_menu_json))
    if project.kernel_args is not None:
        argv.extend(('--kernel-args', project.kernel_args))
    if project.boot_background_path is not None:
        argv.extend(('--boot-background', project.boot_background_path))
    if project.overlay_directory is not None:
        argv.extend(('--overlay-directory', project.overlay_directory))
    if capture_requested:
        argv.extend((
            '--capture-changes', project.capture_mode,
            '--capture-compression', project.capture_compression,
        ))
        if capture_selection_path:
            argv.extend((
                '--capture-selection', os.path.basename(capture_selection_path)))
    if adapter_regex:
        argv.extend(('--exclude', adapter_regex))
    argv.extend(item['path'] for item in additional)

    all_input_records = list(included_source_records)
    all_input_records.extend(additional_records)
    if config_record and config_record.get('path'):
        all_input_records.append(config_record)
    if background_record:
        all_input_records.append(background_record)
    base_manifest['buildable'] = True
    base_manifest['output'].update({
        'job_directory': '<private-job-directory>',
        'partial_path': '<private-partial-output>',
        'adapter_manifest_path': '<private-build-manifest>',
        'job_device': int(job_stat.st_dev),
        'job_inode': int(job_stat.st_ino),
        'job_mode': '0700',
    })
    display_argv = _redacted_build_argv(
        argv, additional_paths=(item['path'] for item in additional))
    base_manifest['adapter']['argv'] = list(display_argv)
    base_manifest['errors'] = []
    base_manifest['warnings'] = [_public_diagnostic(item) for item in warnings]
    base_manifest['plan_id'] = _json_digest(base_manifest)
    return BuildPlan(
        errors, warnings, estimated_input_bytes, argv, output_path,
        partial_path, job_directory, adapter_manifest_path, base_manifest,
        all_input_records, _identity(job_stat), output_expectation,
        tool_capabilities,
        capture_selection_path=capture_selection_path,
        capture_selection_payload=capture_selection_payload,
        capture_selection_digest=capture_selection_digest,
        session_inventory=session_inventory,
        expected_base_module_fingerprint=expected_base_module_fingerprint,
        capture_requested=capture_requested,
        live_config_path=live_config_path,
        live_config_payload=rendered_config_payload,
        live_config_digest=rendered_config_digest,
        customization_requested=customization_requested,
        adapter_customization_requested=adapter_customization_requested,
        overlay_directory=project.overlay_directory,
        overlay_inventory=overlay_inventory_value,
        boot_config_payloads=boot_expected_payloads,
        job_descriptor=job_descriptor,
        source_path=source_info.source_path,
        display_argv=display_argv,
        scratch_directory=os.path.realpath(scratch_directory),
        scratch_identity=scratch_identity,
        _token=_PLAN_TOKEN)


preflight = create_build_plan
build_plan = create_build_plan


def _validate_job_identity(plan):
    if (not plan.job_directory or not plan._job_identity or
            plan._job_descriptor is None):
        raise ImageProjectError('plan has no allocated job directory')
    try:
        retained_stat = os.fstat(plan._job_descriptor)
    except OSError as error:
        raise ImageProjectError(
            'private job directory descriptor is unavailable: {}'.format(
                error))
    if (not stat.S_ISDIR(retained_stat.st_mode) or
            _identity(retained_stat) != plan._job_identity or
            stat.S_IMODE(retained_stat.st_mode) != 0o700):
        raise ImageProjectError(
            'private job directory descriptor identity changed')
    if (hasattr(os, 'geteuid') and
            retained_stat.st_uid != os.geteuid()):
        raise ImageProjectError('private job directory owner changed')
    file_stat = os.lstat(plan.job_directory)
    if (stat.S_ISLNK(file_stat.st_mode) or
            not stat.S_ISDIR(file_stat.st_mode) or
            _identity(file_stat) != plan._job_identity or
            stat.S_IMODE(file_stat.st_mode) != 0o700):
        raise ImageProjectError('private job directory identity changed')
    if hasattr(os, 'geteuid') and file_stat.st_uid != os.geteuid():
        raise ImageProjectError('private job directory owner changed')
    return retained_stat


def _validate_scratch_identity(plan):
    if not plan.scratch_directory or plan._scratch_identity is None:
        raise ImageProjectError(
            'plan has no validated temporary work directory')
    try:
        file_stat = os.lstat(plan.scratch_directory)
    except OSError as error:
        raise ImageProjectError(
            'temporary work directory is unavailable: {}'.format(error))
    if (stat.S_ISLNK(file_stat.st_mode) or
            not stat.S_ISDIR(file_stat.st_mode) or
            _identity(file_stat) != plan._scratch_identity or
            not _mode_is_writable_directory(plan.scratch_directory)):
        raise ImageProjectError(
            'temporary work directory changed after preflight')
    trust_error = _scratch_path_trust_error(plan.scratch_directory)
    if trust_error:
        raise ImageProjectError(
            'temporary work directory is no longer trusted: {}'.format(
                trust_error))
    probe_error = _probe_private_workspace(plan.scratch_directory)
    if probe_error:
        raise ImageProjectError(
            'temporary work directory is no longer usable: {}'.format(
                probe_error))


def _duplicate_job_descriptor(plan):
    _validate_job_identity(plan)
    try:
        descriptor = os.dup(plan._job_descriptor)
    except OSError as error:
        raise ImageProjectError(
            'cannot retain private job directory: {}'.format(error))
    try:
        metadata = os.fstat(descriptor)
        if (_identity(metadata) != plan._job_identity or
                not stat.S_ISDIR(metadata.st_mode)):
            raise ImageProjectError(
                'private job directory descriptor identity changed')
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def revalidate_build_plan_inputs(plan):
    """Re-hash every effective input immediately before executing ``argv``."""
    if not isinstance(plan, BuildPlan) or not plan.buildable:
        raise TypeError('a buildable BuildPlan is required')
    diagnostics = []
    try:
        _validate_job_identity(plan)
    except (OSError, ImageProjectError) as error:
        diagnostics.append(Diagnostic(
            'error', 'job_identity_changed', str(error), plan.job_directory))
    if os.path.lexists(plan.partial_output_path):
        diagnostics.append(Diagnostic(
            'error', 'partial_output_preexists',
            'Partial output unexpectedly exists before build.',
            plan.partial_output_path))
    source_path = plan._source_path
    if not source_path:
        diagnostics.append(Diagnostic(
            'error', 'build_source_changed',
            'Build plan has no private source binding.'))
        return tuple(diagnostics)
    try:
        current_fingerprint, unused_size, unused_manifest = (
            _build_source_manifest(source_path))
        if current_fingerprint != plan.manifest['source']['fingerprint']:
            raise ImageProjectError('complete source fingerprint changed')
    except (OSError, ImageProjectError, SourceInspectionError) as error:
        diagnostics.append(Diagnostic(
            'error', 'build_source_changed',
            'Source changed after preflight: {}'.format(error), source_path))
    for frozen in plan._input_records:
        record = _thaw(frozen)
        path = record['path']
        try:
            file_stat = os.lstat(path)
            if record['type'] == 'symlink':
                if not stat.S_ISLNK(file_stat.st_mode):
                    raise ImageProjectError('symlink type changed')
                link_target = os.readlink(path)
                digest = hashlib.sha256(
                    b'symlink\x00' + link_target.encode(
                        'utf-8', 'surrogateescape')).hexdigest()
                if (link_target != record['link_target'] or
                        digest != record['sha256']):
                    raise ImageProjectError('symlink target changed')
            else:
                if stat.S_ISLNK(file_stat.st_mode):
                    raise ImageProjectError('input became a symlink')
                digest, opened_stat = _secure_hash_regular(path, file_stat)
                if digest != record['sha256']:
                    raise ImageProjectError('content digest changed')
        except (OSError, ImageProjectError, SourceInspectionError) as error:
            diagnostics.append(Diagnostic(
                'error', 'build_input_changed',
                'Input changed after preflight: {}'.format(error), path))
    if plan._overlay_directory:
        try:
            current_overlay = _overlay_inventory(plan._overlay_directory)
            expected_overlay = _thaw(plan._overlay_inventory)
            if _thaw(_freeze(current_overlay)) != expected_overlay:
                raise ImageProjectError(
                    'overlay content or identity changed after preflight')
        except (OSError, ImageProjectError) as error:
            diagnostics.append(Diagnostic(
                'error', 'overlay_input_changed',
                'Overlay changed after preflight: {}'.format(error)))
    return tuple(diagnostics)


def _private_job_basename(plan, path):
    if (not isinstance(path, str) or
            os.path.dirname(path) != plan.job_directory):
        raise ImageProjectError('private file is outside the retained job')
    basename = os.path.basename(path)
    if basename in ('', '.', '..') or os.sep in basename:
        raise ImageProjectError('private file has an unsafe basename')
    return basename


def _validate_private_file_metadata(metadata, expected_size, context):
    if (metadata is None or stat.S_ISLNK(metadata.st_mode) or
            not stat.S_ISREG(metadata.st_mode) or
            stat.S_IMODE(metadata.st_mode) != 0o600 or
            metadata.st_size != expected_size or metadata.st_nlink != 1 or
            (hasattr(os, 'geteuid') and metadata.st_uid != os.geteuid())):
        raise ImageProjectError(
            '{} is unsafe or changed'.format(context))


def _hash_private_file_at(directory_descriptor, basename, expected_size,
                          context):
    metadata = _entry_metadata(directory_descriptor, basename)
    _validate_private_file_metadata(metadata, expected_size, context)
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    if hasattr(os, 'O_CLOEXEC'):
        flags |= os.O_CLOEXEC
    descriptor = os.open(basename, flags, dir_fd=directory_descriptor)
    try:
        opened = os.fstat(descriptor)
        if _metadata_snapshot(opened) != _metadata_snapshot(metadata):
            raise ImageProjectError(
                '{} changed while opening'.format(context))
        digest = _hash_descriptor(descriptor)
        final_metadata = os.fstat(descriptor)
        if _metadata_snapshot(final_metadata) != _metadata_snapshot(opened):
            raise ImageProjectError(
                '{} changed while reading'.format(context))
        return digest, final_metadata
    finally:
        os.close(descriptor)


def _materialize_private_payload(plan, path, payload, expected_digest,
                                 context):
    if (not isinstance(payload, bytes) or not _is_sha256(expected_digest) or
            hashlib.sha256(payload).hexdigest() != expected_digest):
        raise ImageProjectError('{} payload is invalid'.format(context))
    basename = _private_job_basename(plan, path)
    directory_descriptor = _duplicate_job_descriptor(plan)
    temporary_name = None
    temporary_descriptor = None
    try:
        if _entry_metadata(directory_descriptor, basename) is not None:
            digest, opened = _hash_private_file_at(
                directory_descriptor, basename, len(payload), context)
            if digest != expected_digest:
                raise ImageProjectError(
                    '{} digest changed'.format(context))
            return _identity(opened)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
        for unused_attempt in range(128):
            temporary_name = '.{}.{}.tmp'.format(
                basename, os.urandom(16).hex())
            try:
                temporary_descriptor = os.open(
                    temporary_name, flags, 0o600,
                    dir_fd=directory_descriptor)
                break
            except OSError as error:
                if error.errno != errno.EEXIST:
                    raise
        else:
            raise ImageProjectError(
                'cannot allocate temporary {}'.format(context))
        os.fchmod(temporary_descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(temporary_descriptor, payload[offset:])
            if written <= 0:
                raise OSError(errno.EIO, 'short private file write')
            offset += written
        os.fsync(temporary_descriptor)
        created = os.fstat(temporary_descriptor)
        _validate_private_file_metadata(created, len(payload), context)
        os.replace(
            temporary_name, basename, src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor)
        temporary_name = None
        os.fsync(directory_descriptor)
        digest, opened = _hash_private_file_at(
            directory_descriptor, basename, len(payload), context)
        if (_identity(opened) != _identity(created) or
                digest != expected_digest):
            raise ImageProjectError(
                '{} materialization verification failed'.format(context))
        _validate_job_identity(plan)
        return _identity(opened)
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except OSError:
                pass
        os.close(directory_descriptor)


def _materialize_capture_selection(plan):
    if not plan._capture_selection_path:
        return None
    return _materialize_private_payload(
        plan, plan._capture_selection_path, plan._capture_selection_payload,
        plan._capture_selection_digest, 'private capture selection')


def _materialize_live_config(plan):
    if not plan._live_config_path:
        return None
    return _materialize_private_payload(
        plan, plan._live_config_path, plan._live_config_payload,
        plan._live_config_digest, 'private live config')


def prepare_build_command(plan):
    """Return argv only after the mandatory immediate input revalidation."""
    _validate_scratch_identity(plan)
    diagnostics = revalidate_build_plan_inputs(plan)
    if diagnostics:
        raise ImageProjectError(
            'Build inputs are no longer valid: {}'.format(', '.join(
                item.code for item in diagnostics)))
    if plan._live_config_path:
        _materialize_live_config(plan)
    if plan._capture_selection_path:
        _materialize_capture_selection(plan)
    diagnostics = revalidate_build_plan_inputs(plan)
    if diagnostics:
        raise ImageProjectError(
            'Build inputs changed during private materialization: {}'.format(
                ', '.join(item.code for item in diagnostics)))
    if plan._live_config_path:
        _materialize_live_config(plan)
    if plan._capture_selection_path:
        _materialize_capture_selection(plan)
    _validate_job_identity(plan)
    _validate_scratch_identity(plan)
    return tuple(plan.argv)


class VerificationResult(_Immutable):
    """Attestation emitted only by :func:`verify_iso`."""

    __slots__ = (
        'path', 'level', 'size', 'sha256', 'diagnostics', 'tree_paths',
        'boot_report', 'pvd_report', 'adapter_manifest_sha256', 'commands',
        'plan_id', 'artifact_device', 'artifact_inode', '_artifact_descriptor',
        '_plan_nonce', '_attestation_token', 'capture_summary',
        'customization_summary',
    )

    def __init__(self, path, level, size=0, sha256=None, diagnostics=(),
                 tree_paths=(), boot_report='', pvd_report='',
                 adapter_manifest_sha256=None, commands=(), plan_id=None,
                  artifact_device=None, artifact_inode=None,
                  artifact_descriptor=None, plan_nonce=None, capture_summary=None,
                  customization_summary=None,
                  _token=None):
        if _token is not _VERIFICATION_TOKEN:
            raise TypeError('VerificationResult objects are created by verify_iso')
        self.path = os.path.abspath(_path_string(path))
        self.level = level
        self.size = int(size or 0)
        self.sha256 = sha256
        self.diagnostics = tuple(diagnostics)
        self.tree_paths = tuple(tree_paths)
        self.boot_report = boot_report
        self.pvd_report = pvd_report
        self.adapter_manifest_sha256 = adapter_manifest_sha256
        self.commands = tuple(tuple(item) for item in commands)
        self.plan_id = plan_id
        self.artifact_device = artifact_device
        self.artifact_inode = artifact_inode
        self._artifact_descriptor = artifact_descriptor
        self._plan_nonce = plan_nonce
        self._attestation_token = _VERIFICATION_TOKEN
        self.capture_summary = _freeze(dict(
            capture_summary or {'requested': False}))
        self.customization_summary = _freeze(dict(
            customization_summary or {'requested': False}))
        self._lock()

    def __del__(self):
        descriptor = getattr(self, '_artifact_descriptor', None)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except (AttributeError, OSError):
                pass

    @property
    def errors(self):
        return tuple(item for item in self.diagnostics
                     if item.severity == 'error')

    @property
    def warnings(self):
        return tuple(item for item in self.diagnostics
                     if item.severity == 'warning')

    @property
    def structurally_verified(self):
        return self.level == VERIFICATION_STRUCTURAL

    def to_dict(self):
        return {
            'product_kind': VERIFICATION_KIND,
            'schema_version': 1,
            'path': self.path,
            'level': self.level,
            'size': self.size,
            'sha256': self.sha256,
            'diagnostics': [item.to_dict() for item in self.diagnostics],
            'tree_paths': list(self.tree_paths),
            'boot_report': self.boot_report,
            'pvd_report': self.pvd_report,
            'adapter_manifest_sha256': self.adapter_manifest_sha256,
            'commands': [list(item) for item in self.commands],
            'plan_id': self.plan_id,
            'artifact_device': self.artifact_device,
            'artifact_inode': self.artifact_inode,
            'capture_summary': _thaw(self.capture_summary),
            'customization_summary': _thaw(self.customization_summary),
        }


def _verification_result(plan, level, size=0, sha256=None, diagnostics=(),
                         tree_paths=(), boot_report='', pvd_report='',
                          adapter_manifest_sha256=None, commands=(),
                          artifact_stat=None, artifact_descriptor=None,
                          capture_summary=None, customization_summary=None):
    return VerificationResult(
        plan.partial_output_path or plan.output_path, level, size=size,
        sha256=sha256, diagnostics=diagnostics, tree_paths=tree_paths,
        boot_report=boot_report, pvd_report=pvd_report,
        adapter_manifest_sha256=adapter_manifest_sha256, commands=commands,
        plan_id=plan.plan_id,
        artifact_device=(int(artifact_stat.st_dev) if artifact_stat else None),
        artifact_inode=(int(artifact_stat.st_ino) if artifact_stat else None),
        artifact_descriptor=artifact_descriptor, plan_nonce=plan._nonce,
        capture_summary=capture_summary,
        customization_summary=customization_summary,
        _token=_VERIFICATION_TOKEN)


def _parse_xorriso_tree(stdout, stderr):
    paths = []
    seen = set()
    for line in (stdout + '\n' + stderr).splitlines():
        value = line.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not value.startswith('/'):
            continue
        value = re.sub(r'/+', '/', value)
        if value not in seen:
            seen.add(value)
            paths.append(value)
    return tuple(paths)


def _parse_report_lba(stdout, stderr):
    result = {}
    pattern = re.compile(
        r'^File data lba:\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*'
        r'(\d+)\s*,\s*[\'\"](/.*)[\'\"]\s*$')
    for line in (stdout + '\n' + stderr).splitlines():
        match = pattern.match(line.strip())
        if match:
            result[match.group(2)] = int(match.group(1))
    return result


def _parse_volume_id(report):
    for line in report.splitlines():
        match = re.search(r'Volume\s+id\s*:\s*[\'\"](.*)[\'\"]',
                          line, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _positive_boot_entries(report, platform):
    count = 0
    for line in report.splitlines():
        if ('El Torito boot img' in line and platform.lower() in line.lower() and
                re.search(r'\by\b', line, re.IGNORECASE)):
            count += 1
    return count


def _parse_session_capture_report(payload):
    value = _strict_json_object(payload, 'session capture report')
    expected = set((
        'product_kind', 'schema_version', 'profile', 'union_backend',
        'source_fingerprint', 'boot_id', 'base_module_fingerprint',
        'module_order', 'module', 'selection_sha256',
    ))
    _require_keys(value, expected, expected, 'session capture report')
    version = value.get('schema_version')
    if (value.get('product_kind') != SESSION_CAPTURE_REPORT_KIND or
            isinstance(version, bool) or
            version != SESSION_CAPTURE_REPORT_SCHEMA_VERSION):
        raise ProjectFormatError(
            'unsupported session capture report identity or schema')
    if value.get('profile') not in SESSION_CAPTURE_MODES:
        raise ProjectFormatError('invalid session capture profile')
    if value.get('union_backend') not in ('overlayfs', 'aufs', 'unknown'):
        raise ProjectFormatError('invalid session capture union backend')
    if not _is_sha256(value.get('source_fingerprint')):
        raise ProjectFormatError('invalid session source fingerprint')
    base_fingerprint = value.get('base_module_fingerprint')
    if base_fingerprint is not None and not _is_sha256(base_fingerprint):
        raise ProjectFormatError('invalid session base module fingerprint')
    boot_id = value.get('boot_id')
    if (not isinstance(boot_id, str) or not boot_id or len(boot_id) > 128 or
            '\x00' in boot_id or '\n' in boot_id or '\r' in boot_id):
        raise ProjectFormatError('invalid session boot identity')
    order = value.get('module_order')
    if isinstance(order, bool) or not isinstance(order, int) or order < 1:
        raise ProjectFormatError('invalid session module order')
    module = value.get('module')
    module_keys = set(('target', 'size', 'sha256'))
    _require_keys(module, module_keys, module_keys, 'session capture module')
    target = module.get('target')
    if (not isinstance(target, str) or
            not re.match(r'^minios/[0-9]+-session-changes[.]sb$', target)):
        raise ProjectFormatError('invalid session module target')
    size = module.get('size')
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ProjectFormatError('invalid session module size')
    if not _is_sha256(module.get('sha256')):
        raise ProjectFormatError('invalid session module digest')
    selection_digest = value.get('selection_sha256')
    if selection_digest is not None and not _is_sha256(selection_digest):
        raise ProjectFormatError('invalid session selection digest')
    return value


def _valid_customization_target(value, expression):
    return (isinstance(value, str) and value and '\x00' not in value and
            '\n' not in value and '\r' not in value and
            posixpath.normpath(value) == value and
            bool(re.match(expression, value)))


def _parse_customization_report(payload):
    value = _strict_json_object(payload, 'image customization report')
    top_keys = set(('product_kind', 'schema_version', 'boot', 'overlay'))
    _require_keys(value, top_keys, top_keys, 'image customization report')
    version = value.get('schema_version')
    if (value.get('product_kind') != IMAGE_CUSTOMIZATION_REPORT_KIND or
            isinstance(version, bool) or
            version != IMAGE_CUSTOMIZATION_REPORT_SCHEMA_VERSION):
        raise ProjectFormatError(
            'unsupported image customization report identity or schema')
    boot = value.get('boot')
    boot_keys = set((
        'timeout_seconds', 'default_boot', 'kernel_args', 'configs',
        'background',
    ))
    _require_keys(boot, boot_keys, boot_keys, 'image customization boot report')
    timeout = boot.get('timeout_seconds')
    if timeout is not None and not _is_strict_int(timeout, 0, 300):
        raise ProjectFormatError('invalid customization timeout')
    default_boot = boot.get('default_boot')
    if default_boot is not None and default_boot not in DEFAULT_BOOT_MODES:
        raise ProjectFormatError('invalid customization default boot mode')
    kernel = boot.get('kernel_args')
    if kernel is not None:
        kernel_keys = set(('bytes', 'sha256'))
        _require_keys(
            kernel, kernel_keys, kernel_keys,
            'image customization kernel argument report')
        if (not _is_strict_int(
                kernel.get('bytes'), 1, MAX_KERNEL_ARGUMENT_BYTES) or
                not _is_sha256(kernel.get('sha256'))):
            raise ProjectFormatError(
                'invalid customization kernel argument attestation')
    configs = boot.get('configs')
    if not isinstance(configs, list):
        raise ProjectFormatError('customization configs must be an array')
    config_keys = set(('target', 'size', 'sha256'))
    config_targets = []
    for index, item in enumerate(configs):
        _require_keys(
            item, config_keys, config_keys,
            'image customization config {}'.format(index))
        if (not _valid_customization_target(
                item.get('target'),
                r'^minios/boot/(?:grub|syslinux)/.+[.]cfg$') or
                not _is_strict_int(item.get('size')) or
                not _is_sha256(item.get('sha256'))):
            raise ProjectFormatError(
                'invalid customization config attestation')
        config_targets.append(item['target'])
    if (len(config_targets) != len(set(config_targets)) or
            config_targets != sorted(config_targets)):
        raise ProjectFormatError(
            'customization config targets are duplicate or unsorted')
    background = boot.get('background')
    if background is not None:
        background_keys = set((
            'width', 'height', 'size', 'sha256', 'targets',
        ))
        _require_keys(
            background, background_keys, background_keys,
            'image customization background report')
        targets = background.get('targets')
        if (not _is_strict_int(background.get('width'), 1, 8192) or
                not _is_strict_int(background.get('height'), 1, 8192) or
                not _is_strict_int(background.get('size'), 1) or
                not _is_sha256(background.get('sha256')) or
                not isinstance(targets, list) or
                any(not _valid_customization_target(
                    target, r'^minios/boot/bootlogo.*[.]png$')
                    for target in targets) or
                len(targets) != len(set(targets)) or
                targets != sorted(targets)):
            raise ProjectFormatError(
                'invalid customization background attestation')
    overlay = value.get('overlay')
    if overlay is not None:
        overlay_keys = set((
            'target', 'module_order', 'size', 'sha256',
            'input_tree_fingerprint', 'entry_count',
        ))
        _require_keys(
            overlay, overlay_keys, overlay_keys,
            'image customization overlay report')
        if (not _valid_customization_target(
                overlay.get('target'),
                r'^minios/[0-9]+-image-overlay[.]sb$') or
                not _is_strict_int(overlay.get('module_order')) or
                not _is_strict_int(overlay.get('size'), 1) or
                not _is_sha256(overlay.get('sha256')) or
                not _is_sha256(overlay.get('input_tree_fingerprint')) or
                not _is_strict_int(overlay.get('entry_count'), 1)):
            raise ProjectFormatError(
                'invalid customization overlay attestation')
    return value


def _read_bounded_extracted_file(path, maximum_bytes, context):
    file_stat = os.lstat(path)
    if (stat.S_ISLNK(file_stat.st_mode) or
            not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0):
        raise ImageProjectError(
            '{} is unsafe or empty'.format(context))
    if file_stat.st_size > maximum_bytes:
        raise ImageProjectError('{} is unexpectedly large'.format(context))
    return _read_stable_regular_bytes(path, maximum_bytes)


def _read_private_job_file(plan, path, maximum_bytes, context):
    basename = _private_job_basename(plan, path)
    job_descriptor = _duplicate_job_descriptor(plan)
    descriptor = None
    try:
        observed = _entry_metadata(job_descriptor, basename)
        if (observed is None or stat.S_ISLNK(observed.st_mode) or
                not stat.S_ISREG(observed.st_mode) or observed.st_size <= 0 or
                observed.st_size > maximum_bytes or
                stat.S_IMODE(observed.st_mode) != 0o600 or
                observed.st_nlink != 1 or
                (hasattr(os, 'geteuid') and
                 observed.st_uid != os.geteuid())):
            raise ImageProjectError(
                '{} is unsafe or empty'.format(context))
        flags = os.O_RDONLY
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
        descriptor = os.open(basename, flags, dir_fd=job_descriptor)
        opened = os.fstat(descriptor)
        if _metadata_snapshot(opened) != _metadata_snapshot(observed):
            raise ImageProjectError(
                '{} changed while opening'.format(context))
        chunks = []
        total = 0
        while True:
            block = os.read(descriptor, HASH_CHUNK_SIZE)
            if not block:
                break
            total += len(block)
            if total > maximum_bytes:
                raise ImageProjectError(
                    '{} is unexpectedly large'.format(context))
            chunks.append(block)
        final_metadata = os.fstat(descriptor)
        if (_metadata_snapshot(final_metadata) !=
                _metadata_snapshot(opened) or total != opened.st_size):
            raise ImageProjectError(
                '{} changed while reading'.format(context))
        _validate_job_identity(plan)
        return b''.join(chunks), final_metadata
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(job_descriptor)


def _verify_embedded_build_manifest(plan, iso_path, runner, xorriso,
                                    commands, diagnostics):
    extraction_directory = None
    extraction_identity = None
    try:
        _validate_job_identity(plan)
        extraction_directory = tempfile.mkdtemp(
            prefix='manifest-verify-', dir=plan.job_directory)
        os.chmod(extraction_directory, 0o700)
        extraction_stat = os.lstat(extraction_directory)
        if (stat.S_ISLNK(extraction_stat.st_mode) or
                not stat.S_ISDIR(extraction_stat.st_mode) or
                stat.S_IMODE(extraction_stat.st_mode) != 0o700):
            raise ImageProjectError(
                'manifest extraction directory is not private')
        extraction_identity = _identity(extraction_stat)
        destination = os.path.join(
            extraction_directory, 'build-manifest.json')
        command = [
            xorriso, '-no_rc', '-osirrox', 'on', '-indev', iso_path,
            '-extract', '/minios/build-manifest.json', destination, '-end',
        ]
        commands.append(command)
        returncode, stdout, stderr = _run_command(runner, command)
        if returncode != 0:
            raise ImageProjectError(
                'xorriso build manifest extraction failed{}.'.format(
                    ': ' + (stderr or stdout).strip()
                    if (stderr or stdout).strip() else ''))
        payload, unused_stat = _read_bounded_extracted_file(
            destination, MAX_BUILD_MANIFEST_BYTES,
            'embedded build manifest')
        if payload != plan.manifest_payload:
            raise ImageProjectError(
                'embedded build manifest differs from the canonical plan')
        parsed = _strict_json_object(payload, 'embedded build manifest')
        if parsed != plan.manifest:
            raise ImageProjectError(
                'embedded build manifest differs from the plan')
    except (OSError, TypeError, ValueError, ProjectFormatError,
            ImageProjectError) as error:
        diagnostics.append(Diagnostic(
            'error', 'build_manifest_attestation_failed', str(error)))
    finally:
        try:
            _cleanup_private_extraction(
                extraction_directory, plan, extraction_identity)
        except (OSError, ImageProjectError) as error:
            diagnostics.append(Diagnostic(
                'error', 'private_extraction_cleanup_failed', str(error)))


def _clean_directory_descriptor(descriptor):
    try:
        names = os.listdir(descriptor)
    except OSError as error:
        raise ImageProjectError(
            'cannot list private extraction directory: {}'.format(error))
    for name in names:
        try:
            file_stat = os.stat(
                name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(file_stat.st_mode):
                flags = os.O_RDONLY
                if hasattr(os, 'O_DIRECTORY'):
                    flags |= os.O_DIRECTORY
                if hasattr(os, 'O_NOFOLLOW'):
                    flags |= os.O_NOFOLLOW
                child = os.open(name, flags, dir_fd=descriptor)
                try:
                    if _identity(os.fstat(child)) != _identity(file_stat):
                        raise ImageProjectError(
                            'private extraction directory identity changed')
                    _clean_directory_descriptor(child)
                finally:
                    os.close(child)
                current = os.stat(
                    name, dir_fd=descriptor, follow_symlinks=False)
                if _identity(current) == _identity(file_stat):
                    os.rmdir(name, dir_fd=descriptor)
                else:
                    raise ImageProjectError(
                        'private extraction directory identity changed')
            else:
                os.unlink(name, dir_fd=descriptor)
        except ImageProjectError:
            raise
        except OSError as error:
            raise ImageProjectError(
                'cannot clean private extraction: {}'.format(error))


def _cleanup_private_extraction(directory, plan, expected_identity=None):
    if not directory:
        return
    if (os.path.dirname(directory) != plan.job_directory or
            os.path.basename(directory) in ('', '.', '..')):
        raise ImageProjectError(
            'private extraction is outside the retained job')
    basename = os.path.basename(directory)
    job_descriptor = _duplicate_job_descriptor(plan)
    descriptor = None
    try:
        observed = _entry_metadata(job_descriptor, basename)
        if observed is None:
            if expected_identity is None:
                return
            raise ImageProjectError('private extraction disappeared')
        if (not stat.S_ISDIR(observed.st_mode) or
                stat.S_ISLNK(observed.st_mode) or
                stat.S_IMODE(observed.st_mode) != 0o700 or
                (expected_identity is not None and
                 _identity(observed) != expected_identity)):
            raise ImageProjectError(
                'private extraction directory identity changed')
        descriptor = os.open(
            basename, _directory_open_flags(), dir_fd=job_descriptor)
        directory_stat = os.fstat(descriptor)
        if (_identity(directory_stat) != _identity(observed) or
                not stat.S_ISDIR(directory_stat.st_mode)):
            raise ImageProjectError(
                'private extraction directory changed while opening')
        _clean_directory_descriptor(descriptor)
        os.close(descriptor)
        descriptor = None
        final_stat = os.stat(
            basename, dir_fd=job_descriptor, follow_symlinks=False)
        if _identity(final_stat) != _identity(directory_stat):
            raise ImageProjectError(
                'private extraction directory identity changed')
        os.rmdir(basename, dir_fd=job_descriptor)
        os.fsync(job_descriptor)
        if _entry_metadata(job_descriptor, basename) is not None:
            raise ImageProjectError(
                'private extraction directory still exists')
        _validate_job_identity(plan)
    except ImageProjectError:
        raise
    except OSError as error:
        raise ImageProjectError(
            'cannot clean private extraction: {}'.format(error))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(job_descriptor)


def _verify_image_customization(plan, iso_path, runner, xorriso, unsquashfs,
                                commands, diagnostics):
    summary = {'requested': True}
    extraction_directory = None
    extraction_identity = None
    try:
        _validate_job_identity(plan)
        extraction_directory = tempfile.mkdtemp(
            prefix='customization-verify-', dir=plan.job_directory)
        os.chmod(extraction_directory, 0o700)
        extraction_stat = os.lstat(extraction_directory)
        if (stat.S_ISLNK(extraction_stat.st_mode) or
                not stat.S_ISDIR(extraction_stat.st_mode) or
                stat.S_IMODE(extraction_stat.st_mode) != 0o700):
            raise ImageProjectError(
                'customization extraction directory is not private')
        extraction_identity = _identity(extraction_stat)
        manifest = plan.manifest
        intent = manifest['customization']
        expected_iso = manifest['expected_iso']['image_customization']
        report = None
        report_path = None
        if intent['adapter_report_requested']:
            report_path = os.path.join(
                extraction_directory, 'image-customization.json')
            report_command = [
                xorriso, '-no_rc', '-osirrox', 'on', '-indev', iso_path,
                '-extract', '/minios/image-customization.json', report_path,
                '-end',
            ]
            commands.append(report_command)
            returncode, stdout, stderr = _run_command(runner, report_command)
            if returncode != 0:
                raise ImageProjectError(
                    'xorriso customization report extraction failed{}.'.format(
                        ': ' + (stderr or stdout).strip()
                        if (stderr or stdout).strip() else ''))
            report_payload, unused_report_stat = _read_bounded_extracted_file(
                report_path, MAX_CUSTOMIZATION_REPORT_BYTES,
                'image customization report')
            report = _parse_customization_report(report_payload)
            report_boot = report['boot']
            expected_boot = intent['boot']
            if (report_boot['timeout_seconds'] !=
                    expected_boot['timeout_seconds'] or
                    report_boot['default_boot'] !=
                    expected_boot['default_boot'] or
                    report_boot['kernel_args'] !=
                    expected_boot['kernel_args']):
                raise ImageProjectError(
                    'customization boot intent differs from the plan')
            report_config_targets = [
                item['target'] for item in report_boot['configs']]
            config_count, config_target_digest = (
                customization_target_set_identity(report_config_targets))
            if (config_count != expected_boot['config_target_count'] or
                    config_target_digest !=
                    expected_boot['config_target_set_sha256'] or
                    report_config_targets !=
                    expected_iso['boot_config_targets'] or
                    report_boot['configs'] !=
                    expected_boot['expected_configs']):
                raise ImageProjectError(
                    'customized boot configs differ from the planned transform')
            expected_background = expected_boot['background']
            report_background = report_boot['background']
            if expected_background is None:
                if report_background is not None:
                    raise ImageProjectError(
                        'unexpected boot background attestation')
            else:
                if report_background is None:
                    raise ImageProjectError(
                        'boot background attestation is missing')
                report_background_base = dict(
                    (key, report_background[key])
                    for key in ('width', 'height', 'size', 'sha256'))
                expected_background_base = dict(
                    (key, expected_background[key])
                    for key in ('width', 'height', 'size', 'sha256'))
                target_count, target_digest = (
                    customization_target_set_identity(
                        report_background['targets']))
                if (report_background_base != expected_background_base or
                        target_count != expected_background['target_count'] or
                        target_digest !=
                        expected_background['target_set_sha256'] or
                        report_background['targets'] !=
                        expected_iso['background_targets']):
                    raise ImageProjectError(
                        'boot background attestation differs from the plan')
            expected_overlay = intent['overlay']
            report_overlay = report['overlay']
            if expected_overlay['requested']:
                if report_overlay is None:
                    raise ImageProjectError(
                        'image overlay attestation is missing')
                if (report_overlay['target'] !=
                        expected_overlay['module_target'] or
                        report_overlay['module_order'] !=
                        expected_overlay['module_order'] or
                        report_overlay['input_tree_fingerprint'] !=
                        expected_overlay['input_tree_fingerprint'] or
                        report_overlay['entry_count'] !=
                        expected_overlay['entry_count']):
                    raise ImageProjectError(
                        'image overlay attestation differs from the plan')
            elif report_overlay is not None:
                raise ImageProjectError('unexpected image overlay attestation')

        extractions = []
        if intent['live_config']['requested']:
            extractions.append((
                'live-config', 'minios/config.conf',
                os.path.join(extraction_directory, 'live-config.conf'), None))
        if report is not None:
            for index, item in enumerate(report['boot']['configs']):
                extractions.append((
                    'boot-config', item['target'],
                    os.path.join(
                        extraction_directory,
                        'boot-config-{:04d}.cfg'.format(index)), item))
            background = report['boot']['background']
            if background is not None:
                for index, target in enumerate(background['targets']):
                    extractions.append((
                        'background', target,
                        os.path.join(
                            extraction_directory,
                            'background-{:04d}.png'.format(index)),
                        background))
            if report['overlay'] is not None:
                extractions.append((
                    'overlay', report['overlay']['target'],
                    os.path.join(extraction_directory, 'image-overlay.sb'),
                    report['overlay']))
        if extractions:
            extract_command = [
                xorriso, '-no_rc', '-osirrox', 'on', '-indev', iso_path,
            ]
            for unused_kind, target, destination, unused_record in extractions:
                extract_command.extend((
                    '-extract', '/' + target.lstrip('/'), destination))
            extract_command.append('-end')
            commands.append(extract_command)
            returncode, stdout, stderr = _run_command(runner, extract_command)
            if returncode != 0:
                raise ImageProjectError(
                    'xorriso customization input extraction failed{}.'.format(
                        ': ' + (stderr or stdout).strip()
                        if (stderr or stdout).strip() else ''))

        overlay_module_summary = None
        expected_background_metadata = intent['boot']['background']
        for kind, unused_target, destination, record in extractions:
            file_stat = os.lstat(destination)
            if (stat.S_ISLNK(file_stat.st_mode) or
                    not stat.S_ISREG(file_stat.st_mode) or
                    file_stat.st_size <= 0):
                raise ImageProjectError(
                    'extracted customization input is unsafe or empty')
            digest, opened_stat = _secure_hash_regular(destination, file_stat)
            if kind == 'live-config':
                if (opened_stat.st_size != intent['live_config'][
                        'rendered_size'] or
                        digest != intent['live_config']['rendered_sha256']):
                    raise ImageProjectError(
                        'embedded live config differs from rendered intent')
            elif kind == 'boot-config':
                if (opened_stat.st_size != record['size'] or
                        digest != record['sha256']):
                    raise ImageProjectError(
                        'embedded boot config differs from its attestation')
            elif kind == 'background':
                if opened_stat.st_size > MAX_LIVE_CONFIG_BYTES:
                    raise ImageProjectError(
                        'embedded boot background is unexpectedly large')
                payload, unused_background_stat = _read_stable_regular_bytes(
                    destination, MAX_LIVE_CONFIG_BYTES)
                metadata = _png_metadata_from_payload(payload)
                expected_base = dict(
                    (key, expected_background_metadata[key])
                    for key in ('width', 'height', 'size', 'sha256'))
                if metadata != expected_base:
                    raise ImageProjectError(
                        'embedded boot background differs from input intent')
            elif kind == 'overlay':
                if (opened_stat.st_size != record['size'] or
                        digest != record['sha256']):
                    raise ImageProjectError(
                        'embedded image overlay differs from its attestation')
                squashfs_command = [unsquashfs, '-s', destination]
                commands.append(squashfs_command)
                valid, detail = validate_squashfs(
                    destination, runner=runner, unsquashfs=unsquashfs)
                if not valid:
                    raise ImageProjectError(
                        'embedded image overlay is not valid SquashFS{}.'.format(
                            ': ' + detail if detail else ''))
                overlay_tree = os.path.join(
                    extraction_directory, 'image-overlay-tree')
                unsquash_command = [
                    unsquashfs, '-d', overlay_tree, destination,
                ]
                commands.append(unsquash_command)
                returncode, stdout, stderr = _run_command(
                    runner, unsquash_command)
                if returncode != 0:
                    raise ImageProjectError(
                        'cannot extract embedded image overlay{}.'.format(
                            ': ' + (stderr or stdout).strip()
                            if (stderr or stdout).strip() else ''))
                actual_inventory = _overlay_inventory(overlay_tree)
                planned_inventory = _thaw(plan._overlay_inventory)
                if _overlay_content_records(actual_inventory['records']) != (
                        _overlay_content_records(
                            planned_inventory['records'])):
                    raise ImageProjectError(
                        'embedded image overlay tree differs from planned input')
                overlay_module_summary = {
                    'module_order': record['module_order'],
                    'module_target': record['target'],
                    'module_size': record['size'],
                    'module_sha256': digest,
                    'input_tree_fingerprint': record[
                        'input_tree_fingerprint'],
                    'entry_count': record['entry_count'],
                }

        boot_summary = {
            'requested': intent['boot']['requested'],
            'timeout_seconds': intent['boot']['timeout_seconds'],
            'default_boot': intent['boot']['default_boot'],
            'kernel_args': intent['boot']['kernel_args'],
            'config_target_count': intent['boot']['config_target_count'],
            'config_target_set_sha256': intent['boot'][
                'config_target_set_sha256'],
            'background': intent['boot']['background'],
        }
        summary = {
            'requested': True,
            'adapter_report_verified': bool(report is not None),
            'live_config': intent['live_config'],
            'boot': boot_summary,
            'overlay': overlay_module_summary,
        }
    except (OSError, TypeError, ValueError, ProjectFormatError,
            ImageProjectError) as error:
        diagnostics.append(Diagnostic(
            'error', 'image_customization_attestation_failed', str(error)))
    finally:
        try:
            _cleanup_private_extraction(
                extraction_directory, plan, extraction_identity)
        except (OSError, ImageProjectError) as error:
            diagnostics.append(Diagnostic(
                'error', 'private_extraction_cleanup_failed', str(error)))
    return summary


def _verify_session_capture(plan, iso_path, capture_iso_path, runner,
                            xorriso, unsquashfs, commands, diagnostics):
    summary = {'requested': True}
    extraction_directory = None
    extraction_identity = None
    try:
        _validate_job_identity(plan)
        extraction_directory = tempfile.mkdtemp(
            prefix='capture-verify-', dir=plan.job_directory)
        os.chmod(extraction_directory, 0o700)
        extract_stat = os.lstat(extraction_directory)
        if (not stat.S_ISDIR(extract_stat.st_mode) or
                stat.S_ISLNK(extract_stat.st_mode) or
                stat.S_IMODE(extract_stat.st_mode) != 0o700):
            raise ImageProjectError(
                'capture extraction directory is not private')
        extraction_identity = _identity(extract_stat)
        report_path = os.path.join(
            extraction_directory, 'session-capture.json')
        module_path = os.path.join(extraction_directory, 'capture.sb')
        extraction_command = [
            xorriso, '-no_rc', '-osirrox', 'on', '-indev', iso_path,
            '-extract', '/minios/session-capture.json', report_path,
            '-extract', capture_iso_path, module_path, '-end',
        ]
        commands.append(extraction_command)
        returncode, stdout, stderr = _run_command(
            runner, extraction_command)
        if returncode != 0:
            raise ImageProjectError(
                'xorriso capture extraction failed{}.'.format(
                    ': ' + (stderr or stdout).strip()
                    if (stderr or stdout).strip() else ''))
        report_stat = os.lstat(report_path)
        module_stat = os.lstat(module_path)
        for extracted_path, file_stat in (
                (report_path, report_stat), (module_path, module_stat)):
            if (stat.S_ISLNK(file_stat.st_mode) or
                    not stat.S_ISREG(file_stat.st_mode) or
                    file_stat.st_size <= 0):
                raise ImageProjectError(
                    'xorriso extracted an unsafe or empty capture file: '
                    '{}'.format(os.path.basename(extracted_path)))
        if report_stat.st_size > 1024 * 1024:
            raise ImageProjectError('session capture report is too large')
        report_flags = os.O_RDONLY
        if hasattr(os, 'O_NOFOLLOW'):
            report_flags |= os.O_NOFOLLOW
        report_descriptor = os.open(report_path, report_flags)
        try:
            opened_report_stat = os.fstat(report_descriptor)
            if (_identity(opened_report_stat) != _identity(report_stat) or
                    not stat.S_ISREG(opened_report_stat.st_mode)):
                raise ImageProjectError(
                    'session capture report changed while opening')
            report_chunks = []
            report_bytes = 0
            while True:
                block = os.read(report_descriptor, 64 * 1024)
                if not block:
                    break
                report_bytes += len(block)
                if report_bytes > 1024 * 1024:
                    raise ImageProjectError(
                        'session capture report is too large')
                report_chunks.append(block)
            report_payload = b''.join(report_chunks)
            final_report_stat = os.fstat(report_descriptor)
            if (report_bytes != opened_report_stat.st_size or
                    opened_report_stat.st_size != final_report_stat.st_size or
                    _stat_mtime_ns(opened_report_stat) !=
                    _stat_mtime_ns(final_report_stat)):
                raise ImageProjectError(
                    'session capture report changed while reading')
        finally:
            os.close(report_descriptor)
        report = _parse_session_capture_report(report_payload)
        module_digest, hashed_module_stat = _secure_hash_regular(
            module_path, module_stat)
        module_stat = hashed_module_stat
        module = report['module']
        expected_capture = plan.manifest['expected_iso']['session_capture']
        if report['profile'] != plan.manifest['capture']['mode']:
            raise ImageProjectError('session capture profile mismatch')
        if report['base_module_fingerprint'] != (
                plan._expected_base_module_fingerprint):
            raise ImageProjectError('session base module fingerprint mismatch')
        if report['module_order'] != expected_capture['module_order']:
            raise ImageProjectError('session capture module order mismatch')
        if module['target'] != expected_capture['module_target']:
            raise ImageProjectError('session capture module target mismatch')
        if (module['size'] != module_stat.st_size or
                module['sha256'] != module_digest):
            raise ImageProjectError(
                'session capture report does not match extracted module bytes')
        expected_selection = plan._capture_selection_digest
        if report['selection_sha256'] != expected_selection:
            raise ImageProjectError('session selection digest mismatch')
        inventory = plan._session_inventory
        if inventory is not None:
            if report['source_fingerprint'] != inventory.source_fingerprint:
                raise ImageProjectError(
                    'session inventory source fingerprint mismatch')
            if report['union_backend'] != inventory.union_backend:
                raise ImageProjectError('session inventory union mismatch')
        unsquash_command = [unsquashfs, '-s', module_path]
        commands.append(unsquash_command)
        valid, detail = validate_squashfs(
            module_path, runner=runner, unsquashfs=unsquashfs)
        if not valid:
            raise ImageProjectError(
                'captured module is not valid SquashFS{}.'.format(
                    ': ' + detail if detail else ''))
        summary = {
            'requested': True,
            'profile': report['profile'],
            'union_backend': report['union_backend'],
            'source_fingerprint': report['source_fingerprint'],
            'base_module_fingerprint': report['base_module_fingerprint'],
            'module_order': report['module_order'],
            'module_target': module['target'],
            'module_size': module['size'],
            'module_sha256': module_digest,
            'selection_sha256': report['selection_sha256'],
        }
    except (OSError, TypeError, ValueError, ProjectFormatError,
            ImageProjectError) as error:
        diagnostics.append(Diagnostic(
            'error', 'session_capture_attestation_failed', str(error)))
    finally:
        try:
            _cleanup_private_extraction(
                extraction_directory, plan, extraction_identity)
        except (OSError, ImageProjectError) as error:
            diagnostics.append(Diagnostic(
                'error', 'private_extraction_cleanup_failed', str(error)))
    return summary


def verify_iso(plan, runner=None, xorriso=None, unsquashfs=None):
    """Verify the exact artifact and expectations bound into ``plan``."""
    if not isinstance(plan, BuildPlan) or not plan.buildable:
        raise TypeError('a buildable BuildPlan is required')
    path = plan.partial_output_path
    diagnostics = []
    commands = []
    capture_summary = {'requested': plan.capture_requested}
    customization_summary = {'requested': plan.customization_requested}
    try:
        _validate_job_identity(plan)
    except (OSError, ImageProjectError) as error:
        diagnostics.append(Diagnostic(
            'error', 'job_identity_changed', str(error), plan.job_directory))
        return _verification_result(
            plan, VERIFICATION_NOT_BUILT, diagnostics=diagnostics,
            capture_summary=capture_summary,
            customization_summary=customization_summary)
    try:
        file_stat = os.lstat(path)
    except OSError:
        diagnostics.append(Diagnostic(
            'error', 'output_missing', 'Partial ISO does not exist.', path))
        return _verification_result(
            plan, VERIFICATION_NOT_BUILT, diagnostics=diagnostics,
            capture_summary=capture_summary,
            customization_summary=customization_summary)
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        diagnostics.append(Diagnostic(
            'error', 'output_not_regular',
            'Partial ISO must be a non-symlink regular file.', path))
        return _verification_result(
            plan, VERIFICATION_NOT_BUILT, diagnostics=diagnostics,
            capture_summary=capture_summary,
            customization_summary=customization_summary)
    if file_stat.st_size <= 0:
        diagnostics.append(Diagnostic(
            'error', 'output_empty', 'Partial ISO is empty.', path))
        return _verification_result(
            plan, VERIFICATION_NOT_BUILT, diagnostics=diagnostics,
            capture_summary=capture_summary,
            customization_summary=customization_summary)

    adapter_manifest_sha256 = None
    try:
        adapter_manifest_payload, unused_manifest_stat = (
            _read_private_job_file(
                plan, plan.adapter_manifest_path, MAX_BUILD_MANIFEST_BYTES,
                'adapter manifest'))
        if adapter_manifest_payload != plan.manifest_payload:
            raise ImageProjectError(
                'adapter manifest is not the canonical build plan')
        adapter_manifest_data = _strict_json_object(
            adapter_manifest_payload, 'adapter manifest')
        if adapter_manifest_data != plan.manifest:
            raise ImageProjectError('adapter manifest differs from the plan')
        adapter_manifest_sha256 = hashlib.sha256(
            adapter_manifest_payload).hexdigest()
    except (OSError, ValueError, ProjectFormatError,
            ImageProjectError) as error:
        diagnostics.append(Diagnostic(
            'error', 'adapter_manifest_invalid', str(error),
            plan.adapter_manifest_path))

    xorriso = xorriso or _tool_path(
        _thaw(plan._tool_capabilities), 'xorriso') or 'xorriso'
    tree_command = [xorriso, '-indev', path, '-find', '/']
    type_command = [xorriso, '-indev', path, '-find', '/', '-type', 'f',
                    '-exec', 'report_lba', '--']
    link_command = [xorriso, '-indev', path, '-find', '/', '-type', 'l']
    boot_command = [xorriso, '-indev', path,
                    '-report_el_torito', 'plain']
    pvd_command = [xorriso, '-indev', path, '-pvd_info']
    commands.extend((tree_command, type_command, link_command,
                     boot_command, pvd_command))
    outputs = []
    for code, command in (
            ('xorriso_tree_failed', tree_command),
            ('xorriso_file_report_failed', type_command),
            ('xorriso_symlink_report_failed', link_command),
            ('xorriso_boot_report_failed', boot_command),
            ('xorriso_pvd_report_failed', pvd_command)):
        try:
            returncode, stdout, stderr = _run_command(runner, command)
        except (OSError, TypeError) as error:
            returncode, stdout, stderr = -1, '', str(error)
        outputs.append((returncode, stdout, stderr))
        if returncode != 0:
            detail = (stderr or stdout).strip()
            diagnostics.append(Diagnostic(
                'error', code, 'xorriso inspection failed{}.'.format(
                    ': ' + detail if detail else ''), path))
    tree_rc, tree_stdout, tree_stderr = outputs[0]
    type_rc, type_stdout, type_stderr = outputs[1]
    link_rc, link_stdout, link_stderr = outputs[2]
    boot_rc, boot_stdout, boot_stderr = outputs[3]
    pvd_rc, pvd_stdout, pvd_stderr = outputs[4]
    tree_paths = _parse_xorriso_tree(tree_stdout, tree_stderr)
    path_set = set(item.rstrip('/') or '/' for item in tree_paths)
    file_sizes = _parse_report_lba(type_stdout, type_stderr)
    symlink_paths = set(_parse_xorriso_tree(link_stdout, link_stderr))
    boot_report = (boot_stdout + '\n' + boot_stderr).strip()
    pvd_report = (pvd_stdout + '\n' + pvd_stderr).strip()
    expected = plan.manifest['expected_iso']
    expected_capture = expected['session_capture']
    expected_customization = expected['image_customization']
    build_manifest_path = '/' + expected['build_manifest_target'].lstrip('/')
    capture_report_path = '/minios/session-capture.json'
    capture_layer_pattern = re.compile(
        r'^/minios/(?:.*/)?[0-9]+-session-changes[.]sb$')
    capture_layer_paths = sorted(
        item for item in path_set if capture_layer_pattern.match(item))
    customization_report_path = '/minios/image-customization.json'
    overlay_layer_pattern = re.compile(
        r'^/minios/(?:.*/)?[0-9]+-image-overlay[.]sb$')
    overlay_layer_paths = sorted(
        item for item in path_set if overlay_layer_pattern.match(item))

    required_targets = []
    required_targets.extend(expected['module_targets'])
    required_targets.append(expected['config_target'])
    required_targets.append(expected['build_manifest_target'])
    required_targets.extend(expected['required_boot_targets'])
    required_targets.extend(expected['kernel_targets'])
    required_targets.extend(expected['initramfs_targets'])
    required_targets.extend(expected['menu_targets'])
    if expected_capture['requested']:
        required_targets.append(expected_capture['report_target'])
        required_targets.append(expected_capture['module_target'])
    if expected_customization['adapter_report_requested']:
        required_targets.append(expected_customization['report_target'])
    required_targets.extend(expected_customization['boot_config_targets'])
    required_targets.extend(expected_customization['background_targets'])
    if expected_customization['overlay_requested']:
        required_targets.append(expected_customization['overlay_target'])
    required_targets = sorted(set('/' + item.lstrip('/')
                                  for item in required_targets))
    for target in required_targets:
        if target not in path_set:
            diagnostics.append(Diagnostic(
                'error', 'expected_iso_path_missing',
                'Expected ISO path is missing: {}'.format(target), target))
    if tree_rc == 0 and build_manifest_path not in path_set:
        diagnostics.append(Diagnostic(
            'error', 'build_manifest_missing',
            'The canonical build manifest is missing from the ISO.',
            build_manifest_path))
    for target in expected['forbidden_module_targets']:
        target = '/' + target.lstrip('/')
        if target in path_set:
            diagnostics.append(Diagnostic(
                'error', 'deselected_module_present',
                'Deselected module is present: {}'.format(target), target))

    if expected_customization['adapter_report_requested']:
        if customization_report_path not in path_set:
            diagnostics.append(Diagnostic(
                'error', 'image_customization_report_missing',
                'Requested image customization report is missing.',
                customization_report_path))
    elif customization_report_path in path_set:
        diagnostics.append(Diagnostic(
            'error', 'unexpected_image_customization_report',
            'ISO unexpectedly contains an image customization report.',
            customization_report_path))
    expected_overlay_path = None
    if expected_customization['overlay_requested']:
        expected_overlay_path = '/' + expected_customization[
            'overlay_target'].lstrip('/')
        if overlay_layer_paths != [expected_overlay_path]:
            diagnostics.append(Diagnostic(
                'error', 'image_overlay_module_set_mismatch',
                'ISO must contain exactly the planned dynamic image overlay.'))
        overlay_order = expected_customization['overlay_order']
        capture_path_for_order = (
            '/' + expected_capture['module_target'].lstrip('/')
            if expected_capture['requested'] else None)
        for candidate in path_set:
            if (candidate in (expected_overlay_path, capture_path_for_order) or
                    not re.match(r'^/minios/(?:.*/)?[^/]+[.]sb$', candidate)):
                continue
            order_match = re.match(
                r'^([0-9]+)', posixpath.basename(candidate))
            if (order_match and
                    (len(order_match.group(1)) > 6 or
                     int(order_match.group(1)) >= overlay_order)):
                diagnostics.append(Diagnostic(
                    'error', 'image_overlay_not_after_static_modules',
                    'A static module has an order at or above the planned '
                    'image overlay.', candidate))
    elif overlay_layer_paths:
        diagnostics.append(Diagnostic(
            'error', 'unexpected_image_overlay_module',
            'ISO unexpectedly contains an image overlay module.'))

    capture_iso_path = None
    if expected_capture['requested']:
        capture_iso_path = '/' + expected_capture['module_target'].lstrip('/')
        if capture_report_path not in path_set:
            diagnostics.append(Diagnostic(
                'error', 'session_capture_report_missing',
                'Requested session capture report is missing.',
                capture_report_path))
        if capture_layer_paths != [capture_iso_path]:
            diagnostics.append(Diagnostic(
                'error', 'session_capture_module_set_mismatch',
                'ISO must contain exactly the planned dynamic session layer.'))
        expected_order = expected_capture['module_order']
        for candidate in path_set:
            if (candidate == capture_iso_path or
                    not re.match(r'^/minios/(?:.*/)?[^/]+[.]sb$',
                                 candidate)):
                continue
            order_match = re.match(r'^([0-9]+)',
                                   posixpath.basename(candidate))
            if (order_match and
                    (len(order_match.group(1)) > 6 or
                     int(order_match.group(1)) >= expected_order)):
                diagnostics.append(Diagnostic(
                    'error', 'session_capture_not_last_module',
                    'A module has an order at or above the planned final '
                    'session layer.', candidate))
    else:
        if capture_report_path in path_set:
            diagnostics.append(Diagnostic(
                'error', 'unexpected_session_capture_report',
                'No-capture ISO unexpectedly contains a session report.',
                capture_report_path))
        if capture_layer_paths:
            diagnostics.append(Diagnostic(
                'error', 'unexpected_session_capture_module',
                'No-capture ISO unexpectedly contains a session layer.'))

    expected_type_by_path = {}
    for item in plan.manifest['input_digests']['source_files']:
        expected_type_by_path['/minios/' + item['relative_path']] = item['type']
    for target in required_targets:
        expected_type = expected_type_by_path.get(target, 'file')
        if type_rc == 0 and expected_type == 'file':
            if target not in file_sizes:
                diagnostics.append(Diagnostic(
                    'error', 'expected_regular_file_unobserved',
                    'Expected regular file type was not observed: {}'.format(
                        target), target))
            elif file_sizes[target] <= 0:
                diagnostics.append(Diagnostic(
                    'error', 'expected_file_empty',
                    'Expected ISO file is empty: {}'.format(target), target))
        elif expected_type == 'symlink' and link_rc == 0:
            if target not in symlink_paths:
                diagnostics.append(Diagnostic(
                    'error', 'expected_symlink_unobserved',
                    'Expected ISO symlink type was not observed: {}'.format(
                        target), target))
            if target in file_sizes:
                diagnostics.append(Diagnostic(
                    'error', 'expected_symlink_is_regular_file',
                    'Expected symlink was emitted as a regular file: {}'.format(
                        target), target))

    if (tree_rc == 0 and type_rc == 0 and
            build_manifest_path in path_set and
            file_sizes.get(build_manifest_path, 0) > 0):
        _verify_embedded_build_manifest(
            plan, path, runner, xorriso, commands, diagnostics)

    customization_required_targets = {
        '/' + expected['config_target'].lstrip('/')}
    if expected_customization['adapter_report_requested']:
        customization_required_targets.add(
            '/' + expected_customization['report_target'].lstrip('/'))
    customization_required_targets.update(
        '/' + item.lstrip('/')
        for item in expected_customization['boot_config_targets'])
    customization_required_targets.update(
        '/' + item.lstrip('/')
        for item in expected_customization['background_targets'])
    if expected_customization['overlay_requested']:
        customization_required_targets.add(
            '/' + expected_customization['overlay_target'].lstrip('/'))
    customization_targets_ready = all(
        target in path_set and file_sizes.get(target, 0) > 0
        for target in customization_required_targets)
    if (expected_customization['requested'] and tree_rc == 0 and type_rc == 0 and
            customization_targets_ready and
            (not expected_customization['adapter_report_requested'] or
             customization_report_path in path_set) and
            (not expected_customization['overlay_requested'] or
             overlay_layer_paths == [expected_overlay_path])):
        unsquashfs_path = unsquashfs or _tool_path(
            _thaw(plan._tool_capabilities), 'unsquashfs') or 'unsquashfs'
        customization_summary = _verify_image_customization(
            plan, path, runner, xorriso, unsquashfs_path, commands,
            diagnostics)

    if (expected_capture['requested'] and tree_rc == 0 and type_rc == 0 and
            capture_report_path in path_set and
            capture_layer_paths == [capture_iso_path] and
            file_sizes.get(capture_report_path, 0) > 0 and
            file_sizes.get(capture_iso_path, 0) > 0):
        unsquashfs = unsquashfs or _tool_path(
            _thaw(plan._tool_capabilities), 'unsquashfs') or 'unsquashfs'
        capture_summary = _verify_session_capture(
            plan, path, capture_iso_path, runner, xorriso, unsquashfs,
            commands, diagnostics)

    if boot_rc == 0:
        if expected['bios_required'] and _positive_boot_entries(
                boot_report, 'BIOS') < 1:
            diagnostics.append(Diagnostic(
                'error', 'iso_bios_boot_missing',
                'No positive El Torito BIOS boot entry was found.', path))
        bios_target = expected.get('bios_target')
        if (expected['bios_required'] and bios_target and
                '/' + bios_target.lstrip('/') not in boot_report and
                bios_target not in boot_report):
            diagnostics.append(Diagnostic(
                'error', 'iso_bios_boot_path_missing',
                'Boot report lacks BIOS image path {}.'.format(bios_target),
                bios_target))
        uefi_targets = expected['uefi_targets']
        if uefi_targets:
            if _positive_boot_entries(boot_report, 'UEFI') < len(uefi_targets):
                diagnostics.append(Diagnostic(
                    'error', 'iso_uefi_boot_entries_missing',
                    'Expected two positive El Torito UEFI entries.', path))
            for target in uefi_targets:
                if '/' + target.lstrip('/') not in boot_report and target not in boot_report:
                    diagnostics.append(Diagnostic(
                        'error', 'iso_uefi_boot_path_missing',
                        'Boot report lacks UEFI image path {}.'.format(target),
                        target))
    if pvd_rc == 0:
        volume_id = _parse_volume_id(pvd_report)
        expected_volume = plan.manifest['volume_label']
        if volume_id is None:
            diagnostics.append(Diagnostic(
                'error', 'iso_volume_id_missing',
                'PVD report contains no volume id.', path))
        elif volume_id != expected_volume:
            diagnostics.append(Diagnostic(
                'error', 'iso_volume_id_mismatch',
                'ISO volume id {!r} does not match {!r}.'.format(
                    volume_id, expected_volume), path))

    digest = None
    artifact_descriptor = None
    opened_stat = file_stat
    try:
        flags = os.O_RDONLY
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
        artifact_descriptor = os.open(path, flags)
        opened_stat = os.fstat(artifact_descriptor)
        if (not stat.S_ISREG(opened_stat.st_mode) or
                _metadata_snapshot(opened_stat) !=
                _metadata_snapshot(file_stat)):
            raise ImageProjectError(
                'artifact changed before final verification')
        digest = _hash_descriptor(artifact_descriptor)
        after = os.fstat(artifact_descriptor)
        final_lstat = os.lstat(path)
        if (_metadata_snapshot(after) != _metadata_snapshot(opened_stat) or
                _metadata_snapshot(final_lstat) !=
                _metadata_snapshot(opened_stat)):
            raise ImageProjectError(
                'artifact changed during final verification')
    except (OSError, ImageProjectError, SourceInspectionError) as error:
        if artifact_descriptor is not None:
            os.close(artifact_descriptor)
            artifact_descriptor = None
        diagnostics.append(Diagnostic(
            'error', 'output_hash_failed', str(error), path))
        opened_stat = file_stat
    level = (VERIFICATION_STRUCTURAL
             if not any(item.severity == 'error' for item in diagnostics)
             else VERIFICATION_BUILT)
    if level != VERIFICATION_STRUCTURAL and artifact_descriptor is not None:
        os.close(artifact_descriptor)
        artifact_descriptor = None
    return _verification_result(
        plan, level, size=file_stat.st_size, sha256=digest,
        diagnostics=diagnostics, tree_paths=tree_paths,
        boot_report=boot_report, pvd_report=pvd_report,
        adapter_manifest_sha256=adapter_manifest_sha256,
        commands=commands, artifact_stat=opened_stat,
        artifact_descriptor=artifact_descriptor,
        capture_summary=capture_summary,
        customization_summary=customization_summary)


def _output_matches_expectation(plan):
    expectation = _thaw(plan._output_expectation)
    exists = os.path.lexists(plan.output_path)
    if not expectation['exists']:
        if exists:
            raise OutputPublishError(
                'Destination appeared after preflight; refusing to overwrite it')
        return
    if not exists:
        raise OutputPublishError('Expected destination disappeared')
    try:
        current = _existing_regular_identity(plan.output_path)
    except (OSError, ImageProjectError, SourceInspectionError) as error:
        raise OutputPublishError(str(error))
    if current != expectation['identity']:
        raise OutputPublishError('Destination changed after preflight')


def publish_verified_output(plan, verification_result, runner=None,
                            xorriso=None, unsquashfs=None):
    """Repeat verification and atomically publish the exact plan artifact."""
    if not isinstance(plan, BuildPlan) or not plan.buildable:
        raise OutputPublishError('a buildable BuildPlan is required')
    if not isinstance(verification_result, VerificationResult):
        raise OutputPublishError('an attested VerificationResult is required')
    if (verification_result._attestation_token is not _VERIFICATION_TOKEN or
            verification_result._plan_nonce is not plan._nonce or
            verification_result.plan_id != plan.plan_id or
            not verification_result.structurally_verified):
        raise OutputPublishError(
            'verification is not a structural attestation for this plan')
    if (verification_result.artifact_device is None or
            verification_result.artifact_inode is None or
            verification_result._artifact_descriptor is None):
        raise OutputPublishError('verification has no retained artifact identity')
    try:
        _validate_job_identity(plan)
        retained = os.fstat(verification_result._artifact_descriptor)
        before = os.lstat(plan.partial_output_path)
    except (OSError, ImageProjectError) as error:
        raise OutputPublishError(str(error))
    if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or
            _identity(retained) != (verification_result.artifact_device,
                                    verification_result.artifact_inode) or
            _metadata_snapshot(before) != _metadata_snapshot(retained)):
        raise OutputPublishError(
            'partial artifact identity changed after verification')

    repeated = verify_iso(
        plan, runner=runner, xorriso=xorriso, unsquashfs=unsquashfs)
    if (not repeated.structurally_verified or
            repeated.sha256 != verification_result.sha256 or
            repeated.adapter_manifest_sha256 !=
            verification_result.adapter_manifest_sha256 or
            _thaw(repeated.capture_summary) !=
            _thaw(verification_result.capture_summary) or
            _thaw(repeated.customization_summary) !=
            _thaw(verification_result.customization_summary) or
            repeated.artifact_device != verification_result.artifact_device or
            repeated.artifact_inode != verification_result.artifact_inode):
        raise OutputPublishError('repeated structural verification did not match')
    _output_matches_expectation(plan)

    if repeated._artifact_descriptor is None:
        raise OutputPublishError(
            'repeated verification has no retained artifact identity')
    try:
        descriptor = os.dup(repeated._artifact_descriptor)
    except OSError as error:
        raise OutputPublishError(
            'cannot retain verified partial output: {}'.format(error))
    try:
        held_stat = os.fstat(descriptor)
        latest = os.lstat(plan.partial_output_path)
        if (_identity(held_stat) != (repeated.artifact_device,
                                     repeated.artifact_inode) or
                _metadata_snapshot(latest) != _metadata_snapshot(held_stat) or
                _hash_descriptor(descriptor) != repeated.sha256):
            raise OutputPublishError('partial output changed before publication')
        os.fsync(descriptor)
        latest = os.lstat(plan.partial_output_path)
        if _metadata_snapshot(latest) != _metadata_snapshot(held_stat):
            raise OutputPublishError('partial output path was replaced')
        expectation = _thaw(plan._output_expectation)
        if expectation['exists']:
            if not plan.manifest['output']['overwrite_allowed']:
                raise OutputPublishError('overwrite policy does not permit replace')
            _output_matches_expectation(plan)
            os.replace(plan.partial_output_path, plan.output_path)
        else:
            # Atomic no-overwrite publication on the same filesystem.
            os.link(plan.partial_output_path, plan.output_path,
                    follow_symlinks=False)
            os.unlink(plan.partial_output_path)
        _fsync_directory(os.path.dirname(plan.output_path) or os.curdir)
        published = os.lstat(plan.output_path)
        if (stat.S_ISLNK(published.st_mode) or
                not stat.S_ISREG(published.st_mode) or
                _identity(published) != _identity(held_stat)):
            raise OutputPublishError('published output identity is unexpected')
    except OSError as error:
        raise OutputPublishError('atomic publication failed: {}'.format(error))
    finally:
        os.close(descriptor)
    return plan.output_path


def detect_vm_capabilities(which=None):
    which = which or shutil.which
    virtualbox = None
    for name in ('VBoxManage', 'VBoxManage.exe'):
        virtualbox = which(name)
        if virtualbox:
            break
    qemu = {}
    for architecture, name in (
            ('x86_64', 'qemu-system-x86_64'),
            ('i386', 'qemu-system-i386')):
        path = which(name)
        if path:
            qemu[architecture] = path
    return {
        'virtualbox': {'available': bool(virtualbox),
                       'executable': virtualbox},
        'qemu': {'available': bool(qemu), 'executables': qemu,
                 'architectures': sorted(qemu)},
        'boot_test_available': bool(virtualbox or qemu),
        'actions': [
            {'id': 'virtualbox_boot_iso', 'provider': 'virtualbox',
             'available': bool(virtualbox), 'executable': virtualbox,
             'executes_automatically': False},
            {'id': 'qemu_boot_iso', 'provider': 'qemu',
             'available': bool(qemu), 'executables': dict(qemu),
             'executes_automatically': False},
        ],
    }


class ModuleManagerHandoff(_Immutable):
    __slots__ = ('module_paths',)

    def __init__(self, module_paths):
        paths = _unique_strings(module_paths, 'module_paths')
        normalized = []
        seen = set()
        for path in paths:
            path = _path_string(path, 'module_path')
            if not os.path.basename(path).endswith('.sb'):
                raise ValueError('module paths must end with .sb')
            identity = os.path.normcase(os.path.abspath(path))
            if identity in seen:
                raise ValueError('duplicate module path')
            seen.add(identity)
            normalized.append(path)
        if not normalized:
            raise ValueError('at least one module path is required')
        self.module_paths = tuple(normalized)
        self._lock()

    def to_dict(self, base_dir=None):
        paths = list(self.module_paths)
        if base_dir:
            paths = [_relative_path(os.path.abspath(path), base_dir)
                     for path in paths]
        return {
            'product_kind': MODULE_MANAGER_HANDOFF_KIND,
            'schema_version': MODULE_MANAGER_HANDOFF_SCHEMA_VERSION,
            'action': 'add-module-paths', 'module_paths': paths,
        }


class ApplicationInstallIntent(_Immutable):
    __slots__ = ('application_ids',)

    def __init__(self, application_ids):
        values = _unique_strings(application_ids, 'application_ids')
        if not values:
            raise ValueError('at least one application id is required')
        for value in values:
            if not _APPLICATION_ID_RE.match(value):
                raise ValueError('invalid application id: {!r}'.format(value))
        self.application_ids = values
        self._lock()

    def to_dict(self):
        return {
            'product_kind': STORE_INSTALL_INTENT_KIND,
            'schema_version': STORE_INSTALL_INTENT_SCHEMA_VERSION,
            'action': 'stage-application-install',
            'target': 'image-project',
            'application_ids': list(self.application_ids),
            'execution': 'not-implemented',
        }


def _payload_object(payload, context):
    if isinstance(payload, bytes):
        try:
            payload = payload.decode('utf-8')
        except UnicodeDecodeError as error:
            raise ProjectFormatError('{} is not UTF-8: {}'.format(
                context, error))
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as error:
            raise ProjectFormatError('Invalid {} JSON: {}'.format(
                context, error))
    if not isinstance(payload, dict):
        raise ProjectFormatError('{} must be an object'.format(context))
    return payload


def create_module_manager_handoff(module_paths, base_dir=None):
    return ModuleManagerHandoff(module_paths).to_dict(base_dir)


def parse_module_manager_handoff(payload, base_dir=None,
                                 require_existing=False):
    payload = _payload_object(payload, 'module handoff')
    keys = set(('product_kind', 'schema_version', 'action', 'module_paths'))
    _require_keys(payload, keys, keys, 'module handoff')
    if payload.get('product_kind') != MODULE_MANAGER_HANDOFF_KIND:
        raise UnsupportedSchemaError('Unsupported module handoff kind')
    version = payload.get('schema_version')
    if (isinstance(version, bool) or
            version != MODULE_MANAGER_HANDOFF_SCHEMA_VERSION):
        raise UnsupportedSchemaError('Unsupported module handoff schema')
    if payload.get('action') != 'add-module-paths':
        raise ProjectFormatError('Unsupported module handoff action')
    try:
        paths = payload.get('module_paths')
        if base_dir:
            paths = [_resolve_path(path, base_dir) for path in paths]
        handoff = ModuleManagerHandoff(paths)
    except (TypeError, ValueError) as error:
        raise ProjectFormatError('Invalid module handoff: {}'.format(error))
    if require_existing:
        for path in handoff.module_paths:
            try:
                file_stat = os.lstat(path)
            except OSError:
                raise ProjectFormatError('Handoff module is unavailable: {}'.format(
                    path))
            if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
                raise ProjectFormatError(
                    'Handoff module must be a regular file: {}'.format(path))
    return handoff


def create_application_install_intent(application_ids):
    return ApplicationInstallIntent(application_ids).to_dict()


def parse_application_install_intent(payload):
    payload = _payload_object(payload, 'application install intent')
    keys = set((
        'product_kind', 'schema_version', 'action', 'target',
        'application_ids', 'execution'))
    _require_keys(payload, keys, keys, 'application install intent')
    if payload.get('product_kind') != STORE_INSTALL_INTENT_KIND:
        raise UnsupportedSchemaError('Unsupported install intent kind')
    version = payload.get('schema_version')
    if (isinstance(version, bool) or
            version != STORE_INSTALL_INTENT_SCHEMA_VERSION):
        raise UnsupportedSchemaError('Unsupported install intent schema')
    if (payload.get('action') != 'stage-application-install' or
            payload.get('target') != 'image-project' or
            payload.get('execution') != 'not-implemented'):
        raise ProjectFormatError('Invalid non-executing install intent')
    try:
        return ApplicationInstallIntent(payload.get('application_ids'))
    except (TypeError, ValueError) as error:
        raise ProjectFormatError('Invalid install intent: {}'.format(error))


create_store_application_install_intent = create_application_install_intent
parse_store_application_install_intent = parse_application_install_intent


__all__ = [
    'ApplicationInstallIntent', 'BuildPlan', 'Diagnostic', 'ImageProject',
    'ImageProjectError', 'ModuleInfo', 'ModuleManagerHandoff',
    'OutputPublishError', 'ProjectFormatError', 'SessionEntry',
    'SessionInventory', 'SourceInfo', 'SourceInspectionError',
    'UnsupportedSchemaError', 'VerificationResult', 'BUILD_PHASE_BOOT_COPY',
    'BUILD_PHASE_CAPTURE', 'BUILD_PHASE_CUSTOMIZE', 'BUILD_PHASE_ORDER',
    'BUILD_PHASE_PREPARE',
    'CAPTURE_COMPRESSIONS', 'CAPTURE_MODES', 'CAPTURE_PHASE_IDS',
    'COMPOSE_BACKEND_NAME', 'COMPOSE_BACKEND_PATH',
    'CURRENT_COMPOSITION', 'DEFAULT_BOOT_MODES', 'BOOT_MENU_MAX_ENTRIES',
    'BOOT_MENU_MAX_JSON_BYTES', 'BOOT_MENU_TITLE_MAX_BYTES',
    'FILESYSTEM_CLASS_PERSISTENT', 'FILESYSTEM_CLASS_RAM_BACKED',
    'FILESYSTEM_CLASS_LIVE_OVERLAY', 'FILESYSTEM_CLASS_REMOVABLE',
    'FILESYSTEM_CLASS_UNKNOWN',
    'IMAGE_CUSTOMIZATION_REPORT_KIND',
    'IMAGE_CUSTOMIZATION_REPORT_SCHEMA_VERSION',
    'LIVE_CONFIG_OVERRIDE_KEYS', 'MENU_LOCALES', 'NO_SESSION_CAPTURE',
    'PROJECT_KIND',
    'PROJECT_SCHEMA_VERSION', 'SOURCE_ERROR', 'SOURCE_SUPPORTED',
    'SOURCE_UNSUPPORTED', 'SESSION_CAPTURE_MODES',
    'SESSION_CAPTURE_REPORT_KIND', 'SESSION_CAPTURE_REPORT_SCHEMA_VERSION',
    'SESSION_INVENTORY_KIND', 'SESSION_INVENTORY_SCHEMA_VERSION',
    'SESSION_SELECTION_KIND', 'SESSION_SELECTION_SCHEMA_VERSION',
    'COMPOSE_CUSTOMIZATION_OPTIONS', 'SUPPORTED_CAPTURE_MODE',
    'VERIFICATION_BUILT', 'VERIFICATION_NOT_BUILT',
    'VERIFICATION_STRUCTURAL', 'atomic_write_json',
    'boot_background_metadata', 'build_plan',
    'build_session_inventory_command', 'cleanup_session_inventory',
    'create_application_install_intent', 'create_build_plan',
    'create_module_manager_handoff', 'create_project_overlay_directory',
    'create_store_application_install_intent',
    'customization_target_set_identity', 'describe_module_name',
    'detect_vm_capabilities',
    'discover_active_external_modules', 'discover_running_minios',
    'discover_running_source', 'discover_mounted_source',
    'list_optical_devices', 'MOUNTED_SOURCE_BACKENDS',
    'resolve_device_mountpoint', 'find_loop_backing_device',
    'grep_ere_validate', 'inspect_source_boot_menu', 'inspect_source_modules',
    'inspect_overlay_directory', 'load_image_project',
    'load_session_inventory', 'module_exclusion_regex', 'overlay_fingerprint',
    'parse_application_install_intent', 'parse_module_manager_handoff',
    'parse_module_order', 'parse_session_inventory', 'preflight',
    'prepare_build_command', 'render_live_config',
    'render_live_config_overrides',
    'probe_required_tools',
    'publish_verified_output', 'required_source_boot_files',
    'request_session_inventory_cancel', 'revalidate_build_plan_inputs',
    'compose_module_target', 'sha256_file',
    'source_tree_fingerprint', 'source_tree_inventory',
    'validate_boot_menu_entries', 'validate_kernel_arguments',
    'validate_live_config_overrides',
    'validate_squashfs', 'verify_iso',
]
