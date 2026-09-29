"""Versioned path-only reads compile the same bounded query as query strings."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_read_query_path_aliases_match_query_string_content_etag_and_cursor(installed):
    app, _ = installed
    key, user, _ = await register(app, 'path-query-owner')
    for index in range(3):
        created = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': f'path item {index}'},
            key=key,
            subject=user,
        )
        assert created.status == 'ok'
    suffix = '/q/1/r/%2Fmain/t/post/s/n/f/i,n/n/2'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        query = await http.get(
            '/_read/query?root=%2Fmain&filter=type:post&sort=name&select=id,name&first=2'
        )
        long = await http.get('/_read' + suffix)
        short = await http.get('/_r' + suffix)
        assert query.status_code == long.status_code == short.status_code == 200
        assert query.content == long.content == short.content
        assert query.headers['etag'] == long.headers['etag'] == short.headers['etag']
        assert query.json()['next'].startswith('/_r/c/')
        assert not long.is_redirect and not short.is_redirect
        cursor = query.json()['next'].removeprefix('/_r/c/')
        via_path = await http.get('/_r/q/1/a/' + cursor)
        via_query = await http.get('/_read/query?after=' + cursor)
        assert via_path.status_code == via_query.status_code == 200
        assert via_path.content == via_query.content
        cached = await http.get('/_r' + suffix, headers={'If-None-Match': query.headers['etag']})
        assert cached.status_code == 304
        dictionary = await http.get('/-/d/read.query')
        assert dictionary.status_code == 200
        assert dictionary.json()['version'] == 1
        assert dictionary.json()['segments']['root'] == 'r'


@pytest.mark.asyncio
async def test_private_path_query_and_continuation_use_path_signature_without_header(installed):
    app, _ = installed
    key, user, _ = await register(app, 'path-private-owner')
    topic = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'path-private'},
        key=key,
        subject=user,
    )
    rid = topic.resources[0].id
    for index in range(2):
        await call(
            app, 'content.post_create', {'parent': rid, 'body': str(index)}, key=key, subject=user
        )
    await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0700'},
        key=key,
        subject=user,
        expected=((rid, topic.data['generation']),),
    )
    args = {'parent': rid, 'limit': 1, 'fields': ['id', 'name'], 'sort': 'id'}
    packet = request_for(
        'discovery.read_query',
        args,
        app.settings.service_url,
        signer=key,
        subject=user,
        expires_at=NOW + timedelta(seconds=60),
    )
    path = f'/_r/q/1/r/{rid}/s/i/f/i,n/n/1'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        denied = await http.get(path)
        assert denied.status_code == 403
        header = await http.get(
            f'/_read/query?root={rid}&first=1&select=id,name&sort=id',
            headers={'X-Msg-Request': b64(canonical(packet))},
        )
        path_only = await http.get(path + '/p/' + b64(canonical(packet)))
        assert path_only.status_code == header.status_code == 200, (path_only.text, header.text)
        assert path_only.content == header.content
        assert path_only.headers['cache-control'] == 'no-store'
        assert '/p/' not in path_only.json()['next']
        reused = await http.get(path_only.json()['next'] + '/p/' + b64(canonical(packet)))
        assert reused.status_code == 400
        assert reused.json()['error']['code'] == 'representation_mismatch'
        overlong = request_for(
            'discovery.read_query',
            args,
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=120),
        )
        too_long = await http.get(path + '/p/' + b64(canonical(overlong)))
        assert too_long.status_code == 400
        assert too_long.json()['error']['code'] == 'path_proof_expiry'
        bearer = request_for(
            'discovery.read_query',
            args,
            app.settings.service_url,
            token=('t_unused', b'x' * 32),
            subject=user,
            expires_at=NOW + timedelta(seconds=60),
        )
        unsafe = await http.get(path + '/p/' + b64(canonical(bearer)))
        assert unsafe.status_code == 400
        assert unsafe.json()['error']['code'] == 'path_signature_required'
        cursor = path_only.json()['next'].removeprefix('/_r/c/')
        continuation = request_for(
            'discovery.read_query',
            {'cursor': cursor},
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=60),
        )
        resumed = await http.get('/_r/c/' + cursor + '/p/' + b64(canonical(continuation)))
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()['items'][0]['id'] != path_only.json()['items'][0]['id']


@pytest.mark.asyncio
async def test_path_query_rejects_ambiguous_or_unknown_segments(installed):
    app, _ = installed
    paths = (
        ('/_read/q/1/r/%2fmain', 'invalid_path'),
        ('/_read/q/1/r/%2Fmain/r/%2Fmain', 'duplicate_query_parameter'),
        ('/_r/q/1/z/x', 'unknown_query_parameter'),
        ('/_r/q/1/r/%2Fmain/n/101', 'query_cost_exceeded'),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        for path, code in paths:
            result = await http.get(path)
            assert result.status_code == 400, (path, result.text)
            assert result.json()['error']['code'] == code


@pytest.mark.asyncio
async def test_search_path_matches_query_string_with_cursor_and_no_business_write(installed):
    app, _ = installed
    key, user, _ = await register(app, 'path-search-owner')
    for index in range(3):
        await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': f'path-search-needle {index}'},
            key=key,
            subject=user,
        )
    async with app.metadata.transaction(write=False) as tx:
        before = (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT SUM(generation) FROM resources')[0],
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        query = await http.get('/_search?query=path-search-needle&limit=2')
        long = await http.get('/_search/q/1/q/path-search-needle/n/2')
        short = await http.get('/_s/q/1/q/path-search-needle/n/2')
        assert query.status_code == long.status_code == short.status_code == 200
        assert query.content == long.content == short.content
        assert query.headers['etag'] == long.headers['etag'] == short.headers['etag']
        assert query.json().get('cursor')
        assert query.json()['next'].startswith('/_s/q/1/')
        continued = await http.get(query.json()['next'])
        assert continued.status_code == 200
        assert continued.json()['items'][0]['id'] not in {
            item['id'] for item in query.json()['items']
        }
    async with app.metadata.transaction(write=False) as tx:
        assert before == (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT SUM(generation) FROM resources')[0],
        )


@pytest.mark.asyncio
async def test_private_search_path_accepts_short_lived_signature_without_header(installed):
    app, _ = installed
    key, user, _ = await register(app, 'path-search-private')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'needle-private-path'},
        key=key,
        subject=user,
    )
    rid = created.resources[0].id
    locked = await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0600'},
        key=key,
        subject=user,
        expected=((rid, created.data['generation']),),
    )
    assert locked.status == 'ok'
    args = {'limit': 50, 'query': 'needle-private-path'}
    packet = request_for(
        'discovery.search',
        args,
        app.settings.service_url,
        signer=key,
        subject=user,
        expires_at=NOW + timedelta(seconds=60),
    )
    path = '/_s/q/1/q/needle-private-path'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        anonymous = await http.get(path)
        assert anonymous.status_code == 200 and anonymous.json()['items'] == []
        signed = await http.get(path + '/p/' + b64(canonical(packet)))
        header = await http.get(
            '/_search?query=needle-private-path', headers={'X-Msg-Request': b64(canonical(packet))}
        )
        assert signed.status_code == header.status_code == 200
        assert signed.content == header.content
        assert [item['id'] for item in signed.json()['items']] == [rid]
        assert signed.headers['cache-control'] == 'no-store'
