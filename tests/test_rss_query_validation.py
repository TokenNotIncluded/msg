"""RSS reader errors remain client errors on the real HTTP adapter."""

import httpx
import pytest

from msg.transports.http import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/rss', '/-/rss'])
async def test_rss_rejects_nondecimal_digits_without_internal_error(installed, path):
    app, _ = installed
    async with app.metadata.transaction(write=False) as tx:
        before = tx.one('SELECT COUNT(*) FROM events')[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        for limit in ('²', '³', '①', '1²'):
            response = await http.get(path, params={'limit': limit})
            assert response.status_code == 400
            assert response.json()['error']['code'] == 'invalid_limit'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM events')[0] == before


@pytest.mark.asyncio
async def test_rss_keeps_decimal_limits_and_head_read_only(installed):
    app, _ = installed
    async with app.metadata.transaction(write=False) as tx:
        before = tx.one('SELECT COUNT(*) FROM events')[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        for limit in ('1', '01', '١'):
            response = await http.get('/rss', params={'limit': limit})
            assert response.status_code == 200
            assert response.headers['content-type'].startswith('application/rss+xml')
            head = await http.head('/rss', params={'limit': limit})
            assert head.status_code == 200
            assert not head.content
            assert head.headers['content-length'] == response.headers['content-length']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM events')[0] == before
