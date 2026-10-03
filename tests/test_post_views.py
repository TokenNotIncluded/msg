"""Browser heat is explicit, approximate, ACL checked and retry safe."""

import asyncio
import re
from datetime import timedelta

import httpx
import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import call

from msg.core.codec import b64, canonical, loads, unb64
from msg.storage.post_view_migration import record_view, view_count
from msg.storage.sqlite import SqliteMetadataStore
from msg.transports.post_views import HASH


async def post(oauth, body='# Browser heat\n\nA readable post.'):
    app, key, subject, _ = oauth
    result = await call(
        app, 'content.post_create', {'parent': '/main', 'body': body}, key=key, subject=subject
    )
    assert result.status == 'ok'
    return result.resources[0].id


async def page_token(http, rid):
    page = await http.get('/_id/' + rid, headers={'Accept': 'text/html'})
    assert page.status_code == 200, page.text
    match = re.search('data-msg-view-token="([^"]+)"', page.text)
    assert match, page.text
    assert 'sha256-' + HASH in page.headers['content-security-policy']
    assert page.headers['cache-control'] == 'private, no-store'
    return match[1], page


async def event(oauth, token, **kwargs):
    app, _, _, http = oauth
    return await http.post(
        '/-/view-event',
        json={'token': token},
        headers={'Origin': app.settings.service_url, **kwargs.pop('headers', {})},
        **kwargs,
    )


@pytest.mark.asyncio
async def test_reads_do_not_count_and_visible_event_dedupes_in_metadata(oauth):
    app, _, _, http = oauth
    rid = await post(oauth)
    token, page = await page_token(http, rid)
    assert 'data-msg-view-count>0</span>' in page.text
    for path in (
        '/_r/' + rid,
        '/_r/' + rid + '/meta',
        '/_r/' + rid + '/json',
        '/_r/' + rid + '?format=raw',
    ):
        await http.get(path)
        await http.head(path, headers={'Accept': 'text/html'})
    initial = await call(app, 'discovery.get', {'id': rid})
    assert initial.data['view_count'] == 0
    first = await event(oauth, token)
    assert first.status_code == 200 and first.json()['view_count'] == 1, first.text
    retry = await event(oauth, token)
    assert retry.json()['view_count'] == 1
    fresh_token, refreshed = await page_token(http, rid)
    assert refreshed.headers.get('set-cookie') is None
    assert (await event(oauth, fresh_token)).json()['view_count'] == 1
    meta = await call(app, 'discovery.get', {'id': rid, 'view': 'meta'})
    assert meta.data['view_count'] == 1
    explicit = await call(app, 'discovery.get', {'id': rid, 'fields': ['id', 'view_count']})
    assert explicit.data == {'id': rid, 'view_count': 1}
    channel = await http.get('/main', headers={'Accept': 'text/html'})
    home = await http.get('/', headers={'Accept': 'text/html'})
    assert '1 浏览 · views' in channel.text and '1 浏览 · views' in home.text
    async with app.metadata.transaction(write=False) as tx:
        rows = tx.rows('SELECT day,visitor_digest FROM post_view_days WHERE resource_id=?', (rid,))
        assert len(rows) == 1 and len(rows[0][1]) == 43
        assert http.cookies.get('msg_view') not in str(rows)


@pytest.mark.asyncio
async def test_concurrent_events_count_each_visitor_once_and_expire_by_taipei_day(oauth):
    app, _, _, http = oauth
    rid = await post(oauth)
    token, _ = await page_token(http, rid)
    duplicates = await asyncio.gather(*(event(oauth, token) for _ in range(8)))
    assert all(
        result.status_code == 200 and result.json()['view_count'] == 1 for result in duplicates
    )
    # A second independent browser cookie counts once; no IP/UA is recorded.
    async with httpx.AsyncClient(
        transport=http._transport, base_url=app.settings.service_url
    ) as other:
        other_token, _ = await page_token(other, rid)
        second = await other.post(
            '/-/view-event',
            json={'token': other_token},
            headers={'Origin': app.settings.service_url},
        )
        assert second.json()['view_count'] == 2
    app._oauth_clock[0] += timedelta(days=1)
    assert (await event(oauth, token)).status_code >= 400
    next_day, _ = await page_token(http, rid)
    assert (await event(oauth, next_day)).json()['view_count'] == 3
    async with app.metadata.transaction(write=False) as tx:
        assert len(tx.rows('SELECT * FROM post_view_days')) == 1


@pytest.mark.asyncio
async def test_event_rejects_forged_binding_origin_private_or_inactive_target(oauth):
    app, key, subject, http = oauth
    rid = await post(oauth)
    token, _ = await page_token(http, rid)
    assert (await event(oauth, token + 'x')).status_code >= 400
    assert (
        await event(oauth, token, headers={'Origin': 'https://evil.example'})
    ).status_code >= 400
    assert (
        await event(oauth, token, headers={'Authorization': 'Bearer invalid'})
    ).status_code >= 400
    assert (await http.get('/-/view-event')).status_code == 405
    payload, signature = token.split('.')
    forged = loads(unb64(payload))
    forged['id'] = 'r_' + '0' * 32
    assert (await event(oauth, b64(canonical(forged)) + '.' + signature)).status_code >= 400
    changed = await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0700'},
        key=key,
        subject=subject,
        expected=((rid, 1),),
    )
    assert changed.status == 'ok', changed.error
    assert (await event(oauth, token)).status_code >= 400
    await browser_login(oauth)
    private_token, _ = await page_token(http, rid)
    assert (await event(oauth, private_token)).json()['view_count'] == 1
    async with app.metadata.transaction(write=True) as tx:
        tx.execute("UPDATE resources SET state='archived' WHERE id=?", (rid,), write=True)
        from dataclasses import replace

        resource = await tx.resource(rid)
        # Keep the typed resource's active state synchronized, as real writes do.
        tx.execute(
            'UPDATE resources SET body=? WHERE id=?',
            (canonical(replace(resource, state='archived')).decode(), rid),
            write=True,
        )
    assert (await event(oauth, private_token)).status_code >= 400
    async with app.metadata.transaction(write=False) as tx:
        assert view_count(tx, rid) == 1


@pytest.mark.asyncio
async def test_signing_in_does_not_turn_same_browser_refresh_into_new_view(oauth):
    _, _, _, http = oauth
    rid = await post(oauth)
    anonymous, _ = await page_token(http, rid)
    assert (await event(oauth, anonymous)).json()['view_count'] == 1
    await browser_login(oauth)
    signed_in, _ = await page_token(http, rid)
    assert (await event(oauth, signed_in)).json()['view_count'] == 1


@pytest.mark.asyncio
async def test_event_obeys_runtime_pause_and_quarantine(oauth):
    app, _, _, http = oauth
    rid = await post(oauth)
    token, _ = await page_token(http, rid)
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('runtime_config', {'accept_writes': False})
    assert (await event(oauth, token)).status_code >= 400
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('runtime_config', {'accept_writes': True})
        tx.set_setting('recovery_quarantine', {})
    assert (await event(oauth, token)).status_code >= 400
    async with app.metadata.transaction(write=False) as tx:
        assert view_count(tx, rid) == 0


@pytest.mark.asyncio
async def test_sqlite_migration_is_idempotent_and_preserves_counts(tmp_path):
    path = tmp_path / 'metadata.sqlite'
    store = SqliteMetadataStore(path)
    async with store.transaction(write=True) as tx:
        tx.execute(
            'INSERT INTO resources (id,type,name,parent,owner,grp,mode,generation,revision,state,created_at,modified_at,body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                'r_test',
                'post',
                'post.md',
                None,
                'owner',
                'group',
                0,
                1,
                None,
                'active',
                'now',
                'now',
                '{}',
            ),
            write=True,
        )
        assert record_view(tx, 'r_test', '2026-10-03', 'digest') == 1
        assert record_view(tx, 'r_test', '2026-10-03', 'digest') == 1
    await store.close()
    again = SqliteMetadataStore(path)
    async with again.transaction(write=False) as tx:
        assert view_count(tx, 'r_test') == 1
        assert tx.rows('SELECT version FROM schema_version') == [(1,)]
    await again.close()
