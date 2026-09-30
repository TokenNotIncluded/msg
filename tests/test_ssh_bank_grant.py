"""SSH opt-in grants only a Bank role; Root money movement stays console-only."""

import asyncio
import shutil

import pytest
from test_service import call, register
from test_ssh_root_provisioning import setup_terminal

from msg.admin import money, root
from msg.core.errors import Failure
from msg.daemon import parser


def test_bank_grant_flag_has_no_money_movement_or_revocation_path():
    assert parser().parse_args(['money', 'bank', 'add', '@alice', '--allow-ssh']).allow_ssh
    assert not parser().parse_args(['money', 'bank', 'add', '@alice']).allow_ssh
    for arguments in (
        ['money', 'bank', 'remove', '@alice'],
        ['money', 'bank', 'fund', '@alice', '20'],
        ['money', 'mint', '20'],
        ['money', 'burn', '20'],
        ['money', 'transfer', '--from', '@root', '--to', '@alice', '20'],
    ):
        with pytest.raises(SystemExit):
            parser().parse_args([*arguments, '--allow-ssh'])


@pytest.mark.parametrize('action', ['mint', 'burn', 'transfer', 'bank_fund', 'bank_remove'])
def test_programmatic_opt_in_cannot_expand_to_other_root_actions(monkeypatch, action):
    monkeypatch.setattr(money, 'require_ssh_administrator', lambda _: pytest.fail('SSH gate reached'))
    with pytest.raises(Failure, match='ssh_bank_role_grant_only'):
        money.MoneyAdmin('/etc/msgd').execute(action, allow_ssh=True)


def test_grant_opt_in_cannot_carry_an_amount():
    with pytest.raises(Failure, match='ssh_bank_role_grant_only'):
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
            'bank_add', subject_id=subject, allow_ssh=True,
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
            'bank_add', subject_id=subject, allow_ssh=True,
        )
    assert not (await call(app, 'money.banks', {})).data['banks']
    monkeypatch.setattr(money.getpass, 'getpass', lambda _: 'correct-horse-test-passphrase')
    await asyncio.to_thread(
        money.MoneyAdmin(app.settings.config_dir).execute,
        'bank_add', subject_id=subject, allow_ssh=True,
    )
    banks = (await call(app, 'money.banks', {})).data['banks']
    assert len(banks) == 1 and banks[0]['subject_id'] == subject and banks[0]['status'] == 'active'
    assert (await call(app, 'money.state', {})).data['total_supply_minor'] == 0
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == 0
