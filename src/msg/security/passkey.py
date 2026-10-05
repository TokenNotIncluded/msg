"""WebAuthn verification and one-use ceremony facts for trusted login adapters.

The login executor owns binding and session writes. Its passkey branch calls
finalize_* in that transaction; HTTP never commits counters or consumes a
challenge before the account operation commits.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from json import dumps
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from fido2.server import Fido2Server
from fido2.webauthn import (
    AttestedCredentialData,
    AuthenticationResponse,
    RegistrationResponse,
)

from msg.core.codec import b64, canonical, digest, freeze_json, loads, unb64, wire
from msg.core.errors import Failure, require
from msg.security.oauth import get, save, secret, state_id

CHALLENGE_TTL = 180
MAX_RESPONSE_BYTES = 32768
ALGORITHMS = frozenset({-7})  # ES256; validate the requested key and curve before binding.
PROVIDER_DATA_FIELDS = frozenset({
    'credential_data',
    'sign_count',
    'user_handle',
    'backup_eligible',
    'backup_state',
})


def user_handle(subject):
    require(isinstance(subject, str) and 1 <= len(subject) <= 160, 'invalid_passkey_subject')
    return sha256(b'msg-passkey-user-v1:' + subject.encode()).digest()


def _client_response(value, mode):
    require(isinstance(value, Mapping), 'invalid_passkey_response')
    require(len(canonical(value)) <= MAX_RESPONSE_BYTES, 'invalid_passkey_response')
    require(value.get('type') == 'public-key', 'invalid_passkey_response')
    require(value.get('id') == value.get('rawId'), 'invalid_passkey_response')
    credential_id = unb64(value.get('rawId'), limit=1023)
    require(bool(credential_id), 'invalid_passkey_response')
    response = value.get('response')
    require(isinstance(response, Mapping), 'invalid_passkey_response')
    client_data = loads(unb64(response.get('clientDataJSON'), limit=4096))
    require(isinstance(client_data, Mapping), 'invalid_passkey_response')
    require(client_data.get('crossOrigin', False) is False, 'invalid_passkey_response')
    require('topOrigin' not in client_data, 'invalid_passkey_response')
    require(len(unb64(client_data.get('challenge'), limit=64)) == 32, 'invalid_passkey_response')
    if mode == 'registration':
        unb64(response.get('attestationObject'), limit=16384)
        return RegistrationResponse.from_dict(value)
    unb64(response.get('authenticatorData'), limit=8192)
    unb64(response.get('signature'), limit=2048)
    unb64(response.get('userHandle'), limit=64)
    return AuthenticationResponse.from_dict(value)


def _state(value):
    require(
        isinstance(value, Mapping)
        and set(value) == {'challenge', 'user_verification', 'origin', 'rp_id', 'algorithms'}
        and value['user_verification'] == 'required'
        and isinstance(value['origin'], str)
        and isinstance(value['rp_id'], str)
        and value['algorithms'] == sorted(ALGORITHMS)
        and len(unb64(value['challenge'], limit=64)) == 32,
        'invalid_passkey_challenge',
    )
    return value


def _public_key(key):
    """Validate keys now, including coordinates skipped by attestation=none."""
    require(key.get(3) in ALGORITHMS, 'invalid_passkey_response')
    require(key.get(1) == 2 and key.get(-1) == 1, 'invalid_passkey_response')
    require(
        type(key.get(-2)) is bytes
        and len(key[-2]) == 32
        and type(key.get(-3)) is bytes
        and len(key[-3]) == 32,
        'invalid_passkey_response',
    )
    ec.EllipticCurvePublicNumbers(
        int.from_bytes(key[-2], 'big'), int.from_bytes(key[-3], 'big'), ec.SECP256R1()
    ).public_key()


def _backup(auth_data):
    eligible, backed_up = auth_data.is_backup_eligible(), auth_data.is_backed_up()
    require(eligible or not backed_up, 'invalid_passkey_response')
    return eligible, backed_up


def _stored(value, subject):
    require(
        isinstance(value, Mapping) and set(value) == PROVIDER_DATA_FIELDS, 'invalid_passkey_binding'
    )
    require(
        type(value['sign_count']) is int and 0 <= value['sign_count'] <= 0xFFFFFFFF,
        'invalid_passkey_binding',
    )
    require(
        type(value['backup_eligible']) is bool and type(value['backup_state']) is bool,
        'invalid_passkey_binding',
    )
    require(value['backup_eligible'] or not value['backup_state'], 'invalid_passkey_binding')
    require(
        hmac.compare_digest(unb64(value['user_handle'], limit=64), user_handle(subject)),
        'invalid_passkey_binding',
    )
    credential = AttestedCredentialData(unb64(value['credential_data'], limit=4096))
    require(credential.public_key.ALGORITHM in ALGORITHMS, 'invalid_passkey_binding')
    _public_key(credential.public_key)
    return credential


@dataclass(frozen=True, slots=True, repr=False)
class PasskeyProof:
    mode: str
    subject: str
    credential_id: str
    challenge: str
    provider_data: Mapping
    previous_data_digest: str | None = None
    state_digest: str = ''


class PasskeyServer:
    def __init__(self, origin):
        try:
            parsed = urlsplit(origin)
            valid = (
                isinstance(origin, str)
                and len(origin) <= 2000
                and bool(parsed.hostname)
                and not parsed.username
                and not parsed.password
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and not any(ord(character) <= 32 for character in origin)
                and '\\' not in origin
                and parsed.port != 0
                and (
                    parsed.scheme == 'https'
                    or (
                        parsed.scheme == 'http'
                        and parsed.hostname in {'localhost', '127.0.0.1', '::1'}
                    )
                )
            )
        except TypeError, ValueError:
            valid = False
        require(valid, 'invalid_passkey_origin')
        self.origin, self.rp_id = origin, parsed.hostname
        self.server = Fido2Server(
            {'id': self.rp_id, 'name': 'MSG'},
            attestation='none',
            verify_origin=lambda value: value == self.origin,
        )
        self.server.allowed_algorithms = [
            parameter for parameter in self.server.allowed_algorithms if parameter.alg in ALGORITHMS
        ]
        self.server.timeout = CHALLENGE_TTL * 1000

    def _issued_state(self, state):
        return dict(
            wire(state), origin=self.origin, rp_id=self.rp_id, algorithms=sorted(ALGORITHMS)
        )

    def _verify_state(self, state):
        state = _state(state)
        require(
            state['origin'] == self.origin and state['rp_id'] == self.rp_id,
            'invalid_passkey_challenge',
        )
        return state

    def registration_options(self, subject, name, credentials=()):
        existing = [
            AttestedCredentialData(unb64(value['credential_data'], limit=4096))
            for value in credentials
        ]
        options, state = self.server.register_begin(
            {'id': user_handle(subject), 'name': name, 'displayName': name},
            existing,
            resident_key_requirement='required',
            user_verification='required',
        )
        return loads(dumps(dict(options))), self._issued_state(state)

    def authentication_options(self):
        # Discoverable credentials avoid publishing per-account credential ID lists.
        options, state = self.server.authenticate_begin(user_verification='required')
        return loads(dumps(dict(options))), self._issued_state(state)

    def verify_registration(self, state, response, subject):
        try:
            state = self._verify_state(state)
            parsed = _client_response(response, 'registration')
            auth_data = self.server.register_complete(state, parsed)
            credential = auth_data.credential_data
            require(
                credential is not None and credential.credential_id == parsed.raw_id,
                'invalid_passkey_response',
            )
            require(credential.public_key.ALGORITHM in ALGORITHMS, 'invalid_passkey_response')
            _public_key(credential.public_key)
            eligible, backed_up = _backup(auth_data)
            data = freeze_json({
                'credential_data': b64(bytes(credential)),
                'sign_count': auth_data.counter,
                'user_handle': b64(user_handle(subject)),
                'backup_eligible': eligible,
                'backup_state': backed_up,
            })
            return PasskeyProof(
                'registration',
                subject,
                b64(parsed.raw_id),
                state['challenge'],
                data,
                state_digest=digest(state),
            )
        except Failure:
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            AssertionError,
            IndexError,
            RecursionError,
            OverflowError,
        ):
            raise Failure('invalid_passkey_response') from None

    def verify_authentication(self, state, response, subject, provider_data):
        try:
            state = self._verify_state(state)
            parsed = _client_response(response, 'authentication')
            credential = _stored(provider_data, subject)
            require(parsed.raw_id == credential.credential_id, 'invalid_passkey_response')
            require(
                parsed.response.user_handle is not None
                and hmac.compare_digest(parsed.response.user_handle, user_handle(subject)),
                'invalid_passkey_response',
            )
            self.server.authenticate_complete(state, [credential], parsed)
            auth_data = parsed.response.authenticator_data
            eligible, backed_up = _backup(auth_data)
            require(eligible == provider_data['backup_eligible'], 'invalid_passkey_response')
            previous, current = provider_data['sign_count'], auth_data.counter
            require(
                (previous == 0 and current == 0) or current > previous, 'passkey_counter_changed'
            )
            data = freeze_json(dict(provider_data, sign_count=current, backup_state=backed_up))
            return PasskeyProof(
                'authentication',
                subject,
                b64(parsed.raw_id),
                state['challenge'],
                data,
                digest(provider_data),
                digest(state),
            )
        except Failure:
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            AssertionError,
            IndexError,
            RecursionError,
            OverflowError,
        ):
            raise Failure('invalid_passkey_response') from None


def begin_challenge(tx, state, *, mode, now, flow, subject=None):
    require(mode in {'registration', 'authentication'}, 'invalid_passkey_challenge')
    require(isinstance(flow, str) and 32 <= len(flow) <= 512, 'invalid_passkey_challenge')
    if mode == 'registration':
        user_handle(subject)
    else:
        require(subject is None, 'invalid_passkey_challenge')
    challenge_id = secret()
    body = {
        'kind': 'passkey',
        'mode': mode,
        'state': wire(_state(state)),
        'subject': subject,
        'flow_hash': digest(('msg-passkey-flow-v1', flow)),
        'consumed_at': None,
    }
    # Random IDs are insert-only: even a collision cannot replace a pending ceremony.
    tx.execute(
        'INSERT INTO oauth_states VALUES (?,?,?,?)',
        (
            state_id('passkey', challenge_id),
            'passkey',
            wire(now + timedelta(seconds=CHALLENGE_TTL)),
            canonical(body).decode(),
        ),
        write=True,
    )
    return challenge_id


def read_challenge(tx, challenge_id, *, mode, now, flow, subject=None):
    require(
        isinstance(challenge_id, str) and 32 <= len(challenge_id) <= 64, 'invalid_passkey_challenge'
    )
    require(isinstance(flow, str) and 32 <= len(flow) <= 512, 'invalid_passkey_challenge')
    _, body = get(tx, state_id('passkey', challenge_id), now)
    kind = tx.one('SELECT kind FROM oauth_states WHERE id=?', (state_id('passkey', challenge_id),))
    require(
        kind is not None and kind[0] == 'passkey' and body.get('kind') == 'passkey',
        'invalid_passkey_challenge',
    )
    require(
        body.get('mode') == mode and body.get('consumed_at') is None, 'invalid_passkey_challenge'
    )
    require(
        hmac.compare_digest(body.get('flow_hash', ''), digest(('msg-passkey-flow-v1', flow))),
        'invalid_passkey_challenge',
    )
    require(body.get('subject') == subject, 'invalid_passkey_challenge')
    _state(body.get('state'))
    return body


@dataclass(frozen=True, slots=True, repr=False)
class _Evidence:
    proof: PasskeyProof
    challenge_id: str
    flow: str


_PASSKEY_EVIDENCE = ContextVar('msg_verified_passkey', default=None)


@contextmanager
def trusted_passkey(proof, *, challenge_id, flow):
    require(isinstance(proof, PasskeyProof), 'passkey_verification_required')
    token = _PASSKEY_EVIDENCE.set(_Evidence(proof, challenge_id, flow))
    try:
        yield
    finally:
        _PASSKEY_EVIDENCE.reset(token)


def _finalize(tx, verified, subject, mode, now):
    evidence = _PASSKEY_EVIDENCE.get()
    require(evidence is not None, 'passkey_verification_required')
    proof = evidence.proof
    require(
        verified.provider == 'passkey'
        and verified.sub == proof.credential_id
        and proof.mode == mode
        and proof.subject == subject,
        'passkey_verification_required',
    )
    body = read_challenge(
        tx,
        evidence.challenge_id,
        mode=mode,
        now=now,
        flow=evidence.flow,
        subject=subject if mode == 'registration' else None,
    )
    require(body['state']['challenge'] == proof.challenge, 'invalid_passkey_challenge')
    require(digest(body['state']) == proof.state_digest, 'invalid_passkey_challenge')
    return evidence, body


def finalize_registration(tx, verified, subject, *, now):
    evidence, body = _finalize(tx, verified, subject, 'registration', now)
    save(tx, state_id('passkey', evidence.challenge_id), dict(body, consumed_at=wire(now)))
    return wire(evidence.proof.provider_data)


def finalize_authentication(tx, verified, subject, provider_data, *, now):
    evidence, body = _finalize(tx, verified, subject, 'authentication', now)
    require(digest(provider_data) == evidence.proof.previous_data_digest, 'passkey_state_changed')
    save(tx, state_id('passkey', evidence.challenge_id), dict(body, consumed_at=wire(now)))
    return wire(evidence.proof.provider_data)
