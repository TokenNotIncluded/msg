"""Read-only evidence for a bounded custody migration, never retirement authority.

A valid ACK is the client's signed declaration that it decrypted one mapped copy.
It cannot prove that no external ciphertext, recipient or recoverable backup exists.
"""

from __future__ import annotations

from msg.core.codec import canonical, decode, unb64
from msg.core.errors import Failure
from msg.core.models import Signature
from msg.security.age_keys import encryption_key_id, public_from_recipient
from msg.security.backup_retirement import verified as verified_backup
from msg.security.crypto import key_id, verify


def _sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith('sha256:')
        and all(char in '0123456789abcdef' for char in value[7:])
    )


def _entry(value):
    return (
        isinstance(value, dict)
        and set(value) == {'id', 'revision', 'ciphertext_digest'}
        and all(isinstance(value[key], str) and value[key] for key in ('id', 'revision'))
        and _sha256(value['ciphertext_digest'])
    )


def history_evidence(subject_id, challenge_id, challenge, details, current):
    """Reverify persisted ACKs, not merely their presence or cached success flags.

    Legacy mappings bind the keys through the immutable challenge. New mappings
    additionally carry the exact old/new key IDs; a conflicting binding is denied.
    Unknown or malformed records stay pending and are not repaired by this read.
    """
    frozen = details.get('age_inventory')
    mappings = details.get('rewrap_mappings', {})
    acks = details.get('rewrap_acks', {})
    result = {
        'verification_scope': 'enumerated_keystore_age_revisions',
        'external_coverage': 'unknown',
        'inventory_changed': True,
        'mapped_count': len(mappings) if isinstance(mappings, dict) else 0,
        'acked_count': len(acks) if isinstance(acks, dict) else 0,
        'verified_acked_count': 0,
        'known_ciphertexts_migrated': False,
        'historical_revisions_require_migration': True,
    }
    try:
        if not (
            isinstance(frozen, list)
            and all(_entry(item) for item in frozen)
            and isinstance(mappings, dict)
            and isinstance(acks, dict)
            and isinstance(current, list)
            and all(_entry(item) for item in current)
        ):
            return result
        sources = {item['revision']: item for item in frozen}
        if len(sources) != len(frozen):
            return result
        public = unb64(challenge['public_key'], limit=32)
        if (
            challenge['subject_id'] != subject_id
            or challenge['challenge_id'] != challenge_id
            or key_id(public) != details['new_identity_key_id']
            or encryption_key_id(public_from_recipient(challenge['encryption_recipient']))
            != details['new_encryption_key_id']
        ):
            return result
        copies = []
        for revision, mapping in mappings.items():
            if not (
                isinstance(mapping, dict)
                and mapping.get('old') == sources.get(revision)
                and revision in sources
                and _entry(mapping.get('new'))
                and mapping['new']['revision'] != revision
                and mapping.get('recipient') == challenge['encryption_recipient']
                and mapping.get('old_encryption_key_id', details['old_encryption_key_id'])
                == details['old_encryption_key_id']
                and mapping.get('new_encryption_key_id', details['new_encryption_key_id'])
                == details['new_encryption_key_id']
            ):
                return result
            copies.append(mapping['new'])
        if len({item['revision'] for item in copies}) != len(copies):
            return result
        expected = sorted(frozen + copies, key=lambda item: (item['id'], item['revision']))
        result['inventory_changed'] = current != expected
        verified = 0
        for revision, ack in acks.items():
            mapping = mappings.get(revision)
            if not (
                mapping is not None
                and isinstance(ack, dict)
                and ack.get('old_revision') == revision
                and ack.get('new_revision') == mapping['new']['revision']
                and ack.get('ciphertext_digest') == mapping['new']['ciphertext_digest']
                and _sha256(ack.get('plaintext_digest'))
                and isinstance(ack.get('request_id'), str)
                and ack['request_id']
            ):
                continue
            signed = {
                'subject_id': subject_id,
                'challenge_id': challenge_id,
                'old': mapping['old'],
                'new': mapping['new'],
                'plaintext_digest': ack['plaintext_digest'],
                'request_id': ack['request_id'],
            }
            try:
                verify(
                    public,
                    canonical(signed),
                    decode(Signature, ack['signature']),
                    purpose='custodial-rewrap-ack',
                )
            except Failure, KeyError, TypeError, ValueError:
                continue
            verified += 1
        result['verified_acked_count'] = verified
        complete = (
            set(mappings) == set(acks) == set(sources)
            and verified == len(sources)
            and not result['inventory_changed']
        )
        result['known_ciphertexts_migrated'] = complete
        result['historical_revisions_require_migration'] = not complete
    except Failure, KeyError, TypeError, ValueError:
        # Persisted damaged state is not successful evidence, nor a reason to
        # expose ciphertext/secret details through an exception message.
        pass
    return result


async def retirement_evidence(tx, subject_id, details, *, history_recoverable, now=None):
    """Separate observable online retirement from locally attested backup retirement.

    backup_retired needs one exact root-signed record imported at the physical
    console; server_key_retired additionally needs observable online retirement.
    """
    new_signing = tx.one(
        'SELECT key_id FROM identity_keys WHERE subject=? AND is_primary=1', (subject_id,)
    )
    new_encryption = tx.one(
        'SELECT key_id FROM encryption_subkeys WHERE subject=? AND is_primary=1', (subject_id,)
    )
    switched = (
        new_signing is not None
        and new_encryption is not None
        and new_signing[0] == details.get('new_identity_key_id')
        and new_encryption[0] == details.get('new_encryption_key_id')
    )
    vault = tx.one(
        """SELECT status,signing_nonce,signing_ciphertext,age_nonce,age_ciphertext,
        destroyed_at,signing_key_id,encryption_key_id FROM custodial_vault WHERE subject=?""",
        (subject_id,),
    )
    destroyed = (
        vault is not None
        and vault[0] == 'destroyed'
        and vault[5] is not None
        and all(value is None for value in vault[1:5])
        and vault[6] == details.get('old_identity_key_id')
        and vault[7] == details.get('old_encryption_key_id')
    )
    old_signing = tx.one(
        'SELECT retired_at,is_primary FROM identity_keys WHERE subject=? AND key_id=?',
        (subject_id, details.get('old_identity_key_id')),
    )
    old_encryption = tx.one(
        'SELECT retired_at,is_primary FROM encryption_subkeys WHERE subject=? AND key_id=?',
        (subject_id, details.get('old_encryption_key_id')),
    )
    try:
        credential = await tx.credential(details.get('old_identity_key_id'))
        signing_revoked = credential.subject_id == subject_id and credential.revoked_at is not None
    except Failure:
        signing_revoked = False
    online = (
        switched
        and destroyed
        and signing_revoked
        and all(
            row is not None and row[0] is not None and not row[1]
            for row in (old_signing, old_encryption)
        )
    )
    status, attestation = await verified_backup(tx, subject_id, details, now=now)
    backup = attestation is not None and attestation.binds(subject_id, details)
    server = online and backup
    return {
        'identity_switched': switched,
        'history_recoverable': history_recoverable,
        'online_retired': online,
        'backup_retired': backup,
        'server_key_retired': server,
        'backup_retirement_evidence': status if not backup else 'local_attestation_verified',
        'backup_retirement_record': attestation.view() if backup else None,
        'completion_status': (
            'server_key_retired'
            if server and history_recoverable
            else 'pending_backup_retirement'
            if online and history_recoverable
            else 'pending_history'
            if switched
            else 'pending_identity_switch'
        ),
    }
