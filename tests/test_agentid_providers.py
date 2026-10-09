"""AgentID verification with real ES256 signatures and isolated provider HTTP."""

import base64
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from msg.core.codec import b64
from msg.core.errors import Failure
from msg.login_config import LoginConfig, load_login
from msg.security.login_jwt import token_key_id
from msg.transports import login_providers as providers

ISSUER = 'https://auth.agentid.com'
DISCOVERY = ISSUER + '/.well-known/openid-configuration'
AUTHORIZE = ISSUER + '/v0/authorize'
TOKEN = ISSUER + '/v0/token'
JWKS = ISSUER + '/v0/jwks.json'
CALLBACK = 'https://msg.example/-/login/callback/agentid'
NOW = datetime(2026, 10, 9, tzinfo=UTC)
NONCE = 'original-agentid-browser-nonce'
VERIFIER = 'v' * 43
CODE = 'fixture-authorization-code'
CLIENT = 'fixture-agentid-client'


@pytest.fixture(autouse=True)
def clear_caches():
    providers._jwks_cache.clear()
    providers._jwks_locks.clear()
    yield
    providers._jwks_cache.clear()
    providers._jwks_locks.clear()


@pytest.fixture(scope='module')
def signing_keys():
    return [ec.generate_private_key(ec.SECP256R1()) for _ in range(2)]


def public_jwk(key, kid='current'):
    return json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key())) | {
        'kid': kid,
        'alg': 'ES256',
        'use': 'sig',
        'key_ops': ['verify'],
    }


class AgentScenario:
    def __init__(self, keys, credential):
        self.keys = keys
        self.key = keys[0]
        self.kid = 'current'
        self.config = {'client_id': CLIENT, 'credential_file': str(credential)}
        self.claims = {
            'iss': ISSUER,
            'aud': CLIENT,
            'sub': 's' * 43,
            'iat': int(NOW.timestamp()) - 1,
            'exp': int(NOW.timestamp()) + 599,
            'nonce': NONCE,
            'actor_type': 'agent',
            'scope': 'openid email profile',
            'email': 'agent@example.org',
            'email_verified': True,
        }
        self.metadata = {
            'issuer': ISSUER,
            'authorization_endpoint': AUTHORIZE,
            'token_endpoint': TOKEN,
            'jwks_uri': JWKS,
            'id_token_signing_alg_values_supported': ['ES256'],
            'authorization_response_iss_parameter_supported': True,
        }
        self.jwks = {'keys': [public_jwk(self.key)]}
        self.requests = []
        self.responses = {}
        self.token_override = None

    def handle(self, request):
        self.requests.append(request)
        url = str(request.url)
        if url in self.responses:
            return self.responses[url]
        if url == DISCOVERY:
            return httpx.Response(200, json=self.metadata)
        if url == TOKEN:
            token = self.token_override or jwt.encode(
                self.claims, self.key, algorithm='ES256', headers={'kid': self.kid}
            )
            return httpx.Response(
                200, json={'id_token': token, 'access_token': 'fixture-not-retained'}
            )
        if url == JWKS:
            return httpx.Response(200, json=self.jwks, headers={'Cache-Control': 'max-age=3600'})
        pytest.fail('Unexpected provider endpoint: ' + url)

    async def exchange(self, now=NOW):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as client:
            return await providers.exchange_identity(
                'agentid', self.config, CODE, VERIFIER, NONCE, CALLBACK, now, client=client
            )

    def count(self, url):
        return Counter(str(request.url) for request in self.requests)[url]


@pytest.fixture
def flow(signing_keys, tmp_path):
    credential = tmp_path / 'client.secret'
    credential.write_text('fixture-only-secret\n')
    credential.chmod(0o600)
    return AgentScenario(signing_keys, credential)


def test_agentid_config_is_opt_in_and_uses_registered_basic(flow):
    assert not LoginConfig().provider('agentid').enabled
    config = load_login(
        {'enabled': True, 'providers': {'agentid': flow.config}}, 'https://msg.example'
    )
    provider = config.provider('agentid')
    assert provider.enabled
    assert provider.token_auth_method == 'client_secret_basic'
    assert provider.credential_file == flow.config['credential_file']


@pytest.mark.parametrize(
    'override',
    [
        {'client_id': ''},
        {'client_id': '客户端'},
        {'credential_file': ''},
        {'credential_file': 'relative.secret'},
        {'token_auth_method': 'none'},
        {'token_auth_method': 'private_key_jwt'},
        {'client_secret': 'do-not-store-in-config'},
    ],
)
def test_agentid_config_rejects_missing_or_unsafe_credentials(flow, override):
    with pytest.raises(Failure):
        load_login(
            {'enabled': True, 'providers': {'agentid': flow.config | override}},
            'https://msg.example',
        )


def test_authorize_is_pinned_has_pkce_nonce_and_minimal_scopes(flow):
    url = providers.authorization_url(
        'agentid',
        flow.config,
        'original-state',
        VERIFIER,
        NONCE,
        CALLBACK,
        login_hint='agent+test@example.org',
    )
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    assert parsed._replace(query='').geturl() == AUTHORIZE
    assert params == {
        'client_id': [CLIENT],
        'response_type': ['code'],
        'redirect_uri': [CALLBACK],
        'state': ['original-state'],
        'nonce': [NONCE],
        'code_challenge_method': ['S256'],
        'code_challenge': [b64(hashlib.sha256(VERIFIER.encode()).digest())],
        'scope': ['openid profile email'],
        'login_hint': ['agent+test@example.org'],
    }
    assert VERIFIER not in url and 'fixture-only-secret' not in url


@pytest.mark.parametrize('hint', ['', 'x\r\nInjected:yes', 'a' * 255, ['agent@example.org']])
def test_rejects_unsafe_login_hint(flow, hint):
    with pytest.raises(Failure, match='^login_provider_invalid_request$'):
        providers.authorization_url(
            'agentid', flow.config, 'state', VERIFIER, NONCE, CALLBACK, login_hint=hint
        )


@pytest.mark.parametrize('method', ['client_secret_basic', 'client_secret_post'])
async def test_real_es256_identity_and_registered_authentication(flow, method):
    flow.config['token_auth_method'] = method
    identity = await flow.exchange()
    assert (identity.provider, identity.issuer, identity.subject) == ('agentid', ISSUER, 's' * 43)
    assert set(asdict(identity)) == {'provider', 'issuer', 'subject', 'client_id'}
    assert identity.client_id is None
    request = next(item for item in flow.requests if str(item.url) == TOKEN)
    data = parse_qs(request.content.decode())
    assert data['code'] == [CODE] and data['code_verifier'] == [VERIFIER]
    assert data['redirect_uri'] == [CALLBACK]
    assert data['grant_type'] == ['authorization_code']
    if method == 'client_secret_basic':
        header = request.headers['authorization'].removeprefix('Basic ')
        assert base64.b64decode(header).decode() == CLIENT + ':fixture-only-secret'
        assert 'client_secret' not in data
    else:
        assert 'authorization' not in request.headers
        assert data['client_secret'] == ['fixture-only-secret']
    assert {str(item.url) for item in flow.requests} == {DISCOVERY, TOKEN, JWKS}


async def test_profile_changes_do_not_change_identity_or_merge_inboxes(flow):
    original = await flow.exchange()
    flow.claims.update(
        email='renamed@example.org', name='Renamed agent', owner_email='owner@example.org'
    )
    assert await flow.exchange() == original
    flow.claims['sub'] = 'other-inbox'
    assert (await flow.exchange()).stable_id != original.stable_id
    assert flow.count(JWKS) == 1


@pytest.mark.parametrize(
    ('claim', 'value'),
    [
        ('iss', 'https://attacker.example'),
        ('aud', 'other-client'),
        ('nonce', 'another-browser'),
        ('exp', int(NOW.timestamp())),
        ('exp', True),
        ('iat', int(NOW.timestamp()) + 6),
        ('iat', True),
        ('sub', ''),
        ('sub', 42),
        ('aud', [CLIENT, 'other-client']),
        ('azp', 'other-client'),
        ('nbf', int(NOW.timestamp()) + 1),
    ],
)
async def test_rejects_signed_wrong_claims(flow, claim, value):
    flow.claims[claim] = value
    with pytest.raises(Failure, match='^login_provider_invalid_token$') as error:
        await flow.exchange()
    assert error.value.details is None
    assert CODE not in str(error.value)


@pytest.mark.parametrize('claim', ['iss', 'aud', 'sub', 'exp', 'iat', 'nonce'])
async def test_rejects_missing_claims(flow, claim):
    del flow.claims[claim]
    with pytest.raises(Failure, match='^login_provider_invalid_token$'):
        await flow.exchange()


@pytest.mark.parametrize('kind', ['forged', 'none', 'HS256', 'RS256'])
async def test_rejects_forged_or_wrong_algorithm(flow, kind):
    if kind == 'forged':
        flow.key = flow.keys[1]
    else:
        key = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if kind == 'RS256'
            else 'x' * 32
            if kind == 'HS256'
            else ''
        )
        flow.token_override = jwt.encode(
            flow.claims, key, algorithm=kind, headers={'kid': 'current'}
        )
    with pytest.raises(Failure, match='^login_provider_invalid_token$'):
        await flow.exchange()
    if kind != 'forged':
        assert flow.count(JWKS) == 0


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('kty', 'RSA'),
        ('crv', 'P-384'),
        ('alg', 'RS256'),
        ('use', 'enc'),
        ('key_ops', ['sign']),
        ('d', 'a' * 43),
        ('x', 'a' * 42),
        ('y', '!' * 43),
    ],
)
async def test_rejects_invalid_or_private_jwks(flow, field, value):
    flow.jwks['keys'][0][field] = value
    with pytest.raises(Failure, match='^login_provider_invalid_token$'):
        await flow.exchange()


async def test_rejects_duplicate_key_ids(flow):
    flow.jwks['keys'].append(dict(flow.jwks['keys'][0]))
    with pytest.raises(Failure, match='^login_provider_invalid_token$'):
        await flow.exchange()


async def test_rotation_refresh_is_bounded(flow):
    await flow.exchange()
    flow.key, flow.kid = flow.keys[1], 'next'
    flow.jwks = {'keys': [public_jwk(flow.key, 'next')]}
    with pytest.raises(Failure, match='^login_provider_invalid_token$'):
        await flow.exchange(NOW + timedelta(seconds=29))
    assert flow.count(JWKS) == 1
    assert (await flow.exchange(NOW + timedelta(seconds=30))).subject == 's' * 43
    assert flow.count(JWKS) == 2


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('issuer', 'https://attacker.example'),
        ('token_endpoint', 'https://attacker.example/token'),
        ('authorization_endpoint', 'https://attacker.example/auth'),
        ('jwks_uri', 'https://attacker.example/keys'),
        ('id_token_signing_alg_values_supported', ['RS256']),
        ('authorization_response_iss_parameter_supported', False),
    ],
)
async def test_discovery_mismatch_fails_before_sending_credentials(flow, field, value):
    flow.metadata[field] = value
    with pytest.raises(Failure, match='^login_provider_invalid_discovery$'):
        await flow.exchange()
    assert flow.count(TOKEN) == 0


@pytest.mark.parametrize('endpoint', [DISCOVERY, TOKEN, JWKS])
async def test_provider_redirects_are_not_followed(flow, endpoint):
    flow.responses[endpoint] = httpx.Response(302, headers={'Location': 'https://attacker.example'})
    with pytest.raises(Failure, match='^login_provider_invalid_response$'):
        await flow.exchange()
    assert all(str(request.url) in {DISCOVERY, TOKEN, JWKS} for request in flow.requests)


def test_es256_does_not_relax_existing_provider_algorithm(flow):
    token = jwt.encode(flow.claims, flow.key, algorithm='ES256', headers={'kid': 'current'})
    assert token_key_id(token, algorithm='ES256') == 'current'
    with pytest.raises(Failure, match='^login_provider_invalid_token$'):
        token_key_id(token)
