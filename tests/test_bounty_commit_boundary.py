"""A real deferred COMMIT failure cannot consume any Bounty business fact."""

import pytest
from read_only_evidence import business_snapshot
from test_market_72 import fixture, proof
from test_service import call

from msg.core.codec import wire
from msg.plugins.money import _balance, _supply


@pytest.mark.asyncio
async def test_deferred_claim_commit_failure_preserves_all_rows_and_retry(installed):
    app, root = installed
    _, (key, buyer), created = await fixture(app, root)
    listing = created.data['bounty']['listing_id']
    args = await proof(app, (key, buyer), listing)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            """CREATE FUNCTION reject_test_bounty_commit() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'injected_bounty_commit_failure'; END;
            $$ LANGUAGE plpgsql""",
            write=True,
        )
        tx.execute(
            """CREATE CONSTRAINT TRIGGER reject_test_bounty_commit
            AFTER INSERT ON bounty_claims DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION reject_test_bounty_commit()""",
            write=True,
        )
    before = await business_snapshot(app)
    failed = await call(
        app, 'bounty.claim', args, key=key, subject=buyer, rid='deferred-bounty-claim'
    )
    assert failed.status == 'error', wire(failed)
    assert await business_snapshot(app) == before
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('DROP TRIGGER reject_test_bounty_commit ON bounty_claims', write=True)
        tx.execute('DROP FUNCTION reject_test_bounty_commit()', write=True)
    paid = await call(
        app, 'bounty.claim', args, key=key, subject=buyer, rid='deferred-bounty-claim'
    )
    assert paid.status == 'ok' and not paid.replayed, wire(paid)
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx, buyer) == 10 and _supply(tx) == 40
        assert tx.one('SELECT COUNT(*) FROM bounty_claims WHERE listing_id=?', (listing,))[0] == 1
        ledger_count = tx.one(
            'SELECT COUNT(*) FROM money_ledger WHERE reference=?', ('bounty_claim:' + listing,)
        )[0]
        notice_count = tx.one(
            "SELECT COUNT(*) FROM messages WHERE recipient=? AND body::jsonb->>'source'='bounty_claim'",
            (buyer,),
        )[0]
        assert ledger_count == notice_count == 1
    committed = await business_snapshot(app)
    replay = await call(
        app, 'bounty.claim', args, key=key, subject=buyer, rid='deferred-bounty-claim'
    )
    assert replay.status == 'ok' and replay.replayed and replay.data == paid.data
    assert await business_snapshot(app) == committed


@pytest.mark.asyncio
async def test_subject_limit_rejects_fresh_challenge_while_budget_remains(installed):
    app, root = installed
    _, (key, buyer), created = await fixture(app, root, max_claims=2)
    listing = created.data['bounty']['listing_id']
    args = await proof(app, (key, buyer), listing)
    paid = await call(app, 'bounty.claim', args, key=key, subject=buyer)
    assert paid.status == 'ok', wire(paid)
    assert paid.data['escrow_balance_minor'] == 10
    before = await business_snapshot(app)
    denied = await call(app, 'bounty.challenge', {'listing_id': listing}, key=key, subject=buyer)
    assert denied.status == 'error' and denied.error.code == 'bounty_subject_limit'
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_unsupported_challenge_verifier_cannot_consume_budget_or_nonce(installed):
    app, root = installed
    _, (key, buyer), created = await fixture(app, root)
    listing = created.data['bounty']['listing_id']
    args = await proof(app, (key, buyer), listing)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'UPDATE bounty_challenges SET verifier_version=2 WHERE id=?',
            (args['challenge_id'],),
            write=True,
        )
    before = await business_snapshot(app)
    denied = await call(app, 'bounty.claim', args, key=key, subject=buyer)
    assert denied.status == 'error' and denied.error.code == 'bounty_verifier_unsupported'
    assert await business_snapshot(app) == before
