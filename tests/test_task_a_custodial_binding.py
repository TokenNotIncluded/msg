"""Persisted challenges must remain verifiable even for empty history.

These cases use real PostgreSQL and real Ed25519/age; a corrupted row must not
become trusted merely because there is no ciphertext ACK to verify.
"""
from copy import deepcopy

import pytest
from test_service import NOW

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.core.models import Credential
from msg.security.age_keys import encryption_key_id, generate_age_key, public_from_recipient
from msg.security.crypto import Ed25519Signer
from msg.security.custodial_migration import snapshot
from msg.storage.postgres import PostgresMetadataStore


@pytest.fixture
async def bound_history(pg_dsn):
    store = PostgresMetadataStore(pg_dsn)
    old, new = Ed25519Signer.generate(), Ed25519Signer.generate()
    old_recipient, recipient = generate_age_key()[1], generate_age_key()[1]
    challenge = {'subject_id': 'u_owner', 'challenge_id': 'cupg_bound',
                 'public_key': b64(new.public_key), 'encryption_recipient': recipient}
    details = {'age_inventory': [], 'new_identity_key_id': new.key_id,
               'old_identity_key_id': old.key_id,
               'new_encryption_key_id': encryption_key_id(public_from_recipient(recipient)),
               'old_encryption_key_id': encryption_key_id(public_from_recipient(old_recipient)),
               'server_signature': wire(old.sign(canonical(challenge),
                                                 purpose='custodial-upgrade-challenge'))}
    credential = Credential(id=old.key_id, subject_id='u_owner', kind='signing_key',
                            verifier=old.public_key, ceiling=(), not_before=NOW,
                            expires_at=None, revoked_at=None)
    async with store.transaction(write=True) as tx:
        tx.execute('INSERT INTO credentials VALUES (?,?,?)',
                   (old.key_id, 'u_owner', canonical(credential).decode()), write=True)
        for key, address, primary in ((details['old_encryption_key_id'], old_recipient, 0),
                                      (details['new_encryption_key_id'], recipient, 1)):
            tx.execute('INSERT INTO encryption_subkeys VALUES (?,?,?,?,?,?,?)',
                       (key, 'u_owner', address, b64(public_from_recipient(address)),
                        wire(NOW), wire(NOW) if not primary else None, primary), write=True)
    yield store, challenge, details
    await store.close()


@pytest.mark.asyncio
async def test_valid_empty_inventory_is_read_only_and_not_backup_retirement(bound_history):
    store, challenge, details = bound_history
    before = deepcopy(details)
    async with store.transaction(write=False) as tx:
        result = await snapshot(tx, 'u_owner', 'pending_rewrap', challenge, details)
        assert result.data['history_recoverable'] is True
        assert result.data['retirement']['backup_retired'] is False
        assert details == before


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['subject_id', 'challenge_id', 'public_key', 'encryption_recipient'])
async def test_persisted_challenge_tampering_is_rejected_without_any_acks(bound_history, field):
    store, challenge, details = bound_history
    changed = dict(challenge)
    changed[field] = {'subject_id': 'u_other', 'challenge_id': 'cupg_other',
                     'public_key': b64(Ed25519Signer.generate().public_key),
                     'encryption_recipient': generate_age_key()[1]}[field]
    async with store.transaction(write=False) as tx:
        with pytest.raises(Failure, match='^custodial_challenge_invalid$'):
            await snapshot(tx, 'u_owner', 'pending_rewrap', changed, details)


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['new_identity_key_id', 'new_encryption_key_id',
                                  'old_identity_key_id', 'old_encryption_key_id'])
async def test_stored_key_id_cannot_override_the_signed_challenge(bound_history, field):
    store, challenge, details = bound_history
    changed = dict(details, **{field: 'foreign-key'})
    async with store.transaction(write=False) as tx:
        with pytest.raises(Failure, match='^custodial_challenge_invalid$'):
            await snapshot(tx, 'u_owner', 'pending_rewrap', challenge, changed)


@pytest.mark.asyncio
async def test_retired_new_target_blocks_in_progress_migration(bound_history):
    store, challenge, details = bound_history
    async with store.transaction(write=True) as tx:
        tx.execute('UPDATE encryption_subkeys SET retired_at=?,is_primary=0 WHERE key_id=?',
                   (wire(NOW), details['new_encryption_key_id']), write=True)
    async with store.transaction(write=False) as tx:
        with pytest.raises(Failure, match='^custodial_encryption_target_changed$'):
            await snapshot(tx, 'u_owner', 'pending_rewrap', challenge, details)
