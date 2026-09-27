"""Live share revocation crosses real adapters, historical reads, search and Sync."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import os
from pathlib import Path
import socket
import sys
from uuid import uuid4

import pytest
import uvicorn

from msg.admin.root import _approve_csr, _provision
from msg.application import Application
from msg.client import ClientState, MsgClient
from msg.config import write_example
from msg.core.codec import canonical, digest, loads, wire
from msg.core.executor import result_wire
from msg.transports.client import HTTPTransport, TRANSPORTS
from msg.transports.http import create_app


def ok(result):
    value = result_wire(result) if not isinstance(result, dict) else result
    assert value['status'] == 'ok', value
    return value


@asynccontextmanager
async def live_adapters(tmp_path, pg_dsn, mode):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.setblocking(False)
    url = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    app = Application(write_example(tmp_path / 'etc', tmp_path / 'data', url,
                                    postgres_dsn=pg_dsn))
    server = None
    serving = process = None
    transports = []
    try:
        csr, root = await _provision(app, 'share-matrix-test-passphrase')
        await _approve_csr(app, csr, root, expected_digest=None, operator='matrix-fixture')
        server = uvicorn.Server(uvicorn.Config(create_app(app), log_level='critical',
                                               access_log=False, ws='none'))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.02)
        assert server.started
        peers = []
        for name in ('owner', 'middle', 'reader'):
            transport = HTTPTransport(url)
            transports.append(transport)
            peer = MsgClient(ClientState(tmp_path / name, server=url), transport)
            ok(await peer.register('matrix-' + name))
            peers.append(peer)
        reader = peers[-1]
        transport = TRANSPORTS.get(mode, HTTPTransport)(url)
        transports.append(transport)
        client = MsgClient(reader.state, transport)
        environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')}
        command = [sys.executable, '-m', 'msg.cli', '--config-dir',
                   str(reader.state.directory), '--server', url]
        if mode == 'mcp_stdio':
            process = await asyncio.create_subprocess_exec(*command, 'mcp', env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            process.stdin.write(canonical({'jsonrpc': '2.0', 'id': 'init',
                'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}}) + b'\n')
            await process.stdin.drain()
            assert 'result' in loads(await asyncio.wait_for(process.stdout.readline(), 20))

        async def invoke(operation, arguments):
            request_id = uuid4().hex
            if mode == 'cli':
                child = await asyncio.create_subprocess_exec(*command, 'call', operation,
                    canonical(arguments).decode(), '--request-id', request_id, env=environment,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(child.communicate(), 30)
                except BaseException:
                    if child.returncode is None:
                        child.kill()
                    await child.wait()
                    raise
                assert child.returncode in {0, 1}, stderr.decode()
                return loads(stdout)
            if mode == 'mcp_stdio':
                process.stdin.write(canonical({'jsonrpc': '2.0', 'id': request_id,
                    'method': 'tools/call', 'params': {'name': operation, 'arguments': arguments,
                    '_meta': {'msg/request_id': request_id}}}) + b'\n')
                await process.stdin.drain()
                response = loads(await asyncio.wait_for(process.stdout.readline(), 20))
                assert 'result' in response, response
                return response['result']['structuredContent']
            return result_wire(await client.call(operation, arguments, request_id=request_id))

        yield app, peers, invoke
    finally:
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                process.kill()
                await process.wait()
        for transport in transports:
            await transport.close()
        if serving is not None:
            server.should_exit = True
            await asyncio.wait_for(serving, 15)
        sock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['http', 'path_get', 'graphql', 'mcp_http', 'cli', 'mcp_stdio'])
async def test_live_group_reshare_revocation_across_read_surfaces(tmp_path, pg_dsn, mode):
    async with live_adapters(tmp_path, pg_dsn, mode) as (app, peers, invoke):
        owner, middle, reader = peers
        marker = 'shareboundary' + uuid4().hex
        group = ok(await owner.call('group.create', {'name': 'matrix-group'}))['resources'][0]['id']
        ok(await owner.call('group.invite', {'group': group, 'subject': middle.state.subject}))
        ok(await middle.call('group.join', {'group': group}))
        post = ok(await owner.call('content.post_create', {'parent': '/main', 'body': marker}))
        rid, original = post['resources'][0]['id'], post['resources'][0]['revision']
        private = ok(await owner.call('content.chmod', {'id': rid, 'mode': '0600'},
                                     expected=((rid, post['data']['generation']),)))
        ok(await owner.call('content.post_edit', {'id': rid, 'expected_revision': original,
            'body': marker + '\nnew revision'}, expected=((rid, private['data']['generation']),)))
        expiry = wire(app.clock() + timedelta(minutes=30))
        terms = {'resource': rid, 'operations': ['read'], 'expires_at': expiry}
        parent = ok(await owner.call('sharing.grant', {**terms, 'grantee': group,
            'grantee_kind': 'group', 'allow_reshare': True}, contract_version=2))['data']['grant']['id']
        ok(await middle.call('sharing.grant', {**terms, 'grantee': reader.state.subject,
            'grantee_kind': 'user', 'source_grant_id': parent}, contract_version=2))
        current = ok(await invoke('discovery.get', {'id': rid}))
        assert marker in canonical(current).decode()
        historical = ok(await invoke('discovery.get', {'id': rid, 'revision': original}))
        assert marker in canonical(historical).decode()
        history = ok(await invoke('discovery.get', {'id': rid, 'view': 'history'}))
        assert original in canonical(history).decode()
        cached = {'id': rid, 'known_digest': digest(current['data'])}
        assert ok(await invoke('discovery.get', cached))['data']['not_modified']
        search = {'scope': '/main', 'terms': marker, 'field': 'body', 'snippet': True}
        assert rid in canonical(ok(await invoke('discovery.lexical_search', search))).decode()
        ok(await reader.call('communication.watch', {'id': rid}))
        synced = ok(await invoke('communication.sync', {'limit': 100}))['data']
        assert any(item.get('ref', {}).get('id') == rid for item in synced['items'])
        cursor = synced['sync_cursor']

        # Reuse the same clients, credential, cursor and cache digest after the
        # source member leaves. Child grants are not independent authority.
        ok(await middle.call('group.leave', {'group': group}))
        for query in ({'id': rid}, {'id': rid, 'revision': original},
                      {'id': rid, 'view': 'history'}, cached):
            denied = await invoke('discovery.get', query)
            assert denied['status'] == 'error' and denied['error']['code'] == 'permission_denied', denied
            assert marker not in canonical(denied).decode()
            assert not denied.get('data', {}).get('not_modified', False)
        hidden = ok(await invoke('discovery.lexical_search', search))
        assert rid not in canonical(hidden).decode() and marker not in canonical(hidden['data']).decode()
        delta = ok(await invoke('communication.sync', {'cursor': cursor, 'limit': 100}))['data']
        assert delta['resync_required'] is True
        assert any(item['kind'] == 'revoked' and item['ref']['id'] == rid for item in delta['items'])
        assert marker not in canonical(delta).decode()

        # An invalid old source must not veto an independent live owner grant.
        direct = ok(await owner.call('sharing.grant', {**terms, 'grantee': reader.state.subject,
            'grantee_kind': 'user'}, contract_version=2))['data']['grant']['id']
        ok(await invoke('discovery.get', {'id': rid, 'revision': original}))
        ok(await owner.call('sharing.revoke', {'grant_id': parent}, contract_version=2))
        ok(await invoke('discovery.get', {'id': rid}))
        ok(await owner.call('sharing.revoke', {'grant_id': direct}, contract_version=2))
        final = await invoke('discovery.get', {'id': rid})
        assert final['status'] == 'error' and final['error']['code'] == 'permission_denied', final
