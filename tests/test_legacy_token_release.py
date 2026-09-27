"""Version-one token issuance is disabled because it has no safe recovery."""

import os

import pytest

from msg.core.codec import b64, unb64
from msg.core.requests import request_for
from test_service import NOW, call, register, temporary_v3_args


def nonce():
    return b64(os.urandom(32))


async def state_counts(app):
    async with app.metadata.transaction(write=False) as tx:
        return tuple(tx.one(f'SELECT COUNT(*) FROM {table}')[0] for table in
                     ('identities', 'credentials', 'events', 'results', 'token_deliveries'))


async def assert_legacy_rejected_without_state_change(app, operation, args, **call_args):
    before = await state_counts(app)
    result = await call(app, operation, args, contract_version=1, **call_args)
    assert result.status == 'error'
    assert result.error.code == 'credential_delivery_upgrade_required'
    assert 'token' not in result.data
    assert await state_counts(app) == before


@pytest.mark.asyncio
async def test_custodial_create_v1_is_rejected_before_issuance(installed):
    app, _ = installed
    await assert_legacy_rejected_without_state_change(
        app, 'identity.custodial_create',
        {'handle': 'legacy-custodial-release', 'nonce': nonce()},
        rid='legacy-custodial-once')


@pytest.mark.asyncio
async def test_token_rotate_v1_is_rejected_before_issuance(installed):
    app, _ = installed
    args, request_id, _, _ = temporary_v3_args()
    bootstrap = await call(app, 'identity.temporary', args, rid=request_id,
                           contract_version=3)
    assert bootstrap.status == 'ok'
    subject = bootstrap.data['subject_id']
    old_token = (bootstrap.data['credential_id'], unb64(bootstrap.data['token']))
    await assert_legacy_rejected_without_state_change(
        app, 'identity.token_rotate', {'nonce': nonce()},
        subject=subject, token=old_token, rid='legacy-rotate-once')


@pytest.mark.asyncio
async def test_token_create_v1_is_rejected_before_issuance(installed):
    app, _ = installed
    signer, subject, _ = await register(app, 'legacy-token-create')
    await assert_legacy_rejected_without_state_change(
        app, 'identity.token_create',
        {'nonce': nonce(), 'ceiling': [], 'ttl': 900},
        key=signer, subject=subject, rid='legacy-token-create-once')


@pytest.mark.asyncio
async def test_old_cached_custodial_result_cannot_release_token(installed):
    """Simulate a durable v1 idempotency result from before the release gate."""
    app, _ = installed
    rid = 'legacy-cached-custodial'
    request_nonce = nonce()
    v2_args = {'handle': 'legacy-cached-release', 'nonce': request_nonce,
               'recovery_secret': nonce()}
    issued = await call(app, 'identity.custodial_create', v2_args,
                        rid=rid, contract_version=2)
    assert issued.status == 'ok' and issued.data.get('token')

    # Old cached results contain the same stable operation result. Bind that row
    # to the former v1 packet digest while retaining its real credential record.
    v1_args = {'handle': v2_args['handle'], 'nonce': request_nonce}
    old_packet = request_for('identity.custodial_create', v1_args,
        app.settings.service_url, request_id=rid, expires_at=NOW,
        contract_version=1)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE results SET digest=? WHERE subject=? AND request_id=?',
                   (old_packet.payload_digest, issued.subject, rid), write=True)

    replay = await call(app, 'identity.custodial_create', v1_args,
                        rid=rid, contract_version=1)
    assert replay.status == 'error'
    assert replay.error.code == 'credential_delivery_upgrade_required'
    assert 'token' not in replay.data
