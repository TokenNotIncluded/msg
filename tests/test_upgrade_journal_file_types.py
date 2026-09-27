"""Unsafe upgrade journals must fail before file I/O can block a client."""
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from msg.client import ClientState
from msg.client_upgrade import read_intent
from msg.core.codec import b64, canonical
from msg.storage.git import durable_write


# A subprocess bounds a regression in synchronous os.open; an asyncio timeout
# cannot interrupt a FIFO open that blocks the event-loop thread itself.
CHECK_FIFO = '''
import asyncio
from pathlib import Path
import sys
import httpx
from msg.client import ClientState, MsgClient
from msg.client_upgrade import pending_upgrade, read_intent
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport

state = ClientState(Path(sys.argv[1]))

async def upgrade():
    def reject_network(request):
        raise AssertionError('unsafe journal attempted network I/O')
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject_network)) as http:
        transport = HTTPTransport(state.server, http=http)
        try:
            await MsgClient(state, transport, retries=0).upgrade('fifo-agent')
        finally:
            assert transport.calls == 0

try:
    if sys.argv[2] == 'read':
        read_intent(state.directory / 'identity-upgrade.json')
    elif sys.argv[2] == 'status':
        pending_upgrade(state)
    else:
        asyncio.run(upgrade())
except Failure as error:
    assert error.code == 'unsafe_upgrade_journal', error.code
    print(error.code)
else:
    raise AssertionError('unsafe journal was accepted')
'''


@pytest.mark.parametrize('entrypoint', ('read', 'status', 'upgrade'))
def test_upgrade_fifo_is_rejected_without_blocking_or_changing_identity(tmp_path, entrypoint):
    state = ClientState(tmp_path / 'client', server='https://example.invalid')
    state.data.update(subject_id='u_existing', token={
        'credential_id': 't_existing', 'value': b64(bytes(range(32))),
        'expires_at': '2030-01-01T00:00:00Z'})
    state._save()
    before = state.path.read_bytes()
    journal = state.directory / 'identity-upgrade.json'
    os.mkfifo(journal, 0o600)
    inode = journal.lstat().st_ino
    env = dict(os.environ)
    source = str(Path(__file__).resolve().parents[1] / 'src')
    env['PYTHONPATH'] = os.pathsep.join(filter(None, (source, env.get('PYTHONPATH'))))
    result = subprocess.run([sys.executable, '-c', CHECK_FIFO,
                             str(state.directory), entrypoint],
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'unsafe_upgrade_journal'
    assert state.path.read_bytes() == before
    assert not state.key_path.exists() and not state.age_key_path.exists()
    assert stat.S_ISFIFO(journal.lstat().st_mode) and journal.lstat().st_ino == inode


def test_nonblocking_upgrade_reader_preserves_regular_intent(tmp_path):
    journal = tmp_path / 'identity-upgrade.json'
    pending = {'version': 1, 'server': 'https://example.invalid',
               'subject_id': 'u_existing', 'handle': 'existing-agent',
               'public_key': 'saved-public-key', 'encryption_recipient': 'saved-age-recipient',
               'credential_id': 't_existing', 'request_id': 'a' * 32}
    durable_write(journal, canonical(pending), mode=0o600)
    before = journal.read_bytes()
    assert read_intent(journal) == pending
    assert journal.read_bytes() == before
