"""Read-only FUSE projection of authorized resources; never a website crawler.

The SDK stays on its asyncio loop. Synchronous FUSE callbacks run in a worker
and submit bounded reads back to that loop. Native bindings are loaded only by
``mount`` so the normal client remains usable without libfuse.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure, require
from msg.transports.client import HTTPTransport

READ_BYTES = 65536
MAX_ENTRIES = 10000
MAX_HANDLES = 64
_ID = re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')
_MISSING = {
    'not_found',
    'resource_not_found',
    'revision_not_found',
    'resource_purged',
    'ancestor_inactive',
}
_DENIED = {
    'authentication_required',
    'permission_denied',
    'credential_ceiling',
    'certificate_gate',
    'capability_required',
    'tool_certificate_required',
    'delegation_scope',
    'request_expired',
    'credential_revoked',
    'credential_expired',
    'invalid_signature',
    'invalid_credential',
}


def io_error(number):
    """Expose errno, not remote response bodies, private URLs or credentials."""
    return OSError(number, os.strerror(number))


def remote_error(code):
    return io_error(
        errno.ENOENT if code in _MISSING else errno.EACCES if code in _DENIED else errno.EIO
    )


def canonical_path(value):
    require(
        isinstance(value, str)
        and value.startswith('/')
        and '\x00' not in value
        and '\\' not in value
        and (value == '/' or all(part not in {'', '.', '..'} for part in value[1:].split('/'))),
        'invalid_mount_path',
    )
    return value


def add_commands(commands):
    command = commands.add_parser('mount', help='Mount authorized resources as a read-only folder.')
    command.add_argument('mountpoint', type=Path)
    command.add_argument('--path', default='/', help='Remote directory to mount (default: /).')
    command.add_argument(
        '--max-entries',
        type=int,
        default=MAX_ENTRIES,
        help='Maximum entries per directory; larger listings fail explicitly (default: 10000).',
    )
    command.add_argument(
        '--timeout',
        type=float,
        default=45,
        help='Deadline for each filesystem request in seconds (default: 45).',
    )
    command.epilog = (
        'Runs in the foreground; Ctrl-C unmounts. Requires msgctl[fuse] and system FUSE. '
        'No writes, automatic ACKs, follows, or persistent content cache.'
    )


@dataclass(frozen=True)
class Node:
    metadata: Mapping
    projection: bytes | None = None

    @property
    def directory(self):
        return self.metadata['container']

    @property
    def size(self):
        if self.directory:
            return 0
        return len(self.projection) if self.projection is not None else self.metadata['size']

    def attributes(self):
        created = self.metadata.get('created_at')
        modified = self.metadata.get('modified_at') or created

        def nanoseconds(value):
            return int(datetime.fromisoformat(value).timestamp() * 1_000_000_000) if value else 0

        return {
            'st_mode': (stat.S_IFDIR | 0o500) if self.directory else (stat.S_IFREG | 0o400),
            'st_nlink': 2 if self.directory else 1,
            'st_uid': os.geteuid(),
            'st_gid': os.getegid(),
            'st_size': self.size,
            'st_ino': int.from_bytes(
                hashlib.blake2b(self.metadata['id'].encode(), digest_size=8).digest(), 'big'
            )
            & ((1 << 63) - 1)
            or 1,
            'st_atime': nanoseconds(modified),
            'st_mtime': nanoseconds(modified),
            'st_ctime': nanoseconds(created),
            'st_blksize': READ_BYTES,
            'st_blocks': (self.size + 511) // 512,
        }


class MountBackend:
    """All paths and listings pass through the existing authorization boundary."""

    def __init__(self, client, root='/', *, max_entries=MAX_ENTRIES):
        require(type(client.transport) is HTTPTransport, 'mount_http_transport_required')
        require(type(max_entries) is int and 1 <= max_entries <= 100000, 'invalid_mount_limit')
        self.client = client
        self.root = canonical_path(root)
        self.max_entries = max_entries

    def remote_path(self, path):
        path = canonical_path(path)
        return self.root if path == '/' else self.root.rstrip('/') + path

    async def query(self, operation, arguments):
        # A closed allowlist prevents future filesystem helpers acquiring write effects.
        require(
            operation in {'file.stat', 'discovery.get', 'discovery.read_query'}, 'mount_read_only'
        )
        description = await self.client.transport.description()
        require(description['operations'].get(operation) == 'read', 'effect_mismatch')
        result = await self.client.call(operation, arguments)
        if result.status != 'ok':
            raise remote_error(result.error.code if result.error is not None else '')
        require(isinstance(result.data, Mapping), 'invalid_mount_response')
        return result.data

    async def node(self, path=None, *, resource_id=None):
        target = resource_id if resource_id is not None else self.remote_path(path)
        data = await self.query('file.stat', {'id': target})
        require(type(data.get('container')) is bool, 'mount_server_upgrade_required')
        require(
            isinstance(data.get('id'), str) and _ID.fullmatch(data['id']) is not None,
            'invalid_mount_response',
        )
        if data.get('state') in {'archived', 'purged'}:
            raise io_error(errno.ENOENT)
        projection = None
        if not data['container']:
            if data.get('revision'):
                require(
                    isinstance(data['revision'], str)
                    and _ID.fullmatch(data['revision']) is not None
                    and type(data.get('size')) is int
                    and 0 <= data['size'] < 2**63,
                    'invalid_mount_response',
                )
            else:
                # Certificates and other revisionless leaves have an authorized
                # JSON representation, not a fabricated empty raw file.
                projection = (
                    canonical(await self.query('discovery.get', {'id': data['id']})) + b'\n'
                )
                require(len(projection) <= 1048576, 'response_too_large')
        return Node(data, projection)

    async def directory(self, path):
        parent = await self.node(path)
        if not parent.directory:
            raise io_error(errno.ENOTDIR)
        parent_path = canonical_path(parent.metadata['path']).rstrip('/') + '/'
        first = {'parent': parent.metadata['id'], 'limit': 100, 'fields': ['id', 'path']}
        arguments = first
        names, seen, cursors = [], set(), set()
        for _ in range(self.max_entries + 1):
            page = await self.query('discovery.read_query', arguments)
            require(isinstance(page.get('items'), (list, tuple)), 'invalid_mount_response')
            for item in page['items']:
                require(isinstance(item, Mapping), 'invalid_mount_response')
                child = canonical_path(item.get('path'))
                require(child.startswith(parent_path), 'invalid_mount_response')
                name = child[len(parent_path) :]
                require(
                    name not in {'', '.', '..'} and '/' not in name and len(name.encode()) <= 255,
                    'invalid_mount_response',
                )
                require(name not in seen, 'invalid_mount_response')
                if len(names) >= self.max_entries:
                    raise io_error(errno.EOVERFLOW)
                names.append(name)
                seen.add(name)
            cursor = page.get('cursor')
            if cursor is None:
                return ['.', '..', *names]
            require(
                isinstance(cursor, str) and cursor and cursor not in cursors,
                'invalid_mount_response',
            )
            cursors.add(cursor)
            # Never follow server-provided next URLs; the opaque cursor returns
            # through the same authenticated, principal-bound read operation.
            arguments = {'cursor': cursor}
        raise io_error(errno.EOVERFLOW)

    async def read(self, node, size, offset):
        if node.directory:
            raise io_error(errno.EISDIR)
        if size < 0 or offset < 0:
            raise io_error(errno.EINVAL)
        length = min(size, READ_BYTES, max(0, node.size - offset))
        if node.projection is not None or length == 0:
            # Cached JSON, empty files and EOF still pass the current ACL.
            # A zero-byte read must not keep accepting a revoked open handle.
            await self.query('file.stat', {'id': node.metadata['id']})
            return node.projection[offset : offset + length] if node.projection is not None else b''
        from msg.client_oauth import read_session, refresh

        client = self.client
        if client.signer_override is None and read_session(client.state) is not None:
            await refresh(client)
        transport = client.transport
        description = await transport.description()
        require(description['operations'].get('discovery.raw') == 'read', 'effect_mismatch')
        rid, revision = node.metadata['id'], node.metadata['revision']
        packet = client.prepare(
            'discovery.raw', {'id': rid, 'revision': revision, 'offset': offset, 'length': length}
        )
        path = f'/_id/{rid}/revisions/{revision}/raw'
        end = offset + length - 1
        async with transport.http.stream(
            'GET',
            transport.endpoint + path,
            headers={
                'X-Msg-Request': b64(canonical(wire(packet))),
                'Range': f'bytes={offset}-{end}',
                'Accept-Encoding': 'identity',
            },
            follow_redirects=False,
        ) as response:
            if response.status_code in {401, 403}:
                raise io_error(errno.EACCES)
            if response.status_code in {404, 410}:
                raise io_error(errno.ENOENT)
            require(
                response.status_code == 206
                and response.headers.get('content-range') == f'bytes {offset}-{end}/{node.size}'
                and response.headers.get('content-length') == str(length)
                and response.headers.get('content-encoding', 'identity') == 'identity',
                'invalid_mount_response',
            )
            payload = bytearray()
            async for chunk in response.aiter_raw():
                require(len(payload) + len(chunk) <= length, 'response_too_large')
                payload.extend(chunk)
            require(len(payload) == length, 'invalid_mount_response')
            return bytes(payload)


class ReadOnlyMount:
    """Path-based mfusepy adapter; no imports or kernel device needed in tests."""

    use_ns = True

    def __init__(self, backend, loop, *, timeout=45):
        self.backend, self.loop, self.timeout = backend, loop, timeout
        self.handles = {}
        self.next_handle = 1

    def init_with_config(self, connection, config):
        # libfuse 3 requires the init handshake to agree with -o max_read.
        if connection is not None and hasattr(connection, 'max_read'):
            connection.max_read = READ_BYTES

    def invoke(self, coroutine):
        async def bounded():
            async with asyncio.timeout(self.timeout):
                return await coroutine

        future = asyncio.run_coroutine_threadsafe(bounded(), self.loop)
        try:
            return future.result(self.timeout + 1)
        except TimeoutError:
            future.cancel()
            raise io_error(errno.ETIMEDOUT) from None
        except Failure as exc:
            raise remote_error(exc.code) from None
        except httpx.HTTPError:
            raise io_error(errno.EIO) from None

    def getattr(self, path, fh=None):
        # Paths always reauthorize; open descriptors keep their pinned size.
        if fh is not None and fh in self.handles:
            node = self.handles[fh]
            self.invoke(self.backend.query('file.stat', {'id': node.metadata['id']}))
        else:
            node = self.invoke(self.backend.node(path))
        return node.attributes()

    def readdir(self, path, fh):
        return self.invoke(self.backend.directory(path))

    def access(self, path, mode):
        if mode & os.W_OK:
            raise io_error(errno.EROFS)
        node = self.invoke(self.backend.node(path))
        if mode & os.X_OK and not node.directory:
            raise io_error(errno.EACCES)
        return 0

    def open(self, path, flags):
        if flags & os.O_ACCMODE != os.O_RDONLY or flags & (os.O_TRUNC | os.O_CREAT | os.O_APPEND):
            raise io_error(errno.EROFS)
        if len(self.handles) >= MAX_HANDLES:
            raise io_error(errno.EMFILE)
        node = self.invoke(self.backend.node(path))
        if node.directory:
            raise io_error(errno.EISDIR)
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = node
        return handle

    def read(self, path, size, offset, fh):
        if fh not in self.handles:
            raise io_error(errno.EBADF)
        return self.invoke(self.backend.read(self.handles[fh], size, offset))

    def release(self, path, fh):
        self.handles.pop(fh, None)
        return 0

    def flush(self, path, fh):
        return 0

    def statfs(self, path):
        return {
            'f_bsize': 4096,
            'f_frsize': 4096,
            'f_blocks': 0,
            'f_bfree': 0,
            'f_bavail': 0,
            'f_files': 0,
            'f_ffree': 0,
            'f_favail': 0,
            'f_flag': os.ST_RDONLY,
            'f_namemax': 255,
        }

    def listxattr(self, path):
        return []

    def getxattr(self, path, name, position=0):
        raise io_error(getattr(errno, 'ENOATTR', errno.ENODATA))

    def readlink(self, path):
        raise io_error(errno.EINVAL)

    def readonly(self, *args):
        raise io_error(errno.EROFS)

    create = write = truncate = unlink = rmdir = mkdir = rename = readonly
    chmod = chown = utimens = symlink = link = mknod = readonly
    setxattr = removexattr = fallocate = readonly


def mountpoint_path(value):
    path = Path(value).expanduser().absolute()
    require(not path.is_symlink(), 'unsafe_mountpoint')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o022 == 0,
        'unsafe_mountpoint',
    )
    require(not os.path.ismount(path), 'mountpoint_busy')
    require(next(path.iterdir(), None) is None, 'mountpoint_not_empty')
    return path.resolve()


async def run_mount(client, args):
    import math
    import sys

    require(
        sys.platform.startswith('linux') or sys.platform == 'darwin', 'mount_platform_unsupported'
    )
    require(math.isfinite(args.timeout) and 0 < args.timeout <= 300, 'invalid_mount_timeout')
    try:
        import mfusepy
    except ImportError:
        raise Failure(
            'mount_dependency_missing', details={'install': 'uv tool install "msgctl[fuse]"'}
        ) from None
    except OSError, AttributeError:
        raise Failure(
            'mount_system_fuse_missing',
            details={'install': 'Install fuse3 (Linux) or macFUSE (macOS).'},
        ) from None
    backend = MountBackend(client, args.path, max_entries=args.max_entries)
    async with asyncio.timeout(args.timeout):
        root = await backend.node('/')
    require(root.directory, 'mount_root_not_directory')
    mountpoint = mountpoint_path(args.mountpoint)
    operations = ReadOnlyMount(backend, asyncio.get_running_loop(), timeout=args.timeout)
    await serve_mount(mfusepy, operations, mountpoint)
    return 0


async def serve_mount(binding, operations, mountpoint):
    """Keep native callbacks off the SDK loop; unmount on task cancellation."""
    import shutil

    task = asyncio.create_task(
        asyncio.to_thread(
            binding.FUSE,
            operations,
            str(mountpoint),
            foreground=True,
            nothreads=True,
            ro=True,
            nodev=True,
            nosuid=True,
            noexec=True,
            default_permissions=True,
            direct_io=True,
            attr_timeout=0,
            entry_timeout=0,
            negative_timeout=0,
            max_read=READ_BYTES,
            fsname='msg',
        )
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Native libfuse normally handles SIGINT itself. Cancellation from an
        # embedding application must not leave its worker mounted indefinitely.
        for _ in range(100):
            if task.done() or await asyncio.to_thread(os.path.ismount, mountpoint):
                break
            await asyncio.sleep(0.02)
        if not task.done() and await asyncio.to_thread(os.path.ismount, mountpoint):
            executable = shutil.which('fusermount3') or shutil.which('fusermount')
            command = (
                [executable, '-u', '--', str(mountpoint)]
                if executable
                else ['umount', str(mountpoint)]
            )
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await process.wait()
        await asyncio.shield(task)
        raise
    except RuntimeError:
        raise Failure(
            'mount_failed',
            details={'hint': 'Check FUSE installation and mountpoint permissions.'},
        ) from None
    finally:
        operations.handles.clear()
