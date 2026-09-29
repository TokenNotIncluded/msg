"""Real CLI and subject-path watch contract parity."""

import httpx
import pytest
from test_service import NOW

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import loads
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_watch_cli_uses_current_public_contract(installed, tmp_path, monkeypatch, capsys):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        directory = tmp_path / 'client'
        client = MsgClient(
            ClientState(directory, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('watch-cli')).status == 'ok'
        monkeypatch.setattr(
            cli,
            'MsgClient',
            lambda state, transport: MsgClient(state, transport, clock=lambda: NOW),
        )

        async def invoke(*command):
            args = cli.parser().parse_args([
                '--config-dir',
                str(directory),
                '--server',
                app.settings.service_url,
                'watch',
                *command,
            ])
            status = await cli.run(args)
            output = loads(capsys.readouterr().out.encode().strip())
            assert status == 0, output
            return output['data']

        made = await invoke('create', '/main', '--event', 'content.post_create')
        wid = made['id']
        listing = await invoke('list')
        assert [item['id'] for item in listing['items']] == [wid]
        got = await invoke('get', wid)
        assert got['status'] == 'active' and got['delivery'] == 'inbox'
        assert got['event_types'] == ['content.post_create']
        assert 'principal' not in got and 'operation' not in got
        cancelled = await invoke('cancel', wid)
        assert cancelled == {'id': wid, 'status': 'cancelled'}
        assert (await invoke('get', wid))['status'] == 'cancelled'
