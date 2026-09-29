"""A restored mutable bounty projection cannot replace published payout terms."""

import pytest
from test_market_72 import fixture, proof
from test_service import NOW, call

from msg.admin.diagnostics import doctor
from msg.core.codec import canonical, wire
from msg.plugins.money import _balance


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field,value',
    [
        ('reward_minor', 20),
        ('max_claims', 2),
        ('claim_limit_per_subject', 2),
        ('eligibility', canonical({'kind': 'allowlist', 'subjects': []}).decode()),
    ],
)
async def test_mixed_bounty_projection_cannot_change_published_terms(installed, field, value):
    app, root = installed
    _, identity, created = await fixture(app, root)
    key, buyer = identity
    lid = created.data['bounty']['listing_id']
    args = await proof(app, identity, lid)
    # Emulate a mixed restore of the materialized accounting projection. The
    # immutable Resource revision still commits the publisher's original terms.
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            f'UPDATE bounty_listings SET {field}=? WHERE listing_id=?', (value, lid), write=True
        )
    result = await call(app, 'bounty.claim', args, key=key, subject=buyer)
    assert result.status == 'error', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx, buyer) == 0
        assert tx.one('SELECT COUNT(*) FROM bounty_claims')[0] == 0
        assert (
            tx.one('SELECT consumed_at FROM bounty_challenges WHERE id=?', (args['challenge_id'],))[
                0
            ]
            is None
        )
    for operation, params in [
        ('bounty.get', {'listing_id': lid}),
        ('store.listing_get', {'id': lid}),
    ]:
        read = await call(app, operation, params)
        assert read.status == 'error' and read.error.code == 'bounty_contract_mismatch', wire(read)


@pytest.mark.asyncio
async def test_doctor_rejects_mixed_bounty_terms_without_repair(installed):
    app, root = installed
    _, _, created = await fixture(app, root)
    lid = created.data['bounty']['listing_id']
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'UPDATE bounty_listings SET reward_minor=20 WHERE listing_id=?', (lid,), write=True
        )
        before = tx.rows('SELECT * FROM money_ledger ORDER BY id')
    report = doctor(app.settings.config_dir, clock=lambda: NOW)
    assert report['checks']['market_clearing'] == {'ok': False, 'code': 'bounty_contract_mismatch'}
    async with app.metadata.transaction(write=False) as tx:
        assert tx.rows('SELECT * FROM money_ledger ORDER BY id') == before
        assert (
            tx.one('SELECT reward_minor FROM bounty_listings WHERE listing_id=?', (lid,))[0] == 20
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'operation,extra',
    [
        ('bounty.top_up', {'amount_minor': 1}),
        ('bounty.close', {}),
        ('bounty.pause', {}),
        ('bounty.resume', {}),
        ('bounty.challenge', {}),
    ],
)
async def test_mixed_terms_block_management_without_ledger_changes(installed, operation, extra):
    app, root = installed
    publisher, identity, created = await fixture(app, root)
    lid = created.data['bounty']['listing_id']
    key, subject = identity if operation == 'bounty.challenge' else publisher
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'UPDATE bounty_listings SET reward_minor=20 WHERE listing_id=?', (lid,), write=True
        )
        before = tx.rows('SELECT * FROM money_ledger ORDER BY id')
        before_listing = tx.one('SELECT * FROM bounty_listings WHERE listing_id=?', (lid,))
    result = await call(app, operation, {'listing_id': lid, **extra}, key=key, subject=subject)
    assert result.status == 'error' and result.error.code == 'bounty_contract_mismatch', wire(
        result
    )
    async with app.metadata.transaction(write=False) as tx:
        assert tx.rows('SELECT * FROM money_ledger ORDER BY id') == before
        assert tx.one('SELECT * FROM bounty_listings WHERE listing_id=?', (lid,)) == before_listing
        assert tx.one('SELECT COUNT(*) FROM bounty_challenges')[0] == 0
