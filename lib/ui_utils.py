#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared UI utilities for MiniOS Image Builder.

Provides CSS loading, common dialogs, and the specialized CommandRunner that
streams framed backend output without blocking the GTK main loop. The log
widget itself is shared by MiniOS applications through ``minios_gui``.

Copyright (C) 2026 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

import gettext
import os
import shlex
import signal
import subprocess
import threading

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

from image_builder_state import OutputFrameDecoder
from minios_gui import LogView

# The main module binds and selects the text domain process-wide, so using the
# module-level gettext here resolves against that same catalog.
_ = gettext.gettext

# Icon names (from the icon theme, shared with the rest of MiniOS)
ICON_WINDOW   = 'isomaster'
ICON_WARNING  = 'dialog-warning'
ICON_BUILD    = 'drive-optical'
ICON_ADD      = 'list-add-symbolic'
ICON_REMOVE   = 'list-remove-symbolic'
ICON_OPEN     = 'document-open-symbolic'
ICON_INFO     = 'dialog-information-symbolic'

def apply_css_if_exists(css_paths):
    """Load and apply the first CSS file that exists from css_paths."""
    if isinstance(css_paths, str):
        css_paths = [css_paths]
    for css_file_path in css_paths:
        if css_file_path and os.path.exists(css_file_path):
            provider = Gtk.CssProvider()
            try:
                provider.load_from_path(css_file_path)
            except Exception:
                continue
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            return


def show_error_dialog(parent_window, message, secondary=None):
    """Show a modal error dialog."""
    dialog = Gtk.MessageDialog(
        transient_for=parent_window,
        modal=True,
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK,
        text=message
    )
    if secondary:
        dialog.format_secondary_text(secondary)
    dialog.run()
    dialog.destroy()


def show_info_dialog(parent_window, message, secondary=None):
    """Show a modal informational dialog."""
    dialog = Gtk.MessageDialog(
        transient_for=parent_window,
        modal=True,
        message_type=Gtk.MessageType.INFO,
        buttons=Gtk.ButtonsType.OK,
        text=message
    )
    if secondary:
        dialog.format_secondary_text(secondary)
    dialog.run()
    dialog.destroy()


def ask_confirmation(parent_window, message, secondary=None,
                     confirm_label=None):
    """Show a modal yes/no dialog and return True if the user confirmed."""
    dialog = Gtk.MessageDialog(
        transient_for=parent_window,
        modal=True,
        message_type=Gtk.MessageType.QUESTION,
        buttons=(Gtk.ButtonsType.NONE if confirm_label
                 else Gtk.ButtonsType.OK_CANCEL),
        text=message
    )
    if confirm_label:
        dialog.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
        confirm = dialog.add_button(confirm_label, Gtk.ResponseType.OK)
        confirm.get_style_context().add_class('suggested-action')
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
    if secondary:
        dialog.format_secondary_text(secondary)
    response = dialog.run()
    dialog.destroy()
    return response == Gtk.ResponseType.OK


def human_size(num_bytes):
    """Return a human readable representation of a size in bytes."""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return ''
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024.0 or unit == 'TiB':
            if unit == 'B':
                return '%d %s' % (int(size), unit)
            return '%.1f %s' % (size, unit)
        size /= 1024.0
    return '%.1f TiB' % size


def format_command(argv):
    """Format argv for display without changing its list-based execution."""
    return ' '.join(shlex.quote(str(argument)) for argument in argv)


class CommandRunner:
    """Run a command in a background thread, calling line_cb for every output line.

    ``line_cb(line)`` is called from the worker thread for each newline or
    carriage-return frame, including its terminator. It is the caller's
    responsibility to schedule GTK updates via GLib.idle_add.

    ``on_finished(returncode, cancelled)`` is delivered on the GTK main loop.
    """

    def __init__(self, argv, line_cb, on_finished, cwd=None, env=None,
                 state_cb=None, cancel_grace=3.0,
                 maximum_output_buffer=64 * 1024,
                 maximum_output_bytes=8 * 1024 * 1024,
                 display_argv=None):
        if isinstance(argv, str):
            raise TypeError('argv must be a sequence, not a command string')
        self.argv = tuple(str(argument) for argument in argv)
        self.display_argv = tuple(str(argument) for argument in (
            display_argv if display_argv is not None else self.argv))
        self.line_cb = line_cb
        self.on_finished = on_finished
        self.cwd = cwd
        self.env = env
        self.state_cb = state_cb
        self.cancel_grace = max(0.1, float(cancel_grace))
        self.maximum_output_buffer = max(1024, int(maximum_output_buffer))
        self.maximum_output_bytes = max(1024, int(maximum_output_bytes))
        self._process = None
        self._pgid = None
        self._cancelled = False
        self._state = 'idle'
        self._returncode = None
        self._thread = None
        self._escalation_started = False
        self._finished_event = threading.Event()
        self._emitted_output_bytes = 0
        self._output_truncated = False
        self._lock = threading.Lock()

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def process(self):
        with self._lock:
            return self._process

    @property
    def pid(self):
        process = self.process
        return process.pid if process is not None else None

    @property
    def pgid(self):
        with self._lock:
            return self._pgid

    @property
    def returncode(self):
        with self._lock:
            return self._returncode

    @property
    def cancelled(self):
        with self._lock:
            return self._cancelled

    @property
    def is_running(self):
        return self.state in (
            'starting', 'running', 'cancelling', 'killing')

    def wait(self, timeout=None):
        return self._finished_event.wait(timeout)

    @property
    def formatted_command(self):
        return format_command(self.display_argv)

    def _set_state(self, state):
        with self._lock:
            self._state = state
        if self.state_cb is not None:
            GLib.idle_add(self._deliver_state, state)

    def _deliver_state(self, state):
        self.state_cb(state)
        return False

    def start(self):
        with self._lock:
            if self._state != 'idle':
                raise RuntimeError('CommandRunner can only be started once')
            self._state = 'starting'
        if self.state_cb is not None:
            GLib.idle_add(self._deliver_state, 'starting')
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def cancel(self):
        """Request cancellation and escalate the process group after grace."""
        with self._lock:
            if self._state in ('finished', 'cancelled', 'failed-to-start'):
                return False
            self._cancelled = True
            process = self._process
            pgid = self._pgid
            self._state = 'cancelling'
        if self.state_cb is not None:
            GLib.idle_add(self._deliver_state, 'cancelling')
        if process is not None and pgid is not None:
            self._signal_process(process, pgid, signal.SIGTERM)
            self._start_escalation(process, pgid)
        return True

    def _signal_process(self, process, pgid, process_signal):
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

    def _start_escalation(self, process, pgid):
        with self._lock:
            if self._escalation_started:
                return
            self._escalation_started = True
        thread = threading.Thread(
            target=self._escalate_cancellation,
            args=(process, pgid), daemon=True)
        thread.start()

    def _escalate_cancellation(self, process, pgid):
        threading.Event().wait(self.cancel_grace)
        with self._lock:
            should_kill = self._cancelled and self._process is process
            notify = should_kill and self._state in (
                'starting', 'running', 'cancelling', 'killing')
            if notify:
                self._state = 'killing'
        if not should_kill:
            return
        if notify and self.state_cb is not None:
            GLib.idle_add(self._deliver_state, 'killing')
        self._signal_process(process, pgid, signal.SIGKILL)

    def _emit_line(self, line):
        encoded_size = len(line.encode('utf-8', 'replace'))
        preserve = line.lstrip().startswith(('P:', 'E:'))
        if (self._emitted_output_bytes + encoded_size >
                self.maximum_output_bytes and not preserve):
            if not self._output_truncated:
                self._output_truncated = True
                try:
                    self.line_cb(_(
                        'Output display limit reached; continuing with phase '
                        'and error records only.\n'))
                except Exception:
                    pass
            return
        self._emitted_output_bytes += encoded_size
        try:
            self.line_cb(line)
        except Exception:
            # A presentation callback must never leave a child blocked on a
            # full output pipe.
            pass

    def _worker(self):
        env = self.env
        if env is None:
            env = os.environ.copy()
            # The shell tools animate progress with tput(1); give them a usable
            # terminal type so it does not spam "No value for $TERM" warnings.
            env.setdefault('TERM', 'xterm')
        try:
            process = subprocess.Popen(
                self.argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=self.cwd,
                env=env,
                close_fds=True,
                shell=False,
                start_new_session=True,
            )
        except Exception as exc:
            self._emit_line(_('Failed to start: ') + str(exc) + '\n')
            with self._lock:
                self._returncode = 127
                self._state = 'failed-to-start'
            self._finished_event.set()
            if self.state_cb is not None:
                GLib.idle_add(self._deliver_state, 'failed-to-start')
            GLib.idle_add(self._deliver_result, 127)
            return

        with self._lock:
            self._process = process
            self._pgid = process.pid
            cancelled = self._cancelled
            self._state = 'cancelling' if cancelled else 'running'
        if self.state_cb is not None:
            GLib.idle_add(
                self._deliver_state,
                'cancelling' if cancelled else 'running')
        if cancelled:
            self._signal_process(process, process.pid, signal.SIGTERM)
            self._start_escalation(process, process.pid)

        decoder = OutputFrameDecoder(self.maximum_output_buffer)
        fd = process.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            for frame in decoder.feed(chunk):
                self._emit_line(frame.decode('utf-8', 'replace'))
        for frame in decoder.flush():
            self._emit_line(frame.decode('utf-8', 'replace'))
        process.wait()
        try:
            process.stdout.close()
        except Exception:
            pass
        with self._lock:
            self._returncode = process.returncode
            self._state = 'cancelled' if self._cancelled else 'finished'
            self._pgid = None
            state = self._state
        self._finished_event.set()
        if self.state_cb is not None:
            GLib.idle_add(self._deliver_state, state)
        GLib.idle_add(self._deliver_result, process.returncode)

    def _deliver_result(self, returncode):
        self.on_finished(returncode, self._cancelled)
        return False
