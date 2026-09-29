"""Test fixture orchestration with fake processes, not PostgreSQL acceptance.

Load only the fixture definitions so these lifecycle checks require no database
client library. Real PostgreSQL integration remains in the full test suite.
"""

import ast
import inspect
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def fixture_function(path, name, namespace):
    module = ast.parse(path.read_text())
    node = next(item for item in module.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name)
    node.decorator_list = []
    code = compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec')
    exec(code, namespace)
    return namespace[name]


def test_private_cluster_connects_as_the_explicit_initdb_user(tmp_path, monkeypatch):
    monkeypatch.delenv('MSG_TEST_POSTGRES_URL_TEMPLATE', raising=False)
    calls = []
    created = tmp_path / 'cluster'

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'initdb':
            Path(command[command.index('-D') + 1]).mkdir()
        return SimpleNamespace(returncode=0)

    def make_directory(**kwargs):
        created.mkdir()
        return str(created)

    namespace = {
        'os': os, 'Path': Path, 'quote': quote, 'shutil': shutil,
        'tempfile': SimpleNamespace(mkdtemp=make_directory),
        'subprocess': SimpleNamespace(run=run),
    }
    fixture = fixture_function(ROOT / 'conftest.py', 'pg_cluster', namespace)
    generator = fixture()
    try:
        dsn = next(generator)
        init = calls[0]
        assert '-U' in init, 'initdb must create an explicit disposable role'
        assert urlsplit(dsn).username == init[init.index('-U') + 1]
        assert '-h' not in init
    finally:
        generator.close()
    assert not created.exists()
    assert calls[-1][-1] == 'stop'


def fixture_namespace(tmp_path, *, fail_load=False, fail_provision=False):
    seed = tmp_path / 'seed'
    (seed / 'etc').mkdir(parents=True)
    (seed / 'data').mkdir()
    (seed / 'etc/msgd.toml').write_text('seed configuration\n')
    calls = []

    class App:
        def __init__(self, settings, **kwargs):
            self.settings = settings

        async def load(self):
            calls.append('load')
            if fail_load:
                raise RuntimeError('injected load failure')

        async def close(self):
            calls.append('close')

    async def provision(*args):
        calls.append('provision')
        if fail_provision:
            raise RuntimeError('injected provision failure')
        return 'csr', 'root'

    async def approve(*args, **kwargs):
        calls.append('approve')

    namespace = {
        'Application': App,
        'NOW': datetime(2026, 9, 27, tzinfo=UTC),
        'uuid': SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='fixture')),
        'shutil': shutil,
        '_provision': provision,
        '_approve_csr': approve,
        'write_example': lambda *args, **kwargs: 'settings',
        'load_settings': lambda *args: 'settings',
        '_create_database': lambda *args, **kwargs: calls.append('create'),
        '_drop_database': lambda *args: calls.append('drop'),
    }
    return namespace, calls, seed


async def test_seed_database_is_released_after_the_fixture_session(tmp_path):
    namespace, calls, seed = fixture_namespace(tmp_path)
    fixture = fixture_function(ROOT / 'tests/conftest.py', 'installation_seed', namespace)
    assert inspect.isasyncgenfunction(fixture), 'seed must own its teardown lifecycle'
    generator = fixture(SimpleNamespace(mktemp=lambda *args: seed), 'postgresql://test/{database}')
    value = await anext(generator)
    assert value[2] == 'msg_seed_fixture'
    assert calls == ['create', 'provision', 'approve', 'close']
    await generator.aclose()
    assert calls[-1] == 'drop'


async def test_seed_setup_failure_closes_app_and_drops_database(tmp_path):
    namespace, calls, seed = fixture_namespace(tmp_path, fail_provision=True)
    fixture = fixture_function(ROOT / 'tests/conftest.py', 'installation_seed', namespace)
    assert inspect.isasyncgenfunction(fixture), 'seed must own its teardown lifecycle'
    generator = fixture(SimpleNamespace(mktemp=lambda *args: seed), 'postgresql://test/{database}')
    with pytest.raises(RuntimeError, match='injected provision failure'):
        await anext(generator)
    assert calls == ['create', 'provision', 'close', 'drop']


async def test_install_load_failure_closes_app_and_drops_database(tmp_path):
    namespace, calls, seed = fixture_namespace(tmp_path, fail_load=True)
    fixture = fixture_function(ROOT / 'tests/conftest.py', 'installed', namespace)
    work = tmp_path / 'work'
    work.mkdir()
    generator = fixture(work, (seed, 'root', 'seed-db', 'seed-dsn'), 'postgresql://test/{database}')
    with pytest.raises(RuntimeError, match='injected load failure'):
        await anext(generator)
    assert calls == ['create', 'load', 'close', 'drop']
