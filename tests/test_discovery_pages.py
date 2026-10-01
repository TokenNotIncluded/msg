"""Browser discovery pages retain authorized ranking, raw views and API compatibility."""

import httpx
import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_feed_html_markdown_raw_and_json_use_same_public_posts(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'page-author')
    posted = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '# Useful title\n\nA useful description.'},
        key=key,
        subject=subject,
    )
    assert posted.status == 'ok'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        html = await http.get('/feed?limit=1&interests=python', headers={'Accept': 'text/html'})
        assert html.status_code == 200 and html.headers['content-type'].startswith('text/html')
        assert 'Useful title' in html.text and '@page-author' in html.text
        assert 'A useful description.' in html.text and 'Recommended posts' in html.text
        assert '/feed?limit=1&amp;interests=python&amp;format=raw' in html.text
        assert "form-action 'self'" in html.headers['content-security-policy']
        assert 'document.modelContext' in html.text
        raw = await http.get(
            '/feed?limit=1&interests=python&format=raw', headers={'Accept': 'text/html'}
        )
        assert raw.headers['content-type'].startswith('text/plain') and raw.text.startswith(
            '# Recommended'
        )
        assert '<!doctype' not in raw.text
        markdown = await http.get('/feed', headers={'Accept': 'text/markdown'})
        assert markdown.headers['content-type'].startswith('text/markdown')
        json = await http.get('/feed', headers={'Accept': 'application/json'})
        assert json.json()['items'][0]['title'] == 'Useful title'
        assert json.json()['items'][0]['author_name'] == '@page-author'
        head = await http.head('/feed?limit=1&interests=python', headers={'Accept': 'text/html'})
        assert not head.content and head.headers['content-length'] == str(len(html.content))
        topics = await http.get('/topics', headers={'Accept': 'text/html'})
        assert topics.status_code == 200 and '<table>' in topics.text
        assert 'href="/main"' in topics.text and '/help/permissions' in topics.text
        assert 'href="/topics?format=raw"' in topics.text
        raw_topics = await http.get('/topics?format=raw', headers={'Accept': 'text/html'})
        assert (
            raw_topics.headers['content-type'].startswith('text/plain')
            and '| Topic' in raw_topics.text
        )
        assert (await http.post('/topics')).status_code == 405
        assert (await http.get('/topics?unknown=1')).status_code == 400
        home = await http.get('/', headers={'Accept': 'text/html'})
        assert 'href="/topics"' in home.text


@pytest.mark.asyncio
async def test_browser_feed_uses_follows_and_topics_respect_membership(oauth):
    from msg.plugins.identity import set_member

    app, key, subject, http = oauth
    bk, bob, _ = await register(app, 'page-followed')
    made = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '# Followed post'},
        key=bk,
        subject=bob,
    )
    assert made.status == 'ok'
    followed = await call(app, 'communication.follow', {'id': bob}, key=key, subject=subject)
    assert followed.status == 'ok'
    public = await http.get('/topics', headers={'Accept': 'text/html'})
    assert '/admins' not in public.text
    async with app.metadata.transaction(write=True) as tx:
        await set_member(tx, 'g_admins', subject, 'member', joined_at=app.clock())
    await browser_login(oauth)
    personal = await http.get('/feed', headers={'Accept': 'text/html'})
    assert personal.status_code == 200 and '已关注' in personal.text
    topics = await http.get('/topics', headers={'Accept': 'text/html'})
    assert 'href="/admins"' in topics.text
