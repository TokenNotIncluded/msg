"""Live PostgreSQL checks; set MSG_TEST_POSTGRES_DSN to a disposable database."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from msg.core.errors import Failure
from msg.core.models import (
    AuditEvent,
    EffectJob,
    Event,
    OperationResult,
    Principal,
    Resource,
    ResourceId,
)
from msg.storage.postgres import PostgresMetadataStore


def item(id='root', parent=None):
    now = datetime.now(UTC)
    return Resource(
        id=ResourceId(id),
        type='topic',
        type_version=1,
        name=id,
        parent=parent,
        owner=ResourceId('owner'),
        group=ResourceId('group'),
        mode=0o1777,
        generation=0,
        revision=None,
        state='active',
        created_at=now,
        created_by=ResourceId('owner'),
        modified_at=now,
        modified_by=ResourceId('owner'),
    )


async def test_postgres_session_contract_and_rollback(pg_dsn):
    store = PostgresMetadataStore(pg_dsn)
    with pytest.raises(RuntimeError):
        async with store.transaction(write=True) as tx:
            await tx.insert(item())
            raise RuntimeError('rollback')
    async with store.transaction(write=True) as tx:
        assert tx.one('SELECT COUNT(*) FROM resources')[0] == 0
        await tx.insert(item())
        await tx.insert(item('a', 'root'))
        await tx.insert(item('b', 'root'))
        assert [r.id for r in (await tx.children('root', limit=1)).items] == ['a']
        assert (await tx.children('root', limit=1)).next_cursor == 'a'
        with pytest.raises(RuntimeError):
            async with store.transaction(write=True) as nested:
                await nested.insert(item('nested', 'root'))
                raise RuntimeError('nested rollback')
        assert tx.one('SELECT id FROM resources WHERE id=?', ('nested',)) is None
        result = OperationResult(
            request_id='req', operation='test', status='ok', actor=None, subject=None
        )
        await tx.save_result('subject', 'digest', result)
        assert await tx.request_result('subject', 'req', 'digest') == result
        with pytest.raises(Failure, match='idempotency_conflict'):
            await tx.request_result('subject', 'req', 'different')
        await tx.replace(replace(await tx.resource('a'), generation=1, mode=0o700), 0)
    async with store.transaction(write=False) as tx:
        assert await tx.resolve('/a') == 'a'
        assert (await tx.resource('a')).mode == 0o700
        assert tx.rows("SELECT key,value FROM settings WHERE key LIKE 'policy:%'") == []
        with pytest.raises(Failure, match='read_only_transaction'):
            await tx.insert(item('nope', 'root'))
    await store.close()


async def test_postgres_audit_append_only_and_concurrent_writers(pg_dsn):
    first = PostgresMetadataStore(pg_dsn)
    second = PostgresMetadataStore(pg_dsn, initialize=False)
    holding = asyncio.Event()
    release = asyncio.Event()
    entered_second = asyncio.Event()

    async def writer_one():
        async with first.transaction(write=True) as tx:
            await tx.insert(item())
            holding.set()
            await release.wait()

    async def writer_two():
        await holding.wait()
        async with second.transaction(write=True) as tx:
            entered_second.set()
            await tx.insert(item('child', 'root'))

    a = asyncio.create_task(writer_one())
    b = asyncio.create_task(writer_two())
    await asyncio.wait_for(holding.wait(), 5)
    await asyncio.sleep(0.1)
    assert not entered_second.is_set()
    release.set()
    await asyncio.wait_for(asyncio.gather(a, b), 5)

    event = Event(
        id='e',
        type='test',
        time=datetime.now(UTC),
        request_id='req',
        actor=ResourceId('owner'),
        subject=ResourceId('owner'),
        resources=(),
        data={},
    )
    audit = AuditEvent(
        event=event,
        authority=(),
        before_digest=None,
        after_digest=None,
        previous_digest=None,
        entry_digest='',
        result='ok',
    )
    async with first.transaction(write=True) as tx:
        await tx.append_audit(audit)
        await tx.append_audit(replace(audit, event=replace(event, id='e2')))
        rows = tx.rows('SELECT digest,previous FROM audit ORDER BY seq')
        assert rows[0][1] is None
        assert rows[1][1] == rows[0][0]
    with pytest.raises(Failure, match='constraint_conflict'):
        async with first.transaction(write=True) as tx:
            tx.execute('DELETE FROM audit', write=True)
    await first.close()
    await second.close()


async def test_outbox_signal_only_after_commit_and_does_not_own_result(pg_dsn):
    class Signal:
        def __init__(self):
            self.seen = []
            self.fail = False

        async def publish_pending(self, ids):
            self.seen.append(tuple(ids))
            if self.fail:
                raise RuntimeError('valkey_offline')

    signal = Signal()
    store = PostgresMetadataStore(pg_dsn, signal=signal)
    now = datetime.now(UTC)
    principal = Principal(
        actor=ResourceId('owner'),
        subject=ResourceId('owner'),
        credential_id=None,
        method='local',
        certificates=(),
        ceiling=(),
    )

    def job(id):
        return EffectJob(
            id=id,
            event_id='event',
            kind='mail',
            dedupe_key=id,
            principal=principal,
            operation='mail.send',
            arguments={},
            state='pending',
            attempts=0,
            next_attempt_at=now,
            lease_until=None,
        )

    with pytest.raises(RuntimeError, match='rollback'):
        async with store.transaction(write=True) as tx:
            await tx.enqueue(job('rolled-back'))
            raise RuntimeError('rollback')
    assert signal.seen == []
    signal.fail = True
    async with store.transaction(write=True) as tx:
        await tx.enqueue(job('committed'))
    assert signal.seen == [('committed',)]
    async with store.transaction(write=False) as tx:
        assert (await tx.job('committed')).state == 'pending'
    await store.close()
