"""Offline cooperation uses one account, private local storage, and independent cursors."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from msg.client import ClientState
from msg.client_subagents import LocalAgents, agent_name, normalize_agent, validate_username
from msg.core.errors import Failure


def state_at(path, *, username='alice', subject='s_alice', server='https://msg.example.org'):
    state = ClientState(path, server=server)
    if username:
        state.data['handle'] = username
    if subject:
        state.data['subject_id'] = subject
    return state


def mailbox(tmp_path):
    state = state_at(tmp_path / 'client')
    agents = LocalAgents(state)
    agents.create('bot1')
    agents.create('bot2')
    return state, agents


def test_local_full_labels_and_no_network(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Local agent commands must never use the network')

    monkeypatch.setattr(httpx.Client, 'request', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'request', forbidden)
    state, agents = mailbox(tmp_path)
    event = agents.send('@alice#bot1', '#bot2', '协作信息')
    assert event['type'] == 'subagent.message'
    assert event['from'] == '@alice#bot1'
    assert event['to'] == '@alice#bot2'
    assert agents.inbox('bot2')['items'] == [event]
    assert LocalAgents(state).list() == agents.list()
    assert agents.path.stat().st_mode & 0o077 == 0
    assert agents.directory.stat().st_mode & 0o077 == 0
    assert (agents.directory / 'agents.json').stat().st_mode & 0o077 == 0


def test_offline_explicit_username_persists_without_registration(tmp_path):
    state = state_at(tmp_path / 'client', username=None, subject=None)
    with pytest.raises(Failure, match='subagent_username_required'):
        LocalAgents(state)
    agents = LocalAgents(state, username='@alice')
    agents.create('bot1')
    assert LocalAgents(state).list()[0]['name'] == '@alice#bot1'
    assert 'handle' not in state.data
    assert 'subject_id' not in state.data


@pytest.mark.parametrize(
    'bad', ['', '.', '../bot', 'bot/name', ' bot', '机器人', 'a' * 65, '#', 'b#c']
)
def test_invalid_labels(bad):
    with pytest.raises(Failure, match='invalid_subagent_name'):
        normalize_agent(bad, 'alice')


def test_another_account_cannot_be_addressed(tmp_path):
    state, agents = mailbox(tmp_path)
    assert agent_name('Bot_1', '/@alice') == '@alice#Bot_1'
    assert validate_username('@alice') == 'alice'
    with pytest.raises(Failure, match='subagent_account_mismatch'):
        agents.send('bot1', '@bob#bot2', 'no')
    with pytest.raises(Failure, match='subagent_account_mismatch'):
        LocalAgents(state, username='bob')


def test_message_idempotency_and_archive_retains_history(tmp_path):
    _, agents = mailbox(tmp_path)
    message_id = str(uuid4())
    event = agents.send('bot1', 'bot2', 'first', message_id=message_id)
    assert agents.send('bot1', 'bot2', 'first', message_id=message_id) == event
    with pytest.raises(Failure, match='subagent_message_conflict'):
        agents.send('bot1', 'bot2', 'changed', message_id=message_id)
    assert len(agents.inbox('bot2')['items']) == 1
    archived = agents.archive('bot2')
    assert archived['active'] is False
    assert agents.archive('bot2') == archived
    with pytest.raises(Failure, match='subagent_archived'):
        agents.create('bot2')
    with pytest.raises(Failure, match='subagent_archived'):
        agents.send('bot1', 'bot2', 'too late')
    assert agents.inbox('bot2')['items'] == [event]


def test_cursor_pagination_independent_listeners_and_tail(tmp_path):
    _, agents = mailbox(tmp_path)
    first = agents.send('bot1', 'bot2', 'first')
    tail = agents.inbox('bot2', tail=True)
    assert tail['items'] == []
    second = agents.send('bot1', 'bot2', 'second')
    page = agents.inbox('bot2', limit=1)
    assert page['items'] == [first] and page['has_more'] is True
    assert agents.inbox('bot2', limit=1) == page
    next_page = agents.inbox('bot2', cursor=page['cursor'], limit=1)
    assert next_page['items'] == [second] and next_page['has_more'] is False
    assert agents.inbox('bot2', cursor=tail['cursor'])['items'] == [second]
    assert agents.inbox('bot2', cursor=next_page['cursor'])['items'] == []
    with pytest.raises(Failure, match='invalid_subagent_cursor'):
        agents.inbox('bot1', cursor=page['cursor'])
    with pytest.raises(Failure, match='invalid_subagent_cursor'):
        agents.inbox('bot2', cursor=page['cursor'][:-4] + 'AAAA')


def test_account_and_service_namespaces_are_separate(tmp_path):
    state, alice = mailbox(tmp_path)
    alice.send('bot1', 'bot2', 'alice private')
    cursor = alice.inbox('bot2')['cursor']
    state.data['subject_id'] = 's_bob'
    state.data['handle'] = 'bob'
    bob = LocalAgents(state)
    assert bob.list() == []
    bob.create('bot2')
    assert bob.inbox('bot2')['items'] == []
    with pytest.raises(Failure, match='invalid_subagent_cursor'):
        bob.inbox('bot2', cursor=cursor)
    state.data['subject_id'] = 's_alice'
    state.data['handle'] = 'alice'
    state.server = 'https://other.example.org'
    assert LocalAgents(state).list() == []


def test_registered_handle_rename_retains_mailbox(tmp_path):
    state, agents = mailbox(tmp_path)
    agents.send('bot1', 'bot2', 'existing')
    state.data['handle'] = 'new-alice'
    renamed = LocalAgents(state)
    assert renamed.path == agents.path
    assert renamed.inbox('bot2')['items'][0]['to'] == '@new-alice#bot2'


def test_concurrent_process_connections_do_not_lose_messages(tmp_path):
    state, agents = mailbox(tmp_path)

    def send(index):
        other = LocalAgents(state)
        return other.send('bot1', 'bot2', str(index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        events = list(pool.map(send, range(80)))
    page = agents.inbox('bot2', limit=200)
    assert len(page['items']) == 80
    assert {item['message'] for item in page['items']} == {str(i) for i in range(80)}
    assert len({event['seq'] for event in events}) == 80
    assert [item['seq'] for item in page['items']] == sorted(item['seq'] for item in page['items'])


@pytest.mark.parametrize(
    'filename', ['mailbox.sqlite3', 'mailbox.sqlite3-journal', 'agents.json', 'lock']
)
def test_local_state_rejects_symlinks(tmp_path, filename):
    state, agents = mailbox(tmp_path)
    victim = tmp_path / 'victim'
    victim.write_text('untouched')
    target = agents.directory / filename
    if target.exists():
        target.unlink()
    target.symlink_to(victim)
    with pytest.raises(Failure, match='unsafe_(subagent|client)_state'):
        LocalAgents(state)
    assert victim.read_text() == 'untouched'


def test_local_state_rejects_readable_database_and_hardlinks(tmp_path):
    state, agents = mailbox(tmp_path)
    agents.path.chmod(0o644)
    with pytest.raises(Failure, match='unsafe_subagent_state'):
        LocalAgents(state)
    agents.path.chmod(0o600)
    os.link(agents.path, tmp_path / 'linked-db')
    with pytest.raises(Failure, match='unsafe_subagent_state'):
        LocalAgents(state)


def test_four_real_processes_share_the_offline_queue(tmp_path):
    state, agents = mailbox(tmp_path)
    program = """
import sys
from msg.client import ClientState
from msg.client_subagents import LocalAgents
state = ClientState(sys.argv[1], server='https://msg.example.org')
state.data.update(handle='alice', subject_id='s_alice')
agents = LocalAgents(state)
for index in range(20):
    agents.send('bot1', 'bot2', sys.argv[2] + ':' + str(index))
"""
    processes = [
        subprocess.Popen(
            [sys.executable, '-c', program, str(state.directory), str(index)],
            env=os.environ | {'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in range(4)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
    items = agents.inbox('bot2', limit=200)['items']
    assert len(items) == 80
    assert {item['message'] for item in items} == {
        f'{worker}:{index}' for worker in range(4) for index in range(20)
    }


def test_symlinked_directory_and_foreign_owner_are_rejected(tmp_path, monkeypatch):
    state, agents = mailbox(tmp_path)
    moved = agents.directory.with_name('moved')
    agents.directory.rename(moved)
    agents.directory.symlink_to(moved)
    with pytest.raises(Failure, match='unsafe_client_directory'):
        LocalAgents(state)
    agents.directory.unlink()
    moved.rename(agents.directory)
    monkeypatch.setattr(os, 'geteuid', lambda: agents.directory.stat().st_uid + 1)
    with pytest.raises(Failure, match='client_directory_not_owned'):
        LocalAgents(state)


def test_local_agents_show_read_and_sender_filter(tmp_path):
    state, agents = mailbox(tmp_path)
    agents.create('bot1')
    agents.create('bot2')
    agents.create('bot3')

    # show
    info = agents.show('bot1')
    assert info['label'] == 'bot1'
    assert info['active'] is True

    with pytest.raises(Failure, match='subagent_not_found'):
        agents.show('ghost')

    # send with custom message_id
    msg_id = 'custom-test-id-123'
    sent = agents.send('bot1', 'bot2', 'hello from bot1', message_id=msg_id)
    assert sent['id'] == msg_id

    agents.send('bot3', 'bot2', 'hello from bot3')

    # read
    read_item = agents.read('bot2', msg_id)
    assert read_item['id'] == msg_id
    assert read_item['message'] == 'hello from bot1'

    with pytest.raises(Failure, match='not_found'):
        agents.read('bot2', 'nonexistent-id')

    # inbox with sender filter
    bot1_inbox = agents.inbox('bot2', sender='bot1')
    assert len(bot1_inbox['items']) == 1
    assert bot1_inbox['items'][0]['id'] == msg_id

    bot3_inbox = agents.inbox('bot2', sender='bot3')
    assert len(bot3_inbox['items']) == 1
    assert bot3_inbox['items'][0]['message'] == 'hello from bot3'
