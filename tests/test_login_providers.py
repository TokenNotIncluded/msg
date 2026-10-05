"""Real signed identity tokens over bounded, pinned mock provider HTTP exchanges."""

import base64
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from msg.core.codec import b64
from msg.core.errors import Failure
from msg.transports import login_providers as providers

NOW = datetime(2026, 10, 5, tzinfo=UTC)
VERIFIER = 'a' * 43
NONCE = 'original-browser-nonce'
CALLBACK = 'https://msg.example/-/login/callback'
CODE = 'private-authorization-code'


@pytest.fixture(autouse=True)
def clear_public_caches():
    providers._jwks_cache.clear()
    providers._jwks_locks.clear()
    providers._discovery_expires_at = 0
    providers._discovery_lock = None


@pytest.fixture(scope='module')
def signing_keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / 'provider-secret'
    path.write_text('private-provider-secret\n')
    path.chmod(0o600)
    return path


def public_jwk(key, kid='first'):
    return json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {
        'kid': kid,
        'use': 'sig',
        'alg': 'RS256',
        'key_ops': ['verify'],
    }


def discovery():
    return {
        'issuer': providers.CHATGPT_ISSUER,
        'authorization_endpoint': providers._AUTHORIZATION['chatgpt'],
        'token_endpoint': providers._TOKEN['chatgpt'],
        'jwks_uri': providers._JWKS['chatgpt'],
        'id_token_signing_alg_values_supported': ['RS256'],
    }


class Scenario:
    def __init__(self, provider, keys, config):
        self.provider, self.keys, self.config = provider, keys, config
        self.claims = {
            'iss': providers.GOOGLE_ISSUER if provider == 'google' else providers.CHATGPT_ISSUER,
            'aud': config['client_id'],
            'sub': 'immutable-provider-subject',
            'iat': int(NOW.timestamp()) - 1,
            'exp': int(NOW.timestamp()) + 7200,
            'nonce': NONCE,
            'email': 'untrusted-profile@example.com',
        }
        self.signing_key = keys[0]
        self.kid = 'first'
        self.jwks = {'keys': [public_jwk(keys[0])]}
        self.metadata = discovery()
        self.user = {'id': 42, 'login': 'old-handle', 'email': 'profile@example.com'}
        self.token_payload = None
        self.responses = {}
        self.requests = []
        self.cache_control = 'max-age=3600'

    def token(self):
        return jwt.encode(
            self.claims, self.signing_key, algorithm='RS256', headers={'kid': self.kid}
        )

    def handle(self, request):
        self.requests.append(request)
        url = str(request.url)
        if url in self.responses:
            response = self.responses[url]
            if isinstance(response, Exception):
                raise response
            return response
        if url == providers._DISCOVERY:
            return httpx.Response(
                200, json=self.metadata, headers={'Cache-Control': self.cache_control}
            )
        if url == providers._TOKEN[self.provider]:
            payload = self.token_payload
            if payload is None:
                payload = (
                    {'access_token': 'private-github-access', 'token_type': 'bearer'}
                    if self.provider == 'github'
                    else {'id_token': self.token()}
                )
            return httpx.Response(200, json=payload)
        if url == providers._JWKS.get(self.provider):
            return httpx.Response(
                200, json=self.jwks, headers={'Cache-Control': self.cache_control}
            )
        if url == 'https://api.github.com/user':
            return httpx.Response(200, json=self.user)
        pytest.fail('unpinned provider request')

    async def exchange(self, now=NOW, **overrides):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), follow_redirects=True
        ) as client:
            identity = await providers.exchange_identity(
                self.provider,
                self.config,
                overrides.get('code', CODE),
                overrides.get('verifier', VERIFIER),
                overrides.get('nonce', NONCE),
                overrides.get('redirect_uri', CALLBACK),
                now,
                client=client,
            )
            assert not client.is_closed
            return identity

    def counts(self):
        return Counter(str(request.url) for request in self.requests)


def scenario(provider, keys, secret_file):
    config = {'client_id': 'oaiapp_own_client' if provider == 'chatgpt' else 'registered-client'}
    if provider != 'chatgpt':
        config['credential_file'] = secret_file
    return Scenario(provider, keys, config)


@pytest.mark.parametrize('provider', ['google', 'github', 'chatgpt'])
def test_authorization_is_pinned_and_s256(provider, secret_file):
    config = {'client_id': 'oaiapp_own' if provider == 'chatgpt' else 'registered-client'}
    if provider != 'chatgpt':
        config['credential_file'] = secret_file
    url = providers.authorization_url(provider, config, 'original-state', VERIFIER, NONCE, CALLBACK)
    parsed = urlsplit(url)
    assert parsed._replace(query='').geturl() == providers._AUTHORIZATION[provider]
    params = parse_qs(parsed.query)
    assert params['state'] == ['original-state']
    assert params['code_challenge'] == [b64(hashlib.sha256(VERIFIER.encode()).digest())]
    assert params['code_challenge_method'] == ['S256']
    assert params['redirect_uri'] == [CALLBACK]
    assert VERIFIER not in url
    if provider == 'github':
        assert params['allow_signup'] == ['false']
        assert params['scope'] == ['read:user']
        assert 'nonce' not in params
    else:
        assert params['nonce'] == [NONCE]
        assert params['scope'] == ['openid profile email']
        assert 'offline_access' not in url


@pytest.mark.parametrize('provider', ['google', 'github', 'chatgpt'])
async def test_verified_identity_and_exchange_body(provider, signing_keys, secret_file):
    flow = scenario(provider, signing_keys, secret_file)
    identity = await flow.exchange()
    assert identity.provider == provider
    assert identity.subject == ('42' if provider == 'github' else flow.claims['sub'])
    assert identity.stable_id.startswith('sha256:')
    assert len(identity.stable_id) == 71
    assert identity.client_id == (flow.config['client_id'] if provider == 'chatgpt' else None)
    assert (
        not {'email', 'login', 'id_token', 'access_token', 'refresh_token'}
        & asdict(identity).keys()
    )
    token_request = next(request for request in flow.requests if request.method == 'POST')
    params = parse_qs(token_request.content.decode())
    assert params['code_verifier'] == [VERIFIER]
    assert params['redirect_uri'] == [CALLBACK]
    assert params['code'] == [CODE]
    assert params['grant_type'] == ['authorization_code']
    assert 'authorization' not in token_request.headers
    if provider == 'chatgpt':
        assert 'client_secret' not in params
    else:
        assert params['client_secret'] == ['private-provider-secret']
    if provider == 'github':
        user_request = flow.requests[-1]
        assert user_request.headers['authorization'] == 'Bearer private-github-access'
        assert user_request.headers['x-github-api-version'] == '2022-11-28'
    for request in flow.requests:
        assert request.headers['accept-encoding'] == 'identity'
        assert request.extensions['timeout'] == {'connect': 5, 'read': 10, 'write': 10, 'pool': 5}


async def test_google_legacy_issuer_and_email_do_not_change_mapping(signing_keys, secret_file):
    flow = scenario('google', signing_keys, secret_file)
    original = await flow.exchange()
    flow.claims.update(iss='accounts.google.com', email='renamed@example.com')
    renamed = await flow.exchange()
    assert renamed.issuer == providers.GOOGLE_ISSUER
    assert renamed.stable_id == original.stable_id
    assert flow.counts()[providers._JWKS['google']] == 1


async def test_github_rename_preserves_numeric_identity(signing_keys, secret_file):
    flow = scenario('github', signing_keys, secret_file)
    original = await flow.exchange()
    flow.user.update(login='new-handle', email='changed@example.com')
    renamed = await flow.exchange()
    assert original == renamed
    assert flow.counts()['https://api.github.com/user'] == 2
    flow.user['id'] = 43
    assert (await flow.exchange()).stable_id != original.stable_id


@pytest.mark.parametrize('subject', [True, False, None, '42', 0, -1, 1.5, 2**63])
async def test_github_rejects_non_numeric_stable_subject(subject, signing_keys, secret_file):
    flow = scenario('github', signing_keys, secret_file)
    flow.user['id'] = subject
    with pytest.raises(Failure, match='^login_provider_invalid_identity$'):
        await flow.exchange()


@pytest.mark.parametrize('token_type', [None, True, [], 'mac'])
async def test_github_rejects_non_bearer_token_type(token_type, signing_keys, secret_file):
    flow = scenario('github', signing_keys, secret_file)
    flow.token_payload = {'access_token': 'private-token', 'token_type': token_type}
    with pytest.raises(Failure, match='^login_provider_invalid_response$'):
        await flow.exchange()
    assert len(flow.requests) == 1


async def test_chatgpt_basic_secret_only_header_and_identity_only(signing_keys, secret_file):
    secret_file.write_text('a secret:+/ü\n')
    flow = scenario('chatgpt', signing_keys, secret_file)
    flow.config.update(credential_file=secret_file, token_auth_method='client_secret_basic')
    identity = await flow.exchange()
    token_request = next(request for request in flow.requests if request.method == 'POST')
    assert base64.b64decode(token_request.headers['authorization'].removeprefix('Basic ')) == (
        b'oaiapp_own_client:a+secret%3A%2B%2F%C3%BC'
    )
    assert 'client_secret' not in parse_qs(token_request.content.decode())
    assert identity.subject == flow.claims['sub']


async def test_chatgpt_mapping_includes_own_client_id(signing_keys, secret_file):
    flow = scenario('chatgpt', signing_keys, secret_file)
    first = await flow.exchange()
    flow.config['client_id'] = flow.claims['aud'] = 'oaiapp_another_own_client'
    second = await flow.exchange()
    assert second.stable_id != first.stable_id


@pytest.mark.parametrize(
    ('claim', 'value'),
    [
        ('iss', 'https://attacker.example'),
        ('aud', 'another-client'),
        ('nonce', 'other-browser-nonce'),
        ('exp', int(NOW.timestamp())),
        ('exp', True),
        ('exp', int(NOW.timestamp()) + 0.5),
        ('iat', int(NOW.timestamp()) + 6),
        ('iat', True),
        ('sub', ''),
        ('sub', '界'),
        ('sub', 's' * 256),
        ('sub', 42),
        ('aud', ['registered-client', 'other-client']),
        ('azp', 'another-client'),
        ('nbf', int(NOW.timestamp()) + 1),
    ],
)
@pytest.mark.parametrize('provider', ['google', 'chatgpt'])
async def test_rejects_signed_invalid_claims(provider, claim, value, signing_keys, secret_file):
    flow = scenario(provider, signing_keys, secret_file)
    if claim == 'aud' and isinstance(value, list):
        value = [flow.config['client_id'], 'other-client']
    flow.claims[claim] = value
    with pytest.raises(Failure, match='^login_provider_invalid_token$') as error:
        await flow.exchange()
    assert CODE not in str(error.value)
    assert error.value.details is None


@pytest.mark.parametrize('claim', ['iss', 'aud', 'nonce', 'exp', 'iat', 'sub'])
async def test_required_claims(claim, signing_keys, secret_file):
    flow = scenario('google', signing_keys, secret_file)
    del flow.claims[claim]
    with pytest.raises(Failure, match='^login_provider_invalid_token$'):
        await flow.exchange()


async def test_multi_audience_requires_matching_azp(signing_keys, secret_file):
    flow = scenario('google', signing_keys, secret_file)
    flow.claims.update(aud=[flow.config['client_id'], 'other-client'], azp=flow.config['client_id'])
    assert (await flow.exchange()).subject == flow.claims['sub']


@pytest.mark.parametrize('algorithm', ['none', 'HS256', 'forged', 'missing', 'oversize'])
async def test_rejects_unsafe_or_forged_tokens(algorithm, signing_keys, secret_file):
    flow = scenario('google', signing_keys, secret_file)
    if algorithm == 'forged':
        token = jwt.encode(
            flow.claims, signing_keys[1], algorithm='RS256', headers={'kid': 'first'}
        )
    elif algorithm == 'missing':
        token = None
    elif algorithm == 'oversize':
        token = 'a' * (16 * 1024 + 1)
    else:
        token = jwt.encode(
            flow.claims,
            '' if algorithm == 'none' else 'x' * 32,
            algorithm=algorithm,
            headers={'kid': 'first'},
        )
    flow.token_payload = {'id_token': token, 'access_token': 'private-ignored-token'}
    with pytest.raises(Failure, match='^login_provider_invalid_token$'):
        await flow.exchange()
    if algorithm != 'forged':
        assert flow.counts()[providers._JWKS['google']] == 0


async def test_unknown_key_refresh_is_bounded_and_rotation_recovers(signing_keys, secret_file):
    flow = scenario('google', signing_keys, secret_file)
    await flow.exchange()
    flow.signing_key, flow.kid = signing_keys[1], 'second'
    flow.jwks = {'keys': [public_jwk(signing_keys[1], 'second')]}
    for offset in (1, 2, 29):
        with pytest.raises(Failure, match='^login_provider_invalid_token$'):
            await flow.exchange(NOW + timedelta(seconds=offset))
    assert flow.counts()[providers._JWKS['google']] == 1
    assert (await flow.exchange(NOW + timedelta(seconds=30))).subject == flow.claims['sub']
    assert flow.counts()[providers._JWKS['google']] == 2


async def test_initial_unknown_key_does_not_double_fetch(signing_keys, secret_file):
    flow = scenario('google', signing_keys, secret_file)
    flow.kid = 'unknown'
    with pytest.raises(Failure, match='^login_provider_invalid_token$'):
        await flow.exchange()
    assert flow.counts()[providers._JWKS['google']] == 1


@pytest.mark.parametrize('control', ['max-age=1', 'no-cache', 'no-store'])
async def test_jwks_expiry_and_no_store(control, signing_keys, secret_file):
    flow = scenario('google', signing_keys, secret_file)
    flow.cache_control = control
    await flow.exchange()
    await flow.exchange(NOW + timedelta(seconds=1))
    assert flow.counts()[providers._JWKS['google']] == 2


def test_cache_ttl_is_bounded():
    assert (
        providers._cache_seconds({'cache-control': 'public, max-age=99999999999999999999'}) == 3600
    )
    assert providers._cache_seconds({'cache-control': 'max-age=20'}) == 20
    assert providers._cache_seconds({}) == 300


@pytest.mark.parametrize(
    'field', ['issuer', 'authorization_endpoint', 'token_endpoint', 'jwks_uri']
)
async def test_openai_discovery_cannot_redirect_to_untrusted_origins(
    field, signing_keys, secret_file
):
    flow = scenario('chatgpt', signing_keys, secret_file)
    flow.metadata[field] = 'http://127.0.0.1/private-service'
    with pytest.raises(Failure, match='^login_provider_invalid_discovery$'):
        await flow.exchange()
    assert [str(request.url) for request in flow.requests] == [providers._DISCOVERY]


@pytest.mark.parametrize('algorithm', [None, ['HS256'], 'RS256'])
async def test_openai_discovery_requires_advertised_rs256(algorithm, signing_keys, secret_file):
    flow = scenario('chatgpt', signing_keys, secret_file)
    flow.metadata['id_token_signing_alg_values_supported'] = algorithm
    with pytest.raises(Failure, match='^login_provider_invalid_discovery$'):
        await flow.exchange()


@pytest.mark.parametrize(
    ('response', 'error_code'),
    [
        (
            httpx.Response(302, headers={'Location': 'https://attacker.example/token'}),
            'invalid_response',
        ),
        (
            httpx.Response(400, json={'error_description': 'private-code private-token'}),
            'invalid_response',
        ),
        (httpx.Response(503, text='private-token'), 'unavailable'),
        (httpx.Response(200, text='<html>private-token</html>'), 'invalid_response'),
        (httpx.Response(200, json=[]), 'invalid_response'),
        (httpx.Response(200, content=b'{"id_token":"one","id_token":"two"}'), 'invalid_response'),
        (httpx.Response(200, content=b'{"id_token":NaN}'), 'invalid_response'),
        (httpx.Response(200, content=b'x' * 65537), 'invalid_response'),
        (httpx.Response(200, json={}, headers={'content-length': '65537'}), 'invalid_response'),
        (httpx.Response(200, json={}, headers={'content-length': 'bogus'}), 'invalid_response'),
        (httpx.Response(200, json={}, headers={'content-encoding': 'br'}), 'invalid_response'),
        (httpx.ReadTimeout('private-token private-code'), 'unavailable'),
    ],
)
async def test_network_failures_are_bounded_and_sanitized(
    response, error_code, signing_keys, secret_file
):
    flow = scenario('google', signing_keys, secret_file)
    flow.responses[providers._TOKEN['google']] = response
    with pytest.raises(Failure, match='^login_provider_' + error_code + '$') as error:
        await flow.exchange()
    assert error.value.details is None
    assert 'private-' not in str(error.value)
    assert error.value.__cause__ is None
    assert len(flow.requests) == 1


class OversizedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b' ' * 65536
        yield b'{}'
        pytest.fail('oversized response was not stopped')


async def test_streamed_size_limit_applies_without_content_length(signing_keys, secret_file):
    flow = scenario('google', signing_keys, secret_file)
    flow.responses[providers._TOKEN['google']] = httpx.Response(200, stream=OversizedStream())
    with pytest.raises(Failure, match='^login_provider_invalid_response$'):
        await flow.exchange()


@dataclass
class ProviderConfig:
    client_id: str
    credential_file: str | None = None
    token_auth_method: str | None = None


async def test_config_dataclass_and_legacy_aliases(signing_keys, secret_file):
    flow = scenario('google', signing_keys, secret_file)
    flow.config = ProviderConfig('registered-client', str(secret_file), 'client_secret_post')
    assert (await flow.exchange()).provider == 'google'
    flow.config = {
        'client_id': 'registered-client',
        'secret_file': secret_file,
        'client_auth_method': 'client_secret_post',
    }
    assert (await flow.exchange()).provider == 'google'


@pytest.mark.parametrize(
    'config',
    [
        {'client_id': 'codex-public-client'},
        {'client_id': 'oaiapp_own', 'token_auth_method': 'client_secret_post'},
        {'client_id': 'oaiapp_own', 'token_auth_method': 'client_secret_basic'},
        {
            'client_id': 'oaiapp_own',
            'credential_file': '/private/secret',
            'token_auth_method': 'none',
        },
        {'client_id': 'oaiapp_own', 'token_auth_method': []},
        {'client_id': 'oaiapp_own', 'credential_file': 'one', 'secret_file': 'two'},
        {
            'client_id': 'oaiapp_own',
            'token_auth_method': 'none',
            'client_auth_method': 'client_secret_basic',
        },
    ],
)
async def test_chatgpt_requires_own_registered_client_and_explicit_auth(
    config, signing_keys, secret_file
):
    flow = scenario('chatgpt', signing_keys, secret_file)
    flow.config = config
    with pytest.raises(Failure, match='^login_provider_configuration$'):
        await flow.exchange()
    assert flow.requests == []


@pytest.mark.parametrize(
    'case', ['symlink', 'world-readable', 'oversize', 'empty', 'missing', 'directory']
)
async def test_secrets_are_private_bounded_regular_files(case, signing_keys, secret_file, tmp_path):
    flow = scenario('google', signing_keys, secret_file)
    if case == 'symlink':
        path = tmp_path / 'symlink'
        path.symlink_to(secret_file)
        flow.config['credential_file'] = path
    elif case == 'world-readable':
        secret_file.chmod(0o644)
    elif case == 'oversize':
        secret_file.write_bytes(b'x' * 8193)
    elif case == 'empty':
        secret_file.write_bytes(b'')
    elif case == 'missing':
        secret_file.unlink()
    else:
        flow.config['credential_file'] = tmp_path
    with pytest.raises(Failure, match='^login_provider_configuration$') as error:
        await flow.exchange()
    assert str(tmp_path) not in str(error.value)
    assert flow.requests == []


@pytest.mark.parametrize(
    'override',
    [
        {'verifier': 'short'},
        {'verifier': '!' * 43},
        {'nonce': ''},
        {'code': 'code\nheader'},
        {'redirect_uri': 'https://user:pass@msg.example/callback'},
        {'redirect_uri': 'https://msg.example/callback#fragment'},
        {'redirect_uri': 'http://attacker.example/callback'},
    ],
)
async def test_invalid_exchange_inputs_do_not_reach_network(override, signing_keys, secret_file):
    flow = scenario('google', signing_keys, secret_file)
    with pytest.raises(Failure, match='^login_provider_invalid_request$'):
        await flow.exchange(**override)
    assert flow.requests == []


async def test_invalid_clock_does_not_reach_network(signing_keys, secret_file):
    flow = scenario('google', signing_keys, secret_file)
    with pytest.raises(Failure, match='^login_provider_invalid_request$'):
        await flow.exchange(NOW.replace(tzinfo=None))
    assert flow.requests == []
