"""FUSE backend integration against the actual HTTP service and database ACLs."""

import errno

import httpx
import pytest
from test_service import NOW

from msg.client import ClientState, MsgClient
from msg.client_mount import MountBackend
from msg.core.codec import b64, wire
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_mount_reads_raw_bytes_and_rechecks_revoked_access(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner = MsgClient(
            ClientState(tmp_path / 'owner', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await owner.register('mount-owner')).status == 'ok'
        guest = MsgClient(
            ClientState(tmp_path / 'guest', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        created = await owner.call(
            'content.post_create', {'parent': '/main', 'body': '原始 Markdown\n'}
        )
        assert created.status == 'ok', wire(created)
        rid = created.resources[0].id
        meta = await owner.call('file.stat', {'id': rid})
        assert meta.status == 'ok' and meta.data['container'] is False, wire(meta)
        path = meta.data['path']
        mount = MountBackend(guest)
        pinned = await mount.node(path)
        assert await mount.read(pinned, 65536, 0) == '原始 Markdown\n'.encode()
        assert await mount.read(pinned, 3, 1) == '原始 Markdown\n'.encode()[1:4]
        root = await mount.node('/main')
        assert root.directory
        assert path.rsplit('/', 1)[-1] in await mount.directory('/main')
        changed = await owner.call(
            'content.post_edit',
            {'id': rid, 'expected_revision': pinned.metadata['revision'], 'body': 'new revision'},
            expected=((rid, created.data['generation']),),
        )
        assert changed.status == 'ok', wire(changed)
        assert await mount.read(pinned, 65536, 0) == '原始 Markdown\n'.encode()
        current = await mount.node(path)
        assert await mount.read(current, 65536, 0) == b'new revision'
        private = await owner.call(
            'content.chmod',
            {'id': rid, 'mode': '0600'},
            expected=((rid, changed.data['generation']),),
        )
        assert private.status == 'ok', wire(private)
        for action in (mount.node(path), mount.read(pinned, 10, 0)):
            with pytest.raises(OSError) as exc:
                await action
            assert exc.value.errno == errno.EACCES
        assert path.rsplit('/', 1)[-1] not in await mount.directory('/main')
        own = MountBackend(owner)
        assert await own.read(await own.node(path), 65536, 0) == b'new revision'
        # Reading the mount does not express user intent to ACK, react or follow.
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT COUNT(*) FROM reactions')[0] == 0
            assert tx.one('SELECT COUNT(*) FROM agent_follows')[0] == 0
            assert tx.one('SELECT COUNT(*) FROM watches')[0] == 0


@pytest.mark.asyncio
async def test_mount_binary_empty_files_and_private_subtree(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        client = MsgClient(
            ClientState(tmp_path / 'owner', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('mount-binary')).status == 'ok'
        parent = '/@mount-binary/files'
        bodies = {'bytes.bin': bytes(range(256)) * 500, 'empty.txt': b''}
        for name, body in bodies.items():
            created = await client.call(
                'file.create', {'parent': parent, 'name': name, 'data': b64(body)}
            )
            assert created.status == 'ok', wire(created)
        backend = MountBackend(client, parent)
        assert set(await backend.directory('/')) == {'.', '..', *bodies}
        for name, body in bodies.items():
            node = await backend.node('/' + name)
            assert node.size == len(body)
            result = b''.join([
                await backend.read(node, 65536, offset) for offset in range(0, len(body), 65536)
            ])
            assert result == body
            assert await backend.read(node, 10, len(body)) == b''
