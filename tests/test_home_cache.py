"""Public snapshots stay bounded while content authority remains live."""

import asyncio
from dataclasses import replace

import httpx
import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.core.errors import Failure
from msg.transports.home_cache import HomeReadSession, PublicHomeCache, current_snapshot
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_cold_wait_is_bounded_and_concurrent_reads_have_one_loader():
    ready = asyncio.Event()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        await ready.wait()
        return {'posts': calls}, {'quota': None}

    cache = PublicHomeCache(load, cold_wait=0.01)
    try:
        results = await asyncio.gather(*(cache.get() for _ in range(12)))
        assert all(value.data is None for value in results)
        assert calls == 1 and not cache.pending.done()
        ready.set()
        await cache.pending
        assert (await cache.get()).data == {'posts': 1}
        assert calls == 1
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_expiry_refreshes_without_blocking_but_never_serves_unbounded_stale():
    now = [10.0]
    ready = asyncio.Event()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        if calls > 1:
            await ready.wait()
        return {'posts': calls}, None

    cache = PublicHomeCache(load, ttl=1, stale=3, cold_wait=0.01, clock=lambda: now[0])
    first = await cache.get()
    assert first.data == {'posts': 1}
    now[0] += 1.1
    assert await cache.get() is first
    await asyncio.sleep(0)
    assert calls == 2
    now[0] += 3
    assert (await cache.get()).data is None
    await cache.close()
    assert cache.pending.cancelled() and (await cache.get()).data is None


@pytest.mark.asyncio
async def test_failed_refresh_keeps_bounded_stale_and_backs_off():
    now = [10.0]
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        return ({'posts': 2}, None) if calls == 1 else (None, None)

    cache = PublicHomeCache(load, ttl=1, stale=3, clock=lambda: now[0])
    try:
        first = await cache.get()
        now[0] += 1.1
        assert await cache.get() is first
        await cache.pending
        for _ in range(3):
            assert await cache.get() is first
        assert calls == 2
        now[0] += 3
        assert (await cache.get()).data is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_oversized_snapshot_is_not_retained():
    async def load():
        return {'text': 'x' * 1000}, None

    cache = PublicHomeCache(load, max_bytes=100)
    try:
        assert (await cache.get()).data is None
        assert cache.snapshot is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_read_memo_is_bounded_request_only_and_live_fences_bypass_it(installed):
    app, _ = installed
    async with app.metadata.transaction(write=False) as tx:
        memo = HomeReadSession(tx, limit=2)
        count = 0
        original = tx.one

        def one(sql, parameters=()):
            nonlocal count
            count += 1
            return original(sql, parameters)

        tx.one = one
        for _ in range(2):
            memo.setting('ordinary-example')
            memo.setting('recovery_runtime_generation')
            memo.setting('recovery_quarantine')
        assert count == 5
        for key in ('another', 'third', 'fourth'):
            memo.setting(key)
        assert len(memo.memo) == 2
    with pytest.raises(Failure, match='transaction_closed'):
        memo.setting('ordinary-example')
    async with app.metadata.transaction(write=True) as tx:
        with pytest.raises(Failure, match='read_only_transaction'):
            HomeReadSession(tx)


@pytest.fixture
async def cached_home(installed):
    app, _ = installed
    asgi = create_app(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi), base_url=app.settings.service_url
    ) as http:
        try:
            yield app, asgi.state.home_cache, http
        finally:
            await asgi.state.home_cache.close()


async def warm(cache):
    await cache.get()
    if cache.pending is not None:
        await cache.pending


@pytest.mark.asyncio
async def test_warm_hits_revalidate_candidates_without_another_full_scan(cached_home):
    app, cache, http = cached_home
    scans = 0
    original = app.executor.execute

    async def execute(packet, **kwargs):
        nonlocal scans
        if packet.operation == 'discovery.read_query' and packet.contract_version == 4:
            scans += current_snapshot() is None
        return await original(packet, **kwargs)

    app.executor.execute = execute
    await warm(cache)
    for _ in range(3):
        response = await http.get('/')
        assert response.status_code == 200
        assert '## Channels' in response.text
    assert scans == 1


@pytest.mark.asyncio
async def test_cached_post_excerpt_never_survives_current_parent_acl_or_edit(cached_home):
    app, cache, http = cached_home
    key, subject, _ = await register(app, 'snapshot-author')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'PUBLIC THEN PRIVATE'},
        key=key,
        subject=subject,
    )
    assert created.status == 'ok'
    await warm(cache)
    before = await http.get('/')
    assert 'PUBLIC THEN PRIVATE' in before.text
    async with app.metadata.transaction(write=True) as tx:
        parent = await tx.resource('t_main')
        await tx.replace(
            replace(parent, mode=0o700, generation=parent.generation + 1), parent.generation
        )
    denied = await http.get('/', headers={'If-None-Match': before.headers['etag']})
    assert denied.status_code == 200 and 'PUBLIC THEN PRIVATE' not in denied.text
    assert '[main](/main)' not in denied.text
    assert denied.headers['etag'] != before.headers['etag']


@pytest.mark.asyncio
async def test_cache_update_visible_without_retaining_old_revision(cached_home):
    app, cache, http = cached_home
    key, subject, _ = await register(app, 'snapshot-editor')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'OLD PREVIEW'},
        key=key,
        subject=subject,
    )
    await warm(cache)
    assert 'OLD PREVIEW' in (await http.get('/')).text
    edited = await call(
        app,
        'content.post_edit',
        {
            'id': created.resources[0].id,
            'body': 'NEW PREVIEW',
            'expected_revision': created.resources[0].revision,
        },
        key=key,
        subject=subject,
        expected=((created.resources[0].id, created.data['generation']),),
    )
    assert edited.status == 'ok', edited.error
    changed = await http.get('/')
    assert 'OLD PREVIEW' not in changed.text
    await warm(cache)
    assert 'NEW PREVIEW' in (await http.get('/')).text


@pytest.mark.asyncio
async def test_home_conditional_get_validates_first_and_cookie_never_reuses_it(cached_home):
    _, cache, http = cached_home
    await warm(cache)
    response = await http.get('/')
    assert response.headers['cache-control'] == 'private, no-cache'
    etag = response.headers['etag']
    matched = await http.get('/', headers={'If-None-Match': 'W/' + etag})
    assert matched.status_code == 304 and not matched.content
    assert 'content-length' not in matched.headers
    cookie = await http.get('/', headers={'Cookie': 'unrelated=value', 'If-None-Match': etag})
    assert cookie.status_code == 200
    assert cookie.headers['cache-control'] == 'private, no-store'
    assert 'etag' not in cookie.headers


@pytest.mark.asyncio
async def test_browser_quota_is_live_and_never_enters_anonymous_snapshot(oauth):
    from msg.plugins.public_board import quota

    app, _, subject, http = oauth
    asgi = http._transport.app
    cache = asgi.state.home_cache
    try:
        await warm(cache)
        assert cache.snapshot.board.get('quota') is None
        await browser_login(oauth)
        for count in (3, 4):
            async with app.metadata.transaction(write=True) as tx:
                record = quota(None, app.clock())
                tx.set_setting(
                    'public_board_quota:' + subject,
                    {
                        **record,
                        'hour_count': count,
                        'day_count': count,
                    },
                )
            page = await http.get('/', headers={'Accept': 'text/html'})
            assert f'本小时已用 {count}/5' in page.text
            assert page.headers['cache-control'] == 'private, no-store'
            assert 'etag' not in page.headers
            assert cache.snapshot.board.get('quota') is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi), base_url=app.settings.service_url
        ) as anonymous:
            page = await anonymous.get('/', headers={'Accept': 'text/html'})
            assert '本小时已用 4/5' not in page.text
            assert 'data-signed-in="false"' in page.text
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_fixed_release_assets_support_etag_and_head(cached_home):
    _, _, http = cached_home
    for path in ('/install', '/favicon.png'):
        response = await http.get(path)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'public, max-age=300'
        matched = await http.get(path, headers={'If-None-Match': response.headers['etag']})
        assert matched.status_code == 304 and not matched.content
        head = await http.head(path)
        assert not head.content and head.headers['content-length'] == str(len(response.content))
