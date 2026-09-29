"""Legacy extensionless Post links redirect only after the current read check."""

from datetime import timedelta

import httpx
from test_service import NOW, call, register

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.http import create_app


async def test_public_legacy_post_path_redirects_only_for_read_methods(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'legacy-public')
    post = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'name': 'old-link', 'body': 'public body'},
        key=key,
        subject=subject,
    )
    assert post.status == 'ok'
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(
        transport=transport, base_url='http://testserver', follow_redirects=False
    ) as http:
        old = await http.get('/main/old-link')
        assert old.status_code == 308
        assert old.headers['location'] == '/main/old-link.md'
        assert old.headers['cache-control'] == 'no-store'
        assert (await http.get(old.headers['location'])).status_code == 200
        head = await http.head('/main/old-link')
        assert head.status_code == 308 and head.headers['location'] == old.headers['location']
        forbidden = await http.post('/main/old-link', content=b'write')
        assert forbidden.status_code == 405 and 'location' not in forbidden.headers
        unknown = await http.get('/main/no-such-post')
        assert unknown.status_code == 404 and 'location' not in unknown.headers


async def test_private_legacy_post_path_never_leaks_redirect_to_anonymous(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'legacy-private')
    post = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'name': 'secret-title', 'body': 'private body'},
        key=key,
        subject=subject,
    )
    rid = post.resources[0].id
    changed = await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0600'},
        key=key,
        subject=subject,
        expected=((rid, post.data['generation']),),
    )
    assert changed.status == 'ok'
    packet = request_for(
        'discovery.get',
        {'id': rid},
        app.settings.service_url,
        signer=key,
        subject=subject,
        expires_at=NOW + timedelta(seconds=90),
    )
    headers = {'x-msg-request': b64(canonical(packet))}
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(
        transport=transport, base_url='http://testserver', follow_redirects=False
    ) as http:
        denied = await http.get('/main/secret-title')
        stable_denied = await http.get('/_id/' + rid)
        assert denied.status_code == stable_denied.status_code == 403
        assert 'location' not in denied.headers
        assert 'secret-title' not in denied.text
        allowed = await http.get('/main/secret-title', headers=headers)
        assert allowed.status_code == 308
        assert allowed.headers['location'] == '/main/secret-title.md'
        stable_allowed = await http.get('/_id/' + rid, headers=headers)
        assert stable_allowed.status_code == 200
