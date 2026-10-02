"""Local status identifies the selected profile without acquiring remote authority."""

import httpx
import pytest

from msg import cli
from msg.atomic_file import durable_write
from msg.client import ClientState
from msg.client_yubikey import YubiKeySigner
from msg.core.codec import b64, canonical, loads
from msg.security.crypto import Ed25519Signer
from msg.transports.client import HTTPTransport

SERVER = 'https://example.org'


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MSG_SERVER', SERVER)
    for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def configure(state, methods):
    if 'signature' in methods:
        state.save_signer(Ed25519Signer.generate())
    if 'yubikey' in methods:
        state.save_signer(YubiKeySigner(Ed25519Signer.generate().public_key, '83'))
    if 'token' in methods:
        state.data['token'] = {'credential_id': 't_temporary', 'value': b64(b't' * 32)}
    if 'api-key' in methods:
        state.data['api_key'] = {'credential_id': 't_api', 'value': b64(b'a' * 32)}
    if 'oauth' in methods:
        durable_write(
            state.file('oauth-session.json'),
            canonical({
                'server': SERVER,
                'client_id': 'msg-cli',
                'subject_id': state.subject,
                'access_token': 't_oauth_selected.' + b64(b'o' * 32),
                'refresh_token': 'refresh-secret-for-this-test',
            }),
            mode=0o600,
        )
    state._save()


@pytest.mark.parametrize(
    'methods,auth',
    [
        ((), 'none'),
        (('signature',), 'signature'),
        (('yubikey',), 'signature'),
        (('signature', 'api-key'), 'api-key'),
        (('signature', 'api-key', 'token'), 'token'),
        (('signature', 'api-key', 'token', 'oauth'), 'oauth'),
    ],
)
async def test_cli_identity_show_reports_actual_local_account_and_auth(
    home, monkeypatch, capsys, methods, auth
):
    state = ClientState(account='work-account')
    state.data.update(handle='alice', subject_id='u_alice')
    configure(state, methods)
    before = state.path.read_bytes()

    def forbidden(request):
        raise AssertionError('local identity status must not contact a service')

    def read_secret(state):
        raise AssertionError('status needs only session presence, not its token bytes')

    monkeypatch.setattr('msg.client_oauth.read_session', read_secret)
    monkeypatch.setattr('msg.client_yubikey.device_session', read_secret)
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        assert (
            await cli.run(
                cli.parser().parse_args(['--account', 'work-account', 'identity', 'show'])
            )
            == 0
        )
    output = capsys.readouterr().out
    shown = loads(output.encode())
    assert shown['auth'] == auth
    assert shown['account'] == 'work-account' and shown['handle'] == 'alice'
    assert shown['subject_id'] == 'u_alice' and shown['server'] == SERVER
    for secret in (b64(b't' * 32), b64(b'a' * 32), b64(b'o' * 32), 'refresh-secret-for-this-test'):
        assert secret not in output
    assert state.path.read_bytes() == before


async def test_invocation_key_override_remains_the_reported_signer(home, monkeypatch, capsys):
    state = ClientState(account='alice')
    state.data.update(handle='alice', subject_id='u_alice')
    configure(state, ('signature', 'token', 'api-key', 'oauth'))
    override = Ed25519Signer.generate()
    override_path = home / 'invocation.key'
    durable_write(override_path, override.private_bytes(), mode=0o600)
    before = state.path.read_bytes()

    def forbidden(request):
        raise AssertionError('local identity status must not contact a service')

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        assert (
            await cli.run(
                cli.parser().parse_args([
                    '--account',
                    'alice',
                    '--key',
                    str(override_path),
                    'identity',
                    'show',
                ])
            )
            == 0
        )
    output = capsys.readouterr().out
    shown = loads(output.encode())
    assert shown['auth'] == 'signature' and shown['key_id'] == override.key_id
    assert shown['account'] == 'alice' and shown['handle'] == 'alice'
    assert state.path.read_bytes() == before
    assert ClientState(account='alice').signer.key_id == state.signer.key_id
