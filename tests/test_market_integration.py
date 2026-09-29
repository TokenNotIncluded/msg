"""Published checkout compatibility, restored snapshot integrity and disabled mail."""

from dataclasses import replace

import pytest
from test_market_delivery import Sender, drain, enable_mail, verified_address
from test_market_lifecycle import buy, market
from test_orders import _intent
from test_service import call

from msg.core.codec import digest, wire
from msg.plugins.delivery import _delivery


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field,value',
    [
        ('seller', None),
        ('listing_id', None),
        ('listing_revision', 'r_mixed'),
        ('package_revision', 'r_mixed'),
        ('terms_digest', digest('mixed terms')),
        ('escrow_policy', 'escrow-instant-v1'),
        ('dispute_policy', 'mixed-policy'),
        ('created_at', '2026-01-01T00:00:00Z'),
        ('unit_price_minor', 1),
        ('quantity', 2),
    ],
)
async def test_funding_rejects_mixed_immutable_checkout_projection(installed, field, value):
    app, root = installed
    _, _, key, buyer, listing, _ = await market(app, root, mode='service', kind='service')
    created = await call(app, 'orders.create', _intent(listing), key=key, subject=buyer)
    assert created.status == 'ok', wire(created)
    order = created.data['order']
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            f'UPDATE store_orders SET {field}=? WHERE id=?',
            (buyer if value is None else value, order['id']),
            write=True,
        )
        counts = {
            table: tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('money_ledger', 'order_transitions', 'events', 'jobs')
        }
    result = await call(
        app,
        'orders.fund',
        {
            'order_id': order['id'],
            'order_digest': order['order_digest'],
            'total_price_minor': 5_000_000,
            'currency_id': 'primary',
        },
        key=key,
        subject=buyer,
    )
    assert result.status == 'error', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order['id'],)) == ('created',)
        for table, count in counts.items():
            assert tx.one(f'SELECT COUNT(*) FROM {table}')[0] == count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'changed', ['terms', 'payment_refs', 'settled_at', 'delivery_id', 'prepared_at']
)
async def test_claim_rejects_mixed_settled_snapshot(installed, changed):
    app, root = installed
    _, _, key, buyer, listing, _ = await market(app, root)
    bought = await buy(app, key, buyer, listing)
    assert bought.status == 'ok', wire(bought)
    oid = bought.data['order']['id']
    async with app.metadata.transaction(write=True) as tx:
        if changed == 'terms':
            tx.execute(
                'UPDATE store_orders SET terms_digest=? WHERE id=?',
                (digest('mixed terms'), oid),
                write=True,
            )
        elif changed == 'payment_refs':
            tx.execute("UPDATE store_orders SET receipt_refs='[]' WHERE id=?", (oid,), write=True)
        elif changed == 'settled_at':
            tx.execute(
                'UPDATE store_orders SET settled_at=? WHERE id=?',
                ('2026-01-01T00:00:00Z', oid),
                write=True,
            )
        elif changed == 'prepared_at':
            tx.execute(
                'UPDATE store_deliveries SET prepared_at=? WHERE order_id=?',
                ('2026-01-01T00:00:00Z', oid),
                write=True,
            )
        else:
            from msg.market.delivery import body_for
            from msg.plugins.orders import _row

            d = _delivery(tx, oid)
            identifier = 'dlv_other_restored_delivery'
            body = body_for(
                _row(tx, oid, buyer), identifier, d['kind'], d['manifest'], d['payload_refs']
            )
            tx.execute(
                'UPDATE store_deliveries SET id=?,delivery_digest=? WHERE order_id=?',
                (identifier, digest(body), oid),
                write=True,
            )
        delivery = _delivery(tx, oid)
        counts = {
            table: tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('money_ledger', 'order_settlements', 'events', 'jobs')
        }
    result = await call(
        app,
        'delivery.accept',
        {'order_id': oid, 'delivery_digest': delivery['delivery_digest']},
        key=key,
        subject=buyer,
        contract_version=2,
    )
    assert result.status == 'error', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state,claimed_at FROM store_deliveries WHERE order_id=?', (oid,)) == (
            'prepared',
            None,
        )
        for table, count in counts.items():
            assert tx.one(f'SELECT COUNT(*) FROM {table}')[0] == count


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['before_checkout', 'before_worker'])
async def test_explicitly_disabled_mail_never_calls_sender(installed, when):
    app, root = installed
    enable_mail(app)
    _, seller, key, buyer, listing, _ = await market(app, root)
    await verified_address(app, key, buyer, 'buyer@example.invalid')

    def disable():
        app.settings = replace(
            app.settings,
            server=replace(
                app.settings.server, mail=replace(app.settings.server.mail, enabled=False)
            ),
        )

    if when == 'before_checkout':
        disable()
    result = await buy(app, key, buyer, listing, email='buyer@example.invalid')
    assert result.status == 'ok' and result.data['order']['state'] == 'settled', wire(result)
    oid = result.data['order']['id']
    disable()
    sender = Sender()
    await drain(app, sender)
    assert sender.messages == []
    result = await call(app, 'orders.get', {'order_id': oid}, key=key, subject=buyer)
    assert result.data['order']['email_status'] == 'disabled', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        from msg.plugins.money import _balance

        assert _balance(tx, buyer) == 15_000_000 and _balance(tx, seller) == 5_000_000
        assert tx.one('SELECT state,claimed_at FROM store_deliveries WHERE order_id=?', (oid,)) == (
            'prepared',
            None,
        )
