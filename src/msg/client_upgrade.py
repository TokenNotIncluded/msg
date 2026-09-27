"""Durable same-subject upgrade intent; status recovery never reuses old authority."""
from contextlib import contextmanager
import os
import re
import stat
from uuid import uuid4

from msg.core.codec import b64, canonical, loads, wire
from msg.core.errors import Failure, require
from msg.security.crypto import Ed25519Signer
from msg.storage.git import durable_write


@contextmanager
def upgrade_lock(directory):
    """A crash releases the OS lock; a second caller fails instead of blocking an event loop."""
    try:
        import fcntl
    except ImportError:
        raise Failure('upgrade_lock_unavailable') from None
    try:
        fd = os.open(directory/'identity-upgrade.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise Failure('unsafe_upgrade_lock') from None
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                info.st_nlink == 1 and info.st_mode & 0o077 == 0, 'unsafe_upgrade_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('identity_upgrade_busy', retryable=True) from None
        yield
    finally:
        os.close(fd)



@contextmanager
def locked_state(state):
    """Refresh accepted credentials under the same lock used by every transition."""
    with upgrade_lock(state.directory):
        fresh = type(state)(state.directory, server=state.server)
        state.__dict__.update(fresh.__dict__)
        yield

def read_intent(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Failure('unsafe_upgrade_journal') from None
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                info.st_nlink == 1 and info.st_mode & 0o077 == 0, 'unsafe_upgrade_journal')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(8193)
    finally:
        os.close(fd)
    require(len(raw) <= 8192, 'invalid_upgrade_journal')
    try:
        value = loads(raw)
        require(isinstance(value, dict) and set(value) == {
            'version', 'server', 'subject_id', 'handle', 'public_key',
            'encryption_recipient', 'credential_id', 'request_id'}, 'invalid_upgrade_journal')
        require(value['version'] == 1 and all(isinstance(value[name], str) and value[name]
            for name in value if name != 'version'), 'invalid_upgrade_journal')
        require(re.fullmatch('[0-9a-f]{32}', value['request_id']) is not None,
                'invalid_upgrade_journal')
    except (ValueError, TypeError, KeyError):
        raise Failure('invalid_upgrade_journal') from None
    return value


def confirm_local_identity(state, pending, result, journal):
    require(result.status == 'ok' and result.subject == pending['subject_id'] and
            result.data.get('subject_id') == pending['subject_id'] and
            result.data.get('key_id') == state.signer.key_id and
            result.data.get('handle') == pending['handle'] and
            result.data.get('encryption_recipient') == pending['encryption_recipient'] and
            isinstance(result.data.get('certificate_id'), str) and bool(result.data['certificate_id']),
            'upgrade_result_mismatch')
    if result.operation == 'identity.upgrade_result':
        require(result.data.get('status') == 'completed' and
                result.data.get('upgrade_request_id') == pending['request_id'],
                'upgrade_result_mismatch')
    else:
        require(result.operation == 'identity.upgrade' and result.request_id == pending['request_id'],
                'upgrade_result_mismatch')
    # Do not remove the recovery journal until the local identity is durable.
    # A crash after this write but before unlink is recovered with the new key.
    state.accept_identity(result)
    journal.unlink()
    fd = os.open(state.directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return result


def pending_upgrade(state):
    path = state.directory/'identity-upgrade.json'
    if not (path.exists() or path.is_symlink()):
        return None
    pending = read_intent(path)
    require(pending['server'] == state.server and pending['subject_id'] == state.subject and
            re.fullmatch(r'[a-z][a-z0-9-]{1,40}', pending['handle']), 'upgrade_journal_mismatch')
    return {'status': 'pending', 'handle': pending['handle'], 'request_id': pending['request_id'],
            'resume': 'msg identity upgrade'}


async def upgrade_identity(client, handle=None):
    state = client.state
    client._require_token_secret_transport()
    with locked_state(state):
        journal = state.directory/'identity-upgrade.json'
        resuming = journal.exists() or journal.is_symlink()
        pending = read_intent(journal) if resuming else None
        if handle is None:
            require(pending is not None, 'upgrade_handle_required')
            handle = pending['handle']
        require(isinstance(handle, str) and re.fullmatch(r'[a-z][a-z0-9-]{1,40}', handle)
                and handle not in {'root', 'online-ca'}, 'invalid_handle')
        if resuming:
            require(state.signer is not None and state.encryption_recipient is not None,
                    'upgrade_journal_key_missing')
            require((pending['server'], pending['subject_id'], pending['handle'],
                     pending['public_key'], pending['encryption_recipient']) ==
                    (state.server, state.subject, handle, b64(state.signer.public_key),
                     state.encryption_recipient), 'upgrade_journal_mismatch')
            require(state.token is None or state.token[0] == pending['credential_id'],
                    'upgrade_journal_mismatch')
        else:
            require(state.subject is not None and state.token is not None, 'temporary_identity_required')
            require(not any((state.directory/name).exists() or (state.directory/name).is_symlink() for name in (
                'temporary.json', 'custodial-bootstrap.json', 'token-rotation.json',
                'custodial-upgrade.json')), 'identity_recovery_pending')
            if state.signer is None:
                state.save_signer(Ed25519Signer.generate())
            recipient = state.ensure_encryption_key()
            pending = {'version': 1, 'server': state.server, 'subject_id': state.subject,
                'handle': handle, 'public_key': b64(state.signer.public_key),
                'encryption_recipient': recipient, 'credential_id': state.token[0],
                'request_id': uuid4().hex}
            # Only a credential ID is stored here, never the old token or a private key.
            durable_write(journal, canonical(pending), mode=0o600)

        async def recover():
            return await client.call('identity.upgrade_result',
                {'upgrade_request_id': pending['request_id']}, signer=state.signer,
                subject=pending['subject_id'], certificates=())

        if resuming:
            recovered = await recover()
            if recovered.status == 'ok':
                return confirm_local_identity(state, pending, recovered, journal)
            # A not-yet-bound new key can occur after a pre-send crash. Only the
            # still-valid original credential may submit the fixed intention.
            if state.token is None or recovered.error.code not in {
                    'credential_not_found', 'not_found', 'invalid_signature',
                    'credential_ceiling', 'self_custody_signature_required',
                    'upgrade_not_completed'}:
                return recovered

        require(state.token is not None, 'temporary_identity_required')
        signed = {name: pending[name] for name in ('subject_id', 'handle', 'public_key',
                                                   'encryption_recipient')}
        arguments = {name: pending[name] for name in ('handle', 'public_key', 'encryption_recipient')}
        arguments['possession_proof'] = wire(state.signer.sign(canonical(signed), purpose='upgrade'))
        try:
            result = await client.call('identity.upgrade', arguments,
                                       request_id=pending['request_id'], contract_version=2)
        except Failure as exc:
            if exc.code != 'transport_uncertain':
                raise
            recovered = await recover()
            if recovered.status != 'ok':
                raise exc
            result = recovered
        if result.status == 'error' and result.error.code in {
                'credential_revoked', 'credential_expired', 'not_temporary'}:
            recovered = await recover()
            if recovered.status == 'ok':
                result = recovered
        if result.status == 'ok':
            return confirm_local_identity(state, pending, result, journal)
        return result
