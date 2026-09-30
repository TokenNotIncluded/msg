"""OAuth acceptance through real PostgreSQL, identity verification and HTTP."""

import hashlib
import os
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from test_service import NOW, call, register

from msg.client import ClientState, MsgClient
from msg.client_api_keys import create as create_api_key
from msg.client_oauth import read_session, refresh, save_session
from msg.core.codec import b64, canonical, loads, unb64, wire
from msg.core.errors import Failure
from msg.core.requests import request_for
from msg.oauth_config import OAuthClient, OAuthConfig, load_oauth
from msg.security.oauth import DEVICE_GRANT
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from msg.transports.oauth_http import csrf


@pytest.fixture
async def oauth(installed):
    app, _ = installed
    app.settings = replace(
        app.settings,
        oauth=OAuthConfig(
            enabled=True,
            clients=(
                OAuthClient('msg-cli', 'MSG CLI'),
                OAuthClient('example', 'Example', ('https://example.com/callback',)),
            ),
        ),
    )
    app.authenticator.oauth_config = app.settings.oauth
    app._oauth_clock = [NOW]
    app.clock = lambda: app._oauth_clock[0]
    app.authenticator.clock = app.clock
    app.executor.clock = app.clock
    app.certificates.clock = app.clock
    key, subject, _ = await register(app, 'oauth-owner')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        yield app, key, subject, http


async def device_tokens(oauth, scope='openid profile msg.read offline_access'):
    app, key, subject, http = oauth
    response = await http.post(
        '/oauth/device_authorization', json={'client_id': 'msg-cli', 'scope': scope}
    )
    assert response.status_code == 200, response.text
    pending = response.json()
    approved = await call(
        app,
        'identity.oauth_approve',
        {'user_code': pending['user_code'], 'decision': 'approve'},
        key=key,
        subject=subject,
    )
    assert approved.status == 'ok', wire(approved)
    result = await http.post(
        '/oauth/token',
        json={
            'grant_type': DEVICE_GRANT,
            'client_id': 'msg-cli',
            'device_code': pending['device_code'],
        },
    )
    assert result.status_code == 200, result.text
    return result.json()


async def browser_login(oauth):
    app, key, subject, http = oauth
    page = await http.get('/oauth/login')
    assert page.status_code == 200
    # Observe the actual displayed approval code, not private database state.
    code = page.text.split('msg auth approve ')[1].split('</code>')[0]
    approved = await call(
        app,
        'identity.oauth_approve',
        {'user_code': code, 'decision': 'approve'},
        key=key,
        subject=subject,
    )
    assert approved.status == 'ok', wire(approved)
    cookie = http.cookies.get('msg_login')
    result = await http.post(
        '/oauth/login/poll',
        json={'csrf': csrf(cookie)},
        headers={'Origin': app.settings.service_url},
    )
    assert result.status_code == 200 and result.json() == {'logged_in': True}, result.text
    header = result.headers['set-cookie']
    assert 'HttpOnly' in header and 'SameSite=lax' in header
    return http.cookies.get('msg_session')


@pytest.mark.asyncio
async def test_device_flow_pending_denial_replay_and_poll_backoff(oauth):
    app, key, subject, http = oauth
    start = (await http.post('/oauth/device_authorization', data={'client_id': 'msg-cli'})).json()
    params = {
        'grant_type': DEVICE_GRANT,
        'client_id': 'msg-cli',
        'device_code': start['device_code'],
    }
    assert (await http.post('/oauth/token', data=params)).json()['error'] == 'authorization_pending'
    assert (await http.post('/oauth/token', data=params)).json()['error'] == 'slow_down'
    denied = await call(
        app,
        'identity.oauth_approve',
        {'user_code': start['user_code'], 'decision': 'deny'},
        key=key,
        subject=subject,
    )
    assert denied.status == 'ok'
    app._oauth_clock[0] = NOW + timedelta(seconds=15)
    assert (await http.post('/oauth/token', data=params)).json()['error'] == 'access_denied'
    again = await call(
        app,
        'identity.oauth_approve',
        {'user_code': start['user_code'], 'decision': 'approve'},
        key=key,
        subject=subject,
    )
    assert again.status == 'error' and again.error.code == 'invalid_grant'


@pytest.mark.asyncio
async def test_device_tokens_userinfo_scopes_and_one_time_consumption(oauth):
    app, _, subject, http = oauth
    tokens = await device_tokens(oauth)
    info = await http.get(
        '/oauth/userinfo', headers={'Authorization': 'Bearer ' + tokens['access_token']}
    )
    assert info.status_code == 200 and info.json() == {
        'sub': subject,
        'preferred_username': 'oauth-owner',
    }, info.text
    credential, _, encoded = tokens['access_token'].partition('.')
    result = await call(
        app, 'discovery.get', {'id': '/main'}, subject=subject, token=(credential, unb64(encoded))
    )
    assert result.status == 'ok' and result.actor == subject, wire(result)
    denied = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'no write scope'},
        subject=subject,
        token=(credential, unb64(encoded)),
    )
    assert denied.status == 'error' and denied.error.code == 'credential_ceiling'
    async with app.metadata.transaction(write=False) as tx:
        persisted = canonical(tx.rows('SELECT body FROM oauth_states')).decode()
        persisted += canonical(tx.rows('SELECT body FROM results')).decode()
        for value in (tokens['access_token'], encoded, tokens['refresh_token']):
            assert value not in persisted


@pytest.mark.asyncio
async def test_refresh_rotation_reuse_revokes_family_and_absolute_expiry(oauth):
    app, _, _, http = oauth
    initial = await device_tokens(oauth)
    data = {
        'client_id': 'msg-cli',
        'grant_type': 'refresh_token',
        'refresh_token': initial['refresh_token'],
    }
    rotated = await http.post('/oauth/token', json=data)
    assert rotated.status_code == 200, rotated.text
    new = rotated.json()
    assert new['refresh_token'] != initial['refresh_token']
    assert new['refresh_expires_at'] == initial['refresh_expires_at']
    assert (await http.post('/oauth/token', json=data)).json()['error'] == 'invalid_grant'
    assert (
        await http.get(
            '/oauth/userinfo', headers={'Authorization': 'Bearer ' + new['access_token']}
        )
    ).status_code != 200
    assert (
        await http.post('/oauth/token', json=dict(data, refresh_token=new['refresh_token']))
    ).json()['error'] == 'invalid_grant'


@pytest.mark.asyncio
async def test_pkce_cookie_csrf_oidc_and_code_binding(oauth):
    app, _, subject, http = oauth
    cookie = await browser_login(oauth)
    verifier = b64(os.urandom(48))
    args = {
        'client_id': 'example',
        'response_type': 'code',
        'redirect_uri': 'https://example.com/callback',
        'scope': 'openid profile msg.read offline_access',
        'state': 'state-with-enough-entropy',
        'nonce': 'nonce-with-enough-entropy',
        'code_challenge_method': 'S256',
        'code_challenge': b64(hashlib.sha256(verifier.encode()).digest()),
    }
    page = await http.get('/oauth/authorize?' + urlencode(args))
    assert page.status_code == 200 and '同意' in page.text
    bad = await http.post(
        '/oauth/authorize',
        data=dict(args, csrf='wrong', decision='approve'),
        headers={'Origin': app.settings.service_url},
    )
    assert bad.status_code == 400
    authorized = await http.post(
        '/oauth/authorize',
        data=dict(args, csrf=csrf(cookie), decision='approve'),
        headers={'Origin': app.settings.service_url},
    )
    assert authorized.status_code == 303, authorized.text
    query = parse_qs(urlsplit(authorized.headers['location']).query)
    assert query['state'] == [args['state']]
    data = {
        'client_id': 'example',
        'grant_type': 'authorization_code',
        'code': query['code'][0],
        'redirect_uri': args['redirect_uri'],
        'code_verifier': verifier,
    }
    wrong = await http.post('/oauth/token', json=dict(data, code_verifier='x' * 43))
    assert wrong.status_code == 400 and wrong.json()['error'] == 'invalid_grant'
    response = await http.post('/oauth/token', data=data)
    assert response.status_code == 200, response.text
    tokens = response.json()
    header, claims, signature = tokens['id_token'].split('.')
    jwk = (await http.get('/oauth/jwks')).json()['keys'][0]
    Ed25519PublicKey.from_public_bytes(unb64(jwk['x'])).verify(
        unb64(signature), (header + '.' + claims).encode()
    )
    body = loads(unb64(claims))
    assert body['iss'] == app.settings.service_url and body['aud'] == 'example'
    assert body['sub'] == subject and body['nonce'] == args['nonce']
    assert (await http.post('/oauth/token', data=data)).json()['error'] == 'invalid_grant'
    # Logout invalidates all grants made using this browser session.
    logout = await http.post(
        '/oauth/logout', json={'csrf': csrf(cookie)}, headers={'Origin': app.settings.service_url}
    )
    assert logout.status_code == 200
    assert (
        await http.get(
            '/oauth/userinfo', headers={'Authorization': 'Bearer ' + tokens['access_token']}
        )
    ).status_code != 200


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['parent', 'authority', 'quarantine', 'runtime'])
async def test_oauth_never_revives_revoked_or_restored_authority(oauth, mutation):
    app, _, _, http = oauth
    tokens = await device_tokens(oauth)
    async with app.metadata.transaction(write=True) as tx:
        if mutation == 'parent':
            key = oauth[1]
            old = await tx.credential(key.key_id)
            subject = await tx.subject(old.subject_id)
            await tx.save_credential(replace(old, revoked_at=NOW), subject.auth_version)
        elif mutation == 'authority':
            subject = await tx.subject(oauth[2])
            await tx.update_identity(
                replace(subject, auth_version=subject.auth_version + 1), subject.auth_version
            )
        else:
            tx.set_setting(
                'recovery_quarantine'
                if mutation == 'quarantine'
                else 'recovery_runtime_generation',
                'changed',
            )
    response = await http.post(
        '/oauth/token',
        json={
            'client_id': 'msg-cli',
            'grant_type': 'refresh_token',
            'refresh_token': tokens['refresh_token'],
        },
    )
    assert response.status_code >= 400
    assert (
        await http.get(
            '/oauth/userinfo', headers={'Authorization': 'Bearer ' + tokens['access_token']}
        )
    ).status_code >= 400


@pytest.mark.asyncio
async def test_persistent_client_refresh_and_private_file_permissions(oauth, tmp_path):
    app, _, subject, http = oauth
    tokens = await device_tokens(oauth)
    state = ClientState(tmp_path / 'cli', server=app.settings.service_url)
    client = MsgClient(state, HTTPTransport(state.server, http=http), clock=lambda: app.clock())
    save_session(client, tokens)
    assert state.subject == subject
    assert (state.directory / 'oauth-session.json').stat().st_mode & 0o777 == 0o600
    reloaded = MsgClient(ClientState(state.directory), client.transport, clock=lambda: app.clock())
    app._oauth_clock[0] = NOW + timedelta(seconds=910)
    result = await reloaded.call('discovery.get', {'id': '/main'})
    assert result.status == 'ok' and result.actor == subject, wire(result)
    saved = read_session(reloaded.state)
    assert saved['refresh_token'] != tokens['refresh_token']
    assert not saved.get('refresh_pending')
    (state.directory / 'oauth-session.json').chmod(0o644)
    with pytest.raises(Failure, match='unsafe_oauth_session'):
        await refresh(reloaded)


@pytest.mark.asyncio
async def test_api_key_private_signature_rotation_delivery_and_header(oauth, tmp_path):
    app, _, _, http = oauth
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
    assert response.status_code == 200 and response.json()['actor'] == state.subject, response.text
    app._oauth_clock[0] = NOW + timedelta(seconds=121)
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
async def test_custodial_signup_login_and_device_consent_reuses_vault(oauth):
    app, _, _, http = oauth
    await http.get('/oauth/signup')
    binder = http.cookies.get('msg_login')
    response = await http.post(
        '/oauth/signup',
        data={'handle': 'hosted-oauth', 'csrf': csrf(binder)},
        headers={'Origin': app.settings.service_url},
    )
    assert response.status_code == 200, response.text
    cookie = http.cookies.get('msg_session')
    pending = (
        await http.post(
            '/oauth/device_authorization',
            data={'client_id': 'msg-cli', 'scope': 'openid profile msg.read offline_access'},
        )
    ).json()
    form_page = await http.get('/oauth/device?' + urlencode({'user_code': pending['user_code']}))
    assert form_page.status_code == 200 and '同意' in form_page.text
    response = await http.post(
        '/oauth/device',
        data={'user_code': pending['user_code'], 'decision': 'approve', 'csrf': csrf(cookie)},
        headers={'Origin': app.settings.service_url},
    )
    assert response.status_code == 200, response.text
    tokens = (
        await http.post(
            '/oauth/token',
            data={
                'client_id': 'msg-cli',
                'grant_type': DEVICE_GRANT,
                'device_code': pending['device_code'],
            },
        )
    ).json()
    assert 'access_token' in tokens, tokens
    # The original one-hour bootstrap token is no longer needed for the login.
    app._oauth_clock[0] = NOW + timedelta(seconds=3601)
    response = await http.post(
        '/oauth/token',
        data={
            'client_id': 'msg-cli',
            'grant_type': 'refresh_token',
            'refresh_token': tokens['refresh_token'],
        },
    )
    assert response.status_code == 200, response.text
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM custodial_vault')[0] == 1


@pytest.mark.parametrize(
    'uri',
    [
        'http://example.com/cb',
        'https://example.com/cb#x',
        'https://user@example.com/cb',
        'https://example.com/cb?other=1',
    ],
)
def test_redirect_registration_rejects_unsafe_uris(uri):
    with pytest.raises(Failure, match='invalid_redirect_uri'):
        load_oauth(
            {
                'enabled': True,
                'clients': [{'client_id': 'example', 'name': 'Example', 'redirect_uris': [uri]}],
            },
            'https://msg.lmm.best',
        )


@pytest.mark.asyncio
async def test_api_key_lost_response_recovers_with_source_binding(oauth, tmp_path):
    app, _, _, http = oauth
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
        tx.execute('DELETE FROM oauth_states WHERE id=?', ('api:' + credential.id,), write=True)
    denied = await restarted.call('discovery.get', {'id': '/main'})
    assert denied.status == 'error' and denied.error.code == 'invalid_grant'


@pytest.mark.asyncio
async def test_concurrent_refresh_consumes_once_and_revokes_reused_family(oauth):
    import asyncio

    _, _, _, http = oauth
    tokens = await device_tokens(oauth)
    body = {
        'client_id': 'msg-cli',
        'grant_type': 'refresh_token',
        'refresh_token': tokens['refresh_token'],
    }
    replies = await asyncio.gather(
        http.post('/oauth/token', json=body), http.post('/oauth/token', json=body)
    )
    assert sorted(r.status_code for r in replies) == [200, 400]
    issued = next(r.json() for r in replies if r.status_code == 200)
    assert (
        await http.get(
            '/oauth/userinfo', headers={'Authorization': 'Bearer ' + issued['access_token']}
        )
    ).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'patch',
    [
        {'client_id': 'unknown'},
        {'redirect_uri': 'https://example.com/callback/extra'},
        {'redirect_uri': 'https://attacker.example/callback'},
        {'code_challenge_method': 'plain'},
        {'scope': 'openid admin'},
        {'state': 'tiny'},
        {'response_type': 'token'},
    ],
)
async def test_browser_rejects_unregistered_or_downgraded_authorization(oauth, patch):
    _, _, _, http = oauth
    args = {
        'client_id': 'example',
        'response_type': 'code',
        'redirect_uri': 'https://example.com/callback',
        'scope': 'openid profile',
        'state': 'state-with-enough-entropy',
        'code_challenge_method': 'S256',
        'code_challenge': b64(os.urandom(32)),
    }
    response = await http.get('/oauth/authorize?' + urlencode(dict(args, **patch)))
    assert response.status_code == 400 and 'location' not in response.headers


@pytest.mark.asyncio
async def test_device_grant_cannot_be_redeemed_twice(oauth):
    app, key, subject, http = oauth
    start = (await http.post('/oauth/device_authorization', data={'client_id': 'msg-cli'})).json()
    await call(
        app,
        'identity.oauth_approve',
        {'user_code': start['user_code'], 'decision': 'approve'},
        key=key,
        subject=subject,
    )
    body = {'client_id': 'msg-cli', 'grant_type': DEVICE_GRANT, 'device_code': start['device_code']}
    assert (await http.post('/oauth/token', json=body)).status_code == 200
    assert (await http.post('/oauth/token', json=body)).json()['error'] == 'invalid_grant'
