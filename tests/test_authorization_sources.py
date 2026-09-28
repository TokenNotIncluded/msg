"""Live source matrix: credentials are ceilings; independent grants are alternatives.

These regressions also simulate stale/corrupt metadata restored from a backup.
The live authorizer must not assume that issuance-time validation ran recently.
"""
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from msg.core.codec import canonical, wire
from msg.core.requests import request_for
from msg.transports.client import HTTPTransport, PathGETTransport, MCPHTTPTransport
from msg.transports.http import create_app
from test_service import NOW, call, register
from test_share_grants_v2 import invoke, ok


async def private_post(app, owner):
    post = ok(await invoke(app, owner, 'content.post_create',
                           {'parent': '/main', 'body': 'source-matrix-private'}))
    rid = post.resources[0].id
    ok(await invoke(app, owner, 'content.chmod', {'id': rid, 'mode': '0600'},
                    expected=((rid, post.data['generation']),)))
    return rid, post.resources[0].revision


async def grant(app, owner, rid, grantee, *, kind='user', parent=None, reshare=False):
    args = {'resource': rid, 'grantee': grantee, 'grantee_kind': kind,
            'operations': ['read'], 'expires_at': wire(NOW + timedelta(days=1)),
            'allow_reshare': reshare}
    if parent:
        args['source_grant_id'] = parent
    return ok(await invoke(app, owner, 'sharing.grant', args, version=2)).data['grant']['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['mode', 'share'])
async def test_archived_organization_cannot_authorize_stale_active_membership(installed, source):
    app, _ = installed
    owner = await register(app, 'matrix-owner')
    member = await register(app, 'matrix-member')
    group = ok(await invoke(app, owner, 'group.create', {'name': 'matrix-org'})).resources[0].id
    ok(await invoke(app, owner, 'group.invite', {'group': group, 'subject': member[1]}))
    ok(await invoke(app, member, 'group.join', {'group': group}))
    rid, _ = await private_post(app, owner)
    if source == 'share':
        await grant(app, owner, rid, group, kind='group')
    else:
        async with app.metadata.transaction(write=False) as tx:
            generation = (await tx.resource(rid)).generation
        changed = ok(await invoke(app, owner, 'content.chgrp', {'id': rid, 'group': group},
                                  expected=((rid, generation),)))
        ok(await invoke(app, owner, 'content.chmod', {'id': rid, 'mode': '0640'},
                        expected=((rid, changed.data['generation']),)))
    ok(await invoke(app, member, 'discovery.get', {'id': rid}))
    async with app.metadata.transaction(write=True) as tx:
        organization = await tx.resource(group)
        # A restored membership row is not authority for a retired organization.
        await tx.replace(replace(organization, state='archived',
                                 generation=organization.generation + 1), organization.generation)
    result = await invoke(app, member, 'discovery.get', {'id': rid})
    assert result.error and result.error.code == 'permission_denied', wire(result)
    # An independent explicit user grant still works; source failure is not a veto.
    await grant(app, owner, rid, member[1])
    ok(await invoke(app, member, 'discovery.get', {'id': rid}))


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['future_operation', 'future_constraint', 'parent_expiry'])
async def test_restored_share_sources_cannot_silently_expand_contract(installed, corruption):
    app, _ = installed
    owner = await register(app, 'restore-owner')
    middle = await register(app, 'restore-middle')
    reader = await register(app, 'restore-reader')
    rid, _ = await private_post(app, owner)
    parent = await grant(app, owner, rid, middle[1], reshare=True)
    child = await grant(app, middle, rid, reader[1], parent=parent)
    ok(await invoke(app, reader, 'discovery.get', {'id': rid}))
    async with app.metadata.transaction(write=True) as tx:
        if corruption == 'future_operation':
            tx.execute('UPDATE share_grants_v2 SET operations=? WHERE id=?',
                       ('["read","write"]', child), write=True)
        elif corruption == 'future_constraint':
            tx.execute('UPDATE share_grants_v2 SET constraints=? WHERE id=?',
                       ('{"future_permission":true}', child), write=True)
        else:
            tx.execute('UPDATE share_grants_v2 SET expires_at=? WHERE id=?',
                       (wire(NOW + timedelta(hours=1)), parent), write=True)
    result = await invoke(app, reader, 'discovery.get', {'id': rid})
    assert result.error and result.error.code == 'permission_denied', wire(result)


@pytest.mark.asyncio
async def test_existing_share_source_stops_at_new_preview_boundary(installed):
    app, _ = installed
    owner = await register(app, 'preview-matrix-owner')
    reader = await register(app, 'preview-matrix-reader')
    rid, _ = await private_post(app, owner)
    source = await grant(app, owner, rid, reader[1], reshare=True)
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('hosting_preview_file:' + rid, {'owner': owner[1]})
    async with app.metadata.transaction(write=False) as tx:
        resource = await tx.resource(rid)
        assert not await app.authorizer.share_source_active(
            resource, source, reader[1], NOW, tx, reshare=True)
    denied = await invoke(app, reader, 'discovery.get', {'id': rid})
    assert denied.error.code == 'permission_denied'


@pytest.mark.asyncio
async def test_topic_ban_cannot_be_bypassed_by_a_preexisting_nested_topic(installed):
    app, _ = installed
    owner = await register(app, 'ban-matrix-owner')
    writer = await register(app, 'ban-matrix-writer')
    parent = ok(await invoke(app, owner, 'content.topic_create',
                             {'parent': '/main', 'name': 'ban-matrix'})).resources[0].id
    child = ok(await invoke(app, writer, 'content.topic_create',
                            {'parent': parent, 'name': 'nested'})).resources[0].id
    ok(await invoke(app, owner, 'content.topic_ban',
                    {'id': parent, 'subject_id': writer[1]}))
    result = await invoke(app, writer, 'content.post_create',
                          {'parent': child, 'body': 'must not cross ancestor ban'})
    assert result.error and result.error.code == 'topic_banned', wire(result)


@pytest.mark.asyncio
async def test_share_revocation_matrix_covers_current_history_transports_search_sync(installed):
    app, _ = installed
    owner = await register(app, 'transport-matrix-owner')
    reader = await register(app, 'transport-matrix-reader')
    rid, revision = await private_post(app, owner)
    gid = await grant(app, owner, rid, reader[1])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        transports = [kind(app.settings.service_url, http=http) for kind in
                      (HTTPTransport, PathGETTransport, MCPHTTPTransport)]
        async def matrix(expected):
            for args in ({'id': rid}, {'id': rid, 'view': 'history'},
                         {'id': rid, 'revision': revision}):
                packet = request_for('discovery.get', args, app.settings.service_url,
                                     signer=reader[0], subject=reader[1], source='msg',
                                     expires_at=NOW + timedelta(seconds=60))
                for transport in transports:
                    result = await transport.call(packet)
                    assert result.status == expected, (transport.name, args, wire(result))
                response = await http.post('/_r/graphql', json={
                    'query': 'query($p: JSON!) { call(packet: $p) }',
                    'variables': {'p': wire(packet)}})
                assert response.json()['data']['call']['status'] == expected, response.text
        await matrix('ok')
        sync = ok(await invoke(app, reader, 'communication.sync', {}))
        ok(await invoke(app, owner, 'sharing.revoke', {'grant_id': gid}, version=2))
        async with app.metadata.transaction(write=False) as tx:
            before = (tx.one('SELECT COUNT(*) FROM events')[0],
                      tx.one('SELECT COUNT(*) FROM jobs')[0],
                      tx.one('SELECT COUNT(*) FROM revisions')[0])
        await matrix('error')
        search = ok(await invoke(app, reader, 'discovery.search', {'query': 'source-matrix-private'}))
        assert rid not in canonical(search.data).decode()
        current = ok(await invoke(app, reader, 'communication.sync', {}))
        assert all(item.get('kind') == 'revoked' or item.get('ref', {}).get('id') != rid
                   for item in current.data['items'])
        async with app.metadata.transaction(write=False) as tx:
            assert before == (tx.one('SELECT COUNT(*) FROM events')[0],
                              tx.one('SELECT COUNT(*) FROM jobs')[0],
                              tx.one('SELECT COUNT(*) FROM revisions')[0])
