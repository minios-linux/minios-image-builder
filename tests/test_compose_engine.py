import importlib.util
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
