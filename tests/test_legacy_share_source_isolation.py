"""Restored legacy shares fail closed without vetoing an independent source."""

from datetime import timedelta

import pytest
from test_authorization_sources import grant, private_post
from test_service import NOW, register
from test_share_grants_v2 import invoke, ok

from msg.core.codec import wire


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('column', 'value'),
    [
        ('expires_at', 'corrupt-restored-time'),
        ('created_at', 'corrupt-restored-time'),
        ('created_at', wire(NOW + timedelta(hours=1))),
    ],
)
async def test_malformed_legacy_source_does_not_veto_independent_share(installed, column, value):
    app, _ = installed
    owner = await register(app, 'legacy-source-owner')
    reader = await register(app, 'legacy-source-reader')
    rid, revision = await private_post(app, owner)
    legacy = ok(
        await invoke(
            app,
            owner,
            'sharing.grant',
            {'resource': rid, 'grantee': reader[1], 'expires_at': wire(NOW + timedelta(days=1))},
        )
    ).data['grant']['id']
    queries = ({'id': rid}, {'id': rid, 'view': 'history'}, {'id': rid, 'revision': revision})
    for query in queries:
        ok(await invoke(app, reader, 'discovery.get', query))

    async with app.metadata.transaction(write=True) as tx:
        tx.execute(f'UPDATE share_grants SET {column}=? WHERE id=?', (value, legacy), write=True)
    for query in queries:
        denied = await invoke(app, reader, 'discovery.get', query)
        assert denied.status == 'error' and denied.error.code == 'permission_denied', wire(denied)
        assert not denied.resources and not denied.data

    independent = await grant(app, owner, rid, reader[1])
    for query in queries:
        ok(await invoke(app, reader, 'discovery.get', query))
    ok(await invoke(app, owner, 'sharing.revoke', {'grant_id': independent}, version=2))
    for query in queries:
        denied = await invoke(app, reader, 'discovery.get', query)
        assert denied.status == 'error' and denied.error.code == 'permission_denied', wire(denied)
        assert not denied.resources and not denied.data
