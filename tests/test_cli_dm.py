"""The DM convenience commands use the same signed public operations."""

import httpx
import pytest
from test_service import NOW

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import loads
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_dm_cli_request_accept_send_read_and_block(installed, tmp_path, monkeypatch, capsys):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        alice_dir, bob_dir = tmp_path / 'alice', tmp_path / 'bob'
        alice = MsgClient(
            ClientState(alice_dir, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        bob = MsgClient(
            ClientState(bob_dir, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await alice.register('cli-dm-alice')).status == 'ok'
        assert (await bob.register('cli-dm-bob')).status == 'ok'
        monkeypatch.setattr(
            cli,
            'MsgClient',
            lambda state, transport: MsgClient(state, transport, clock=lambda: NOW),
        )

        async def invoke(directory, *command):
            args = cli.parser().parse_args([
                '--config-dir',
                str(directory),
                '--server',
                app.settings.service_url,
                'dm',
                *command,
            ])
            status = await cli.run(args)
            result = loads(capsys.readouterr().out.encode().strip())
            return status, result

        status, requested = await invoke(
            alice_dir, 'request', bob.state.subject, '--intro', 'Hello from Alice'
        )
        assert status == 0 and requested['status'] == 'ok'
        conversation = requested['data']['conversation_id']
        intro_id = requested['data']['introduction_ref']['id']
        status, introduction = await invoke(bob_dir, 'read', intro_id)
        assert status == 0 and introduction['data']['content'] == 'Hello from Alice'
        status, accepted = await invoke(bob_dir, 'accept', conversation)
        assert status == 0 and accepted['data']['state'] == 'active'
        status, sent = await invoke(
            alice_dir, 'send', conversation, '--text', 'private CLI message'
        )
        assert status == 0 and sent['status'] == 'ok'
        post = sent['resources'][0]['id']
        async with app.metadata.transaction(write=False) as tx:
            before = tx.one("SELECT COUNT(*) FROM reactions WHERE kind LIKE 'ack.%'")[0]
        status, reading = await invoke(bob_dir, 'read', post)
        assert status == 0 and reading['data']['content'] == 'private CLI message'
        status, listed = await invoke(bob_dir, 'list')
        assert status == 0 and any(
            item['conversation_id'] == conversation for item in listed['data']['items']
        )
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM reactions WHERE kind LIKE 'ack.%'")[0] == before
        status, blocked = await invoke(bob_dir, 'block', alice.state.subject)
        assert status == 0 and blocked['data']['blocked'] is True
        status, denied = await invoke(alice_dir, 'send', conversation, '--text', 'after block')
        assert status == 1 and denied['status'] == 'error'
