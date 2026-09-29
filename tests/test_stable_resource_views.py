"""Stable resource-ID projections keep authorization and canonical paths separate."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_stable_id_views_survive_move_and_reuse_existing_projections(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'stable-view-owner')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'stable body'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    rid, revision = created.resources[0].id, created.resources[0].revision
    base = f'/_r/{rid}'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        first = await http.get(base + '/json')
        assert first.status_code == 200, first.text
        assert first.json()['id'] == rid
        assert (await http.get(base + '/raw')).content == b'stable body'
        assert (await http.get(base + '/rev/' + revision)).status_code == 200

        moved = await call(
            app,
            'content.move',
            {'id': rid, 'parent': '/intro', 'name': 'renamed.md'},
            key=key,
            subject=uid,
            certs=(cert,),
            expected=((rid, created.data['generation']),),
        )
        assert moved.status == 'ok'
        async with app.metadata.transaction(write=False) as tx:
            events_before = tx.one('SELECT COUNT(*) FROM events')[0]
        after = await http.get(base + '/json')
        assert after.status_code == 200, after.text
        assert after.json()['path'] == '/intro/renamed.md'
        assert after.json()['id'] == rid
        assert (await http.get(base + '/raw')).content == b'stable body'
        ranged = await http.get(base + '/raw', headers={'Range': 'bytes=0-5'})
        assert ranged.status_code == 206 and ranged.content == b'stable'
        assert (await http.get(base + '/rev/' + revision)).status_code == 200
        meta = await http.get(base + '/meta')
        history = await http.get(base + '/history')
        assert meta.status_code == history.status_code == 200
        assert meta.json()['id'] == history.json()['id'] == rid
        assert (await http.post(base + '/json')).status_code == 405
        assert (await http.get(base + '/status')).status_code == 404
        alternate = '/_r/%' + format(ord(rid[0]), '02X') + rid[1:] + '/json'
        assert (await http.get(alternate)).status_code == 404
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT COUNT(*) FROM events')[0] == events_before


@pytest.mark.asyncio
async def test_private_stable_views_do_not_disclose_via_head_etag_or_status(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'stable-private-owner')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'hidden body'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    rid, revision = created.resources[0].id, created.resources[0].revision
    await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0600'},
        key=key,
        subject=uid,
        certs=(cert,),
        expected=((rid, created.data['generation']),),
    )
    base = f'/_r/{rid}'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        for suffix in ('/json', '/meta', '/raw', '/history', '/rev/' + revision):
            for method in (http.get, http.head):
                response = await method(base + suffix, headers={'If-None-Match': '*'})
                assert response.status_code == 403, (suffix, method.__name__, response.text)
                assert 'etag' not in response.headers
                assert 'content-range' not in response.headers
        missing = await http.get(base + '/status')
        assert missing.status_code == 404
        assert 'etag' not in missing.headers

        signed = request_for(
            'discovery.get',
            {'id': rid, 'view': 'meta'},
            app.settings.service_url,
            signer=key,
            subject=uid,
            expires_at=NOW + timedelta(seconds=120),
        )
        headers = {'X-Msg-Request': b64(canonical(signed))}
        permitted = await http.get(base + '/meta', headers=headers)
        assert permitted.status_code == 200, permitted.text
        assert permitted.json()['id'] == rid
        head = await http.head(base + '/meta', headers=headers)
        assert head.status_code == 200 and head.content == b''
        assert head.headers['etag'] == permitted.headers['etag']
