"""Rollback compensation remains serialized with real independent writers."""
import asyncio
import sqlite3

import psycopg
import pytest

from msg.core.errors import Failure
from msg.storage.postgres import PostgresMetadataStore
from msg.storage.sqlite import SqliteMetadataStore


@pytest.fixture(params=['sqlite', 'postgres'])
def stores(request, tmp_path):
    if request.param == 'postgres':
        dsn = request.getfixturevalue('pg_dsn')
        return (PostgresMetadataStore(dsn),
                PostgresMetadataStore(dsn, initialize=False), request.param)
    path = tmp_path / 'fence.db'
    return SqliteMetadataStore(path), SqliteMetadataStore(path), request.param


@pytest.mark.parametrize('sql_failure', [False, True])
async def test_rollback_compensation_excludes_competing_writer(stores, tmp_path, sql_failure):
    first, second, backend = stores
    compensating, release, attempted, entered = (asyncio.Event() for _ in range(4))
    pin = tmp_path / 'pin'
    pin.write_text('retained')
    observed = []

    async def restore():
        compensating.set()
        await release.wait()
        pin.write_text('retained')

    async def abort():
        async with first.transaction(write=True) as tx:
            tx.on_rollback(restore)
            pin.unlink()
            tx.set_setting('aborted', True)
            if sql_failure:
                # Exercise automatic SQLite transaction abort as well as PG's
                # INERROR state, not only an application exception before ROLLBACK.
                sql = ('INSERT OR ROLLBACK' if backend == 'sqlite' else 'INSERT')
                tx.execute(sql + ' INTO schema_version VALUES (1)', write=True)
            raise ValueError('original failure')

    async def compete():
        attempted.set()
        async with second.transaction(write=True) as tx:
            entered.set()
            observed.append(pin.exists())
            tx.set_setting('next-writer', True)

    first_task = asyncio.create_task(abort())
    second_task = None
    try:
        await asyncio.wait_for(compensating.wait(), 5)
        second_task = asyncio.create_task(compete())
        await asyncio.wait_for(attempted.wait(), 5)
        await asyncio.sleep(0.1)
        remained_blocked = not entered.is_set()
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(
            *([first_task, second_task] if second_task else [first_task]),
            return_exceptions=True), 5)
    if sql_failure:
        assert isinstance(results[0], Failure) and results[0].code == 'constraint_conflict', results
        expected_cause = sqlite3.IntegrityError if backend == 'sqlite' else psycopg.IntegrityError
        assert isinstance(results[0].__cause__, expected_cause), results
    else:
        assert isinstance(results[0], ValueError), results
    assert results[1] is None, results
    assert remained_blocked, 'writer entered before rollback compensation finished'
    assert observed == [True], 'writer saw a half-compensated external pin'
    async with second.transaction(write=False) as tx:
        assert tx.setting('aborted') is None
        assert tx.setting('next-writer') is True


@pytest.mark.parametrize('initial_cancel', [False, True])
async def test_repeated_cancellation_cannot_interrupt_compensation(stores, tmp_path, initial_cancel):
    store, other, _ = stores
    started, release = asyncio.Event(), asyncio.Event()
    pin = tmp_path / 'temporary-pin'
    original = asyncio.CancelledError('initial') if initial_cancel else ValueError('primary')

    async def compensate():
        started.set()
        await release.wait()
        pin.unlink()

    async def abort():
        async with store.transaction(write=True) as tx:
            tx.on_rollback(compensate)
            tx.set_setting('aborted', True)
            pin.write_text('uncommitted')
            raise original

    task = asyncio.create_task(abort())
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
    finally:
        release.set()
        result, = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    if initial_cancel:
        assert isinstance(result, asyncio.CancelledError)
    else:
        assert result is original
    assert not pin.exists(), 'cancellation abandoned external cleanup'
    async with other.transaction(write=True) as tx:
        assert tx.setting('aborted') is None
        tx.set_setting('lock-released', True)


async def test_cancelled_writer_wait_does_not_leak_the_fence(stores):
    first, second, _ = stores
    holding, release, attempted = (asyncio.Event() for _ in range(3))

    async def hold():
        async with first.transaction(write=True):
            holding.set()
            await release.wait()

    async def wait():
        attempted.set()
        async with second.transaction(write=True):
            pytest.fail('cancelled writer entered')

    holder = asyncio.create_task(hold())
    waiter = None
    try:
        await asyncio.wait_for(holding.wait(), 5)
        waiter = asyncio.create_task(wait())
        await asyncio.wait_for(attempted.wait(), 5)
        await asyncio.sleep(0.05)
        waiter.cancel()
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(
            *([holder, waiter] if waiter else [holder]), return_exceptions=True), 5)
    assert results[0] is None
    assert isinstance(results[1], asyncio.CancelledError), results
    async with second.transaction(write=True) as tx:
        tx.set_setting('lock-released', True)


async def test_sqlite_fence_timeout_remains_retryable(tmp_path):
    first = SqliteMetadataStore(tmp_path / 'timeout.db')
    second = SqliteMetadataStore(first.path, busy_timeout=0.02)
    async with first.transaction(write=True):
        with pytest.raises(Failure) as captured:
            async with second.transaction(write=True):
                pytest.fail('writer entered while fence was held')
        assert captured.value.code == 'server_busy'
        assert captured.value.retryable
    async with second.transaction(write=True) as tx:
        tx.set_setting('retry', True)


async def test_cancelled_git_pin_finishes_before_rollback(stores, tmp_path, monkeypatch):
    from threading import Event

    from msg.storage.git import GitContentStore

    store, _, _ = stores
    contents = GitContentStore(tmp_path / 'content')
    blob = await contents.put_bytes(b'cancelled text upload', 'text/plain')
    started, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    original_run = contents._run

    def delayed_pin(*args, **kwargs):
        if args[0] == 'update-ref' and args[1] != '-d':
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise RuntimeError('pin test barrier timed out')
        return original_run(*args, **kwargs)

    monkeypatch.setattr(contents, '_run', delayed_pin)

    async def upload():
        async with store.transaction(write=True) as tx:
            tx.on_rollback(lambda: contents.unpin(blob, 'cancelled-upload'))
            await contents.pin(blob, 'cancelled-upload')

    task = asyncio.create_task(upload())
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0.05)
        waited_for_io = not task.done()
    finally:
        release.set()
        result, = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    assert isinstance(result, asyncio.CancelledError), result
    assert waited_for_io, 'rollback overtook a still-running Git pin write'
    assert not await contents.pinned(blob, 'cancelled-upload')
