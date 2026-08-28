#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GTK3 workspace for customizing a selected MiniOS image.

The application is a project-oriented front-end for the secure
``image_project`` backend.  It remasters a selected MiniOS source;
it never source-builds MiniOS and never mutates source media.
"""

from __future__ import absolute_import

import datetime
import gettext
import os
import shutil
import stat
import subprocess
import sys

import gi


_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

gi.require_version('Gtk', '3.0')
gi.require_version('Gio', '2.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gio, GLib, Gtk, Pango

import image_project as backend
from image_builder_state import (
    CaptureInventoryViewModel,
    STEP_BUILD, STEP_CONTENT, STEP_DEFAULTS, STEP_IDS, STEP_REVIEW,
    STEP_SOURCE, BackgroundTask, CancellableCommandRunner, ProjectState,
    capture_entry_selected, capture_path_excluded_by,
    clear_capture_runtime, cleanup_inventory_workspace, cleanup_plan_job,
    collision_paths,
    create_inventory_workspace, create_project_plan, parse_build_output_line,
    overwrite_approval_matches, parse_capture_rule_text,
    plan_revision_matches, planned_output_observation,
    planning_navigation_allowed, prepare_plan_execution,
    read_current_live_config,
    redact_command_paths, request_inventory_cancel,
    reset_overwrite_intent_for_retry, review_plan_completion_action,
    review_customization_summary,
    session_inventory_summary, verification_capture_summary,
    verification_customization_summary, verification_selection_summary)
from ui_utils import (
    CommandRunner, LogView, apply_css_if_exists, ask_confirmation,
    human_size, show_error_dialog)

from minios_gui import (HelpPopoverButton, TokenCompletionPopover,
                        classify_module, document_asset_path,
                        load_localized_document, new_header_bar, new_icon,
                        resolve_icon)


APPLICATION_ID = 'org.minios.imagebuilder'
APP_NAME = 'minios-image-builder'
LOCALE_DIRECTORY = '/usr/share/locale'
ICON_WINDOW = 'isomaster'
CSS_PATHS = (
    '/usr/share/minios/minios.css',
    '/usr/share/minios-image-builder/style.css',
    os.path.normpath(os.path.join(
        _LIB_DIR, '..', 'share', 'styles', 'style.css')),
)
HELP_ROOTS = (
    os.path.normpath(os.path.join(_LIB_DIR, '..', 'share', 'help')),
    '/usr/share/minios-image-builder/help',
)

gettext.bindtextdomain(APP_NAME, LOCALE_DIRECTORY)
gettext.textdomain(APP_NAME)
_ = gettext.gettext


def _help_document(name):
    for root in HELP_ROOTS:
        try:
            return load_localized_document(root, name)
        except FileNotFoundError:
            continue
    return {
        'product_kind': 'minios-markup-document',
        'schema_version': 1,
        'nodes': [[
            'block', 'paragraph',
            [['text', _('Help content is unavailable.')]],
        ]],
    }


def _help_asset(name):
    for root in HELP_ROOTS:
        try:
            return document_asset_path(root, name)
        except (FileNotFoundError, ValueError):
            continue
    raise FileNotFoundError(name)

MODULE_DESCRIPTION_TRANSLATIONS = {
    'Base filesystem and essential system utilities.': _(
        'Base filesystem and essential system utilities.'),
    'Linux kernel and hardware drivers.': _(
        'Linux kernel and hardware drivers.'),
    'Firmware for graphics, networking, and other devices.': _(
        'Firmware for graphics, networking, and other devices.'),
    'Display server and desktop graphics stack.': _(
        'Display server and desktop graphics stack.'),
    'Desktop shell, window manager, and supporting applications.': _(
        'Desktop shell, window manager, and supporting applications.'),
    'Web browser and its runtime.': _('Web browser and its runtime.'),
    'Additional command-line and system utilities.': _(
        'Additional command-line and system utilities.'),
    'Bundled user applications.': _('Bundled user applications.'),
    'A MiniOS SquashFS module.': _('A MiniOS SquashFS module.'),
}

DIAGNOSTIC_TRANSLATIONS = {
    'clean_capture_allowlist': (
        _('Reusable changes only'),
        _('Clean capture uses a strict allowlist. It omits general system '
          'state, user data, identity files, logs, and caches.')),
}

RESULT_INTENT_CLASSES = (
    'result-success', 'result-error', 'result-cancelled')



def _module_text(value):
    return MODULE_DESCRIPTION_TRANSLATIONS.get(value, _(value))


def diagnostic_display_text(code, message):
    """Return localized UI text without changing stable backend diagnostics."""
    return DIAGNOSTIC_TRANSLATIONS.get(code, (code, message))


def build_command_failure_detail(returncode, backend_error=None):
    if backend_error:
        return _('The image backend reported: {error}').format(
            error=backend_error)
    return _('The image command exited with status {status}.').format(
        status=returncode)


MENU_CHOICES = (
    ('multilang', _('Multilingual menu')),
    ('en_US', _('English (en_US)')),
    ('ru_RU', _('Russian (ru_RU)')),
    ('de_DE', _('German (de_DE)')),
    ('es_ES', _('Spanish (es_ES)')),
    ('it_IT', _('Italian (it_IT)')),
    ('id_ID', _('Indonesian (id_ID)')),
    ('pt_BR', _('Portuguese, Brazil (pt_BR)')),
    ('pt_PT', _('Portuguese, Portugal (pt_PT)')),
    ('fr_FR', _('French (fr_FR)')),
)

STEP_TITLES = (
    _('Source'), _('Content'), _('Settings'), _('Review'), _('Build'))

PHASE_LABELS = {
    'prepare': _('Preparing build'),
    'capture-inventory': _('Inspecting writable session changes'),
    'capture': _('Capturing writable session changes'),
    'capture-copy': _('Copying selected session changes'),
    'capture-compress': _('Compressing the session layer'),
    'capture-complete': _('Session layer complete'),
    'customize': _('Applying image customizations'),
    'boot-copy': _('Preparing boot files'),
    'persistence': _('Creating persistence data'),
    'iso-write': _('Writing the image'),
    'verify': _('Checking the generated image'),
    'complete': _('Build command complete'),
}

CAPTURE_MODE_CHOICES = (
    (
        'custom', _('Do not include session changes'),
        _('Build from the selected source modules and current configuration '
          'only. Changes made after MiniOS started are ignored.'),
        _('Recommended').upper(), 'success',
    ),
    (
        'exact', _('Include all session changes'),
        _('Include every writable change that can be captured from the current '
          'session. This may include passwords, tokens, personal files, logs, '
          'and machine-specific state.'),
        _('Admin access').upper(), 'warning',
    ),
    (
        'clean', _('Include reusable changes only'),
        _('Include only allowlisted software and system settings intended for '
          'reuse. Personal data, identity, logs, caches, and other broad state '
          'are omitted.'),
        _('Admin access').upper(), 'warning',
    ),
    (
        'selected', _('Choose session changes manually'),
        _('Analyze the current session and choose specific files or directories '
          'to include in the image.'),
        _('Admin access').upper(), 'warning',
    ),
)

CAPTURE_MODE_TITLES = dict(
    (mode, title) for mode, title, _detail, _badge, _style
    in CAPTURE_MODE_CHOICES)

CAPTURE_CATEGORY_TITLES = {
    'runtime': _('Runtime state'),
    'user-data': _('User data'),
    'logs-cache': _('Logs and caches'),
    'machine-identity': _('Machine identity'),
    'network-identity': _('Network identity'),
    'software': _('Software'),
    'system-config': _('System configuration'),
    'other': _('Other'),
}

CAPTURE_TYPE_TITLES = {
    'regular': _('File'),
    'directory': _('Directory'),
    'symlink': _('Symbolic link'),
    'whiteout': _('Deletion marker'),
    'unsupported': _('Unsupported'),
}

DEFAULT_BOOT_TITLES = {
    'resume': _('Resume saved session'),
    'new': _('Start a new session'),
    'choose': _('Choose at boot'),
    'fresh': _('Start fresh'),
    'toram': _('Copy the system to RAM'),
}

BOOT_MODE_DESCRIPTIONS = {
    'resume': _('Continue the most recent compatible saved session.'),
    'new': _('Always create a separate persistent session.'),
    'choose': _('Ask which saved session to use during startup.'),
    'fresh': _('Start without loading persistent changes.'),
    'toram': _('Copy MiniOS to memory so the boot device can be removed.'),
}

BOOT_MODE_PARAMETERS = {
    'resume': 'perchdir=resume',
    'new': 'perchdir=new',
    'choose': 'perchdir=ask',
    'fresh': _('no persistence selector'),
    'toram': 'toram',
}

BOOT_PARAMETER_SUGGESTIONS = (
    'text', 'automount', 'toram=full', 'toram=trim',
    'nozram', 'zramcomp=lzo', 'zramcomp=lzo-rle', 'zramcomp=lz4',
    'zramcomp=lz4hc', 'zramcomp=zstd', 'zramsize=',
    'from=askdisk', 'perch', 'perchdir=resume', 'perchdir=new',
    'perchdir=ask', 'perchdir=', 'perchmode=native',
    'perchmode=dynfilefs', 'perchmode=raw', 'perchmode=luks',
    'perchmode=squashfs',
    'perchsize=', 'perchreserve=', 'load=', 'noload=',
    'locales=', 'timezone=', 'keyboard-layouts=',
    'nomodeset', 'quiet', 'debug',
)

BOOT_PARAMETER_DEFAULTS = {
    'persistence_mode': 'keep',
    'persistence_size': '',
    'persistence_reserve': '',
    'ram_copy': 'keep',
    'load_modules': '',
    'skip_modules': '',
    'startup': 'keep',
    'graphics': 'keep',
    'automount': False,
    'zram': 'keep',
    'zram_compression': 'keep',
    'zram_size': '',
    'locale': '',
    'timezone': '',
    'keyboard': '',
    'quiet': False,
    'debug': False,
    'extra': '',
}

_BOOT_PARAMETER_VALUE_KEYS = {
    'perchmode': 'persistence_mode',
    'perchsize': 'persistence_size',
    'perchreserve': 'persistence_reserve',
    'load': 'load_modules',
    'noload': 'skip_modules',
    'zramcomp': 'zram_compression',
    'zramsize': 'zram_size',
    'locales': 'locale',
    'timezone': 'timezone',
    'keyboard-layouts': 'keyboard',
    'default-target': 'startup',
    'default_target': 'startup',
}

_BOOT_PARAMETER_ENUM_VALUES = {
    'persistence_mode': ('native', 'dynfilefs', 'raw', 'luks', 'squashfs'),
    'zram_compression': ('lzo', 'lzo-rle', 'lz4', 'lz4hc', 'zstd'),
    'startup': ('graphical', 'graphical.target', 'multi-user',
                'multi-user.target', 'rescue', 'rescue.target'),
}


def parse_boot_parameters(value):
    """Split supported MiniOS options from an opaque expert tail."""
    result = dict(BOOT_PARAMETER_DEFAULTS)
    extra = []
    text_mode = False
    for token in (value or '').split():
        if token in ('toram=full', 'toram=trim'):
            result['ram_copy'] = token.split('=', 1)[1]
        elif token == 'text':
            text_mode = True
        elif token == 'nomodeset':
            result['graphics'] = 'nomodeset'
        elif token == 'automount':
            result['automount'] = True
        elif token == 'nozram':
            result['zram'] = 'off'
        elif token == 'quiet':
            result['quiet'] = True
        elif token == 'debug':
            result['debug'] = True
        elif '=' in token and token.split('=', 1)[0] in _BOOT_PARAMETER_VALUE_KEYS:
            name, setting = token.split('=', 1)
            key = _BOOT_PARAMETER_VALUE_KEYS[name]
            allowed = _BOOT_PARAMETER_ENUM_VALUES.get(key)
            if setting and (allowed is None or setting in allowed):
                if key == 'startup' and not setting.endswith('.target'):
                    setting += '.target'
                result[key] = setting
            else:
                extra.append(token)
        else:
            extra.append(token)
    if text_mode:
        result['startup'] = 'text'
    result['extra'] = ' '.join(extra)
    return result


def compile_boot_parameters(settings):
    """Compile typed controls to the stable boot-menu kernel_args format."""
    values = dict(BOOT_PARAMETER_DEFAULTS)
    values.update(settings or {})
    tokens = []
    if values['persistence_mode'] != 'keep':
        tokens.append('perchmode={}'.format(values['persistence_mode']))
    for name, key in (
            ('perchsize', 'persistence_size'),
            ('perchreserve', 'persistence_reserve')):
        if values[key]:
            tokens.append('{}={}'.format(name, values[key]))
    if values['ram_copy'] != 'keep':
        tokens.append('toram={}'.format(values['ram_copy']))
    for name, key in (('load', 'load_modules'), ('noload', 'skip_modules')):
        if values[key]:
            tokens.append('{}={}'.format(name, values[key]))
    if values['startup'] == 'text':
        tokens.append('text')
    elif values['startup'] != 'keep':
        tokens.append('default-target={}'.format(values['startup']))
    if values['graphics'] == 'nomodeset':
        tokens.append('nomodeset')
    if values['automount']:
        tokens.append('automount')
    if values['zram'] == 'off':
        tokens.append('nozram')
    if values['zram_compression'] != 'keep':
        tokens.append('zramcomp={}'.format(values['zram_compression']))
    if values['zram_size']:
        tokens.append('zramsize={}'.format(values['zram_size']))
    for name, key in (
            ('locales', 'locale'), ('timezone', 'timezone'),
            ('keyboard-layouts', 'keyboard')):
        if values[key]:
            tokens.append('{}={}'.format(name, values[key]))
    if values['quiet']:
        tokens.append('quiet')
    if values['debug']:
        tokens.append('debug')
    if values['extra']:
        tokens.extend(values['extra'].split())
    return ' '.join(tokens)

LIVE_CONFIG_TEXT_FIELDS = (
    ('LIVE_HOSTNAME', _('Hostname'), _('Keep current hostname')),
    ('LIVE_TIMEZONE', _('Timezone'), _('Keep current timezone')),
    ('ENABLE_SERVICES', _('Enable services'), _('None')),
    ('DISABLE_SERVICES', _('Disable services'), _('None')),
)

LIVE_CONFIG_HELP = {
    'LIVE_HOSTNAME': _(
        "Set the computer's network name (hostname).\n\n"
        "• Allowed characters: letters, numbers, hyphens.\n"
        "• Example: 'minios-pc'.\n\n"
        "See: man 7 live-config (search 'hostname')"),
    'LIVE_TIMEZONE': _(
        "Set the system timezone (e.g., 'Europe/Berlin', "
        "'America/New_York').\n\n"
        "• Affects system clock and displayed times.\n\n"
        "See: man 7 live-config (search 'timezone')"),
    'DEFAULT_TARGET': _(
        "Set the default boot target:\n"
        "• 'graphical.target' – start with a desktop.\n"
        "• 'multi-user.target' – console mode.\n"
        "• 'rescue.target' – minimal rescue mode."),
    'ENABLE_SERVICES': _(
        "List services to enable at boot, separated by commas.\n\n"
        "• Example: 'ssh, NetworkManager'.\n"
        "• systemd units and sysvinit script names are supported."),
    'DISABLE_SERVICES': _(
        "List services to disable at boot, separated by commas.\n\n"
        "• Example: 'bluetooth, ModemManager'.\n"
        "• systemd units and sysvinit script names are supported."),
    'SECURITY_PRESET': _(
        "Choose a preset to fill the settings below. You can customize any "
        "setting afterward. Only the individual settings are saved."),
    'LIVE_SUDO_MODE': _(
        "passwordless keeps historical MiniOS behavior; password requires "
        "the user password; disabled removes the MiniOS sudo grant."),
    'LIVE_POLKIT_MODE': _(
        "passwordless keeps historical MiniOS GUI admin convenience; "
        "password/disabled remove that rule and use normal PolicyKit "
        "authentication."),
    'LIVE_SSH_PERMIT_ROOT_LOGIN': _(
        "Allow or deny root login through OpenSSH."),
    'LIVE_SSH_PASSWORD_AUTHENTICATION': _(
        "Allow or deny password authentication through OpenSSH."),
    'LIVE_XRDP_MODE': _(
        "relaxed keeps MiniOS defaults; hardened binds to localhost and "
        "disables root login; disabled disables common XRDP service links."),
    'LIVE_X11_MODE': _(
        "relaxed keeps compatibility; hardened removes the permissive -ac "
        "launch option and tightens Xwrapper where present."),
    'LIVE_LOCKSCREEN_MODE': _(
        "relaxed keeps live-session convenience; hardened preserves/enables "
        "screen locking where supported."),
    'LIVE_ISSUE_PASSWORD_HINTS': _(
        "Show default root/live password hints in /etc/issue."),
    'LIVE_LINK_USER_DIRS': _(
        "If enabled, user home directories will be symlinked to persistent "
        "storage.\n\n"
        "• Uses the FAT32, exFAT, or NTFS MiniOS drive.\n"
        "• Unavailable with toram, toram=full, and toram=trim.\n"
        "• Conflicting non-empty folders are never merged automatically.\n\n"
        "See: man 7 live-config (search 'link-user-dirs')"),
    'LIVE_BIND_USER_DIRS': _(
        "If enabled, user home directories will be bind-mounted to persistent "
        "storage.\n\n"
        "• Uses the FAT32, exFAT, or NTFS MiniOS drive.\n"
        "• Unavailable with toram, toram=full, and toram=trim.\n"
        "• Conflicting non-empty folders are never merged automatically.\n\n"
        "See: man 7 live-config (search 'bind-user-dirs')"),
    'LIVE_USER_DIRS_PATH': _(
        "Set the base path on persistent storage for user directories.\n\n"
        "• Example: '/minios/userdirs'.\n\n"
        "• The path must stay inside the MiniOS drive; '.', '..', and empty "
        "segments are not allowed.\n\n"
        "See: man 7 live-config (search 'user-dirs-path')"),
}

LIVE_CONFIG_CHOICE_FIELDS = (
    ('DEFAULT_TARGET', _('Default boot target'), (
        ('keep', _('Keep current')),
        ('graphical.target', 'graphical.target'),
        ('multi-user.target', 'multi-user.target'),
        ('rescue.target', 'rescue.target'),
    )),
    ('LIVE_SUDO_MODE', _('Sudo mode'), (
        ('keep', _('Keep current')),
        ('passwordless', _('Passwordless')),
        ('password', _('Require password')),
        ('disabled', _('Disabled')),
    )),
    ('LIVE_POLKIT_MODE', _('PolicyKit mode'), (
        ('keep', _('Keep current')),
        ('passwordless', _('Passwordless')),
        ('password', _('Require password')),
        ('disabled', _('Disabled')),
    )),
    ('LIVE_SSH_PERMIT_ROOT_LOGIN', _('SSH root login'), (
        ('keep', _('Keep current')),
        ('true', _('Enabled')),
        ('false', _('Disabled')),
    )),
    ('LIVE_SSH_PASSWORD_AUTHENTICATION', _('SSH password authentication'), (
        ('keep', _('Keep current')),
        ('true', _('Enabled')),
        ('false', _('Disabled')),
    )),
    ('LIVE_XRDP_MODE', _('XRDP mode'), (
        ('keep', _('Keep current')),
        ('relaxed', _('Relaxed')),
        ('hardened', _('Hardened')),
        ('disabled', _('Disabled')),
    )),
    ('LIVE_X11_MODE', _('X11 mode'), (
        ('keep', _('Keep current')),
        ('relaxed', _('Relaxed')),
        ('hardened', _('Hardened')),
    )),
    ('LIVE_LOCKSCREEN_MODE', _('Lock screen mode'), (
        ('keep', _('Keep current')),
        ('relaxed', _('Relaxed')),
        ('hardened', _('Hardened')),
    )),
    ('LIVE_ISSUE_PASSWORD_HINTS', _('Show password hints'), (
        ('keep', _('Keep current')),
        ('true', _('Enabled')),
        ('false', _('Disabled')),
    )),
    ('LIVE_LINK_USER_DIRS', _('Link user directories to storage'), (
        ('keep', _('Keep current')),
        ('true', _('Link')),
        ('false', _('Do not link')),
    )),
    ('LIVE_BIND_USER_DIRS', _('Bind user directories to storage'), (
        ('keep', _('Keep current')),
        ('true', _('Bind mount')),
        ('false', _('Do not bind mount')),
    )),
)

# Security keys filled by a preset, in display order. Mirrors the profile
# matrix shipped by python3-minios-security (used by minios-installer and
# minios-configurator); the authoritative values are imported at runtime when
# that library is present, with this table as a self-contained fallback.
SECURITY_PROFILE_KEYS = (
    'LIVE_SUDO_MODE', 'LIVE_POLKIT_MODE', 'LIVE_SSH_PERMIT_ROOT_LOGIN',
    'LIVE_SSH_PASSWORD_AUTHENTICATION', 'LIVE_XRDP_MODE', 'LIVE_X11_MODE',
    'LIVE_LOCKSCREEN_MODE', 'LIVE_ISSUE_PASSWORD_HINTS',
)

SECURITY_PROFILE_CHOICES = (
    ('convenient', _('Convenient')),
    ('balanced', _('Balanced')),
    ('strict', _('Strict')),
)

SECURITY_PROFILE_DESCRIPTIONS = {
    'convenient': _('Easy to use: passwordless sudo and polkit, relaxed '
                    'remote and desktop access, and visible password hints.'),
    'balanced': _('Recommended: require passwords for local administration '
                  'while keeping practical remote access.'),
    'strict': _('Hardened: disable SSH root login and SSH password '
                'authentication, hide password hints, and harden XRDP.'),
}

_SECURITY_PROFILE_FALLBACK = {
    'convenient': {
        'LIVE_SUDO_MODE': 'passwordless', 'LIVE_POLKIT_MODE': 'passwordless',
        'LIVE_SSH_PERMIT_ROOT_LOGIN': 'true',
        'LIVE_SSH_PASSWORD_AUTHENTICATION': 'true',
        'LIVE_XRDP_MODE': 'relaxed', 'LIVE_X11_MODE': 'relaxed',
        'LIVE_LOCKSCREEN_MODE': 'relaxed', 'LIVE_ISSUE_PASSWORD_HINTS': 'true',
    },
    'balanced': {
        'LIVE_SUDO_MODE': 'password', 'LIVE_POLKIT_MODE': 'password',
        'LIVE_SSH_PERMIT_ROOT_LOGIN': 'false',
        'LIVE_SSH_PASSWORD_AUTHENTICATION': 'true',
        'LIVE_XRDP_MODE': 'hardened', 'LIVE_X11_MODE': 'hardened',
        'LIVE_LOCKSCREEN_MODE': 'hardened', 'LIVE_ISSUE_PASSWORD_HINTS': 'true',
    },
    'strict': {
        'LIVE_SUDO_MODE': 'password', 'LIVE_POLKIT_MODE': 'password',
        'LIVE_SSH_PERMIT_ROOT_LOGIN': 'false',
        'LIVE_SSH_PASSWORD_AUTHENTICATION': 'false',
        'LIVE_XRDP_MODE': 'disabled', 'LIVE_X11_MODE': 'hardened',
        'LIVE_LOCKSCREEN_MODE': 'hardened',
        'LIVE_ISSUE_PASSWORD_HINTS': 'false',
    },
}


def security_profile_values(profile):
    """Return the security-key values for a profile, preferring the shared
    python3-minios-security matrix and falling back to the bundled table."""
    values = None
    try:
        from minios_security.security_profiles import live_config_for_profile
        values = live_config_for_profile(profile)
    except Exception:
        values = _SECURITY_PROFILE_FALLBACK.get(profile)
    if not values:
        return {}
    return dict((key, values[key]) for key in SECURITY_PROFILE_KEYS
                if key in values)


CAPTURE_STORE_SELECTED = 0
CAPTURE_STORE_PATH = 1
CAPTURE_STORE_CATEGORY = 2
CAPTURE_STORE_DETAIL = 3
CAPTURE_STORE_SENSITIVE = 4
CAPTURE_STORE_ELIGIBLE = 5
CAPTURE_STORE_ENTRY = 6
CAPTURE_STORE_EXCLUDED = 7
CAPTURE_STORE_EXCLUDED_BY = 8

MODULE_ROLE_LABELS = {
    'core': _('Core system'),
    'kernel': _('Kernel and drivers'),
    'firmware': _('Hardware firmware'),
    'gui-base': _('Graphical base'),
    'desktop': _('Desktop environment'),
    'browser': _('Web browser'),
    'toolbox': _('Toolbox utilities'),
    'ultra': _('Ultra applications'),
    'apps': _('Application bundle'),
    'custom': _('Custom module'),
}


def _module_presentation(name):
    role, icons = classify_module(name)
    return MODULE_ROLE_LABELS[role], icons



def _clear(container):
    for child in container.get_children():
        container.remove(child)


def _set_margins(widget, top=0, bottom=0, start=0, end=0):
    widget.set_margin_top(top)
    widget.set_margin_bottom(bottom)
    widget.set_margin_start(start)
    widget.set_margin_end(end)


def _run_background(worker, callback):
    def completed(outcome):
        return callback(
            outcome.result, outcome.error, outcome.cancelled)

    return BackgroundTask(
        worker, completed, dispatcher=GLib.idle_add).start()


def _read_available_timezones():
    try:
        from zoneinfo import available_timezones
        return set(available_timezones())
    except (ImportError, OSError):
        zones = set()
        zone_root = '/usr/share/zoneinfo'
        for root, _directories, files in os.walk(zone_root):
            for filename in files:
                path = os.path.join(root, filename)
                zones.add(os.path.relpath(path, zone_root))
        return zones


def _read_available_services():
    services = set()
    systemctl = shutil.which('systemctl')
    if systemctl:
        try:
            output = subprocess.check_output(
                [systemctl, 'list-unit-files', '--type=service', '--no-legend',
                 '--no-pager'], universal_newlines=True, timeout=5)
            for line in output.splitlines():
                fields = line.split()
                if not fields:
                    continue
                unit = fields[0]
                services.add(unit)
                if unit.endswith('.service'):
                    services.add(unit[:-8])
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        for name in os.listdir('/etc/init.d'):
            path = os.path.join('/etc/init.d', name)
            if os.path.isfile(path) and not name.startswith('.'):
                services.add(name)
    except OSError:
        pass
    return services


def _read_available_locales():
    locales = set()
    try:
        with open('/usr/share/i18n/SUPPORTED', 'r') as supported:
            for line in supported:
                fields = line.split()
                if fields:
                    locales.add(fields[0])
    except OSError:
        pass
    return locales


def _read_available_keyboard_layouts():
    layouts = set()
    in_layouts = False
    try:
        with open('/usr/share/X11/xkb/rules/base.lst', 'r') as rules:
            for line in rules:
                if line.startswith('!'):
                    in_layouts = line.split()[1:2] == ['layout']
                    continue
                if in_layouts:
                    fields = line.split()
                    if fields:
                        layouts.add(fields[0])
    except OSError:
        pass
    return layouts


class ImageBuilderWindow(Gtk.ApplicationWindow):
    def __init__(self, application):
        Gtk.ApplicationWindow.__init__(
            self, application=application, title=_('MiniOS Image Builder'))
        self.set_default_size(960, 680)
        self.set_size_request(680, 520)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name(ICON_WINDOW)

        output_path = self._new_default_output_path()
        self.state = ProjectState(
            output_path, project_base=os.path.dirname(output_path))
        self.plan = None
        self.active_plan = None
        self.verification_result = None
        self.runner = None
        self.final_output_path = None
        self.vm_capabilities = None
        self.cleanup_warnings = []

        self.source_loading = True
        self.source_exception = None
        self.build_started = False
        self.build_status = 'idle'
        self._source_generation = 0
        self._plan_generation = 0
        self._build_generation = 0
        self._inventory_generation = 0
        self._plan_revision = None
        self._operation = None
        self._task = None
        self._source_task = None
        self._source_mode = 'session'
        self._source_iso_path = None
        self._source_optical_device = None
        self._medium_mount = None
        self._cancel_requested = False
        self._closing = False
        self._overwrite_approved_path = None
        self._overwrite_approved_identity = None
        self._inventory_workspace = None
        self._inventory_status = 'idle'
        self._inventory_message = ''
        self._inventory_search_source = None
        self._capture_store_signature = None
        self._build_output_redactions = ()
        self._last_command_error = None
        self.inventory_view = CaptureInventoryViewModel()
        self.capture_probe_error = None
        self._syncing = False
        self._applying_security_preset = False
        self.available_timezones = _read_available_timezones()
        self.available_services = _read_available_services()
        self.available_locales = _read_available_locales()
        self.available_keyboard_layouts = _read_available_keyboard_layouts()

        apply_css_if_exists(CSS_PATHS)
        self._build_header()
        self._build_workspace()
        self.connect('size-allocate', self._on_size_allocate)
        self._install_actions(application)
        self.connect('delete-event', self._on_delete_event)
        self._sync_defaults_widgets()
        self._render_content()
        self._render_source()
        self._update_chrome()
        GLib.idle_add(self._initial_discovery)

    # Header and workspace -------------------------------------------------
    def _build_header(self):
        self.header = new_header_bar(_('MiniOS Image Builder'))
        self.set_titlebar(self.header)

        self.new_button = self._header_button(
            'document-new-symbolic', _('New project'),
            _('New project (Ctrl+N)'), 'win.new-project')
        self.open_button = self._header_button(
            'document-open-symbolic', _('Open project'),
            _('Open project (Ctrl+O)'), 'win.open-project')
        self.save_button = self._header_button(
            'document-save-symbolic', _('Save project'),
            _('Save project (Ctrl+S)'), 'win.save-project')
        self.save_as_button = self._header_button(
            'document-save-as-symbolic', _('Save project as'),
            _('Save project as (Ctrl+Shift+S)'), 'win.save-project-as')
        self.header.pack_start(self.new_button)
        self.header.pack_start(self.open_button)
        self.header.pack_end(self.save_as_button)
        self.header.pack_end(self.save_button)

    def _header_button(self, icon_name, accessible_name, tooltip, action_name):
        button = Gtk.Button()
        button.set_image(new_icon(
            icon_name, accessible_name=accessible_name))
        button.set_tooltip_text(tooltip)
        button.set_focus_on_click(False)
        button.set_action_name(action_name)
        accessible = button.get_accessible()
        if accessible is not None:
            accessible.set_name(accessible_name)
        return button

    def _install_actions(self, application):
        actions = (
            ('new-project', self._on_new_project, ('<Primary>n',)),
            ('open-project', self._on_open_project, ('<Primary>o',)),
            ('save-project', self._on_save_project, ('<Primary>s',)),
            ('save-project-as', self._on_save_project_as,
             ('<Primary><Shift>s',)),
        )
        for name, callback, accelerators in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            self.add_action(action)
            application.set_accels_for_action(
                'win.{}'.format(name), list(accelerators))

    def _build_workspace(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.get_style_context().add_class('image-builder')
        self.add(root)

        workspace = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        workspace.get_style_context().add_class('workspace')
        root.pack_start(workspace, True, True, 0)

        self.rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.rail.set_size_request(180, -1)
        self.rail.get_style_context().add_class('minios-sidebar')
        _set_margins(self.rail, top=8, bottom=8, start=6, end=4)
        workspace.pack_start(self.rail, False, False, 0)

        self.step_buttons = []
        self.step_markers = []
        self.step_labels = []
        for index, title in enumerate(STEP_TITLES):
            button = Gtk.Button()
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.get_style_context().add_class('sidebar-step')
            button.connect('clicked', self._on_step_clicked, index)
            button.set_tooltip_text(
                _('Step {number}: {title}').format(
                    number=index + 1, title=title))

            row = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            marker = Gtk.Label(xalign=0)
            marker.set_width_chars(2)
            marker.get_style_context().add_class('sidebar-marker')
            row.pack_start(marker, False, False, 0)
            label = Gtk.Label(label=title, xalign=0)
            label.set_line_wrap(True)
            label.set_max_width_chars(16)
            label.get_style_context().add_class('sidebar-label')
            row.pack_start(label, True, True, 0)
            button.add(row)
            self.rail.pack_start(button, False, False, 0)
            self.step_buttons.append(button)
            self.step_markers.append(marker)
            self.step_labels.append(label)

        boundary = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        boundary.get_style_context().add_class('rail-boundary')
        workspace.pack_start(boundary, False, False, 0)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main.set_hexpand(True)
        main.set_vexpand(True)
        workspace.pack_start(main, True, True, 0)

        self.page_stack = Gtk.Stack()
        self.page_stack.set_transition_type(
            Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.page_stack.set_transition_duration(180)
        self.page_stack.set_hexpand(True)
        self.page_stack.set_vexpand(True)
        main.pack_start(self.page_stack, True, True, 0)

        self.page_stack.add_named(self._build_source_page(), 'source')
        self.page_stack.add_named(self._build_content_page(), 'content')
        self.page_stack.add_named(self._build_defaults_page(), 'defaults')
        self.page_stack.add_named(self._build_review_page(), 'review')
        self.page_stack.add_named(self._build_build_page(), 'build')

        self._build_footer(main)
        self.page_stack.set_visible_child_name('source')

    def _on_size_allocate(self, _widget, allocation):
        compact = allocation.width <= 760
        self.rail.set_size_request(72 if compact else 180, -1)
        for label in self.step_labels:
            label.set_visible(not compact)

    def _build_footer(self, parent):
        footer = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.get_style_context().add_class('action-bar')
        _set_margins(footer, top=0, bottom=0, start=10, end=10)

        self.back_button = Gtk.Button(label=_('Back'))
        self.back_button.connect('clicked', self._on_back_clicked)
        footer.pack_start(self.back_button, False, False, 0)

        self.footer_status = Gtk.Label(xalign=0)
        self.footer_status.set_ellipsize(3)
        self.footer_status.get_style_context().add_class('footer-status')
        footer.pack_start(self.footer_status, True, True, 4)

        self.secondary_button = Gtk.Button()
        self.secondary_button.connect('clicked', self._on_secondary_clicked)
        footer.pack_end(self.secondary_button, False, False, 0)

        self.primary_button = Gtk.Button()
        self.primary_button.get_style_context().add_class('suggested-action')
        self.primary_button.connect('clicked', self._on_primary_clicked)
        footer.pack_end(self.primary_button, False, False, 0)
        parent.pack_end(footer, False, False, 0)

    def _page(self, eyebrow, title, description):
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_shadow_type(Gtk.ShadowType.NONE)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_hexpand(True)
        _set_margins(body, top=20, bottom=24, start=20, end=20)
        scrolled.add(body)

        eyebrow_label = Gtk.Label(label=eyebrow, xalign=0)
        eyebrow_label.get_style_context().add_class('page-eyebrow')
        body.pack_start(eyebrow_label, False, False, 0)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.get_style_context().add_class('page-title')
        body.pack_start(title_label, False, False, 0)
        description_label = Gtk.Label(label=description, xalign=0)
        description_label.set_line_wrap(True)
        description_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        description_label.get_style_context().add_class('page-description')
        body.pack_start(description_label, False, False, 0)
        return scrolled, body

    def _field_block(self, parent, title, widget, description=None):
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        label = Gtk.Label(label=title, xalign=0)
        label.get_style_context().add_class('field-label')
        block.pack_start(label, False, False, 0)
        block.pack_start(widget, False, False, 0)
        if description:
            detail = Gtk.Label(label=description, xalign=0)
            detail.set_line_wrap(True)
            detail.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            detail.get_style_context().add_class('field-description')
            block.pack_start(detail, False, False, 0)
        parent.pack_start(block, False, False, 0)
        return block

    def _settings_card(self, body, title, description=None):
        frame = Gtk.Frame()
        frame.get_style_context().add_class('content-card')
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _set_margins(inner, top=12, bottom=12, start=14, end=14)
        frame.add(inner)
        heading = Gtk.Label(label=title, xalign=0)
        heading.get_style_context().add_class('section-heading')
        inner.pack_start(heading, False, False, 0)
        if description:
            detail = Gtk.Label(label=description, xalign=0)
            detail.set_line_wrap(True)
            detail.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            detail.get_style_context().add_class('section-description')
            inner.pack_start(detail, False, False, 0)
        body.pack_start(frame, False, False, 0)
        return inner

    def _compact_grid(self):
        grid = Gtk.Grid(row_spacing=8, column_spacing=14)
        grid.get_style_context().add_class('compact-grid')
        grid.set_column_homogeneous(False)
        grid.set_hexpand(True)
        return grid

    def _compact_row(self, grid, row, title, widget, help_text=None):
        label = Gtk.Label(label=title, xalign=0)
        label.set_valign(Gtk.Align.CENTER)
        label.set_hexpand(False)
        label.get_style_context().add_class('field-label')
        label_widget = label
        if help_text:
            label.set_tooltip_text(help_text)
            widget.set_tooltip_text(help_text)
            label_widget = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            label_widget.set_valign(Gtk.Align.CENTER)
            label_widget.pack_start(label, True, True, 0)
            help_button = HelpPopoverButton(
                title, summary=help_text, compact=True, tooltip=help_text)
            help_button.set_valign(Gtk.Align.CENTER)
            label_widget.pack_end(help_button, False, False, 0)
        widget.set_hexpand(True)
        label_group = getattr(self, '_settings_label_group', None)
        if label_group is not None:
            label_group.add_widget(label_widget)
        field_group = getattr(self, '_settings_field_group', None)
        if field_group is not None:
            field_group.add_widget(widget)
        grid.attach(label_widget, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)

    def _choice_combo(self, choices, callback, *callback_args):
        combo = Gtk.ComboBoxText()
        for value, title in choices:
            combo.append(value, title)
        combo.connect('scroll-event', self._on_choice_combo_scroll)
        combo.connect('changed', callback, *callback_args)
        return combo

    def _on_choice_combo_scroll(self, _combo, _event):
        # Prevent page scrolling from silently changing a focused choice.
        return True

    def _attach_live_config_completion(self, entry, key):
        if key == 'LIVE_TIMEZONE':
            items = self.available_timezones
            aliases = None
        elif key in ('ENABLE_SERVICES', 'DISABLE_SERVICES'):
            items = self.available_services
            aliases = lambda value: (
                value, value[:-8] if value.endswith('.service') else value)
        else:
            return
        TokenCompletionPopover(
            entry, items=items, delimiters=',', min_chars=1,
            max_results=12, aliases=aliases)

    def _section(self, parent, title, description=None):
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.get_style_context().add_class('section-separator')
        parent.pack_start(separator, False, False, 5)
        label = Gtk.Label(label=title, xalign=0)
        label.get_style_context().add_class('section-heading')
        parent.pack_start(label, False, False, 0)
        if description:
            detail = Gtk.Label(label=description, xalign=0)
            detail.set_line_wrap(True)
            detail.get_style_context().add_class('section-description')
            parent.pack_start(detail, False, False, 0)

    def _badge(self, text, style='neutral'):
        label = Gtk.Label(label=text)
        label.set_valign(Gtk.Align.CENTER)
        label.get_style_context().add_class('badge')
        label.get_style_context().add_class('badge-{}'.format(style))
        return label

    # Source page ----------------------------------------------------------
    def _build_source_page(self):
        page, body = self._page(
            _('STEP 1 OF 5'), _('Choose the MiniOS source'),
            _('Image Builder remasters an existing MiniOS into a new ISO. Use '
              'the running session, a MiniOS ISO image file, or an optical '
              'disc. It never downloads sources or builds MiniOS from source.'))

        chooser = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        _set_margins(chooser, bottom=4)
        mode_label = Gtk.Label(label=_('Source:'), xalign=0)
        mode_label.get_style_context().add_class('field-label')
        chooser.pack_start(mode_label, False, False, 0)
        self.source_mode_combo = Gtk.ComboBoxText()
        for mode_id, mode_title in (
                ('session', _('Running session')),
                ('iso', _('ISO image file')),
                ('optical', _('Optical disc (CD/DVD)'))):
            self.source_mode_combo.append(mode_id, mode_title)
        self.source_mode_combo.set_active_id('session')
        self.source_mode_combo.connect(
            'scroll-event', self._on_choice_combo_scroll)
        self.source_mode_combo.connect('changed', self._on_source_mode_changed)
        chooser.pack_start(self.source_mode_combo, False, False, 0)

        self.source_iso_button = Gtk.Button(label=_('Choose ISO file…'))
        self.source_iso_button.set_image(Gtk.Image.new_from_icon_name(
            'document-open-symbolic', Gtk.IconSize.BUTTON))
        self.source_iso_button.get_style_context().add_class(
            'minios-text-button')
        self.source_iso_button.set_no_show_all(True)
        self.source_iso_button.connect('clicked', self._on_choose_source_iso)
        chooser.pack_start(self.source_iso_button, True, True, 0)

        self.source_optical_combo = Gtk.ComboBoxText()
        self.source_optical_combo.set_no_show_all(True)
        self.source_optical_combo.connect(
            'scroll-event', self._on_choice_combo_scroll)
        chooser.pack_start(self.source_optical_combo, True, True, 0)

        self.source_use_button = Gtk.Button(label=_('Mount and inspect'))
        self.source_use_button.set_no_show_all(True)
        self.source_use_button.connect('clicked', self._on_use_media_source)
        chooser.pack_end(self.source_use_button, False, False, 0)
        body.pack_start(chooser, False, False, 0)

        self.source_hero = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.source_hero.get_style_context().add_class('source-hero')
        _set_margins(self.source_hero, top=5, bottom=3)
        body.pack_start(self.source_hero, False, False, 0)

        self.source_spinner = Gtk.Spinner()
        self.source_spinner.set_size_request(32, 32)
        self.source_icon = Gtk.Image()
        self.source_icon.set_pixel_size(32)
        self.source_hero.pack_start(self.source_spinner, False, False, 0)
        self.source_hero.pack_start(self.source_icon, False, False, 0)

        hero_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.source_hero.pack_start(hero_text, True, True, 0)
        self.source_hero_title = Gtk.Label(xalign=0)
        self.source_hero_title.set_line_wrap(True)
        self.source_hero_title.get_style_context().add_class('hero-title')
        hero_text.pack_start(self.source_hero_title, False, False, 0)
        self.source_hero_detail = Gtk.Label(xalign=0)
        self.source_hero_detail.set_line_wrap(True)
        self.source_hero_detail.get_style_context().add_class('hero-detail')
        hero_text.pack_start(self.source_hero_detail, False, False, 0)

        self.source_refresh_button = Gtk.Button(label=_('Refresh'))
        self.source_refresh_button.set_image(Gtk.Image.new_from_icon_name(
            resolve_icon(('view-refresh', 'view-refresh-symbolic')),
            Gtk.IconSize.BUTTON))
        self.source_refresh_button.get_style_context().add_class(
            'minios-text-button')
        self.source_refresh_button.set_valign(Gtk.Align.CENTER)
        self.source_refresh_button.connect(
            'clicked', self._on_source_refresh)
        self.source_hero.pack_end(
            self.source_refresh_button, False, False, 0)

        self.source_details = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=9)
        body.pack_start(self.source_details, False, False, 0)
        self._section(
            self.source_details, _('Detected source'),
            _('The source is read-only and fingerprinted before a plan is '
              'allowed to build.'))
        self.source_metadata_grid = Gtk.Grid(
            row_spacing=8, column_spacing=18)
        self.source_metadata_grid.get_style_context().add_class(
            'metadata-grid')
        self.source_details.pack_start(
            self.source_metadata_grid, False, False, 0)
        self.source_metadata = {}
        metadata_fields = (
            ('path', _('Source path')),
            ('release', _('Release')),
            ('version', _('Version')),
            ('architecture', _('Architecture')),
            ('bootloader', _('Boot support')),
            ('size', _('Source size')),
            ('source_modules', _('Source modules')),
            ('external_modules', _('Active external modules')),
        )
        self.source_metadata_names = {}
        for row, (key, title) in enumerate(metadata_fields):
            name = Gtk.Label(label=title, xalign=0)
            name.get_style_context().add_class('metadata-name')
            value = Gtk.Label(xalign=0)
            value.set_line_wrap(True)
            value.set_line_wrap_mode(Pango.WrapMode.CHAR)
            value.set_selectable(key == 'path')
            value.get_style_context().add_class('metadata-value')
            self.source_metadata_grid.attach(name, 0, row, 1, 1)
            self.source_metadata_grid.attach(value, 1, row, 1, 1)
            self.source_metadata[key] = value
            self.source_metadata_names[key] = name

        self.source_diagnostics = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.pack_start(self.source_diagnostics, False, False, 0)
        return page

    def _initial_discovery(self):
        # Gtk.ApplicationWindow.show_all() runs after construction. Reapply
        # dynamic visibility once the first main-loop turn begins.
        self._reset_build_page()
        self.review_spinner.stop()
        self.review_spinner_box.hide()
        self._sync_defaults_widgets()
        self._render_content()
        self._start_source_discovery(adopt_reference=True)
        return False

    def _start_source_discovery(self, adopt_reference=False):
        if self._source_task is not None:
            self._source_task.cancel()
        # Release any medium this application previously mounted before it
        # inspects a new source. Never touches media mounted by anyone else.
        self._release_medium_mount()
        self._source_generation += 1
        generation = self._source_generation
        self.source_loading = True
        self.source_exception = None
        self.capture_probe_error = None
        self._clear_runtime_inventory()
        self.state.set_capture_capability_status(None)
        self.source_refresh_button.set_sensitive(False)
        self._invalidate_plan()
        self._render_source()
        self._update_chrome()

        mode = self._source_mode
        iso_path = self._source_iso_path
        device = self._source_optical_device

        def worker(token):
            runner = CancellableCommandRunner(token=token, cancel_grace=1.0)
            mount_ownership = None
            if mode in backend.MOUNTED_SOURCE_BACKENDS:
                mount_ownership = self._mount_medium(
                    runner, mode, iso_path, device)
                try:
                    source_result = backend.discover_mounted_source(
                        mount_ownership['mount_path'], media_category=mode)
                except BaseException:
                    self._unmount_medium(mount_ownership)
                    raise
            else:
                source_result = backend.discover_running_source()
            token.checkpoint()
            capabilities = None
            probe_error = None
            try:
                capabilities = backend.probe_required_tools(
                    runner=runner, capture_requested=True)
            except Exception as error:
                probe_error = str(error)
            token.checkpoint()
            return source_result, capabilities, probe_error, mount_ownership

        def finished(result, error, cancelled):
            if generation == self._source_generation:
                self._source_task = None
            new_mount = (result[3] if (result and not error and
                                       len(result) >= 4) else None)
            if self._closing:
                self._medium_mount = new_mount or self._medium_mount
                self._finish_close_if_idle()
                return False
            if cancelled or generation != self._source_generation:
                # A newer discovery superseded this one; release its orphan.
                self._unmount_medium(new_mount)
                return False
            self.source_loading = False
            self.source_refresh_button.set_sensitive(True)
            if error is not None:
                self.source_exception = str(error)
                source_result = backend.SourceInfo(
                    backend.SOURCE_ERROR,
                    diagnostics=(backend.Diagnostic(
                        'error', 'source_discovery_exception', str(error)),))
                capabilities = None
                probe_error = None
            else:
                source_result, capabilities, probe_error, _mount = result
                self._medium_mount = new_mount
            self.capture_probe_error = probe_error
            self.state.set_capture_capability_status(
                capabilities, probe_complete=True)
            self.state.apply_source_info(
                source_result, adopt_reference=adopt_reference)
            self._render_source()
            self._render_content()
            self._sync_defaults_widgets()
            self._update_chrome()
            return False

        self._source_task = _run_background(worker, finished)

    def _on_source_refresh(self, _button):
        if self._source_mode == 'optical':
            self._refresh_optical_devices()
        self._start_source_discovery(
            adopt_reference=not self.state.loaded_project)

    # Mounted-media source lifecycle --------------------------------------
    def _udisksctl_path(self):
        found = shutil.which('udisksctl')
        if found:
            return found
        if os.access('/usr/bin/udisksctl', os.X_OK):
            return '/usr/bin/udisksctl'
        return None

    def _on_source_mode_changed(self, combo):
        mode = combo.get_active_id() or 'session'
        if mode not in ('session',) + tuple(backend.MOUNTED_SOURCE_BACKENDS):
            mode = 'session'
        # Switching source releases any medium we own; the previous discovered
        # source becomes stale until the user inspects the new one.
        if mode != self._source_mode:
            self._release_medium_mount()
        self._source_mode = mode
        self.source_iso_button.set_visible(mode == 'iso')
        self.source_optical_combo.set_visible(mode == 'optical')
        self.source_use_button.set_visible(
            mode in backend.MOUNTED_SOURCE_BACKENDS)
        if mode == 'optical':
            self._refresh_optical_devices()
        self._update_use_button_sensitivity()

    def _refresh_optical_devices(self):
        previous = self.source_optical_combo.get_active_id()
        self.source_optical_combo.remove_all()
        try:
            devices = backend.list_optical_devices()
        except Exception:
            devices = []
        for device_path, label in devices:
            self.source_optical_combo.append(device_path, label)
        if devices:
            device_ids = [device_path for device_path, _label in devices]
            if previous in device_ids:
                self.source_optical_combo.set_active_id(previous)
            else:
                self.source_optical_combo.set_active(0)
            self._source_optical_device = (
                self.source_optical_combo.get_active_id())
        else:
            self.source_optical_combo.append(
                '', _('No optical drive detected'))
            self.source_optical_combo.set_active(0)
            self._source_optical_device = None
        self._update_use_button_sensitivity()

    def _update_use_button_sensitivity(self):
        mode = self._source_mode
        if mode == 'iso':
            ready = bool(self._source_iso_path)
        elif mode == 'optical':
            ready = bool(self.source_optical_combo.get_active_id())
        else:
            ready = False
        self.source_use_button.set_sensitive(ready)

    def _on_choose_source_iso(self, _button):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose a MiniOS ISO image'), transient_for=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Choose'), Gtk.ResponseType.OK)
        file_filter = Gtk.FileFilter()
        file_filter.set_name(_('ISO images (*.iso)'))
        file_filter.add_pattern('*.iso')
        dialog.add_filter(file_filter)
        response = dialog.run()
        path = (dialog.get_filename()
                if response == Gtk.ResponseType.OK else None)
        dialog.destroy()
        if path:
            self._source_iso_path = path
            self.source_iso_button.set_label(os.path.basename(path))
        self._update_use_button_sensitivity()

    def _on_use_media_source(self, _button):
        if self._source_mode == 'optical':
            self._source_optical_device = (
                self.source_optical_combo.get_active_id() or None)
            if not self._source_optical_device:
                show_error_dialog(
                    self, _('Choose an optical drive'),
                    _('No optical drive is selected. Insert a disc and '
                      'refresh, then choose a drive.'))
                return
        elif self._source_mode == 'iso':
            if (not self._source_iso_path or
                    not os.path.isfile(self._source_iso_path)):
                show_error_dialog(
                    self, _('Choose an ISO file'),
                    _('Select a readable MiniOS ISO image first.'))
                return
        else:
            return
        self._start_source_discovery(
            adopt_reference=not self.state.loaded_project)

    def _mount_medium(self, runner, mode, iso_path, device):
        """Read-only udisks mount of an ISO or optical disc (worker thread).

        Returns an ownership record. The mount point is resolved from the
        kernel mount table, never from udisks human output, and unmount
        ownership is recorded only for what this application actually mounted.
        """
        udisks = self._udisksctl_path()
        if not udisks:
            raise RuntimeError(_('udisksctl is not available to mount media.'))
        loop_device = None
        if mode == 'iso':
            if not iso_path or not os.path.isfile(iso_path):
                raise RuntimeError(_('Choose a readable MiniOS ISO file.'))
            runner([udisks, 'loop-setup', '-r', '-f', iso_path,
                    '--no-user-interaction'])
            loop_device = backend.find_loop_backing_device(iso_path)
            if not loop_device:
                raise RuntimeError(
                    _('Could not set up a read-only loop device for the ISO.'))
            block_device = loop_device
        else:
            if not device:
                raise RuntimeError(_('Choose an optical drive.'))
            block_device = device
        returncode, _out, _err = runner(
            [udisks, 'mount', '-b', block_device, '--no-user-interaction'])
        mounted_now = returncode == 0
        mount_path = backend.resolve_device_mountpoint(block_device)
        if not mount_path or not os.path.isdir(mount_path):
            self._unmount_medium({
                'block_device': block_device if mounted_now else None,
                'loop_device': loop_device})
            raise RuntimeError(
                _('The medium mounted but its mount point could not be '
                  'resolved.'))
        return {
            'mount_path': mount_path,
            'block_device': block_device if mounted_now else None,
            'loop_device': loop_device,
            'media_category': mode,
        }

    def _unmount_medium(self, ownership):
        """Best-effort bounded unmount of a medium this application mounted."""
        if not ownership:
            return
        udisks = self._udisksctl_path()
        if not udisks:
            return
        block_device = ownership.get('block_device')
        loop_device = ownership.get('loop_device')
        for argv in (
                ([udisks, 'unmount', '-b', block_device,
                  '--no-user-interaction'] if block_device else None),
                ([udisks, 'loop-delete', '-b', loop_device,
                  '--no-user-interaction'] if loop_device else None)):
            if argv is None:
                continue
            try:
                subprocess.run(
                    argv, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                    timeout=25, check=False, shell=False)
            except (OSError, subprocess.SubprocessError):
                pass

    def _release_medium_mount(self):
        ownership = self._medium_mount
        self._medium_mount = None
        self._unmount_medium(ownership)

    def _reset_source_mode_to_session(self):
        self._source_iso_path = None
        self.source_iso_button.set_label(_('Choose ISO file…'))
        self._release_medium_mount()
        self.source_mode_combo.set_active_id('session')
        self._source_mode = 'session'
        self.source_iso_button.set_visible(False)
        self.source_optical_combo.set_visible(False)
        self.source_use_button.set_visible(False)

    def _render_source(self):
        context = self.source_hero.get_style_context()
        for class_name in ('source-supported', 'source-warning',
                           'source-error', 'source-loading'):
            context.remove_class(class_name)
        _clear(self.source_diagnostics)

        if self.source_loading:
            context.add_class('source-loading')
            self.source_spinner.show()
            self.source_spinner.start()
            self.source_icon.hide()
            self.source_hero_title.set_text(
                _('Inspecting the selected MiniOS source'))
            self.source_hero_detail.set_text(
                _('Hashing the source and modules in the background. Large '
                  'sources can take a moment.'))
            self.source_details.hide()
            return

        self.source_spinner.stop()
        self.source_spinner.hide()
        self.source_icon.show()
        info = self.state.source_info
        if info is None or info.status == backend.SOURCE_ERROR:
            context.add_class('source-error')
            self.source_icon.set_from_icon_name(
                resolve_icon(('dialog-error', 'dialog-error-symbolic')),
                Gtk.IconSize.DIALOG)
            self.source_hero_title.set_text(_('Source inspection failed'))
            self.source_hero_detail.set_text(
                _('Review the diagnostics, fix access to the selected source, '
                  'and refresh.'))
            self.source_details.hide()
        elif info.status == backend.SOURCE_UNSUPPORTED:
            context.add_class('source-warning')
            self.source_icon.set_from_icon_name(
                resolve_icon(('dialog-warning', 'dialog-warning-symbolic')),
                Gtk.IconSize.DIALOG)
            self.source_hero_title.set_text(
                _('A supported MiniOS source was not found'))
            self.source_hero_detail.set_text(
                _('Select the running session, a MiniOS ISO file, or an '
                  'optical disc containing supported MiniOS media.'))
            self.source_details.hide()
        else:
            context.add_class('source-supported')
            self.source_icon.set_from_icon_name(
                resolve_icon(('emblem-ok', 'emblem-ok-symbolic')),
                Gtk.IconSize.DIALOG)
            self.source_hero_title.set_text(_('MiniOS source is ready'))
            self.source_hero_detail.set_text(
                _('This tool rebuilds the selected MiniOS source into a new '
                  'ISO. The summary below shows what will be remastered; the '
                  'source media is only read, never changed. Use Refresh '
                  'after plugging in media or modules.'))
            self.source_details.show_all()
            metadata = info.metadata
            release = (metadata.get('pretty_name') or
                       metadata.get('distribution') or
                       metadata.get('edition') or _('MiniOS'))
            version = (metadata.get('version') or
                       metadata.get('version_id') or
                       metadata.get('build_id') or _('Unknown'))
            self.source_metadata['path'].set_text(
                info.source_path or _('Unavailable'))
            self.source_metadata['path'].set_tooltip_text(info.source_path)
            self.source_metadata['release'].set_text(str(release))
            self.source_metadata['version'].set_text(str(version))
            self.source_metadata['architecture'].set_text(
                str(metadata.get('architecture') or _('Unknown')))
            boot_support = {
                'syslinux-native': _('BIOS (SYSLINUX) + UEFI (GRUB2)'),
                'syslinux-grub': _('BIOS (SYSLINUX + GRUB2) + UEFI (GRUB2)'),
                'grub-only': _('BIOS + UEFI (GRUB2)'),
            }.get(metadata.get('bootloader'), _('Unknown'))
            self.source_metadata['bootloader'].set_text(boot_support)
            self.source_metadata['size'].set_text(
                human_size(info.total_bytes) or _('Unknown'))
            self.source_metadata['source_modules'].set_text(str(
                metadata.get('source_module_count', len(info.modules))))
            external_count = metadata.get(
                'active_external_module_count',
                len(info.active_external_modules))
            self.source_metadata['external_modules'].set_text(
                str(external_count))
            show_external = external_count > 0
            self.source_metadata['external_modules'].set_visible(show_external)
            self.source_metadata_names['external_modules'].set_visible(
                show_external)

            if (self.state.loaded_project and
                    self.state.source_fingerprint != info.fingerprint):
                warning = self._diagnostic_row(
                    'warning', 'project_source_fingerprint_differs',
                    _('The open project targets a different source '
                      'fingerprint. Review will block the build until the '
                      'project matches the selected MiniOS source.'), None)
                self.source_diagnostics.pack_start(
                    warning, False, False, 0)

        if info is not None and info.diagnostics:
            self._section(self.source_diagnostics, _('Diagnostics'))
            for diagnostic in info.diagnostics:
                self.source_diagnostics.pack_start(
                    self._diagnostic_widget(diagnostic), False, False, 0)
        self.source_diagnostics.show_all()

    # Content page ---------------------------------------------------------
    def _build_content_page(self):
        page, body = self._page(
            _('STEP 2 OF 5'), _('Choose image content'),
            _('Source modules keep their MiniOS roles and order. Required '
              'core and kernel modules stay locked; runtime modules remain '
              'opt-in.'))

        self.content_summary = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.content_summary.get_style_context().add_class(
            'composition-summary')
        self.content_count_label = Gtk.Label(xalign=0)
        self.content_count_label.get_style_context().add_class(
            'summary-strong')
        self.content_size_label = Gtk.Label(xalign=1)
        self.content_size_label.get_style_context().add_class('summary-muted')
        self.content_summary.pack_start(
            self.content_count_label, True, True, 0)
        self.content_summary.pack_end(
            self.content_size_label, False, False, 0)
        body.pack_start(self.content_summary, False, False, 0)

        legend_expander = Gtk.Expander()
        legend_expander.set_expanded(False)
        legend_expander.get_style_context().add_class(
            'module-legend-expander')
        legend_header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        legend_header.get_style_context().add_class('module-legend-header')
        legend_icon = Gtk.Image.new_from_icon_name(
            'dialog-information', Gtk.IconSize.BUTTON)
        legend_header.pack_start(legend_icon, False, False, 0)
        legend_title = Gtk.Label(
            label=_('What do the module labels mean?'), xalign=0)
        legend_title.get_style_context().add_class('field-label')
        legend_header.pack_start(legend_title, False, False, 0)
        legend_header.set_tooltip_text(
            _('Show or hide help about the labels used in the module list.'))
        legend_expander.set_label_widget(legend_header)

        legend = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        legend.get_style_context().add_class('module-legend')
        legend_items = Gtk.FlowBox()
        legend_items.set_selection_mode(Gtk.SelectionMode.NONE)
        legend_items.set_homogeneous(True)
        legend_items.set_min_children_per_line(1)
        legend_items.set_max_children_per_line(3)
        legend_items.set_column_spacing(12)
        legend_items.set_row_spacing(7)
        for text, style, description in (
                (_('Source').upper(), 'accent',
                 _('Stored on the selected MiniOS source.')),
                (_('Active').upper(), 'success',
                 _('Mounted in the running session.')),
                (_('Active external').upper(), 'success',
                 _('Active in this session but stored outside the source; '
                   'included only when selected.')),
                (_('Required').upper(), 'warning',
                 _('Required for boot; its selection is locked.')),
                (_('Additional').upper(), 'neutral',
                 _('Added from a module file outside the source.')),
                (_('Collision').upper(), 'error',
                 _('Conflicts with another module and prevents continuing.'))):
            item = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            item.get_style_context().add_class('module-legend-item')
            item.pack_start(self._badge(text, style), False, False, 0)
            detail = Gtk.Label(label=description, xalign=0)
            detail.set_line_wrap(True)
            detail.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            detail.set_max_width_chars(32)
            detail.get_style_context().add_class('module-legend-detail')
            item.pack_start(detail, True, True, 0)
            legend_items.add(item)
        legend.pack_start(legend_items, False, False, 0)
        legend_expander.add(legend)
        body.pack_start(legend_expander, False, False, 0)

        self.collision_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.collision_box.get_style_context().add_class('collision-banner')
        body.pack_start(self.collision_box, False, False, 0)

        self._section(
            body, _('Source modules'),
            _('Every module found on the MiniOS source media is listed here.'))
        self.source_module_list = self._list_box()
        body.pack_start(self.source_module_list, False, False, 0)

        self.external_section = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.external_section.set_no_show_all(True)
        self._section(
            self.external_section, _('Active external modules'),
            _('These modules are active in this session but are outside the '
              'source media. They are not included unless you turn them on.'))
        self.external_module_list = self._list_box()
        self.external_section.pack_start(
            self.external_module_list, False, False, 0)
        body.pack_start(self.external_section, False, False, 0)

        self._section(
            body, _('Additional module files'),
            _('Add readable, non-symlink .sb files from any accessible '
              'location. Full SquashFS validation runs during Review.'))
        add_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_module_button = Gtk.Button(label=_('Add module files'))
        self.add_module_button.set_image(Gtk.Image.new_from_icon_name(
            'list-add-symbolic', Gtk.IconSize.BUTTON))
        self.add_module_button.get_style_context().add_class(
            'minios-text-button')
        self.add_module_button.connect('clicked', self._on_add_modules)
        add_row.pack_start(self.add_module_button, False, False, 0)
        body.pack_start(add_row, False, False, 0)
        self.additional_module_list = self._list_box()
        body.pack_start(self.additional_module_list, False, False, 0)
        return page

    def _list_box(self):
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.get_style_context().add_class('module-list')
        return list_box

    def _render_content(self):
        _clear(self.source_module_list)
        _clear(self.external_module_list)
        _clear(self.additional_module_list)
        _clear(self.collision_box)
        collisions = self.state.collisions()
        affected = collision_paths(collisions)

        self.content_count_label.set_text(
            _('{count} modules selected').format(
                count=self.state.selected_module_count()))
        self.content_size_label.set_text(
            _('Estimated input: {size}').format(
                size=(human_size(self.state.estimated_input_bytes()) or
                      _('0 B'))))

        if collisions:
            title = Gtk.Label(
                label=_('Resolve module collisions before Review'), xalign=0)
            title.get_style_context().add_class('banner-title')
            self.collision_box.pack_start(title, False, False, 0)
            for collision in collisions:
                if 'target' in collision['code']:
                    text = _('Target collision: {value}').format(
                        value=collision['value'])
                else:
                    text = _('Basename collision: {value}').format(
                        value=collision['value'])
                label = Gtk.Label(label=text, xalign=0)
                label.set_line_wrap(True)
                self.collision_box.pack_start(label, False, False, 0)
            self.collision_box.show_all()
        else:
            self.collision_box.hide()

        info = self.state.source_info
        if info is not None and info.supported:
            selected = set(self.state.selected_source_modules)
            for module in info.modules:
                row = self._module_row(
                    module, 'source', module.basename in selected,
                    module.path in affected)
                self.source_module_list.add(row)
            external_paths = set(self.state.additional_module_paths)
            for module in info.active_external_modules:
                row = self._module_row(
                    module, 'external', module.path in external_paths,
                    module.path in affected)
                self.external_module_list.add(row)

        external_count = (len(info.active_external_modules)
                          if info is not None and info.supported else 0)
        if external_count:
            self.external_section.set_no_show_all(False)
            self.external_section.show_all()
        else:
            self.external_section.hide()
            self.external_section.set_no_show_all(True)

        for path in self.state.manual_additional_paths():
            self.additional_module_list.add(
                self._additional_module_row(path, path in affected))
        if self.state.manual_additional_paths():
            self.additional_module_list.show_all()
        else:
            empty = Gtk.ListBoxRow()
            empty.set_activatable(False)
            label = Gtk.Label(
                label=_('No additional module files selected.'), xalign=0)
            label.get_style_context().add_class('empty-note')
            _set_margins(label, top=10, bottom=10, start=12, end=12)
            empty.add(label)
            self.additional_module_list.add(empty)
            self.additional_module_list.show_all()
        self.source_module_list.show_all()
        self._update_chrome()

    def _module_row(self, module, kind, selected, colliding):
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.get_style_context().add_class('module-row')
        if colliding:
            row.get_style_context().add_class('module-row-collision')

        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        _set_margins(content, top=6, bottom=6, start=8, end=8)
        row.add(content)

        check = Gtk.CheckButton()
        check.set_active(bool(selected or module.required))
        check.set_sensitive(not module.required)
        check.set_valign(Gtk.Align.CENTER)
        if module.required:
            check.set_tooltip_text(
                _('Core and kernel modules are required for a bootable image.'))
        elif kind == 'external':
            check.set_tooltip_text(
                _('Include this active external module in the image'))
        else:
            check.set_tooltip_text(_('Include this source module'))
        accessible = check.get_accessible()
        if accessible is not None:
            accessible.set_name(
                _('Include {module}').format(module=module.basename))
        check.connect('toggled', self._on_module_toggled, module, kind)
        content.pack_start(check, False, False, 0)

        icon = Gtk.Image.new_from_icon_name(
            resolve_icon(_module_presentation(module.basename)[1]), Gtk.IconSize.DND)
        icon.set_valign(Gtk.Align.CENTER)
        content.pack_start(icon, False, False, 0)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content.pack_start(details, True, True, 0)

        title_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=_module_presentation(module.basename)[0], xalign=0)
        title.set_tooltip_text(_module_text(module.description))
        title.get_style_context().add_class('module-title')
        title_row.pack_start(title, False, False, 0)
        badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        if kind == 'source':
            badges.pack_start(
                self._badge(_('Source').upper(), 'accent'), False, False, 0)
            if module.active:
                badges.pack_start(
                    self._badge(_('Active').upper(), 'success'), False, False, 0)
        else:
            badges.pack_start(
                self._badge(_('Active external').upper(), 'success'),
                False, False, 0)
        if module.required:
            badges.pack_start(
                self._badge(_('Required').upper(), 'warning'), False, False, 0)
        if colliding:
            badges.pack_start(
                self._badge(_('Collision').upper(), 'error'), False, False, 0)
        title_row.pack_start(badges, False, False, 0)
        size = Gtk.Label(label=human_size(module.size) or '', xalign=1)
        size.get_style_context().add_class('module-size')
        title_row.pack_end(size, False, False, 0)
        details.pack_start(title_row, False, False, 0)

        basename = Gtk.Label(label=module.basename, xalign=0)
        basename.set_ellipsize(3)
        basename.set_tooltip_text(module.path)
        basename.get_style_context().add_class('module-basename')
        details.pack_start(basename, False, False, 0)
        return row

    def _additional_module_row(self, path, colliding):
        details = backend.describe_module_name(path)
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        row.get_style_context().add_class('module-row')
        if colliding:
            row.get_style_context().add_class('module-row-collision')
        content = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        _set_margins(content, top=6, bottom=6, start=8, end=8)
        row.add(content)
        icon = Gtk.Image.new_from_icon_name(
            resolve_icon(_module_presentation(os.path.basename(path))[1]), Gtk.IconSize.DND)
        icon.set_valign(Gtk.Align.CENTER)
        content.pack_start(icon, False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content.pack_start(text, True, True, 0)
        title_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(
            label=_module_presentation(os.path.basename(path))[0], xalign=0)
        title.get_style_context().add_class('module-title')
        title_row.pack_start(title, False, False, 0)
        badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        badges.pack_start(
            self._badge(_('Additional').upper()), False, False, 0)
        if colliding:
            badges.pack_start(
                self._badge(_('Collision').upper(), 'error'), False, False, 0)
        title_row.pack_start(badges, False, False, 0)
        try:
            size_text = human_size(os.path.getsize(path))
        except OSError:
            size_text = _('Missing')
        size = Gtk.Label(label=size_text, xalign=1)
        size.get_style_context().add_class('module-size')
        title_row.pack_end(size, False, False, 0)
        text.pack_start(title_row, False, False, 0)
        name = Gtk.Label(label=os.path.basename(path), xalign=0)
        name.set_ellipsize(3)
        name.set_tooltip_text(path)
        name.get_style_context().add_class('module-basename')
        text.pack_start(name, False, False, 0)
        remove = Gtk.Button()
        remove.set_relief(Gtk.ReliefStyle.NONE)
        remove.set_valign(Gtk.Align.CENTER)
        remove.add(Gtk.Image.new_from_icon_name(
            'list-remove-symbolic', Gtk.IconSize.BUTTON))
        remove.set_tooltip_text(
            _('Remove {module}').format(module=os.path.basename(path)))
        remove.connect('clicked', self._on_remove_module, path)
        content.pack_end(remove, False, False, 0)
        return row

    def _on_module_toggled(self, check, module, kind):
        if self._syncing:
            return
        if kind == 'source':
            changed = self.state.set_source_module_selected(
                module.basename, check.get_active())
        else:
            changed = self.state.set_additional_module_selected(
                module.path, check.get_active())
        if changed:
            self._intent_changed(render_content=True)

    def _on_add_modules(self, _button):
        dialog = Gtk.FileChooserDialog(
            title=_('Add MiniOS module files'), transient_for=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Add'), Gtk.ResponseType.OK)
        dialog.set_select_multiple(True)
        file_filter = Gtk.FileFilter()
        file_filter.set_name(_('MiniOS modules (*.sb)'))
        file_filter.add_pattern('*.sb')
        dialog.add_filter(file_filter)
        response = dialog.run()
        paths = dialog.get_filenames() if response == Gtk.ResponseType.OK else []
        dialog.destroy()
        if not paths:
            return
        errors = []
        changed = False
        error_messages = {
            'invalid-path': _('The path is invalid.'),
            'invalid-module-name': _(
                'The filename is not a portable .sb module name.'),
            'module-not-found': _('The file no longer exists.'),
            'module-is-symlink': _('Symbolic-link modules are not accepted.'),
            'module-not-readable-file': _(
                'The path is not a readable regular file.'),
        }
        for path in paths:
            try:
                changed = self.state.add_additional_module(path) or changed
            except ValueError as error:
                errors.append('{}: {}'.format(
                    os.path.basename(path),
                    error_messages.get(str(error), _('The module is invalid.'))))
        if changed:
            self._intent_changed(render_content=True)
        if errors:
            show_error_dialog(
                self, _('Some module files could not be added.'),
                '\n'.join(errors))

    def _on_remove_module(self, _button, path):
        if self.state.remove_additional_module(path):
            self._intent_changed(render_content=True)

    # Settings page --------------------------------------------------------
    def _build_defaults_page(self):
        page, body = self._page(
            _('STEP 3 OF 5'), _('Settings'),
            _('Choose the image identity, boot presentation, system and '
              'security defaults, and any writable-session capture.'))
        self._settings_label_group = Gtk.SizeGroup(
            Gtk.SizeGroupMode.HORIZONTAL)
        self._settings_field_group = Gtk.SizeGroup(
            Gtk.SizeGroupMode.HORIZONTAL)

        identity = self._settings_card(body, _('Image identity'))
        identity_grid = self._compact_grid()
        identity.pack_start(identity_grid, False, False, 0)

        self.menu_combo = Gtk.ComboBoxText()
        for value, title in MENU_CHOICES:
            self.menu_combo.append(value, title)
        self.menu_combo.connect('changed', self._on_menu_changed)
        self._compact_row(identity_grid, 0, _('Boot menu locale'),
                          self.menu_combo)

        self.volume_entry = Gtk.Entry()
        self.volume_entry.set_max_length(32)
        self.volume_entry.set_placeholder_text(_('MINIOS'))
        self.volume_entry.connect('changed', self._on_volume_changed)
        self._compact_row(identity_grid, 1, _('ISO volume label'),
                          self.volume_entry)

        output_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.output_entry = Gtk.Entry()
        self.output_entry.set_hexpand(True)
        self.output_entry.set_placeholder_text(
            _('Path to the .iso file to create'))
        self.output_entry.connect('changed', self._on_output_changed)
        output_row.pack_start(self.output_entry, True, True, 0)
        browse = Gtk.Button(label=_('Choose'))
        browse.set_image(Gtk.Image.new_from_icon_name(
            'document-save-as-symbolic', Gtk.IconSize.BUTTON))
        browse.get_style_context().add_class('minios-text-button')
        browse.connect('clicked', self._on_choose_output)
        output_row.pack_end(browse, False, False, 0)
        self._compact_row(identity_grid, 2, _('Output image'), output_row)
        output_detail = Gtk.Label(
            label=_('Use a writable disk with enough free space. Review '
                    'checks both destination and scratch requirements.'),
            xalign=0)
        output_detail.set_line_wrap(True)
        output_detail.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        output_detail.get_style_context().add_class('field-description')
        identity.pack_start(output_detail, False, False, 0)

        config_card = self._settings_card(
            body, _('Current configuration'),
            _('The current /etc/live/config.conf is always included in the '
              'image because minios-image-compose requires it. Its bytes are '
              'copied verbatim into private build storage; values are not '
              'interpreted, shown, or logged.'))
        del config_card

        self._build_customization_controls(body)

        capture_card = self._settings_card(
            body, _('Changes from the current session'),
            _('Choose whether changes made after this MiniOS session started '
              'should be copied into the new image. If you only want to change '
              'modules, configuration, and image settings, keep the first '
              'option.'))
        capture_help = HelpPopoverButton(
            _('Changes from the current session'),
            summary=_('A live MiniOS session has a writable layer containing '
                      'changes made after startup, such as installed software, '
                      'changed settings, logs, and user files.'),
            sections=((_('Which option should I choose?'),
                       _('Choose Do not include session changes when you only '
                         'want the selected source modules and configuration. '
                         'Choose Include all session changes to preserve the '
                         'whole capturable writable layer. Choose Include '
                         'reusable changes only for the narrow software/system '
                         'allowlist. Choose Choose session changes manually when '
                         'you want to inspect and select paths yourself.')),
                      (_('Administrator access'),
                       _('The first option does not capture the writable layer. '
                         'The other options use trusted savechanges and may ask '
                         'for administrator authorization; the image builder '
                         'itself is not elevated.')),
                      (_('Privacy'),
                       _('Including all session changes can copy passwords, '
                         'tokens, personal files, logs, and machine identity. '
                         'The reusable and manual modes reduce what is copied '
                         'but are not a guarantee that an image is safe to '
                         'share.'))),
            compact=True, tooltip=_('Explain session-change options'))
        capture_card.pack_start(capture_help, False, False, 0)
        self.capture_mode_buttons = {}
        group = None
        for mode, title, detail, badge, badge_style in CAPTURE_MODE_CHOICES:
            button = self._capture_mode_button(
                group, mode, title, detail, badge, badge_style)
            if group is None:
                group = button
            self.capture_mode_buttons[mode] = button
            body.pack_start(button, False, False, 0)

        self.capture_capability_warning = Gtk.Label(xalign=0)
        self.capture_capability_warning.set_line_wrap(True)
        self.capture_capability_warning.set_line_wrap_mode(
            Pango.WrapMode.WORD_CHAR)
        self.capture_capability_warning.get_style_context().add_class(
            'warning-inline')
        body.pack_start(
            self.capture_capability_warning, False, False, 0)

        self.capture_compression_combo = Gtk.ComboBoxText()
        for compression in backend.CAPTURE_COMPRESSIONS:
            self.capture_compression_combo.append(
                compression, compression.upper())
        self.capture_compression_combo.connect(
            'changed', self._on_capture_compression_changed)
        self.capture_compression_block = self._field_block(
            body, _('Session layer compression'),
            self.capture_compression_combo,
            _('Compression applies only to a captured writable-session layer.'))

        self.capture_ack_check = Gtk.CheckButton(
            label=_('I understand that Include all session changes can preserve passwords, '
                    'tokens, identity, personal files, logs, and other '
                    'sensitive writable state. This acknowledgement is stored '
                    'in the project and remains in effect until I revoke it'))
        capture_ack_label = self.capture_ack_check.get_child()
        capture_ack_label.set_line_wrap(True)
        capture_ack_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        capture_ack_label.set_xalign(0)
        self.capture_ack_check.connect(
            'toggled', self._on_capture_ack_toggled)
        body.pack_start(self.capture_ack_check, False, False, 2)

        self._build_capture_inventory_controls(body)
        self._build_selected_capture_controls(body)

        notes_card = self._settings_card(body, _('Project notes'))
        notes_frame = Gtk.Frame()
        notes_frame.get_style_context().add_class('notes-frame')
        self.notes_view = Gtk.TextView()
        self.notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes_view.set_left_margin(8)
        self.notes_view.set_right_margin(8)
        self.notes_view.set_top_margin(6)
        self.notes_view.set_bottom_margin(6)
        self.notes_view.set_size_request(-1, 90)
        self.notes_view.get_buffer().connect(
            'changed', self._on_notes_changed)
        notes_frame.add(self.notes_view)
        notes_card.pack_start(notes_frame, False, False, 0)

        advanced = Gtk.Expander(label=_('Advanced exclusions'))
        advanced.get_style_context().add_class('advanced-expander')
        advanced_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=7)
        _set_margins(advanced_box, top=8, bottom=4, start=2, end=2)
        warning = Gtk.Label(
            label=_('Expert option: this unchanged POSIX ERE can remove '
                    'files from the source. A broad or invalid expression can '
                    'make the image unbootable. Review blocks matches against '
                    'mandatory inputs.'), xalign=0)
        warning.set_line_wrap(True)
        warning.get_style_context().add_class('warning-panel')
        advanced_box.pack_start(warning, False, False, 0)
        self.exclusion_entry = Gtk.Entry()
        self.exclusion_entry.set_placeholder_text(
            _('POSIX ERE, for example: ^docs/.*[.]tmp$'))
        self.exclusion_entry.connect(
            'changed', self._on_exclusion_changed)
        advanced_box.pack_start(self.exclusion_entry, False, False, 0)
        self.exclusion_load_warning = Gtk.Label(xalign=0)
        self.exclusion_load_warning.set_line_wrap(True)
        self.exclusion_load_warning.get_style_context().add_class(
            'warning-inline')
        advanced_box.pack_start(
            self.exclusion_load_warning, False, False, 0)
        advanced.add(advanced_box)
        body.pack_start(advanced, False, False, 2)
        return page

    def _build_customization_controls(self, body):
        self.live_config_widgets = {}
        system_card = self._settings_card(
            body, _('System defaults'),
            _('Optional allowlisted settings are appended safely to the live '
              'configuration. Empty fields and Keep current preserve the '
              'source value.'))
        system_grid = self._compact_grid()
        system_card.pack_start(system_grid, False, False, 0)
        text_fields = dict((key, (title, placeholder))
                           for key, title, placeholder
                           in LIVE_CONFIG_TEXT_FIELDS)
        row = 0
        for key in ('LIVE_HOSTNAME', 'LIVE_TIMEZONE'):
            title, placeholder = text_fields[key]
            entry = Gtk.Entry()
            entry.set_placeholder_text(placeholder)
            entry.connect('changed', self._on_live_config_entry_changed, key)
            if key == 'LIVE_TIMEZONE':
                self._attach_live_config_completion(entry, key)
            self.live_config_widgets[key] = entry
            self._compact_row(
                system_grid, row, title, entry, LIVE_CONFIG_HELP[key])
            row += 1
        target = dict((key, choices)
                      for key, unused_title, choices
                      in LIVE_CONFIG_CHOICE_FIELDS)['DEFAULT_TARGET']
        target_combo = self._choice_combo(
            target, self._on_live_config_choice_changed, 'DEFAULT_TARGET')
        self.live_config_widgets['DEFAULT_TARGET'] = target_combo
        self._compact_row(
            system_grid, row, _('Default boot target'), target_combo,
            LIVE_CONFIG_HELP['DEFAULT_TARGET'])
        row += 1
        for key in ('ENABLE_SERVICES', 'DISABLE_SERVICES'):
            title, placeholder = text_fields[key]
            entry = Gtk.Entry()
            entry.set_placeholder_text(placeholder)
            entry.connect('changed', self._on_live_config_entry_changed, key)
            self._attach_live_config_completion(entry, key)
            self.live_config_widgets[key] = entry
            self._compact_row(
                system_grid, row, title, entry, LIVE_CONFIG_HELP[key])
            row += 1

        security_box = self._settings_card(body, _('Security & access'))
        preset_grid = self._compact_grid()
        self.security_preset_combo = Gtk.ComboBoxText()
        self.security_preset_combo.append('', _('Custom'))
        for profile_id, profile_label in SECURITY_PROFILE_CHOICES:
            self.security_preset_combo.append(profile_id, profile_label)
        self.security_preset_combo.connect(
            'scroll-event', self._on_choice_combo_scroll)
        self.security_preset_combo.connect(
            'changed', self._on_security_preset_changed)
        self._compact_row(
            preset_grid, 0, _('Security preset'), self.security_preset_combo,
            LIVE_CONFIG_HELP['SECURITY_PRESET'])
        security_box.pack_start(preset_grid, False, False, 0)
        preset_note = Gtk.Label(
            label=_('Choose a preset to fill the settings below. You can '
                    'adjust any individual value afterward.'), xalign=0)
        preset_note.set_line_wrap(True)
        preset_note.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        preset_note.get_style_context().add_class('field-description')
        security_box.pack_start(preset_note, False, False, 0)

        security_grid = self._compact_grid()
        security_keys = (
            'LIVE_SUDO_MODE', 'LIVE_POLKIT_MODE',
            'LIVE_SSH_PERMIT_ROOT_LOGIN',
            'LIVE_SSH_PASSWORD_AUTHENTICATION', 'LIVE_XRDP_MODE',
            'LIVE_X11_MODE', 'LIVE_LOCKSCREEN_MODE',
            'LIVE_ISSUE_PASSWORD_HINTS',
        )
        choice_fields = dict(
            (key, (title, choices)) for key, title, choices
            in LIVE_CONFIG_CHOICE_FIELDS)
        for row, key in enumerate(security_keys):
            title, choices = choice_fields[key]
            combo = self._choice_combo(
                choices, self._on_live_config_choice_changed, key)
            self.live_config_widgets[key] = combo
            self._compact_row(
                security_grid, row, title, combo, LIVE_CONFIG_HELP[key])
        security_box.pack_start(security_grid, False, False, 0)

        user_data = self._settings_card(body, _('User data'))
        user_grid = self._compact_grid()
        user_data.pack_start(user_grid, False, False, 0)
        for row, key in enumerate((
                'LIVE_LINK_USER_DIRS', 'LIVE_BIND_USER_DIRS')):
            title, choices = choice_fields[key]
            combo = self._choice_combo(
                choices, self._on_live_config_choice_changed, key)
            self.live_config_widgets[key] = combo
            self._compact_row(
                user_grid, row, title, combo, LIVE_CONFIG_HELP[key])
        user_path = Gtk.Entry()
        user_path.set_placeholder_text(
            _('Root-relative path, for example home/live'))
        user_path.connect(
            'changed', self._on_live_config_entry_changed,
            'LIVE_USER_DIRS_PATH')
        self.live_config_widgets['LIVE_USER_DIRS_PATH'] = user_path
        self._compact_row(
            user_grid, 2, _('User directories path on storage'), user_path,
            LIVE_CONFIG_HELP['LIVE_USER_DIRS_PATH'])

        self.customization_status = Gtk.Label(xalign=0)
        self.customization_status.set_line_wrap(True)
        self.customization_status.get_style_context().add_class(
            'warning-inline')
        body.pack_start(self.customization_status, False, False, 0)

        boot_card = self._settings_card(
            body, _('Boot behavior & appearance'),
            _('Preserve source leaves the existing boot setting unchanged.'))
        boot_grid = self._compact_grid()
        boot_card.pack_start(boot_grid, False, False, 0)
        timeout_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.boot_timeout_preserve = Gtk.CheckButton(
            label=_('Preserve source'))
        self.boot_timeout_preserve.connect(
            'toggled', self._on_boot_timeout_preserve_toggled)
        timeout_box.pack_start(
            self.boot_timeout_preserve, False, False, 0)
        timeout_adjustment = Gtk.Adjustment(
            value=10, lower=0, upper=300, step_increment=1,
            page_increment=10)
        self.boot_timeout_spin = Gtk.SpinButton(
            adjustment=timeout_adjustment, climb_rate=1, digits=0)
        self.boot_timeout_spin.set_numeric(True)
        self.boot_timeout_spin.connect(
            'value-changed', self._on_boot_timeout_changed)
        timeout_box.pack_start(self.boot_timeout_spin, False, False, 0)
        timeout_unit = Gtk.Label(label=_('seconds'), xalign=0)
        timeout_box.pack_start(timeout_unit, False, False, 0)
        self._compact_row(boot_grid, 0, _('Boot timeout'), timeout_box)

        default_choices = [('preserve', _('Preserve source'))]
        default_choices.extend(
            (mode, DEFAULT_BOOT_TITLES[mode])
            for mode in backend.DEFAULT_BOOT_MODES)
        self.default_boot_combo = self._choice_combo(
            default_choices, self._on_default_boot_changed)
        self._compact_row(
            boot_grid, 1, _('Default session'), self.default_boot_combo)

        self._build_boot_menu_constructor(boot_card)

        background_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.background_choose_button = Gtk.Button(label=_('Choose PNG'))
        self.background_choose_button.connect(
            'clicked', self._on_choose_boot_background)
        background_actions.pack_start(
            self.background_choose_button, False, False, 0)
        self.background_clear_button = Gtk.Button(label=_('Clear'))
        self.background_clear_button.connect(
            'clicked', self._on_clear_boot_background)
        background_actions.pack_start(
            self.background_clear_button, False, False, 0)
        self._compact_row(
            boot_grid, 2, _('Boot background'), background_actions)
        self.background_status = Gtk.Label(xalign=0)
        self.background_status.set_line_wrap(True)
        self.background_status.get_style_context().add_class(
            'field-description')
        boot_card.pack_start(self.background_status, False, False, 0)

        kernel_expander = Gtk.Expander(label=_('Expert: kernel arguments'))
        kernel_expander.get_style_context().add_class('advanced-expander')
        kernel_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=5)
        _set_margins(kernel_box, top=8, bottom=4, start=2, end=2)
        kernel_warning = Gtk.Label(
            label=_('Bootloader-safe arguments only. Raw arguments are never '
                    'shown in Review, results, or command logs.'), xalign=0)
        kernel_warning.set_line_wrap(True)
        kernel_warning.get_style_context().add_class('warning-panel')
        kernel_box.pack_start(kernel_warning, False, False, 0)
        kernel_entry_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.kernel_args_entry = Gtk.Entry()
        self.kernel_args_entry.set_placeholder_text(
            _('Empty preserves source kernel arguments'))
        self.kernel_args_entry.connect(
            'changed', self._on_kernel_args_changed)
        TokenCompletionPopover(
            self.kernel_args_entry, items=BOOT_PARAMETER_SUGGESTIONS,
            delimiters=' ', min_chars=1, max_results=12)
        kernel_entry_row.pack_start(
            self.kernel_args_entry, True, True, 0)
        kernel_help = HelpPopoverButton(
            _('Kernel parameters'),
            document=_help_document('boot-menu/parameters.json'),
            asset_resolver=_help_asset, compact=True, tooltip=_('Explain boot and kernel parameters'))
        kernel_entry_row.pack_end(kernel_help, False, False, 0)
        kernel_box.pack_start(kernel_entry_row, False, False, 0)
        kernel_expander.add(kernel_box)
        boot_card.pack_start(kernel_expander, False, False, 0)

        overlay_card = self._settings_card(
            body, _('Project filesystem layer'),
            _('A root-relative directory tree becomes a final reusable '
              'filesystem layer. It does not run scripts, chroot operations, '
              'or package commands. Reusable .sb modules belong in Module '
              'Manager. The selected directory is never deleted.'))
        overlay_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.overlay_choose_button = Gtk.Button(
            label=_('Choose existing'))
        self.overlay_choose_button.connect(
            'clicked', self._on_choose_overlay_directory)
        overlay_actions.pack_start(
            self.overlay_choose_button, False, False, 0)
        self.overlay_create_button = Gtk.Button(
            label=_('Create project layer'))
        self.overlay_create_button.connect(
            'clicked', self._on_create_overlay_directory)
        overlay_actions.pack_start(
            self.overlay_create_button, False, False, 0)
        self.overlay_open_button = Gtk.Button(label=_('Open'))
        self.overlay_open_button.connect(
            'clicked', self._on_open_overlay_directory)
        overlay_actions.pack_start(
            self.overlay_open_button, False, False, 0)
        self.overlay_clear_button = Gtk.Button(label=_('Clear'))
        self.overlay_clear_button.connect(
            'clicked', self._on_clear_overlay_directory)
        overlay_actions.pack_start(
            self.overlay_clear_button, False, False, 0)
        overlay_card.pack_start(overlay_actions, False, False, 0)
        self.overlay_status = Gtk.Label(xalign=0)
        self.overlay_status.set_line_wrap(True)
        self.overlay_status.get_style_context().add_class('field-description')
        overlay_card.pack_start(self.overlay_status, False, False, 0)

    def _build_boot_menu_constructor(self, boot_card):
        expander = Gtk.Expander(label=_('Boot menu entries'))
        expander.set_expanded(True)
        expander.get_style_context().add_class('advanced-expander')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _set_margins(box, top=8, bottom=4, start=2, end=2)

        intro = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        note = Gtk.Label(
            label=_('Choose what people see at startup. Each entry can have '
                    'its own persistence, memory, module, graphics, language, '
                    'and diagnostic settings.'),
            xalign=0)
        note.set_line_wrap(True)
        note.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        note.get_style_context().add_class('field-description')
        intro.pack_start(note, True, True, 0)
        help_button = HelpPopoverButton(
            _('Boot menu constructor'),
            document=_help_document('boot-menu/overview.json'),
            asset_resolver=_help_asset, compact=True, tooltip=_('Boot menu constructor help'))
        intro.pack_end(help_button, False, False, 0)
        box.pack_start(intro, False, False, 0)

        self.boot_menu_status = Gtk.Label(xalign=0)
        self.boot_menu_status.set_line_wrap(True)
        self.boot_menu_status.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.boot_menu_status.get_style_context().add_class('availability-note')
        box.pack_start(self.boot_menu_status, False, False, 0)

        self.boot_menu_rows_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.boot_menu_rows = {}
        self.boot_menu_order = []
        self.boot_menu_field_label_group = Gtk.SizeGroup(
            Gtk.SizeGroupMode.HORIZONTAL)
        box.pack_start(self.boot_menu_rows_box, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_button = Gtk.Button(label=_('Add boot entry'))
        add_button.set_image(Gtk.Image.new_from_icon_name(
            'list-add-symbolic', Gtk.IconSize.BUTTON))
        add_button.get_style_context().add_class('minios-text-button')
        add_button.connect('clicked', self._on_boot_menu_add)
        actions.pack_start(add_button, False, False, 0)
        reset_button = Gtk.Button(label=_('Restore source menu'))
        reset_button.get_style_context().add_class('minios-text-button')
        reset_button.connect('clicked', self._on_boot_menu_reset)
        actions.pack_start(reset_button, False, False, 0)
        parameter_help = HelpPopoverButton(
            _('Kernel parameter reference'),
            document=_help_document('boot-menu/parameters.json'),
            asset_resolver=_help_asset, label=_('Parameter help'),
            tooltip=_('Explain common boot and kernel parameters'))
        actions.pack_end(parameter_help, False, False, 0)
        box.pack_start(actions, False, False, 0)

        expander.add(box)
        boot_card.pack_start(expander, False, False, 0)

    def _source_boot_menu_settings(self):
        info = self.state.source_info
        if info is not None and info.supported:
            try:
                return backend.inspect_source_boot_menu(
                    info, self.state.menu_locale)
            except (OSError, ValueError, backend.ImageProjectError):
                pass
        return None

    def _source_boot_menu_editor_entries(self):
        source_menu = self._source_boot_menu_settings()
        if source_menu is not None:
            return [dict(item) for item in source_menu['entries']]
        default_mode = (self.state.default_boot
                        if self.state.default_boot in backend.DEFAULT_BOOT_MODES
                        else backend.DEFAULT_BOOT_MODES[0])
        return [
            {
                'id': mode, 'base_mode': mode, 'enabled': True,
                'default': mode == default_mode, 'title': None,
                'kernel_args': '',
            }
            for mode in backend.DEFAULT_BOOT_MODES
        ]

    def _next_boot_menu_entry_id(self, entries=None):
        entries = entries or self._boot_menu_editor_entries()
        used = set(item['id'] for item in entries)
        for number in range(1, backend.BOOT_MENU_MAX_ENTRIES + 1):
            candidate = 'custom-{}'.format(number)
            if candidate not in used:
                return candidate
        raise ValueError(_('The boot menu already contains the maximum number of entries.'))

    def _create_boot_menu_row(self, entry, default_group):
        entry_id = entry['id']
        frame = Gtk.Frame()
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        _set_margins(inner, top=7, bottom=7, start=8, end=8)
        frame.add(inner)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        enabled = Gtk.CheckButton(label=_('Show'))
        enabled.set_tooltip_text(_('Show this entry in the boot menu'))
        enabled.connect('toggled', self._on_boot_menu_enabled_toggled, entry_id)
        top.pack_start(enabled, False, False, 0)

        default = Gtk.RadioButton.new_from_widget(default_group)
        default.set_label(_('Default'))
        default.set_tooltip_text(_('Use this as the default boot entry'))
        default.connect('toggled', self._on_boot_menu_default_toggled, entry_id)
        top.pack_start(default, False, False, 0)

        template_label = Gtk.Label(label=_('Template'), xalign=0)
        template_label.get_style_context().add_class('field-label')
        top.pack_start(template_label, False, False, 0)
        template = Gtk.ComboBoxText()
        for mode in backend.DEFAULT_BOOT_MODES:
            template.append(mode, DEFAULT_BOOT_TITLES[mode])
        template.connect('scroll-event', self._on_choice_combo_scroll)
        template.connect('changed', self._on_boot_menu_base_changed, entry_id)
        top.pack_start(template, False, False, 0)

        duplicate = Gtk.Button()
        duplicate.set_relief(Gtk.ReliefStyle.NONE)
        duplicate.add(Gtk.Image.new_from_icon_name(
            'edit-copy-symbolic', Gtk.IconSize.MENU))
        duplicate.set_tooltip_text(_('Duplicate entry'))
        duplicate.connect('clicked', self._on_boot_menu_duplicate, entry_id)
        top.pack_start(duplicate, False, False, 0)

        up = Gtk.Button()
        up.set_relief(Gtk.ReliefStyle.NONE)
        up.add(Gtk.Image.new_from_icon_name('go-up-symbolic', Gtk.IconSize.MENU))
        up.set_tooltip_text(_('Move entry up'))
        up.connect('clicked', self._on_boot_menu_move, entry_id, -1)
        top.pack_start(up, False, False, 0)
        down = Gtk.Button()
        down.set_relief(Gtk.ReliefStyle.NONE)
        down.add(Gtk.Image.new_from_icon_name('go-down-symbolic', Gtk.IconSize.MENU))
        down.set_tooltip_text(_('Move entry down'))
        down.connect('clicked', self._on_boot_menu_move, entry_id, 1)
        top.pack_start(down, False, False, 0)

        remove = Gtk.Button()
        remove.set_relief(Gtk.ReliefStyle.NONE)
        remove.add(Gtk.Image.new_from_icon_name(
            'edit-delete-symbolic', Gtk.IconSize.MENU))
        remove.set_tooltip_text(_('Remove custom entry'))
        remove.connect('clicked', self._on_boot_menu_remove, entry_id)
        remove.set_visible(entry_id not in backend.DEFAULT_BOOT_MODES)
        top.pack_start(remove, False, False, 0)
        inner.pack_start(top, False, False, 0)

        template_detail = Gtk.Label(xalign=0)
        template_detail.set_line_wrap(True)
        template_detail.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        template_detail.get_style_context().add_class('field-description')
        inner.pack_start(template_detail, False, False, 0)

        title_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title_label = Gtk.Label(label=_('Name'), xalign=0)
        title_label.get_style_context().add_class('field-label')
        self.boot_menu_field_label_group.add_widget(title_label)
        title_row.pack_start(title_label, False, False, 0)
        title = Gtk.Entry()
        title.set_max_length(128)
        title.set_placeholder_text(_('Keep template title'))
        title.set_tooltip_text(_('Visible name of this boot menu entry'))
        title.connect('changed', self._on_boot_menu_title_changed, entry_id)
        title_row.pack_start(title, True, True, 0)
        inner.pack_start(title_row, False, False, 0)

        options = Gtk.Expander(label=_('Startup options'))
        options.get_style_context().add_class('advanced-expander')
        options_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _set_margins(options_box, top=8, bottom=4, start=2, end=2)
        option_widgets = {}

        def add_heading(text, document):
            heading = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            heading.set_hexpand(True)
            heading.get_style_context().add_class('boot-option-heading')
            _set_margins(heading, top=10, bottom=3)
            label = Gtk.Label(label=text, xalign=0)
            label.get_style_context().add_class('section-heading')
            heading.pack_start(label, True, True, 0)
            help_button = HelpPopoverButton(
                text, document=_help_document(document), asset_resolver=_help_asset,
                compact=True,
                tooltip=_('Help for {section}').format(section=text))
            heading.pack_end(help_button, False, False, 0)
            options_box.pack_start(heading, False, False, 1)

        def add_grid():
            grid = Gtk.Grid(column_spacing=10, row_spacing=6)
            grid.set_hexpand(True)
            options_box.pack_start(grid, False, False, 0)
            return grid

        def add_field(grid, row, column, text, widget):
            display_row = row * 2 + column
            label = Gtk.Label(label=text, xalign=0)
            label.get_style_context().add_class('field-label')
            grid.attach(label, 0, display_row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, display_row, 1, 1)

        def new_combo(key, choices):
            widget = Gtk.ComboBoxText()
            for value, text in choices:
                widget.append(value, text)
            widget.connect('scroll-event', self._on_choice_combo_scroll)
            widget.connect(
                'changed', self._on_boot_menu_options_changed, entry_id)
            option_widgets[key] = widget
            return widget

        def new_entry(key, placeholder, completion_items=None, aliases=None):
            widget = Gtk.Entry()
            widget.set_placeholder_text(placeholder)
            widget.connect(
                'changed', self._on_boot_menu_options_changed, entry_id)
            option_widgets[key] = widget
            if completion_items:
                TokenCompletionPopover(
                    widget, items=completion_items, delimiters=',',
                    min_chars=1, max_results=12, aliases=aliases)
            return widget

        module_completions = set()
        if self.state.source_info is not None:
            module_completions.update(
                module.basename for module in self.state.source_info.modules)
        module_completions.update(
            os.path.basename(path) for path in self.state.additional_module_paths)
        module_aliases = lambda value: (
            value, value[:-3] if value.endswith('.sb') else value)

        add_heading(
            _('Session and storage'),
            'boot-menu/session-storage.json')
        session_grid = add_grid()
        add_field(session_grid, 0, 0, _('Changes storage'), new_combo(
            'persistence_mode', (
                ('keep', _('Automatic')),
                ('native', _('Directory on a Linux filesystem')),
                ('dynfilefs', _('Expandable container')),
                ('raw', _('Fixed-size image')),
                ('luks', _('Encrypted container')),
                ('squashfs', _('SquashFS session')))))
        add_field(session_grid, 0, 1, _('Container size'), new_entry(
            'persistence_size', _('Automatic, or for example 8GB')))
        add_field(session_grid, 1, 0, _('Free space to keep'), new_entry(
            'persistence_reserve', _('Default: 256 MiB')))
        add_field(session_grid, 1, 1, _('Copy to RAM'), new_combo(
            'ram_copy', (
                ('keep', _('Template default')),
                ('full', _('Entire system')),
                ('trim', _('Loaded modules only')))))

        add_heading(
            _('System and modules'),
            'boot-menu/system-modules.json')
        system_grid = add_grid()
        add_field(system_grid, 0, 0, _('Load modules'), new_entry(
            'load_modules', _('All modules'), module_completions,
            module_aliases))
        add_field(system_grid, 0, 1, _('Skip modules'), new_entry(
            'skip_modules', _('None'), module_completions, module_aliases))
        add_field(system_grid, 1, 0, _('Startup environment'), new_combo(
            'startup', (
                ('keep', _('Image default')),
                ('graphical.target', _('Graphical desktop')),
                ('text', _('Text console')),
                ('rescue.target', _('Rescue mode')))))
        add_field(system_grid, 1, 1, _('Graphics'), new_combo(
            'graphics', (
                ('keep', _('Normal graphics')),
                ('nomodeset', _('Compatibility mode (nomodeset)')))))
        automount = Gtk.CheckButton(label=_('Mount other disks automatically'))
        automount.connect(
            'toggled', self._on_boot_menu_options_changed, entry_id)
        option_widgets['automount'] = automount
        system_grid.attach(automount, 0, 4, 2, 1)

        add_heading(
            _('Memory'),
            'boot-menu/memory.json')
        memory_grid = add_grid()
        add_field(memory_grid, 0, 0, _('zRAM'), new_combo(
            'zram', (
                ('keep', _('Automatic')),
                ('off', _('Disabled')))))
        add_field(memory_grid, 0, 1, _('Compression'), new_combo(
            'zram_compression', (
                ('keep', _('Automatic')),
                ('lzo', 'LZO'), ('lzo-rle', 'LZO-RLE'), ('lz4', 'LZ4'),
                ('lz4hc', 'LZ4HC'), ('zstd', 'Zstandard'))))
        add_field(memory_grid, 1, 0, _('zRAM size'), new_entry(
            'zram_size', _('Automatic, in MiB')))

        add_heading(
            _('Language for this entry'),
            'boot-menu/language.json')
        locale_grid = add_grid()
        add_field(locale_grid, 0, 0, _('Locale'), new_entry(
            'locale', _('Image default, for example ru_RU.UTF-8'),
            self.available_locales))
        add_field(locale_grid, 0, 1, _('Timezone'), new_entry(
            'timezone', _('Image default, for example Europe/Moscow'),
            self.available_timezones))
        add_field(locale_grid, 1, 0, _('Keyboard layout'), new_entry(
            'keyboard', _('Image default, for example us or ru'),
            self.available_keyboard_layouts))

        add_heading(
            _('Diagnostics and advanced options'),
            'boot-menu/diagnostics.json')
        diagnostic_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        quiet = Gtk.CheckButton(label=_('Hide routine boot messages'))
        quiet.connect(
            'toggled', self._on_boot_menu_options_changed, entry_id)
        option_widgets['quiet'] = quiet
        diagnostic_row.pack_start(quiet, False, False, 0)
        debug = Gtk.CheckButton(label=_('Enable diagnostic logging'))
        debug.connect(
            'toggled', self._on_boot_menu_options_changed, entry_id)
        option_widgets['debug'] = debug
        diagnostic_row.pack_start(debug, False, False, 0)
        options_box.pack_start(diagnostic_row, False, False, 2)

        expert_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        expert_label = Gtk.Label(label=_('Additional parameters'), xalign=0)
        expert_label.get_style_context().add_class('field-label')
        expert_row.pack_start(expert_label, False, False, 0)
        expert = Gtk.Entry()
        expert.set_placeholder_text(
            _('Only options not available above'))
        expert.set_tooltip_text(
            _('Unrecognized options from existing projects are kept here'))
        expert.connect(
            'changed', self._on_boot_menu_options_changed, entry_id)
        TokenCompletionPopover(
            expert, items=BOOT_PARAMETER_SUGGESTIONS,
            delimiters=' ', min_chars=1, max_results=12)
        option_widgets['extra'] = expert
        expert_row.pack_start(expert, True, True, 0)
        parameter_help = HelpPopoverButton(
            _('Additional parameters'),
            document=_help_document('boot-menu/parameters.json'),
            asset_resolver=_help_asset, compact=True, tooltip=_('Explain boot parameters'))
        expert_row.pack_end(parameter_help, False, False, 0)
        options_box.pack_start(expert_row, False, False, 0)

        option_summary = Gtk.Label(xalign=0)
        option_summary.set_line_wrap(True)
        option_summary.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        option_summary.get_style_context().add_class('field-description')
        inner.pack_start(option_summary, False, False, 0)
        options.add(options_box)
        inner.pack_start(options, False, False, 0)

        return {
            'frame': frame, 'enabled': enabled, 'default': default,
            'template': template, 'template_detail': template_detail,
            'title': title, 'option_widgets': option_widgets,
            'option_summary': option_summary, 'options': options,
            'duplicate': duplicate, 'up': up, 'down': down, 'remove': remove,
            'kernel_args_schema': entry.get('kernel_args_schema'),
        }


    def _capture_mode_button(self, group, mode, title, description,
                             badge, badge_style):
        button = Gtk.RadioButton.new_from_widget(group)
        button.set_hexpand(True)
        button.get_style_context().add_class('capture-mode-row')
        button.get_style_context().add_class('choice-card')
        button.get_style_context().add_class('minios-choice')
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.get_style_context().add_class('capability-title')
        detail_label = Gtk.Label(label=description, xalign=0)
        detail_label.set_line_wrap(True)
        detail_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        detail_label.get_style_context().add_class('field-description')
        text.pack_start(title_label, False, False, 0)
        text.pack_start(detail_label, False, False, 0)
        content.pack_start(text, True, True, 0)
        badge_label = self._badge(badge, badge_style)
        content.pack_end(badge_label, False, False, 0)
        button.add(content)
        button.capture_badge = badge_label
        button.connect('toggled', self._on_capture_mode_toggled, mode)
        return button

    def _build_capture_inventory_controls(self, body):
        self._section(
            body, _('Session change inventory'),
            _('Analysis is never automatic. It asks savechanges for '
              'metadata-only inventory through pkexec when authorization is '
              'needed, then keeps the validated result in memory only.'))
        action_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.inventory_analyze_button = Gtk.Button(
            label=_('Analyze session changes'))
        self.inventory_analyze_button.set_image(Gtk.Image.new_from_icon_name(
            'system-search-symbolic', Gtk.IconSize.BUTTON))
        self.inventory_analyze_button.get_style_context().add_class(
            'minios-text-button')
        self.inventory_analyze_button.connect(
            'clicked', self._on_analyze_session)
        action_row.pack_start(
            self.inventory_analyze_button, False, False, 0)
        self.inventory_spinner = Gtk.Spinner()
        action_row.pack_start(self.inventory_spinner, False, False, 2)
        self.inventory_cancel_button = Gtk.Button(label=_('Cancel analysis'))
        self.inventory_cancel_button.get_style_context().add_class(
            'destructive-action')
        self.inventory_cancel_button.connect(
            'clicked', self._on_cancel_inventory)
        action_row.pack_end(
            self.inventory_cancel_button, False, False, 0)
        body.pack_start(action_row, False, False, 0)

        self.inventory_status_label = Gtk.Label(xalign=0)
        self.inventory_status_label.set_line_wrap(True)
        self.inventory_status_label.set_line_wrap_mode(
            Pango.WrapMode.WORD_CHAR)
        self.inventory_status_label.get_style_context().add_class(
            'field-description')
        body.pack_start(self.inventory_status_label, False, False, 0)

        self.inventory_summary_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.inventory_summary_box.get_style_context().add_class(
            'inventory-summary')
        body.pack_start(self.inventory_summary_box, False, False, 0)

    def _build_selected_capture_controls(self, body):
        self.selected_capture_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._section(
            self.selected_capture_box, _('Select analyzed changes'),
            _('Paths are sensitive metadata. They are shown only in this '
              'in-memory selector and are never copied into Review or logs. '
              'A selected directory represents its descendants; savechanges '
              'enforces the final match.'))

        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.capture_search_entry = Gtk.SearchEntry()
        self.capture_search_entry.set_placeholder_text(
            _('Filter inventory paths'))
        self.capture_search_entry.set_hexpand(True)
        self.capture_search_entry.connect(
            'search-changed', self._on_capture_search_changed)
        tools.pack_start(self.capture_search_entry, True, True, 0)
        self.capture_category_filter = Gtk.ComboBoxText()
        self.capture_category_filter.append('all', _('All entries'))
        self.capture_category_filter.append(
            'recommended', _('Recommended safe'))
        for category in sorted(CAPTURE_CATEGORY_TITLES):
            self.capture_category_filter.append(
                category, CAPTURE_CATEGORY_TITLES[category])
        self.capture_category_filter.set_active_id('all')
        self.capture_category_filter.connect(
            'changed', self._on_capture_category_changed)
        tools.pack_start(self.capture_category_filter, False, False, 0)
        self.capture_clear_button = Gtk.Button(label=_('Clear selection'))
        self.capture_clear_button.connect(
            'clicked', self._on_clear_capture_selection)
        tools.pack_end(self.capture_clear_button, False, False, 0)
        self.selected_capture_box.pack_start(tools, False, False, 0)

        self.capture_selection_status = Gtk.Label(xalign=0)
        self.capture_selection_status.set_line_wrap(True)
        self.capture_selection_status.get_style_context().add_class(
            'field-description')
        self.selected_capture_box.pack_start(
            self.capture_selection_status, False, False, 0)

        rules = Gtk.Expander(label=_('Advanced include/exclude rules'))
        rules.get_style_context().add_class('advanced-expander')
        rules_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=7)
        _set_margins(rules_box, top=8, bottom=4, start=2, end=2)
        rules_detail = Gtk.Label(
            label=_('One normalized relative path per line. Exclusions are '
                    'persisted project intent and override matching includes '
                    'for that path and its descendants.'), xalign=0)
        rules_detail.set_line_wrap(True)
        rules_detail.get_style_context().add_class('field-description')
        rules_box.pack_start(rules_detail, False, False, 0)
        rules_grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        rules_grid.set_column_homogeneous(True)
        for column, (title, attribute) in enumerate((
                (_('Include paths'), 'capture_include_editor'),
                (_('Exclude paths'), 'capture_exclude_editor'))):
            label = Gtk.Label(label=title, xalign=0)
            label.get_style_context().add_class('field-label')
            rules_grid.attach(label, column, 0, 1, 1)
            editor = Gtk.TextView()
            editor.set_monospace(True)
            editor.set_wrap_mode(Gtk.WrapMode.NONE)
            editor.set_left_margin(5)
            editor.set_right_margin(5)
            editor.set_size_request(-1, 82)
            editor.get_style_context().add_class('capture-rule-editor')
            editor_scroll = Gtk.ScrolledWindow()
            editor_scroll.set_policy(
                Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            editor_scroll.set_shadow_type(Gtk.ShadowType.IN)
            editor_scroll.add(editor)
            rules_grid.attach(editor_scroll, column, 1, 1, 1)
            setattr(self, attribute, editor)
        rules_box.pack_start(rules_grid, False, False, 0)
        rules_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.capture_rules_apply_button = Gtk.Button(
            label=_('Apply selection rules'))
        self.capture_rules_apply_button.connect(
            'clicked', self._on_apply_capture_rules)
        rules_actions.pack_start(
            self.capture_rules_apply_button, False, False, 0)
        self.capture_rules_status = Gtk.Label(xalign=0)
        self.capture_rules_status.set_line_wrap(True)
        self.capture_rules_status.get_style_context().add_class(
            'field-description')
        rules_actions.pack_start(
            self.capture_rules_status, True, True, 0)
        rules_box.pack_start(rules_actions, False, False, 0)
        rules.add(rules_box)
        self.selected_capture_box.pack_start(rules, False, False, 0)

        self.capture_store = Gtk.ListStore(
            bool, str, str, str, bool, bool, object, bool, str)
        self.capture_tree = Gtk.TreeView(model=self.capture_store)
        self.capture_tree.set_headers_visible(True)
        self.capture_tree.set_fixed_height_mode(True)

        toggle = Gtk.CellRendererToggle()
        toggle.connect('toggled', self._on_capture_entry_toggled)
        toggle_column = Gtk.TreeViewColumn('', toggle)
        toggle_column.add_attribute(
            toggle, 'active', CAPTURE_STORE_SELECTED)
        toggle_column.add_attribute(
            toggle, 'activatable', CAPTURE_STORE_ELIGIBLE)
        toggle_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        toggle_column.set_fixed_width(38)
        self.capture_tree.append_column(toggle_column)

        path_renderer = Gtk.CellRendererText()
        path_renderer.set_property('ellipsize', Pango.EllipsizeMode.MIDDLE)
        path_column = Gtk.TreeViewColumn(_('Path'), path_renderer,
                                         text=CAPTURE_STORE_PATH)
        path_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        path_column.set_fixed_width(280)
        path_column.set_expand(True)
        path_column.set_cell_data_func(
            path_renderer, self._capture_path_cell_data)
        self.capture_tree.append_column(path_column)

        for title, column_id, width in (
                (_('Category'), CAPTURE_STORE_CATEGORY, 120),
                (_('Type / size'), CAPTURE_STORE_DETAIL, 110)):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=column_id)
            column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            column.set_fixed_width(width)
            self.capture_tree.append_column(column)

        sensitive_renderer = Gtk.CellRendererText()
        sensitive_column = Gtk.TreeViewColumn(
            _('Sensitivity'), sensitive_renderer)
        sensitive_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        sensitive_column.set_fixed_width(92)
        sensitive_column.set_cell_data_func(
            sensitive_renderer, self._capture_sensitive_cell_data)
        self.capture_tree.append_column(sensitive_column)

        inventory_scroll = Gtk.ScrolledWindow()
        inventory_scroll.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        inventory_scroll.set_shadow_type(Gtk.ShadowType.IN)
        inventory_scroll.set_size_request(-1, 250)
        inventory_scroll.add(self.capture_tree)
        self.selected_capture_box.pack_start(
            inventory_scroll, False, False, 0)

        display_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.capture_display_status = Gtk.Label(xalign=0)
        self.capture_display_status.set_line_wrap(True)
        self.capture_display_status.get_style_context().add_class(
            'field-description')
        display_row.pack_start(
            self.capture_display_status, True, True, 0)
        self.capture_load_more_button = Gtk.Button(label=_('Load more'))
        self.capture_load_more_button.connect(
            'clicked', self._on_capture_load_more)
        display_row.pack_end(
            self.capture_load_more_button, False, False, 0)
        self.selected_capture_box.pack_start(display_row, False, False, 0)
        body.pack_start(self.selected_capture_box, False, False, 0)

    def _sync_defaults_widgets(self):
        self._syncing = True
        self.menu_combo.set_active_id(self.state.menu_locale)
        self.volume_entry.set_text(self.state.volume_label)
        self.output_entry.set_text(self.state.output_path)
        for key, widget in self.live_config_widgets.items():
            value = self.state.live_config_overrides.get(key)
            if isinstance(widget, Gtk.Entry):
                widget.set_text(value or '')
            else:
                if key == 'DEFAULT_TARGET':
                    value = {
                        'graphical': 'graphical.target',
                        'multi-user': 'multi-user.target',
                        'rescue': 'rescue.target',
                    }.get(value, value)
                widget.set_active_id(value or 'keep')
        self._sync_security_preset()
        self._sync_boot_menu_editor()
        preserve_timeout = self.state.boot_timeout is None
        source_menu = self._source_boot_menu_settings()
        source_timeout = (source_menu.get('timeout') if source_menu and
                          source_menu.get('timeout_known') else None)
        self.boot_timeout_preserve.set_label(
            _('Preserve source ({seconds} seconds)').format(
                seconds=source_timeout)
            if preserve_timeout and source_timeout is not None else
            _('Preserve source'))
        self.boot_timeout_preserve.set_active(preserve_timeout)
        self.boot_timeout_spin.set_sensitive(not preserve_timeout)
        if self.state.boot_timeout is not None:
            self.boot_timeout_spin.set_value(self.state.boot_timeout)
        elif source_timeout is not None:
            self.boot_timeout_spin.set_value(source_timeout)
        self.kernel_args_entry.set_text(self.state.kernel_args or '')
        self.customization_status.set_text('')
        self.customization_status.hide()
        capture_button = self.capture_mode_buttons.get(self.state.capture_mode)
        if capture_button is not None:
            capture_button.set_active(True)
        self.capture_compression_combo.set_active_id(
            self.state.capture_compression)
        self.capture_ack_check.set_active(
            self.state.sensitive_capture_acknowledged)
        if len(self.state.exclusions) == 1:
            self.exclusion_entry.set_text(self.state.exclusions[0])
            self.exclusion_load_warning.hide()
        elif len(self.state.exclusions) > 1:
            self.exclusion_entry.set_text('')
            self.exclusion_load_warning.set_text(
                _('This project contains multiple exclusion expressions, '
                  'which the provider cannot represent. Entering a new '
                  'expression replaces them.'))
            self.exclusion_load_warning.show()
        else:
            self.exclusion_entry.set_text('')
            self.exclusion_load_warning.hide()
        notes = self.notes_view.get_buffer()
        notes.set_text(self.state.notes)
        self._syncing = False
        self._sync_capture_rule_editors()
        self._sync_inventory_view()
        self._render_customization_metadata()
        self._render_capture_controls()

    def _sync_capture_rule_editors(self):
        values = (
            (self.capture_include_editor,
             '\n'.join(self.state.capture_include_paths)),
            (self.capture_exclude_editor,
             '\n'.join(self.state.capture_exclude_paths)),
        )
        for editor, text in values:
            buffer_ = editor.get_buffer()
            current = buffer_.get_text(
                buffer_.get_start_iter(), buffer_.get_end_iter(), False)
            if current != text:
                buffer_.set_text(text)

    def _sync_inventory_view(self):
        inventory = self.state.session_inventory
        if self.inventory_view.inventory is not inventory:
            self.inventory_view.set_inventory(inventory)
            self._capture_store_signature = None

    def _clear_inventory_presentation(self):
        if self._inventory_search_source is not None:
            GLib.source_remove(self._inventory_search_source)
            self._inventory_search_source = None
        self.inventory_view.clear()
        self._capture_store_signature = None
        self.capture_store.clear()
        self._syncing = True
        self.capture_search_entry.set_text('')
        self.capture_category_filter.set_active_id('all')
        self._syncing = False
        self.capture_display_status.set_text('')
        self.capture_load_more_button.set_sensitive(False)

    def _clear_runtime_inventory(self):
        clear_capture_runtime(self.state, self.inventory_view)
        if hasattr(self, 'capture_store'):
            self._clear_inventory_presentation()

    def _render_capture_controls(self):
        mode = self.state.capture_mode
        status = self.state.capture_capability_status
        available = bool(status.get('available'))
        inventory_busy = self._operation in ('inventory', 'inventory-load')
        for name, button in self.capture_mode_buttons.items():
            button.set_sensitive(
                not inventory_busy and
                (name == backend.NO_SESSION_CAPTURE or available))

        reasons = status.get('reason_codes', ())
        reason_messages = {
            'not-probed': _(
                'Checking savechanges and pkexec capture support…'),
            'probe-failed': _(
                'Capture capability probing completed unsuccessfully. Upgrade '
                'minios-tools to 1.5.0 or newer, then refresh Source.'),
            'savechanges-unavailable': _(
                'Session capture needs trusted /usr/bin/savechanges from '
                'minios-tools 1.5.0 or newer.'),
            'savechanges-version-probe-failed': _(
                'The installed savechanges version probe failed. Upgrade or '
                'reinstall minios-tools 1.5.0 or newer.'),
            'authorization-unavailable': _(
                'Session capture needs root or trusted /usr/bin/pkexec. '
                'Non-root desktops also need a running polkit authentication '
                'agent. Building without session changes remains fully available.'),
        }
        if reasons:
            self.capture_capability_warning.set_text(
                ' '.join(reason_messages.get(reason, reason)
                         for reason in reasons))
            self.capture_capability_warning.show()
        elif self.capture_probe_error:
            self.capture_capability_warning.set_text(
                _('Capture capability probing failed. Building without session changes '
                  'remains available; refresh Source to retry.'))
            self.capture_capability_warning.show()
        else:
            self.capture_capability_warning.hide()

        privilege_mode = status.get('privilege_mode')
        for name, button in self.capture_mode_buttons.items():
            badge = button.capture_badge
            context = badge.get_style_context()
            for class_name in (
                    'badge-success', 'badge-warning', 'badge-error'):
                context.remove_class(class_name)
            if name == backend.NO_SESSION_CAPTURE:
                badge.set_text(_('Recommended').upper())
                context.add_class('badge-success')
            elif not available:
                badge.set_text(_('Unavailable').upper())
                context.add_class('badge-error')
            elif privilege_mode == 'direct':
                badge.set_text(_('Ready').upper())
                context.add_class('badge-success')
            else:
                badge.set_text(_('Admin access').upper())
                context.add_class('badge-warning')

        session_mode = mode in backend.SESSION_CAPTURE_MODES
        self.capture_compression_block.set_visible(session_mode)
        self.capture_compression_combo.set_sensitive(
            available and not inventory_busy)
        self.capture_ack_check.set_visible(mode == 'exact')
        self.capture_ack_check.set_sensitive(
            available and not inventory_busy)

        self.inventory_analyze_button.set_sensitive(
            session_mode and available and not inventory_busy and
            self.runner is None)
        self.inventory_cancel_button.set_visible(inventory_busy)
        self.inventory_cancel_button.set_sensitive(inventory_busy)
        if inventory_busy:
            self.inventory_spinner.start()
            self.inventory_spinner.show()
        else:
            self.inventory_spinner.stop()
            self.inventory_spinner.hide()

        message = self._inventory_message
        if not message:
            if not session_mode:
                message = _(
                    'This option does not inspect or capture writable '
                    'session changes.')
            elif self.state.session_inventory is None:
                message = _(
                    'No inventory is in memory. Include all session changes and Include reusable changes only can '
                    'still be reviewed with an unknown estimate; Selected '
                    'changes requires analysis before creating a selection.')
            else:
                message = _('A validated inventory is available in memory.')
        self.inventory_status_label.set_text(message)
        self._render_inventory_summary()

        self.selected_capture_box.set_visible(mode == 'selected')
        if mode == 'selected':
            self._sync_capture_rule_editors()
            self._populate_capture_store()
        self.capture_search_entry.set_sensitive(not inventory_busy)
        self.capture_category_filter.set_sensitive(not inventory_busy)
        self.capture_rules_apply_button.set_sensitive(not inventory_busy)
        self.capture_include_editor.set_sensitive(not inventory_busy)
        self.capture_exclude_editor.set_sensitive(not inventory_busy)
        self.capture_clear_button.set_sensitive(bool(
            self.state.capture_include_paths or
            self.state.capture_exclude_paths) and not inventory_busy)
        if inventory_busy:
            self.capture_load_more_button.set_sensitive(False)

    def _render_inventory_summary(self):
        _clear(self.inventory_summary_box)
        self._sync_inventory_view()
        summary = self.inventory_view.summary
        if summary is None:
            self.inventory_summary_box.hide()
            return
        categories = ', '.join(
            _('{category}: {count}').format(
                category=CAPTURE_CATEGORY_TITLES.get(category, category),
                count=count)
            for category, count in sorted(
                summary['category_counts'].items()) if count)
        byte_value = self._size_value(summary['regular_bytes'])
        if summary['unknown_regular_sizes']:
            byte_value = _('{size} known; {count} file sizes unknown').format(
                size=byte_value,
                count=summary['unknown_regular_sizes'])
        self.inventory_summary_box.pack_start(self._key_value_grid((
            (_('Union backend'), summary['union_backend']),
            (_('Inventory entries'), summary['entry_count']),
            (_('Regular-file bytes'), byte_value),
            (_('Categories'), categories or _('None')),
            (_('Sensitive entries'), summary['sensitive_count']),
            (_('Exact defaults'), summary['exact_default_count']),
            (_('Reusable-change defaults'), summary['clean_default_count']),
        )), False, False, 0)
        privacy = Gtk.Label(
            label=_('Filenames are sensitive metadata. This inventory is not '
                    'saved in the project and remains in memory only.'),
            xalign=0)
        privacy.set_line_wrap(True)
        privacy.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        privacy.get_style_context().add_class('privacy-note')
        self.inventory_summary_box.pack_start(privacy, False, False, 0)
        self.inventory_summary_box.show_all()

    def _populate_capture_store(self):
        self._sync_inventory_view()
        inventory = self.state.session_inventory
        signature = (
            inventory.document_sha256 if inventory is not None else None,
            self.inventory_view.search_text,
            self.inventory_view.category,
            self.inventory_view.display_limit,
            self.state.capture_include_paths,
            self.state.capture_exclude_paths,
        )
        if signature == self._capture_store_signature:
            self._update_capture_selection_status()
            self._update_capture_display_status()
            return
        self.capture_store.clear()
        selected = set(self.state.capture_include_paths)
        excludes = self.state.capture_exclude_paths
        if inventory is not None:
            for entry in self.inventory_view.visible_entries():
                if entry.type == 'regular':
                    size = (human_size(entry.size) if entry.size is not None
                            else _('unknown size'))
                    detail = _('{kind}, {size}').format(
                        kind=CAPTURE_TYPE_TITLES[entry.type], size=size)
                else:
                    detail = CAPTURE_TYPE_TITLES.get(entry.type, entry.type)
                excluded_by = capture_path_excluded_by(
                    entry.path, excludes)
                eligible = bool(
                    entry.default_exact and entry.type != 'unsupported' and
                    excluded_by is None)
                self.capture_store.append((
                    entry.path in selected and excluded_by is None,
                    entry.path,
                    CAPTURE_CATEGORY_TITLES.get(
                        entry.category, entry.category),
                    detail,
                    entry.sensitive,
                    eligible,
                    entry,
                    excluded_by is not None,
                    excluded_by or '',
                ))
        self._capture_store_signature = signature
        self._update_capture_selection_status()
        self._update_capture_display_status()

    def _update_capture_selection_status(self):
        inventory = self.state.session_inventory
        include_count = len(self.state.capture_include_paths)
        exclude_count = len(self.state.capture_exclude_paths)
        if inventory is None and include_count:
            text = _(
                'Loaded project selection: {includes} include paths and '
                '{excludes} exclude paths. Paths remain private; analyze to '
                'compare them with this session.').format(
                    includes=include_count, excludes=exclude_count)
        elif inventory is None:
            text = _(
                'Analyze session changes, then select at least one eligible '
                'path. Sensitive entries are never preselected.')
        else:
            text = _(
                '{count} include paths selected; {excludes} loaded exclude '
                'paths. Sensitive entries are never selected automatically.').format(
                    count=include_count, excludes=exclude_count)
        self.capture_selection_status.set_text(text)

    def _update_capture_display_status(self):
        if self.state.session_inventory is None:
            self.capture_display_status.set_text(
                _('No runtime inventory is displayed. Loaded selection rules '
                  'remain editable above.'))
            self.capture_load_more_button.set_sensitive(False)
            return
        displayed = self.inventory_view.displayed_count
        matched = self.inventory_view.matched_count
        total = self.inventory_view.total_count
        if self.inventory_view.display_cap_reached:
            text = _(
                'Showing the capped {displayed} of {matched} matching entries '
                '({total} total). Refine the search to inspect other rows.').format(
                    displayed=displayed, matched=matched, total=total)
        else:
            text = _(
                'Showing {displayed} of {matched} matching entries '
                '({total} total).').format(
                    displayed=displayed, matched=matched, total=total)
        self.capture_display_status.set_text(text)
        self.capture_load_more_button.set_sensitive(
            displayed < matched and not self.inventory_view.display_cap_reached)

    def _refresh_capture_row_states(self):
        selected = self.state.capture_include_paths
        excludes = self.state.capture_exclude_paths
        tree_iter = self.capture_store.get_iter_first()
        while tree_iter is not None:
            entry = self.capture_store.get_value(
                tree_iter, CAPTURE_STORE_ENTRY)
            excluded_by = capture_path_excluded_by(entry.path, excludes)
            eligible = bool(
                entry.default_exact and entry.type != 'unsupported' and
                excluded_by is None)
            self.capture_store.set_value(
                tree_iter, CAPTURE_STORE_SELECTED,
                capture_entry_selected(entry.path, selected, excludes))
            self.capture_store.set_value(
                tree_iter, CAPTURE_STORE_ELIGIBLE, eligible)
            self.capture_store.set_value(
                tree_iter, CAPTURE_STORE_EXCLUDED, excluded_by is not None)
            self.capture_store.set_value(
                tree_iter, CAPTURE_STORE_EXCLUDED_BY, excluded_by or '')
            tree_iter = self.capture_store.iter_next(tree_iter)
        self._capture_store_signature = None
        self._update_capture_selection_status()
        self._update_capture_display_status()
        self.capture_clear_button.set_sensitive(bool(
            self.state.capture_include_paths or
            self.state.capture_exclude_paths))

    def _capture_path_cell_data(self, _column, cell, model, tree_iter, _data):
        sensitive = model.get_value(tree_iter, CAPTURE_STORE_SENSITIVE)
        excluded = model.get_value(tree_iter, CAPTURE_STORE_EXCLUDED)
        cell.set_property(
            'weight', Pango.Weight.BOLD
            if sensitive and not excluded else Pango.Weight.NORMAL)
        cell.set_property('strikethrough', excluded)
        cell.set_property(
            'foreground', '#77767b' if excluded else
            '#c64600' if sensitive else None)

    def _capture_sensitive_cell_data(self, _column, cell, model,
                                     tree_iter, _data):
        sensitive = model.get_value(tree_iter, CAPTURE_STORE_SENSITIVE)
        excluded = model.get_value(tree_iter, CAPTURE_STORE_EXCLUDED)
        cell.set_property(
            'text', _('EXCLUDED') if excluded else
            _('SENSITIVE') if sensitive else '')
        cell.set_property('weight',
                          Pango.Weight.BOLD
                          if sensitive or excluded else Pango.Weight.NORMAL)
        cell.set_property(
            'foreground', '#77767b' if excluded else
            '#c64600' if sensitive else None)

    def _on_menu_changed(self, combo):
        if self._syncing or not combo.get_active_id():
            return
        changed = self.state.set_menu_locale(combo.get_active_id())
        entries = (
            [dict(item) for item in self.state.boot_menu_entries]
            if self.state.boot_menu_entries is not None else [])
        self._set_customization_error(
            'boot-menu', self._boot_menu_locale_error(entries) if entries else None)
        self._sync_boot_menu_editor()
        if changed:
            self._intent_changed()

    def _on_volume_changed(self, entry):
        if not self._syncing:
            if self.state.set_volume_label(entry.get_text()):
                self._intent_changed()

    def _on_output_changed(self, entry):
        if not self._syncing:
            if self.state.set_output_path(entry.get_text().strip()):
                self._intent_changed()

    def _set_customization_error(self, key, error):
        changed = self.state.set_customization_input_error(key, error)
        if error:
            self.customization_status.set_text(
                _('Customization value is invalid: {error}').format(
                    error=error))
            self.customization_status.show()
        else:
            self.customization_status.set_text('')
            self.customization_status.hide()
        if changed:
            self._invalidate_plan()
            self._update_chrome()
        return changed

    def _apply_live_config_override(self, key, value):
        overrides = dict(self.state.live_config_overrides)
        if value is None:
            overrides.pop(key, None)
        else:
            overrides[key] = value
        other_key = {
            'LIVE_LINK_USER_DIRS': 'LIVE_BIND_USER_DIRS',
            'LIVE_BIND_USER_DIRS': 'LIVE_LINK_USER_DIRS',
        }.get(key)
        if value == 'true' and other_key:
            overrides.pop(other_key, None)
            other = self.live_config_widgets.get(other_key)
            if other is not None and other.get_active_id() == 'true':
                self._syncing = True
                other.set_active_id('keep')
        self._syncing = False
        try:
            changed = self.state.set_live_config_overrides(overrides)
        except (TypeError, ValueError) as error:
            self._set_customization_error(key, error)
            return False
        error_changed = self._set_customization_error(key, None)
        if changed:
            self._intent_changed()
        elif error_changed:
            self._update_chrome()
        return changed

    def _on_live_config_entry_changed(self, entry, key):
        if self._syncing:
            return
        value = entry.get_text().strip()
        self._apply_live_config_override(key, value or None)

    def _on_live_config_choice_changed(self, combo, key):
        if self._syncing:
            return
        value = combo.get_active_id()
        if value is None:
            return
        self._apply_live_config_override(
            key, None if value == 'keep' else value)
        if key in SECURITY_PROFILE_KEYS and not self._applying_security_preset:
            self._sync_security_preset()

    def _on_security_preset_changed(self, combo):
        if self._syncing or self._applying_security_preset:
            return
        profile = combo.get_active_id() or ''
        if not profile:
            return
        values = security_profile_values(profile)
        if not values:
            return
        self._applying_security_preset = True
        try:
            for key in SECURITY_PROFILE_KEYS:
                value = values.get(key)
                widget = self.live_config_widgets.get(key)
                if value is None or widget is None:
                    continue
                widget.set_active_id(value)
                self._apply_live_config_override(key, value)
        finally:
            self._applying_security_preset = False

    def _matching_security_preset(self):
        overrides = self.state.live_config_overrides
        for profile_id, _label in SECURITY_PROFILE_CHOICES:
            values = security_profile_values(profile_id)
            if values and all(
                    overrides.get(key) == value
                    for key, value in values.items()):
                return profile_id
        return ''

    def _sync_security_preset(self):
        if not hasattr(self, 'security_preset_combo'):
            return
        self._applying_security_preset = True
        try:
            self.security_preset_combo.set_active_id(
                self._matching_security_preset())
        finally:
            self._applying_security_preset = False

    def _boot_menu_editor_entries(self):
        if not getattr(self, 'boot_menu_order', None):
            if self.state.boot_menu_entries is not None:
                return [dict(item) for item in self.state.boot_menu_entries]
            return self._source_boot_menu_editor_entries()
        entries = []
        for entry_id in self.boot_menu_order:
            row = self.boot_menu_rows[entry_id]
            title = row['title'].get_text().strip()
            entries.append({
                'id': entry_id,
                'base_mode': row['template'].get_active_id() or 'fresh',
                'enabled': row['enabled'].get_active(),
                'default': row['default'].get_active(),
                'title': title or None,
                'kernel_args': compile_boot_parameters(
                    self._boot_menu_row_settings(row)),
            })
            if row.get('kernel_args_schema') is not None:
                entries[-1]['kernel_args_schema'] = row['kernel_args_schema']
        return entries

    def _boot_menu_row_settings(self, row):
        settings = {}
        for key, widget in row['option_widgets'].items():
            if isinstance(widget, Gtk.Entry):
                settings[key] = widget.get_text().strip()
            elif isinstance(widget, Gtk.CheckButton):
                settings[key] = widget.get_active()
            else:
                settings[key] = widget.get_active_id() or 'keep'
        return settings

    def _boot_menu_option_summary(self, row):
        values = self._boot_menu_row_settings(row)
        details = []
        persistence = {
            'native': _('directory persistence'),
            'dynfilefs': _('expandable persistence'),
            'raw': _('fixed-size persistence'),
            'luks': _('encrypted persistence'),
            'squashfs': _('SquashFS session'),
        }.get(values['persistence_mode'])
        if persistence:
            details.append(persistence)
        if values['persistence_size']:
            details.append(_('persistence size {size}').format(
                size=values['persistence_size']))
        if values['ram_copy'] == 'full':
            details.append(_('entire system in RAM'))
        elif values['ram_copy'] == 'trim':
            details.append(_('loaded modules in RAM'))
        if values['load_modules'] or values['skip_modules']:
            details.append(_('module filter'))
        if values['startup'] == 'text':
            details.append(_('text console'))
        elif values['startup'] == 'graphical.target':
            details.append(_('graphical desktop'))
        elif values['startup'] == 'rescue.target':
            details.append(_('rescue mode'))
        if values['graphics'] == 'nomodeset':
            details.append(_('compatible graphics'))
        if values['automount']:
            details.append(_('automatic disk mounting'))
        if values['zram'] == 'off':
            details.append(_('zRAM disabled'))
        elif (values['zram_compression'] != 'keep' or
              values['zram_size']):
            details.append(_('custom zRAM'))
        if values['locale'] or values['timezone'] or values['keyboard']:
            details.append(_('custom language settings'))
        if values['quiet']:
            details.append(_('quiet boot'))
        if values['debug']:
            details.append(_('diagnostic logging'))
        if values['extra']:
            details.append(_('additional expert options'))
        return (', '.join(details) if details else
                _('Uses the template defaults.'))

    def _refresh_boot_menu_row(self, row):
        mode = row['template'].get_active_id() or 'fresh'
        row['template_detail'].set_text(BOOT_MODE_DESCRIPTIONS[mode])
        self._refresh_boot_menu_option_dependencies(row)
        row['option_summary'].set_text(self._boot_menu_option_summary(row))

    def _refresh_boot_menu_option_dependencies(self, row):
        widgets = row['option_widgets']
        persistence_mode = widgets['persistence_mode'].get_active_id() or 'keep'
        widgets['persistence_size'].set_sensitive(
            persistence_mode not in ('native', 'squashfs'))
        zram_enabled = (widgets['zram'].get_active_id() or 'keep') != 'off'
        widgets['zram_compression'].set_sensitive(zram_enabled)
        widgets['zram_size'].set_sensitive(zram_enabled)

    def _boot_menu_locale_error(self, entries):
        titles = [item.get('title') for item in entries if item.get('title')]
        if self.state.menu_locale == 'multilang':
            if any(any(ord(character) >= 128 for character in title) for title in titles):
                return _(
                    'Multilingual custom entry names must use ASCII because '
                    'native SYSLINUX language menus use different encodings.')
            return None
        info = self.state.source_info
        bootloader = (info.metadata.get('bootloader')
                      if info is not None else None)
        if bootloader != 'syslinux-native':
            return None
        codec = 'cp866' if self.state.menu_locale == 'ru_RU' else 'iso-8859-1'
        for title in titles:
            try:
                title.encode(codec, 'strict')
            except UnicodeError:
                return _(
                    'A custom entry name cannot be represented by the '
                    'selected native SYSLINUX menu encoding.')
        return None

    def _sync_default_boot_choices(self):
        custom = self.state.boot_menu_entries is not None
        self.default_boot_combo.remove_all()
        if custom:
            default_entry = next(
                (item for item in self.state.boot_menu_entries
                 if item['enabled'] and item['default']), None)
            if default_entry is not None:
                title = (default_entry.get('title') or
                         DEFAULT_BOOT_TITLES[default_entry['base_mode']])
                self.default_boot_combo.append(
                    'constructor',
                    _('Set in menu constructor: {title}').format(title=title))
                self.default_boot_combo.set_active_id('constructor')
            self.default_boot_combo.set_sensitive(False)
            return
        self.default_boot_combo.set_sensitive(True)
        source_menu = self._source_boot_menu_settings()
        source_default = None
        if source_menu and source_menu.get('default_known'):
            source_default = next(
                (item for item in source_menu['entries'] if item['default']),
                None)
        preserve_title = _('Preserve source')
        if source_default is not None:
            title = (source_default.get('title') or
                     DEFAULT_BOOT_TITLES[source_default['base_mode']])
            preserve_title = _('Preserve source ({title})').format(title=title)
        self.default_boot_combo.append('preserve', preserve_title)
        for mode in backend.DEFAULT_BOOT_MODES:
            self.default_boot_combo.append(mode, DEFAULT_BOOT_TITLES[mode])
        current = self.state.default_boot
        self.default_boot_combo.set_active_id(
            current if current in backend.DEFAULT_BOOT_MODES else 'preserve')

    def _sync_boot_menu_editor(self):
        if not hasattr(self, 'boot_menu_rows_box'):
            return
        previous_syncing = self._syncing
        self._syncing = True
        try:
            entries = (
                [dict(item) for item in self.state.boot_menu_entries]
                if self.state.boot_menu_entries is not None
                else self._source_boot_menu_editor_entries())
            _clear(self.boot_menu_rows_box)
            self.boot_menu_rows = {}
            self.boot_menu_field_label_group = Gtk.SizeGroup(
                Gtk.SizeGroupMode.HORIZONTAL)
            self.boot_menu_order = [item['id'] for item in entries]
            default_group = None
            for position, item in enumerate(entries):
                row = self._create_boot_menu_row(item, default_group)
                if default_group is None:
                    default_group = row['default']
                self.boot_menu_rows[item['id']] = row
                row['enabled'].set_active(bool(item['enabled']))
                row['default'].set_active(bool(item['default']))
                row['template'].set_active_id(item['base_mode'])
                row['title'].set_text(item.get('title') or '')
                settings = parse_boot_parameters(item.get('kernel_args') or '')
                for key, widget in row['option_widgets'].items():
                    value = settings[key]
                    if isinstance(widget, Gtk.Entry):
                        widget.set_text(value)
                    elif isinstance(widget, Gtk.CheckButton):
                        widget.set_active(bool(value))
                    else:
                        widget.set_active_id(value)
                self._refresh_boot_menu_row(row)
                row['up'].set_sensitive(position > 0)
                row['down'].set_sensitive(position + 1 < len(entries))
                row['remove'].set_no_show_all(
                    item['id'] in backend.DEFAULT_BOOT_MODES)
                self.boot_menu_rows_box.pack_start(
                    row['frame'], False, False, 0)
            self.boot_menu_rows_box.show_all()
            for entry_id in backend.DEFAULT_BOOT_MODES:
                row = self.boot_menu_rows.get(entry_id)
                if row is not None:
                    row['remove'].hide()
            if self.state.boot_menu_entries is None:
                self.boot_menu_status.set_text(_(
                    'The source menu is currently preserved. Changing an '
                    'entry, its order, or its parameters creates a custom menu.'))
            else:
                enabled = [item for item in entries if item['enabled']]
                default_entry = next(
                    (item for item in enabled if item['default']), None)
                default_title = (
                    default_entry.get('title') or
                    DEFAULT_BOOT_TITLES[default_entry['base_mode']]
                    if default_entry else _('None'))
                self.boot_menu_status.set_text(_(
                    '{total} entries, {shown} shown. Default: {default}.').format(
                        total=len(entries), shown=len(enabled),
                        default=default_title))
            self._sync_default_boot_choices()
        finally:
            self._syncing = previous_syncing

    def _apply_boot_menu_editor(self, resync=False):
        if self._syncing:
            return False
        entries = self._boot_menu_editor_entries()
        try:
            changed = self.state.set_boot_menu_entries(entries)
        except (TypeError, ValueError) as error:
            self._set_customization_error('boot-menu', error)
            self._sync_boot_menu_editor()
            return False
        locale_error = self._boot_menu_locale_error(entries)
        self._set_customization_error('boot-menu', locale_error)
        if resync:
            self._sync_boot_menu_editor()
        else:
            previous_syncing = self._syncing
            self._syncing = True
            try:
                self._sync_default_boot_choices()
            finally:
                self._syncing = previous_syncing
        if changed:
            self._intent_changed()
        return changed

    def _on_boot_menu_enabled_toggled(self, check, entry_id):
        if self._syncing:
            return
        row = self.boot_menu_rows.get(entry_id)
        if row is None:
            return
        if not check.get_active() and row['default'].get_active():
            replacement = next(
                (self.boot_menu_rows[other]['default']
                 for other in self.boot_menu_order
                 if other != entry_id and
                 self.boot_menu_rows[other]['enabled'].get_active()), None)
            if replacement is None:
                self._syncing = True
                check.set_active(True)
                self._syncing = False
                self._set_customization_error(
                    'boot-menu', _('At least one boot menu entry must remain enabled.'))
                return
            replacement.set_active(True)
        self._apply_boot_menu_editor()

    def _on_boot_menu_default_toggled(self, radio, entry_id):
        if self._syncing or not radio.get_active():
            return
        row = self.boot_menu_rows.get(entry_id)
        if row is None:
            return
        if not row['enabled'].get_active():
            self._syncing = True
            row['enabled'].set_active(True)
            self._syncing = False
        self._apply_boot_menu_editor()

    def _on_boot_menu_base_changed(self, _combo, entry_id):
        row = self.boot_menu_rows.get(entry_id)
        if row is not None:
            self._refresh_boot_menu_row(row)
        self._apply_boot_menu_editor()

    def _on_boot_menu_title_changed(self, _entry, _entry_id):
        self._apply_boot_menu_editor()

    def _on_boot_menu_options_changed(self, _widget, entry_id):
        if self._syncing:
            return
        row = self.boot_menu_rows.get(entry_id)
        if row is not None:
            self._refresh_boot_menu_row(row)
        self._apply_boot_menu_editor()

    def _on_boot_menu_move(self, _button, entry_id, delta):
        if self._syncing or entry_id not in self.boot_menu_order:
            return
        entries = self._boot_menu_editor_entries()
        index = self.boot_menu_order.index(entry_id)
        target = index + delta
        if target < 0 or target >= len(entries):
            return
        entries[index], entries[target] = entries[target], entries[index]
        try:
            changed = self.state.set_boot_menu_entries(entries)
        except (TypeError, ValueError) as error:
            self._set_customization_error('boot-menu', error)
            return
        self._set_customization_error(
            'boot-menu', self._boot_menu_locale_error(entries))
        self._sync_boot_menu_editor()
        if changed:
            self._intent_changed()

    def _on_boot_menu_add(self, _button):
        entries = self._boot_menu_editor_entries()
        if len(entries) >= backend.BOOT_MENU_MAX_ENTRIES:
            self._set_customization_error(
                'boot-menu', _('The boot menu already has 32 entries.'))
            return
        try:
            entry_id = self._next_boot_menu_entry_id(entries)
        except ValueError as error:
            self._set_customization_error('boot-menu', error)
            return
        entries.append({
            'id': entry_id, 'base_mode': 'fresh', 'enabled': True,
            'default': False,
            'title': 'Custom MiniOS {}'.format(entry_id.split('-')[-1]),
            'kernel_args': '',
            'kernel_args_schema': (3 if self.state.menu_locale == 'multilang'
                                   else 2),
        })
        try:
            changed = self.state.set_boot_menu_entries(entries)
        except (TypeError, ValueError) as error:
            self._set_customization_error('boot-menu', error)
            return
        self._set_customization_error(
            'boot-menu', self._boot_menu_locale_error(entries))
        self._sync_boot_menu_editor()
        if changed:
            self._intent_changed()

    def _on_boot_menu_duplicate(self, _button, entry_id):
        entries = self._boot_menu_editor_entries()
        if len(entries) >= backend.BOOT_MENU_MAX_ENTRIES:
            self._set_customization_error(
                'boot-menu', _('The boot menu already has 32 entries.'))
            return
        source_index = next(
            (index for index, item in enumerate(entries)
             if item['id'] == entry_id), None)
        if source_index is None:
            return
        try:
            new_id = self._next_boot_menu_entry_id(entries)
        except ValueError as error:
            self._set_customization_error('boot-menu', error)
            return
        source = dict(entries[source_index])
        source.update({
            'id': new_id, 'enabled': True, 'default': False,
            'title': source.get('title') or 'Custom {}'.format(source['base_mode']),
        })
        entries.insert(source_index + 1, source)
        try:
            changed = self.state.set_boot_menu_entries(entries)
        except (TypeError, ValueError) as error:
            self._set_customization_error('boot-menu', error)
            return
        self._set_customization_error(
            'boot-menu', self._boot_menu_locale_error(entries))
        self._sync_boot_menu_editor()
        if changed:
            self._intent_changed()

    def _on_boot_menu_remove(self, _button, entry_id):
        if entry_id in backend.DEFAULT_BOOT_MODES:
            return
        entries = [item for item in self._boot_menu_editor_entries()
                   if item['id'] != entry_id]
        if not entries:
            return
        enabled = [item for item in entries if item['enabled']]
        if not enabled:
            entries[0]['enabled'] = True
            enabled = [entries[0]]
        if not any(item['default'] for item in enabled):
            for item in entries:
                item['default'] = item['id'] == enabled[0]['id']
        try:
            changed = self.state.set_boot_menu_entries(entries)
        except (TypeError, ValueError) as error:
            self._set_customization_error('boot-menu', error)
            return
        self._set_customization_error(
            'boot-menu', self._boot_menu_locale_error(entries))
        self._sync_boot_menu_editor()
        if changed:
            self._intent_changed()

    def _on_boot_menu_reset(self, _button):
        if self._syncing:
            return
        changed = self.state.set_boot_menu_entries(None)
        self._set_customization_error('boot-menu', None)
        self._sync_boot_menu_editor()
        if changed:
            self._intent_changed()


    def _on_boot_timeout_preserve_toggled(self, check):
        if self._syncing:
            return
        preserve = check.get_active()
        self.boot_timeout_spin.set_sensitive(not preserve)
        value = None if preserve else self.boot_timeout_spin.get_value_as_int()
        if self.state.set_boot_timeout(value):
            self._intent_changed()

    def _on_boot_timeout_changed(self, spin):
        if self._syncing or self.boot_timeout_preserve.get_active():
            return
        if self.state.set_boot_timeout(spin.get_value_as_int()):
            self._intent_changed()

    def _on_default_boot_changed(self, combo):
        if self._syncing or combo.get_active_id() is None:
            return
        value = combo.get_active_id()
        try:
            changed = self.state.set_default_boot(
                None if value == 'preserve' else value)
        except ValueError as error:
            self._set_customization_error('default-boot', error)
            self._sync_boot_menu_editor()
            return
        self._set_customization_error('default-boot', None)
        if changed:
            self._sync_boot_menu_editor()
            self._intent_changed()

    def _on_kernel_args_changed(self, entry):
        if self._syncing:
            return
        value = entry.get_text()
        value = value if value else None
        try:
            changed = self.state.set_kernel_args(value)
        except (TypeError, ValueError) as error:
            self._set_customization_error('kernel_args', error)
            return
        error_changed = self._set_customization_error('kernel_args', None)
        if changed:
            self._intent_changed()
        elif error_changed:
            self._update_chrome()

    def _on_choose_boot_background(self, _button):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose boot background PNG'), transient_for=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Choose'), Gtk.ResponseType.OK)
        file_filter = Gtk.FileFilter()
        file_filter.set_name(_('PNG images (*.png)'))
        file_filter.add_mime_type('image/png')
        file_filter.add_pattern('*.png')
        dialog.add_filter(file_filter)
        response = dialog.run()
        path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not path:
            return
        try:
            changed = self.state.set_boot_background_path(path)
        except Exception as error:
            show_error_dialog(
                self, _('The boot background could not be used.'), str(error))
            return
        if changed:
            self._intent_changed()
        self._render_customization_metadata()

    def _on_clear_boot_background(self, _button):
        if self.state.set_boot_background_path(None):
            self._intent_changed()
        self._render_customization_metadata()

    def _choose_overlay_parent(self):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose the project directory'), transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Choose'), Gtk.ResponseType.OK)
        if os.path.isdir(self.state.project_base):
            dialog.set_current_folder(self.state.project_base)
        response = dialog.run()
        path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        return path

    def _on_choose_overlay_directory(self, _button):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose an existing project filesystem layer'),
            transient_for=self, action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Choose'), Gtk.ResponseType.OK)
        if os.path.isdir(self.state.project_base):
            dialog.set_current_folder(self.state.project_base)
        response = dialog.run()
        path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not path:
            return
        try:
            changed = self.state.set_overlay_directory(path)
        except Exception as error:
            show_error_dialog(
                self, _('The project filesystem layer could not be used.'),
                str(error))
            return
        if changed:
            self._intent_changed()
        self._render_customization_metadata()

    def _on_create_overlay_directory(self, _button):
        parent = (self.state.project_base if self.state.project_path
                  else self._choose_overlay_parent())
        if not parent:
            return
        try:
            path = self.state.create_overlay_directory(parent)
        except Exception as error:
            show_error_dialog(
                self, _('The project filesystem layer could not be created.'),
                str(error))
            return
        self._intent_changed()
        self._render_customization_metadata()
        self._open_directory(path)

    def _open_directory(self, path):
        try:
            canonical = os.path.abspath(path)
            metadata = os.lstat(canonical)
            if (os.path.realpath(canonical) != canonical or
                    stat.S_ISLNK(metadata.st_mode) or
                    not stat.S_ISDIR(metadata.st_mode)):
                raise ValueError('directory is not a canonical real directory')
            uri = Gio.File.new_for_path(canonical).get_uri()
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as error:
            show_error_dialog(
                self, _('The directory could not be opened.'), str(error))

    def _on_open_overlay_directory(self, _button):
        if self.state.overlay_directory:
            self._open_directory(self.state.overlay_directory)

    def _on_clear_overlay_directory(self, _button):
        if self.state.set_overlay_directory(None):
            self._intent_changed()
        self._render_customization_metadata()

    def _render_customization_metadata(self):
        background = self.state.boot_background_metadata
        if background and self.state.boot_background_path:
            self.background_status.set_text(_(
                '{name} · {width}×{height} · {size} · SHA-256 {digest}').format(
                    name=os.path.basename(self.state.boot_background_path),
                    width=background.get('width'),
                    height=background.get('height'),
                    size=human_size(background.get('size')) or _('Unknown'),
                    digest=background.get('sha256') or _('Unavailable')))
        else:
            self.background_status.set_text(_('Preserve source background'))
        self.background_clear_button.set_sensitive(
            self.state.boot_background_path is not None)

        overlay = self.state.overlay_metadata
        if overlay and self.state.overlay_directory:
            self.overlay_status.set_text(_(
                '{name} · {entries} entries · {size} · fingerprint '
                '{fingerprint}').format(
                    name=os.path.basename(self.state.overlay_directory),
                    entries=overlay.get('entry_count'),
                    size=human_size(overlay.get('regular_bytes')) or _('0 B'),
                    fingerprint=(overlay.get('input_tree_fingerprint') or
                                 _('Unavailable'))))
        else:
            self.overlay_status.set_text(_('No project filesystem layer'))
        active = self.state.overlay_directory is not None
        self.overlay_open_button.set_sensitive(active)
        self.overlay_clear_button.set_sensitive(active)

    def _on_capture_mode_toggled(self, button, mode):
        if not self._syncing and button.get_active():
            if self.state.set_capture_mode(mode):
                self._intent_changed()
                self._inventory_message = ''
                if self.state.session_inventory is None:
                    self._clear_inventory_presentation()
                self._sync_capture_rule_editors()
            self._render_capture_controls()

    def _on_capture_compression_changed(self, combo):
        if not self._syncing and combo.get_active_id():
            if self.state.set_capture_compression(combo.get_active_id()):
                self._intent_changed()

    def _on_capture_ack_toggled(self, check):
        if not self._syncing:
            if self.state.set_sensitive_capture_acknowledged(
                    check.get_active()):
                self._intent_changed()
                self._render_capture_controls()

    def _on_capture_search_changed(self, _widget):
        if self._syncing:
            return
        if self._inventory_search_source is not None:
            GLib.source_remove(self._inventory_search_source)
        self._inventory_search_source = GLib.timeout_add(
            250, self._apply_capture_filter)

    def _on_capture_category_changed(self, _widget):
        if not self._syncing:
            if self._inventory_search_source is not None:
                GLib.source_remove(self._inventory_search_source)
                self._inventory_search_source = None
            self._apply_capture_filter()

    def _apply_capture_filter(self):
        self._inventory_search_source = None
        self.inventory_view.set_filter(
            self.capture_search_entry.get_text(),
            self.capture_category_filter.get_active_id() or 'all')
        self._capture_store_signature = None
        self._populate_capture_store()
        return False

    def _on_capture_load_more(self, _button):
        if self.inventory_view.load_more():
            self._capture_store_signature = None
            self._populate_capture_store()

    def _capture_editor_text(self, editor):
        buffer_ = editor.get_buffer()
        return buffer_.get_text(
            buffer_.get_start_iter(), buffer_.get_end_iter(), False)

    def _on_apply_capture_rules(self, _button):
        try:
            includes = parse_capture_rule_text(
                self._capture_editor_text(self.capture_include_editor))
            excludes = parse_capture_rule_text(
                self._capture_editor_text(self.capture_exclude_editor))
            changed = self.state.set_capture_paths(includes, excludes)
        except ValueError as error:
            self.capture_rules_status.set_text(
                _('Selection rules were not applied: {error}').format(
                    error=error))
            return
        self.capture_rules_status.set_text(
            _('Selection rules are valid and applied.'))
        if changed:
            self._intent_changed()
        self._sync_capture_rule_editors()
        self._refresh_capture_row_states()

    def _on_clear_capture_selection(self, _button):
        if self.state.set_capture_paths((), ()):
            self._intent_changed()
        self.capture_rules_status.set_text('')
        self._sync_capture_rule_editors()
        self._refresh_capture_row_states()

    def _on_capture_entry_toggled(self, _renderer, path):
        child_iter = self.capture_store.get_iter(path)
        entry = self.capture_store.get_value(
            child_iter, CAPTURE_STORE_ENTRY)
        excluded_by = self.capture_store.get_value(
            child_iter, CAPTURE_STORE_EXCLUDED_BY)
        if excluded_by:
            self.capture_rules_status.set_text(
                _('This row is blocked by an exclusion rule. Remove or edit '
                  'that rule above before selecting the row.'))
            return
        if not self.capture_store.get_value(
                child_iter, CAPTURE_STORE_ELIGIBLE):
            return
        include_paths = set(self.state.capture_include_paths)
        selected = self.capture_store.get_value(
            child_iter, CAPTURE_STORE_SELECTED)
        if selected:
            include_paths.discard(entry.path)
        else:
            include_paths.add(entry.path)
            if entry.type == 'directory':
                include_paths = set(
                    value for value in include_paths
                    if value == entry.path or
                    not value.startswith(entry.path + '/'))
        try:
            changed = self.state.set_capture_paths(
                include_paths, self.state.capture_exclude_paths)
        except ValueError as error:
            self.capture_rules_status.set_text(
                _('The selection could not be changed: {error}').format(
                    error=error))
            return
        if changed:
            self.capture_rules_status.set_text('')
            self._intent_changed()
            self._sync_capture_rule_editors()
            self._refresh_capture_row_states()

    def _on_analyze_session(self, _button):
        self._start_inventory_analysis()

    def _start_inventory_analysis(self):
        if (self._operation is not None or self.runner is not None or
                self.state.capture_mode not in backend.SESSION_CAPTURE_MODES or
                not self.state.capture_capability_status.get('available')):
            return
        self._clear_runtime_inventory()
        self._invalidate_plan()
        workspace = None
        try:
            workspace = create_inventory_workspace()
            argv = backend.build_session_inventory_command(
                workspace.output_path, cancel_file=workspace.cancel_path)
        except Exception:
            cleanup_inventory_workspace(workspace)
            self._inventory_status = 'error'
            self._inventory_message = _(
                'Could not prepare a private inventory analysis. Refresh '
                'Source and verify minios-tools installation.')
            self._render_capture_controls()
            return

        self._inventory_generation += 1
        generation = self._inventory_generation
        self._inventory_workspace = workspace
        self._inventory_status = 'running'
        self._inventory_message = _(
            'Analyzing metadata only. An authorization prompt may appear; '
            'inventory content and output paths are not logged.')
        self._operation = 'inventory'
        display_argv = redact_command_paths(
            argv, (workspace.output_path, workspace.cancel_path,
                   workspace.directory))
        self.runner = CommandRunner(
            list(argv), line_cb=lambda _line: None,
            on_finished=lambda returncode, cancelled:
                self._on_inventory_command_finished(
                    generation, returncode, cancelled),
            display_argv=display_argv)
        self.runner.start()
        self._render_capture_controls()
        self._update_chrome()

    def _on_cancel_inventory(self, _button):
        if self._operation == 'inventory' and self.runner is not None:
            result = request_inventory_cancel(
                self._inventory_workspace, self.runner)
        elif self._operation == 'inventory-load' and self._task is not None:
            self._task.cancel()
            result = None
        else:
            return
        self._inventory_status = 'cancelling'
        if result is not None and result.error:
            self._inventory_message = _(
                'Cancellation reported an error: {error}. Process signalling '
                'was attempted as a fallback.').format(error=result.error)
        else:
            self._inventory_message = _(
                'Cancelling analysis and waiting for privileged cleanup.')
        self._render_capture_controls()

    def _on_inventory_command_finished(self, generation, returncode,
                                       cancelled):
        self.runner = None
        workspace = self._inventory_workspace
        if (generation != self._inventory_generation or cancelled or
                self._closing or returncode != 0):
            cleanup = cleanup_inventory_workspace(workspace)
            self._inventory_workspace = None
            self._operation = None
            self._clear_runtime_inventory()
            if cancelled:
                self._inventory_status = 'cancelled'
                self._inventory_message = _(
                    'Session analysis was cancelled; no inventory was kept.')
            elif returncode != 0:
                self._inventory_status = 'error'
                self._inventory_message = _(
                    'Session analysis failed or authorization was declined. '
                    'No inventory content was logged or retained.')
            if not cleanup.cleaned:
                self._inventory_status = 'error'
                self._inventory_message = _(
                    'Analysis stopped, but the private workspace could not be '
                    'removed safely. It was left untouched.')
            self._render_capture_controls()
            self._update_chrome()
            if self._closing:
                self._finish_close_if_idle()
            return False

        self._operation = 'inventory-load'
        cleanup_results = []
        self._inventory_message = _(
            'Validating the private inventory and removing its temporary '
            'workspace…')
        self._render_capture_controls()

        def worker(token):
            try:
                token.checkpoint()
                inventory = backend.load_session_inventory(
                    workspace.output_path, cleanup=True)
                token.checkpoint()
                summary = session_inventory_summary(inventory)
                token.checkpoint()
                return inventory, summary
            finally:
                cleanup_results.append(
                    cleanup_inventory_workspace(workspace))

        def finished(inventory_result, error, was_cancelled):
            self._task = None
            self._operation = None
            self._inventory_workspace = None
            cleanup = (cleanup_results[0] if cleanup_results else None)
            if self._closing:
                self._finish_close_if_idle()
                return False
            if (generation != self._inventory_generation or was_cancelled):
                self._clear_runtime_inventory()
                self._inventory_status = 'cancelled'
                self._inventory_message = _(
                    'Session analysis was cancelled; no inventory was kept.')
            elif error is not None or cleanup is None or not cleanup.cleaned:
                self._clear_runtime_inventory()
                self._inventory_status = 'error'
                self._inventory_message = _(
                    'The inventory failed validation or its private workspace '
                    'could not be removed safely. No inventory was kept.')
            else:
                inventory, summary = inventory_result
                self.state.set_session_inventory(inventory)
                self.inventory_view.set_inventory(inventory, summary=summary)
                self._capture_store_signature = None
                self._invalidate_plan()
                self._inventory_status = 'success'
                self._inventory_message = _(
                    'Analysis complete. The validated metadata inventory is '
                    'held in memory only.')
            self._render_capture_controls()
            self._update_chrome()
            return False

        self._task = _run_background(worker, finished)
        self._update_chrome()
        return False

    def _on_exclusion_changed(self, entry):
        if not self._syncing:
            if self.state.set_exclusion_pattern(entry.get_text()):
                self._intent_changed()

    def _on_notes_changed(self, text_buffer):
        if self._syncing:
            return
        text = text_buffer.get_text(
            text_buffer.get_start_iter(), text_buffer.get_end_iter(), False)
        if self.state.set_notes(text):
            self._intent_changed()

    def _on_choose_output(self, _button):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose output image'), transient_for=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Choose'), Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(False)
        current = self.state.output_path
        if current:
            directory = os.path.dirname(current)
            if os.path.isdir(directory):
                dialog.set_current_folder(directory)
            dialog.set_current_name(os.path.basename(current))
        file_filter = Gtk.FileFilter()
        file_filter.set_name(_('ISO images (*.iso)'))
        file_filter.add_pattern('*.iso')
        dialog.add_filter(file_filter)
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            if not path.lower().endswith('.iso'):
                path += '.iso'
            self.output_entry.set_text(path)
        dialog.destroy()

    # Review page and planning --------------------------------------------
    def _build_review_page(self):
        page, body = self._page(
            _('STEP 4 OF 5'), _('Review the resolved build plan'),
            _('Review re-hashes inputs, checks provider capabilities and disk '
              'space, and allocates a private job only when the plan is '
              'buildable.'))
        self.review_spinner_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.review_spinner = Gtk.Spinner()
        self.review_spinner_box.pack_start(
            self.review_spinner, False, False, 0)
        self.review_spinner_label = Gtk.Label(xalign=0)
        self.review_spinner_box.pack_start(
            self.review_spinner_label, True, True, 0)
        self.review_spinner_box.get_style_context().add_class('planning-row')
        body.pack_start(self.review_spinner_box, False, False, 2)
        self.review_content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=9)
        body.pack_start(self.review_content, False, False, 0)
        return page

    def _start_planning(self):
        if self._operation is not None:
            return
        self._discard_plan()
        self._plan_generation += 1
        generation = self._plan_generation
        if (self.state.overwrite_output and
                (self._overwrite_approved_path is None or
                 self._overwrite_approved_identity is None)):
            self._clear_overwrite_approval()
        revision = self.state.revision
        try:
            project = self.state.to_image_project(
                overwrite_output=False)
        except Exception as error:
            self._render_planning_error(error)
            return
        approved_path = self._overwrite_approved_path
        approved_identity = self._overwrite_approved_identity
        using_overwrite_approval = bool(
            approved_path == project.output_path and approved_identity and
            self.state.overwrite_output)
        if using_overwrite_approval:
            project = self.state.to_image_project(overwrite_output=True)
        source_info = self.state.source_info
        session_inventory = self.state.session_inventory
        self._operation = 'planning'
        self.review_spinner_label.set_text(
            _('Resolving and validating the build plan…'))
        self.review_spinner.start()
        self.review_spinner_box.show_all()
        self.review_content.hide()
        self._update_chrome()

        def worker(token):
            token.checkpoint()
            runner = CancellableCommandRunner(
                token=token, cancel_grace=1.0)
            current_config_payload = read_current_live_config(runner)
            token.checkpoint()
            return create_project_plan(
                project, source_info, session_inventory,
                command_runner=runner,
                current_config_payload=current_config_payload)

        def finished(plan, error, cancelled):
            self._task = None
            self._operation = None
            self.review_spinner.stop()
            self.review_spinner_box.hide()
            stale = (generation != self._plan_generation or
                     revision != self.state.revision)
            if (using_overwrite_approval and plan is not None and
                    not overwrite_approval_matches(
                        plan, approved_path, approved_identity)):
                self._cleanup_plan(plan)
                self._clear_overwrite_approval()
                self._update_chrome()
                if self.state.current_step == STEP_REVIEW and not self._closing:
                    GLib.idle_add(self._start_planning)
                return False
            action = review_plan_completion_action(
                self.state.current_step, stale=stale,
                cancelled=cancelled, closing=self._closing)
            if action != 'accept' and plan is not None:
                self._cleanup_plan(plan)
            if cancelled and using_overwrite_approval:
                self._clear_overwrite_approval()
            if action == 'close':
                self._finish_close_if_idle()
                return False
            if action == 'restart':
                self._update_chrome()
                GLib.idle_add(self._start_planning)
                return False
            if action == 'discard':
                self._update_chrome()
                return False
            if error is not None:
                if using_overwrite_approval:
                    self._clear_overwrite_approval()
                self._render_planning_error(error)
                self._update_chrome()
                return False
            self.plan = plan
            self._plan_revision = revision
            self._render_review()
            self._update_chrome()
            return False

        self._task = _run_background(worker, finished)

    def _render_planning_error(self, error):
        self._clear_overwrite_approval()
        _clear(self.review_content)
        self.review_content.pack_start(self._diagnostic_row(
            'error', 'planning_failed',
            _('The build plan could not be created.'), str(error)),
            False, False, 0)
        self.review_content.show_all()
        self.review_spinner_box.hide()

    def _render_review(self):
        _clear(self.review_content)
        plan = self.plan
        if plan is None:
            return
        manifest = plan.manifest
        if plan.buildable:
            hero = self._review_hero(
                'success', _('Ready to build'),
                _('All mandatory checks passed. Build will revalidate every '
                  'effective input immediately before execution.'))
        else:
            hero = self._review_hero(
                'error', _('Build is blocked'),
                _('Resolve the {count} blocking issue(s) listed below. Each '
                  'issue has a button that opens the step where you can fix '
                  'it.').format(count=len(plan.errors)))
        self.review_content.pack_start(hero, False, False, 0)

        self._section(self.review_content, _('Source summary'))
        source = manifest.get('source', {})
        self.review_content.pack_start(self._key_value_grid((
            (_('Initramfs'), source.get('backend') or _('Unknown')),
            (_('Source'), source.get('tree_path') or _('Unavailable')),
            (_('Bootloader'), source.get('bootloader') or _('Unknown')),
            (_('Fingerprint'), source.get('fingerprint') or _('Unavailable')),
        )), False, False, 0)

        composition = manifest.get('composition', {})
        self._section(self.review_content, _('Composition'))
        self._review_list(
            self.review_content, _('Selected source modules'),
            [item.get('basename', '') for item in
             composition.get('selected_source_modules', [])])
        self._review_list(
            self.review_content, _('Deselected source modules'),
            [item.get('basename', '') for item in
             composition.get('deselected_source_modules', [])])
        self._review_list(
            self.review_content, _('Additional modules'),
            [item.get('path', '') for item in
             composition.get('additional_modules', [])])

        self._section(self.review_content, _('Output and defaults'))
        output = manifest.get('output', {})
        self.review_content.pack_start(self._key_value_grid((
            (_('Output'), output.get('final_path') or _('Unavailable')),
            (_('Boot menu'), manifest.get('menu_locale') or _('Unknown')),
            (_('Volume label'), manifest.get('volume_label') or _('MINIOS')),
        )), False, False, 0)

        customization = review_customization_summary(
            manifest, self.state.boot_background_path,
            self.state.overlay_directory)
        override_keys = customization.get('override_keys', ())
        kernel = customization.get('kernel_args')
        background = customization.get('background')
        overlay = customization.get('overlay')
        kernel_value = (
            _('{count} bytes · SHA-256 {digest}').format(
                count=kernel.get('bytes'), digest=kernel.get('sha256'))
            if kernel else _('Preserve source'))
        background_value = (
            _('{name} · {width}×{height} · SHA-256 {digest}').format(
                name=background.get('basename'),
                width=background.get('width'),
                height=background.get('height'),
                digest=background.get('sha256'))
            if background else _('Preserve source'))
        overlay_value = (
            _('{name} · {entries} entries · {size} · fingerprint '
              '{fingerprint}').format(
                name=overlay.get('basename'),
                entries=overlay.get('entry_count'),
                size=self._size_value(overlay.get('regular_bytes')),
                fingerprint=overlay.get('input_tree_fingerprint'))
            if overlay else _('None'))
        boot_menu_entries = customization.get('boot_menu_entries', ())
        if boot_menu_entries:
            enabled_entries = [
                item for item in boot_menu_entries if item.get('enabled')]
            default_entry = next(
                (item for item in enabled_entries if item.get('default')), None)
            visible_names = [
                item.get('title') or
                DEFAULT_BOOT_TITLES.get(item.get('base_mode'), item.get('id', ''))
                for item in enabled_entries]
            parameterized = sum(
                1 for item in enabled_entries if item.get('kernel_args'))
            custom_count = sum(
                1 for item in enabled_entries
                if item.get('id') not in backend.DEFAULT_BOOT_MODES)
            boot_menu_value = _('{count} entries · order: {order}').format(
                count=len(enabled_entries), order=' → '.join(visible_names))
            if custom_count:
                boot_menu_value += _(' · {count} custom').format(
                    count=custom_count)
            if parameterized:
                boot_menu_value += _(' · {count} with entry parameters').format(
                    count=parameterized)
            default_value = (
                default_entry.get('title') or
                DEFAULT_BOOT_TITLES.get(default_entry.get('base_mode'),
                                        default_entry.get('id', ''))
                if default_entry else _('Unknown'))
        else:
            boot_menu_value = _('Preserve source')
            default_value = DEFAULT_BOOT_TITLES.get(
                customization.get('default_boot'), _('Preserve source'))
        self._section(self.review_content, _('Image customization'))
        self.review_content.pack_start(self._key_value_grid((
            (_('Configuration override keys'),
             ', '.join(override_keys) if override_keys else _('None')),
            (_('Boot timeout'),
             _('{seconds} seconds').format(
                 seconds=customization.get('boot_timeout'))
             if customization.get('boot_timeout') is not None
             else _('Preserve source')),
            (_('Default session'), default_value),
            (_('Boot menu entries'), boot_menu_value),
            (_('Kernel arguments'), kernel_value),
            (_('Boot background'), background_value),
            (_('Project filesystem layer'), overlay_value),
            (_('Customization privilege'),
             _('Rootless; only optional session capture may request '
               'savechanges authorization')),
        )), False, False, 0)

        capture = manifest.get('capture', {})
        selection = capture.get('selection', {})
        inventory = capture.get('inventory', {})
        capture_mode = capture.get('mode') or backend.NO_SESSION_CAPTURE
        if capture.get('requested'):
            capture_privilege = manifest.get('tools', {}).get(
                'capture_privilege', {})
            if capture_privilege.get('euid') == 0:
                privilege = _(
                    'Direct capture as EUID 0; minios-image-compose remains '
                    'within the current root process')
            else:
                privilege = _(
                    'pkexec authorizes trusted savechanges only; '
                    'minios-image-compose remains unprivileged')
            compression = capture.get('compression') or _('Unknown')
            inventory_value = (
                _('{count} entries provided').format(
                    count=inventory.get('entry_count'))
                if inventory.get('provided') else
                _('Not provided; estimate is unknown'))
            capture_estimate = (
                self._size_value(capture.get('estimated_bytes'))
                if capture.get('estimated_bytes') is not None else
                _('Unknown'))
            selected_count = inventory.get('selected_entry_count')
            if capture_mode == 'selected':
                selection_count = _(
                    '{includes} include paths, {excludes} exclude paths').format(
                        includes=selection.get('include_count', 0),
                        excludes=selection.get('exclude_count', 0))
            else:
                selection_count = (
                    str(selected_count) if selected_count is not None
                    else _('Unknown'))
            selection_digest = (
                selection.get('sha256') or _('Not applicable'))
        else:
            privilege = _('No session-capture authorization')
            compression = _('Not applicable')
            inventory_value = _('Not requested')
            capture_estimate = _('Not applicable')
            selection_count = '0'
            selection_digest = _('Not applicable')
        self._section(self.review_content, _('Session capture'))
        self.review_content.pack_start(self._key_value_grid((
            (_('Mode'), CAPTURE_MODE_TITLES.get(
                capture_mode, capture_mode)),
            (_('Capture authorization'), privilege),
            (_('Compression'), compression),
            (_('Selection count'), selection_count),
            (_('Selection SHA-256'), selection_digest),
            (_('Session inventory'), inventory_value),
            (_('Union backend'), inventory.get('union_backend') or
             _('Unknown') if capture.get('requested') else _('Not applicable')),
            (_('Estimated capture size'), capture_estimate),
        )), False, False, 0)
        if capture_mode == 'exact':
            warning = Gtk.Label(
                label=_('Include all session changes is union-provider-specific and may '
                        'preserve sensitive writable state. The explicit '
                        'acknowledgement is stored in the project and remains '
                        'in effect until revoked.'), xalign=0)
            warning.set_line_wrap(True)
            warning.get_style_context().add_class('warning-panel')
            self.review_content.pack_start(warning, False, False, 0)
        elif capture_mode == 'clean':
            warning = Gtk.Label(
                label=_('Include reusable changes only uses a strict allowlist and '
                        'intentionally omits broad state. It is not a '
                        'guarantee that an image is shareable.'), xalign=0)
            warning.set_line_wrap(True)
            warning.get_style_context().add_class('warning-panel')
            self.review_content.pack_start(warning, False, False, 0)

        estimate = manifest.get('estimate', {})
        self._section(self.review_content, _('Size and free space'))
        storage_labels = {
            backend.FILESYSTEM_CLASS_PERSISTENT: _('Persistent disk'),
            backend.FILESYSTEM_CLASS_RAM_BACKED: _('RAM-backed (tmpfs)'),
            backend.FILESYSTEM_CLASS_LIVE_OVERLAY: _('Live overlay'),
            backend.FILESYSTEM_CLASS_REMOVABLE: _('Removable media'),
            backend.FILESYSTEM_CLASS_UNKNOWN: _('Unknown'),
        }
        rows = [
            (_('Estimated input size'), self._size_value(
                estimate.get('input_bytes'))),
            (_('Required destination space'), self._size_value(
                estimate.get('required_destination_bytes'))),
            (_('Available destination space'), self._size_value(
                estimate.get('destination_free_bytes'))),
            (_('Destination storage type'), storage_labels.get(
                estimate.get('destination_filesystem_class'), _('Unknown'))),
            (_('Required temporary space'), self._size_value(
                estimate.get('required_scratch_bytes'))),
            (_('Available temporary space'), self._size_value(
                estimate.get('scratch_free_bytes'))),
            (_('Temporary storage type'), storage_labels.get(
                estimate.get('scratch_filesystem_class'), _('Unknown'))),
        ]
        if estimate.get('peak_memory_bytes') is not None:
            rows.append((_('Peak RAM use'), self._size_value(
                estimate.get('peak_memory_bytes'))))
            rows.append((_('Available memory'), self._size_value(
                estimate.get('available_memory_bytes'))))
        self.review_content.pack_start(
            self._key_value_grid(tuple(rows)), False, False, 0)

        if plan.errors:
            self._section(self.review_content, _('Blocking issues'))
            for diagnostic in plan.errors:
                self.review_content.pack_start(
                    self._diagnostic_widget(diagnostic), False, False, 0)
        if plan.warnings:
            self._section(self.review_content, _('Warnings'))
            for diagnostic in plan.warnings:
                self.review_content.pack_start(
                    self._diagnostic_widget(diagnostic), False, False, 0)
        codes = set(item.code for item in plan.errors)
        if 'compose_backend_missing' in codes:
            guidance = Gtk.Label(
                label=_('The minios-image-compose backend is missing. '
                        'Reinstall the minios-image-compose package that '
                        'matches this application version.'), xalign=0)
            guidance.set_line_wrap(True)
            guidance.get_style_context().add_class('warning-panel')
            self.review_content.pack_start(guidance, False, False, 0)
        if not plan.errors and not plan.warnings:
            self._section(self.review_content, _('Diagnostics'))
            clean = Gtk.Label(
                label=_('No warnings or blocking issues.'), xalign=0)
            clean.get_style_context().add_class('success-inline')
            self.review_content.pack_start(clean, False, False, 0)

        if 'output_exists_overwrite_not_allowed' in codes:
            overwrite = Gtk.Button(label=_('Review existing output'))
            overwrite.set_image(Gtk.Image.new_from_icon_name(
                'document-save-symbolic', Gtk.IconSize.BUTTON))
            overwrite.get_style_context().add_class('minios-text-button')
            overwrite.connect('clicked', self._on_approve_overwrite)
            self.review_content.pack_start(overwrite, False, False, 5)
        self.review_content.show_all()

    def _review_hero(self, style, title, detail):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.get_style_context().add_class('review-hero')
        box.get_style_context().add_class('review-{}'.format(style))
        icon_name = ('emblem-ok-symbolic' if style == 'success'
                     else 'dialog-error-symbolic')
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.DIALOG)
        box.pack_start(icon, False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.get_style_context().add_class('hero-title')
        detail_label = Gtk.Label(label=detail, xalign=0)
        detail_label.set_line_wrap(True)
        detail_label.get_style_context().add_class('hero-detail')
        text.pack_start(title_label, False, False, 0)
        text.pack_start(detail_label, False, False, 0)
        box.pack_start(text, True, True, 0)
        return box

    def _key_value_grid(self, values):
        grid = Gtk.Grid(row_spacing=6, column_spacing=18)
        grid.get_style_context().add_class('review-grid')
        for row, (name, value) in enumerate(values):
            name_label = Gtk.Label(label=name, xalign=0)
            name_label.get_style_context().add_class('metadata-name')
            value_label = Gtk.Label(label=str(value), xalign=0)
            value_label.set_line_wrap(True)
            value_label.set_line_wrap_mode(Pango.WrapMode.CHAR)
            value_label.set_selectable(name in (_('Source'), _('Output'),
                                                _('Fingerprint')))
            value_label.get_style_context().add_class('metadata-value')
            grid.attach(name_label, 0, row, 1, 1)
            grid.attach(value_label, 1, row, 1, 1)
        return grid

    def _review_list(self, parent, title, values):
        label = Gtk.Label(label=title, xalign=0)
        label.get_style_context().add_class('review-list-title')
        parent.pack_start(label, False, False, 0)
        if values:
            text = '\n'.join('• {}'.format(value) for value in values)
        else:
            text = _('None')
        value_label = Gtk.Label(label=text, xalign=0)
        value_label.set_line_wrap(True)
        value_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        value_label.get_style_context().add_class('review-list')
        parent.pack_start(value_label, False, False, 0)

    def _size_value(self, value):
        if value is None:
            return _('Unavailable')
        return human_size(value) or _('Unavailable')

    def _on_approve_overwrite(self, _button):
        if self.plan is None:
            return
        observation = planned_output_observation(self.plan)
        if observation is None:
            self._clear_overwrite_approval()
            self._discard_plan()
            self._start_planning()
            return
        path, identity = observation
        if not ask_confirmation(
                self, _('Replace the existing output image?'),
                _('The backend will bind this exact existing file into a new '
                  'plan and will refuse publication if it changes.\n\n{path}').format(
                    path=path), confirm_label=_('Replace')):
            self._clear_overwrite_approval()
            return
        self.state.set_overwrite_output(True)
        self._overwrite_approved_path = path
        self._overwrite_approved_identity = identity
        self._discard_plan()
        self._start_planning()

    # Build page and lifecycle --------------------------------------------
    def _build_build_page(self):
        page, body = self._page(
            _('STEP 5 OF 5'), _('Build and verify the image'),
            _('The image is first created in a private job directory, then '
              'structurally verified, and only then published atomically to '
              'the final path.'))

        self.build_phase_label = Gtk.Label(xalign=0)
        self.build_phase_label.get_style_context().add_class('build-phase')
        body.pack_start(self.build_phase_label, False, False, 0)
        self.build_detail_label = Gtk.Label(xalign=0)
        self.build_detail_label.set_line_wrap(True)
        self.build_detail_label.get_style_context().add_class('build-detail')
        body.pack_start(self.build_detail_label, False, False, 0)
        self.build_progress = Gtk.ProgressBar()
        self.build_progress.set_show_text(False)
        body.pack_start(self.build_progress, False, False, 3)

        timeline = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        timeline.get_style_context().add_class('build-timeline')
        self.build_milestones = {}
        for key, title in (
                ('created', _('Created')),
                ('verified', _('Structurally verified')),
                ('published', _('Published safely'))):
            row = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            icon = Gtk.Image.new_from_icon_name(
                'radio-symbolic', Gtk.IconSize.MENU)
            label = Gtk.Label(label=title, xalign=0)
            label.get_style_context().add_class('milestone-pending')
            row.pack_start(icon, False, False, 0)
            row.pack_start(label, True, True, 0)
            timeline.pack_start(row, False, False, 0)
            self.build_milestones[key] = (icon, label)
        body.pack_start(timeline, False, False, 2)

        self.build_result = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.build_result.get_style_context().add_class('result-panel')
        self.build_result_title = Gtk.Label(xalign=0)
        self.build_result_title.get_style_context().add_class('result-title')
        self.build_result.pack_start(
            self.build_result_title, False, False, 0)
        self.build_result_detail = Gtk.Label(xalign=0)
        self.build_result_detail.set_line_wrap(True)
        self.build_result.pack_start(
            self.build_result_detail, False, False, 0)
        self.result_grid = Gtk.Grid(row_spacing=6, column_spacing=16)
        self.result_values = {}
        for row, (key, title) in enumerate((
                ('path', _('Final path')),
                ('size', _('Size')),
                ('sha256', _('SHA-256')))):
            name = Gtk.Label(label=title, xalign=0)
            name.get_style_context().add_class('metadata-name')
            value = Gtk.Label(xalign=0)
            value.set_line_wrap(True)
            value.set_line_wrap_mode(Pango.WrapMode.CHAR)
            value.set_selectable(True)
            value.get_style_context().add_class('metadata-value')
            self.result_grid.attach(name, 0, row, 1, 1)
            self.result_grid.attach(value, 1, row, 1, 1)
            self.result_values[key] = value
        self.build_result.pack_start(self.result_grid, False, False, 0)
        self.capture_result_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=5)
        capture_result_title = Gtk.Label(
            label=_('Verified session capture'), xalign=0)
        capture_result_title.get_style_context().add_class(
            'review-list-title')
        self.capture_result_box.pack_start(
            capture_result_title, False, False, 0)
        self.capture_result_grid = Gtk.Grid(
            row_spacing=6, column_spacing=16)
        self.capture_result_values = {}
        for row, (key, title) in enumerate((
                ('profile', _('Profile')),
                ('union', _('Union backend')),
                ('selection-count', _('Selection count')),
                ('selection-sha256', _('Selection digest')),
                ('layer', _('Capture layer')),
                ('layer-size', _('Capture layer size')),
                ('layer-sha256', _('Capture layer SHA-256')))):
            name = Gtk.Label(label=title, xalign=0)
            name.get_style_context().add_class('metadata-name')
            value = Gtk.Label(xalign=0)
            value.set_line_wrap(True)
            value.set_line_wrap_mode(Pango.WrapMode.CHAR)
            value.set_selectable(
                key in ('selection-sha256', 'layer-sha256'))
            value.get_style_context().add_class('metadata-value')
            self.capture_result_grid.attach(name, 0, row, 1, 1)
            self.capture_result_grid.attach(value, 1, row, 1, 1)
            self.capture_result_values[key] = value
        self.capture_result_box.pack_start(
            self.capture_result_grid, False, False, 0)
        self.build_result.pack_start(
            self.capture_result_box, False, False, 0)
        self.customization_result_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=5)
        customization_result_title = Gtk.Label(
            label=_('Verified image customization'), xalign=0)
        customization_result_title.get_style_context().add_class(
            'review-list-title')
        self.customization_result_box.pack_start(
            customization_result_title, False, False, 0)
        self.customization_result_grid = Gtk.Grid(
            row_spacing=6, column_spacing=16)
        self.customization_result_values = {}
        for row, (key, title) in enumerate((
                ('config-keys', _('Configuration override keys')),
                ('boot-timeout', _('Boot timeout')),
                ('default-boot', _('Default session')),
                ('boot-menu', _('Boot menu entries')),
                ('kernel', _('Kernel arguments')),
                ('background', _('Boot background')),
                ('overlay', _('Project filesystem layer')),
                ('overlay-size', _('Overlay layer size')),
                ('overlay-sha256', _('Overlay layer SHA-256')),
                ('overlay-fingerprint', _('Overlay input fingerprint')))):
            name = Gtk.Label(label=title, xalign=0)
            name.get_style_context().add_class('metadata-name')
            value = Gtk.Label(xalign=0)
            value.set_line_wrap(True)
            value.set_line_wrap_mode(Pango.WrapMode.CHAR)
            value.set_selectable(key in (
                'kernel', 'background', 'overlay-sha256',
                'overlay-fingerprint'))
            value.get_style_context().add_class('metadata-value')
            self.customization_result_grid.attach(name, 0, row, 1, 1)
            self.customization_result_grid.attach(value, 1, row, 1, 1)
            self.customization_result_values[key] = value
        self.customization_result_box.pack_start(
            self.customization_result_grid, False, False, 0)
        self.build_result.pack_start(
            self.customization_result_box, False, False, 0)
        self.build_diagnostics = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.build_result.pack_start(
            self.build_diagnostics, False, False, 0)
        result_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.result_open_button = Gtk.Button(
            label=_('Open containing folder'))
        self.result_open_button.set_image(Gtk.Image.new_from_icon_name(
            'folder-open-symbolic', Gtk.IconSize.BUTTON))
        self.result_open_button.get_style_context().add_class(
            'minios-text-button')
        self.result_open_button.connect('clicked', self._on_open_folder)
        result_actions.pack_start(
            self.result_open_button, False, False, 0)
        self.result_new_button = Gtk.Button(label=_('New image project'))
        self.result_new_button.set_image(Gtk.Image.new_from_icon_name(
            'document-new-symbolic', Gtk.IconSize.BUTTON))
        self.result_new_button.get_style_context().add_class(
            'minios-text-button')
        self.result_new_button.connect('clicked', self._on_result_new)
        result_actions.pack_start(self.result_new_button, False, False, 0)
        self.build_result.pack_start(result_actions, False, False, 0)
        self.vm_note = Gtk.Label(xalign=0)
        self.vm_note.set_line_wrap(True)
        self.vm_note.get_style_context().add_class('vm-note')
        self.build_result.pack_start(self.vm_note, False, False, 0)
        body.pack_start(self.build_result, False, False, 2)

        self.build_log = LogView(maximum_characters=2 * 1024 * 1024)
        self.build_log.set_min_content_height(180)
        details = Gtk.Expander(label=_('Technical details'))
        details.add(self.build_log)
        body.pack_start(details, True, True, 2)
        self._reset_build_page()
        return page

    def _reset_build_page(self):
        self.build_phase_label.set_text(_('Waiting to build'))
        self.build_detail_label.set_text('')
        self.build_progress.set_fraction(0.0)
        if hasattr(self, 'build_log'):
            self.build_log.clear()
        self._last_command_error = None
        for icon, label in self.build_milestones.values():
            icon.set_from_icon_name('radio-symbolic', Gtk.IconSize.MENU)
            label.get_style_context().remove_class('milestone-complete')
            label.get_style_context().add_class('milestone-pending')
        self.build_result.hide()
        self.capture_result_box.hide()
        self.customization_result_box.hide()
        for value in self.capture_result_values.values():
            value.set_text('')
        for value in self.customization_result_values.values():
            value.set_text('')
        self.vm_note.hide()

    def _set_milestone(self, key):
        icon, label = self.build_milestones[key]
        icon.set_from_icon_name('emblem-ok-symbolic', Gtk.IconSize.MENU)
        label.get_style_context().remove_class('milestone-pending')
        label.get_style_context().add_class('milestone-complete')

    def _start_build(self):
        if self.plan is None or not self.plan.buildable:
            return
        if not plan_revision_matches(
                self._plan_revision, self.state.revision):
            self._discard_plan()
            self._start_planning()
            return
        self.active_plan = self.plan
        self.plan = None
        self.build_started = True
        self.build_status = 'preparing'
        self._cancel_requested = False
        self._build_generation += 1
        generation = self._build_generation
        self.state.visit_step(STEP_BUILD, build_started=True)
        self.page_stack.set_visible_child_name('build')
        self._reset_build_page()
        self.build_phase_label.set_text(_('Revalidating inputs'))
        self.build_detail_label.set_text(
            _('Every source, module, and configuration digest is being '
              'checked again before execution.'))
        self.build_progress.set_fraction(0.02)
        self._operation = 'prepare'
        self._update_chrome()
        plan = self.active_plan

        def worker(token):
            token.checkpoint()
            argv = prepare_plan_execution(plan)
            token.checkpoint()
            return argv

        def finished(argv, error, cancelled):
            self._task = None
            self._operation = None
            if generation != self._build_generation:
                return False
            if cancelled or self._closing or self._cancel_requested:
                self._finish_cancelled()
                if self._closing:
                    self._finish_close_if_idle()
                return False
            if error is not None:
                self._finish_build_failure(
                    'build-failed', _('Build failed'),
                    _('Input revalidation failed: {error}').format(error=error))
                return False
            self._start_command(argv)
            return False

        self._task = _run_background(worker, finished)

    def _start_command(self, argv):
        plan = self.active_plan
        if (plan is None or not argv or
                os.path.basename(str(argv[0])) !=
                backend.COMPOSE_BACKEND_NAME):
            self._finish_build_failure(
                'build-failed', _('Build failed'),
                _('The backend did not return a plain minios-image-compose '
                  'command. Privilege escalation is permitted only inside the '
                  'trusted session-capture boundary.'))
            return
        self.build_status = 'building'
        self.build_phase_label.set_text(_('Starting image build'))
        self.build_detail_label.set_text('')
        redactions = []
        private_values = [
            (self.state.kernel_args, '<redacted-kernel-arguments>'),
            (self.state.boot_background_path, '<boot-background-input>'),
            (self.state.overlay_directory, '<project-overlay-input>'),
            (getattr(plan, 'job_directory', None), '<private-job-directory>'),
            (getattr(plan, 'adapter_manifest_path', None),
             '<private-build-manifest>'),
            (getattr(plan, 'partial_output_path', None),
             '<private-partial-output>'),
        ]
        private_values.extend(
            (value, '<live-config-value>')
            for value in self.state.live_config_overrides.values())
        private_values.extend(
            (path, '<additional-module-input>')
            for path in self.state.additional_module_paths)
        private_values.extend((
            (self.state.source_path, '<running-minios-source>'),
            (self.state.source_root_path, '<running-minios-root>'),
        ))
        for value, replacement in private_values:
            if value and isinstance(value, str):
                redactions.append((value, replacement))
        self._build_output_redactions = tuple(sorted(
            redactions, key=lambda item: len(item[0]), reverse=True))
        self.runner = CommandRunner(
            list(argv), line_cb=self._on_command_line,
            on_finished=self._on_command_finished,
            cwd=plan.execution_cwd, display_argv=plan.display_argv)
        self.build_log.feed('$ {}\n\n'.format(
            self.runner.formatted_command))
        self.runner.start()
        self._update_chrome()

    def _on_command_line(self, line):
        for private_value, replacement in self._build_output_redactions:
            line = line.replace(private_value, replacement)
        for path in sorted(
                self.state.capture_include_paths +
                self.state.capture_exclude_paths,
                key=len, reverse=True):
            line = line.replace(path, '<selected-path>')
        event = parse_build_output_line(line)
        GLib.idle_add(self._handle_command_event, event)

    def _handle_command_event(self, event):
        if self._closing and self.runner is None:
            return False
        timestamp = datetime.datetime.now().strftime('%H:%M:%S')
        raw = event.get('raw', '')
        if raw:
            self.build_log.append_line(raw, timestamp=timestamp)
        if event['kind'] == 'phase':
            phase_id = event['phase_id']
            self.build_phase_label.set_text(
                PHASE_LABELS.get(phase_id, _('Building image')))
            if event['fraction'] is not None:
                self.build_progress.set_fraction(event['fraction'])
        elif event['kind'] == 'text':
            if event['level'] in ('I', 'E') and event['text']:
                self.build_detail_label.set_text(event['text'])
            if event['level'] == 'E' and not self._last_command_error:
                self._last_command_error = event['text']
        return False

    def _on_command_finished(self, returncode, cancelled):
        self.runner = None
        if self._closing or cancelled or self._cancel_requested:
            self._finish_cancelled()
            if self._closing:
                self._finish_close_if_idle()
            return False
        if returncode != 0:
            self._finish_build_failure(
                'build-failed', _('Build failed'),
                build_command_failure_detail(
                    returncode, self._last_command_error))
            return False

        self._set_milestone('created')
        self.build_phase_label.set_text(_('Created'))
        self.build_detail_label.set_text(
            _('The private image was created. Structural verification is '
              'running before publication.'))
        self.build_progress.set_fraction(0.86)
        self.build_status = 'verifying'
        self._operation = 'verify'
        self._update_chrome()
        generation = self._build_generation
        plan = self.active_plan

        def worker(token):
            runner = CancellableCommandRunner(
                token=token, cancel_grace=1.0)
            return backend.verify_iso(plan, runner=runner)

        def finished(result, error, cancelled):
            self._task = None
            self._operation = None
            if generation != self._build_generation:
                return False
            if cancelled or self._closing or self._cancel_requested:
                self.verification_result = result
                self._finish_cancelled()
                if self._closing:
                    self._finish_close_if_idle()
                return False
            if error is not None:
                self._finish_build_failure(
                    'verification-failed', _('Verification failed'),
                    _('Structural verification could not run: {error}').format(
                        error=error))
                return False
            self.verification_result = result
            self._render_build_diagnostics(result.diagnostics)
            if not result.structurally_verified:
                self._finish_build_failure(
                    'verification-failed', _('Verification failed'),
                    _('An image was created, but it did not pass structural '
                      'verification.'))
                return False
            self._set_milestone('verified')
            self.build_phase_label.set_text(_('Structurally verified'))
            self.build_detail_label.set_text(
                _('All required image paths and boot records were verified. '
                  'Safe publication is next.'))
            self.build_progress.set_fraction(0.94)
            self.build_status = 'publish-wait'
            self._update_chrome()
            GLib.timeout_add(60, self._begin_publication, generation)
            return False

        self._task = _run_background(worker, finished)
        return False

    def _begin_publication(self, generation):
        if generation != self._build_generation:
            return False
        if self._closing or self._cancel_requested:
            self._finish_cancelled()
            if self._closing:
                self._finish_close_if_idle()
            return False
        self.build_status = 'publishing'
        self._operation = 'publish'
        self.build_detail_label.set_text(
            _('Repeating structural verification and publishing atomically.'))
        self._update_chrome()
        plan = self.active_plan
        verification = self.verification_result

        def worker(_token):
            path = backend.publish_verified_output(plan, verification)
            capabilities = backend.detect_vm_capabilities()
            return path, capabilities

        def finished(result, error, _cancelled):
            self._task = None
            self._operation = None
            if generation != self._build_generation:
                return False
            if error is not None:
                self._finish_build_failure(
                    'publication-failed', _('Publication failed'),
                    _('The verified image could not be published safely: '
                      '{error}').format(error=error))
                if self._closing:
                    self._finish_close_if_idle()
                return False
            path, capabilities = result
            self.final_output_path = path
            self.vm_capabilities = capabilities
            self._set_milestone('published')
            self.build_phase_label.set_text(_('Image ready'))
            self.build_detail_label.set_text(
                _('The structurally verified image was published safely.'))
            self.build_progress.set_fraction(1.0)
            self.build_status = 'success'
            self._show_success_result()
            self._clear_overwrite_approval()
            self._cleanup_active_plan()
            self._update_chrome()
            if self._closing:
                self._finish_close_if_idle()
            return False

        self._task = _run_background(worker, finished)
        return False

    def _finish_cancelled(self):
        self._clear_overwrite_approval()
        self.build_status = 'cancelled'
        self.build_phase_label.set_text(_('Cancelled'))
        cleaned = self._cleanup_active_plan()
        if cleaned:
            detail = _(
                'The build was cancelled and private partial output was '
                'cleaned up.')
        else:
            detail = _(
                'The build was cancelled, but private files could not be '
                'removed safely. Review the cleanup warning.')
        self.build_detail_label.set_text(detail)
        self.build_result_title.set_text(_('Build cancelled'))
        self._set_result_intent('cancelled')
        self.build_result_detail.set_text(
            _('No image was published. You can return to Review and try '
              'again.'))
        self.result_grid.hide()
        self.capture_result_box.hide()
        self.customization_result_box.hide()
        for value in self.capture_result_values.values():
            value.set_text('')
        for value in self.customization_result_values.values():
            value.set_text('')
        self.result_open_button.hide()
        self.result_new_button.hide()
        self.vm_note.hide()
        self.build_result.show()
        self._update_chrome()

    def _finish_build_failure(self, status, title, detail):
        self._clear_overwrite_approval()
        self.build_status = status
        self.build_phase_label.set_text(title)
        self.build_detail_label.set_text(detail)
        self.build_result_title.set_text(title)
        self._set_result_intent('error')
        self.build_result_detail.set_text(detail)
        self.result_grid.hide()
        for value in self.result_values.values():
            value.set_text('')
        self.capture_result_box.hide()
        self.customization_result_box.hide()
        for value in self.capture_result_values.values():
            value.set_text('')
        for value in self.customization_result_values.values():
            value.set_text('')
        self.result_open_button.hide()
        self.result_new_button.hide()
        self.vm_note.hide()
        self.build_result.show()
        self._cleanup_active_plan()
        self._update_chrome()

    def _show_success_result(self):
        result = self.verification_result
        self.build_result_title.set_text(_('Verified image published'))
        self._set_result_intent('success')
        self.build_result_detail.set_text(
            _('The final image passed structural verification and is ready '
              'at the selected path.'))
        self._set_result_values(
            self.final_output_path, result.size, result.sha256)
        self.result_grid.show_all()
        capture = verification_capture_summary(result.capture_summary)
        if capture['requested']:
            self.capture_result_values['profile'].set_text(
                CAPTURE_MODE_TITLES.get(
                    capture.get('profile'), capture.get('profile') or
                    _('Unknown')))
            self.capture_result_values['union'].set_text(
                capture.get('union_backend') or _('Unknown'))
            plan_manifest = (
                self.active_plan.manifest
                if self.active_plan is not None else {})
            selection = verification_selection_summary(
                result.capture_summary, plan_manifest)
            if selection.get('valid'):
                selection_count = _(
                    '{includes} include paths, {excludes} exclude paths').format(
                        includes=selection['include_count'],
                        excludes=selection['exclude_count'])
                selection_digest = (
                    selection['selection_sha256'])
            elif selection.get('applicable'):
                selection_count = _('Unavailable')
                selection_digest = _('Unavailable')
            else:
                selection_count = _('Not applicable')
                selection_digest = _('Not applicable')
            self.capture_result_values['selection-count'].set_text(
                selection_count)
            self.capture_result_values['selection-sha256'].set_text(
                selection_digest)
            self.capture_result_values['layer'].set_text(
                capture.get('layer_basename') or _('Unavailable'))
            self.capture_result_values['layer-size'].set_text(
                self._size_value(capture.get('layer_size')))
            self.capture_result_values['layer-sha256'].set_text(
                capture.get('layer_sha256') or _('Unavailable'))
            self.capture_result_box.show_all()
        else:
            self.capture_result_box.hide()
        plan_manifest = (
            self.active_plan.manifest
            if self.active_plan is not None else {})
        customization = verification_customization_summary(
            result.customization_summary, plan_manifest,
            self.state.boot_background_path)
        if customization.get('requested'):
            keys = customization.get('override_keys', ())
            kernel = customization.get('kernel_args')
            background = customization.get('background')
            overlay = customization.get('overlay')
            self.customization_result_values['config-keys'].set_text(
                ', '.join(keys) if keys else _('None'))
            self.customization_result_values['boot-timeout'].set_text(
                _('{seconds} seconds').format(
                    seconds=customization.get('boot_timeout'))
                if customization.get('boot_timeout') is not None
                else _('Preserve source'))
            verified_menu = customization.get('boot_menu_entries', ())
            if verified_menu:
                enabled_menu = [item for item in verified_menu
                                if item.get('enabled')]
                default_entry = next(
                    (item for item in enabled_menu if item.get('default')), None)
                default_text = (
                    default_entry.get('title') or
                    DEFAULT_BOOT_TITLES.get(
                        default_entry.get('base_mode'), default_entry.get('id', ''))
                    if default_entry else _('Unknown'))
                menu_text = _('{count} enabled: {entries}').format(
                    count=len(enabled_menu),
                    entries=' → '.join(
                        item.get('title') or DEFAULT_BOOT_TITLES.get(
                            item.get('base_mode'), item.get('id', ''))
                        for item in enabled_menu))
            else:
                default_text = DEFAULT_BOOT_TITLES.get(
                    customization.get('default_boot'), _('Preserve source'))
                menu_text = _('Preserve source')
            self.customization_result_values['default-boot'].set_text(
                default_text)
            self.customization_result_values['boot-menu'].set_text(menu_text)
            self.customization_result_values['kernel'].set_text(
                _('{count} bytes · SHA-256 {digest}').format(
                    count=kernel.get('bytes'), digest=kernel.get('sha256'))
                if kernel else _('Preserve source'))
            self.customization_result_values['background'].set_text(
                _('{name} · SHA-256 {digest}').format(
                    name=background.get('basename'),
                    digest=background.get('sha256'))
                if background else _('Preserve source'))
            self.customization_result_values['overlay'].set_text(
                overlay.get('layer_basename') if overlay else _('None'))
            self.customization_result_values['overlay-size'].set_text(
                self._size_value(overlay.get('layer_size'))
                if overlay else _('Not applicable'))
            self.customization_result_values['overlay-sha256'].set_text(
                overlay.get('layer_sha256') if overlay else _('Not applicable'))
            self.customization_result_values['overlay-fingerprint'].set_text(
                overlay.get('input_tree_fingerprint')
                if overlay else _('Not applicable'))
            self.customization_result_box.show_all()
        else:
            self.customization_result_box.hide()
        self.result_open_button.show_all()
        self.result_new_button.show_all()
        if (self.vm_capabilities is not None and
                self.vm_capabilities.get('boot_test_available')):
            self.vm_note.set_text(
                _('Boot test available externally. A compatible VM provider '
                  'was detected; Image Builder does not invoke it.'))
            self.vm_note.show()
        else:
            self.vm_note.hide()
        self.build_result.show_all()
        if not capture['requested']:
            self.capture_result_box.hide()
        if not customization.get('requested'):
            self.customization_result_box.hide()
        if not (self.vm_capabilities and
                self.vm_capabilities.get('boot_test_available')):
            self.vm_note.hide()

    def _set_result_values(self, path, size, digest):
        self.result_values['path'].set_text(path or _('Unavailable'))
        self.result_values['size'].set_text(
            human_size(size) if size else _('Unavailable'))
        self.result_values['sha256'].set_text(digest or _('Unavailable'))

    def _set_result_intent(self, intent):
        context = self.build_result.get_style_context()
        for class_name in RESULT_INTENT_CLASSES:
            context.remove_class(class_name)
        context.add_class('result-{}'.format(intent))

    def _render_build_diagnostics(self, diagnostics):
        _clear(self.build_diagnostics)
        for diagnostic in diagnostics:
            self.build_diagnostics.pack_start(
                self._diagnostic_widget(diagnostic), False, False, 0)
        self.build_diagnostics.show_all()

    def _request_cancel(self):
        if self.build_status == 'publishing':
            return
        self._clear_overwrite_approval()
        self._cancel_requested = True
        self.build_phase_label.set_text(_('Cancelling'))
        self.build_detail_label.set_text(
            _('Waiting for the current safe checkpoint. Hashing may finish '
              'its current pass before cancellation completes.'))
        if self.runner is not None:
            self.runner.cancel()
        elif (self._task is not None and
              self._operation in ('prepare', 'verify')):
            self._task.cancel()
        self._update_chrome()

    # Shared diagnostics and cleanup --------------------------------------
    def _diagnostic_widget(self, diagnostic):
        return self._diagnostic_row(
            diagnostic.severity, diagnostic.code, diagnostic.message,
            diagnostic.path)

    def _diagnostic_fix_step(self, code):
        """Map a blocker code to the step where the user can resolve it."""
        if not code:
            return None
        if 'collision' in code or 'module' in code:
            return STEP_CONTENT
        if (code.startswith('source') or 'fingerprint' in code or
                'minios_source' in code or code in (
                    'not_running_minios', 'live_root_unreadable',
                    'multiple_minios_sources')):
            return STEP_SOURCE
        return STEP_DEFAULTS

    def _diagnostic_row(self, severity, code, message, path):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        row.get_style_context().add_class('diagnostic-row')
        row.get_style_context().add_class('diagnostic-{}'.format(severity))
        icon_name = {
            'error': 'dialog-error-symbolic',
            'warning': 'dialog-warning-symbolic',
            'info': 'dialog-information-symbolic',
        }.get(severity, 'dialog-information-symbolic')
        icon = Gtk.Image.new_from_icon_name(
            resolve_icon((icon_name,)), Gtk.IconSize.MENU)
        icon.set_valign(Gtk.Align.START)
        row.pack_start(icon, False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title, display_message = diagnostic_display_text(code, message)
        code_label = Gtk.Label(label=title, xalign=0)
        code_label.get_style_context().add_class('diagnostic-code')
        message_label = Gtk.Label(label=display_message, xalign=0)
        message_label.set_line_wrap(True)
        text.pack_start(code_label, False, False, 0)
        text.pack_start(message_label, False, False, 0)
        if path:
            path_label = Gtk.Label(label=path, xalign=0)
            path_label.set_line_wrap(True)
            path_label.set_line_wrap_mode(Pango.WrapMode.CHAR)
            path_label.set_selectable(True)
            path_label.get_style_context().add_class('diagnostic-path')
            text.pack_start(path_label, False, False, 0)
        row.pack_start(text, True, True, 0)
        if severity == 'error':
            step = self._diagnostic_fix_step(code)
            if step is not None and step != self.state.current_step:
                fix = Gtk.Button(
                    label=_('Fix in {step}').format(step=STEP_TITLES[step]))
                fix.set_valign(Gtk.Align.CENTER)
                fix.set_tooltip_text(
                    _('Go to the {step} step to resolve this issue.').format(
                        step=STEP_TITLES[step]))
                fix.connect(
                    'clicked', lambda _button, target=step:
                    self._go_to_step(target))
                row.pack_end(fix, False, False, 0)
        return row

    def _cleanup_plan(self, plan):
        result = cleanup_plan_job(plan)
        if not result.cleaned:
            path = getattr(plan, 'job_directory', _('Unknown path'))
            warning = '{}: {}'.format(path, result.warning)
            self.cleanup_warnings.append(warning)
            if hasattr(self, 'build_log'):
                self.build_log.feed(
                    '\n{} {}\n'.format(_('Cleanup warning:'), warning))
            if self._closing:
                sys.stderr.write('{} {}\n'.format(
                    _('Cleanup warning:'), warning))
            else:
                show_error_dialog(
                    self, _('Private build files were left in place.'),
                    _('The job directory failed identity-safe cleanup. '
                      'Inspect it manually; Image Builder did not remove an '
                      'untrusted path.\n\n{detail}').format(detail=warning))
        return result.cleaned

    def _discard_plan(self):
        if self.plan is not None:
            plan = self.plan
            self.plan = None
            self._cleanup_plan(plan)
        self._plan_revision = None

    def _cleanup_active_plan(self):
        cleaned = True
        if self.active_plan is not None:
            plan = self.active_plan
            self.active_plan = None
            cleaned = self._cleanup_plan(plan)
        return cleaned

    def _clear_overwrite_approval(self):
        self._overwrite_approved_path = None
        self._overwrite_approved_identity = None
        reset_overwrite_intent_for_retry(self.state)

    def _invalidate_plan(self):
        self._plan_generation += 1
        if self._operation == 'planning' and self._task is not None:
            self._task.cancel()
        self._discard_plan()
        self._clear_overwrite_approval()

    def _intent_changed(self, render_content=False):
        self._invalidate_plan()
        if render_content:
            self._render_content()
        self._update_chrome()

    # Navigation and chrome ------------------------------------------------
    def _on_step_clicked(self, _button, step):
        self._go_to_step(step)

    def _go_to_step(self, step):
        if (self._build_active() or
                self._operation in ('inventory', 'inventory-load')):
            return
        current = self.state.current_step
        if not planning_navigation_allowed(
                current, step, self._operation == 'planning'):
            return
        if step == STEP_BUILD and not self.build_started:
            return
        if step > current and not self.state.can_enter_step(
                step, plan=self.plan, build_started=self.build_started):
            return
        if current == STEP_REVIEW and step != STEP_REVIEW:
            self._plan_generation += 1
            self._discard_plan()
        self.state.visit_step(
            step, plan=self.plan, build_started=self.build_started)
        self.page_stack.set_visible_child_name(STEP_IDS[step])
        if step == STEP_CONTENT:
            self._render_content()
        elif step == STEP_DEFAULTS:
            self._sync_defaults_widgets()
        elif step == STEP_REVIEW:
            self._start_planning()
        self._update_chrome()

    def _on_back_clicked(self, _button):
        if self.state.current_step > STEP_SOURCE and not self._build_active():
            self._go_to_step(self.state.current_step - 1)

    def _on_primary_clicked(self, _button):
        step = self.state.current_step
        if step < STEP_REVIEW:
            self._go_to_step(step + 1)
        elif step == STEP_REVIEW:
            self._start_build()
        elif step == STEP_BUILD:
            if self.build_status == 'success':
                self._new_project_flow()
            elif self.build_status in (
                    'cancelled', 'build-failed', 'verification-failed',
                    'publication-failed'):
                self.build_started = False
                self.state.furthest_step = STEP_REVIEW
                self._go_to_step(STEP_REVIEW)

    def _on_secondary_clicked(self, _button):
        if self.state.current_step != STEP_BUILD:
            return
        if self._build_active():
            self._request_cancel()
        elif self.build_status == 'success':
            self._open_output_folder()

    def _build_active(self):
        return self.build_status in (
            'preparing', 'building', 'verifying', 'publish-wait', 'publishing')

    def _update_chrome(self):
        self._update_header()
        self._update_steps()
        self._update_footer()

    def _update_header(self):
        if self.state.project_path:
            subtitle = os.path.basename(self.state.project_path)
            if self.state.dirty:
                subtitle = '{} - {}'.format(
                    subtitle, _('Unsaved changes'))
        else:
            subtitle = _('New project - Unsaved')
        self.header.props.subtitle = subtitle
        busy = bool(self._operation or self.runner or self._build_active())
        self.lookup_action('new-project').set_enabled(not busy)
        self.lookup_action('open-project').set_enabled(not busy)
        can_save = self.state.has_source_reference and not busy
        self.lookup_action('save-project').set_enabled(can_save)
        self.lookup_action('save-project-as').set_enabled(can_save)

    def _update_steps(self):
        current = self.state.current_step
        statuses = (
            self.state.source_supported and not self.source_loading,
            self.state.content_ready(),
            self.state.defaults_ready(),
            self.plan is not None and self.plan.buildable,
            self.build_status == 'success',
        )
        for index, button in enumerate(self.step_buttons):
            context = button.get_style_context()
            context.remove_class('sidebar-step-active')
            context.remove_class('sidebar-step-done')
            context.remove_class('sidebar-step-todo')
            context.remove_class('sidebar-step-error')
            marker = self.step_markers[index]
            error = False
            if index == STEP_SOURCE and not self.source_loading and not (
                    self.state.source_supported):
                error = True
            elif index == STEP_CONTENT and self.state.collisions():
                error = True
            elif (index == STEP_REVIEW and self.plan is not None and
                    self.plan.errors):
                error = True
            if index == current:
                context.add_class('sidebar-step-active')
                marker.set_text('●')
            elif error:
                context.add_class('sidebar-step-error')
                marker.set_text('!')
            elif statuses[index]:
                context.add_class('sidebar-step-done')
                marker.set_text('✓')
            else:
                context.add_class('sidebar-step-todo')
                marker.set_text(str(index + 1))

            if self._operation in ('planning', 'inventory', 'inventory-load'):
                sensitive = index == current
            elif self._build_active():
                sensitive = index == STEP_BUILD
            elif index <= current:
                sensitive = True
            elif index == STEP_BUILD:
                sensitive = self.build_started
            else:
                sensitive = self.state.can_enter_step(
                    index, plan=self.plan,
                    build_started=self.build_started)
            button.set_sensitive(sensitive)

    def _update_footer(self):
        step = self.state.current_step
        self.back_button.set_visible(True)
        self.back_button.set_sensitive(
            step > STEP_SOURCE and not self._build_active() and
            self._operation is None)
        self.secondary_button.hide()
        self.secondary_button.get_style_context().remove_class(
            'destructive-action')
        self.primary_button.show()
        self.primary_button.get_style_context().remove_class(
            'destructive-action')

        if step == STEP_SOURCE:
            self.primary_button.set_label(_('Continue'))
            self.primary_button.set_sensitive(
                not self.source_loading and self.state.source_supported)
            if self.source_loading:
                self.footer_status.set_text(_('Inspecting source…'))
            elif not self.state.source_supported:
                self.footer_status.set_text(
                    _('Select a supported MiniOS source to continue.'))
            else:
                self.footer_status.set_text('')
        elif step == STEP_CONTENT:
            self.primary_button.set_label(_('Continue'))
            self.primary_button.set_sensitive(self.state.content_ready())
            if self.state.collisions():
                self.footer_status.set_text(
                    _('Resolve module collisions to continue.'))
            else:
                self.footer_status.set_text('')
        elif step == STEP_DEFAULTS:
            self.primary_button.set_label(_('Review'))
            self.primary_button.set_sensitive(
                self.state.content_ready() and self.state.defaults_ready() and
                self._operation is None)
            if self._operation in ('inventory', 'inventory-load'):
                self.footer_status.set_text(_('Session analysis is running…'))
            elif not self.state.include_current_config:
                self.footer_status.set_text(
                    _('The loaded project omits required current '
                      'configuration; Review is blocked.'))
            elif (self.state.customization_input_errors or
                  self.state.customization_error):
                self.footer_status.set_text(
                    _('Correct the invalid customization setting to continue.'))
            elif (self.state.capture_mode == 'exact' and not
                  self.state.sensitive_capture_acknowledged):
                self.footer_status.set_text(
                    _('Acknowledge the sensitivity of including all session changes to continue.'))
            elif (self.state.capture_mode == 'selected' and not
                  self.state.capture_include_paths):
                self.footer_status.set_text(
                    _('Analyze and select at least one session path.'))
            elif not self.state.defaults_ready():
                self.footer_status.set_text(
                    _('Complete the required output and capture settings.'))
            else:
                self.footer_status.set_text('')
        elif step == STEP_REVIEW:
            self.primary_button.set_label(_('Build image'))
            self.primary_button.set_sensitive(bool(
                self.plan is not None and self.plan.buildable and
                self._operation is None))
            if self._operation == 'planning':
                self.footer_status.set_text(_('Planning…'))
            elif self.plan is not None and self.plan.errors:
                self.footer_status.set_text(
                    _('Build remains disabled while blockers are present.'))
            else:
                self.footer_status.set_text('')
        else:
            if self._build_active():
                self.primary_button.hide()
                if self.build_status != 'publishing':
                    self.secondary_button.set_label(_('Cancel'))
                    self.secondary_button.get_style_context().add_class(
                        'destructive-action')
                    self.secondary_button.set_sensitive(
                        not self._cancel_requested)
                    self.secondary_button.show()
                self.footer_status.set_text(
                    _('Do not remove the destination while this operation '
                      'is running.'))
            elif self.build_status == 'success':
                self.primary_button.set_label(_('New image project'))
                self.primary_button.set_sensitive(True)
                self.secondary_button.set_label(
                    _('Open containing folder'))
                self.secondary_button.set_sensitive(True)
                self.secondary_button.show()
                self.back_button.set_sensitive(False)
                self.footer_status.set_text(_('Image published successfully.'))
            else:
                self.primary_button.set_label(_('Return to Review'))
                self.primary_button.set_sensitive(True)
                self.footer_status.set_text('')

    # Project actions ------------------------------------------------------
    def _on_new_project(self, _action, _parameter):
        self._new_project_flow()

    def _on_open_project(self, _action, _parameter):
        if not self._confirm_discard():
            return
        dialog = Gtk.FileChooserDialog(
            title=_('Open image project'), transient_for=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Open'), Gtk.ResponseType.OK)
        file_filter = Gtk.FileFilter()
        file_filter.set_name(_('JSON image projects (*.json)'))
        file_filter.add_pattern('*.json')
        dialog.add_filter(file_filter)
        response = dialog.run()
        path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not path:
            return
        try:
            project = backend.ImageProject.load(path)
        except Exception as error:
            show_error_dialog(
                self, _('Could not open this image project.'),
                _('{error}\n\nChoose a JSON project created by MiniOS Image '
                  'Builder. The file was not changed.').format(error=error))
            return
        self._discard_plan()
        current_source = self.state.source_info
        self.state.load_project(project, source_info=current_source)
        self._reset_build_state()
        self._reset_source_mode_to_session()
        self._sync_defaults_widgets()
        self._render_content()
        self.state.current_step = STEP_SOURCE
        self.state.furthest_step = STEP_SOURCE
        self.page_stack.set_visible_child_name('source')
        self._render_source()
        self._update_chrome()
        self._start_source_discovery(adopt_reference=False)

    def _on_save_project(self, _action, _parameter):
        if self.state.project_path:
            self._save_project_to(self.state.project_path)
        else:
            self._choose_project_save_path()

    def _on_save_project_as(self, _action, _parameter):
        self._choose_project_save_path()

    def _choose_project_save_path(self):
        dialog = Gtk.FileChooserDialog(
            title=_('Save image project'), transient_for=self,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(
            _('Cancel'), Gtk.ResponseType.CANCEL,
            _('Save'), Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(True)
        if self.state.project_path:
            dialog.set_current_folder(os.path.dirname(self.state.project_path))
            dialog.set_current_name(os.path.basename(self.state.project_path))
        else:
            dialog.set_current_name('minios-image-project.json')
        file_filter = Gtk.FileFilter()
        file_filter.set_name(_('JSON image projects (*.json)'))
        file_filter.add_pattern('*.json')
        dialog.add_filter(file_filter)
        response = dialog.run()
        path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if path:
            if not path.lower().endswith('.json'):
                path += '.json'
            self._save_project_to(path)

    def _save_project_to(self, path):
        try:
            project = self.state.to_image_project(
                project_base=os.path.dirname(os.path.abspath(path)),
                project_path=os.path.abspath(path))
            project.save(path)
        except ValueError as error:
            if str(error) == 'source-unavailable':
                detail = _(
                    'Wait for a supported MiniOS source before saving.')
            else:
                detail = str(error)
            show_error_dialog(
                self, _('The image project could not be saved.'), detail)
            return
        except Exception as error:
            show_error_dialog(
                self, _('The image project could not be saved.'), str(error))
            return
        if self.state.mark_saved(path):
            self._invalidate_plan()
            self._sync_defaults_widgets()
        self._update_chrome()

    def _new_project_flow(self):
        if not self._confirm_discard():
            return
        self._discard_plan()
        self._source_generation += 1
        output_path = self._new_default_output_path()
        self.state.default_output_path = output_path
        self.state.default_project_base = os.path.dirname(output_path)
        self.state.reset()
        self._reset_build_state()
        self._reset_source_mode_to_session()
        self._sync_defaults_widgets()
        self._render_content()
        self.page_stack.set_visible_child_name('source')
        self.source_loading = True
        self._render_source()
        self._update_chrome()
        self._start_source_discovery(adopt_reference=True)

    def _reset_build_state(self):
        self.plan = None
        self.active_plan = None
        self.verification_result = None
        self.final_output_path = None
        self.vm_capabilities = None
        self.build_started = False
        self.build_status = 'idle'
        self._cancel_requested = False
        self._clear_overwrite_approval()
        self._inventory_generation += 1
        self._inventory_workspace = None
        self._inventory_status = 'idle'
        self._inventory_message = ''
        self._build_output_redactions = ()
        self._clear_runtime_inventory()
        self._reset_build_page()

    def _confirm_discard(self):
        if not self.state.dirty:
            return True
        return ask_confirmation(
            self, _('Discard changes?'),
            _('Unsaved changes in the current project will be lost.'),
            confirm_label=_('Discard changes'))

    def _new_default_output_path(self):
        directory = os.path.expanduser('~')
        if (not directory or not os.path.isdir(directory) or
                not os.access(directory, os.W_OK | os.X_OK)):
            directory = '/tmp'
        name = 'minios-{}.iso'.format(
            datetime.datetime.now().strftime('%Y%m%d_%H%M'))
        return os.path.join(directory, name)

    # Result actions and close --------------------------------------------
    def _on_open_folder(self, _button):
        self._open_output_folder()

    def _open_output_folder(self):
        path = self.final_output_path
        if not path:
            return
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            show_error_dialog(
                self, _('The containing folder is no longer available.'),
                directory)
            return
        try:
            uri = Gio.File.new_for_path(directory).get_uri()
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as error:
            show_error_dialog(
                self, _('Could not open the containing folder.'), str(error))

    def _on_result_new(self, _button):
        self._new_project_flow()

    def _on_delete_event(self, _window, _event):
        if self._closing:
            return True
        if not self._confirm_discard():
            return True
        source_busy = self._source_task is not None
        if source_busy or self._operation or self.runner or self._build_active():
            if not ask_confirmation(
                    self, _('Stop the current operation and close?'),
                    _('Image Builder will cancel subprocesses immediately. '
                      'Pure Python hashing may finish its current pass before '
                      'the safe cleanup point; publication already in progress '
                      'will complete atomically.'),
                    confirm_label=_('Stop and close')):
                return True
            self._closing = True
            self._cancel_requested = True
            self.set_sensitive(False)
            if self._source_task is not None:
                self._source_task.cancel()
            if self.runner is not None:
                if self._operation == 'inventory':
                    result = request_inventory_cancel(
                        self._inventory_workspace, self.runner)
                    if result.error:
                        sys.stderr.write(
                            'Inventory cancellation error: {}\n'.format(
                                result.error))
                else:
                    self.runner.cancel()
            elif (self._task is not None and
                  self._operation != 'publish'):
                self._task.cancel()
            return True
        self._closing = True
        self._discard_plan()
        self._cleanup_active_plan()
        self._release_medium_mount()
        return False

    def _finish_close(self):
        self._discard_plan()
        self._cleanup_active_plan()
        self._release_medium_mount()
        self.destroy()

    def _finish_close_if_idle(self):
        if (not self._closing or self._source_task is not None or
                self._task is not None or self.runner is not None):
            return
        self._finish_close()


class MiniOSImageBuilderApp(Gtk.Application):
    def __init__(self):
        Gtk.Application.__init__(self, application_id=APPLICATION_ID)
        self.window = None

    def do_activate(self):
        if self.window is None:
            self.window = ImageBuilderWindow(self)
            self.window.connect('destroy', self._on_window_destroyed)
        self.window.show_all()
        self.window.present()

    def _on_window_destroyed(self, _window):
        self.window = None


def main():
    try:
        return MiniOSImageBuilderApp().run(sys.argv)
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
