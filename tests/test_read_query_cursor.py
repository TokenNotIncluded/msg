"""ReadQuery pages are stable, bounded, and reauthorized on every GET."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.application import Application
from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.http import create_app


async def state(app):
    async with app.metadata.transaction(write=False) as tx:
        return (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM jobs')[0],
            tx.one('SELECT COUNT(*) FROM revisions')[0],
            tx.one('SELECT SUM(generation) FROM resources')[0],
        )


@pytest.mark.asyncio
async def test_read_query_page_cursor_aliases_snapshot_and_zero_business_effects(installed):
    app, _ = installed
    key, user, _ = await register(app, 'page-reader')
    for index in range(4):
        created = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': f'page {index}'},
            key=key,
            subject=user,
        )
        assert created.status == 'ok'
    before = await state(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        first = await http.get('/_read/query?root=%2Fmain&first=2&select=id,name&sort=id')
        assert first.status_code == 200, first.text
        page = first.json()
        assert len(page['items']) == 2
        assert page['next'].startswith('/_r/c/')
        alias = await http.get('/_r/query?root=%2Fmain&first=2&select=id,name&sort=id')
        assert alias.status_code == 200 and alias.json() == page
        cursor = page['next'].removeprefix('/_r/c/')
        long = await http.get('/_read/c/' + cursor)
        short = await http.get('/_r/c/' + cursor)
        assert long.status_code == short.status_code == 200
        assert long.json() == short.json()
        assert not long.is_redirect and not short.is_redirect
        fresh = Application(app.settings, clock=lambda: NOW)
        await fresh.load()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(fresh)), base_url='http://testserver'
            ) as resumed:
                after_disconnect = await resumed.get('/_r/c/' + cursor)
                assert after_disconnect.status_code == 200
                assert after_disconnect.json() == short.json()
        finally:
            await fresh.close()
        seen = {item['id'] for item in page['items'] + long.json()['items']}
        assert len(seen) == 4
        assert await state(app) == before
        broken = cursor[:-1] + ('A' if cursor[-1] != 'A' else 'B')
        tampered = await http.get('/_r/c/' + broken)
        assert tampered.status_code == 400
        assert tampered.json()['error']['code'] == 'invalid_cursor'

        app.clock = lambda: NOW + timedelta(seconds=1)
        app.executor.clock = app.clock
        added = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': 'new after snapshot'},
            key=key,
            subject=user,
        )
        assert added.status == 'ok'
        repeat = await http.get('/_r/c/' + cursor)
        assert added.resources[0].id not in {item['id'] for item in repeat.json()['items']}
        app.clock = lambda: NOW + timedelta(minutes=16)
        expired = await http.get('/_r/c/' + cursor)
        assert expired.status_code == 400
        assert expired.json()['error']['code'] == 'cursor_expired'


@pytest.mark.asyncio
async def test_page_cursor_binds_principal_and_rechecks_current_access(installed):
    app, _ = installed
    key, user, _ = await register(app, 'private-reader')
    topic = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'private-pages'},
        key=key,
        subject=user,
    )
    rid = topic.resources[0].id
    for index in range(2):
        await call(
            app, 'content.post_create', {'parent': rid, 'body': str(index)}, key=key, subject=user
        )
    args = {'parent': rid, 'limit': 1, 'fields': ['id', 'name'], 'sort': 'id'}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        packet = request_for(
            'discovery.read_query',
            args,
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=120),
        )
        first = await http.get(
            f'/_read/query?root={rid}&first=1&select=id,name&sort=id',
            headers={'X-Msg-Request': b64(canonical(packet))},
        )
        assert first.status_code == 200, first.text
        next_path = first.json()['next']
        anonymous = await http.get(next_path)
        assert anonymous.status_code in {400, 403}
        assert anonymous.json()['error']['code'] in {
            'cursor_principal_mismatch',
            'permission_denied',
        }
        next_args = {'cursor': next_path.removeprefix('/_r/c/')}
        signed = request_for(
            'discovery.read_query',
            next_args,
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=120),
        )
        await call(
            app,
            'content.chmod',
            {'id': rid, 'mode': '0700'},
            key=key,
            subject=user,
            expected=((rid, topic.data['generation']),),
        )
        allowed = await http.get(next_path, headers={'X-Msg-Request': b64(canonical(signed))})
        assert allowed.status_code == 200, allowed.text
        changed = await call(
            app,
            'content.chmod',
            {'id': rid, 'mode': '0000'},
            key=key,
            subject=user,
            expected=((rid, topic.data['generation'] + 1),),
        )
        assert changed.status == 'ok'
        denied = await http.get(next_path, headers={'X-Msg-Request': b64(canonical(signed))})
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_read_query_rejects_expensive_or_unsupported_expansion(installed):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        excessive = await http.get('/_read/query?root=%2Fmain&first=101')
        assert excessive.status_code == 400
        assert excessive.json()['error']['code'] == 'query_cost_exceeded'
        expanded = await http.get('/_r/query?root=%2Fmain&expand=children')
        assert expanded.status_code == 400
        assert expanded.json()['error']['code'] == 'query_cost_exceeded'
