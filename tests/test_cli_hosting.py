"""CLI website workflow keeps previews private and publication explicit."""

import httpx
import pytest
from test_service import NOW

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import b64, canonical, loads, wire
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_hosting_cli_preview_deploy_rollback_and_revocation(
    installed, tmp_path, monkeypatch, capsys
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        directory = tmp_path / 'client'
        client = MsgClient(
            ClientState(directory, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('hosting-cli')).status == 'ok'
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
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
                'hosting',
                *command,
            ])
            status = await cli.run(args)
            return status, loads(capsys.readouterr().out.encode().strip())

        status, created = await invoke('create', '/@hosting-cli', 'web')
        assert status == 0 and created['status'] == 'ok'
        site = created['resources'][0]['id']
        source = await client.call(
            'content.file_put',
            {
                'parent': '/@hosting-cli/files',
                'name': 'candidate.html',
                'data': b64(b'<script>bad()</script><h1>candidate</h1>'),
                'media_type': 'text/html',
            },
        )
        assert source.status == 'ok'
        entries = canonical([{'path': 'index.html', 'source': wire(source.resources[0])}]).decode()
        status, candidate = await invoke('preview', site, entries)
        assert status == 0 and candidate['status'] == 'ok'
        assert 'url' not in candidate['data']
        candidate_id = candidate['resources'][0]['id']
        destination = tmp_path / 'candidate.txt'
        status, saved = await invoke(
            'preview-get',
            '/@hosting-cli/web',
            candidate_id,
            'index.html',
            '--output',
            str(destination),
        )
        assert status == 0 and saved['bytes'] == len(destination.read_bytes())
        assert destination.read_bytes().startswith(b'<script>')
        assert destination.stat().st_mode & 0o077 == 0
        with pytest.raises(Failure, match='invalid_hosting_path'):
            await client.hosting_preview_get('/@hosting-cli/web', candidate_id, '../private')
        with pytest.raises(Failure, match='preview_not_found'):
            await client.hosting_preview_get('/@hosting-cli/web', 'p_missing', 'index.html')

        status, deployed = await invoke('deploy', site, entries)
        assert status == 0 and deployed['status'] == 'ok'
        first_revision = deployed['resources'][0]['revision']
        second = await client.call(
            'content.file_put',
            {
                'parent': '/@hosting-cli/files',
                'name': 'second.html',
                'data': b64(b'<h1>second</h1>'),
                'media_type': 'text/html',
            },
        )
        second_entries = canonical([
            {'path': 'index.html', 'source': wire(second.resources[0])}
        ]).decode()
        status, _ = await invoke('deploy', site, second_entries)
        assert status == 0
        status, rolled = await invoke('activate', site, first_revision)
        assert status == 0 and rolled['resources'][0]['revision'] == first_revision
        public = await http.get('/@hosting-cli/web/')
        assert public.content == destination.read_bytes()
        assert 'allow-scripts' not in public.headers['content-security-policy']
        status, history = await invoke('history', site)
        assert status == 0 and history['status'] == 'ok'
        changed = await client.call(
            'content.chmod',
            {'id': candidate_id, 'mode': '0000'},
            expected=((candidate_id, candidate['data']['generation']),),
        )
        assert changed.status == 'ok'
        with pytest.raises(Failure, match='preview_permission_denied'):
            await client.hosting_preview_get('/@hosting-cli/web', candidate_id, 'index.html')
