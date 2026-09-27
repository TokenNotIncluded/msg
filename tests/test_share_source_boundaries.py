"""Every consumer of a ShareGrant must recheck the same current target boundary."""
from dataclasses import replace
from datetime import timedelta

import pytest

from msg.core.codec import canonical, wire
from test_service import NOW, register
from test_share_grants_v2 import invoke, ok


@pytest.mark.asyncio
async def test_named_source_revalidates_target_and_persisted_constraints(installed):
    app, _ = installed
    owner = await register(app, 'boundary-owner')
    reader = await register(app, 'boundary-reader')
    post = ok(await invoke(app, owner, 'content.post_create',
        {'parent': '/main', 'body': 'private source evidence'}))
    rid = post.resources[0].id
    ok(await invoke(app, owner, 'content.chmod', {'id': rid, 'mode': '0600'},
                    expected=((rid, post.data['generation']),)))
    grant = ok(await invoke(app, owner, 'sharing.grant',
        {'resource': rid, 'grantee': reader[1], 'grantee_kind': 'user',
         'operations': ['read'], 'expires_at': wire(NOW + timedelta(days=2))},
        version=2)).data['grant']['id']
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(rid)
        assert await app.authorizer.share_source_active(resource, grant, reader[1], NOW, tx)
        # These represent restore corruption or later metadata/preview changes.
        # The direct-source API is also consumed by certificate validation.
        cases = [
            ('operations', canonical(['read', 'write']).decode()),
            ('operations', '"read"'),
            ('constraints', canonical({'unknown': True}).decode()),
            ('created_at', wire(NOW + timedelta(hours=1))),
        ]
        for column, value in cases:
            original = tx.one(f'SELECT {column} FROM share_grants_v2 WHERE id=?', (grant,))[0]
            tx.execute(f'UPDATE share_grants_v2 SET {column}=? WHERE id=?', (value, grant), write=True)
            assert not await app.authorizer.share_source_active(resource, grant, reader[1], NOW, tx), column
            tx.execute(f'UPDATE share_grants_v2 SET {column}=? WHERE id=?', (original, grant), write=True)
        assert not await app.authorizer.share_source_active(
            replace(resource, state='archived'), grant, reader[1], NOW, tx)
        tx.set_setting('hosting_preview_file:' + rid, {'owner': owner[1]})
        assert not await app.authorizer.share_source_active(resource, grant, reader[1], NOW, tx)


@pytest.mark.asyncio
async def test_child_cannot_outlive_source_even_after_restore(installed):
    app, _ = installed
    owner = await register(app, 'duration-owner')
    middle = await register(app, 'duration-middle')
    reader = await register(app, 'duration-reader')
    post = ok(await invoke(app, owner, 'content.post_create',
        {'parent': '/main', 'body': 'duration'}))
    rid = post.resources[0].id
    source = ok(await invoke(app, owner, 'sharing.grant',
        {'resource': rid, 'grantee': middle[1], 'grantee_kind': 'user',
         'operations': ['read'], 'expires_at': wire(NOW + timedelta(days=2)),
         'allow_reshare': True}, version=2)).data['grant']['id']
    child = ok(await invoke(app, middle, 'sharing.grant',
        {'resource': rid, 'grantee': reader[1], 'grantee_kind': 'user',
         'operations': ['read'], 'expires_at': wire(NOW + timedelta(days=1)),
         'source_grant_id': source}, version=2)).data['grant']['id']
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(rid)
        assert await app.authorizer.share_source_active(resource, child, reader[1], NOW, tx)
        tx.execute('UPDATE share_grants_v2 SET expires_at=? WHERE id=?',
            (wire(NOW + timedelta(days=3)), child), write=True)
        assert not await app.authorizer.share_source_active(resource, child, reader[1], NOW, tx)
        # An independent owner grant is not vetoed by this malformed source.
    ok(await invoke(app, owner, 'sharing.grant',
        {'resource': rid, 'grantee': reader[1], 'grantee_kind': 'user',
         'operations': ['read'], 'expires_at': wire(NOW + timedelta(hours=1))}, version=2))
    ok(await invoke(app, reader, 'discovery.get', {'id': rid}))
