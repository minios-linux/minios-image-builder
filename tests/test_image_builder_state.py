import os
import stat
import struct
import sys
import threading
import time
import zlib
from types import SimpleNamespace

import pytest

import image_builder_state as controller
import image_project as backend


def _module(path, relative_path, role='custom', required=False, size=1024):
    return backend.ModuleInfo(
        path=str(path), relative_path=relative_path, size=size,
        sha256='a' * 64, order_prefix=backend.parse_module_order(
            os.path.basename(str(path))), role=role,
        friendly_name='Test module', description='Test description',
        source_category='source-top-level', required=required,
        core=role == 'core', active=None)


def _source(tmp_path, status=backend.SOURCE_SUPPORTED):
    if status != backend.SOURCE_SUPPORTED:
        return backend.SourceInfo(status)
    root = tmp_path / 'live-root'
    source = root / 'data' / 'minios'
    modules = (
        _module(source / '00-core-amd64.sb', '00-core-amd64.sb',
                role='core', required=True, size=2048),
        _module(source / '01-kernel-amd64.sb', '01-kernel-amd64.sb',
                role='kernel', required=True, size=4096),
        _module(source / '04-desktop-amd64.sb', '04-desktop-amd64.sb',
                role='desktop', size=8192),
    )
    return backend.SourceInfo(
        status, backend='livekit', root_path=str(root),
        source_path=str(source), media_category='data',
        fingerprint='{}:{}'.format(
            backend.SOURCE_FINGERPRINT_ALGORITHM, 'b' * 64),
        metadata={'architecture': 'amd64'}, modules=modules,
        total_bytes=20000, non_module_bytes=5656)


def _layered_source(tmp_path):
    root = tmp_path / 'live-root'
    source = root / 'data' / 'minios'
    specs = (
        ('00-core-amd64.sb', 'core', True),
        ('01-kernel-amd64.sb', 'kernel', True),
        ('02-firmware-amd64.sb', 'firmware', False),
        ('03-gui-base-amd64.sb', 'gui-base', False),
        ('04-xfce-desktop-amd64.sb', 'desktop', False),
        ('05-firefox-amd64.sb', 'browser', False),
    )
    modules = tuple(
        _module(source / name, name, role=role, required=required)
        for name, role, required in specs)
    return backend.SourceInfo(
        backend.SOURCE_SUPPORTED, backend='livekit', root_path=str(root),
        source_path=str(source), media_category='data',
        fingerprint='{}:{}'.format(
            backend.SOURCE_FINGERPRINT_ALGORITHM, 'c' * 64),
        metadata={'architecture': 'amd64'}, modules=modules,
        total_bytes=60000, non_module_bytes=6000)


def test_deselecting_a_layer_cascades_to_higher_layers(tmp_path):
    state = _state(tmp_path, _layered_source(tmp_path))
    # All six layers selected by default.
    assert state.module_dependencies_satisfied()

    # Dropping the firmware layer (02) must also drop everything stacked on it.
    assert state.set_source_module_selected('02-firmware-amd64.sb', False)
    selected = set(state.selected_source_modules)
    assert selected == {'00-core-amd64.sb', '01-kernel-amd64.sb'}
    assert state.module_dependencies_satisfied()


def test_selecting_a_layer_cascades_to_lower_layers(tmp_path):
    state = _state(tmp_path, _layered_source(tmp_path))
    for name in ('02-firmware-amd64.sb', '03-gui-base-amd64.sb',
                 '04-xfce-desktop-amd64.sb', '05-firefox-amd64.sb'):
        state.set_source_module_selected(name, False)
    assert set(state.selected_source_modules) == {
        '00-core-amd64.sb', '01-kernel-amd64.sb'}

    # Re-enabling the browser (05) must pull in every layer beneath it.
    assert state.set_source_module_selected('05-firefox-amd64.sb', True)
    assert set(state.selected_source_modules) == {
        '00-core-amd64.sb', '01-kernel-amd64.sb', '02-firmware-amd64.sb',
        '03-gui-base-amd64.sb', '04-xfce-desktop-amd64.sb',
        '05-firefox-amd64.sb'}
    assert state.module_dependencies_satisfied()


def test_required_layers_cannot_be_deselected(tmp_path):
    state = _state(tmp_path, _layered_source(tmp_path))
    assert state.set_source_module_selected('00-core-amd64.sb', False) is False
    assert state.set_source_module_selected('01-kernel-amd64.sb', False) is False
    assert {'00-core-amd64.sb', '01-kernel-amd64.sb'}.issubset(
        set(state.selected_source_modules))


def test_module_dependencies_satisfied_detects_a_gap(tmp_path):
    state = _state(tmp_path, _layered_source(tmp_path))
    # Force a hole directly (as a hand-edited project could): keep 04 but drop 02.
    state.selected_source_modules = [
        '00-core-amd64.sb', '01-kernel-amd64.sb', '03-gui-base-amd64.sb',
        '04-xfce-desktop-amd64.sb']
    assert state.module_dependencies_satisfied() is False
    assert state.content_ready() is False


def _state(tmp_path, source_info=None):
    output = tmp_path / 'output' / 'custom.iso'
    output.parent.mkdir(exist_ok=True)
    state = controller.ProjectState(
        str(output), project_base=str(tmp_path))
    if source_info is not None:
        state.apply_source_info(source_info, adopt_reference=True)
    return state


def _capture_probe(available=True):
    return {
        'tools': {
            'savechanges': {
                'available': available,
                'path': '/usr/bin/savechanges' if available else None,
                'version': 'savechanges 1.1.0' if available else None,
                'version_probe_returncode': 0 if available else 127,
            },
        },
        'capture_privilege': {
            'available': available, 'euid': 1000,
            'pkexec': '/usr/bin/pkexec' if available else None,
        },
    }


def _inventory(entries=None):
    if entries is None:
        entries = [
            {
                'path': 'etc/example.conf', 'type': 'regular',
                'category': 'software', 'sensitive': False,
                'default_exact': True, 'default_clean': True, 'size': 4096,
            },
            {
                'path': 'home/live/private.txt', 'type': 'regular',
                'category': 'user-data', 'sensitive': True,
                'default_exact': True, 'default_clean': False, 'size': 1024,
            },
            {
                'path': 'var/cache/example', 'type': 'directory',
                'category': 'logs-cache', 'sensitive': False,
                'default_exact': True, 'default_clean': False,
            },
        ]
    return backend.parse_session_inventory({
        'product_kind': backend.SESSION_INVENTORY_KIND,
        'schema_version': backend.SESSION_INVENTORY_SCHEMA_VERSION,
        'source_fingerprint': 'd' * 64,
        'union_backend': 'overlayfs',
        'entries': entries,
    })


def _png(path, width=32, height=24):
    def chunk(kind, payload):
        body = kind + payload
        return (struct.pack('>I', len(payload)) + body +
                struct.pack('>I', zlib.crc32(body) & 0xffffffff))

    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    rows = b''.join(b'\0' + b'\0' * (width * 3)
                    for unused_row in range(height))
    path.write_bytes(
        b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) +
        chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
    return path


def test_project_state_maps_to_fresh_image_project(tmp_path):
    info = _source(tmp_path)
    state = _state(tmp_path, info)
    addon = tmp_path / 'addon.sb'
    addon.write_bytes(b'squashfs-placeholder')

    assert state.set_source_module_selected(
        '04-desktop-amd64.sb', False)
    assert state.add_additional_module(str(addon))
    state.set_menu_locale('de_DE')
    state.set_volume_label('MINIOS LAB')
    state.set_sensitive_config_acknowledged(True)
    state.set_exclusion_pattern(r'^docs/.*[.]tmp$')
    state.set_notes('release candidate')

    project = state.to_image_project(
        project_base=str(tmp_path), overwrite_output=True)

    assert isinstance(project, backend.ImageProject)
    assert project.source_fingerprint == info.fingerprint
    assert project.selected_source_modules == (
        '00-core-amd64.sb', '01-kernel-amd64.sb')
    assert project.additional_module_paths == (str(addon),)
    assert project.menu_locale == 'de_DE'
    assert project.capture_mode == 'custom'
    assert project.include_current_config is True
    assert project.exclusions == (r'^docs/.*[.]tmp$',)
    assert project.volume_label == 'MINIOS LAB'
    assert project.notes == 'release candidate'
    assert project.sensitive_config_acknowledged is True
    assert project.overwrite_output is True


def test_capture_project_mapping_round_trip_and_runtime_state(tmp_path):
    state = _state(tmp_path, _source(tmp_path))
    state.set_capture_capability_status(_capture_probe())
    state.set_capture_mode('selected')
    state.set_capture_paths(
        ('opt/example',), ('opt/example/cache',))
    state.set_capture_compression('gzip')
    state.set_sensitive_capture_acknowledged(True)
    inventory = _inventory()
    state.set_session_inventory(inventory)

    project = state.to_image_project()
    restored = _state(tmp_path)
    restored.load_project(project)

    assert project.capture_mode == 'selected'
    assert project.capture_include_paths == ('opt/example',)
    assert project.capture_exclude_paths == ('opt/example/cache',)
    assert project.capture_compression == 'gzip'
    assert project.sensitive_capture_acknowledged is True
    assert restored.capture_mode == 'selected'
    assert restored.capture_include_paths == ('opt/example',)
    assert restored.capture_exclude_paths == ('opt/example/cache',)
    assert restored.capture_compression == 'gzip'
    assert restored.sensitive_capture_acknowledged is True
    assert restored.session_inventory is None
    assert not restored.capture_capability_status['available']


def test_false_current_config_intent_survives_state_round_trip(tmp_path):
    state = _state(tmp_path, _source(tmp_path))
    state.include_current_config = False

    project = state.to_image_project()
    restored = _state(tmp_path)
    restored.load_project(project, source_info=_source(tmp_path))

    assert project.include_current_config is False
    assert restored.include_current_config is False
    assert restored.to_image_project().include_current_config is False
    assert not restored.defaults_ready()
    assert not restored.dirty


def test_customization_state_round_trip_dirty_revision_and_readiness(tmp_path):
    state = _state(tmp_path, _source(tmp_path))
    background = _png(tmp_path / 'art' / 'background.png', 64, 48)
    overlay = tmp_path / 'project-layer'
    (overlay / 'etc').mkdir(parents=True)
    (overlay / 'etc' / 'example.conf').write_text(
        'enabled=true\n', encoding='utf-8')
    initial_revision = state.revision

    assert state.set_live_config_overrides({
        'LIVE_HOSTNAME': 'image-host',
        'LIVE_TIMEZONE': 'Etc/UTC',
        'DEFAULT_TARGET': 'graphical',
        'ENABLE_SERVICES': 'ssh,cron',
        'LIVE_SUDO_MODE': 'password',
        'LIVE_LINK_USER_DIRS': 'true',
        'LIVE_USER_DIRS_PATH': 'home/live/Documents',
    })
    assert state.set_boot_timeout(7)
    assert state.set_default_boot('fresh')
    assert state.set_kernel_args('audit=1 quiet')
    assert state.set_boot_background_path(str(background))
    assert state.set_overlay_directory(str(overlay))
    assert state.dirty
    assert state.revision == initial_revision + 6
    assert state.defaults_ready()
    assert state.customization_requested
    assert state.boot_background_metadata['width'] == 64
    assert state.overlay_metadata['entry_count'] == 3

    project = state.to_image_project()
    restored = _state(tmp_path)
    restored.load_project(project)

    assert dict(project.live_config_overrides) == state.live_config_overrides
    assert restored.live_config_overrides == state.live_config_overrides
    assert restored.boot_timeout == 7
    assert restored.default_boot == 'fresh'
    assert restored.kernel_args == 'audit=1 quiet'
    assert restored.boot_background_path == str(background)
    assert restored.overlay_directory == str(overlay)
    assert restored.boot_background_metadata['sha256']
    assert restored.overlay_metadata['input_tree_fingerprint']
    assert restored.defaults_ready()
    assert not restored.dirty


@pytest.mark.parametrize('key,value', [
    ('LIVE_HOSTNAME', 'portable-host'),
    ('LIVE_TIMEZONE', 'Europe/London'),
    ('DEFAULT_TARGET', 'multi-user'),
    ('ENABLE_SERVICES', 'ssh,cron'),
    ('DISABLE_SERVICES', 'cups'),
    ('LIVE_SUDO_MODE', 'password'),
    ('LIVE_POLKIT_MODE', 'disabled'),
    ('LIVE_SSH_PERMIT_ROOT_LOGIN', 'false'),
    ('LIVE_SSH_PASSWORD_AUTHENTICATION', 'true'),
    ('LIVE_XRDP_MODE', 'hardened'),
    ('LIVE_X11_MODE', 'relaxed'),
    ('LIVE_LOCKSCREEN_MODE', 'hardened'),
    ('LIVE_ISSUE_PASSWORD_HINTS', 'false'),
    ('LIVE_LINK_USER_DIRS', 'true'),
    ('LIVE_BIND_USER_DIRS', 'false'),
    ('LIVE_USER_DIRS_PATH', 'home/live/Documents'),
])
def test_customization_override_setters_use_backend_allowlist(
        tmp_path, key, value):
    state = _state(tmp_path, _source(tmp_path))

    assert state.set_live_config_override(key, value)
    assert state.live_config_overrides == {key: value}
    assert state.to_image_project().live_config_overrides[key] == value
    assert state.set_live_config_override(key, None)
    assert state.live_config_overrides == {}


def test_customization_override_conflicts_and_invalid_values_are_rejected(
        tmp_path):
    state = _state(tmp_path, _source(tmp_path))
    state.set_live_config_override('LIVE_LINK_USER_DIRS', 'true')

    with pytest.raises(ValueError, match='conflict'):
        state.set_live_config_override('LIVE_BIND_USER_DIRS', 'true')
    with pytest.raises(ValueError):
        state.set_live_config_override('LIVE_HOSTNAME', 'Bad Host')
    with pytest.raises(ValueError):
        state.set_live_config_override('LIVE_SUDO_MODE', 'unrestricted')
    with pytest.raises(ValueError):
        state.set_kernel_args('secret=$unsafe')
    with pytest.raises(ValueError):
        state.set_boot_timeout(301)
    with pytest.raises(ValueError):
        state.set_default_boot('automatic')

    assert state.live_config_overrides == {'LIVE_LINK_USER_DIRS': 'true'}


def test_customization_preserve_defaults_and_missing_project_fields(tmp_path):
    state = _state(tmp_path, _source(tmp_path))

    assert state.live_config_overrides == {}
    assert state.boot_timeout is None
    assert state.default_boot is None
    assert state.kernel_args is None
    assert state.boot_background_path is None
    assert state.overlay_directory is None
    assert not state.customization_requested
    assert state.defaults_ready()

    project = state.to_image_project()
    restored = _state(tmp_path)
    restored.load_project(project)
    assert not restored.customization_requested
    assert restored.defaults_ready()


def test_project_overlay_creation_uses_backend_and_rejects_unsafe_result(
        tmp_path, monkeypatch):
    state = _state(tmp_path, _source(tmp_path))
    calls = []

    def create(parent):
        calls.append(parent)
        path = os.path.join(parent, 'image-overlay')
        os.mkdir(path, 0o700)
        return path

    assert hasattr(backend, 'create_project_overlay_directory')
    monkeypatch.setattr(
        controller.image_project, 'create_project_overlay_directory', create)

    path = state.create_overlay_directory(str(tmp_path))

    assert calls == [str(tmp_path)]
    assert path == str(tmp_path / 'image-overlay')
    assert state.overlay_directory == path
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o700

    project_parent = tmp_path / 'project-parent'
    project_parent.mkdir()
    other = tmp_path / 'other'
    other.mkdir()
    unsafe_state = _state(tmp_path, _source(tmp_path))
    monkeypatch.setattr(
        controller.image_project, 'create_project_overlay_directory',
        lambda _parent: str(other), raising=False)
    with pytest.raises(ValueError, match='escaped'):
        unsafe_state.create_overlay_directory(str(project_parent))


def test_customization_review_and_verification_summaries_are_path_free(
        tmp_path):
    background = str(tmp_path / 'private-art' / 'splash.png')
    overlay = str(tmp_path / 'private-layer')
    manifest = {
        'config': {
            'override_keys': ['LIVE_HOSTNAME', 'LIVE_SUDO_MODE'],
        },
        'customization': {
            'requested': True,
            'boot': {
                'timeout_seconds': 5,
                'default_boot': 'fresh',
                'kernel_args': {'bytes': 7, 'sha256': 'a' * 64},
                'background': {
                    'width': 80, 'height': 60, 'size': 200,
                    'sha256': 'b' * 64,
                },
            },
            'overlay': {
                'requested': True,
                'module_target': 'minios/05-image-overlay.sb',
                'input_tree_fingerprint': 'c' * 64,
                'entry_count': 4,
                'regular_bytes': 100,
            },
        },
    }
    review = controller.review_customization_summary(
        manifest, background, overlay)
    verified = controller.verification_customization_summary({
        'requested': True,
        'adapter_report_verified': True,
        'live_config': {'override_count': 2},
        'boot': {
            'timeout_seconds': 5,
            'default_boot': 'fresh',
            'kernel_args': {'bytes': 7, 'sha256': 'a' * 64},
            'background': {
                'width': 80, 'height': 60, 'size': 200,
                'sha256': 'b' * 64,
            },
        },
        'overlay': {
            'module_target': 'minios/05-image-overlay.sb',
            'module_size': 4096,
            'module_sha256': 'd' * 64,
            'input_tree_fingerprint': 'c' * 64,
            'entry_count': 4,
        },
    }, manifest, background)

    assert review['override_keys'] == (
        'LIVE_HOSTNAME', 'LIVE_SUDO_MODE')
    assert review['background']['basename'] == 'splash.png'
    assert review['overlay']['basename'] == 'private-layer'
    assert verified['override_keys'] == review['override_keys']
    assert verified['background']['basename'] == 'splash.png'
    assert verified['overlay']['layer_basename'] == '05-image-overlay.sb'
    serialized = repr((review, verified))
    assert str(tmp_path) not in serialized
    assert 'private-value' not in serialized


def test_capture_readiness_modes_acknowledgement_and_selection(tmp_path):
    state = _state(tmp_path, _source(tmp_path))
    state.set_capture_capability_status(_capture_probe())

    assert state.defaults_ready()
    state.set_capture_mode('exact')
    assert not state.defaults_ready()
    state.set_sensitive_capture_acknowledged(True)
    assert state.defaults_ready()

    state.set_capture_mode('clean')
    assert state.defaults_ready()
    state.set_capture_capability_status(_capture_probe(available=False))
    assert not state.defaults_ready()
    state.set_capture_mode('custom')
    assert state.defaults_ready()

    state.set_capture_capability_status(_capture_probe())
    state.set_capture_mode('selected')
    assert not state.defaults_ready()
    state.set_capture_paths(('opt/example',))
    assert state.defaults_ready()


def test_all_capture_intent_changes_are_dirty_and_revisioned(tmp_path):
    state = _state(tmp_path, _source(tmp_path))
    state.mark_saved(str(tmp_path / 'project.json'))
    state.set_capture_capability_status(_capture_probe())
    runtime_revision = state.revision
    assert not state.dirty

    assert state.set_capture_mode('selected')
    assert state.dirty and state.revision == runtime_revision + 1
    state.mark_saved(str(tmp_path / 'project.json'))
    revision = state.revision
    assert state.set_capture_paths(('opt/example',))
    assert state.revision == revision + 1 and state.dirty
    state.mark_saved(str(tmp_path / 'project.json'))
    revision = state.revision
    assert state.set_capture_compression('xz')
    assert state.revision == revision + 1 and state.dirty
    state.mark_saved(str(tmp_path / 'project.json'))
    revision = state.revision
    assert state.set_sensitive_capture_acknowledged(True)
    assert state.revision == revision + 1 and state.dirty

    state.mark_saved(str(tmp_path / 'project.json'))
    revision = state.revision
    assert state.set_session_inventory(_inventory())
    assert state.revision == revision + 1
    assert not state.dirty


def test_capture_capability_gating_is_conditional():
    available = controller.evaluate_capture_capabilities(_capture_probe())
    missing = controller.evaluate_capture_capabilities(
        _capture_probe(available=False))

    assert available == {
        'available': True,
        'reason_codes': (),
        'probe_complete': True,
        'euid': 1000,
        'privilege_mode': 'pkexec',
    }
    assert not missing['available']
    assert set(missing['reason_codes']) == {
        'savechanges-unavailable', 'authorization-unavailable',
    }
    assert controller.capture_mode_ready('custom', (), False, missing)
    assert not controller.capture_mode_ready('clean', (), False, missing)


def test_capture_capability_requires_completed_version_probe():
    version_failed = _capture_probe()
    version_failed['tools']['savechanges']['version_probe_returncode'] = 1

    assert controller.evaluate_capture_capabilities(
        None, probe_complete=True)['reason_codes'] == ('probe-failed',)
    assert controller.evaluate_capture_capabilities(
        version_failed)['reason_codes'] == (
            'savechanges-version-probe-failed',)

    direct = _capture_probe()
    direct['capture_privilege'].update(euid=0, pkexec=None)
    status = controller.evaluate_capture_capabilities(direct)
    assert status['available']
    assert status['privilege_mode'] == 'direct'


def test_navigation_gating_tracks_source_content_and_defaults(tmp_path):
    state = _state(tmp_path)
    state.apply_source_info(
        _source(tmp_path, backend.SOURCE_UNSUPPORTED),
        adopt_reference=True)
    assert not state.can_enter_step(controller.STEP_CONTENT)

    info = _source(tmp_path)
    state.apply_source_info(info, adopt_reference=True)
    assert state.can_enter_step(controller.STEP_CONTENT)
    assert state.can_enter_step(controller.STEP_DEFAULTS)
    assert state.can_enter_step(controller.STEP_REVIEW)

    duplicate = tmp_path / '00-core-amd64.sb'
    duplicate.write_bytes(b'duplicate')
    state.add_additional_module(str(duplicate))
    assert not state.content_ready()
    assert not state.can_enter_step(controller.STEP_DEFAULTS)

    state.remove_additional_module(str(duplicate))
    state.set_output_path(str(tmp_path / 'not-an-image'))
    assert state.content_ready()
    assert not state.defaults_ready()
    assert not state.can_enter_step(controller.STEP_REVIEW)


def test_required_modules_are_selected_and_locked(tmp_path):
    info = _source(tmp_path)
    state = _state(tmp_path, info)

    assert not state.set_source_module_selected('00-core-amd64.sb', False)
    assert '00-core-amd64.sb' in state.selected_source_modules


def test_dirty_state_and_save_baseline(tmp_path):
    state = _state(tmp_path, _source(tmp_path))
    assert state.dirty

    project_path = tmp_path / 'project.json'
    state.mark_saved(str(project_path))
    assert not state.dirty
    assert not state.set_menu_locale('multilang')
    assert not state.dirty

    assert state.set_menu_locale('fr_FR')
    assert state.dirty
    state.mark_saved(str(project_path))
    assert not state.dirty


def test_overwrite_policy_requires_explicit_state_and_resets_for_new_path(
        tmp_path):
    state = _state(tmp_path, _source(tmp_path))
    state.mark_saved(str(tmp_path / 'project.json'))

    assert state.overwrite_output is False
    state.set_overwrite_output(True)
    assert state.to_image_project().overwrite_output is True
    assert state.dirty

    state.set_output_path(str(tmp_path / 'other.iso'))
    assert state.overwrite_output is False
    assert state.to_image_project().overwrite_output is False


def test_overwrite_retry_requires_same_observed_destination(tmp_path):
    state = _state(tmp_path, _source(tmp_path))
    identity = {
        'device': 1, 'inode': 2, 'size': 3, 'mtime_ns': 4,
        'sha256': 'a' * 64,
    }
    path = state.output_path
    plan = SimpleNamespace(
        output_path=path,
        manifest={'output': {
            'final_path': path,
            'overwrite_allowed': True,
            'existing_output': {'exists': True, 'identity': identity},
        }})
    observation = controller.planned_output_observation(plan)

    state.set_overwrite_output(True)
    assert controller.overwrite_approval_matches(
        plan, observation[0], observation[1])
    changed_plan = SimpleNamespace(
        output_path=path,
        manifest={'output': {
            'final_path': path,
            'overwrite_allowed': True,
            'existing_output': {
                'exists': True,
                'identity': dict(identity, sha256='b' * 64),
            },
        }})
    assert not controller.overwrite_approval_matches(
        changed_plan, observation[0], observation[1])

    for _terminal_status in (
            'cancelled', 'build-failed', 'verification-failed',
            'publication-failed', 'destination-changed'):
        state.set_overwrite_output(True)
        assert controller.reset_overwrite_intent_for_retry(state)
        assert state.overwrite_output is False


def test_save_as_preserves_absolute_targets_and_invalidates_project_base(
        tmp_path):
    first_base = tmp_path / 'first-project'
    second_base = tmp_path / 'second-project'
    first_base.mkdir()
    second_base.mkdir()
    state = controller.ProjectState(
        str(first_base / 'default.iso'), project_base=str(first_base))
    state.apply_source_info(_source(tmp_path), adopt_reference=True)
    background = _png(first_base / 'background.png')

    state.set_output_path('images/custom.iso')
    state.set_boot_background_path(str(background))
    expected = str(first_base / 'images' / 'custom.iso')
    assert state.output_path == expected
    revision = state.revision
    rebased_project = state.to_image_project(project_base=str(second_base))
    assert rebased_project.output_path == expected
    assert rebased_project.boot_background_path == str(background)

    runtime_changed = state.mark_saved(str(second_base / 'project.json'))
    assert runtime_changed is True
    assert state.revision == revision + 1
    assert state.output_path == expected
    assert state.boot_background_path == str(background)
    assert not controller.plan_revision_matches(revision, state.revision)
    assert state.defaults_ready()


def test_save_as_rejects_overlay_outside_new_project_base(tmp_path):
    first_base = tmp_path / 'first-project'
    second_base = tmp_path / 'second-project'
    first_base.mkdir()
    second_base.mkdir()
    state = controller.ProjectState(
        str(first_base / 'default.iso'), project_base=str(first_base))
    state.apply_source_info(_source(tmp_path), adopt_reference=True)
    overlay = first_base / 'overlay'
    overlay.mkdir()
    state.set_overlay_directory(str(overlay))

    # The overlay is a child of first_base and remains buildable there.
    assert state.customization_ready(project_base=str(first_base))
    # Saving the project under a different base would leave the overlay outside
    # the project directory, which the adapter cannot build, so it is rejected
    # early with a clear error rather than failing late during the build.
    assert not state.customization_ready(project_base=str(second_base))
    with pytest.raises(ValueError) as excinfo:
        state.to_image_project(project_base=str(second_base))
    assert 'child of the project directory' in str(excinfo.value)


def test_plan_revision_and_review_planning_policy():
    assert controller.plan_revision_matches(8, 8)
    assert not controller.plan_revision_matches(8, 9)
    assert not controller.planning_navigation_allowed(
        controller.STEP_REVIEW, controller.STEP_DEFAULTS, True)
    assert controller.planning_navigation_allowed(
        controller.STEP_REVIEW, controller.STEP_REVIEW, True)
    assert controller.review_plan_completion_action(
        controller.STEP_REVIEW, stale=True) == 'restart'
    assert controller.review_plan_completion_action(
        controller.STEP_DEFAULTS, cancelled=True) == 'discard'
    assert controller.review_plan_completion_action(
        controller.STEP_REVIEW, closing=True) == 'close'


def test_phase_parser_uses_stable_ids_and_never_translated_prose():
    phase = controller.parse_build_output_line('P:iso-write\n')
    assert phase['kind'] == 'phase'
    assert phase['phase_id'] == 'iso-write'
    assert phase['fraction'] == controller.PHASE_PROGRESS['iso-write']

    translated = controller.parse_build_output_line(
        'I: Generando la imagen ISO [done]\n')
    assert translated['kind'] == 'text'
    assert translated['level'] == 'I'
    assert translated['text'] == 'Generando la imagen ISO'
    assert translated['fraction'] is None

    unknown = controller.parse_build_output_line(
        '\x1b[32mP:future-phase\x1b[0m\n')
    assert unknown['kind'] == 'phase'
    assert unknown['phase_id'] == 'future-phase'
    assert unknown['fraction'] is None

    carriage = controller.parse_build_output_line(
        'I: Working [|]\rI: Working [done]\n')
    assert carriage['text'] == 'Working'

    for phase_id in (
            'capture', 'capture-inventory', 'capture-copy',
            'capture-compress', 'capture-complete', 'customize'):
        event = controller.parse_build_output_line(
            'P:{}\n'.format(phase_id))
        assert event['fraction'] == controller.PHASE_PROGRESS[phase_id]
        assert event['fraction'] < controller.PHASE_PROGRESS['boot-copy']


def test_command_display_quotes_each_argv_element():
    assert controller.format_command([
        '/usr/bin/minios-image-compose', '--name', '/tmp/My image.iso', "a'b.sb",
    ]) == "/usr/bin/minios-image-compose --name '/tmp/My image.iso' 'a'\"'\"'b.sb'"


def test_inventory_workspace_cleanup_and_command_redaction(tmp_path):
    workspace = controller.create_inventory_workspace(str(tmp_path))
    directory_stat = os.lstat(workspace.directory)
    assert stat.S_IMODE(directory_stat.st_mode) == 0o700
    assert workspace.identity == (
        int(directory_stat.st_dev), int(directory_stat.st_ino))
    assert os.path.dirname(workspace.cancel_path) == workspace.directory
    assert os.path.basename(workspace.cancel_path) == 'session-inventory.cancel'
    assert not os.path.lexists(workspace.cancel_path)

    with open(workspace.output_path, 'w', encoding='utf-8') as handle:
        handle.write('{}')
    os.chmod(workspace.output_path, 0o600)
    assert backend.request_session_inventory_cancel(
        workspace.cancel_path, workspace.identity)
    cancel_stat = os.lstat(workspace.cancel_path)
    assert stat.S_ISREG(cancel_stat.st_mode)
    assert stat.S_IMODE(cancel_stat.st_mode) == 0o600
    assert cancel_stat.st_uid == os.geteuid()
    command = (
        '/usr/bin/pkexec', '/usr/bin/savechanges', '--inventory-json',
        workspace.output_path, '--cancel-file', workspace.cancel_path,
        workspace.directory)
    redacted = controller.redact_command_paths(
        command, (workspace.output_path, workspace.cancel_path,
                  workspace.directory))
    display = controller.format_command(redacted)
    assert workspace.output_path not in display
    assert workspace.cancel_path not in display
    assert workspace.directory not in display
    assert display.count('<private-path>') == 3

    cleanup = controller.cleanup_inventory_workspace(workspace)
    assert cleanup.cleaned
    assert not os.path.exists(workspace.directory)


def test_inventory_workspace_cleanup_rejects_unsafe_cancel_marker(tmp_path):
    workspace = controller.create_inventory_workspace(str(tmp_path))
    with open(workspace.cancel_path, 'w', encoding='utf-8') as handle:
        handle.write('cancel')
    os.chmod(workspace.cancel_path, 0o644)

    cleanup = controller.cleanup_inventory_workspace(workspace)

    assert not cleanup.cleaned
    assert 'cancel marker is unsafe' in cleanup.warning
    assert os.path.isdir(workspace.directory)
    assert os.path.isfile(workspace.cancel_path)

    os.chmod(workspace.cancel_path, 0o600)
    assert controller.cleanup_inventory_workspace(workspace).cleaned


def test_inventory_workspace_cleanup_checks_cancel_marker_identity(
        tmp_path, monkeypatch):
    workspace = controller.create_inventory_workspace(str(tmp_path))
    assert backend.request_session_inventory_cancel(
        workspace.cancel_path, workspace.identity)
    original_cleanup = controller.image_project.cleanup_session_inventory

    def replace_before_cleanup(path, expected_identity=None):
        os.unlink(path)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('replacement')
        os.chmod(path, 0o600)
        return original_cleanup(path, expected_identity)

    monkeypatch.setattr(
        controller.image_project, 'cleanup_session_inventory',
        replace_before_cleanup)

    cleanup = controller.cleanup_inventory_workspace(workspace)

    assert not cleanup.cleaned
    assert 'identity changed' in cleanup.warning
    assert os.path.isfile(workspace.cancel_path)
    monkeypatch.setattr(
        controller.image_project, 'cleanup_session_inventory',
        original_cleanup)
    assert controller.cleanup_inventory_workspace(workspace).cleaned


def test_inventory_workspace_cleanup_refuses_identity_change(tmp_path):
    workspace = controller.create_inventory_workspace(str(tmp_path))
    os.rmdir(workspace.directory)
    os.mkdir(workspace.directory, 0o700)

    cleanup = controller.cleanup_inventory_workspace(workspace)

    assert not cleanup.cleaned
    assert os.path.isdir(workspace.directory)


def test_inventory_cancel_requests_marker_before_runner_signal(
        tmp_path, monkeypatch):
    workspace = controller.create_inventory_workspace(str(tmp_path))
    events = []

    def request(cancel_path, parent_identity):
        events.append(('marker', cancel_path, parent_identity))
        return True

    runner = SimpleNamespace(
        cancel=lambda: events.append(('signal',)) or True)
    monkeypatch.setattr(
        controller.image_project, 'request_session_inventory_cancel', request)

    result = controller.request_inventory_cancel(workspace, runner)

    assert result == controller.InventoryCancelResult(True, True, None)
    assert events == [
        ('marker', workspace.cancel_path, workspace.identity),
        ('signal',),
    ]
    assert controller.cleanup_inventory_workspace(workspace).cleaned


def test_inventory_cancel_reports_marker_error_and_signals_fallback(
        tmp_path, monkeypatch):
    workspace = controller.create_inventory_workspace(str(tmp_path))
    events = []

    def request(_cancel_path, _parent_identity):
        events.append('marker')
        raise RuntimeError('marker denied')

    runner = SimpleNamespace(
        cancel=lambda: events.append('signal') or True)
    monkeypatch.setattr(
        controller.image_project, 'request_session_inventory_cancel', request)

    result = controller.request_inventory_cancel(workspace, runner)

    assert not result.marker_requested
    assert result.runner_cancelled
    assert 'marker denied' in result.error
    assert events == ['marker', 'signal']
    assert controller.cleanup_inventory_workspace(workspace).cleaned


def test_planning_forwards_runtime_inventory(monkeypatch):
    inventory = _inventory()
    observed = {}

    def create(project, source_info=None, session_inventory=None):
        observed.update(
            project=project, source_info=source_info,
            inventory=session_inventory)
        return 'plan'

    monkeypatch.setattr(controller.image_project, 'create_build_plan', create)
    assert controller.create_project_plan(
        'project', 'source', inventory) == 'plan'
    assert observed == {
        'project': 'project', 'source_info': 'source',
        'inventory': inventory,
    }


def test_planning_forwards_authorized_live_config(monkeypatch):
    observed = {}

    def create(project, **options):
        observed.update(options)
        return 'plan'

    monkeypatch.setattr(controller.image_project, 'create_build_plan', create)
    assert controller.create_project_plan(
        'project', 'source', current_config_payload=b'private\n') == 'plan'
    assert observed['current_config_payload'] == b'private\n'


def test_planning_forwards_selected_scratch_directory(monkeypatch):
    observed = {}

    def create(project, **options):
        observed.update(options)
        return 'plan'

    monkeypatch.setattr(controller.image_project, 'create_build_plan', create)
    assert controller.create_project_plan(
        'project', 'source', scratch_directory='/mnt/work') == 'plan'
    assert observed['scratch_directory'] == '/mnt/work'


def test_private_live_config_uses_fixed_privileged_reader(monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError('private')

    calls = []
    monkeypatch.setattr(
        controller.image_project, '_read_stable_regular_bytes', denied)
    monkeypatch.setattr(controller.os, 'geteuid', lambda: 1000)

    payload = controller.read_current_live_config(
        lambda argv: calls.append(argv) or (0, b'CONFIG=value\n', b''))

    assert payload == b'CONFIG=value\n'
    assert calls == [[
        '/usr/bin/pkexec',
        '/usr/lib/minios-image-builder/minios-image-builder-read-live-config',
    ]]


def test_inventory_and_verification_capture_summaries_hide_paths():
    inventory_summary = controller.session_inventory_summary(_inventory())
    assert inventory_summary['union_backend'] == 'overlayfs'
    assert inventory_summary['entry_count'] == 3
    assert inventory_summary['regular_bytes'] == 5120
    assert inventory_summary['sensitive_count'] == 1
    assert inventory_summary['exact_default_count'] == 3
    assert inventory_summary['clean_default_count'] == 1
    assert inventory_summary['category_counts']['software'] == 1

    result = controller.verification_capture_summary({
        'requested': True,
        'profile': 'clean',
        'union_backend': 'overlayfs',
        'module_target': 'minios/42-session-changes.sb',
        'module_size': 8192,
        'module_sha256': 'a' * 64,
        'selection_sha256': 'b' * 64,
        'private_path': 'home/live/secret',
    })
    assert result == {
        'requested': True,
        'profile': 'clean',
        'union_backend': 'overlayfs',
        'layer_basename': '42-session-changes.sb',
        'layer_size': 8192,
        'layer_sha256': 'a' * 64,
        'selection_sha256': 'b' * 64,
    }
    assert 'secret' not in repr(result)

    selection = controller.verification_selection_summary({
        'requested': True,
        'profile': 'selected',
        'union_backend': 'overlayfs',
        'module_target': 'minios/42-session-changes.sb',
        'module_size': 8192,
        'module_sha256': 'a' * 64,
        'selection_sha256': 'b' * 64,
    }, {
        'capture': {'selection': {'include_count': 3, 'exclude_count': 2}},
    })
    assert selection == {
        'applicable': True,
        'valid': True,
        'include_count': 3,
        'exclude_count': 2,
        'selection_sha256': 'b' * 64,
    }


def test_capture_exclusion_precedence_and_rule_parser():
    includes = ('home/live', 'home/live/cache/file')
    excludes = ('home/live/cache',)

    assert controller.capture_path_excluded_by(
        'home/live/cache', excludes) == 'home/live/cache'
    assert controller.capture_path_excluded_by(
        'home/live/cache/file', excludes) == 'home/live/cache'
    assert controller.capture_path_excluded_by(
        'home/live/document', excludes) is None
    assert controller.capture_entry_selected(
        'home/live/cache/file', includes, excludes) is False
    assert controller.capture_entry_selected(
        'home/live', includes, excludes) is True
    assert controller.parse_capture_rule_text(
        'home/live\n\nopt/example\n') == ('home/live', 'opt/example')
    with pytest.raises(ValueError):
        controller.parse_capture_rule_text('../escape')


def test_inventory_runtime_clear_preserves_selection_intent(tmp_path):
    state = _state(tmp_path, _source(tmp_path))
    state.set_capture_capability_status(_capture_probe())
    state.set_capture_mode('selected')
    state.set_capture_paths(('home/live',), ('home/live/cache',))
    inventory = _inventory()
    state.set_session_inventory(inventory)
    view = controller.CaptureInventoryViewModel(page_size=1, maximum_rows=2)
    view.set_inventory(inventory)
    view.set_filter('private', 'user-data')
    view.load_more()

    assert controller.clear_capture_runtime(state, view)
    assert state.session_inventory is None
    assert state.capture_include_paths == ('home/live',)
    assert state.capture_exclude_paths == ('home/live/cache',)
    assert view.inventory is None
    assert view.summary is None
    assert view.search_text == ''
    assert view.category == 'all'
    assert view.visible_entries() == ()

    state.set_session_inventory(inventory)
    state.apply_source_info(_source(tmp_path), adopt_reference=False)
    assert state.session_inventory is None


def test_inventory_view_is_bounded_with_fifty_thousand_entries():
    entries = [{
        'path': 'opt/bulk/item-{:05d}'.format(index),
        'type': 'regular', 'category': 'software', 'sensitive': False,
        'default_exact': True, 'default_clean': True, 'size': index,
    } for index in range(50000)]
    inventory = _inventory(entries)
    view = controller.CaptureInventoryViewModel(
        page_size=500, maximum_rows=2000)

    view.set_inventory(inventory)
    cached_summary = view.summary
    assert cached_summary['entry_count'] == 50000
    assert view.total_count == 50000
    assert view.matched_count == 50000
    assert len(view.visible_entries()) == 500

    assert view.load_more()
    assert len(view.visible_entries()) == 1000
    assert view.load_more()
    assert view.load_more()
    assert len(view.visible_entries()) == 2000
    assert view.display_cap_reached
    assert not view.load_more()

    view.set_filter('item-49999', 'software')
    assert view.matched_count == 1
    assert [entry.path for entry in view.visible_entries()] == [
        'opt/bulk/item-49999']
    assert view.summary is cached_summary


def test_prepare_execution_revalidates_before_materializing_manifest(
        tmp_path, monkeypatch):
    events = []
    plan = SimpleNamespace(
        adapter_manifest_path=str(tmp_path / 'job' / 'manifest.json'),
        manifest={'plan_id': 'test-plan'})

    def prepare(received):
        assert received is plan
        events.append('prepare')
        return ('minios-image-compose', '--manifest', plan.adapter_manifest_path)

    def write(path, payload):
        events.append(('write', path, payload))

    monkeypatch.setattr(
        controller.image_project, 'prepare_build_command', prepare)
    monkeypatch.setattr(
        controller.image_project, 'atomic_write_json', write)

    argv = controller.prepare_plan_execution(plan)

    assert argv[0] == 'minios-image-compose'
    assert events == [
        'prepare',
        ('write', plan.adapter_manifest_path, plan.manifest),
    ]


def test_background_task_delivers_cancelled_outcome_without_using_result():
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    outcomes = []

    def worker(_token):
        started.set()
        release.wait(2)
        return 'stale-result'

    def completion(outcome):
        outcomes.append(outcome)
        completed.set()

    task = controller.BackgroundTask(worker, completion).start()
    assert started.wait(1)
    assert task.cancel()
    release.set()
    assert completed.wait(2)
    assert outcomes[0].cancelled is True
    assert outcomes[0].result == 'stale-result'
    assert task.state == 'cancelled'


def test_background_task_cancels_result_queued_for_dispatch():
    queued = []
    outcomes = []

    def dispatcher(callback, outcome):
        queued.append((callback, outcome))

    task = controller.BackgroundTask(
        lambda _token: 'queued-result', outcomes.append,
        dispatcher=dispatcher).start()
    assert task.wait(1)
    assert task.state == 'finished'
    assert task.cancel()

    callback, outcome = queued.pop()
    callback(outcome)
    assert outcomes[0].cancelled is True
    assert outcomes[0].result == 'queued-result'
    assert task.state == 'cancelled'


def test_cancellable_command_runner_kills_process_group():
    token = controller.CancellationToken()
    runner = controller.CancellableCommandRunner(
        token=token, cancel_grace=0.1)
    errors = []

    def run():
        try:
            runner.run([
                sys.executable, '-c',
                'import signal,time; '
                'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                'time.sleep(30)',
            ])
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.time() + 2
    while runner.process is None and time.time() < deadline:
        time.sleep(0.01)
    assert runner.process is not None
    time.sleep(0.2)
    token.cancel()
    thread.join(3)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], controller.TaskCancelled)
    assert runner.process is None


def test_cancellable_runner_kills_child_after_group_leader_exits(tmp_path):
    token = controller.CancellationToken()
    runner = controller.CancellableCommandRunner(
        token=token, cancel_grace=0.1, maximum_output_bytes=4096)
    pid_path = tmp_path / 'child.pid'
    errors = []
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

    def run():
        try:
            runner.run([
                sys.executable, '-c', leader_code, str(pid_path), child_code])
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.time() + 3
        while (not pid_path.exists() or runner.process is None or
               runner.process.poll() is None) and time.time() < deadline:
            time.sleep(0.01)
        assert pid_path.exists()
        child_pid = int(pid_path.read_text(encoding='utf-8'))
        assert runner.process is not None
        assert runner.process.poll() == 0

        token.cancel()
        thread.join(3)
        assert not thread.is_alive()
        assert errors and isinstance(errors[0], controller.TaskCancelled)
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail('pipe-holding process-group child survived cancellation')
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


def test_output_frame_decoder_streams_cr_and_bounds_partial_data():
    decoder = controller.OutputFrameDecoder(maximum_buffer=1024)
    assert decoder.feed(b'I: one\rI: two\rI: three\n') == [
        b'I: one\r', b'I: two\r', b'I: three\n']
    assert decoder.feed(b'line\r\nnext\n') == [b'line\r\n', b'next\n']

    frames = decoder.feed(b'x' * 3000)
    assert [len(frame) for frame in frames] == [1024, 1024]
    assert decoder.buffered_bytes == 952
    assert decoder.flush() == [b'x' * 952]


def test_collision_detection_covers_basename_target_and_case(tmp_path):
    source = tmp_path / 'source'
    modules = (
        _module(source / 'modules' / 'addon.sb', 'modules/addon.sb'),
        _module(source / 'modules' / 'Case.sb', 'modules/Case.sb'),
    )
    info = backend.SourceInfo(
        backend.SOURCE_SUPPORTED, backend='livekit',
        root_path=str(tmp_path), source_path=str(source),
        fingerprint='{}:{}'.format(
            backend.SOURCE_FINGERPRINT_ALGORITHM, 'c' * 64),
        modules=modules)
    first = tmp_path / 'addon.sb'
    second = tmp_path / 'case.sb'
    first.write_bytes(b'one')
    second.write_bytes(b'two')

    collisions = controller.detect_module_collisions(
        info, ('addon.sb', 'Case.sb'), (str(first), str(second)))
    codes = set(item['code'] for item in collisions)

    assert 'module_basename_collision' in codes
    assert 'duplicate_module_target' in codes
    assert 'module_basename_case_collision' in codes
    assert 'module_target_case_collision' in codes
    assert str(first) in controller.collision_paths(collisions)


def _fake_plan(tmp_path):
    output = tmp_path / 'release.iso'
    job = tmp_path / '.minios-image-builder-test'
    job.mkdir(mode=0o700)
    os.chmod(str(job), 0o700)
    (job / 'image.partial.iso').write_bytes(b'partial')
    (job / 'compose-manifest.json').write_text('{}', encoding='utf-8')
    file_stat = os.lstat(str(job))
    flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY'):
        flags |= os.O_DIRECTORY
    descriptor = os.open(str(job), flags)
    output_directory_descriptor = os.open(str(tmp_path), flags)
    output_directory_stat = os.fstat(output_directory_descriptor)
    return SimpleNamespace(
        output_path=str(output), scratch_directory=str(tmp_path),
        job_directory=str(job),
        partial_output_path=str(job / 'image.partial.iso'),
        adapter_manifest_path=str(job / 'compose-manifest.json'),
        _job_identity=(int(file_stat.st_dev), int(file_stat.st_ino)),
        _job_descriptor=descriptor,
        _output_directory_identity=(int(output_directory_stat.st_dev),
                                    int(output_directory_stat.st_ino)),
        _output_directory_descriptor=output_directory_descriptor)


def test_safe_cleanup_removes_only_identity_checked_plan_job(tmp_path):
    plan = _fake_plan(tmp_path)
    result = controller.cleanup_plan_job(plan)

    assert result.cleaned
    assert result.warning is None
    assert not os.path.exists(plan.job_directory)
    assert plan._job_descriptor is None
    assert plan._output_directory_descriptor is None


def test_safe_cleanup_refuses_replaced_or_unrelated_directory(tmp_path):
    replaced = _fake_plan(tmp_path)
    old_identity = replaced._job_identity
    for name in os.listdir(replaced.job_directory):
        os.unlink(os.path.join(replaced.job_directory, name))
    os.rmdir(replaced.job_directory)
    os.mkdir(replaced.job_directory, 0o700)
    replaced._job_identity = old_identity

    result = controller.cleanup_plan_job(replaced)
    assert not result.cleaned
    assert os.path.isdir(replaced.job_directory)
    os.close(replaced._job_descriptor)
    replaced._job_descriptor = None
    os.close(replaced._output_directory_descriptor)
    replaced._output_directory_descriptor = None

    outside = tmp_path / 'ordinary-directory'
    outside.mkdir()
    outside_stat = os.lstat(str(outside))
    unrelated = SimpleNamespace(
        output_path=str(tmp_path / 'release.iso'),
        scratch_directory=str(tmp_path), job_directory=str(outside),
        _job_identity=(outside_stat.st_dev, outside_stat.st_ino))
    result = controller.cleanup_plan_job(unrelated)
    assert not result.cleaned
    assert outside.is_dir()


def test_boot_menu_state_owns_default_and_restores_source(tmp_path):
    state = controller.ProjectState(
        str(tmp_path / 'custom.iso'), project_base=str(tmp_path))
    state.default_boot = 'fresh'
    entries = [
        {
            'id': 'ram', 'base_mode': 'toram', 'enabled': True,
            'default': True, 'title': None, 'kernel_args': 'toram=trim',
        },
        {
            'id': 'safe', 'base_mode': 'fresh', 'enabled': True,
            'default': False, 'title': 'Safe graphics', 'kernel_args': 'nomodeset',
        },
    ]
    assert state.set_boot_menu_entries(entries)
    assert state.default_boot is None
    assert state.boot_menu_entries[0]['default'] is True
    with pytest.raises(ValueError, match='own default'):
        state.set_default_boot('resume')
    assert state.set_boot_menu_entries(None)
    assert state.boot_menu_entries is None
    assert state.default_boot is None
