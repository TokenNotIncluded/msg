"""One storage contract exercised independently against PostgreSQL and SQLite."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_postgres import item

from msg.core.contracts import MetadataSession, MetadataStore
from msg.core.errors import Failure
from msg.core.models import AuditEvent, EffectJob, Event, OperationResult, Principal


def test_postgres_import_does_not_load_a_sqlite_adapter_or_driver():
    code = """
import importlib.abc, sys
class NoSqlite(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'msg.storage.sqlite' or fullname == 'sqlite3' or fullname.startswith('sqlite3.'):
            raise AssertionError('PostgreSQL imported SQLite: ' + fullname)
sys.meta_path.insert(0, NoSqlite())
import msg.storage.postgres
assert 'msg.storage.sqlite' not in sys.modules
assert 'sqlite3' not in sys.modules
"""
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_backends_share_only_the_neutral_session_implementation():
    from msg.storage.postgres import PostgresSession
    from msg.storage.sqlite import SqliteSession

    common = importlib.import_module('msg.storage.session').RelationalSession
    assert not issubclass(PostgresSession, SqliteSession)
    assert PostgresSession.__bases__ == SqliteSession.__bases__ == (common,)
    for name in ('resource', 'children', 'save_result', 'append_audit', 'on_rollback', 'check'):
        assert (
            getattr(PostgresSession, name) is getattr(SqliteSession, name) is getattr(common, name)
        )
    assert PostgresSession.execute is not SqliteSession.execute
    assert 'execute' in common.__abstractmethods__


def test_shared_layer_does_not_own_connections_dialects_schemas_or_commits():
    module = importlib.import_module('msg.storage.session')
    text = Path(module.__file__).read_text()
    for forbidden in (
        'sqlite3',
        'psycopg',
        '_connection',
        'PRAGMA ',
        'CREATE TABLE ',
        'INSERT OR IGNORE',
        '.commit(',
        '.rollback(',
        'pg_advisory',
    ):
        assert forbidden not in text
    assert set(module.RelationalSession.__abstractmethods__) == {'execute'}
    assert set(inspect.signature(module.RelationalSession).parameters) == {'write'}


def test_shared_implementation_covers_the_declared_metadata_contract():
    common = importlib.import_module('msg.storage.session').RelationalSession
    for name, member in vars(MetadataSession).items():
        if not name.startswith('_') and callable(member):
            assert name in vars(common), name
            assert callable(getattr(common, name)), name
    assert 'transaction' in vars(MetadataStore)
    assert {
        'one',
        'rows',
        'check',
        'setting',
        'set_setting',
        'job',
        'save_job',
        'organization',
        'path',
        'ancestors',
        'run_rollback_effects',
    } <= set(vars(common))


@pytest.fixture(params=['postgres', 'sqlite'])
async def metadata(request, pg_dsn, tmp_path):
    from msg.storage.postgres import PostgresMetadataStore
    from msg.storage.sqlite import SqliteMetadataStore

    store = (
        PostgresMetadataStore(pg_dsn)
        if request.param == 'postgres'
        else SqliteMetadataStore(tmp_path / 'metadata.sqlite')
    )
    try:
        yield store
    finally:
        await store.close()


async def test_shared_resource_generation_path_and_paging(metadata):
    async with metadata.transaction(write=True) as tx:
        await tx.insert(item())
        await tx.insert(item('a', 'root'))
        await tx.insert(item('b', 'root'))
        assert await tx.resolve('/a') == 'a'
        assert await tx.path('a') == '/a'
        assert [r.id for r in await tx.ancestors('a')] == ['root']
        first = await tx.children('root', limit=1)
        assert [r.id for r in first.items] == ['a'] and first.next_cursor == 'a'
        assert [r.id for r in (await tx.children('root', cursor=first.next_cursor)).items] == ['b']
        old = await tx.resource('a')
        await tx.replace(replace(old, generation=1, mode=0o700), 0)
        assert tx.setting('authorization_epoch') == 1
        with pytest.raises(Failure, match='generation_conflict'):
            await tx.replace(replace(old, generation=1), 0)
    async with metadata.transaction(write=False) as tx:
        assert (await tx.resource('a')).generation == 1
        assert (await tx.resource('a')).mode == 0o700


async def test_shared_result_event_audit_and_job_roundtrips(metadata):
    now = datetime.now(UTC)
    principal = Principal(
        actor='owner',
        subject='owner',
        credential_id=None,
        method='local',
        certificates=(),
        ceiling=(),
    )
    result = OperationResult(
        request_id='once', operation='test', status='ok', actor=None, subject=None
    )
    event = Event(
        id='event-one',
        type='test',
        time=now,
        request_id='once',
        actor='owner',
        subject='owner',
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
    job = EffectJob(
        id='job-one',
        event_id=event.id,
        kind='mail',
        dedupe_key='unique-job',
        principal=principal,
        operation='mail.send',
        arguments={},
        state='pending',
        attempts=0,
        next_attempt_at=now,
        lease_until=None,
    )
    async with metadata.transaction(write=True) as tx:
        await tx.save_result('owner', 'digest-one', result)
        await tx.append_event(event)
        await tx.append_audit(audit)
        await tx.append_audit(replace(audit, event=replace(event, id='event-two')))
        await tx.enqueue(job)
    async with metadata.transaction(write=False) as tx:
        assert await tx.request_result('owner', 'once', 'digest-one') == result
        with pytest.raises(Failure, match='idempotency_conflict'):
            await tx.request_result('owner', 'once', 'another-digest')
        rows = tx.rows('SELECT digest,previous FROM audit ORDER BY seq')
        assert rows[0][1] is None and rows[1][1] == rows[0][0]
        assert await tx.job(job.id) == job
        assert tx.one('SELECT id FROM events WHERE id=?', (event.id,))[0] == event.id


async def test_shared_savepoints_and_compensation_keep_their_scope(metadata):
    effects = []

    async def compensate(value):
        effects.append(value)

    async with metadata.transaction(write=True) as outer:
        outer.set_setting('outer', True)
        outer.on_rollback(lambda: compensate('outer'))
        with pytest.raises(ValueError, match='inner'):
            async with metadata.transaction(write=True) as inner:
                assert inner is outer
                inner.set_setting('inner', True)
                inner.on_rollback(lambda: compensate('inner'))
                raise ValueError('inner')
        assert effects == ['inner'] and outer.setting('inner') is None
    assert effects == ['inner']
    with pytest.raises(ValueError, match='whole'):
        async with metadata.transaction(write=True) as tx:
            tx.set_setting('whole', True)
            tx.on_rollback(lambda: compensate('whole'))
            raise ValueError('whole')
    async with metadata.transaction(write=False) as tx:
        assert tx.setting('outer') is True and tx.setting('whole') is None
    assert effects == ['inner', 'whole']


async def test_shared_read_only_closed_and_cross_task_guards(metadata):
    async with metadata.transaction(write=False) as tx:
        with pytest.raises(Failure, match='read_only_transaction'):
            tx.set_setting('forbidden', True)
        with pytest.raises(Failure, match='read_only_transaction'):
            async with metadata.transaction(write=True):
                pytest.fail('read-only transaction was upgraded')

        async def other_task():
            tx.setting('anything')

        with pytest.raises(Failure, match='transaction_cross_task'):
            await asyncio.create_task(other_task())
    with pytest.raises(Failure, match='transaction_closed'):
        tx.setting('anything')


async def test_each_driver_translates_unique_errors_and_rolls_back(metadata):
    async with metadata.transaction(write=True) as tx:
        await tx.insert(item())
    with pytest.raises(Failure, match='constraint_conflict'):
        async with metadata.transaction(write=True) as tx:
            await tx.insert(item('child', 'root'))
            await tx.insert(item('child', 'root'))
    async with metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM resources')[0] == 1
        assert tx.one('SELECT id FROM resources WHERE id=?', ('child',)) is None


async def test_each_driver_preserves_literal_percent_and_question_mark(metadata):
    async with metadata.transaction(write=False) as tx:
        row = tx.one("SELECT '?', ?, '50%'", ('literal?%',))
        assert tuple(row) == ('?', 'literal?%', '50%')
