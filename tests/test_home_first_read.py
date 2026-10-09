"""First browser reads must not wait for a full-site statistics snapshot."""

import asyncio
from dataclasses import replace

import pytest
from test_home_cache import cached_home as cached_home
from test_service import call, register

from msg.core.identifiers import hex_id
from msg.transports.home_page import home_html


async def create_posts(app):
    key, subject, _ = await register(app, 'first-read-author')
    visible = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '# First visit post\n\nReadable without reloading.'},
        key=key,
        subject=subject,
    )
    private = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '# PRIVATE FIRST READ\n\nNot public.'},
        key=key,
        subject=subject,
    )
    assert visible.status == private.status == 'ok'
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(private.resources[0].id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1),
            resource.generation,
        )
    return '/*' + hex_id(visible.resources[0].id)


@pytest.mark.asyncio
async def test_cold_summary_keeps_posts_in_first_response(cached_home, monkeypatch):
    app, cache, http = cached_home
    path = await create_posts(app)
    ready = asyncio.Event()
    loader = cache.loader
    queries = []
    execute = app.executor.execute

    async def delayed():
        await ready.wait()
        return await loader()

    async def observed(packet, **kwargs):
        if packet.operation == 'discovery.read_query':
            queries.append(packet.contract_version)
        return await execute(packet, **kwargs)

    cache.loader = delayed
    cache.cold_wait = 0.001
    monkeypatch.setattr(app.executor, 'execute', observed)
    first = await http.get('/', headers={'Accept': 'text/html'})
    assert first.status_code == 200
    assert first.headers['x-msg-home-snapshot'] == 'unavailable'
    assert not cache.pending.done()
    assert f'href="{path}">First visit post</a>' in first.text
    assert 'PRIVATE FIRST READ' not in first.text
    assert queries.count(5) == 1
    assert first.headers['cache-control'] == 'private, no-store'
    assert 'etag' not in first.headers
    assert '<strong>0</strong>' not in first.text
    assert 'location.reload' not in first.text
    ready.set()
    await cache.pending
    # A direct post read also returns its content on the first request.
    post = await http.get(path, headers={'Accept': 'text/html'})
    assert post.status_code == 200
    assert '<p>Readable without reloading.</p>' in post.text


@pytest.mark.asyncio
async def test_failed_summary_keeps_posts_and_raw_does_not_read_browser_index(
    cached_home, monkeypatch
):
    app, cache, http = cached_home
    path = await create_posts(app)

    async def failed():
        return None, None

    cache.loader = failed
    first = await http.get('/', headers={'Accept': 'text/html'})
    assert f'href="{path}">First visit post</a>' in first.text
    assert 'PRIVATE FIRST READ' not in first.text
    assert first.headers['cache-control'] == 'private, no-store'
    assert '<p class="empty-state" data-i18n="no_posts">' not in first.text
    execute = app.executor.execute

    async def no_browser_index(packet, **kwargs):
        assert packet.operation != 'discovery.read_query' or packet.contract_version != 5
        return await execute(packet, **kwargs)

    monkeypatch.setattr(app.executor, 'execute', no_browser_index)
    raw = await http.get('/?format=raw', headers={'Accept': 'text/html'})
    assert raw.status_code == 200
    assert raw.headers['content-type'].startswith('text/plain')
    assert '<html' not in raw.text


def test_independent_index_escapes_content_and_precedes_statistics():
    item = {
        'name': 'post',
        'title': '<script>not executable</script>',
        'excerpt': '<img src=x onerror=alert(1)>',
        'path': '/main/post',
        'created_at': '2026-10-09T14:00:00Z',
        'view_count': 2,
    }
    page = home_html(None, post_index={'items': [item]}).decode().split('<script>')[0]
    assert '&lt;script&gt;not executable&lt;/script&gt;' in page
    assert '&lt;img src=x onerror=alert(1)&gt;' in page
    assert '<img src=x' not in page
    assert '<strong>0</strong>' not in page
    data = {
        'posts': 3,
        'posts_today': 1,
        'users': 2,
        'date': '2026-10-09',
        'timezone': 'Asia/Taipei',
        'latest': [],
    }
    page = home_html(data, post_index={'items': [item]}).decode()
    assert page.index('class="latest"') < page.index('class="activity"')


@pytest.mark.parametrize('available', [True, False])
def test_empty_index_is_not_confused_with_failed_index(available):
    index = {'items': []} if available else {'items': [], 'error': 'server_busy'}
    page = home_html(None, post_index=index).decode().split('<script>')[0]
    assert ('No public posts yet.' in page) is available
    assert ('主帖列表暂时无法读取' in page) is not available
