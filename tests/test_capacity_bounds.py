"""Deployment caps reject unbounded metadata and Git input without deleting user content."""

import asyncio
import io

import pytest

from msg.core.errors import Failure
from msg.storage.sqlite import SqliteMetadataStore


@pytest.mark.asyncio
async def test_transfer_rows_and_staged_bytes_are_capped(tmp_path, monkeypatch):
    from msg.storage import capacity

    monkeypatch.setattr(capacity, 'MAX_TRANSFERS', 1)
    monkeypatch.setattr(capacity, 'MAX_STAGED_CHUNK_BYTES', 4)
    store = SqliteMetadataStore(tmp_path / 'db')
    async with store.transaction(write=True) as tx:
        capacity.require_transfer_capacity(tx, new_transfer=True)
        tx.execute(
            'INSERT INTO transfers VALUES (?,?,?,?,?)', ('tr_1', 'u', 0, '{}', '{}'), write=True
        )
        with pytest.raises(Failure, match='^storage_capacity_exceeded$'):
            capacity.require_transfer_capacity(tx, new_transfer=True)
        capacity.require_transfer_capacity(tx)
        tx.execute('INSERT INTO chunks VALUES (?,?,?,?)', ('tr_1', 0, 3, '{}'), write=True)
        capacity.require_transfer_capacity(tx, 1)
        with pytest.raises(Failure, match='^storage_capacity_exceeded$'):
            capacity.require_transfer_capacity(tx, 2)
    await store.close()


@pytest.mark.asyncio
async def test_purge_records_keep_the_newest_bounded_set(tmp_path, monkeypatch):
    from msg.storage import capacity

    monkeypatch.setattr(capacity, 'MAX_PURGE_RECORDS', 2)
    store = SqliteMetadataStore(tmp_path / 'db')
    async with store.transaction(write=True) as tx:
        for name, stamp in (
            ('a', '2020-01-01T00:00:00Z'),
            ('b', '2021-01-01T00:00:00Z'),
            ('c', '2022-01-01T00:00:00Z'),
        ):
            tx.set_setting('purge_record:' + name, {'time': stamp, 'reason': 'test'})
        capacity.trim_purge_records(tx)
        keys = {
            key
            for key, _value in tx.rows(
                "SELECT key, value FROM settings WHERE key LIKE 'purge_record:%'"
            )
        }
    await store.close()
    assert keys == {'purge_record:b', 'purge_record:c'}


@pytest.mark.asyncio
async def test_finished_webhook_jobs_yield_before_pending_work_is_refused(tmp_path, monkeypatch):
    from msg.storage import capacity

    monkeypatch.setattr(capacity, 'MAX_QUEUED_WEBHOOKS', 1)
    monkeypatch.setattr(capacity, 'MAX_WEBHOOK_ROWS', 2)
    store = SqliteMetadataStore(tmp_path / 'db')
    async with store.transaction(write=True) as tx:
        tx.execute(
            'INSERT INTO jobs VALUES (?,?,?,?,?,?)',
            ('j_done', 'd1', 'webhook', 'done', '2020-01-01T00:00:00Z', '{}'),
            write=True,
        )
        tx.execute(
            'INSERT INTO jobs VALUES (?,?,?,?,?,?)',
            ('j_fail', 'd2', 'webhook', 'failed', '2020-01-02T00:00:00Z', '{}'),
            write=True,
        )
        capacity.require_webhook_capacity(tx)
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='webhook'")[0] == 1
        tx.execute(
            'INSERT INTO jobs VALUES (?,?,?,?,?,?)',
            ('j_live', 'd3', 'webhook', 'pending', '2024-01-01T00:00:00Z', '{}'),
            write=True,
        )
        with pytest.raises(Failure, match='^storage_capacity_exceeded$'):
            capacity.require_webhook_capacity(tx)
        assert tx.one('SELECT id FROM jobs WHERE id=?', ('j_live',)) == ('j_live',)
    await store.close()


def test_git_repository_bytes_stop_at_the_deployment_cap(tmp_path, monkeypatch):
    from msg.extensions import repositories

    monkeypatch.setattr(repositories, 'MAX_GIT_REPOSITORY_BYTES', 10)
    monkeypatch.setattr(repositories, 'MAX_GIT_PACK_BYTES', 4)
    (tmp_path / 'pack').write_bytes(b'01234567')
    assert repositories.git_repository_bytes(tmp_path) == 8
    repositories.require_git_repository_capacity(tmp_path, incoming=2)
    with pytest.raises(Failure, match='^storage_capacity_exceeded$'):
        repositories.require_git_repository_capacity(tmp_path, incoming=4)


@pytest.mark.asyncio
async def test_ssh_receive_stdin_is_bounded():
    from msg.extensions.ssh_git import relay_bounded_stdin

    allowed = await asyncio.create_subprocess_exec(
        'cat', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    code = await relay_bounded_stdin(allowed, io.BytesIO(b'ab'), limit=3, timeout=5)
    assert code == 0 and await allowed.stdout.read() == b'ab'
    rejected = await asyncio.create_subprocess_exec(
        'cat', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    try:
        with pytest.raises(Failure, match='^request_too_large$'):
            await relay_bounded_stdin(rejected, io.BytesIO(b'abcdef'), limit=3, timeout=5)
    finally:
        if rejected.returncode is None:
            rejected.kill()
            await rejected.wait()


def test_error_status_maps_storage_capacity_to_507():
    from msg.transports.http_routes import error_status

    assert error_status('storage_capacity_exceeded') == 507
