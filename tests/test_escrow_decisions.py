"""Existing objective settlement must pass one signed, atomic decision boundary."""

import asyncio

import pytest
from test_orders import _intent, _sale
from test_service import call, register

from msg.admin.money import apply_money
from msg.core.codec import canonical, decode, loads, wire
from msg.core.models import Signature
from msg.security.crypto import verify


async def funded(installed):
    app, root = installed
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'decision-buyer')
    listing, _ = await _sale(app, seller_key, seller)
    await apply_money(app, root, action='mint', operator='test-console', amount_minor=5_000_000)
    await apply_money(
        app,
        root,
        action='transfer',
        operator='test-console',
        subject_id=buyer,
        amount_minor=5_000_000,
    )
    result = await call(app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer)
    assert result.status == 'ok', wire(result)
    return app, buyer_key, buyer, seller, result.data['order']['id']


@pytest.mark.asyncio
async def test_cancel_commits_one_signed_decision_for_competing_requests(installed):
    app, key, buyer, seller, order_id = await funded(installed)
    results = await asyncio.gather(
        *(
            call(
                app,
                'orders.cancel',
                {'order_id': order_id},
                key=key,
                subject=buyer,
                rid=f'cancel-{number}',
            )
            for number in range(2)
        )
    )
    assert sum(result.status == 'ok' for result in results) == 1, wire(results)
    successful = next(result for result in results if result.status == 'ok')
    async with app.metadata.transaction(write=False) as tx:
        rows = tx.rows(
            'SELECT body,signature,transaction_id FROM order_escrow_decisions WHERE order_id=?',
            (order_id,),
        )
        assert len(rows) == 1
        body = loads(rows[0][0])
        assert body['outcome'] == 'refund' and body['recipient'] == buyer
        verify(
            app.receipt_signer.public_key,
            canonical(body),
            decode(Signature, loads(rows[0][1])),
            purpose='escrow-decision-v1',
        )
        assert rows[0][2] == successful.data['refund']['body']['transaction_id']
        assert (
            tx.one(
                'SELECT COUNT(*) FROM money_ledger WHERE reference=?', ('order_refund:' + order_id,)
            )[0]
            == 1
        )
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order_id,))[0] == 'refunded'
    from msg.core.errors import Failure

    for statement in (
        "UPDATE order_escrow_decisions SET body='{}' WHERE order_id=?",
        'DELETE FROM order_escrow_decisions WHERE order_id=?',
    ):
        with pytest.raises(Failure):
            async with app.metadata.transaction(write=True) as tx:
                tx.execute(statement, (order_id,), write=True)


@pytest.mark.asyncio
async def test_accept_uses_same_decision_boundary_and_dispute_freezes_payment(installed):
    app, key, buyer, seller, order_id = await funded(installed)
    prepared = await call(app, 'delivery.prepare', {'order_id': order_id}, key=key, subject=buyer)
    assert prepared.status == 'ok', wire(prepared)
    read = await call(app, 'delivery.get', {'order_id': order_id}, key=key, subject=buyer)
    assert read.status == 'ok', wire(read)
    args = {'order_id': order_id, 'delivery_digest': read.data['delivery']['delivery_digest']}
    # A persisted dispute cannot be bypassed by an otherwise valid buyer proof.
    async with app.metadata.transaction(write=True) as tx:
        tx.execute("UPDATE store_orders SET state='disputed' WHERE id=?", (order_id,), write=True)
    blocked = await call(app, 'delivery.accept', args, key=key, subject=buyer)
    assert blocked.status == 'error'
    async with app.metadata.transaction(write=True) as tx:
        assert not tx.rows('SELECT id FROM order_escrow_decisions WHERE order_id=?', (order_id,))
        tx.execute("UPDATE store_orders SET state='delivered' WHERE id=?", (order_id,), write=True)
    result = await call(app, 'delivery.accept', args, key=key, subject=buyer)
    assert result.status == 'ok', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        body = loads(
            tx.one('SELECT body FROM order_escrow_decisions WHERE order_id=?', (order_id,))[0]
        )
        assert body['outcome'] == 'release' and body['recipient'] == seller
        assert body['evidence_digest'] and body['policy_digest']


@pytest.mark.asyncio
async def test_failed_decision_journal_rolls_back_money_and_order(installed, monkeypatch):
    app, key, buyer, seller, order_id = await funded(installed)
    session_type = None
    async with app.metadata.transaction(write=False) as tx:
        session_type = type(tx)
        before = tx.one('SELECT COUNT(*) FROM money_ledger')[0]
    original = session_type.execute

    def fail_journal(self, sql, parameters=(), **kwargs):
        if 'INSERT INTO order_escrow_decisions' in sql:
            from msg.core.errors import Failure

            raise Failure('injected_journal_failure')
        return original(self, sql, parameters, **kwargs)

    monkeypatch.setattr(session_type, 'execute', fail_journal)
    result = await call(app, 'orders.cancel', {'order_id': order_id}, key=key, subject=buyer)
    assert result.status == 'error' and result.error.code == 'injected_journal_failure'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == before
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order_id,))[0] == 'funded'
        assert tx.one('SELECT COUNT(*) FROM order_escrow_decisions')[0] == 0


@pytest.mark.parametrize(
    'tamper', ['amount', 'recipient', 'policy', 'signature', 'purpose', 'expired']
)
def test_decision_validation_rejects_tampering_and_expiry(tamper):
    from datetime import timedelta

    from test_service import NOW

    from msg.core.errors import Failure
    from msg.market.escrow import validate_decision
    from msg.security.crypto import Ed25519Signer

    signer = Ed25519Signer.generate()
    expected = {
        'amount_minor': 5_000_000,
        'recipient': 'buyer',
        'policy_version': 1,
        'expires_at': wire(NOW + timedelta(seconds=60)),
    }
    body = dict(expected)
    if tamper == 'amount':
        body['amount_minor'] = 1
    elif tamper == 'recipient':
        body['recipient'] = 'other'
    elif tamper == 'policy':
        body['policy_version'] = 2
    key = Ed25519Signer.generate() if tamper == 'signature' else signer
    purpose = 'money-receipt' if tamper == 'purpose' else 'escrow-decision-v1'
    decision = {'body': body, 'signature': wire(key.sign(canonical(body), purpose=purpose))}
    now = NOW + timedelta(seconds=60) if tamper == 'expired' else NOW
    with pytest.raises(Failure):
        validate_decision(decision, expected, signer.public_key, now)


@pytest.mark.asyncio
async def test_unknown_stored_policy_and_foreign_buyer_cannot_debit_escrow(installed):
    app, key, buyer, seller, order_id = await funded(installed)
    other_key, other, _ = await register(app, 'decision-outsider')
    for oid in (order_id, 'ord_absent'):
        result = await call(app, 'orders.cancel', {'order_id': oid}, key=other_key, subject=other)
        assert result.status == 'error' and result.error.code == 'order_not_found'
    async with app.metadata.transaction(write=True) as tx:
        before = tx.one('SELECT COUNT(*) FROM money_ledger')[0]
        tx.execute(
            "UPDATE store_orders SET dispute_policy='unknown-v2' WHERE id=?",
            (order_id,),
            write=True,
        )
    denied = await call(app, 'orders.cancel', {'order_id': order_id}, key=key, subject=buyer)
    assert denied.status == 'error' and denied.error.code == 'escrow_policy_unsupported'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == before
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order_id,))[0] == 'funded'
