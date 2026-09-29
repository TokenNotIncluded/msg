"""All network adapters reject legacy token issuance before writing state."""

import os

import httpx
import pytest

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.http import create_app


async def _state_counts(app):
    async with app.metadata.transaction(write=False) as tx:
        return tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('identities', 'credentials', 'events', 'results', 'token_deliveries')
        )


def _assert_no_token_field(value):
    if isinstance(value, dict):
        assert 'token' not in value
        for nested in value.values():
            _assert_no_token_field(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_token_field(nested)


def _legacy_packet(app, suffix):
    return request_for(
        'identity.custodial_create',
        {
            'handle': 'legacy-entry-' + suffix,
            'nonce': b64(os.urandom(32)),
        },
        app.settings.service_url,
        request_id='legacy-entry-' + suffix,
        contract_version=1,
    )


def _assert_rejected(result):
    assert result['status'] == 'error', result
    assert result['error']['code'] == 'credential_delivery_upgrade_required', result
    _assert_no_token_field(result)


@pytest.mark.asyncio
async def test_old_custodial_issuance_packet_rejected_by_http_graphql_and_mcp(installed):
    app, _ = installed
    asgi = create_app(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi),
        base_url='http://testserver',
        headers={
            'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-11-25',
        },
    ) as http:
        packet = _legacy_packet(app, 'http')
        before = await _state_counts(app)
        response = await http.post('/-/p/identity.custodial_create', content=canonical(packet))
        assert response.status_code == 400, response.text
        _assert_rejected(response.json())
        assert await _state_counts(app) == before

        packet = _legacy_packet(app, 'graphql')
        before = await _state_counts(app)
        response = await http.post(
            '/-/graphql',
            json={
                'query': 'mutation($p: JSON!) { call(packet: $p) }',
                'variables': {'p': wire(packet)},
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()['data']['call']
        _assert_rejected(result)
        assert await _state_counts(app) == before

        packet = _legacy_packet(app, 'mcp')
        before = await _state_counts(app)
        response = await http.post(
            '/-/mcp',
            json={
                'jsonrpc': '2.0',
                'id': 3,
                'method': 'tools/call',
                'params': {
                    'name': 'identity.custodial_create',
                    'arguments': {'packet': wire(packet)},
                },
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()['result']['structuredContent']
        _assert_rejected(result)
        assert await _state_counts(app) == before
