"""Recovery records are owner opt-in metadata, never authority or decryption proof."""

from dataclasses import replace

import pytest
from test_service import call, register

from msg.core.codec import b64, wire
from msg.security.age_keys import encryption_key_id, generate_age_key, public_from_recipient


async def current_key(app, subject):
    result = await call(app, 'identity.encryption_key_get', {'subject_id': subject})
    assert result.status == 'ok'
    return result.data['key_id']


@pytest.mark.asyncio
async def test_policy_default_deny_and_signed_owner_opt_in(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'recovery-policy-owner')
    other_key, other, _ = await register(app, 'recovery-policy-other')
    empty = await call(app, 'identity.recovery_policy_get', {}, key=owner_key, subject=owner)
    assert empty.status == 'ok' and empty.data['version'] == 0 and empty.data['recipients'] == ()
    directory = await call(app, 'identity.recovery_custodians', {})
    assert directory.status == 'ok' and not directory.data['items']
    recipient = generate_age_key()[1]
    fingerprint = encryption_key_id(public_from_recipient(recipient))
    own_key = await current_key(app, owner)
    wrong = await call(
        app,
        'identity.recovery_policy_set',
        {
            'expected_version': 0,
            'encryption_key_id': await current_key(app, other),
            'recipients': [{'recipient': recipient}],
        },
        key=owner_key,
        subject=owner,
    )
    assert wrong.status == 'error'
    chosen = await call(
        app,
        'identity.recovery_policy_set',
        {
            'expected_version': 0,
            'encryption_key_id': own_key,
            'recipients': [{'recipient': recipient}],
        },
        key=owner_key,
        subject=owner,
        rid='policy-one',
    )
    assert chosen.status == 'ok' and chosen.data['version'] == 1
    assert chosen.data['recipients'][0]['fingerprint'] == fingerprint
    replay = await call(
        app,
        'identity.recovery_policy_set',
        {
            'expected_version': 0,
            'encryption_key_id': own_key,
            'recipients': [{'recipient': recipient}],
        },
        key=owner_key,
        subject=owner,
        rid='policy-one',
    )
    assert replay.status == 'ok' and replay.replayed
    conflict = await call(
        app,
        'identity.recovery_policy_set',
        {
            'expected_version': 0,
            'encryption_key_id': own_key,
            'recipients': [{'recipient': recipient}],
        },
        key=owner_key,
        subject=owner,
    )
    assert conflict.status == 'error'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM recovery_policies WHERE subject=?', (owner,))[0] == 1
    denied = await call(app, 'discovery.get', {'id': '/private'}, key=owner_key, subject=owner)
    assert denied.status == 'error'


@pytest.mark.asyncio
async def test_envelope_binds_exact_private_age_revision_and_owner_declared_recipient(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'envelope-owner')
    other_key, other, _ = await register(app, 'envelope-other')
    recipient = generate_age_key()[1]
    fingerprint = encryption_key_id(public_from_recipient(recipient))
    own_key = await current_key(app, owner)
    policy = await call(
        app,
        'identity.recovery_policy_set',
        {
            'expected_version': 0,
            'encryption_key_id': own_key,
            'recipients': [{'recipient': recipient}],
        },
        key=owner_key,
        subject=owner,
    )
    assert policy.status == 'ok', wire(policy)
    ciphertext = b'age-encryption.org/v1\nmetadata-test-ciphertext'
    stored = await call(
        app,
        'keystore.put',
        {'name': 'recovery-envelope', 'format': 'age', 'ciphertext': b64(ciphertext)},
        key=owner_key,
        subject=owner,
    )
    assert stored.status == 'ok', wire(stored)
    ref = wire(stored.resources[0])
    args = {
        'ciphertext_ref': ref,
        'encryption_key_id': own_key,
        'policy_version': 1,
        'recipient_fingerprints': [fingerprint],
        'purpose': 'encryption-subkey-recovery',
    }
    registered = await call(
        app,
        'identity.recovery_envelope_register',
        args,
        key=owner_key,
        subject=owner,
        rid='envelope-one',
    )
    assert registered.status == 'ok', wire(registered)
    assert registered.data['recipient_claim'] == 'owner_declared_unverified'
    replay = await call(
        app,
        'identity.recovery_envelope_register',
        args,
        key=owner_key,
        subject=owner,
        rid='envelope-one',
    )
    assert replay.status == 'ok' and replay.replayed
    found = await call(
        app,
        'identity.recovery_envelope_get',
        {'id': registered.data['id']},
        key=owner_key,
        subject=owner,
    )
    assert found.status == 'ok' and found.data['ciphertext_ref'] == ref
    outsider = await call(
        app,
        'identity.recovery_envelope_get',
        {'id': registered.data['id']},
        key=other_key,
        subject=other,
    )
    assert outsider.status == 'error'
    wrong_fingerprint = await call(
        app,
        'identity.recovery_envelope_register',
        {**args, 'recipient_fingerprints': [own_key]},
        key=owner_key,
        subject=owner,
    )
    assert wrong_fingerprint.status == 'error'
    wrong_purpose = await call(
        app,
        'identity.recovery_envelope_register',
        {**args, 'purpose': 'account-recovery'},
        key=owner_key,
        subject=owner,
    )
    assert wrong_purpose.status == 'error'
    bad_revision = await call(
        app,
        'identity.recovery_envelope_register',
        {**args, 'ciphertext_ref': {'id': ref['id'], 'revision': 'v_other'}},
        key=owner_key,
        subject=owner,
    )
    assert bad_revision.status == 'error'
    other_stored = await call(
        app,
        'keystore.put',
        {'name': 'other-recovery', 'format': 'age', 'ciphertext': b64(ciphertext)},
        key=other_key,
        subject=other,
    )
    cross = await call(
        app,
        'identity.recovery_envelope_register',
        {**args, 'ciphertext_ref': wire(other_stored.resources[0])},
        key=owner_key,
        subject=owner,
    )
    assert cross.status == 'error'
    replaced = await call(
        app,
        'keystore.put',
        {
            'id': ref['id'],
            'name': 'recovery-envelope',
            'format': 'age',
            'ciphertext': b64(b'age-encryption.org/v1\nnew-ciphertext'),
        },
        key=owner_key,
        subject=owner,
        expected=((ref['id'], stored.data['generation']),),
    )
    assert replaced.status == 'ok', wire(replaced)
    stale = await call(
        app, 'identity.recovery_envelope_register', args, key=owner_key, subject=owner
    )
    assert stale.status == 'error' and stale.error.code == 'recovery_revision_not_current'
    historical = await call(
        app,
        'identity.recovery_envelope_get',
        {'id': registered.data['id']},
        key=owner_key,
        subject=owner,
    )
    assert historical.status == 'ok' and historical.data['ciphertext_ref'] == ref
    opted_out = await call(
        app,
        'identity.recovery_policy_set',
        {'expected_version': 1, 'encryption_key_id': own_key, 'recipients': []},
        key=owner_key,
        subject=owner,
    )
    assert opted_out.status == 'ok' and not opted_out.data['opted_in']
    after_opt_out = await call(
        app,
        'identity.recovery_envelope_register',
        {**args, 'ciphertext_ref': wire(replaced.resources[0]), 'policy_version': 2},
        key=owner_key,
        subject=owner,
    )
    assert after_opt_out.status == 'error'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM recovery_envelopes WHERE owner=?', (owner,))[0] == 1


def test_platform_custodian_config_rejects_private_material(tmp_path):
    from msg.config import load_settings, write_example

    _, recipient = generate_age_key()
    settings = write_example(tmp_path / 'etc', tmp_path / 'data', postgres_dsn='service=msgd')
    path = settings.config_dir / 'msgd.toml'
    if not path.exists():
        path = settings.config_dir / 'server.toml'
    path.write_text(
        path.read_text()
        + f'\n[recovery]\ncustodians = [{{id="offline-one", name="Offline", recipient="{recipient}", private_key="forbidden"}}]\n'
    )
    with pytest.raises(ValueError, match='unknown_recovery_custodian_field'):
        load_settings(settings.config_dir)


@pytest.mark.asyncio
async def test_configured_custodian_is_public_but_recipient_binding_is_checked(installed):
    from msg.config import RecoveryCustodian

    app, _ = installed
    key, owner, _ = await register(app, 'directory-owner')
    _, recipient = generate_age_key()
    fingerprint = encryption_key_id(public_from_recipient(recipient))
    app.settings = replace(
        app.settings,
        recovery_custodians=(
            RecoveryCustodian(
                id='offline-one',
                name='Offline Custodian',
                recipient=recipient,
                fingerprint=fingerprint,
                description='External key holder',
            ),
        ),
    )
    directory = await call(app, 'identity.recovery_custodians', {})
    assert directory.status == 'ok' and directory.data['items'][0]['fingerprint'] == fingerprint
    assert 'private' not in str(directory.data).lower()
    other = generate_age_key()[1]
    wrong = await call(
        app,
        'identity.recovery_policy_set',
        {
            'expected_version': 0,
            'encryption_key_id': await current_key(app, owner),
            'recipients': [{'recipient': other, 'custodian_ref': 'offline-one'}],
        },
        key=key,
        subject=owner,
    )
    assert wrong.status == 'error' and wrong.error.code == 'custodian_recipient_mismatch'
    accepted = await call(
        app,
        'identity.recovery_policy_set',
        {
            'expected_version': 0,
            'encryption_key_id': await current_key(app, owner),
            'recipients': [{'recipient': recipient, 'custodian_ref': 'offline-one'}],
        },
        key=key,
        subject=owner,
    )
    assert accepted.status == 'ok'
