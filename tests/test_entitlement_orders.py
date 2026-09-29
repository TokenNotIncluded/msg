"""Explicit synchronous redemption shares Order storage without faking buyer acceptance."""
import pytest

from msg.admin.market_check import inspect_clearing
from msg.core.codec import digest, wire
from msg.core.errors import Failure
from msg.market.policy import contract
from msg.plugins import offers
from msg.plugins.money import _balance, _supply
from test_market_redemption import setup_offer
from test_service import call


async def intent(app, args):
    quote = (await call(app, 'money.offers', {})).data['offers'][0]
    async with app.metadata.transaction(write=False) as tx:
        resource = await tx.resource(args['offer_id'])
    return {**args, 'listing_revision': resource.revision,
            'offer_snapshot_digest': digest(quote), 'total_price_minor': quote['price_minor'] * args['quantity'],
            'settlement_policy': 'deterministic-entitlement-v1'}


@pytest.mark.asyncio
async def test_explicit_entitlement_redemption_uses_one_order_and_conserves_supply(installed):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    signed = await intent(app, args)
    result = await call(app, 'money.redeem', signed, key=key, subject=owner, contract_version=3,
                        rid='order-redemption')
    assert result.status == 'ok', wire(result)
    order = result.data['order']
    assert order['id'].startswith('ord_') and order['state'] == 'settled'
    assert order['contract_version'] == 5 and order['delivered_at'] is None
    repeated = await call(app, 'money.redeem', signed, key=key, subject=owner, contract_version=3,
                          rid='order-redemption')
    assert repeated.replayed and repeated.data == result.data
    conflict = await call(app, 'money.redeem', {**signed, 'quantity': 6, 'total_price_minor': 12},
                          key=key, subject=owner, contract_version=3, rid='order-redemption')
    assert conflict.error.code == 'idempotency_conflict' and not conflict.replayed
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_purchases')[0] == 0
        assert tx.one('SELECT COUNT(*) FROM store_orders')[0] == 1
        assert tx.one('SELECT COUNT(*) FROM store_deliveries')[0] == 0
        assert tx.one('SELECT COUNT(*) FROM resource_entitlements')[0] == 1
        assert tx.one('SELECT COUNT(*) FROM order_settlements')[0] == 1
        assert tx.one("SELECT COUNT(*) FROM order_transitions WHERE body LIKE '%accepted%'")[0] == 0
        locked = contract(tx, order['id'])
        assert _balance(tx, owner) == 90 and _balance(tx, 'u_root') == 10
        assert _balance(tx, locked['escrow_subject']) == 0 and _supply(tx) == 100
        inspect_clearing(app, tx)
    read = await call(app, 'orders.get', {'order_id': order['id']}, key=key, subject=owner)
    assert read.status == 'ok' and read.data['order'] == result.data['order']
    cancel = await call(app, 'orders.cancel', {'order_id': order['id']}, key=key, subject=owner,
                        contract_version=2)
    assert cancel.error.code == 'order_not_cancellable'


@pytest.mark.asyncio
async def test_grant_failure_rolls_back_shared_order_funding_and_release(installed, monkeypatch):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    signed = await intent(app, args)
    provider = offers.ENTITLEMENT_FULFILLERS[offers.ENTITLEMENT_KIND]
    def fail(*args):
        provider[3](*args)
        raise Failure('entitlement_order_injected_failure')
    monkeypatch.setitem(offers.ENTITLEMENT_FULFILLERS, offers.ENTITLEMENT_KIND, (*provider[:3], fail))
    tables = ('store_orders', 'order_contracts', 'order_settlements', 'order_transitions',
              'money_ledger', 'ledger_accounts', 'resource_entitlements', 'events', 'audit')
    async with app.metadata.transaction(write=False) as tx:
        before = {table: tx.rows('SELECT * FROM ' + table + ' ORDER BY 1') for table in tables}
    result = await call(app, 'money.redeem', signed, key=key, subject=owner, contract_version=3)
    assert result.error.code == 'entitlement_order_injected_failure', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert {table: tx.rows('SELECT * FROM ' + table + ' ORDER BY 1') for table in tables} == before


@pytest.mark.asyncio
async def test_explicit_intent_and_legacy_pending_are_not_interchangeable(installed):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    pending = await call(app, 'money.redeem', {**args, 'defer': True}, key=key, subject=owner, contract_version=2)
    assert pending.status == 'ok'
    original = pending.data['purchase']
    signed = await intent(app, args)
    for changed in ({'settlement_policy': 'buyer-acceptance'}, {'offer_snapshot_digest': 'sha256:'+'0'*64},
                    {'total_price_minor': 11}, {'listing_revision': 'wrong-revision'}):
        result = await call(app, 'money.redeem', {**signed, **changed}, key=key, subject=owner,
                            contract_version=3)
        assert result.status == 'error'
    missing = {k: v for k, v in signed.items() if k != 'settlement_policy'}
    assert (await call(app, 'money.redeem', missing, key=key, subject=owner, contract_version=3)).status == 'error'
    result = await call(app, 'money.redeem', signed, key=key, subject=owner, contract_version=3)
    assert result.status == 'ok', wire(result)
    old = await call(app, 'money.purchase_get', {'purchase_id': original['id']}, key=key, subject=owner)
    assert old.data['purchase'] == original
    cancelled = await call(app, 'money.purchase_cancel', {'purchase_id': original['id']}, key=key, subject=owner)
    assert cancelled.status == 'ok' and cancelled.data['purchase']['state'] == 'refunded'


@pytest.mark.asyncio
async def test_concurrent_redemptions_cannot_spend_last_balance_twice(installed):
    import asyncio
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    signed = await intent(app, {**args, 'quantity': 40})
    results = await asyncio.gather(*[call(app, 'money.redeem', signed, key=key, subject=owner,
                                        contract_version=3, rid=rid) for rid in ('last-a', 'last-b')])
    assert sorted(r.status for r in results) == ['error', 'ok']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM store_orders')[0] == 1
        assert tx.one('SELECT COUNT(*) FROM resource_entitlements')[0] == 1
        assert _balance(tx, owner) == 20 and _balance(tx, 'u_root') == 80 and _supply(tx) == 100
        inspect_clearing(app, tx)


@pytest.mark.asyncio
async def test_revoked_key_cannot_replay_but_historical_signed_order_still_verifies(installed):
    from test_market_account_scope import restricted_key
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    scoped = await restricted_key(app, key, owner, 'money.redeem', 3, account=True)
    signed = await intent(app, args)
    original = await call(app, 'money.redeem', signed, key=scoped, subject=owner,
                          contract_version=3, rid='revocable-redemption')
    assert original.status == 'ok', wire(original)
    revoked = await call(app, 'identity.key_revoke', {'key_id': scoped.key_id}, key=key, subject=owner)
    assert revoked.status == 'ok', wire(revoked)
    denied = await call(app, 'money.redeem', signed, key=scoped, subject=owner,
                        contract_version=3, rid='revocable-redemption')
    assert denied.status == 'error' and not denied.replayed
    read = await call(app, 'orders.get', {'order_id': original.data['order']['id']}, key=key, subject=owner)
    assert read.status == 'ok', wire(read)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM resource_entitlements')[0] == 1
        assert _balance(tx, owner) == 90


@pytest.mark.asyncio
async def test_recomputed_contract_digest_cannot_forge_signed_settlement_consent(installed):
    from msg.core.codec import loads
    from msg.market.policy import validate_projection
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    result = await call(app, 'money.redeem', await intent(app, args), key=key, subject=owner,
                        contract_version=3)
    assert result.status == 'ok', wire(result)
    oid = result.data['order']['id']
    async with app.metadata.transaction(write=True) as tx:
        locked = loads(tx.one('SELECT body FROM order_contracts WHERE order_id=?', (oid,))[0])
        locked['redemption_request']['source'] = 'manual'
        # Restore validation receives the candidate body before trusting its digest.
        with pytest.raises(Failure):
            validate_projection(tx, locked)
        assert contract(tx, oid)['redemption_request']['source'] != 'manual'


@pytest.mark.asyncio
async def test_unavailable_provider_version_rejects_before_funding(installed, monkeypatch):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    signed = await intent(app, args)
    provider = offers.ENTITLEMENT_FULFILLERS[offers.ENTITLEMENT_KIND]
    monkeypatch.setitem(offers.ENTITLEMENT_FULFILLERS, offers.ENTITLEMENT_KIND,
                        (provider[0], provider[1], 2, provider[3]))
    result = await call(app, 'money.redeem', signed, key=key, subject=owner, contract_version=3)
    assert result.error.code == 'offer_provider_unavailable'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM store_orders')[0] == 0
        assert _balance(tx, owner) == 100 and _supply(tx) == 100


@pytest.mark.asyncio
async def test_order_writer_preserves_full_byte_quantity_above_signed_integer32(installed):
    from msg.admin.money import apply_money, apply_offer
    app, root = installed
    key, owner, args, fields = await setup_offer(app, root)
    quantity = 2**31 + 1
    quote = await apply_offer(app, root, action='set', operator='test', offer_id=args['offer_id'],
                              fields={**fields, 'price_minor': 1, 'max_quantity': quantity})
    await apply_money(app, root, action='mint', operator='test', amount_minor=quantity)
    await apply_money(app, root, action='transfer', operator='test', amount_minor=quantity, subject_id=owner)
    signed = await intent(app, {**args, 'quantity': quantity, 'price_revision': quote['offer']['price_revision']})
    result = await call(app, 'money.redeem', signed, key=key, subject=owner, contract_version=3)
    assert result.status == 'ok', wire(result)
    assert result.data['order']['quantity'] == quantity
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT quantity FROM resource_entitlements')[0] == quantity
        assert _balance(tx, owner) == 100 and _balance(tx, 'u_root') == quantity
        assert _supply(tx) == quantity + 100


@pytest.mark.asyncio
async def test_imported_legacy_quote_requires_explicit_adoption_before_order_writer(installed):
    from msg.admin.offer_import import preview_import, apply_import
    from test_offer_import import legacy
    app, root = installed
    key, owner, args = await legacy(app, root)
    quote = next(o for o in (await call(app, 'money.offers', {})).data['offers'] if o['offer_id'] == 'old-offer')
    signed = {**args, 'listing_revision': 'not-a-resource', 'offer_snapshot_digest': digest(quote),
              'total_price_minor': 10, 'settlement_policy': 'deterministic-entitlement-v1'}
    denied = await call(app, 'money.redeem', signed, key=key, subject=owner, contract_version=3)
    assert denied.error.code == 'offer_requires_import'
    async with app.metadata.transaction(write=False) as tx:
        plan = await preview_import(app, tx, 'old-offer')
    await apply_import(app, root, plan, operator='test')
    signed['listing_revision'] = plan['revision_id']
    result = await call(app, 'money.redeem', signed, key=key, subject=owner, contract_version=3)
    assert result.status == 'ok', wire(result)
    assert result.data['order']['listing_id'] == 'old-offer'
    assert result.data['order']['listing_revision'] == plan['revision_id']


@pytest.mark.asyncio
async def test_backup_preserves_order_grant_and_receipts_without_reexecution(installed, tmp_path, pg_dsn):
    from msg.admin.backups import backup, restore
    from msg.application import Application
    from msg.config import load_settings
    from test_service import NOW
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    signed = await intent(app, args)
    result = await call(app, 'money.redeem', signed, key=key, subject=owner, contract_version=3,
                        rid='backup-order-redemption')
    assert result.status == 'ok', wire(result)
    tables = ('store_orders', 'order_contracts', 'order_settlements', 'order_transitions',
              'money_ledger', 'resource_entitlements', 'server_offer_resources')
    async with app.metadata.transaction(write=False) as tx:
        before = {table: tx.rows('SELECT * FROM ' + table + ' ORDER BY 1') for table in tables}
    archive = tmp_path/'order-redemption.zip'
    await backup(app, archive)
    restore(archive, tmp_path/'restored-etc', tmp_path/'restored-data', postgres_dsn=pg_dsn)
    restored = Application(load_settings(tmp_path/'restored-etc'), clock=lambda: NOW)
    await restored.load()
    try:
        async with restored.metadata.transaction(write=False) as tx:
            assert {table: tx.rows('SELECT * FROM ' + table + ' ORDER BY 1') for table in tables} == before
            assert inspect_clearing(restored, tx)['conserved']
            assert contract(tx, result.data['order']['id'])['version'] == 5
        blocked = await call(restored, 'money.redeem', signed, key=key, subject=owner, contract_version=3,
                             rid='backup-order-redemption')
        assert blocked.error.code == 'writes_paused'
        async with restored.metadata.transaction(write=False) as tx:
            assert {table: tx.rows('SELECT * FROM ' + table + ' ORDER BY 1') for table in tables} == before
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_diagnostics_reject_incomplete_synchronous_order_projection(installed):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    result = await call(app, 'money.redeem', await intent(app, args), key=key, subject=owner,
                        contract_version=3)
    assert result.status == 'ok', wire(result)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute("UPDATE store_orders SET state='created' WHERE id=?", (result.data['order']['id'],), write=True)
        with pytest.raises(Failure, match='incomplete_entitlement_commit'):
            inspect_clearing(app, tx)


@pytest.mark.asyncio
async def test_entitlement_expiry_cannot_drift_from_signed_duration(installed):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root, duration=60)
    result = await call(app, 'money.redeem', await intent(app, args), key=key, subject=owner,
                        contract_version=3)
    assert result.status == 'ok', wire(result)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE resource_entitlements SET expires_at=NULL WHERE id=?',
                   (result.data['order']['entitlement_id'],), write=True)
    async with app.metadata.transaction(write=False) as tx:
        with pytest.raises(Failure, match='^order_entitlement_mismatch$'):
            contract(tx, result.data['order']['id'])
