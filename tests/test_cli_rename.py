"""Convenience renames use stable IDs, current authorization and generation checks."""

import httpx
import pytest
from test_service import NOW

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import b64, loads
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_cli_named_posts_and_rename_preserve_content_and_reject_invalid_changes(
    installed, tmp_path, monkeypatch, capsys
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        directory = tmp_path / 'owner'
        client = MsgClient(
            ClientState(directory, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('rename-owner')).status == 'ok'
        other = MsgClient(
            ClientState(tmp_path / 'other', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await other.register('rename-other')).status == 'ok'
        monkeypatch.setattr(
            cli,
            'MsgClient',
            lambda state, transport: MsgClient(state, transport, clock=lambda: NOW),
        )

        async def invoke(*command):
            args = cli.parser().parse_args([
                '--config-dir',
                str(directory),
                '--server',
                app.settings.service_url,
                *command,
            ])
            status = await cli.run(args)
            return status, loads(capsys.readouterr().out.encode().strip())

        status, created = await invoke('post', '/main', '--name', 'Original', '--text', 'unchanged')
        assert status == 0
        rid, revision = created['resources'][0]['id'], created['resources'][0]['revision']
        async with app.metadata.transaction(write=False) as tx:
            before = await tx.resource(rid)
            count = tx.one('SELECT COUNT(*) FROM revisions WHERE resource_id=?', (rid,))[0]
            original_path = await tx.path(rid)
        assert before.name == 'Original.md'
        status, changed = await invoke('rename', original_path, '新的名称')
        assert status == 0 and changed['resources'][0]['id'] == rid
        async with app.metadata.transaction(write=False) as tx:
            after = await tx.resource(rid)
            new_path = await tx.path(rid)
            assert after.name == '新的名称.md' and after.revision == revision
            assert after.generation == before.generation + 1
            assert tx.one('SELECT COUNT(*) FROM revisions WHERE resource_id=?', (rid,))[0] == count
        reading = await client.call('discovery.get', {'id': new_path})
        assert reading.status == 'ok' and reading.data['content'] == 'unchanged'
        denied = await other.rename(rid, 'stolen')
        assert denied.status == 'error'
        collision = await client.call(
            'content.post_create', {'parent': '/main', 'name': 'Taken', 'body': 'another'}
        )
        assert collision.status == 'ok'
        assert (await client.rename(rid, 'Taken')).status == 'error'
        assert (await client.rename(rid, '../escape')).status == 'error'
        assert (await client.rename('/missing.md', 'name')).status == 'error'
        async with app.metadata.transaction(write=False) as tx:
            assert await tx.resource(rid) == after

        file = await client.call(
            'content.file_put',
            {
                'parent': '/@rename-owner/files',
                'name': 'original.txt',
                'data': b64(b'file content'),
            },
        )
        assert file.status == 'ok'
        file_id, file_revision = file.resources[0].id, file.resources[0].revision
        status, renamed = await invoke('rename', file_id, 'renamed.txt')
        assert status == 0 and renamed['resources'][0]['id'] == file_id
        async with app.metadata.transaction(write=False) as tx:
            resource = await tx.resource(file_id)
            assert resource.name == 'renamed.txt' and resource.revision == file_revision
            assert resource.parent == await tx.resolve('/@rename-owner/files')


@pytest.mark.asyncio
async def test_rename_does_not_overwrite_a_concurrent_metadata_change(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        client = MsgClient(
            ClientState(tmp_path / 'race', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('rename-race')).status == 'ok'
        created = await client.call('content.post_create', {'parent': '/main', 'body': 'race'})
        rid = created.resources[0].id
        original_call = client.call

        async def interleaved(operation, arguments, **kwargs):
            result = await original_call(operation, arguments, **kwargs)
            if operation == 'discovery.get':
                edited = await original_call(
                    'content.post_edit_metadata',
                    {'id': rid, 'name': 'concurrent'},
                    expected=((rid, result.data['generation']),),
                )
                assert edited.status == 'ok'
            return result

        client.call = interleaved
        rejected = await client.rename(rid, 'stale')
        assert rejected.error.code == 'generation_conflict'
        async with app.metadata.transaction(write=False) as tx:
            assert (await tx.resource(rid)).name == 'concurrent.md'
