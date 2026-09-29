"""Real PostgreSQL COMMIT rejection after durable Git/CAS publication."""

import pytest
from test_service import call, register

from msg.core.codec import b64
from msg.core.models import ResourceRef
from msg.workers.maintenance import _collect


@pytest.mark.asyncio
@pytest.mark.parametrize('media', ['text/plain', 'application/octet-stream'])
async def test_deferred_commit_failure_leaves_collectable_orphan_and_retry(
    installed, monkeypatch, media
):
    app, _ = installed
    key, subject, _ = await register(app, 'commit-fault')
    initial = await call(
        app,
        'file.create',
        {
            'parent': '/@commit-fault/files',
            'name': 'retained.dat',
            'data': b64(b'retained bytes'),
            'media_type': media,
        },
        key=key,
        subject=subject,
    )
    assert initial.status == 'ok', initial.error
    rid = initial.resources[0].id
    tables = ('resources', 'revisions', 'events', 'results', 'audit', 'jobs')
    async with app.metadata.transaction(write=False) as tx:
        old = await tx.revision(ResourceRef(id=rid))
        before = tuple(tx.one('SELECT COUNT(*) FROM ' + table)[0] for table in tables)
    captured = []
    commit_revision = app.contents.commit_revision

    async def observe(topic, revision):
        result = await commit_revision(topic, revision)
        captured.append(revision)
        return result

    monkeypatch.setattr(app.contents, 'commit_revision', observe)
    # A real deferred PostgreSQL trigger fails inside connection.commit(), after
    # handler, Event and idempotency rows have been prepared. No mocked principal
    # or fabricated connection failure can short-circuit that boundary.
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            """CREATE FUNCTION reject_test_revision_commit() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'injected_deferred_commit_failure'; END;
            $$ LANGUAGE plpgsql""",
            write=True,
        )
        tx.execute(
            """CREATE CONSTRAINT TRIGGER reject_test_revision_commit
            AFTER INSERT ON revisions DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION reject_test_revision_commit()""",
            write=True,
        )
    args = {'id': rid, 'base_revision': old.id, 'data': b64(b'uncommitted bytes')}
    failed = await call(
        app,
        'file.write',
        args,
        key=key,
        subject=subject,
        expected=((rid, initial.data['generation']),),
        rid='commit-fault-request',
    )
    assert failed.status == 'error', failed
    assert len(captured) == 1  # durable publication ran before COMMIT rejected it
    orphan = captured[0]
    assert await app.contents.read_bytes(orphan.content) == b'uncommitted bytes'
    assert not await app.contents.pinned(orphan.content, orphan.id)
    async with app.metadata.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT COUNT(*) FROM ' + table)[0] for table in tables) == before
        assert (await tx.resource(rid)).revision == old.id
        assert tx.one('SELECT 1 FROM results WHERE request_id=?', ('commit-fault-request',)) is None
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('DROP TRIGGER reject_test_revision_commit ON revisions', write=True)
        tx.execute('DROP FUNCTION reject_test_revision_commit()', write=True)
        result = await _collect(app, tx, grace_seconds=0)
    assert result['unreferenced_contents_removed'] == 1
    assert not (app.contents.index / orphan.content.digest[7:]).exists()
    assert not (app.contents.path / 'revisions' / orphan.id).exists()
    if media == 'application/octet-stream':
        assert not (app.contents.binary / orphan.content.digest[7:]).exists()
    assert await app.contents.read_bytes(old.content) == b'retained bytes'
    assert await app.contents.pinned(old.content, old.id)
    retry = await call(
        app,
        'file.write',
        args,
        key=key,
        subject=subject,
        expected=((rid, initial.data['generation']),),
        rid='commit-fault-request',
    )
    assert retry.status == 'ok' and not retry.replayed, retry.error
    # Old immutable bytes remain retained after a new revision and ordinary archive.
    archived = await call(
        app,
        'file.delete',
        {'id': rid},
        key=key,
        subject=subject,
        expected=((rid, retry.data['generation']),),
    )
    assert archived.status == 'ok', archived.error
    async with app.metadata.transaction(write=True) as tx:
        assert (await _collect(app, tx, grace_seconds=0))['unreferenced_contents_removed'] == 0
        assert (await tx.revision(ResourceRef(id=rid, revision=old.id))) == old
    assert await app.contents.read_bytes(old.content) == b'retained bytes'
    assert await app.contents.read_bytes(captured[-1].content) == b'uncommitted bytes'
