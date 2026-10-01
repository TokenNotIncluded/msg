from datetime import timedelta

import httpx
import pytest
from read_only_evidence import business_snapshot, readonly_evidence
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.client import (
    GraphQLTransport,
    HTTPTransport,
    MCPHTTPTransport,
    PathGETTransport,
)
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_feed_personalizes_explicit_follows_and_interests_without_private_content(installed):
    app, _ = installed
    ak, alice, _ = await register(app, 'feed-alice')
    bk, bob, _ = await register(app, 'feed-bob')
    ck, carol, _ = await register(app, 'feed-carol')

    async def invoke(op, args, key=ak, subject=alice, **kw):
        result = await call(app, op, args, key=key, subject=subject, **kw)
        assert result.status == 'ok', wire(result)
        return result

    followed = await invoke(
        'content.post_create',
        {'parent': '/main', 'name': 'Useful', 'body': 'work'},
        key=bk,
        subject=bob,
    )
    general = await invoke(
        'content.post_create',
        {'parent': '/main', 'name': 'Python', 'body': 'code'},
        key=ck,
        subject=carol,
    )
    await invoke(
        'content.tags_set',
        {'id': general.resources[0].id, 'tags': ['python']},
        key=ck,
        subject=carol,
        expected=((general.resources[0].id, general.data['generation']),),
    )
    private = await invoke(
        'content.post_create', {'parent': '/main', 'body': 'PRIVATE-BODY'}, key=bk, subject=bob
    )
    await invoke(
        'content.chmod',
        {'id': private.resources[0].id, 'mode': '0600'},
        key=bk,
        subject=bob,
        expected=((private.resources[0].id, private.data['generation']),),
    )
    dm = await invoke('communication.dm_request', {'recipient': bob})
    await invoke(
        'communication.dm_accept',
        {'conversation_id': dm.data['conversation_id']},
        key=bk,
        subject=bob,
    )
    message = await invoke(
        'communication.dm_send',
        {
            'conversation_id': dm.data['conversation_id'],
            'body': 'PRIVATE-DM-BODY',
        },
    )
    await invoke('communication.follow', {'id': bob})
    before = await business_snapshot(app)
    personal = await invoke('discovery.recommendations', {})
    assert personal.data['algorithm'] == 'msg for bot need'
    assert personal.data['items'][0]['id'] == followed.resources[0].id
    assert 'followed_author' in personal.data['items'][0]['reasons']
    anonymous = await call(app, 'discovery.recommendations', {'interests': ['python']})
    assert anonymous.status == 'ok', wire(anonymous)
    assert anonymous.data['items'][0]['id'] == general.resources[0].id
    owner = await invoke('discovery.recommendations', {}, key=bk, subject=bob)
    for result in (personal, anonymous, owner):
        raw = str(wire(result))
        assert private.resources[0].id not in raw and message.resources[0].id not in raw
        assert 'PRIVATE-' not in raw
    assert await business_snapshot(app) == before
    await invoke('communication.dm_block', {'subject_id': bob})
    after_block = await invoke('discovery.recommendations', {})
    assert all(item['author'] != bob for item in after_block.data['items'])


@pytest.mark.asyncio
async def test_feed_http_and_transports_are_bounded_readonly_and_reauthorize(
    installed, monkeypatch
):
    app, _ = installed
    key, subject, _ = await register(app, 'feed-http')
    created = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'public'}, key=key, subject=subject
    )
    assert created.status == 'ok', wire(created)
    args = {'limit': 10, 'interests': ['python']}
    packet = request_for(
        'discovery.recommendations',
        args,
        app.settings.service_url,
        signer=key,
        subject=subject,
        expires_at=NOW + timedelta(seconds=120),
    )
    headers = {'X-Msg-Request': b64(canonical(packet))}
    async with readonly_evidence(app, monkeypatch):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
        ) as http:
            expected = None
            for kind in (HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport):
                result = await kind(app.settings.service_url, http=http).call(packet)
                assert result.status == 'ok', wire(result)
                if expected is None:
                    expected = wire(result.data)
                assert wire(result.data) == expected
            response = await http.get('/feed?limit=10&interests=python', headers=headers)
            assert response.status_code == 200 and response.json() == expected
            assert response.headers['cache-control'] == 'no-store'
            head = await http.head('/feed?limit=10&interests=python', headers=headers)
            assert head.status_code == 200 and not head.content
            assert head.headers['content-length'] == str(len(response.content))
            assert (await http.post('/feed')).status_code == 405
            for query in ('limit=101', 'limit=0', 'limit=1&limit=2', 'unknown=1', 'interests='):
                assert (await http.get('/feed?' + query)).status_code == 400, query
            mismatch = await http.get('/feed?limit=1', headers=headers)
            assert mismatch.status_code == 400
    changed = await call(
        app,
        'content.chmod',
        {'id': created.resources[0].id, 'mode': '0600'},
        key=key,
        subject=subject,
        expected=((created.resources[0].id, created.data['generation']),),
    )
    assert changed.status == 'ok', wire(changed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        fresh = await http.get('/feed', headers={'If-None-Match': 'old'})
        assert fresh.status_code == 200 and fresh.json()['items'] == []
