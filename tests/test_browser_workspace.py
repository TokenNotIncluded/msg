"""Browser approval uses the same executor and current private-read authority."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_browser_sessions import browser_login, signed_browser as signed_browser
from test_service import call, register

from msg.core.codec import canonical
from msg.core.requests import request_for
from msg.security.browser_actions import BROWSER_POST_WRITES
from msg.security.sessions import BrowserSessionService, get, state_id
from msg.transports.http import create_app
from msg.transports.session_http import csrf


@pytest.mark.asyncio
async def test_browser_home_private_reads_logout_and_no_cookie_writes(signed_browser):
    app, key, subject, http = signed_browser
    other_key, other_subject, _ = await register(app, 'browser-other')
    paths = []
    for signer, owner, body in (
        (key, subject, 'OWN PRIVATE BODY'),
        (other_key, other_subject, 'OTHER PRIVATE BODY'),
    ):
        created = await call(
            app, 'content.post_create', {'parent': '/main', 'body': body}, key=signer, subject=owner
        )
        assert created.status == 'ok', created.error
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(created.resources[0].id)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
        paths.append('/*' + created.resources[0].id.split('_')[-1])
    anonymous = await http.get('/')
    assert '[Sign in with your MSG identity](/login)' in anonymous.text
    assert 'Your account' not in anonymous.text
    assert (await http.get(paths[0])).status_code == 403
    cookie = await browser_login(signed_browser)
    home = await http.get('/')
    assert '[Inbox](/@key-owner/in)' in home.text
    assert '[Direct messages](/@key-owner/dm)' in home.text
    assert 'Signed in as [@key-owner]' in home.text
    assert home.headers['cache-control'] == 'private, no-store'
    assert set(home.headers['vary'].split(', ')) == {'Accept', 'Cookie'}
    assert 'OWN PRIVATE BODY' not in home.text
    for path in ('/@key-owner/in', '/@key-owner/out', '/@key-owner/dm'):
        response = await http.get(path)
        assert response.status_code == 200, response.text
    assert 'OWN PRIVATE BODY' in (await http.get(paths[0])).text
    assert (await http.get(paths[1])).status_code == 403
    assert (await http.get('/@browser-other/in')).status_code == 403
    assert (await http.get('/@browser-other/dm')).status_code == 403
    async with app.metadata.transaction(write=False) as tx:
        credentials = await BrowserSessionService(app).browser_credentials(tx, cookie)
        credential = await tx.credential(credentials[1])
        assert all(
            (
                app.registry.operation(op.split('@')[0], int(op.split('@')[1])).effect == 'read'
                or (
                    op.split('@')[0] in BROWSER_POST_WRITES
                    and op.endswith('@1')
                    and not app.registry.operation(op.split('@')[0], 1).require_signature
                )
            )
            for grant in credential.ceiling
            for op in grant.operations
        )
    write = request_for(
        'content.post_create',
        {'parent': '/main', 'body': 'COOKIE MUST NOT WRITE'},
        app.settings.service_url,
        subject=subject,
    )
    rejected = await http.post(
        '/-/p/content.post_create',
        content=canonical(write),
        headers={'Content-Type': 'application/json'},
    )
    assert rejected.status_code != 200
    confirm = await http.get('/session/logout')
    assert confirm.status_code == 200 and 'Confirm sign-out' in confirm.text
    assert (await http.get(paths[0])).status_code == 200
    bad_logout = await http.post(
        '/session/logout', json={'csrf': csrf(cookie)}, headers={'Origin': 'https://evil.example'}
    )
    assert bad_logout.status_code == 400
    logout = await http.post(
        '/session/logout', data={'csrf': csrf(cookie)}, headers={'Origin': app.settings.service_url}
    )
    assert logout.status_code == 200
    # A copied old cookie cannot resurrect a logged-out session.
    http.cookies.set('msg_session', cookie)
    assert 'Your account' not in (await http.get('/')).text
    assert (await http.get(paths[0])).status_code == 403


@pytest.mark.asyncio
async def test_browser_revocation_expiry_and_read_only_persistent_state(signed_browser):
    app, _, subject, http = signed_browser
    cookie = await browser_login(signed_browser)
    async with app.metadata.transaction(write=False) as tx:
        before = tx.rows('SELECT id,body FROM credential_states ORDER BY id')
    for _ in range(2):
        response = await http.get('/@key-owner/in')
        assert response.status_code == 200, response.text
    async with app.metadata.transaction(write=False) as tx:
        assert before == tx.rows('SELECT id,body FROM credential_states ORDER BY id')
    async with app.metadata.transaction(write=True) as tx:
        _, body = get(tx, state_id('session', cookie), app.clock())
        parent = await tx.credential(body['parent'])
        await tx.save_credential(
            replace(parent, revoked_at=app.clock()), (await tx.subject(subject)).auth_version
        )
    denied = await http.get('/@key-owner/in')
    assert denied.status_code == 401
    assert 'expired or was revoked' in (await http.get('/')).text


@pytest.mark.asyncio
async def test_browser_cookie_does_not_cross_accounts(signed_browser):
    app, _, _, http = signed_browser
    await browser_login(signed_browser)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as anonymous:
        assert 'Your account' not in (await anonymous.get('/')).text
        assert (await anonymous.get('/@key-owner/in')).status_code == 401
        anonymous.cookies.set('msg_session', 'forged-cookie')
        assert 'Your account' not in (await anonymous.get('/')).text
    app._session_clock[0] += timedelta(days=31)
    assert 'Your account' not in (await http.get('/')).text
    assert (await http.get('/@key-owner/in')).status_code == 401


@pytest.mark.asyncio
async def test_browser_session_cannot_survive_source_permission_contraction(signed_browser):
    app, _, subject, http = signed_browser
    cookie = await browser_login(signed_browser)
    assert (await http.get('/@key-owner/in')).status_code == 200
    async with app.metadata.transaction(write=True) as tx:
        _, body = get(tx, state_id('session', cookie), app.clock())
        parent = await tx.credential(body['parent'])
        await tx.save_credential(
            replace(parent, ceiling=()), (await tx.subject(subject)).auth_version
        )
    assert (await http.get('/@key-owner/in')).status_code == 401
    assert 'Your account' not in (await http.get('/')).text


@pytest.mark.asyncio
async def test_browser_derived_token_is_read_only_and_disabled_with_oauth(signed_browser):
    app, _, subject, http = signed_browser
    cookie = await browser_login(signed_browser)
    async with app.metadata.transaction(write=False) as tx:
        _, credential_id, token = await BrowserSessionService(app).browser_credentials(tx, cookie)
    forbidden = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'FORBIDDEN'},
        subject=subject,
        token=(credential_id, token),
    )
    assert forbidden.status == 'error' and forbidden.error.code == 'credential_ceiling'
    app.settings = replace(app.settings, browser=replace(app.settings.browser, enabled=False))
    app.authenticator.browser_config = app.settings.browser
    denied = await call(
        app, 'discovery.get', {'id': '/main'}, subject=subject, token=(credential_id, token)
    )
    assert denied.status == 'error' and denied.error.code == 'browser_sessions_disabled'


@pytest.mark.asyncio
async def test_custodial_browser_signup_is_removed(signed_browser):
    _, _, _, http = signed_browser
    for method in ('GET', 'POST'):
        response = await http.request(method, '/oauth/signup')
        assert response.status_code == 410
        assert 'set-cookie' not in response.headers
