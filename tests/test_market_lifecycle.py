"""The v3 market contract: real signatures, immutable snapshots and conserved money."""

import asyncio
from datetime import timedelta

import pytest
from test_orders import _intent, _sale
from test_service import NOW, call, register

from msg.admin.money import apply_money
from msg.core.codec import unb64, wire


async def market(
    app, root, *, mode='managed_instant', kind='bundle', quantity=2, policy='dispute-v1'
):
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'order-buyer')
    if mode == 'managed_instant':
        listing, package = await _sale(app, seller_key, seller, quantity=quantity)
    else:
        created = await call(
            app,
            'store.listing_create',
            {
                'name': 'sealed sale',
                'item_kind': kind,
                'price_minor': 5_000_000,
                'currency_id': 'primary',
                'quantity': quantity,
                'delivery_mode': mode,
                'escrow_policy': 'escrow-v1',
                'dispute_policy': policy,
                'terms': 'Delivery within one day; manual acceptance.',
            },
            key=seller_key,
            subject=seller,
        )
        assert created.status == 'ok', wire(created)
        active = await call(
            app,
            'store.listing_update',
            {'id': created.resources[0].id, 'state': 'active'},
            key=seller_key,
            subject=seller,
            expected=((created.resources[0].id, created.data['generation']),),
        )
        assert active.status == 'ok', wire(active)
        listing, package = active.data['listing'], None
    await apply_money(app, root, action='mint', operator='test-console', amount_minor=20_000_000)
    await apply_money(
        app,
        root,
        action='transfer',
        operator='test-console',
        subject_id=buyer,
        amount_minor=20_000_000,
    )
    return seller_key, seller, buyer_key, buyer, listing, package


async def buy(app, key, buyer, listing, **extra):
    return await call(
        app, 'orders.buy', {**_intent(listing), **extra}, key=key, subject=buyer, contract_version=3
    )


@pytest.mark.asyncio
async def test_instant_delivery_settlement_and_claim_are_distinct(installed):
    app, root = installed
    _sk, seller, bk, buyer, listing, package = await market(app, root)
    args = _intent(listing)
    result = await call(
        app, 'orders.buy', args, key=bk, subject=buyer, contract_version=3, rid='instant-once'
    )
    assert result.status == 'ok', wire(result)
    order = result.data['order']
    assert order['state'] == 'settled'
    assert order['package_digest'] == package['digest']
    replay = await call(
        app, 'orders.buy', args, key=bk, subject=buyer, contract_version=3, rid='instant-once'
    )
    assert replay.replayed and replay.data == result.data
    delivery = await call(
        app, 'delivery.get', {'order_id': order['id']}, key=bk, subject=buyer, contract_version=2
    )
    assert delivery.status == 'ok', wire(delivery)
    data = delivery.data['delivery']
    assert data['state'] == 'prepared' and data['claimed_at'] is None
    assert data['manifest']['text'] == 'msg.lmm.best store selftest'
    assert unb64(data['payloads'][0]['data']) == b'delivery-ok\n'
    async with app.metadata.transaction(write=False) as tx:
        escrow = tx.one('SELECT escrow_subject FROM store_orders WHERE id=?', (order['id'],))[0]
        from msg.plugins.money import _balance

        assert _balance(tx, buyer) == 15_000_000
        assert _balance(tx, seller) == 5_000_000
        assert _balance(tx, escrow) == 0
        assert (
            tx.one('SELECT COUNT(*) FROM order_settlements WHERE order_id=?', (order['id'],))[0]
            == 1
        )
        count = tx.one('SELECT COUNT(*) FROM money_ledger')[0]
    for _ in range(2):
        claimed = await call(
            app,
            'delivery.accept',
            {'order_id': order['id'], 'delivery_digest': data['delivery_digest']},
            key=bk,
            subject=buyer,
            contract_version=2,
        )
        assert claimed.status == 'ok', wire(claimed)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == count


@pytest.mark.asyncio
async def test_created_payment_snapshot_and_cancel_before_fund(installed):
    app, root = installed
    sk, seller, bk, buyer, listing, _ = await market(app, root)
    created = await call(app, 'orders.create', _intent(listing), key=bk, subject=buyer)
    assert created.status == 'ok', wire(created)
    order = created.data['order']
    assert order['state'] == 'created' and order['funded_at'] is None
    changed = await call(
        app,
        'store.listing_update',
        {'id': listing['listing_id'], 'price_minor': 7_000_000, 'terms': 'Later terms'},
        key=sk,
        subject=seller,
        expected=((listing['listing_id'], 2),),
    )
    assert changed.status == 'ok', wire(changed)
    funded = await call(
        app,
        'orders.fund',
        {
            'order_id': order['id'],
            'order_digest': order['order_digest'],
            'total_price_minor': 5_000_000,
            'currency_id': 'primary',
        },
        key=bk,
        subject=buyer,
    )
    assert funded.status == 'ok' and funded.data['order']['state'] == 'settled', wire(funded)
    assert funded.data['order']['listing_revision'] == listing['listing_revision']
    assert funded.data['order']['terms_digest'] == listing['terms_digest']
    other = await call(
        app, 'orders.create', _intent(changed.data['listing']), key=bk, subject=buyer
    )
    cancelled = await call(
        app,
        'orders.cancel',
        {'order_id': other.data['order']['id']},
        key=bk,
        subject=buyer,
        contract_version=2,
    )
    assert cancelled.status == 'ok' and cancelled.data['order']['state'] == 'cancelled'


@pytest.mark.asyncio
async def test_objective_missing_package_refunds_atomically(installed):
    app, root = installed
    _sk, seller, bk, buyer, listing, package = await market(app, root)
    created = await call(app, 'orders.create', _intent(listing), key=bk, subject=buyer)
    order = created.data['order']
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('DELETE FROM store_packages WHERE id=?', (package['id'],), write=True)
    funded = await call(
        app,
        'orders.fund',
        {
            'order_id': order['id'],
            'order_digest': order['order_digest'],
            'total_price_minor': 5_000_000,
            'currency_id': 'primary',
        },
        key=bk,
        subject=buyer,
    )
    assert funded.status == 'ok' and funded.data['order']['state'] == 'refunded', wire(funded)
    async with app.metadata.transaction(write=False) as tx:
        from msg.plugins.money import _balance

        assert _balance(tx, buyer) == 20_000_000 and _balance(tx, seller) == 0
        assert tx.one('SELECT COUNT(*) FROM store_deliveries')[0] == 0
        assert tx.one('SELECT COUNT(*) FROM order_settlements')[0] == 1


@pytest.mark.asyncio
async def test_last_quantity_and_timeout_do_not_double_spend(installed):
    app, root = installed
    _sk, seller, bk, buyer, listing, _ = await market(
        app, root, mode='service', kind='service', quantity=1
    )
    results = await asyncio.gather(*(buy(app, bk, buyer, listing) for _ in range(2)))
    assert sum(r.status == 'ok' for r in results) == 1
    assert [r.error.code for r in results if r.error] == ['quantity_unavailable']
    order = next(r.data['order'] for r in results if r.status == 'ok')
    assert order['state'] == 'funded'
    app.clock = lambda: NOW + timedelta(days=2)
    from msg.market.escrow import resolve_due

    await resolve_due(app)
    await resolve_due(app)
    async with app.metadata.transaction(write=False) as tx:
        from msg.plugins.money import _balance

        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order['id'],))[0] == 'refunded'
        assert _balance(tx, buyer) == 20_000_000 and _balance(tx, seller) == 0
        assert tx.one('SELECT COUNT(*) FROM order_settlements')[0] == 1
