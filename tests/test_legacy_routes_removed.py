"""Old public operation URLs must not remain executable HTTP routes."""
from datetime import timedelta
from dataclasses import replace
import gzip

import httpx
import pytest

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.dictionary import build_dictionary
from msg.transports.http import create_app
from test_service import NOW, register

BUSINESS_TABLES = ('resources', 'events', 'audit', 'jobs', 'results', 'transfers',
                   'credentials', 'reactions', 'watches')


async def business_counts(app):
    async with app.metadata.transaction(write=False) as tx:
        return {table: tx.one(f'SELECT COUNT(*) FROM {table}')[0]
                for table in BUSINESS_TABLES}


@pytest.mark.asyncio
async def test_legacy_operation_paths_are_not_routes(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'legacy-routes')
    write = request_for('content.post_create', {'parent': '/main', 'body': 'must not appear'},
                        app.settings.service_url, signer=key, subject=uid,
                        request_id='removed-route', expires_at=NOW + timedelta(seconds=90))
    read = request_for('discovery.get', {'id': '/main'}, app.settings.service_url)
    paths = (
        '/!content.post_create',
        '/!content.post_create/schema',
        '/!content.post_create/run/j/' + b64(canonical(write)),
        '/~discovery.get',
        '/~discovery.get/schema',
        '/~discovery.get/run/j/' + b64(canonical(read)),
        '/~discovery.get/run/gz/' + b64(gzip.compress(canonical(read), mtime=0)),
        '/run/j/' + b64(canonical(write)),
        '/run/gz/' + b64(gzip.compress(canonical(write), mtime=0)),
    )
    before = await business_counts(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        for path in paths:
            for method in ('GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'OPTIONS'):
                response = await http.request(method, path + '?arguments=%7B%7D',
                                              headers={'X-Msg-Request': b64(canonical(read))})
                assert response.status_code == 404, (method, path, response.text)
        assert (await http.post('/mcp', content=b'{}')).status_code == 404
    assert await business_counts(app) == before


@pytest.mark.asyncio
async def test_protocol_prefix_and_dot_segments_cannot_alias_a_write(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'protocol-alias')
    packet = request_for('content.post_create', {'parent': '/main', 'body': 'alias write'},
                         app.settings.service_url, signer=key, subject=uid,
                         request_id='aliased-route', expires_at=NOW + timedelta(seconds=90))
    encoded = b64(canonical(packet))
    paths = (
        '/%2D/g/content.post_create/j/' + encoded,
        '/-/%67/content.post_create/j/' + encoded,
        '/-/g%2Fcontent.post_create/j/' + encoded,
        '/-/g/content%2Epost_create/j/' + encoded,
        '/-/g/content.post_create/%6A/' + encoded,
        '/-/%2E/g/content.post_create/j/' + encoded,
        '/-/%2E%2E/-/g/content.post_create/j/' + encoded,
    )
    before = await business_counts(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        for path in paths:
            response = await http.get(path)
            assert response.status_code == 404, (path, response.text)
        for path in ('/main', '/rss', '/latest/post', '/!content.post_create'):
            response = await http.post(path, content=canonical(packet),
                                       headers={'X-HTTP-Method-Override': 'GET'})
            assert response.status_code == 405, (path, response.text)
        for header in ('X-HTTP-Method-Override', 'X-Method-Override'):
            for method, path in (('GET', '/-/g/content.post_create/j/' + encoded),
                                 ('POST', '/-/p/content.post_create')):
                response = await http.request(method, path, content=canonical(packet),
                                              headers={header: 'GET'})
                assert response.status_code == 405, (method, path, header, response.text)
        read_code = build_dictionary(app.registry).code_for('operation', 'discovery.get@1')
        direct_read = await http.get(f'/-/g/{read_code}/%2Fmain')
        assert direct_read.status_code == 200, direct_read.text
    assert await business_counts(app) == before


@pytest.mark.asyncio
async def test_public_views_reject_write_methods(installed):
    app, _ = installed
    paths = ('/', '/main', '/rss', '/latest/post', '/_id/missing/raw',
             '/@missing/repo.git/git-receive-pack')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        for path in paths:
            for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
                response = await http.request(method, path)
                assert response.status_code == 405, (method, path, response.text)


@pytest.mark.asyncio
async def test_public_views_require_read_effects(installed, monkeypatch):
    app, _ = installed
    routes = (
        ('/rss', 'discovery.feed'),
        ('/latest/post', 'discovery.list'),
        ('/main', 'discovery.get'),
        ('/_id/missing/raw', 'discovery.raw'),
        ('/missing/raw', 'discovery.raw'),
        ('/_id/missing/json', 'discovery.get'),
        ('/missing.md', 'discovery.get'),
        ('/@missing/repo.git/info/refs?service=git-upload-pack', 'git.refs'),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        for path, operation in routes:
            key = (operation, 1)
            original = app.registry._operations[key]
            monkeypatch.setitem(app.registry._operations, key,
                                replace(original, effect='transaction'))
            try:
                response = await http.get(path)
                assert response.status_code == 405, (path, response.text)
                assert response.json()['error']['code'] == 'effect_mismatch'
            finally:
                monkeypatch.setitem(app.registry._operations, key, original)
