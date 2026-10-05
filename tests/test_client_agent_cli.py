"""Exercise actual CLI processes without a server or network access."""

import json
import subprocess
import sys
import time

import pytest

from msg.cli import parser
from msg.client_connection import expand_connection_args


def invocation(tmp_path, *args):
    return [
        sys.executable,
        '-m',
        'msg.cli',
        '--config-dir',
        str(tmp_path / 'identity'),
        '--server',
        'https://offline.invalid',
        '--offline',
        '--username',
        'alice',
        *args,
    ]


def run(tmp_path, *args):
    result = subprocess.run(invocation(tmp_path, *args), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_offline_cli_exchange_and_idempotent_retry(tmp_path):
    run(tmp_path, 'agent', 'create', 'bot1')
    run(tmp_path, 'agent', 'create', 'bot2')
    first = run(tmp_path, '--agent', 'bot1', 'agent', 'send', '@alice#bot2', 'offline payload')
    again = run(
        tmp_path,
        '--agent',
        'bot1',
        'agent',
        'send',
        '@alice#bot2',
        'offline payload',
        '--message-id',
        first['data']['id'],
    )
    assert first['data']['id'] == again['data']['id']
    inbox = run(tmp_path, '--agent', 'bot2', 'agent', 'inbox')
    assert len(inbox['data']['items']) == 1
    assert inbox['data']['items'][0]['from'] == '@alice#bot1'
    assert inbox['data']['items'][0]['message'] == 'offline payload'
    assert not (tmp_path / 'identity' / 'identity.key').exists()


def test_listener_process_waits_then_delivers_and_resumes_without_duplicate(tmp_path):
    run(tmp_path, 'agent', 'create', 'bot1')
    run(tmp_path, 'agent', 'create', 'bot2')
    checkpoint = tmp_path / 'bot2.json'
    process = subprocess.Popen(
        invocation(
            tmp_path,
            '--agent',
            'bot2',
            'listen',
            '--from-now',
            '--max-events',
            '1',
            '--interval',
            '0.1',
            '--cursor-file',
            str(checkpoint),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        while not checkpoint.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.03)
        assert checkpoint.exists() and process.poll() is None
        sent = run(tmp_path, '--agent', 'bot1', 'agent', 'send', '@alice#bot2', 'wake up')
        output, error = process.communicate(timeout=8)
        assert process.returncode == 0 and not error
        assert len(output.splitlines()) == 1
        assert json.loads(output)['id'] == sent['data']['id']
        resumed = subprocess.run(
            invocation(
                tmp_path, '--agent', 'bot2', 'listen', '--once', '--cursor-file', str(checkpoint)
            ),
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert resumed.returncode == 0 and resumed.stdout == '' and resumed.stderr == ''
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def test_local_cli_forbids_network_even_when_account_state_is_missing(tmp_path):
    script = """import socket, httpx
from msg.cli import main
def blocked(*a, **kw): raise AssertionError('Network access from offline CLI')
socket.socket.connect = blocked
httpx.AsyncClient.request = blocked
raise SystemExit(main(__import__('sys').argv[1:]))"""
    result = subprocess.run(
        [sys.executable, '-c', script, *invocation(tmp_path)[3:], 'agent', 'create', 'bot1'],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stderr


def test_agent_address_and_host_expansion():
    value = expand_connection_args(
        ['--agent', 'bot1', 'alice@msg.example.org', 'agent send "@alice#bot2" "hello"'],
        {'agent', 'listen'},
    )
    args = parser().parse_args(value)
    assert args.agent == 'bot1' and args.user == 'alice' and args.recipient == '@alice#bot2'
    assert args.message == 'hello'


def test_remote_receipt_parses_without_changing_default_send():
    command = ['--agent', 'bot1', 'agent', 'send', '@alice#bot2', 'hello', '--remote']
    assert parser().parse_args(command + ['--receipt']).receipt is True
    assert parser().parse_args(command).receipt is False


def test_once_page_budget_cli_parses_and_keeps_default_unbounded():
    assert parser().parse_args(['listen', '--once', '--max-pages', '2']).max_pages == 2
    assert parser().parse_args(['listen', '--once']).max_pages is None


def test_receipt_requires_remote_before_creating_local_state(tmp_path):
    result = subprocess.run(
        invocation(
            tmp_path, '--agent', 'bot1', 'agent', 'send', '@alice#bot2', 'hello', '--receipt'
        ),
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 1
    assert json.loads(result.stderr)['error']['code'] == 'receipt_requires_remote'
    assert not (tmp_path / 'identity').exists()


@pytest.mark.parametrize(
    'command',
    [['--agent', 'bot1', 'post', '/main', '--text', 'bad'], ['--offline', 'listen', '--once']],
)
def test_agent_context_rejects_public_post_and_offline_account_feed(tmp_path, command):
    result = subprocess.run(
        [
            sys.executable,
            '-m',
            'msg.cli',
            '--config-dir',
            str(tmp_path / 'id'),
            '--server',
            'https://offline.invalid',
            *command,
        ],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 1
    assert json.loads(result.stderr)['status'] == 'error'


def test_agent_inbox_tail_and_list_active_flags(tmp_path):
    run(tmp_path, 'agent', 'create', 'bot1')
    run(tmp_path, 'agent', 'create', 'bot2')
    run(tmp_path, 'agent', 'archive', 'bot2')

    all_agents = run(tmp_path, 'agent', 'list')
    assert len(all_agents['data']) == 2

    active_agents = run(tmp_path, 'agent', 'list', '--active')
    assert len(active_agents['data']) == 1
    assert active_agents['data'][0]['label'] == 'bot1'

    tail = run(tmp_path, '--agent', 'bot1', 'agent', 'inbox', '--tail')
    assert tail['data']['items'] == []
    assert isinstance(tail['data']['cursor'], str)


def test_agent_show_read_and_sender_filter(tmp_path):
    run(tmp_path, 'agent', 'create', 'bot1')
    run(tmp_path, 'agent', 'create', 'bot2')
    run(tmp_path, 'agent', 'create', 'bot3')

    # Test agent show
    show_bot1 = run(tmp_path, 'agent', 'show', 'bot1')
    assert show_bot1['data']['label'] == 'bot1'
    assert show_bot1['data']['active'] is True

    # Test agent send with custom id
    msg_id = 'test-msg-001'
    sent = run(
        tmp_path,
        '--agent',
        'bot1',
        'agent',
        'send',
        'bot2',
        'hello from bot1',
        '--message-id',
        msg_id,
    )
    assert sent['data']['id'] == msg_id

    # Send another message from bot3 to bot2
    run(tmp_path, '--agent', 'bot3', 'agent', 'send', 'bot2', 'hello from bot3')

    # Test agent read direct
    read_msg = run(tmp_path, 'agent', 'read', msg_id, 'bot2')
    assert read_msg['data']['id'] == msg_id
    assert read_msg['data']['message'] == 'hello from bot1'

    # Also test agent read with --agent bot2
    read_msg_agent = run(tmp_path, '--agent', 'bot2', 'agent', 'read', msg_id)
    assert read_msg_agent['data']['id'] == msg_id

    # Test inbox filtering by sender
    inbox_bot1 = run(tmp_path, 'agent', 'inbox', 'bot2', '--sender', 'bot1')
    assert len(inbox_bot1['data']['items']) == 1
    assert inbox_bot1['data']['items'][0]['id'] == msg_id

    inbox_bot3 = run(tmp_path, 'agent', 'inbox', 'bot2', '--from', 'bot3')
    assert len(inbox_bot3['data']['items']) == 1
    assert inbox_bot3['data']['items'][0]['message'] == 'hello from bot3'

