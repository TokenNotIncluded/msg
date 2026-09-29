"""Post metadata and rollback keep independent Revision and generation boundaries."""

import pytest
from test_service import call, register

from msg.core.codec import wire
from msg.core.models import ResourceRef


async def snapshot(app, rid):
    async with app.metadata.transaction(write=False) as tx:
        resource = await tx.resource(rid)
        rows = tx.one('SELECT COUNT(*) FROM revisions WHERE resource_id=?', (rid,))[0]
        return resource, rows


@pytest.mark.asyncio
async def test_metadata_edit_does_not_rewrite_content_or_author(installed):
    app, _ = installed
    signer, owner, _ = await register(app, 'metadata-owner')
    post = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'first'}, key=signer, subject=owner
    )
    rid = post.resources[0].id
    original, rows = await snapshot(app, rid)
    changed = await call(
        app,
        'content.post_edit_metadata',
        {'id': rid, 'name': 'renamed', 'tags': ['one', 'two']},
        key=signer,
        subject=owner,
        expected=((rid, original.generation),),
    )
    assert changed.status == 'ok', wire(changed)
    resource, after_rows = await snapshot(app, rid)
    assert resource.name == 'renamed.md' and resource.tags == ('one', 'two')
    assert resource.revision == original.revision and after_rows == rows
    async with app.metadata.transaction(write=False) as tx:
        revision = await tx.revision(ResourceRef(id=rid))
        assert revision.author == owner and revision.actor == owner


@pytest.mark.asyncio
async def test_rollback_creates_new_revision_with_historical_content_and_current_author(installed):
    app, _ = installed
    signer, owner, _ = await register(app, 'rollback-owner')
    created = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'first'}, key=signer, subject=owner
    )
    rid = created.resources[0].id
    first = created.resources[0].revision
    edited = await call(
        app,
        'content.post_write',
        {'id': rid, 'expected_revision': first, 'body': 'second'},
        key=signer,
        subject=owner,
        expected=((rid, created.data['generation']),),
    )
    assert edited.status == 'ok', wire(edited)
    patched = await call(
        app,
        'content.post_patch',
        {
            'id': rid,
            'base_revision': edited.resources[0].revision,
            'exact': 'second',
            'replacement': 'second revised',
        },
        key=signer,
        subject=owner,
        expected=((rid, edited.data['generation']),),
    )
    assert patched.status == 'ok', wire(patched)
    previous, rows = await snapshot(app, rid)
    rolled = await call(
        app,
        'content.post_rollback',
        {'id': rid, 'base_revision': previous.revision, 'target_revision': first},
        key=signer,
        subject=owner,
        expected=((rid, previous.generation),),
    )
    assert rolled.status == 'ok', wire(rolled)
    current, after_rows = await snapshot(app, rid)
    assert current.revision not in {first, previous.revision}
    assert current.generation == previous.generation + 1 and after_rows == rows + 1
    assert rolled.data['rolled_back_from'] == first
    async with app.metadata.transaction(write=False) as tx:
        old = await tx.revision(ResourceRef(id=rid, revision=first))
        middle = await tx.revision(ResourceRef(id=rid, revision=previous.revision))
        new = await tx.revision(ResourceRef(id=rid))
        assert new.parents == (middle.id,)
        assert new.content == old.content and new.author == old.author == middle.author == owner
        assert new.actor == owner and new.subject == owner
        assert new.manifest_digest != old.manifest_digest
        assert (await app.contents.read_bytes(new.content)).decode() == 'first'


@pytest.mark.asyncio
async def test_metadata_and_rollback_reject_conflicts_and_unauthorized_without_publication(
    installed,
):
    app, _ = installed
    signer, owner, _ = await register(app, 'rollback-conflict-owner')
    outsider_key, outsider, _ = await register(app, 'rollback-conflict-other')
    created = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'first'}, key=signer, subject=owner
    )
    rid = created.resources[0].id
    original, rows = await snapshot(app, rid)
    cases = [
        (
            'content.post_edit_metadata',
            {'id': rid, 'tags': ['x']},
            outsider_key,
            outsider,
            original.generation,
            'permission_denied',
        ),
        (
            'content.post_edit_metadata',
            {'id': rid, 'tags': ['x']},
            signer,
            owner,
            original.generation + 1,
            'generation_conflict',
        ),
        (
            'content.post_rollback',
            {'id': rid, 'base_revision': 'v_stale', 'target_revision': original.revision},
            signer,
            owner,
            original.generation,
            'revision_conflict',
        ),
        (
            'content.post_rollback',
            {'id': rid, 'base_revision': original.revision, 'target_revision': 'v_missing'},
            signer,
            owner,
            original.generation,
            'revision_not_found',
        ),
        (
            'content.post_rollback',
            {'id': rid, 'base_revision': original.revision, 'target_revision': original.revision},
            signer,
            owner,
            original.generation,
            'rollback_target_current',
        ),
    ]
    for operation, args, key, subject, generation, code in cases:
        result = await call(
            app, operation, args, key=key, subject=subject, expected=((rid, generation),)
        )
        assert result.status == 'error' and result.error.code == code, wire(result)
        resource, after_rows = await snapshot(app, rid)
        assert resource == original and after_rows == rows


@pytest.mark.asyncio
async def test_reply_rollback_changes_only_reply(installed):
    app, _ = installed
    signer, owner, _ = await register(app, 'reply-rollback-owner')
    root = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'root'}, key=signer, subject=owner
    )
    root_id = root.resources[0].id
    reply = await call(
        app,
        'discussion.reply',
        {'target': {'id': root_id, 'revision': root.resources[0].revision}, 'body': 'first reply'},
        key=signer,
        subject=owner,
    )
    assert reply.status == 'ok', wire(reply)
    rid = reply.resources[0].id
    reply_first = reply.resources[0].revision
    before_root, _ = await snapshot(app, root_id)
    changed = await call(
        app,
        'content.post_write',
        {'id': rid, 'expected_revision': reply_first, 'body': 'edited reply'},
        key=signer,
        subject=owner,
        expected=((rid, reply.data['generation']),),
    )
    assert changed.status == 'ok', wire(changed)
    rolled = await call(
        app,
        'content.post_rollback',
        {'id': rid, 'base_revision': changed.resources[0].revision, 'target_revision': reply_first},
        key=signer,
        subject=owner,
        expected=((rid, changed.data['generation']),),
    )
    assert rolled.status == 'ok', wire(rolled)
    after_root, _ = await snapshot(app, root_id)
    assert before_root == after_root
    async with app.metadata.transaction(write=False) as tx:
        new = await tx.revision(ResourceRef(id=rid))
        assert (await app.contents.read_bytes(new.content)).decode() == 'first reply'
