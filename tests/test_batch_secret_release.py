from datetime import timedelta

import pytest
from test_batch import packet
from test_service import NOW, call, register

from msg.core.codec import b64, wire
from msg.core.models import OperationResult
from msg.core.requests import request_for


@pytest.mark.asyncio
async def test_batch_rejects_one_time_secret_operations(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'batchsecret')
    operations = [
        'identity.token_recover',
        'identity.token_create',
        'identity.token_rotate',
        'identity.custodial_create',
        'identity.temporary',
    ]

    for index, operation in enumerate(operations):
        child = packet(app, key, uid, operation, {}, f'child-secret-{index}')
        result = await call(app, 'batch.atomic', {'requests': [child]}, key=key, subject=uid)

        assert result.status == 'error'
        assert result.error.code == 'operation_not_batchable'


@pytest.mark.asyncio
async def test_old_cached_secret_batch_is_rejected_before_replay(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'batchcachedsecret')
    secret = 'cached-token-must-not-escape'
    child_rid = 'cached-secret-child'
    child = packet(
        app,
        key,
        uid,
        'identity.token_recover',
        {
            'credential_id': 't_oldcredential',
            'original_request_id': 'old-token-creation',
            'recovery_secret': b64(b'r' * 32),
            'nonce': b64(b'n' * 32),
            'new_recovery_secret': b64(b's' * 32),
        },
        child_rid,
    )
    args = {'requests': [child]}
    parent = request_for(
        'batch.atomic',
        args,
        app.settings.service_url,
        signer=key,
        subject=uid,
        request_id='cached-secret-parent',
        expires_at=NOW + timedelta(seconds=120),
    )
    old_child_result = OperationResult(
        request_id=child_rid,
        operation='identity.token_recover',
        status='ok',
        actor=uid,
        subject=uid,
        data={'token': secret},
    )
    old_parent_result = OperationResult(
        request_id=parent.request_id,
        operation='batch.atomic',
        status='ok',
        actor=uid,
        subject=uid,
        data={'atomic': True, 'results': [wire(old_child_result)]},
    )
    async with app.metadata.transaction(write=True) as tx:
        await tx.save_result(uid, parent.payload_digest, old_parent_result)

    replay = await app.executor.execute(parent)

    assert replay.status == 'error'
    assert replay.error.code == 'operation_not_batchable'
    assert secret not in str(wire(replay))
    assert replay.replayed is False

    ordinary_child = packet(
        app,
        key,
        uid,
        'content.post_create',
        {'parent': '/main', 'body': 'ordinary cached batch'},
        'ordinary-cached-child',
    )
    ordinary_args = {'requests': [ordinary_child]}
    ordinary = request_for(
        'batch.atomic',
        ordinary_args,
        app.settings.service_url,
        signer=key,
        subject=uid,
        request_id='ordinary-cached-parent',
        expires_at=NOW + timedelta(seconds=120),
    )
    first = await app.executor.execute(ordinary)
    assert first.status == 'ok', first
    repeated = await app.executor.execute(ordinary)
    assert repeated.status == 'ok' and repeated.replayed, repeated


@pytest.mark.asyncio
async def test_independent_batch_rejects_recovery_before_child_or_parent_write(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'batchindependentsecret')
    recovery_secret = b64(b'recovery-secret-for-independent-batch')
    original_rid = 'independent-token-source'
    issued = await call(
        app,
        'identity.token_create',
        {
            'nonce': b64(b'creation-nonce-for-independent-batch'),
            'recovery_secret': recovery_secret,
            'ceiling': [],
            'ttl': 900,
        },
        key=key,
        subject=uid,
        certs=(cert,),
        rid=original_rid,
        contract_version=2,
    )
    assert issued.status == 'ok' and issued.data['token']
    old_token = issued.data['token']
    credential_id = issued.data['credential_id']
    async with app.metadata.transaction(write=False) as tx:
        before_credential = await tx.credential(credential_id)
        before_delivery = tx.one(
            'SELECT consumed_at FROM token_deliveries WHERE credential_id=?', (credential_id,)
        )
    assert before_credential.revoked_at is None
    assert before_delivery is not None and before_delivery[0] is None

    child = packet(
        app,
        key,
        uid,
        'identity.token_recover',
        {
            'credential_id': credential_id,
            'original_request_id': original_rid,
            'recovery_secret': recovery_secret,
            'nonce': b64(b'recovery-nonce-for-independent-batch'),
            'new_recovery_secret': b64(b'new-recovery-secret-for-independent-batch'),
        },
        'independent-recovery-child',
    )
    parent = request_for(
        'batch.independent',
        {'requests': [child]},
        app.settings.service_url,
        signer=key,
        subject=uid,
        request_id='independent-recovery-parent',
        expires_at=NOW + timedelta(seconds=120),
    )

    result = await app.executor.execute(parent)

    assert result.status == 'error'
    assert result.error.code == 'operation_not_batchable'
    assert old_token not in str(wire(result))
    assert result.replayed is False
    async with app.metadata.transaction(write=False) as tx:
        after_credential = await tx.credential(credential_id)
        after_delivery = tx.one(
            'SELECT consumed_at FROM token_deliveries WHERE credential_id=?', (credential_id,)
        )
        assert (
            tx.one(
                'SELECT 1 FROM results WHERE subject=? AND request_id=?', (uid, parent.request_id)
            )
            is None
        )
        assert (
            tx.one(
                'SELECT 1 FROM batches WHERE subject=? AND request_id=?', (uid, parent.request_id)
            )
            is None
        )
    assert after_credential.revoked_at is None
    assert after_delivery is not None and after_delivery[0] is None
