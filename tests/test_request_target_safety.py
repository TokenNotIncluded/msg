"""Raw URL boundaries without a database; no business execution is simulated."""
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from msg.core.errors import Failure
from msg.core.executor import OperationExecutor
from msg.core.requests import request_for
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from msg.transports.packet import require_url_safe_packet


def boundary_service():
    """Only health/root and rejection paths are usable on this service."""
    return SimpleNamespace(_loaded=True, settings=SimpleNamespace(
        service_url='http://testserver', server=SimpleNamespace(limits=SimpleNamespace(
            max_path_bytes=8192, max_request_bytes=1048576, max_response_bytes=1048576))))


SECRET_TARGETS = (
    '/?token={secret}', '/?ToKeN={secret}', '/?%74oken={secret}',
    '/?%2572ecovery_secret={secret}', '/?%252574oken={secret}',
    '/?ordinary=x%26token={secret}', '/?token%3D{secret}',
    '/?ordinary=x;password={secret}', '/?credentials[token]={secret}',
    '/?credentials.token={secret}', '/?api-key={secret}',
    '/?recoverySecret={secret}', '/?bootstrap_claim={secret}',
    '/healthz?%2574oken={secret}',
    '/-/g/content.post_create/token/{secret}',
    '/-/g/content.post_create/ToKeN/{secret}',
    '/-/g/content.post_create/%2574oken/{secret}',
    '/-/g/content.post_create/bootstrap/{secret}',
    '/-/g/example/%2562ootstrap/{secret}',
)


@pytest.mark.parametrize('method', ['GET', 'HEAD', 'POST'])
@pytest.mark.parametrize('target', SECRET_TARGETS)
async def test_raw_request_target_rejects_secret_labels_before_route_dispatch(method, target):
    secret = 'nonlive_' + uuid4().hex
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(boundary_service())),
                                 base_url='http://testserver') as http:
        response = await http.request(method, target.format(secret=secret))
    assert response.status_code == 400
    if method != 'HEAD':
        assert response.json()['error']['code'] == 'secure_channel_required'
    assert secret not in response.text + str(response.headers)
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('target', [
    '/', '/healthz', '/?word=token', '/?q=100%25', '/?q=%E6%B5%8B%E8%AF%95',
    '/?scope=%2Fmain', '/?q=%252Fmain', '/?q=tokenization',
])
async def test_ordinary_no_secret_get_is_unchanged(target):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(boundary_service())),
                                 base_url='http://testserver') as http:
        response = await http.get(target)
    assert response.status_code == 200


async def test_query_is_included_in_bounded_request_target():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(boundary_service())),
                                 base_url='http://testserver') as http:
        response = await http.get('/healthz?q=' + 'x' * 8192)
    assert response.status_code == 413
    assert response.json()['error']['code'] == 'path_too_large'


@pytest.mark.parametrize('target', [*SECRET_TARGETS,
    '/healthz#token={secret}', '/healthz#section', '/healthz?x=1#token={secret}'])
async def test_client_rejects_secrets_and_fragments_before_network(target):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json={'status': 'ok'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        client = HTTPTransport('http://testserver', http=http)
        with pytest.raises(Failure):
            await client._json('GET', target.format(secret='nonlive_' + uuid4().hex))
    assert not calls
    assert client.calls == 0


@pytest.mark.parametrize('field', ['token', 'ToKeN', '%2574oken', 'privateKey',
                                   'nested[api_key]', 'auth.recovery_secret'])
def test_url_packet_uses_the_same_secret_field_policy(field):
    packet = request_for('content.post_create', {'values': [{field: 'nonlive'}]},
                         'http://testserver')
    with pytest.raises(Failure, match='secure_channel_required'):
        require_url_safe_packet(packet)


async def test_post_body_can_still_carry_secrets_over_its_configured_channel():
    received = []

    def send(request):
        received.append(request.content)
        return httpx.Response(200, json={'status': 'ok'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        client = HTTPTransport('https://testserver', http=http)
        await client._json('POST', '/-/p/identity.token_recover',
                           body={'recovery_secret': 'nonlive'})
    assert received == [b'{"recovery_secret":"nonlive"}']


async def test_http_unexpected_exception_never_logs_its_message(caplog):
    secret = 'nonlive_' + uuid4().hex

    class BrokenRegistry:
        def operation(self, *_args):
            raise RuntimeError(secret)

    service = boundary_service()
    service.registry = BrokenRegistry()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)),
                                 base_url='http://testserver') as http:
        response = await http.get('/latest/post')
    assert response.status_code == 500
    assert secret not in caplog.text + response.text + str(response.headers)
    assert 'http_dispatch_failed' in caplog.text


async def test_executor_unexpected_exception_never_logs_input_or_traceback(caplog):
    secret = 'nonlive_' + uuid4().hex

    class BrokenRegistry:
        def operation(self, *_args):
            raise RuntimeError(secret)

    executor = OperationExecutor(BrokenRegistry(), None, None, None, None, None, None)
    packet = request_for('content.post_create', {'body': secret}, 'http://testserver')
    result = await executor.execute(packet)
    assert result.error.code == 'internal_error'
    assert secret not in caplog.text
    assert 'operation_failed' in caplog.text


@pytest.mark.parametrize('target,code', [
    (b'/%2D/g/content.post_create/j/encoded', 'not_found'),
    (b'/-/%67/content.post_create/j/encoded', 'not_found'),
    (b'/-/g%2Fcontent.post_create/j/encoded', 'not_found'),
    (b'/-/g/content%2Epost_create/j/encoded', 'not_found'),
    (b'/-/g/content.post_create/%6A/encoded', 'not_found'),
    (b'/-/%2E%2E/-/g/content.post_create/j/encoded', 'not_found'),
    (b'/-/%252E%252E/-/g/content.post_create/j/encoded', 'not_found'),
    (b'/healthz/%GG', 'invalid_path'),
    (b'/healthz/\x00', 'invalid_path'),
    (b'/healthz/%00', 'invalid_path'),
    (b'/healthz/\\anything', 'invalid_path'),
    (b'/healthz/%255Canything', 'invalid_path'),
    (b'/healthz#token=nonlive', 'secure_channel_required'),
])
def test_raw_control_paths_reject_without_normalizing_into_an_execution(target, code):
    from msg.transports.url_safety import require_safe_request_target
    with pytest.raises(Failure) as caught:
        require_safe_request_target(target, maximum=8192)
    assert caught.value.code == code


@pytest.mark.parametrize('target', [
    b'/_r/q/1/r/%2Fmain', b'/_r/q/1/r/%2Fmain/n/5',
    b'/-/g/example/%2Fmain/100%25', b'/token/ordinary-post.md',
    b'/@example/profile', b'/%E6%B5%8B%E8%AF%95',
])
def test_quoted_data_and_topics_named_token_remain_usable(target):
    from msg.transports.url_safety import require_safe_request_target
    require_safe_request_target(target, maximum=8192)


def test_excessive_decoding_is_bounded_and_rejected():
    from msg.transports.url_safety import require_safe_request_target
    nested = b'%2F'
    for _ in range(10):
        nested = nested.replace(b'%', b'%25')
    with pytest.raises(Failure, match='invalid_path'):
        require_safe_request_target(b'/healthz', b'q=' + nested, maximum=8192)


@pytest.mark.parametrize('host', ['testserver:invalid', 'other@testserver',
                                  'testserver:65536', 'testserver/suffix', 'other.example'])
async def test_invalid_host_fails_closed_without_echo(host, caplog):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(boundary_service())),
                                 base_url='http://testserver') as http:
        response = await http.get('/healthz', headers={'Host': host})
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'forbidden_host'
    assert host not in response.text
    assert 'Traceback' not in caplog.text


async def test_client_does_not_follow_a_response_redirect():
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(308, headers={'Location': 'https://other.example/receive'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        client = HTTPTransport('https://testserver', http=http)
        with pytest.raises(Failure, match='redirect_not_allowed'):
            await client._json('POST', '/-/p/identity.token_recover',
                               body={'recovery_secret': 'nonlive'})
    assert len(calls) == 1
    assert calls[0].url.host == 'testserver'
