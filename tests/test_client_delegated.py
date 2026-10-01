"""A worker receives authority without ever receiving the owner's private key."""

from types import SimpleNamespace

import httpx
import pytest
from test_service import NOW

from msg import cli
from msg.client import ClientState, MsgClient
from msg.client_delegated import run_command
from msg.core.codec import canonical, loads
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


def args(action, **kwargs):
    return SimpleNamespace(action='delegated-' + action, **kwargs)


@pytest.mark.asyncio
async def test_public_key_handoff_and_recovery_after_response_loss(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        transport = HTTPTransport(app.settings.service_url, http=http)
        owner_state = ClientState(tmp_path / 'owner', server=app.settings.service_url)
        owner = MsgClient(owner_state, transport, clock=lambda: NOW)
        assert (await owner.register('alice')).status == 'ok'
        worker_state = ClientState(tmp_path / 'worker', server=app.settings.service_url)
        worker = MsgClient(worker_state, transport, clock=lambda: NOW)
        public_request, public_grant = tmp_path / 'request.json', tmp_path / 'grant.json'
        grants_file = tmp_path / 'grants.json'
        prepare = args('prepare', output=public_request, grantor='@alice')
        await run_command(worker, prepare)
        original_key = worker_state.signer.key_id
        await run_command(worker, prepare)
        assert worker_state.signer.key_id == original_key != owner_state.signer.key_id
        prepared = loads(public_request.read_bytes())
        assert set(prepared) == {
            'grantor',
            'target_service',
            'public_key',
            'encryption_recipient',
            'possession_proof',
        }
        grants_file.write_bytes(
            canonical([
                {
                    'capability': 'discovery.basic',
                    'version': 1,
                    'scope': {'resource_id': '/@alice', 'descendants': False},
                    'operations': ['discovery.get@1'],
                    'constraints': {},
                }
            ])
        )
        issue = args(
            'create',
            request=public_request,
            grants=grants_file,
            minutes=10,
            depth=0,
            max_uses=None,
            output=public_grant,
        )
        original = owner.call
        lost = True

        async def lose_response(operation, arguments=None, **kwargs):
            nonlocal lost
            result = await original(operation, arguments, **kwargs)
            if operation == 'identity.delegated_create' and lost:
                lost = False
                raise Failure('transport_uncertain')
            return result

        monkeypatch.setattr(owner, 'call', lose_response)
        with pytest.raises(Failure, match='transport_uncertain'):
            await run_command(owner, issue)
        result = await run_command(owner, issue)
        assert result.status == 'ok' and result.replayed
        accept = args('accept', grant=public_grant)
        await run_command(worker, accept)
        await run_command(worker, accept)
        restored = ClientState(tmp_path / 'worker', server=app.settings.service_url)
        resumed = MsgClient(restored, transport, clock=lambda: NOW)
        read = await resumed.call('discovery.get', {'id': '/@alice'})
        assert read.status == 'ok', read
        assert read.actor == result.data['subject_id'] and read.subject == owner_state.subject
        assert restored.key_path.read_bytes() != owner_state.key_path.read_bytes()
        assert all(
            p.stat().st_mode & 0o777 == 0o600
            for p in (restored.key_path, restored.age_key_path, restored.path)
        )
        revoked = await run_command(owner, args('revoke', id=result.data['delegation_id']))
        assert revoked.status == 'ok'
        denied = await resumed.call('discovery.get', {'id': '/@alice'})
        assert denied.error.code == 'authority_source_inactive'


def test_cli_commands_are_reachable():
    parser = cli.parser()
    parsed = parser.parse_args([
        'identity',
        'delegated-create',
        '--request',
        'request.json',
        '--grants',
        'grants.json',
        '--minutes',
        '30',
        '--max-uses',
        '1',
        '--output',
        'grant.json',
    ])
    assert parsed.minutes == 30 and parsed.max_uses == 1
