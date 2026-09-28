"""Descriptor-checked private Root files and a nonblocking local rotation lock.

These helpers confer no Root authority. RootAdmin must still enforce the real
OS-console/PIN ceremony; isolated tests exercise only the internal file use case.
"""
import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from msg.core.errors import Failure, require


def _private_descriptor(fd):
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
            stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
            'unsafe_root_private_file')
    return info


def read_private(path, *, limit=4 * 1024 * 1024):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise Failure('unsafe_root_private_file') from exc
    try:
        before = _private_descriptor(fd)
        require(0 < before.st_size <= limit, 'unsafe_root_private_file')
        chunks, count = [], 0
        while chunk := os.read(fd, min(65536, limit + 1 - count)):
            count += len(chunk)
            require(count <= limit, 'unsafe_root_private_file')
            chunks.append(chunk)
        after = os.fstat(fd)
        require(before.st_size == count == after.st_size and
                before.st_mtime_ns == after.st_mtime_ns and before.st_ctime_ns == after.st_ctime_ns,
                'root_private_file_changed')
        return b''.join(chunks)
    finally:
        os.close(fd)


@contextmanager
def rotation_lock(protected):
    protected = Path(protected)
    require(not protected.is_symlink(), 'unsafe_root_private_path')
    protected.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = protected.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
            stat.S_IMODE(info.st_mode) == 0o700, 'unsafe_root_private_path')
    try:
        fd = os.open(protected / '.rotation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW |
                     os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise Failure('unsafe_root_private_file') from exc
    try:
        _private_descriptor(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Failure('root_rotation_busy', retryable=True) from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
