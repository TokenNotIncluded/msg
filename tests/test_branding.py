"""The homepage is Markdown even for browsers; hosted files keep their own media type."""

from importlib.resources import files

import re

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
        assert browser.headers['content-type'].startswith('text/plain')
        assert b'<html' not in browser.content and b'msg.lmm.best' in browser.content
        assert b'<script' not in browser.content
        assert 'sandbox' in browser.headers['content-security-policy']
        agent = await http.get('/', headers={'Accept': 'text/markdown'})
        assert agent.status_code == 200
        assert agent.headers['content-type'].startswith('text/markdown')
        assert '/AGENTS.md' in agent.text
        assert 'sandbox' in agent.headers['content-security-policy']
        assert browser.content == agent.content
        assert not re.search(r"[\u3400-\u9fff]", browser.text)
        browser_head = await http.head('/', headers={'Accept': 'text/html'})
        assert browser_head.content == b''
        assert browser_head.headers['content-type'].startswith('text/plain')
        assert browser_head.headers['content-length'] == str(len(browser.content))
        assert (await http.post('/', content=b'overwrite')).status_code == 405
        for path in ('/AGENTS.md', '/main', '/_rules'):
            document = await http.get(path, headers={'Accept': 'text/html'})
            assert document.status_code == 200
            assert document.headers['content-type'].startswith('text/plain')
            markdown = await http.get(path, headers={'Accept': 'text/markdown'})
            assert markdown.headers['content-type'].startswith('text/markdown')
            assert document.content == markdown.content
            assert 'sandbox' in document.headers['content-security-policy']
            assert document.headers['x-content-type-options'] == 'nosniff'
            assert 'sandbox' in markdown.headers['content-security-policy']
        favicon = await http.get('/favicon.png')
        assert favicon.status_code == 200 and favicon.content.startswith(b'\x89PNG\r\n\x1a\n')
        head = await http.head('/favicon.png')
        assert head.status_code == 200 and head.content == b''
        assert head.headers['content-length'] == str(len(favicon.content))
        assert (await http.post('/favicon.png')).status_code == 405
        hosted = await http.get('/@root/web/index.html')
        assert hosted.status_code == 200 and b'<svg' in hosted.content
        assert '<html lang="en">' in hosted.text
        assert not re.search(r"[\u3400-\u9fff]", hosted.text)
        csp = hosted.headers['content-security-policy']
        assert "style-src 'unsafe-inline'" in csp
        assert 'font-src data:' in csp and 'img-src data:' in csp
        assert 'sandbox' in csp and 'allow-scripts' not in csp
        assert 'allow-same-origin' not in csp
        assert 'data:font/woff2;base64,' in hosted.text
        assert (
            '__SANS_FONT__' not in hosted.text
            and '__SANS_BOLD_FONT__' not in hosted.text
            and '<script' not in hosted.text
        )
