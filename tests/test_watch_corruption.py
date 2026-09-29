"""A broken subscription cannot poison another publisher's Event transaction."""
import pytest

from msg.core.codec import digest, wire
from msg.core.models import ResourceRef
from test_service import call, register


@pytest.mark.asyncio
@pytest.mark.parametrize('damage,code', [('missing', 'content_missing'), ('invalid_index', 'watch_content_invalid')])
async def test_missing_watch_blob_is_isolated_and_diagnosed(installed, caplog, damage, code):
    app, _ = installed
    ak, author, _ = await register(app, 'watch-corrupt-author')
    bk, broken, _ = await register(app, 'watch-corrupt-broken')
    gk, good, _ = await register(app, 'watch-corrupt-good')
    args = {'target': '/main', 'event_types': ['content.post_create'], 'delivery': 'inbox'}
    bad = await call(app, 'communication.watch_create', args, key=bk, subject=broken)
    healthy = await call(app, 'communication.watch_create', args, key=gk, subject=good)
    assert bad.status == healthy.status == 'ok'
    async with app.metadata.transaction(write=False) as tx:
        revision = await tx.revision(ResourceRef(id=bad.data['id']))
    index = app.contents.index / app.contents._key(revision.content)
    if damage == 'missing':
        index.unlink()
    else:
        index.write_text('{}')
    with caplog.at_level('WARNING', logger='msg.plugins.watches'):
        post = await call(app, 'content.post_create', {'parent': '/main', 'body': 'publisher survives'}, key=ak, subject=author)
    assert post.status == 'ok', wire(post)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM messages WHERE recipient=?', (broken,))[0] == 0
        assert tx.one('SELECT COUNT(*) FROM messages WHERE recipient=? AND resource=?', (good, post.resources[0].id))[0] == 1
    report = next(r for r in caplog.records if r.message.startswith('watch_projection_corrupt '))
    assert report.watch_digest == digest(bad.data['id'])
    assert report.error_code == code
    assert broken not in caplog.text and bad.data['id'] not in caplog.text


@pytest.mark.asyncio
async def test_watch_database_failure_is_not_swallowed_and_rolls_back(installed, monkeypatch):
    from msg.plugins import watches
    app, _ = installed
    key, subject, _ = await register(app, 'watch-database-failure')
    watched = await call(app, 'communication.watch_create', {'target': '/main',
        'event_types': ['content.post_create'], 'delivery': 'inbox'}, key=key, subject=subject)
    assert watched.status == 'ok'
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(tx.one('SELECT COUNT(*) FROM '+table)[0] for table in ('resources', 'events', 'messages'))

    async def broken_database(app, tx, rid):
        # A real PostgreSQL statement error aborts this business transaction.
        tx.one('SELECT * FROM nonexistent_watch_regression_table')

    monkeypatch.setattr(watches, 'record', broken_database)
    posted = await call(app, 'content.post_create', {'parent': '/main', 'body': 'must roll back'}, key=key, subject=subject)
    assert posted.status == 'error', wire(posted)
    async with app.metadata.transaction(write=False) as tx:
        after = tuple(tx.one('SELECT COUNT(*) FROM '+table)[0] for table in ('resources', 'events', 'messages'))
    assert after == before
