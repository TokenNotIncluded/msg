"""Crawler discovery advertises only unconditional public URLs."""

from xml.etree import ElementTree as ET

import httpx
import pytest

from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_crawler_discovery_is_public_read_only_and_uses_configured_origin(installed):
    app, _ = installed
    origin = app.settings.service_url.rstrip('/')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=origin
    ) as http:
        robots = await http.get('/robots.txt')
        assert robots.status_code == 200
        assert robots.headers['content-type'].startswith('text/plain')
        assert 'User-agent: *\nAllow: /\nDisallow: /-/\n' in robots.text
        assert f'Sitemap: {origin}/sitemap.xml\n' in robots.text

        sitemap = await http.get('/sitemap.xml')
        assert sitemap.status_code == 200
        assert sitemap.headers['content-type'].startswith('application/xml')
        document = ET.fromstring(sitemap.content)
        namespace = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        locations = [item.text for item in document.findall('s:url/s:loc', namespace)]
        # No resource names, account paths, operation URLs or private metadata.
        assert locations == [origin + '/']
        assert (await http.get(locations[0])).status_code == 200

        for path, response in (('/robots.txt', robots), ('/sitemap.xml', sitemap)):
            head = await http.head(path)
            assert head.status_code == 200 and head.content == b''
            assert head.headers['content-length'] == str(len(response.content))
            assert head.headers['content-type'] == response.headers['content-type']
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['x-content-type-options'] == 'nosniff'
            assert 'sandbox' in response.headers['content-security-policy']
            assert (await http.post(path, content=b'overwrite')).status_code == 405
            assert (await http.get(path + '?extra=1')).status_code == 400
            assert (await http.get(path, headers={'Host': 'evil.example'})).status_code == 403
