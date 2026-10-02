"""Exercise real client imports and signed envelopes without a server runtime.

Run with --minimal-install in a fresh wheel-only virtualenv, outside the source
checkout. Network traffic uses httpx.MockTransport, not a remote service.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.abc
import importlib.util
import json
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_DEPENDENCIES = ('starlette', 'uvicorn', 'psycopg', 'valkey', 'aiohttp', 'dns', 'graphql')
HARDWARE_DEPENDENCIES = ('smartcard', 'ykman', 'yubikit')
FORBIDDEN = (
    'msg.application',
    'msg.core.executor',
    'msg.storage',
    'msg.admin',
    'msg.workers',
    'msg.security.vault',
    'msg.security.custodial_migration',
    'msg.transports.http',
    'msg.transports.graphql',
    'msg.transports.mcp',
)


class ClientBoundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in FORBIDDEN):
            raise RuntimeError('client imported a server implementation: ' + fullname)
        return None


def check_hardware_boundary(directory, public_key):
    """Descriptors need no SDK; absent drivers fail before any device interaction."""
    from msg import client_yubikey as hardware
    from msg.client import ClientState
    from msg.core.errors import Failure

    signer = hardware.YubiKeySigner(public_key, '83')
    assert hardware.YubiKeySigner.from_descriptor(signer.descriptor()) == signer
    if any(importlib.util.find_spec(name) is not None for name in HARDWARE_DEPENDENCIES):
        return False  # Never probe devices in an environment with installed card drivers.
    empty = ClientState(directory / 'unconfigured', server='https://unit.invalid')
    saved = ClientState(directory / 'hardware', server='https://unit.invalid')
    saved.save_signer(signer)
    saved.data.update(subject_id='u_test', handle='test', certificates=[])
    saved._save()
    operations = {
        'sdk': hardware.sdk,
        'initialize': lambda: hardware.initialize(empty, '83'),
        'attach': lambda: hardware.attach(empty, '83'),
        'load_profile': lambda: hardware.load_profile(empty.server, '83'),
        'sign': lambda: signer.sign(b'client boundary', purpose='request'),
        'save_profile': lambda: hardware.save_profile(saved),
    }
    for name, operation in operations.items():
        try:
            operation()
        except Failure as exc:
            assert exc.code == 'yubikey_driver_unavailable', (name, exc.code)
        else:
            raise AssertionError('missing hardware driver did not fail: ' + name)
    assert empty.signer is None
    assert isinstance(ClientState(directory / 'hardware').signer, hardware.YubiKeySigner)
    assert not any(path.exists() for path in (empty.key_path, saved.key_path, saved.age_key_path))
    cli = subprocess.run(
        [
            sys.executable,
            '-I',
            '-m',
            'msg.cli',
            '--config-dir',
            str(directory / 'cli-no-driver'),
            '--server',
            empty.server,
            'yubikey',
            'init',
            '--slot',
            '83',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert cli.returncode == 1 and not cli.stdout, cli.stdout + cli.stderr
    assert json.loads(cli.stderr)['error']['code'] == 'yubikey_driver_unavailable'
    return True


async def check(directory):
    import httpx

    from msg.client import ClientState
    from msg.core.codec import canonical, loads, wire
    from msg.core.requests import request_for, signing_bytes
    from msg.security.crypto import Ed25519Signer, verify
    from msg.transports.client import TRANSPORTS
    from msg.transports.packet import decode_packet, path_packet

    for name in (
        'cli',
        'tui',
        'client_certificates',
        'client_content',
        'client_custodial',
        'client_market',
        'client_recovery',
        'client_secrets',
        'client_tokens',
        'client_upgrade',
    ):
        importlib.import_module('msg.' + name)
    state = ClientState(directory, server='https://unit.invalid')
    signer = Ed25519Signer.from_bytes(bytes(range(32)))
    state.save_signer(signer)
    state.ensure_encryption_key()
    restored = ClientState(directory)
    assert restored.signer.public_key == signer.public_key
    assert restored.encryption_recipient == state.encryption_recipient
    for path in (state.path, state.key_path, state.age_key_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    request = request_for(
        'content.read',
        {'id': 'r_test'},
        state.server,
        subject='u_test',
        signer=signer,
        request_id='client-boundary',
    )
    calls = []

    def respond(incoming):
        path = incoming.url.path
        if path == '/_transports':
            return httpx.Response(
                200,
                json={
                    'version': 1,
                    'target_service': state.server,
                    'operations': {'content.read': 'read'},
                    'limits': {
                        'max_request_bytes': 1048576,
                        'max_response_bytes': 1048576,
                        'max_path_bytes': 8192,
                        'encodings': ['j', 'gz'],
                    },
                },
            )
        if path.startswith('/-/g/'):
            _, _, _, operation, encoding, encoded = path.split('/')
            packet = path_packet(encoded, encoding, 1048576)
            assert packet.operation == operation and incoming.method == 'GET'
            kind = 'path_get'
        else:
            data = loads(incoming.content)
            if path == '/_read/graphql':
                data = data['variables']['packet']
                kind = 'graphql'
            elif path == '/-/mcp':
                assert incoming.headers['MCP-Protocol-Version'] == '2025-11-25'
                data = data['params']['arguments']['packet']
                kind = 'mcp_http'
            else:
                assert path == '/-/p/content.read'
                kind = 'http'
            assert incoming.method == 'POST'
            packet = decode_packet(data)
        assert canonical(packet) == canonical(request)
        verify(signer.public_key, signing_bytes(packet), packet.proof.signature, purpose='request')
        calls.append(kind)
        result = {
            'request_id': packet.request_id,
            'operation': packet.operation,
            'status': 'ok',
            'actor': 'u_test',
            'subject': 'u_test',
            'resources': [{'id': 'r_test'}],
            'data': {'content': 'signed client response'},
        }
        if kind == 'graphql':
            result = {'data': {'call': result}}
        elif kind == 'mcp_http':
            result = {
                'jsonrpc': '2.0',
                'id': packet.request_id,
                'result': {'structuredContent': result},
            }
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        for name, transport in TRANSPORTS.items():
            result = await transport(state.server, http=http).call(request)
            assert result.status == 'ok' and result.resources[0].id == 'r_test', (
                name,
                wire(result),
            )
            assert result.data['content'] == 'signed client response'
    assert sorted(calls) == sorted(TRANSPORTS)
    assert not any(
        any(name == prefix or name.startswith(prefix + '.') for prefix in FORBIDDEN)
        for name in sys.modules
    )
    hardware_checked = check_hardware_boundary(directory, signer.public_key)
    return calls, hardware_checked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--minimal-install', action='store_true')
    args = parser.parse_args()
    if args.minimal_install:
        present = [
            name for name in SERVER_DEPENDENCIES if importlib.util.find_spec(name) is not None
        ]
        assert not present, 'server-only Python dependencies in client environment: ' + ', '.join(
            present
        )
    sys.meta_path.insert(0, ClientBoundary())
    with tempfile.TemporaryDirectory(prefix='msg-client-install-') as folder:
        calls, hardware_checked = asyncio.run(check(Path(folder) / 'client'))
    print(
        json.dumps({
            'status': 'ok',
            'transports': sorted(calls),
            'server_implementation_imported': False,
            'minimal_install': args.minimal_install,
            'hardware_signer_descriptor_checked': True,
            'hardware_driver_unavailable_checked': hardware_checked,
        })
    )


if __name__ == '__main__':
    main()
