"""Passkey cryptography and one-use challenges against real PostgreSQL."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from fido2 import cbor
from fido2.cose import ES256, EdDSA
from fido2.webauthn import AttestedCredentialData, AuthenticatorData
from starlette.applications import Starlette
from starlette.routing import Route
from test_oauth import browser_login, oauth  # noqa: F401
from test_service import NOW, call

from msg.core.codec import b64, canonical, digest, loads, unb64
from msg.core.errors import Failure
from msg.login_config import load_login
from msg.security.oauth import OAuthService, save, state_id
from msg.security.passkey import (
    PasskeyServer,
    begin_challenge,
    finalize_authentication,
    finalize_registration,
    read_challenge,
    trusted_passkey,
    user_handle,
)
from msg.transports.oauth_http import csrf
from msg.transports.passkey_login import dispatch

ORIGIN = 'https://msg.example'
SUBJECT = 'u_passkey_owner'
FLOW = 'independent-http-only-cookie-value-12345'


class Authenticator:
    def __init__(self, algorithm=-7):
        self.key = (
            ec.generate_private_key(ec.SECP256R1())
            if algorithm == -7
            else ed25519.Ed25519PrivateKey.generate()
        )
        self.public = (ES256 if algorithm == -7 else EdDSA).from_cryptography_key(
            self.key.public_key()
        )
        self.id = sha256(
            self.key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).digest()

    def registration(
        self,
        state,
        *,
        flags=0x45,
        counter=0,
        origin=ORIGIN,
        rp='msg.example',
        embedded_id=None,
        public=None,
        client_extra=None,
    ):
        client = canonical(
            dict(
                type='webauthn.create',
                challenge=state['challenge'],
                origin=origin,
                **(client_extra or {}),
            )
        )
        credential = AttestedCredentialData.create(
            bytes(16), embedded_id or self.id, public or self.public
        )
        auth_data = AuthenticatorData.create(
            sha256(rp.encode()).digest(), flags, counter, credential
        )
        return {
            'id': b64(self.id),
            'rawId': b64(self.id),
            'type': 'public-key',
            'response': {
                'clientDataJSON': b64(client),
                'attestationObject': b64(
                    cbor.encode({'fmt': 'none', 'attStmt': {}, 'authData': bytes(auth_data)})
                ),
            },
        }

    def authentication(
        self,
        state,
        *,
        flags=5,
        counter=1,
        origin=ORIGIN,
        rp='msg.example',
        handle=None,
        client_extra=None,
    ):
        client = canonical(
            dict(
                type='webauthn.get',
                challenge=state['challenge'],
                origin=origin,
                **(client_extra or {}),
            )
        )
        auth_data = AuthenticatorData.create(sha256(rp.encode()).digest(), flags, counter)
        signed = bytes(auth_data) + sha256(client).digest()
        signature = (
            self.key.sign(signed, ec.ECDSA(hashes.SHA256()))
            if isinstance(self.key, ec.EllipticCurvePrivateKey)
            else self.key.sign(signed)
        )
        return {
            'id': b64(self.id),
            'rawId': b64(self.id),
            'type': 'public-key',
            'response': {
                'clientDataJSON': b64(client),
                'authenticatorData': b64(bytes(auth_data)),
                'signature': b64(signature),
                'userHandle': b64(user_handle(SUBJECT) if handle is None else handle),
            },
        }


@pytest.fixture
def ceremony():
    server = PasskeyServer(ORIGIN)
    device = Authenticator()
    options, state = server.registration_options(SUBJECT, 'owner')
    proof = server.verify_registration(state, device.registration(state), SUBJECT)
    return server, device, options, state, proof


def test_registration_and_discoverable_login_require_uv_and_valid_signatures():
    server, device = PasskeyServer(ORIGIN), Authenticator()
    options, state = server.registration_options(SUBJECT, 'owner')
    assert options['publicKey']['authenticatorSelection']['userVerification'] == 'required'
    assert options['publicKey']['authenticatorSelection']['residentKey'] == 'required'
    assert {item['alg'] for item in options['publicKey']['pubKeyCredParams']} == {-7}
    proof = server.verify_registration(state, device.registration(state), SUBJECT)
    options, state = server.authentication_options()
    assert not options['publicKey'].get('allowCredentials')
    assert options['publicKey']['userVerification'] == 'required'
    authenticated = server.verify_authentication(
        state, device.authentication(state), SUBJECT, proof.provider_data
    )
    assert authenticated.provider_data['sign_count'] == 1
    assert authenticated.previous_data_digest == digest(proof.provider_data)
    assert repr(authenticated).startswith('<msg.security.passkey.PasskeyProof object')


@pytest.mark.parametrize(
    'change',
    [
        {'origin': 'https://evil.example'},
        {'rp': 'evil.example'},
        {'flags': 0x41},
        {'flags': 0x44},
        {'flags': 0x55},
        {'embedded_id': b'other'},
        {'client_extra': {'crossOrigin': True}},
        {'client_extra': {'crossOrigin': 0}},
        {'client_extra': {'topOrigin': ORIGIN}},
    ],
)
def test_registration_rejects_origin_rp_uv_up_backup_id_and_embedding(ceremony, change):
    server, device, _, state, _ = ceremony
    with pytest.raises(Failure):
        server.verify_registration(state, device.registration(state, **change), SUBJECT)


def test_registration_rejects_invalid_curve_and_algorithm(ceremony):
    server, device, _, state, _ = ceremony
    for public in (
        ES256({1: 2, 3: -7, -1: 1, -2: bytes(32), -3: bytes(32)}),
        ES256(dict(device.public)),
    ):
        if public[-2] != bytes(32):
            public[3] = -35
        with pytest.raises(Failure):
            server.verify_registration(state, device.registration(state, public=public), SUBJECT)


def test_ed25519_and_weak_identity_point_are_not_supported(ceremony):
    server, device, _, state, _ = ceremony
    weak = EdDSA({1: 1, 3: -8, -1: 6, -2: b'\x01' + bytes(31)})
    for public in (weak, Authenticator(-8).public):
        with pytest.raises(Failure):
            server.verify_registration(state, device.registration(state, public=public), SUBJECT)


@pytest.mark.parametrize(
    'change',
    [
        {'origin': 'https://evil.example'},
        {'rp': 'evil.example'},
        {'flags': 1},
        {'flags': 4},
        {'flags': 0x15},
        {'flags': 0x0D},
        {'handle': b'other'},
        {'handle': b''},
        {'client_extra': {'crossOrigin': True}},
        {'client_extra': {'topOrigin': ORIGIN}},
    ],
)
def test_authentication_rejects_origin_rp_flags_handle_and_embedding(ceremony, change):
    server, device, _, _, proof = ceremony
    _, state = server.authentication_options()
    with pytest.raises(Failure):
        server.verify_authentication(
            state, device.authentication(state, **change), SUBJECT, proof.provider_data
        )


@pytest.mark.parametrize(
    'case',
    ['challenge', 'signature', 'raw_id', 'null_handle', 'duplicate_json', 'type', 'state_origin'],
)
def test_authentication_rejects_malformed_and_swapped_evidence(ceremony, case):
    server, device, _, _, proof = ceremony
    _, state = server.authentication_options()
    response = device.authentication(state)
    if case == 'challenge':
        changed = dict(state, challenge=b64(bytes(32)))
        response = device.authentication(changed)
    elif case == 'signature':
        response['response']['signature'] = b64(bytes(64))
    elif case == 'raw_id':
        response['id'] = response['rawId'] = b64(b'other')
    elif case == 'null_handle':
        response['response']['userHandle'] = None
    elif case == 'duplicate_json':
        data = unb64(response['response']['clientDataJSON'])
        response['response']['clientDataJSON'] = b64(
            data[:-1] + b',"origin":"https://msg.example"}'
        )
    elif case == 'type':
        response['type'] = 'password'
    else:
        state = dict(state, origin='https://other.example')
    with pytest.raises(Failure):
        server.verify_authentication(state, response, SUBJECT, proof.provider_data)


def test_counters_and_synced_backup_state_follow_stored_credential(ceremony):
    server, device, _, _, proof = ceremony
    _, state = server.authentication_options()
    zero = server.verify_authentication(
        state, device.authentication(state, counter=0), SUBJECT, proof.provider_data
    )
    assert zero.provider_data['sign_count'] == 0
    one = server.verify_authentication(
        state, device.authentication(state, counter=1), SUBJECT, proof.provider_data
    )
    for count in (0, 1):
        with pytest.raises(Failure, match='passkey_counter_changed'):
            server.verify_authentication(
                state, device.authentication(state, counter=count), SUBJECT, one.provider_data
            )
    _, registration_state = server.registration_options(SUBJECT, 'owner')
    synced = server.verify_registration(
        registration_state, device.registration(registration_state, flags=0x4D), SUBJECT
    )
    backed_up = server.verify_authentication(
        state, device.authentication(state, flags=0x1D), SUBJECT, synced.provider_data
    )
    assert backed_up.provider_data['backup_eligible'] and backed_up.provider_data['backup_state']


@pytest.mark.parametrize(
    'origin',
    [
        'http://msg.example',
        'https://msg.example/',
        'https://u:p@msg.example',
        'https://msg.example?x=1',
        'https://msg.example:0',
        ' https://msg.example',
    ],
)
def test_configured_origin_is_an_exact_secure_origin(origin):
    with pytest.raises(Failure, match='invalid_passkey_origin'):
        PasskeyServer(origin)


@pytest.mark.asyncio
async def test_challenge_atomic_consumption_rollback_binding_digest_and_context_reset(
    installed, ceremony
):
    app, _ = installed
    server, device, _, state, proof = ceremony
    verified = SimpleNamespace(provider='passkey', sub=proof.credential_id)
    async with app.metadata.transaction(write=True) as tx:
        challenge = begin_challenge(
            tx, state, mode='registration', now=NOW, flow=FLOW, subject=SUBJECT
        )
    for changes in (
        {'flow': FLOW + 'other'},
        {'subject': 'other'},
        {'mode': 'authentication'},
        {'now': NOW + timedelta(seconds=180)},
    ):
        async with app.metadata.transaction(write=False) as tx:
            params = {
                'mode': 'registration',
                'now': NOW,
                'flow': FLOW,
                'subject': SUBJECT,
            } | changes
            with pytest.raises(Failure):
                read_challenge(tx, challenge, **params)
    with trusted_passkey(proof, challenge_id=challenge, flow=FLOW):
        with pytest.raises(RuntimeError):
            async with app.metadata.transaction(write=True) as tx:
                finalize_registration(tx, verified, SUBJECT, now=NOW)
                raise RuntimeError('later session creation failure')
        async with app.metadata.transaction(write=True) as tx:
            stored = finalize_registration(tx, verified, SUBJECT, now=NOW)
        async with app.metadata.transaction(write=True) as tx:
            with pytest.raises(Failure):
                finalize_registration(tx, verified, SUBJECT, now=NOW)
    async with app.metadata.transaction(write=True) as tx:
        with pytest.raises(Failure, match='passkey_verification_required'):
            finalize_registration(tx, verified, SUBJECT, now=NOW)
    _, auth_state = server.authentication_options()
    auth_proof = server.verify_authentication(
        auth_state, device.authentication(auth_state), SUBJECT, stored
    )
    async with app.metadata.transaction(write=True) as tx:
        challenge = begin_challenge(tx, auth_state, mode='authentication', now=NOW, flow=FLOW)
    with trusted_passkey(auth_proof, challenge_id=challenge, flow=FLOW):
        async with app.metadata.transaction(write=True) as tx:
            with pytest.raises(Failure, match='passkey_state_changed'):
                finalize_authentication(tx, verified, SUBJECT, dict(stored, sign_count=7), now=NOW)
            updated = finalize_authentication(tx, verified, SUBJECT, stored, now=NOW)
        assert updated['sign_count'] == 1


@pytest.mark.asyncio
async def test_challenge_rejects_changed_kind_and_issued_state(installed, ceremony):
    app, _ = installed
    _, _, _, state, proof = ceremony
    verified = SimpleNamespace(provider='passkey', sub=proof.credential_id)
    async with app.metadata.transaction(write=True) as tx:
        challenge = begin_challenge(
            tx, state, mode='registration', now=NOW, flow=FLOW, subject=SUBJECT
        )
        body = read_challenge(
            tx, challenge, mode='registration', now=NOW, flow=FLOW, subject=SUBJECT
        )
        changed = deepcopy(body)
        changed['state']['origin'] = 'https://other.example'
        save(tx, state_id('passkey', challenge), changed)
    with trusted_passkey(proof, challenge_id=challenge, flow=FLOW):
        async with app.metadata.transaction(write=True) as tx:
            with pytest.raises(Failure):
                finalize_registration(tx, verified, SUBJECT, now=NOW)

            save(tx, state_id('passkey', challenge), body)
            tx.execute(
                'UPDATE oauth_states SET kind=? WHERE id=?',
                ('other', state_id('passkey', challenge)),
                write=True,
            )
            with pytest.raises(Failure):
                finalize_registration(tx, verified, SUBJECT, now=NOW)


@pytest.fixture
async def passkey_http(oauth):  # noqa: F811
    app, key, subject, original = oauth
    session = await browser_login(oauth)
    app.settings = replace(
        app.settings,
        service_url='http://localhost',
        login=load_login(
            {'enabled': True, 'providers': {'passkey': {'enabled': True}}}, 'http://localhost'
        ),
    )
    app.authenticator.service = app.settings.service_url

    async def endpoint(request):
        async def source():
            async with app.metadata.transaction(write=False) as tx:
                return (
                    await OAuthService(app).session(tx, request.cookies.get('msg_session', ''))
                )[1]

        try:
            return await dispatch(
                request,
                app=app,
                source=source,
                browser=request.cookies.get('flow', ''),
                csrf=csrf(FLOW),
                session_cookie=request.cookies.get('msg_session'),
            )
        except Failure as exc:
            from msg.transports.oauth_http import json

            return json({'error': exc.code}, 400)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=Starlette(
                routes=[Route('/-/login/passkey/{path:path}', endpoint, methods=['POST'])]
            )
        ),
        base_url=app.settings.service_url,
    ) as http:
        http.cookies.set('msg_session', session)
        http.cookies.set('flow', FLOW)
        yield app, key, subject, http


async def passkey_post(fixture, path, body):
    app, _, _, http = fixture
    return await http.post(
        '/-/login/passkey/' + path,
        json={'csrf': csrf(FLOW), **body},
        headers={'Origin': app.settings.service_url},
    )


async def bound_passkey(fixture):
    app, key, subject, _ = fixture
    device = Authenticator()
    began = await passkey_post(fixture, 'bind/options', {})
    assert began.status_code == 200, began.text
    data = began.json()
    state = {'challenge': data['options']['publicKey']['challenge']}
    response = device.registration(state, origin=app.settings.service_url, rp='localhost')
    pending = await passkey_post(
        fixture, 'bind/complete', {'challenge_id': data['challenge_id'], 'credential': response}
    )
    assert pending.status_code == 200 and pending.json()['approval_required'], pending.text
    approved = await call(
        app,
        'identity.oauth_approve',
        {'user_code': pending.json()['user_code'], 'decision': 'approve'},
        key=key,
        subject=subject,
    )
    assert approved.status == 'ok', approved.error
    finished = await passkey_post(fixture, 'bind/complete', {'challenge_id': data['challenge_id']})
    assert finished.status_code == 200, finished.text
    return device, data['challenge_id']


@pytest.mark.asyncio
async def test_http_bind_existing_owner_login_remove_and_no_account_creation(passkey_http):
    app, key, subject, http = passkey_http
    async with app.metadata.transaction(write=False) as tx:
        accounts = tx.one('SELECT COUNT(*) FROM identities')[0]
        vaults = (
            tx.one('SELECT COUNT(*) FROM custodial_vault')[0]
            if tx.one("SELECT to_regclass('custodial_vault')")[0]
            else None
        )
    device, registration_challenge = await bound_passkey(passkey_http)
    replay = await passkey_post(
        passkey_http, 'bind/complete', {'challenge_id': registration_challenge}
    )
    assert replay.status_code == 400
    http.cookies.delete('msg_session')
    started = (await passkey_post(passkey_http, 'options', {})).json()
    state = {'challenge': started['options']['publicKey']['challenge']}
    response = device.authentication(
        state, origin=app.settings.service_url, rp='localhost', handle=user_handle(subject)
    )
    logged_in = await passkey_post(
        passkey_http, 'complete', {'challenge_id': started['challenge_id'], 'credential': response}
    )
    assert logged_in.status_code == 200, logged_in.text
    async with app.metadata.transaction(write=False) as tx:
        _, source = await OAuthService(app).session(tx, http.cookies.get('msg_session'))
        assert source['subject'] == subject
        assert tx.one('SELECT COUNT(*) FROM identities')[0] == accounts
        binding = tx.one(
            'SELECT id,provider_data FROM login_bindings WHERE provider=? AND subject=?',
            ('passkey', subject),
        )
        assert loads(binding[1])['sign_count'] == 1
        if vaults is not None:
            assert tx.one('SELECT COUNT(*) FROM custodial_vault')[0] == vaults
    from msg.security.login import remove_login, request_login_approval

    removal = await request_login_approval(
        app,
        None,
        subject,
        action='remove',
        binding_id=binding[0],
        request_id='remove-passkey-fixture',
    )
    assert (
        await call(
            app,
            'identity.oauth_approve',
            {'user_code': removal.user_code, 'decision': 'approve'},
            key=key,
            subject=subject,
        )
    ).status == 'ok'
    assert (
        await remove_login(app, request_id='remove-passkey-fixture', intent_id=removal.intent_id)
    ).status == 'ok'
    async with app.metadata.transaction(write=False) as tx:
        with pytest.raises(Failure):
            await OAuthService(app).session(tx, http.cookies.get('msg_session'))
    started = (await passkey_post(passkey_http, 'options', {})).json()
    response = device.authentication(
        {'challenge': started['options']['publicKey']['challenge']},
        origin=app.settings.service_url,
        rp='localhost',
        handle=user_handle(subject),
        counter=2,
    )
    denied = await passkey_post(
        passkey_http, 'complete', {'challenge_id': started['challenge_id'], 'credential': response}
    )
    assert denied.status_code == 400 and denied.json()['error'] == 'login_binding_required'


@pytest.mark.asyncio
async def test_http_unbound_passkey_csrf_and_owner_rejection(passkey_http):
    app, _, subject, http = passkey_http
    bad = await http.post(
        '/-/login/passkey/options',
        json={'csrf': csrf(FLOW)},
        headers={'Origin': 'http://evil.example'},
    )
    assert bad.status_code == 400
    assert (await passkey_post(passkey_http, 'options', {'subject': subject})).status_code == 400
    http.cookies.delete('msg_session')
    assert (await passkey_post(passkey_http, 'bind/options', {})).status_code == 400
    started = (await passkey_post(passkey_http, 'options', {})).json()
    device = Authenticator()
    denied = await passkey_post(
        passkey_http,
        'complete',
        {
            'challenge_id': started['challenge_id'],
            'credential': device.authentication(
                {'challenge': started['options']['publicKey']['challenge']},
                origin=app.settings.service_url,
                rp='localhost',
                handle=user_handle(subject),
            ),
        },
    )
    assert denied.status_code == 400 and denied.json()['error'] == 'login_binding_required'


@pytest.mark.asyncio
async def test_http_concurrent_finish_and_counter_rejection_leave_challenge_reusable(passkey_http):
    app, _, subject, _ = passkey_http
    device, _ = await bound_passkey(passkey_http)
    start = (await passkey_post(passkey_http, 'options', {})).json()
    assertion = device.authentication(
        {'challenge': start['options']['publicKey']['challenge']},
        origin=app.settings.service_url,
        rp='localhost',
        handle=user_handle(subject),
    )
    body = {'challenge_id': start['challenge_id'], 'credential': assertion}
    results = await asyncio.gather(
        passkey_post(passkey_http, 'complete', body), passkey_post(passkey_http, 'complete', body)
    )
    assert sorted(result.status_code for result in results) == [200, 400]
    start = (await passkey_post(passkey_http, 'options', {})).json()
    state = {'challenge': start['options']['publicKey']['challenge']}
    body = {
        'challenge_id': start['challenge_id'],
        'credential': device.authentication(
            state,
            origin=app.settings.service_url,
            rp='localhost',
            handle=user_handle(subject),
            counter=1,
        ),
    }
    denied = await passkey_post(passkey_http, 'complete', body)
    assert denied.status_code == 400 and denied.json()['error'] == 'passkey_counter_changed'
    body['credential'] = device.authentication(
        state,
        origin=app.settings.service_url,
        rp='localhost',
        handle=user_handle(subject),
        counter=2,
    )
    assert (await passkey_post(passkey_http, 'complete', body)).status_code == 200


@pytest.mark.asyncio
async def test_http_fresh_management_approval_and_expired_management_rejection(passkey_http):
    app, _, subject, _ = passkey_http
    await bound_passkey(passkey_http)
    second = Authenticator()
    start = (await passkey_post(passkey_http, 'bind/options', {})).json()
    result = await passkey_post(
        passkey_http,
        'bind/complete',
        {
            'challenge_id': start['challenge_id'],
            'credential': second.registration(
                {'challenge': start['options']['publicKey']['challenge']},
                origin=app.settings.service_url,
                rp='localhost',
            ),
        },
    )
    assert result.status_code == 200 and result.json()['bound'], result.text
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT COUNT(*) FROM login_bindings WHERE subject=? AND revoked_at IS NULL',
                (subject,),
            )[0]
            == 2
        )
    app._oauth_clock[0] += timedelta(seconds=301)
    third = Authenticator()
    start = (await passkey_post(passkey_http, 'bind/options', {})).json()
    result = await passkey_post(
        passkey_http,
        'bind/complete',
        {
            'challenge_id': start['challenge_id'],
            'credential': third.registration(
                {'challenge': start['options']['publicKey']['challenge']},
                origin=app.settings.service_url,
                rp='localhost',
            ),
        },
    )
    assert result.status_code == 400 and result.json()['error'] == 'login_management_expired'
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT COUNT(*) FROM login_bindings WHERE subject=? AND revoked_at IS NULL',
                (subject,),
            )[0]
            == 2
        )
