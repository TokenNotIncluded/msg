"""Sale checkout protects price snapshots, escrow, and private order views."""
import asyncio
import re

import pytest

from msg.admin.money import apply_money
from msg.core.codec import b64, wire
from msg.core.errors import Failure
from test_service import call, register


async def _sale(app, seller_key, seller, *, price=5_000_000, quantity=2):
    listing = await call(app, 'store.listing_create', {
        'name': 'bundle', 'item_kind': 'bundle', 'price_minor': price,
        'currency_id': 'primary', 'quantity': quantity,
        'delivery_mode': 'managed_instant', 'escrow_policy': 'escrow-v1',
        'dispute_policy': 'dispute-v1', 'terms': 'Two immutable items.'},
        key=seller_key, subject=seller)
    assert listing.status == 'ok', wire(listing)
    listing_id = listing.resources[0].id
    file = await call(app, 'content.file_put', {
        'parent': '/@order-seller/files', 'name': 'hello.txt',
        'data': b64(b'delivery-ok\n'), 'media_type': 'text/plain'},
        key=seller_key, subject=seller)
    assert file.status == 'ok', wire(file)
    deposited = await call(app, 'store.package_deposit', {
        'listing_id': listing_id,
        'listing_revision': listing.resources[0].revision,
        'manifest': {'text': 'msg.lmm.best store selftest'},
        'payload_refs': [{'id': file.resources[0].id,
                          'revision': file.resources[0].revision}]},
        key=seller_key, subject=seller)
    assert deposited.status == 'ok', wire(deposited)
    active = await call(app, 'store.listing_update', {
        'id': listing_id, 'package_ref': deposited.data['package']['id'],
        'state': 'active'}, key=seller_key, subject=seller,
        expected=((listing_id, listing.data['generation']),))
    assert active.status == 'ok', wire(active)
    return active.data['listing'], deposited.data['package']


def _intent(listing, *, quantity=1, price=None):
    return {'listing_id': listing['listing_id'],
            'listing_revision': listing['listing_revision'],
            'quantity': quantity, 'currency_id': 'primary',
            'total_price_minor': price if price is not None else
                                 listing['price_minor'] * quantity}


@pytest.mark.asyncio
async def test_buy_funds_escrow_once_and_does_not_claim_delivery(installed):
    app, root = installed
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'order-buyer')
    listing, package = await _sale(app, seller_key, seller)
    await apply_money(app, root, action='mint', operator='test-console',
                      amount_minor=5_000_000)
    await apply_money(app, root, action='transfer', operator='test-console',
                      subject_id=buyer, amount_minor=5_000_000)
    args = _intent(listing)
    bought = await call(app, 'orders.buy', args, key=buyer_key,
                        subject=buyer, rid='buy-one')
    assert bought.status == 'ok', wire(bought)
    order = bought.data['order']
    assert re.fullmatch(r'ord_[a-z2-7]{32}', order['id'])
    assert order['state'] == 'funded'
    assert order['listing_revision'] == listing['listing_revision']
    assert order['package_revision'] == package['revision']
    assert order['package_digest'] == package['digest']
    assert order['terms_digest'] == listing['terms_digest']
    assert order['delivery_target'] == {'subject_id': buyer, 'channel': 'site'}
    assert order['delivered_at'] is None and order['settled_at'] is None
    assert bought.data['payment']['body']['amount_minor'] == 5_000_000
    replay = await call(app, 'orders.buy', args, key=buyer_key,
                        subject=buyer, rid='buy-one')
    assert replay.status == 'ok' and replay.replayed
    assert replay.data['order']['id'] == order['id']
    async with app.metadata.transaction(write=False) as tx:
        row = tx.one('SELECT escrow_subject,state FROM store_orders WHERE id=?',
                     (order['id'],))
        assert row[1] == 'funded'
        assert tx.one('SELECT kind,subject_id,source_id FROM ledger_accounts WHERE id=?',
                      (row[0],)) == ('order_escrow', None, order['id'])
        assert tx.one('SELECT 1 FROM identities WHERE id=?', (row[0],)) is None
        assert tx.one('SELECT COUNT(*) FROM credentials WHERE subject=?',
                      (row[0],))[0] == 0
        assert tx.one('SELECT COUNT(*) FROM money_ledger WHERE credit_account=?',
                      (row[0],))[0] == 1
    assert (await call(app, 'money.balance', {}, key=buyer_key,
                       subject=buyer)).data['balance_minor'] == 0
    assert (await call(app, 'money.balance', {}, key=seller_key,
                       subject=seller)).data['balance_minor'] == 0
    assert (await call(app, 'money.state', {})).data['total_supply_minor'] == 5_000_000


@pytest.mark.asyncio
async def test_order_views_are_private_and_reads_are_side_effect_free(installed):
    app, root = installed
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'order-buyer')
    outsider_key, outsider, _ = await register(app, 'order-outsider')
    listing, _ = await _sale(app, seller_key, seller)
    await apply_money(app, root, action='mint', operator='test-console',
                      amount_minor=5_000_000)
    await apply_money(app, root, action='transfer', operator='test-console',
                      subject_id=buyer, amount_minor=5_000_000)
    bought = await call(app, 'orders.buy', _intent(listing),
                        key=buyer_key, subject=buyer)
    assert bought.status == 'ok', wire(bought)
    order_id = bought.data['order']['id']
    async with app.metadata.transaction(write=False) as tx:
        before = (tx.one('SELECT COUNT(*) FROM store_orders')[0],
                  tx.one('SELECT COUNT(*) FROM money_ledger')[0],
                  tx.one('SELECT COUNT(*) FROM events')[0])
    seller_view = await call(app, 'orders.get', {'order_id': order_id},
                             key=seller_key, subject=seller)
    assert seller_view.status == 'ok', wire(seller_view)
    assert 'delivery_target' not in seller_view.data['order']
    assert 'receipt_refs' not in seller_view.data['order']
    assert 'payment_intent_digest' not in seller_view.data['order']
    buyer_view = await call(app, 'orders.get', {'order_id': order_id},
                            key=buyer_key, subject=buyer)
    assert buyer_view.data['order']['delivery_target']['subject_id'] == buyer
    paid = await call(app, 'orders.payment', {'order_id': order_id},
                      key=buyer_key, subject=buyer)
    assert paid.data['payment']['escrow_balance_minor'] == 5_000_000
    assert paid.data['payment']['receipt']['body']['transaction_id'] == \
           bought.data['payment']['body']['transaction_id']
    seller_payment = await call(app, 'orders.payment', {'order_id': order_id},
                               key=seller_key, subject=seller)
    assert 'receipt' not in seller_payment.data['payment']
    for actor_key, actor in ((outsider_key, outsider), (None, None)):
        hidden = await call(app, 'orders.get', {'order_id': order_id},
                            key=actor_key, subject=actor)
        missing = await call(app, 'orders.get',
                             {'order_id': 'ord_' + 'a'*32},
                             key=actor_key, subject=actor)
        assert hidden.error.code == missing.error.code == 'order_not_found'
    assert not (await call(app, 'orders.list', {}, key=outsider_key,
                           subject=outsider)).data['orders']
    assert len((await call(app, 'orders.list', {'role': 'buy'},
                           key=buyer_key, subject=buyer)).data['orders']) == 1
    async with app.metadata.transaction(write=False) as tx:
        after = (tx.one('SELECT COUNT(*) FROM store_orders')[0],
                 tx.one('SELECT COUNT(*) FROM money_ledger')[0],
                 tx.one('SELECT COUNT(*) FROM events')[0])
    assert after == before


@pytest.mark.asyncio
async def test_price_race_quantity_and_failed_payment_roll_back(installed):
    app, root = installed
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'order-buyer')
    listing, _ = await _sale(app, seller_key, seller, quantity=1)
    insufficient = await call(app, 'orders.buy', _intent(listing),
                              key=buyer_key, subject=buyer)
    assert insufficient.error.code == 'insufficient_funds'
    await apply_money(app, root, action='mint', operator='test-console',
                      amount_minor=10_000_000)
    await apply_money(app, root, action='transfer', operator='test-console',
                      subject_id=buyer, amount_minor=10_000_000)
    changed = await call(app, 'store.listing_update', {
        'id': listing['listing_id'], 'price_minor': 6_000_000,
        'terms': 'New terms'}, key=seller_key, subject=seller,
        expected=((listing['listing_id'], 2),))
    assert changed.status == 'ok', wire(changed)
    stale = await call(app, 'orders.buy', _intent(listing),
                       key=buyer_key, subject=buyer)
    assert stale.error.code == 'listing_revision_conflict'
    current = changed.data['listing']
    low = await call(app, 'orders.buy', _intent(current, price=5_000_000),
                     key=buyer_key, subject=buyer)
    assert low.error.code == 'price_changed'
    bought = await call(app, 'orders.buy', _intent(current),
                        key=buyer_key, subject=buyer)
    assert bought.status == 'ok', wire(bought)
    second = await call(app, 'orders.buy', _intent(current),
                        key=buyer_key, subject=buyer)
    assert second.error.code == 'quantity_unavailable'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM store_orders')[0] == 1
        assert tx.one("SELECT COUNT(*) FROM money_ledger WHERE reference LIKE 'order_fund:%'")[0] == 1
        assert tx.one("SELECT COUNT(*) FROM ledger_accounts WHERE kind='order_escrow'")[0] == 1


@pytest.mark.asyncio
async def test_last_unit_competition_and_posted_debit_failure_roll_back(installed,
                                                                       monkeypatch):
    app, root = installed
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'order-buyer')
    listing, _ = await _sale(app, seller_key, seller, quantity=1)
    await apply_money(app, root, action='mint', operator='test-console',
                      amount_minor=10_000_000)
    await apply_money(app, root, action='transfer', operator='test-console',
                      subject_id=buyer, amount_minor=10_000_000)
    from msg.plugins import orders
    original = orders._post_transfer

    def abort_after_debit(*args, **kwargs):
        original(*args, **kwargs)
        raise Failure('forced_rollback')

    monkeypatch.setattr(orders, '_post_transfer', abort_after_debit)
    failed = await call(app, 'orders.buy', _intent(listing),
                        key=buyer_key, subject=buyer, rid='force-rollback')
    assert failed.error.code == 'forced_rollback'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM store_orders')[0] == 0
        assert tx.one("SELECT COUNT(*) FROM ledger_accounts WHERE kind='order_escrow'")[0] == 0
        assert tx.one("SELECT COUNT(*) FROM money_ledger WHERE reference LIKE 'order_fund:%'")[0] == 0
    assert (await call(app, 'money.balance', {}, key=buyer_key,
                       subject=buyer)).data['balance_minor'] == 10_000_000
    monkeypatch.setattr(orders, '_post_transfer', original)
    async def buy(request_id):
        return await call(app, 'orders.buy', _intent(listing),
                          key=buyer_key, subject=buyer, rid=request_id)
    results = await asyncio.gather(buy('last-unit-a'), buy('last-unit-b'))
    assert sorted(r.status for r in results) == ['error', 'ok']
    assert next(r for r in results if r.status == 'error').error.code == 'quantity_unavailable'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM store_orders')[0] == 1
        assert tx.one("SELECT COUNT(*) FROM money_ledger WHERE reference LIKE 'order_fund:%'")[0] == 1


@pytest.mark.asyncio
async def test_buyer_can_cancel_funded_order_once_and_recover_full_balance(installed):
    app, root = installed
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'order-buyer')
    other_key, other, _ = await register(app, 'order-other')
    listing, _ = await _sale(app, seller_key, seller)
    await apply_money(app, root, action='mint', operator='test-console',
                      amount_minor=5_000_000)
    await apply_money(app, root, action='transfer', operator='test-console',
                      subject_id=buyer, amount_minor=5_000_000)
    bought = await call(app, 'orders.buy', _intent(listing),
                        key=buyer_key, subject=buyer)
    assert bought.status == 'ok', wire(bought)
    order_id = bought.data['order']['id']
    for key, subject in ((seller_key, seller), (other_key, other)):
        denied = await call(app, 'orders.cancel', {'order_id': order_id},
                            key=key, subject=subject)
        absent = await call(app, 'orders.cancel', {'order_id': 'ord_'+'a'*32},
                            key=key, subject=subject)
        assert denied.error.code == absent.error.code == 'order_not_found'
    async def cancel(request_id):
        return await call(app, 'orders.cancel', {'order_id': order_id},
                          key=buyer_key, subject=buyer, rid=request_id)
    results = await asyncio.gather(cancel('cancel-a'), cancel('cancel-b'))
    assert sorted(r.status for r in results) == ['error', 'ok']
    cancelled = next(r for r in results if r.status == 'ok')
    winning_request = cancelled.request_id
    assert next(r for r in results if r.status == 'error').error.code == \
           'order_not_cancellable'
    assert cancelled.status == 'ok', wire(cancelled)
    assert cancelled.data['order']['state'] == 'refunded'
    assert cancelled.data['order']['payment_status'] == 'refunded'
    assert cancelled.data['refund']['body']['kind'] == 'refund'
    assert cancelled.data['refund']['body']['amount_minor'] == 5_000_000
    replay = await call(app, 'orders.cancel', {'order_id': order_id},
                        key=buyer_key, subject=buyer, rid=winning_request)
    assert replay.status == 'ok' and replay.replayed
    assert replay.data['refund'] == cancelled.data['refund']
    twice = await call(app, 'orders.cancel', {'order_id': order_id},
                       key=buyer_key, subject=buyer, rid='cancel-again')
    assert twice.error.code == 'order_not_cancellable'
    assert (await call(app, 'money.balance', {}, key=buyer_key,
                       subject=buyer)).data['balance_minor'] == 5_000_000
    assert (await call(app, 'money.balance', {}, key=seller_key,
                       subject=seller)).data['balance_minor'] == 0
    payment = await call(app, 'orders.payment', {'order_id': order_id},
                         key=buyer_key, subject=buyer)
    assert payment.data['payment']['status'] == 'refunded'
    assert payment.data['payment']['escrow_balance_minor'] == 0
    assert len(payment.data['payment']['receipts']) == 2
    assert payment.data['payment']['receipts'][1]['body']['kind'] == 'refund'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM money_ledger WHERE reference=?",
                      ('order_refund:' + order_id,))[0] == 1
        assert tx.one("SELECT COUNT(*) FROM events WHERE body LIKE ?",
                      ('%orders.cancel%',))[0] == 1


@pytest.mark.asyncio
async def test_cancel_refuses_delivery_and_rolls_back_after_refund_post(installed,
                                                                        monkeypatch):
    app, root = installed
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'order-buyer')
    listing, _ = await _sale(app, seller_key, seller)
    await apply_money(app, root, action='mint', operator='test-console',
                      amount_minor=10_000_000)
    await apply_money(app, root, action='transfer', operator='test-console',
                      subject_id=buyer, amount_minor=10_000_000)
    first = await call(app, 'orders.buy', _intent(listing),
                       key=buyer_key, subject=buyer)
    second = await call(app, 'orders.buy', _intent(listing),
                        key=buyer_key, subject=buyer)
    assert first.status == second.status == 'ok'
    first_id, second_id = first.data['order']['id'], second.data['order']['id']
    from msg.market import escrow
    original = escrow._post_transfer

    def abort_after_refund(*args, **kwargs):
        receipt = original(*args, **kwargs)
        if kwargs.get('kind') == 'refund':
            raise Failure('forced_refund_rollback')
        return receipt

    monkeypatch.setattr(escrow, '_post_transfer', abort_after_refund)
    failed = await call(app, 'orders.cancel', {'order_id': first_id},
                        key=buyer_key, subject=buyer)
    assert failed.error.code == 'forced_refund_rollback'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (first_id,))[0] == 'funded'
        assert tx.one("SELECT COUNT(*) FROM money_ledger WHERE reference=?",
                      ('order_refund:' + first_id,))[0] == 0
    monkeypatch.setattr(escrow, '_post_transfer', original)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('''INSERT INTO store_deliveries
            (id,order_id,recipient_subject,kind,payload_refs,manifest,
             package_digest,delivery_digest,channel,state,prepared_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            ('del_test_prepared', first_id, buyer, 'bundle', '[]', '{}',
             'package-test', 'delivery-test', 'site', 'prepared',
             '2026-09-27T00:00:00Z'), write=True)
    prepared = await call(app, 'orders.cancel', {'order_id': first_id},
                          key=buyer_key, subject=buyer)
    assert prepared.error.code == 'order_not_cancellable'
    # Simulate a future delivery transition. The cancellation guard must keep
    # the escrow intact even when a delivery module has changed order state.
    async with app.metadata.transaction(write=True) as tx:
        tx.execute("UPDATE store_orders SET state='delivered',delivered_at=? WHERE id=?",
                   ('2026-09-27T00:00:00Z', second_id), write=True)
    delivered = await call(app, 'orders.cancel', {'order_id': second_id},
                           key=buyer_key, subject=buyer)
    assert delivered.error.code == 'order_not_cancellable'
    assert (await call(app, 'money.balance', {}, key=buyer_key,
                       subject=buyer)).data['balance_minor'] == 0
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM money_ledger WHERE kind='refund'")[0] == 0
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (second_id,))[0] == 'delivered'


@pytest.mark.asyncio
async def test_buy_rejects_unimplemented_delivery_mode_before_escrow(installed):
    app, root = installed
    seller_key, seller, _ = await register(app, 'order-service-seller')
    buyer_key, buyer, _ = await register(app, 'order-service-buyer')
    listing = await call(app, 'store.listing_create', {
        'name': 'service-draft', 'item_kind': 'service',
        'price_minor': 5_000_000, 'currency_id': 'primary', 'quantity': 1,
        'delivery_mode': 'service', 'escrow_policy': 'escrow-v1',
        'dispute_policy': 'dispute-v1', 'terms': 'Future service'},
        key=seller_key, subject=seller)
    assert listing.status == 'ok', wire(listing)
    active = await call(app, 'store.listing_update', {
        'id': listing.resources[0].id, 'state': 'active'},
        key=seller_key, subject=seller,
        expected=((listing.resources[0].id, listing.data['generation']),))
    assert active.status == 'ok', wire(active)
    await apply_money(app, root, action='mint', operator='test-console',
                      amount_minor=5_000_000)
    await apply_money(app, root, action='transfer', operator='test-console',
                      subject_id=buyer, amount_minor=5_000_000)
    denied = await call(app, 'orders.buy', _intent(active.data['listing']),
                        key=buyer_key, subject=buyer)
    assert denied.error.code == 'order_delivery_mode_unsupported'
    assert (await call(app, 'money.balance', {}, key=buyer_key,
                       subject=buyer)).data['balance_minor'] == 5_000_000
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM store_orders')[0] == 0
