"""The supported manual market path conserves funds across bounty and sale."""

import pytest
from test_orders import _intent, _sale
from test_service import call, register

from msg.admin.money import apply_money
from msg.core.codec import b64, canonical, wire


@pytest.mark.asyncio
async def test_prefunded_reward_then_manual_delivery_and_settlement(installed):
    app, root = installed
    bank_key, bank, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'market-buyer')
    await apply_money(app, root, action='mint', operator='test-console', amount_minor=20_000_000)
    await apply_money(app, root, action='bank_add', operator='test-console', subject_id=bank)
    await apply_money(
        app,
        root,
        action='transfer',
        operator='test-console',
        subject_id=bank,
        amount_minor=20_000_000,
    )
    bounty = await call(
        app,
        'bounty.create',
        {
            'name': 'signature-reward',
            'terms': 'Sign a one-use challenge.',
            'reward_minor': 10_000_000,
            'budget_minor': 10_000_000,
            'max_claims': 1,
        },
        key=bank_key,
        subject=bank,
    )
    assert bounty.status == 'ok', wire(bounty)
    bounty_id = bounty.data['bounty']['listing_id']
    challenge = await call(
        app, 'bounty.challenge', {'listing_id': bounty_id}, key=buyer_key, subject=buyer
    )
    assert challenge.status == 'ok', wire(challenge)
    payload = challenge.data['challenge']
    proof = wire(buyer_key.sign(canonical(payload), purpose='bounty-pop-v1'))
    claimed = await call(
        app,
        'bounty.claim',
        {'challenge_id': payload['challenge_id'], 'proof': proof},
        key=buyer_key,
        subject=buyer,
    )
    assert claimed.status == 'ok', wire(claimed)
    assert (await call(app, 'money.balance', {}, key=bank_key, subject=bank)).data[
        'balance_minor'
    ] == 10_000_000
    assert (await call(app, 'money.balance', {}, key=buyer_key, subject=buyer)).data[
        'balance_minor'
    ] == 10_000_000
    listing, package = await _sale(app, bank_key, bank, price=5_000_000, quantity=1)
    bought = await call(app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer)
    assert bought.status == 'ok', wire(bought)
    order_id = bought.data['order']['id']
    assert bought.data['order']['state'] == 'funded'
    prepared = await call(
        app, 'delivery.prepare', {'order_id': order_id}, key=buyer_key, subject=buyer
    )
    assert prepared.status == 'ok', wire(prepared)
    delivered = await call(
        app, 'delivery.get', {'order_id': order_id}, key=buyer_key, subject=buyer
    )
    assert delivered.status == 'ok', wire(delivered)
    content = delivered.data['delivery']
    assert content['package_digest'] == package['digest']
    assert content['manifest']['text'] == 'msg.lmm.best store selftest'
    assert content['payloads'][0]['data'] == b64(b'delivery-ok\n')
    accepted = await call(
        app,
        'delivery.accept',
        {'order_id': order_id, 'delivery_digest': content['delivery_digest']},
        key=buyer_key,
        subject=buyer,
    )
    assert accepted.status == 'ok', wire(accepted)
    assert accepted.data['state'] == 'settled'
    assert (await call(app, 'money.balance', {}, key=bank_key, subject=bank)).data[
        'balance_minor'
    ] == 15_000_000
    assert (await call(app, 'money.balance', {}, key=buyer_key, subject=buyer)).data[
        'balance_minor'
    ] == 5_000_000
    assert (await call(app, 'money.state', {})).data['total_supply_minor'] == 20_000_000
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order_id,))[0] == 'settled'
        assert (
            tx.one(
                'SELECT COUNT(*) FROM money_bank_roles WHERE subject_id=? AND status=?',
                (bank, 'active'),
            )[0]
            == 1
        )
        assert tx.one('SELECT COUNT(*) FROM money_ledger WHERE credit_account=?', (bank,))[0] >= 2
        assert (
            tx.one(
                'SELECT COUNT(*) FROM money_ledger WHERE debit_account=?',
                (bought.data['payment']['body']['to_subject'],),
            )[0]
            == 1
        )
