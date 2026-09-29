import gzip
from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_http_post_path_get_and_head_have_exact_effects(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'http-agent')
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as http:
        packet = request_for(
            'content.post_create',
            {'parent': '/main', 'body': 'same operation'},
            app.settings.service_url,
            signer=key,
            subject=uid,
            request_id='http-one',
            expires_at=NOW + timedelta(seconds=90),
            source='manual',
        )
        path = '/-/g/content.post_create/j/' + b64(canonical(packet))
        before = await http.head(path)
        assert before.status_code == 200
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0
        post = await http.post('/-/p/content.post_create', content=canonical(packet))
        assert post.status_code == 200, post.text
        assert post.json()['prefer_cli'] is True
        get = await http.get(path)
        assert get.status_code == 200 and get.json()['replayed'], get.text
        assert 'http-one' == get.json()['request_id']
        forbidden = await http.get('/~content.post_create/run/j/' + b64(canonical(packet)))
        assert forbidden.status_code == 404
        describe = await http.get('/-/d/content.post_create')
        assert describe.status_code == 200
        raw = await http.get('/_id/' + post.json()['resources'][0]['id'] + '/raw')
        assert raw.content == b'same operation', raw.text
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 1


@pytest.mark.asyncio
async def test_http_gzip_packet_and_read_views_do_not_bypass_acl(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'views-agent')
    p = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'private message'},
        key=key,
        subject=uid,
    )
    rid = p.resources[0].id
    await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0600'},
        key=key,
        subject=uid,
        expected=((rid, p.data['generation']),),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        for suffix in (
            '',
            '/json',
            '/meta',
            '/raw',
            '/history',
            '/revisions/' + p.resources[0].revision,
        ):
            result = await http.get('/_id/' + rid + suffix)
            assert result.status_code == 403, (suffix, result.text)
        packet = request_for(
            'discovery.get',
            {'id': rid},
            app.settings.service_url,
            signer=key,
            subject=uid,
            expires_at=NOW + timedelta(seconds=90),
        )
        response = await http.get(
            '/-/g/discovery.get/gz/' + b64(gzip.compress(canonical(packet), mtime=0))
        )
        assert response.status_code == 200, response.text
        assert 'private message' in response.text
        bad = await http.post('/-/p/content.post_create', content=b'{"a":1,"a":2}')
        assert bad.status_code == 400
        assert bad.json()['error']['code'] == 'duplicate_json_key'
        wrong_origin = await http.post(
            '/-/mcp',
            headers={'Origin': 'https://untrusted.example'},
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'initialize',
                'params': {'protocolVersion': '2025-11-25'},
            },
        )
        assert wrong_origin.status_code == 403


@pytest.mark.asyncio
async def test_mcp_streamable_http_lists_transfers_and_calls_same_executor(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'mcp-agent')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url='http://testserver',
        headers={
            'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-11-25',
        },
    ) as http:
        init = await http.post(
            '/-/mcp',
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-11-25',
                    'capabilities': {},
                    'clientInfo': {'name': 'test', 'version': '1'},
                },
            },
        )
        assert init.status_code == 200, init.text
        assert init.json()['result']['capabilities']['tools'] is not None
        names = []
        cursor = None
        while True:
            response = await http.post(
                '/-/mcp',
                json={
                    'jsonrpc': '2.0',
                    'id': 2,
                    'method': 'tools/list',
                    'params': {'cursor': cursor} if cursor else {},
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()['result']
            names += [tool['name'] for tool in result['tools']]
            cursor = result.get('nextCursor')
            if cursor is None:
                break
        assert {
            'transfer.open',
            'transfer.part_put',
            'transfer.part_get',
            'transfer.status',
            'transfer.seal',
            'transfer.cancel',
        } <= set(names)
        assert not any('root' in n for n in names)
        packet = request_for(
            'content.post_create',
            {'parent': '/main', 'body': 'MCP post'},
            app.settings.service_url,
            signer=key,
            subject=uid,
            expires_at=NOW + timedelta(seconds=90),
        )
        params = {'name': 'content.post_create', 'arguments': {'packet': wire(packet)}}
        notification = await http.post(
            '/-/mcp', json={'jsonrpc': '2.0', 'method': 'tools/call', 'params': params}
        )
        assert notification.status_code == 202
        response = await http.post(
            '/-/mcp', json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': params}
        )
        assert response.json()['result']['structuredContent']['status'] == 'ok', response.text
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 1
