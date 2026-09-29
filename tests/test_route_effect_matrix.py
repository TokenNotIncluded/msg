"""Executable ordinary-route matrix: reads and rejected writes preserve state."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.http import RouteEffect, classify_route, create_app


async def database_snapshot(app):
    """Compare complete rows, so a read counter or an updated ACK cannot hide."""
    async with app.metadata.transaction(write=False) as tx:
        tables = [
            row[0]
            for row in tx.rows(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename<>'schema_version' ORDER BY tablename"
            )
        ]
        return {
            table: sorted(repr(tuple(row)) for row in tx.rows(f'SELECT * FROM {table}'))
            for table in tables
        }


@pytest.mark.asyncio
async def test_ordinary_read_and_failed_route_matrix_preserves_all_authority_rows(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'route-effect-owner')
    post = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'name': 'effect-probe', 'body': 'route effect marker'},
        key=key,
        subject=subject,
    )
    repo = await call(
        app,
        'git.create',
        {'parent': '/@route-effect-owner', 'name': 'code.git'},
        key=key,
        subject=subject,
    )
    assert post.status == repo.status == 'ok'
    rid = post.resources[0].id
    repo_id = repo.resources[0].id
    read_repo = repo.data['read_url'].removeprefix(app.settings.service_url.rstrip('/'))
    write = request_for(
        'content.post_create',
        {'parent': '/main', 'body': 'must never be written by an ordinary path'},
        app.settings.service_url,
        signer=key,
        subject=subject,
        request_id='ordinary-route-matrix',
        expires_at=NOW + timedelta(seconds=90),
    )
    encoded = b64(canonical(write))
    before = await database_snapshot(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url='http://testserver',
        follow_redirects=False,
    ) as http:
        reads = (
            ('GET', '/', 200),
            ('GET', '/main', 200),
            ('HEAD', '/main', 200),
            ('GET', '/main/effect-probe', 308),
            ('HEAD', '/main/effect-probe', 308),
            ('GET', f'/_read/{rid}/json', 200),
            ('HEAD', f'/_r/{rid}/meta', 200),
            ('GET', '/rss', 200),
            ('GET', '/latest/post', 200),
            ('GET', '/_search?query=route', 200),
            ('GET', '/_s/q/2/s/%2Fmain/t/route/f/b/fc/type,tag', 200),
            ('GET', f'{read_repo}/info/refs?service=git-upload-pack', 200),
        )
        for method, path, status in reads:
            response = await http.request(method, path)
            assert response.status_code == status, (method, path, response.text)
        facets = await http.get('/_search?scope=%2Fmain&terms=route&facets=type,tag')
        assert facets.status_code == 200, facets.text
        assert set(facets.json()['facets']) == {'type', 'tag'}
        for path in (
            '/_search?scope=%2Fmain&terms=route&facets=type,type',
            '/_s/q/2/s/%2Fmain/t/route/fc/invalid',
        ):
            response = await http.get(path)
            assert response.status_code == 400, (path, response.text)
        batch = {'operation': 'download', 'objects': [{'oid': '0' * 64, 'size': 4}]}
        lfs = await http.post(
            read_repo + '/info/lfs/objects/batch',
            json=batch,
            headers={'Content-Type': 'application/vnd.git-lfs+json'},
        )
        assert lfs.status_code == 200, lfs.text
        assert lfs.json()['objects'][0]['error']['code'] == 404
        upload = await http.post(
            read_repo + '/info/lfs/objects/batch',
            json={**batch, 'operation': 'upload'},
            headers={'Content-Type': 'application/vnd.git-lfs+json'},
        )
        assert upload.status_code == 405
        rejected = (
            ('POST', '/main'),
            ('PUT', '/main/effect-probe'),
            ('POST', '/_read/graphql'),
            ('POST', '/@route-effect-owner/code.git/git-receive-pack'),
            ('GET', '/!content.post_create/run/j/' + encoded),
            ('POST', '/!content.post_create/run/j/' + encoded),
            ('GET', '/run/j/' + encoded),
            ('POST', '/run/j/' + encoded),
            ('GET', '/~content.post_create/run/j/' + encoded),
        )
        for method, path in rejected:
            response = await http.request(method, path, content=canonical(write))
            assert response.status_code in {400, 404, 405}, (method, path, response.text)
        gql = {'query': 'mutation($p: JSON!) { call(packet: $p) }', 'variables': {'p': wire(write)}}
        for path in ('/_read/graphql', '/_r/graphql'):
            response = await http.post(path, json=gql)
            assert (
                response.status_code == 400
                and response.json()['error']['code'] == 'graphql_effect_mismatch'
            )
        signed_read = request_for(
            'discovery.get',
            {'id': rid},
            app.settings.service_url,
            signer=key,
            subject=subject,
            expires_at=NOW + timedelta(seconds=90),
        )
        invalid = await http.get(
            f'/_read/{rid}/json', headers={'X-Msg-Request': b64(canonical(write))}
        )
        assert invalid.status_code == 400
        good = await http.get(
            f'/_read/{rid}/json', headers={'X-Msg-Request': b64(canonical(signed_read))}
        )
        assert good.status_code == 200
    assert await database_snapshot(app) == before
    assert not app.settings.server.repositories_dir.joinpath(repo_id + '.git', 'lfs').exists()


@pytest.mark.asyncio
async def test_effect_classification_matches_native_git_and_lfs_boundaries(installed):
    app, _ = installed
    cases = {
        ('/main', 'POST'): RouteEffect.PURE_READ,
        ('/@owner/code.git/git-upload-pack', 'POST'): RouteEffect.PURE_READ,
        ('/@owner/code.git/info/lfs/objects/batch', 'POST'): RouteEffect.PURE_READ,
        ('/-/git/repo/info/lfs/objects/batch', 'POST'): RouteEffect.PURE_READ,
        ('/-/git/repo/info/lfs/objects/' + 'a' * 64 + '/10', 'PUT'): RouteEffect.EXTERNAL_EFFECT,
        ('/-/git/repo/git-receive-pack', 'POST'): RouteEffect.EXTERNAL_EFFECT,
        ('/-/git/repo/info/refs', 'GET'): RouteEffect.PURE_READ,
        ('/-/graphql', 'POST'): RouteEffect.EXTERNAL_EFFECT,
        ('/-/mcp', 'POST'): RouteEffect.EXTERNAL_EFFECT,
        ('/-/transfer', 'POST'): RouteEffect.BUSINESS_WRITE,
        ('/-/p/content.post_create', 'POST'): RouteEffect.BUSINESS_WRITE,
    }
    for (path, method), effect in cases.items():
        assert classify_route(path, method, app.registry).effect is effect, (method, path)
