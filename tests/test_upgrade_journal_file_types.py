"""Unsafe upgrade journals must fail before file I/O can block a client."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from msg.client import ClientState
from msg.client_upgrade import read_intent
from msg.core.codec import b64, canonical
from msg.storage.git import durable_write

# A subprocess bounds a regression in synchronous os.open; an asyncio timeout
# cannot interrupt a FIFO open that blocks the event-loop thread itself.
CHECK_FIFO = """
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
"""


@pytest.mark.parametrize('entrypoint', ('read', 'status', 'upgrade'))
def test_upgrade_fifo_is_rejected_without_blocking_or_changing_identity(tmp_path, entrypoint):
    state = ClientState(tmp_path / 'client', server='https://example.invalid')
    state.data.update(
        subject_id='u_existing',
        token={
            'credential_id': 't_existing',
            'value': b64(bytes(range(32))),
            'expires_at': '2030-01-01T00:00:00Z',
        },
    )
    state._save()
    before = state.path.read_bytes()
    journal = state.directory / 'identity-upgrade.json'
    os.mkfifo(journal, 0o600)
    inode = journal.lstat().st_ino
    env = dict(os.environ)
    source = str(Path(__file__).resolve().parents[1] / 'src')
    env['PYTHONPATH'] = os.pathsep.join(filter(None, (source, env.get('PYTHONPATH'))))
    result = subprocess.run(
        [sys.executable, '-c', CHECK_FIFO, str(state.directory), entrypoint],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'unsafe_upgrade_journal'
    assert state.path.read_bytes() == before
    assert not state.key_path.exists() and not state.age_key_path.exists()
    assert stat.S_ISFIFO(journal.lstat().st_mode) and journal.lstat().st_ino == inode


def test_nonblocking_upgrade_reader_preserves_regular_intent(tmp_path):
    journal = tmp_path / 'identity-upgrade.json'
    pending = {
        'version': 1,
        'server': 'https://example.invalid',
        'subject_id': 'u_existing',
        'handle': 'existing-agent',
        'public_key': 'saved-public-key',
        'encryption_recipient': 'saved-age-recipient',
        'credential_id': 't_existing',
        'request_id': 'a' * 32,
    }
    durable_write(journal, canonical(pending), mode=0o600)
    before = journal.read_bytes()
    assert read_intent(journal) == pending
    assert journal.read_bytes() == before


@pytest.mark.parametrize('kind', ('upgrade', 'token'))
def test_directory_journal_rejection_closes_descriptor(tmp_path, monkeypatch, kind):
    import errno

    from msg.client_tokens import read_journal
    from msg.core.errors import Failure

    state = ClientState(tmp_path / 'client', server='https://example.invalid')
    journal = state.directory / ('identity-upgrade.json' if kind == 'upgrade' else 'temporary.json')
    journal.mkdir(mode=0o700)
    opened = []
    original_open = os.open

    def tracked_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, 'open', tracked_open)
    try:
        with pytest.raises(Failure, match=f'unsafe_{kind}_journal'):
            read_intent(journal) if kind == 'upgrade' else read_journal(state)
        assert len(opened) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
        assert journal.is_dir()
    finally:
        # Clean leaked descriptors when this regression runs against old code.
        for fd in opened:
            try:
                os.close(fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
