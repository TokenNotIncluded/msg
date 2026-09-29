"""A live independent grant survives stacked changes to another source."""
import pytest
from test_authorization_sources import grant, private_post
from test_service import register
from test_share_grants_v2 import invoke, ok

from msg.core.codec import canonical


@pytest.mark.asyncio
async def test_existing_direct_share_survives_move_leave_and_parent_revocation(installed):
    app, _ = installed
    owner = await register(app, 'stacked-owner')
    middle = await register(app, 'stacked-middle')
    independent = await register(app, 'stacked-independent')
    derived_only = await register(app, 'stacked-derived-only')
    group = ok(await invoke(app, owner, 'group.create', {'name': 'stacked-sources'})).resources[0].id
    ok(await invoke(app, owner, 'group.invite', {'group': group, 'subject': middle[1]}))
    ok(await invoke(app, middle, 'group.join', {'group': group}))
    rid, revision = await private_post(app, owner)
    parent_grant = await grant(app, owner, rid, group, kind='group', reshare=True)
    for reader in (independent, derived_only):
        await grant(app, middle, rid, reader[1], parent=parent_grant)
    direct = await grant(app, owner, rid, independent[1])

    queries = ({'id': rid}, {'id': rid, 'view': 'history'}, {'id': rid, 'revision': revision})

    async def check(reader, allowed):
        for query in queries:
            result = await invoke(app, reader, 'discovery.get', query)
            if allowed:
                ok(result)
                expected = revision if query.get('view') == 'history' else 'source-matrix-private'
                assert expected in canonical(result.data).decode()
            else:
                assert result.status == 'error' and result.error.code == 'permission_denied'
                assert not result.resources
                assert not result.data

    await check(independent, True)
    await check(derived_only, True)
    destination = ok(await invoke(app, owner, 'content.topic_create',
                                 {'parent': '/main', 'name': 'private-destination'}))
    directory = destination.resources[0].id
    ok(await invoke(app, owner, 'content.chmod', {'id': directory, 'mode': '0700'},
                    expected=((directory, destination.data['generation']),)))
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(rid)).generation
    ok(await invoke(app, owner, 'content.move', {'id': rid, 'parent': directory},
                    expected=((rid, generation),)))
    await check(independent, True)
    await check(derived_only, True)

    ok(await invoke(app, middle, 'group.leave', {'group': group}))
    await check(independent, True)
    await check(derived_only, False)
    ok(await invoke(app, owner, 'sharing.revoke', {'grant_id': parent_grant}, version=2))
    await check(independent, True)
    await check(derived_only, False)
    # Target-specific sharing must never make its new private parent readable.
    for reader in (independent, derived_only):
        denied = await invoke(app, reader, 'discovery.get', {'id': directory})
        assert denied.error.code == 'permission_denied'
    ok(await invoke(app, owner, 'sharing.revoke', {'grant_id': direct}, version=2))
    await check(independent, False)
    await check(derived_only, False)
