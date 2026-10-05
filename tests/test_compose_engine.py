import importlib.util
import hashlib
import json
import os
from types import SimpleNamespace

import image_project
import pytest


ENGINE_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'cli', 'lib',
    'minios_image_compose_engine.py'))
SPEC = importlib.util.spec_from_file_location('minios_image_compose_engine',
                                              ENGINE_PATH)
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


@pytest.mark.parametrize('ram', ('toram', 'toram=trim', 'toram=full'))
def test_ram_policy_does_not_conflict_with_session_selection(ram):
    arguments = 'boot=live perchdir=resume {} perchtoram=full'.format(ram)
    assert engine.boot_semantic(arguments) == 'resume'
    assert image_project._boot_semantic(arguments) == 'resume'
    for transform in (engine.kernel_arguments_for_base,
                      image_project._kernel_arguments_for_base):
        changed = transform(arguments, 'choose')
        assert 'perchdir=ask' in changed.split()
        assert ram in changed.split()
        assert 'perchtoram=full' in changed.split()


def test_constructor_keeps_perchtoram_as_an_independent_argument():
    assert image_project._constructor_arguments(
        'boot=live perchdir=resume perchtoram=trim') == 'perchtoram=trim'
    for value in ('trim', 'full', 'off'):
        assert engine.managed_boot_argument('perchtoram=' + value)
        assert image_project._managed_boot_argument('perchtoram=' + value)
    assert not engine.managed_boot_argument('perchtoram=invalid')


def test_readonly_module_snapshot_reuses_original_path(tmp_path, monkeypatch):
    module = tmp_path / '01-kernel.sb'
    module.write_bytes(b'module-bytes-must-not-be-read')
    readonly_flag = getattr(os, 'ST_RDONLY', 1)
    monkeypatch.setattr(
        engine.os, 'fstatvfs',
        lambda descriptor: SimpleNamespace(f_flag=readonly_flag))

    def unexpected_hash(*args, **kwargs):
        raise AssertionError('read-only module was content-hashed')

    monkeypatch.setattr(engine, 'hash_fd', unexpected_hash)
    record = engine.input_record(
        os.fsencode(str(module)), allow_readonly_metadata=True)
    assert record['integrity'] == 'readonly-metadata'
    assert record['sha256'] is None

    records = tmp_path / 'records.json'
    records.write_text(json.dumps([record]), encoding='utf-8')
    sources = tmp_path / 'sources.list'
    sources.write_bytes(os.fsencode(str(module)) + b'\0')
    snapshots = tmp_path / 'snapshots'
    mapping = tmp_path / 'mapping'

    engine.snapshot_inputs(
        str(records), str(sources), str(snapshots), str(mapping))

    assert mapping.read_bytes() == (
        os.fsencode(str(module)) + b'\0' +
        os.fsencode(str(module)) + b'\0')
    assert list(snapshots.iterdir()) == []


@pytest.mark.parametrize('kind,payload', [
    ('grub', b'menuentry "$language" --hotkey=f2 --id minios-language {\n'
     b' configfile /minios/boot/grub/languages.cfg\n}\n'
     b'menuentry "$help" --hotkey=f1 --id minios-help {\n'
     b' echo $"F2 changes the menu and system language."\n read answer\n}\n'),
    ('syslinux', ('MENU TABMSG [F1] Справка [F2] Язык [Tab] Параметры\n'
     'MENU HIDDENKEY F2 minios-language\nF1 help/modes_ru_RU.txt\n'
     'LABEL default\nMENU LABEL Запустить MiniOS\nAPPEND perchdir=resume\n'
     'LABEL minios-language\nMENU HIDE\nCONFIG lang/select_ru_RU.cfg\n').encode('cp866')),
    ('theme', 'text = "[F1] Справка [F2] Язык [E] Параметры"\n'.encode('utf-8')),
    ('help', b'MiniOS\nF2 changes the menu language, keyboard\nand time zone.\n'
     b'Tab edits parameters.\n\nPress any key.\n'),
])
@pytest.mark.parametrize('bracketed', [False, True])
def test_single_language_resources_preserve_help_and_encodings(kind, payload, bracketed):
    if bracketed:
        payload = payload.replace(b'F2 changes', b'[F2] changes')
    expected = image_project._single_language_boot_payload(payload, kind)
    assert engine.single_language_boot_payload(payload, kind) == expected
    assert b'F2' not in expected
    assert b'minios-language' not in expected
    if kind in ('syslinux', 'theme'):
        assert b'[F1]' in expected
    if kind == 'syslinux':
        assert 'Запустить MiniOS'.encode('cp866') in expected
        assert b'perchdir=resume' in expected
    if kind == 'grub':
        assert b'--hotkey=f1' in expected and b'read answer' in expected
    if kind == 'help':
        assert b'Tab edits' in expected and b'time zone' not in expected


def test_new_single_language_menu_planning_matches_composer(tmp_path):
    import hashlib

    source = tmp_path / 'source'
    grub = source / 'boot' / 'grub'
    syslinux = source / 'boot' / 'syslinux'
    (grub / 'po').mkdir(parents=True)
    (syslinux / 'lang').mkdir(parents=True)
    modes = (
        ('Start MiniOS', 'resume', 'default', 'perchdir=resume'),
        ('Start a new session', 'new', 'perch', 'perchdir=setup'),
        ('Choose a saved session', 'switch', 'asksession', 'perchdir=ask'),
        ('Start without saving', 'live', 'live', ''),
        ('Run from RAM', 'ram', 'toram', 'toram'),
    )
    template = 'set default=0\nset timeout=10\n'
    syslinux_text = ('DEFAULT default\nTIMEOUT 100\n'
                     'MENU HIDDENKEY F2 minios-language\n')
    for title, style, label, arguments in modes:
        template += ('menuentry "{}" --class {} {{\n'
                     ' linux /minios/boot/vmlinuz boot=live {}\n}}\n').format(
                         title, style, arguments)
        syslinux_text += ('LABEL {}\nMENU LABEL {}\n'
                         'KERNEL /minios/boot/vmlinuz\n'
                         'APPEND boot=live {}\n').format(label, title, arguments)
    template += 'source /minios/boot/grub/navigation.cfg\n'
    syslinux_text += 'LABEL minios-language\nMENU HIDE\nCONFIG lang/select_ru_RU.cfg\n'
    (grub / 'grub.cfg').write_text(template)
    (grub / 'grub.template.cfg').write_text(template)
    (grub / 'po' / 'ru_RU.po').write_text(
        '\n\n'.join('msgid "{}"\nmsgstr "RU {}"'.format(mode[0], mode[0])
                    for mode in modes), encoding='utf-8')
    navigation = (
        'menuentry "$language" --hotkey=f2 --id minios-language {\n'
        ' configfile /minios/boot/grub/languages.cfg\n}\n'
        'menuentry " " --id minios-separator {\n true\n}\n'
        'menuentry "$help" --hotkey=f1 --id minios-help {\n read answer\n}\n')
    (grub / 'navigation.cfg').write_text(navigation)
    (syslinux / 'lang' / 'ru_RU.cfg').write_text(syslinux_text)
    included = [str(path.relative_to(source)) for path in source.rglob('*') if path.is_file()]
    mapping, roots = image_project._effective_boot_config_mapping(
        str(source), 'syslinux-native', 'ru_RU', included)
    root = mapping['minios/boot/grub/grub.cfg'][0].decode('utf-8')
    assert 'set lang=ru_RU\nexport lang\n' in root
    for title, unused_style, unused_label, unused_args in modes:
        assert 'menuentry "RU {}"'.format(title) in root
    expected_records, expected_payloads = image_project._expected_boot_customization_records(
        str(source), 'syslinux-native', 'ru_RU', included, 7, 'toram', 'audit=1')
    plan_mapping = []
    for index, (target, (payload, kind)) in enumerate(sorted(mapping.items())):
        path = tmp_path / ('input-' + str(index))
        path.write_bytes(payload)
        plan_mapping.append(dict(target=target, source=str(path), kind=kind,
                                 source_size=len(payload), source_sha256=hashlib.sha256(payload).hexdigest()))
    records, outputs = engine.execute_boot_plan(dict(
        mapping=plan_mapping, roots=list(roots), timeout=7, default_boot='toram',
        kernel_args='audit=1', menu_locale='ru_RU'), str(tmp_path / 'outputs'))
    assert dict((record['target'], record['sha256']) for record in records) == dict(
        (record['target'], record['sha256']) for record in expected_records)
    for target, path in outputs:
        with open(path, 'rb') as stream:
            assert stream.read() == expected_payloads[target]
    help_payload = expected_payloads['minios/boot/grub/navigation.cfg']
    assert b'--hotkey=f1' in help_payload and b'F2' not in help_payload
    assert b'set timeout=7' not in help_payload
    assert (grub / 'navigation.cfg').read_text() == navigation


@pytest.mark.parametrize('bootloader', ['grub-only', 'syslinux-native'])
@pytest.mark.parametrize('locale', ['multilang', 'ru_RU'])
@pytest.mark.parametrize('timeout', [0, 7, 300])
def test_f2_navigation_cycles_allow_timeout_changes(tmp_path, bootloader, locale, timeout):
    source = tmp_path / 'source'
    grub = source / 'boot/grub'
    syslinux = source / 'boot/syslinux'
    grub.mkdir(parents=True)
    (syslinux / 'lang').mkdir(parents=True)
    main = (
        'set default=0\nset timeout=10\n'
        'menuentry "MiniOS" --class live {\n'
        ' linux /minios/boot/vmlinuz boot=live\n}\n'
        'source /minios/boot/grub/navigation.cfg\n')
    navigation = (
        'menuentry "Language" --id minios-language --hotkey=f2 {\n'
        ' configfile /minios/boot/grub/languages.cfg\n}\n'
        'menuentry "Help" --id minios-help --hotkey=f1 {\n read answer\n}\n')
    languages = (
        'set default="en_US"\nset timeout=-1\n'
        'menuentry "English" --id en_US {\n'
        ' configfile /minios/boot/grub/main.cfg\n}\n')
    for name, text in [('grub.cfg', 'configfile /minios/boot/grub/main.cfg\n'),
                       ('main.cfg', main), ('navigation.cfg', navigation),
                       ('languages.cfg', languages)]:
        (grub / name).write_text(text)
    native_menu = (
        'DEFAULT live\nTIMEOUT 100\n'
        'LABEL live\nKERNEL /minios/boot/vmlinuz\nAPPEND boot=live\n'
        'LABEL minios-language\nCONFIG lang/select.cfg\n')
    native_selector = (
        'TIMEOUT 0\nLABEL english\nCONFIG lang/en_US-interactive.cfg\n')
    (syslinux / 'syslinux.cfg').write_text(native_menu)
    (syslinux / 'lang/ru_RU.cfg').write_text(native_menu)
    (syslinux / 'lang/select.cfg').write_text(native_selector)
    (syslinux / 'lang/en_US-interactive.cfg').write_text(
        native_menu.replace('TIMEOUT 100', 'TIMEOUT 0'))
    included = [str(p.relative_to(source)) for p in source.rglob('*.cfg')]
    info = image_project.SourceInfo(
        image_project.SOURCE_SUPPORTED, source_path=str(source),
        metadata={'bootloader': bootloader},
        input_manifest=[{'relative_path': path} for path in included])
    imported = image_project.inspect_source_boot_menu(info, locale)
    assert imported['entries'][0]['base_mode'] == 'fresh'
    expected, payloads = image_project._expected_boot_customization_records(
        str(source), bootloader, locale, included, timeout, 'fresh', '')
    assert ('set timeout={}\n'.format(timeout).encode() in
            payloads['minios/boot/grub/main.cfg'])
    if locale == 'multilang':
        assert payloads['minios/boot/grub/languages.cfg'] == languages.encode()
        assert payloads['minios/boot/grub/navigation.cfg'] == navigation.encode()
        if bootloader == 'syslinux-native':
            assert payloads['minios/boot/syslinux/lang/select.cfg'] == native_selector.encode()
            assert b'TIMEOUT 0\n' in payloads['minios/boot/syslinux/lang/en_US-interactive.cfg']
    mapping, roots = image_project._effective_boot_config_mapping(
        str(source), bootloader, locale, included)
    inputs = []
    for index, (target, (data, kind)) in enumerate(mapping.items()):
        path = tmp_path / ('input-{}'.format(index))
        path.write_bytes(data)
        inputs.append(dict(target=target, source=str(path), kind=kind,
                           source_size=len(data), source_sha256=hashlib.sha256(data).hexdigest()))
    records, outputs = engine.execute_boot_plan(dict(
        mapping=inputs, roots=list(roots), timeout=timeout,
        default_boot='fresh', kernel_args='', menu_locale=locale), str(tmp_path / 'outputs'))
    assert {item['target']: item['sha256'] for item in records} == {
        item['target']: item['sha256'] for item in expected}
    for target, path in outputs:
        with open(path, 'rb') as stream:
            assert stream.read() == payloads[target]


@pytest.mark.parametrize('kind', ['grub', 'syslinux'])
def test_real_include_cycles_are_rejected_even_with_parallel_navigation(kind):
    base = 'minios/boot/{}/'.format(kind)
    if kind == 'grub':
        first = b'menuentry "Navigate" {\n configfile b.cfg\n}\nsource b.cfg\n'
        second = b'source a.cfg\n'
    else:
        first = b'LABEL navigate\nCONFIG b.cfg\nINCLUDE b.cfg\n'
        second = b'INCLUDE a.cfg\n'
    mapping = {base + 'a.cfg': (first, kind), base + 'b.cfg': (second, kind)}
    with pytest.raises(image_project.ImageProjectError, match='cycle'):
        image_project._validate_boot_reference_graph(mapping, [base + 'a.cfg'])
    with pytest.raises(engine.AdapterError, match='cycle'):
        engine.validate_boot_reference_graph(mapping, [base + 'a.cfg'])


def test_interactive_grub_branch_keeps_disabled_timeout():
    payload = (
        b'if [ "$minios_interactive" = "1" ]; then\n set timeout=-1\n'
        b'else\n set timeout=10\nfi\n'
        b'menuentry "MiniOS" --class live {\n'
        b' linux /minios/boot/vmlinuz boot=live\n}\n')
    predicted, _refs, _session = image_project._transform_grub_payload(payload, 7, None, '')
    actual, _refs, _session, _count = engine.transform_grub(payload, 7, None, '')
    assert actual == predicted
    assert b'set timeout=-1\n' in actual and b'set timeout=7\n' in actual
