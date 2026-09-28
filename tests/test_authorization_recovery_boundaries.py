"""Real restore and in-flight revocation never turn a stored reference into access."""
import asyncio
from datetime import timedelta

import httpx
import pytest

from msg.admin.backups import backup, restore
from msg.application import Application
from msg.config import load_settings
from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.core.requests import request_for
from msg.transports.client import HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport
from msg.transports.http import create_app
from msg.workers.effects import EffectWorker
from test_authorization_sources import private_post, grant
from test_share_grants_v2 import invoke, ok
from test_share_links import proof
from test_service import NOW, register


@pytest.mark.asyncio
async def test_shared_post_does_not_open_attachment_and_concurrent_revocation_is_final(installed):
    app, _ = installed
    owner = await register(app, 'boundary-owner')
    reader = await register(app, 'boundary-reader')
    rid, revision = await private_post(app, owner)
    source = ok(await invoke(app, owner, 'content.file_put', {
        'parent': '/@boundary-owner/files', 'name': 'secret.txt',
        'data': b64(b'attachment-secret'), 'media_type': 'text/plain'}))
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(rid)).generation
    attached = ok(await invoke(app, owner, 'content.attach', {
        'post': rid, 'source': wire(source.resources[0])}, expected=((rid, generation),)))
    attachment = attached.data['attachment']['id']
    gid = await grant(app, owner, rid, reader[1])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        adapters = [kind(app.settings.service_url, http=http) for kind in
                    (HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport)]
        def packet(operation, args):
            return request_for(operation, args, app.settings.service_url,
                signer=reader[0], subject=reader[1], source='msg',
                expires_at=NOW + timedelta(seconds=60))
        for adapter in adapters:
            ok(await adapter.call(packet('discovery.get', {'id': rid})))
            hidden = await adapter.call(packet('discovery.get', {'id': attachment}))
            assert hidden.status == 'error', wire(hidden)
            links = ok(await adapter.call(packet('discovery.links', {'id': rid, 'rel': 'f'})))
            assert not links.data['items'], wire(links)
        # Reads may serialize before or after revoke, but every call started
        # after the mutation completes must reject, even on reused clients.
        results = await asyncio.gather(
            *(adapter.call(packet('discovery.get', {'id': rid})) for adapter in adapters),
            invoke(app, owner, 'sharing.revoke', {'grant_id': gid}, version=2))
        ok(results[-1])
        for adapter in adapters:
            for args in ({'id': rid}, {'id': rid, 'revision': revision}):
                denied = await adapter.call(packet('discovery.get', args))
                assert denied.error.code == 'permission_denied', wire(denied)


@pytest.mark.asyncio
async def test_share_link_every_supported_transport_and_explicit_get_rejection(installed):
    app, _ = installed
    owner = await register(app, 'link-matrix-owner')
    rid, _ = await private_post(app, owner)
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('share_links_enabled', True)
    token, verifier = proof()
    created = ok(await invoke(app, owner, 'sharing.link_create', {
        'resource': rid, 'verifier': verifier,
        'expires_at': wire(NOW + timedelta(hours=1))}))
    lid = created.data['link']['id']
    packet = request_for('sharing.link_read', {'link_id': lid, 'token': token},
                         app.settings.service_url, expires_at=NOW + timedelta(seconds=60))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        adapters = [kind(app.settings.service_url, http=http) for kind in
                    (HTTPTransport, GraphQLTransport, MCPHTTPTransport)]
        for adapter in adapters:
            result = ok(await adapter.call(packet))
            assert set(result.data) == {'id', 'revision', 'media_type', 'data'}
        with pytest.raises(Failure):
            await PathGETTransport(app.settings.service_url, http=http).call(packet)
        ok(await invoke(app, owner, 'sharing.link_revoke', {'link_id': lid}))
        for adapter in adapters:
            result = await adapter.call(packet)
            assert result.error.code == 'share_link_unavailable', wire(result)


@pytest.mark.asyncio
async def test_actual_postgres_restore_preserves_revocation_and_filters_notifications(installed, tmp_path, pg_dsn):
    app, _ = installed
    owner = await register(app, 'restored-owner')
    reader = await register(app, 'restored-reader')
    rid, revision = await private_post(app, owner)
    gid = await grant(app, owner, rid, reader[1])
    control = await register(app, 'restored-live-source-control')
    live_gid = await grant(app, owner, rid, control[1])
    ok(await invoke(app, control, 'discovery.get', {'id': rid}))
    ok(await invoke(app, owner, 'communication.send', {
        'recipient': reader[1], 'resource': {'id': rid, 'revision': revision}}))
    ok(await invoke(app, reader, 'discovery.get', {'id': rid}))
    ok(await invoke(app, owner, 'sharing.revoke', {'grant_id': gid}, version=2))
    # Current-authorization and projection evidence is independent of the restore
    # gate. Exercise the real signed adapters/handlers before taking the backup;
    # later compare their persisted inputs exactly, not a mock permission result.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        for kind in (HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport):
            adapter = kind(app.settings.service_url, http=http)
            for args in ({'id': rid}, {'id': rid, 'view': 'history'}, {'id': rid, 'revision': revision}):
                packet = request_for('discovery.get', args, app.settings.service_url,
                    signer=reader[0], subject=reader[1], expires_at=NOW + timedelta(seconds=60))
                denied = await adapter.call(packet)
                assert denied.error.code == 'permission_denied', wire(denied)
    for operation in ('communication.inbox', 'communication.outbox', 'communication.sync'):
        projected = ok(await invoke(app, reader, operation, {}))
        assert 'source-matrix-private' not in canonical(projected.data).decode()
        if operation != 'communication.sync':
            assert rid not in canonical(projected.data).decode()
    async def projection_inputs(tx):
        return {
            'resources': tx.rows('SELECT id,body FROM resources ORDER BY id'),
            'grants': tx.rows('SELECT * FROM share_grants_v2 ORDER BY id'),
            'messages': tx.rows('SELECT * FROM messages ORDER BY id'),
            'events': tx.rows('SELECT seq,body FROM events ORDER BY seq'),
            'memberships': tx.rows('SELECT * FROM memberships ORDER BY org,subject'),
            'credentials': tx.rows('SELECT * FROM credentials ORDER BY id'),
        }
    async with app.metadata.transaction(write=False) as tx:
        before = await projection_inputs(tx)
        assert before['messages']  # Nonempty input: not an empty-success test.
    archive = tmp_path / 'revoked.zip'
    await backup(app, archive)
    config = tmp_path / 'restored-etc'
    result = restore(archive, config, tmp_path / 'restored-data', postgres_dsn=pg_dsn)
    assert result['outbound'] == 'disabled_recovery_drill'
    restored = Application(load_settings(config), clock=lambda: NOW)
    await restored.load()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(restored)),
                                     base_url=restored.settings.service_url) as http:
            for kind in (HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport):
                adapter = kind(restored.settings.service_url, http=http)
                for args in ({'id': rid}, {'id': rid, 'view': 'history'}, {'id': rid, 'revision': revision}):
                    packet = request_for('discovery.get', args, restored.settings.service_url,
                        signer=reader[0], subject=reader[1], expires_at=NOW + timedelta(seconds=60))
                    # Discovery itself is a business read; fresh clients must
                    # not get even the operation catalogue from a restored ACL.
                    with pytest.raises(Failure, match='^recovery_quarantined$'):
                        await adapter.call(packet)
            for path in ('/', '/_transports', '/_r/' + rid, '/_r/' + rid + '/history'):
                response = await http.get(path)
                assert response.status_code == 503
                assert response.json() == {'error': {'code': 'recovery_quarantined', 'retryable': False}}
        for operation in ('communication.inbox', 'communication.outbox', 'communication.sync'):
            denied = await invoke(restored, reader, operation, {})
            assert denied.error.code == 'recovery_quarantined', wire(denied)
        async with restored.metadata.transaction(write=False) as tx:
            assert await projection_inputs(tx) == before
            resource = await tx.resource(rid)
            # The actual low-level live-source resolver is separately exercised
            # on restored rows. It cannot authorize an external request or clear
            # quarantine, and must still reject the revoked source.
            assert not await restored.authorizer.share_source_active(
                resource, gid, reader[1], NOW, tx)
            assert await restored.authorizer.share_source_active(
                resource, live_gid, control[1], NOW, tx)
            assert tx.one('SELECT revoked_at FROM share_grants_v2 WHERE id=?', (gid,))[0] == wire(NOW)
            assert tx.one("SELECT value FROM settings WHERE key='recovery_quarantine'") is not None
        assert await EffectWorker(restored).run_once() is False
    finally:
        await restored.close()
