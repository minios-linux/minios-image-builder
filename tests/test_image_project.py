import errno
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import zlib
from types import SimpleNamespace

import pytest

import image_project as backend


def _write(path, data=b'x'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _fake_module(path, payload=b'module'):
    return _write(path, b'fake-squashfs-' + payload)


def _make_source(tmp_path, backend_name='livekit', category='data',
                 module_names=None, kernel_version='6.12.1',
                 initramfs_version=None, release_arch=None):
    if module_names is None:
        module_names = ('00-core-amd64.sb', '01-kernel-amd64.sb',
                        '04-xfce-desktop-amd64.sb')
    if initramfs_version is None:
        initramfs_version = kernel_version
    root = tmp_path / '{}-root'.format(backend_name)
    source = root / category / 'minios'
    boot = source / 'boot'
    _write(boot / 'vmlinuz-{}'.format(kernel_version), b'kernel-data')
    _write(boot / 'initrfs-{}.img'.format(initramfs_version), b'initramfs-data')
    _write(boot / 'syslinux' / 'isolinux.bin', b'bios-image')
    _write(boot / 'syslinux' / 'isohdpfx.bin', b'hybrid-mbr')
    _write(
        boot / 'syslinux' / 'syslinux.cfg',
        b'TIMEOUT 100\nDEFAULT live\nLABEL live\n'
        + ('KERNEL /minios/boot/vmlinuz-{}\n'.format(
            kernel_version)).encode('ascii') + b'APPEND boot=live quiet\n')
    _write(boot / 'grub' / 'i386-pc' / 'marker', b'grub-bios')
    _write(boot / 'grub' / 'i386-pc' / 'eltorito.img', b'eltorito')
    _write(boot / 'grub' / 'i386-pc' / 'boot_hybrid.img', b'grub-mbr')
    _write(boot / 'grub' / 'efi.img', b'dual-architecture-efi-image')
    grub_menu = (
        b'set timeout=10\nset default=0\n'
        b'menuentry "Fresh" --class live {\n'
        + ('  linux /minios/boot/vmlinuz-{} boot=live quiet\n'.format(
            kernel_version)).encode('ascii') + b'}\n')
    _write(boot / 'grub' / 'grub.cfg', grub_menu)
    _write(boot / 'grub' / 'grub.multilang.cfg', grub_menu)
    for index, name in enumerate(module_names):
        _fake_module(source / name, str(index).encode('ascii'))
    mounts = tmp_path / '{}-mounts'.format(backend_name)
    mounts.write_text('', encoding='utf-8')
    sys_block = tmp_path / '{}-sys'.format(backend_name)
    sys_block.mkdir()
    release = tmp_path / '{}-release'.format(backend_name)
    content = 'VERSION="5.2.0"\nEDITION=standard\n'
    if release_arch:
        content += 'ARCH={}\n'.format(release_arch)
    release.write_text(content, encoding='utf-8')
    info = backend.discover_running_source(
        roots=((backend_name, str(root)),), mounts_path=str(mounts),
        sys_block_root=str(sys_block), runtime_release_path=str(release))
    return root, source, mounts, sys_block, release, info


def _config(tmp_path, content='LIVE_USER=live\n'):
    return _write(tmp_path / 'current-config.conf', content.encode('utf-8'))


def _png(path, width=32, height=24):
    def chunk(kind, payload):
        body = kind + payload
        return (struct.pack('>I', len(payload)) + body +
                struct.pack('>I', zlib.crc32(body) & 0xffffffff))

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    rows = b''.join(b'\0' + b'\0' * (width * 3)
                    for unused_row in range(height))
    return _write(
        path, b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) +
        chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def _inventory(entries=None, source_fingerprint=None,
               union_backend='overlayfs'):
    if entries is None:
        entries = [
            {
                'path': 'etc/example.conf',
                'type': 'regular',
                'category': 'software',
                'sensitive': False,
                'default_exact': True,
                'default_clean': True,
                'size': 4096,
            },
            {
                'path': 'home/live/private.txt',
                'type': 'regular',
                'category': 'user-data',
                'sensitive': True,
                'default_exact': True,
                'default_clean': False,
                'size': 1024,
            },
        ]
    return backend.parse_session_inventory({
        'product_kind': backend.SESSION_INVENTORY_KIND,
        'schema_version': backend.SESSION_INVENTORY_SCHEMA_VERSION,
        'source_fingerprint': source_fingerprint or '2' * 64,
        'union_backend': union_backend,
        'entries': entries,
    })


def _large_disk(_path):
    return (10 ** 12, 0, 10 ** 12)


def _tool_capabilities(**_ignored_option_overrides):
    result = {
        'tools': {
            name: {
                'available': True,
                'path': '/tools/{}'.format(name),
                'version': '{} test-version'.format(name),
            }
            for name in backend.REQUIRED_TOOL_NAMES
        },
        'capture_privilege': {
            'requested': True, 'euid': 0, 'available': True, 'pkexec': None,
        },
    }
    result['tools'][backend.COMPOSE_BACKEND_NAME] = {
        'available': True,
        'path': '/tools/{}'.format(backend.COMPOSE_BACKEND_NAME),
    }
    result['tools']['savechanges'] = {
        'available': True,
        'path': '/usr/bin/savechanges',
        'version': 'savechanges test-version',
    }
    result['tools']['mksquashfs'] = {
        'available': True,
        'path': '/tools/mksquashfs',
        'version': 'mksquashfs test-version',
    }
    return result


class AcceptSquashfsRunner(object):
    def __init__(self, reject=None):
        self.reject = reject
        self.calls = []

    def __call__(self, argv, input_data=None):
        self.calls.append((list(argv), input_data))
        if '-s' in argv:
            if self.reject and self.reject in argv[-1]:
                return 1, '', 'invalid squashfs'
            return 0, 'Found a valid SQUASHFS superblock', ''
        return 0, '', ''


def _project(info, output, project_base, **kwargs):
    return backend.ImageProject.from_source(
        info, str(output), project_base=str(project_base), **kwargs)


def _plan(project, info, config, runner=None, **kwargs):
    return backend.create_build_plan(
        project, info, current_config_path=str(config),
        disk_usage_func=_large_disk,
        scratch_directory=str(project.project_base),
        tool_capabilities=_tool_capabilities(),
        command_runner=runner or AcceptSquashfsRunner(), **kwargs)


def _error_codes(result):
    return set(item.code for item in result.errors)


def _warning_codes(result):
    return set(item.code for item in result.warnings)


def _prepare_artifact(plan, payload=b'fake-iso'):
    _write_path(plan.partial_output_path, payload)
    backend.atomic_write_json(plan.adapter_manifest_path, plan.manifest)


def _write_path(path, data):
    with open(path, 'wb') as handle:
        handle.write(data)


def _expected_iso_paths(plan):
    expected = plan.manifest['expected_iso']
    paths = set()
    paths.update(expected['module_targets'])
    paths.add(expected['config_target'])
    paths.add(expected['build_manifest_target'])
    paths.update(expected['required_boot_targets'])
    paths.update(expected['kernel_targets'])
    paths.update(expected['initramfs_targets'])
    paths.update(expected['menu_targets'])
    capture = expected['session_capture']
    if capture['requested']:
        paths.add(capture['report_target'])
        paths.add(capture['module_target'])
    customization = expected['image_customization']
    if customization['adapter_report_requested']:
        paths.add(customization['report_target'])
    paths.update(customization['boot_config_targets'])
    paths.update(customization['background_targets'])
    if customization['overlay_requested']:
        paths.add(customization['overlay_target'])
    return tuple(sorted('/' + path.lstrip('/') for path in paths))


class FakeXorriso(object):
    def __init__(self, plan, omit=(), extra=(), boot=True,
                  volume_label=None, returncode=0, capture_module=None,
                  capture_report=None, customization_report=None,
                  customization_overlay_module=None, build_manifest=None):
        self.plan = plan
        self.omit = set(omit)
        self.extra = tuple(extra)
        self.boot = boot
        self.volume_label = volume_label or plan.manifest['volume_label']
        self.returncode = returncode
        self.calls = []
        self.capture_module = capture_module or b'fake-captured-squashfs'
        self.build_manifest = (
            plan.manifest_payload if build_manifest is None
            else build_manifest)
        inventory = plan._session_inventory
        capture = plan.manifest['expected_iso']['session_capture']
        self.capture_report = capture_report
        if capture['requested'] and capture_report is None:
            self.capture_report = {
                'product_kind': backend.SESSION_CAPTURE_REPORT_KIND,
                'schema_version':
                    backend.SESSION_CAPTURE_REPORT_SCHEMA_VERSION,
                'profile': plan.manifest['capture']['mode'],
                'union_backend': (inventory.union_backend
                                  if inventory else 'overlayfs'),
                'source_fingerprint': (inventory.source_fingerprint
                                       if inventory else '1' * 64),
                'boot_id': '11111111-2222-3333-4444-555555555555',
                'base_module_fingerprint': plan.manifest['capture'][
                    'expected_base_module_fingerprint'],
                'module_order': capture['module_order'],
                'module': {
                    'target': capture['module_target'],
                    'size': len(self.capture_module),
                    'sha256': hashlib.sha256(
                        self.capture_module).hexdigest(),
                },
                'selection_sha256': plan.manifest['capture']['selection'][
                    'sha256'],
            }
        customization = plan.manifest['customization']
        expected_customization = plan.manifest['expected_iso'][
            'image_customization']
        self.custom_boot_files = backend._thaw(plan._boot_config_payloads)
        self.custom_background = None
        for frozen in plan._input_records:
            record = backend._thaw(frozen)
            if record['kind'] == 'boot-background':
                with open(record['path'], 'rb') as handle:
                    self.custom_background = handle.read()
                break
        self.customization_overlay_module = (
            customization_overlay_module or b'fake-overlay-squashfs')
        self.customization_report = customization_report
        if (customization['adapter_report_requested'] and
                customization_report is None):
            background = customization['boot']['background']
            report_background = None
            if background is not None:
                report_background = {
                    key: background[key]
                    for key in ('width', 'height', 'size', 'sha256')
                }
                report_background['targets'] = expected_customization[
                    'background_targets']
            report_overlay = None
            if customization['overlay']['requested']:
                report_overlay = {
                    'target': customization['overlay']['module_target'],
                    'module_order': customization['overlay']['module_order'],
                    'size': len(self.customization_overlay_module),
                    'sha256': hashlib.sha256(
                        self.customization_overlay_module).hexdigest(),
                    'input_tree_fingerprint': customization['overlay'][
                        'input_tree_fingerprint'],
                    'entry_count': customization['overlay']['entry_count'],
                }
            self.customization_report = {
                'product_kind': backend.IMAGE_CUSTOMIZATION_REPORT_KIND,
                'schema_version':
                    backend.IMAGE_CUSTOMIZATION_REPORT_SCHEMA_VERSION,
                'boot': {
                    'timeout_seconds': customization['boot'][
                        'timeout_seconds'],
                    'default_boot': customization['boot']['default_boot'],
                    'kernel_args': customization['boot']['kernel_args'],
                    'configs': [{
                        'target': target,
                        'size': len(self.custom_boot_files[target]),
                        'sha256': hashlib.sha256(
                            self.custom_boot_files[target]).hexdigest(),
                    } for target in expected_customization[
                        'boot_config_targets']],
                    'background': report_background,
                },
                'overlay': report_overlay,
            }

    def __call__(self, argv, input_data=None):
        self.calls.append(list(argv))
        if '-s' in argv:
            return 0, 'Found a valid SQUASHFS superblock', ''
        if ('-d' in argv and argv and
                os.path.basename(argv[0]) == 'unsquashfs'):
            destination = argv[argv.index('-d') + 1]
            shutil.copytree(
                self.plan._overlay_directory, destination, symlinks=True)
            return 0, '', ''
        if '-extract' in argv:
            if self.returncode != 0:
                return self.returncode, '', 'extraction failed'
            index = 0
            while index < len(argv):
                if argv[index] != '-extract':
                    index += 1
                    continue
                source_path = argv[index + 1]
                destination = argv[index + 2]
                if source_path == '/minios/session-capture.json':
                    if isinstance(self.capture_report, bytes):
                        payload = self.capture_report
                    else:
                        payload = (json.dumps(
                            self.capture_report, sort_keys=True,
                            separators=(',', ':')) + '\n').encode('utf-8')
                elif source_path == '/minios/image-customization.json':
                    if isinstance(self.customization_report, bytes):
                        payload = self.customization_report
                    else:
                        payload = (json.dumps(
                            self.customization_report, sort_keys=True,
                            separators=(',', ':')) + '\n').encode('utf-8')
                elif source_path == '/minios/build-manifest.json':
                    payload = self.build_manifest
                elif source_path == '/minios/config.conf':
                    payload = self.plan._live_config_payload or b'configuration\n'
                elif source_path.lstrip('/') in self.custom_boot_files:
                    payload = self.custom_boot_files[source_path.lstrip('/')]
                elif source_path.lstrip('/') in self.plan.manifest[
                        'expected_iso']['image_customization'][
                            'background_targets']:
                    payload = self.custom_background
                elif source_path.lstrip('/') == self.plan.manifest[
                        'expected_iso']['image_customization'][
                            'overlay_target']:
                    payload = self.customization_overlay_module
                else:
                    payload = self.capture_module
                _write_path(destination, payload)
                index += 3
            return 0, '', ''
        paths = [path for path in _expected_iso_paths(self.plan)
                 if path not in self.omit]
        paths.extend(self.extra)
        if '-report_el_torito' in argv:
            if not self.boot:
                return self.returncode, 'No El Torito information', ''
            report = '\n'.join((
                'El Torito boot img :   1  BIOS  y   none  0x0000  0x00  4  40',
                'El Torito img path :   1  /minios/boot/syslinux/isolinux.bin',
                'El Torito boot img :   2  UEFI  y   none  0x0000  0x00  4  41',
                'El Torito img path :   2  /minios/boot/grub/efi.img',
            ))
            return self.returncode, report, ''
        if '-pvd_info' in argv:
            return self.returncode, "Volume id    : '{}'".format(
                self.volume_label), ''
        if 'report_lba' in argv:
            lines = [
                "File data lba: 0 , 33 , 1 , 12 , '{}'".format(path)
                for path in paths
            ]
            return self.returncode, '\n'.join(lines), ''
        if '-type' in argv and argv[argv.index('-type') + 1] == 'l':
            symlinks = set(
                '/minios/' + item['relative_path']
                for item in self.plan.manifest['input_digests']['source_files']
                if item['type'] == 'symlink')
            return self.returncode, '\n'.join(
                path for path in paths if path in symlinks), ''
        return self.returncode, '\n'.join(paths), ''


def test_discovery_hashes_contents_and_inspects_livekit_modules(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)

    assert info.status == backend.SOURCE_SUPPORTED
    assert info.backend == 'livekit'
    assert info.fingerprint.startswith('effective-content-sha256-v2:')
    assert info.metadata['version'] == '5.2.0'
    assert info.metadata['kernel_version_coherent'] is True
    assert [item.basename for item in info.modules] == [
        '00-core-amd64.sb', '01-kernel-amd64.sb',
        '04-xfce-desktop-amd64.sb',
    ]
    assert all(len(item.sha256) == 64 for item in info.modules)
    assert all(item.active is None for item in info.modules)
    assert all(len(item['sha256']) == 64 for item in info.input_manifest)
    json.dumps(info.to_dict())


@pytest.mark.parametrize('backend_name,category', [
    ('dracut', 'medium'), ('dracut', 'iso'), ('livekit', 'iso'),
])
def test_discovery_supports_both_roots_and_all_source_categories(
        tmp_path, backend_name, category):
    root, source, mounts, sys_block, release, info = _make_source(
        tmp_path, backend_name=backend_name, category=category)
    assert info.supported
    assert info.backend == backend_name
    assert info.media_category == category
    assert info.source_path == str(source)


def test_initramfs_implementation_is_detected_from_marker(tmp_path):
    # dracut-mos and livekit-mos both mount at the same place; the real
    # implementation is read from the marker directory beside the mount root.
    (tmp_path / 'dracut-mos').mkdir()
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    assert info.backend == 'dracut'


def test_architecture_falls_back_to_module_architecture(tmp_path):
    # The VM's minios-release omits ARCH; the source architecture must still be
    # reported from the numbered modules instead of "Unknown".
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    assert 'ARCH=' not in release.read_text(encoding='utf-8')
    assert info.metadata['architecture'] == 'amd64'


def test_release_architecture_takes_precedence_over_modules(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(
        tmp_path, release_arch='arm64')
    assert info.metadata['architecture'] == 'arm64'


def test_discovery_reports_unsupported_and_error_distinctly(
        tmp_path, monkeypatch):
    unsupported = backend.discover_running_source(
        roots=(('livekit', str(tmp_path / 'missing')),),
        mounts_path=str(tmp_path / 'mounts'))
    assert unsupported.status == backend.SOURCE_UNSUPPORTED

    denied = tmp_path / 'denied'
    denied.mkdir()
    real_lstat = backend.os.lstat

    def denied_lstat(path, *args, **kwargs):
        if os.path.normpath(str(path)) == os.path.normpath(str(denied)):
            raise OSError(errno.EACCES, 'denied')
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(backend.os, 'lstat', denied_lstat)
    failed = backend.discover_running_source(
        roots=(('livekit', str(denied)),),
        mounts_path=str(tmp_path / 'mounts'))
    assert failed.status == backend.SOURCE_ERROR
    assert failed.diagnostics[0].code == 'live_root_unreadable'


def test_discovery_ignores_private_replaced_source_config(tmp_path):
    root, source, mounts, sys_block, release, first = _make_source(tmp_path)
    config = _write(source / 'config.conf', b'PRIVATE=value\n')
    config.chmod(0)

    info = backend.discover_running_source(
        roots=(('livekit', str(root)),), mounts_path=str(mounts),
        sys_block_root=str(sys_block), runtime_release_path=str(release))

    assert info.supported
    assert 'config.conf' not in {
        item['relative_path'] for item in info.input_manifest}
    assert info.fingerprint == first.fingerprint


def test_authorized_config_payload_is_staged_without_reopening_path(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    output = tmp_path / 'project' / 'release.iso'
    output.parent.mkdir()
    missing = tmp_path / 'unreadable-config.conf'
    payload = b'LIVE_USERNAME=live\n'
    project = _project(info, output, output.parent)

    plan = _plan(
        project, info, missing, current_config_payload=payload)

    assert plan.buildable
    config_index = plan.argv.index('--config') + 1
    assert plan.argv[config_index] == 'live-config.conf'
    assert plan.manifest['input_digests']['config']['sha256'] == hashlib.sha256(
        payload).hexdigest()
    backend.prepare_build_command(plan)
    staged = os.path.join(plan.job_directory, 'live-config.conf')
    assert open(staged, 'rb').read() == payload
    assert stat.S_IMODE(os.stat(staged).st_mode) == 0o600


def test_fingerprint_detects_same_size_same_mtime_content_mutation(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    module = source / '04-xfce-desktop-amd64.sb'
    original_stat = module.stat()
    original = module.read_bytes()
    changed = bytes((byte + 1) % 256 for byte in original)
    module.write_bytes(changed)
    os.utime(str(module), ns=(original_stat.st_atime_ns,
                              original_stat.st_mtime_ns))

    current = backend.source_tree_fingerprint(str(source))

    assert current != info.fingerprint
    output = tmp_path / 'project' / 'release.iso'
    output.parent.mkdir()
    project = _project(info, output, output.parent)
    plan = _plan(project, info, _config(tmp_path))
    assert not plan.buildable
    assert 'source_drift' in _error_codes(plan)


def test_recursive_modules_safe_symlinks_and_active_source_mapping(tmp_path):
    root, source, mounts, sys_block, release, first = _make_source(tmp_path)
    nested = _fake_module(source / 'modules' / 'apps' / '07-extra-amd64.sb')
    alias = source / 'modules' / 'aliases' / '07-extra-alias.sb'
    alias.parent.mkdir(parents=True)
    os.symlink('../apps/07-extra-amd64.sb', str(alias))
    backing = sys_block / 'loop7' / 'loop' / 'backing_file'
    _write(backing, str(nested).encode('utf-8') + b'\n')
    mounts.write_text(
        '/dev/loop7 /run/initramfs/memory/bundles/07-extra.sb '
        'squashfs ro 0 0\n', encoding='utf-8')

    info = backend.discover_running_source(
        roots=(('livekit', str(root)),), mounts_path=str(mounts),
        sys_block_root=str(sys_block), runtime_release_path=str(release))

    names = set(item.relative_path for item in info.modules)
    assert 'modules/apps/07-extra-amd64.sb' in names
    assert 'modules/aliases/07-extra-alias.sb' in names
    active = [item for item in info.modules if item.active]
    assert set(item.real_path for item in active) == {os.path.realpath(str(nested))}
    assert any(item.is_symlink for item in info.modules)
    assert 'module_real_path_alias' in set(
        item.code for item in info.diagnostics)


def test_active_external_basename_collision_is_reported(tmp_path):
    root, source, mounts, sys_block, release, first = _make_source(tmp_path)
    external = _fake_module(
        tmp_path / 'external' / '04-xfce-desktop-amd64.sb')
    backing = sys_block / 'loop8' / 'loop' / 'backing_file'
    _write(backing, str(external).encode('utf-8') + b'\n')
    mounts.write_text(
        '/dev/loop8 /run/initramfs/memory/bundles/external.sb '
        'squashfs ro 0 0\n', encoding='utf-8')
    info = backend.discover_running_source(
        roots=(('livekit', str(root)),), mounts_path=str(mounts),
        sys_block_root=str(sys_block), runtime_release_path=str(release))
    assert 'runtime_source_basename_collision' in set(
        item.code for item in info.diagnostics)


def test_unsafe_or_dangling_source_symlink_is_an_inspection_error(tmp_path):
    root, source, mounts, sys_block, release, first = _make_source(tmp_path)
    link = source / 'modules' / 'escape.sb'
    link.parent.mkdir()
    os.symlink('/etc/passwd', str(link))

    info = backend.discover_running_source(
        roots=(('livekit', str(root)),), mounts_path=str(mounts),
        sys_block_root=str(sys_block), runtime_release_path=str(release))

    assert info.status == backend.SOURCE_ERROR
    assert 'Unsafe or dangling source symlink' in info.diagnostics[-1].message


def test_symlink_cannot_bypass_adapter_excluded_source_tree(tmp_path):
    root, source, mounts, sys_block, release, first = _make_source(tmp_path)
    hidden = _write(source / 'changes' / 'hidden', b'hidden')
    link = source / 'visible-link'
    os.symlink('changes/hidden', str(link))
    info = backend.discover_running_source(
        roots=(('livekit', str(root)),), mounts_path=str(mounts),
        sys_block_root=str(sys_block), runtime_release_path=str(release))
    assert info.status == backend.SOURCE_ERROR


def test_project_round_trip_serializes_paths_relative_and_is_deeply_immutable(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    additional = _fake_module(project_dir / 'inputs' / 'addon.sb')
    output = project_dir / 'release' / 'custom.iso'
    output.parent.mkdir(parents=True)
    project_path = project_dir / 'image-project.json'
    project = _project(
        info, output, project_dir,
        additional_module_paths=('inputs/addon.sb',),
        volume_label='MINIOS LAB', notes='release',
        sensitive_config_acknowledged=True, overwrite_output=True)

    project.save(str(project_path))
    payload = json.loads(project_path.read_text(encoding='utf-8'))
    loaded = backend.ImageProject.load(str(project_path))

    assert not os.path.isabs(payload['source']['root_path'])
    assert not os.path.isabs(payload['source']['tree_path'])
    assert payload['additional_module_paths'] == ['inputs/addon.sb']
    assert payload['output_path'] == 'release/custom.iso'
    assert loaded.additional_module_paths == (str(additional),)
    assert loaded.output_path == str(output)
    assert loaded.sensitive_config_acknowledged is True
    with pytest.raises(AttributeError):
        del loaded.output_path
    with pytest.raises(TypeError):
        info.metadata['new'] = 'value'
    with pytest.raises(TypeError):
        info.input_manifest[0]['sha256'] = 'changed'


def test_customization_project_round_trip_and_legacy_defaults(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    background = _png(project_dir / 'art' / 'background.png')
    overlay = project_dir / 'overlay'
    _write(overlay / 'etc' / 'example.conf', b'configured\n')
    project = _project(
        info, project_dir / 'custom.iso', project_dir,
        live_config_overrides={
            'LIVE_HOSTNAME': 'image-host',
            'LIVE_USER_FULLNAME': "MiniOS O'Brien",
        },
        boot_timeout=7, default_boot='fresh', kernel_args='audit=1',
        boot_background_path='art/background.png',
        overlay_directory='overlay')
    project_path = project_dir / 'customization.json'

    project.save(str(project_path))
    payload = json.loads(project_path.read_text(encoding='utf-8'))
    loaded = backend.load_image_project(str(project_path))

    assert payload['boot_background_path'] == 'art/background.png'
    assert payload['overlay_directory'] == 'overlay'
    assert loaded.live_config_overrides['LIVE_HOSTNAME'] == 'image-host'
    assert loaded.boot_timeout == 7
    assert loaded.default_boot == 'fresh'
    assert loaded.kernel_args == 'audit=1'
    assert loaded.boot_background_path == str(background)
    assert loaded.overlay_directory == str(overlay)
    assert loaded.customization_requested
    with pytest.raises(TypeError):
        loaded.live_config_overrides['LIVE_HOSTNAME'] = 'changed'

    legacy = project.to_dict(project_dir)
    for key in (
            'live_config_overrides', 'boot_timeout', 'default_boot',
            'kernel_args', 'boot_background_path', 'overlay_directory'):
        legacy.pop(key)
    legacy_path = project_dir / 'legacy-customization.json'
    legacy_path.write_text(json.dumps(legacy), encoding='utf-8')
    loaded_legacy = backend.load_image_project(str(legacy_path))
    assert not loaded_legacy.customization_requested
    assert loaded_legacy.live_config_overrides == {}


def test_live_config_kernel_and_target_set_validators_are_strict():
    source = b"LIVE_HOSTNAME='old'\nUNKNOWN='preserved'\n"
    rendered = backend.render_live_config(
        source,
        {'LIVE_HOSTNAME': 'new-host',
         'LIVE_USER_FULLNAME': "MiniOS O'Brien"})
    text = rendered.decode('utf-8')
    assert rendered.startswith(source)
    assert text.index("LIVE_HOSTNAME='old'") < text.index(
        '# MiniOS Image Builder overrides')
    assert "LIVE_HOSTNAME='new-host'" in text
    assert "LIVE_USER_FULLNAME='MiniOS O'\\''Brien'" in text
    assert "UNKNOWN='preserved'" in text
    assert backend.validate_live_config_overrides({
        'LIVE_SSH_PASSWORD_AUTHENTICATION': 'false'}) == {
            'LIVE_SSH_PASSWORD_AUTHENTICATION': 'false'}

    for overrides in (
            {'LIVE_USER_PASSWORD_CRYPTED': 'secret'},
            {'UNSUPPORTED': 'value'},
            {'LIVE_HOSTNAME': 'bad\nhost'}):
        with pytest.raises(ValueError):
            backend.validate_live_config_overrides(overrides)
    with pytest.raises(ValueError):
        backend.validate_kernel_arguments('name=$unsafe')
    count, digest = backend.validate_kernel_arguments('audit=1 quiet')
    assert count == len(b'audit=1 quiet')
    assert digest == hashlib.sha256(b'audit=1 quiet').hexdigest()

    targets = ('minios/boot/z.cfg', 'minios/boot/a.cfg')
    expected = hashlib.sha256()
    expected.update(b'minios-image-target-set-v1\0')
    for target in sorted(targets):
        expected.update(target.encode('utf-8') + b'\0')
    assert backend.customization_target_set_identity(targets) == (
        2, expected.hexdigest())


@pytest.mark.parametrize('source', [
    b"readonly LIVE_HOSTNAME='locked'\n",
    b"declare -r LIVE_HOSTNAME='locked'\n",
    b'unset LIVE_HOSTNAME\n',
    b'printf "%s" "$LIVE_HOSTNAME"\n',
    b"LIVE_HOSTNAME='old'; readonly LIVE_HOSTNAME\n",
    b'cat <<EOF\nLIVE_HOSTNAME=hidden\nEOF\n',
    b'prefix=continued\\\nLIVE_HOSTNAME=hidden\n',
])
def test_live_config_rejects_ambiguous_override_key_shell_syntax(source):
    with pytest.raises(ValueError, match='shell|here-document'):
        backend.render_live_config(
            source, {'LIVE_HOSTNAME': 'safe-host'})


def test_live_config_append_block_preserves_unrelated_bytes():
    source = b"# retained\r\nUNKNOWN='same'"
    rendered = backend.render_live_config(
        source, {'LIVE_HOSTNAME': 'safe-host'})
    assert rendered.startswith(source + b'\n')
    assert rendered.endswith(
        b"LIVE_HOSTNAME='safe-host'\n"
        b'# End MiniOS Image Builder overrides\n')


def test_project_atomic_write_preserves_old_file_on_replace_failure(
        tmp_path, monkeypatch):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    path = project_dir / 'image-project.json'
    path.write_text('old', encoding='utf-8')
    project = _project(info, project_dir / 'out.iso', project_dir)

    def fail_replace(_source, _target):
        raise OSError(errno.EIO, 'replace failed')

    monkeypatch.setattr(backend.os, 'replace', fail_replace)
    with pytest.raises(backend.ImageProjectError):
        project.save(str(path))
    assert path.read_text(encoding='utf-8') == 'old'
    assert list(project_dir.glob('.image-project.json.*.tmp')) == []


@pytest.mark.parametrize('mutator,exception', [
    (lambda data: data.update(schema_version=2),
     backend.UnsupportedSchemaError),
    (lambda data: data.update(password='secret'),
     backend.ProjectFormatError),
    (lambda data: data.update(capture_mode='source-build'),
     backend.ProjectFormatError),
])
def test_project_rejects_unknown_or_malformed_schema(
        tmp_path, mutator, exception):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    payload = project.to_dict(project_dir)
    mutator(payload)
    path = project_dir / 'bad.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(exception):
        backend.load_image_project(str(path))


def test_selected_capture_project_round_trip_and_legacy_custom_load(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    selected = _project(
        info, project_dir / 'selected.iso', project_dir,
        capture_mode='selected',
        capture_include_paths=('opt/example',),
        capture_exclude_paths=('opt/example/cache',),
        capture_compression='gzip')
    selected_path = project_dir / 'selected.json'

    selected.save(str(selected_path))
    loaded = backend.load_image_project(str(selected_path))

    assert loaded.capture_mode == 'selected'
    assert loaded.capture_include_paths == ('opt/example',)
    assert loaded.capture_exclude_paths == ('opt/example/cache',)
    assert loaded.capture_compression == 'gzip'

    legacy_payload = _project(
        info, project_dir / 'legacy.iso', project_dir).to_dict(project_dir)
    for key in (
            'capture_include_paths', 'capture_exclude_paths',
            'capture_compression', 'sensitive_capture_acknowledged'):
        legacy_payload.pop(key)
    legacy_path = project_dir / 'legacy.json'
    legacy_path.write_text(json.dumps(legacy_payload), encoding='utf-8')
    legacy = backend.load_image_project(str(legacy_path))
    assert legacy.capture_mode == backend.NO_SESSION_CAPTURE
    assert legacy.capture_include_paths == ()
    assert legacy.capture_compression == 'zstd'


@pytest.mark.parametrize('include,exclude', [
    (('/absolute',), ()),
    (('../escape',), ()),
    (('opt//app',), ()),
    (('opt/app',), ('opt/app',)),
])
def test_capture_selection_paths_are_strictly_normalized(
        tmp_path, include, exclude):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    with pytest.raises(ValueError):
        _project(
            info, project_dir / 'out.iso', project_dir,
            capture_mode='selected', capture_include_paths=include,
            capture_exclude_paths=exclude)


def test_session_inventory_parser_loader_and_cleanup_are_strict(tmp_path):
    inventory = _inventory()
    assert len(inventory.entries) == 2
    assert inventory.entries[1].sensitive is True
    assert len(inventory.document_sha256) == 64
    with pytest.raises(AttributeError):
        inventory.entries[0].size = 1

    path = tmp_path / 'inventory.json'
    path.write_text(json.dumps(inventory.to_dict()), encoding='utf-8')
    os.chmod(str(path), 0o600)
    loaded = backend.load_session_inventory(str(path), cleanup=True)
    assert loaded.source_fingerprint == inventory.source_fingerprint
    assert not path.exists()

    duplicate = (
        b'{"product_kind":"minios-session-inventory",'
        b'"product_kind":"minios-session-inventory",'
        b'"schema_version":2,"source_fingerprint":"' + b'1' * 64 +
        b'","union_backend":"overlayfs","entries":[]}')
    with pytest.raises(backend.ProjectFormatError, match='duplicate JSON field'):
        backend.parse_session_inventory(duplicate)

    unsafe = inventory.to_dict()
    unsafe['entries'][0]['path'] = '../escape'
    with pytest.raises(backend.ProjectFormatError):
        backend.parse_session_inventory(unsafe)

    contradictory = inventory.to_dict()
    contradictory['entries'][1]['default_exact'] = False
    contradictory['entries'][1]['default_clean'] = True
    with pytest.raises(backend.ProjectFormatError, match='clean inventory'):
        backend.parse_session_inventory(contradictory)


def test_inventory_loader_rejects_mode_symlink_and_identity_change(tmp_path):
    payload = json.dumps(_inventory().to_dict())
    loose = tmp_path / 'loose.json'
    loose.write_text(payload, encoding='utf-8')
    os.chmod(str(loose), 0o644)
    with pytest.raises(backend.ProjectFormatError, match='0600'):
        backend.load_session_inventory(str(loose))

    target = tmp_path / 'target.json'
    target.write_text(payload, encoding='utf-8')
    os.chmod(str(target), 0o600)
    link = tmp_path / 'link.json'
    os.symlink(str(target), str(link))
    with pytest.raises(backend.ProjectFormatError, match='regular file'):
        backend.load_session_inventory(str(link))
    with pytest.raises(backend.ImageProjectError, match='unsafe'):
        backend.cleanup_session_inventory(str(link))
    with pytest.raises(backend.ImageProjectError, match='identity changed'):
        backend.cleanup_session_inventory(str(target), (0, 0))
    assert target.exists()


def test_inventory_command_has_narrow_privilege_boundary(tmp_path):
    output = tmp_path / 'inventory.json'
    changes = tmp_path / 'changes'
    changes.mkdir()
    trusted = {
        '/usr/bin/savechanges': '/usr/bin/savechanges',
        'savechanges': '/usr/bin/savechanges',
        '/usr/bin/pkexec': '/usr/bin/pkexec',
        'pkexec': '/usr/bin/pkexec',
    }
    resolver = lambda name: trusted.get(name)

    root_command = backend.build_session_inventory_command(
        str(output), str(changes), euid=0, resolver=resolver)
    user_command = backend.build_session_inventory_command(
        str(output), str(changes), euid=1000, resolver=resolver)

    assert root_command == (
        '/usr/bin/savechanges', '--inventory-json', str(output),
        str(changes))
    assert user_command == ('/usr/bin/pkexec',) + root_command
    assert 'minios-image-compose' not in ' '.join(user_command)
    with pytest.raises(backend.ImageProjectError, match='trusted executable'):
        backend.build_session_inventory_command(
            str(output), euid=0,
            resolver=lambda _name: '/opt/untrusted/savechanges')
    with pytest.raises(ValueError, match='euid'):
        backend.build_session_inventory_command(
            str(output), euid=False, resolver=resolver)


def test_inventory_command_adds_cancel_file_before_changes_directory(tmp_path):
    private = tmp_path / 'inventory-private'
    private.mkdir()
    os.chmod(str(private), 0o700)
    output = private / 'inventory.json'
    cancel_file = private / 'inventory.cancel'
    changes = tmp_path / 'changes'
    changes.mkdir()
    trusted = {
        '/usr/bin/savechanges': '/usr/bin/savechanges',
        'savechanges': '/usr/bin/savechanges',
        '/usr/bin/pkexec': '/usr/bin/pkexec',
        'pkexec': '/usr/bin/pkexec',
    }
    resolver = lambda name: trusted.get(name)

    command = backend.build_session_inventory_command(
        str(output), str(changes), euid=1000, resolver=resolver,
        cancel_file=str(cancel_file))

    assert command == (
        '/usr/bin/pkexec', '/usr/bin/savechanges', '--inventory-json',
        str(output), '--cancel-file', str(cancel_file), str(changes))
    assert not cancel_file.exists()


def test_inventory_cancel_command_rejects_insecure_or_alternate_paths(
        tmp_path):
    private = tmp_path / 'private'
    alternate = tmp_path / 'alternate'
    private.mkdir()
    alternate.mkdir()
    os.chmod(str(private), 0o700)
    os.chmod(str(alternate), 0o700)
    output = private / 'inventory.json'
    cancel_file = private / 'cancel'
    resolver = lambda name: (
        '/usr/bin/savechanges'
        if name in ('/usr/bin/savechanges', 'savechanges') else None)

    with pytest.raises(backend.ImageProjectError, match='share'):
        backend.build_session_inventory_command(
            str(output), euid=0, resolver=resolver,
            cancel_file=str(alternate / 'cancel'))
    with pytest.raises(ValueError, match='normalized'):
        backend.build_session_inventory_command(
            str(output), euid=0, resolver=resolver,
            cancel_file=str(private / '..' / 'alternate' / 'cancel'))
    with pytest.raises(ValueError, match='line break'):
        backend.build_session_inventory_command(
            str(output), euid=0, resolver=resolver,
            cancel_file=str(cancel_file) + '\n')

    cancel_file.write_bytes(b'')
    os.chmod(str(cancel_file), 0o600)
    with pytest.raises(backend.ImageProjectError, match='already exists'):
        backend.build_session_inventory_command(
            str(output), euid=0, resolver=resolver,
            cancel_file=str(cancel_file))
    cancel_file.unlink()

    victim = tmp_path / 'victim'
    victim.write_bytes(b'unchanged')
    os.symlink(str(victim), str(cancel_file))
    with pytest.raises(backend.ImageProjectError, match='already exists'):
        backend.build_session_inventory_command(
            str(output), euid=0, resolver=resolver,
            cancel_file=str(cancel_file))
    assert victim.read_bytes() == b'unchanged'
    cancel_file.unlink()

    os.chmod(str(private), 0o750)
    with pytest.raises(backend.ImageProjectError, match='mode 0700'):
        backend.build_session_inventory_command(
            str(output), euid=0, resolver=resolver,
            cancel_file=str(cancel_file))


def test_request_inventory_cancel_creates_durable_idempotent_marker(tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    os.chmod(str(private), 0o700)
    cancel_file = private / 'cancel'
    parent_stat = os.lstat(str(private))
    parent_identity = (parent_stat.st_dev, parent_stat.st_ino)

    previous_umask = os.umask(0o777)
    try:
        assert backend.request_session_inventory_cancel(
            str(cancel_file), parent_identity) is True
    finally:
        os.umask(previous_umask)
    marker_stat = os.lstat(str(cancel_file))
    assert stat.S_ISREG(marker_stat.st_mode)
    assert stat.S_IMODE(marker_stat.st_mode) == 0o600
    assert marker_stat.st_uid == os.geteuid()
    assert marker_stat.st_size == 0

    assert backend.request_session_inventory_cancel(
        str(cancel_file), parent_identity) is True
    repeated_stat = os.lstat(str(cancel_file))
    assert (repeated_stat.st_dev, repeated_stat.st_ino) == (
        marker_stat.st_dev, marker_stat.st_ino)


def test_request_inventory_cancel_rejects_unsafe_existing_markers(
        tmp_path, monkeypatch):
    private = tmp_path / 'private'
    private.mkdir()
    os.chmod(str(private), 0o700)
    cancel_file = private / 'cancel'
    victim = tmp_path / 'victim'
    victim.write_bytes(b'keep-me')
    os.chmod(str(victim), 0o600)

    os.symlink(str(victim), str(cancel_file))
    with pytest.raises(backend.ImageProjectError, match='regular file'):
        backend.request_session_inventory_cancel(str(cancel_file))
    assert victim.read_bytes() == b'keep-me'
    cancel_file.unlink()

    cancel_file.mkdir()
    with pytest.raises(backend.ImageProjectError, match='regular file'):
        backend.request_session_inventory_cancel(str(cancel_file))
    cancel_file.rmdir()

    cancel_file.write_bytes(b'')
    os.chmod(str(cancel_file), 0o640)
    with pytest.raises(backend.ImageProjectError, match='0600'):
        backend.request_session_inventory_cancel(str(cancel_file))
    os.chmod(str(cancel_file), 0o600)

    marker_stat = os.lstat(str(cancel_file))
    marker_identity = (marker_stat.st_dev, marker_stat.st_ino)
    real_fstat = backend.os.fstat

    def wrong_owner_fstat(descriptor):
        metadata = real_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == marker_identity:
            values = list(metadata)
            values[4] = metadata.st_uid + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(backend.os, 'fstat', wrong_owner_fstat)
    with pytest.raises(backend.ImageProjectError, match='current-user-owned'):
        backend.request_session_inventory_cancel(str(cancel_file))


def test_request_inventory_cancel_rejects_parent_identity_mode_and_owner(
        tmp_path, monkeypatch):
    private = tmp_path / 'private'
    private.mkdir()
    os.chmod(str(private), 0o700)
    cancel_file = private / 'cancel'
    parent_stat = os.lstat(str(private))
    parent_identity = (parent_stat.st_dev, parent_stat.st_ino)

    with pytest.raises(backend.ImageProjectError, match='identity'):
        backend.request_session_inventory_cancel(
            str(cancel_file), (parent_stat.st_dev, parent_stat.st_ino + 1))
    assert not cancel_file.exists()

    os.chmod(str(private), 0o750)
    with pytest.raises(backend.ImageProjectError, match='mode 0700'):
        backend.request_session_inventory_cancel(
            str(cancel_file), parent_identity)
    os.chmod(str(private), 0o700)

    real_fstat = backend.os.fstat

    def wrong_owner_fstat(descriptor):
        metadata = real_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == parent_identity:
            values = list(metadata)
            values[4] = metadata.st_uid + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(backend.os, 'fstat', wrong_owner_fstat)
    with pytest.raises(backend.ImageProjectError, match='current-user-owned'):
        backend.request_session_inventory_cancel(
            str(cancel_file), parent_identity)
    assert not cancel_file.exists()


def test_request_inventory_cancel_rejects_path_and_ancestor_races(
        tmp_path, monkeypatch):
    ancestor = tmp_path / 'raced-ancestor'
    private = ancestor / 'private'
    private.mkdir(parents=True)
    os.chmod(str(private), 0o700)
    other = tmp_path / 'other'
    other.mkdir()
    os.chmod(str(other), 0o700)

    with pytest.raises(ValueError, match='normalized'):
        backend.request_session_inventory_cancel(
            str(private / '..' / '..' / 'other' / 'cancel'))

    linked_parent = tmp_path / 'linked-parent'
    os.symlink(str(private), str(linked_parent))
    with pytest.raises(backend.ImageProjectError):
        backend.request_session_inventory_cancel(
            str(linked_parent / 'cancel'))
    assert not (private / 'cancel').exists()

    attacker = tmp_path / 'attacker'
    attacker_private = attacker / 'private'
    attacker_private.mkdir(parents=True)
    os.chmod(str(attacker_private), 0o700)
    moved = tmp_path / 'raced-ancestor-original'
    real_open = backend.os.open
    raced = []

    def racing_open(path, flags, mode=0o777, dir_fd=None):
        if (path == ancestor.name and dir_fd is not None and not raced):
            os.rename(str(ancestor), str(moved))
            os.symlink(str(attacker), str(ancestor))
            raced.append(True)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(backend.os, 'open', racing_open)
    with pytest.raises(backend.ImageProjectError):
        backend.request_session_inventory_cancel(
            str(private / 'raced-cancel'))
    assert raced
    assert not (attacker_private / 'raced-cancel').exists()
    assert not (moved / 'private' / 'raced-cancel').exists()


def test_request_inventory_cancel_rejects_replaced_expected_parent(tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    os.chmod(str(private), 0o700)
    parent_stat = os.lstat(str(private))
    parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
    moved = tmp_path / 'private-original'
    os.rename(str(private), str(moved))
    private.mkdir()
    os.chmod(str(private), 0o700)

    with pytest.raises(backend.ImageProjectError, match='identity'):
        backend.request_session_inventory_cancel(
            str(private / 'cancel'), parent_identity)
    assert not (private / 'cancel').exists()
    assert not (moved / 'cancel').exists()


def test_tool_probe_reports_external_versions_and_fixed_backend():
    paths = dict((name, '/tools/{}'.format(name))
                 for name in backend.REQUIRED_TOOL_NAMES)
    paths[backend.COMPOSE_BACKEND_PATH] = '/opt/minios-image-compose'

    def resolver(name):
        return paths.get(name)

    def runner(argv):
        version = '1.3.0' if os.path.basename(argv[0]) == 'savechanges' else '1.2.3'
        return 0, '{} {}'.format(os.path.basename(argv[0]), version), ''

    result = backend.probe_required_tools(resolver=resolver, runner=runner)

    assert all(result['tools'][name]['available']
               for name in backend.REQUIRED_TOOL_NAMES)
    assert result['tools']['xorriso']['version'].endswith('1.2.3')
    # The same-source backend is resolved at its fixed path and is never
    # version, option, or basename probed.
    backend_entry = result['tools'][backend.COMPOSE_BACKEND_NAME]
    assert backend_entry == {
        'available': True, 'path': '/opt/minios-image-compose'}
    assert 'compose_capabilities' not in result
    assert 'compose_contract' not in result


def test_missing_fixed_backend_is_reported_as_unavailable():
    paths = dict((name, '/tools/{}'.format(name))
                 for name in backend.REQUIRED_TOOL_NAMES)

    result = backend.probe_required_tools(
        resolver=lambda name: paths.get(name),
        runner=lambda argv: (0, 'x 1.0', ''))

    assert result['tools'][backend.COMPOSE_BACKEND_NAME] == {
        'available': False, 'path': None}


def test_capture_tool_probe_is_conditional_and_checks_privilege():
    paths = dict((name, '/tools/{}'.format(name))
                 for name in backend.REQUIRED_TOOL_NAMES)
    paths.update({
        backend.COMPOSE_BACKEND_PATH: backend.COMPOSE_BACKEND_PATH,
        '/usr/bin/savechanges': '/usr/bin/savechanges',
        '/usr/bin/pkexec': '/usr/bin/pkexec',
    })

    def resolver(name):
        return paths.get(name)

    def runner(argv):
        return 0, '{} 1.2.3'.format(os.path.basename(argv[0])), ''

    custom = backend.probe_required_tools(
        resolver=resolver, runner=runner, capture_requested=False,
        euid=1000)
    capture = backend.probe_required_tools(
        resolver=resolver, runner=runner, capture_requested=True,
        euid=1000)

    assert 'savechanges' not in custom['tools']
    assert 'savechanges' in capture['tools']
    assert capture['capture_privilege']['pkexec'] == '/usr/bin/pkexec'
    assert capture['capture_privilege']['available'] is True

    def old_runner(argv):
        return 0, '{} 1.2.9'.format(os.path.basename(argv[0])), ''

    old_capture = backend.probe_required_tools(
        resolver=resolver, runner=old_runner, capture_requested=True,
        euid=1000)
    assert old_capture['tools']['savechanges']['available'] is False


def test_backend_options_are_not_capability_gated(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    config = _config(project_dir)
    tools = _tool_capabilities()
    ordinary = _project(info, project_dir / 'ordinary.iso', project_dir)
    clean = _project(
        info, project_dir / 'clean.iso', project_dir, capture_mode='clean')
    selected = _project(
        info, project_dir / 'selected.iso', project_dir,
        capture_mode='selected', capture_include_paths=('etc',))
    labelled = _project(
        info, project_dir / 'labelled.iso', project_dir,
        volume_label='MINIOS TEST')

    ordinary_plan = backend.create_build_plan(
        ordinary, info, current_config_path=str(config),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=tools, command_runner=AcceptSquashfsRunner())
    clean_plan = backend.create_build_plan(
        clean, info, current_config_path=str(config),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=tools, command_runner=AcceptSquashfsRunner())
    selected_plan = backend.create_build_plan(
        selected, info, current_config_path=str(config),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=tools, command_runner=AcceptSquashfsRunner(),
        session_inventory=_inventory(entries=[{
            'path': 'etc/example', 'type': 'regular',
            'category': 'software', 'sensitive': False,
            'default_exact': True, 'default_clean': True, 'size': 1,
        }]))
    labelled_plan = backend.create_build_plan(
        labelled, info, current_config_path=str(config),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=tools, command_runner=AcceptSquashfsRunner())

    # The same-source backend's advertised options are not probed, so every
    # option combination is buildable once the fixed backend resolves.
    assert ordinary_plan.buildable
    assert clean_plan.buildable
    assert selected_plan.buildable
    assert labelled_plan.buildable


def test_overlay_requires_mksquashfs(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    overlay = project_dir / 'overlay'
    _write(overlay / 'value', b'value')
    config = _config(project_dir)
    tools = _tool_capabilities()
    tools['tools']['mksquashfs']['available'] = False
    overlay_project = _project(
        info, project_dir / 'overlay.iso', project_dir,
        overlay_directory=str(overlay))
    missing_mks = backend.create_build_plan(
        overlay_project, info, current_config_path=str(config),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=tools, command_runner=AcceptSquashfsRunner())
    assert 'overlay_mksquashfs_unavailable' in _error_codes(missing_mks)


def test_missing_external_tool_or_backend_blocks_preflight(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    config = _config(project_dir)
    project = _project(info, project_dir / 'out.iso', project_dir)

    missing_tool = _tool_capabilities()
    missing_tool['tools']['xorriso']['available'] = False
    tool_plan = backend.create_build_plan(
        project, info, current_config_path=str(config),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=missing_tool, command_runner=AcceptSquashfsRunner())
    assert not tool_plan.buildable
    assert 'required_tool_missing' in _error_codes(tool_plan)

    missing_backend = _tool_capabilities()
    missing_backend['tools'][backend.COMPOSE_BACKEND_NAME] = {
        'available': False, 'path': None}
    backend_plan = backend.create_build_plan(
        project, info, current_config_path=str(config),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=missing_backend,
        command_runner=AcceptSquashfsRunner())
    assert not backend_plan.buildable
    assert 'compose_backend_missing' in _error_codes(backend_plan)



def test_missing_capture_contract_capabilities_block_only_capture(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')
    tools = _tool_capabilities()
    tools['tools']['savechanges']['available'] = False
    tools['capture_privilege']['available'] = False

    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(tmp_path)),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=tools, command_runner=AcceptSquashfsRunner())

    assert not plan.buildable
    codes = _error_codes(plan)
    assert 'capture_savechanges_unavailable' in codes
    assert 'capture_privilege_unavailable' in codes


def test_build_plan_uses_secure_job_and_explicit_companion_contract(tmp_path):
    names = ('00-core-amd64.sb', '01-kernel-amd64.sb',
             '02-firmware-amd64.sb')
    root, source, mounts, sys_block, release, info = _make_source(
        tmp_path, module_names=names)
    project_dir = tmp_path / 'project'
    output_dir = project_dir / 'release'
    output_dir.mkdir(parents=True)
    additional = _fake_module(project_dir / 'inputs' / 'addon.sb')
    config = _config(project_dir)
    project = backend.ImageProject(
        project_base=str(project_dir), source_backend=info.backend,
        source_root_path=info.root_path, source_path=info.source_path,
        source_fingerprint=info.fingerprint,
        selected_source_modules=('00-core-amd64.sb', '01-kernel-amd64.sb'),
        additional_module_paths=('inputs/addon.sb',),
        output_path='release/custom.iso', capture_mode='custom',
        volume_label='MINIOS LAB')

    plan = _plan(project, info, config)

    assert plan.buildable
    assert os.path.dirname(plan.job_directory) == str(output_dir)
    assert stat.S_IMODE(os.lstat(plan.job_directory).st_mode) == 0o700
    assert plan.partial_output_path.startswith(plan.job_directory + os.sep)
    assert not os.path.exists(plan.partial_output_path)
    assert not os.path.exists(plan.adapter_manifest_path)
    argv = list(plan.argv)
    assert argv[argv.index('--source') + 1] == str(source)
    assert argv[argv.index('--config') + 1] == str(config)
    assert argv[argv.index('--name') + 1] == 'image.partial.iso'
    assert argv[argv.index('--manifest') + 1] == 'compose-manifest.json'
    assert plan.execution_cwd.startswith('/proc/self/fd/')
    assert backend._identity(os.stat(plan.execution_cwd)) == plan._job_identity
    assert argv[argv.index('--volume-label') + 1] == 'MINIOS LAB'
    assert argv[-1] == str(additional)
    assert '--exclude' in argv
    assert plan.manifest['input_digests']['config']['sha256'] == hashlib.sha256(
        config.read_bytes()).hexdigest()
    assert plan.manifest['composition']['additional_modules'][0]['sha256']
    assert backend.revalidate_build_plan_inputs(plan) == ()
    backend.atomic_write_json(plan.adapter_manifest_path, plan.manifest)
    assert backend.prepare_build_command(plan) == plan.argv


def test_customization_plan_materializes_private_config_and_combines_order(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    config_secret = 'existing-config-secret'
    override_secret = 'private-full-name'
    kernel_text = 'audit=1 customization_marker=private'
    config = _config(
        project_dir,
        'LIVE_USER=live\nWIFI_PASSWORD={}\n'.format(config_secret))
    background = _png(project_dir / 'background.png', 64, 48)
    overlay = project_dir / 'overlay'
    overlay_file = _write(
        overlay / 'etc' / 'customized.conf', b'customized=true\n')
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        live_config_overrides={'LIVE_USER_FULLNAME': override_secret},
        boot_timeout=7, default_boot='fresh', kernel_args=kernel_text,
        boot_background_path=str(background),
        overlay_directory=str(overlay), capture_mode='clean',
        sensitive_config_acknowledged=True)

    plan = _plan(project, info, config)

    assert plan.buildable
    assert plan.customization_requested
    customization = plan.manifest['customization']
    assert customization['boot']['kernel_args'] == {
        'bytes': len(kernel_text.encode('utf-8')),
        'sha256': hashlib.sha256(kernel_text.encode('utf-8')).hexdigest(),
    }
    assert customization['overlay']['module_order'] == 5
    assert customization['overlay']['module_target'] == (
        'minios/05-image-overlay.sb')
    assert plan.manifest['capture']['expected_module_order'] == 6
    assert plan.manifest['capture']['expected_module_target'] == (
        'minios/06-session-changes.sb')
    transformed_boot = backend._thaw(plan._boot_config_payloads)
    assert b'set timeout=7' in transformed_boot[
        'minios/boot/grub/grub.cfg']
    assert b'set default=0' in transformed_boot[
        'minios/boot/grub/grub.cfg']
    assert b'TIMEOUT 70' in transformed_boot[
        'minios/boot/syslinux/syslinux.cfg']
    assert b'DEFAULT live' in transformed_boot[
        'minios/boot/syslinux/syslinux.cfg']
    assert all(kernel_text.encode('utf-8') in payload
               for payload in transformed_boot.values())
    argv = list(plan.argv)
    assert argv[argv.index('--config') + 1] == 'live-config.conf'
    private_config = plan._live_config_path
    assert private_config != str(config)
    assert private_config != plan.adapter_manifest_path
    assert not os.path.exists(private_config)
    assert not os.path.exists(plan.adapter_manifest_path)
    assert argv[argv.index('--boot-background') + 1] == str(background)
    assert argv[argv.index('--overlay-directory') + 1] == str(overlay)

    public_plan = json.dumps(plan.manifest, sort_keys=True)
    assert config_secret not in public_plan
    assert override_secret not in public_plan
    assert kernel_text not in public_plan
    assert str(background) not in public_plan
    assert str(overlay) not in public_plan
    assert str(source) not in public_plan
    assert str(config) not in public_plan
    assert '<redacted-kernel-arguments>' in public_plan
    display = ' '.join(plan.display_argv)
    assert kernel_text not in display
    assert str(source) not in display
    assert str(config) not in display
    assert str(background) not in display
    assert str(overlay) not in display
    assert '<redacted-kernel-arguments>' in display

    assert backend.prepare_build_command(plan) == plan.argv
    materialized = os.lstat(private_config)
    assert stat.S_IMODE(materialized.st_mode) == 0o600
    with open(private_config, 'r', encoding='utf-8') as handle:
        private_text = handle.read()
    assert config_secret in private_text
    assert override_secret in private_text
    assert not os.path.exists(plan.adapter_manifest_path)
    assert overlay_file.read_bytes() == b'customized=true\n'


def test_build_plan_display_and_manifest_redact_private_inputs(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    config = _config(project_dir, 'LIVE_USER=private-user\n')
    additional = _fake_module(project_dir / 'private-addon.sb')
    background = _png(project_dir / 'private-background.png')
    overlay = project_dir / 'private-overlay'
    _write(overlay / 'etc' / 'value', b'value')
    kernel_args = 'audit=1 private_kernel_marker=1'
    entry_args = 'private_entry_marker=1 nomodeset'
    boot_menu = [
        {
            'id': 'normal', 'base_mode': 'fresh', 'enabled': True,
            'default': True, 'title': None, 'kernel_args': '',
        },
        {
            'id': 'safe', 'base_mode': 'fresh', 'enabled': True,
            'default': False, 'title': None,
            'kernel_args': entry_args,
        },
    ]
    override = 'private-full-name'
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        additional_module_paths=(str(additional),),
        live_config_overrides={'LIVE_USER_FULLNAME': override},
        kernel_args=kernel_args, boot_menu_entries=boot_menu,
        boot_background_path=str(background),
        overlay_directory=str(overlay))

    plan = _plan(project, info, config)
    public = json.dumps(plan.manifest, sort_keys=True)
    display = ' '.join(plan.display_argv)

    assert plan.buildable
    for private_value in (
            str(source), str(config), str(additional), str(background),
            str(overlay), kernel_args, entry_args, override):
        assert private_value not in public
        assert private_value not in display
    assert kernel_args in plan.argv
    boot_menu_json = plan.argv[plan.argv.index('--boot-menu-json') + 1]
    assert entry_args in boot_menu_json
    assert '\n' not in boot_menu_json and '\r' not in boot_menu_json
    assert '<redacted-boot-menu-json>' in plan.display_argv
    menu_summary = plan.manifest['customization']['boot']['menu_entries']
    assert menu_summary[1]['kernel_args'] == {
        'bytes': len(entry_args.encode('utf-8')),
        'sha256': hashlib.sha256(entry_args.encode('utf-8')).hexdigest(),
    }
    assert str(source) in plan.argv
    assert str(additional) in plan.argv
    assert '<redacted-kernel-arguments>' in plan.display_argv
    assert '<running-minios-source>' in plan.display_argv
    assert '<additional-module-input>' in plan.display_argv


def test_private_materialization_uses_retained_job_descriptor_on_path_race(
        tmp_path, monkeypatch):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        live_config_overrides={'LIVE_HOSTNAME': 'descriptor-bound'})
    plan = _plan(project, info, _config(project_dir))
    moved = plan.job_directory + '.original'
    attacker = project_dir / 'attacker-job'
    attacker.mkdir()
    os.chmod(str(attacker), 0o700)
    real_replace = backend.os.replace
    raced = []

    def racing_replace(source_name, target_name, *args, **kwargs):
        if (target_name == 'live-config.conf' and
                kwargs.get('src_dir_fd') is not None and not raced):
            os.rename(plan.job_directory, moved)
            os.symlink(str(attacker), plan.job_directory)
            raced.append(True)
        return real_replace(source_name, target_name, *args, **kwargs)

    monkeypatch.setattr(backend.os, 'replace', racing_replace)
    with pytest.raises(backend.ImageProjectError, match='job directory'):
        backend.prepare_build_command(plan)

    assert raced
    assert os.path.isfile(os.path.join(moved, 'live-config.conf'))
    assert not (attacker / 'live-config.conf').exists()


def test_prepare_revalidates_inputs_after_private_materialization(
        tmp_path, monkeypatch):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    config = _config(project_dir, 'LIVE_USER=before\n')
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        live_config_overrides={'LIVE_HOSTNAME': 'post-check'})
    plan = _plan(project, info, config)
    real_materialize = backend._materialize_live_config

    def mutating_materialize(materialized_plan):
        identity = real_materialize(materialized_plan)
        config.write_bytes(b'LIVE_USER=after!\n')
        return identity

    monkeypatch.setattr(
        backend, '_materialize_live_config', mutating_materialize)
    with pytest.raises(
            backend.ImageProjectError, match='during private materialization'):
        backend.prepare_build_command(plan)


def test_execution_cwd_remains_bound_after_job_path_replacement(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        live_config_overrides={'LIVE_HOSTNAME': 'descriptor-cwd'})
    plan = _plan(project, info, _config(project_dir))
    backend.prepare_build_command(plan)
    with open(plan._live_config_path, 'rb') as handle:
        expected = handle.read()
    moved = plan.job_directory + '.original'
    os.rename(plan.job_directory, moved)
    os.mkdir(plan.job_directory, 0o700)
    _write_path(
        os.path.join(plan.job_directory, 'live-config.conf'), b'attacker\n')

    result = subprocess.run(
        ['/bin/cat', 'live-config.conf'], cwd=plan.execution_cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    assert result.returncode == 0
    assert result.stdout == expected
    assert result.stdout != b'attacker\n'


def test_customization_revalidation_detects_overlay_and_background_changes(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    background = _png(project_dir / 'background.png')
    overlay = project_dir / 'overlay'
    overlay_file = _write(overlay / 'value', b'before')
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        boot_background_path=str(background),
        overlay_directory=str(overlay))
    plan = _plan(project, info, _config(project_dir))
    assert plan.buildable

    overlay_file.write_bytes(b'after')
    diagnostics = backend.revalidate_build_plan_inputs(plan)
    assert 'overlay_input_changed' in set(item.code for item in diagnostics)

    second_project = _project(
        info, project_dir / 'second.iso', project_dir,
        boot_background_path=str(background))
    second = _plan(second_project, info, _config(project_dir))
    background.write_bytes(background.read_bytes()[:-1] + b'x')
    diagnostics = backend.revalidate_build_plan_inputs(second)
    assert 'build_input_changed' in set(item.code for item in diagnostics)


def test_overlay_inventory_rejects_descriptor_traversal_race(
        tmp_path, monkeypatch):
    overlay = tmp_path / 'overlay'
    victim = overlay / 'victim'
    _write(victim / 'trusted', b'trusted')
    attacker = tmp_path / 'attacker'
    _write(attacker / 'untrusted', b'untrusted')
    moved = overlay / 'victim-original'
    real_open = backend.os.open
    raced = []

    def racing_open(path, flags, mode=0o777, dir_fd=None):
        if path == 'victim' and dir_fd is not None and not raced:
            os.rename(str(victim), str(moved))
            os.symlink(str(attacker), str(victim))
            raced.append(True)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(backend.os, 'open', racing_open)
    with pytest.raises((OSError, backend.ImageProjectError)):
        backend.inspect_overlay_directory(str(overlay))

    assert raced
    assert (attacker / 'untrusted').read_bytes() == b'untrusted'


def test_create_project_overlay_directory_creates_private_noncolliding_child(
        tmp_path):
    assert hasattr(backend, 'create_project_overlay_directory')
    assert 'create_project_overlay_directory' in backend.__all__
    parent = tmp_path / 'project'
    parent.mkdir()

    first = backend.create_project_overlay_directory(str(parent))
    assert os.path.dirname(first) == os.path.realpath(str(parent))
    assert os.path.basename(first) == 'image-overlay'
    first_stat = os.lstat(first)
    assert stat.S_ISDIR(first_stat.st_mode)
    assert not stat.S_ISLNK(first_stat.st_mode)
    assert stat.S_IMODE(first_stat.st_mode) == 0o700
    if hasattr(os, 'geteuid'):
        assert first_stat.st_uid == os.geteuid()

    # A repeat never reuses or deletes the existing directory.
    second = backend.create_project_overlay_directory(str(parent))
    assert second != first
    assert os.path.basename(second) == 'image-overlay-1'
    assert os.path.isdir(first)

    # A freshly created empty overlay is representable, not a crash.
    summary = backend.inspect_overlay_directory(first)
    assert summary['entry_count'] == 1
    assert summary['regular_bytes'] == 0

    # A custom name refuses to reuse a pre-existing plain directory.
    os.mkdir(str(parent / 'reserved'))
    reserved = backend.create_project_overlay_directory(
        str(parent), name='reserved')
    assert os.path.basename(reserved) == 'reserved-1'
    assert (parent / 'reserved').is_dir()


def test_create_project_overlay_directory_rejects_unsafe_parents(tmp_path):
    parent = tmp_path / 'project'
    parent.mkdir()

    with pytest.raises(ValueError):
        backend.create_project_overlay_directory('relative/project')
    with pytest.raises(ValueError):
        backend.create_project_overlay_directory(str(parent) + '\x00x')
    with pytest.raises(ValueError):
        backend.create_project_overlay_directory(str(parent) + '\nname')
    for bad in ('', '..', 'a/b', 'bad\nname'):
        with pytest.raises(ValueError):
            backend.create_project_overlay_directory(str(parent), name=bad)

    with pytest.raises((backend.ImageProjectError, OSError)):
        backend.create_project_overlay_directory(str(parent / 'missing'))

    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    os.symlink(str(real), str(link))
    with pytest.raises(backend.ImageProjectError):
        backend.create_project_overlay_directory(str(link))
    assert list(real.iterdir()) == []


def test_build_plan_accepts_helper_created_overlay(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    overlay_path = backend.create_project_overlay_directory(str(project_dir))
    _write_path(os.path.join(overlay_path, 'overlay.conf'), b'enabled=true\n')
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        overlay_directory=overlay_path)

    plan = _plan(project, info, _config(project_dir))

    assert plan.buildable
    argv = list(plan.argv)
    assert argv[argv.index('--overlay-directory') + 1] == overlay_path
    customization = plan.manifest['customization']
    assert customization['overlay']['module_target'] == (
        'minios/05-image-overlay.sb')

    _prepare_artifact(plan)
    result = backend.verify_iso(
        plan, runner=FakeXorriso(plan), xorriso='/tools/xorriso',
        unsquashfs='/tools/unsquashfs')
    assert result.structurally_verified
    assert result.customization_summary['overlay']['module_order'] == 5


def test_build_plan_warns_on_empty_helper_created_overlay(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    overlay_path = backend.create_project_overlay_directory(str(project_dir))
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        overlay_directory=overlay_path)

    plan = _plan(project, info, _config(project_dir))

    assert plan.buildable
    assert 'overlay_directory_empty' in _warning_codes(plan)


def test_selected_capture_materializes_private_digest_bound_intent(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    include_path = 'opt/private-application-name'
    exclude_path = include_path + '/cache'
    inventory = _inventory(entries=[{
        'path': include_path + '/settings.conf',
        'type': 'regular',
        'category': 'software',
        'sensitive': False,
        'default_exact': True,
        'default_clean': True,
        'size': 8192,
    }])
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        capture_mode='selected', capture_include_paths=(include_path,),
        capture_exclude_paths=(exclude_path,))

    plan = _plan(
        project, info, _config(project_dir),
        session_inventory=inventory)

    assert plan.buildable
    argv = list(plan.argv)
    assert argv[argv.index('--capture-selection') + 1] == (
        'session-selection.json')
    selection_path = plan._capture_selection_path
    assert not os.path.exists(selection_path)
    serialized_manifest = json.dumps(plan.manifest)
    assert include_path not in serialized_manifest
    assert exclude_path not in serialized_manifest
    assert plan.manifest['capture']['selection']['include_count'] == 1
    assert plan.manifest['capture']['inventory']['entry_count'] == 1

    assert backend.prepare_build_command(plan) == plan.argv
    selection_stat = os.lstat(selection_path)
    assert stat.S_IMODE(selection_stat.st_mode) == 0o600
    with open(selection_path, 'rb') as handle:
        selection_payload = handle.read()
    selection = json.loads(selection_payload.decode('utf-8'))
    assert selection['include_paths'] == [include_path]
    assert selection['exclude_paths'] == [exclude_path]
    assert hashlib.sha256(selection_payload).hexdigest() == plan.manifest[
        'capture']['selection']['sha256']
    assert backend.prepare_build_command(plan) == plan.argv

    _write_path(selection_path, b'tampered')
    os.chmod(selection_path, 0o600)
    with pytest.raises(backend.ImageProjectError, match='selection'):
        backend.prepare_build_command(plan)


def test_capture_order_is_dynamic_and_additional_module_is_not_base_binding(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    additional = _fake_module(project_dir / '100-addon.sb')
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        additional_module_paths=(str(additional),), capture_mode='clean')

    plan = _plan(project, info, _config(project_dir))

    assert plan.buildable
    capture = plan.manifest['expected_iso']['session_capture']
    assert capture['module_order'] == 101
    assert capture['module_target'] == 'minios/101-session-changes.sb'
    expected = hashlib.sha256()
    expected.update(b'minios-base-modules-v2\x00')
    for module in sorted(
            info.modules,
            key=lambda item: (int(re.match(r'^([0-9]+)', item.basename).group(1)),
                              item.basename), reverse=True):
        expected.update(module.basename.encode('ascii') + b'\x00')
        expected.update(str(module.size).encode('ascii') + b'\x00')
        expected.update(module.sha256.encode('ascii') + b'\x00')
    assert plan.manifest['capture'][
        'expected_base_module_fingerprint'] == expected.hexdigest()


def test_capture_base_binding_matches_compose_source_module_scope(tmp_path):
    root, source, mounts, sys_block, release, first = _make_source(tmp_path)
    _fake_module(source / 'modules' / 'deep' / '99-deep-addon.sb')
    info = backend.discover_running_source(
        roots=(('livekit', str(root)),), mounts_path=str(mounts),
        sys_block_root=str(sys_block), runtime_release_path=str(release))
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')

    plan = _plan(project, info, _config(project_dir))

    assert plan.buildable
    expected = hashlib.sha256()
    expected.update(b'minios-base-modules-v2\x00')
    effective = [
        module for module in info.modules
        if module.relative_path.endswith('.sb') and
        ('/' not in module.relative_path or
         module.relative_path.startswith('modules/'))]
    for module in sorted(
            effective,
            key=lambda item: (int(re.match(r'^([0-9]+)', item.basename).group(1)),
                              item.basename), reverse=True):
        expected.update(module.basename.encode('ascii') + b'\x00')
        expected.update(str(module.size).encode('ascii') + b'\x00')
        expected.update(module.sha256.encode('ascii') + b'\x00')
    assert plan.manifest['capture'][
        'expected_base_module_fingerprint'] == expected.hexdigest()
    assert plan.manifest['expected_iso']['session_capture'][
        'module_order'] == 100


def test_base_fingerprint_matches_sortmod_for_unnumbered_modules():
    modules = (
        SimpleNamespace(basename='addon-a.sb', size=1, sha256='a' * 64),
        SimpleNamespace(basename='01-core.sb', size=2, sha256='b' * 64),
        SimpleNamespace(basename='addon-z.sb', size=3, sha256='c' * 64),
    )
    expected = hashlib.sha256()
    expected.update(b'minios-base-modules-v2\x00')
    for module in (modules[1], modules[2], modules[0]):
        expected.update(module.basename.encode('ascii') + b'\x00')
        expected.update(str(module.size).encode('ascii') + b'\x00')
        expected.update(module.sha256.encode('ascii') + b'\x00')
    assert backend._base_module_fingerprint(modules) == expected.hexdigest()


def test_capture_inventory_drives_estimate_and_rejects_unmatched_selection(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    config = _config(project_dir)
    selected = _project(
        info, project_dir / 'selected.iso', project_dir,
        capture_mode='selected',
        capture_include_paths=('opt/not-in-inventory',))
    blocked = _plan(
        selected, info, config, session_inventory=_inventory())
    assert 'capture_selection_unmatched' in _error_codes(blocked)

    unknown_inventory = _inventory(entries=[{
        'path': 'opt/example/file',
        'type': 'regular',
        'category': 'software',
        'sensitive': False,
        'default_exact': True,
        'default_clean': True,
    }])
    clean = _project(
        info, project_dir / 'clean.iso', project_dir,
        capture_mode='clean')
    estimated = _plan(
        clean, info, config, session_inventory=unknown_inventory)
    assert estimated.buildable
    assert estimated.manifest['capture']['estimated_bytes'] is None
    assert 'capture_size_unknown' in _warning_codes(estimated)

    whiteout_inventory = _inventory(entries=[{
        'path': 'etc/.wh.deleted',
        'type': 'whiteout',
        'category': 'system-config',
        'sensitive': False,
        'default_exact': True,
        'default_clean': False,
    }], union_backend='aufs')
    deletion = _project(
        info, project_dir / 'deletion.iso', project_dir,
        capture_mode='selected',
        capture_include_paths=('etc/deleted/child',))
    deletion_plan = _plan(
        deletion, info, config, session_inventory=whiteout_inventory)
    assert deletion_plan.buildable
    assert deletion_plan.manifest['capture']['inventory'][
        'selected_entry_count'] == 1


def test_saved_session_artifacts_in_source_allow_further_customization(tmp_path):
    root, source, mounts, sys_block, release, first = _make_source(tmp_path)
    _write(source / 'session-capture.json', b'{}')
    _fake_module(source / '99-session-changes.sb')
    info = backend.discover_running_source(
        roots=(('livekit', str(root)),), mounts_path=str(mounts),
        sys_block_root=str(sys_block), runtime_release_path=str(release))
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)

    plan = _plan(project, info, _config(project_dir))

    assert plan.buildable
    assert 'source_session_capture_artifact' in _warning_codes(plan)
    assert 'reserved_session_capture_artifact' not in _error_codes(plan)

    capture_project = _project(
        info, project_dir / 'captured.iso', project_dir,
        capture_mode='clean')
    capture_plan = _plan(capture_project, info, _config(project_dir))
    assert capture_plan.buildable
    assert capture_plan.manifest['capture']['expected_module_target'] == (
        'minios/100-session-changes.sb')


def test_reserved_capture_name_in_added_module_is_blocked(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    (project_dir / 'inputs').mkdir(parents=True)
    _fake_module(project_dir / 'inputs' / '99-session-changes.sb')
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        additional_module_paths=('inputs/99-session-changes.sb',))

    plan = _plan(project, info, _config(project_dir))

    assert 'reserved_session_capture_artifact' in _error_codes(plan)


def test_prior_image_builder_customization_in_source_is_allowed(tmp_path):
    root, source, mounts, sys_block, release, first = _make_source(tmp_path)
    _write(source / 'image-customization.json', b'{}')
    _fake_module(source / '98-image-overlay.sb')
    info = backend.discover_running_source(
        roots=(('livekit', str(root)),), mounts_path=str(mounts),
        sys_block_root=str(sys_block), runtime_release_path=str(release))
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)

    plan = _plan(project, info, _config(project_dir))

    assert plan.buildable
    assert 'source_image_customization_artifact' in _warning_codes(plan)
    assert 'reserved_image_customization_artifact' not in _error_codes(plan)


def test_revalidation_detects_same_size_additional_module_replacement(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    (project_dir / 'release').mkdir(parents=True)
    additional = _fake_module(project_dir / 'inputs' / 'addon.sb', b'1234')
    project = _project(
        info, project_dir / 'release' / 'out.iso', project_dir,
        additional_module_paths=('inputs/addon.sb',))
    plan = _plan(project, info, _config(project_dir))
    before = additional.stat()
    additional.write_bytes(b'changed-content!!')
    replacement = additional.read_bytes()
    original_size = plan.manifest['composition']['additional_modules'][0]['size']
    if len(replacement) != original_size:
        additional.write_bytes(b'z' * original_size)
    os.utime(str(additional), ns=(before.st_atime_ns, before.st_mtime_ns))

    diagnostics = backend.revalidate_build_plan_inputs(plan)

    assert 'build_input_changed' in set(item.code for item in diagnostics)
    with pytest.raises(backend.ImageProjectError):
        backend.prepare_build_command(plan)


def test_revalidation_detects_new_source_path_not_in_original_manifest(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = _plan(project, info, _config(project_dir))
    _write(source / 'new-after-plan', b'unplanned')

    diagnostics = backend.revalidate_build_plan_inputs(plan)

    assert 'build_source_changed' in set(item.code for item in diagnostics)


def test_exact_capture_requires_sensitive_data_acknowledgement(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    with pytest.raises(ValueError, match='sensitive capture acknowledgement'):
        _project(
            info, project_dir / 'out.iso', project_dir,
            capture_mode='exact')


@pytest.mark.parametrize('mode,acknowledged', [
    ('exact', True), ('clean', False),
])
def test_session_capture_modes_use_unprivileged_compose_contract(
        tmp_path, mode, acknowledged):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode=mode,
        sensitive_capture_acknowledged=acknowledged,
        capture_compression='xz')

    plan = _plan(
        project, info, _config(project_dir),
        session_inventory=_inventory())

    assert plan.buildable
    assert plan.capture_requested
    argv = list(plan.argv)
    assert argv[0] == '/tools/minios-image-compose'
    assert '/usr/bin/pkexec' not in argv
    assert argv[argv.index('--capture-changes') + 1] == mode
    assert argv[argv.index('--capture-compression') + 1] == 'xz'
    assert '--capture-selection' not in argv
    capture = plan.manifest['expected_iso']['session_capture']
    assert capture['module_order'] == 5
    assert capture['module_target'] == 'minios/05-session-changes.sb'
    assert len(plan.manifest['capture'][
        'expected_base_module_fingerprint']) == 64
    if mode == 'clean':
        assert 'clean_capture_allowlist' in _warning_codes(plan)


def test_custom_mode_needs_no_capture_tools_or_privilege(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    tools = _tool_capabilities(**dict(
        (option, False) for option in backend.COMPOSE_CAPTURE_OPTIONS))
    tools['tools'].pop('savechanges')
    tools['capture_privilege']['available'] = False

    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=tools, command_runner=AcceptSquashfsRunner())

    assert plan.buildable
    assert not plan.capture_requested
    assert '--capture-changes' not in plan.argv


def test_include_current_config_false_is_blocked_but_volume_label_is_supported(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        include_current_config=False, volume_label='CUSTOM')
    plan = _plan(project, info, _config(project_dir))
    assert 'adapter_requires_current_config' in _error_codes(plan)
    assert 'volume_label_not_supported_by_adapter' not in _error_codes(plan)


def test_sensitive_plaintext_config_warns_without_blocking_or_leaking(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    config = _config(project_dir, 'WIFI_PASSWORD=correct-horse-battery\n')
    project = _project(info, project_dir / 'out.iso', project_dir)

    plan = _plan(project, info, config)
    serialized = json.dumps(plan.manifest)

    # A plaintext secret is surfaced as a non-blocking warning, never a blocker,
    # and its value is never recorded in the project or plan.
    assert 'sensitive_config_acknowledgement_required' not in _error_codes(plan)
    assert 'sensitive_config_present' in _warning_codes(plan)
    assert plan.buildable
    assert 'correct-horse-battery' not in serialized


def test_standard_crypted_passwords_are_not_flagged_as_sensitive(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    # The stock live configuration ships already-hashed password digests; these
    # must not block the build nor even warn.
    config = _config(
        project_dir,
        'LIVE_USER_PASSWORD_CRYPTED=$6$abc$def\n'
        'LIVE_ROOT_PASSWORD_CRYPTED=$6$ghi$jkl\n')
    project = _project(info, project_dir / 'out.iso', project_dir)

    plan = _plan(project, info, config)

    assert 'sensitive_config_acknowledgement_required' not in _error_codes(plan)
    assert 'sensitive_config_present' not in _warning_codes(plan)
    assert plan.buildable


def test_posix_character_class_advanced_regex_is_unchanged_and_evaluated(
        tmp_path):
    if shutil.which('grep') is None:
        pytest.skip('grep is unavailable')
    root, source, mounts, sys_block, release, first = _make_source(tmp_path)
    _write(source / 'docs' / '12-release.txt', b'release notes')
    info = backend.discover_running_source(
        roots=(('livekit', str(root)),), mounts_path=str(mounts),
        sys_block_root=str(sys_block), runtime_release_path=str(release))
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    pattern = r'^docs/[[:digit:]]+-release[.]txt$'
    project = _project(
        info, project_dir / 'out.iso', project_dir, exclusions=(pattern,))

    plan = _plan(project, info, _config(project_dir), grep=shutil.which('grep'))

    assert plan.buildable
    assert plan.manifest['exclusions']['advanced_regex'] == pattern
    assert plan.argv[plan.argv.index('--exclude') + 1] == pattern
    assert plan.manifest['exclusions']['advanced_matched_paths'] == [
        'docs/12-release.txt',
    ]


def test_advanced_regex_matching_boot_or_selected_module_is_blocked(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()

    def validator(_pattern, paths):
        matches = tuple(path for path in paths if path.startswith('boot/'))
        return True, matches, ''

    project = _project(
        info, project_dir / 'out.iso', project_dir,
        exclusions=('^boot/.*',))
    plan = _plan(
        project, info, _config(project_dir), regex_validator=validator)
    assert 'advanced_regex_matches_mandatory_input' in _error_codes(plan)


def test_advanced_regex_and_module_deselection_cannot_change_backreferences(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = backend.ImageProject(
        project_base=str(project_dir), source_backend=info.backend,
        source_root_path=info.root_path, source_path=info.source_path,
        source_fingerprint=info.fingerprint,
        selected_source_modules=(
            '00-core-amd64.sb', '01-kernel-amd64.sb'),
        exclusions=(r'^(docs)/(.*)\2$',), output_path='out.iso')
    plan = _plan(project, info, _config(project_dir))
    assert 'advanced_regex_cannot_combine_safely' in _error_codes(plan)
    assert plan.argv == ()


def test_invalid_advanced_regex_is_blocked_when_validator_cannot_guarantee_it(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir, exclusions=('(',))
    plan = _plan(
        project, info, _config(project_dir),
        regex_validator=lambda pattern, paths: (False, (), 'invalid ERE'))
    assert 'advanced_regex_validation_failed' in _error_codes(plan)


def test_deselecting_a_middle_layer_while_keeping_higher_layers_is_blocked(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(
        tmp_path,
        module_names=(
            '00-core-amd64.sb', '01-kernel-amd64.sb', '02-firmware-amd64.sb',
            '03-gui-base-amd64.sb', '04-xfce-desktop-amd64.sb'))
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    # A hand-edited project keeps the desktop layer (04) but drops firmware (02).
    holed = backend.ImageProject(
        project_base=str(project_dir), source_backend=info.backend,
        source_root_path=info.root_path, source_path=info.source_path,
        source_fingerprint=info.fingerprint,
        selected_source_modules=(
            '00-core-amd64.sb', '01-kernel-amd64.sb', '03-gui-base-amd64.sb',
            '04-xfce-desktop-amd64.sb'),
        output_path='out.iso')
    holed_plan = _plan(holed, info, _config(project_dir))
    assert 'module_dependency_gap' in _error_codes(holed_plan)
    assert not holed_plan.buildable

    # A gap-free stack (dropping the contiguous top) builds fine.
    stacked = backend.ImageProject(
        project_base=str(project_dir), source_backend=info.backend,
        source_root_path=info.root_path, source_path=info.source_path,
        source_fingerprint=info.fingerprint,
        selected_source_modules=(
            '00-core-amd64.sb', '01-kernel-amd64.sb', '02-firmware-amd64.sb'),
        output_path='out.iso')
    stacked_plan = _plan(stacked, info, _config(project_dir))
    assert 'module_dependency_gap' not in _error_codes(stacked_plan)


def test_output_symlink_existing_policy_and_source_containment_are_blocked(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    config = _config(project_dir)
    target = _write(project_dir / 'real.iso', b'existing')
    symlink = project_dir / 'link.iso'
    os.symlink(str(target), str(symlink))
    symlink_project = _project(info, symlink, project_dir, overwrite_output=True)
    symlink_plan = _plan(symlink_project, info, config)
    assert 'output_not_regular' in _error_codes(symlink_plan)

    existing_project = _project(info, target, project_dir)
    existing_plan = _plan(existing_project, info, config)
    assert 'output_exists_overwrite_not_allowed' in _error_codes(existing_plan)

    source_output_project = _project(
        info, source / 'generated.iso', project_dir)
    source_output_plan = _plan(source_output_project, info, config)
    assert 'output_within_source' in _error_codes(source_output_plan)


def test_output_cannot_replace_config_and_scratch_cannot_be_source(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    config = _write(project_dir / 'config.iso', b'LIVE_USER=live\n')
    project = _project(
        info, config, project_dir, overwrite_output=True)
    overlap = _plan(project, info, config)
    assert 'output_overlaps_input' in _error_codes(overlap)

    safe_project = _project(info, project_dir / 'safe.iso', project_dir)
    scratch = backend.create_build_plan(
        safe_project, info, current_config_path=str(config),
        disk_usage_func=_large_disk, scratch_directory=str(source),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner())
    assert 'scratch_within_source' in _error_codes(scratch)


def test_output_and_scratch_directory_symlinks_are_blocked(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    real_output = project_dir / 'real-output'
    real_output.mkdir(parents=True)
    linked_output = project_dir / 'linked-output'
    os.symlink(str(real_output), str(linked_output))
    project = _project(info, linked_output / 'out.iso', project_dir)
    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(linked_output),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner())
    assert 'output_directory_symlink' in _error_codes(plan)
    assert 'scratch_directory_symlink' in _error_codes(plan)


def test_scratch_storage_must_support_private_workspace(tmp_path, monkeypatch):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    monkeypatch.setattr(
        backend, '_probe_private_workspace',
        lambda _path: 'filesystem cannot enforce mode 0700')

    plan = _plan(project, info, _config(project_dir))

    assert 'scratch_directory_incompatible' in _error_codes(plan)


def test_shared_non_sticky_scratch_directory_is_rejected(
        tmp_path, monkeypatch):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    scratch = tmp_path / 'shared-work'
    project_dir.mkdir()
    scratch.mkdir()
    scratch.chmod(0o777)
    project = _project(info, project_dir / 'out.iso', project_dir)
    probe_calls = []
    monkeypatch.setattr(
        backend, '_probe_private_workspace',
        lambda path: probe_calls.append(path))

    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(scratch),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner())

    assert 'scratch_directory_untrusted' in _error_codes(plan)
    assert probe_calls == []


def test_writable_acl_marks_scratch_path_untrusted(tmp_path, monkeypatch):
    scratch = tmp_path / 'acl-work'
    scratch.mkdir()
    scratch.chmod(0o770)
    monkeypatch.setattr(
        backend.os, 'getxattr',
        lambda *_args, **_kwargs: b'posix-acl')

    error = backend._scratch_path_trust_error(str(scratch))

    assert 'shared writable directory' in error


@pytest.mark.parametrize('union_fstype', ('overlay', 'aufs'))
def test_capture_rejects_live_union_scratch_but_custom_build_allows_it(
        tmp_path, union_fstype):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    mount_table = _write_mount_table(tmp_path, [
        ('none', '/', union_fstype),
    ])
    capture = _project(
        info, project_dir / 'captured.iso', project_dir,
        capture_mode='clean')
    ordinary = _project(info, project_dir / 'ordinary.iso', project_dir)

    capture_plan = _plan(
        capture, info, _config(project_dir), mounts_path=mount_table,
        changes_roots=())
    ordinary_plan = _plan(
        ordinary, info, _config(project_dir), mounts_path=mount_table,
        changes_roots=())

    assert 'scratch_on_captured_live_overlay' in _error_codes(capture_plan)
    assert 'scratch_on_captured_live_overlay' not in _error_codes(ordinary_plan)
    assert ordinary_plan.buildable


def test_capture_allows_scratch_on_independent_nested_overlay(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    mount_table = _write_mount_table(tmp_path, [
        ('/dev/sda1', '/', 'ext4'),
        ('overlay', os.path.realpath(str(project_dir)), 'overlay'),
    ])
    mountinfo = _write_mountinfo(tmp_path, [
        ('1', '0', '8:1', '/', '/', 'ext4', 'rw'),
        ('2', '1', '0:55', '/', str(project_dir), 'overlay',
         'rw,upperdir=/independent/upper'),
    ])
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')

    plan = _plan(
        project, info, _config(project_dir), mounts_path=mount_table,
        changes_roots=(), mountinfo_path=mountinfo)

    assert 'scratch_on_captured_live_overlay' not in _error_codes(plan)
    assert plan.buildable


@pytest.mark.parametrize('union_fstype', ('overlay', 'aufs'))
def test_capture_rejects_output_job_on_live_union(tmp_path, union_fstype):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    scratch = tmp_path / 'external-work'
    project_dir.mkdir()
    scratch.mkdir()
    mount_table = _write_mount_table(tmp_path, [
        ('none', '/', union_fstype),
        ('/dev/sdb1', os.path.realpath(str(scratch)), 'ext4'),
    ])
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')

    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(scratch),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner(), mounts_path=mount_table,
        changes_roots=())

    assert 'destination_on_captured_live_overlay' in _error_codes(plan)
    assert 'scratch_on_captured_live_overlay' not in _error_codes(plan)


def test_capture_allows_overlayfs_sibling_but_rejects_effective_changes_root(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    changes_container = tmp_path / 'live-changes'
    effective_changes = changes_container / 'changes'
    workdir = changes_container / 'workdir'
    captured_scratch = effective_changes / 'builder-work'
    sibling_scratch = changes_container / 'builder-work'
    for directory in (
            effective_changes, workdir, captured_scratch, sibling_scratch):
        directory.mkdir(parents=True, exist_ok=True)
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')
    config = _config(project_dir)
    options = {
        'current_config_path': str(config),
        'disk_usage_func': _large_disk,
        'tool_capabilities': _tool_capabilities(),
        'command_runner': AcceptSquashfsRunner(),
        'changes_roots': (str(changes_container),),
    }

    captured_plan = backend.create_build_plan(
        project, info, scratch_directory=str(captured_scratch), **options)
    sibling_plan = backend.create_build_plan(
        project, info, scratch_directory=str(sibling_scratch), **options)

    assert 'scratch_within_captured_changes' in _error_codes(captured_plan)
    assert 'scratch_within_captured_changes' not in _error_codes(sibling_plan)
    assert sibling_plan.buildable


def test_capture_rejects_bind_alias_of_effective_changes_root(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    changes_container = tmp_path / 'live-changes'
    effective_changes = changes_container / 'changes'
    (changes_container / 'workdir').mkdir(parents=True)
    effective_changes.mkdir()
    alias = tmp_path / 'changes-alias'
    scratch = alias / 'builder-work'
    scratch.mkdir(parents=True)
    mountinfo = _write_mountinfo(tmp_path, [
        ('1', '0', '8:1', '/', '/'),
        ('2', '1', '8:1', str(effective_changes), str(alias)),
    ])
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')

    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(scratch),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner(),
        changes_roots=(str(changes_container),),
        mountinfo_path=mountinfo)

    assert 'scratch_within_captured_changes' in _error_codes(plan)


def test_capture_rejects_bind_aliases_of_root_live_union(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    output_alias = tmp_path / 'output-alias'
    scratch_alias = tmp_path / 'scratch-alias'
    output_alias.mkdir()
    scratch_alias.mkdir()
    mount_table = _write_mount_table(tmp_path, [
        ('none', '/', 'overlay'),
        ('none', str(output_alias), 'overlay'),
        ('none', str(scratch_alias), 'overlay'),
    ])
    mountinfo = _write_mountinfo(tmp_path, [
        ('1', '0', '0:55', '/', '/', 'overlay', 'rw'),
        ('2', '1', '0:55', '/home/live/output', str(output_alias),
         'overlay', 'rw'),
        ('3', '1', '0:55', '/home/live/scratch', str(scratch_alias),
         'overlay', 'rw'),
    ])
    project = backend.ImageProject.from_source(
        info, str(output_alias / 'out.iso'),
        project_base=str(tmp_path), capture_mode='clean')

    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(tmp_path)),
        disk_usage_func=_large_disk, scratch_directory=str(scratch_alias),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner(), mounts_path=mount_table,
        mountinfo_path=mountinfo, changes_roots=())

    assert 'destination_on_captured_live_overlay' in _error_codes(plan)
    assert 'scratch_on_captured_live_overlay' in _error_codes(plan)


def test_capture_rejects_nested_overlay_with_upper_in_changes(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    scratch = tmp_path / 'nested-overlay'
    project_dir.mkdir()
    scratch.mkdir()
    changes_container = tmp_path / 'live-changes'
    effective_changes = changes_container / 'changes'
    upper = effective_changes / 'nested-upper'
    (changes_container / 'workdir').mkdir(parents=True)
    upper.mkdir(parents=True)
    mount_table = _write_mount_table(tmp_path, [
        ('/dev/sda1', '/', 'ext4'),
        ('overlay', str(scratch), 'overlay'),
    ])
    mountinfo = _write_mountinfo(tmp_path, [
        ('1', '0', '8:1', '/', '/', 'ext4', 'rw'),
        ('2', '1', '0:77', '/', str(scratch), 'overlay',
         'rw,upperdir={}'.format(upper)),
    ])
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')

    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(scratch),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner(), mounts_path=mount_table,
        mountinfo_path=mountinfo,
        changes_roots=(str(changes_container),))

    assert 'scratch_on_captured_live_overlay' in _error_codes(plan)


def test_prepare_rejects_replaced_scratch_directory(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    scratch = tmp_path / 'scratch'
    project_dir.mkdir()
    scratch.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(scratch),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner())
    assert plan.buildable
    assert plan.scratch_directory == os.path.realpath(str(scratch))

    scratch.rmdir()
    scratch.mkdir()

    with pytest.raises(backend.ImageProjectError,
                       match='temporary work directory changed'):
        backend.prepare_build_command(plan)


def test_scratch_inside_project_overlay_is_rejected_at_preflight(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    overlay = project_dir / 'overlay'
    scratch = overlay / 'work'
    scratch.mkdir(parents=True)
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        overlay_directory='overlay')

    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(scratch),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner())

    assert 'scratch_within_project_overlay' in _error_codes(plan)


def test_bind_alias_of_project_overlay_is_rejected_as_scratch(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    overlay = project_dir / 'overlay'
    alias = tmp_path / 'overlay-alias'
    _write(overlay / 'value', b'payload')
    alias.mkdir()
    mountinfo = _write_mountinfo(tmp_path, [
        ('1', '0', '8:1', '/', '/', 'ext4', 'rw'),
        ('2', '1', '8:1', str(overlay), str(alias), 'ext4', 'rw'),
    ])
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        overlay_directory='overlay')

    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(alias),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner(),
        mountinfo_path=mountinfo)

    assert 'scratch_within_project_overlay' in _error_codes(plan)


def test_graft_unsafe_module_basename_and_duplicate_target_are_blocked(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    unsafe = _fake_module(project_dir / 'bad name.sb')
    duplicate = _fake_module(project_dir / '00-core-amd64.sb')
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        additional_module_paths=('bad name.sb', '00-core-amd64.sb'))
    plan = _plan(project, info, _config(project_dir))
    assert 'module_target_not_graft_safe' in _error_codes(plan)
    assert 'duplicate_module_target' in _error_codes(plan)


def test_unsquashfs_rejection_blocks_module(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = _plan(
        project, info, _config(project_dir),
        runner=AcceptSquashfsRunner(reject='01-kernel'))
    assert 'selected_module_not_squashfs' in _error_codes(plan)


def test_real_unsquashfs_validation_with_mksquashfs_fixture(tmp_path):
    mksquashfs = shutil.which('mksquashfs')
    unsquashfs = shutil.which('unsquashfs')
    if not mksquashfs or not unsquashfs:
        pytest.skip('squashfs-tools are unavailable')
    source = tmp_path / 'squash-root'
    _write(source / 'file.txt', b'payload')
    module = tmp_path / 'actual.sb'
    result = subprocess.run(
        [mksquashfs, str(source), str(module), '-noappend', '-quiet',
         '-processors', '1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        pytest.skip('mksquashfs is not usable in this environment')
    valid, detail = backend.validate_squashfs(
        str(module), unsquashfs=unsquashfs)
    assert valid, detail


def test_kernel_initramfs_version_and_architecture_coherence(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(
        tmp_path, kernel_version='6.12.1', initramfs_version='6.11.9',
        release_arch='arm64')
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = _plan(project, info, _config(project_dir))
    codes = _error_codes(plan)
    assert 'kernel_initramfs_version_mismatch' in codes
    assert 'runtime_module_architecture_mismatch' in codes


def test_kernel_module_declared_version_must_match_boot_pair(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(
        tmp_path, module_names=(
            '00-core-amd64.sb', '01-kernel-6.11.0-amd64.sb'))
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = _plan(project, info, _config(project_dir))
    assert 'kernel_module_version_mismatch' in _error_codes(plan)


def test_destination_and_scratch_space_are_checked_conservatively(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=lambda path: (100, 100, 1),
        scratch_directory=str(project_dir),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner())
    assert 'destination_space_insufficient' in _error_codes(plan)
    assert 'scratch_space_insufficient' in _error_codes(plan)
    assert 'combined_space_insufficient' in _error_codes(plan)


def _write_mount_table(tmp_path, entries):
    path = tmp_path / 'resource-mounts'
    path.write_text(''.join(
        '{} {} {} rw 0 0\n'.format(device, mountpoint, fstype)
        for device, mountpoint, fstype in entries))
    return str(path)


def _write_mountinfo(tmp_path, entries):
    path = tmp_path / 'resource-mountinfo'
    lines = []
    for entry in entries:
        mount_id, parent_id, device, mount_root, mountpoint = entry[:5]
        fstype = entry[5] if len(entry) > 5 else 'ext4'
        options = entry[6] if len(entry) > 6 else 'rw'
        lines.append(
            '{} {} {} {} {} rw - {} source {}\n'.format(
                mount_id, parent_id, device, mount_root, mountpoint,
                fstype, options))
    path.write_text(''.join(lines))
    return str(path)


def _write_meminfo(tmp_path, mem_available_kb):
    path = tmp_path / 'resource-meminfo'
    path.write_text(
        'MemTotal:       16000000 kB\n'
        'MemAvailable:   {} kB\n'.format(mem_available_kb))
    return str(path)


def test_resource_planner_classifies_ram_workspace_and_reports_estimate(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    mount_table = _write_mount_table(tmp_path, [
        ('/dev/sda1', '/', 'ext4'),
        ('tmpfs', os.path.realpath(str(project_dir)), 'tmpfs'),
    ])
    meminfo = _write_meminfo(tmp_path, 8 * 1024 * 1024)  # 8 GiB available
    project = _project(info, project_dir / 'out.iso', project_dir)

    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner(),
        mounts_path=mount_table, meminfo_path=meminfo)

    estimate = plan.manifest['estimate']
    assert estimate['destination_filesystem_class'] == (
        backend.FILESYSTEM_CLASS_RAM_BACKED)
    assert estimate['scratch_filesystem_class'] == (
        backend.FILESYSTEM_CLASS_RAM_BACKED)
    assert estimate['available_memory_bytes'] == 8 * 1024 * 1024 * 1024
    assert estimate['peak_memory_bytes'] > estimate['required_scratch_bytes']
    # A deliberate RAM build that fits available memory is fully buildable and
    # not warned.
    assert plan.buildable
    assert 'ram_workspace_memory_pressure' not in _warning_codes(plan)


def test_ram_workspace_memory_pressure_warns_without_blocking(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    mount_table = _write_mount_table(tmp_path, [
        ('/dev/sda1', '/', 'ext4'),
        ('tmpfs', os.path.realpath(str(project_dir)), 'tmpfs'),
    ])
    meminfo = _write_meminfo(tmp_path, 64 * 1024)  # only 64 MiB available
    project = _project(info, project_dir / 'out.iso', project_dir)

    # tmpfs advertises ample free space, so the disk checks pass while memory
    # is actually scarce.
    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner(),
        mounts_path=mount_table, meminfo_path=meminfo)

    assert plan.buildable  # advisory, never blocked by filesystem type
    assert 'ram_workspace_memory_pressure' in _warning_codes(plan)
    message = next(item.message for item in plan.warnings
                   if item.code == 'ram_workspace_memory_pressure')
    assert 'persistent MiniOS changes storage' in message


def test_persistent_workspace_has_no_memory_accounting(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    mount_table = _write_mount_table(tmp_path, [
        ('/dev/sda1', '/', 'ext4'),
        ('/dev/sdb1', os.path.realpath(str(project_dir)), 'ext4'),
    ])
    meminfo = _write_meminfo(tmp_path, 64 * 1024)  # scarce memory
    project = _project(info, project_dir / 'out.iso', project_dir)

    plan = backend.create_build_plan(
        project, info, current_config_path=str(_config(project_dir)),
        disk_usage_func=_large_disk, scratch_directory=str(project_dir),
        tool_capabilities=_tool_capabilities(),
        command_runner=AcceptSquashfsRunner(),
        mounts_path=mount_table, meminfo_path=meminfo)

    estimate = plan.manifest['estimate']
    assert estimate['destination_filesystem_class'] == (
        backend.FILESYSTEM_CLASS_PERSISTENT)
    assert estimate['peak_memory_bytes'] is None
    assert plan.buildable
    assert 'ram_workspace_memory_pressure' not in _warning_codes(plan)


def test_resolve_device_mountpoint_matches_by_canonical_path(tmp_path):
    real_mount = tmp_path / 'media' / 'disc'
    real_mount.mkdir(parents=True)
    link = tmp_path / 'dev' / 'sr0-link'
    (tmp_path / 'dev').mkdir()
    os.symlink('/dev/sr0', str(link))
    mounts = tmp_path / 'mounts'
    mounts.write_text(
        '/dev/sda1 / ext4 rw 0 0\n'
        '/dev/sr0 {} iso9660 ro 0 0\n'.format(real_mount))

    assert backend.resolve_device_mountpoint(
        '/dev/sr0', mounts_path=str(mounts)) == str(real_mount)
    # A symlink to the same device resolves to the same mountpoint.
    assert backend.resolve_device_mountpoint(
        str(link), mounts_path=str(mounts)) == str(real_mount)
    assert backend.resolve_device_mountpoint(
        '/dev/sr9', mounts_path=str(mounts)) is None
    assert backend.resolve_device_mountpoint(
        '', mounts_path=str(mounts)) is None


def test_resolve_device_mountpoint_unescapes_spaced_mountpoint(tmp_path):
    mounts = tmp_path / 'mounts'
    mounts.write_text('/dev/loop3 /media/My\\040Disc iso9660 ro 0 0\n')
    assert backend.resolve_device_mountpoint(
        '/dev/loop3', mounts_path=str(mounts)) == '/media/My Disc'


def test_find_loop_backing_device_matches_backing_file(tmp_path):
    iso = tmp_path / 'image.iso'
    iso.write_bytes(b'iso-bytes')
    sys_block = tmp_path / 'sys-block'
    for name, backing in (('loop0', '/other/file.iso'),
                          ('loop1', str(iso)),
                          ('sda', None)):
        node = sys_block / name
        node.mkdir(parents=True)
        if backing is not None:
            (node / 'loop').mkdir()
            (node / 'loop' / 'backing_file').write_text(backing + '\n')

    assert backend.find_loop_backing_device(
        str(iso), sys_block_root=str(sys_block)) == '/dev/loop1'
    assert backend.find_loop_backing_device(
        str(tmp_path / 'absent.iso'), sys_block_root=str(sys_block)) is None
    assert backend.find_loop_backing_device('', sys_block_root=str(sys_block)) \
        is None


def test_find_loop_backing_device_ignores_deleted_suffix(tmp_path):
    iso = tmp_path / 'image.iso'
    iso.write_bytes(b'iso-bytes')
    sys_block = tmp_path / 'sys-block'
    node = sys_block / 'loop7' / 'loop'
    node.mkdir(parents=True)
    (node / 'backing_file').write_text('{} (deleted)\n'.format(iso))
    assert backend.find_loop_backing_device(
        str(iso), sys_block_root=str(sys_block)) == '/dev/loop7'


def test_structural_verification_is_bound_to_plan_and_exact_expected_paths(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)
    runner = FakeXorriso(plan)

    result = backend.verify_iso(plan, runner=runner, xorriso='/tools/xorriso')

    assert result.structurally_verified
    assert result.plan_id == plan.plan_id
    assert result.sha256 == hashlib.sha256(b'fake-iso').hexdigest()
    assert result.adapter_manifest_sha256 == hashlib.sha256(
        plan.manifest_payload).hexdigest()
    assert result.capture_summary['requested'] is False
    assert runner.calls[0] == [
        '/tools/xorriso', '-indev', plan.partial_output_path, '-find', '/',
    ]
    assert '-print' not in runner.calls[0]
    assert len(runner.calls) == 6


def test_customization_verification_attests_config_boot_background_and_overlay(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    override_value = 'verification-private-value'
    kernel_text = 'audit=1 verify_customization=private'
    background = _png(project_dir / 'background.png', 80, 60)
    overlay = project_dir / 'overlay'
    _write(overlay / 'etc' / 'verified.conf', b'verified=true\n')
    link = overlay / 'etc' / 'verified-link'
    os.symlink('verified.conf', str(link))
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        live_config_overrides={'LIVE_USER_FULLNAME': override_value},
        boot_timeout=4, kernel_args=kernel_text,
        boot_background_path=str(background),
        overlay_directory=str(overlay))
    plan = _plan(project, info, _config(project_dir))
    backend.prepare_build_command(plan)
    _prepare_artifact(plan)
    runner = FakeXorriso(plan)

    result = backend.verify_iso(
        plan, runner=runner, xorriso='/tools/xorriso',
        unsquashfs='/tools/unsquashfs')

    assert result.structurally_verified
    summary = result.customization_summary
    assert summary['requested'] is True
    assert summary['adapter_report_verified'] is True
    assert summary['live_config']['override_count'] == 1
    assert summary['boot']['timeout_seconds'] == 4
    assert summary['boot']['kernel_args']['sha256'] == hashlib.sha256(
        kernel_text.encode('utf-8')).hexdigest()
    assert summary['boot']['background']['width'] == 80
    assert summary['overlay']['module_order'] == 5
    assert summary['overlay']['module_sha256'] == hashlib.sha256(
        runner.customization_overlay_module).hexdigest()
    serialized = json.dumps(result.to_dict(), sort_keys=True)
    assert override_value not in serialized
    assert kernel_text not in serialized
    assert str(background) not in serialized
    assert str(overlay) not in serialized
    assert not list(project_dir.glob(
        '.minios-image-builder-*/customization-verify-*'))
    assert (overlay / 'etc' / 'verified.conf').exists()


def test_customization_verification_compares_squashfs_mtime_precision(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    overlay = project_dir / 'overlay'
    overlay_file = _write(overlay / 'value', b'same-content')
    original_ns = 1700000000123456789
    os.utime(str(overlay_file), ns=(original_ns, original_ns))
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        overlay_directory=str(overlay))
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)
    os.utime(
        str(overlay_file),
        ns=(original_ns + 2000000000, original_ns + 2000000000))

    result = backend.verify_iso(
        plan, runner=FakeXorriso(plan), xorriso='/tools/xorriso',
        unsquashfs='/tools/unsquashfs')

    assert result.level == backend.VERIFICATION_BUILT
    assert 'image_customization_attestation_failed' in _error_codes(result)


@pytest.mark.parametrize('tamper', [
    'kernel', 'config-digest', 'coordinated-config', 'background-target',
    'overlay-fingerprint', 'unknown-field',
])
def test_customization_verification_rejects_tampered_report(tmp_path, tamper):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    background = _png(project_dir / 'background.png')
    overlay = project_dir / 'overlay'
    _write(overlay / 'value', b'trusted')
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        boot_timeout=3, kernel_args='audit=1',
        boot_background_path=str(background),
        overlay_directory=str(overlay))
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)
    runner = FakeXorriso(plan)
    if tamper == 'kernel':
        runner.customization_report['boot']['kernel_args']['sha256'] = 'a' * 64
    elif tamper == 'config-digest':
        runner.customization_report['boot']['configs'][0]['sha256'] = 'a' * 64
    elif tamper == 'coordinated-config':
        target = runner.customization_report['boot']['configs'][0]['target']
        runner.custom_boot_files[target] = b'coordinated tamper\n'
        runner.customization_report['boot']['configs'][0].update({
            'size': len(runner.custom_boot_files[target]),
            'sha256': hashlib.sha256(
                runner.custom_boot_files[target]).hexdigest(),
        })
    elif tamper == 'background-target':
        runner.customization_report['boot']['background']['targets'] = [
            'minios/boot/bootlogo999.png']
    elif tamper == 'overlay-fingerprint':
        runner.customization_report['overlay'][
            'input_tree_fingerprint'] = 'a' * 64
    else:
        runner.customization_report['unexpected'] = True

    result = backend.verify_iso(
        plan, runner=runner, xorriso='/tools/xorriso',
        unsquashfs='/tools/unsquashfs')

    assert result.level == backend.VERIFICATION_BUILT
    assert 'image_customization_attestation_failed' in _error_codes(result)
    assert not list(project_dir.glob(
        '.minios-image-builder-*/customization-verify-*'))


def test_live_config_only_customization_verifies_without_adapter_report(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        live_config_overrides={'LIVE_HOSTNAME': 'configured-image'})
    plan = _plan(project, info, _config(project_dir))
    backend.prepare_build_command(plan)
    _prepare_artifact(plan)

    result = backend.verify_iso(
        plan, runner=FakeXorriso(plan), xorriso='/tools/xorriso')

    assert result.structurally_verified
    assert result.customization_summary['adapter_report_verified'] is False
    assert result.customization_summary['live_config']['override_count'] == 1
    assert '--boot-timeout' not in plan.argv


def test_overlay_and_capture_verify_as_consecutive_final_layers(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    overlay = project_dir / 'overlay'
    _write(overlay / 'value', b'combined')
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        overlay_directory=str(overlay), capture_mode='clean')
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)
    runner = FakeXorriso(plan)

    result = backend.verify_iso(
        plan, runner=runner, xorriso='/tools/xorriso',
        unsquashfs='/tools/unsquashfs')

    assert result.structurally_verified
    assert result.customization_summary['overlay']['module_order'] == 5
    assert result.capture_summary['module_order'] == 6
    assert result.customization_summary['boot']['config_target_count'] == 0


def test_capture_verification_extracts_and_attests_bound_layer(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    inventory = _inventory()
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')
    plan = _plan(
        project, info, _config(project_dir), session_inventory=inventory)
    _prepare_artifact(plan)
    runner = FakeXorriso(plan)

    result = backend.verify_iso(
        plan, runner=runner, xorriso='/tools/xorriso',
        unsquashfs='/tools/unsquashfs')

    assert result.structurally_verified
    assert result.capture_summary['requested'] is True
    assert result.capture_summary['profile'] == 'clean'
    assert result.capture_summary['source_fingerprint'] == (
        inventory.source_fingerprint)
    assert result.capture_summary['module_sha256'] == hashlib.sha256(
        runner.capture_module).hexdigest()
    assert len(result.commands) == 8
    assert any('-extract' in command for command in result.commands)
    assert not list(
        project_dir.glob('.minios-image-builder-*/capture-verify-*'))


def test_selected_capture_verification_binds_selection_without_paths(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    include_path = 'opt/selection-secret-name'
    inventory = _inventory(entries=[{
        'path': include_path + '/config',
        'type': 'regular',
        'category': 'software',
        'sensitive': False,
        'default_exact': True,
        'default_clean': True,
        'size': 10,
    }])
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        capture_mode='selected', capture_include_paths=(include_path,))
    plan = _plan(
        project, info, _config(project_dir), session_inventory=inventory)
    backend.prepare_build_command(plan)
    _prepare_artifact(plan)

    result = backend.verify_iso(plan, runner=FakeXorriso(plan))

    assert result.structurally_verified
    assert result.capture_summary['selection_sha256'] == plan.manifest[
        'capture']['selection']['sha256']
    assert include_path not in json.dumps(result.to_dict())


@pytest.mark.parametrize('tamper', [
    'base', 'source', 'selection', 'module-digest', 'profile', 'unknown-field',
])
def test_capture_verification_rejects_tampered_report(tmp_path, tamper):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')
    plan = _plan(
        project, info, _config(project_dir),
        session_inventory=_inventory())
    _prepare_artifact(plan)
    runner = FakeXorriso(plan)
    if tamper == 'base':
        runner.capture_report['base_module_fingerprint'] = 'a' * 64
    elif tamper == 'source':
        runner.capture_report['source_fingerprint'] = 'a' * 64
    elif tamper == 'selection':
        runner.capture_report['selection_sha256'] = 'a' * 64
    elif tamper == 'module-digest':
        runner.capture_report['module']['sha256'] = 'a' * 64
    elif tamper == 'profile':
        runner.capture_report['profile'] = 'exact'
    else:
        runner.capture_report['unexpected'] = True

    result = backend.verify_iso(plan, runner=runner)

    assert result.level == backend.VERIFICATION_BUILT
    assert 'session_capture_attestation_failed' in _error_codes(result)
    assert not list(
        project_dir.glob('.minios-image-builder-*/capture-verify-*'))


def test_capture_verification_rejects_module_bytes_not_matching_report(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)
    runner = FakeXorriso(plan)
    runner.capture_module = b'different-captured-module'

    result = backend.verify_iso(plan, runner=runner)

    assert result.level == backend.VERIFICATION_BUILT
    assert 'session_capture_attestation_failed' in _error_codes(result)


def test_capture_tree_requires_exact_dynamic_layer_and_last_order(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)
    capture_path = '/' + plan.manifest['expected_iso']['session_capture'][
        'module_target']

    missing = backend.verify_iso(
        plan, runner=FakeXorriso(plan, omit=(capture_path,)))
    extra = backend.verify_iso(
        plan, runner=FakeXorriso(
            plan, extra=('/minios/06-session-changes.sb',
                         '/minios/99-unplanned.sb')))

    assert 'session_capture_module_set_mismatch' in _error_codes(missing)
    assert 'session_capture_module_set_mismatch' in _error_codes(extra)
    assert 'session_capture_not_last_module' in _error_codes(extra)


def test_custom_verification_forbids_capture_report_and_layer(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)

    result = backend.verify_iso(
        plan, runner=FakeXorriso(plan, extra=(
            '/minios/session-capture.json',
            '/minios/05-session-changes.sb',
            '/minios/image-customization.json',
            '/minios/05-image-overlay.sb')))

    assert result.level == backend.VERIFICATION_BUILT
    assert 'unexpected_session_capture_report' in _error_codes(result)
    assert 'unexpected_session_capture_module' in _error_codes(result)
    assert 'unexpected_image_customization_report' in _error_codes(result)
    assert 'unexpected_image_overlay_module' in _error_codes(result)


def test_verification_requires_modules_config_kernel_initramfs_and_forbids_deselected(
        tmp_path):
    names = ('00-core-amd64.sb', '01-kernel-amd64.sb',
             '04-xfce-desktop-amd64.sb')
    root, source, mounts, sys_block, release, info = _make_source(
        tmp_path, module_names=names)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = backend.ImageProject(
        project_base=str(project_dir), source_backend=info.backend,
        source_root_path=info.root_path, source_path=info.source_path,
        source_fingerprint=info.fingerprint,
        selected_source_modules=(
            '00-core-amd64.sb', '01-kernel-amd64.sb'),
        output_path='out.iso')
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)
    missing = {
        '/minios/config.conf',
        '/' + plan.manifest['expected_iso']['kernel_targets'][0],
        '/' + plan.manifest['expected_iso']['initramfs_targets'][0],
    }
    forbidden = '/' + plan.manifest['expected_iso'][
        'forbidden_module_targets'][0]
    result = backend.verify_iso(
        plan, runner=FakeXorriso(plan, omit=missing, extra=(forbidden,)))
    codes = _error_codes(result)
    assert result.level == backend.VERIFICATION_BUILT
    assert 'expected_iso_path_missing' in codes
    assert 'deselected_module_present' in codes


def test_nonboot_or_wrong_volume_iso_fails_structural_verification(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir, volume_label='EXPECTED')
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)
    result = backend.verify_iso(
        plan, runner=FakeXorriso(
            plan, boot=False, volume_label='WRONG'))
    assert result.level == backend.VERIFICATION_BUILT
    assert 'iso_bios_boot_missing' in _error_codes(result)
    assert 'iso_uefi_boot_entries_missing' in _error_codes(result)
    assert 'iso_volume_id_mismatch' in _error_codes(result)


def test_verification_requires_valid_companion_json_manifest(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = _plan(project, info, _config(project_dir))
    _write_path(plan.partial_output_path, b'fake-iso')
    _write_path(plan.adapter_manifest_path, b'not-json')
    result = backend.verify_iso(plan, runner=FakeXorriso(plan))
    assert result.level == backend.VERIFICATION_BUILT
    assert 'adapter_manifest_invalid' in _error_codes(result)


def test_verification_requires_canonical_local_and_embedded_build_manifest(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)

    local_plan = _plan(project, info, _config(project_dir))
    _write_path(local_plan.partial_output_path, b'fake-iso')
    compact = json.dumps(
        local_plan.manifest, ensure_ascii=True, sort_keys=True,
        separators=(',', ':')).encode('ascii')
    _write_path(local_plan.adapter_manifest_path, compact)
    os.chmod(local_plan.adapter_manifest_path, 0o600)
    local_result = backend.verify_iso(
        local_plan, runner=FakeXorriso(local_plan))
    assert 'adapter_manifest_invalid' in _error_codes(local_result)

    embedded_project = _project(
        info, project_dir / 'embedded.iso', project_dir)
    embedded_plan = _plan(embedded_project, info, _config(project_dir))
    _prepare_artifact(embedded_plan)
    embedded_compact = json.dumps(
        embedded_plan.manifest, ensure_ascii=True, sort_keys=True,
        separators=(',', ':')).encode('ascii')
    embedded_result = backend.verify_iso(
        embedded_plan,
        runner=FakeXorriso(
            embedded_plan, build_manifest=embedded_compact))
    assert 'build_manifest_attestation_failed' in _error_codes(
        embedded_result)


def test_verification_requires_embedded_build_manifest_path(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)

    result = backend.verify_iso(
        plan, runner=FakeXorriso(
            plan, omit=('/minios/build-manifest.json',)))

    assert result.level == backend.VERIFICATION_BUILT
    assert 'build_manifest_missing' in _error_codes(result)


def test_private_extraction_cleanup_failure_blocks_structural_verification(
        tmp_path, monkeypatch):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)
    real_cleanup = backend._cleanup_private_extraction

    def failing_cleanup(directory, cleanup_plan, expected_identity=None):
        real_cleanup(directory, cleanup_plan, expected_identity)
        raise backend.ImageProjectError('forced cleanup failure')

    monkeypatch.setattr(
        backend, '_cleanup_private_extraction', failing_cleanup)
    result = backend.verify_iso(plan, runner=FakeXorriso(plan))

    assert result.level == backend.VERIFICATION_BUILT
    assert 'private_extraction_cleanup_failed' in _error_codes(result)


def test_verification_result_cannot_be_asserted_by_caller(tmp_path):
    with pytest.raises(TypeError):
        backend.VerificationResult(
            str(tmp_path / 'fake.iso'), backend.VERIFICATION_STRUCTURAL)


def test_verify_rejects_symlink_partial_and_replaced_job_directory(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    symlink_plan = _plan(project, info, _config(project_dir))
    target = _write(project_dir / 'target.iso', b'payload')
    os.symlink(str(target), symlink_plan.partial_output_path)
    _write_path(symlink_plan.adapter_manifest_path, b'{}')
    result = backend.verify_iso(
        symlink_plan, runner=FakeXorriso(symlink_plan))
    assert result.level == backend.VERIFICATION_NOT_BUILT
    assert 'output_not_regular' in _error_codes(result)

    second_project = _project(
        info, project_dir / 'second.iso', project_dir)
    second = _plan(second_project, info, _config(project_dir))
    moved = second.job_directory + '.moved'
    os.rename(second.job_directory, moved)
    os.mkdir(second.job_directory, 0o700)
    result = backend.verify_iso(second, runner=FakeXorriso(second))
    assert 'job_identity_changed' in _error_codes(result)


def test_publish_repeats_verification_and_rejects_inode_replacement(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan)
    runner = FakeXorriso(plan)
    verified = backend.verify_iso(plan, runner=runner)
    original_calls = len(runner.calls)

    published = backend.publish_verified_output(
        plan, verified, runner=runner)

    assert published == str(project_dir / 'out.iso')
    assert os.path.isfile(published)
    assert len(runner.calls) == original_calls + 6

    second_project = _project(
        info, project_dir / 'second.iso', project_dir)
    second = _plan(second_project, info, _config(project_dir))
    _prepare_artifact(second, b'same-bytes')
    second_runner = FakeXorriso(second)
    second_verified = backend.verify_iso(second, runner=second_runner)
    os.unlink(second.partial_output_path)
    _write_path(second.partial_output_path, b'same-bytes')
    with pytest.raises(backend.OutputPublishError):
        backend.publish_verified_output(
            second, second_verified, runner=second_runner)

    third_project = _project(
        info, project_dir / 'third.iso', project_dir)
    third = _plan(third_project, info, _config(project_dir))
    _prepare_artifact(third, b'manifest-test')
    third_runner = FakeXorriso(third)
    third_verified = backend.verify_iso(third, runner=third_runner)
    _write_path(third.adapter_manifest_path, b'{"changed":true}\n')
    with pytest.raises(backend.OutputPublishError):
        backend.publish_verified_output(
            third, third_verified, runner=third_runner)


def test_publish_repeats_capture_attestation_before_atomic_output(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'captured.iso', project_dir,
        capture_mode='clean')
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan, b'captured-iso')
    runner = FakeXorriso(plan)
    verified = backend.verify_iso(plan, runner=runner)
    original_calls = len(runner.calls)

    published = backend.publish_verified_output(
        plan, verified, runner=runner,
        unsquashfs='/tools/unsquashfs')

    assert published == str(project_dir / 'captured.iso')
    assert len(runner.calls) == original_calls + 8
    assert not list(
        project_dir.glob('.minios-image-builder-*/capture-verify-*'))


def test_publish_is_plan_bound_and_refuses_destination_appearing_after_plan(
        tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    first_project = _project(info, project_dir / 'one.iso', project_dir)
    second_project = _project(info, project_dir / 'two.iso', project_dir)
    config = _config(project_dir)
    first = _plan(first_project, info, config)
    second = _plan(second_project, info, config)
    _prepare_artifact(first)
    _prepare_artifact(second)
    first_result = backend.verify_iso(first, runner=FakeXorriso(first))
    with pytest.raises(backend.OutputPublishError):
        backend.publish_verified_output(
            second, first_result, runner=FakeXorriso(second))

    appeared = project_dir / 'one.iso'
    appeared.write_bytes(b'do-not-overwrite')
    with pytest.raises(backend.OutputPublishError):
        backend.publish_verified_output(
            first, first_result, runner=FakeXorriso(first))
    assert appeared.read_bytes() == b'do-not-overwrite'


def test_explicit_overwrite_publishes_only_unchanged_existing_inode(tmp_path):
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    output = _write(project_dir / 'out.iso', b'old')
    project = _project(
        info, output, project_dir, overwrite_output=True)
    plan = _plan(project, info, _config(project_dir))
    _prepare_artifact(plan, b'new')
    runner = FakeXorriso(plan)
    verified = backend.verify_iso(plan, runner=runner)
    backend.publish_verified_output(plan, verified, runner=runner)
    assert output.read_bytes() == b'new'

    guarded_output = _write(project_dir / 'guarded.iso', b'original')
    guarded_project = _project(
        info, guarded_output, project_dir, overwrite_output=True)
    guarded = _plan(guarded_project, info, _config(project_dir))
    _prepare_artifact(guarded, b'replacement')
    guarded_runner = FakeXorriso(guarded)
    guarded_verified = backend.verify_iso(guarded, runner=guarded_runner)
    guarded_output.write_bytes(b'changed-after-plan')
    with pytest.raises(backend.OutputPublishError):
        backend.publish_verified_output(
            guarded, guarded_verified, runner=guarded_runner)
    assert guarded_output.read_bytes() == b'changed-after-plan'


def test_real_xorriso_integration_rejects_nonboot_iso(tmp_path):
    xorriso = shutil.which('xorriso')
    if not xorriso:
        pytest.skip('xorriso is unavailable')
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(info, project_dir / 'out.iso', project_dir)
    plan = _plan(project, info, _config(project_dir))
    staging = tmp_path / 'iso-tree'
    for iso_path in _expected_iso_paths(plan):
        _write(staging / iso_path.lstrip('/'), b'nonempty')
    _write(
        staging / plan.manifest['expected_iso']['build_manifest_target'],
        plan.manifest_payload)
    command = [
        xorriso, '-as', 'mkisofs', '-V', plan.manifest['volume_label'],
        '-o', plan.partial_output_path, str(staging),
    ]
    built = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if built.returncode != 0:
        pytest.skip('xorriso cannot create integration fixture')
    backend.atomic_write_json(plan.adapter_manifest_path, plan.manifest)

    result = backend.verify_iso(plan, xorriso=xorriso)
    codes = _error_codes(result)

    assert result.level == backend.VERIFICATION_BUILT
    assert 'xorriso_tree_failed' not in codes
    assert 'xorriso_file_report_failed' not in codes
    assert 'xorriso_symlink_report_failed' not in codes
    assert 'xorriso_pvd_report_failed' not in codes
    assert 'expected_regular_file_unobserved' not in codes
    assert 'build_manifest_attestation_failed' not in codes
    assert 'iso_bios_boot_missing' in codes


def test_real_xorriso_extracts_and_attests_capture_layer(tmp_path):
    xorriso = shutil.which('xorriso')
    mksquashfs = shutil.which('mksquashfs')
    unsquashfs = shutil.which('unsquashfs')
    if not xorriso or not mksquashfs or not unsquashfs:
        pytest.skip('xorriso and squashfs-tools are unavailable')
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    project = _project(
        info, project_dir / 'out.iso', project_dir, capture_mode='clean')
    plan = _plan(project, info, _config(project_dir))

    capture_root = tmp_path / 'capture-root'
    _write(capture_root / 'etc' / 'captured.conf', b'captured=true\n')
    capture_module = tmp_path / 'capture.sb'
    compressed = subprocess.run(
        [mksquashfs, str(capture_root), str(capture_module), '-noappend',
         '-quiet', '-processors', '1'], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    if compressed.returncode != 0:
        pytest.skip('mksquashfs cannot create capture fixture')
    module_bytes = capture_module.read_bytes()
    report = FakeXorriso(
        plan, capture_module=module_bytes).capture_report
    staging = tmp_path / 'capture-iso-tree'
    for iso_path in _expected_iso_paths(plan):
        _write(staging / iso_path.lstrip('/'), b'nonempty')
    _write(
        staging / plan.manifest['expected_iso']['build_manifest_target'],
        plan.manifest_payload)
    capture = plan.manifest['expected_iso']['session_capture']
    _write(staging / capture['module_target'], module_bytes)
    _write(
        staging / capture['report_target'],
        (json.dumps(report, sort_keys=True, separators=(',', ':')) +
         '\n').encode('utf-8'))
    built = subprocess.run(
        [xorriso, '-as', 'mkisofs', '-V', plan.manifest['volume_label'],
         '-o', plan.partial_output_path, str(staging)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if built.returncode != 0:
        pytest.skip('xorriso cannot create capture integration fixture')
    backend.atomic_write_json(plan.adapter_manifest_path, plan.manifest)

    result = backend.verify_iso(
        plan, xorriso=xorriso, unsquashfs=unsquashfs)
    codes = _error_codes(result)

    assert result.level == backend.VERIFICATION_BUILT
    assert 'build_manifest_attestation_failed' not in codes
    assert 'session_capture_attestation_failed' not in codes
    assert 'session_capture_module_set_mismatch' not in codes
    assert result.capture_summary['profile'] == 'clean'
    assert result.capture_summary['module_sha256'] == hashlib.sha256(
        module_bytes).hexdigest()
    assert not list(
        project_dir.glob('.minios-image-builder-*/capture-verify-*'))


def test_real_xorriso_extracts_and_attests_image_customization(tmp_path):
    xorriso = shutil.which('xorriso')
    mksquashfs = shutil.which('mksquashfs')
    unsquashfs = shutil.which('unsquashfs')
    if not xorriso or not mksquashfs or not unsquashfs:
        pytest.skip('xorriso and squashfs-tools are unavailable')
    root, source, mounts, sys_block, release, info = _make_source(tmp_path)
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    background = _png(project_dir / 'background.png', 48, 32)
    overlay = project_dir / 'overlay'
    overlay_file = _write(
        overlay / 'etc' / 'real-customization.conf', b'enabled=true\n')
    overlay_mtime = 1700000000123456789
    for path in (overlay_file, overlay / 'etc', overlay):
        os.utime(str(path), ns=(overlay_mtime, overlay_mtime))
    project = _project(
        info, project_dir / 'out.iso', project_dir,
        live_config_overrides={'LIVE_HOSTNAME': 'real-image'},
        boot_timeout=4, boot_background_path=str(background),
        overlay_directory=str(overlay))
    plan = _plan(project, info, _config(project_dir))
    backend.prepare_build_command(plan)

    overlay_module = tmp_path / 'real-overlay.sb'
    compressed = subprocess.run(
        [mksquashfs, str(overlay), str(overlay_module), '-noappend',
         '-quiet', '-processors', '1'], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    if compressed.returncode != 0:
        pytest.skip('mksquashfs cannot create customization fixture')
    overlay_bytes = overlay_module.read_bytes()
    report = FakeXorriso(
        plan, customization_overlay_module=overlay_bytes).customization_report
    customization = plan.manifest['expected_iso']['image_customization']
    staging = tmp_path / 'customization-iso-tree'
    for iso_path in _expected_iso_paths(plan):
        _write(staging / iso_path.lstrip('/'), b'nonempty')
    _write(
        staging / plan.manifest['expected_iso']['build_manifest_target'],
        plan.manifest_payload)
    _write(staging / 'minios/config.conf', plan._live_config_payload)
    _write(
        staging / customization['report_target'],
        (json.dumps(report, sort_keys=True, separators=(',', ':')) +
         '\n').encode('utf-8'))
    for target, payload in backend._thaw(
            plan._boot_config_payloads).items():
        _write(staging / target, payload)
    for target in customization['background_targets']:
        _write(staging / target, background.read_bytes())
    _write(staging / customization['overlay_target'], overlay_bytes)
    built = subprocess.run(
        [xorriso, '-as', 'mkisofs', '-V', plan.manifest['volume_label'],
         '-o', plan.partial_output_path, str(staging)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if built.returncode != 0:
        pytest.skip('xorriso cannot create customization integration fixture')
    backend.atomic_write_json(plan.adapter_manifest_path, plan.manifest)

    result = backend.verify_iso(
        plan, xorriso=xorriso, unsquashfs=unsquashfs)
    codes = _error_codes(result)

    assert result.level == backend.VERIFICATION_BUILT
    assert 'build_manifest_attestation_failed' not in codes
    assert 'image_customization_attestation_failed' not in codes
    assert 'image_overlay_module_set_mismatch' not in codes
    assert result.customization_summary['boot']['timeout_seconds'] == 4
    assert result.customization_summary['overlay']['module_sha256'] == (
        hashlib.sha256(overlay_bytes).hexdigest())
    assert not list(project_dir.glob(
        '.minios-image-builder-*/customization-verify-*'))


def test_vm_detection_and_handoff_contracts_remain_nonexecuting(tmp_path):
    paths = {
        'VBoxManage.exe': '/mnt/c/VBoxManage.exe',
        'qemu-system-x86_64': '/usr/bin/qemu-system-x86_64',
    }
    capabilities = backend.detect_vm_capabilities(
        which=lambda name: paths.get(name))
    assert capabilities['boot_test_available'] is True
    assert all(not item['executes_automatically']
               for item in capabilities['actions'])

    module = _fake_module(tmp_path / 'modules' / 'addon.sb')
    document = backend.create_module_manager_handoff(
        (str(module),), base_dir=str(tmp_path))
    parsed = backend.parse_module_manager_handoff(
        document, base_dir=str(tmp_path), require_existing=True)
    assert parsed.module_paths == (str(module),)
    intent_document = backend.create_store_application_install_intent(
        ('org.example.Editor',))
    intent = backend.parse_store_application_install_intent(intent_document)
    assert intent.application_ids == ('org.example.Editor',)
    assert intent_document['execution'] == 'not-implemented'


def test_role_order_and_sha_helpers(tmp_path):
    assert backend.parse_module_order('01-kernel-amd64.sb') == 1
    assert backend.describe_module_name('04-xfce-desktop.sb')['role'] == 'desktop'
    path = _write(tmp_path / 'file', b'content')
    assert backend.sha256_file(str(path)) == hashlib.sha256(b'content').hexdigest()


def _custom_boot_menu():
    return [
        {
            'id': 'ram-trim', 'base_mode': 'toram', 'enabled': True,
            'default': True, 'title': 'Run entirely from RAM',
            'kernel_args': 'toram=trim noload=firefox',
        },
        {
            'id': 'resume', 'base_mode': 'resume', 'enabled': True,
            'default': False, 'title': 'Continue MiniOS', 'kernel_args': '',
        },
        {
            'id': 'choose', 'base_mode': 'choose', 'enabled': False,
            'default': False, 'title': None, 'kernel_args': '',
        },
        {
            'id': 'fresh', 'base_mode': 'fresh', 'enabled': True,
            'default': False, 'title': None, 'kernel_args': '',
        },
        {
            'id': 'new', 'base_mode': 'new', 'enabled': True,
            'default': False, 'title': None, 'kernel_args': '',
        },
        {
            'id': 'safe-graphics', 'base_mode': 'fresh', 'enabled': True,
            'default': False, 'title': 'Safe graphics',
            'kernel_args': 'nomodeset',
        },
    ]

def _five_entry_grub_payload():
    rows = ['set default=0\n', 'set timeout=10\n']
    details = (
        ('resume', 'resume', 'perchdir=resume'),
        ('new', 'new', 'perchdir=new'),
        ('choose', 'switch', 'perchdir=ask'),
        ('fresh', 'live', 'boot=live'),
        ('toram', 'ram', 'toram'),
    )
    for mode, class_name, arguments in details:
        rows.extend((
            'menuentry "{}" --class {} {{\n'.format(mode, class_name),
            '  linux /minios/boot/vmlinuz boot=live {}\n'.format(arguments),
            '}\n',
            '\n',
        ))
    return ''.join(rows).encode('utf-8')


def _five_entry_syslinux_payload():
    rows = ['TIMEOUT 100\n', 'DEFAULT default\n']
    details = (
        ('default', 'Resume', 'perchdir=resume'),
        ('perch', 'New', 'perchdir=new'),
        ('asksession', 'Choose', 'perchdir=ask'),
        ('live', 'Fresh', 'boot=live'),
        ('toram', 'RAM', 'toram'),
    )
    for label, title, arguments in details:
        rows.extend((
            'LABEL {}\n'.format(label),
            'MENU LABEL {}\n'.format(title),
            'KERNEL /minios/boot/vmlinuz\n',
            'APPEND boot=live {}\n'.format(arguments),
            '\n',
        ))
    return ''.join(rows).encode('ascii')


def test_custom_boot_menu_round_trips_and_validates_multilingual_titles(tmp_path):
    project = backend.ImageProject(
        project_base=str(tmp_path), source_backend='livekit',
        source_root_path=str(tmp_path), source_path=str(tmp_path),
        source_fingerprint='a' * 64,
        selected_source_modules=['00-core.sb'], menu_locale='en_US',
        boot_menu_entries=_custom_boot_menu())
    path = tmp_path / 'project.json'
    project.save(str(path))
    restored = backend.ImageProject.load(str(path))
    assert [dict(item) for item in restored.boot_menu_entries] == _custom_boot_menu()

    multilingual = _custom_boot_menu()
    multilingual[0]['title'] = 'RAM only'
    backend.ImageProject(
        project_base=str(tmp_path), source_backend='livekit',
        source_root_path=str(tmp_path), source_path=str(tmp_path),
        source_fingerprint='a' * 64,
        selected_source_modules=['00-core.sb'], menu_locale='multilang',
        boot_menu_entries=multilingual)
    multilingual[0]['title'] = 'Запуск из ОЗУ'
    with pytest.raises(ValueError, match='ASCII'):
        backend.ImageProject(
            project_base=str(tmp_path), source_backend='livekit',
            source_root_path=str(tmp_path), source_path=str(tmp_path),
            source_fingerprint='a' * 64,
            selected_source_modules=['00-core.sb'], menu_locale='multilang',
            boot_menu_entries=multilingual)

    with pytest.raises(ValueError, match='cannot be combined'):
        backend.ImageProject(
            project_base=str(tmp_path), source_backend='livekit',
            source_root_path=str(tmp_path), source_path=str(tmp_path),
            source_fingerprint='a' * 64,
            selected_source_modules=['00-core.sb'], menu_locale='en_US',
            default_boot='toram', boot_menu_entries=_custom_boot_menu())


def test_boot_menu_transform_creates_custom_entries_and_per_entry_parameters():
    entries = _custom_boot_menu()
    grub, _references, session = backend._transform_grub_payload(
        _five_entry_grub_payload(), 4, None, 'audit=1',
        boot_menu_entries=entries)
    text = grub.decode('utf-8')
    assert session is True
    assert text.index('Run entirely from RAM') < text.index('Continue MiniOS')
    assert '--class switch' not in text
    assert text.count('Safe graphics') == 1
    assert 'nomodeset' in text
    assert 'toram=trim noload=firefox' in text
    assert text.count(' audit=1') == 5
    assert 'set default=0' in text
    assert 'set timeout=4' in text

    syslinux, _references, session = backend._transform_syslinux_payload(
        _five_entry_syslinux_payload(), 4, None, 'audit=1',
        boot_menu_entries=entries, menu_locale='en_US')
    text = syslinux.decode('latin-1')
    assert session is True
    assert text.index('Run entirely from RAM') < text.index('Continue MiniOS')
    assert 'LABEL asksession' not in text
    assert 'LABEL safe-graphics' in text
    assert 'MENU LABEL Safe graphics' in text
    assert 'nomodeset' in text
    assert 'toram=trim noload=firefox' in text
    assert 'DEFAULT ram-trim' in text
    assert 'TIMEOUT 40' in text


def test_boot_menu_rejects_an_oversized_serialized_constructor():
    entries = []
    for index in range(20):
        entries.append({
            'id': 'entry-{}'.format(index), 'base_mode': 'fresh',
            'enabled': True, 'default': index == 0, 'title': None,
            'kernel_args': 'a' * 4096,
        })
    with pytest.raises(ValueError, match='65536 JSON bytes'):
        backend.validate_boot_menu_entries(entries)


def test_boot_menu_can_create_multiple_variants_from_one_template():
    entries = [
        {
            'id': 'normal', 'base_mode': 'fresh', 'enabled': True,
            'default': True, 'title': 'Normal boot', 'kernel_args': '',
        },
        {
            'id': 'safe', 'base_mode': 'fresh', 'enabled': True,
            'default': False, 'title': 'Safe graphics', 'kernel_args': 'nomodeset',
        },
    ]
    grub, _references, _session = backend._transform_grub_payload(
        _five_entry_grub_payload(), None, None, None,
        boot_menu_entries=entries)
    text = grub.decode('utf-8')
    assert text.count('--class live') == 2
    assert 'menuentry "Normal boot"' in text
    assert 'menuentry "Safe graphics"' in text
    assert 'nomodeset' in text

    syslinux, _references, _session = backend._transform_syslinux_payload(
        _five_entry_syslinux_payload(), None, None, None,
        boot_menu_entries=entries, menu_locale='en_US')
    text = syslinux.decode('latin-1')
    assert 'LABEL normal' in text
    assert 'LABEL safe' in text
    assert 'DEFAULT normal' in text
    assert 'nomodeset' in text


def test_russian_syslinux_custom_boot_title_uses_cp866_bytes():
    entries = _custom_boot_menu()
    entries[0]['title'] = 'Запуск из ОЗУ'
    syslinux, _references, _session = backend._transform_syslinux_payload(
        _five_entry_syslinux_payload(), None, None, None,
        boot_menu_entries=entries, menu_locale='ru_RU')
    assert 'Запуск из ОЗУ'.encode('cp866') in syslinux


def _source_with_bootloader(info, bootloader):
    metadata = backend._thaw(info.metadata)
    metadata['bootloader'] = bootloader
    return backend.SourceInfo(
        backend.SOURCE_SUPPORTED, backend=info.backend,
        root_path=info.root_path, source_path=info.source_path,
        media_category=info.media_category, fingerprint=info.fingerprint,
        metadata=metadata, modules=info.modules,
        active_external_modules=info.active_external_modules,
        diagnostics=info.diagnostics, collisions=info.collisions,
        total_bytes=info.total_bytes, non_module_bytes=info.non_module_bytes,
        input_manifest=info.input_manifest)


def test_source_boot_menu_imports_grub_settings_and_managed_arguments(tmp_path):
    unused_root, source, unused_mounts, unused_sys, unused_release, info = (
        _make_source(tmp_path))
    _write(
        source / 'boot' / 'grub' / 'grub.cfg',
        b'set default=1\nset timeout=7\n'
        b'menuentry "Resume MiniOS" --class resume {\n'
        b' linux /minios/boot/vmlinuz boot=live quiet audit=1 perchdir=resume\n}\n'
        b'menuentry "Safe fresh start" --class live {\n'
        b' linux /minios/boot/vmlinuz boot=live nomodeset audit=1\n}\n')
    refreshed = backend.discover_running_source(
        roots=((info.backend, info.root_path),),
        mounts_path=str(tmp_path / 'livekit-mounts'),
        sys_block_root=str(tmp_path / 'livekit-sys'),
        runtime_release_path=str(tmp_path / 'livekit-release'))
    result = backend.inspect_source_boot_menu(
        _source_with_bootloader(refreshed, 'grub-only'), 'en_US')

    assert result['timeout'] == 7
    assert result['default_known'] is True
    assert [item['base_mode'] for item in result['entries']] == [
        'resume', 'fresh']
    assert [item['title'] for item in result['entries']] == [
        'Resume MiniOS', 'Safe fresh start']
    assert [item['kernel_args'] for item in result['entries']] == [
        'quiet', 'nomodeset']
    assert result['entries'][1]['default'] is True
    assert all(item['kernel_args_schema'] == 2
               for item in result['entries'])


def test_source_boot_menu_imports_native_syslinux_default_and_timeout(tmp_path):
    unused_root, source, unused_mounts, unused_sys, unused_release, unused_info = (
        _make_source(tmp_path))
    syslinux = (
        b'TIMEOUT 30\nDEFAULT resume-id\nONTIMEOUT resume-id\n'
        b'LABEL fresh-id\nMENU LABEL Fresh source\n'
        b'KERNEL /minios/boot/vmlinuz\nAPPEND boot=live debug mystery=1\n'
        b'LABEL resume-id\nMENU DEFAULT\nMENU LABEL Continue source\n'
        b'KERNEL /minios/boot/vmlinuz\n'
        b'APPEND boot=live quiet mystery=1 perchdir=resume\n')
    _write(source / 'boot' / 'syslinux' / 'syslinux.cfg', syslinux)
    _write(
        source / 'boot' / 'grub' / 'grub.multilang.cfg',
        b'set timeout=3\nset default=1\n'
        b'menuentry "Fresh source" --class live {\n'
        b' linux /minios/boot/vmlinuz boot=live debug mystery=1\n}\n'
        b'menuentry "Continue source" --class resume {\n'
        b' linux /minios/boot/vmlinuz boot=live quiet mystery=1 perchdir=resume\n}\n')
    info = backend.discover_running_source(
        roots=(('livekit', str(tmp_path / 'livekit-root')),),
        mounts_path=str(tmp_path / 'livekit-mounts'),
        sys_block_root=str(tmp_path / 'livekit-sys'),
        runtime_release_path=str(tmp_path / 'livekit-release'))
    result = backend.inspect_source_boot_menu(info, 'multilang')

    assert result['bootloader'] == 'syslinux'
    assert result['timeout'] == 3
    assert [item['base_mode'] for item in result['entries']] == [
        'fresh', 'resume']
    assert all(item['title'] is None for item in result['entries'])
    assert all(item['kernel_args_schema'] == 3
               for item in result['entries'])
    assert result['entries'][1]['default'] is True
    assert result['entries'][0]['kernel_args'] == 'debug'


def test_imported_menu_replaces_managed_args_and_keeps_unknown_source_args():
    entries = [{
        'id': 'fresh', 'base_mode': 'fresh', 'enabled': True,
        'default': True, 'title': None, 'kernel_args': 'nomodeset',
        'kernel_args_schema': 2,
    }]
    payload = (
        b'set default=0\nmenuentry "Fresh" --class live {\n'
        b' linux /vmlinuz boot=live quiet debug audit=1 mystery=keep\n}\n')
    transformed, unused_references, unused_session = (
        backend._transform_grub_payload(
            payload, None, None, None, boot_menu_entries=entries))
    text = transformed.decode('utf-8')
    assert ' quiet' not in text
    assert ' debug' not in text
    assert text.count(' nomodeset') == 1
    assert 'boot=live' in text
    assert 'audit=1 mystery=keep' in text


def test_old_boot_menu_entry_schema_keeps_append_semantics():
    entries = [{
        'id': 'fresh', 'base_mode': 'fresh', 'enabled': True,
        'default': True, 'title': None, 'kernel_args': 'nomodeset',
    }]
    normalized = backend.validate_boot_menu_entries(entries)
    assert 'kernel_args_schema' not in normalized[0]
    payload = (
        b'set default=0\nmenuentry "Fresh" --class live {\n'
        b' linux /vmlinuz boot=live quiet audit=1\n}\n')
    transformed, unused_references, unused_session = (
        backend._transform_grub_payload(
            payload, None, None, None, boot_menu_entries=entries))
    text = transformed.decode('utf-8')
    assert 'boot=live quiet audit=1 nomodeset' in text


def test_multilingual_imported_menu_preserves_source_locale_arguments():
    entries = [{
        'id': 'fresh', 'base_mode': 'fresh', 'enabled': True,
        'default': True, 'title': None, 'kernel_args': 'nomodeset',
        'kernel_args_schema': 3,
    }]
    payload = (
        b'set default=0\nmenuentry "Fresh" --class live {\n'
        b' linux /vmlinuz boot=live quiet locales=ru_RU.UTF-8 '
        b'timezone=Europe/Moscow keyboard-layouts=us,ru audit=1\n}\n')
    transformed, unused_references, unused_session = (
        backend._transform_grub_payload(
            payload, None, None, None, boot_menu_entries=entries))
    text = transformed.decode('utf-8')
    assert ' quiet' not in text
    assert ' nomodeset' in text
    assert 'locales=ru_RU.UTF-8' in text
    assert 'timezone=Europe/Moscow' in text
    assert 'keyboard-layouts=us,ru' in text
    assert 'audit=1' in text
