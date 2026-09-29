"""Tool readiness and isolated real-runner exercise; never contact an external host."""

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

from msg.core.errors import Failure, require
from msg.core.models import Scope
from msg.security.capabilities import grant_for
from msg.workers.sandbox import BubblewrapRunner


async def inspect_tool_sandbox(app):
    """Exercise the installed sandbox and its deny path without making a request."""
    tool = app.registry.tool('tool_curl')
    policy = replace(tool.network, hosts=('denied.invalid',), timeout_ms=1000)
    with tempfile.TemporaryDirectory(prefix='msg-tool-doctor-') as directory:
        try:
            await BubblewrapRunner(app)(
                tool, {'url': 'http://127.0.0.1/'}, (policy,), Path(directory)
            )
        except Failure as exc:
            require(exc.code == 'network_policy_denied', exc.code)
        else:
            raise Failure('tool_sandbox_denial_missing')
    return {'runner': 'bubblewrap', 'policy_denial': True, 'external_requests': 0}


async def check_tool_sandbox(app, call, approve, key, subject):
    """Check real tool.run denial and real bwrap output using a loopback-only fixture."""
    require(app.selftest_run_id is not None, 'selftest_namespace_required')
    payload = b'msg isolated sandbox selftest\n'
    received = []

    async def serve(reader, writer):
        try:
            request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            received.append(request.split(b'\r\n', 1)[0])
            writer.write(
                b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: '
                + str(len(payload)).encode()
                + b'\r\nConnection: close\r\n\r\n'
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, '127.0.0.1', 0, limit=8192)
    try:
        port = server.sockets[0].getsockname()[1]
        scope = Scope(resource_id='tool_curl', descendants=False)
        grant = replace(
            grant_for(app.registry.capability('tool.use'), scope=scope),
            constraints={'hosts': ['127.0.0.1'], 'ports': [80], 'methods': ['GET']},
        )
        certificate = await approve(key, subject, (grant,))
        denied = await call(
            'tool.run',
            {'id': 'tool_curl', 'arguments': {'url': 'http://127.0.0.1/'}},
            key,
            subject,
            (certificate.resource_id,),
        )
        require(
            denied.error is not None and denied.error.code == 'network_policy_denied',
            'tool_selftest_private_denial_missing',
        )
        tool = app.registry.tool('tool_curl')
        policy = replace(
            tool.network,
            hosts=('127.0.0.1',),
            ports=frozenset({port}),
            methods=frozenset({'GET'}),
            allow_private=True,
            timeout_ms=3000,
            max_response_bytes=4096,
            max_redirects=0,
        )
        # This exercises the real runner port. It deliberately does not alter the
        # registered tool's 80/443 policy to claim a normal tool.run success.
        with tempfile.TemporaryDirectory(prefix='msg-tool-selftest-') as directory:
            result = await BubblewrapRunner(app)(
                tool, {'url': f'http://127.0.0.1:{port}/'}, (policy,), Path(directory)
            )
            require(
                result.path.read_bytes() == payload and result.metadata['status'] == 200,
                'tool_selftest_output_mismatch',
            )
        require(received == [b'GET / HTTP/1.1'], 'tool_selftest_request_mismatch')
        await inspect_tool_sandbox(app)
        require(received == [b'GET / HTTP/1.1'], 'tool_selftest_unexpected_request')
        return {
            'operation': 'private_target_denied',
            'runner': 'loopback_output_and_policy_denial',
            'normal_operation_pipeline': 'not_exercised',
            'external_requests': 0,
        }
    finally:
        server.close()
        await server.wait_closed()
