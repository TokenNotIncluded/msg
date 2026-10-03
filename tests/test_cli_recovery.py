"""Recovery CLI requires opt-in and restores only a local age identity."""

import shutil

import httpx
import pytest
from test_service import NOW

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import loads
from msg.security.age_keys import _encode, generate_age_key, recipient_from_identity
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_cli_policy_backup_and_offline_restore(installed, tmp_path, monkeypatch, capsys):
    if shutil.which('age') is None:
        pytest.skip('age CLI is unavailable')
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        directory = tmp_path / 'client'
        state = ClientState(directory, server=app.settings.service_url)
        client = MsgClient(
            state, HTTPTransport(app.settings.service_url, http=http), clock=lambda: NOW
        )
        assert (await client.register('cli-recovery')).status == 'ok'
        old_recipient = state.encryption_recipient
        custodian_private, custodian_recipient = generate_age_key()
        custodian = tmp_path / 'custodian.agekey'
        custodian.write_text(custodian_private + '\n')
        custodian.chmod(0o600)
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        selected_versions = []

        def cli_client(state, transport):
            result = MsgClient(state, transport, clock=lambda: NOW)
            original_call = result.call

            async def observed_call(operation, *args, **kwargs):
                if operation == 'identity.recovery_policy_set':
                    selected_versions.append(kwargs.get('contract_version', 1))
                return await original_call(operation, *args, **kwargs)

            result.call = observed_call
            return result

        monkeypatch.setattr(cli, 'MsgClient', cli_client)

        async def invoke(*command):
            parsed = cli.parser().parse_args([
                '--config-dir',
                str(directory),
                '--server',
                app.settings.service_url,
                'recovery',
                *command,
            ])
            status = await cli.run(parsed)
            return status, loads(capsys.readouterr().out.encode().strip())

        status, policy = await invoke('policy-set', '--recipient', custodian_recipient)
        assert status == 0 and policy['data']['version'] == 1
        status, backup = await invoke(
            'backup', '--recipient', custodian_recipient, '--policy-version', '1'
        )
        assert status == 0 and backup['envelope']['data']['recipient_claim'] == (
            'owner_declared_unverified'
        )
        status, listed = await invoke('list')
        assert status == 0 and len(listed['data']['items']) == 1
        resource = backup['keystore']['resources'][0]['id']
        entry = client.checked(await client.call('keystore.get', {'id': resource}))
        ciphertext = tmp_path / 'backup.age'
        await client.download(entry.output, ciphertext)
        output = tmp_path / 'restored.agekey'
        status, restored = await invoke(
            'restore',
            str(ciphertext),
            '--identity',
            str(custodian),
            '--output',
            str(output),
            '--subject',
            state.subject,
            '--key-id',
            backup['metadata']['encryption_key_id'],
        )
        assert status == 0 and restored['account_authority'] == 'none'
        assert recipient_from_identity(output.read_text().strip()) == old_recipient
        assert output.stat().st_mode & 0o077 == 0
        assert selected_versions == [1]
        status, _ = await invoke('policy-set', '--recipient', _encode('age1yubikey', b'public'))
        assert status == 0 and selected_versions[-1] == 2
        status, _ = await invoke('policy-set', '--recipient', _encode('age1tag', b'public'))
        assert status == 0 and selected_versions[-1] == 2
        status, _ = await invoke('policy-set', '--clear')
        assert status == 0 and selected_versions[-1] == 1
        status, _ = await invoke('policy-set', '--recipient', custodian_recipient)
        assert status == 0 and selected_versions[-1] == 1
