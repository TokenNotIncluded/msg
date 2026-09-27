"""Owned token journals fail closed without changing or disclosing local state."""
import os
import subprocess
import sys

import pytest

from msg.client import ClientState
from msg.client_journal import identity_lock
from msg.client_tokens import claim, pending_delivery, read_journal
from msg.core.codec import b64, canonical
from msg.core.errors import Failure
from msg.security.crypto import Ed25519Signer
from msg.storage.git import durable_write


def pending_state(tmp_path):
    state = ClientState(tmp_path/'client', server='http://testserver')
    state.save_signer(Ed25519Signer.generate())
    state.ensure_encryption_key()
    nonce, secret = b64(os.urandom(32)), b64(os.urandom(32))
    request_id = 'a'*32
    subject, credential = claim(nonce, request_id, 'identity.temporary', None)
    pending = {'operation': 'identity.temporary', 'contract_version': 3,
        'server': state.server, 'nonce': nonce, 'recovery_secret': secret,
        'request_id': request_id, 'subject_id': subject, 'credential_id': credential,
        'public_key': b64(state.signer.public_key), 'encryption_recipient': state.encryption_recipient,
        'input': {}, 'generation': 0}
    path = state.directory/'temporary.json'
    durable_write(path, canonical(pending), mode=0o600)
    return state, path, pending


@pytest.mark.parametrize('mutation,code', [
    ({'server': 'https://other.invalid'}, 'token_journal_mismatch'),
    ({'subject_id': 'u_other'}, 'token_journal_mismatch'),
    ({'credential_id': 't_other'}, 'token_journal_mismatch'),
    ({'nonce': 'eA'}, 'invalid_token_journal'),
    ({'recovery_secret': 'eA'}, 'invalid_token_journal'),
    ({'request_id': '../other'}, 'invalid_token_journal'),
    ({'request_id': []}, 'invalid_token_journal'),
    ({'generation': -1}, 'invalid_token_journal'),
    ({'generation': True}, 'invalid_token_journal'),
    ({'operation': 'identity.token_rotate'}, 'invalid_token_journal'),
    ({'contract_version': 2}, 'legacy_token_journal_requires_manual_resolution'),
])
def test_corrupt_or_foreign_journal_cannot_silently_start_new_identity(tmp_path, mutation, code):
    state, path, pending = pending_state(tmp_path)
    pending.update(mutation)
    durable_write(path, canonical(pending), mode=0o600)
    before = path.read_bytes(), state.path.read_bytes(), state.key_path.read_bytes()
    with pytest.raises(Failure, match=code):
        read_journal(state)
    assert (path.read_bytes(), state.path.read_bytes(), state.key_path.read_bytes()) == before


@pytest.mark.parametrize('unsafe', ('symlink', 'hardlink', 'public', 'fifo'))
def test_unsafe_journal_is_rejected_without_following_it(tmp_path, unsafe):
    state, path, _ = pending_state(tmp_path)
    original = path.read_bytes()
    target = tmp_path/'untouched'
    target.write_bytes(original)
    if unsafe == 'public':
        path.chmod(0o644)
    else:
        path.unlink()
        if unsafe == 'symlink':
            path.symlink_to(target)
        elif unsafe == 'hardlink':
            target.chmod(0o600)
            os.link(target, path)
        else:
            os.mkfifo(path, mode=0o600)
    with pytest.raises(Failure, match='unsafe_token_journal'):
        read_journal(state)
    assert target.read_bytes() == original


@pytest.mark.parametrize('raw', (b'{', b'[]', b'null', b' '*65537))
def test_bounded_journal_reader_never_echoes_invalid_data(tmp_path, raw):
    state, path, _ = pending_state(tmp_path)
    durable_write(path, raw, mode=0o600)
    with pytest.raises(Failure, match='invalid_token_journal') as error:
        read_journal(state)
    assert error.value.details is None and path.read_bytes() == raw


def test_pending_summary_has_no_nonce_or_recovery_secret(tmp_path):
    state, path, pending = pending_state(tmp_path)
    summary = pending_delivery(state)
    assert set(summary) == {'operation', 'request_id', 'status', 'resume'}
    assert pending['nonce'] not in canonical(summary).decode()
    assert pending['recovery_secret'] not in canonical(summary).decode()
    assert path.is_file()


def test_missing_key_cannot_regenerate_pending_temporary_identity(tmp_path):
    state, path, _ = pending_state(tmp_path)
    before = path.read_bytes()
    state.key_path.unlink()
    restarted = ClientState(state.directory)
    with pytest.raises(Failure, match='token_journal_key_missing'):
        read_journal(restarted)
    assert path.read_bytes() == before and not state.key_path.exists()


def test_process_lock_coordinates_upgrade_and_token_and_releases_on_exit(tmp_path):
    directory = tmp_path/'client'
    directory.mkdir(mode=0o700)
    script = '''
import sys
from pathlib import Path
from msg.client_journal import identity_lock
from msg.core.errors import Failure
try:
    with identity_lock(Path(sys.argv[1]), busy='token_operation_busy'):
        print('acquired')
except Failure as exc:
    print(exc.code)
'''
    with identity_lock(directory):
        locked = subprocess.run([sys.executable, '-c', script, str(directory)],
                                capture_output=True, text=True, timeout=15, check=True)
        assert locked.stdout.strip() == 'token_operation_busy'
    released = subprocess.run([sys.executable, '-c', script, str(directory)],
                              capture_output=True, text=True, timeout=15, check=True)
    assert released.stdout.strip() == 'acquired'
    with identity_lock(directory):
        assert (directory/'identity-upgrade.lock').stat().st_mode & 0o777 == 0o600


def test_client_state_constructor_does_not_overwrite_accepted_state(tmp_path, monkeypatch):
    state, _, _ = pending_state(tmp_path)
    state.data['subject_id'] = 'u_accepted'
    state._save()
    before = state.path.read_bytes()
    def unexpected(*args, **kwargs):
        raise AssertionError('reading existing client state must not write it')
    monkeypatch.setattr('msg.client.durable_write', unexpected)
    assert ClientState(state.directory).subject == 'u_accepted'
    assert state.path.read_bytes() == before


@pytest.mark.parametrize('advanced', (False, True))
def test_previous_client_journal_can_resume_without_losing_bound_material(tmp_path, advanced):
    state, path, pending = pending_state(tmp_path)
    for field in ('server', 'generation', 'public_key', 'encryption_recipient', 'input'):
        pending.pop(field)
    pending['expires_at'] = '2026-09-27T00:03:00Z'
    if advanced:
        pending['request_id'] = 'b'*32
        pending['credential_id'] = claim(pending['nonce'], pending['request_id'],
                                        'identity.token_recover', pending['subject_id'])[1]
        pending['recovery_secret'] = b64(os.urandom(32))
    durable_write(path, canonical(pending), mode=0o600)
    before = path.read_bytes()
    found, loaded = read_journal(state)
    assert found == path and path.read_bytes() == before
    assert loaded['request_id'] == pending['request_id']
    assert loaded['recovery_secret'] == pending['recovery_secret']
    assert loaded.get('generation', 0) == int(advanced)
