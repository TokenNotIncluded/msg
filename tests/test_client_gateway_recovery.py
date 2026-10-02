"""Recover read-only gateway outages without changing write or cursor semantics."""

import asyncio
import io
import json
from types import SimpleNamespace

import httpx
import pytest

from msg.client import MsgClient
from msg.client_listener import listen
from msg.core.codec import wire
from msg.core.errors import Failure
from msg.core.requests import request_for
from msg.security.crypto import Ed25519Signer
from msg.transports.client import (
    GraphQLTransport,
    HTTPTransport,
    MCPHTTPTransport,
    PathGETTransport,
)

SERVER = 'http://testserver'
TYPES = (HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport)
CONTEXT = {'server': SERVER, 'subject': 'u_reader', 'agent': 'gateway-reader'}
HTML = b'<html>upstream unavailable; token=synthetic-secret</html>'
DESCRIPTION = {
    'version': 1,
    'target_service': SERVER,
    'operations': {
        'communication.changes': 'read',
        'discovery.get': 'read',
        'content.file_put': 'transaction',
        'tool.run': 'external',
    },
    'limits': {
        'max_request_bytes': 1048576,
        'max_response_bytes': 1048576,
        'max_path_bytes': 8192,
        'encodings': ['j', 'gz'],
    },
    'transports': {'graphql': '/-/graphql'},
}


def packet(operation='communication.changes', arguments=None):
    return request_for(
        operation,
        arguments or {'limit': 1},
        SERVER,
        subject='u_reader',
        signer=Ed25519Signer.generate(),
    )


def result(request, data):
    return {
        'request_id': request.request_id,
        'operation': request.operation,
        'status': 'ok',
        'subject': request.subject,
        'data': data,
    }


def envelope(kind, value):
    if kind is GraphQLTransport:
        return {'data': {'call': value}}
    if kind is MCPHTTPTransport:
        return {'result': {'structuredContent': value}}
    return value


def client_for(kind, http, *, retries=2, **options):
    transport = kind(SERVER, http=http, **options)
    transport._description = DESCRIPTION
    return MsgClient(SimpleNamespace(server=SERVER), transport, retries=retries)


@pytest.mark.parametrize('kind', TYPES)
@pytest.mark.parametrize('status', [502, 503, 504])
async def test_read_gateway_retry_reuses_exact_signed_packet(kind, status, monkeypatch):
    signed = packet()
    sent = []
    delays = []

    def handle(request):
        sent.append((request.method, str(request.url), request.content))
        if len(sent) == 1:
            return httpx.Response(status, content=HTML, headers={'content-type': 'text/html'})
        return httpx.Response(200, json=envelope(kind, result(signed, {'items': []})))

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = client_for(kind, http)
        assert (await client.send(signed)).status == 'ok'
    assert len(sent) == 2 and sent[0] == sent[1]
    assert delays == [0.2]


@pytest.mark.parametrize('kind', TYPES)
@pytest.mark.parametrize('operation', ['content.file_put', 'tool.run'])
@pytest.mark.parametrize('status', [502, 503, 504])
async def test_write_gateway_is_not_newly_retried(kind, operation, status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, content=HTML, headers={'content-type': 'text/html'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = client_for(kind, http)
        with pytest.raises(Failure) as caught:
            await client.send(packet(operation))
    assert caught.value.code == 'invalid_server_response' and not caught.value.retryable
    assert len(calls) == 1


@pytest.mark.parametrize(
    'status,body,headers,expected',
    [
        (200, HTML, {'content-type': 'text/html'}, 'invalid_server_response'),
        (500, HTML, {'content-type': 'text/html'}, 'invalid_server_response'),
        (403, HTML, {}, 'invalid_server_response'),
        (503, b'{broken', {'content-type': 'application/json'}, 'invalid_server_response'),
        (503, b'{broken', {'content-type': 'application/problem+json'}, 'invalid_server_response'),
        (503, b'{"a":1,"a":2}', {}, 'invalid_server_response'),
        (503, b'{"a":NaN}', {}, 'invalid_server_response'),
        (503, b'[]', {}, 'invalid_server_response'),
        (503, b'{"status":"unknown"}', {}, 'missing_field'),
        (403, b'{"error":"permission_denied"}', {}, 'permission_denied'),
        (503, HTML, {'location': '/elsewhere'}, 'response_too_large'),
        (307, HTML, {'location': 'https://other.invalid'}, 'redirect_not_allowed'),
    ],
)
async def test_read_permanent_responses_and_guards_stop(status, body, headers, expected, capsys):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, content=body, headers=headers)

    limit = 8 if expected == 'response_too_large' else 1048576
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = client_for(HTTPTransport, http, max_response_bytes=limit)
        with pytest.raises(Failure) as caught:
            await client.send(packet())
    assert caught.value.code == expected and not caught.value.retryable
    assert len(calls) == 1
    assert 'synthetic-secret' not in str(caught.value) + capsys.readouterr().err


@pytest.mark.parametrize(
    'kind,status',
    [
        (HTTPTransport, 503),
        (PathGETTransport, 503),
        (GraphQLTransport, 200),
        (MCPHTTPTransport, 200),
    ],
)
async def test_json_result_permission_failure_keeps_terminal_flag(kind, status):
    signed = packet()
    response = result(signed, {})
    response.pop('data')
    response.update(status='error', error={'code': 'permission_denied', 'retryable': False})
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json=envelope(kind, response))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = client_for(kind, http)
        with pytest.raises(Failure) as caught:
            client.checked(await client.send(signed))
    assert caught.value.code == 'permission_denied' and not caught.value.retryable
    assert len(calls) == 1


@pytest.mark.parametrize('kind', TYPES)
@pytest.mark.parametrize('operation', ['content.file_put', 'tool.run'])
async def test_existing_json_write_retry_reuses_exact_signed_packet(kind, operation, monkeypatch):
    signed, sent, delays = packet(operation), [], []

    def handle(request):
        sent.append((request.method, str(request.url), request.content))
        return httpx.Response(503, json={'error': 'upstream_busy'})

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = client_for(kind, http)
        with pytest.raises(Failure) as caught:
            await client.send(signed)
    assert caught.value.code == 'upstream_busy' and caught.value.retryable
    assert caught.value.details is None
    assert len(sent) == 3 and sent[0] == sent[1] == sent[2]
    assert delays == [0.2, 0.4]


@pytest.mark.parametrize('operation', ['content.file_put', 'tool.run'])
async def test_existing_write_timeout_keeps_uncertain_request_id(operation, monkeypatch):
    signed, sent = packet(operation), []

    def handle(request):
        sent.append((request.method, str(request.url), request.content))
        raise httpx.ReadTimeout('synthetic timeout', request=request)

    async def sleep(delay):
        assert delay in {0.2, 0.4}

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = client_for(HTTPTransport, http)
        with pytest.raises(Failure) as caught:
            await client.send(signed)
    assert caught.value.code == 'transport_uncertain' and caught.value.retryable
    assert caught.value.details == {'request_id': signed.request_id}
    assert len(sent) == 3 and sent[0] == sent[1] == sent[2]


async def test_description_recovery_does_not_retry_dispatched_write(monkeypatch):
    methods = []

    def handle(request):
        methods.append(request.method)
        if methods == ['GET']:
            return httpx.Response(502, content=HTML)
        if request.method == 'GET':
            return httpx.Response(200, json=DESCRIPTION)
        return httpx.Response(503, content=HTML)

    async def sleep(delay):
        assert delay == 0.2

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        transport = HTTPTransport(SERVER, http=http)
        client = MsgClient(SimpleNamespace(server=SERVER), transport)
        with pytest.raises(Failure) as caught:
            await client.send(packet('content.file_put'))
    assert caught.value.code == 'invalid_server_response' and not caught.value.retryable
    assert methods == ['GET', 'GET', 'POST']


async def test_read_batch_gateway_recovery_preserves_alias_packets():
    packets = [packet('discovery.get', {'id': '/main'}), packet()]
    sent = []

    def handle(request):
        sent.append(request.content)
        if len(sent) == 1:
            return httpx.Response(504, content=b'')
        return httpx.Response(
            200, json={'data': {f'r{i}': result(p, {}) for i, p in enumerate(packets)}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        transport = client_for(HTTPTransport, http).transport
        with pytest.raises(Failure) as caught:
            await transport.call_reads(packets)
        assert caught.value.retryable
        assert [r.request_id for r in await transport.call_reads(packets)] == [
            p.request_id for p in packets
        ]
    assert sent[0] == sent[1]
    assert json.loads(sent[0])['variables'] == {f'p{i}': wire(p) for i, p in enumerate(packets)}


async def test_listener_gateway_recovery_preserves_pending_and_durable_cursor(
    tmp_path, monkeypatch, capsys
):
    checkpoint = tmp_path / 'cursor.json'
    output, delays, cursors = io.StringIO(), [], []
    attempts = 0

    def handle(request):
        nonlocal attempts
        incoming = json.loads(request.content)
        cursor = incoming['arguments'].get('cursor')
        cursors.append(cursor)
        if cursor is None:
            data = {'items': [{'id': 'a'}, {'id': 'b'}], 'cursor': '2', 'has_more': False}
        else:
            assert cursor == '2'
            assert json.loads(checkpoint.read_text())['cursor'] == '2'
            attempts += 1
            if attempts <= 2:
                return httpx.Response(503, content=HTML)
            data = {'items': [{'id': 'c'}], 'cursor': '3', 'has_more': False}
        return httpx.Response(
            200,
            json={
                'request_id': incoming['request_id'],
                'operation': incoming['operation'],
                'status': 'ok',
                'data': data,
            },
        )

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = client_for(HTTPTransport, http, retries=0)

        async def fetch(cursor, tail=False):
            arguments = {'limit': 1}
            if cursor is not None:
                arguments['cursor'] = cursor
            return wire(client.checked(await client.send(packet(arguments=arguments))).data)

        assert (
            await listen(
                fetch, context=CONTEXT, cursor_file=checkpoint, max_events=1, output=output
            )
            == 1
        )
        assert json.loads(checkpoint.read_text())['pending'] == [{'id': 'b'}]
        assert (
            await listen(
                fetch, context=CONTEXT, cursor_file=checkpoint, max_events=2, output=output
            )
            == 2
        )
    assert [json.loads(line)['id'] for line in output.getvalue().splitlines()] == ['a', 'b', 'c']
    assert cursors == [None, '2', '2', '2']
    # Emitting the saved final item first incurs the normal idle wait, then backoff.
    assert delays == [1, 1, 2]
    saved = json.loads(checkpoint.read_text())
    assert saved['cursor'] == '3' and saved['pending'] == []
    assert 'synthetic-secret' not in capsys.readouterr().err


async def test_gateway_backoff_caps_and_cancel_releases_cursor_lock(tmp_path, monkeypatch):
    checkpoint = tmp_path / 'cursor.json'
    output, delays = io.StringIO(), []
    waiting = asyncio.Event()

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 8:
            waiting.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(502, content=HTML))
    ) as http:
        client = client_for(HTTPTransport, http, retries=0)

        async def fetch(cursor, tail=False):
            return wire(client.checked(await client.send(packet())).data)

        task = asyncio.create_task(
            listen(fetch, context=CONTEXT, cursor_file=checkpoint, output=output)
        )
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30]
    assert output.getvalue() == '' and not checkpoint.exists()

    async def recovered(cursor, tail=False):
        return {'items': [], 'cursor': 'ready', 'has_more': False}

    assert (
        await listen(recovered, context=CONTEXT, cursor_file=checkpoint, once=True, output=output)
        == 0
    )
