"""Custodial exit invariants; evidence is scoped, and deletion is not retirement.

The stored challenge binds both new public keys. Every later decision signs a
fresh inventory and result digest. A recovery envelope is compatibility recovery,
not a replacement ciphertext. No network claim can attest backup destruction.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from msg.core.codec import canonical, decode, digest, loads, unb64, wire
from msg.core.errors import Failure, require
from msg.core.models import ResourceRef, Signature
from msg.security.crypto import key_id, verify
from msg.security.age_keys import encryption_key_id, public_from_recipient
from msg.security.backup_retirement import BackupRetirement, verified as verified_backup


class MigrationPhase(StrEnum):
    CHALLENGE = 'challenge'
    MIGRATING = 'migrating'
    IDENTITY_SWITCHED = 'identity_switched'
    HISTORY_RECOVERABLE = 'history_recoverable'
    ONLINE_KEY_RETIRED = 'online_key_retired'
    SERVER_KEY_RETIRED = 'server_key_retired'


def ordered(items):
    return sorted(items, key=lambda item: (item['id'], item['revision']))


def inventory_commitment(subject, details, challenge):
    return digest({'domain': 'msg-custodial-inventory-v1', 'subject_id': subject,
        'challenge_id': challenge['challenge_id'],
        'version': details.get('inventory_version', 1),
        'old_identity_key_id': details['old_identity_key_id'],
        'new_identity_key_id': details['new_identity_key_id'],
        'old_encryption_key_id': details['old_encryption_key_id'],
        'new_encryption_key_id': details['new_encryption_key_id'],
        'recipient': challenge['encryption_recipient'],
        'items': details['age_inventory'],
        'source_keys': details.get('source_keys', {})})


def stage_statement(subject, arguments, request_id):
    """Sign every decision field, not a caller-selected subset of the manifest."""
    return {'domain': 'msg-custodial-decision-v1', 'subject_id': subject,
            'request_id': request_id,
            'decision': {key: value for key, value in arguments.items()
                         if key != 'migration_ack'}}


def ack_statement(subject, challenge_id, inventory_digest, source, target,
                  plaintext_digest, request_id, *, method='rewrap'):
    return {'domain': 'msg-custodial-history-ack-v1', 'subject_id': subject,
            'challenge_id': challenge_id, 'inventory_digest': inventory_digest,
            'source': source, 'target': target, 'method': method,
            'plaintext_digest': plaintext_digest, 'request_id': request_id}


def keyed_source(item, details):
    return dict(item, key_id=details.get('source_keys', {}).get(
        item['revision'], details['old_encryption_key_id']))


def keyed_target(mapping, details):
    return dict(mapping['new'], key_id=details['new_encryption_key_id'],
                recipient=mapping['recipient'])


async def owned_age_inventory(tx, subject):
    """All retained owned keystore age revisions, not just current revisions."""
    folder = tx.one("SELECT id FROM resources WHERE parent=? AND name='keystore'", (subject,))
    if folder is None:
        return []
    result = []
    for rid, revision_id in tx.rows('''SELECT r.id,v.id FROM revisions v
        JOIN resources r ON r.id=v.resource_id WHERE r.parent=? AND r.owner=?
        AND r.type='keystore' ORDER BY r.id,v.id''', (folder[0], subject)):
        if tx.setting('keystore_format:' + revision_id) == 'age':
            revision = await tx.revision(ResourceRef(id=rid, revision=revision_id))
            result.append({'id': rid, 'revision': revision_id,
                           'ciphertext_digest': revision.content.digest})
    return result


def output_refs(details):
    """Outputs are not new source work; direct new-key writes remain sources."""
    result = [m['new'] for m in details.get('rewrap_mappings', {}).values()
              if m.get('copy_kind') != 'new_key_write']
    envelope = details.get('recovery_envelope')
    if envelope is not None:
        result.append(envelope['ciphertext'])
    return result


def retirement_view(details, vault, *, identity_switched, subject_id=None, backup=None,
                    backup_status='requires_offline_verification'):
    """An absent online key cannot prove that historical backups lost that key."""
    signing_deleted = bool(vault and vault[1] is None and vault[2] is None)
    encryption_deleted = bool(vault and vault[3] is None and vault[4] is None)
    # Only a locally imported, root-signed record verified by
    # msg.security.backup_retirement may set this field. The network migration
    # API never writes or accepts one; details['backup_retirement'] is ignored.
    retired = isinstance(backup, BackupRetirement) and backup.binds(subject_id, details)
    online = bool(identity_switched and vault and vault[0] == 'destroyed' and
                  signing_deleted and encryption_deleted)
    return {'identity_switched': identity_switched,
            'token_revoked': identity_switched,
            'online_signing_key_deleted': signing_deleted,
            'online_encryption_key_deleted': encryption_deleted,
            'backup_retired': retired,
            'backup_status': 'local_attestation_verified' if retired else backup_status,
            'backup_retirement_record': backup.view() if retired else None,
            'server_key_retired': retired and online}


@dataclass(frozen=True)
class MigrationSnapshot:
    data: dict

    @property
    def inventory_digest(self):
        return self.data['inventory_digest']

    def unchanged(self):
        require(not self.data['inventory_changed'], 'custodial_inventory_changed')

    def require_observation(self, arguments):
        require(arguments.get('inventory_digest') == self.inventory_digest and
                arguments.get('results_digest') == self.data['results_digest'] and
                arguments.get('observed_digest') == self.data['observed_digest'],
                'custodial_migration_preview_stale')


async def verify_challenge_binding(tx, subject, status, challenge, details):
    """Verify the original challenge even when there are no history ACKs.

    The retired signing credential remains a historical verifier, not current
    authority. A mutable copy of a key ID in the migration body is not a proof.
    """
    try:
        public = unb64(challenge['public_key'], limit=32)
        old = await tx.credential(details['old_identity_key_id'])
        require(challenge['subject_id'] == subject and old.subject_id == subject and
                old.kind == 'signing_key' and key_id(old.verifier) == old.id and
                key_id(public) == details['new_identity_key_id'] and
                old.id != details['new_identity_key_id'] and
                encryption_key_id(public_from_recipient(challenge['encryption_recipient'])) ==
                    details['new_encryption_key_id'] and
                details['old_encryption_key_id'] != details['new_encryption_key_id'],
                'custodial_challenge_invalid')
        verify(old.verifier, canonical(challenge), decode(Signature, details['server_signature']),
               purpose='custodial-upgrade-challenge')
        old_age = tx.one('SELECT subject,recipient,public_key FROM encryption_subkeys WHERE key_id=?',
                         (details['old_encryption_key_id'],))
        require(old_age is not None and old_age[0] == subject and
                encryption_key_id(public_from_recipient(old_age[1])) == details['old_encryption_key_id'] and
                unb64(old_age[2], limit=32) == public_from_recipient(old_age[1]),
                'custodial_challenge_invalid')
    except (Failure, KeyError, TypeError, ValueError) as exc:
        raise Failure('custodial_challenge_invalid') from exc
    if status != 'completed':
        require(old.revoked_at is None, 'custodial_signing_key_retired')
    retained = tx.one('SELECT status FROM custodial_vault WHERE subject=?', (subject,))
    if status == 'pending_rewrap' or (status == 'completed' and retained == ('decrypt_only',)):
        target = tx.one('SELECT subject,recipient,is_primary,retired_at FROM encryption_subkeys '
                        'WHERE key_id=?', (details['new_encryption_key_id'],))
        require(target == (subject, challenge['encryption_recipient'], 1, None),
                'custodial_encryption_target_changed')


async def snapshot(tx, subject, status, challenge, details, *, now=None):
    """Without a trusted clock a backup attestation cannot be current, so it is not used."""
    await verify_challenge_binding(tx, subject, status, challenge, details)
    frozen = details.get('age_inventory')
    require(isinstance(frozen, list), 'custodial_inventory_required')
    require(frozen == ordered(frozen) and len({i['revision'] for i in frozen}) == len(frozen),
            'custodial_inventory_corrupt')
    current = await owned_age_inventory(tx, subject)
    outputs = output_refs(details)
    current_sources = [item for item in current if item not in outputs]
    expected = ordered(frozen + [item for item in outputs if item not in frozen])
    added = [item for item in current_sources if item not in frozen]
    missing = [item for item in frozen if item not in current]
    drift = current != expected
    commitment = inventory_commitment(subject, details, challenge)
    mappings = details.get('rewrap_mappings', {})
    acks = details.get('rewrap_acks', {})
    recovery_acks = details.get('recovery_acks', {})
    envelope = details.get('recovery_envelope')
    sources = {item['revision']: item for item in frozen}
    require(set(mappings) <= sources.keys() and set(acks) <= sources.keys() and
            set(recovery_acks) <= sources.keys(), 'custodial_mapping_corrupt')
    targets = set()
    verified, legacy_acked, methods = set(), set(), {}
    for revision, mapping in mappings.items():
        source = sources[revision]
        require(mapping['old'] == source and
                mapping['recipient'] == challenge['encryption_recipient'] and
                mapping.get('old_key_id', keyed_source(source, details)['key_id']) ==
                    keyed_source(source, details)['key_id'] and
                mapping.get('new_key_id', details['new_encryption_key_id']) ==
                    details['new_encryption_key_id'], 'custodial_mapping_key_mismatch')
        ref = mapping['new']
        marker = (ref['id'], ref['revision'])
        require(marker not in targets, 'custodial_mapping_target_reused')
        targets.add(marker)
        if ref not in current or source in missing:
            continue
        ack = acks.get(revision)
        if ack is None:
            continue
        require(ack['new_revision'] == ref['revision'] and
                ack['ciphertext_digest'] == ref['ciphertext_digest'],
                'custodial_rewrap_mapping_mismatch')
        legacy_acked.add(revision)
        if ack.get('inventory_digest') != commitment:
            continue
        body = ack_statement(subject, challenge['challenge_id'], commitment,
            keyed_source(source, details), keyed_target(mapping, details),
            ack['plaintext_digest'], ack['request_id'])
        verify(unb64(challenge['public_key']), canonical(body), decode(Signature, ack['signature']),
               purpose='custodial-history-ack-v1')
        verified.add(revision)
        methods[revision] = 'rewrap'
    if envelope is not None:
        require(envelope['subject_id'] == subject and
                envelope['old_key_id'] == details['old_encryption_key_id'] and
                envelope['new_key_id'] == details['new_encryption_key_id'] and
                envelope['recipient'] == challenge['encryption_recipient'] and
                envelope['purpose'] == 'retired-encryption-subkey-recovery',
                'custodial_recovery_envelope_mismatch')
        if envelope['ciphertext'] in current:
            for revision, ack in recovery_acks.items():
                if sources[revision] in missing or ack.get('inventory_digest') != commitment:
                    continue
                require(keyed_source(sources[revision], details)['key_id'] == envelope['old_key_id'],
                        'custodial_recovery_key_mismatch')
                body = ack_statement(subject, challenge['challenge_id'], commitment,
                    keyed_source(sources[revision], details), envelope,
                    ack['plaintext_digest'], ack['request_id'], method='compatibility_recovery')
                verify(unb64(challenge['public_key']), canonical(body),
                       decode(Signature, ack['signature']), purpose='custodial-history-ack-v1')
                verified.add(revision)
                methods[revision] = 'compatibility_recovery'
    policy_row = tx.one('SELECT body FROM recovery_policies WHERE subject=? ORDER BY version DESC LIMIT 1',
                        (subject,))
    policy = loads(policy_row[0]) if policy_row else {'version': 0, 'opted_in': False}
    vault = tx.one('''SELECT status,signing_nonce,signing_ciphertext,age_nonce,age_ciphertext
        FROM custodial_vault WHERE subject=?''', (subject,))
    switched = status == 'completed'
    backup_status, backup = await verified_backup(tx, subject, details, now=now) if switched \
        else ('unavailable', None)
    retirement = retirement_view(details, vault, identity_switched=switched, subject_id=subject,
        backup=backup, backup_status='requires_offline_verification' if backup_status == 'unavailable'
                                     else 'local_attestation_' + backup_status)
    complete = not drift and verified == set(sources)
    phase = MigrationPhase.CHALLENGE if status == 'pending' else MigrationPhase.MIGRATING
    if switched:
        phase = MigrationPhase.HISTORY_RECOVERABLE if complete else MigrationPhase.IDENTITY_SWITCHED
        if retirement['online_signing_key_deleted'] and retirement['online_encryption_key_deleted']:
            phase = MigrationPhase.ONLINE_KEY_RETIRED
        if retirement['server_key_retired']:
            phase = MigrationPhase.SERVER_KEY_RETIRED
    results = {'mappings': mappings, 'acks': acks, 'recovery_envelope': envelope,
               'recovery_acks': recovery_acks, 'resolution': details.get('resolution')}
    return MigrationSnapshot({'challenge_id': challenge['challenge_id'], 'status': status,
        'phase': str(phase), 'recipient': challenge['encryption_recipient'],
        'old_key_id': details['old_encryption_key_id'], 'new_key_id': details['new_encryption_key_id'],
        'frozen': frozen, 'sources': [keyed_source(item, details) for item in frozen],
        'frozen_digest': digest(frozen), 'inventory_digest': commitment,
        'inventory_version': details.get('inventory_version', 1),
        'observed_digest': digest(current), 'results_digest': digest(results),
        'mappings': mappings, 'acks': acks, 'recovery_envelope': envelope,
        'recovery_acks': recovery_acks, 'methods': methods,
        'inventory_changed': drift, 'delta': {'added': added, 'missing': missing},
        'mapped_count': len(mappings), 'acked_count': len(legacy_acked),
        'verified_revisions': sorted(verified),
        'unresolved_revisions': sorted(set(sources) - verified),
        'known_ciphertexts_migrated': not drift and legacy_acked == set(sources),
        'history_recoverable': complete,
        'verification_scope': 'enumerated_and_client_verified_revisions_only',
        'recovery_policy_version': policy['version'],
        'recovery_policy_requires_review': bool(policy.get('opted_in')),
        'external_ciphertexts_migrated_owner_claim': details.get('external_ciphertexts_migrated', False),
        'external_ciphertexts_verified': False,
        'historical_revisions_require_migration': not complete,
        'finalize_ready': complete and not policy.get('opted_in'),
        'vault_remains_active': bool(vault and vault[0] == 'active'),
        'retirement': retirement})
