"""Bounded, offline verification of identity tokens from pinned login providers."""

from __future__ import annotations

import base64
import hmac
import json
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, EllipticCurvePublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from msg.core.errors import Failure

_MAX_TOKEN_BYTES = 16 * 1024
_MAX_HEADER_BYTES = 2048
_MAX_KEYS = 32
_COMPACT_PART = re.compile(r'[A-Za-z0-9_-]+\Z')
_KEY_ID = re.compile(r'[\x21-\x7e]{1,128}\Z')
_REQUIRED_CLAIMS = ('iss', 'aud', 'exp', 'iat', 'sub', 'nonce')


@dataclass(frozen=True, slots=True)
class TokenIdentity:
    issuer: str
    subject: str


def _invalid() -> Failure:
    return Failure('login_provider_invalid_token')


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise _invalid()


def _json_part(part: str, *, limit: int) -> dict[str, object]:
    raw = base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))
    if len(raw) > limit:
        raise _invalid()
    value = json.loads(
        raw.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
    )
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _token_parts(token: str) -> tuple[str, str, str]:
    if not isinstance(token, str) or not token.isascii() or len(token) > _MAX_TOKEN_BYTES:
        raise _invalid()
    parts = token.split('.')
    if len(parts) != 3 or not all(_COMPACT_PART.fullmatch(part) for part in parts):
        raise _invalid()
    return parts[0], parts[1], parts[2]


def token_key_id(token: str, *, algorithm: str = 'RS256') -> str:
    """Read a bounded key ID with the caller-pinned algorithm; header URLs never cause a network request."""
    try:
        header_part, _, _ = _token_parts(token)
        header = _json_part(header_part, limit=_MAX_HEADER_BYTES)
        key_id = header.get('kid')
        if (
            algorithm not in {'RS256', 'ES256'}
            or header.get('alg') != algorithm
            or 'crit' in header
            or header.get('b64', True) is not True
            or not isinstance(key_id, str)
            or not _KEY_ID.fullmatch(key_id)
        ):
            raise _invalid()
        return key_id
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise _invalid() from None


def _public_key(
    jwks: Mapping[str, object], key_id: str, algorithm: str
) -> RSAPublicKey | EllipticCurvePublicKey:
    if not isinstance(jwks, Mapping):
        raise _invalid()
    keys = jwks.get('keys')
    if not isinstance(keys, list) or not 1 <= len(keys) <= _MAX_KEYS:
        raise _invalid()
    selected = None
    seen = set()
    for key in keys:
        if not isinstance(key, dict):
            raise _invalid()
        candidate_id = key.get('kid')
        if (
            not isinstance(candidate_id, str)
            or not _KEY_ID.fullmatch(candidate_id)
            or candidate_id in seen
            or key.get('kty') != ('EC' if algorithm == 'ES256' else 'RSA')
            or (algorithm == 'ES256' and key.get('crv') != 'P-256')
            or key.get('use', 'sig') != 'sig'
            or key.get('alg', algorithm) != algorithm
            or ('key_ops' in key and key['key_ops'] != ['verify'])
            or any(
                private_field in key for private_field in ('d', 'p', 'q', 'dp', 'dq', 'qi', 'oth')
            )
        ):
            raise _invalid()
        seen.add(candidate_id)
        if candidate_id == key_id:
            selected = key
    if selected is None:
        raise _invalid()
    for field in ('x', 'y') if algorithm == 'ES256' else ('n', 'e'):
        value = selected.get(field)
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 1400
            or (algorithm == 'ES256' and len(value) != 43)
            or not _COMPACT_PART.fullmatch(value)
        ):
            raise _invalid()
    public_key = jwt.PyJWK.from_dict(selected, algorithm=algorithm).key
    if algorithm == 'ES256':
        if not isinstance(public_key, EllipticCurvePublicKey) or not isinstance(
            public_key.curve, SECP256R1
        ):
            raise _invalid()
    elif not isinstance(public_key, RSAPublicKey) or not 2048 <= public_key.key_size <= 8192:
        raise _invalid()
    return public_key


def _string(value: object, *, limit: int) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= limit


def verify_identity_token(
    token: str,
    jwks: Mapping[str, object],
    *,
    issuers: Collection[str],
    audience: str,
    nonce: str,
    now: datetime,
    algorithm: str = 'RS256',
) -> TokenIdentity:
    """Verify a real signature and return only the provider's immutable identity."""
    try:
        key_id = token_key_id(token, algorithm=algorithm)
        if (
            isinstance(issuers, str)
            or not isinstance(issuers, Collection)
            or not 1 <= len(issuers) <= 8
            or not all(_string(issuer, limit=512) for issuer in issuers)
            or not _string(audience, limit=512)
            or not _string(nonce, limit=1024)
            or not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise _invalid()
        timestamp = now.timestamp()
        if not math.isfinite(timestamp):
            raise _invalid()
        # Reject ambiguous JSON before handing the signed bytes to the maintained verifier.
        _, payload_part, _ = _token_parts(token)
        _json_part(payload_part, limit=_MAX_TOKEN_BYTES)
        claims = jwt.decode(
            token,
            _public_key(jwks, key_id, algorithm),
            algorithms=[algorithm],
            options={
                'require': list(_REQUIRED_CLAIMS),
                'verify_signature': True,
                'verify_exp': False,
                'verify_iat': False,
                'verify_nbf': False,
                'verify_iss': False,
                'verify_aud': False,
                'verify_sub': False,
            },
        )
        issuer, subject = claims['iss'], claims['sub']
        expires, issued = claims['exp'], claims['iat']
        claim_nonce, claim_audience = claims['nonce'], claims['aud']
        if (
            not _string(issuer, limit=512)
            or issuer not in issuers
            or not _string(subject, limit=255)
            or not subject.isascii()
            or type(expires) is not int
            or type(issued) is not int
            or not issued < expires
            or expires <= timestamp
            or issued > timestamp + 5
            or not _string(claim_nonce, limit=1024)
            or not hmac.compare_digest(claim_nonce.encode('utf-8'), nonce.encode('utf-8'))
        ):
            raise _invalid()
        if isinstance(claim_audience, str):
            if claim_audience != audience:
                raise _invalid()
        elif isinstance(claim_audience, list):
            if (
                not 1 <= len(claim_audience) <= 16
                or not all(_string(item, limit=512) for item in claim_audience)
                or len(set(claim_audience)) != len(claim_audience)
                or audience not in claim_audience
                or (len(claim_audience) > 1 and claims.get('azp') != audience)
            ):
                raise _invalid()
        else:
            raise _invalid()
        if 'azp' in claims and claims['azp'] != audience:
            raise _invalid()
        if 'nbf' in claims and (type(claims['nbf']) is not int or claims['nbf'] > timestamp):
            raise _invalid()
        return TokenIdentity(issuer=issuer, subject=subject)
    except jwt.PyJWTError, ValueError, TypeError, UnicodeError, OverflowError, RecursionError:
        raise _invalid() from None
