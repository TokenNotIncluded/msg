"""Mixed restored projections cannot spend an unrelated ledger account."""

import pytest
from test_bounty import fund, terms
from test_market_72 import proof
from test_service import call, register

from msg.core.codec import wire


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['subject', 'other_bounty'])
@pytest.mark.parametrize(
    'operation', ['bounty.claim', 'bounty.close', 'bounty.top_up', 'bounty.get']
)
async def test_bounty_escrow_must_belong_to_its_listing(installed, target, operation):
    app, _ = installed
    publisher_key, publisher, _ = await register(app, 'escrow-binding-publisher')
    claimant_key, claimant, _ = await register(app, 'escrow-binding-claimant')
    await fund(app, publisher, 40)
    created = await call(app, 'bounty.create', terms(), key=publisher_key, subject=publisher)
    assert created.status == 'ok', wire(created)
    listing_id = created.data['bounty']['listing_id']
    claim = await proof(app, (claimant_key, claimant), listing_id)
    wrong_account = publisher
    if target == 'other_bounty':
        other = await call(
            app, 'bounty.create', terms(name='other-escrow'), key=publisher_key, subject=publisher
        )
        assert other.status == 'ok', wire(other)
        wrong_account = other.data['funding']['body']['to_subject']
    async with app.metadata.transaction(write=True) as tx:
        if target == 'other_bounty':
            # Keep the unique escrow pointer constraint intact while emulating
            # inconsistent materialized rows from different restored snapshots.
            tx.execute(
                'UPDATE bounty_listings SET escrow_subject=? WHERE listing_id=?',
                (publisher, other.data['bounty']['listing_id']),
                write=True,
            )
        tx.execute(
            'UPDATE bounty_listings SET escrow_subject=? WHERE listing_id=?',
            (wrong_account, listing_id),
            write=True,
        )
        before_ledger = tx.rows('SELECT * FROM money_ledger ORDER BY id')
        before_challenge = tx.one(
            'SELECT * FROM bounty_challenges WHERE id=?', (claim['challenge_id'],)
        )
    if operation == 'bounty.claim':
        params, key, subject = claim, claimant_key, claimant
    else:
        params, key, subject = {'listing_id': listing_id}, publisher_key, publisher
        if operation == 'bounty.top_up':
            params['amount_minor'] = 1
    result = await call(app, operation, params, key=key, subject=subject)
    assert result.status == 'error' and result.error.code == 'bounty_escrow_mismatch', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.rows('SELECT * FROM money_ledger ORDER BY id') == before_ledger
        assert (
            tx.one('SELECT * FROM bounty_challenges WHERE id=?', (claim['challenge_id'],))
            == before_challenge
        )
        assert tx.one('SELECT COUNT(*) FROM bounty_claims')[0] == 0
        assert (
            tx.one("SELECT COUNT(*) FROM messages WHERE body::jsonb->>'source'='bounty_claim'")[0]
            == 0
        )
