"""Bounded, real-operation content editing vector for the isolated selftest."""

from msg.core.codec import b64
from msg.core.errors import require
from msg.core.models import ResourceRef


async def check_content_editing(app, call, register, test_path):
    owner_key, owner = await register('content-edit-owner')
    other_key, other = await register('content-edit-outsider')
    original = '# Selftest\n\nKeep this line.\nReplace this word.\n'
    expected_text = original.replace('word', 'value')
    created = await call(
        'file.create',
        {
            'parent': test_path + '/@content-edit-owner/files',
            'name': 'edit.md',
            'data': b64(original.encode()),
            'media_type': 'text/markdown',
        },
        owner_key,
        owner,
    )
    require(created.status == 'ok', 'selftest_content_create_failed')
    rid = created.resources[0].id
    old_ref = created.resources[0]
    async with app.metadata.transaction(write=False) as tx:
        original_revision = await tx.revision(old_ref)
    patch = {'kind': 'exact', 'exact': 'word', 'replacement': 'value'}
    args = {
        'id': rid,
        'base_revision': old_ref.revision,
        'base_generation': created.data['generation'],
        'patch': patch,
    }
    patched = await call(
        'file.patch', args, owner_key, owner, expected=((rid, created.data['generation']),)
    )
    require(patched.status == 'ok', 'selftest_content_patch_failed')
    generation = patched.data['generation']
    tables = ('resources', 'revisions', 'events', 'results', 'audit', 'jobs')
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(tx.one('SELECT COUNT(*) FROM ' + table)[0] for table in tables)
        current_before = await tx.resource(rid)
    # Current generation lets this request reach the stale base_revision check.
    stale = await call('file.patch', args, owner_key, owner, expected=((rid, generation),))
    denied = await call(
        'file.patch',
        {
            **args,
            'base_revision': patched.resources[0].revision,
            'base_generation': generation,
            'patch': {'kind': 'exact', 'exact': 'value', 'replacement': 'unauthorized'},
        },
        other_key,
        other,
        expected=((rid, generation),),
    )
    current = await call('file.read', {'id': rid}, owner_key, owner)
    previous = await call('file.read', {'id': rid, 'revision': old_ref.revision}, owner_key, owner)
    history = await call('file.read', {'id': rid, 'view': 'history', 'limit': 10}, owner_key, owner)
    async with app.metadata.transaction(write=False) as tx:
        after = tuple(tx.one('SELECT COUNT(*) FROM ' + table)[0] for table in tables)
        unchanged = (
            await tx.resource(rid) == current_before
            and await tx.revision(old_ref) == original_revision
        )
        revision = await tx.revision(ResourceRef(id=rid))
    return (
        stale.error is not None
        and stale.error.code == 'revision_conflict'
        and denied.error is not None
        and denied.error.code == 'permission_denied'
        and all(result.status == 'ok' for result in (current, previous, history))
        and current.data['content'] == expected_text
        and previous.data['content'] == original
        and {row['id'] for row in history.data['revisions']}
        == {old_ref.revision, patched.resources[0].revision}
        and len(history.data['revisions']) == 2
        and revision.parents == (old_ref.revision,)
        and revision.author == owner
        and generation == created.data['generation'] + 1
        and before == after
        and unchanged
    )
