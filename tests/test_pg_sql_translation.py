"""SQL dialect and backup regressions for the PostgreSQL adapter."""

import asyncio
import subprocess
import threading

import pytest
from psycopg.conninfo import make_conninfo

from msg.storage.postgres import PostgresMetadataStore, _postgres_sql


def test_sql_translation_protects_literals_and_comments():
    source = "SELECT '?', 'offset', '100%' FROM chunks -- ? offset\nWHERE offset=?"
    translated = _postgres_sql(source, has_parameters=True)
    assert translated == (
        "SELECT '?', 'offset', '100%%' FROM chunks -- ? offset\nWHERE \"offset\"=%s"
    )
    assert _postgres_sql('INSERT OR IGNORE INTO watches VALUES (?,?); ') == (
        'INSERT INTO watches VALUES (%s,%s) ON CONFLICT DO NOTHING; '
    )
    assert _postgres_sql('SELECT id FROM resources LIMIT 1 OFFSET 2') == (
        'SELECT id FROM resources LIMIT 1 OFFSET 2'
    )


async def test_live_sql_translation_and_duplicate_ignore(pg_dsn):
    store = PostgresMetadataStore(pg_dsn)
    async with store.transaction(write=True) as tx:
        tx.execute(
            'INSERT OR IGNORE INTO watches VALUES (?,?);', ('subject', 'resource'), write=True
        )
        tx.execute(
            'INSERT OR IGNORE INTO watches VALUES (?,?);', ('subject', 'resource'), write=True
        )
        assert tx.one('SELECT COUNT(*) FROM watches')[0] == 1
        tx.execute('INSERT INTO chunks VALUES (?,?,?,?)', ('transfer', 4, 3, '{}'), write=True)
        assert tx.one("SELECT '?', 'offset', '100%' FROM chunks WHERE offset=?", (4,)) == (
            '?',
            'offset',
            '100%',
        )
        assert tx.one('SELECT offset,length FROM chunks WHERE transfer_id=?', ('transfer',)) == (
            4,
            3,
        )


def test_pg_backup_is_custom_format_and_hides_password(pg_dsn, tmp_path, monkeypatch):
    store = PostgresMetadataStore(pg_dsn)
    archive = tmp_path / 'metadata.dump'
    store.backup(archive)
    listing = subprocess.run(
        ['pg_restore', '--list', str(archive)], capture_output=True, text=True, check=True
    ).stdout
    assert 'TABLE public resources' in listing
    assert 'TABLE public audit' in listing

    captured = {}

    def fake_run(command, **kwargs):
        captured['command'] = command
        captured['env'] = kwargs['env']

    protected = PostgresMetadataStore(make_conninfo(pg_dsn, password='topsecret'), initialize=False)
    monkeypatch.setattr('msg.storage.postgres.subprocess.run', fake_run)
    protected.backup(tmp_path / 'ignored.dump')
    assert 'topsecret' not in ' '.join(captured['command'])
    assert captured['env']['PGPASSWORD'] == 'topsecret'


async def test_cancelled_lock_wait_releases_connection(pg_dsn):
    first = PostgresMetadataStore(pg_dsn)
    second = PostgresMetadataStore(pg_dsn, initialize=False)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with first.transaction(write=True):
            holding.set()
            await release.wait()

    async def waiter():
        async with second.transaction(write=True):
            pytest.fail('cancelled waiter entered its transaction')

    holder_task = asyncio.create_task(holder())
    await asyncio.wait_for(holding.wait(), 5)
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    waiter_task.cancel()
    release.set()
    await asyncio.wait_for(holder_task, 5)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter_task, 5)
    async with second.transaction(write=True) as tx:
        assert tx.one('SELECT version FROM schema_version')[0] == 1


async def test_cancelled_connect_closes_late_connection(pg_dsn, monkeypatch):
    store = PostgresMetadataStore(pg_dsn, initialize=False)
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class LateConnection:
        def close(self):
            closed.set()

    def slow_connect():
        started.set()
        release.wait(5)
        return LateConnection()

    monkeypatch.setattr(store, '_connect', slow_connect)
    task = asyncio.create_task(store._connect_async())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert closed.is_set()
