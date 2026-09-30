"""Interactive SSH money administration retains the Root confirmation and signing ceremony."""

import asyncio
import shutil

import pytest
from test_service import call, register
from test_ssh_root_provisioning import setup_terminal

from msg.admin import money, root
from msg.core.errors import Failure
from msg.daemon import parser


@pytest.mark.parametrize(
    'arguments',
    [
        ['money', 'bank', 'add', '@alice'],
        ['money', 'bank', 'remove', '@alice'],
        ['money', 'bank', 'fund', '@alice', '20'],
        ['money', 'mint', '20'],
        ['money', 'burn', '20'],
        ['money', 'transfer', '--from', '@root', '--to', '@alice', '20'],
    ],
)
def test_money_actions_have_explicit_opt_in_without_unattended_approval(arguments):
    assert parser().parse_args([*arguments, '--allow-ssh']).allow_ssh
    assert not parser().parse_args(arguments).allow_ssh
    with pytest.raises(SystemExit):
        parser().parse_args([*arguments, '--yes'])


@pytest.mark.parametrize('action', ['offer_set', 'rotate', 'unknown'])
def test_programmatic_opt_in_cannot_expand_to_unrelated_actions(monkeypatch, action):
    monkeypatch.setattr(
        money, 'require_ssh_administrator', lambda _: pytest.fail('SSH gate reached')
    )
    with pytest.raises(Failure, match='invalid_money_action'):
        money.MoneyAdmin('/etc/msgd').execute(action, allow_ssh=True)


def test_grant_opt_in_cannot_carry_an_amount():
    with pytest.raises(Failure, match='invalid_money_amount'):
        money.MoneyAdmin('/etc/msgd').execute(
            'bank_add', subject_id='@alice', amount='1', allow_ssh=True
        )


def test_default_bank_grant_still_refuses_ssh(monkeypatch):
    setup_terminal(monkeypatch)
    with pytest.raises(Failure, match='remote_admin_forbidden'):
        money.MoneyAdmin('/etc/msgd').execute('bank_add', subject_id='@alice')


@pytest.mark.parametrize(
    'kwargs,code',
    [
        ({'uid': 1000}, 'local_os_administrator_required'),
        ({'connection': ''}, 'ssh_administrator_required'),
        ({'tty': '/dev/tty1'}, 'ssh_terminal_required'),
        ({'mode': 0o40777}, 'unsafe_config_owner'),
    ],
)
def test_bank_grant_requires_the_same_os_root_ssh_boundary(monkeypatch, kwargs, code):
    setup_terminal(monkeypatch, **kwargs)
    with pytest.raises(Failure, match=code):
        money.MoneyAdmin('/etc/msgd').execute('bank_add', subject_id='@alice', allow_ssh=True)


@pytest.mark.asyncio
async def test_ssh_grant_still_requires_exact_confirmation_pin_and_root_signature(
    installed, installation_seed, monkeypatch
):
    app, _ = installed
    _, subject, _ = await register(app, 'ssh-bank-test')
    monkeypatch.setattr(money, 'require_ssh_administrator', lambda _: 'uid:0:ssh:/dev/pts/3:test')
    monkeypatch.setattr('builtins.input', lambda _: 'WRONG')
    monkeypatch.setattr(money.getpass, 'getpass', lambda _: pytest.fail('PIN before confirmation'))
    with pytest.raises(Failure, match='approval_cancelled'):
        await asyncio.to_thread(
            money.MoneyAdmin(app.settings.config_dir).execute,
            'bank_add',
            subject_id=subject,
            allow_ssh=True,
        )
    assert not (await call(app, 'money.banks', {})).data['banks']
    seed, _, _, _ = installation_seed
    shutil.copytree(
        root.root_envelope(seed / 'etc').parent, root.root_envelope(app.settings.config_dir).parent
    )
    monkeypatch.setattr(
        'builtins.input', lambda p: p.removeprefix('Type ').removesuffix(' to continue: ')
    )
    monkeypatch.setattr(money.getpass, 'getpass', lambda _: 'wrong-test-pin')
    with pytest.raises(Failure):
        await asyncio.to_thread(
            money.MoneyAdmin(app.settings.config_dir).execute,
            'bank_add',
            subject_id=subject,
            allow_ssh=True,
        )
    assert not (await call(app, 'money.banks', {})).data['banks']
    monkeypatch.setattr(money.getpass, 'getpass', lambda _: 'correct-horse-test-passphrase')
    await asyncio.to_thread(
        money.MoneyAdmin(app.settings.config_dir).execute,
        'bank_add',
        subject_id=subject,
        allow_ssh=True,
    )
    banks = (await call(app, 'money.banks', {})).data['banks']
    assert len(banks) == 1 and banks[0]['subject_id'] == subject and banks[0]['status'] == 'active'
    assert (await call(app, 'money.state', {})).data['total_supply_minor'] == 0
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == 0


@pytest.mark.parametrize(
    'action', ['mint', 'burn', 'transfer', 'bank_add', 'bank_remove', 'bank_fund']
)
def test_all_money_defaults_refuse_ssh_before_loading(monkeypatch, action):
    setup_terminal(monkeypatch)
    with pytest.raises(Failure, match='remote_admin_forbidden'):
        money.MoneyAdmin('/etc/msgd').execute(action)


@pytest.mark.parametrize('action', ['mint', 'burn', 'transfer', 'bank_remove', 'bank_fund'])
@pytest.mark.asyncio
async def test_ssh_money_preserves_confirm_pin_atomic_writes_and_audit(
    installed, installation_seed, monkeypatch, action
):
    app, signer = installed
    _, subject, _ = await register(app, 'ssh-money-test')
    await money.apply_money(
        app, signer, action='mint', operator='test-setup', amount_minor=20_000_000
    )
    if action == 'bank_remove':
        await money.apply_money(
            app, signer, action='bank_add', operator='test-setup', subject_id=subject
        )
    arguments = {'allow_ssh': True}
    if action != 'bank_remove':
        arguments['amount'] = '2'
    if action in {'transfer', 'bank_fund', 'bank_remove'}:
        arguments['subject_id'] = '@ssh-money-test'
    operator = 'uid:0:ssh:/dev/pts/3:192.0.2.1 1234 192.0.2.2 22'
    monkeypatch.setattr(money, 'require_ssh_administrator', lambda _: operator)
    admin = money.MoneyAdmin(app.settings.config_dir)

    async def snapshot():
        async with app.metadata.transaction(write=False) as tx:
            return tuple(
                tx.one('SELECT COUNT(*) FROM ' + table)[0]
                for table in ('money_ledger', 'money_bank_roles', 'audit', 'events')
            )

    before = await snapshot()
    prompts = []

    def refuse_last(prompt):
        prompts.append(prompt)
        if action == 'bank_fund' and len(prompts) == 1:
            return prompt.removeprefix('Type ').removesuffix(' to continue: ')
        return 'WRONG'

    monkeypatch.setattr('builtins.input', refuse_last)
    monkeypatch.setattr(money.getpass, 'getpass', lambda _: pytest.fail('PIN before confirmation'))
    with pytest.raises(Failure, match='approval_cancelled'):
        await asyncio.to_thread(admin.execute, action, **arguments)
    assert len(prompts) == (2 if action == 'bank_fund' else 1)
    assert await snapshot() == before
    seed, _, _, _ = installation_seed
    shutil.copytree(
        root.root_envelope(seed / 'etc').parent, root.root_envelope(app.settings.config_dir).parent
    )
    monkeypatch.setattr(
        'builtins.input', lambda p: p.removeprefix('Type ').removesuffix(' to continue: ')
    )
    monkeypatch.setattr(money.getpass, 'getpass', lambda _: 'wrong-test-pin')
    with pytest.raises(Failure):
        await asyncio.to_thread(admin.execute, action, **arguments)
    assert await snapshot() == before
    monkeypatch.setattr(money.getpass, 'getpass', lambda _: 'correct-horse-test-passphrase')
    result = await asyncio.to_thread(admin.execute, action, **arguments)
    expected_supply = (
        22_000_000 if action == 'mint' else 18_000_000 if action == 'burn' else 20_000_000
    )
    expected_root = (
        22_000_000 if action == 'mint' else 20_000_000 if action == 'bank_remove' else 18_000_000
    )
    assert result['total_supply_minor'] == expected_supply
    assert result['root_balance_minor'] == expected_root
    assert len(result['audit_event_ids']) == (2 if action == 'bank_fund' else 1)
    async with app.metadata.transaction(write=False) as tx:
        for event_id in result['audit_event_ids']:
            event = money.loads(tx.one('SELECT body FROM events WHERE event_id=?', (event_id,))[0])
            assert event['data']['operator'] == operator
            assert event['data']['root_signature']
        if action in {'transfer', 'bank_fund'}:
            assert money._balance(tx, subject) == 2_000_000
