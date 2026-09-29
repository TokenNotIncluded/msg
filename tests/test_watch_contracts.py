from dataclasses import replace

import pytest

from msg.core.codec import wire
from test_service import call, register, NOW


@pytest.mark.asyncio
async def test_watch_events_current_acl_cancel_and_closed_query(installed):
    app, _ = installed
    ak, author, _ = await register(app, 'watch-author')
    rk, reader, _ = await register(app, 'watch-reader')
    post = await call(app, 'content.post_create', {'parent': '/main', 'body': 'one'}, key=ak, subject=author)
    target = post.resources[0].id
    args = {'target': target, 'event_types': ['content.post_edit'], 'delivery': 'inbox'}
    watched = await call(app, 'communication.watch_create', args, key=rk, subject=reader)
    assert watched.status == 'ok', wire(watched)
    wid = watched.data['id']
    denied = await call(app, 'discovery.get', {'id': wid}, key=rk, subject=reader)
    assert denied.status != 'ok'
    changed = await call(app, 'content.post_edit', {'id': target, 'body': 'two', 'expected_revision': post.resources[0].revision}, key=ak, subject=author, expected=((target, post.data['generation']),))
    assert changed.status == 'ok', wire(changed)
    inbox = await call(app, 'communication.inbox', {}, key=rk, subject=reader)
    assert len(inbox.data['items']) == 1, wire(inbox)
    assert set(inbox.data['items'][0]['resource']) == {'id'}
    followed = await call(app, 'communication.watch', {'id': target}, key=rk, subject=reader)
    assert followed.status == 'ok', wire(followed)
    hidden = await call(app, 'content.chmod', {'id': target, 'mode': '0600'}, key=ak, subject=author, expected=((target, changed.data['generation']),))
    assert hidden.status == 'ok', wire(hidden)
    unfollowed = await call(app, 'communication.unwatch', {'id': target}, key=rk, subject=reader)
    assert unfollowed.status == 'ok', wire(unfollowed)
    clipped = await call(app, 'communication.watch_get', {'id': wid}, key=rk, subject=reader)
    assert clipped.status == 'ok' and 'target' not in clipped.data, wire(clipped)
    inbox = await call(app, 'communication.inbox', {}, key=rk, subject=reader)
    assert not inbox.data['items']
    cancelled = await call(app, 'communication.watch_cancel', {'id': wid}, key=rk, subject=reader)
    assert cancelled.status == 'ok', wire(cancelled)
    unsupported = await call(app, 'communication.watch_create', {'query_ref': 'anything', 'event_types': ['content.post_edit'], 'delivery': 'inbox'}, key=rk, subject=reader)
    assert unsupported.error.code == 'watch_query_unsupported', wire(unsupported)


@pytest.mark.asyncio
async def test_follow_delivery_dedup_and_revoked_credential(installed):
    app, _ = installed
    ak, author, _ = await register(app, 'watch-owner2')
    rk, reader, _ = await register(app, 'watch-reader2')
    for _ in range(2):
        followed = await call(app, 'communication.watch', {'id': '/main'}, key=rk, subject=reader)
        assert followed.status == 'ok', wire(followed)
    watches = await call(app, 'communication.watch_list', {}, key=rk, subject=reader)
    assert len(watches.data['items']) == 1, wire(watches)
    post = await call(app, 'content.post_create', {'parent': '/main', 'body': 'visible'}, key=ak, subject=author)
    assert post.status == 'ok', wire(post)
    async with app.metadata.transaction(write=True) as tx:
        count = tx.one('SELECT COUNT(*) FROM messages WHERE recipient=?', (reader,))[0]
        assert count == 1
        credential = await tx.credential(rk.key_id)
        await tx.save_credential(replace(credential, revoked_at=NOW), (await tx.subject(reader)).auth_version)
    post = await call(app, 'content.post_create', {'parent': '/main', 'body': 'after revoke'}, key=ak, subject=author)
    assert post.status == 'ok', wire(post)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM messages WHERE recipient=?', (reader,))[0] == count


@pytest.mark.asyncio
async def test_cancelled_watches_leave_the_active_set(installed):
    app, _ = installed
    ak, author, _ = await register(app, 'watch-churn-author')
    rk, reader, _ = await register(app, 'watch-churn-reader')
    # Owners cannot archive watch resources through generic content operations,
    # so cancellation is the only way to shrink the set watch_list must return.
    for _ in range(3):
        followed = await call(app, 'communication.watch', {'id': '/main'}, key=rk, subject=reader)
        assert followed.status == 'ok', wire(followed)
        unfollowed = await call(app, 'communication.unwatch', {'id': '/main'}, key=rk, subject=reader)
        assert unfollowed.status == 'ok', wire(unfollowed)
    created = await call(app, 'communication.watch_create', {
        'target': '/main', 'event_types': ['content.post_create'], 'delivery': 'inbox'},
        key=rk, subject=reader)
    assert created.status == 'ok', wire(created)
    cancelled = await call(app, 'communication.watch_cancel', {'id': created.data['id']},
                           key=rk, subject=reader)
    assert cancelled.status == 'ok', wire(cancelled)
    watches = await call(app, 'communication.watch_list', {}, key=rk, subject=reader)
    assert watches.status == 'ok' and not watches.data['items'], wire(watches)
    got = await call(app, 'communication.watch_get', {'id': created.data['id']}, key=rk, subject=reader)
    assert got.status == 'ok' and got.data['status'] == 'cancelled', wire(got)
    archived = await call(app, 'content.archive', {'id': created.data['id']}, key=rk, subject=reader)
    assert archived.status == 'error', wire(archived)
    post = await call(app, 'content.post_create', {'parent': '/main', 'body': 'after churn'},
                      key=ak, subject=author)
    assert post.status == 'ok', wire(post)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM messages WHERE recipient=?', (reader,))[0] == 0


@pytest.mark.asyncio
async def test_watch_quarantine_and_duplicate_event_projection(installed):
    from msg.core.codec import decode, loads
    from msg.core.models import Event
    from msg.plugins.watches import enqueue
    app, _ = installed
    ak, author, _ = await register(app, 'watch-owner3')
    rk, reader, _ = await register(app, 'watch-reader3')
    args = {'target': '/main', 'event_types': ['content.post_create'], 'delivery': 'inbox'}
    created = await call(app, 'communication.watch_create', args, key=rk, subject=reader, rid='watch-idempotent')
    replay = await call(app, 'communication.watch_create', args, key=rk, subject=reader, rid='watch-idempotent')
    assert created.status == replay.status == 'ok'
    assert created.data == replay.data
    invalid = await call(app, 'communication.watch_create', {**args, 'delivery': 'webhook'}, key=rk, subject=reader)
    assert invalid.status == 'error'
    post = await call(app, 'content.post_create', {'parent': '/main', 'body': 'event'}, key=ak, subject=author)
    assert post.status == 'ok'
    async with app.metadata.transaction(write=True) as tx:
        raw = tx.one("SELECT body FROM events ORDER BY seq DESC LIMIT 1")[0]
        event = decode(Event, loads(raw))
        await enqueue(app, tx, event)
        assert tx.one('SELECT COUNT(*) FROM messages WHERE recipient=?', (reader,))[0] == 1
        tx.execute('DELETE FROM messages WHERE recipient=?', (reader,), write=True)
        tx.set_setting('recovery_quarantine', {'active': True})
        await enqueue(app, tx, event)
        assert tx.one('SELECT COUNT(*) FROM messages WHERE recipient=?', (reader,))[0] == 0


@pytest.mark.asyncio
async def test_watch_removed_creating_operation_version_stops_delivery(installed):
    app, _ = installed
    ak, author, _ = await register(app, 'watch-version-author')
    rk, reader, _ = await register(app, 'watch-version-reader')
    watched = await call(app, 'communication.watch_create', {
        'target': '/main', 'event_types': ['content.post_create'], 'delivery': 'inbox'},
        key=rk, subject=reader)
    assert watched.status == 'ok', wire(watched)
    # Historical v1 disappears while a newer version exists. A name-only lookup
    # or fall back to the newest contract would incorrectly authorize delivery.
    original = app.registry._operations.pop(('communication.watch_create', 1))
    app.registry._operations[('communication.watch_create', 2)] = replace(original, version=2)
    try:
        post = await call(app, 'content.post_create', {'parent': '/main', 'body': 'new'}, key=ak, subject=author)
        assert post.status == 'ok', wire(post)
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT COUNT(*) FROM messages WHERE recipient=?', (reader,))[0] == 0
    finally:
        app.registry._operations[('communication.watch_create', 1)] = original
        del app.registry._operations[('communication.watch_create', 2)]
