"""The packaged mark is visible in human and hosted entry points without scripts."""

from importlib.resources import files

import httpx
import pytest

from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_logo_is_packaged_and_served_read_only(installed):
    app, _ = installed
    for name in ('logo.svg', 'logo-dark.svg', 'favicon.png'):
        assert files('msg.data').joinpath(name).is_file()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        browser = await http.get('/', headers={'Accept': 'text/html'})
        assert browser.status_code == 200
        assert browser.headers['content-type'].startswith('text/html')
        assert b'<svg' in browser.content and b'msg.lmm.best' in browser.content
        assert b'<script' not in browser.content
        assert 'sandbox' in browser.headers['content-security-policy']
        agent = await http.get('/', headers={'Accept': 'text/markdown'})
        assert agent.status_code == 200
        assert agent.headers['content-type'].startswith('text/markdown')
        assert '/AGENTS.md' in agent.text
        favicon = await http.get('/favicon.png')
        assert favicon.status_code == 200 and favicon.content.startswith(b'\x89PNG\r\n\x1a\n')
        head = await http.head('/favicon.png')
        assert head.status_code == 200 and head.content == b''
        assert head.headers['content-length'] == str(len(favicon.content))
        assert (await http.post('/favicon.png')).status_code == 405
        hosted = await http.get('/@root/web/index.html')
        assert hosted.status_code == 200 and b'<svg' in hosted.content
        assert 'sandbox' in hosted.headers['content-security-policy']
