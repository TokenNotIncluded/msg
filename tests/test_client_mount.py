"""Mount contract tests do not import native bindings or require /dev/fuse."""

import argparse
import asyncio
import errno
import os
import stat
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from msg.client_mount import (
    MAX_HANDLES,
    READ_BYTES,
    MountBackend,
    Node,
    ReadOnlyMount,
    add_commands,
    canonical_path,
    mountpoint_path,
    run_mount,
)
from msg.core.codec import canonical, loads, unb64
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport, PathGETTransport


def metadata(**changes):
    return {
        'id': 'r_file',
        'path': '/main/a.md',
        'container': False,
        'revision': 'v_first',
        'size': 100,
        'state': 'active',
        'created_at': '2026-10-01T00:00:00Z',
        'modified_at': '2026-10-01T01:00:00Z',
        **changes,
    }


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as http:
        transport = HTTPTransport('https://example.test', http=http)
        transport._description = {
            'operations': dict.fromkeys(
                ('file.stat', 'discovery.get', 'discovery.read_query', 'discovery.raw'), 'read'
            )
        }
        value = SimpleNamespace(
            transport=transport,
            signer_override=object(),
            call=AsyncMock(return_value=SimpleNamespace(status='ok', data=metadata(), error=None)),
            prepare=lambda operation, arguments: {'operation': operation, 'arguments': arguments},
        )
        yield value


def ok(data):
    return SimpleNamespace(status='ok', data=data, error=None)


@pytest.mark.parametrize(
    'path', ['', 'main', '//', '/main/', '/./a', '/../a', '/a//b', '/a/../b', '/a\\b', '/\x00']
)
def test_invalid_paths(path):
    with pytest.raises(Failure, match='invalid_mount_path'):
        canonical_path(path)


@pytest.mark.asyncio
async def test_mount_root_and_lazy_node(client):
    backend = MountBackend(client, '/main')
    assert backend.remote_path('/') == '/main'
    assert backend.remote_path('/a.md') == '/main/a.md'
    node = await backend.node('/a.md')
    assert node.size == 100
    client.call.assert_awaited_once_with('file.stat', {'id': '/main/a.md'})
    attrs = node.attributes()
    assert stat.S_ISREG(attrs['st_mode']) and attrs['st_mode'] & 0o777 == 0o400
    assert attrs['st_mtime'] > attrs['st_ctime'] > 0
    assert attrs['st_ino'] == Node(metadata()).attributes()['st_ino']
    assert attrs['st_uid'] == os.geteuid()


@pytest.mark.asyncio
async def test_authoritative_container_required(client):
    value = metadata()
    del value['container']
    client.call.return_value = ok(value)
    with pytest.raises(Failure, match='mount_server_upgrade_required'):
        await MountBackend(client).node('/')


@pytest.mark.asyncio
async def test_non_http_transport_rejected(client):
    client.transport = PathGETTransport('https://example.test', http=client.transport.http)
    with pytest.raises(Failure, match='mount_http_transport_required'):
        MountBackend(client)


@pytest.mark.asyncio
async def test_closed_read_allowlist_and_effect_guard(client):
    backend = MountBackend(client)
    with pytest.raises(Failure, match='mount_read_only'):
        await backend.query('file.delete', {'id': 'r_file'})
    client.transport._description['operations']['file.stat'] = 'transaction'
    with pytest.raises(Failure, match='effect_mismatch'):
        await backend.node('/')
    client.call.assert_not_called()


@pytest.mark.asyncio
async def test_paginated_directory_uses_only_opaque_cursor(client):
    client.call.side_effect = [
        ok(metadata(id='r_topic', path='/main', container=True, revision=None)),
        ok({
            'items': [{'id': 'a', 'path': '/main/a.md'}],
            'cursor': 'next',
            'next': 'https://evil.test/',
        }),
        ok({'items': [{'id': 'b', 'path': '/main/你好.md'}]}),
    ]
    assert await MountBackend(client, '/main').directory('/') == ['.', '..', 'a.md', '你好.md']
    assert client.call.await_args_list[1].args == (
        'discovery.read_query',
        {'parent': 'r_topic', 'limit': 100, 'fields': ['id', 'path']},
    )
    assert client.call.await_args_list[2].args == ('discovery.read_query', {'cursor': 'next'})


@pytest.mark.parametrize(
    'bad', ['/elsewhere/a', '/main/../secret', '/main/sub/a', '/main/', '/main/' + 'x' * 256]
)
@pytest.mark.asyncio
async def test_directory_rejects_unsafe_children(client, bad):
    client.call.side_effect = [
        ok(metadata(path='/main', container=True)),
        ok({'items': [{'path': bad}]}),
    ]
    with pytest.raises(Failure):
        await MountBackend(client).directory('/main')


@pytest.mark.asyncio
async def test_directory_refuses_truncation_and_repeated_cursors(client):
    root = ok(metadata(path='/main', container=True))
    client.call.side_effect = [root, ok({'items': [{'path': '/main/a'}, {'path': '/main/b'}]})]
    with pytest.raises(OSError) as exc:
        await MountBackend(client, max_entries=1).directory('/main')
    assert exc.value.errno == errno.EOVERFLOW
    client.call.side_effect = [
        root,
        ok({'items': [], 'cursor': 'c'}),
        ok({'items': [], 'cursor': 'c'}),
    ]
    with pytest.raises(Failure, match='invalid_mount_response'):
        await MountBackend(client).directory('/main')


@pytest.mark.asyncio
async def test_revisionless_projection_reauthorizes_every_read(client):
    client.call.side_effect = [
        ok(metadata(revision=None)),
        ok({'id': 'r_file', 'type': 'certificate'}),
        ok(metadata(revision=None)),
        SimpleNamespace(status='error', error=SimpleNamespace(code='permission_denied')),
    ]
    backend = MountBackend(client)
    node = await backend.node('/main/a.md')
    assert node.projection == canonical({'id': 'r_file', 'type': 'certificate'}) + b'\n'
    assert await backend.read(node, 20, 0) == node.projection[:20]
    with pytest.raises(OSError) as exc:
        await backend.read(node, 20, 0)
    assert exc.value.errno == errno.EACCES


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield self.body


@pytest.mark.asyncio
async def test_raw_random_read_is_signed_pinned_and_bounded(client):
    captured = []
    body = bytes(range(256)) * 600

    def handler(request):
        captured.append(request)
        packet = loads(unb64(request.headers['x-msg-request']))
        args = packet['arguments']
        offset, length = args['offset'], args['length']
        assert packet['operation'] == 'discovery.raw'
        assert args['revision'] == 'v_first'
        assert request.headers['range'] == f'bytes={offset}-{offset + length - 1}'
        assert request.headers['accept-encoding'] == 'identity'
        assert request.url.path == '/_id/r_file/revisions/v_first/raw'
        return httpx.Response(
            206,
            headers={
                'content-range': f'bytes {offset}-{offset + length - 1}/{len(body)}',
                'content-length': str(length),
            },
            stream=BytesStream(body[offset : offset + length]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client.transport.http = http
        backend = MountBackend(client)
        node = Node(metadata(size=len(body)))
        assert await backend.read(node, 2**20, 29) == body[29 : 29 + READ_BYTES]
        assert await backend.read(node, 4096, len(body) - 10) == body[-10:]
        assert await backend.read(node, 4096, len(body)) == b''
        assert len(captured) == 2
        with pytest.raises(OSError) as exc:
            await backend.read(node, 1, -1)
        assert exc.value.errno == errno.EINVAL


@pytest.mark.parametrize(
    'status,headers,body,expected',
    [
        (302, {'location': 'https://evil.test'}, b'', Failure),
        (403, {}, b'secret', OSError),
        (404, {}, b'', OSError),
        (200, {}, b'whole object', Failure),
        (206, {'content-range': 'bytes 1-9/100', 'content-length': '10'}, b'0123456789', Failure),
        (206, {'content-range': 'bytes 0-9/100', 'content-length': '10'}, b'01234567890', Failure),
        (206, {'content-range': 'bytes 0-9/100', 'content-length': '10'}, b'short', Failure),
    ],
)
@pytest.mark.asyncio
async def test_raw_response_validation(client, status, headers, body, expected):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers, stream=BytesStream(body))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client.transport.http = http
        with pytest.raises(expected):
            await MountBackend(client).read(Node(metadata()), 10, 0)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_adapter_open_read_release_and_write_denial(client):
    backend = MountBackend(client)
    backend.read = AsyncMock(return_value=b'bytes')
    mount = ReadOnlyMount(backend, asyncio.get_running_loop())
    for flags in (os.O_WRONLY, os.O_RDWR, os.O_RDONLY | os.O_TRUNC, os.O_RDONLY | os.O_APPEND):
        with pytest.raises(OSError) as exc:
            mount.open('/main/a.md', flags)
        assert exc.value.errno == errno.EROFS
    client.call.assert_not_called()
    fh = await asyncio.to_thread(mount.open, '/main/a.md', os.O_RDONLY)
    assert await asyncio.to_thread(mount.read, '/main/a.md', 5, 8, fh) == b'bytes'
    backend.read.assert_awaited_once_with(Node(metadata()), 5, 8)
    assert mount.release('/main/a.md', fh) == 0
    with pytest.raises(OSError) as exc:
        mount.read('/main/a.md', 5, 8, fh)
    assert exc.value.errno == errno.EBADF
    for method in (
        'create',
        'write',
        'truncate',
        'unlink',
        'mkdir',
        'rmdir',
        'rename',
        'chmod',
        'chown',
        'utimens',
        'symlink',
        'link',
        'mknod',
        'setxattr',
        'removexattr',
        'fallocate',
    ):
        with pytest.raises(OSError) as exc:
            getattr(mount, method)('/main/a.md')
        assert exc.value.errno == errno.EROFS
    mount.handles = dict.fromkeys(range(MAX_HANDLES))
    with pytest.raises(OSError) as exc:
        mount.open('/main/a.md', os.O_RDONLY)
    assert exc.value.errno == errno.EMFILE


@pytest.mark.asyncio
async def test_callback_errors_are_sanitized_and_timeouts_cancel(client):
    backend = MountBackend(client)
    mount = ReadOnlyMount(backend, asyncio.get_running_loop(), timeout=0.02)
    backend.node = AsyncMock(side_effect=Failure('permission_denied', details={'secret': 'TOKEN'}))
    with pytest.raises(OSError) as exc:
        await asyncio.to_thread(mount.getattr, '/private')
    assert exc.value.errno == errno.EACCES and 'TOKEN' not in str(exc.value)
    cancelled = asyncio.Event()

    async def slow(path):
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.set()

    backend.node = slow
    with pytest.raises(OSError) as exc:
        await asyncio.to_thread(mount.getattr, '/slow')
    assert exc.value.errno == errno.ETIMEDOUT
    await asyncio.wait_for(cancelled.wait(), 1)


def test_mountpoint_must_be_owned_empty_directory(tmp_path):
    target = tmp_path / 'new'
    assert mountpoint_path(target) == target
    assert target.stat().st_mode & 0o777 == 0o700
    (target / 'keep').write_text('do not hide')
    with pytest.raises(Failure, match='mountpoint_not_empty'):
        mountpoint_path(target)
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(Failure, match='unsafe_mountpoint'):
        mountpoint_path(link)
    target.chmod(0o777)
    with pytest.raises(Failure, match='unsafe_mountpoint'):
        mountpoint_path(target)


def test_mount_cli_does_not_import_optional_binding():
    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['mount', '/tmp/msg-mount', '--path', '/main'])
    assert args.command == 'mount' and args.path == '/main'
    assert args.max_entries == 10000 and args.timeout == 45
    assert 'mfusepy' not in sys.modules


@pytest.mark.asyncio
async def test_missing_dependency_is_actionable_not_an_import_crash(client, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'mfusepy', None)
    args = SimpleNamespace(path='/', max_entries=10000, timeout=45, mountpoint=tmp_path / 'mount')
    with pytest.raises(Failure, match='mount_dependency_missing'):
        await run_mount(client, args)
    assert not args.mountpoint.exists()
