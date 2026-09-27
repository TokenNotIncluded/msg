"""Small owned-file primitives shared by credential and identity journals."""
import os
import stat
from contextlib import contextmanager

from msg.core.codec import loads
from msg.core.errors import Failure, require


@contextmanager
def identity_lock(directory, *, busy='identity_upgrade_busy', unsafe='unsafe_upgrade_lock'):
    """One nonblocking OS lock, released on process death, for identity transitions."""
    try:
        import fcntl
    except ImportError:
        raise Failure('identity_lock_unavailable') from None
    try:
        fd = os.open(directory/'identity-upgrade.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise Failure(unsafe) from None
    try:
        _owned_regular(os.fstat(fd), unsafe)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure(busy, retryable=True) from None
        yield
    finally:
        os.close(fd)


def _owned_regular(info, code):
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
            info.st_nlink == 1 and info.st_mode & 0o077 == 0, code)


def read_owned_json(path, *, maximum, unsafe, invalid):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Failure(unsafe) from None
    with os.fdopen(fd, 'rb') as stream:
        _owned_regular(os.fstat(stream.fileno()), unsafe)
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, invalid)
    try:
        value = loads(raw)
        require(isinstance(value, dict), invalid)
    except (ValueError, TypeError):
        raise Failure(invalid) from None
    return value


def remove_journal(path):
    path.unlink()
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
