"""Release gate: run explicitly with Python 3.15 and ALL project dependencies.

No importorskip, fake GraphQL adapter, or expected-failure marker is used here.
The local delivery environment cannot execute the GraphQL case; CI must do so.
"""

import asyncio
import os
import socket
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn

from msg.admin.root import _approve_csr, _provision
from msg.application import Application
from msg.client import ClientState, MsgClient
from msg.config import write_example
from msg.core.codec import b64, canonical, digest, loads, unb64
from msg.transports.client import TRANSPORTS, HTTPTransport
from msg.transports.http import create_app


def test_release_dependencies_are_available():
    assert sys.version_info[:2] >= (3, 15), 'Release verification requires Python 3.15'
    import graphql
    import tiktoken

    assert graphql.__version__ and tiktoken.__version__


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['http', 'path_get', 'graphql', 'mcp_http', 'cli', 'mcp_stdio'])
async def test_transfer_conformance_for_every_transport(mode, tmp_path, pg_dsn):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.setblocking(False)
    url = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    app = Application(write_example(tmp_path / 'etc', tmp_path / 'data', url, postgres_dsn=pg_dsn))
    csr, root = await _provision(app, 'conformance-test-passphrase')
    await _approve_csr(app, csr, root, expected_digest=None, operator='conformance-fixture')
    server = uvicorn.Server(
        uvicorn.Config(create_app(app), log_level='critical', access_log=False, ws='none')
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started
    state = ClientState(tmp_path / 'client', server=url)
    transport = TRANSPORTS.get(mode, HTTPTransport)(url)
    client = MsgClient(state, transport)
    peer_transport = HTTPTransport(url)
    peer = MsgClient(state, peer_transport)
    await peer.register('conformance-agent')
    process = None
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')}
    command = [
        sys.executable,
        '-m',
        'msg.cli',
        '--config-dir',
        str(state.directory),
        '--server',
        url,
    ]
    if mode == 'mcp_stdio':
        process = await asyncio.create_subprocess_exec(
            *command,
            'mcp',
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        process.stdin.write(
            canonical({
                'jsonrpc': '2.0',
                'id': 'init',
                'method': 'initialize',
                'params': {'protocolVersion': '2025-11-25'},
            })
            + b'\n'
        )
        await process.stdin.drain()
        assert 'result' in loads(await asyncio.wait_for(process.stdout.readline(), 15))

    async def invoke(operation, args, rid=None):
        rid = rid or uuid4().hex
        if mode == 'cli':
            child = await asyncio.create_subprocess_exec(
                *command,
                'call',
                operation,
                canonical(args).decode(),
                '--request-id',
                rid,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
            assert child.returncode in {0, 1}, stderr.decode()
            return loads(stdout)
        if mode == 'mcp_stdio':
            process.stdin.write(
                canonical({
                    'jsonrpc': '2.0',
                    'id': rid,
                    'method': 'tools/call',
                    'params': {
                        'name': operation,
                        'arguments': args,
                        '_meta': {'msg/request_id': rid},
                    },
                })
                + b'\n'
            )
            await process.stdin.drain()
            result = loads(await asyncio.wait_for(process.stdout.readline(), 15))
            assert 'result' in result, result
            return result['result']['structuredContent']
        result = await client.call(operation, args, request_id=rid)
        from msg.core.executor import result_wire

        return result_wire(result)

    try:
        data = b'UTF8-exact\r\n' + bytes(range(128))
        opened = await invoke(
            'transfer.open',
            {
                'direction': 'upload',
                'size': len(data),
                'digest': digest(data),
                'requested_part_bytes': 97,
            },
        )
        assert opened['status'] == 'ok', opened
        tid = opened['data']['transfer_id']
        chunks = [(offset, data[offset : offset + 97]) for offset in range(0, len(data), 97)]
        offset, part = chunks[-1]
        args = {'transfer_id': tid, 'offset': offset, 'data': b64(part), 'digest': digest(part)}
        first = await invoke('transfer.part_put', args, 'same-chunk')
        again = await invoke('transfer.part_put', args, 'same-chunk')
        assert first['status'] == 'ok' and again['replayed']
        bad = await invoke('transfer.part_put', {**args, 'digest': digest(b'wrong')})
        assert bad['status'] == 'error'
        incomplete = await invoke(
            'transfer.seal',
            {'transfer_id': tid, 'final_size': len(data), 'final_digest': digest(data)},
        )
        assert incomplete['status'] == 'error'
        for offset, part in reversed(chunks[:-1]):
            piece = {
                'transfer_id': tid,
                'offset': offset,
                'data': b64(part),
                'digest': digest(part),
            }
            # Recover the same subject-bound session using another real adapter.
            if offset == 0:
                assert (await peer.call('transfer.part_put', piece)).status == 'ok'
            else:
                assert (await invoke('transfer.part_put', piece))['status'] == 'ok'
        status = await invoke('transfer.status', {'transfer_id': tid})
        assert status['status'] == 'ok'
        sealed = await invoke(
            'transfer.seal',
            {'transfer_id': tid, 'final_size': len(data), 'final_digest': digest(data)},
        )
        assert sealed['status'] == 'ok', sealed
        repeated = await invoke(
            'transfer.seal',
            {'transfer_id': tid, 'final_size': len(data), 'final_digest': digest(data)},
        )
        assert repeated['output'] == sealed['output']
        download = await invoke(
            'transfer.open',
            {'direction': 'download', 'target': sealed['output'], 'requested_part_bytes': 83},
        )
        assert download['status'] == 'ok'
        restored = bytearray()
        for offset in range(0, len(data), 83):
            result = await invoke(
                'transfer.part_get',
                {
                    'transfer_id': download['data']['transfer_id'],
                    'offset': offset,
                    'length': min(83, len(data) - offset),
                },
            )
            assert result['status'] == 'ok', result
            chunk = unb64(result['data']['data'])
            assert digest(chunk) == result['data']['chunk']['content']['digest']
            restored.extend(chunk)
        assert bytes(restored) == data and digest(bytes(restored)) == digest(data)
        cancelled = await invoke(
            'transfer.cancel', {'transfer_id': download['data']['transfer_id']}
        )
        assert cancelled['status'] == 'ok'
    finally:
        if process:
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 15)
        await transport.close()
        await peer_transport.close()
        server.should_exit = True
        await serving
        sock.close()
