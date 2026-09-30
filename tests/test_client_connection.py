"""SSH-like connection/config syntax with real identity and write boundaries."""

import argparse

import httpx
import pytest
from test_service import NOW, register

from msg import cli
from msg.client import ClientState, MsgClient
from msg.client_connection import expand_connection_args, host_options
from msg.core.codec import loads
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


def parse(argv):
    parser = cli.parser()
    commands = next(
        action.choices
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return parser.parse_args(expand_connection_args(argv, commands))


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    for variable in (
        'XDG_CONFIG_HOME',
        'XDG_DATA_HOME',
        'XDG_STATE_HOME',
        'XDG_CACHE_HOME',
        'MSG_SERVER',
    ):
        monkeypatch.delenv(variable, raising=False)
    return tmp_path


@pytest.mark.parametrize('command', [[], [''], ['   ']])
def test_requested_user_at_domain_empty_command_opens_tui(home, command):
    args = parse(['lightjunction@msg.lmm.best', *command])
    assert args.server == 'https://msg.lmm.best'
    assert args.user == 'lightjunction'
    assert args.command == 'tui'


def test_quoted_commands_and_existing_cli_arguments(home):
    args = parse([
        'lightjunction@own.example.org',
        'post /main --text "hello world; $(touch /tmp/bad)"',
    ])
    assert args.command == 'post' and args.topic == '/main'
    assert args.text == 'hello world; $(touch /tmp/bad)'
    ordinary = ['--server', 'https://own.example.org', 'identity', 'show']
    assert expand_connection_args(ordinary, {'identity'}) == ordinary


def test_host_config_matches_alias_patterns_and_first_value_wins(home):
    config = home / '.config/msg/config'
    config.parent.mkdir(parents=True)
    config.write_text("""Host work other
    HostName=own.example.org
    User lightjunction
    Port 8443
    IdentityFile "~/.local/share/msg/services/own.example.org~8443/identity.key"
Host * !excluded
    User default-user
    Scheme https
""")
    config.chmod(0o600)
    args = parse(['work', 'identity show'])
    assert args.server == 'https://own.example.org:8443'
    assert args.user == 'lightjunction'
    assert args.key == home / '.local/share/msg/services/own.example.org~8443/identity.key'
    assert host_options('excluded') == {}
    assert host_options('random')['user'] == 'default-user'
    overridden = parse(['-l', 'override-user', '-p', '443', 'work', 'identity show'])
    assert overridden.user == 'override-user' and overridden.server == 'https://own.example.org'
    explicit_user = parse(['different-user@work', 'identity show'])
    assert explicit_user.user == 'different-user'


def test_explicit_config_file_and_ipv6(home):
    config = home / 'custom-config'
    config.write_text('Host local\nHostName [::1]\nPort 8042\nScheme http\nUser local-user\n')
    config.chmod(0o600)
    args = parse(['-F', str(config), 'local', 'identity show'])
    assert args.server == 'http://[::1]:8042' and args.user == 'local-user'


@pytest.mark.parametrize(
    'argv,code',
    [
        (['bad@@example.org'], 'invalid_connection_target'),
        (['@example.org'], 'invalid_connection_target'),
        (['invalid_user@example.org'], 'invalid_connection_user'),
        (['alice@example.org', 'sh -c echo'], 'unknown_connection_command'),
        (['alice@example.org', 'post "'], 'invalid_connection_command'),
        (['-p', '99999', 'alice@example.org'], 'invalid_connection_port'),
        (
            ['--server', 'https://different.example.org', 'alice@example.org'],
            'connection_server_conflict',
        ),
    ],
)
def test_invalid_connections_fail_before_any_local_identity_or_network(home, argv, code):
    with pytest.raises(Failure, match=code):
        parse(argv)
    assert not (home / '.local/share/msg').exists()


def test_unsafe_config_and_executable_directives_are_rejected(home):
    config = home / '.config/msg/config'
    config.parent.mkdir(parents=True)
    config.write_text('Host work\nProxyCommand echo secret\n')
    config.chmod(0o600)
    with pytest.raises(Failure, match='unsupported_connection_config'):
        parse(['work'])
    config.write_text('Host work\nHostName own.example.org\n')
    config.chmod(0o666)
    with pytest.raises(Failure, match='unsafe_connection_config'):
        parse(['work'])
    config.unlink()
    config.symlink_to(home / 'missing')
    with pytest.raises(Failure, match='unsafe_connection_config'):
        parse(['work'])


@pytest.mark.asyncio
async def test_alias_and_user_at_host_use_same_identity_and_wrong_user_cannot_write(
    installed,
    home,
    monkeypatch,
    capsys,
):
    app, _ = installed
    config = home / '.config/msg/config'
    config.parent.mkdir(parents=True)
    config.write_text(
        'Host work testserver\nHostName testserver\nScheme http\nUser connection-agent\n'
    )
    config.chmod(0o600)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        state = ClientState(server=app.settings.service_url)
        client = MsgClient(state, HTTPTransport(state.server, http=http), clock=lambda: NOW)
        assert (await client.register('connection-agent')).status == 'ok'
        await register(app, 'different-user')
        key_before = state.key_path.read_bytes()
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        monkeypatch.setattr(
            cli,
            'MsgClient',
            lambda state, transport: MsgClient(state, transport, clock=lambda: NOW),
        )
        assert await cli.run(parse(['work', 'identity show'])) == 0
        shown = loads(capsys.readouterr().out.encode())
        assert shown['subject_id'] == state.subject
        assert (
            await cli.run(parse(['connection-agent@testserver', 'post /main --text "hello world"']))
            == 0
        )
        posted = loads(capsys.readouterr().out.encode())
        assert posted['status'] == 'ok' and posted['actor'] == state.subject
        async with app.metadata.transaction(write=False) as tx:
            count = tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0]
        with pytest.raises(Failure, match='connection_user_mismatch'):
            await cli.run(
                parse(['different-user@testserver', 'post /main --text "must not publish"'])
            )
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == count
        assert state.key_path.read_bytes() == key_before
        assert ClientState(server=app.settings.service_url).subject == state.subject
