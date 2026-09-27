"""Protected one-use credential journals; no network authority comes from an ID."""
from functools import wraps
import os
import stat

from msg.client_upgrade import locked_state
from msg.core.codec import b64, loads, unb64
from msg.core.errors import Failure, require

JOURNAL_OPERATIONS = {
    'temporary.json': ('identity.temporary', 3),
    'custodial-bootstrap.json': ('identity.custodial_create', 2),
    'token-rotation.json': ('identity.token_rotate', 2),
}


def remove_journal(path):
    """The local credential must already be durable before removing its intent."""
    path.unlink()
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def token_operation(method):
    """Serialize with identity upgrades too, without blocking the event loop."""
    @wraps(method)
    async def locked(self, *args, **kwargs):
        self._require_token_secret_transport()
        with locked_state(self.state):
            require(not any((self.state.directory/name).exists() or
                            (self.state.directory/name).is_symlink() for name in
                            ('identity-upgrade.json', 'custodial-upgrade.json')),
                    'identity_recovery_pending')
            self._token_journal()
            return await method(self, *args, **kwargs)
    return locked


def read_journal(state):
    paths = [state.directory/name for name in JOURNAL_OPERATIONS]
    present = [p for p in paths if p.exists() or p.is_symlink()]
    require(len(present) <= 1, 'multiple_token_journals')
    if not present:
        return None, None
    path = present[0]
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Failure('unsafe_token_journal') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                info.st_nlink == 1 and info.st_mode & 0o077 == 0, 'unsafe_token_journal')
        raw = stream.read(8193)
    require(len(raw) <= 8192, 'invalid_token_journal')
    try:
        saved = loads(raw)
        require(isinstance(saved, dict), 'invalid_token_journal')
        operation, version = JOURNAL_OPERATIONS[path.name]
        require(saved.get('operation') == operation and saved.get('contract_version') == version,
                'legacy_token_journal_requires_manual_resolution')
        # Older journals stay recoverable using their pre-bound secret, but cannot
        # silently generate replacement keys. Newly written intentions also bind
        # the origin and local key pair, including before any network call.
        if 'server' in saved:
            require(saved['server'] == state.server, 'token_journal_server_mismatch')
        for field in ('nonce', 'recovery_secret'):
            require(len(unb64(saved[field], limit=64)) >= 32, 'invalid_token_journal')
        for field in ('credential_id', 'subject_id', 'request_id'):
            require(isinstance(saved[field], str) and 0 < len(saved[field]) <= 160,
                    'invalid_token_journal')
        recovery = saved.get('recovery')
        if recovery is not None:
            require(isinstance(recovery, dict), 'invalid_token_journal')
            for field in ('nonce', 'new_recovery_secret'):
                require(len(unb64(recovery[field], limit=64)) >= 32, 'invalid_token_journal')
            for field in ('request_id', 'credential_id'):
                require(isinstance(recovery[field], str) and 0 < len(recovery[field]) <= 160,
                        'invalid_token_journal')
        require(state.subject is None or saved['subject_id'] == state.subject,
                'token_journal_subject_mismatch')
        if operation == 'identity.temporary':
            require(state.signer is not None and state.encryption_recipient is not None,
                    'token_journal_key_missing')
            if 'public_key' in saved:
                require(saved['public_key'] == b64(state.signer.public_key) and
                        saved.get('encryption_recipient') == state.encryption_recipient,
                        'token_journal_key_mismatch')
    except Failure:
        raise
    except (KeyError, ValueError, TypeError):
        raise Failure('invalid_token_journal') from None
    return path, saved
