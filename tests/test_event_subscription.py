import asyncio
from datetime import timedelta

import pytest
from test_resource_event_unification import new_post
from test_service import NOW, call, register

from msg.core.codec import wire


async def events(app, key, subject, **args):
    return await call(app, 'communication.events', args, key=key, subject=subject, contract_version=2)


async def test_authority_change_reuses_position_and_never_crosses_account(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'subscription-owner')
    other_key, other, _ = await register(app, 'subscription-other')
    first = await events(app, key, owner, tail=True)
    assert first.status == 'ok', wire(first)
    cursor = first.data['cursor']
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('authorization_epoch', tx.setting('authorization_epoch', 0) + 1)
    resumed = await events(app, key, owner, cursor=cursor)
    assert resumed.status == 'ok', wire(resumed)
    assert resumed.data['authority_changed']
    before = app.cursors.inspect(cursor)['position']['seq']
    assert app.cursors.inspect(resumed.data['cursor'])['position']['seq'] == before
    denied = await events(app, other_key, other, cursor=cursor)
    assert denied.error.code == 'cursor_query_mismatch'
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('sync_floor', before + 1)
    lost = await events(app, key, owner, cursor=cursor)
    assert lost.error.code == 'resync_required'


async def test_legacy_event_cursor_promotes_without_replaying_consumed_events(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'subscription-legacy')
    old = await call(app, 'communication.events', {}, key=key, subject=owner)
    assert old.status == 'ok', wire(old)
    while len(old.data['items']) == 50:
        old = await call(app, 'communication.events', {'cursor': old.data['cursor']},
                         key=key, subject=owner)
    cursor = old.data['cursor']
    saved = app.cursors.inspect(cursor)
    saved['position']['expires_at'] = wire(NOW - timedelta(seconds=1))
    cursor = app.cursors.encode(saved['kind'], saved['query'], saved['position'])
    resumed = await events(app, key, owner, cursor=cursor)
    assert resumed.status == 'ok', wire(resumed)
    assert app.cursors.inspect(resumed.data['cursor'])['kind'] == 'events-v2'
    assert all(item['seq'] > saved['position']['seq'] for item in resumed.data['items'])
    assert (await call(app, 'communication.events', {'cursor': cursor},
                       key=key, subject=owner)).error.code == 'cursor_expired'


async def test_committed_write_wakes_waiter_and_contains_only_authorized_references(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'subscription-wakeup')
    baseline = await events(app, key, owner, tail=True)
    signal = app.metadata.event_signal
    pending = asyncio.create_task(events(app, key, owner, cursor=baseline.data['cursor'], wait_ms=2000))
    try:
        await asyncio.wait_for(signal.ready.wait(), 1)
        note = await call(app, 'identity.note_put', {'name': 'done', 'body': 'never in notification'},
                          key=key, subject=owner)
        assert note.status == 'ok', wire(note)
        result = await asyncio.wait_for(pending, 1)
        assert result.status == 'ok', wire(result)
        assert result.data['items']
        assert 'never in notification' not in str(wire(result.data))
        assert all('before_cursor' in item and 'resume_cursor' in item for item in result.data['items'])
        assert signal.waiters == 0
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_rollback_does_not_notify_and_second_process_commit_does(installed):
    from msg.storage.event_signal import EventMetadataStore

    app, _ = installed
    signal = app.metadata.event_signal
    assert await signal.start()
    generation = signal.generation
    with pytest.raises(RuntimeError):
        async with app.metadata.transaction(write=True) as tx:
            tx.set_setting('not-committed', True)
            raise RuntimeError('rollback')
    await asyncio.sleep(0.05)
    assert signal.generation == generation
    second = EventMetadataStore(app.settings.server.postgres_dsn, initialize=False)
    try:
        async with second.transaction(write=True) as tx:
            tx.set_setting('committed-on-second-store', True)
        await signal.wait(generation, 1)
        assert signal.generation > generation
    finally:
        await second.close()


async def test_scope_revoked_during_wait_fails_closed_and_regrant_resumes(installed):
    app, _ = installed
    key, owner, cert = await register(app, 'subscription-scope-owner')
    other_key, other, _ = await register(app, 'subscription-scope-reader')
    post = await new_post(app, key, owner, cert)
    rid = post.resources[0].id
    baseline = await events(app, other_key, other, resource=rid, tail=True)
    assert baseline.status == 'ok', wire(baseline)
    pending = asyncio.create_task(events(
        app, other_key, other, resource=rid, cursor=baseline.data['cursor'], wait_ms=2000,
    ))
    try:
        await asyncio.wait_for(app.metadata.event_signal.ready.wait(), 1)
        changed = await call(app, 'content.chmod', {'id': rid, 'mode': '0600'}, key=key,
                              subject=owner, certs=(cert,), expected=((rid, post.data['generation']),))
        assert changed.status == 'ok', wire(changed)
        denied = await asyncio.wait_for(pending, 1)
        assert denied.status == 'error' and not denied.data
        restored = await call(app, 'content.chmod', {'id': rid, 'mode': '0644'}, key=key,
                               subject=owner, certs=(cert,), expected=((rid, changed.data['generation']),))
        assert restored.status == 'ok', wire(restored)
        resumed = await events(app, other_key, other, resource=rid, cursor=baseline.data['cursor'])
        assert resumed.status == 'ok', wire(resumed)
        assert resumed.data['authority_changed']
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
