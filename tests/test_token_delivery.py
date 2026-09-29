"""Strict token delivery is opt-in at issuance version 2."""

import asyncio
import os
from datetime import timedelta

import pytest
from test_service import NOW, call, register, temporary_v3_args

from msg.application import Application
from msg.core.codec import b64, unb64, wire
from msg.core.requests import receipt_bytes, request_for
from msg.security.crypto import verify


def secret():
    return b64(os.urandom(32))


@pytest.mark.asyncio
async def test_v2_bootstrap_claim_once_and_recover_lost_response(installed):
    app, _ = installed
    original, _, _, _ = temporary_v3_args(request_id='strict-bootstrap')
    created = await call(
        app, 'identity.temporary', original, rid='strict-bootstrap', contract_version=3
    )
    assert created.status == 'ok' and created.data['token']
    uid = created.data['subject_id']
    old_id = created.data['credential_id']
    async with app.metadata.transaction(write=False) as tx:
        row = tx.one(
            'SELECT recovery_verifier,claimed_at,consumed_at FROM token_deliveries WHERE credential_id=?',
            (old_id,),
        )
        assert row[1] and row[2] is None
        assert original['recovery_secret'] not in str(row)
        assert created.data['token'] not in str(row)
    replay = await call(
        app, 'identity.temporary', original, rid='strict-bootstrap', contract_version=3
    )
    assert replay.error.code == 'token_delivery_unavailable'
    args = {
        'credential_id': old_id,
        'original_request_id': 'strict-bootstrap',
        'recovery_secret': original['recovery_secret'],
        'nonce': secret(),
        'new_recovery_secret': secret(),
    }
    wrong = await call(
        app,
        'identity.token_recover',
        {**args, 'recovery_secret': secret()},
        subject=uid,
        rid='bad-recovery',
    )
    assert wrong.error.code == 'recovery_unavailable'
    recovered = await call(app, 'identity.token_recover', args, subject=uid, rid='recover-once')
    assert recovered.status == 'ok' and recovered.data['token'], recovered.error.code
    verify(
        app.receipt_signer.public_key,
        receipt_bytes(recovered),
        recovered.receipt,
        purpose='receipt',
    )
    new = (recovered.data['credential_id'], unb64(recovered.data['token']))
    old = (old_id, unb64(created.data['token']))
    assert new[0] != old[0]
    repeated = await call(app, 'identity.token_recover', args, subject=uid, rid='recover-once')
    assert repeated.error.code == 'token_delivery_unavailable'
    reuse = await call(
        app,
        'identity.token_recover',
        {**args, 'nonce': secret()},
        subject=uid,
        rid='another-recovery',
    )
    assert reuse.error.code == 'recovery_unavailable'
    blocked = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'old'}, subject=uid, token=old
    )
    assert blocked.error.code == 'credential_revoked'
    allowed = await call(
        app, 'content.post_create', {'parent': '/main', 'body': 'new'}, subject=uid, token=new
    )
    assert allowed.status == 'ok', wire(allowed)


@pytest.mark.asyncio
async def test_v2_concurrent_claim_and_cross_subject_denial(installed):
    app, _ = installed
    args = {'handle': 'delivery-race', 'nonce': secret(), 'recovery_secret': secret()}
    first, second = await asyncio.gather(
        *(
            call(app, 'identity.custodial_create', args, rid='race', contract_version=2)
            for _ in range(2)
        )
    )
    assert sorted((first.status, second.status)) == ['error', 'ok']
    assert (
        next(r for r in (first, second) if r.status == 'error').error.code
        == 'token_delivery_unavailable'
    )
    winner = next(r for r in (first, second) if r.status == 'ok')
    other_args, other_rid, _, _ = temporary_v3_args()
    other = await call(app, 'identity.temporary', other_args, rid=other_rid, contract_version=3)
    denied = await call(
        app,
        'identity.token_recover',
        {
            'credential_id': winner.data['credential_id'],
            'original_request_id': 'race',
            'recovery_secret': args['recovery_secret'],
            'nonce': secret(),
            'new_recovery_secret': secret(),
        },
        subject=other.data['subject_id'],
    )
    assert denied.error.code == 'recovery_unavailable'


@pytest.mark.asyncio
async def test_v2_unclaimed_commit_survives_restart_and_expiry(installed):
    app, _ = installed
    args, _, _, _ = temporary_v3_args(request_id='restart-before-claim')
    original_hook = app.executor.response_hook
    app.executor.response_hook = None  # simulates commit followed by process loss before claim
    committed = await call(
        app, 'identity.temporary', args, rid='restart-before-claim', contract_version=3
    )
    assert committed.status == 'ok' and 'token' not in committed.data
    second = Application(app.settings, clock=lambda: NOW)
    await second.load()
    try:
        claimed = await call(
            second, 'identity.temporary', args, rid='restart-before-claim', contract_version=3
        )
        assert claimed.status == 'ok' and claimed.replayed and claimed.data['token']
        exhausted = await call(
            second, 'identity.temporary', args, rid='restart-before-claim', contract_version=3
        )
        assert exhausted.error.code == 'token_delivery_unavailable'
        uid = claimed.data['subject_id']
        recovery = {
            'credential_id': claimed.data['credential_id'],
            'original_request_id': 'restart-before-claim',
            'recovery_secret': args['recovery_secret'],
            'nonce': secret(),
            'new_recovery_secret': secret(),
        }
        second.clock = lambda: NOW + timedelta(minutes=16)
        second.authenticator.clock = second.clock
        packet = request_for(
            'identity.token_recover',
            recovery,
            second.settings.service_url,
            subject=uid,
            expires_at=NOW + timedelta(minutes=18),
        )
        expired = await second.executor.execute(packet)
        assert expired.error.code == 'recovery_unavailable'
    finally:
        await second.close()
        app.executor.response_hook = original_hook


@pytest.mark.asyncio
async def test_v2_rotate_and_signed_create_bind_separate_recovery(installed):
    app, _ = installed
    bootstrap_args, _, _, _ = temporary_v3_args(request_id='rotate-bootstrap')
    bootstrap = await call(
        app, 'identity.temporary', bootstrap_args, rid='rotate-bootstrap', contract_version=3
    )
    uid = bootstrap.data['subject_id']
    token = (bootstrap.data['credential_id'], unb64(bootstrap.data['token']))
    rotate_args = {'nonce': secret(), 'recovery_secret': secret()}
    rotated = await call(
        app,
        'identity.token_rotate',
        rotate_args,
        subject=uid,
        token=token,
        rid='rotate-v2',
        contract_version=2,
    )
    assert rotated.status == 'ok' and rotated.data['token'], rotated.error
    replay = await call(
        app,
        'identity.token_rotate',
        rotate_args,
        subject=uid,
        token=token,
        rid='rotate-v2',
        contract_version=2,
    )
    assert replay.error.code == 'token_delivery_unavailable'
    recovered = await call(
        app,
        'identity.token_recover',
        {
            'credential_id': rotated.data['credential_id'],
            'original_request_id': 'rotate-v2',
            'recovery_secret': rotate_args['recovery_secret'],
            'nonce': secret(),
            'new_recovery_secret': secret(),
        },
        subject=uid,
    )
    assert recovered.status == 'ok' and recovered.data['token'], recovered.error

    signer, signed_uid, _ = await register(app, 'signed-delivery')
    created_args = {'nonce': secret(), 'recovery_secret': secret(), 'ceiling': [], 'ttl': 900}
    issued = await call(
        app,
        'identity.token_create',
        created_args,
        key=signer,
        subject=signed_uid,
        contract_version=2,
    )
    assert issued.status == 'ok' and issued.data['token'], issued.error
    again = await call(
        app,
        'identity.token_create',
        created_args,
        key=signer,
        subject=signed_uid,
        rid=issued.request_id,
        contract_version=2,
    )
    assert again.error.code == 'token_delivery_unavailable'
    restored = await call(
        app,
        'identity.token_recover',
        {
            'credential_id': issued.data['credential_id'],
            'original_request_id': issued.request_id,
            'recovery_secret': created_args['recovery_secret'],
            'nonce': secret(),
            'new_recovery_secret': secret(),
        },
        subject=signed_uid,
    )
    assert restored.status == 'ok' and restored.data['token'], restored.error


@pytest.mark.asyncio
async def test_v2_recovery_secret_is_consumed_once_under_race(installed):
    app, _ = installed
    args, request_id, _, _ = temporary_v3_args()
    created = await call(app, 'identity.temporary', args, rid=request_id, contract_version=3)
    uid = created.data['subject_id']
    base = {
        'credential_id': created.data['credential_id'],
        'original_request_id': created.request_id,
        'recovery_secret': args['recovery_secret'],
    }
    first, second = await asyncio.gather(
        call(
            app,
            'identity.token_recover',
            {**base, 'nonce': secret(), 'new_recovery_secret': secret()},
            subject=uid,
            rid='recover-race-a',
        ),
        call(
            app,
            'identity.token_recover',
            {**base, 'nonce': secret(), 'new_recovery_secret': secret()},
            subject=uid,
            rid='recover-race-b',
        ),
    )
    assert sorted((first.status, second.status)) == ['error', 'ok']
    assert (
        next(r for r in (first, second) if r.status == 'error').error.code == 'recovery_unavailable'
    )
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one(
            'SELECT consumed_at FROM token_deliveries WHERE credential_id=?',
            (created.data['credential_id'],),
        )[0]
