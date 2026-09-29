"""Search and grep CLI use the signed operations and explicit bounded pages."""

import httpx
import pytest
from test_service import NOW

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import loads
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_cli_search_one_page_cursor_and_scoped_grep(installed, tmp_path, monkeypatch, capsys):
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
        assert (await client.register('cli-search')).status == 'ok'
        for body in ('CLI needle first', 'CLI needle second'):
            assert (
                await client.call('content.post_create', {'parent': '/main', 'body': body})
            ).status == 'ok'
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
                *command,
            ])
            status = await cli.run(args)
            return status, loads(capsys.readouterr().out.encode().strip())

        status, first = await invoke(
            'search', '/main', 'CLI needle', '--field', 'body', '--limit', '1', '--order', 'name'
        )
        assert status == 0 and first['status'] == 'ok'
        assert len(first['data']['items']) == 1 and first['data']['cursor']
        status, second = await invoke('search', '--cursor', first['data']['cursor'])
        assert status == 0 and len(second['data']['items']) == 1
        assert first['data']['items'][0]['ref']['id'] != second['data']['items'][0]['ref']['id']
        status, grepped = await invoke(
            'grep', '/main', 'needle', '--ignore-case', '--max-matches', '1'
        )
        assert status == 0 and len(grepped['data']['matches']) == 1
        assert grepped['data']['truncated'] is True
        status, counted = await invoke('grep', '/main', 'needle', '--ignore-case', '--count-only')
        assert status == 0 and counted['data']['count'] == 2


@pytest.mark.asyncio
async def test_cli_v5_search_flags_typed_scope_and_cursor_use_signed_contract(
    installed, tmp_path, monkeypatch, capsys
):
    from msg.core.codec import canonical

    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        directory = tmp_path / 'client-v5'
        client = MsgClient(
            ClientState(directory, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('cli-search-v5')).status == 'ok'
        refs = []
        for name in ('gadget-one', 'gadget-two'):
            created = await client.call(
                'content.post_create', {'parent': '/main', 'name': name, 'body': 'body'}
            )
            assert created.status == 'ok'
            refs.append({'id': created.resources[0].id})
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
                *command,
            ])
            status = await cli.run(args)
            return status, loads(capsys.readouterr().out.encode().strip())

        scope = canonical({'resource_refs': refs}).decode()
        status, first = await invoke(
            'search',
            scope,
            'gadget',
            '--field',
            'title',
            '--limit',
            '1',
            '--facet',
            'type',
            '--spell',
            '--fields',
            'id,title',
        )
        assert status == 0 and first['status'] == 'ok', first
        assert first['data']['spelling']['source'] == 'name'
        assert 'title' in first['data']['items'][0]
        assert first['data']['facets'] == {'type': [{'value': 'post', 'count': 2}]}
        status, second = await invoke('search', '--cursor', first['data']['cursor'])
        assert status == 0 and second['status'] == 'ok', second
        assert first['data']['items'][0]['id'] != second['data']['items'][0]['id']
        with pytest.raises(Failure, match='cursor_query_mismatch'):
            await invoke('search', '--cursor', first['data']['cursor'], '--spell')
        payload, tag = first['data']['cursor'].split('.')
        tampered = payload + '.' + ('A' if tag[0] != 'A' else 'B') + tag[1:]
        status, denied = await invoke('search', '--cursor', tampered)
        assert status != 0 and denied['error']['code'] == 'invalid_cursor'
        status, negative = await invoke('search', scope, 'gadget', '--no-replies')
        assert status == 0 and len(negative['data']['items']) == 2


def test_cli_search_grep_reject_unbounded_or_mixed_options():
    p = cli.parser()
    parsed = p.parse_args(['search', '--cursor', 'opaque', '--limit', '100'])
    assert parsed.cursor == 'opaque' and parsed.limit == 100
    with pytest.raises(SystemExit):
        p.parse_args(['grep', '/main', 'needle', '--count-only', '--files-with-matches'])
