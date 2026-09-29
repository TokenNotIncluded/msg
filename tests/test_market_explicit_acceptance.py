"""V4 managed delivery keeps payment in escrow until signed buyer acceptance."""
import pytest

from msg.core.codec import wire
from msg.plugins.money import _balance
from test_market_lifecycle import market
from test_orders import _intent
from test_service import call


@pytest.mark.asyncio
async def test_v4_delivery_requires_current_buyer_and_exact_digest_before_settlement(installed):
    app, root = installed
    sk, seller, bk, buyer, listing, _ = await market(app, root)
    args = _intent(listing)
    bought = await call(app, 'orders.buy', args, key=bk, subject=buyer,
                        contract_version=4, rid='explicit-buy')
    assert bought.status == 'ok', wire(bought)
    order = bought.data['order']
    assert order['state'] == 'delivered' and order['contract_version'] == 4
    contract = await call(app, 'orders.contract', {'order_id': order['id']},
                          key=bk, subject=buyer)
    assert contract.status == 'ok', wire(contract)
    assert contract.data['contract']['settlement_policy'] == {
        'id': 'explicit-buyer-acceptance', 'version': 1}
    replay = await call(app, 'orders.buy', args, key=bk, subject=buyer,
                        contract_version=4, rid='explicit-buy')
    assert replay.replayed and replay.data == bought.data
    delivery = await call(app, 'delivery.get', {'order_id': order['id']},
                          key=bk, subject=buyer, contract_version=2)
    assert delivery.status == 'ok', wire(delivery)
    assert delivery.data['delivery']['state'] == 'prepared'
    accept = {'order_id': order['id'],
              'delivery_digest': delivery.data['delivery']['delivery_digest']}
    wrong_buyer = await call(app, 'delivery.accept', accept,
                            key=sk, subject=seller, contract_version=2)
    assert wrong_buyer.status == 'error'
    wrong_digest = await call(app, 'delivery.accept',
        {**accept, 'delivery_digest': 'sha256:' + '0' * 64},
        key=bk, subject=buyer, contract_version=2)
    assert wrong_digest.error.code == 'delivery_mismatch'
    async with app.metadata.transaction(write=False) as tx:
        escrow = tx.one('SELECT escrow_subject FROM store_orders WHERE id=?', (order['id'],))[0]
        assert (_balance(tx, buyer), _balance(tx, seller), _balance(tx, escrow)) == (15_000_000, 0, 5_000_000)
        assert tx.one('SELECT COUNT(*) FROM order_settlements WHERE order_id=?', (order['id'],))[0] == 0
    accepted = await call(app, 'delivery.accept', accept, key=bk, subject=buyer,
                          contract_version=2, rid='explicit-accept')
    assert accepted.status == 'ok' and accepted.data['state'] == 'settled', wire(accepted)
    repeated = await call(app, 'delivery.accept', accept, key=bk, subject=buyer,
                          contract_version=2, rid='explicit-accept')
    assert repeated.replayed and repeated.data == accepted.data
    again = await call(app, 'delivery.accept', accept, key=bk, subject=buyer,
                       contract_version=2, rid='explicit-accept-again')
    assert again.status == 'ok' and again.data['receipt'] == accepted.data['receipt']
    async with app.metadata.transaction(write=False) as tx:
        assert (_balance(tx, buyer), _balance(tx, seller), _balance(tx, escrow)) == (15_000_000, 5_000_000, 0)
        assert tx.one('SELECT COUNT(*) FROM order_settlements WHERE order_id=?', (order['id'],))[0] == 1


@pytest.mark.asyncio
async def test_cli_default_checkout_waits_for_signed_acceptance(installed):
    from test_market_cli_bindings import ExecutorClient, command
    from msg.core.codec import canonical
    app, root = installed
    _, _, bk, buyer, listing, _ = await market(app, root)
    bought = await command(ExecutorClient(app, bk, buyer), 'orders', 'buy',
                            canonical(_intent(listing)).decode())
    assert bought.status == 'ok', wire(bought)
    assert bought.data['order']['contract_version'] == 4
    assert bought.data['order']['state'] == 'delivered'


@pytest.mark.asyncio
async def test_revoked_acceptance_key_cannot_replay_settlement(installed):
    from test_market_account_scope import restricted_key
    app, root = installed
    _, _, bk, buyer, listing, _ = await market(app, root)
    accept_key = await restricted_key(app, bk, buyer, 'delivery.accept', 2, account=True)
    bought = await call(app, 'orders.buy', _intent(listing), key=bk, subject=buyer,
                        contract_version=4)
    order = bought.data['order']
    delivery = await call(app, 'delivery.get', {'order_id': order['id']},
                          key=bk, subject=buyer, contract_version=2)
    args = {'order_id': order['id'],
            'delivery_digest': delivery.data['delivery']['delivery_digest']}
    accepted = await call(app, 'delivery.accept', args, key=accept_key, subject=buyer,
                          contract_version=2, rid='accept-revocable')
    assert accepted.status == 'ok', wire(accepted)
    revoked = await call(app, 'identity.key_revoke', {'key_id': accept_key.key_id},
                         key=bk, subject=buyer)
    assert revoked.status == 'ok', wire(revoked)
    replay = await call(app, 'delivery.accept', args, key=accept_key, subject=buyer,
                        contract_version=2, rid='accept-revocable')
    assert replay.status == 'error' and not replay.replayed


@pytest.mark.asyncio
async def test_explicit_reservation_policy_survives_separate_funding(installed):
    app, root = installed
    _, _, bk, buyer, listing, _ = await market(app, root)
    created = await call(app, 'orders.create', _intent(listing), key=bk, subject=buyer,
                         contract_version=2)
    assert created.status == 'ok', wire(created)
    order = created.data['order']
    assert order['contract_version'] == 4
    funded = await call(app, 'orders.fund', {
        'order_id': order['id'], 'order_digest': order['order_digest'],
        'total_price_minor': order['total_price_minor'], 'currency_id': 'primary'},
        key=bk, subject=buyer)
    assert funded.status == 'ok' and funded.data['order']['state'] == 'delivered', wire(funded)


@pytest.mark.asyncio
async def test_waiting_acceptance_survives_restart_without_release(installed):
    from msg.application import Application
    from msg.admin.market_check import inspect_clearing
    from test_service import NOW
    app, root = installed
    _, seller, bk, buyer, listing, _ = await market(app, root)
    bought = await call(app, 'orders.buy', _intent(listing), key=bk, subject=buyer,
                        contract_version=4)
    assert bought.status == 'ok', wire(bought)
    order = bought.data['order']
    restarted = Application(app.settings, clock=lambda: NOW)
    await restarted.load()
    try:
        delivery = await call(restarted, 'delivery.get', {'order_id': order['id']},
                              key=bk, subject=buyer, contract_version=2)
        assert delivery.status == 'ok' and delivery.data['delivery']['state'] == 'prepared'
        async with restarted.metadata.transaction(write=False) as tx:
            assert inspect_clearing(restarted, tx)['conserved']
            assert _balance(tx, seller) == 0
        accepted = await call(restarted, 'delivery.accept', {
            'order_id': order['id'], 'delivery_digest': delivery.data['delivery']['delivery_digest']},
            key=bk, subject=buyer, contract_version=2)
        assert accepted.status == 'ok' and accepted.data['state'] == 'settled', wire(accepted)
    finally:
        await restarted.close()
