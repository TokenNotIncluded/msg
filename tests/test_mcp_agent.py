"""日常 MCP 工具通过真实 HTTP、OAuth、PostgreSQL 和统一执行器验证。"""

from datetime import timedelta

import httpx
import pytest
import test_mcp_oauth
import test_oauth
from test_mcp_oauth import mcp_tokens
from test_service import NOW, call, register

from msg.core.codec import canonical, wire
from msg.core.requests import request_for
from msg.transports.http import create_app

# 复用现有身份登录路径，不在测试中伪造 principal 或执行器。
oauth = test_oauth.oauth
mcp_oauth = test_mcp_oauth.mcp_oauth


async def rpc(http, method, params=None, *, rpc_id=1, bearer=None, session=None):
    headers = {}
    if bearer:
        headers['Authorization'] = 'Bearer ' + bearer
    if session:
        headers['MCP-Session-Id'] = session
    value = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
    if rpc_id is not None:
        value['id'] = rpc_id
    return await http.post('/-/mcp', headers=headers, json=value)


async def tool(http, name, arguments=None, **kwargs):
    return await rpc(http, 'tools/call', {'name': name, 'arguments': arguments or {}}, **kwargs)


def data(response):
    assert response.status_code == 200, response.text
    result = response.json()['result']['structuredContent']
    assert result['status'] == 'ok', result
    return result['data']


@pytest.mark.asyncio
async def test_default_catalog_connect_and_public_read_without_wire(oauth):
    app, _, _, http = oauth
    initialized = await rpc(http, 'initialize', {'protocolVersion': '2025-11-25'})
    assert initialized.status_code == 200
    session = initialized.headers['mcp-session-id']
    listed = await rpc(http, 'tools/list', session=session)
    tools = listed.json()['result']['tools']
    assert {item['name'] for item in tools} == {
        'msg_me',
        'msg_connect',
        'msg_resolve',
        'msg_read',
        'msg_list',
        'msg_search',
    }
    forbidden = {
        'packet',
        'proof',
        'signature',
        'credential_id',
        'token',
        'key_id',
        'request_id',
        'payload_digest',
        'expires_at',
        'expected_generations',
        'target_service',
        'protocol_version',
        'contract_version',
    }

    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(v) for v in value))
        return set()

    assert not forbidden & keys([item['inputSchema'] for item in tools])
    assert data(await tool(http, 'msg_me', session=session))['authenticated'] is False
    connect = await tool(http, 'msg_connect', session=session)
    assert connect.json()['result']['isError'] is True
    challenges = connect.json()['result']['_meta']['mcp/www_authenticate']
    assert len(challenges) == 1 and 'resource_metadata=' in challenges[0]
    assert all(
        scope in challenges[0] for scope in ('msg.mcp.read', 'msg.mcp.message', 'msg.mcp.post')
    )
    resolved = data(
        await tool(http, 'msg_resolve', {'address': '/AGENTS.md'}, session=session, rpc_id=2)
    )
    read = data(
        await tool(
            http, 'msg_read', {'ref': resolved['ref'], 'max_bytes': 512}, session=session, rpc_id=3
        )
    )
    assert read['text'] and len(read['text'].encode()) <= 512
    assert 'proof' not in read and 'payload_digest' not in read
    assert (
        data(await tool(http, 'msg_read', {'address': '/main'}, session=session, rpc_id=4))['type']
        == 'topic'
    )
    inbox = await tool(http, 'msg_inbox', session=session, rpc_id=5)
    assert inbox.json()['result']['structuredContent']['error']['code'] == 'authentication_required'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0


@pytest.mark.asyncio
async def test_oauth_identity_post_retry_restart_and_no_secret(mcp_oauth):
    app, _, subject, http = mcp_oauth
    tokens = await mcp_tokens(mcp_oauth, 'msg.mcp.read msg.mcp.message msg.mcp.post offline_access')
    bearer = tokens['access_token']
    initialized = await rpc(http, 'initialize', bearer=bearer)
    session = initialized.headers['mcp-session-id']
    me = data(await tool(http, 'msg_me', bearer=bearer, session=session, rpc_id=2))
    assert me['authenticated'] is True and me['subject'] == subject
    assert me['handle'] == '@oauth-owner'
    assert me['credential_type'] == 'oauth'
    assert (
        data(await tool(http, 'msg_connect', bearer=bearer, session=session, rpc_id='connected'))[
            'authenticated'
        ]
        is True
    )
    listed = await rpc(http, 'tools/list', bearer=bearer, session=session, rpc_id=3)
    names = {item['name'] for item in listed.json()['result']['tools']}
    assert {'msg_inbox', 'msg_send', 'msg_reply', 'msg_post', 'msg_ack'} <= names
    assert len(names) <= 13
    args = {'topic': '/main', 'title': '日常标题 / with spaces', 'body': 'idempotent post'}
    first = await tool(http, 'msg_post', args, bearer=bearer, session=session, rpc_id='write-one')
    first_data = data(first)
    assert first_data.get('id') or first_data.get('refs')
    second = await tool(http, 'msg_post', args, bearer=bearer, session=session, rpc_id='write-one')
    assert data(second) == first_data
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as restarted:
        repeated = await tool(
            restarted, 'msg_post', args, bearer=bearer, session=session, rpc_id='write-one'
        )
        assert data(repeated) == first_data
    conflict = await tool(
        http,
        'msg_post',
        dict(args, body='changed'),
        bearer=bearer,
        session=session,
        rpc_id='write-one',
    )
    assert conflict.json()['result']['structuredContent']['error']['code'] == 'jsonrpc_id_conflict'
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one("SELECT COUNT(*) FROM resources WHERE type='post' AND parent='t_main'")[0] == 1
        )
    all_outputs = canonical([
        initialized.json(),
        me,
        listed.json(),
        first.json(),
        second.json(),
    ]).decode()
    credential, _, encoded = bearer.partition('.')
    assert credential not in all_outputs and encoded not in all_outputs
    assert bearer not in all_outputs and tokens['refresh_token'] not in all_outputs


@pytest.mark.asyncio
async def test_read_scope_cannot_write_and_notifications_cannot_execute(mcp_oauth):
    app, _, _, http = mcp_oauth
    tokens = await mcp_tokens(mcp_oauth)
    bearer = tokens['access_token']
    session = (await rpc(http, 'initialize', bearer=bearer)).headers['mcp-session-id']
    names = {
        item['name']
        for item in (await rpc(http, 'tools/list', bearer=bearer, session=session)).json()[
            'result'
        ]['tools']
    }
    assert 'msg_inbox' in names
    assert not {'msg_post', 'msg_send', 'msg_ack'} & names
    denied = await tool(
        http, 'msg_post', {'topic': '/main', 'body': 'forbidden'}, bearer=bearer, session=session
    )
    assert denied.json()['result']['structuredContent']['error']['code'] == 'credential_ceiling'
    assert 'msg.mcp.post' in denied.json()['result']['_meta']['mcp/www_authenticate'][0]
    notification = await tool(
        http,
        'msg_post',
        {'topic': '/main', 'body': 'notify'},
        bearer=bearer,
        session=session,
        rpc_id=None,
    )
    assert notification.status_code == 202
    injected = await tool(
        http, 'msg_me', {'token': 'never-consumed'}, bearer=bearer, session=session, rpc_id=3
    )
    assert injected.json()['result']['isError'] is True
    invalid = await rpc(http, 'tools/call', {'name': 7}, bearer=bearer, session=session, rpc_id=4)
    assert invalid.json()['error']['message'] == 'invalid_jsonrpc_params'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0


@pytest.mark.asyncio
async def test_direct_reply_and_archived_conversation_are_private(mcp_oauth):
    app, key, subject, http = mcp_oauth
    other_key, other, _ = await register(app, 'mcp-other')
    requested = await call(
        app, 'communication.dm_request', {'recipient': other}, key=key, subject=subject
    )
    assert requested.status == 'ok', wire(requested)
    conversation = requested.data['conversation_id']
    accepted = await call(
        app,
        'communication.dm_accept',
        {'conversation_id': conversation},
        key=other_key,
        subject=other,
    )
    assert accepted.status == 'ok', wire(accepted)
    tokens = await mcp_tokens(mcp_oauth, 'msg.mcp.read msg.mcp.message msg.mcp.post')
    bearer = tokens['access_token']
    session = (await rpc(http, 'initialize', bearer=bearer)).headers['mcp-session-id']
    sent = data(
        await tool(
            http,
            'msg_send',
            {'recipient': '@mcp-other', 'body': 'private hello'},
            bearer=bearer,
            session=session,
            rpc_id=2,
        )
    )
    assert sent['private'] is True and sent['conversation_id'] == conversation
    ref = sent['refs'][0]
    archived = await call(
        app, 'communication.dm_archive', {'conversation_id': conversation}, key=key, subject=subject
    )
    assert archived.status == 'ok'
    denied = await tool(
        http,
        'msg_reply',
        {'post_ref': ref, 'body': 'no public bypass'},
        bearer=bearer,
        session=session,
        rpc_id=3,
    )
    assert denied.json()['result']['structuredContent']['error']['code'] == 'dm_reference_private'
    private = data(
        await tool(
            http,
            'msg_reply',
            {'message_ref': ref, 'body': 'private reply'},
            bearer=bearer,
            session=session,
            rpc_id=4,
        )
    )
    assert private['private'] is True and private['conversation_id'] == conversation
    assert (
        data(await tool(http, 'msg_read', {'ref': ref}, bearer=bearer, session=session, rpc_id=5))[
            'text'
        ]
        == 'private hello'
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as anonymous:
        unavailable = await tool(anonymous, 'msg_read', {'ref': ref})
        assert unavailable.json()['result']['isError'] is True
        assert 'private hello' not in unavailable.text
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one("SELECT COUNT(*) FROM reactions WHERE kind IN ('ack.signature','ack.token')")[0]
            == 0
        )


@pytest.mark.asyncio
async def test_raw_sdk_compatibility_unadvertised_on_default_entry(oauth):
    app, key, subject, http = oauth
    packet = request_for(
        'content.post_create',
        {'parent': '/main', 'body': 'old SDK'},
        app.settings.service_url,
        subject=subject,
        signer=key,
        expires_at=NOW + timedelta(seconds=90),
    )
    result = await tool(http, 'content.post_create', {'packet': wire(packet)})
    assert result.json()['result']['structuredContent']['status'] == 'ok', result.text
    advanced = await http.post(
        '/-/mcp/raw', json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}
    )
    assert 'packet' in advanced.json()['result']['tools'][0]['inputSchema']['properties']


@pytest.mark.asyncio
async def test_session_upgrade_isolation_and_concurrent_same_id(mcp_oauth):
    app, _, _, http = mcp_oauth
    anonymous_session = (await rpc(http, 'initialize')).headers['mcp-session-id']
    one = await mcp_tokens(mcp_oauth, 'msg.mcp.read msg.mcp.post')
    bearer = one['access_token']
    upgraded = await tool(http, 'msg_me', bearer=bearer, session=anonymous_session, rpc_id=2)
    assert data(upgraded)['authenticated'] is True
    session = upgraded.headers['mcp-session-id']
    assert session != anonymous_session
    second = await mcp_tokens(mcp_oauth, 'msg.mcp.read msg.mcp.post')
    borrowed = await tool(http, 'msg_me', bearer=second['access_token'], session=session, rpc_id=3)
    assert borrowed.json()['error']['message'] == 'mcp_session_invalid'
    invalid = await tool(http, 'msg_me', bearer=bearer, session='forged.session', rpc_id=4)
    assert invalid.json()['error']['message'] == 'mcp_session_invalid'
    missing = await tool(
        http, 'msg_post', {'topic': '/main', 'body': 'needs session'}, bearer=bearer, rpc_id=5
    )
    assert missing.json()['result']['structuredContent']['error']['code'] == 'mcp_session_required'
    import asyncio

    args = {'topic': '/main', 'body': 'concurrent exactly once'}
    results = await asyncio.gather(
        *(
            tool(http, 'msg_post', args, bearer=bearer, session=session, rpc_id='concurrent')
            for _ in range(4)
        )
    )
    assert all(data(result) == data(results[0]) for result in results)
    separate = (await rpc(http, 'initialize', bearer=bearer)).headers['mcp-session-id']
    data(await tool(http, 'msg_post', args, bearer=bearer, session=separate, rpc_id='concurrent'))
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one("SELECT COUNT(*) FROM resources WHERE type='post' AND parent='t_main'")[0] == 2
        )


@pytest.mark.asyncio
async def test_invitation_retry_acceptance_and_send_keep_one_write_plan(mcp_oauth):
    app, _, _, http = mcp_oauth
    other_key, other, _ = await register(app, 'mcp-invited')
    tokens = await mcp_tokens(mcp_oauth, 'msg.mcp.read msg.mcp.message')
    bearer = tokens['access_token']
    session = (await rpc(http, 'initialize', bearer=bearer)).headers['mcp-session-id']
    args = {'recipient': '@mcp-invited', 'body': 'hello, can we talk?'}
    invited = data(
        await tool(http, 'msg_send', args, bearer=bearer, session=session, rpc_id='invite')
    )
    assert invited['state'] == 'pending' and invited['needs_acceptance'] is True
    assert invited['sent'] is False
    repeated = data(
        await tool(http, 'msg_send', args, bearer=bearer, session=session, rpc_id='invite')
    )
    assert repeated == invited
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as restarted:
        assert (
            data(
                await tool(
                    restarted, 'msg_send', args, bearer=bearer, session=session, rpc_id='invite'
                )
            )
            == invited
        )
    other_context = (app, other_key, other, http)
    other_tokens = await mcp_tokens(other_context, 'msg.mcp.read msg.mcp.message')
    other_bearer = other_tokens['access_token']
    other_session = (await rpc(http, 'initialize', bearer=other_bearer)).headers['mcp-session-id']
    accepted = data(
        await tool(
            http,
            'msg_dm_decide',
            {'ref': {'id': invited['conversation_id']}, 'accept': True},
            bearer=other_bearer,
            session=other_session,
            rpc_id='accept',
        )
    )
    assert accepted['state'] == 'active'
    assert (
        data(await tool(http, 'msg_send', args, bearer=bearer, session=session, rpc_id='invite'))
        == invited
    )
    new_args = dict(args, body='active message ' * 100)
    actual = data(
        await tool(http, 'msg_send', new_args, bearer=bearer, session=session, rpc_id='active')
    )
    assert actual['sent'] is True
    assert (
        data(
            await tool(http, 'msg_send', new_args, bearer=bearer, session=session, rpc_id='active')
        )
        == actual
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as restarted:
        assert (
            data(
                await tool(
                    restarted, 'msg_send', new_args, bearer=bearer, session=session, rpc_id='active'
                )
            )
            == actual
        )
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT COUNT(*) FROM resources WHERE parent=? AND type=?',
                (invited['conversation_id'], 'post'),
            )[0]
            == 2
        )
    inbox = data(
        await tool(
            http,
            'msg_inbox',
            {'limit': 20},
            bearer=other_bearer,
            session=other_session,
            rpc_id='inbox',
        )
    )
    assert inbox['items']
    assert any(
        item.get('sender_contact', {}).get('name') == '@oauth-owner' for item in inbox['items']
    )
    assert new_args['body'] not in canonical(inbox).decode()
