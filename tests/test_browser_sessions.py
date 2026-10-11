"""Native key approval through real PostgreSQL, signatures and browser HTTP."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.browser_config import BrowserConfig
from msg.client import ClientState, MsgClient
from msg.client_api_keys import create as create_api_key
from msg.core.codec import b64, unb64, wire
from msg.core.errors import Failure
from msg.core.requests import request_for
from msg.security.sessions import BrowserSessionService, get, state_id
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from msg.transports.session_http import csrf


@pytest.fixture
async def signed_browser(installed):
    app, _ = installed
    app.settings = replace(app.settings, browser=BrowserConfig(enabled=True))
    app.authenticator.browser_config = app.settings.browser
    app._session_clock = [NOW]
    app.clock = lambda: app._session_clock[0]
    app.authenticator.clock = app.clock
    app.executor.clock = app.clock
    app.certificates.clock = app.clock
    key, subject, _ = await register(app, 'key-owner')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        yield app, key, subject, http


async def start(signed_browser):
    _, _, _, http = signed_browser
    page = await http.get('/login')
    assert page.status_code == 200, page.text
    return page.text.split('msg auth approve ')[1].split('</code>')[0], http.cookies.get(
        'msg_login'
    )


async def approve(signed_browser, code, decision='approve'):
    app, key, subject, _ = signed_browser
    return await call(
        app,
        'identity.session_approve',
        {'user_code': code, 'decision': decision},
        key=key,
        subject=subject,
    )


async def poll(signed_browser, cookie):
    app, _, _, http = signed_browser
    return await http.post(
        '/session/poll', json={'csrf': csrf(cookie)}, headers={'Origin': app.settings.service_url}
    )


async def browser_login(signed_browser):
    _, _, _, http = signed_browser
    code, cookie = await start(signed_browser)
    approved = await approve(signed_browser, code)
    assert approved.status == 'ok', wire(approved)
    result = await poll(signed_browser, cookie)
    assert result.status_code == 200 and result.json() == {'logged_in': True}, result.text
    assert (
        'HttpOnly' in result.headers['set-cookie']
        and 'SameSite=lax' in result.headers['set-cookie']
    )
    return http.cookies.get('msg_session')


async def test_key_approval_creates_bounded_session_without_storing_private_key(signed_browser):
    app, key, subject, http = signed_browser
    code, cookie = await start(signed_browser)
    inspected = await call(app, 'identity.session_request', {'user_code': code})
    assert inspected.status == 'ok' and inspected.data['kind'] == 'browser', wire(inspected)
    assert inspected.data['service'] == app.settings.service_url
    assert (await approve(signed_browser, code)).status == 'ok'
    assert (await poll(signed_browser, cookie)).status_code == 200
    saved_cookie = http.cookies.get('msg_session')
    async with app.metadata.transaction(write=False) as tx:
        expiry, body = get(tx, state_id('session', saved_cookie), app.clock())
        assert expiry == NOW + timedelta(hours=1)
        assert body['subject'] == subject and body['parent'] == key.key_id
        rows = tx.rows('SELECT body FROM credential_states')
        text = str(rows)
        assert (
            saved_cookie not in text and cookie not in text and b64(key.private_bytes()) not in text
        )
        _, credential_id, _ = await BrowserSessionService(app).browser_credentials(tx, saved_cookie)
        assert credential_id.startswith('t_session_')
        credential = await tx.credential(credential_id)
        assert not any(
            op.startswith(('identity.', 'root.', 'system.'))
            for g in credential.ceiling
            for op in g.operations
        )
    assert (await http.get('/@key-owner/in')).status_code == 200


async def test_confirmation_requires_signature_and_is_consumed_once(signed_browser):
    app, _, subject, http = signed_browser
    code, cookie = await start(signed_browser)
    anonymous = await call(
        app, 'identity.session_approve', {'user_code': code, 'decision': 'approve'}
    )
    assert anonymous.status == 'error'
    await browser_login(signed_browser)
    session_cookie = http.cookies.get('msg_session')
    async with app.metadata.transaction(write=False) as tx:
        _, credential, secret = await BrowserSessionService(app).browser_credentials(
            tx, session_cookie
        )
    denied = await call(
        app,
        'identity.session_approve',
        {'user_code': code, 'decision': 'approve'},
        subject=subject,
        token=(credential, secret),
    )
    assert denied.status == 'error'
    assert (await approve(signed_browser, code)).status == 'ok'
    again = await approve(signed_browser, code)
    assert again.status == 'error' and again.error.code == 'invalid_grant'
    async with app.metadata.transaction(write=True) as tx:
        logged, error = await BrowserSessionService(app).pending_result(tx, cookie)
        assert logged and error is None
    with pytest.raises(Failure, match='invalid_grant'):
        async with app.metadata.transaction(write=True) as tx:
            await BrowserSessionService(app).pending_result(tx, cookie)


async def test_pending_backoff_denial_expiry_and_cross_browser_protection(signed_browser):
    app, _, _, http = signed_browser
    code, cookie = await start(signed_browser)
    assert (await poll(signed_browser, cookie)).json()['error'] == 'authorization_pending'
    assert (await poll(signed_browser, cookie)).json()['error'] == 'slow_down'
    assert (await approve(signed_browser, code, 'deny')).status == 'ok'
    app._session_clock[0] += timedelta(seconds=20)
    assert (await poll(signed_browser, cookie)).json()['error'] == 'access_denied'
    other_code, other_cookie = await start(signed_browser)
    assert (await poll(signed_browser, cookie)).status_code == 400
    app._session_clock[0] += timedelta(minutes=11)
    assert (await poll(signed_browser, other_cookie)).status_code == 400
    assert other_code != code


@pytest.mark.parametrize(
    'mutation', ['revoke', 'auth_version', 'expiry', 'logout', 'ceiling', 'disabled']
)
async def test_browser_session_rechecks_authority_on_every_read(signed_browser, mutation):
    app, key, subject, http = signed_browser
    cookie = await browser_login(signed_browser)
    assert (await http.get('/@key-owner/in')).status_code == 200
    if mutation == 'logout':
        result = await http.post(
            '/session/logout',
            json={'csrf': csrf(cookie)},
            headers={'Origin': app.settings.service_url},
        )
        assert result.status_code == 200
        http.cookies.set('msg_session', cookie)
    elif mutation == 'expiry':
        app._session_clock[0] += timedelta(hours=2)
    elif mutation == 'disabled':
        app.settings = replace(app.settings, browser=BrowserConfig(enabled=False))
        app.authenticator.browser_config = app.settings.browser
    else:
        async with app.metadata.transaction(write=True) as tx:
            parent = await tx.credential(key.key_id)
            owner = await tx.subject(subject)
            if mutation == 'auth_version':
                await tx.update_identity(
                    replace(owner, auth_version=owner.auth_version + 1), owner.auth_version
                )
            else:
                parent = replace(
                    parent, **({'revoked_at': NOW} if mutation == 'revoke' else {'ceiling': ()})
                )
                await tx.save_credential(parent, owner.auth_version)
    result = await http.get('/@key-owner/in')
    assert result.status_code in {400, 401, 403}, result.text


@pytest.mark.parametrize('token', ['非ASCII', '', 'wrong', None, ['wrong']])
async def test_csrf_failure_is_controlled_and_does_not_sign_in(signed_browser, token):
    app, _, _, http = signed_browser
    code, _ = await start(signed_browser)
    assert (await approve(signed_browser, code)).status == 'ok'
    response = await http.post(
        '/session/poll', json={'csrf': token}, headers={'Origin': app.settings.service_url}
    )
    assert response.status_code == 400
    assert http.cookies.get('msg_session') is None


async def test_concurrent_browser_poll_issues_at_most_one_session(signed_browser):
    app, _, _, _ = signed_browser
    code, cookie = await start(signed_browser)
    assert (await approve(signed_browser, code)).status == 'ok'
    results = await asyncio.gather(*(poll(signed_browser, cookie) for _ in range(2)))
    assert sorted(r.status_code for r in results) == [200, 400]
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM credential_states WHERE kind='session'")[0] == 1


@pytest.mark.asyncio
async def test_api_key_private_signature_rotation_delivery_and_header(signed_browser, tmp_path):
    app, _, _, http = signed_browser
    state = ClientState(tmp_path / 'api', server=app.settings.service_url)
    client = MsgClient(state, HTTPTransport(state.server, http=http), clock=lambda: app.clock())
    registered = await client.register('api-owner')
    assert registered.status == 'ok'
    initial = await create_api_key(client, ttl=60)
    assert initial.status == 'ok', wire(initial)
    first = dict(state.data['api_key'])
    result = await client.call('discovery.get', {'id': '/main'})
    assert result.status == 'ok' and result.actor == state.subject, wire(result)
    rotated = await create_api_key(client, ttl=120, rotate=True)
    assert rotated.status == 'ok', wire(rotated)
    denied = await call(
        app,
        'discovery.get',
        {'id': '/main'},
        subject=state.subject,
        token=(first['credential_id'], unb64(first['value'])),
    )
    assert denied.status == 'error' and denied.error.code == 'credential_revoked'
    current = state.data['api_key']
    packet = request_for(
        'discovery.get',
        {'id': '/main'},
        state.server,
        subject=state.subject,
        expires_at=NOW + timedelta(seconds=120),
    )
    response = await http.post(
        '/-/p/discovery.get',
        json=wire(packet),
        headers={'Authorization': 'Bearer ' + current['credential_id'] + '.' + current['value']},
    )
    assert response.status_code == 401 and response.json()['error'] == 'signature_required'
    app._session_clock[0] = NOW + timedelta(seconds=121)
    packet = request_for(
        'discovery.get',
        {'id': '/main'},
        state.server,
        subject=state.subject,
        token=(current['credential_id'], unb64(current['value'])),
        expires_at=app.clock() + timedelta(seconds=120),
    )
    result = await app.executor.execute(packet)
    assert result.status == 'error' and result.error.code == 'credential_expired'


@pytest.mark.asyncio
async def test_api_key_lost_response_recovers_with_source_binding(signed_browser, tmp_path):
    app, _, _, http = signed_browser
    state = ClientState(tmp_path / 'lost-api', server=app.settings.service_url)
    transport = HTTPTransport(state.server, http=http)
    client = MsgClient(state, transport, clock=lambda: app.clock(), retries=0)
    await client.register('lost-api')
    private = state.signer.private_bytes()
    original = transport.call

    async def lose(packet):
        result = await original(packet)
        if packet.operation == 'identity.token_create':
            raise httpx.ReadTimeout('lost response')
        return result

    transport.call = lose
    with pytest.raises(Failure, match='transport_uncertain'):
        await create_api_key(client)
    journal = state.directory / 'api-key-create.json'
    assert journal.exists() and state.data.get('api_key') is None
    restarted = MsgClient(
        ClientState(state.directory),
        HTTPTransport(state.server, http=http),
        clock=lambda: app.clock(),
    )
    recovered = await restarted.recover_token()
    assert recovered.status == 'ok', wire(recovered)
    assert not journal.exists() and restarted.state.token is None
    assert restarted.state.signer.private_bytes() == private
    assert restarted.state.data['api_key']['credential_id'] == recovered.data['credential_id']
    result = await restarted.call('discovery.get', {'id': '/main'})
    assert result.status == 'ok', wire(result)
    saved = restarted.state.data['api_key']
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(saved['credential_id'])
        assert credential.source_credential_id == state.signer.key_id
        tx.execute(
            'DELETE FROM credential_states WHERE id=?', ('api:' + credential.id,), write=True
        )
    denied = await restarted.call('discovery.get', {'id': '/main'})
    assert denied.status == 'error' and denied.error.code == 'invalid_grant'


async def test_existing_key_with_old_approval_grants_can_use_only_native_login(signed_browser):
    app, key, subject, http = signed_browser
    async with app.metadata.transaction(write=True) as tx:
        parent = await tx.credential(key.key_id)
        owner = await tx.subject(subject)
        renames = {
            'identity.session_request@1': 'identity.oauth_request@1',
            'identity.session_approve@1': 'identity.oauth_approve@1',
        }
        ceiling = tuple(
            replace(grant, operations=frozenset(renames.get(op, op) for op in grant.operations))
            for grant in parent.ceiling
        )
        for grant in ceiling:
            await app.certificates.validate_grant(grant, tx)
        await tx.save_credential(replace(parent, ceiling=ceiling), owner.auth_version)
    await browser_login(signed_browser)
    assert (await http.get('/@key-owner/in')).status_code == 200
    retired = await call(app, 'identity.oauth_approve', {}, key=key, subject=subject)
    assert retired.status == 'error' and retired.error.code == 'operation_disabled'


async def test_raw_login_and_logout_confirmation_are_read_only(signed_browser, monkeypatch):
    from read_only_evidence import readonly_evidence

    app, _, _, http = signed_browser
    await browser_login(signed_browser)
    async with readonly_evidence(app, monkeypatch):
        raw = await http.get('/login?format=raw')
        assert raw.status_code == 200 and 'set-cookie' not in raw.headers
        page = await http.get('/session/logout')
        assert page.status_code == 200 and 'set-cookie' not in page.headers
        assert (await http.get('/@key-owner/in')).status_code == 200
