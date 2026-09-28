"""Issue #68 slice: a physical-console, root-signed backup retirement record.

Without that exact record backup_retired stays false; server_key_retired also
needs observable online retirement. Altered, expired, misbound, wrong-signer and
superseded-root records fail closed. No network path can write or supply one.
"""
from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path

import pytest

from msg.admin.backup_retirement import import_record
from msg.admin.root import RootAdmin
from msg.constants import ROOT_SPACE, ROOT_SUBJECT
from msg.core.codec import b64, canonical, loads, unb64, wire
from msg.core.errors import Failure
from msg.core.models import (Certificate, CapabilityGrant, Credential, IssuancePolicy, Scope,
                             Signature)
from msg.security.age_keys import generate_age_key, public_from_recipient
from msg.security.backup_retirement import (BackupRetirement, check, setting_key, sign_record,
                                            statement)
from msg.security.certificates import sign_certificate
from msg.security.crypto import Ed25519Signer
from msg.security.custodial_migration import retirement_view
from msg.security.custody_history import retirement_evidence
from msg.storage.sqlite import FakeMetadataStore
from test_custodial_upgrade import begin, finish
from test_service import NOW, call

SUBJECT = 'u_exit'
SETS = ['offsite-2026-09-weekly', 'pg-basebackup-2026-09-20']


def root_certificate(root):
    grant = CapabilityGrant(capability='cert.issue', version=1,
        scope=Scope(resource_id=ROOT_SPACE, descendants=True),
        operations=frozenset({'cert.publish@1'}), constraints={})
    return sign_certificate(Certificate(resource_id='cert_root', serial='cert_root',
        subject_id=ROOT_SUBJECT, key_id=root.key_id, issuer_id=ROOT_SUBJECT,
        parent_certificate_id=None, authority_sources=(), kind='ca', grants=(grant,),
        not_before=NOW - timedelta(days=1), expires_at=NOW + timedelta(days=365),
        target_service='http://testserver', delegation_depth=8,
        issuance=IssuancePolicy(issue_grants=(grant,), max_cert_ttl_seconds=86400,
            max_child_ca_depth=3, max_delegation_depth=8),
        signature=Signature(key_id=root.key_id, algorithm='ed25519', value=b'')), root)


def credential(signer, subject, *, revoked=None):
    return Credential(id=signer.key_id, subject_id=subject, kind='signing_key',
        verifier=signer.public_key, ceiling=(), not_before=NOW - timedelta(days=1),
        expires_at=None, revoked_at=revoked)


@pytest.fixture
async def exited():
    """An online-retired custodial exit plus an active root, all in a disposable store."""
    store = FakeMetadataStore()
    root, old, new = (Ed25519Signer.generate() for _ in range(3))
    details = {'old_identity_key_id': old.key_id, 'new_identity_key_id': new.key_id,
               'old_encryption_key_id': 'e_old', 'new_encryption_key_id': 'e_new'}
    async with store.transaction(write=True) as tx:
        for key, primary in ((old, 0), (new, 1)):
            tx.execute('INSERT INTO identity_keys VALUES(?,?,?,?,?,?)',
                (key.key_id, SUBJECT, b64(key.public_key), wire(NOW),
                 None if primary else wire(NOW), primary), write=True)
        for kid, primary in (('e_old', 0), ('e_new', 1)):
            _, recipient = generate_age_key()
            tx.execute('INSERT INTO encryption_subkeys VALUES(?,?,?,?,?,?,?)',
                (kid, SUBJECT, recipient, b64(public_from_recipient(recipient)), wire(NOW),
                 None if primary else wire(NOW), primary), write=True)
        for signer, subject, revoked in ((old, SUBJECT, NOW), (root, ROOT_SUBJECT, None)):
            tx.execute('INSERT INTO credentials VALUES(?,?,?)', (signer.key_id, subject,
                canonical(credential(signer, subject, revoked=revoked)).decode()), write=True)
        tx.execute('''INSERT INTO custodial_vault(subject,signing_key_id,encryption_key_id,
            status,created_at,destroyed_at) VALUES(?,?,?,?,?,?)''',
            (SUBJECT, old.key_id, 'e_old', 'destroyed', wire(NOW), wire(NOW)), write=True)
        tx.execute('''INSERT INTO custodial_upgrades(id,subject,credential_id,status,expires_at,
            challenge,body) VALUES(?,?,?,?,?,?,?)''', ('cupg_exit', SUBJECT, 't_old', 'completed',
            wire(NOW), '{}', canonical(details).decode()), write=True)
        await tx.register_certificate(root_certificate(root), None, 0)
        tx.set_setting('active_root_certificate', 'cert_root')
    yield store, root, details
    await store.close()


def signed(details, signer, *, subject=SUBJECT, attested=NOW - timedelta(hours=1),
           not_after=NOW + timedelta(days=30), sets=SETS, **changes):
    body = statement(subject, dict(details, **changes), sets, attested_at=attested,
                     not_after=not_after, operator='console-operator')
    return sign_record(body, signer)


async def evidence(store, details, *, now=NOW):
    async with store.transaction(write=False) as tx:
        return await retirement_evidence(tx, SUBJECT, details, history_recoverable=True, now=now)


async def stored(store, details):
    async with store.transaction(write=False) as tx:
        return tx.setting(setting_key(SUBJECT, details['old_identity_key_id']))


@pytest.mark.asyncio
async def test_without_record_backup_and_server_key_remain_false(exited):
    store, _, details = exited
    async with store.transaction(write=True) as tx:
        tx.set_setting('backup_retired', True)  # An arbitrary flag is not evidence.
    result = await evidence(store, details)
    assert result['online_retired'] and not result['backup_retired']
    assert result['server_key_retired'] is False
    assert result['backup_retirement_evidence'] == 'unavailable'
    assert result['completion_status'] == 'pending_backup_retirement'


@pytest.mark.asyncio
async def test_imported_root_signed_record_retires_exactly_that_backup_scope(exited):
    store, root, details = exited
    async with store.transaction(write=True) as tx:
        attestation = await import_record(tx, signed(details, root), now=NOW, operator='uid:0:/dev/tty1')
    assert attestation.backup_sets == tuple(SETS)
    result = await evidence(store, details)
    assert result['backup_retired'] is True and result['server_key_retired'] is True
    assert result['completion_status'] == 'server_key_retired'
    assert result['backup_retirement_record']['scope'] == 'listed_backup_sets_only'
    assert result['backup_retirement_record']['backup_sets'] == SETS
    assert not (await evidence(store, details, now=None))['backup_retired']
    other = dict(details, old_identity_key_id=Ed25519Signer.generate().key_id)
    assert not (await evidence(store, other))['backup_retired']
    async with store.transaction(write=False) as tx:
        audits = [loads(body) for (body,) in tx.rows('SELECT body FROM audit')]
    assert [a['event']['type'] for a in audits] == ['root.custodial.backup_retirement']
    assert audits[0]['event']['data']['operator'] == 'uid:0:/dev/tty1'


@pytest.mark.asyncio
async def test_backup_attestation_alone_is_not_server_key_retirement(exited):
    store, root, details = exited
    async with store.transaction(write=True) as tx:
        await import_record(tx, signed(details, root), now=NOW, operator='console')
        tx.execute('UPDATE custodial_vault SET age_ciphertext=? WHERE subject=?',
                   ('surviving encrypted key', SUBJECT), write=True)
    result = await evidence(store, details)
    assert result['backup_retired'] is True and result['online_retired'] is False
    assert result['server_key_retired'] is False
    assert result['completion_status'] == 'pending_history'


def tampered(record):
    body = dict(record['statement'], backup_sets=['a-different-set'])
    return dict(record, statement=body)


@pytest.mark.asyncio
@pytest.mark.parametrize('make, code', [
    (lambda d, root: signed(d, Ed25519Signer.generate()), 'backup_retirement_invalid'),
    (lambda d, root: tampered(signed(d, root)), 'backup_retirement_invalid'),
    (lambda d, root: signed(d, root, not_after=NOW), 'backup_retirement_expired'),
    (lambda d, root: signed(d, root, attested=NOW + timedelta(minutes=1),
                            not_after=NOW + timedelta(days=1)), 'backup_retirement_expired'),
    (lambda d, root: signed(d, root, not_after=NOW + timedelta(days=500)), 'backup_retirement_invalid'),
    (lambda d, root: signed(d, root, sets=[]), 'backup_retirement_invalid'),
    (lambda d, root: signed(d, root, old_encryption_key_id='e_new'), 'backup_retirement_binding_mismatch'),
    (lambda d, root: signed(d, root, subject='u_other'), 'backup_retirement_migration_not_found'),
    (lambda d, root: dict(signed(d, root), backup_retired=True), 'backup_retirement_invalid'),
    (lambda d, root: dict(signed(d, root), statement=dict(signed(d, root)['statement'], extra=1)),
     'backup_retirement_invalid'),
    (lambda d, root: True, 'backup_retirement_invalid'),
])
async def test_bad_records_are_rejected_at_import_and_never_stored(exited, make, code):
    store, root, details = exited
    with pytest.raises(Failure, match='^' + code + '$'):
        async with store.transaction(write=True) as tx:
            await import_record(tx, make(details, root), now=NOW, operator='console')
    assert await stored(store, details) is None
    assert not (await evidence(store, details))['backup_retired']


@pytest.mark.asyncio
async def test_persisted_record_is_reverified_and_fails_closed(exited):
    store, root, details = exited
    good = signed(details, root)
    async with store.transaction(write=True) as tx:
        await import_record(tx, good, now=NOW, operator='console')
    later = await evidence(store, details, now=NOW + timedelta(days=30))
    assert not later['backup_retired'] and later['backup_retirement_evidence'] == 'expired'
    for bad in (tampered(good), signed(details, Ed25519Signer.generate()),
                signed(details, root, old_encryption_key_id='e_new')):
        async with store.transaction(write=True) as tx:
            tx.set_setting(setting_key(SUBJECT, details['old_identity_key_id']), bad)
        result = await evidence(store, details)
        assert not result['backup_retired'] and not result['server_key_retired']
        assert result['backup_retirement_evidence'] == 'invalid'
    async with store.transaction(write=True) as tx:
        tx.set_setting(setting_key(SUBJECT, details['old_identity_key_id']), good)
    assert (await evidence(store, details))['backup_retired']
    async with store.transaction(write=True) as tx:
        root_credential = await tx.credential(root.key_id)
        tx.execute('UPDATE credentials SET body=? WHERE id=?', (canonical(
            replace(root_credential, revoked_at=NOW)).decode(), root.key_id), write=True)
    rotated = await evidence(store, details)
    assert not rotated['backup_retired'] and rotated['backup_retirement_evidence'] == 'invalid'


def test_retirement_view_ignores_network_body_and_caller_flags():
    root = Ed25519Signer.generate()
    details = {'old_identity_key_id': 'k_old', 'old_encryption_key_id': 'e_old'}
    record = signed(details, root)
    attestation = check(record, root.public_key, SUBJECT, details, now=NOW)
    destroyed = ('destroyed', None, None, None, None)
    network = dict(details, backup_retirement=record)
    for backup in (None, True, 'verified', {'backup_retired': True}, record):
        view = retirement_view(network, destroyed, identity_switched=True, subject_id=SUBJECT,
                               backup=backup)
        assert view['backup_retired'] is False and view['server_key_retired'] is False
        assert view['backup_retirement_record'] is None
    assert not retirement_view(details, destroyed, identity_switched=True, subject_id='u_other',
                               backup=attestation)['backup_retired']
    forged = BackupRetirement(**{**attestation.__dict__, 'old_encryption_key_id': 'e_other'})
    assert not retirement_view(details, destroyed, identity_switched=True, subject_id=SUBJECT,
                               backup=forged)['backup_retired']
    view = retirement_view(details, destroyed, identity_switched=True, subject_id=SUBJECT,
                           backup=attestation)
    assert view['backup_retired'] and view['server_key_retired']
    assert view['backup_status'] == 'local_attestation_verified'
    retained = retirement_view(details, ('decrypt_only', None, None, 'n', 'c'),
        identity_switched=True, subject_id=SUBJECT, backup=attestation)
    assert retained['backup_retired'] and not retained['server_key_retired']


@pytest.mark.parametrize('euid, env, code', [
    (1000, {}, 'local_os_administrator_required'),
    (0, {'SSH_CONNECTION': '203.0.113.1 5 198.51.100.2 22'}, 'remote_admin_forbidden'),
])
def test_import_and_signing_require_the_physical_console(tmp_path, monkeypatch, euid, env, code):
    import msg.admin.root as root_module
    monkeypatch.setattr(root_module.os, 'geteuid', lambda: euid, raising=False)
    for name in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY'):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    source = tmp_path/'record.json'
    source.write_bytes(b'{}')
    admin = RootAdmin(tmp_path)
    with pytest.raises(Failure, match='^' + code + '$'):
        admin.import_backup_retirement(source)
    with pytest.raises(Failure, match='^' + code + '$'):
        admin.sign_backup_retirement(source, tmp_path/'signed.json')
    assert not (tmp_path/'signed.json').exists()


def test_no_network_adapter_can_write_or_import_the_record():
    src = Path(__file__).resolve().parents[1]/'src'/'msg'
    allowed = {src/'admin'/'backup_retirement.py', src/'admin'/'root.py',
               src/'security'/'backup_retirement.py'}
    for path in src.rglob('*.py'):
        if path in allowed:
            continue
        text = path.read_text()
        assert 'admin.backup_retirement' not in text, path
        assert 'import_record' not in text, path
        assert 'custodial_backup_retirement:' not in text, path
        assert 'setting_key' not in text or 'backup_retirement' not in text, path


@pytest.mark.asyncio
async def test_network_inventory_reports_backup_retired_only_after_local_import(installed):
    app, root = installed
    created = await call(app, 'identity.custodial_create', {'handle': 'backup-exit',
        'nonce': b64(os.urandom(32)), 'recovery_secret': b64(os.urandom(32))}, contract_version=2)
    subject = created.data['subject_id']
    token = (created.data['credential_id'], unb64(created.data['token']))
    signer = Ed25519Signer.generate()
    age_key, recipient = generate_age_key()
    challenge = await begin(app, subject, token, signer, recipient)
    done = await finish(app, subject, token, signer, age_key, challenge.data)
    assert done.status == 'ok' and done.data['status'] == 'completed'
    args = {'challenge_id': challenge.data['challenge_id']}

    async def inventory(extra=None):
        return await call(app, 'identity.custodial_upgrade_inventory', dict(args, **(extra or {})),
                          key=signer, subject=subject)

    before = await inventory()
    assert before.status == 'ok', wire(before)
    assert before.data['phase'] == 'online_key_retired'
    assert before.data['retirement']['backup_retired'] is False
    assert before.data['retirement']['server_key_retired'] is False
    async with app.metadata.transaction(write=False) as tx:
        details = loads(tx.one('SELECT body FROM custodial_upgrades WHERE id=?',
                               (args['challenge_id'],))[0])
    supplied = await inventory({'backup_retirement': signed(details, root, subject=subject)})
    assert supplied.status == 'error'
    with pytest.raises(Failure, match='^backup_retirement_invalid$'):
        async with app.metadata.transaction(write=True) as tx:
            await import_record(tx, signed(details, Ed25519Signer.generate(), subject=subject),
                                now=NOW, operator='console')
    assert (await inventory()).data['retirement']['backup_retired'] is False
    record = signed(details, root, subject=subject)
    async with app.metadata.transaction(write=True) as tx:
        await import_record(tx, record, now=NOW, operator='console')
    after = await inventory()
    assert after.status == 'ok', wire(after)
    assert after.data['retirement']['backup_retired'] is True
    assert after.data['retirement']['server_key_retired'] is True
    assert list(after.data['retirement']['backup_retirement_record']['backup_sets']) == SETS
    assert after.data['phase'] == 'server_key_retired'
    async with app.metadata.transaction(write=False) as tx:
        result = await retirement_evidence(tx, subject, details, history_recoverable=True, now=NOW)
    assert result['online_retired'] and result['backup_retired'] and result['server_key_retired']
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting(setting_key(subject, details['old_identity_key_id']), tampered(record))
    damaged = await inventory()
    assert damaged.status == 'ok', wire(damaged)
    assert damaged.data['retirement']['backup_retired'] is False
    assert damaged.data['retirement']['backup_status'] == 'local_attestation_invalid'
    assert damaged.data['phase'] == 'online_key_retired'
