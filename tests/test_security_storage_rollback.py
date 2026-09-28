"""Compensation retains savepoint ownership and never hides cancellation."""
import asyncio

import pytest
from test_service import call, register

from msg.core.codec import b64, decode, digest, wire
from msg.core.models import BlobRef
from msg.storage.sqlite import SqliteMetadataStore
from msg.workers import maintenance


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['postgres', 'sqlite'])
async def test_nested_rollback_compensates_only_its_scope(installed, tmp_path, backend):
    app, _ = installed
    store = app.metadata if backend == 'postgres' else SqliteMetadataStore(tmp_path / 'nested.db')
    effects = []
    async def record(value):
        effects.append(value)
    async with store.transaction(write=True) as outer:
        outer.on_rollback(lambda: record('outer'))
        with pytest.raises(ValueError, match='savepoint'):
            async with store.transaction(write=True) as inner:
                inner.on_rollback(lambda: record('inner'))
                inner.set_setting('rolled-back', True)
                raise ValueError('savepoint')
        assert effects == ['inner']
        assert outer.setting('rolled-back') is None
        outer.set_setting('committed', True)
    assert effects == ['inner']
    async with store.transaction(write=False) as tx:
        assert tx.setting('committed') is True
    with pytest.raises(ValueError, match='outer'):
        async with store.transaction(write=True) as outer:
            outer.on_rollback(lambda: record('outer-abort'))
            async with store.transaction(write=True) as inner:
                inner.on_rollback(lambda: record('released-savepoint'))
            raise ValueError('outer')
    assert effects == ['inner', 'released-savepoint', 'outer-abort']


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['postgres', 'sqlite'])
async def test_compensation_failure_preserves_original_and_runs_remaining(installed, tmp_path, backend):
    app, _ = installed
    store = app.metadata if backend == 'postgres' else SqliteMetadataStore(tmp_path / 'fail.db')
    effects = []
    async def record(value):
        effects.append(value)
    async def fail():
        effects.append('failure')
        raise OSError('injected')
    original = ValueError('primary')
    with pytest.raises(ValueError) as captured:
        async with store.transaction(write=True) as tx:
            tx.on_rollback(lambda: record('first'))
            tx.on_rollback(fail)
            tx.on_rollback(lambda: record('last'))
            raise original
    assert captured.value is original
    assert effects == ['last', 'failure', 'first']
    assert original.__notes__ == ['rollback compensation failed: OSError']


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['postgres', 'sqlite'])
async def test_task_cancellation_rolls_back_sql_and_external_effect(installed, tmp_path, backend):
    app, _ = installed
    store = app.metadata if backend == 'postgres' else SqliteMetadataStore(tmp_path / 'cancel.db')
    entered = asyncio.Event()
    external = tmp_path / 'external'
    async def compensate():
        external.unlink()
    async def work():
        async with store.transaction(write=True) as tx:
            tx.set_setting('cancelled', True)
            tx.on_rollback(compensate)
            external.write_text('uncommitted')
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(work())
    await entered.wait()
    assert external.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not external.exists()
    async with store.transaction(write=False) as tx:
        assert tx.setting('cancelled') is None


@pytest.mark.asyncio
async def test_failed_transfer_cleanup_restores_pin_and_metadata(installed, monkeypatch):
    app, _ = installed
    key, subject, _ = await register(app, 'security-rollback-upload')
    opened = await call(app, 'transfer.open', {'direction': 'upload'}, key=key, subject=subject)
    assert opened.status == 'ok', wire(opened)
    tid = opened.data['transfer_id']
    result = await call(app, 'transfer.part_put', {'transfer_id': tid, 'offset': 0,
        'data': b64(b'abcd'), 'digest': digest(b'abcd')}, key=key, subject=subject)
    assert result.status == 'ok', wire(result)
    blob = decode(BlobRef, result.data['chunk']['content'])
    from datetime import timedelta

    from test_security_patch_regressions import set_clock
    from test_service import NOW
    set_clock(app, NOW + timedelta(seconds=app.settings.transfer_ttl + 1))
    unpin = app.contents.unpin
    async def fail_after_unpin(*args):
        await unpin(*args)
        raise OSError('injected after external change')
    monkeypatch.setattr(app.contents, 'unpin', fail_after_unpin)
    with pytest.raises(OSError, match='injected'):
        await maintenance.run_maintenance(app, 'cleanup_expired', scheduled=True)
    assert await app.contents.pinned(blob, tid + ':0')
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.transfer(tid)).state == 'open'
        assert tx.one('SELECT COUNT(*) FROM chunks WHERE transfer_id=?', (tid,))[0] == 1
    monkeypatch.setattr(app.contents, 'unpin', unpin)
    await maintenance.run_maintenance(app, 'cleanup_expired', scheduled=True)
    assert not await app.contents.pinned(blob, tid + ':0')


@pytest.mark.asyncio
async def test_scheduled_collection_is_rate_limited_and_respects_cleanup_pause(installed):
    app, _ = installed
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('runtime_config', {'cleanup_enabled': False})
    assert await maintenance.run_maintenance(app, 'collect_garbage', scheduled=True) == {'skipped': 'cleanup_disabled'}
    async with app.metadata.transaction(write=True) as tx:
        assert tx.setting('garbage_collection_last') is None
        tx.set_setting('runtime_config', {'cleanup_enabled': True})
    result = await maintenance.run_maintenance(app, 'collect_garbage', scheduled=True)
    assert result['unreferenced_contents_removed'] == 0
    assert await maintenance.run_maintenance(app, 'collect_garbage', scheduled=True) == {'skipped': 'not_due'}


@pytest.mark.asyncio
async def test_purged_sealed_output_does_not_break_expiry_or_collection(installed):
    from datetime import timedelta

    from test_security_patch_regressions import set_clock
    from test_service import NOW
    app, _ = installed
    key, subject, _ = await register(app, 'security-purged-upload')
    data = b'unreferenced after explicit purge'
    opened = await call(app, 'transfer.open', {'direction': 'upload', 'size': len(data)}, key=key, subject=subject)
    assert opened.status == 'ok', wire(opened)
    tid = opened.data['transfer_id']
    uploaded = await call(app, 'transfer.part_put', {'transfer_id': tid, 'offset': 0,
        'data': b64(data), 'digest': digest(data)}, key=key, subject=subject)
    assert uploaded.status == 'ok', wire(uploaded)
    sealed = await call(app, 'transfer.seal', {'transfer_id': tid, 'final_size': len(data),
        'final_digest': digest(data)}, key=key, subject=subject)
    assert sealed.status == 'ok', wire(sealed)
    async with app.metadata.transaction(write=True) as tx:
        revision = await tx.revision(sealed.resources[0])
        resource = await tx.resource(sealed.resources[0].id)
        await maintenance.purge_revisions(app, tx, resource, actor=subject,
                                         request_id='test-explicit-purge', reason='retention_expired')
    assert await app.contents.pinned(revision.content, tid + ':sealed')
    set_clock(app, NOW + timedelta(seconds=app.settings.transfer_ttl + 1))
    await maintenance.run_maintenance(app, 'cleanup_expired', scheduled=True)
    assert not await app.contents.pinned(revision.content, tid + ':sealed')
    async with app.metadata.transaction(write=True) as tx:
        result = await maintenance._collect(app, tx, grace_seconds=0)
    assert result['unreferenced_contents_removed'] == 1
    assert not (app.contents.index / revision.content.digest[7:]).exists()
