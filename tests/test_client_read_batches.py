"""A shared HTTP query never changes the meaning or authority of its reads."""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from msg.client import MsgClient
from msg.client_subagents_remote import RemoteAgents
from msg.core.codec import result_wire, wire
from msg.core.errors import Failure
from msg.core.models import OperationError, OperationResult
from msg.core.requests import request_for
from msg.security.crypto import Ed25519Signer
from msg.transports.client import HTTPTransport, MCPHTTPTransport, PathGETTransport

SERVER = 'http://testserver'
DESCRIPTION = {
    'operations': {'discovery.get': 'read', 'file.read': 'read', 'file.create': 'transaction'},
    'transports': {'graphql': '/-/graphql'},
}


def packets():
    signer = Ed25519Signer.generate()
    return [
        request_for(
            operation,
            {'id': '/private/' + str(index)},
            SERVER,
            signer=signer,
            subject=signer.key_id,
            request_id='read-' + str(index),
        )
        for index, operation in enumerate(('discovery.get', 'file.read'))
    ]


def results(requests):
    return {
        'data': {
            f'r{index}': result_wire(
                OperationResult(
                    request_id=request.request_id,
                    operation=request.operation,
                    status='ok',
                    actor=request.subject,
                    subject=request.subject,
                    data={'id': request.arguments['id']},
                )
            )
            for index, request in enumerate(requests)
        }
    }


@pytest.mark.asyncio
async def test_reads_share_http_without_replacing_or_combining_signed_envelopes():
    requests = packets()
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=results(requests))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        transport = HTTPTransport(SERVER, http=http)
        transport._description = DESCRIPTION
        values = await transport.call_reads(requests)
        assert len(seen) == transport.calls == 1
        assert seen[0].url.path == '/_read/graphql' and seen[0].method == 'POST'
        payload = json.loads(seen[0].content)
        assert payload['query'].startswith('query MsgReads(')
        assert payload['variables'] == {'p0': wire(requests[0]), 'p1': wire(requests[1])}
        assert [value.data['id'] for value in values] == ['/private/0', '/private/1']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'problem', ('missing', 'extra', 'swapped', 'operation', 'subject', 'replayed')
)
async def test_read_results_fail_closed_if_alias_or_envelope_binding_is_broken(problem):
    requests = packets()
    response = results(requests)
    data = response['data']
    if problem == 'missing':
        del data['r1']
    elif problem == 'extra':
        data['unrequested'] = data['r1']
    elif problem == 'swapped':
        data['r0'], data['r1'] = data['r1'], data['r0']
    elif problem == 'operation':
        data['r0']['operation'] = 'file.read'
    elif problem == 'subject':
        data['r0']['subject'] = 'u_other'
    else:
        data['r0']['replayed'] = True
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as http:
        transport = HTTPTransport(SERVER, http=http)
        transport._description = DESCRIPTION
        with pytest.raises(Failure, match='invalid_graphql_result'):
            await transport.call_reads(requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ('write', 'foreign', 'empty', 'too_many'))
async def test_write_and_unbounded_or_foreign_read_sets_are_rejected_before_http(problem):
    requests = packets()
    expected = 'read_batch_required'
    if problem == 'write':
        requests[0] = replace(requests[0], operation='file.create')
    elif problem == 'foreign':
        requests[0] = replace(requests[0], target_service='https://other.example')
        expected = 'service_mismatch'
    else:
        requests = [] if problem == 'empty' else requests * 5
        expected = 'read_batch_limit'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) as http:
        transport = HTTPTransport(SERVER, http=http)
        transport._description = DESCRIPTION
        with pytest.raises(Failure, match=expected):
            await transport.call_reads(requests)
        assert transport.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('transport_class', (HTTPTransport, PathGETTransport, MCPHTTPTransport))
async def test_legacy_description_and_selected_non_http_transport_keep_normal_read_path(
    transport_class,
):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) as http:
        transport = transport_class(SERVER, http=http)
        transport._description = (
            {'operations': DESCRIPTION['operations']}
            if transport_class is HTTPTransport
            else DESCRIPTION
        )
        assert await transport.call_reads(packets()) is None
        assert transport.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ('permission_denied', 'credential_ceiling', 'token=private'))
async def test_graphql_errors_preserve_stable_authority_codes_without_echoing_untrusted_text(error):
    response = {'errors': [{'extensions': {'code': error}}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as http:
        transport = HTTPTransport(SERVER, http=http)
        transport._description = DESCRIPTION
        with pytest.raises(Failure) as caught:
            await transport.call_reads(packets())
        assert caught.value.code == ('graphql_error' if error == 'token=private' else error)
        assert 'private' not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('code', 'retryable', 'expected'),
    (
        ('not_found', False, 'not_found'),
        ('server_busy', True, 'server_busy'),
        ('credential_ceiling', False, 'credential_ceiling'),
        ('token=private', False, 'graphql_error'),
    ),
)
async def test_old_http_route_failure_envelopes_preserve_safe_code_and_retryability(
    code, retryable, expected
):
    response = {
        'status': 'error',
        'error': {'code': code, 'retryable': retryable, 'message': 'private'},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404, json=response))
    ) as http:
        transport = HTTPTransport(SERVER, http=http)
        transport._description = DESCRIPTION
        with pytest.raises(Failure) as caught:
            await transport.call_reads(packets())
        assert caught.value.code == expected and caught.value.retryable is retryable
        assert 'private' not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('retryable_index', (0, 4))
async def test_an_authority_error_prevents_fallback_even_when_another_read_is_retryable(
    retryable_index,
):
    owner = 'u_owner'
    values = [
        OperationResult(
            request_id=str(index),
            operation='discovery.get',
            status='ok',
            actor=owner,
            subject=owner,
            data={'owner': owner, 'mode': '0700', 'state': 'active', 'type': 'topic'},
        )
        for index in range(6)
    ]
    values[3] = replace(
        values[3], status='error', error=OperationError(code='credential_ceiling', retryable=False)
    )
    values[retryable_index] = replace(
        values[retryable_index],
        status='error',
        error=OperationError(code='server_busy', retryable=True),
    )
    calls = []

    async def read_many(requests):
        return values

    async def call(operation, args):
        calls.append(operation)
        raise AssertionError('An authority failure cannot enter the serial fallback')

    client = SimpleNamespace(
        state=SimpleNamespace(subject=owner),
        transport=SimpleNamespace(call_reads=read_many),
        prepare=lambda op, args: (op, args),
        checked=MsgClient.checked,
        call=call,
    )
    with pytest.raises(Failure, match='credential_ceiling'):
        await RemoteAgents(client)._send_preflight('/@owner/files/agents', 'sender', 'recipient')
    assert calls == []
