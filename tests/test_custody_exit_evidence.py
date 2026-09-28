"""Verify real age copies before ACK, and distinguish online from backup retirement."""
import os
import shutil

import pytest

from msg.client_recovery import _age
from msg.core.codec import b64, canonical, digest, loads, unb64, wire
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer
from test_custodial_rewrap import _trusted_age_entry
from test_custodial_upgrade import begin, finish
from test_service import call


@pytest.mark.asyncio
async def test_empty_upgrade_still_reports_backup_retirement_pending(installed):
    app, _ = installed
    created = await call(app, 'identity.custodial_create', {'handle':'exit-state',
        'nonce':b64(os.urandom(32)), 'recovery_secret':b64(os.urandom(32))}, contract_version=2)
    subject = created.data['subject_id']
    token = (created.data['credential_id'], unb64(created.data['token']))
    signer = Ed25519Signer.generate()
    age_key, recipient = generate_age_key()
    challenge = await begin(app, subject, token, signer, recipient)
    done = await finish(app, subject, token, signer, age_key, challenge.data)
    assert done.status == 'ok' and done.data['status'] == 'completed'
    result = await call(app, 'identity.custodial_upgrade_inventory',
        {'challenge_id':challenge.data['challenge_id']}, key=signer, subject=subject)
    assert result.status == 'ok', wire(result)
    retirement = result.data['retirement']
    assert retirement['identity_switched']
    assert retirement['online_signing_key_deleted'] and retirement['online_encryption_key_deleted']
    assert result.data['phase'] == 'online_key_retired'
    assert result.data['history_recoverable']
    assert result.data['verification_scope'] == 'enumerated_and_client_verified_revisions_only'
    assert retirement['backup_retired'] is False and retirement['server_key_retired'] is False


@pytest.mark.asyncio
async def test_changed_persisted_ack_is_not_still_successful_evidence(installed, tmp_path):
    if shutil.which('age') is None:
        pytest.skip('age CLI is unavailable')
    app, _ = installed
    created = await call(app, 'identity.custodial_create', {'handle':'ack-corruption',
        'nonce':b64(os.urandom(32)), 'recovery_secret':b64(os.urandom(32))}, contract_version=2)
    subject = created.data['subject_id']
    token = (created.data['credential_id'], unb64(created.data['token']))
    plaintext = b'client-verified historical content\n'
    old_cipher = _age('--encrypt', '--recipient', created.data['encryption_recipient'], input_data=plaintext)
    rid, revision = await _trusted_age_entry(app, subject, old_cipher)
    signer = Ed25519Signer.generate()
    age_key, recipient = generate_age_key()
    challenge = await begin(app, subject, token, signer, recipient)
    cid = challenge.data['challenge_id']
    await finish(app, subject, token, signer, age_key, challenge.data)
    mapped = await call(app, 'identity.custodial_rewrap_revision', {
        'challenge_id':cid, 'ciphertext_ref':{'id':rid, 'revision':revision},
        'old_encryption_key_id':created.data['encryption_key_id'], 'new_recipient':recipient},
        subject=subject, token=token)
    assert mapped.status == 'ok', wire(mapped)
    mapping = mapped.data['mapping']
    assert mapping['old_key_id'] == created.data['encryption_key_id']
    assert mapping['new_key_id'] != mapping['old_key_id']
    fetched = await call(app, 'identity.custodial_migration_get',
        {'challenge_id':cid, 'old_revision':revision}, subject=subject, token=token)
    assert fetched.status == 'ok', wire(fetched)
    async with app.metadata.transaction(write=False) as tx:
        from msg.core.models import ResourceRef
        new = await tx.revision(ResourceRef(id=mapping['new']['id'], revision=mapping['new']['revision']))
    identity = tmp_path/'age.key'
    identity.write_text(age_key+'\n'); identity.chmod(0o600)
    ciphertext = await app.contents.read_bytes(new.content)
    assert _age('--decrypt', '--identity', str(identity), input_data=ciphertext) == plaintext
    inventory = await call(app, 'identity.custodial_upgrade_inventory', {'challenge_id':cid},
                           subject=subject, token=token)
    assert inventory.status == 'ok', wire(inventory)
    request_id = 'verified-ack'
    from msg.security.custodial_migration import ack_statement, keyed_source, keyed_target
    async with app.metadata.transaction(write=False) as tx:
        details = loads(tx.one('SELECT body FROM custodial_upgrades WHERE id=?', (cid,))[0])
    signed = ack_statement(subject, cid, inventory.data['inventory_digest'],
        keyed_source(mapping['old'], details), keyed_target(mapping, details),
        digest(plaintext), request_id, method='rewrap')
    ack = await call(app, 'identity.custodial_rewrap_ack', {
        'challenge_id':cid, 'old_revision':revision, 'new_revision':mapping['new']['revision'],
        'ciphertext_digest':mapping['new']['ciphertext_digest'], 'plaintext_digest':digest(plaintext),
        'inventory_digest':inventory.data['inventory_digest'], 'method':'rewrap',
        'decryption_ack':wire(signer.sign(canonical(signed), purpose='custodial-history-ack-v1'))},
        subject=subject, token=token, rid=request_id, contract_version=2)
    assert ack.status == 'ok', wire(ack)
    inventory = await call(app, 'identity.custodial_upgrade_inventory', {'challenge_id':cid},
                           subject=subject, token=token)
    assert inventory.data['history_recoverable']
    assert revision in inventory.data['verified_revisions']
    async with app.metadata.transaction(write=True) as tx:
        details = loads(tx.one('SELECT body FROM custodial_upgrades WHERE id=?', (cid,))[0])
        details['rewrap_acks'][revision]['plaintext_digest'] = digest(b'changed after signature')
        tx.execute('UPDATE custodial_upgrades SET body=? WHERE id=?',
                   (canonical(details).decode(), cid), write=True)
    damaged = await call(app, 'identity.custodial_upgrade_inventory', {'challenge_id':cid},
                         subject=subject, token=token)
    assert damaged.status == 'error'
    assert damaged.error is not None
