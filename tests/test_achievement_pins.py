"""Display order is not certificate, capability or evidence authority."""

import pytest
from test_service import NOW, call, register

from msg.core.codec import canonical, digest, wire


async def _fixture_grants(app, subject):
    """Signed test fixtures, not automatic answers to a subject's declarations."""
    grants = []
    async with app.metadata.transaction(write=True) as tx:
        for number in range(2):
            grant = {
                'id': f'achg_fixture_{number}',
                'subject_id': subject,
                'achievement_id': f'fixture-{number}',
                'spec_version': 1,
                'issuer': app.receipt_signer.key_id,
                'issued_at': wire(NOW),
                'claim': 'Isolated test fixture.',
                'auth_method': 'signature',
                'evidence_digest': digest(f'private-evidence-{number}'),
                'automatic': False,
                'revoked_at': None,
                'metadata': {'protocol_passed': True},
            }
            grant['signature'] = wire(
                app.receipt_signer.sign(canonical(grant), purpose='achievement-grant')
            )
            tx.execute(
                'INSERT INTO achievement_grants VALUES (?,?,?,?,?)',
                (grant['id'], subject, grant['achievement_id'], 1, canonical(grant).decode()),
                write=True,
            )
            grants.append(grant)
    return grants


@pytest.mark.asyncio
async def test_pin_reorder_unpin_changes_only_display(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'pin-owner')
    grants = await _fixture_grants(app, subject)
    async with app.metadata.transaction(write=False) as tx:
        security_before = (
            tx.rows('SELECT body FROM certificates ORDER BY id'),
            tx.rows('SELECT body FROM credentials ORDER BY id'),
        )
    ids = [g['id'] for g in grants]
    for grant_id in ids:
        result = await call(
            app, 'achievement.pin', {'grant_id': grant_id}, key=key, subject=subject
        )
        assert result.status == 'ok', wire(result)
    reordered = await call(
        app, 'achievement.reorder', {'grant_ids': ids[::-1]}, key=key, subject=subject
    )
    assert reordered.status == 'ok', wire(reordered)
    public = await call(app, 'achievement.list', {'subject_id': subject})
    assert list(public.data['pinned_grant_ids']) == ids[::-1]
    assert 'private-evidence-' not in canonical(public.data).decode()
    removed = await call(app, 'achievement.unpin', {'grant_id': ids[0]}, key=key, subject=subject)
    assert removed.status == 'ok', wire(removed)
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.rows('SELECT body FROM certificates ORDER BY id'),
            tx.rows('SELECT body FROM credentials ORDER BY id'),
        ) == security_before
        assert [row[0] for row in tx.rows('SELECT body FROM achievement_grants ORDER BY id')] == [
            canonical(g).decode() for g in grants
        ]
    denied = await call(app, 'discovery.get', {'id': '/private'}, key=key, subject=subject)
    assert denied.status == 'error'


@pytest.mark.asyncio
async def test_foreign_revoked_or_incomplete_pin_requests_do_not_leak_or_mutate(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'pin-boundary')
    stranger_key, stranger, _ = await register(app, 'pin-stranger')
    grants = await _fixture_grants(app, owner)
    for grant_id in (grants[0]['id'], 'achg_absent'):
        result = await call(
            app, 'achievement.pin', {'grant_id': grant_id}, key=stranger_key, subject=stranger
        )
        assert result.status == 'error' and result.error.code == 'achievement_grant_not_found'
    ok = await call(
        app, 'achievement.pin', {'grant_id': grants[0]['id']}, key=owner_key, subject=owner
    )
    assert ok.status == 'ok', wire(ok)
    bad = await call(app, 'achievement.reorder', {'grant_ids': []}, key=owner_key, subject=owner)
    assert bad.status == 'error' and bad.error.code == 'achievement_pin_set_mismatch'
    async with app.metadata.transaction(write=True) as tx:
        revoked = dict(grants[0], revoked_at=wire(NOW))
        tx.execute(
            'UPDATE achievement_grants SET body=? WHERE id=?',
            (canonical(revoked).decode(), grants[0]['id']),
            write=True,
        )
    public = await call(app, 'achievement.list', {'subject_id': owner})
    assert not public.data['pinned_grant_ids']
    rejected = await call(
        app, 'achievement.pin', {'grant_id': grants[0]['id']}, key=owner_key, subject=owner
    )
    assert rejected.status == 'error' and rejected.error.code == 'achievement_grant_not_found'


@pytest.mark.asyncio
async def test_custodial_pin_changes_are_signed_audited_and_keep_grants(installed):
    import os

    from msg.core.codec import b64, decode, loads, unb64
    from msg.core.models import Signature
    from msg.security.crypto import verify

    app, _ = installed
    created = await call(
        app,
        'identity.custodial_create',
        {
            'handle': 'custodial-pin-owner',
            'nonce': b64(os.urandom(32)),
            'recovery_secret': b64(os.urandom(32)),
        },
        contract_version=2,
    )
    assert created.status == 'ok', wire(created)
    subject = created.data['subject_id']
    token = (created.data['credential_id'], unb64(created.data['token']))
    grants = await _fixture_grants(app, subject)
    ids = [g['id'] for g in grants]
    async with app.metadata.transaction(write=False) as tx:
        before = (
            tx.rows('SELECT body FROM certificates ORDER BY id'),
            tx.rows('SELECT body FROM credentials ORDER BY id'),
        )
    first = await call(
        app,
        'achievement.pin',
        {'grant_id': ids[0]},
        subject=subject,
        token=token,
        rid='custodial-pin-first',
    )
    assert first.status == 'ok' and first.data['signature_source'] == 'custodial', wire(first)
    replay = await call(
        app,
        'achievement.pin',
        {'grant_id': ids[0]},
        subject=subject,
        token=token,
        rid='custodial-pin-first',
    )
    assert replay.status == 'ok' and replay.replayed
    second = await call(app, 'achievement.pin', {'grant_id': ids[1]}, subject=subject, token=token)
    assert second.status == 'ok', wire(second)
    reordered = await call(
        app, 'achievement.reorder', {'grant_ids': ids[::-1]}, subject=subject, token=token
    )
    assert reordered.status == 'ok' and list(reordered.data['pinned_grant_ids']) == ids[::-1]
    removed = await call(
        app, 'achievement.unpin', {'grant_id': ids[0]}, subject=subject, token=token
    )
    assert removed.status == 'ok' and list(removed.data['pinned_grant_ids']) == [ids[1]]
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.rows('SELECT body FROM certificates ORDER BY id'),
            tx.rows('SELECT body FROM credentials ORDER BY id'),
        ) == before
        assert [row[0] for row in tx.rows('SELECT body FROM achievement_grants ORDER BY id')] == [
            canonical(g).decode() for g in grants
        ]
        audits = [loads(row[0]) for row in tx.rows('SELECT body FROM audit')]
        displays = [
            row['event'] for row in audits if row['event']['type'] == 'achievement.display.update'
        ]
        assert len(displays) == 4
        for event in displays:
            data = event['data']
            assert data['signature_source'] == 'custodial' and data['auth_method'] == 'token'
            assert data['confirmation']['subject_id'] == subject
            assert data['confirmation']['request_id'] == event['request_id']
            credential = await tx.credential(data['signature']['key_id'])
            verify(
                credential.verifier,
                canonical(data['confirmation']),
                decode(Signature, data['signature']),
                purpose='achievement-display',
            )
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            "UPDATE custodial_vault SET status='decrypt_only',signing_nonce=NULL,signing_ciphertext=NULL WHERE subject=?",
            (subject,),
            write=True,
        )
    denied = await call(
        app, 'achievement.unpin', {'grant_id': ids[1]}, subject=subject, token=token
    )
    assert denied.status == 'error' and denied.error.code == 'custodial_vault_unavailable'
    public = await call(app, 'achievement.list', {'subject_id': subject})
    assert list(public.data['pinned_grant_ids']) == [ids[1]]
    assert 'confirmation' not in public.data and 'signature_source' not in public.data
    private = await call(app, 'discovery.get', {'id': '/private'}, subject=subject, token=token)
    assert private.status == 'error'


@pytest.mark.asyncio
async def test_temporary_token_still_cannot_sign_honor_display(installed):
    from test_service import temporary_v3_args

    from msg.core.codec import unb64

    app, _ = installed
    args, rid, _, _ = temporary_v3_args()
    created = await call(app, 'identity.temporary', args, rid=rid, contract_version=3)
    assert created.status == 'ok', wire(created)
    subject = created.data['subject_id']
    token = (created.data['credential_id'], unb64(created.data['token']))
    denied = await call(
        app, 'achievement.pin', {'grant_id': 'achg_absent'}, subject=subject, token=token
    )
    assert denied.status == 'error' and denied.error.code == 'signature_required'
