"""Identity signatures and age recipients have separate durable key histories."""
import shutil
import subprocess

import pytest

from msg.core.codec import b64, canonical, wire
from msg.security.age_keys import (generate_age_key, recipient_from_identity,
    public_from_recipient, recipient_from_public, encryption_key_id)
from msg.security.crypto import Ed25519Signer, subject_id
from msg.core.errors import Failure
from msg.transports.dictionary import build_dictionary
from test_service import call


def test_age_recipient_round_trip():
    identity, recipient = generate_age_key()
    assert recipient.startswith('age1')
    assert recipient_from_identity(identity) == recipient
    assert recipient_from_public(public_from_recipient(recipient)) == recipient
    assert encryption_key_id(public_from_recipient(recipient)).startswith('ek_')
    assert encryption_key_id(public_from_recipient(recipient)) != Ed25519Signer.generate().key_id
    with pytest.raises(Failure, match='invalid_age_recipient'):
        public_from_recipient(recipient[:-1] + ('q' if recipient[-1] != 'q' else 'p'))
    with pytest.raises(Failure, match='invalid_age_recipient'):
        public_from_recipient(recipient_from_public(bytes(32)))


@pytest.mark.skipif(not shutil.which('age-keygen') or not shutil.which('age'),
                    reason='official age CLI unavailable')
def test_generated_identity_interoperates_with_official_age(tmp_path):
    identity, recipient = generate_age_key()
    key_file = tmp_path / 'age.key'
    key_file.write_text(identity + '\n')
    key_file.chmod(0o600)
    from_age = subprocess.check_output(['age-keygen', '-y', str(key_file)], text=True).strip()
    assert from_age == recipient
    ciphertext = subprocess.check_output(['age', '-e', '-r', recipient], input=b'age key compatibility')
    plaintext = subprocess.check_output(['age', '-d', '-i', str(key_file)], input=ciphertext)
    assert plaintext == b'age key compatibility'


@pytest.mark.asyncio
async def test_register_v1_denied_v2_creates_two_independent_public_keys(installed):
    app, _ = installed
    assert set(app.registry.schema(app.registry.operation('identity.register',1).input_schema)['properties']) == {
        'handle','public_key'}
    assert set(app.registry.schema(app.registry.operation('identity.register',2).input_schema)['required']) == {
        'handle','public_key','encryption_recipient'}
    codes=build_dictionary(app.registry)
    assert codes.code_for('operation','identity.register@1') != codes.code_for('operation','identity.register@2')
    signer = Ed25519Signer.generate()
    subject = subject_id(signer.public_key)
    old = await call(app, 'identity.register', {'handle': 'two-keys',
        'public_key': b64(signer.public_key)}, key=signer, subject=subject)
    assert old.status == 'error' and old.error.code == 'encryption_subkey_required', wire(old)
    _, recipient = generate_age_key()
    created = await call(app, 'identity.register', {'handle': 'two-keys',
        'public_key': b64(signer.public_key), 'encryption_recipient': recipient},
        key=signer, subject=subject, contract_version=2)
    assert created.status == 'ok', wire(created)
    assert created.data['key_id'] == signer.key_id
    assert created.data['encryption_key_id'] != signer.key_id
    assert created.data['encryption_recipient'] == recipient
    pk = await call(app, 'identity.identity_key_get', {'subject_id': subject})
    ek = await call(app, 'identity.encryption_key_get', {'subject_id': subject})
    assert pk.data['public_key'] == b64(signer.public_key) and pk.data['primary']
    assert ek.data['recipient'] == recipient and ek.data['primary']
    assert ek.data['algorithm'] == 'age-x25519' and pk.data['algorithm'] == 'ed25519'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM identity_keys WHERE subject=?', (subject,))[0] == 1
        assert tx.one('SELECT COUNT(*) FROM encryption_subkeys WHERE subject=?', (subject,))[0] == 1
        stored = tx.one('SELECT body FROM credentials WHERE id=?', (signer.key_id,))[0]
        assert 'AGE-SECRET-KEY' not in stored


@pytest.mark.asyncio
async def test_age_rotation_preserves_historical_recipient_without_signing_change(installed):
    app, _ = installed
    signer = Ed25519Signer.generate()
    subject = subject_id(signer.public_key)
    _, first = generate_age_key()
    registered = await call(app, 'identity.register', {'handle': 'rotate-age',
        'public_key': b64(signer.public_key), 'encryption_recipient': first},
        key=signer, subject=subject, contract_version=2)
    assert registered.status == 'ok', wire(registered)
    _, second = generate_age_key()
    rotated = await call(app, 'identity.encryption_key_rotate', {'encryption_recipient': second},
                         key=signer, subject=subject)
    assert rotated.status == 'ok', (rotated.error, wire(rotated))
    assert rotated.data['old_ciphertexts_require_rewrap'] is True
    old = await call(app, 'identity.encryption_key_get', {'subject_id': subject,
        'key_id': registered.data['encryption_key_id']})
    current = await call(app, 'identity.encryption_key_get', {'subject_id': subject})
    identity = await call(app, 'identity.identity_key_get', {'subject_id': subject})
    assert old.data['recipient'] == first and not old.data['primary'] and old.data['retired_at']
    assert current.data['recipient'] == second and current.data['primary']
    assert identity.data['key_id'] == signer.key_id and identity.data['primary']
    keys = await call(app, 'identity.encryption_key_list', {'subject_id': subject})
    assert len(keys.data['keys']) == 2


@pytest.mark.asyncio
async def test_identity_key_replacement_keeps_old_verification_material(installed):
    app, _ = installed
    first = Ed25519Signer.generate()
    subject = subject_id(first.public_key)
    _, recipient = generate_age_key()
    created = await call(app, 'identity.register', {'handle': 'identity-rotation',
        'public_key': b64(first.public_key), 'encryption_recipient': recipient},
        key=first, subject=subject, contract_version=2)
    assert created.status == 'ok', wire(created)
    second = Ed25519Signer.generate()
    public = b64(second.public_key)
    proof = second.sign(canonical({'subject_id': subject, 'public_key': public}), purpose='key-add')
    added = await call(app, 'identity.key_add', {'public_key': public,
        'possession_proof': wire(proof), 'ceiling': []}, key=first, subject=subject)
    assert added.status == 'ok', wire(added)
    revoked = await call(app, 'identity.key_revoke', {'key_id': first.key_id},
                         key=first, subject=subject)
    assert revoked.status == 'ok', wire(revoked)
    old = await call(app, 'identity.identity_key_get', {'subject_id': subject,
        'key_id': first.key_id})
    primary = await call(app, 'identity.identity_key_get', {'subject_id': subject})
    assert old.data['public_key'] == b64(first.public_key) and old.data['retired_at']
    assert not old.data['primary'] and primary.data['key_id'] == second.key_id
