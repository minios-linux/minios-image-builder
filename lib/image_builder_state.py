#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Headless project state and controller helpers for MiniOS Image Builder.

This module deliberately has no GTK dependency.  It keeps project intent out
of widgets and contains the small pieces of policy that must be unit tested:
navigation gates, module collisions, structured progress parsing, overwrite
scope, and conservative cleanup of private backend job directories.
"""

from __future__ import absolute_import

import os
import posixpath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections import namedtuple
from collections.abc import Mapping

import image_project


STEP_SOURCE = 0
STEP_CONTENT = 1
STEP_DEFAULTS = 2
STEP_REVIEW = 3
STEP_BUILD = 4
STEP_IDS = ('source', 'content', 'defaults', 'review', 'build')

PHASE_PROGRESS = {
    'prepare': 0.04,
    'capture-inventory': 0.08,
    'capture': 0.10,
    'capture-copy': 0.13,
    'capture-compress': 0.17,
    'capture-complete': 0.21,
    'customize': 0.24,
    'boot-copy': 0.28,
    'persistence': 0.42,
    'iso-write': 0.64,
    'verify': 0.78,
    'complete': 0.84,
}

_ANSI_RE = re.compile(
    r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|[\x00\x07\x08]')
_PHASE_RE = re.compile(r'^P:([A-Za-z0-9][A-Za-z0-9._-]*)\s*$')
_TEXT_RE = re.compile(r'^([IWE]):\s*(.*)$')
_SPINNER_SUFFIX_RE = re.compile(r'\s*\[(?:[|/\-\\]|done)\]\s*$')
_SAFE_MODULE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]*\.sb$')
_JOB_PREFIX = '.minios-image-builder-'

CAPTURE_INVENTORY_PAGE_SIZE = 500
CAPTURE_INVENTORY_MAX_DISPLAY_ROWS = 2000

CleanupResult = namedtuple('CleanupResult', ('cleaned', 'warning'))
TaskOutcome = namedtuple('TaskOutcome', ('result', 'error', 'cancelled'))
InventoryCancelResult = namedtuple(
    'InventoryCancelResult', ('marker_requested', 'runner_cancelled', 'error'))
InventoryWorkspace = namedtuple(
    'InventoryWorkspace',
    ('directory', 'output_path', 'identity', 'cancel_path'))


class TaskCancelled(Exception):
    """Raised at a cooperative cancellation checkpoint."""


class CancellationToken(object):
    """Thread-safe cooperative token with process-cancellation callbacks."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks = []

    @property
    def cancelled(self):
        return self._event.is_set()

    def cancel(self):
        with self._lock:
            if self._event.is_set():
                return False
            self._event.set()
            callbacks = list(self._callbacks)
            self._callbacks = []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass
        return True

    def checkpoint(self):
        if self.cancelled:
            raise TaskCancelled('operation was cancelled')

    def wait(self, timeout=None):
        return self._event.wait(timeout)

    def add_cancel_callback(self, callback):
        call_now = False
        with self._lock:
            if self._event.is_set():
                call_now = True
            else:
                self._callbacks.append(callback)
        if call_now:
            callback()

        def remove():
            with self._lock:
                try:
                    self._callbacks.remove(callback)
                except ValueError:
                    pass

        return remove


class BackgroundTask(object):
    """Run one token-aware worker and always deliver a terminal outcome."""

    def __init__(self, worker, completion, dispatcher=None, token=None):
        self.worker = worker
        self.completion = completion
        self.dispatcher = dispatcher
        self.token = token or CancellationToken()
        self._state = 'idle'
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread = None

    @property
    def state(self):
        with self._lock:
            return self._state

    def start(self):
        with self._lock:
            if self._state != 'idle':
                raise RuntimeError('BackgroundTask can only be started once')
            self._state = 'running'
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def cancel(self):
        return self.token.cancel()

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def _run(self):
        result = None
        error = None
        try:
            result = self.worker(self.token)
        except TaskCancelled as caught:
            error = caught
        except Exception as caught:
            error = caught
        cancelled = self.token.cancelled or isinstance(error, TaskCancelled)
        with self._lock:
            if cancelled:
                self._state = 'cancelled'
            elif error is not None:
                self._state = 'failed'
            else:
                self._state = 'finished'
        outcome = TaskOutcome(result, error, cancelled)
        self._done.set()
        if self.dispatcher is None:
            self._deliver(outcome)
        else:
            self.dispatcher(self._deliver, outcome)

    def _deliver(self, outcome):
        if self.token.cancelled and not outcome.cancelled:
            outcome = TaskOutcome(outcome.result, outcome.error, True)
            with self._lock:
                self._state = 'cancelled'
        self.completion(outcome)
        return False


def _signal_process_group(process, pgid, process_signal):
    """Signal the retained group even when its original leader has exited."""
    try:
        os.killpg(pgid, process_signal)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if process.poll() is None:
        try:
            process.send_signal(process_signal)
        except Exception:
            pass


def _read_bounded_pipe(stream, maximum_bytes, result):
    chunks = []
    retained = 0
    try:
        while True:
            block = os.read(stream.fileno(), 64 * 1024)
            if not block:
                break
            available = maximum_bytes - retained
            if available > 0:
                chunk = block[:available]
                chunks.append(chunk)
                retained += len(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass
        result.append(b''.join(chunks))


class CancellableCommandRunner(object):
    """Synchronous backend runner whose process group follows a token."""

    def __init__(self, token=None, cancel_grace=1.0, env=None,
                 maximum_output_bytes=64 * 1024 * 1024):
        self.token = token or CancellationToken()
        self.cancel_grace = max(0.1, float(cancel_grace))
        self.env = env
        self.maximum_output_bytes = max(1024, int(maximum_output_bytes))
        self._process = None
        self._pgid = None
        self._lock = threading.Lock()

    @property
    def process(self):
        with self._lock:
            return self._process

    @property
    def pgid(self):
        with self._lock:
            return self._pgid

    def cancel(self):
        return self.token.cancel()

    def __call__(self, argv, input_data=None):
        return self.run(argv, input_data=input_data)

    def run(self, argv, input_data=None):
        self.token.checkpoint()
        if isinstance(argv, str):
            raise TypeError('argv must be a sequence')
        if isinstance(input_data, str):
            input_data = input_data.encode('utf-8')
        process = subprocess.Popen(
            tuple(str(argument) for argument in argv),
            stdin=(subprocess.PIPE if input_data is not None
                   else subprocess.DEVNULL),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.env, close_fds=True, shell=False,
            start_new_session=True)
        pgid = process.pid
        with self._lock:
            self._process = process
            self._pgid = pgid

        escalation_threads = []

        def terminate():
            _signal_process_group(process, pgid, signal.SIGTERM)

            def escalate():
                time.sleep(self.cancel_grace)
                if self.token.cancelled:
                    _signal_process_group(process, pgid, signal.SIGKILL)

            thread = threading.Thread(target=escalate, daemon=True)
            escalation_threads.append(thread)
            thread.start()

        remove_callback = self.token.add_cancel_callback(terminate)
        stdout_result = []
        stderr_result = []
        stdout_thread = threading.Thread(
            target=_read_bounded_pipe,
            args=(process.stdout, self.maximum_output_bytes, stdout_result),
            daemon=True)
        stderr_thread = threading.Thread(
            target=_read_bounded_pipe,
            args=(process.stderr, self.maximum_output_bytes, stderr_result),
            daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            if input_data is not None:
                try:
                    process.stdin.write(input_data)
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    process.stdin.close()
            process.wait()
            cancel_deadline = None
            for thread, stream in (
                    (stdout_thread, process.stdout),
                    (stderr_thread, process.stderr)):
                while thread.is_alive():
                    thread.join(0.1)
                    if self.token.cancelled and cancel_deadline is None:
                        cancel_deadline = (
                            time.monotonic() + self.cancel_grace + 1.0)
                    if (cancel_deadline is not None and
                            time.monotonic() >= cancel_deadline):
                        try:
                            stream.close()
                        except Exception:
                            pass
                        thread.join(0.2)
                        break
        finally:
            remove_callback()
            for thread in escalation_threads:
                thread.join(0.2)
            with self._lock:
                if self._process is process:
                    self._process = None
                    self._pgid = None
        stdout = stdout_result[0] if stdout_result else b''
        stderr = stderr_result[0] if stderr_result else b''
        self.token.checkpoint()
        return process.returncode, stdout, stderr


class OutputFrameDecoder(object):
    """Incrementally split LF, CRLF, and CR frames with bounded buffering."""

    def __init__(self, maximum_buffer=64 * 1024):
        self.maximum_buffer = max(1024, int(maximum_buffer))
        self._buffer = b''

    @property
    def buffered_bytes(self):
        return len(self._buffer)

    def feed(self, chunk):
        if not isinstance(chunk, bytes):
            raise TypeError('stream chunks must be bytes')
        self._buffer += chunk
        frames = []
        while self._buffer:
            cr_index = self._buffer.find(b'\r')
            lf_index = self._buffer.find(b'\n')
            indexes = [index for index in (cr_index, lf_index) if index >= 0]
            if not indexes:
                if len(self._buffer) > self.maximum_buffer:
                    frames.append(self._buffer[:self.maximum_buffer])
                    self._buffer = self._buffer[self.maximum_buffer:]
                    continue
                break
            index = min(indexes)
            delimiter_size = 1
            if (self._buffer[index:index + 1] == b'\r' and
                    self._buffer[index + 1:index + 2] == b'\n'):
                delimiter_size = 2
            end = index + delimiter_size
            frames.append(self._buffer[:end])
            self._buffer = self._buffer[end:]
        return frames

    def flush(self):
        if not self._buffer:
            return []
        frame = self._buffer
        self._buffer = b''
        return [frame]


def _ordered_unique(values):
    result = []
    seen = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def canonicalize_output_path(value, base_directory):
    """Resolve runtime output intent independently of project serialization."""
    if not isinstance(value, str) or not value.strip():
        return ''
    value = os.path.expanduser(value.strip())
    if not os.path.isabs(value):
        value = os.path.join(base_directory, value)
    return os.path.abspath(value)


def canonicalize_customization_path(value, base_directory, field):
    """Resolve a user-selected input and reject symlinked path spellings."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('{} must be a path'.format(field))
    value = os.path.expanduser(value.strip())
    if not os.path.isabs(value):
        value = os.path.join(base_directory, value)
    value = os.path.abspath(value)
    if os.path.realpath(value) != value:
        raise ValueError('{} path must be canonical'.format(field))
    return value


def validate_boot_background_path(value, base_directory):
    path = canonicalize_customization_path(
        value, base_directory, 'boot background')
    return path, image_project.boot_background_metadata(path)


def validate_overlay_directory(value, base_directory, require_child=False):
    path = canonicalize_customization_path(
        value, base_directory, 'overlay directory')
    try:
        within = os.path.commonpath((path, os.path.abspath(base_directory))) == (
            os.path.abspath(base_directory))
    except ValueError:
        within = False
    if require_child and (
            not within or path == os.path.abspath(base_directory)):
        raise ValueError(
            'overlay directory must be a child of the project directory')
    return path, image_project.inspect_overlay_directory(path)


def create_project_overlay_directory(parent_directory):
    """Create one backend-owned private layer without emulating its policy."""
    parent = canonicalize_customization_path(
        parent_directory, parent_directory, 'project directory')
    if not os.path.isdir(parent):
        raise ValueError('project directory must exist')
    create = getattr(
        image_project, 'create_project_overlay_directory', None)
    if not callable(create):
        raise RuntimeError(
            'backend does not provide create_project_overlay_directory')
    path = canonicalize_customization_path(
        create(parent), parent, 'overlay directory')
    try:
        within = os.path.commonpath((path, parent)) == parent
    except ValueError:
        within = False
    if not within or path == parent:
        raise ValueError('backend overlay directory escaped the project')
    metadata = os.lstat(path)
    if (stat.S_ISLNK(metadata.st_mode) or
            not stat.S_ISDIR(metadata.st_mode) or
            stat.S_IMODE(metadata.st_mode) != 0o700 or
            (hasattr(os, 'geteuid') and metadata.st_uid != os.geteuid())):
        raise ValueError('backend overlay directory is not private')
    return path, image_project.inspect_overlay_directory(path)


def normalize_capture_paths(values):
    """Return deterministic backend-compatible relative session paths."""
    if isinstance(values, str):
        raise ValueError('capture paths must be a sequence')
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError('capture path must be a string')
        try:
            value.encode('utf-8', 'strict')
        except UnicodeError:
            raise ValueError('capture path must contain valid UTF-8 text')
        components = value.split('/')
        if (not value or value.startswith('/') or '\x00' in value or
                '\n' in value or '\r' in value or
                any(item in ('', '.', '..') for item in components) or
                posixpath.normpath(value) != value):
            raise ValueError('capture path must be normalized and relative')
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(sorted(result))


def evaluate_capture_capabilities(probe_result, probe_complete=False):
    """Reduce a backend tool probe to stable session-mode gating reasons."""
    reasons = []
    euid = None
    privilege_mode = None
    if not isinstance(probe_result, dict):
        reasons.append('probe-failed' if probe_complete else 'not-probed')
    else:
        probe_complete = True
        tools = probe_result.get('tools', {})
        savechanges = tools.get('savechanges', {})
        if (not savechanges.get('available') or
                savechanges.get('path') != '/usr/bin/savechanges'):
            reasons.append('savechanges-unavailable')
        elif (savechanges.get('version_probe_returncode') != 0 or
              not savechanges.get('version')):
            reasons.append('savechanges-version-probe-failed')
        privilege = probe_result.get('capture_privilege', {})
        euid = privilege.get('euid')
        privilege_mode = 'direct' if euid == 0 else 'pkexec'
        if not privilege.get('available'):
            reasons.append('authorization-unavailable')
    return {
        'available': not reasons,
        'reason_codes': tuple(reasons),
        'probe_complete': bool(probe_complete),
        'euid': euid,
        'privilege_mode': privilege_mode,
    }


def capture_mode_ready(mode, include_paths, acknowledged,
                       capability_status):
    if mode not in image_project.CAPTURE_MODES:
        return False
    if mode == image_project.NO_SESSION_CAPTURE:
        return True
    if not (isinstance(capability_status, dict) and
            capability_status.get('available')):
        return False
    if mode == 'exact' and not acknowledged:
        return False
    if mode == 'selected' and not include_paths:
        return False
    return True


def session_inventory_summary(inventory):
    """Return aggregate inventory metadata without exposing any path."""
    if inventory is None:
        return None
    if not isinstance(inventory, image_project.SessionInventory):
        raise TypeError('inventory must be a SessionInventory')
    category_counts = dict(
        (name, 0) for name in sorted(image_project.SessionEntry.CATEGORIES))
    regular_bytes = 0
    unknown_regular_sizes = 0
    sensitive_count = 0
    exact_default_count = 0
    clean_default_count = 0
    for entry in inventory.entries:
        category_counts[entry.category] += 1
        if entry.type == 'regular':
            if entry.size is None:
                unknown_regular_sizes += 1
            else:
                regular_bytes += entry.size
        sensitive_count += int(entry.sensitive)
        exact_default_count += int(entry.default_exact)
        clean_default_count += int(entry.default_clean)
    return {
        'union_backend': inventory.union_backend,
        'entry_count': len(inventory.entries),
        'regular_bytes': regular_bytes,
        'unknown_regular_sizes': unknown_regular_sizes,
        'category_counts': category_counts,
        'sensitive_count': sensitive_count,
        'exact_default_count': exact_default_count,
        'clean_default_count': clean_default_count,
        'document_sha256': inventory.document_sha256,
    }


def parse_capture_rule_text(value):
    """Parse one-path-per-line editor text through the state path contract."""
    if not isinstance(value, str):
        raise ValueError('capture rule text must be a string')
    return normalize_capture_paths(
        line.strip() for line in value.splitlines() if line.strip())


def capture_path_excluded_by(path, exclude_paths):
    """Return the most specific exact or ancestor exclusion for ``path``."""
    matches = [
        excluded for excluded in exclude_paths
        if path == excluded or path.startswith(excluded + '/')]
    return max(matches, key=len) if matches else None


def capture_entry_selected(path, include_paths, exclude_paths):
    return bool(
        path in set(include_paths) and
        capture_path_excluded_by(path, exclude_paths) is None)


class CaptureInventoryViewModel(object):
    """Bounded, cached inventory projection used by the GTK selector."""

    def __init__(self, page_size=CAPTURE_INVENTORY_PAGE_SIZE,
                 maximum_rows=CAPTURE_INVENTORY_MAX_DISPLAY_ROWS):
        self.page_size = max(1, int(page_size))
        self.maximum_rows = max(self.page_size, int(maximum_rows))
        self.clear()

    def clear(self):
        self.inventory = None
        self.summary = None
        self.search_text = ''
        self.category = 'all'
        self._matches = ()
        self._filter_key = None
        self.display_limit = self.page_size

    def set_inventory(self, inventory, summary=None):
        if inventory is None:
            self.clear()
            return
        if not isinstance(inventory, image_project.SessionInventory):
            raise TypeError('inventory must be a SessionInventory')
        self.inventory = inventory
        self.summary = (summary if summary is not None
                        else session_inventory_summary(inventory))
        self.search_text = ''
        self.category = 'all'
        self._matches = ()
        self._filter_key = None
        self.display_limit = self.page_size

    def set_filter(self, search_text='', category='all'):
        if not isinstance(search_text, str):
            raise ValueError('inventory search text must be a string')
        allowed = set(image_project.SessionEntry.CATEGORIES)
        allowed.update(('all', 'recommended'))
        if category not in allowed:
            raise ValueError('unsupported inventory category filter')
        search_text = search_text.strip().casefold()
        changed = (search_text, category) != (
            self.search_text, self.category)
        self.search_text = search_text
        self.category = category
        if changed:
            self._filter_key = None
            self.display_limit = self.page_size
        return changed

    def _ensure_matches(self):
        inventory_digest = (
            self.inventory.document_sha256 if self.inventory is not None
            else None)
        key = (inventory_digest, self.search_text, self.category)
        if key == self._filter_key:
            return
        matches = []
        if self.inventory is not None:
            if not self.search_text and self.category == 'all':
                self._matches = self.inventory.entries
                self._filter_key = key
                return
            for entry in self.inventory.entries:
                if (self.search_text and
                        self.search_text not in entry.path.casefold()):
                    continue
                if (self.category == 'recommended' and
                        not (entry.default_clean and not entry.sensitive)):
                    continue
                if (self.category not in ('all', 'recommended') and
                        entry.category != self.category):
                    continue
                matches.append(entry)
        self._matches = tuple(matches)
        self._filter_key = key

    @property
    def total_count(self):
        return (len(self.inventory.entries)
                if self.inventory is not None else 0)

    @property
    def matched_count(self):
        self._ensure_matches()
        return len(self._matches)

    @property
    def displayed_count(self):
        return len(self.visible_entries())

    @property
    def display_cap_reached(self):
        return bool(
            self.matched_count > self.displayed_count and
            self.display_limit >= self.maximum_rows)

    def visible_entries(self):
        self._ensure_matches()
        return self._matches[:min(self.display_limit, self.maximum_rows)]

    def load_more(self):
        before = self.display_limit
        self.display_limit = min(
            self.maximum_rows, self.display_limit + self.page_size)
        return self.display_limit != before


def verification_capture_summary(capture_summary):
    """Select only user-safe capture result metadata for presentation."""
    capture_summary = capture_summary or {}
    if not capture_summary.get('requested'):
        return {
            'requested': False,
            'profile': image_project.NO_SESSION_CAPTURE,
        }
    target = str(capture_summary.get('module_target') or '')
    return {
        'requested': True,
        'profile': capture_summary.get('profile'),
        'union_backend': capture_summary.get('union_backend'),
        'layer_basename': posixpath.basename(target),
        'layer_size': capture_summary.get('module_size'),
        'layer_sha256': capture_summary.get('module_sha256'),
        'selection_sha256': capture_summary.get('selection_sha256'),
    }


def verification_selection_summary(capture_summary, plan_manifest):
    """Join verified selection digest to path-free attested plan counts."""
    capture = verification_capture_summary(capture_summary)
    if (not capture.get('requested') or
            capture.get('profile') != 'selected'):
        return {'applicable': False}
    manifest_capture = (
        plan_manifest.get('capture', {})
        if isinstance(plan_manifest, dict) else {})
    selection = manifest_capture.get('selection', {})
    include_count = selection.get('include_count')
    exclude_count = selection.get('exclude_count')
    digest = capture.get('selection_sha256')
    if (isinstance(include_count, bool) or
            not isinstance(include_count, int) or include_count < 0 or
            isinstance(exclude_count, bool) or
            not isinstance(exclude_count, int) or exclude_count < 0 or
            not isinstance(digest, str)):
        return {'applicable': True, 'valid': False}
    return {
        'applicable': True,
        'valid': True,
        'include_count': include_count,
        'exclude_count': exclude_count,
        'selection_sha256': digest,
    }


def review_customization_summary(manifest, background_path=None,
                                 overlay_directory=None):
    """Select path-free customization intent from one backend plan."""
    manifest = manifest if isinstance(manifest, dict) else {}
    customization = manifest.get('customization', {})
    config = manifest.get('config', {})
    boot = customization.get('boot', {})
    background = boot.get('background')
    overlay = customization.get('overlay', {})
    return {
        'requested': bool(customization.get('requested')),
        'override_keys': tuple(sorted(
            key for key in config.get('override_keys', ())
            if isinstance(key, str))),
        'boot_timeout': boot.get('timeout_seconds'),
        'default_boot': boot.get('default_boot'),
        'kernel_args': (dict(boot.get('kernel_args'))
                        if isinstance(boot.get('kernel_args'), dict) else None),
        'background': (dict(
            background,
            basename=os.path.basename(background_path or 'boot-background.png'))
            if isinstance(background, dict) else None),
        'overlay': ({
            'basename': os.path.basename(
                overlay_directory or overlay.get('module_target') or
                'image-overlay'),
            'input_tree_fingerprint': overlay.get(
                'input_tree_fingerprint'),
            'entry_count': overlay.get('entry_count'),
            'regular_bytes': overlay.get('regular_bytes'),
        } if overlay.get('requested') else None),
    }


def verification_customization_summary(customization_summary, plan_manifest,
                                       background_path=None):
    """Join verified customization metadata to path-free attested key names."""
    verified = (dict(customization_summary)
                if isinstance(customization_summary, Mapping) else {})
    if not verified.get('requested'):
        return {'requested': False}
    manifest = plan_manifest if isinstance(plan_manifest, dict) else {}
    config = manifest.get('config', {})
    keys = tuple(sorted(
        key for key in config.get('override_keys', ())
        if isinstance(key, str)))
    live_config = verified.get('live_config', {})
    if live_config.get('override_count') != len(keys):
        keys = ()
    boot = verified.get('boot', {})
    background = boot.get('background')
    overlay = verified.get('overlay')
    return {
        'requested': True,
        'adapter_report_verified': bool(
            verified.get('adapter_report_verified')),
        'override_keys': keys,
        'boot_timeout': boot.get('timeout_seconds'),
        'default_boot': boot.get('default_boot'),
        'kernel_args': (dict(boot.get('kernel_args'))
                        if isinstance(boot.get('kernel_args'), Mapping)
                        else None),
        'background': (dict(
            background,
            basename=os.path.basename(background_path or 'boot-background.png'))
            if isinstance(background, Mapping) else None),
        'overlay': ({
            'layer_basename': posixpath.basename(
                overlay.get('module_target') or ''),
            'layer_size': overlay.get('module_size'),
            'layer_sha256': overlay.get('module_sha256'),
            'input_tree_fingerprint': overlay.get(
                'input_tree_fingerprint'),
            'entry_count': overlay.get('entry_count'),
        } if isinstance(overlay, Mapping) else None),
    }


def create_inventory_workspace(parent_directory=None):
    """Allocate an identity-bound private directory for one inventory."""
    parent_directory = os.path.abspath(
        parent_directory or tempfile.gettempdir())
    parent_stat = os.lstat(parent_directory)
    if (stat.S_ISLNK(parent_stat.st_mode) or
            not stat.S_ISDIR(parent_stat.st_mode)):
        raise ValueError('inventory parent must be a real directory')
    directory = tempfile.mkdtemp(
        prefix='.minios-image-builder-inventory-', dir=parent_directory)
    try:
        os.chmod(directory, 0o700)
        directory_stat = os.lstat(directory)
        if (stat.S_ISLNK(directory_stat.st_mode) or
                not stat.S_ISDIR(directory_stat.st_mode) or
                stat.S_IMODE(directory_stat.st_mode) != 0o700 or
                (hasattr(os, 'geteuid') and
                 directory_stat.st_uid != os.geteuid())):
            raise ValueError('inventory workspace is not private')
        output_path = os.path.join(directory, 'session-inventory.json')
        cancel_path = os.path.join(directory, 'session-inventory.cancel')
        if os.path.lexists(output_path) or os.path.lexists(cancel_path):
            raise ValueError('inventory workspace is not empty')
        return InventoryWorkspace(
            directory, output_path,
            (int(directory_stat.st_dev), int(directory_stat.st_ino)),
            cancel_path)
    except Exception:
        try:
            os.rmdir(directory)
        except OSError:
            pass
        raise


def cleanup_inventory_workspace(workspace):
    """Remove only an identity-checked inventory output and its own directory."""
    if workspace is None:
        return CleanupResult(True, None)
    if not isinstance(workspace, InventoryWorkspace):
        return CleanupResult(False, 'inventory workspace identity is invalid')
    directory = os.path.abspath(workspace.directory)
    output_path = os.path.abspath(workspace.output_path)
    cancel_path = os.path.abspath(workspace.cancel_path)
    if (os.path.dirname(output_path) != directory or
            os.path.basename(output_path) != 'session-inventory.json' or
            os.path.dirname(cancel_path) != directory or
            os.path.basename(cancel_path) != 'session-inventory.cancel'):
        return CleanupResult(False, 'inventory paths escaped their workspace')
    try:
        directory_stat = os.lstat(directory)
    except OSError as error:
        if getattr(error, 'errno', None) == 2:
            return CleanupResult(True, None)
        return CleanupResult(False, 'cannot inspect inventory workspace')
    identity = (int(directory_stat.st_dev), int(directory_stat.st_ino))
    if (stat.S_ISLNK(directory_stat.st_mode) or
            not stat.S_ISDIR(directory_stat.st_mode) or
            identity != tuple(workspace.identity) or
            stat.S_IMODE(directory_stat.st_mode) != 0o700 or
            (hasattr(os, 'geteuid') and
             directory_stat.st_uid != os.geteuid())):
        return CleanupResult(False, 'inventory workspace identity changed')

    output_identity = None
    if os.path.lexists(output_path):
        try:
            output_stat = os.lstat(output_path)
            if (stat.S_ISLNK(output_stat.st_mode) or
                    not stat.S_ISREG(output_stat.st_mode)):
                return CleanupResult(False, 'inventory output is unsafe')
            output_identity = (
                int(output_stat.st_dev), int(output_stat.st_ino))
        except OSError:
            return CleanupResult(False, 'cannot inspect inventory output')

    cancel_identity = None
    if os.path.lexists(cancel_path):
        try:
            cancel_stat = os.lstat(cancel_path)
            if (stat.S_ISLNK(cancel_stat.st_mode) or
                    not stat.S_ISREG(cancel_stat.st_mode) or
                    stat.S_IMODE(cancel_stat.st_mode) != 0o600 or
                    (hasattr(os, 'geteuid') and
                     cancel_stat.st_uid != os.geteuid())):
                return CleanupResult(False, 'inventory cancel marker is unsafe')
            cancel_identity = (
                int(cancel_stat.st_dev), int(cancel_stat.st_ino))
        except OSError:
            return CleanupResult(
                False, 'cannot inspect inventory cancel marker')

    try:
        if output_identity is not None:
            image_project.cleanup_session_inventory(
                output_path, output_identity)
        if cancel_identity is not None:
            image_project.cleanup_session_inventory(
                cancel_path, cancel_identity)
    except Exception as error:
        return CleanupResult(
            False, 'cannot clean inventory workspace entry: {}'.format(error))
    try:
        final_stat = os.lstat(directory)
        if (stat.S_ISLNK(final_stat.st_mode) or
                not stat.S_ISDIR(final_stat.st_mode) or
                (int(final_stat.st_dev), int(final_stat.st_ino)) !=
                tuple(workspace.identity) or
                stat.S_IMODE(final_stat.st_mode) != 0o700 or
                (hasattr(os, 'geteuid') and
                 final_stat.st_uid != os.geteuid())):
            return CleanupResult(False, 'inventory workspace changed')
        os.rmdir(directory)
    except OSError as error:
        return CleanupResult(
            False, 'cannot remove inventory workspace: {}'.format(error))
    return CleanupResult(True, None)


def request_inventory_cancel(workspace, runner):
    """Request cooperative inventory cancellation, then signal as fallback."""
    errors = []
    marker_requested = False
    if not isinstance(workspace, InventoryWorkspace):
        errors.append('inventory workspace identity is invalid')
    else:
        try:
            marker_requested = bool(
                image_project.request_session_inventory_cancel(
                    workspace.cancel_path, workspace.identity))
            if not marker_requested:
                errors.append(
                    'backend did not confirm the inventory cancel marker')
        except Exception as error:
            errors.append(
                'cannot request the inventory cancel marker: {}'.format(error))

    runner_cancelled = False
    cancel = getattr(runner, 'cancel', None)
    if not callable(cancel):
        errors.append('inventory command runner is unavailable')
    else:
        try:
            runner_cancelled = bool(cancel())
        except Exception as error:
            errors.append(
                'cannot signal the inventory command: {}'.format(error))
    return InventoryCancelResult(
        marker_requested, runner_cancelled,
        '; '.join(errors) if errors else None)


def redact_command_paths(argv, private_paths):
    """Return display-only argv with exact private path arguments removed."""
    private = set(str(path) for path in private_paths if path)
    return tuple(
        '<private-path>' if str(argument) in private else str(argument)
        for argument in argv)


def plan_revision_matches(plan_revision, state_revision):
    return (isinstance(plan_revision, int) and
            plan_revision == state_revision)


def planning_navigation_allowed(current_step, target_step, planning):
    """Planning locks page navigation so Review cannot become blank."""
    return not planning or target_step == current_step


def review_plan_completion_action(current_step, stale=False,
                                  cancelled=False, closing=False):
    """Return accept, restart, discard, or close for a planning completion."""
    if closing:
        return 'close'
    if stale or cancelled:
        return 'restart' if current_step == STEP_REVIEW else 'discard'
    return 'accept'


_OUTPUT_IDENTITY_KEYS = ('device', 'inode', 'size', 'mtime_ns', 'sha256')


def planned_output_observation(plan):
    """Return the exact existing destination identity shown by a plan."""
    manifest = getattr(plan, 'manifest', None)
    if not isinstance(manifest, dict):
        return None
    output = manifest.get('output', {})
    existing = output.get('existing_output', {})
    identity = existing.get('identity') if existing.get('exists') else None
    path = output.get('final_path') or getattr(plan, 'output_path', None)
    if not path or not isinstance(identity, dict):
        return None
    values = tuple(identity.get(key) for key in _OUTPUT_IDENTITY_KEYS)
    if (any(value is None for value in values) or
            not isinstance(values[-1], str)):
        return None
    return (os.path.abspath(path), values)


def overwrite_approval_matches(plan, approved_path, approved_identity):
    observation = planned_output_observation(plan)
    if observation is None:
        return False
    manifest = getattr(plan, 'manifest', {})
    output = manifest.get('output', {})
    return bool(
        output.get('overwrite_allowed') and
        observation == (os.path.abspath(approved_path), approved_identity))


def format_command(argv):
    """Return a shell-safe display string without changing the argv contract."""
    return ' '.join(shlex.quote(str(argument)) for argument in argv)


def prepare_plan_execution(plan):
    """Revalidate a plan, then atomically materialize its minios-image-compose manifest."""
    argv = image_project.prepare_build_command(plan)
    image_project.atomic_write_json(
        plan.adapter_manifest_path, plan.manifest)
    return argv


def create_project_plan(project, source_info, session_inventory=None,
                        command_runner=None, current_config_payload=None):
    """Forward the in-memory inventory explicitly into backend planning."""
    options = {
        'source_info': source_info,
        'session_inventory': session_inventory,
    }
    if current_config_payload is not None:
        options['current_config_payload'] = current_config_payload
    if command_runner is not None:
        options['command_runner'] = command_runner
    return image_project.create_build_plan(project, **options)


LIVE_CONFIG_PATH = '/etc/live/config.conf'
LIVE_CONFIG_READER = (
    '/usr/lib/minios-image-builder/minios-image-builder-read-live-config')


def read_current_live_config(command_runner, path=LIVE_CONFIG_PATH):
    """Read the fixed live config locally or through the narrow root helper."""
    try:
        payload, unused_stat = image_project._read_stable_regular_bytes(
            path, image_project.MAX_LIVE_CONFIG_BYTES)
        return payload
    except PermissionError:
        pass
    argv = [LIVE_CONFIG_READER]
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        argv.insert(0, '/usr/bin/pkexec')
    returncode, stdout, unused_stderr = command_runner(argv)
    if returncode != 0:
        raise RuntimeError(
            'Authorization to read the current MiniOS configuration failed.')
    if len(stdout) > image_project.MAX_LIVE_CONFIG_BYTES:
        raise RuntimeError('Current MiniOS configuration exceeds the size limit.')
    return stdout


def parse_build_output_line(raw):
    """Parse one minios-image-compose output line without interpreting localized prose.

    Stable ``P:<id>`` records are the only source of progress.  ``I:``, ``W:``
    and ``E:`` records are retained as human-readable text only.
    """
    if raw is None:
        raw = ''
    text = _ANSI_RE.sub('', str(raw))
    frames = [frame.rstrip('\n') for frame in text.split('\r')]
    clean = next((frame.rstrip() for frame in reversed(frames)
                  if frame.strip()), '')
    phase_match = _PHASE_RE.match(clean)
    if phase_match:
        phase_id = phase_match.group(1)
        return {
            'kind': 'phase',
            'phase_id': phase_id,
            'fraction': PHASE_PROGRESS.get(phase_id),
            'level': None,
            'text': '',
            'raw': clean,
        }
    text_match = _TEXT_RE.match(clean)
    if text_match:
        return {
            'kind': 'text',
            'phase_id': None,
            'fraction': None,
            'level': text_match.group(1),
            'text': _SPINNER_SUFFIX_RE.sub(
                '', text_match.group(2)).strip(),
            'raw': clean,
        }
    return {
        'kind': 'other',
        'phase_id': None,
        'fraction': None,
        'level': None,
        'text': clean,
        'raw': clean,
    }


def validate_additional_module_path(path):
    """Return a normalized valid module path or raise a stable error code."""
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError('invalid-path')
    path = os.path.abspath(os.path.expanduser(path))
    basename = os.path.basename(path)
    if not _SAFE_MODULE_RE.match(basename):
        raise ValueError('invalid-module-name')
    try:
        file_stat = os.lstat(path)
    except OSError:
        raise ValueError('module-not-found')
    if stat.S_ISLNK(file_stat.st_mode):
        raise ValueError('module-is-symlink')
    if not stat.S_ISREG(file_stat.st_mode) or not os.access(path, os.R_OK):
        raise ValueError('module-not-readable-file')
    return path


def _composition_entries(source_info, selected_names, additional_paths):
    entries = []
    selected_names = set(selected_names)
    if source_info is not None:
        for module in source_info.modules:
            if module.basename in selected_names:
                entries.append({
                    'path': module.path,
                    'basename': module.basename,
                    'target': module.target_path,
                    'kind': 'source',
                })
    for path in additional_paths:
        entries.append({
            'path': os.path.abspath(path),
            'basename': os.path.basename(path),
            'target': image_project.compose_module_target(
                os.path.basename(path)),
            'kind': 'additional',
        })
    return entries


def detect_module_collisions(source_info, selected_names, additional_paths):
    """Return basename and target collisions for the proposed composition."""
    entries = _composition_entries(
        source_info, selected_names, additional_paths)
    collisions = []
    seen = set()

    def add_collision(code, value, grouped):
        paths = tuple(sorted(set(item['path'] for item in grouped)))
        if len(paths) < 2:
            return
        identity = (code, value, paths)
        if identity in seen:
            return
        seen.add(identity)
        collisions.append({
            'code': code,
            'value': value,
            'paths': paths,
            'basenames': tuple(sorted(set(
                item['basename'] for item in grouped))),
            'targets': tuple(sorted(set(item['target'] for item in grouped))),
        })

    by_basename = {}
    by_target = {}
    by_folded_basename = {}
    by_folded_target = {}
    for entry in entries:
        by_basename.setdefault(entry['basename'], []).append(entry)
        by_target.setdefault(entry['target'], []).append(entry)
        by_folded_basename.setdefault(
            entry['basename'].lower(), []).append(entry)
        by_folded_target.setdefault(entry['target'].lower(), []).append(entry)

    for value, grouped in sorted(by_basename.items()):
        if len(grouped) > 1:
            add_collision('module_basename_collision', value, grouped)
    for value, grouped in sorted(by_target.items()):
        if len(grouped) > 1:
            add_collision('duplicate_module_target', value, grouped)
    for value, grouped in sorted(by_folded_basename.items()):
        exact = set(item['basename'] for item in grouped)
        if len(exact) > 1:
            add_collision('module_basename_case_collision', value, grouped)
    for value, grouped in sorted(by_folded_target.items()):
        exact = set(item['target'] for item in grouped)
        if len(exact) > 1:
            add_collision('module_target_case_collision', value, grouped)
    return tuple(collisions)


def collision_paths(collisions):
    result = set()
    for collision in collisions:
        result.update(collision['paths'])
    return result


def cleanup_plan_job(plan):
    """Remove only an identity-checked private job directory owned by a plan.

    Refusing cleanup is preferable to recursively deleting a path whose
    identity or relationship to the destination changed.  Callers must surface
    the warning when ``cleaned`` is false.
    """
    if plan is None or not getattr(plan, 'job_directory', None):
        return CleanupResult(True, None)
    job_directory = os.path.abspath(plan.job_directory)
    output_path = getattr(plan, 'output_path', None)
    if not output_path:
        return CleanupResult(False, 'plan output path is unavailable')
    output_directory = os.path.abspath(
        os.path.dirname(output_path) or os.curdir)
    if (os.path.dirname(job_directory) != output_directory or
            not os.path.basename(job_directory).startswith(_JOB_PREFIX)):
        return CleanupResult(
            False, 'job directory is outside the expected output directory')
    expected_identity = getattr(plan, '_job_identity', None)
    if (not isinstance(expected_identity, tuple) or
            len(expected_identity) != 2):
        return CleanupResult(False, 'plan job identity is unavailable')
    try:
        file_stat = os.lstat(job_directory)
    except OSError as error:
        if getattr(error, 'errno', None) == 2:
            return CleanupResult(True, None)
        return CleanupResult(False, 'cannot inspect job directory: {}'.format(
            error))
    current_identity = (int(file_stat.st_dev), int(file_stat.st_ino))
    if (stat.S_ISLNK(file_stat.st_mode) or
            not stat.S_ISDIR(file_stat.st_mode) or
            current_identity != expected_identity or
            stat.S_IMODE(file_stat.st_mode) != 0o700):
        return CleanupResult(False, 'private job directory identity changed')
    if (hasattr(os, 'geteuid') and file_stat.st_uid != os.geteuid()):
        return CleanupResult(False, 'private job directory owner changed')
    try:
        final_stat = os.lstat(job_directory)
        if ((int(final_stat.st_dev), int(final_stat.st_ino)) !=
                expected_identity):
            return CleanupResult(
                False, 'private job directory changed before cleanup')
        shutil.rmtree(job_directory)
    except OSError as error:
        return CleanupResult(False, 'cannot remove private job directory: {}'.format(
            error))
    if os.path.lexists(job_directory):
        return CleanupResult(False, 'private job directory still exists')
    return CleanupResult(True, None)


class ProjectState(object):
    """Plain Python project intent used by the GTK controller."""

    def __init__(self, default_output_path, project_base=None):
        self.default_output_path = os.path.abspath(default_output_path)
        self.default_project_base = os.path.abspath(
            project_base or os.path.dirname(self.default_output_path) or os.curdir)
        self.source_info = None
        self.revision = 0
        self.reset()

    def reset(self, source_info=None):
        self.project_path = None
        self.project_base = self.default_project_base
        self.source_backend = None
        self.source_root_path = None
        self.source_path = None
        self.source_fingerprint = None
        self.source_fingerprint_algorithm = (
            image_project.SOURCE_FINGERPRINT_ALGORITHM)
        self.selected_source_modules = []
        self.additional_module_paths = []
        self.menu_locale = 'multilang'
        self.capture_mode = image_project.SUPPORTED_CAPTURE_MODE
        self.capture_include_paths = ()
        self.capture_exclude_paths = ()
        self.capture_compression = 'zstd'
        self.sensitive_capture_acknowledged = False
        self.session_inventory = None
        self.capture_capability_status = evaluate_capture_capabilities(None)
        self.include_current_config = True
        self.live_config_overrides = {}
        self.boot_timeout = None
        self.default_boot = None
        self.kernel_args = None
        self.boot_background_path = None
        self.boot_background_metadata = None
        self.overlay_directory = None
        self.overlay_metadata = None
        self.customization_error = None
        self.customization_input_errors = {}
        self.exclusions = ()
        self.output_path = self.default_output_path
        self.volume_label = 'MINIOS'
        self.notes = ''
        self.sensitive_config_acknowledged = False
        self.overwrite_output = False
        self.dirty = True
        self.loaded_project = False
        self.current_step = STEP_SOURCE
        self.furthest_step = STEP_SOURCE
        self.source_info = None
        self.revision += 1
        if source_info is not None:
            self.apply_source_info(source_info, adopt_reference=True)

    @property
    def has_source_reference(self):
        return bool(
            self.source_backend and self.source_root_path and self.source_path
            and self.source_fingerprint)

    @property
    def source_supported(self):
        return bool(self.source_info is not None and self.source_info.supported)

    def _mark_changed(self):
        self.dirty = True
        self.revision += 1

    def _set_source_reference(self, source_info):
        before = (
            self.source_backend, self.source_root_path, self.source_path,
            self.source_fingerprint, self.source_fingerprint_algorithm)
        self.source_backend = source_info.backend
        self.source_root_path = source_info.root_path
        self.source_path = source_info.source_path
        self.source_fingerprint = source_info.fingerprint
        self.source_fingerprint_algorithm = source_info.fingerprint_algorithm
        after = (
            self.source_backend, self.source_root_path, self.source_path,
            self.source_fingerprint, self.source_fingerprint_algorithm)
        return before != after

    def apply_source_info(self, source_info, adopt_reference=False):
        # Inventory describes one observed writable/source state and is never
        # valid across a source refresh, even if the resulting fingerprint is
        # unchanged.
        self.session_inventory = None
        self.source_info = source_info
        intent_changed = False
        self.revision += 1
        if not source_info.supported:
            return
        if adopt_reference or not self.has_source_reference:
            intent_changed = self._set_source_reference(source_info)
        if not self.selected_source_modules and not self.loaded_project:
            self.selected_source_modules = [
                module.basename for module in source_info.modules]
            intent_changed = True
        required = [module.basename for module in source_info.modules
                    if module.required]
        selected = set(self.selected_source_modules)
        missing_required = [name for name in required if name not in selected]
        if missing_required:
            self.selected_source_modules.extend(missing_required)
            intent_changed = True
        self.selected_source_modules = _ordered_unique(
            self.selected_source_modules)
        if intent_changed:
            self.dirty = True

    def load_project(self, project, source_info=None):
        if not isinstance(project, image_project.ImageProject):
            raise TypeError('project must be an ImageProject')
        self.project_path = project.project_path
        self.project_base = project.project_base
        self.source_backend = project.source_backend
        self.source_root_path = project.source_root_path
        self.source_path = project.source_path
        self.source_fingerprint = project.source_fingerprint
        self.source_fingerprint_algorithm = (
            project.source_fingerprint_algorithm)
        self.selected_source_modules = list(project.selected_source_modules)
        self.additional_module_paths = list(project.additional_module_paths)
        self.menu_locale = project.menu_locale
        self.capture_mode = project.capture_mode
        self.capture_include_paths = tuple(project.capture_include_paths)
        self.capture_exclude_paths = tuple(project.capture_exclude_paths)
        self.capture_compression = project.capture_compression
        self.sensitive_capture_acknowledged = (
            project.sensitive_capture_acknowledged)
        self.session_inventory = None
        self.capture_capability_status = evaluate_capture_capabilities(None)
        self.include_current_config = project.include_current_config
        self.live_config_overrides = dict(project.live_config_overrides)
        self.boot_timeout = project.boot_timeout
        self.default_boot = project.default_boot
        self.kernel_args = project.kernel_args
        self.boot_background_path = project.boot_background_path
        self.boot_background_metadata = None
        self.overlay_directory = project.overlay_directory
        self.overlay_metadata = None
        self.customization_error = None
        self.customization_input_errors = {}
        self._refresh_customization_metadata()
        self.exclusions = tuple(project.exclusions)
        self.output_path = project.output_path
        self.volume_label = project.volume_label or ''
        self.notes = project.notes or ''
        self.sensitive_config_acknowledged = (
            project.sensitive_config_acknowledged)
        # Overwrite approval is bound to a runtime file observation and must
        # never be trusted merely because a project persisted the old intent.
        self.overwrite_output = False
        self.loaded_project = True
        self.current_step = STEP_SOURCE
        self.furthest_step = STEP_SOURCE
        self.source_info = None
        self.dirty = bool(project.overwrite_output)
        self.revision += 1
        if source_info is not None:
            self.apply_source_info(source_info, adopt_reference=False)

    def mark_saved(self, path):
        canonical_output = canonicalize_output_path(
            self.output_path, self.project_base)
        project_path = os.path.abspath(path)
        project_base = os.path.dirname(project_path)
        runtime_changed = (
            canonical_output != self.output_path or
            project_base != self.project_base)
        self.output_path = canonical_output
        self.project_path = project_path
        self.project_base = project_base
        self.loaded_project = True
        self.dirty = False
        if runtime_changed:
            self.revision += 1
            self._refresh_customization_metadata()
        return runtime_changed

    def set_source_module_selected(self, basename, selected):
        selected = bool(selected)
        if self.source_info is None:
            return False
        modules = list(self.source_info.modules)
        target = None
        for module in modules:
            if module.basename == basename:
                target = module
                break
        if target is None:
            return False
        if not selected and target.required:
            return False
        values = set(self.selected_source_modules)
        order = target.order_prefix
        if order is None:
            # Unnumbered source module: toggle independently of the stack.
            if selected:
                values.add(basename)
            else:
                values.discard(basename)
        elif selected:
            # A numbered layer sits on top of every lower-numbered layer, so
            # selecting it must also select all layers it depends on.
            for module in modules:
                if (module.order_prefix is not None and
                        module.order_prefix <= order):
                    values.add(module.basename)
        else:
            # Deselecting a layer must also remove every higher-numbered layer
            # stacked on top of it, which would otherwise be broken.
            for module in modules:
                if (module.order_prefix is not None and
                        module.order_prefix >= order and not module.required):
                    values.discard(module.basename)
        new_selected = [
            module.basename for module in modules
            if module.basename in values]
        if new_selected != list(self.selected_source_modules):
            self.selected_source_modules = new_selected
            self._mark_changed()
            return True
        return False

    def module_dependencies_satisfied(self):
        """Numbered source layers must form a gap-free stack from the bottom."""
        if self.source_info is None:
            return False
        selected = set(self.selected_source_modules)
        selected_orders = [
            module.order_prefix for module in self.source_info.modules
            if module.order_prefix is not None and
            module.basename in selected]
        if not selected_orders:
            return True
        highest = max(selected_orders)
        for module in self.source_info.modules:
            if (module.order_prefix is not None and
                    module.order_prefix < highest and
                    module.basename not in selected):
                return False
        return True

    def add_additional_module(self, path, validate=True):
        if validate:
            path = validate_additional_module_path(path)
        else:
            path = os.path.abspath(os.path.expanduser(path))
        identity = os.path.normcase(path)
        if any(os.path.normcase(existing) == identity
               for existing in self.additional_module_paths):
            return False
        self.additional_module_paths.append(path)
        self._mark_changed()
        return True

    def remove_additional_module(self, path):
        identity = os.path.normcase(os.path.abspath(path))
        values = [existing for existing in self.additional_module_paths
                  if os.path.normcase(os.path.abspath(existing)) != identity]
        if len(values) == len(self.additional_module_paths):
            return False
        self.additional_module_paths = values
        self._mark_changed()
        return True

    def set_additional_module_selected(self, path, selected):
        if selected:
            return self.add_additional_module(path, validate=False)
        return self.remove_additional_module(path)

    def set_menu_locale(self, value):
        return self._set_value('menu_locale', value)

    def set_capture_mode(self, value):
        if value not in image_project.CAPTURE_MODES:
            raise ValueError('unsupported capture mode')
        before = (
            self.capture_mode, self.capture_include_paths,
            self.capture_exclude_paths, self.session_inventory)
        self.capture_mode = value
        if value != 'selected':
            self.capture_include_paths = ()
            self.capture_exclude_paths = ()
        if value == image_project.NO_SESSION_CAPTURE:
            self.session_inventory = None
        after = (
            self.capture_mode, self.capture_include_paths,
            self.capture_exclude_paths, self.session_inventory)
        if before == after:
            return False
        self._mark_changed()
        return True

    def set_capture_paths(self, include_paths, exclude_paths=()):
        includes = normalize_capture_paths(include_paths)
        excludes = normalize_capture_paths(exclude_paths)
        if set(includes) & set(excludes):
            raise ValueError('capture excludes cannot duplicate includes')
        values = (includes, excludes)
        if values == (self.capture_include_paths, self.capture_exclude_paths):
            return False
        self.capture_include_paths, self.capture_exclude_paths = values
        self._mark_changed()
        return True

    def set_capture_compression(self, value):
        if value not in image_project.CAPTURE_COMPRESSIONS:
            raise ValueError('unsupported capture compression')
        return self._set_value('capture_compression', value)

    def set_sensitive_capture_acknowledged(self, value):
        return self._set_value(
            'sensitive_capture_acknowledged', bool(value))

    def set_session_inventory(self, inventory):
        if (inventory is not None and
                not isinstance(inventory, image_project.SessionInventory)):
            raise TypeError('inventory must be a SessionInventory')
        before = (self.session_inventory.document_sha256
                  if self.session_inventory is not None else None)
        after = (inventory.document_sha256 if inventory is not None else None)
        if before == after:
            self.session_inventory = inventory
            return False
        self.session_inventory = inventory
        self.revision += 1
        return True

    def clear_runtime_inventory(self):
        return self.set_session_inventory(None)

    def set_capture_capability_status(self, probe_result,
                                      probe_complete=False):
        status = evaluate_capture_capabilities(
            probe_result, probe_complete=probe_complete)
        if status == self.capture_capability_status:
            return False
        self.capture_capability_status = status
        self.revision += 1
        return True

    def set_volume_label(self, value):
        return self._set_value('volume_label', value)

    def set_notes(self, value):
        return self._set_value('notes', value)

    def set_sensitive_config_acknowledged(self, value):
        return self._set_value(
            'sensitive_config_acknowledged', bool(value))

    def _refresh_customization_metadata(self):
        if self.boot_background_path is None:
            self.boot_background_metadata = None
        if self.overlay_directory is None:
            self.overlay_metadata = None
        errors = []
        try:
            self.live_config_overrides = dict(
                image_project.validate_live_config_overrides(
                    self.live_config_overrides))
        except (TypeError, ValueError) as error:
            errors.append(str(error))
        if self.boot_timeout is not None and (
                isinstance(self.boot_timeout, bool) or
                not isinstance(self.boot_timeout, int) or
                self.boot_timeout < 0 or self.boot_timeout > 300):
            errors.append('boot timeout must be an integer from 0 to 300')
        if (self.default_boot is not None and
                self.default_boot not in image_project.DEFAULT_BOOT_MODES):
            errors.append('default boot mode is unsupported')
        if self.kernel_args is not None:
            try:
                image_project.validate_kernel_arguments(self.kernel_args)
            except (TypeError, ValueError) as error:
                errors.append(str(error))
        if self.boot_background_path is not None:
            try:
                path = canonicalize_customization_path(
                    self.boot_background_path, self.project_base,
                    'boot background')
                self.boot_background_path = path
                if self.boot_background_metadata is None:
                    self.boot_background_metadata = (
                        image_project.boot_background_metadata(path))
            except (OSError, TypeError, ValueError,
                    image_project.ImageProjectError) as error:
                errors.append(str(error))
        if self.overlay_directory is not None:
            try:
                path = canonicalize_customization_path(
                    self.overlay_directory, self.project_base,
                    'overlay directory')
                self.overlay_directory = path
                if self.overlay_metadata is None:
                    self.overlay_metadata = (
                        image_project.inspect_overlay_directory(path))
            except (OSError, TypeError, ValueError,
                    image_project.ImageProjectError) as error:
                errors.append(str(error))
        self.customization_error = '; '.join(errors) if errors else None
        return self.customization_error is None

    @property
    def customization_requested(self):
        return bool(
            self.live_config_overrides or self.boot_timeout is not None or
            self.default_boot is not None or self.kernel_args is not None or
            self.boot_background_path is not None or
            self.overlay_directory is not None)

    def customization_ready(self, project_base=None):
        if self.customization_input_errors:
            return False
        base = os.path.abspath(project_base or self.project_base)
        if base == self.project_base:
            return self._refresh_customization_metadata()
        try:
            image_project.validate_live_config_overrides(
                self.live_config_overrides)
            if (self.boot_timeout is not None and
                    (isinstance(self.boot_timeout, bool) or
                     not isinstance(self.boot_timeout, int) or
                     self.boot_timeout < 0 or self.boot_timeout > 300)):
                raise ValueError(
                    'boot timeout must be an integer from 0 to 300')
            if (self.default_boot is not None and
                    self.default_boot not in image_project.DEFAULT_BOOT_MODES):
                raise ValueError('default boot mode is unsupported')
            if self.kernel_args is not None:
                image_project.validate_kernel_arguments(self.kernel_args)
            if self.boot_background_path is not None:
                validate_boot_background_path(
                    self.boot_background_path, base)
            if self.overlay_directory is not None:
                validate_overlay_directory(
                    self.overlay_directory, base, require_child=True)
        except (OSError, TypeError, ValueError,
                image_project.ImageProjectError) as error:
            self.customization_error = str(error)
            return False
        self.customization_error = None
        return True

    def set_customization_input_error(self, key, error):
        if not isinstance(key, str) or not key:
            raise ValueError('customization error key is invalid')
        error = str(error) if error else None
        before = dict(self.customization_input_errors)
        if error:
            self.customization_input_errors[key] = error
        else:
            self.customization_input_errors.pop(key, None)
        if before == self.customization_input_errors:
            return False
        self._mark_changed()
        return True

    def set_live_config_overrides(self, overrides):
        normalized = dict(
            image_project.validate_live_config_overrides(overrides))
        if normalized == self.live_config_overrides:
            return False
        self.live_config_overrides = normalized
        self.customization_error = None
        self._mark_changed()
        return True

    def set_live_config_override(self, key, value):
        overrides = dict(self.live_config_overrides)
        if value is None:
            overrides.pop(key, None)
        else:
            overrides[key] = value
        return self.set_live_config_overrides(overrides)

    def set_boot_timeout(self, value):
        if (value is not None and
                (isinstance(value, bool) or not isinstance(value, int) or
                 value < 0 or value > 300)):
            raise ValueError('boot timeout must be an integer from 0 to 300')
        return self._set_value('boot_timeout', value)

    def set_default_boot(self, value):
        if value is not None and value not in image_project.DEFAULT_BOOT_MODES:
            raise ValueError('unsupported default boot mode')
        return self._set_value('default_boot', value)

    def set_kernel_args(self, value):
        if value is not None:
            image_project.validate_kernel_arguments(value)
        return self._set_value('kernel_args', value)

    def set_boot_background_path(self, value):
        if value is None:
            if self.boot_background_path is None:
                return False
            self.boot_background_path = None
            self.boot_background_metadata = None
        else:
            path, metadata = validate_boot_background_path(
                value, self.project_base)
            if (path == self.boot_background_path and
                    metadata == self.boot_background_metadata):
                return False
            self.boot_background_path = path
            self.boot_background_metadata = metadata
        self.customization_error = None
        self._mark_changed()
        return True

    def set_overlay_directory(self, value, metadata=None):
        if value is None:
            if self.overlay_directory is None:
                return False
            self.overlay_directory = None
            self.overlay_metadata = None
        else:
            if metadata is None:
                path, metadata = validate_overlay_directory(
                    value, self.project_base, require_child=True)
            else:
                path = canonicalize_customization_path(
                    value, self.project_base, 'overlay directory')
            if (path == self.overlay_directory and
                    metadata == self.overlay_metadata):
                return False
            self.overlay_directory = path
            self.overlay_metadata = dict(metadata)
        self.customization_error = None
        self._mark_changed()
        return True

    def create_overlay_directory(self, parent_directory):
        parent = canonicalize_customization_path(
            parent_directory, self.project_base, 'project directory')
        if self.project_path is not None and parent != self.project_base:
            raise ValueError(
                'saved project overlays must be created in the project directory')
        path, metadata = create_project_overlay_directory(parent)
        if self.project_path is None and parent != self.project_base:
            self.project_base = parent
            self.revision += 1
        self.set_overlay_directory(path, metadata=metadata)
        return path

    def set_exclusion_pattern(self, value):
        value = value.strip()
        exclusions = (value,) if value else ()
        return self._set_value('exclusions', exclusions)

    def set_output_path(self, value):
        value = canonicalize_output_path(value, self.project_base)
        if value == self.output_path:
            return False
        self.output_path = value
        self.overwrite_output = False
        self._mark_changed()
        return True

    def set_overwrite_output(self, value):
        return self._set_value('overwrite_output', bool(value))

    def _set_value(self, name, value):
        if getattr(self, name) == value:
            return False
        setattr(self, name, value)
        self._mark_changed()
        return True

    def source_modules(self, selected=None):
        if self.source_info is None:
            return ()
        selected_names = set(self.selected_source_modules)
        if selected is True:
            return tuple(module for module in self.source_info.modules
                         if module.basename in selected_names)
        if selected is False:
            return tuple(module for module in self.source_info.modules
                         if module.basename not in selected_names)
        return self.source_info.modules

    def active_external_paths(self):
        if self.source_info is None:
            return set()
        return set(module.path
                   for module in self.source_info.active_external_modules)

    def manual_additional_paths(self):
        external = self.active_external_paths()
        return tuple(path for path in self.additional_module_paths
                     if path not in external)

    def collisions(self):
        return detect_module_collisions(
            self.source_info, self.selected_source_modules,
            self.additional_module_paths)

    def selected_module_count(self):
        return len(self.source_modules(selected=True)) + len(
            self.additional_module_paths)

    def estimated_input_bytes(self):
        if self.source_info is None:
            return 0
        total = self.source_info.non_module_bytes
        total += sum(module.size or 0
                     for module in self.source_modules(selected=True))
        for path in self.additional_module_paths:
            try:
                file_stat = os.lstat(path)
                if stat.S_ISREG(file_stat.st_mode):
                    total += file_stat.st_size
            except OSError:
                pass
        return total

    def required_modules_selected(self):
        if self.source_info is None:
            return False
        selected = set(self.selected_source_modules)
        return all(not module.required or module.basename in selected
                   for module in self.source_info.modules)

    def content_ready(self):
        return bool(
            self.source_supported and self.source_modules(selected=True) and
            self.required_modules_selected() and
            self.module_dependencies_satisfied() and not self.collisions())

    def defaults_ready(self):
        output = self.output_path.strip()
        if not output or not output.lower().endswith('.iso'):
            return False
        if self.menu_locale not in image_project.MENU_LOCALES:
            return False
        if not capture_mode_ready(
                self.capture_mode, self.capture_include_paths,
                self.sensitive_capture_acknowledged,
                self.capture_capability_status):
            return False
        if not self.include_current_config:
            return False
        if not self.customization_ready():
            return False
        label = self.volume_label
        if label and (len(label) > 32 or
                      any(ord(character) < 32 or ord(character) >= 127
                          for character in label)):
            return False
        return True

    def can_enter_step(self, step, plan=None, build_started=False):
        if step == STEP_SOURCE:
            return True
        if step == STEP_CONTENT:
            return self.source_supported
        if step == STEP_DEFAULTS:
            return self.content_ready()
        if step == STEP_REVIEW:
            return self.content_ready() and self.defaults_ready()
        if step == STEP_BUILD:
            return bool(build_started or (
                plan is not None and getattr(plan, 'buildable', False)))
        return False

    def can_continue(self, plan=None):
        if self.current_step == STEP_SOURCE:
            return self.can_enter_step(STEP_CONTENT)
        if self.current_step == STEP_CONTENT:
            return self.can_enter_step(STEP_DEFAULTS)
        if self.current_step == STEP_DEFAULTS:
            return self.can_enter_step(STEP_REVIEW)
        if self.current_step == STEP_REVIEW:
            return self.can_enter_step(STEP_BUILD, plan=plan)
        return False

    def visit_step(self, step, plan=None, build_started=False):
        if (step > self.furthest_step and
                not self.can_enter_step(
                    step, plan=plan, build_started=build_started)):
            return False
        self.current_step = step
        self.furthest_step = max(self.furthest_step, step)
        return True

    def _ordered_selected_names(self):
        selected = set(self.selected_source_modules)
        ordered = []
        if self.source_info is not None:
            for module in self.source_info.modules:
                if module.basename in selected and module.basename not in ordered:
                    ordered.append(module.basename)
        ordered.extend(sorted(selected - set(ordered)))
        return ordered

    def to_image_project(self, project_base=None, project_path=None,
                          overwrite_output=None):
        if not self.has_source_reference:
            raise ValueError('source-unavailable')
        base = os.path.abspath(project_base or self.project_base)
        if not self.customization_ready(project_base=base):
            detail = self.customization_error
            if self.customization_input_errors:
                detail = '; '.join(
                    self.customization_input_errors[key]
                    for key in sorted(self.customization_input_errors))
            raise ValueError(
                'customization-invalid: {}'.format(detail))
        if overwrite_output is None:
            overwrite_output = self.overwrite_output
        return image_project.ImageProject(
            project_base=base,
            source_backend=self.source_backend,
            source_root_path=self.source_root_path,
            source_path=self.source_path,
            source_fingerprint=self.source_fingerprint,
            source_fingerprint_algorithm=self.source_fingerprint_algorithm,
            selected_source_modules=self._ordered_selected_names(),
            additional_module_paths=list(self.additional_module_paths),
            menu_locale=self.menu_locale,
            capture_mode=self.capture_mode,
            capture_include_paths=self.capture_include_paths,
            capture_exclude_paths=self.capture_exclude_paths,
            capture_compression=self.capture_compression,
            sensitive_capture_acknowledged=(
                self.sensitive_capture_acknowledged),
            include_current_config=self.include_current_config,
            live_config_overrides=dict(self.live_config_overrides),
            boot_timeout=self.boot_timeout,
            default_boot=self.default_boot,
            kernel_args=self.kernel_args,
            boot_background_path=self.boot_background_path,
            overlay_directory=self.overlay_directory,
            exclusions=self.exclusions,
            output_path=canonicalize_output_path(
                self.output_path, self.project_base),
            volume_label=self.volume_label or None,
            notes=self.notes or None,
            sensitive_config_acknowledged=(
                self.sensitive_config_acknowledged),
            overwrite_output=bool(overwrite_output),
            project_path=project_path or self.project_path)


def clear_capture_runtime(state, inventory_view):
    """Clear path-bearing runtime metadata while preserving project rules."""
    changed = state.clear_runtime_inventory()
    inventory_view.clear()
    return changed


def reset_overwrite_intent_for_retry(state):
    """Require a fresh destination observation before every retry."""
    return state.set_overwrite_output(False)
