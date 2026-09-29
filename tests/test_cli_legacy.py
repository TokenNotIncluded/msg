"""Legacy CLI records signed wishes without performing them."""

import httpx
import pytest
from test_legacy_directive import signed_will
from test_service import NOW

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import canonical, loads
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_legacy_cli_put_revise_archive_keeps_state_active(
    installed, tmp_path, monkeypatch, capsys
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        directory = tmp_path / 'client'
        client = MsgClient(
            ClientState(directory, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('legacy-cli')).status == 'ok'
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
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
                'legacy',
                *command,
            ])
            status = await cli.run(args)
            return status, loads(capsys.readouterr().out.encode().strip())

        first = {
            'visibility': 'public',
            'final_message': 'Please preserve my public posts.',
            'allowed_actions': ['preserve_account'],
        }
        status, created = await invoke('put', canonical(first).decode())
        assert status == 0 and created['status'] == 'ok'
        status, read = await invoke('get')
        assert status == 0 and read['data']['final_message'] == first['final_message']
        revised = {
            **first,
            'final_message': 'Please archive my public posts.',
            'expected_revision': read['data']['revision'],
        }
        status, updated = await invoke('put', canonical(revised).decode())
        assert status == 0 and updated['resources'][0]['revision'] != read['data']['revision']
        status, archived = await invoke('archive')
        assert status == 0 and archived['data']['state'] == 'archived'
        status, current = await invoke('status')
        assert status == 0 and current['data']['state'] == 'active'
        assert current['data']['automatic_transition'] is False


@pytest.mark.asyncio
async def test_legacy_cli_put_can_send_revision_signed_contract(
    installed, tmp_path, monkeypatch, capsys
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        directory = tmp_path / 'client'
        client = MsgClient(
            ClientState(directory, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('legacy-cli-v2')).status == 'ok'
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        monkeypatch.setattr(
            cli,
            'MsgClient',
            lambda state, transport: MsgClient(state, transport, clock=lambda: NOW),
        )
        spec, _ = await signed_will(
            app,
            client.state.signer,
            client.state.subject,
            final_message='Signed from the command line.',
        )
        args = cli.parser().parse_args([
            '--config-dir',
            str(directory),
            '--server',
            app.settings.service_url,
            'legacy',
            'put',
            '--contract-version',
            '2',
            canonical(spec).decode(),
        ])
        status = await cli.run(args)
        created = loads(capsys.readouterr().out.encode().strip())
        assert status == 0 and created['status'] == 'ok', created
        assert created['data']['proof_purpose'] == 'revision'
        assert created['resources'][0]['revision'] == spec['revision_id']
