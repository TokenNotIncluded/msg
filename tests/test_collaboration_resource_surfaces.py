"""CLI and canonical subject views execute the same collaboration contracts."""

import httpx
import pytest
from test_collaboration_surfaces import _headers
from test_service import NOW, call, register

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import loads
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_work_record_paths_are_read_only_current_acl_views(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'work-paths')
    commands = {
        'request': {'title': 'Work', 'description': 'Read', 'requirements': 'Explicit'},
        'offer': {'description': 'Review', 'scope': 'Code', 'availability': 'Today'},
        'checkpoint': {'summary': 'Resume', 'resource_refs': []},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        for kind, args in commands.items():
            made = await call(
                app, 'communication.' + kind + '_create', args, key=key, subject=subject
            )
            rid = made.data['id']
            prefix = '/@work-paths/' + kind + 's'
            headers = _headers(app, 'communication.' + kind + '_list', {}, key, subject)
            listed = await http.get(prefix, headers=headers)
            assert listed.status_code == 200, listed.text
            assert listed.json()['items'][0]['id'] == rid
            headers = _headers(app, 'communication.' + kind + '_get', {'id': rid}, key, subject)
            got = await http.get(prefix + '/' + rid, headers=headers)
            assert got.status_code == 200 and got.json()[kind]['id'] == rid
            head = await http.head(prefix + '/' + rid, headers=headers)
            assert head.headers['etag'] == got.headers['etag']
            cached = await http.get(
                prefix + '/' + rid, headers={**headers, 'If-None-Match': got.headers['etag']}
            )
            assert cached.status_code == 304
            assert (await http.post(prefix + '/' + rid, json={})).status_code in {404, 405}


@pytest.mark.asyncio
async def test_work_cli_create_transition_and_proposal_accept(
    installed, tmp_path, monkeypatch, capsys
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        directory = tmp_path / 'client'
        client = MsgClient(
            ClientState(directory, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('work-cli')).status == 'ok'
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
            result = loads(capsys.readouterr().out.encode().strip())
            assert status == 0, result
            return result

        made = await invoke(
            'request',
            'create',
            '--title',
            'Work',
            '--description',
            'Review',
            '--requirements',
            'Explicit claim',
        )
        rid = made['data']['id']
        claimed = await invoke(
            'request', 'claim', rid, '--generation', str(made['data']['generation'])
        )
        assert claimed['data']['status'] == 'claimed'
        fulfilled = await invoke(
            'request', 'fulfill', rid, '--generation', str(claimed['data']['generation'])
        )
        assert fulfilled['data']['status'] == 'fulfilled'
        assert (await invoke('request', 'get', rid))['data']['request']['status'] == 'fulfilled'
        assert (await invoke('request', 'list'))['data']['items'][0]['id'] == rid
        offer = await invoke(
            'offer',
            'create',
            '--description',
            'Review',
            '--scope',
            'Python',
            '--availability',
            'Now',
        )
        assert (
            await invoke(
                'offer',
                'withdraw',
                offer['data']['id'],
                '--generation',
                str(offer['data']['generation']),
            )
        )['data']['status'] == 'withdrawn'
        checkpoint = await invoke('checkpoint', 'create', '--summary', 'Resume here')
        assert (await invoke('checkpoint', 'get', checkpoint['data']['id']))['data']['checkpoint'][
            'summary'
        ] == 'Resume here'
        target = await client.call('content.post_create', {'parent': '/main', 'body': 'Before'})
        source = await client.call('content.post_create', {'parent': '/main', 'body': 'After'})
        proposal = await invoke(
            'proposal',
            'create',
            '--target',
            target.resources[0].id,
            '--base-revision',
            target.resources[0].revision,
            '--content',
            source.resources[0].id,
            '--content-revision',
            source.resources[0].revision,
            '--message',
            'Replace text',
        )
        accepted = await invoke(
            'proposal',
            'accept',
            proposal['data']['id'],
            '--generation',
            str(proposal['data']['generation']),
            '--proposal-revision',
            proposal['resources'][0]['revision'],
            '--base-revision',
            target.resources[0].revision,
            '--target',
            target.resources[0].id,
            '--target-generation',
            str(target.data['generation']),
        )
        assert accepted['data']['status'] == 'accepted'
