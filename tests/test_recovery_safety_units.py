"""Cryptographic and transaction-boundary units; PostgreSQL drills are separate."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess

import pytest

from msg.constants import ROOT_SUBJECT, ROOT_SPACE, ONLINE_CA
from msg.core.codec import b64, canonical, digest, wire
from msg.core.errors import Failure
from msg.core.models import (Certificate, CertificateRequest, CapabilityGrant, Credential,
                             IssuancePolicy, Scope, Signature)
from msg.security.age_keys import generate_age_key, encryption_key_id, public_from_recipient
from msg.security.certificates import sign_certificate, csr_body
from msg.security.crypto import Ed25519Signer, seal_private_key
from msg.security.custody_history import history_evidence, retirement_evidence
from msg.security.quarantine import SETTING, active, require_live_authority
from msg.security.rotation_journal import authorization, validate
from msg.storage.sqlite import FakeMetadataStore

NOW = datetime(2026, 9, 27, tzinfo=UTC)
PIN = 'isolated-rotation-unit-passphrase'


@pytest.fixture(scope='module')
def rotation_bundle():
    old, new, online = (Ed25519Signer.generate() for _ in range(3))
    grant = CapabilityGrant(capability='cert.issue', version=1,
        scope=Scope(resource_id=ROOT_SPACE, descendants=True),
        operations=frozenset({'cert.publish@1'}), constraints={})
    policy = IssuancePolicy(issue_grants=(grant,), max_cert_ttl_seconds=86400,
        max_child_ca_depth=3, max_delegation_depth=8)
    def certificate(signer, cid):
        return sign_certificate(Certificate(resource_id=cid, serial=cid, subject_id=ROOT_SUBJECT,
            key_id=signer.key_id, issuer_id=ROOT_SUBJECT, parent_certificate_id=None,
            authority_sources=(), kind='ca', grants=(grant,), not_before=NOW,
            expires_at=NOW+timedelta(days=2), target_service='http://testserver',
            delegation_depth=8, issuance=policy,
            signature=Signature(key_id=signer.key_id, algorithm='ed25519', value=b'')), signer)
    old_cert, new_cert = certificate(old, 'cert_old'), certificate(new, 'cert_new')
    csr = CertificateRequest(resource_id='csr_online', applicant=ONLINE_CA, subject_id=ONLINE_CA,
        requested_issuer=ROOT_SUBJECT, public_key=online.public_key, kind='ca', grants=(grant,),
        issuance=replace(policy, max_child_ca_depth=0), requested_ttl_seconds=86400,
        target_service='http://testserver', delegation_depth=0, authority_sources=(),
        request_digest='', possession_proof=Signature(key_id=online.key_id, algorithm='ed25519', value=b''))
    csr = replace(csr, request_digest=digest(csr_body(csr)),
        possession_proof=online.sign(canonical(csr_body(csr)), purpose='csr'))
    journal = {'version':2, 'old_certificate':wire(old_cert),
        'new_trust':{'version':1, 'public_key':b64(new.public_key), 'certificate':wire(new_cert)},
        'new_envelope':seal_private_key(new.private_bytes(), PIN),
        'previous_envelope_digest':None, 'online_csr':wire(csr),
        'statement':{'old_certificate':old_cert.resource_id, 'old_fingerprint':digest(old.public_key),
            'new_certificate':new_cert.resource_id, 'new_fingerprint':digest(new.public_key),
            'time':wire(NOW), 'operator':'isolated-unit', 'previous_key_proved':True}}
    proof = canonical(authorization(journal))
    journal.update(new_signature=wire(new.sign(proof, purpose='root-rotation')),
                   old_signature=wire(old.sign(proof, purpose='root-rotation')))
    return journal, old, new, online


def test_rotation_commitment_binds_all_installation_targets(rotation_bundle):
    journal, _, new, online = rotation_bundle
    signer, old_cert, new_cert, csr, proof = validate(journal, pin=PIN,
        service='http://testserver', online_public=online.public_key)
    assert signer.public_key == new.public_key and old_cert.resource_id != new_cert.resource_id
    assert csr.public_key == online.public_key and proof == authorization(journal)
    assert 'new_envelope' not in proof and new.private_bytes() not in canonical(journal)


@pytest.mark.parametrize('mutation', [
    lambda j: j['online_csr'].update(resource_id='csr_substituted'),
    lambda j: j['new_trust']['certificate'].update(serial='substituted'),
    lambda j: j['old_certificate'].update(serial='substituted'),
    lambda j: j.update(previous_envelope_digest=digest(b'substituted')),
    lambda j: j['statement'].update(previous_key_proved=False),
    lambda j: j['statement'].update(new_fingerprint=digest(b'substituted')),
    lambda j: j.update(old_signature=None),
    lambda j: j.update(unexpected_field='not committed'),
])
def test_rotation_rejects_journal_substitution(rotation_bundle, mutation):
    journal, _, _, online = rotation_bundle
    changed = deepcopy(journal)
    mutation(changed)
    with pytest.raises(Failure):
        validate(changed, pin=PIN, service='http://testserver', online_public=online.public_key)


def test_legacy_unbound_rotation_is_not_silently_resumed(rotation_bundle):
    journal, _, _, online = rotation_bundle
    with pytest.raises(Failure, match='rotation_journal_upgrade_required'):
        validate({**journal, 'version':1}, pin=PIN, service='http://testserver',
                 online_public=online.public_key)


def test_rotation_requires_same_online_key_and_service(rotation_bundle):
    journal, _, _, online = rotation_bundle
    with pytest.raises(Failure, match='rotation_online_csr_mismatch'):
        validate(journal, pin=PIN, service='http://testserver',
                 online_public=Ed25519Signer.generate().public_key)
    with pytest.raises(Failure, match='rotation_service_mismatch'):
        validate(journal, pin=PIN, service='http://another-service', online_public=online.public_key)


@pytest.fixture
def history_bundle():
    signer = Ed25519Signer.generate()
    _, recipient = generate_age_key()
    challenge = {'subject_id':'u_history', 'challenge_id':'cupg_history',
                 'public_key':b64(signer.public_key), 'encryption_recipient':recipient}
    old = {'id':'r_old', 'revision':'v_old', 'ciphertext_digest':digest(b'old ciphertext')}
    new = {'id':'r_new', 'revision':'v_new', 'ciphertext_digest':digest(b'new ciphertext')}
    details = {'old_identity_key_id':'k_old', 'new_identity_key_id':signer.key_id,
               'old_encryption_key_id':'e_old',
               'new_encryption_key_id':encryption_key_id(public_from_recipient(recipient)),
               'age_inventory':[old], 'rewrap_mappings':{'v_old':{
                   'old':old, 'new':new, 'recipient':recipient}}, 'rewrap_acks':{}}
    signed = {'subject_id':'u_history', 'challenge_id':'cupg_history', 'old':old, 'new':new,
              'plaintext_digest':digest(b'client declares decryption'), 'request_id':'ack-one'}
    details['rewrap_acks']['v_old'] = {'old_revision':'v_old', 'new_revision':'v_new',
        'ciphertext_digest':new['ciphertext_digest'], 'plaintext_digest':signed['plaintext_digest'],
        'signature':wire(signer.sign(canonical(signed), purpose='custodial-rewrap-ack')),
        'request_id':signed['request_id']}
    return challenge, details, sorted([old, new], key=lambda x:(x['id'], x['revision']))


def evidence(bundle):
    return history_evidence('u_history', 'cupg_history', *bundle)


def test_ack_is_reverified_not_just_counted(history_bundle):
    result = evidence(history_bundle)
    assert result['known_ciphertexts_migrated'] and result['verified_acked_count'] == 1
    assert result['verification_scope'] == 'enumerated_keystore_age_revisions'
    assert result['external_coverage'] == 'unknown'


@pytest.mark.parametrize('mutation', [
    lambda d: d['rewrap_acks']['v_old'].update(plaintext_digest=digest(b'changed')),
    lambda d: d['rewrap_acks']['v_old'].update(request_id='another-request'),
    lambda d: d['rewrap_acks']['v_old'].update(old_revision='another-source'),
    lambda d: d['rewrap_acks']['v_old'].update(signature={}),
    lambda d: d['rewrap_mappings']['v_old'].update(old_encryption_key_id='other-key'),
    lambda d: d['rewrap_mappings']['v_old'].update(new_encryption_key_id='other-key'),
    lambda d: d['rewrap_mappings']['v_old'].update(recipient=generate_age_key()[1]),
    lambda d: d.update(new_identity_key_id='other-signer'),
    lambda d: d.update(new_encryption_key_id='other-key'),
    lambda d: d.update(age_inventory=None),
    lambda d: d.update(rewrap_acks=[]),
    lambda d: d.update(rewrap_mappings=[]),
])
def test_bad_history_evidence_remains_pending(history_bundle, mutation):
    changed = deepcopy(history_bundle)
    mutation(changed[1])
    result = evidence(changed)
    assert not result['known_ciphertexts_migrated']
    assert result['historical_revisions_require_migration']


def test_inventory_delta_invalidates_previously_verified_acks(history_bundle):
    challenge, details, current = history_bundle
    current = sorted(current+[{'id':'r_late', 'revision':'v_late',
                              'ciphertext_digest':digest(b'late ciphertext')}],
                     key=lambda x:(x['id'], x['revision']))
    result = evidence((challenge, details, current))
    assert result['inventory_changed'] and result['historical_revisions_require_migration']
    assert not result['known_ciphertexts_migrated'] and result['verified_acked_count'] == 1


def test_ack_cannot_cross_subject_or_challenge(history_bundle):
    assert not history_evidence('u_other', 'cupg_history', *history_bundle)['known_ciphertexts_migrated']
    assert not history_evidence('u_history', 'cupg_other', *history_bundle)['known_ciphertexts_migrated']


@pytest.mark.asyncio
@pytest.mark.parametrize('stored', [False, {}, [], 'unknown', None])
async def test_persisted_quarantine_fails_closed_for_malformed_flags(stored):
    store = FakeMetadataStore()
    try:
        async with store.transaction(write=True) as tx:
            assert not active(tx)
            tx.set_setting(SETTING, stored)
            assert active(tx)
            with pytest.raises(Failure, match='recovery_quarantined'):
                require_live_authority(tx)
        async with store.transaction(write=False) as tx:
            assert active(tx)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_online_vault_destruction_is_not_backup_retirement():
    store = FakeMetadataStore()
    old, new = Ed25519Signer.generate(), Ed25519Signer.generate()
    details = {'old_identity_key_id':old.key_id, 'new_identity_key_id':new.key_id,
               'old_encryption_key_id':'e_old', 'new_encryption_key_id':'e_new'}
    try:
        async with store.transaction(write=True) as tx:
            for key, primary in ((old, 0), (new, 1)):
                tx.execute('INSERT INTO identity_keys VALUES(?,?,?,?,?,?)',
                    (key.key_id, 'u_exit', b64(key.public_key), wire(NOW),
                     None if primary else wire(NOW), primary), write=True)
            for kid, primary in (('e_old', 0), ('e_new', 1)):
                _, recipient = generate_age_key()
                tx.execute('INSERT INTO encryption_subkeys VALUES(?,?,?,?,?,?,?)',
                    (kid, 'u_exit', recipient, b64(public_from_recipient(recipient)), wire(NOW),
                     None if primary else wire(NOW), primary), write=True)
            cred = Credential(id=old.key_id, subject_id='u_exit', kind='signing_key',
                verifier=old.public_key, ceiling=(), not_before=NOW, expires_at=None, revoked_at=NOW)
            tx.execute('INSERT INTO credentials VALUES(?,?,?)',
                       (old.key_id, 'u_exit', canonical(cred).decode()), write=True)
            tx.execute('''INSERT INTO custodial_vault(subject,signing_key_id,encryption_key_id,
                status,created_at,destroyed_at) VALUES(?,?,?,?,?,?)''',
                ('u_exit', old.key_id, 'e_old', 'destroyed', wire(NOW), wire(NOW)), write=True)
            tx.set_setting('backup_retired', True)  # An arbitrary flag is not evidence.
        async with store.transaction(write=False) as tx:
            result = await retirement_evidence(tx, 'u_exit', details, history_recoverable=True)
            assert result['identity_switched'] and result['online_retired']
            assert not result['backup_retired'] and not result['server_key_retired']
            assert result['completion_status'] == 'pending_backup_retirement'
        async with store.transaction(write=True) as tx:
            tx.execute('UPDATE custodial_vault SET age_ciphertext=? WHERE subject=?',
                       (b'surviving encrypted key', 'u_exit'), write=True)
        async with store.transaction(write=False) as tx:
            assert not (await retirement_evidence(tx, 'u_exit', details,
                history_recoverable=True))['online_retired']
    finally:
        await store.close()


def test_restore_quarantine_is_inside_single_transaction(tmp_path, monkeypatch):
    from msg.admin import restore_database
    calls = []
    dump = tmp_path/'metadata.dump'
    dump.write_bytes(b'offline dump')
    gate = {'format':'msg-recovery-quarantine-v1', 'source_backup_sha256':'a'*64}
    def command(argv, **kw):
        calls.append(argv)
        script = Path(argv[argv.index('--file')+1])
        if argv[0] == 'pg_restore':
            assert '--dbname' not in argv
            script.write_bytes(b'CREATE TABLE settings(key text PRIMARY KEY,value text);\n')
        else:
            assert '--single-transaction' in argv and '--no-psqlrc' in argv
            assert 'ON_ERROR_STOP=1' in argv and kw['check'] is True
            text = script.read_text()
            assert text.index('CREATE TABLE') < text.index("VALUES('recovery_quarantine'")
            assert canonical(gate).decode() in text
            assert kw['env']['PGPASSWORD'] == 'not-in-command-line'
            assert 'not-in-command-line' not in ' '.join(argv)
            assert script.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(restore_database.subprocess, 'run', command)
    restore_database.restore_dump(dump, safe_dsn='service=test',
                                 env={'PGPASSWORD':'not-in-command-line'}, quarantine=gate)
    assert [call[0] for call in calls] == ['pg_restore', 'psql']
    assert not (tmp_path/'quarantined-restore.sql').exists()


def test_failed_dump_conversion_never_connects_to_restore_database(tmp_path, monkeypatch):
    from msg.admin import restore_database
    calls = []
    def fail(argv, **kw):
        calls.append(argv[0])
        raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(restore_database.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        restore_database.restore_dump(tmp_path/'metadata.dump', safe_dsn='service=test',
                                     env={}, quarantine={})
    assert calls == ['pg_restore']
    assert not (tmp_path/'quarantined-restore.sql').exists()
