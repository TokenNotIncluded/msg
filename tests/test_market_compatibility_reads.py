"""Compatibility projections preserve legacy authority and signed financial facts."""

import pytest
from test_market_redemption import setup_offer
from test_service import NOW, call, register

from msg.core.codec import wire
from msg.core.errors import Failure
from msg.market.compatibility import purchase_order
from msg.market.ledger import post_escrow_release, post_transfer
from msg.plugins.offers import _purchase


async def snapshot(app):
    async with app.metadata.transaction(write=False) as tx:
        return {
            table: tx.rows('SELECT * FROM ' + table + ' ORDER BY 1')
            for table in (
                'money_purchases',
                'server_offers',
                'money_ledger',
                'ledger_accounts',
                'resource_entitlements',
                'store_orders',
                'store_deliveries',
                'events',
                'audit',
            )
        }


@pytest.mark.asyncio
async def test_offer_listing_v2_projection_preserves_v1_and_checks_revision(installed):
    app, root = installed
    _, _, args, _ = await setup_offer(app, root)
    before = await snapshot(app)
    old = await call(app, 'money.offers', {})
    new = await call(app, 'money.offers', {}, contract_version=2)
    assert new.status == 'ok', wire(new)
    assert new.data['offers'] == old.data['offers']
    listing = new.data['listings'][0]
    assert listing['listing_id'] == args['offer_id']
    assert (
        listing['source'] == 'server_offer' and listing['escrow_policy'] == 'legacy-entitlement-v1'
    )
    get = await call(
        app,
        'store.listing_get',
        {'id': args['offer_id'], 'source': 'server_offer'},
        contract_version=2,
    )
    assert get.data['listing'] == listing
    wrong = await call(
        app,
        'store.listing_get',
        {'id': args['offer_id'], 'source': 'server_offer', 'revision': 'nonexistent'},
        contract_version=2,
    )
    assert wrong.error.code == 'listing_not_found'
    native = await call(app, 'store.listing_get', {'id': args['offer_id']}, contract_version=2)
    assert native.status == 'ok'
    assert native.data['listing']['listing_id'] == args['offer_id']
    assert native.data['listing']['server_offer']['price_revision'] == args['price_revision']
    assert await snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['pending', 'settled', 'refunded'])
async def test_purchase_order_projection_zero_writes_and_owner_only(installed, outcome):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    outsider_key, outsider, _ = await register(app, 'compat-outsider')
    created = await call(
        app, 'money.redeem', {**args, 'defer': True}, key=key, subject=owner, contract_version=2
    )
    pid = created.data['purchase']['id']
    if outcome != 'pending':
        op = 'money.purchase_settle' if outcome == 'settled' else 'money.purchase_cancel'
        result = await call(app, op, {'purchase_id': pid}, key=key, subject=owner)
        assert result.status == 'ok', wire(result)
    before = await snapshot(app)
    legacy = await call(app, 'money.purchase_get', {'purchase_id': pid}, key=key, subject=owner)
    new = await call(
        app, 'money.purchase_get', {'purchase_id': pid}, key=key, subject=owner, contract_version=2
    )
    assert new.status == 'ok', wire(new)
    assert new.data['purchase'] == legacy.data['purchase']
    order = new.data['order']
    assert order['id'] == pid and order['source'] == 'legacy_purchase'
    assert order['state'] == ('funded' if outcome == 'pending' else outcome)
    assert order['accepted_at'] is None and order['delivery_id'] is None
    assert order['offer_snapshot'] == legacy.data['purchase']['offer_snapshot']
    unified = await call(
        app,
        'orders.get',
        {'order_id': pid, 'source': 'legacy_purchase'},
        key=key,
        subject=owner,
        contract_version=2,
    )
    assert unified.data['order'] == order
    for id_ in (pid, 'nonexistent'):
        denied = await call(
            app,
            'orders.get',
            {'order_id': id_, 'source': 'legacy_purchase'},
            key=outsider_key,
            subject=outsider,
            contract_version=2,
        )
        assert denied.error.code == 'order_not_found'
    index = await call(
        app, 'orders.list', {'role': 'buy'}, key=key, subject=owner, contract_version=2
    )
    assert index.data['orders'] == (order,)
    absent_status = 'completed' if outcome == 'pending' else 'open'
    empty = await call(
        app, 'orders.list', {'status': absent_status}, key=key, subject=owner, contract_version=2
    )
    assert len(empty.data['orders']) == 0
    seller = await call(
        app, 'orders.list', {'role': 'sell'}, key=key, subject=owner, contract_version=2
    )
    assert len(seller.data['orders']) == 0
    assert await snapshot(app) == before
    # The read adapter never makes a purchase reachable by store mutation code.
    denied = await call(app, 'orders.cancel', {'order_id': pid}, key=key, subject=owner)
    assert denied.error.code == 'order_not_found'


@pytest.mark.asyncio
async def test_purchase_release_boundary_and_projection_preflight_fail_closed(installed):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    created = await call(
        app, 'money.redeem', {**args, 'defer': True}, key=key, subject=owner, contract_version=2
    )
    pid = created.data['purchase']['id']
    before = await snapshot(app)
    async with app.metadata.transaction(write=True) as tx:
        p = _purchase(tx, pid, owner)
        with pytest.raises(Failure, match='legacy_purchase_invalid'):
            purchase_order(tx, {**p, 'state': 'unknown'})
        with pytest.raises(Failure, match='legacy_purchase_invalid'):
            purchase_order(tx, {**p, 'offer_snapshot': {}})
        with pytest.raises(Failure, match='legacy_purchase_invalid'):
            purchase_order(
                tx, {**p, 'offer_snapshot': {**p['offer_snapshot'], 'provider_version': 999}}
            )
        kwargs = {
            'recipient': owner,
            'amount': p['total_minor'],
            'actor': owner,
            'request_id': 'bad-release',
            'now': NOW,
            'receipt_signer': app.receipt_signer,
            'reference': 'invalid',
            'kind': 'refund',
        }
        with pytest.raises(Failure, match='escrow_release_forbidden'):
            post_transfer(tx, sender=p['escrow_account'], **kwargs)
        with pytest.raises(Failure, match='escrow_account_mismatch'):
            post_escrow_release(
                tx,
                escrow_account=p['escrow_account'],
                source_id='wrong',
                account_kind='purchase_escrow',
                buyer=owner,
                seller='u_root',
                **kwargs,
            )
        with pytest.raises(Failure, match='escrow_recipient_mismatch'):
            post_escrow_release(
                tx,
                escrow_account=p['escrow_account'],
                source_id=pid,
                account_kind='purchase_escrow',
                buyer=owner,
                seller='u_root',
                **{**kwargs, 'recipient': 'unrelated'},
            )
    assert await snapshot(app) == before


@pytest.mark.asyncio
async def test_v2_native_reads_preserve_current_order_policy_and_authorization(installed):
    from test_market_lifecycle import market
    from test_orders import _intent

    app, root = installed
    sk, seller, bk, buyer, listing, _ = await market(app, root)
    bought = await call(
        app, 'orders.buy', _intent(listing), key=bk, subject=buyer, contract_version=4
    )
    assert bought.status == 'ok', wire(bought)
    oid = bought.data['order']['id']
    from msg.admin.money import apply_offer
    from msg.plugins.hosting_capacity import ENTITLEMENT_KIND

    quote = await apply_offer(
        app,
        root,
        action='set',
        operator='test',
        offer_id='mixed-offer',
        fields={
            'resource_kind': 'website',
            'unit': 'byte',
            'price_minor': 2,
            'min_quantity': 1,
            'max_quantity': 40,
            'entitlement_kind': ENTITLEMENT_KIND,
            'duration_seconds': None,
        },
    )
    purchase = await call(
        app,
        'money.redeem',
        {
            'offer_id': 'mixed-offer',
            'quantity': 1,
            'currency_id': 'primary',
            'price_revision': quote['offer']['price_revision'],
        },
        key=bk,
        subject=buyer,
    )
    assert purchase.status == 'ok', wire(purchase)
    pid = purchase.data['purchase']['id']
    before = await snapshot(app)
    for key, viewer in ((bk, buyer), (sk, seller)):
        old = await call(app, 'orders.get', {'order_id': oid}, key=key, subject=viewer)
        new = await call(
            app, 'orders.get', {'order_id': oid}, key=key, subject=viewer, contract_version=2
        )
        assert new.status == 'ok' and new.data == old.data
        assert new.data['order']['contract_version'] == 4
    native_listing = await call(app, 'store.listing_get', {'id': listing['listing_id']})
    compatible_listing = await call(
        app, 'store.listing_get', {'id': listing['listing_id']}, contract_version=2
    )
    assert compatible_listing.data == native_listing.data
    index = await call(app, 'orders.list', {}, key=bk, subject=buyer, contract_version=2)
    assert index.status == 'ok'
    assert {o['id'] for o in index.data['orders']} == {oid, pid}
    assert {o['source'] for o in index.data['orders']} == {'store_order', 'legacy_purchase'}
    limited = await call(
        app, 'orders.list', {'limit': 1}, key=bk, subject=buyer, contract_version=2
    )
    assert limited.data['orders'] == index.data['orders'][:1]
    assert await snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['money.purchase_get', 'orders.get', 'orders.list'])
async def test_compatibility_reads_require_current_account_scope(installed, operation):
    from test_market_account_scope import restricted_key

    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    created = await call(app, 'money.redeem', args, key=key, subject=owner)
    pid = created.data['purchase']['id']
    args = (
        {'purchase_id': pid}
        if operation == 'money.purchase_get'
        else {'order_id': pid, 'source': 'legacy_purchase'}
        if operation == 'orders.get'
        else {}
    )
    denied_key = await restricted_key(app, key, owner, operation, 2)
    allowed_key = await restricted_key(app, key, owner, operation, 2, account=True)
    before = await snapshot(app)
    denied = await call(app, operation, args, key=denied_key, subject=owner, contract_version=2)
    assert denied.error.code == 'credential_ceiling'
    allowed = await call(app, operation, args, key=allowed_key, subject=owner, contract_version=2)
    assert allowed.status == 'ok', wire(allowed)
    assert await snapshot(app) == before
