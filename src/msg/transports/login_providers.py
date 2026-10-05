"""Pinned OAuth/OIDC exchanges; provider tokens never cross this module's boundary."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote_plus, urlencode, urlsplit

import httpx

from msg.core.codec import b64, canonical
from msg.core.errors import Failure
from msg.security.login_jwt import token_key_id, verify_identity_token

GOOGLE_ISSUER = 'https://accounts.google.com'
GITHUB_ISSUER = 'https://github.com'
CHATGPT_ISSUER = 'https://auth.openai.com'
_AUTHORIZATION = {
    'google': 'https://accounts.google.com/o/oauth2/v2/auth',
    'github': 'https://github.com/login/oauth/authorize',
    'chatgpt': 'https://auth.openai.com/api/accounts/authorize',
}
_TOKEN = {
    'google': 'https://oauth2.googleapis.com/token',
    'github': 'https://github.com/login/oauth/access_token',
    'chatgpt': 'https://auth.openai.com/api/accounts/oauth/token',
}
_JWKS = {
    'google': 'https://www.googleapis.com/oauth2/v3/certs',
    'chatgpt': 'https://auth.openai.com/.well-known/jwks.json',
}
_DISCOVERY = CHATGPT_ISSUER + '/.well-known/openid-configuration'
_MAX_RESPONSE = 64 * 1024
_TIMEOUT = httpx.Timeout(10, connect=5, pool=5, write=10)


@dataclass(frozen=True)
class VerifiedIdentity:
    """Only verified, non-secret mapping inputs; never a token or email address."""

    provider: str
    issuer: str
    subject: str
    client_id: str | None = None

    @property
    def stable_id(self) -> str:
        identity = {'provider': self.provider, 'issuer': self.issuer, 'subject': self.subject}
        if self.provider == 'chatgpt':
            identity['client_id'] = self.client_id
        return 'sha256:' + hashlib.sha256(canonical(identity)).hexdigest()


@dataclass(frozen=True)
class _JwksEntry:
    keys: dict
    fetched_at: float
    expires_at: float


# Only the two pinned JWKS URLs can enter these maps. No provider token is cached.
_jwks_cache: dict[str, _JwksEntry] = {}
_jwks_locks: dict[str, asyncio.Lock] = {}
_discovery_expires_at = 0.0
_discovery_lock: asyncio.Lock | None = None


def _get(config, name, default=None):
    return (
        config.get(name, default) if isinstance(config, Mapping) else getattr(config, name, default)
    )


def _alias(config, name, legacy):
    value, previous = _get(config, name), _get(config, legacy)
    if value is not None and previous is not None and value != previous:
        raise Failure('login_provider_configuration')
    return value if value is not None else previous


def _credential_file(config):
    return _alias(config, 'credential_file', 'secret_file')


def _text(value, *, limit=4096, minimum=1, code='login_provider_invalid_request'):
    if (
        type(value) is not str
        or not minimum <= len(value) <= limit
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise Failure(code)
    return value


def _settings(provider, config):
    if type(provider) is not str or provider not in _AUTHORIZATION:
        raise Failure('login_provider_unsupported')
    client_id = _text(_get(config, 'client_id'), limit=1024, code='login_provider_configuration')
    method = _alias(config, 'token_auth_method', 'client_auth_method')
    if method is None:
        method = 'none' if provider == 'chatgpt' else 'client_secret_post'
    allowed = {'none', 'client_secret_basic'} if provider == 'chatgpt' else {'client_secret_post'}
    if (
        type(method) is not str
        or method not in allowed
        or (provider == 'chatgpt' and not client_id.startswith('oaiapp_'))
    ):
        raise Failure('login_provider_configuration')
    if method != 'none' and not _credential_file(config):
        raise Failure('login_provider_configuration')
    if method == 'none' and _credential_file(config):
        raise Failure('login_provider_configuration')
    return client_id, method


def _callback_inputs(verifier, nonce, redirect_uri):
    verifier = _text(verifier, minimum=43, limit=128)
    if re.fullmatch(r'[A-Za-z0-9._~-]+', verifier) is None:
        raise Failure('login_provider_invalid_request')
    nonce = _text(nonce, limit=512)
    redirect_uri = _text(redirect_uri, limit=4096)
    try:
        parsed = urlsplit(redirect_uri)
        valid = parsed.scheme == 'https' or (
            parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}
        )
        valid = valid and parsed.hostname and not parsed.username and not parsed.password
        valid = valid and not parsed.fragment
        _ = parsed.port
    except ValueError:
        raise Failure('login_provider_invalid_request') from None
    if not valid:
        raise Failure('login_provider_invalid_request')
    return verifier, nonce, redirect_uri


def authorization_url(provider, config, state, verifier, nonce, redirect_uri) -> str:
    """Build a pinned authorization URL; the HTTP boundary owns one-use state."""
    client_id, _ = _settings(provider, config)
    state = _text(state, limit=1024)
    verifier, nonce, redirect_uri = _callback_inputs(verifier, nonce, redirect_uri)
    parameters = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'state': state,
        'code_challenge': b64(hashlib.sha256(verifier.encode('ascii')).digest()),
        'code_challenge_method': 'S256',
        'scope': 'read:user' if provider == 'github' else 'openid profile email',
    }
    if provider == 'github':
        parameters['allow_signup'] = 'false'
    else:
        parameters['nonce'] = nonce
    return _AUTHORIZATION[provider] + '?' + urlencode(parameters)


def _read_secret(config) -> str:
    """Read only the configured secret file, bounded and without path-bearing errors."""
    descriptor = None
    try:
        path = _credential_file(config)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 8192
            or metadata.st_mode & 0o027
        ):
            raise ValueError
        raw = os.read(descriptor, 8193)
        if len(raw) > 8192:
            raise ValueError
        secret = raw.rstrip(b'\r\n').decode('utf-8')
        if not secret or any(ord(character) < 32 or ord(character) == 127 for character in secret):
            raise ValueError
        return secret
    except OSError, TypeError, ValueError, UnicodeError:
        raise Failure('login_provider_configuration') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError


async def _json_request(client, method, url, *, data=None, headers=None):
    request_headers = {
        'Accept': 'application/json',
        'Accept-Encoding': 'identity',
        'User-Agent': 'MSG-login/1',
    }
    request_headers.update(headers or {})
    try:
        async with client.stream(
            method,
            url,
            data=data,
            headers=request_headers,
            timeout=_TIMEOUT,
            follow_redirects=False,
            auth=None,
        ) as response:
            if response.status_code >= 500:
                raise Failure('login_provider_unavailable', retryable=True)
            if response.status_code != 200:
                raise Failure('login_provider_invalid_response')
            if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                raise Failure('login_provider_invalid_response')
            length = response.headers.get('content-length')
            if length is not None and (not length.isdecimal() or int(length) > _MAX_RESPONSE):
                raise Failure('login_provider_invalid_response')
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_RESPONSE:
                    raise Failure('login_provider_invalid_response')
                body.extend(chunk)
            value = json.loads(
                body.decode('utf-8'),
                object_pairs_hook=_object_pairs,
                parse_constant=_invalid_constant,
            )
            if type(value) is not dict:
                raise Failure('login_provider_invalid_response')
            return value, response.headers
    except httpx.HTTPError, OSError:
        raise Failure('login_provider_unavailable', retryable=True) from None
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, Failure):
            raise
        raise Failure('login_provider_invalid_response') from None


def _cache_seconds(headers) -> int:
    control = headers.get('cache-control', '').lower()
    if 'no-store' in control or 'no-cache' in control:
        return 0
    match = re.search(r'(?:^|,)\s*max-age\s*=\s*"?(\d+)"?(?:\s*,|\s*$)', control)
    return min(int(match[1][:10]), 3600) if match else 300


async def _openai_discovery(client, instant):
    global _discovery_expires_at, _discovery_lock
    if instant < _discovery_expires_at:
        return
    if _discovery_lock is None:
        _discovery_lock = asyncio.Lock()
    async with _discovery_lock:
        if instant < _discovery_expires_at:
            return
        document, headers = await _json_request(client, 'GET', _DISCOVERY)
        required = {
            'issuer': CHATGPT_ISSUER,
            'authorization_endpoint': _AUTHORIZATION['chatgpt'],
            'token_endpoint': _TOKEN['chatgpt'],
            'jwks_uri': _JWKS['chatgpt'],
        }
        if any(document.get(key) != value for key, value in required.items()):
            raise Failure('login_provider_invalid_discovery')
        algorithms = document.get('id_token_signing_alg_values_supported')
        if type(algorithms) is not list or 'RS256' not in algorithms:
            raise Failure('login_provider_invalid_discovery')
        _discovery_expires_at = instant + _cache_seconds(headers)


async def _identity_jwks(provider, client, instant, kid):
    url = _JWKS[provider]
    lock = _jwks_locks.setdefault(url, asyncio.Lock())
    async with lock:
        entry = _jwks_cache.get(url)
        fresh = entry is not None and instant < entry.expires_at
        matching = entry is not None and any(
            type(key) is dict and key.get('kid') == kid for key in entry.keys.get('keys', [])
        )
        # A new unknown kid can trigger one refresh per 30 seconds. The initial fetch
        # already covers unknown keys, so it never causes an immediate second fetch.
        if fresh and (matching or instant - entry.fetched_at < 30):
            return entry.keys
        keys, headers = await _json_request(client, 'GET', url)
        items = keys.get('keys')
        if type(items) is not list or not 1 <= len(items) <= 32:
            raise Failure('login_provider_invalid_response')
        entry = _JwksEntry(keys, instant, instant + _cache_seconds(headers))
        _jwks_cache[url] = entry
        return keys


def _instant(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise Failure('login_provider_invalid_request')
    return now.timestamp()


async def exchange_identity(
    provider, config, code, verifier, nonce, redirect_uri, now, client=None
) -> VerifiedIdentity:
    """Exchange once and verify identity; code, secrets and tokens stay in memory."""
    client_id, auth_method = _settings(provider, config)
    code = _text(code)
    verifier, nonce, redirect_uri = _callback_inputs(verifier, nonce, redirect_uri)
    instant = _instant(now)
    if client is None:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as owned:
            return await exchange_identity(
                provider, config, code, verifier, nonce, redirect_uri, now, client=owned
            )
    if provider == 'chatgpt':
        await _openai_discovery(client, instant)
    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'code_verifier': verifier,
    }
    headers = {}
    if auth_method != 'none':
        secret = _read_secret(config)
        if auth_method == 'client_secret_basic':
            credentials = quote_plus(client_id, safe='') + ':' + quote_plus(secret, safe='')
            headers['Authorization'] = 'Basic ' + base64.b64encode(
                credentials.encode('ascii')
            ).decode('ascii')
        else:
            data['client_secret'] = secret
    tokens, _ = await _json_request(client, 'POST', _TOKEN[provider], data=data, headers=headers)
    if provider == 'github':
        access_token = _text(
            tokens.get('access_token'), limit=8192, code='login_provider_invalid_response'
        )
        token_type = tokens.get('token_type')
        if type(token_type) is not str or token_type.lower() != 'bearer':
            raise Failure('login_provider_invalid_response')
        user, _ = await _json_request(
            client,
            'GET',
            'https://api.github.com/user',
            headers={
                'Authorization': 'Bearer ' + access_token,
                'X-GitHub-Api-Version': '2022-11-28',
            },
        )
        subject = user.get('id')
        if type(subject) is not int or not 0 < subject < 2**63:
            raise Failure('login_provider_invalid_identity')
        return VerifiedIdentity(provider, GITHUB_ISSUER, str(subject))
    id_token = tokens.get('id_token')
    kid = token_key_id(id_token)
    jwks = await _identity_jwks(provider, client, instant, kid)
    identity = verify_identity_token(
        id_token,
        jwks,
        issuers={GOOGLE_ISSUER, 'accounts.google.com'}
        if provider == 'google'
        else {CHATGPT_ISSUER},
        audience=client_id,
        nonce=nonce,
        now=now,
    )
    issuer = GOOGLE_ISSUER if provider == 'google' else identity.issuer
    return VerifiedIdentity(
        provider, issuer, identity.subject, client_id if provider == 'chatgpt' else None
    )
