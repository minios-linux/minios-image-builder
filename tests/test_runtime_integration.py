import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import gi
import pytest
gi.require_version('Gdk', '3.0')
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, GLib, Gtk, Pango

import image_builder_state as controller
import main_image_builder as ui
from ui_utils import CommandRunner, apply_css_if_exists


def test_command_runner_streams_carriage_return_frames():
    frames = []
    live_frames = []
    result = []
    timed_out = []
    loop = GLib.MainLoop()
    script = (
        "import sys,time\n"
        "for frame in ('I: first\\r', 'I: second\\r', 'P:complete\\n'):\n"
        "    sys.stdout.write(frame)\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.2)\n"
    )

    def on_line(frame):
        frames.append(frame)
        live_frames.append(runner.process.poll() is None)

    def on_finished(returncode, cancelled):
        result.append((returncode, cancelled))
        loop.quit()

    def on_timeout():
        timed_out.append(True)
        loop.quit()
        return False

    runner = CommandRunner(
        [sys.executable, '-c', script], on_line, on_finished)
    runner.start()
    timeout_id = GLib.timeout_add(3000, on_timeout)
    loop.run()
    if not timed_out:
        GLib.source_remove(timeout_id)

    assert not timed_out
    assert result == [(0, False)]
    assert frames == ['I: first\r', 'I: second\r', 'P:complete\n']
    assert live_frames[0] is True


def test_application_css_loads_after_shared_css(tmp_path):
    shared = tmp_path / 'shared.css'
    application = tmp_path / 'application.css'
    shared.write_text('.section-heading { font-weight: 400; }')
    application.write_text('.section-heading { font-weight: 700; }')

    loaded = apply_css_if_exists((
        str(shared), str(tmp_path / 'missing.css'), str(application)))

    assert loaded == (str(shared), str(application))


def test_boot_option_heading_has_typographic_application_style():
    if Gdk.Screen.get_default() is None:
        pytest.skip('GTK screen is unavailable')
    assert apply_css_if_exists((ui.CSS_PATHS[2],)) == (ui.CSS_PATHS[2],)
    surface = Gtk.Box()
    surface.get_style_context().add_class('boot-option-heading')
    label = Gtk.Label(label='Session and storage')
    label.get_style_context().add_class('section-heading')
    surface.add(label)
    window = Gtk.Window()
    window.add(surface)
    window.show_all()
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)

    state = Gtk.StateFlags.NORMAL
    font = label.get_style_context().get_property('font', state)
    assert font.get_weight() >= Pango.Weight.BOLD
    window.destroy()


def test_result_intent_replaces_previous_style_class():
    class _Context:
        def __init__(self):
            self.classes = set(('result-success',))

        def add_class(self, name):
            self.classes.add(name)

        def remove_class(self, name):
            self.classes.discard(name)

    context = _Context()
    window = SimpleNamespace(
        build_result=SimpleNamespace(get_style_context=lambda: context))

    ui.ImageBuilderWindow._set_result_intent(window, 'error')

    assert context.classes == {'result-error'}


def test_clean_capture_diagnostic_has_localizable_display_text():
    title, message = ui.diagnostic_display_text(
        'clean_capture_allowlist', 'backend message is not displayed')

    assert title == ui._('Reusable changes only')
    assert message == ui._(
        'Clean capture uses a strict allowlist. It omits general system '
        'state, user data, identity files, logs, and caches.')


def test_existing_session_layer_diagnostic_explains_that_building_can_continue():
    title, message = ui.diagnostic_display_text(
        'source_session_capture_artifact', 'backend message is not displayed')

    assert title == ui._('Source already contains saved session changes')
    assert 'continue modifying the image' in message
    assert '*-session-changes.sb' in message


def test_space_diagnostics_explain_the_settings_that_can_fix_them():
    destination_title, destination_message = ui.diagnostic_display_text(
        'destination_space_insufficient', 'raw backend byte counts')
    scratch_title, scratch_message = ui.diagnostic_display_text(
        'scratch_space_insufficient', 'raw backend byte counts')

    assert destination_title == ui._('Not enough space for the output image')
    assert 'output path' in destination_message
    assert scratch_title == ui._('Not enough temporary workspace')
    assert 'Settings page' in scratch_message


def test_resource_warnings_have_user_facing_localized_text():
    sensitive_title, sensitive_message = ui.diagnostic_display_text(
        'sensitive_config_present', 'raw backend warning')
    capture_title, capture_message = ui.diagnostic_display_text(
        'capture_size_unknown', 'raw backend warning')

    assert sensitive_title == ui._('Configuration may contain sensitive values')
    assert 'does not display or log' in sensitive_message
    assert capture_title == ui._('Session-change size is unknown')
    assert 'analyze the current session changes' in capture_message


def test_custom_boot_menu_default_label_does_not_embed_entry_title():
    class _Combo:
        def __init__(self):
            self.items = []
            self.active = None
            self.sensitive = True

        def remove_all(self):
            self.items = []

        def append(self, value, label):
            self.items.append((value, label))

        def set_active_id(self, value):
            self.active = value

        def set_sensitive(self, value):
            self.sensitive = value

    window = SimpleNamespace()
    window.state = SimpleNamespace(boot_menu_entries=[{
        'enabled': True,
        'default': True,
        'title': 'A very long custom boot menu title that must not resize the page',
        'base_mode': 'resume',
    }])
    window.default_boot_combo = _Combo()

    ui.ImageBuilderWindow._sync_default_boot_choices(window)

    assert window.default_boot_combo.items == [
        ('constructor', ui._('Set in menu constructor'))]
    assert window.default_boot_combo.active == 'constructor'
    assert window.default_boot_combo.sensitive is False


def test_unknown_diagnostic_preserves_backend_text():
    assert ui.diagnostic_display_text('future_code', 'Future message') == (
        'future_code', 'Future message')


def test_build_failure_detail_prefers_backend_error():
    assert ui.build_command_failure_detail(2, 'input failed') == ui._(
        'The image backend reported: {error}').format(error='input failed')
    assert ui.build_command_failure_detail(2) == ui._(
        'The image command exited with status {status}.').format(status=2)


def test_command_runner_display_argv_does_not_change_execution():
    runner = CommandRunner(
        [sys.executable, '-c', 'pass', '/tmp/private-inventory.json'],
        lambda _line: None, lambda _returncode, _cancelled: None,
        display_argv=(sys.executable, '-c', 'pass', '<private-path>'))

    assert runner.argv[-1] == '/tmp/private-inventory.json'
    assert '/tmp/private-inventory.json' not in runner.formatted_command
    assert '<private-path>' in runner.formatted_command


def test_command_runner_executes_in_requested_cwd(tmp_path):
    frames = []
    result = []
    loop = GLib.MainLoop()
    runner = CommandRunner(
        [sys.executable, '-c',
         'import os,sys; sys.stdout.write(os.getcwd() + "\\n")'],
        frames.append,
        lambda returncode, cancelled: (
            result.append((returncode, cancelled)), loop.quit()),
        cwd=str(tmp_path))

    runner.start()
    timeout_id = GLib.timeout_add(3000, lambda: loop.quit() or False)
    loop.run()
    if result:
        GLib.source_remove(timeout_id)

    assert result == [(0, False)]
    assert ''.join(frames).strip() == str(tmp_path)


@pytest.mark.parametrize('key,active_id,expected', [
    ('DEFAULT_TARGET', 'keep', None),
    ('DEFAULT_TARGET', 'graphical', 'graphical'),
    ('LIVE_SUDO_MODE', 'password', 'password'),
    ('LIVE_POLKIT_MODE', 'disabled', 'disabled'),
    ('LIVE_SSH_PERMIT_ROOT_LOGIN', 'false', 'false'),
    ('LIVE_SSH_PASSWORD_AUTHENTICATION', 'true', 'true'),
    ('LIVE_XRDP_MODE', 'hardened', 'hardened'),
    ('LIVE_X11_MODE', 'relaxed', 'relaxed'),
    ('LIVE_LOCKSCREEN_MODE', 'hardened', 'hardened'),
    ('LIVE_ISSUE_PASSWORD_HINTS', 'false', 'false'),
    ('LIVE_LINK_USER_DIRS', 'true', 'true'),
    ('LIVE_BIND_USER_DIRS', 'false', 'false'),
])
def test_live_config_choice_ui_mapping(key, active_id, expected):
    observed = []
    window = SimpleNamespace(
        _syncing=False,
        _applying_security_preset=False,
        _sync_security_preset=lambda: None,
        _apply_live_config_override=lambda received_key, value:
            observed.append((received_key, value)))
    combo = SimpleNamespace(get_active_id=lambda: active_id)

    ui.ImageBuilderWindow._on_live_config_choice_changed(window, combo, key)

    assert observed == [(key, expected)]


def test_security_profile_values_expose_the_expected_keys():
    strict = ui.security_profile_values('strict')
    assert set(strict) == set(ui.SECURITY_PROFILE_KEYS)
    assert strict['LIVE_SUDO_MODE'] == 'password'
    assert strict['LIVE_SSH_PASSWORD_AUTHENTICATION'] == 'false'
    assert strict['LIVE_XRDP_MODE'] == 'disabled'
    assert ui.security_profile_values('convenient')['LIVE_SUDO_MODE'] == (
        'passwordless')
    assert ui.security_profile_values('nonexistent') == {}


def test_diagnostic_fix_step_points_at_the_right_step():
    window = SimpleNamespace()
    fix = ui.ImageBuilderWindow._diagnostic_fix_step
    assert fix(window, 'module_dependency_gap') == ui.STEP_CONTENT
    assert fix(window, 'required_module_deselected') == ui.STEP_CONTENT
    assert fix(window, 'module_basename_collision') == ui.STEP_CONTENT
    assert fix(window, 'source_inspection_failed') == ui.STEP_SOURCE
    assert fix(window, 'project_source_fingerprint_differs') == ui.STEP_SOURCE
    assert fix(window, 'not_running_minios') == ui.STEP_SOURCE
    assert fix(window, 'current_config_invalid') == ui.STEP_DEFAULTS
    assert fix(window, 'required_tool_missing') == ui.STEP_DEFAULTS
    assert fix(window, 'insufficient_free_space') == ui.STEP_DEFAULTS
    assert fix(window, '') is None


def test_security_preset_apply_and_match_round_trip():
    class _Combo:
        def __init__(self):
            self.value = None

        def set_active_id(self, value):
            self.value = value

    widgets = dict((key, _Combo()) for key in ui.SECURITY_PROFILE_KEYS)
    overrides = {}
    window = SimpleNamespace(
        _syncing=False,
        _applying_security_preset=False,
        live_config_widgets=widgets,
        _apply_live_config_override=lambda key, value:
            overrides.__setitem__(key, value),
        state=SimpleNamespace(live_config_overrides=overrides))

    combo = SimpleNamespace(get_active_id=lambda: 'strict')
    ui.ImageBuilderWindow._on_security_preset_changed(window, combo)

    expected = ui.security_profile_values('strict')
    assert overrides == expected
    assert all(widgets[key].value == expected[key] for key in expected)
    # The individual values round-trip back to the same preset.
    assert ui.ImageBuilderWindow._matching_security_preset(window) == 'strict'
    # A single divergence drops the match back to Custom.
    overrides['LIVE_XRDP_MODE'] = 'relaxed'
    assert ui.ImageBuilderWindow._matching_security_preset(window) == ''


@pytest.mark.parametrize('key,text,expected', [
    ('LIVE_HOSTNAME', ' image-host ', 'image-host'),
    ('LIVE_TIMEZONE', '', None),
    ('ENABLE_SERVICES', 'ssh,cron', 'ssh,cron'),
    ('DISABLE_SERVICES', '  cups  ', 'cups'),
    ('LIVE_USER_DIRS_PATH', 'home/live', 'home/live'),
])
def test_live_config_text_ui_mapping(key, text, expected):
    observed = []
    window = SimpleNamespace(
        _syncing=False,
        _apply_live_config_override=lambda received_key, value:
            observed.append((received_key, value)))
    entry = SimpleNamespace(get_text=lambda: text)

    ui.ImageBuilderWindow._on_live_config_entry_changed(window, entry, key)

    assert observed == [(key, expected)]


def test_boot_parameter_controls_parse_existing_project_arguments():
    settings = ui.parse_boot_parameters(
        'perchmode=luks perchsize=8GB perchreserve=512 toram=trim '
        'load=00-04 noload=firefox text default_target=rescue nomodeset '
        'automount nozram zramcomp=zstd zramsize=2048 locales=ru_RU.UTF-8 '
        'timezone=Europe/Moscow keyboard-layouts=ru quiet debug '
        'from=askdisk custom-option=1')

    assert settings == {
        'persistence_mode': 'luks',
        'persistence_size': '8GB',
        'persistence_reserve': '512',
        'ram_copy': 'trim',
        'load_modules': '00-04',
        'skip_modules': 'firefox',
        'startup': 'text',
        'graphics': 'nomodeset',
        'automount': True,
        'zram': 'off',
        'zram_compression': 'zstd',
        'zram_size': '2048',
        'locale': 'ru_RU.UTF-8',
        'timezone': 'Europe/Moscow',
        'keyboard': 'ru',
        'quiet': True,
        'debug': True,
        'extra': 'from=askdisk custom-option=1',
    }


def test_boot_parameter_controls_compile_stable_legacy_kernel_args():
    settings = dict(ui.BOOT_PARAMETER_DEFAULTS)
    settings.update({
        'persistence_mode': 'dynfilefs',
        'persistence_size': '16GB',
        'ram_copy': 'full',
        'skip_modules': 'firefox,libreoffice',
        'graphics': 'nomodeset',
        'zram_compression': 'lz4',
        'locale': 'de_DE.UTF-8',
        'quiet': True,
        'extra': 'from=askdisk audit=1',
    })

    assert ui.compile_boot_parameters(settings) == (
        'perchmode=dynfilefs perchsize=16GB toram=full '
        'noload=firefox,libreoffice nomodeset zramcomp=lz4 '
        'locales=de_DE.UTF-8 quiet from=askdisk audit=1')


def test_unknown_typed_values_remain_in_expert_parameters():
    settings = ui.parse_boot_parameters(
        'perchmode=future zramcomp=future default-target=future.target')

    assert settings['persistence_mode'] == 'keep'
    assert settings['zram_compression'] == 'keep'
    assert settings['startup'] == 'keep'
    assert settings['extra'] == (
        'perchmode=future zramcomp=future default-target=future.target')
    assert ui.compile_boot_parameters(settings) == settings['extra']


def test_squashfs_session_mode_round_trips_through_typed_controls():
    settings = ui.parse_boot_parameters('perchmode=squashfs perchdir=resume')

    assert settings['persistence_mode'] == 'squashfs'
    assert settings['extra'] == 'perchdir=resume'
    assert ui.compile_boot_parameters(settings) == (
        'perchmode=squashfs perchdir=resume')


def test_source_boot_menu_editor_uses_recognized_entries():
    recognized = {
        'entries': ({
            'id': 'source-fresh', 'base_mode': 'fresh', 'enabled': True,
            'default': True, 'title': 'Source fresh',
            'kernel_args': 'quiet', 'kernel_args_schema': 2,
        },),
    }
    window = SimpleNamespace(
        _source_boot_menu_settings=lambda: recognized,
        state=SimpleNamespace(default_boot=None))

    entries = ui.ImageBuilderWindow._source_boot_menu_editor_entries(window)

    assert entries == [dict(recognized['entries'][0])]


def test_disabled_boot_options_make_dependent_fields_insensitive():
    class Choice:
        def __init__(self, value):
            self.value = value

        def get_active_id(self):
            return self.value

    class Field:
        def __init__(self):
            self.sensitive = None

        def set_sensitive(self, value):
            self.sensitive = value

    persistence_size = Field()
    zram_compression = Field()
    zram_size = Field()
    row = {'option_widgets': {
        'persistence_mode': Choice('squashfs'),
        'persistence_size': persistence_size,
        'zram': Choice('off'),
        'zram_compression': zram_compression,
        'zram_size': zram_size,
    }}

    ui.ImageBuilderWindow._refresh_boot_menu_option_dependencies(None, row)

    assert persistence_size.sensitive is False
    assert zram_compression.sensitive is False
    assert zram_size.sensitive is False


def test_enabled_boot_options_keep_dependent_fields_sensitive():
    class Choice:
        def __init__(self, value):
            self.value = value

        def get_active_id(self):
            return self.value

    class Field:
        def __init__(self):
            self.sensitive = None

        def set_sensitive(self, value):
            self.sensitive = value

    persistence_size = Field()
    zram_compression = Field()
    zram_size = Field()
    row = {'option_widgets': {
        'persistence_mode': Choice('dynfilefs'),
        'persistence_size': persistence_size,
        'zram': Choice('keep'),
        'zram_compression': zram_compression,
        'zram_size': zram_size,
    }}

    ui.ImageBuilderWindow._refresh_boot_menu_option_dependencies(None, row)

    assert persistence_size.sensitive is True
    assert zram_compression.sensitive is True
    assert zram_size.sensitive is True


def test_build_runner_receives_plan_cwd_and_redacted_display(monkeypatch):
    secret_kernel = 'audit=1 private_kernel=1'
    secret_value = 'private-config-value'
    background = '/private/project/background.png'
    overlay = '/private/project/overlay'
    raw_argv = (
        '/usr/bin/minios-image-compose', '--kernel-args', secret_kernel,
        '--boot-background', background, '--overlay-directory', overlay)
    display_argv = (
        '/usr/bin/minios-image-compose', '--kernel-args',
        '<redacted-kernel-arguments>', '--boot-background',
        '<boot-background-input>', '--overlay-directory',
        '<project-overlay-input>')
    observed = {}
    events = []

    class FakeRunner(object):
        def __init__(self, argv, line_cb, on_finished, cwd=None, env=None,
                     display_argv=None):
            observed.update(
                argv=tuple(argv), line_cb=line_cb, cwd=cwd, env=env,
                display_argv=tuple(display_argv))
            self.formatted_command = ' '.join(display_argv)

        def start(self):
            observed['started'] = True

    monkeypatch.setattr(ui, 'CommandRunner', FakeRunner)
    monkeypatch.setattr(
        ui.GLib, 'idle_add',
        lambda callback, event: callback(event))
    label = SimpleNamespace(set_text=lambda _text: None)
    log = SimpleNamespace(feed=lambda text: events.append(text))
    plan = SimpleNamespace(
        execution_cwd='/proc/self/fd/55', display_argv=display_argv,
        scratch_directory='/mnt/fast-work',
        job_directory='/private/job',
        adapter_manifest_path='/private/job/build-manifest.json',
        partial_output_path='/private/job/output.iso')
    state = SimpleNamespace(
        kernel_args=secret_kernel,
        boot_background_path=background,
        overlay_directory=overlay,
        live_config_overrides={'LIVE_HOSTNAME': secret_value},
        additional_module_paths=(), source_path='/private/source/minios',
        source_root_path='/private/source', capture_include_paths=(),
        capture_exclude_paths=())
    window = SimpleNamespace(
        active_plan=plan, state=state, build_status='idle',
        scratch_directory='/tmp/changed-after-review',
        build_phase_label=label, build_detail_label=label,
        build_log=log, runner=None,
        _build_output_redactions=(),
        _on_command_finished=lambda *_args: None,
        _handle_command_event=lambda event: events.append(event),
        _update_chrome=lambda: None)
    window._on_command_line = lambda line: (
        ui.ImageBuilderWindow._on_command_line(window, line))

    ui.ImageBuilderWindow._start_command(window, raw_argv)
    observed['line_cb'](
        'I: {} {} {} {} {}\n'.format(
            secret_kernel, secret_value, background, overlay,
            plan.scratch_directory))

    assert observed['argv'] == raw_argv
    assert observed['cwd'] == plan.execution_cwd
    assert observed['env']['TMPDIR'] == '/mnt/fast-work'
    assert observed['display_argv'] == display_argv
    assert observed['started']
    serialized = repr(events)
    for private in (
            secret_kernel, secret_value, background, overlay,
            plan.scratch_directory):
        assert private not in serialized
    assert '<redacted-kernel-arguments>' in serialized
    assert '<live-config-value>' in serialized
    assert '<temporary-work-directory>' in serialized


def test_inventory_ui_command_passes_and_redacts_cancel_path(
        tmp_path, monkeypatch):
    workspace = controller.create_inventory_workspace(str(tmp_path))
    observed = {}

    def build_command(output_path, cancel_file=None):
        observed['backend_call'] = (output_path, cancel_file)
        return (
            '/usr/bin/pkexec', '/usr/bin/savechanges', '--inventory-json',
            output_path, '--cancel-file', cancel_file)

    class FakeRunner(object):
        def __init__(self, argv, line_cb, on_finished, display_argv=None):
            observed['argv'] = tuple(argv)
            observed['display_argv'] = tuple(display_argv)
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(ui, 'create_inventory_workspace', lambda: workspace)
    monkeypatch.setattr(
        ui.backend, 'build_session_inventory_command', build_command)
    monkeypatch.setattr(ui, 'CommandRunner', FakeRunner)
    window = SimpleNamespace(
        _operation=None, runner=None,
        state=SimpleNamespace(
            capture_mode='exact',
            capture_capability_status={'available': True}),
        _inventory_generation=0,
        _clear_runtime_inventory=lambda: None,
        _invalidate_plan=lambda: None,
        _render_capture_controls=lambda: None,
        _update_chrome=lambda: None)

    ui.ImageBuilderWindow._start_inventory_analysis(window)

    assert observed['backend_call'] == (
        workspace.output_path, workspace.cancel_path)
    assert workspace.output_path in observed['argv']
    assert workspace.cancel_path in observed['argv']
    assert workspace.output_path not in observed['display_argv']
    assert workspace.cancel_path not in observed['display_argv']
    assert observed['display_argv'].count('<private-path>') == 2
    assert window.runner.started
    assert controller.cleanup_inventory_workspace(workspace).cleaned


def test_inventory_cancel_button_reports_marker_error_before_fallback(
        tmp_path, monkeypatch):
    workspace = controller.create_inventory_workspace(str(tmp_path))
    events = []

    def fail_marker(_cancel_path, _parent_identity):
        events.append('marker')
        raise RuntimeError('marker denied')

    monkeypatch.setattr(
        ui.backend, 'request_session_inventory_cancel', fail_marker)
    runner = SimpleNamespace(
        cancel=lambda: events.append('signal') or True)
    window = SimpleNamespace(
        _operation='inventory', runner=runner, _task=None,
        _inventory_workspace=workspace,
        _inventory_status='running', _inventory_message='',
        _render_capture_controls=lambda: None)

    ui.ImageBuilderWindow._on_cancel_inventory(window, None)

    assert events == ['marker', 'signal']
    assert window._inventory_status == 'cancelling'
    assert 'marker denied' in window._inventory_message
    assert 'fallback' in window._inventory_message
    assert controller.cleanup_inventory_workspace(workspace).cleaned


def test_window_close_requests_inventory_marker_before_signal(
        tmp_path, monkeypatch):
    workspace = controller.create_inventory_workspace(str(tmp_path))
    events = []

    def request_marker(cancel_path, parent_identity):
        events.append(('marker', cancel_path, parent_identity))
        return True

    monkeypatch.setattr(
        ui.backend, 'request_session_inventory_cancel', request_marker)
    monkeypatch.setattr(ui, 'ask_confirmation', lambda *args, **kwargs: True)
    runner = SimpleNamespace(
        cancel=lambda: events.append(('signal',)) or True)
    window = SimpleNamespace(
        _closing=False, _source_task=None, _operation='inventory',
        runner=runner, _task=None, _inventory_workspace=workspace,
        _cancel_requested=False,
        _confirm_discard=lambda: True,
        _build_active=lambda: False,
        set_sensitive=lambda sensitive: events.append(
            ('sensitive', sensitive)))

    assert ui.ImageBuilderWindow._on_delete_event(
        window, None, None) is True

    assert events == [
        ('sensitive', False),
        ('marker', workspace.cancel_path, workspace.identity),
        ('signal',),
    ]
    assert window._closing
    assert window._cancel_requested
    assert controller.cleanup_inventory_workspace(workspace).cleaned


def test_window_close_reports_inventory_marker_error(
        tmp_path, monkeypatch, capsys):
    workspace = controller.create_inventory_workspace(str(tmp_path))
    events = []

    def fail_marker(_cancel_path, _parent_identity):
        events.append('marker')
        raise RuntimeError('close marker denied')

    monkeypatch.setattr(
        ui.backend, 'request_session_inventory_cancel', fail_marker)
    monkeypatch.setattr(ui, 'ask_confirmation', lambda *args, **kwargs: True)
    runner = SimpleNamespace(
        cancel=lambda: events.append('signal') or True)
    window = SimpleNamespace(
        _closing=False, _source_task=None, _operation='inventory',
        runner=runner, _task=None, _inventory_workspace=workspace,
        _cancel_requested=False,
        _confirm_discard=lambda: True,
        _build_active=lambda: False,
        set_sensitive=lambda _sensitive: None)

    assert ui.ImageBuilderWindow._on_delete_event(
        window, None, None) is True

    assert events == ['marker', 'signal']
    assert 'close marker denied' in capsys.readouterr().err
    assert controller.cleanup_inventory_workspace(workspace).cleaned


def test_build_cancel_and_close_remain_signal_only(monkeypatch):
    events = []

    def unexpected_inventory_cancel(*_args):
        raise AssertionError('build cancellation used the inventory marker')

    monkeypatch.setattr(ui, 'request_inventory_cancel', unexpected_inventory_cancel)
    runner = SimpleNamespace(
        cancel=lambda: events.append('signal') or True)
    label = SimpleNamespace(set_text=lambda _text: None)
    build_window = SimpleNamespace(
        build_status='building', runner=runner, _task=None, _operation=None,
        _cancel_requested=False, build_phase_label=label,
        build_detail_label=label,
        _clear_overwrite_approval=lambda: None,
        _update_chrome=lambda: None)

    ui.ImageBuilderWindow._request_cancel(build_window)

    assert events == ['signal']

    events[:] = []
    monkeypatch.setattr(ui, 'ask_confirmation', lambda *args, **kwargs: True)
    close_window = SimpleNamespace(
        _closing=False, _source_task=None, _operation=None,
        runner=runner, _task=None, _cancel_requested=False,
        _confirm_discard=lambda: True,
        _build_active=lambda: True,
        set_sensitive=lambda sensitive: events.append(
            ('sensitive', sensitive)))

    assert ui.ImageBuilderWindow._on_delete_event(
        close_window, None, None) is True
    assert events == [('sensitive', False), 'signal']


def test_command_runner_kills_pipe_holder_after_leader_exit(tmp_path):
    pid_path = tmp_path / 'child.pid'
    result = []
    timed_out = []
    loop = GLib.MainLoop()
    child_pid = None
    child_code = (
        'import signal,time; '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        'time.sleep(30)')
    leader_code = (
        'import subprocess,sys; '
        'child=subprocess.Popen([sys.executable,"-c",sys.argv[2]], '
        'stdout=sys.stdout,stderr=sys.stderr); '
        'open(sys.argv[1],"w").write(str(child.pid))')

    def finished(returncode, cancelled):
        result.append((returncode, cancelled))
        loop.quit()

    def timeout():
        timed_out.append(True)
        loop.quit()
        return False

    runner = CommandRunner(
        [sys.executable, '-c', leader_code, str(pid_path), child_code],
        lambda _line: None, finished, cancel_grace=0.1)
    runner.start()
    try:
        deadline = time.time() + 3
        while (not pid_path.exists() or runner.process is None or
               runner.process.poll() is None) and time.time() < deadline:
            time.sleep(0.01)
        assert pid_path.exists()
        child_pid = int(pid_path.read_text(encoding='utf-8'))
        assert runner.process.poll() == 0
        assert runner.cancel()

        timeout_id = GLib.timeout_add(4000, timeout)
        loop.run()
        if not timed_out:
            GLib.source_remove(timeout_id)
        assert not timed_out
        assert result == [(0, True)]
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            raise AssertionError(
                'pipe-holding process-group child survived cancellation')
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_command_runner_bounds_display_output_but_keeps_phase_records():
    frames = []
    result = []
    loop = GLib.MainLoop()
    script = (
        'import sys; '
        '[sys.stdout.write("noise-%04d-" % index + "x" * 80 + "\\n") '
        'for index in range(200)]; '
        'sys.stdout.write("P:complete\\n"); sys.stdout.flush()')

    def finished(returncode, cancelled):
        result.append((returncode, cancelled))
        loop.quit()

    runner = CommandRunner(
        [sys.executable, '-c', script], frames.append, finished,
        maximum_output_bytes=1024)
    runner.start()
    timeout_id = GLib.timeout_add(3000, lambda: loop.quit() or False)
    loop.run()
    if result:
        GLib.source_remove(timeout_id)

    assert result == [(0, False)]
    assert any('Output display limit reached' in frame for frame in frames)
    assert 'P:complete\n' in frames
    assert sum(len(frame) for frame in frames) < 4096


def test_launcher_augments_rootless_path_and_preserves_existing_entries():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    launcher = os.path.join(root, 'bin', 'minios-image-builder')
    original_path = '/opt/minios-test:/usr/bin:/bin'
    env = os.environ.copy()
    env['PATH'] = original_path

    completed = subprocess.run(
        ['/bin/sh', '-x', launcher, '--help'], cwd=root, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, check=False)

    expected = (
        '/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin:'
        + original_path)
    assert completed.returncode == 0
    assert '+ PATH={}'.format(expected) in completed.stderr
    assert 'Usage: minios-image-builder' in completed.stdout


def test_mount_medium_records_ownership_for_iso_loop(monkeypatch, tmp_path):
    iso = tmp_path / 'image.iso'
    iso.write_bytes(b'x')
    mount_dir = tmp_path / 'mnt'
    mount_dir.mkdir()
    window = SimpleNamespace(
        _udisksctl_path=lambda: '/usr/bin/udisksctl',
        _unmount_medium=lambda ownership: None)
    calls = []

    def runner(argv):
        calls.append(list(argv))
        return (0, '', '')

    monkeypatch.setattr(ui.backend, 'find_loop_backing_device',
                        lambda path: '/dev/loop5')
    monkeypatch.setattr(ui.backend, 'resolve_device_mountpoint',
                        lambda device: str(mount_dir))

    ownership = ui.ImageBuilderWindow._mount_medium(
        window, runner, 'iso', str(iso), None)

    assert ownership == {
        'mount_path': str(mount_dir), 'block_device': '/dev/loop5',
        'loop_device': '/dev/loop5', 'media_category': 'iso'}
    assert calls[0][:4] == [
        '/usr/bin/udisksctl', 'loop-setup', '-r', '-f']
    assert ['/usr/bin/udisksctl', 'mount', '-b', '/dev/loop5',
            '--no-user-interaction'] in calls


def test_mount_medium_optical_disowns_preexisting_mount(monkeypatch, tmp_path):
    mount_dir = tmp_path / 'disc'
    mount_dir.mkdir()
    window = SimpleNamespace(
        _udisksctl_path=lambda: '/usr/bin/udisksctl',
        _unmount_medium=lambda ownership: None)

    def runner(argv):
        return (1, '', 'already mounted by the system')

    monkeypatch.setattr(ui.backend, 'resolve_device_mountpoint',
                        lambda device: str(mount_dir))

    ownership = ui.ImageBuilderWindow._mount_medium(
        window, runner, 'optical', None, '/dev/sr0')

    assert ownership['mount_path'] == str(mount_dir)
    # The application did not perform the mount, so it records no unmount
    # ownership and will never unmount media it did not mount.
    assert ownership['block_device'] is None
    assert ownership['loop_device'] is None


def test_mount_medium_requires_udisksctl():
    window = SimpleNamespace(_udisksctl_path=lambda: None)
    with pytest.raises(RuntimeError):
        ui.ImageBuilderWindow._mount_medium(
            window, lambda argv: (0, '', ''), 'optical', None, '/dev/sr0')


def test_unmount_medium_only_acts_on_owned_devices(monkeypatch):
    window = SimpleNamespace(_udisksctl_path=lambda: '/usr/bin/udisksctl')
    runs = []
    monkeypatch.setattr(ui.subprocess, 'run',
                        lambda argv, **kwargs: runs.append(list(argv)))

    ui.ImageBuilderWindow._unmount_medium(window, None)
    ui.ImageBuilderWindow._unmount_medium(
        window, {'block_device': None, 'loop_device': None})
    assert runs == []

    ui.ImageBuilderWindow._unmount_medium(
        window, {'block_device': '/dev/sr0', 'loop_device': None})
    ui.ImageBuilderWindow._unmount_medium(
        window, {'block_device': '/dev/loop5', 'loop_device': '/dev/loop5'})
    assert ['/usr/bin/udisksctl', 'unmount', '-b', '/dev/sr0',
            '--no-user-interaction'] in runs
    assert ['/usr/bin/udisksctl', 'loop-delete', '-b', '/dev/loop5',
            '--no-user-interaction'] in runs
