"""Real PostgreSQL regressions for recovery replay and current Root authority."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_backup_retirement_attestation import SUBJECT, credential, root_certificate, signed
from test_service import NOW

from msg.admin.backup_retirement import import_record
from msg.admin.recovery_replay import FORMAT, TrustedCheckpointPin, _replay
from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, digest, wire
from msg.core.errors import Failure
from msg.core.models import Resource
from msg.security.backup_retirement import check, setting_key, verified
from msg.security.certificates import sign_certificate
from msg.security.crypto import Ed25519Signer
from msg.security.quarantine import SETTING, active
from msg.storage.postgres import PostgresMetadataStore


@pytest.fixture
async def retirement_store(pg_dsn):
    store = PostgresMetadataStore(pg_dsn)
    root = Ed25519Signer.generate()
    details = {'old_identity_key_id': 'k_old', 'old_encryption_key_id': 'e_old'}
    record = signed(details, root)
    async with store.transaction(write=True) as tx:
        await tx.register_certificate(root_certificate(root), None, 0)
        tx.execute(
            'INSERT INTO credentials VALUES(?,?,?)',
            (root.key_id, ROOT_SUBJECT, canonical(credential(root, ROOT_SUBJECT)).decode()),
            write=True,
        )
        tx.execute(
            """INSERT INTO custodial_upgrades(id,subject,credential_id,status,expires_at,
            challenge,body) VALUES(?,?,?,?,?,?,?)""",
            (
                'cupg_current',
                SUBJECT,
                't_old',
                'completed',
                wire(NOW),
                '{}',
                canonical(details).decode(),
            ),
            write=True,
        )
        tx.set_setting('active_root_certificate', 'cert_root')
        tx.set_setting(setting_key(SUBJECT, details['old_identity_key_id']), record)
    try:
        yield store, root, details, record
    finally:
        await store.close()


@pytest.mark.parametrize(
    'change',
    [
        'certificate_expired',
        'certificate_boundary',
        'certificate_future',
        'credential_expired',
        'credential_boundary',
        'credential_future',
        'leaf',
        'parent',
        'issuer',
        'tampered',
    ],
)
async def test_retirement_rechecks_current_root_and_import_rejects(retirement_store, change):
    store, root, details, record = retirement_store
    async with store.transaction(write=True) as tx:
        cert = await tx.certificate('cert_root')
        key = await tx.credential(root.key_id)
        if change.startswith('credential_'):
            values = {
                'credential_expired': {'expires_at': NOW - timedelta(seconds=1)},
                'credential_boundary': {'expires_at': NOW},
                'credential_future': {'not_before': NOW + timedelta(seconds=1)},
            }
            key = replace(key, **values[change])
            tx.execute(
                'UPDATE credentials SET body=? WHERE id=?',
                (canonical(key).decode(), key.id),
                write=True,
            )
        else:
            values = {
                'certificate_expired': {'expires_at': NOW - timedelta(seconds=1)},
                'certificate_boundary': {'expires_at': NOW},
                'certificate_future': {'not_before': NOW + timedelta(seconds=1)},
                'leaf': {'kind': 'leaf', 'issuance': None},
                'parent': {'parent_certificate_id': 'cert_other'},
                'issuer': {'issuer_id': 'u_other'},
                'tampered': {'delegation_depth': cert.delegation_depth - 1},
            }
            cert = replace(cert, **values[change])
            if change != 'tampered':
                cert = sign_certificate(cert, root)
            tx.execute(
                'UPDATE certificates SET body=? WHERE id=?',
                (canonical(cert).decode(), cert.resource_id),
                write=True,
            )
    async with store.transaction(write=False) as tx:
        before = tx.rows('SELECT key,value FROM settings ORDER BY key')
        assert await verified(tx, SUBJECT, details, now=NOW) == ('invalid', None)
        assert tx.rows('SELECT key,value FROM settings ORDER BY key') == before
        assert tx.one('SELECT count(*) FROM audit')[0] == 0
    with pytest.raises(Failure, match='^backup_retirement_root_unavailable$'):
        async with store.transaction(write=True) as tx:
            await import_record(tx, record, now=NOW, operator='isolated-test')
    async with store.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM audit')[0] == 0
        assert tx.setting(setting_key(SUBJECT, details['old_identity_key_id'])) == record


async def test_retirement_accepts_valid_root_and_stops_at_credential_expiry(retirement_store):
    store, root, details, _ = retirement_store
    async with store.transaction(write=True) as tx:
        key = replace(
            await tx.credential(root.key_id), not_before=NOW, expires_at=NOW + timedelta(seconds=1)
        )
        tx.execute(
            'UPDATE credentials SET body=? WHERE id=?',
            (canonical(key).decode(), key.id),
            write=True,
        )
    async with store.transaction(write=False) as tx:
        status, proof = await verified(tx, SUBJECT, details, now=NOW)
        assert status == 'verified' and proof.binds(SUBJECT, details)
        assert await verified(tx, SUBJECT, details, now=NOW + timedelta(seconds=1)) == (
            'invalid',
            None,
        )
        assert await verified(tx, SUBJECT, details, now=None) == ('clock_unavailable', None)


def test_far_future_retirement_is_rejected_without_datetime_overflow():
    root = Ed25519Signer.generate()
    details = {'old_identity_key_id': 'k_old', 'old_encryption_key_id': 'e_old'}
    record = signed(
        details,
        root,
        attested=datetime(9999, 1, 1, tzinfo=UTC),
        not_after=datetime(9999, 12, 31, tzinfo=UTC),
    )
    with pytest.raises(Failure, match='^backup_retirement_expired$'):
        check(record, root.public_key, SUBJECT, details, now=NOW)


@pytest.mark.parametrize(
    'status,offset',
    [
        (None, None),
        ('active', None),
        ('lifted', None),
        ('active', -1),
        ('lifted', -1),
        ('active', 60),
        ('lifted', 60),
    ],
)
async def test_checkpoint_ban_cannot_inherit_stale_expiry(pg_dsn, status, offset):
    store = PostgresMetadataStore(pg_dsn)
    signer = Ed25519Signer.generate()
    subject, target = 'u_replay', 't_replay'
    resource = Resource(
        id=target,
        type='topic',
        type_version=1,
        name='replay',
        parent=None,
        owner=subject,
        group='g_public',
        mode=0o700,
        generation=1,
        revision=None,
        state='active',
        created_at=NOW,
        created_by=subject,
        modified_at=NOW,
        modified_by=subject,
    )
    body = {
        'format': FORMAT,
        'service': 'http://testserver',
        'source_backup_sha256': 'a' * 64,
        'sequence': 1,
        'entries': [
            {
                'sequence': 1,
                'kind': 'topic_ban.apply',
                'subject': subject,
                'target': target,
                'at': wire(NOW),
            }
        ],
    }
    pin = TrustedCheckpointPin(
        service=body['service'], public_key=signer.public_key, digest=digest(body), sequence=1
    )
    packet = {
        'checkpoint': body,
        'signature': wire(signer.sign(canonical(body), purpose='recovery-checkpoint-v1')),
    }
    try:
        async with store.transaction(write=True) as tx:
            await tx.insert(resource)
            tx.set_setting(
                SETTING,
                {
                    'format': 'msg-recovery-quarantine-v1',
                    'source_backup_sha256': 'a' * 64,
                    'outbound_enabled': False,
                    'authority': 'health_only',
                    'revocation_replay': 'required',
                },
            )
            if status is not None:
                expires = None if offset is None else wire(NOW + timedelta(seconds=offset))
                tx.execute(
                    'INSERT INTO topic_bans VALUES(?,?,?,?,?,?,?)',
                    (target, subject, subject, wire(NOW), expires, 'original', status),
                    write=True,
                )
        results = await asyncio.gather(
            *(_replay(store, packet, pin=pin, operator='isolated-test') for _ in range(3))
        )
        assert sum(result['changed'] for result in results) == int(
            status != 'active' or offset is not None
        )
        assert all(
            result['promotion'] == 'blocked' and result['backup_retired'] is False
            for result in results
        )
        async with store.transaction(write=False) as tx:
            assert tx.one(
                'SELECT status,expires_at FROM topic_bans WHERE topic=? AND subject=?',
                (target, subject),
            ) == ('active', None)
            assert active(tx) and tx.one('SELECT count(*) FROM jobs')[0] == 0
            assert await tx.resource(target) == resource
            if status is not None:
                assert tx.one(
                    'SELECT actor,reason FROM topic_bans WHERE topic=? AND subject=?',
                    (target, subject),
                ) == (subject, 'original')
        assert (await _replay(store, packet, pin=pin, operator='isolated-test'))['changed'] == 0
    finally:
        await store.close()
