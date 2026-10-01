"""Explicit service defaults and terminal output preserve script/identity boundaries."""

import argparse
import io
import json
from types import SimpleNamespace

import pytest
from test_oauth import oauth as oauth

from msg import cli
from msg.client import ClientState
from msg.client_connection import expand_connection_args
from msg.client_display import print_result, render_text
from msg.client_servers import run_command
from msg.core.codec import loads
from msg.core.errors import Failure
from msg.paths import ClientPaths


@pytest.fixture
def home(tmp_path, monkeypatch):
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


def parse(argv):
    parser = cli.parser()
    commands = next(
        action.choices
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return parser.parse_args(expand_connection_args(argv, commands))


def test_default_can_be_set_before_any_identity_without_connecting(home):
    empty = run_command(parse(['server', 'show']))
    assert empty['default_server'] is None
    assert not (home / '.config/msg').exists()
    result = run_command(parse(['server', 'use', 'https://EXAMPLE.org:443/']))
    assert result['default_server'] == 'https://example.org'
    marker = home / '.config/msg/service.json'
    assert marker.stat().st_mode & 0o777 == 0o600
    assert not (home / '.local/share/msg').exists()
    assert not (home / '.local/state/msg').exists()
    assert ClientState().server == 'https://example.org'


def test_one_time_and_environment_overrides_preserve_default(home, monkeypatch):
    run_command(parse(['server', 'use', 'https://default.example']))
    ClientState(server='https://temporary.example')
    assert ClientState().server == 'https://default.example'
    monkeypatch.setenv('MSG_SERVER', 'https://environment.example')
    assert ClientState().server == 'https://environment.example'
    shown = run_command(parse(['server', 'show']))
    assert shown['default_server'] == 'https://default.example'
    assert shown['source'] == 'MSG_SERVER'
    assert (
        run_command(parse(['--server', 'https://explicit.example', 'server', 'show']))['server']
        == 'https://explicit.example'
    )
    monkeypatch.delenv('MSG_SERVER')
    assert ClientState().server == 'https://default.example'


def test_explicit_selection_changes_server_and_keeps_per_service_account(home):
    from msg.client_accounts import run_command as account_command

    first = ClientState(server='https://one.example', account='alice')
    first.data['subject_id'] = 'u_alice'
    first._save()
    account_command(parse(['--server', 'https://one.example', 'account', 'use', 'alice']))
    second = ClientState(server='https://two.example', account='bob')
    second.data['subject_id'] = 'u_bob'
    second._save()
    account_command(parse(['--server', 'https://two.example', 'account', 'use', 'bob']))
    run_command(parse(['server', 'use', 'https://two.example']))
    assert ClientState().account == 'bob'
    run_command(parse(['server', 'use', 'https://one.example']))
    assert ClientState().account == 'alice'
    assert ClientState().subject == 'u_alice'


def test_profile_defaults_are_independent(home):
    run_command(parse(['server', 'use', 'https://global.example']))
    run_command(parse(['--profile', 'work', 'server', 'use', 'https://work.example']))
    assert ClientState(profile='work').server == 'https://work.example'
    assert ClientState().server == 'https://global.example'


@pytest.mark.parametrize(
    'url',
    [
        'https://user:SECRET@example.org',
        'https://example.org/?token=SECRET',
        '../escape',
    ],
)
def test_invalid_server_does_not_replace_selection_or_leak_url(home, url):
    run_command(parse(['server', 'use', 'https://default.example']))
    marker = home / '.config/msg/service.json'
    before = marker.read_bytes()
    with pytest.raises(Failure, match='invalid_server_url') as caught:
        run_command(parse(['server', 'use', url]))
    assert 'SECRET' not in str(caught.value)
    assert marker.read_bytes() == before


def test_selection_symlink_is_rejected_without_touching_target(home):
    folder = home / '.config/msg'
    folder.mkdir(parents=True)
    target = home / 'outside'
    target.write_text('{"version":1,"server":"https://other.example"}')
    (folder / 'service.json').symlink_to(target)
    with pytest.raises(Failure):
        run_command(parse(['server', 'use', 'https://new.example']))
    assert json.loads(target.read_text())['server'] == 'https://other.example'


def test_account_table_and_script_json_share_selection():
    value = {
        'server': 'https://example.org',
        'accounts': [
            {
                'account': 'alice',
                'handle': 'alice',
                'selected': True,
                'signer': 'software',
                'subject_id': 'u_alice',
            },
            {
                'account': 'bob',
                'handle': 'bob',
                'selected': False,
                'signer': 'yubikey',
                'subject_id': 'u_bob',
            },
        ],
    }
    output = io.StringIO()
    output.isatty = lambda: True
    print_result(value, stream=output, context='account')
    assert '*        alice' in output.getvalue()
    assert 'yubikey' in output.getvalue()
    assert 'msg account use NAME' in output.getvalue()
    output = io.StringIO()
    print_result(value, stream=output, context='account')
    assert loads(output.getvalue().encode()) == value
    output = io.StringIO()
    output.isatty = lambda: True
    print_result(value, SimpleNamespace(output_format='json'), stream=output, context='account')
    assert loads(output.getvalue().encode()) == value


def test_human_output_neutralizes_terminal_control_sequences():
    rendered = render_text({'data': {'content': '\x1b[2Jclear\r\x9b31mred\nnormal'}})
    assert '\x1b' not in rendered and '\r' not in rendered and '\x9b' not in rendered
    assert '\\u001b' in rendered and '\nnormal' in rendered


async def test_cli_preferences_output_and_ssh_format(home, capsys):
    assert await cli.run(parse(['--format', 'text', 'server', 'use', 'https://example.org'])) == 0
    assert 'Default server: https://example.org' in capsys.readouterr().out
    assert await cli.run(parse(['--format', 'json', 'server', 'show'])) == 0
    assert loads(capsys.readouterr().out.encode())['server'] == 'https://example.org'
    assert parse(['--format', 'text', 'alice@example.org', 'identity show']).output_format == 'text'
    assert parse(['--format', 'json', 'alice@example.org', 'identity show']).output_format == 'json'
    assert ClientPaths.discover().config.joinpath('service.json').exists()


def test_invalid_approval_explains_recovery_and_selected_identity():
    identity = {
        'account': 'alice',
        'handle': 'alice',
        'subject_id': 'u_alice',
        'server': 'https://example.org',
    }
    value = {
        'status': 'error',
        'operation': 'identity.oauth_approve',
        'request_id': 'req_failed',
        'subject': 'u_alice',
        'error': {'code': 'invalid_grant', 'message': 'The operation could not be completed.'},
    }
    output = io.StringIO()
    print_result(
        value,
        SimpleNamespace(output_format='text'),
        context='auth_approval',
        identity=identity,
        stream=output,
    )
    shown = output.getvalue()
    assert 'expired, was already handled, or is invalid' in shown
    assert 'Open the sign-in page again' in shown
    assert 'Account: alice' in shown and 'Identity: @alice' in shown
    assert 'Request: req_failed' in shown
    output = io.StringIO()
    print_result(value, context='auth_approval', identity=identity, stream=output)
    assert loads(output.getvalue().encode()) == value


def test_approval_success_identifies_account_and_read_scope():
    shown = render_text(
        {
            'status': 'ok',
            'data': {
                'status': 'approved',
                'kind': 'login',
                'scopes': ['openid', 'profile', 'msg.read'],
            },
        },
        context='auth_approval',
        identity={'account': 'bob', 'handle': 'bob', 'server': 'https://example.org'},
    )
    assert 'Authorization: approved' in shown
    assert 'Identity: @bob' in shown
    assert 'Access: openid profile msg.read' in shown
    assert 'Return to this browser sign-in page' in shown


async def test_browser_approval_success_and_consumed_code_use_cli_identity(
    oauth,
    home,
    monkeypatch,
    capsys,
):
    from msg.client import MsgClient
    from msg.transports.client import HTTPTransport
    from msg.transports.oauth_http import csrf

    app, key, subject, http = oauth
    state = ClientState(server=app.settings.service_url, account='owner')
    state.save_signer(key)
    state.data.update(subject_id=subject, handle='oauth-owner')
    state._save()
    monkeypatch.setitem(
        cli.TRANSPORTS, 'http', lambda server, **kw: HTTPTransport(server, http=http)
    )
    monkeypatch.setattr(
        cli,
        'MsgClient',
        lambda state, transport: MsgClient(state, transport, clock=app.clock),
    )
    page = await http.get('/oauth/login')
    code = page.text.split('msg auth approve ')[1].split('</code>')[0]
    argv = [
        '--format',
        'text',
        '--server',
        app.settings.service_url,
        '--account',
        'owner',
        'auth',
        'approve',
        code,
    ]
    assert await cli.run(parse(argv)) == 0
    shown = capsys.readouterr().out
    assert 'Authorization: approved' in shown and 'Identity: @oauth-owner' in shown
    cookie = http.cookies.get('msg_login')
    response = await http.post(
        '/oauth/login/poll',
        json={'csrf': csrf(cookie)},
        headers={'Origin': app.settings.service_url},
    )
    assert response.status_code == 200 and response.json() == {'logged_in': True}
    assert await cli.run(parse(argv)) == 1
    shown = capsys.readouterr().out
    assert 'Error: invalid_grant' in shown
    assert 'Open the sign-in page again' in shown
    assert 'Account: owner' in shown and 'Identity: @oauth-owner' in shown
    assert code not in shown
