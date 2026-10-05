"""Verified factor, immutable intent, custody and live source boundaries."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_service import NOW, call, register

from msg.core.codec import canonical, wire
from msg.core.errors import Failure
from msg.core.models import CapabilityGrant, Credential, Principal, Scope
from msg.core.requests import request_for
from msg.login_config import PROVIDERS, LoginConfig, LoginProvider, load_login
from msg.oauth_config import OAuthConfig
from msg.security.crypto import Ed25519Signer
from msg.security.login import (
    VerifiedLogin,
    approve_browser_login_intent,
    bind_login,
    complete_login,
    create_login_intent,
    list_bindings,
    lookup_binding,
    remove_login,
    request_login_approval,
    trusted_login,
)
from msg.security.oauth import OAuthService, get
from msg.storage.login_schema import LOGIN_SCHEMA


class LoginSettings:
    def __init__(self, original):
        self.original = original
        self.oauth = OAuthConfig(enabled=True)
        self.login = LoginConfig(
            enabled=True,
            registration='legacy',
            providers=tuple(LoginProvider(name, enabled=True) for name in sorted(PROVIDERS)),
        )

    def __getattr__(self, name):
        return getattr(self.original, name)


@pytest.fixture
async def login_app(installed):
    app, _ = installed
    app.settings = LoginSettings(app.settings)
    app.authenticator.oauth_config = app.settings.oauth
    app._login_clock = [NOW]
    app.clock = lambda: app._login_clock[0]
    app.authenticator.clock = app.executor.clock = app.certificates.clock = app.clock
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(LOGIN_SCHEMA, write=True)
    return app


async def approved_binding(app, key, subject, verified, request_id):
    approval = await request_login_approval(app, verified, subject, request_id=request_id)
    approved = await call(
        app,
        'identity.oauth_approve',
        {'user_code': approval.user_code, 'decision': 'approve'},
        key=key,
        subject=subject,
    )
    assert approved.status == 'ok', wire(approved)
    completion = await bind_login(
        app, verified, request_id=request_id, intent_id=approval.intent_id
    )
    assert completion.result.status == 'ok', wire(completion.result)
    return completion


def test_login_config_defaults_and_invalid_container_types():
    assert load_login({}, 'http://testserver').registration == 'legacy'
    assert load_login({'enabled': True}, 'http://testserver').registration == 'provider_only'
    valid = load_login(
        {
            'enabled': True,
            'providers': {
                'email': {
                    'enabled': True,
                    'transport': 'sequenzy',
                    'credential_file': '/private/mail',
                    'sender': 'MSG <msg@example.test>',
                },
                'chatgpt': {
                    'enabled': True,
                    'client_id': 'oaiapp_example',
                    'token_auth_method': 'none',
                },
            },
        },
        'https://msg.example.test',
    )
    assert valid.provider('email').credential_file == '/private/mail'
    for data in [
        {'registration': []},
        {'providers': {'google': {'token_auth_method': {}}}},
        {'providers': {'email': {'transport': []}}},
        {'providers': {'google': {'credential_file': 'relative'}}},
        {'providers': {'chatgpt': {'enabled': True, 'client_id': 'ordinary-client'}}},
    ]:
        with pytest.raises(Failure):
            load_login(data, 'https://msg.example.test')


@pytest.mark.asyncio
async def test_explicit_registration_reuses_custody_and_never_merges_email(login_app):
    app = login_app
    factor = VerifiedLogin('google', 'stable-google-subject')
    with pytest.raises(Failure, match='login_registration_required'):
        await complete_login(app, factor, request_id='unknown-login')
    first = await complete_login(app, factor, request_id='register-google', handle='google-owner')
    assert first.result.status == 'ok', wire(first.result)
    async with app.metadata.transaction(write=False) as tx:
        initial = tx.one(
            'SELECT signing_key_id,encryption_key_id FROM custodial_vault WHERE subject=?',
            (first.result.subject,),
        )
        assert (await tx.subject(first.result.subject)).kind == 'custodial'
    again = await complete_login(app, factor, request_id='login-google')
    retry = await complete_login(app, factor, request_id='login-google')
    assert again.result.status == retry.result.status == 'ok'
    assert retry.result.replayed and retry.cookie == again.cookie
    assert again.result.subject == first.result.subject
    other = await complete_login(
        app,
        VerifiedLogin('email', 'stable-google-subject'),
        request_id='register-email',
        handle='email-owner',
    )
    assert other.result.status == 'ok' and other.result.subject != first.result.subject
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT signing_key_id,encryption_key_id FROM custodial_vault WHERE subject=?',
                (first.result.subject,),
            )
            == initial
        )
        assert (
            tx.one('SELECT COUNT(*) FROM login_bindings WHERE subject=?', (first.result.subject,))[
                0
            ]
            == 1
        )
    text = canonical(wire(first.result)).decode()
    assert 'stable-google-subject' not in text and 'cookie' not in text
    assert 'PRIVATE KEY' not in text and 'AGE-SECRET-KEY' not in text


@pytest.mark.asyncio
async def test_nonregistration_factors_and_direct_operation_cannot_create(login_app):
    app = login_app
    for provider in ['github', 'chatgpt', 'passkey']:
        with pytest.raises(Failure, match='login_binding_required'):
            await complete_login(
                app, VerifiedLogin(provider, 'unbound'), request_id=provider, handle='unbound-owner'
            )
    direct = await call(app, 'identity.login_complete', {'intent_id': 'asserted'})
    assert direct.status == 'error' and direct.error.code == 'verified_login_required'
    app.settings.login = replace(app.settings.login, registration='provider_only')
    key = Ed25519Signer.generate()
    from msg.core.codec import b64
    from msg.security.age_keys import generate_age_key
    from msg.security.crypto import subject_id

    _, recipient = generate_age_key()
    denied = await call(
        app,
        'identity.register',
        {
            'handle': 'blocked-old-signup',
            'public_key': b64(key.public_key),
            'encryption_recipient': recipient,
        },
        key=key,
        subject=subject_id(key.public_key),
        contract_version=2,
    )
    assert denied.status == 'error' and denied.error.code == 'provider_registration_required'


@pytest.mark.asyncio
async def test_selfcustody_binding_retains_actual_secondary_ceiling(login_app):
    app = login_app
    primary, subject, _ = await register(app, 'self-owner')
    secondary = Ed25519Signer.generate()
    ceiling = (
        CapabilityGrant(
            capability='identity.basic',
            version=1,
            scope=Scope(resource_id=subject),
            operations=frozenset({'identity.oauth_approve@1', 'discovery.get@1'}),
            constraints={},
        ),
    )
    async with app.metadata.transaction(write=True) as tx:
        await tx.save_credential(
            Credential(
                id=secondary.key_id,
                subject_id=subject,
                kind='signing_key',
                verifier=secondary.public_key,
                ceiling=ceiling,
                not_before=NOW,
                expires_at=None,
                revoked_at=None,
            ),
            0,
        )
    factor = VerifiedLogin('github', 'github-immutable-id')
    bound = await approved_binding(app, secondary, subject, factor, 'bind-secondary')
    async with app.metadata.transaction(write=False) as tx:
        binding = lookup_binding(tx, factor.provider, factor.sub)
        assert binding.parent == secondary.key_id and binding.ceiling == ceiling
        assert binding.parent != primary.key_id
        assert (await tx.subject(subject)).kind == 'registered'
        assert tx.one('SELECT COUNT(*) FROM custodial_vault WHERE subject=?', (subject,))[0] == 0
        _, source = await OAuthService(app).session(tx, bound.cookie)
        browser = await tx.credential(get(tx, source['session'], NOW)[1]['browser_credential'])
        assert all('identity.oauth_approve@1' not in grant.operations for grant in browser.ceiling)
        methods = await list_bindings(tx, subject)
        assert methods == [{'binding_id': binding.id, 'provider': 'github'}]
    async with app.metadata.transaction(write=True) as tx:
        original = await tx.credential(secondary.key_id)
        await tx.save_credential(replace(original, ceiling=()), 0)
    with pytest.raises(Failure, match='invalid_grant'):
        async with app.metadata.transaction(write=False) as tx:
            await OAuthService(app).session(tx, bound.cookie)


@pytest.mark.asyncio
async def test_fresh_management_is_cookie_only_and_immutable_intent(login_app):
    app = login_app
    first = await complete_login(
        app,
        VerifiedLogin('google', 'manager-google'),
        request_id='manager-register',
        handle='manager-owner',
    )
    assert first.result.status == 'ok', wire(first.result)
    factor = VerifiedLogin('email', 'manager@example.test')
    approval = await request_login_approval(
        app, factor, first.result.subject, request_id='manager-bind'
    )
    async with app.metadata.transaction(write=True) as tx:
        assert await approve_browser_login_intent(app, tx, first.cookie, approval.intent_id)
    bound = await bind_login(app, factor, request_id='manager-bind', intent_id=approval.intent_id)
    assert bound.result.status == 'ok', wire(bound.result)
    async with app.metadata.transaction(write=False) as tx:
        _, source = await OAuthService(app).session(tx, first.cookie)
        assert 'login_management' not in source
        binding = lookup_binding(tx, factor.provider, factor.sub)
        assert not binding.source.get('session') and not binding.source.get('login_binding')
    with pytest.raises(Failure, match='login_approval_required'):
        await bind_login(
            app, VerifiedLogin('github', 'unapproved'), source, request_id='fake-source'
        )
    wrong = await bind_login(
        app,
        VerifiedLogin('email', 'another@example.test'),
        request_id='manager-bind',
        intent_id=approval.intent_id,
    )
    assert wrong.result.status == 'error' and wrong.result.error.code == 'login_intent_mismatch'
    app._login_clock[0] = NOW + timedelta(seconds=301)
    stale = await request_login_approval(
        app,
        VerifiedLogin('github', 'fresh-required'),
        first.result.subject,
        request_id='stale-session',
    )
    with pytest.raises(Failure, match='login_management_expired'):
        async with app.metadata.transaction(write=True) as tx:
            await approve_browser_login_intent(app, tx, first.cookie, stale.intent_id)


@pytest.mark.asyncio
async def test_legacy_browser_has_no_management_marker(login_app):
    app = login_app
    key, subject, _ = await register(app, 'legacy-session-owner')
    async with app.metadata.transaction(write=True) as tx:
        principal = Principal(
            actor=subject,
            subject=subject,
            credential_id=key.key_id,
            method='signature',
            certificates=(),
            ceiling=(await tx.credential(key.key_id)).ceiling,
        )
        source = await OAuthService(app).source(tx, principal)
        old = await OAuthService(app).create_browser_session(tx, dict(source, scopes=['msg.read']))
    approval = await request_login_approval(
        app, VerifiedLogin('github', 'not-bound'), subject, request_id='old-cookie-management'
    )
    async with app.metadata.transaction(write=True) as tx:
        assert (
            await approve_browser_login_intent(app, tx, old['cookie'], approval.intent_id) is False
        )
        assert get(tx, approval.intent_id, NOW)[1]['status'] == 'pending'


@pytest.mark.asyncio
async def test_removal_revokes_factor_descendants_without_reviving_on_rebind(login_app):
    app = login_app
    google = VerifiedLogin('google', 'revocation-google')
    first = await complete_login(app, google, request_id='revoke-register', handle='revoke-owner')
    assert first.result.status == 'ok', wire(first.result)
    email = VerifiedLogin('email', 'revocation@example.test')
    async with app.metadata.transaction(write=False) as tx:
        _, source = await OAuthService(app).session(tx, first.cookie)
    second = await bind_login(
        app, email, source, request_id='bind-email', session_cookie=first.cookie
    )
    assert second.result.status == 'ok', wire(second.result)
    async with app.metadata.transaction(write=True) as tx:
        _, source = await OAuthService(app).session(tx, first.cookie)
        tokens = await OAuthService(app).tokens(
            tx,
            dict(
                source,
                client_id='msg-cli',
                scopes=['openid', 'profile', 'msg.read', 'offline_access'],
            ),
        )
        binding = lookup_binding(tx, google.provider, google.sub)
    approval = await request_login_approval(
        app,
        None,
        first.result.subject,
        action='remove',
        binding_id=binding.id,
        request_id='remove-google',
    )
    async with app.metadata.transaction(write=True) as tx:
        assert await approve_browser_login_intent(app, tx, second.cookie, approval.intent_id)
    removed = await remove_login(app, request_id='remove-google', intent_id=approval.intent_id)
    assert removed.status == 'ok' and removed.data['removed']
    async with app.metadata.transaction(write=False) as tx:
        await OAuthService(app).session(tx, second.cookie)
        assert (await tx.subject(first.result.subject)).auth_version == 0
    for check in ['session', 'access', 'refresh']:
        with pytest.raises(Failure):
            async with app.metadata.transaction(write=True) as tx:
                if check == 'session':
                    await OAuthService(app).session(tx, first.cookie)
                elif check == 'access':
                    await OAuthService(app).bearer(tx, tokens['access_token'])
                else:
                    await OAuthService(app).exchange(
                        tx,
                        {
                            'grant_type': 'refresh_token',
                            'client_id': 'msg-cli',
                            'refresh_token': tokens['refresh_token'],
                        },
                    )
    rebinding = await request_login_approval(
        app, google, first.result.subject, request_id='rebind-google'
    )
    async with app.metadata.transaction(write=True) as tx:
        assert await approve_browser_login_intent(app, tx, second.cookie, rebinding.intent_id)
    new = await bind_login(app, google, request_id='rebind-google', intent_id=rebinding.intent_id)
    assert new.result.status == 'ok', wire(new.result)
    with pytest.raises(Failure):
        async with app.metadata.transaction(write=False) as tx:
            await OAuthService(app).session(tx, first.cookie)


@pytest.mark.asyncio
async def test_context_is_exact_and_resets_after_use(login_app):
    app = login_app
    factor = VerifiedLogin('google', 'context-subject')
    async with app.metadata.transaction(write=True) as tx:
        intent_id, _ = await create_login_intent(
            app,
            tx,
            factor,
            action='complete',
            request_id='context-complete',
            handle='context-owner',
        )
    request = request_for(
        'identity.login_complete',
        {'intent_id': intent_id, 'handle': 'context-owner'},
        app.settings.service_url,
        request_id='context-complete',
        expires_at=NOW + timedelta(seconds=100),
    )
    with trusted_login(app, factor, intent_id=intent_id, request=request):
        changed = request_for(
            'identity.login_complete',
            {'intent_id': intent_id, 'handle': 'other-owner'},
            app.settings.service_url,
            request_id='context-complete',
            expires_at=NOW + timedelta(seconds=100),
        )
        denied = await app.executor.execute(changed)
        assert denied.status == 'error' and denied.error.code == 'verified_login_required'
    denied = await app.executor.execute(request)
    assert denied.status == 'error' and denied.error.code == 'verified_login_required'
