"""A prepared package never bypasses current buyer/target binding."""

import pytest
from test_delivery import _funded
from test_service import call

from msg.core.codec import canonical, wire


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'target',
    [
        {'subject_id': 'someone-else', 'channel': 'site'},
        {'channel': 'site'},
        {'subject_id': 'buyer', 'channel': 'email'},
        {'subject_id': 'buyer', 'channel': 'site', 'recipient': 'other@example.org'},
    ],
)
async def test_delivery_read_rechecks_order_target_after_prepare(installed, target):
    app, _, identity, order, _ = await _funded(installed)
    key, buyer = identity
    prepared = await call(
        app, 'delivery.prepare', {'order_id': order['id']}, key=key, subject=buyer
    )
    assert prepared.status == 'ok', wire(prepared)
    target = {k: buyer if v == 'buyer' else v for k, v in target.items()}
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'UPDATE store_orders SET delivery_target=? WHERE id=?',
            (canonical(target).decode(), order['id']),
            write=True,
        )
    read = await call(app, 'delivery.get', {'order_id': order['id']}, key=key, subject=buyer)
    assert read.status == 'error', wire(read)
    assert read.error.code == 'delivery_recipient_mismatch'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order['id'],))[0] == 'delivered'
        assert (
            tx.one('SELECT state FROM store_deliveries WHERE order_id=?', (order['id'],))[0]
            == 'prepared'
        )
        assert tx.one('SELECT COUNT(*) FROM order_escrow_decisions')[0] == 0
