"""Published custodial proof and statement bytes shared by both endpoints.

This module has no server-held key, transaction, current-authority or retirement
state. Producing a proof does not verify authority or attest key destruction.
"""

from __future__ import annotations

import hmac

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from msg.core.codec import b64, canonical, unb64
from msg.security.age_keys import private_from_identity

UPGRADE_CONTEXT_FIELDS = (
    'subject_id',
    'challenge_id',
    'request_id',
    'handle',
    'public_key',
    'encryption_recipient',
    'server_public',
    'nonce',
    'expires_at',
)


def _proof(shared, challenge):
    context = {name: challenge[name] for name in UPGRADE_CONTEXT_FIELDS}
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=unb64(context['nonce'], limit=64),
        info=b'msg-custodial-upgrade-pop-v1',
    ).derive(shared)
    return b64(hmac.digest(key, canonical(context), 'sha256'))


def client_upgrade_proof(age_identity, challenge):
    private = private_from_identity(age_identity)
    shared = private.exchange(
        X25519PublicKey.from_public_bytes(unb64(challenge['server_public'], limit=32))
    )
    return _proof(shared, challenge)


def stage_statement(subject, arguments, request_id):
    """Sign every decision field, not a caller-selected subset of the manifest."""
    return {
        'domain': 'msg-custodial-decision-v1',
        'subject_id': subject,
        'request_id': request_id,
        'decision': {key: value for key, value in arguments.items() if key != 'migration_ack'},
    }


def ack_statement(
    subject,
    challenge_id,
    inventory_digest,
    source,
    target,
    plaintext_digest,
    request_id,
    *,
    method='rewrap',
):
    return {
        'domain': 'msg-custodial-history-ack-v1',
        'subject_id': subject,
        'challenge_id': challenge_id,
        'inventory_digest': inventory_digest,
        'source': source,
        'target': target,
        'method': method,
        'plaintext_digest': plaintext_digest,
        'request_id': request_id,
    }
