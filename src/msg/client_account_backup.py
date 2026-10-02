"""Offline, age-encrypted named-account snapshots. Plaintext never enters a temp file.

This restores local credentials, not server permission or an exported hardware key.
All paths are traversed through no-follow descriptors; archives are never extracted.
"""

from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from msg import __version__
from msg.core.codec import b64, canonical, digest, loads, parse_time, timestamp, unb64
from msg.core.errors import Failure, require
from msg.paths import ClientPaths
from msg.security.age_keys import recipient_from_identity
from msg.security.crypto import Ed25519Signer
from msg.service_origin import service_origin

MAX_BYTES = 16 * 1024 * 1024
MAX_ENVELOPE = 24 * 1024 * 1024
MAX_ENTRIES = 2048
MAX_DEPTH = 12
SCOPES = ('config', 'data', 'state')
AGE_TIMEOUT = 180
MAX_AGE_STDERR = 65536


def _age_terminal(process_id):
    """Let a separate age process group use our foreground terminal when present."""
    if threading.current_thread() is not threading.main_thread():
        return None
    try:
        descriptor = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return None
    previous_signal = signal.getsignal(signal.SIGTTOU)
    try:
        previous_group = os.tcgetpgrp(descriptor)
        if previous_group != os.getpgrp():
            os.close(descriptor)
            return None
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        os.tcsetpgrp(descriptor, process_id)
        os.killpg(process_id, signal.SIGCONT)
        return descriptor, previous_group, previous_signal, process_id
    except OSError:
        try:
            if os.tcgetpgrp(descriptor) == process_id:
                os.tcsetpgrp(descriptor, previous_group)
        except OSError:
            pass
        signal.signal(signal.SIGTTOU, previous_signal)
        os.close(descriptor)
        return None


def _restore_age_terminal(terminal):
    if terminal is None:
        return
    descriptor, previous_group, previous_signal, process_id = terminal
    try:
        if os.tcgetpgrp(descriptor) == process_id:
            os.tcsetpgrp(descriptor, previous_group)
    except OSError:
        pass
    finally:
        signal.signal(signal.SIGTTOU, previous_signal)
        os.close(descriptor)


def _path(value):
    return Path(value).expanduser().absolute()


def _directory(path, *, create=False):
    """Open every component without following symlinks, including ancestors."""
    path = _path(path)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            require(part not in {'.', '..'}, 'unsafe_account_backup_path')
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise Failure('unsafe_account_backup_path') from None
    except BaseException:
        os.close(descriptor)
        raise


def _private(info, *, directory=False):
    require(
        (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        and info.st_uid == os.geteuid()
        and info.st_mode & 0o077 == 0
        and (directory or info.st_nlink == 1),
        'unsafe_account_backup_permissions',
    )


def _stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_file(parent, name, *, private, limit=MAX_ENVELOPE):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except OSError:
        raise Failure('unsafe_account_backup_path') from None
    try:
        before = os.fstat(descriptor)
        if private:
            _private(before)
        else:
            require(
                stat.S_ISREG(before.st_mode)
                and before.st_uid == os.geteuid()
                and before.st_nlink == 1,
                'unsafe_account_backup_path',
            )
        require(before.st_size <= limit, 'account_backup_too_large')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        require(len(raw) <= limit, 'account_backup_too_large')
        require(_stamp(before) == _stamp(os.fstat(descriptor)), 'account_backup_changed')
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        require(_stamp(current) == _stamp(before), 'account_backup_changed')
        return raw, _stamp(before)
    finally:
        os.close(descriptor)


def _read_path(path, *, private):
    path = _path(path)
    parent = _directory(path.parent)
    try:
        return _read_file(parent, path.name, private=private)[0]
    finally:
        os.close(parent)


def _snapshot(paths):
    directories, files, stamps = [], {}, {}
    total = 0

    def scan(descriptor, prefix):
        nonlocal total
        info = os.fstat(descriptor)
        _private(info, directory=True)
        before = _stamp(info)
        directories.append(prefix)
        stamps[prefix] = before
        require(len(directories) + len(files) <= MAX_ENTRIES, 'account_backup_too_many_files')
        for name in sorted(os.listdir(descriptor)):
            require('/' not in name and '\\' not in name, 'unsafe_account_backup_path')
            relative = prefix + '/' + name
            require(len(relative.split('/')) <= MAX_DEPTH, 'account_backup_path_too_deep')
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if name == 'cache' and stat.S_ISDIR(info.st_mode):
                continue
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                try:
                    scan(child, relative)
                finally:
                    os.close(child)
            else:
                raw, stamp = _read_file(descriptor, name, private=True, limit=MAX_BYTES - total)
                total += len(raw)
                files[relative] = raw
                stamps[relative] = stamp
                require(
                    len(directories) + len(files) <= MAX_ENTRIES, 'account_backup_too_many_files'
                )
        require(_stamp(os.fstat(descriptor)) == before, 'account_backup_changed')

    for scope in SCOPES:
        path = getattr(paths, scope)
        if not path.exists() and not path.is_symlink():
            directories.append(scope)
            stamps[scope] = None
            continue
        descriptor = _directory(path)
        try:
            scan(descriptor, scope)
            require(_stamp(path.lstat()) == stamps[scope], 'account_backup_changed')
        finally:
            os.close(descriptor)
    return directories, files, stamps


def _identity(files, server):
    require('state/client.json' in files, 'local_account_not_found')
    state = loads(files['state/client.json'])
    require(
        isinstance(state, dict) and type(state.get('version')) is int and state['version'] == 1,
        'invalid_account_backup_state',
    )
    require(service_origin(state.get('server')) == server, 'client_server_mismatch')
    subject = state.get('subject_id')
    require(
        isinstance(subject, str) and re.fullmatch(r'u_[a-f0-9]{32}', subject) is not None,
        'local_account_not_authenticated',
    )
    software = files.get('data/identity.key')
    hardware = files.get('state/hardware-signer.json')
    require(not (software is not None and hardware is not None), 'client_key_backend_conflict')
    require(software is not None or hardware is not None, 'signing_identity_required')
    if software is not None:
        signer = Ed25519Signer.from_bytes(software)
        backend = 'software'
    else:
        from msg.client_yubikey import YubiKeySigner

        signer = YubiKeySigner.from_descriptor(loads(hardware))
        backend = 'yubikey-piv'
    age_identity = files.get('data/encryption.agekey')
    recipient = (
        recipient_from_identity(age_identity.decode().strip()) if age_identity is not None else None
    )
    return subject, signer.key_id, backend, recipient


def _age(arguments, data, *, pass_fds=()):
    executable = shutil.which('age')
    require(executable is not None, 'age_dependency_unavailable')
    process, terminal = None, None
    output, stderr_bytes, written = bytearray(), 0, 0
    deadline = time.monotonic() + AGE_TIMEOUT
    try:
        # A distinct group permits bounded cleanup of plugins too, without killing msg.
        process = subprocess.Popen(
            [executable, *arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
            process_group=0,
        )
        terminal = _age_terminal(process.pid)
        with selectors.DefaultSelector() as poller:
            for stream, kind in (
                (process.stdin, 'input'),
                (process.stdout, 'output'),
                (process.stderr, 'error'),
            ):
                os.set_blocking(stream.fileno(), False)
                poller.register(
                    stream, selectors.EVENT_WRITE if kind == 'input' else selectors.EVENT_READ, kind
                )
            while poller.get_map():
                remaining = deadline - time.monotonic()
                require(remaining > 0, 'age_operation_failed')
                for key, _ in poller.select(min(remaining, 1)):
                    stream, kind = key.fileobj, key.data
                    if kind == 'input':
                        if written == len(data):
                            poller.unregister(stream)
                            stream.close()
                            continue
                        try:
                            written += os.write(
                                stream.fileno(), memoryview(data)[written : written + 65536]
                            )
                        except BrokenPipeError:
                            poller.unregister(stream)
                            stream.close()
                        except BlockingIOError:
                            continue
                    else:
                        chunk = os.read(stream.fileno(), 65536)
                        if not chunk:
                            poller.unregister(stream)
                            stream.close()
                        elif kind == 'output':
                            require(
                                len(output) + len(chunk) <= MAX_ENVELOPE, 'account_backup_too_large'
                            )
                            output.extend(chunk)
                        else:
                            # Count and discard stderr; never return plugin/runtime secret text.
                            stderr_bytes += len(chunk)
                            require(stderr_bytes <= MAX_AGE_STDERR, 'age_output_limit_exceeded')
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'age_operation_failed')
            require(process.wait(timeout=remaining) == 0, 'age_operation_failed')
        return bytes(output)
    except OSError, subprocess.TimeoutExpired:
        raise Failure('age_operation_failed') from None
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise Failure('age_cleanup_failed') from None
            finally:
                _restore_age_terminal(terminal)


def _write_new(parent, name, raw):
    try:
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
    except FileExistsError:
        raise Failure('account_backup_destination_exists') from None
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(descriptor)
        os.fsync(parent)
    except BaseException:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (info.st_dev, info.st_ino) == (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino):
            os.unlink(name, dir_fd=parent)
        raise
    finally:
        os.close(descriptor)


def _paths(server, account):
    paths = ClientPaths.discover(server=server, account=account)
    require(
        len({getattr(paths, scope) for scope in SCOPES}) == 3,
        'account_backup_paths_conflict',
    )
    require(
        not any(
            getattr(paths, a).is_relative_to(getattr(paths, b))
            for a in SCOPES
            for b in SCOPES
            if a != b
        ),
        'account_backup_paths_conflict',
    )
    return paths


def _receipt(server, account, subject, key_id, backend, ciphertext):
    return {
        'format': 'msg-account-backup',
        'version': 1,
        'server': server,
        'account': account,
        'subject_id': subject,
        'key_id': key_id,
        'signer_backend': backend,
        'ciphertext_sha256': digest(ciphertext),
        'client_version': __version__,
        'server_credentials_verified': False,
    }


def ciphertext_digest_pin(value):
    if value is None:
        return None
    require(
        isinstance(value, str) and re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}', value),
        'invalid_backup_hash',
    )
    return 'sha256:' + value.removeprefix('sha256:')


def backup_account(server, account, recipients, output):
    server = service_origin(server)
    paths = _paths(server, account)
    recipients = tuple(recipients)
    require(
        1 <= len(recipients) <= 8
        and len(set(recipients)) == len(recipients)
        and all(
            isinstance(r, str) and 1 <= len(r) <= 4096 and not any(ord(c) < 32 for c in r)
            for r in recipients
        ),
        'invalid_recovery_recipients',
    )
    output = _path(output)
    require(
        not any(output.is_relative_to(getattr(paths, scope)) for scope in (*SCOPES, 'cache')),
        'account_backup_output_inside_account',
    )
    parent = _directory(output.parent)
    try:
        require(not os.path.lexists(output), 'account_backup_destination_exists')
        directories, files, stamps = _snapshot(paths)
        subject, key_id, backend, own_recipient = _identity(files, server)
        require(
            own_recipient is None or any(r != own_recipient for r in recipients),
            'independent_recovery_recipient_required',
        )
        payload = {
            'format': 'msg-account-backup',
            'version': 1,
            'created_at': timestamp(datetime.now(UTC)),
            'client_version': __version__,
            'server': server,
            'original_account': account,
            'subject_id': subject,
            'key_id': key_id,
            'signer_backend': backend,
            'directories': directories,
            'files': [
                {'path': p, 'data': b64(raw), 'sha256': digest(raw)} for p, raw in files.items()
            ],
        }
        plaintext = canonical(payload)
        require(len(plaintext) <= MAX_ENVELOPE - 65536, 'account_backup_too_large')
        require(_snapshot(paths) == (directories, files, stamps), 'account_backup_changed')
        arguments = ['--encrypt']
        for recipient in recipients:
            arguments.extend(('--recipient', recipient))
        ciphertext = _age(arguments, plaintext)
        require(
            ciphertext.startswith(b'age-encryption.org/v1\n'), 'invalid_account_backup_ciphertext'
        )
        require(_snapshot(paths) == (directories, files, stamps), 'account_backup_changed')
        _write_new(parent, output.name, ciphertext)
    finally:
        os.close(parent)
    return _receipt(server, account, subject, key_id, backend, ciphertext) | {
        'output': str(output),
        'encrypted_locally': True,
        'files': len(files),
    }


def _relative(value):
    require(
        isinstance(value, str)
        and 1 <= len(value) <= 512
        and '\\' not in value
        and '\x00' not in value
        and not value.startswith('/')
        and all(part not in {'', '.', '..', 'cache'} for part in value.split('/')),
        'unsafe_account_backup_path',
    )
    parts = PurePosixPath(value).parts
    require(parts[0] in SCOPES and len(parts) <= MAX_DEPTH, 'unsafe_account_backup_path')
    return parts


def _validate(plaintext, server, expected_subject, expected_key_id):
    payload = loads(plaintext)
    require(
        isinstance(payload, dict)
        and set(payload)
        == {
            'format',
            'version',
            'created_at',
            'client_version',
            'server',
            'original_account',
            'subject_id',
            'key_id',
            'signer_backend',
            'directories',
            'files',
        }
        and payload['format'] == 'msg-account-backup'
        and type(payload['version']) is int
        and payload['version'] == 1,
        'invalid_account_backup',
    )
    require(service_origin(payload['server']) == server, 'account_backup_server_mismatch')
    ClientPaths.discover(server=server, account=payload['original_account'])
    parse_time(payload['created_at'])
    require(isinstance(payload['client_version'], str), 'invalid_account_backup')
    require(payload['subject_id'] == expected_subject, 'account_backup_subject_mismatch')
    require(
        expected_key_id is None or payload['key_id'] == expected_key_id,
        'account_backup_key_mismatch',
    )
    require(
        isinstance(payload['directories'], list)
        and isinstance(payload['files'], list)
        and len(payload['directories']) + len(payload['files']) <= MAX_ENTRIES,
        'invalid_account_backup',
    )
    directories = set()
    for name in payload['directories']:
        _relative(name)
        require(name not in directories, 'invalid_account_backup')
        directories.add(name)
    require(set(SCOPES) <= directories, 'invalid_account_backup')
    for name in directories:
        parent = str(PurePosixPath(name).parent)
        require(parent == '.' or parent in directories, 'invalid_account_backup')
    files, total = {}, 0
    for item in payload['files']:
        require(
            isinstance(item, dict) and set(item) == {'path', 'data', 'sha256'},
            'invalid_account_backup',
        )
        name = item['path']
        parts = _relative(name)
        require(
            len(parts) > 1
            and name not in files
            and name not in directories
            and str(PurePosixPath(name).parent) in directories,
            'invalid_account_backup',
        )
        raw = unb64(item['data'], limit=MAX_BYTES - total)
        total += len(raw)
        require(digest(raw) == item['sha256'], 'invalid_account_backup')
        files[name] = raw
    subject, key_id, backend, _ = _identity(files, server)
    require(
        (subject, key_id, backend)
        == (payload['subject_id'], payload['key_id'], payload['signer_backend']),
        'account_backup_identity_mismatch',
    )
    return directories, files, subject, key_id, backend, payload['original_account']


def _remove_contents(descriptor):
    for name in os.listdir(descriptor):
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                _remove_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _install(paths, directories, files):
    roots, parents, created = {}, {}, {}
    try:
        for scope in SCOPES:
            path = getattr(paths, scope)
            parent = _directory(path.parent, create=True)
            parents[scope] = parent
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent)
            except FileExistsError:
                raise Failure('account_restore_destination_exists') from None
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            require(stat.S_ISDIR(info.st_mode), 'unsafe_account_backup_path')
            created[scope] = (info.st_dev, info.st_ino)
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            roots[scope] = descriptor
            opened = os.fstat(descriptor)
            require((opened.st_dev, opened.st_ino) == created[scope], 'unsafe_account_backup_path')
        for name in sorted(directories, key=lambda n: (len(n.split('/')), n)):
            parts = _relative(name)
            if len(parts) == 1:
                continue
            descriptor = os.dup(roots[parts[0]])
            try:
                for part in parts[1:-1]:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    os.close(descriptor)
                    descriptor = child
                os.mkdir(parts[-1], 0o700, dir_fd=descriptor)
            finally:
                os.close(descriptor)
        for name, raw in files.items():
            parts = _relative(name)
            descriptor = os.dup(roots[parts[0]])
            try:
                for part in parts[1:-1]:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    os.close(descriptor)
                    descriptor = child
                _write_new(descriptor, parts[-1], raw)
            finally:
                os.close(descriptor)
        for descriptor in roots.values():
            os.fsync(descriptor)
    except BaseException:
        for scope, identity in created.items():
            parent = parents[scope]
            name = getattr(paths, scope).name
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
                continue
            if scope in roots:
                opened = os.fstat(roots[scope])
                if (opened.st_dev, opened.st_ino) != identity:
                    continue
                _remove_contents(roots[scope])
            os.rmdir(name, dir_fd=parent)
        raise
    finally:
        for descriptor in (*roots.values(), *parents.values()):
            os.close(descriptor)


def restore_account(
    server,
    account,
    ciphertext_path,
    identities,
    *,
    expected_subject,
    expected_key_id=None,
    expected_sha256=None,
):
    server = service_origin(server)
    paths = _paths(server, account)
    require(1 <= len(identities) <= 8, 'invalid_recovery_identities')
    require(
        not any(os.path.lexists(getattr(paths, scope)) for scope in (*SCOPES, 'cache')),
        'account_restore_destination_exists',
    )
    ciphertext = _read_path(ciphertext_path, private=False)
    expected_sha256 = ciphertext_digest_pin(expected_sha256)
    require(
        expected_sha256 is None or digest(ciphertext) == expected_sha256,
        'account_backup_ciphertext_mismatch',
    )
    require(
        ciphertext.startswith((
            b'age-encryption.org/v1\n',
            b'-----BEGIN AGE ENCRYPTED FILE-----\n',
        )),
        'invalid_account_backup_ciphertext',
    )
    arguments, descriptors = ['--decrypt'], []
    try:
        for identity in identities:
            path = _path(identity)
            parent = _directory(path.parent)
            try:
                descriptor = os.open(
                    path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                )
            except OSError:
                raise Failure('unsafe_account_backup_path') from None
            finally:
                os.close(parent)
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            _private(info)
            require(info.st_size <= MAX_BYTES, 'account_backup_too_large')
            # Hold the exact validated file through decryption, avoiding a pathname swap.
            arguments.extend(('--identity', f'/dev/fd/{descriptor}'))
        plaintext = _age(arguments, ciphertext, pass_fds=tuple(descriptors))
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    directories, files, subject, key_id, backend, source_account = _validate(
        plaintext, server, expected_subject, expected_key_id
    )
    _install(paths, directories, files)
    return _receipt(server, account, subject, key_id, backend, ciphertext) | {
        'restored_locally': True,
        'source_account': source_account,
        'selected': False,
        'next_step': 'Verify the restored account with a normal signed read; credentials may have expired or been revoked.',
    }
