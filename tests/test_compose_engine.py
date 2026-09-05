import importlib.util
import json
import os
from types import SimpleNamespace


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
