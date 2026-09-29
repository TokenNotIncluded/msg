"""Root-signed attestation that listed backup sets no longer hold one old key.

Only the physical-console import in msg.admin.backup_retirement persists a
record, under a settings key that no network operation writes. Every read
re-verifies the signature against the current, unrevoked root credential and the
exact subject and old key IDs; an absent, altered, expired or superseded-root
record is not evidence. The statement is an operator attestation for the listed
backup sets only, not a proof that no other copy of the key exists anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, decode, digest, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import Signature
from msg.security.certificates import certificate_body
from msg.security.crypto import key_id, verify

DOMAIN = 'msg-custodial-backup-retirement-v1'
PURPOSE = 'custodial-backup-retirement-v1'
PREFIX = 'custodial_backup_retirement:'
MAX_VALIDITY = timedelta(days=400)
FIELDS = frozenset({
    'domain',
    'subject_id',
    'old_identity_key_id',
    'old_encryption_key_id',
    'backup_sets',
    'attested_at',
    'not_after',
    'operator',
})


@dataclass(frozen=True)
class BackupRetirement:
    """Constructed only by check(); a bare boolean or dict is never accepted."""

    subject_id: str
    old_identity_key_id: str
    old_encryption_key_id: str
    backup_sets: tuple
    attested_at: datetime
    not_after: datetime
    signer_key_id: str
    record_digest: str

    def binds(self, subject_id, details):
        return (
            self.subject_id == subject_id
            and self.old_identity_key_id == details.get('old_identity_key_id')
            and self.old_encryption_key_id == details.get('old_encryption_key_id')
        )

    def view(self):
        return {
            'scope': 'listed_backup_sets_only',
            'backup_sets': list(self.backup_sets),
            'attested_at': wire(self.attested_at),
            'not_after': wire(self.not_after),
            'signer_key_id': self.signer_key_id,
            'record_digest': self.record_digest,
        }


def setting_key(subject_id, old_identity_key_id):
    return PREFIX + subject_id + ':' + old_identity_key_id


def statement(subject_id, details, backup_sets, *, attested_at, not_after, operator):
    return {
        'domain': DOMAIN,
        'subject_id': subject_id,
        'old_identity_key_id': details['old_identity_key_id'],
        'old_encryption_key_id': details['old_encryption_key_id'],
        'backup_sets': sorted(set(backup_sets)),
        'attested_at': wire(attested_at),
        'not_after': wire(not_after),
        'operator': operator,
    }


def sign_record(body, signer):
    return {'statement': body, 'signature': wire(signer.sign(canonical(body), purpose=PURPOSE))}


async def root_verifier(tx, *, now):
    """Recheck the current self-signed Root and its credential, not just historical validity."""
    try:
        certificate_id = tx.setting('active_root_certificate')
        certificate = await tx.certificate(certificate_id)
        credential = await tx.credential(certificate.key_id)
        require(
            certificate.resource_id == certificate_id
            and certificate.subject_id == certificate.issuer_id == ROOT_SUBJECT
            and certificate.parent_certificate_id is None
            and certificate.kind == 'ca'
            and certificate.issuance is not None
            and certificate.not_before <= now < certificate.expires_at
            and not await tx.certificate_revoked(certificate_id)
            and credential.subject_id == ROOT_SUBJECT
            and credential.kind == 'signing_key'
            and credential.revoked_at is None
            and credential.not_before <= now
            and (credential.expires_at is None or now < credential.expires_at)
            and key_id(credential.verifier) == credential.id == certificate.key_id,
            'backup_retirement_root_unavailable',
        )
        verify(
            credential.verifier,
            canonical(certificate_body(certificate)),
            certificate.signature,
            purpose='certificate',
        )
    except (Failure, KeyError, TypeError, ValueError) as exc:
        raise Failure('backup_retirement_root_unavailable') from exc
    return credential.verifier


def _sets(value):
    return (
        isinstance(value, list)
        and bool(value)
        and value == sorted(set(value))
        and all(isinstance(item, str) and 0 < len(item) <= 256 for item in value)
    )


def check(record, verifier, subject_id, details, *, now):
    """Return the attestation only if this exact record is valid now for these old keys."""
    try:
        require(
            isinstance(record, dict) and set(record) == {'statement', 'signature'},
            'backup_retirement_invalid',
        )
        body = record['statement']
        require(
            isinstance(body, dict)
            and set(body) == FIELDS
            and body['domain'] == DOMAIN
            and all(
                isinstance(body[name], str) and body[name]
                for name in (
                    'subject_id',
                    'old_identity_key_id',
                    'old_encryption_key_id',
                    'operator',
                )
            )
            and _sets(body['backup_sets']),
            'backup_retirement_invalid',
        )
        attested, not_after = parse_time(body['attested_at']), parse_time(body['not_after'])
        signature = decode(Signature, record['signature'])
        verify(verifier, canonical(body), signature, purpose=PURPOSE)
        # Subtract instead of adding to an untrusted date near datetime.max.
        require(timedelta(0) < not_after - attested <= MAX_VALIDITY, 'backup_retirement_invalid')
    except (Failure, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise Failure('backup_retirement_invalid') from exc
    require(
        isinstance(details, dict)
        and body['subject_id'] == subject_id
        and body['old_identity_key_id'] == details.get('old_identity_key_id')
        and body['old_encryption_key_id'] == details.get('old_encryption_key_id'),
        'backup_retirement_binding_mismatch',
    )
    try:
        current = attested <= now < not_after
    except TypeError:
        current = False
    require(current, 'backup_retirement_expired')
    return BackupRetirement(
        subject_id=subject_id,
        old_identity_key_id=body['old_identity_key_id'],
        old_encryption_key_id=body['old_encryption_key_id'],
        backup_sets=tuple(body['backup_sets']),
        attested_at=attested,
        not_after=not_after,
        signer_key_id=signature.key_id,
        record_digest=digest(record),
    )


async def verified(tx, subject_id, details, *, now):
    """(status, attestation-or-None); any failure is reported as not retired."""
    old = details.get('old_identity_key_id') if isinstance(details, dict) else None
    if not isinstance(subject_id, str) or not isinstance(old, str) or not old:
        return 'unavailable', None
    record = tx.setting(setting_key(subject_id, old))
    if record is None:
        return 'unavailable', None
    if now is None:
        return 'clock_unavailable', None
    try:
        return 'verified', check(
            record, await root_verifier(tx, now=now), subject_id, details, now=now
        )
    except Failure as exc:
        return ('expired' if exc.code == 'backup_retirement_expired' else 'invalid'), None
