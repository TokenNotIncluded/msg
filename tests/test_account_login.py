"""真实执行器和 PostgreSQL 验证登录表单、共享身份与注册边界。"""

import re
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_oauth import browser_login, oauth  # noqa: F401

from msg.core.codec import wire
from msg.login_config import load_login
from msg.security.oauth import OAuthService
from msg.transports.http import create_app
from msg.transports.oauth_http import csrf


@pytest.fixture
async def account(oauth, monkeypatch):  # noqa: F811
    app, key, subject, _ = oauth
    credential = app.settings.config_dir / 'login-provider.fixture'
    credential.write_text('fixture-only-secret')
    credential.chmod(0o600)
    app.settings = replace(
        app.settings,
        login=load_login(
            {
                'enabled': True,
                'providers': {
                    'google': {
                        'enabled': True,
                        'client_id': 'google-fixture',
                        'credential_file': str(credential),
                    },
                    'github': {
                        'enabled': True,
                        'client_id': 'github-fixture',
                        'credential_file': str(credential),
                    },
                    'email': {'enabled': True},
                },
            },
            app.settings.service_url,
        ),
    )
    sent = []

    class Mail:
        async def send_code(self, address, code):
            sent.append((address, code))

    app._login_mail_sender = Mail()

    async def exchange(provider, *_args):
        return SimpleNamespace(stable_id=provider + '-external-fixture')

    monkeypatch.setattr('msg.transports.account_login.exchange_identity', exchange)
    from msg.transports.login_providers import authorization_url

    def authorization(provider, config, state, verifier, nonce, redirect_uri):
        # ASGI 的 testserver 无真实域名；提供方校验仍只接受 HTTPS 或回环 HTTP。
        assert redirect_uri == app.settings.service_url + '/-/login/callback/' + provider
        return authorization_url(
            provider,
            config,
            state,
            verifier,
            nonce,
            redirect_uri.replace('http://testserver/', 'http://localhost/'),
        )

    monkeypatch.setattr('msg.transports.account_login.authorization_url', authorization)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url=app.settings.service_url,
        headers={'Accept': 'application/json'},
    ) as http:
        yield app, key, subject, http, sent


async def token(http, path='/login'):
    response = await http.get(path)
    assert response.status_code == 200, response.text
    return csrf(http.cookies.get('msg_account_login'))


async def post(account, path, data):
    app, _, _, http, _ = account
    return await http.post(path, data=data, headers={'Origin': app.settings.service_url})


async def begin_provider(account, provider='google', mode='login', **values):
    app, _, _, http, _ = account
    response = await post(
        account,
        '/-/login/start',
        {'csrf': await token(http), 'provider': provider, 'mode': mode, **values},
    )
    assert response.status_code == 303, response.text
    state = parse_qs(urlsplit(response.headers['location']).query)['state'][0]
    callback = await http.get(
        '/-/login/callback/' + provider, params={'state': state, 'code': 'fixture-code'}
    )
    assert callback.status_code == 303, callback.text
    return state, csrf(http.cookies.get('msg_account_login'))


async def session_source(app, http):
    async with app.metadata.transaction(write=False) as tx:
        return (await OAuthService(app).session(tx, http.cookies.get('msg_session')))[1]


async def approve_if_pending(account, response):
    if response.status_code != 200 or 'msg auth approve ' not in response.text:
        return response
    from test_service import call

    app, key, subject, http, _ = account
    code = re.search(r'msg auth approve ([A-Z0-9]{8})', response.text)[1]
    approved = await call(
        app,
        'identity.oauth_approve',
        {'user_code': code, 'decision': 'approve'},
        key=key,
        subject=subject,
    )
    assert approved.status == 'ok', wire(approved)
    pending = re.search(r'name="pending" value="([^"]+)"', response.text)[1]
    return await post(
        account,
        '/-/login/approval',
        {'csrf': csrf(http.cookies.get('msg_account_login')), 'pending': pending},
    )


async def test_email_registration_consent_single_use_and_repeat_identity(account):
    app, _, _, http, sent = account
    form = {
        'csrf': await token(http, '/register'),
        'email': 'alice@example.org',
        'mode': 'register',
        'handle': 'email-alice',
    }
    denied = await post(account, '/-/login/email/start', form)
    assert denied.status_code == 400 and 'custodial_consent_required' in denied.text
    assert not sent
    response = await post(account, '/-/login/email/start', {**form, 'custody': 'yes'})
    assert response.status_code == 200, response.text
    challenge = re.search(r'name="challenge" value="([^"]+)"', response.text)[1]
    assert sent[0][1] not in response.text
    complete = {'csrf': form['csrf'], 'challenge': challenge, 'code': sent[0][1]}
    result = await post(account, '/-/login/email/complete', complete)
    assert result.status_code == 303, result.text
    original = await session_source(app, http)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(original['subject'])).kind == 'custodial'
        before = {
            table: tx.one('SELECT COUNT(*) FROM ' + table)[0]
            for table in ('identities', 'identity_keys', 'encryption_subkeys')
        }
    replay = await post(account, '/-/login/email/complete', complete)
    assert replay.status_code in {400, 403}
    app._oauth_clock[0] += timedelta(seconds=61)
    response = await post(
        account,
        '/-/login/email/start',
        {'csrf': await token(http), 'email': 'alice@example.org', 'mode': 'login'},
    )
    challenge = re.search(r'name="challenge" value="([^"]+)"', response.text)[1]
    result = await post(
        account,
        '/-/login/email/complete',
        {
            'csrf': csrf(http.cookies.get('msg_account_login')),
            'challenge': challenge,
            'code': sent[-1][1],
        },
    )
    assert result.status_code == 303, result.text
    assert (await session_source(app, http))['subject'] == original['subject']
    async with app.metadata.transaction(write=False) as tx:
        assert {table: tx.one('SELECT COUNT(*) FROM ' + table)[0] for table in before} == before


async def test_google_callback_is_ephemeral_and_github_cannot_register(account):
    app, _, _, http, _ = account
    async with app.metadata.transaction(write=False) as tx:
        before = tx.one('SELECT COUNT(*) FROM identities')[0]
    state, value = await begin_provider(
        account, mode='register', handle='google-alice', custody='yes'
    )
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM identities')[0] == before
    assert (await http.get('/login/finish', params={'state': state})).status_code == 200
    completed = await post(account, '/-/login/complete', {'csrf': value, 'state': state})
    assert completed.status_code == 303, completed.text
    first = await session_source(app, http)
    state, value = await begin_provider(account)
    completed = await post(account, '/-/login/complete', {'csrf': value, 'state': state})
    assert completed.status_code == 303, completed.text
    assert (await session_source(app, http))['subject'] == first['subject']
    state, value = await begin_provider(account, provider='github')
    denied = await post(account, '/-/login/complete', {'csrf': value, 'state': state})
    assert denied.status_code == 400 and 'login_binding_required' in denied.text
    denied = await post(
        account,
        '/-/login/start',
        {
            'csrf': await token(http),
            'provider': 'github',
            'mode': 'register',
            'handle': 'github-user',
            'custody': 'yes',
        },
    )
    assert denied.status_code == 400 and 'login_registration_forbidden' in denied.text
    assert (await http.get('/oauth/signup')).headers['location'] == '/register'
    assert (await post(account, '/oauth/signup', {'csrf': value})).status_code == 400


async def test_existing_self_custody_binding_keeps_keys_and_shared_subject(account):
    app, key, subject, http, _ = account
    await browser_login((app, key, subject, http))
    async with app.metadata.transaction(write=False) as tx:
        before = tx.rows('SELECT id,body FROM credentials WHERE subject=?', (subject,))
        vault = tx.one('SELECT COUNT(*) FROM custodial_vault WHERE subject=?', (subject,))[0]
    state, value = await begin_provider(account, provider='github', mode='bind')
    completed = await post(account, '/-/login/complete', {'csrf': value, 'state': state})
    completed = await approve_if_pending(account, completed)
    assert completed.status_code == 303, completed.text
    assert (await session_source(app, http))['subject'] == subject
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(subject)).kind == 'registered'
        after = dict(tx.rows('SELECT id,body FROM credentials WHERE subject=?', (subject,)))
        assert all(after[id] == body for id, body in before)
        assert (
            tx.one('SELECT COUNT(*) FROM custodial_vault WHERE subject=?', (subject,))[0]
            == vault
            == 0
        )
    state, value = await begin_provider(account, provider='github')
    completed = await post(account, '/-/login/complete', {'csrf': value, 'state': state})
    assert completed.status_code == 303, completed.text
    assert (await session_source(app, http))['subject'] == subject
    methods = await http.get('/account/login-methods')
    assert (
        methods.status_code == 200 and 'GitHub' in methods.text and '私钥由你自持' in methods.text
    )
    binding = re.search(r'name="binding_id" value="([^"]+)"', methods.text)[1]
    old_cookie = http.cookies.get('msg_session')
    removed = await post(
        account,
        '/-/login/remove',
        {'csrf': csrf(http.cookies.get('msg_account_login')), 'binding_id': binding},
    )
    assert removed.status_code == 303, removed.text
    from msg.core.errors import Failure

    with pytest.raises(Failure) as invalid:
        async with app.metadata.transaction(write=False) as tx:
            await OAuthService(app).session(tx, old_cookie)
    assert 'invalid_grant' in str(invalid.value)
    state, value = await begin_provider(account, provider='github')
    denied = await post(account, '/-/login/complete', {'csrf': value, 'state': state})
    assert denied.status_code == 400 and 'login_binding_' in denied.text


async def test_custodial_user_manages_bindings_with_fresh_web_login(account):
    app, _, _, http, _ = account
    state, value = await begin_provider(
        account, mode='register', handle='web-custody', custody='yes'
    )
    completed = await post(account, '/-/login/complete', {'csrf': value, 'state': state})
    assert completed.status_code == 303, completed.text
    owner = (await session_source(app, http))['subject']
    assert '/account/login-methods' in (await http.get('/', headers={'Accept': 'text/html'})).text
    state, value = await begin_provider(account, provider='github', mode='bind')
    completed = await post(account, '/-/login/complete', {'csrf': value, 'state': state})
    assert completed.status_code == 303, completed.text
    assert (await session_source(app, http))['subject'] == owner
    async with app.metadata.transaction(write=False) as tx:
        rows = tx.rows(
            'SELECT subject FROM login_bindings WHERE provider IN (?,?)', ('google', 'github')
        )
        assert len(rows) == 2 and all(row[0] == owner for row in rows)


async def test_login_http_rejects_cross_origin_and_mixed_authority(account):
    app, _, _, http, _ = account
    value = await token(http)
    args = {'csrf': value, 'provider': 'google', 'mode': 'login'}
    for headers in (
        {},
        {'Origin': 'https://attacker.example'},
        {'Origin': app.settings.service_url, 'Authorization': 'Bearer fake'},
    ):
        response = await http.post('/-/login/start', data=args, headers=headers)
        assert response.status_code == 403, response.text
    wrong = await post(account, '/-/login/start', {**args, 'csrf': 'wrong'})
    assert wrong.status_code == 403
    wrong = await http.get('/login', headers={'Host': 'attacker.example'})
    assert wrong.status_code == 400
    state, _ = await begin_provider(account)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as other:
        callback = await other.get(
            '/-/login/callback/google', params={'state': state, 'code': 'stolen'}
        )
        assert callback.status_code == 400
    denied = await http.post(
        '/-/login/start?provider=google', data=args, headers={'Origin': app.settings.service_url}
    )
    assert denied.status_code == 400
    assert 'fixture-code' not in wire(denied.headers)
