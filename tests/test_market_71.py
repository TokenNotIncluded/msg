"""Regression contracts for local funding and the shared deterministic ledger."""
import asyncio
import shutil

import pytest

from msg.admin.money import MoneyAdmin, apply_money
from msg.admin.root import root_envelope
from msg.constants import ROOT_SUBJECT
from msg.core.errors import Failure
from msg.daemon import parser
from msg.plugins.money import _balance, _supply
from test_service import call, register


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
