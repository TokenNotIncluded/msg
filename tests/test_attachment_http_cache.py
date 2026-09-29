"""An authorized parent/LinkSet never makes a revoked attachment cache valid."""

import httpx
from http_read_cache_matrix import assert_read_cache_matrix
from read_only_evidence import readonly_evidence
from test_authorization_sources import grant, private_post
from test_service import register
from test_share_grants_v2 import invoke, ok

from msg.core.codec import b64, wire
from msg.transports.http import create_app


async def test_attachment_bytes_history_and_linkset_recheck_current_grant(installed, monkeypatch):
    app, _ = installed
    owner = await register(app, 'attachment-cache-owner')
    reader = await register(app, 'attachment-cache-reader')
    post, _ = await private_post(app, owner)
    source = ok(
        await invoke(
            app,
            owner,
            'content.file_put',
            {
                'parent': '/@attachment-cache-owner/files',
                'name': 'payload.txt',
                'data': b64(b'source-matrix-private'),
                'media_type': 'text/plain',
            },
        )
    )
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(post)).generation
    attached = ok(
        await invoke(
            app,
            owner,
            'content.attach',
            {'post': post, 'source': wire(source.resources[0])},
            expected=((post, generation),),
        )
    )
    rid = attached.data['attachment']['id']
    async with app.metadata.transaction(write=False) as tx:
        revision = (await tx.resource(rid)).revision
    await grant(app, owner, post, reader[1])
    child_grant = await grant(app, owner, rid, reader[1])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        cache = {}
        async with readonly_evidence(app, monkeypatch):
            await assert_read_cache_matrix(http, app, reader, (), rid, revision, 'ok', cache)
            links = ok(await invoke(app, reader, 'discovery.links', {'id': post, 'rel': 'f'}))
            assert rid in str(links.data), wire(links)
        ok(await invoke(app, owner, 'sharing.revoke', {'grant_id': child_grant}, version=2))
        async with readonly_evidence(app, monkeypatch):
            await assert_read_cache_matrix(http, app, reader, (), rid, revision, 'error', cache)
            ok(await invoke(app, reader, 'discovery.get', {'id': post}))
            links = ok(await invoke(app, reader, 'discovery.links', {'id': post, 'rel': 'f'}))
            assert not links.data['items'] and rid not in str(links.data), wire(links)
            await assert_read_cache_matrix(http, app, owner, (), rid, revision, 'ok', {})
