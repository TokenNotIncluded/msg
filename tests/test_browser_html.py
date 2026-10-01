"""HTML negotiation cannot turn resource text into executable markup."""

from base64 import b64encode
from hashlib import sha256

import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.transports.home_page import HOME_BROWSER_HEADERS, document_html
from msg.transports.webmcp import WEBMCP_SCRIPT


def test_rendered_markdown_escapes_html_and_unsafe_links():
    result = document_html(
        '# Heading\n\n<script>alert(1)</script>\n\n'
        '[bad](javascript:alert(1))\n\n'
        '| A | B |\n| --- | --- |\n| one | two |'
    ).decode()
    assert '<h1>Heading</h1>' in result and '<table>' in result
    assert '<script>alert(1)</script>' not in result
    assert 'href="javascript:' not in result
    pinned = b64encode(sha256(WEBMCP_SCRIPT.encode()).digest()).decode()
    assert 'sha256-' + pinned in HOME_BROWSER_HEADERS['Content-Security-Policy']
    assert 'sandbox' not in HOME_BROWSER_HEADERS['Content-Security-Policy']


@pytest.mark.asyncio
async def test_login_alias_html_home_and_private_collection(oauth):
    app, _, _, http = oauth
    login = await http.get('/login')
    assert login.status_code == 200 and 'msg auth approve' in login.text
    assert login.headers['content-type'].startswith('text/html')
    assert login.headers['referrer-policy'] == 'strict-origin'
    home = await http.get('/', headers={'Accept': 'text/html'})
    assert 'href="/login"' in home.text and '<table>' in home.text
    assert 'document.modelContext' in home.text
    await browser_login(oauth)
    logout = await http.get('/oauth/logout')
    assert logout.headers['referrer-policy'] == 'strict-origin'
    home = await http.get('/', headers={'Accept': 'text/html'})
    assert 'href="/@oauth-owner/in"' in home.text
    assert 'href="/@oauth-owner/dm"' in home.text
    for path in ('/@oauth-owner/in', '/@oauth-owner/dm', '/@oauth-owner'):
        html = await http.get(path, headers={'Accept': 'text/html'})
        assert html.status_code == 200 and html.headers['content-type'].startswith('text/html')
        assert '<main>' in html.text and 'document.modelContext' in html.text
        raw = await http.get(path, headers={'Accept': 'text/markdown'})
        assert '<!doctype html>' not in raw.text
        assert html.headers['etag'] != raw.headers['etag']
        assert 'Accept' in html.headers['vary']


@pytest.mark.asyncio
async def test_readable_dm_channel_metadata_and_raw(oauth):
    app, key, subject, http = oauth
    other_key, other, _ = await register(app, 'mail-contact')
    dm = await call(
        app,
        'communication.dm_request',
        {'recipient': other, 'introduction': 'Private introduction'},
        key=key,
        subject=subject,
        contract_version=2,
    )
    assert dm.status == 'ok'
    await browser_login(oauth)
    listing = await http.get('/@oauth-owner/dm', headers={'Accept': 'text/html'})
    assert '@mail-contact' in listing.text
    conversation = await http.get(
        '/_id/' + dm.data['conversation_id'], headers={'Accept': 'text/html'}
    )
    assert 'Private introduction' in conversation.text and '@mail-contact' in conversation.text, (
        conversation.text.split('<main>')[1].split('</main>')[0]
    )
    assert 'List operation' not in conversation.text and 'p_' not in conversation.text
    assert 'class="current-account"' in conversation.text
    post = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '# Readable title\n\nReadable content'},
        key=key,
        subject=subject,
    )
    path = '/_id/' + post.resources[0].id
    html = await http.get(path, headers={'Accept': 'text/html'})
    assert 'class="post-meta"' in html.text and '<h1>Readable title</h1>' in html.text
    assert '<details>' in html.text and 'raw-link' in html.text
    raw = await http.get(path + '?format=raw', headers={'Accept': 'text/html'})
    assert raw.headers['content-type'].startswith('text/plain') and raw.text.startswith('---\n')
    channel = await http.get('/main', headers={'Accept': 'text/html'})
    assert 'Readable title' in channel.text and 'List operation' not in channel.text
    assert 'id="msg-theme"' in channel.text and 'id="msg-language"' in channel.text
    register_html = await http.get('/register', headers={'Accept': 'text/html'})
    register_raw = await http.get('/register?format=raw', headers={'Accept': 'text/html'})
    assert 'identity new myname' in register_html.text
    assert register_raw.headers['content-type'].startswith('text/plain')
    assert register_raw.text.startswith('# Register') and '<html' not in register_raw.text
    await call(
        app,
        'communication.dm_accept',
        {'conversation_id': dm.data['conversation_id']},
        key=other_key,
        subject=other,
    )


@pytest.mark.asyncio
async def test_own_groups_are_visible_in_browser_but_not_public_profile(oauth):
    from msg.plugins.identity import set_member

    app, _, subject, http = oauth
    async with app.metadata.transaction(write=True) as tx:
        await set_member(tx, 'g_admins', subject, 'member', joined_at=app.clock())
    public = await http.get('/@oauth-owner/json')
    assert 'groups' not in public.json()
    await browser_login(oauth)
    home = await http.get('/', headers={'Accept': 'text/html'})
    assert 'href="/&amp;admins"' in home.text and '&amp;admins' in home.text
    mine = await http.get('/@oauth-owner/json')
    assert {'name': '&admins', 'path': '/&admins'} in mine.json()['groups']
