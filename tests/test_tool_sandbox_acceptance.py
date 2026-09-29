"""Real bubblewrap child enforces network policy without contacting a peer."""
from types import SimpleNamespace

import pytest

from msg.core.errors import Failure
from msg.core.models import NetworkPolicy
from msg.workers.sandbox import BubblewrapRunner


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['http://127.0.0.1:9/', 'http://169.254.169.254/',
                                 'file:///etc/passwd', 'http://[::1]:9/'])
async def test_real_sandbox_denies_private_or_non_http_target(tmp_path, url):
    app = SimpleNamespace(settings=SimpleNamespace(server=SimpleNamespace(
        limits=SimpleNamespace(max_request_bytes=1024))))
    tool = SimpleNamespace(executor_key='curl')
    with pytest.raises(Failure) as caught:
        await BubblewrapRunner(app)(tool, {'url': url}, (NetworkPolicy(schemes=frozenset({'http', 'https'}), hosts=(),
            ports=frozenset({9, 80, 443}), methods=frozenset({'GET'}), allow_private=False,
            timeout_ms=1000, max_response_bytes=1024, max_redirects=0),), tmp_path)
    assert caught.value.code == 'network_policy_denied'
    assert not (tmp_path / 'output.bin').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [b'test', b'oversized'])
async def test_real_sandbox_explicit_local_grant_returns_bounded_bytes(tmp_path, body):
    import asyncio

    requests = []

    async def receive(reader, writer):
        requests.append(await reader.readuntil(b'\r\n\r\n'))
        writer.write(f'HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n'.encode() +
                     b'Content-Type: text/plain\r\nConnection: close\r\n\r\n' + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(receive, '127.0.0.1', 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        app = SimpleNamespace(settings=SimpleNamespace(server=SimpleNamespace(
            limits=SimpleNamespace(max_request_bytes=1024))))
        policy = NetworkPolicy(schemes=frozenset({'http'}), hosts=('127.0.0.1',),
            ports=frozenset({port}), methods=frozenset({'GET'}), allow_private=True,
            timeout_ms=3000, max_response_bytes=4, max_redirects=0)
        invocation = BubblewrapRunner(app)(SimpleNamespace(executor_key='curl'),
            {'url': f'http://127.0.0.1:{port}/fixture'}, (policy,), tmp_path)
        if len(body) > policy.max_response_bytes:
            with pytest.raises(Failure, match='tool_response_too_large'):
                await invocation
            assert not (tmp_path / 'output.bin').exists()
            assert len(requests) == 1
            return
        result = await invocation
    assert result.path == tmp_path / 'output.bin'
    assert result.path.read_bytes() == b'test'
    assert result.metadata['status'] == 200
    assert len(requests) == 1
    assert requests[0].startswith(b'GET /fixture HTTP/1.1\r\n')
