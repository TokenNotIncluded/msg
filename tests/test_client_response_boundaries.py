"""Malformed gateways must not crash clients or promote response text to error codes."""

import httpx
import pytest

from msg.core.errors import Failure
from msg.core.requests import request_for
from msg.transports.client import GraphQLTransport, HTTPTransport, MCPHTTPTransport

SECRET = 'token=not-a-real-token-example'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 400, 503])
@pytest.mark.parametrize('payload', [None, False, 7, SECRET, [], [SECRET]])
async def test_non_object_json_is_a_stable_response_failure(status, payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    ) as http:
        transport = HTTPTransport('http://testserver', http=http)
        with pytest.raises(Failure) as caught:
            await transport._json('GET', '/_transports')
        assert caught.value.code == 'invalid_server_response'
        assert SECRET not in str(caught.value)
        assert transport.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [
        None,
        SECRET,
        [],
        {'code': SECRET},
        {'code': 123},
        {'code': 'UPPERCASE'},
        {'code': 'bad\ncode'},
    ],
)
async def test_gateway_error_field_never_becomes_exception_text(error):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={'error': error}))
    ) as http:
        transport = HTTPTransport('http://testserver', http=http)
        with pytest.raises(Failure) as caught:
            await transport._json('GET', '/_transports')
        assert caught.value.code == 'transport_error'
        assert caught.value.retryable is True
        assert SECRET not in str(caught.value)


@pytest.mark.asyncio
async def test_valid_gateway_code_and_success_object_are_preserved():
    responses = [
        httpx.Response(403, json={'error': {'code': 'permission_denied'}}),
        httpx.Response(200, json={'version': 1, 'items': []}),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: responses.pop(0))
    ) as http:
        transport = HTTPTransport('http://testserver', http=http)
        with pytest.raises(Failure) as caught:
            await transport._json('GET', '/_transports')
        assert caught.value.code == 'permission_denied'
        assert not caught.value.retryable
        assert await transport._json('GET', '/_transports') == {'version': 1, 'items': []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'transport_type,payload,expected',
    [
        (GraphQLTransport, {'errors': SECRET}, 'invalid_graphql_result'),
        (GraphQLTransport, {'errors': [None]}, 'invalid_graphql_result'),
        (GraphQLTransport, {'errors': [{'extensions': None}]}, 'graphql_error'),
        (GraphQLTransport, {'errors': [{'extensions': {'code': SECRET}}]}, 'graphql_error'),
        (
            GraphQLTransport,
            {'errors': [{'extensions': {'code': 'permission_denied'}}]},
            'permission_denied',
        ),
        (GraphQLTransport, {'data': None}, 'invalid_graphql_result'),
        (MCPHTTPTransport, {'error': None}, 'mcp_error'),
        (MCPHTTPTransport, {'error': SECRET}, 'mcp_error'),
        (MCPHTTPTransport, {'error': {'message': SECRET}}, 'mcp_error'),
        (MCPHTTPTransport, {'error': {'message': 'permission_denied'}}, 'permission_denied'),
        (MCPHTTPTransport, {'result': None}, 'invalid_mcp_result'),
    ],
)
async def test_protocol_error_shapes_fail_closed(transport_type, payload, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        transport = transport_type('http://testserver', http=http)
        transport._description = {'operations': {'discovery.get': 'read'}}
        packet = request_for('discovery.get', {'id': '/main'}, 'http://testserver')
        with pytest.raises(Failure) as caught:
            await transport.call(packet)
        assert caught.value.code == expected
        assert SECRET not in str(caught.value)
        assert transport.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'transport_type,expected',
    [(GraphQLTransport, 'invalid_graphql_result'), (MCPHTTPTransport, 'invalid_mcp_result')],
)
async def test_empty_accepted_response_is_not_a_completed_read(transport_type, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(202))
    ) as http:
        transport = transport_type('http://testserver', http=http)
        transport._description = {'operations': {'discovery.get': 'read'}}
        with pytest.raises(Failure) as caught:
            await transport.call(request_for('discovery.get', {'id': '/main'}, 'http://testserver'))
        assert caught.value.code == expected


@pytest.mark.parametrize(
    'server',
    [
        'http://:secret@example.invalid',
        'http://@example.invalid',
        'http://user@example.invalid',
        'http://user:secret@example.invalid',
        'http://testserver:broken',
        'http://testserver:99999',
        'http://testserver:0',
        'http://[broken',
        'http://testserver\n',
        'http://test\tserver',
        ' http://testserver',
        'http://testserver\\anything',
        None,
    ],
)
def test_server_origin_rejects_credential_slots_controls_and_invalid_ports(server):
    with pytest.raises(Failure) as caught:
        HTTPTransport(server)
    assert caught.value.code == 'invalid_server_url'
    assert 'secret' not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport_type', [HTTPTransport, GraphQLTransport, MCPHTTPTransport])
@pytest.mark.parametrize('code', [SECRET, '', 'UPPERCASE', 'a' * 97, 'bad\ncode'])
async def test_operation_error_envelope_cannot_leak_text_as_a_code(transport_type, code):
    packet = request_for('discovery.get', {'id': '/main'}, 'http://testserver')
    result = {
        'request_id': packet.request_id,
        'operation': packet.operation,
        'status': 'error',
        'error': {'code': code, 'retryable': False},
    }
    payload = (
        {'data': {'call': result}}
        if transport_type is GraphQLTransport
        else {'result': {'structuredContent': result}}
        if transport_type is MCPHTTPTransport
        else result
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        transport = transport_type('http://testserver', http=http)
        transport._description = {'operations': {'discovery.get': 'read'}}
        with pytest.raises(Failure) as caught:
            await transport.call(packet)
        assert caught.value.code == 'invalid_result_envelope'
        assert SECRET not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('errors', [{}, '', 0, False])
async def test_graphql_falsy_non_list_errors_do_not_hide_an_invalid_envelope(errors):
    packet = request_for('discovery.get', {'id': '/main'}, 'http://testserver')
    payload = {
        'errors': errors,
        'data': {
            'call': {'request_id': packet.request_id, 'operation': packet.operation, 'status': 'ok'}
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        transport = GraphQLTransport('http://testserver', http=http)
        transport._description = {'operations': {'discovery.get': 'read'}}
        with pytest.raises(Failure) as caught:
            await transport.call(packet)
        assert caught.value.code == 'invalid_graphql_result'


def test_result_decoder_preserves_immutable_historical_results():
    from msg.core.codec import canonical, freeze_json, wire
    from msg.transports.packet import decode_result

    historical = freeze_json({
        'request_id': 'original',
        'operation': 'discovery.get',
        'status': 'error',
        'error': {'code': 'permission_denied', 'retryable': False},
    })
    before = canonical(historical)
    result = decode_result(historical)
    assert result.error.code == 'permission_denied' and result.error.retryable is False
    assert canonical(historical) == before
    assert wire(result)['request_id'] == 'original'
