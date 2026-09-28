"""Following projects existing watches; reads cannot publish or retain private refs."""
from datetime import timedelta
from io import StringIO

import httpx
import pytest

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.http import create_app
from msg.tui import TerminalUI
from test_service import NOW, call, register
from test_tui import FakeClient
from read_only_evidence import business_snapshot, readonly_evidence


async def following_setup(app, count=3):
    author_key, author, _ = await register(app, 'following-author')
    reader_key, reader, _ = await register(app, 'following-reader')
    posts = []
    for i in range(count):
        post = await call(app, 'content.post_create', {'parent': '/main', 'body': f'Follow {i}'},
                          key=author_key, subject=author)
        assert post.status == 'ok', wire(post)
        posts.append(post)
        watched = await call(app, 'communication.watch', {'id': post.resources[0].id},
                             key=reader_key, subject=reader)
        assert watched.status == 'ok', wire(watched)
    return author_key, author, reader_key, reader, sorted(posts, key=lambda p: p.resources[0].id)


@pytest.mark.asyncio
async def test_following_empty_private_and_zero_business_writes(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'following-empty')
    before = await business_snapshot(app)
    empty = await call(app, 'communication.following', {}, key=key, subject=subject)
    assert empty.status == 'ok', wire(empty)
    assert wire(empty.data) == {'items': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}
    denied = await call(app, 'communication.following', {})
    assert denied.error.code == 'authentication_required', wire(denied)
    assert await business_snapshot(app) == before
    assert app.registry.operation('communication.following').effect == 'read'


@pytest.mark.asyncio
async def test_following_cursor_rechecks_privacy_and_does_not_write(installed):
    app, _ = installed
    ak, author, rk, reader, posts = await following_setup(app)
    first = await call(app, 'communication.following', {'limit': 1}, key=rk, subject=reader)
    assert first.status == 'ok', wire(first)
    assert [r['id'] for r in first.data['items']] == [posts[0].resources[0].id]
    assert first.data['pageInfo']['endCursor'] == first.data['cursor']
    hidden = posts[1]
    changed = await call(app, 'content.chmod', {'id': hidden.resources[0].id, 'mode': '0600'},
                         key=ak, subject=author,
                         expected=((hidden.resources[0].id, hidden.data['generation']),))
    assert changed.status == 'ok', wire(changed)
    before = await business_snapshot(app)
    second = await call(app, 'communication.following', {'cursor': first.data['cursor']},
                        key=rk, subject=reader)
    assert second.status == 'ok', wire(second)
    assert [r['id'] for r in second.data['items']] == [posts[2].resources[0].id]
    assert second.data['pageInfo'] == {'hasNextPage': False, 'endCursor': None}
    assert 'cursor' not in second.data and hidden.resources[0].id not in str(wire(second))
    assert set(second.data['items'][0]) == {'id', 'type', 'name', 'path', 'revision'}
    mixed = await call(app, 'communication.following', {'cursor': first.data['cursor'], 'limit': 2},
                       key=rk, subject=reader)
    assert mixed.error.code == 'cursor_query_mismatch', wire(mixed)
    other = await call(app, 'communication.following', {'cursor': first.data['cursor']},
                       key=ak, subject=author)
    assert other.error.code == 'cursor_principal_mismatch', wire(other)
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_following_unwatch_and_archive_filter_without_deleting_watch_rows(installed):
    app, _ = installed
    ak, author, rk, reader, posts = await following_setup(app, 2)
    removed = await call(app, 'communication.unwatch', {'id': posts[0].resources[0].id},
                         key=rk, subject=reader)
    archived = await call(app, 'content.archive', {'id': posts[1].resources[0].id},
                          key=ak, subject=author,
                          expected=((posts[1].resources[0].id, posts[1].data['generation']),))
    assert removed.status == archived.status == 'ok'
    before = await business_snapshot(app)
    page = await call(app, 'communication.following', {}, key=rk, subject=reader)
    assert page.status == 'ok' and wire(page.data['items']) == [], wire(page)
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_following_real_http_head_cache_cursor_and_wrong_subject(installed):
    app, _ = installed
    ak, author, rk, reader, posts = await following_setup(app, 2)
    def headers(args):
        request = request_for('communication.following', args, app.settings.service_url,
                              signer=rk, subject=reader, expires_at=NOW+timedelta(seconds=120))
        return {'X-Msg-Request': b64(canonical(request))}
    before = await business_snapshot(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        url = '/@following-reader/following?limit=1'
        first = await http.get(url, headers=headers({'limit': 1}))
        assert first.status_code == 200, first.text
        data = first.json()
        assert data['path'] == '/@following-reader/following'
        head = await http.head(url, headers=headers({'limit': 1}))
        assert head.status_code == 200 and head.content == b''
        assert head.headers['etag'] == first.headers['etag']
        cached = await http.get(url, headers={**headers({'limit': 1}),
                                              'If-None-Match': first.headers['etag']})
        assert cached.status_code == 304
        wrong = await http.get('/@following-author/following?limit=1',
                               headers=headers({'limit': 1}))
        assert wrong.status_code == 403 and wrong.json()['error']['code'] == 'permission_denied'
        denied = await http.get(url)
        assert denied.status_code == 401 and denied.json()['error']['code'] == 'authentication_required'
        next_page = await http.get(data['next'], headers=headers({'cursor': data['cursor']}))
        assert next_page.status_code == 200, next_page.text
        assert len(next_page.json()['items']) == 1
        assert next_page.json()['items'][0]['id'] != data['items'][0]['id']
        mutation = await http.post('/@following-reader/following', json={})
        assert mutation.status_code == 405
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_following_tui_is_explicit_paginated_and_never_watches():
    client = FakeClient(subject='u_reader')
    client.responses['communication.following'] = [
        {'items': [{'id': 'p_one'}], 'cursor': 'opaque'}, {'items': []}]
    ui = TerminalUI(client, stdout=StringIO())
    await ui.command('following')
    assert client.calls == [('communication.following', {'limit': 20})]
    await ui.command('next')
    assert client.calls[-1] == ('communication.following', {'cursor': 'opaque'})
    assert len(client.calls) == 2
    client.state.subject = None
    await ui.command('following')
    assert ui.page is None and len(client.calls) == 2


@pytest.mark.asyncio
async def test_following_transport_parity_and_external_effect_traps(installed, monkeypatch):
    from msg.transports.client import HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport
    app, _ = installed
    _, _, key, subject, _ = await following_setup(app, 1)
    async with readonly_evidence(app, monkeypatch):
        expected = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                     base_url=app.settings.service_url) as http:
            for kind in (HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport):
                transport = kind(app.settings.service_url, http=http)
                packet = request_for('communication.following', {'limit': 20}, app.settings.service_url,
                                     signer=key, subject=subject, expires_at=NOW+timedelta(seconds=120))
                result = await transport.call(packet)
                assert result.status == 'ok', wire(result)
                if expected is None:
                    expected = wire(result.data)
                assert wire(result.data) == expected


@pytest.mark.asyncio
async def test_following_budgets_fail_closed_and_leave_all_facts_unchanged(installed, monkeypatch):
    from msg.core import read_query
    app, _ = installed
    _, _, key, subject, _ = await following_setup(app, 2)
    before = await business_snapshot(app)
    monkeypatch.setattr(read_query, 'MAX_READ_SCANNED', 1)
    result = await call(app, 'communication.following', {}, key=key, subject=subject)
    assert result.error.code == 'query_cost_exceeded', wire(result)
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_following_warm_etag_is_not_authority_after_revoke(installed):
    app, _ = installed
    ak, author, rk, reader, posts = await following_setup(app, 1)
    packet = request_for('communication.following', {}, app.settings.service_url,
                         signer=rk, subject=reader, expires_at=NOW+timedelta(seconds=120))
    headers = {'X-Msg-Request': b64(canonical(packet))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        first = await http.get('/@following-reader/following', headers=headers)
        assert first.status_code == 200 and len(first.json()['items']) == 1
        post = posts[0]
        changed = await call(app, 'content.chmod', {'id': post.resources[0].id, 'mode': '0600'},
                             key=ak, subject=author,
                             expected=((post.resources[0].id, post.data['generation']),))
        assert changed.status == 'ok'
        before = await business_snapshot(app)
        stale = await http.get('/@following-reader/following',
                               headers={**headers, 'If-None-Match': first.headers['etag']})
        assert stale.status_code == 200 and stale.json()['items'] == []
        assert post.resources[0].id not in stale.text
        assert stale.headers['etag'] != first.headers['etag']
        assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_cli_following_signs_and_consumes_only_one_page(installed, tmp_path, monkeypatch, capsys):
    from msg import cli
    from msg.client import ClientState, MsgClient
    from msg.core.codec import loads
    from msg.transports.client import HTTPTransport
    app, _ = installed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        directory = tmp_path / 'following-client'
        client = MsgClient(ClientState(directory, server=app.settings.service_url),
                           HTTPTransport(app.settings.service_url, http=http), clock=lambda: NOW)
        assert (await client.register('cli-following')).status == 'ok'
        for i in range(2):
            post = await client.call('content.post_create', {'parent': '/main', 'body': f'Watched {i}'})
            assert post.status == 'ok'
            assert (await client.call('communication.watch', {'id': post.resources[0].id})).status == 'ok'
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        monkeypatch.setattr(cli, 'MsgClient', lambda state, transport:
                            MsgClient(state, transport, clock=lambda: NOW))
        async def invoke(*command):
            args = cli.parser().parse_args(['--config-dir', str(directory),
                                            '--server', app.settings.service_url, *command])
            status = await cli.run(args)
            return status, loads(capsys.readouterr().out.encode().strip())
        before = await business_snapshot(app)
        status, first = await invoke('following', '--limit', '1')
        assert status == 0 and len(first['data']['items']) == 1
        status, second = await invoke('following', '--cursor', first['data']['cursor'])
        assert status == 0 and len(second['data']['items']) == 1
        assert first['data']['items'][0]['id'] != second['data']['items'][0]['id']
        assert second['data']['pageInfo']['hasNextPage'] is False
        assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_following_doctor_is_read_only_and_reports_the_real_contract(installed):
    from msg.admin.diagnostics import doctor
    app, _ = installed
    before = await business_snapshot(app)
    report = doctor(app.settings.config_dir, clock=lambda: NOW)
    assert report['checks']['following'] == {
        'ok': True, 'default': 'empty', 'read_only': True, 'max_page_size': 100}
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_following_cursor_ttl_and_closed_query_revalidated(installed):
    app, _ = installed
    _, _, key, subject, _ = await following_setup(app, 2)
    first = await call(app, 'communication.following', {'limit': 1}, key=key, subject=subject)
    saved = app.cursors.inspect(first.data['cursor'])
    position = saved['position']
    before = await business_snapshot(app)
    expired = app.cursors.encode_page('communication.following', {'limit': 1}, position['last'],
        NOW, position['principal'], NOW-timedelta(seconds=1))
    denied = await call(app, 'communication.following', {'cursor': expired}, key=key, subject=subject)
    assert denied.error.code == 'cursor_expired', wire(denied)
    unknown = app.cursors.encode_page('communication.following', {'limit': 1, 'constraint': 'all'},
        position['last'], NOW, position['principal'], NOW+timedelta(seconds=60))
    denied = await call(app, 'communication.following', {'cursor': unknown}, key=key, subject=subject)
    assert denied.error.code == 'schema_validation', wire(denied)
    assert await business_snapshot(app) == before
