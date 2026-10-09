"""AgentID browser flows through PostgreSQL and the existing account executor."""

from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from test_account_login import (
    account,  # noqa: F401
    approve_if_pending,
    post,
    session_source,
    token,
)
from test_oauth import browser_login, oauth  # noqa: F401

from msg.login_config import LoginProvider
from msg.transports.login_providers import AGENTID_ISSUER, authorization_url
from msg.transports.oauth_http import csrf


@pytest.fixture
async def agentid_account(account, monkeypatch):  # noqa: F811
    app, _, _, _, _ = account
    credential = app.settings.config_dir / 'agentid.fixture'
    credential.write_text('fixture-only-secret')
    credential.chmod(0o600)
    provider = LoginProvider(
        'agentid',
        enabled=True,
        client_id='agentid-fixture',
        credential_file=str(credential),
        token_auth_method='client_secret_basic',
    )
    app.settings = replace(
        app.settings,
        login=replace(
            app.settings.login,
            providers=tuple(
                provider if item.name == 'agentid' else item
                for item in app.settings.login.providers
            ),
        ),
    )

    def authorize(name, config, state, verifier, nonce, redirect_uri, **options):
        assert redirect_uri == app.settings.service_url + '/-/login/callback/' + name
        return authorization_url(
            name,
            config,
            state,
            verifier,
            nonce,
            redirect_uri.replace('http://testserver/', 'http://localhost/'),
            **options,
        )

    monkeypatch.setattr('msg.transports.account_login.authorization_url', authorize)
    return account


async def begin(case, *, mode='login'):
    _, _, _, http, _ = case
    response = await post(
        case,
        '/-/login/start',
        {
            'csrf': await token(http),
            'provider': 'agentid',
            'mode': mode,
        },
    )
    assert response.status_code == 303, response.text
    state = parse_qs(urlsplit(response.headers['location']).query)['state'][0]
    return state, csrf(http.cookies.get('msg_account_login'))


async def callback(case, state, **overrides):
    return await case[3].get(
        '/-/login/callback/agentid',
        params={
            'state': state,
            'code': 'fixture-code',
            'iss': AGENTID_ISSUER,
            **overrides,
        },
    )


async def test_public_initiate_url_preserves_hint_and_is_login_only(agentid_account):
    app, _, _, http, _ = agentid_account
    async with app.metadata.transaction(write=False) as tx:
        before = tx.one('SELECT COUNT(*) FROM identities')[0]
    response = await http.get(
        '/login/agentid',
        params={
            'iss': AGENTID_ISSUER,
            'login_hint': 'agent+one@example.org',
        },
    )
    assert response.status_code == 303 and 'set-cookie' not in response.headers
    assert response.headers['location'].startswith('/-/login/start?')
    response = await http.get(response.headers['location'])
    assert response.status_code == 303, response.text
    target = urlsplit(response.headers['location'])
    assert target._replace(query='').geturl() == AGENTID_ISSUER + '/v0/authorize'
    params = parse_qs(target.query)
    assert params['login_hint'] == ['agent+one@example.org']
    assert params['client_id'] == ['agentid-fixture']
    assert params['code_challenge_method'] == ['S256']
    assert len(params['state'][0]) == 43
    assert 'Max-Age=600' in response.headers['set-cookie']
    assert 'HttpOnly' in response.headers['set-cookie']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM identities')[0] == before
    assert 'Continue with AgentID' in (await http.get('/login')).text
    assert 'Continue with AgentID' not in (await http.get('/register')).text


@pytest.mark.parametrize(
    'query',
    [
        'provider=agentid&iss=https%3A%2F%2Fattacker.example',
        'provider=agentid&mode=bind',
        'provider=agentid&mode=register',
        'provider=agentid&callbackURL=https%3A%2F%2Fattacker.example',
        'provider=agentid&provider=google',
        'provider=google',
        'provider=agentid&login_hint=a&login_hint=b',
    ],
)
async def test_initiation_rejects_issuer_or_mode_injection(agentid_account, query):
    response = await agentid_account[3].get('/-/login/start?' + query)
    assert response.status_code == 400, response.text
    assert 'location' not in response.headers


@pytest.mark.parametrize('issuer', [None, 'https://attacker.example'])
async def test_callback_requires_exact_issuer(agentid_account, issuer):
    _, _, _, http, _ = agentid_account
    state, _ = await begin(agentid_account)
    params = {'state': state, 'code': 'fixture-code'}
    if issuer is not None:
        params['iss'] = issuer
    denied = await http.get('/-/login/callback/agentid', params=params)
    assert denied.status_code == 400 and 'login_provider_invalid_issuer' in denied.text
    assert (await callback(agentid_account, state)).status_code == 303
    replay = await callback(agentid_account, state)
    assert replay.status_code == 400 and 'invalid_login_transaction' in replay.text


async def test_waiting_callback_and_unbound_identity_do_not_create_account(agentid_account):
    app, _, _, http, _ = agentid_account
    state, value = await begin(agentid_account)
    app._oauth_clock[0] += timedelta(seconds=330)
    returned = await callback(agentid_account, state)
    assert returned.status_code == 303, returned.text
    async with app.metadata.transaction(write=False) as tx:
        before = tx.one('SELECT COUNT(*) FROM identities')[0]
    result = await post(agentid_account, '/-/login/complete', {'csrf': value, 'state': state})
    assert result.status_code == 400 and 'login_binding_required' in result.text
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM identities')[0] == before
    replay = await post(agentid_account, '/-/login/complete', {'csrf': value, 'state': state})
    assert replay.status_code == 400


async def test_agentid_cannot_register_or_bypass_csrf(agentid_account):
    _, _, _, http, _ = agentid_account
    form = {'provider': 'agentid', 'mode': 'register', 'handle': 'new-agent', 'custody': 'yes'}
    denied = await post(agentid_account, '/-/login/start', form)
    assert denied.status_code == 403
    denied = await post(agentid_account, '/-/login/start', {**form, 'csrf': await token(http)})
    assert denied.status_code == 400 and 'login_registration_forbidden' in denied.text


async def test_bound_agent_returns_to_same_self_held_identity(agentid_account):
    app, key, subject, http, _ = agentid_account
    await browser_login((app, key, subject, http))
    async with app.metadata.transaction(write=False) as tx:
        before = tx.rows('SELECT id,body FROM credentials WHERE subject=?', (subject,))
        vaults = tx.one('SELECT COUNT(*) FROM custodial_vault WHERE subject=?', (subject,))[0]
    state, value = await begin(agentid_account, mode='bind')
    assert (await callback(agentid_account, state)).status_code == 303
    result = await post(agentid_account, '/-/login/complete', {'csrf': value, 'state': state})
    result = await approve_if_pending(agentid_account, result)
    assert result.status_code == 303, result.text
    assert (await session_source(app, http))['subject'] == subject
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(subject)).kind == 'registered'
        after = dict(tx.rows('SELECT id,body FROM credentials WHERE subject=?', (subject,)))
        assert all(after[key_id] == body for key_id, body in before)
        assert (
            tx.one('SELECT COUNT(*) FROM custodial_vault WHERE subject=?', (subject,))[0] == vaults
        )
    http.cookies.clear()
    state, value = await begin(agentid_account)
    assert (await callback(agentid_account, state)).status_code == 303
    result = await post(agentid_account, '/-/login/complete', {'csrf': value, 'state': state})
    assert result.status_code == 303, result.text
    assert (await session_source(app, http))['subject'] == subject


async def test_callback_cannot_move_to_another_browser(agentid_account):
    _, _, _, http, _ = agentid_account
    state, _ = await begin(agentid_account)
    http.cookies.clear()
    await token(http)
    denied = await callback(agentid_account, state)
    assert denied.status_code == 400 and 'invalid_login_transaction' in denied.text
