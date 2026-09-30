"""Deployments reject names the hosted HTTP boundary cannot address."""

import httpx
import pytest
from test_service import call, register

from msg.core.codec import b64, wire
from msg.transports.http import create_app


@pytest.mark.parametrize('operation', ['hosting.deploy', 'hosting.preview'])
@pytest.mark.asyncio
async def test_unaddressable_paths_are_rejected_before_materialization(installed, operation):
    app, _ = installed
    key, user, _ = await register(app, 'path-owner')
    site = await call(
        app, 'hosting.create', {'parent': '/@path-owner', 'name': 'web'}, key=key, subject=user
    )
    source = await call(
        app,
        'content.file_put',
        {
            'parent': '/@path-owner/files',
            'name': 'page.html',
            'data': b64(b'<h1>addressable</h1>'),
            'media_type': 'text/html',
        },
        key=key,
        subject=user,
    )
    assert site.status == source.status == 'ok'
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('resources', 'revisions', 'settings')
        )
    for path in (
        'hello world.html',
        '中文.html',
        'a%20b.html',
        'a#b.html',
        'a?b.html',
        '_preview/fake/index.html',
        '_rev/fake/index.html',
    ):
        result = await call(
            app,
            operation,
            {
                'id': site.resources[0].id,
                'entries': [{'path': path, 'source': wire(source.resources[0])}],
            },
            key=key,
            subject=user,
            expected=((site.resources[0].id, site.data['generation']),),
        )
        assert result.status == 'error' and result.error.code == 'invalid_hosting_path', wire(
            result
        )
        async with app.metadata.transaction(write=False) as tx:
            assert (
                tuple(
                    tx.one(f'SELECT COUNT(*) FROM {table}')[0]
                    for table in ('resources', 'revisions', 'settings')
                )
                == before
            )


@pytest.mark.parametrize('path', ["assets/v1/page-_.~!$&'()*+,;=:@.html", '_rev', '_preview'])
@pytest.mark.asyncio
async def test_uri_safe_paths_remain_reachable(installed, path):
    app, _ = installed
    key, user, _ = await register(app, 'safe-path-owner')
    site = await call(
        app, 'hosting.create', {'parent': '/@safe-path-owner', 'name': 'web'}, key=key, subject=user
    )
    html = b'<h1>nested path</h1>'
    source = await call(
        app,
        'content.file_put',
        {
            'parent': '/@safe-path-owner/files',
            'name': 'page.html',
            'data': b64(html),
            'media_type': 'text/html',
        },
        key=key,
        subject=user,
    )
    deployed = await call(
        app,
        'hosting.deploy',
        {
            'id': site.resources[0].id,
            'entries': [{'path': path, 'source': wire(source.resources[0])}],
        },
        key=key,
        subject=user,
        expected=((site.resources[0].id, site.data['generation']),),
    )
    assert deployed.status == 'ok', wire(deployed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        served = await http.get('/@safe-path-owner/web/' + path)
    assert served.status_code == 200 and served.content == html
