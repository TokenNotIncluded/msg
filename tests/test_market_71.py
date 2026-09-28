"""Regression contracts for local funding and the shared deterministic ledger."""
import asyncio
import shutil
from datetime import timedelta

import pytest

from msg.admin.money import MoneyAdmin, apply_money
from msg.admin.root import root_envelope
from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, loads, wire
from msg.core.errors import Failure
from msg.daemon import parser
from msg.market.escrow import resolve_due
from msg.market.policy import contract, delivery_snapshot
from msg.plugins.money import _balance, _supply
from test_service import NOW, call, register


def test_bank_fund_is_a_formal_command_without_unattended_flags():
    args = parser().parse_args(['money', 'bank', 'fund', '@bank-test', '20'])
    assert (args.bank_command, args.subject_id, args.amount) == ('fund', '@bank-test', '20')
    with pytest.raises(SystemExit):
        parser().parse_args(['money', 'bank', 'fund', '@bank-test', '20', '--yes'])


@pytest.mark.asyncio
async def test_bank_fund_role_and_payment_rollback_together(installed):
    app, root = installed
    _, bank, _ = await register(app, 'bank-test')
    with pytest.raises(Failure, match='insufficient_funds'):
        await apply_money(app, root, action='bank_fund', operator='isolated-test',
                          subject_id=bank, amount_minor=20_000_000)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_bank_roles')[0] == 0
        assert _balance(tx, bank) == _supply(tx) == 0
    await apply_money(app, root, action='mint', operator='isolated-test', amount_minor=20_000_000)
    result = await apply_money(app, root, action='bank_fund', operator='isolated-test',
                               subject_id=bank, amount_minor=20_000_000)
    assert len(result['audit_event_ids']) == 2
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx, ROOT_SUBJECT) == 0
        assert _balance(tx, bank) == _supply(tx) == 20_000_000
        assert tx.one('SELECT status FROM money_bank_roles WHERE subject_id=?', (bank,))[0] == 'active'
    await apply_money(app, root, action='bank_remove', operator='isolated-test', subject_id=bank)
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx, bank) == 20_000_000


@pytest.mark.asyncio
async def test_bank_fund_two_console_approvals_before_pin_or_writes(installed, installation_seed, monkeypatch):
    app, root = installed
    _, bank, _ = await register(app, 'bank-test')
    await apply_money(app, root, action='mint', operator='isolated-test', amount_minor=20_000_000)
    from msg.admin import money as module
    monkeypatch.setattr(module, 'require_local_console', lambda _: 'isolated-test-console')
    prompts = []
    def refuse_second(prompt):
        prompts.append(prompt)
        return prompt.removeprefix('Type ').removesuffix(' to continue: ') if len(prompts) == 1 else 'NO'
    monkeypatch.setattr('builtins.input', refuse_second)
    monkeypatch.setattr(module.getpass, 'getpass', lambda _: pytest.fail('PIN before both approvals'))
    with pytest.raises(Failure, match='approval_cancelled'):
        await asyncio.to_thread(MoneyAdmin(app.settings.config_dir).execute, 'bank_fund',
                                subject_id='@bank-test', amount='20')
    assert len(prompts) == 2 and prompts[0] != prompts[1]
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_bank_roles')[0] == 0
        assert _balance(tx, bank) == 0 and _balance(tx, ROOT_SUBJECT) == 20_000_000
    seed, _, _, _ = installation_seed
    shutil.copytree(root_envelope(seed/'etc').parent, root_envelope(app.settings.config_dir).parent)
    monkeypatch.setattr('builtins.input', lambda p: p.removeprefix('Type ').removesuffix(' to continue: '))
    monkeypatch.setattr(module.getpass, 'getpass', lambda _: 'correct-horse-test-passphrase')
    result = await asyncio.to_thread(MoneyAdmin(app.settings.config_dir).execute, 'bank_fund',
                                    subject_id='@bank-test', amount='20')
    assert result['root_balance_minor'] == 0 and result['total_supply_minor'] == 20_000_000
    assert (await call(app, 'money.balance', {}, key=root, subject=ROOT_SUBJECT)).status == 'error'


@pytest.mark.asyncio
async def test_settlement_fact_receipts_must_be_its_own_escrow_legs(installed):
    from test_market_lifecycle import buy, market
    app, root = installed
    _sk, _seller, bk, buyer, listing, _ = await market(app, root, mode='service',
                                                       kind='service', quantity=1)
    bought = await buy(app, bk, buyer, listing)
    assert bought.status == 'ok' and bought.data['order']['state'] == 'funded', wire(bought)
    oid = bought.data['order']['id']
    facts = ('payment_transaction_id', 'payment_intent_digest', 'funded_at', 'delivered_at')
    # A forged refund fact that cites the authentic funding receipt must not
    # make a still-held escrow look refunded, even with a matching projection.
    with pytest.raises(Failure, match='order_settlement_mismatch'):
        async with app.metadata.transaction(write=True) as tx:
            row = tx.one('SELECT total_price_minor,dispute_policy,' + ','.join(facts) +
                         ' FROM store_orders WHERE id=?', (oid,))
            payment = row[2]
            receipt = loads(tx.one('SELECT receipt FROM money_ledger WHERE id=?', (payment,))[0])
            at = wire(NOW)
            fact = {'order_id': oid, 'refund_minor': row[0], 'release_minor': 0,
                    'reason': 'buyer_cancelled', 'decision_id': None, 'policy': row[1],
                    'receipts': [receipt], 'at': at, 'order_facts': dict(zip(facts, row[2:])),
                    'delivery_snapshot': delivery_snapshot(tx, oid)}
            tx.execute('INSERT INTO order_settlements(order_id,decision_id,body) VALUES (?,?,?)',
                       (oid, None, canonical(fact).decode()), write=True)
            tx.execute("UPDATE store_orders SET state='refunded',settled_at=?,receipt_refs=? WHERE id=?",
                       (at, canonical([payment, payment]).decode(), oid), write=True)
            contract(tx, oid)
    app.clock = lambda: NOW + timedelta(days=2)
    assert await resolve_due(app) == [oid]
    async with app.metadata.transaction(write=False) as tx:
        locked = contract(tx, oid)
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (oid,))[0] == 'refunded'
        assert _balance(tx, locked['escrow_subject']) == 0
        assert _balance(tx, buyer) == 20_000_000
