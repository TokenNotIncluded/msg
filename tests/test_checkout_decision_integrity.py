"""A valid old settlement signature does not authorize a different restored snapshot."""

import pytest
from test_managed_checkout import buy, ok, setup_sale
from test_service import call

from msg.core.codec import canonical, digest, wire
from msg.plugins.delivery import _delivery


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'changed', ['terms', 'payment_refs', 'settled_at', 'delivery_id', 'prepared_at']
)
async def test_claim_revalidates_signed_order_and_delivery_snapshots(installed, changed):
    app, _, buyer, _, arguments = await setup_sale(installed)
    result = ok(await buy(app, buyer, arguments))
    order_id = result.data['order']['id']
    # Simulate a stale/mixed restored projection, without bypassing immutable
    # decision or ledger triggers or signing a new server decision.
    async with app.metadata.transaction(write=True) as tx:
        if changed == 'terms':
            tx.execute(
                'UPDATE store_orders SET terms_digest=? WHERE id=?',
                (digest('unrelated restored terms'), order_id),
                write=True,
            )
        elif changed == 'payment_refs':
            tx.execute(
                'UPDATE store_orders SET receipt_refs=? WHERE id=?',
                (
                    canonical([result.data['settlement']['body']['transaction_id']]).decode(),
                    order_id,
                ),
                write=True,
            )
        elif changed == 'settled_at':
            tx.execute(
                'UPDATE store_orders SET settled_at=? WHERE id=?',
                ('2026-01-01T00:00:00Z', order_id),
                write=True,
            )
        elif changed == 'prepared_at':
            tx.execute(
                'UPDATE store_deliveries SET prepared_at=? WHERE order_id=?',
                ('2026-01-01T00:00:00Z', order_id),
                write=True,
            )
        else:
            delivery = _delivery(tx, order_id)
            new_id = 'dlv_restored_other_snapshot'
            body = {
                'delivery_id': new_id,
                'order_id': order_id,
                'recipient_subject': buyer[1],
                'kind': delivery['kind'],
                'manifest': delivery['manifest'],
                'payload_refs': delivery['payload_refs'],
                'package_digest': delivery['package_digest'],
                'channel': 'site',
            }
            tx.execute(
                'UPDATE store_deliveries SET id=?,delivery_digest=? WHERE order_id=?',
                (new_id, digest(body), order_id),
                write=True,
            )
        delivery = _delivery(tx, order_id)
        counts = {
            table: tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('money_ledger', 'order_escrow_decisions', 'events', 'jobs')
        }
    claim = await call(
        app,
        'delivery.claim',
        {'order_id': order_id, 'delivery_digest': delivery['delivery_digest']},
        key=buyer[0],
        subject=buyer[1],
    )
    assert claim.status == 'error' and claim.error.code == 'escrow_decision_mismatch', wire(claim)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one(
            'SELECT state,claimed_at FROM store_deliveries WHERE order_id=?', (order_id,)
        ) == ('prepared', None)
        for table, count in counts.items():
            assert tx.one(f'SELECT COUNT(*) FROM {table}')[0] == count
