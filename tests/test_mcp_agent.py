"""日常 MCP 工具通过真实 HTTP、原生签名、PostgreSQL 和统一执行器验证。"""

from datetime import timedelta

import pytest
from test_browser_sessions import signed_browser as signed_browser
from test_service import NOW

from msg.core.codec import wire
from msg.core.requests import request_for

# 复用现有身份登录路径，不在测试中伪造 principal 或执行器。


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
async def test_default_catalog_connect_and_public_read_without_wire(signed_browser):
    app, _, _, http = signed_browser
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
    connect = await tool(http, 'msg_connect', session=session, rpc_id='connect')
    assert data(connect)['authentication'] == 'public-key-signature'
    assert '_meta' not in connect.json()['result']
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
async def test_raw_sdk_compatibility_unadvertised_on_default_entry(signed_browser):
    app, key, subject, http = signed_browser
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
