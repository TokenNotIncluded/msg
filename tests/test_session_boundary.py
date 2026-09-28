"""The same bounded query and domain contract on real SQLite and PostgreSQL."""
import asyncio
import importlib
import importlib.util
from pathlib import Path
import subprocess
import sys
from typing import get_type_hints

import pytest

from msg.core.errors import Failure
from msg.core.contracts import MetadataSession
from msg.market.policy import DEFAULT_POLICY, load_policy
from msg.storage.postgres import PostgresMetadataStore, PostgresSession
from msg.storage.sqlite import SqliteMetadataStore, SqliteSession


@pytest.fixture(params=['sqlite', 'postgres'])
async def store(request, tmp_path):
    value = (PostgresMetadataStore(request.getfixturevalue('pg_dsn'))
             if request.param == 'postgres' else SqliteMetadataStore(tmp_path / 'metadata.db'))
    yield value
    await value.close()


def test_sessions_are_siblings_and_common_base_does_not_own_a_driver():
    assert not issubclass(PostgresSession, SqliteSession)
    assert importlib.util.find_spec('msg.storage.session') is not None
    common = importlib.import_module('msg.storage.session').RelationalSession
    assert issubclass(PostgresSession, common) and issubclass(SqliteSession, common)
    code = """
import importlib.abc
import sys
class NoSQLite(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'msg.storage.sqlite', 'sqlite3'}:
            raise AssertionError('production storage imported ' + fullname)
sys.meta_path.insert(0, NoSQLite())
from msg.storage.postgres import PostgresSession
"""
    child = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
    source = Path(importlib.import_module('msg.storage.session').__file__).read_text()
    assert 'import sqlite3' not in source and 'import psycopg' not in source
    assert '_connection' not in source


def test_declared_session_covers_real_query_consumer_without_commit_rights():
    assert importlib.util.find_spec('msg.core.query') is not None
    query = importlib.import_module('msg.core.query')
    assert get_type_hints(load_policy)['tx'] is query.QuerySession
    for name in ('execute', 'one', 'rows', 'setting', 'set_setting'):
        assert hasattr(MetadataSession, name), name
        assert hasattr(query.QuerySession, name), name
    for name in ('connection', 'commit', 'rollback'):
        assert not hasattr(MetadataSession, name)
        assert not hasattr(query.QuerySession, name)


async def test_query_result_is_a_guarded_view_not_a_driver_cursor(store):
    async with store.transaction(write=False) as tx:
        result = tx.execute('SELECT 1')
        assert not hasattr(result, 'connection')
        assert not hasattr(result, 'commit')
        assert result.fetchone() == (1,)
        assert result.fetchone() is None
        assert list(tx.execute('SELECT 2 UNION ALL SELECT 3')) == [(2,), (3,)]
        assert tx.rows('SELECT 4') == [(4,)]
        assert tx.one('SELECT 5') == (5,)
    with pytest.raises(Failure, match='transaction_closed'):
        result.fetchall()


async def test_query_result_and_session_refuse_cross_task_and_read_only_writes(store):
    async with store.transaction(write=False) as tx:
        result = tx.execute('SELECT 1')
        async def misuse_result():
            with pytest.raises(Failure, match='transaction_cross_task'):
                result.fetchone()
        async def misuse_session():
            with pytest.raises(Failure, match='transaction_cross_task'):
                tx.one('SELECT 1')
        await asyncio.gather(misuse_result(), misuse_session())
        with pytest.raises(Failure, match='read_only_transaction'):
            tx.set_setting('forbidden', True)
        assert result.fetchone() == (1,)


async def test_declared_policy_consumer_and_nested_rollback_work_on_both_backends(store):
    assert importlib.util.find_spec('msg.core.query') is not None
    query = importlib.import_module('msg.core.query')
    async with store.transaction(write=True) as tx:
        assert isinstance(tx, query.QuerySession)
        assert load_policy(tx, DEFAULT_POLICY['id']) == DEFAULT_POLICY
        tx.set_setting('outer', 'keep')
        with pytest.raises(ValueError, match='abort inner'):
            async with store.transaction(write=True) as child:
                child.set_setting('inner', 'drop')
                raise ValueError('abort inner')
        assert tx.setting('inner') is None
        tx.set_setting('after-inner', True)
    async with store.transaction(write=False) as tx:
        assert tx.setting('outer') == 'keep'
        assert tx.setting('after-inner') is True
        assert tx.setting('inner') is None
