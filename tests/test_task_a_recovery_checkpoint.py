"""Independent checkpoint pins, not database flags, authorize deny-only replay."""
import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest
from test_service import NOW

from msg.admin.recovery_replay import TrustedCheckpointPin, _replay, verify_checkpoint
from msg.core.codec import canonical, digest, wire
from msg.core.errors import Failure
from msg.core.models import Credential
from msg.security.crypto import Ed25519Signer
from msg.security.quarantine import SETTING, active
from msg.storage.postgres import PostgresMetadataStore


async def replay(store, packet, *, pin):
    return await _replay(store, packet, pin=pin, operator="isolated-test-usecase")


@pytest.fixture
def checkpoint():
    signer = Ed25519Signer.generate()
    body = {'format': 'msg-revocation-checkpoint-v1', 'service': 'http://testserver',
            'source_backup_sha256': 'a' * 64, 'sequence': 1,
            'entries': [{'sequence': 1, 'kind': 'credential.revoke', 'subject': 'u_owner',
                         'target': 'token_one', 'at': wire(NOW)}]}
    pin = TrustedCheckpointPin(service=body['service'], public_key=signer.public_key,
                               digest=digest(body), sequence=1)
    def signed(value):
        return {'checkpoint': value,
                'signature': wire(signer.sign(canonical(value), purpose='recovery-checkpoint-v1'))}
    return body, pin, signed


@pytest.fixture
async def restored(pg_dsn):
    store = PostgresMetadataStore(pg_dsn)
    token = Credential(id='token_one', subject_id='u_owner', kind='token', verifier=b'x' * 32,
                       ceiling=(), not_before=NOW, expires_at=None, revoked_at=None)
    async with store.transaction(write=True) as tx:
        tx.set_setting(SETTING, {'format': 'msg-recovery-quarantine-v1',
                                'source_backup_sha256': 'a' * 64,
                                'outbound_enabled': False, 'authority': 'health_only',
                                'revocation_replay': 'required'})
        tx.execute('INSERT INTO credentials VALUES (?,?,?)',
                   (token.id, token.subject_id, canonical(token).decode()), write=True)
    yield store
    await store.close()


def test_checkpoint_requires_an_independently_supplied_exact_pin(checkpoint):
    body, pin, signed = checkpoint
    assert verify_checkpoint(signed(body), pin=pin) == body
    wrong = replace(pin, public_key=Ed25519Signer.generate().public_key)
    with pytest.raises(Failure, match='^recovery_checkpoint_invalid$'):
        verify_checkpoint(signed(body), pin=wrong)
    with pytest.raises(Failure, match='^recovery_checkpoint_pin_required$'):
        verify_checkpoint(signed(body), pin=None)


@pytest.mark.parametrize('change', ['gap', 'rollback', 'unknown', 'extra', 'service', 'bool'])
def test_resigned_but_untrusted_or_malformed_checkpoint_denies(checkpoint, change):
    body, pin, signed = checkpoint
    bad = deepcopy(body)
    if change == 'gap':
        bad['entries'][0]['sequence'] = 2
    elif change == 'rollback':
        bad.update(sequence=0, entries=[])
    elif change == 'unknown':
        bad['entries'][0]['kind'] = 'credential.grant'
    elif change == 'extra':
        bad['entries'][0]['private_key'] = 'never accepted'
    elif change == 'service':
        bad['service'] = 'https://another.example'
    else:
        bad['sequence'] = True
    # Even an authentic older signature cannot replace an independently pinned head.
    with pytest.raises(Failure, match='^recovery_checkpoint_invalid$'):
        verify_checkpoint(signed(bad), pin=pin)
    if change in {'gap', 'unknown', 'extra', 'bool'}:
        with pytest.raises(Failure, match='^recovery_checkpoint_invalid$'):
            verify_checkpoint(signed(bad), pin=replace(pin, digest=digest(bad)))


@pytest.mark.asyncio
async def test_replay_is_atomic_monotonic_idempotent_and_never_promotes(restored, checkpoint):
    body, pin, signed = checkpoint
    results = await asyncio.gather(*(replay(restored, signed(body), pin=pin) for _ in range(3)))
    assert sum(result['changed'] for result in results) == 1
    assert all(result['promotion'] == 'blocked' and result['backup_retired'] is False for result in results)
    async with restored.transaction(write=False) as tx:
        assert active(tx)
        credential = await tx.credential('token_one')
        assert credential.revoked_at == NOW and credential.verifier == b'x' * 32
        assert tx.one('SELECT COUNT(*) FROM audit')[0] == 1
        assert tx.one('SELECT COUNT(*) FROM jobs')[0] == 0
        assert tx.setting('authorization_epoch') == 1
    restarted = PostgresMetadataStore(restored.dsn, initialize=False)
    try:
        assert (await replay(restarted, signed(body), pin=pin))['changed'] == 0
        async with restarted.transaction(write=False) as tx:
            assert active(tx)
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_missing_or_wrong_subject_fact_rolls_back_earlier_revocation(restored, checkpoint):
    body, pin, signed = checkpoint
    body = deepcopy(body)
    body['entries'].append({'sequence': 2, 'kind': 'credential.revoke',
                            'subject': 'u_owner', 'target': 'missing', 'at': wire(NOW)})
    body['sequence'] = 2
    pin = replace(pin, digest=digest(body), sequence=2)
    with pytest.raises(Failure, match='^recovery_fact_missing$'):
        await replay(restored, signed(body), pin=pin)
    async with restored.transaction(write=False) as tx:
        assert (await tx.credential('token_one')).revoked_at is None
        assert active(tx) and tx.setting('recovery_replay') is None
        assert tx.one('SELECT COUNT(*) FROM audit')[0] == 0
    body['entries'][1].update(target='token_one', subject='u_other')
    with pytest.raises(Failure, match='^recovery_fact_subject_mismatch$'):
        await replay(restored, signed(body), pin=replace(pin, digest=digest(body)))
    async with restored.transaction(write=False) as tx:
        assert (await tx.credential('token_one')).revoked_at is None


@pytest.mark.asyncio
async def test_wrong_backup_and_live_database_are_not_replay_targets(restored, checkpoint):
    body, pin, signed = checkpoint
    async with restored.transaction(write=True) as tx:
        gate = tx.setting(SETTING)
        tx.set_setting(SETTING, dict(gate, source_backup_sha256='b' * 64))
    with pytest.raises(Failure, match='^recovery_checkpoint_backup_mismatch$'):
        await replay(restored, signed(body), pin=pin)
    async with restored.transaction(write=True) as tx:
        tx.execute('DELETE FROM settings WHERE key=?', (SETTING,), write=True)
    with pytest.raises(Failure, match='^recovery_quarantine_required$'):
        await replay(restored, signed(body), pin=pin)


@pytest.mark.asyncio
async def test_cached_receipt_cannot_authorize_regression_or_skip_restored_authority(restored, checkpoint):
    body, pin, signed = checkpoint
    await replay(restored, signed(body), pin=pin)
    # A coherent current DB receipt is only a guard, never an external freshness source.
    old = dict(body, sequence=0, entries=[])
    with pytest.raises(Failure, match='^recovery_checkpoint_regression$'):
        await replay(restored, signed(old), pin=replace(pin, sequence=0, digest=digest(old)))
    corrupt = deepcopy(body)
    corrupt['entries'][0]['target'] = 'different-target'
    with pytest.raises(Failure, match='^recovery_checkpoint_regression$'):
        await replay(restored, signed(corrupt), pin=replace(pin, digest=digest(corrupt)))
    async with restored.transaction(write=True) as tx:
        token = await tx.credential('token_one')
        tx.execute('UPDATE credentials SET body=? WHERE id=?',
                   (canonical(replace(token, revoked_at=None)).decode(), token.id), write=True)
    # Re-applying the pinned full log re-revokes the row, even with an identical cached receipt.
    assert (await replay(restored, signed(body), pin=pin))['changed'] == 1
    async with restored.transaction(write=False) as tx:
        assert (await tx.credential('token_one')).revoked_at == NOW
        assert active(tx)


@pytest.mark.asyncio
async def test_actual_backend_termination_rolls_back_and_replay_can_resume(restored, checkpoint):
    import psycopg
    body, pin, signed = checkpoint
    # Kill the actual PostgreSQL session after its revoke but before its COMMIT.
    # No patched driver or mock transaction supplies the rollback evidence.
    with pytest.raises(psycopg.OperationalError):
        async with restored.transaction(write=True) as tx:
            assert (await replay(restored, signed(body), pin=pin))['changed'] == 1
            pid = tx.one('SELECT pg_backend_pid()')[0]
            with psycopg.connect(restored.dsn) as killer:
                assert killer.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone() == (True,)
            tx.one('SELECT 1')
    async with restored.transaction(write=False) as tx:
        assert (await tx.credential('token_one')).revoked_at is None
        assert tx.setting('recovery_replay') is None
        assert active(tx) and tx.one('SELECT count(*) FROM audit')[0] == 0
    assert (await replay(restored, signed(body), pin=pin))['changed'] == 1


@pytest.mark.asyncio
async def test_public_replay_entry_rejects_nonconsole_before_database_access(restored, checkpoint, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from msg.core.codec import loads
    script = '''
import asyncio
from pathlib import Path
from msg.admin.recovery_replay import replay
from msg.core.codec import canonical
from msg.core.errors import Failure
try:
    asyncio.run(replay(None, {}, pin=None, config_dir=Path('.')))
except Failure as exc:
    print(canonical({'code': exc.code}).decode())
'''
    # Capture pipes are not a physical console. On the non-root CI runner the
    # real OS-admin check must reject even earlier; do not mock uid or accept an
    # arbitrary error to disguise the order of these independent safety gates.
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY'}}
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    result = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', script],
        capture_output=True, text=True, check=True, env=environment)
    expected = 'local_console_required' if os.geteuid() == 0 else 'local_os_administrator_required'
    assert loads(result.stdout) == {'code': expected}
    async with restored.transaction(write=False) as tx:
        assert tx.setting('recovery_replay') is None
        assert tx.one('SELECT count(*) FROM audit')[0] == 0


def test_offline_selftest_is_not_field_or_database_acceptance():
    from msg.admin.recovery_replay import selftest
    assert selftest() == {'format': 'msg-revocation-checkpoint-v1',
                         'scope': 'isolated_signature_contract_only', 'checks': 3,
                         'status': 'passed', 'database_used': False,
                         'production_evidence': False, 'promotion': 'blocked'}


@pytest.mark.asyncio
async def test_all_supported_owned_revocations_preserve_signed_bytes_and_never_import_keys(restored, checkpoint):
    from datetime import timedelta

    from msg.admin.recovery_replay import SUPPORTED_FACTS
    from msg.core.codec import b64, decode, loads
    from msg.core.models import Certificate, Membership, Resource, Signature
    from msg.security.age_keys import encryption_key_id, generate_age_key, public_from_recipient
    from msg.security.certificates import sign_certificate
    from msg.security.crypto import key_id
    body, pin, signed = checkpoint
    owner = 'u_owner'
    signer = Ed25519Signer.generate()
    recipient = generate_age_key()[1]
    age_id = encryption_key_id(public_from_recipient(recipient))
    credential = Credential(id=signer.key_id, subject_id=owner, kind='signing_key',
        verifier=signer.public_key, ceiling=(), not_before=NOW, expires_at=None, revoked_at=None)
    certificate = sign_certificate(Certificate(resource_id='cert_one', serial='serial-one',
        subject_id=owner, key_id=key_id(signer.public_key), issuer_id=owner,
        parent_certificate_id=None, authority_sources=(), kind='capability', grants=(),
        not_before=NOW, expires_at=NOW+timedelta(days=1), target_service=pin.service,
        delegation_depth=0, issuance=None,
        signature=Signature(key_id=signer.key_id, algorithm='ed25519', value=b'')), signer)
    original = canonical(certificate).decode()
    member = Membership(organization_id='g_test', subject_id=owner, role='member', version=1)
    resource = Resource(id='t_test', type='topic', type_version=1, name='test', parent=None,
        owner=owner, group='g_public', mode=0o700, generation=1, revision=None, state='active',
        created_at=NOW, created_by=owner, modified_at=NOW, modified_by=owner)
    async with restored.transaction(write=True) as tx:
        await tx.insert(resource)
        tx.execute('INSERT INTO credentials VALUES (?,?,?)',
                   (credential.id, owner, canonical(credential).decode()), write=True)
        tx.execute('INSERT INTO certificates VALUES (?,?,NULL,0,?)',
                   (certificate.resource_id, owner, original), write=True)
        tx.execute('INSERT INTO memberships VALUES (?,?,?,?)',
                   (member.organization_id, owner, member.version, canonical(member).decode()), write=True)
        tx.execute('INSERT INTO topic_memberships VALUES (?,?,?,?,?,NULL)',
                   (resource.id, owner, 'member', 'active', wire(NOW)), write=True)
        tx.execute('INSERT INTO identity_keys VALUES (?,?,?,?,NULL,1)',
                   (signer.key_id, owner, b64(signer.public_key), wire(NOW)), write=True)
        tx.execute('INSERT INTO encryption_subkeys VALUES (?,?,?,?,?,NULL,1)',
                   (age_id, owner, recipient, b64(public_from_recipient(recipient)), wire(NOW)), write=True)
        tx.execute('INSERT INTO custodial_vault VALUES (?,?,?,?,?,?,?,?,?,NULL)',
                   (owner, signer.key_id, age_id, 'signing-nonce', 'sealed-signing',
                    'age-nonce', 'sealed-age', 'active', wire(NOW)), write=True)
        tx.execute('INSERT INTO token_deliveries VALUES (?,?,?,?,?,?,NULL,NULL)',
                   ('token_one', owner, 'original-create', 'digest', 'verifier',
                    wire(NOW+timedelta(hours=1))), write=True)
        tx.execute('INSERT INTO share_grants VALUES (?,?,?,?,?,?,NULL)',
                   ('sg_one', resource.id, owner, 'u_reader', wire(NOW),
                    wire(NOW+timedelta(days=1))), write=True)
        tx.execute('INSERT INTO share_grants_v2 VALUES (?,?,?,?,?,NULL,?,?,0,?,?,NULL)',
                   ('sg_two', resource.id, owner, 'u_reader', 'user', '["read"]', '{}',
                    wire(NOW), wire(NOW+timedelta(days=1))), write=True)
        tx.execute('INSERT INTO share_links VALUES (?,?,?,?,?,?,?,NULL)',
                   ('link_one', resource.id, owner, signer.key_id, 'link-verifier',
                    wire(NOW), wire(NOW+timedelta(days=1))), write=True)
        tx.execute('''INSERT INTO topic_bans (topic,subject,actor,created_at,expires_at,reason,status)
            VALUES (?,?,?,?,NULL,?,?)''',
            (resource.id, owner, owner, wire(NOW), 'restored-ban', 'active'), write=True)
    targets = [('credential.revoke', 'token_one'), ('certificate.revoke', 'cert_one'),
               ('share_grant.revoke', 'sg_one'), ('share_grant_v2.revoke', 'sg_two'),
               ('share_link.revoke', 'link_one'), ('membership.remove', 'g_test'),
               ('topic_membership.remove', 't_test'), ('topic_ban.lift', resource.id),
               ('identity_key.retire', signer.key_id),
               ('encryption_key.retire', age_id), ('vault.destroy', age_id)]
    assert {kind for kind, _ in targets} == SUPPORTED_FACTS
    facts = [{'sequence': i, 'kind': kind, 'subject': owner, 'target': target, 'at': wire(NOW)}
             for i, (kind, target) in enumerate(targets, 1)]
    body = dict(body, entries=facts, sequence=len(facts))
    pin = replace(pin, sequence=len(facts), digest=digest(body))
    result = await replay(restored, signed(body), pin=pin)
    assert result['changed'] == len(facts) and result['backup_retired'] is False
    async with restored.transaction(write=False) as tx:
        assert tx.one('SELECT body,revoked FROM certificates WHERE id=?', ('cert_one',)) == (original, 1)
        assert (await tx.credential(signer.key_id)).revoked_at == NOW
        assert tx.one('SELECT consumed_at FROM token_deliveries WHERE credential_id=?', ('token_one',)) == (wire(NOW),)
        for table in ('identity_keys', 'encryption_subkeys'):
            assert tx.one(f'SELECT retired_at,is_primary FROM {table} WHERE subject=?', (owner,)) == (wire(NOW), 0)
        for table, target in (('share_grants', 'sg_one'), ('share_grants_v2', 'sg_two'), ('share_links', 'link_one')):
            assert tx.one(f'SELECT revoked_at FROM {table} WHERE id=?', (target,)) == (wire(NOW),)
        current = decode(Membership, loads(tx.one('SELECT body FROM memberships WHERE org=? AND subject=?',
                                                ('g_test', owner))[0]))
        assert current.version == 2 and current.status == 'rejected'
        assert tx.one('SELECT status FROM topic_memberships WHERE topic=? AND subject=?',
                      ('t_test', owner)) == ('removed',)
        assert tx.one('SELECT status FROM topic_bans WHERE topic=? AND subject=?',
                      ('t_test', owner)) == ('lifted',)
        assert tx.one('SELECT status,signing_nonce,signing_ciphertext,age_nonce,age_ciphertext '
                      'FROM custodial_vault WHERE subject=?', (owner,)) == ('destroyed', None, None, None, None)
        assert tx.one('SELECT count(*) FROM jobs')[0] == 0 and active(tx)
        assert canonical(await tx.resource(resource.id)).decode() == canonical(resource).decode()
    assert (await replay(restored, signed(body), pin=pin))['changed'] == 0


def test_packaged_schema_and_example_match_the_replay_contract():
    from importlib.resources import files

    from jsonschema import Draft202012Validator

    from msg.admin.recovery_replay import SUPPORTED_FACTS
    from msg.core.codec import loads, unb64
    root = files('msg').joinpath('data')
    schema = loads(root.joinpath('recovery-checkpoint.schema.json').read_bytes())
    example = loads(root.joinpath('recovery-checkpoint.example.json').read_bytes())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(example['packet'])
    props = schema['properties']['checkpoint']['properties']
    assert props['entries']['default'] == [] and props['sequence']['default'] == 0
    assert set(props['entries']['items']['properties']['kind']['enum']) == SUPPORTED_FACTS
    pinned = dict(example['pin'], public_key=unb64(example['pin']['public_key']))
    pin = TrustedCheckpointPin(**pinned)
    assert verify_checkpoint(example['packet'], pin=pin) == example['packet']['checkpoint']
    assert example['example_only'] and 'private_key' not in canonical(example).decode()
    with pytest.raises(Failure, match='^recovery_checkpoint_pin_required$'):
        verify_checkpoint(example['packet'], pin=None)
    assert list(validator.iter_errors({}))
