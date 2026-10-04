import asyncio
from pathlib import Path

import httpx
import pytest
from test_client_link import args
from test_service import NOW

from msg.atomic_file import durable_write
from msg.client import ClientState, MsgClient
from msg.client_link import run_command
from msg.core.codec import canonical, loads
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from msg.transports.mcp import MCPServer
from msg.transports.tool_bridge import ToolBridgeTransport


@pytest.mark.asyncio
async def test_offline_signer_completes_link_through_pinned_mcp_connector(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner = MsgClient(
            ClientState(tmp_path / 'owner', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await owner.register('alice')).status == 'ok'
        invited = await run_command(
            owner,
            args('invite', name='reviewer', task='Connector test.', task_file=None, minutes=30),
        )
        directory = tmp_path / 'exchange'
        bridge = ToolBridgeTransport(app.settings.service_url, exchange_dir=directory, timeout=10)
        helper = MsgClient(
            ClientState(tmp_path / 'helper', server=app.settings.service_url),
            bridge,
            clock=lambda: NOW,
        )
        mcp, calls = MCPServer(app), []

        async def relay():
            while True:
                for file in directory.glob('*.request.json'):
                    request = loads(file.read_bytes())
                    response = Path(request['response_file'])
                    if response.exists():
                        continue
                    assert request['connector'] == app.settings.service_url + '/-/mcp'
                    packet = request['arguments']['packet']
                    assert 'private_key' not in packet and 'token' not in str(packet.get('proof'))
                    result = await mcp.handle({
                        'jsonrpc': '2.0',
                        'id': request['exchange_id'],
                        'method': 'tools/call',
                        'params': {'name': request['tool'], 'arguments': request['arguments']},
                    })
                    assert 'error' not in result, result
                    calls.append(request['tool'])
                    durable_write(
                        response,
                        canonical({
                            'exchange_id': request['exchange_id'],
                            'server': request['server'],
                            'result': result['result']['structuredContent'],
                        }),
                        mode=0o600,
                    )
                await asyncio.sleep(0.01)

        worker = asyncio.create_task(relay())
        try:
            await run_command(helper, args('join', code=invited['data']['invite_code'], wait=False))
            watched = await run_command(owner, args('watch', once=True, interval=0.1))
            assert watched['data']['approved'] == ['reviewer']
            result = await run_command(
                helper, args('join', code=invited['data']['invite_code'], wait=True, timeout=10)
            )
            assert result['data']['task']['message'] == 'Connector test.'
            assert '--transport tool_bridge' in result['data']['commands']['send']
            assert {
                'identity.link_claim',
                'identity.link_collect',
                'file.read',
                'file.create',
            } <= set(calls)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_bridge_timeout_keeps_private_request_for_retry(tmp_path):
    from msg.core.errors import Failure
    from msg.core.requests import request_for

    packet = request_for(
        'identity.link_collect', {}, 'https://msg.example', request_id='bridge-timeout'
    )
    bridge = ToolBridgeTransport(
        'https://msg.example', exchange_dir=tmp_path / 'exchange', timeout=0
    )
    with pytest.raises(Failure, match='tool_exchange_timeout') as caught:
        await bridge.call(packet)
    assert caught.value.retryable
    request = next(bridge.directory.glob('*.request.json'))
    assert request.stat().st_mode & 0o077 == 0
    value = loads(request.read_bytes())
    assert value['server'] == 'https://msg.example'
    assert value['arguments']['packet']['request_id'] == packet.request_id


@pytest.mark.asyncio
async def test_bridge_rejects_other_service_and_reusable_secrets(tmp_path):
    from msg.core.errors import Failure
    from msg.core.requests import request_for

    bridge = ToolBridgeTransport(
        'https://msg.example', exchange_dir=tmp_path / 'exchange', timeout=0
    )
    wrong = request_for('discovery.get', {}, 'https://other.example')
    with pytest.raises(Failure, match='service_mismatch'):
        await bridge.call(wrong)
    secret = request_for(
        'discovery.get', {}, 'https://msg.example', token=('credential', b'x' * 32)
    )
    with pytest.raises(Failure, match='secure_channel_required'):
        await bridge.call(secret)
    assert not list(bridge.directory.iterdir())
