"""The issuer exists only in the local administrator module."""

import asyncio
import shutil

import pytest
from test_service import call, register

from msg.admin.money import MoneyAdmin, apply_money, apply_offer, parse_amount
from msg.constants import ROOT_SUBJECT
from msg.core.errors import Failure
from msg.daemon import parser
from msg.plugins.money import MAX_MINOR
from msg.security.crypto import Ed25519Signer


@pytest.mark.parametrize(
    'text,expected', [('20', 20_000_000), ('0.000001', 1), ('1.234567', 1_234_567)]
)
def test_amount_exact(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize(
    'text', ['0', '-1', '+1', '1e2', '1.0000001', '01', ' 1', '1 ', '9223372036855']
)
def test_amount_rejected(text):
    with pytest.raises(Failure) as exc:
        parse_amount(text)
    assert exc.value.code == 'invalid_money_amount'


def test_cli_requires_explicit_root_source_and_has_no_pin_or_yes():
    args = parser().parse_args(['money', 'transfer', '--from', '@root', '--to', '@alice', '20'])
    assert args.from_subject == '@root' and args.to_subject == '@alice'
    with pytest.raises(SystemExit):
        parser().parse_args(['money', 'transfer', '--to', '@alice', '20'])
    with pytest.raises(SystemExit):
        parser().parse_args(['money', 'mint', '20', '--yes'])
    with pytest.raises(SystemExit):
        parser().parse_args(['money', 'mint', '20', '--pin', 'secret'])
    args = parser().parse_args(['money', 'offer', 'disable', 'storage-1'])
    assert args.offer_command == 'disable' and args.offer_id == 'storage-1'
    with pytest.raises(SystemExit):
        parser().parse_args(['money', 'offer', 'disable', 'storage-1', '--yes'])


@pytest.mark.asyncio
async def test_mint_burn_root_payment_and_bank_role_are_atomic(installed):
    app, root_signer = installed
    key, subject, _ = await register(app, 'bank-local-test')
    assert (await call(app, 'money.state', {})).data['total_supply_minor'] == 0
    assert not (await call(app, 'money.banks', {})).data['banks']

    minted = await apply_money(
        app, root_signer, action='mint', operator='test-console', amount_minor=20_000_000
    )
    assert minted['root_balance_minor'] == 20_000_000
    assert minted['total_supply_minor'] == 20_000_000
    await apply_money(
        app, root_signer, action='bank_add', operator='test-console', subject_id=subject
    )
    funded = await apply_money(
        app,
        root_signer,
        action='transfer',
        operator='test-console',
        subject_id=subject,
        amount_minor=5_000_000,
    )
    assert funded['root_balance_minor'] == 15_000_000
    assert funded['total_supply_minor'] == 20_000_000
    assert (await call(app, 'money.balance', {}, key=key, subject=subject)).data[
        'balance_minor'
    ] == 5_000_000
    await apply_money(
        app, root_signer, action='bank_remove', operator='test-console', subject_id=subject
    )
    assert (await call(app, 'money.balance', {}, key=key, subject=subject)).data[
        'balance_minor'
    ] == 5_000_000
    burned = await apply_money(
        app, root_signer, action='burn', operator='test-console', amount_minor=2_000_000
    )
    assert burned['root_balance_minor'] == 13_000_000
    assert burned['total_supply_minor'] == 18_000_000
    banks = (await call(app, 'money.banks', {})).data['banks']
    assert len(banks) == 1 and banks[0]['subject_id'] == subject and banks[0]['status'] == 'revoked'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == 3
        assert tx.one('SELECT COUNT(*) FROM audit')[0] >= 5
        assert tx.one('SELECT COUNT(*) FROM events WHERE body LIKE ?', ('%root.money.%',))[0] == 5


@pytest.mark.asyncio
async def test_issuer_rejects_impostor_overflow_and_invalid_role(installed):
    app, root_signer = installed
    _, subject, _ = await register(app, 'bank-failure-test')
    with pytest.raises(Failure) as exc:
        await apply_money(
            app, Ed25519Signer.generate(), action='mint', operator='test', amount_minor=1
        )
    assert exc.value.code == 'root_key_mismatch'
    with pytest.raises(Failure) as exc:
        await apply_money(
            app, root_signer, action='mint', operator='test', amount_minor=MAX_MINOR + 1
        )
    assert exc.value.code == 'invalid_money_amount'
    with pytest.raises(Failure) as exc:
        await apply_money(
            app, root_signer, action='bank_remove', operator='test', subject_id=subject
        )
    assert exc.value.code == 'bank_role_unchanged'
    with pytest.raises(Failure) as exc:
        await apply_money(
            app, root_signer, action='bank_add', operator='test', subject_id=ROOT_SUBJECT
        )
    assert exc.value.code == 'invalid_money_recipient'
    with pytest.raises(Failure) as exc:
        await apply_money(app, root_signer, action='burn', operator='test', amount_minor=1)
    assert exc.value.code == 'insufficient_funds'
    with pytest.raises(Failure) as exc:
        await apply_money(
            app,
            root_signer,
            action='mint',
            operator='test',
            amount_minor=1,
            expected_state={'supply': 1, 'root': 0, 'subject': None, 'role': None},
        )
    assert exc.value.code == 'money_preview_stale'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == 0
        assert tx.one('SELECT COUNT(*) FROM money_bank_roles')[0] == 0


@pytest.mark.asyncio
async def test_console_gate_precedes_unlock_and_confirmation(
    installed, installation_seed, monkeypatch
):
    app, _ = installed
    from msg.admin import money as module
    from msg.admin.root import root_envelope

    def forbidden(_):
        raise Failure('local_console_required')

    monkeypatch.setattr(module, 'require_local_console', forbidden)
    with pytest.raises(Failure) as exc:
        await asyncio.to_thread(MoneyAdmin(app.settings.config_dir).execute, 'mint', amount='1')
    assert exc.value.code == 'local_console_required'

    monkeypatch.setattr(module, 'require_local_console', lambda _: 'test-console')
    monkeypatch.setattr('builtins.input', lambda _: 'WRONG')
    monkeypatch.setattr(
        module.getpass, 'getpass', lambda _: pytest.fail('PIN requested before approval')
    )
    with pytest.raises(Failure) as exc:
        await asyncio.to_thread(MoneyAdmin(app.settings.config_dir).execute, 'mint', amount='1')
    assert exc.value.code == 'approval_cancelled'
    assert (await call(app, 'money.state', {})).data['total_supply_minor'] == 0

    monkeypatch.setattr(
        'builtins.input', lambda prompt: prompt.removeprefix('Type ').removesuffix(' to continue: ')
    )
    monkeypatch.setattr(module.getpass, 'getpass', lambda _: 'correct-horse-test-passphrase')
    seed, _, _, _ = installation_seed
    shutil.copytree(
        root_envelope(seed / 'etc').parent, root_envelope(app.settings.config_dir).parent
    )
    result = await asyncio.to_thread(
        MoneyAdmin(app.settings.config_dir).execute, 'mint', amount='1.25'
    )
    assert result['total_supply_minor'] == 1_250_000
    assert result['root_balance_minor'] == 1_250_000


@pytest.mark.asyncio
async def test_offer_set_fail_closed_with_zero_database_change(installed, monkeypatch):
    app, signer = installed
    fields = {
        'resource_kind': 'file',
        'entitlement_kind': 'storage_bytes',
        'unit': 'byte',
        'price_minor': 1,
        'min_quantity': 1,
        'max_quantity': 100,
        'duration_seconds': None,
    }
    async with app.metadata.transaction(write=False) as tx:
        before = (
            tx.one('SELECT COUNT(*) FROM server_offers')[0],
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM audit')[0],
        )
    with pytest.raises(Failure) as exc:
        await apply_offer(
            app, signer, action='set', operator='test-console', offer_id='storage-1', fields=fields
        )
    assert exc.value.code == 'resource_not_purchasable'
    from msg.admin import money as module

    monkeypatch.setattr(module, 'require_local_console', lambda _: 'test-console')
    monkeypatch.setattr(
        'builtins.input', lambda _: pytest.fail('invalid offer reached confirmation')
    )
    monkeypatch.setattr(
        module.getpass, 'getpass', lambda _: pytest.fail('invalid offer reached PIN')
    )
    with pytest.raises(Failure) as exc:
        await asyncio.to_thread(
            MoneyAdmin(app.settings.config_dir).execute_offer,
            'set',
            offer_id='storage-1',
            fields={**{k: v for k, v in fields.items() if k != 'price_minor'}, 'price': '0.000001'},
        )
    assert exc.value.code == 'resource_not_purchasable'
    async with app.metadata.transaction(write=False) as tx:
        after = (
            tx.one('SELECT COUNT(*) FROM server_offers')[0],
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM audit')[0],
        )
    assert after == before


@pytest.mark.asyncio
async def test_offer_disable_console_confirmation_pin_audit_and_repeat(
    installed, installation_seed, monkeypatch
):
    app, signer = installed
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            """INSERT INTO server_offers
            (offer_id,resource_kind,unit,price_minor,min_quantity,max_quantity,
             entitlement_kind,duration_seconds,enabled,price_revision)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ('legacy-offer', 'file', 'byte', 5, 1, 10, 'storage_bytes', None, True, 'price-old'),
            write=True,
        )
    with pytest.raises(Failure) as exc:
        await apply_offer(
            app, signer, action='disable', operator='test-console', offer_id='missing'
        )
    assert exc.value.code == 'offer_not_found'
    from msg.admin import money as module
    from msg.admin.root import root_envelope

    monkeypatch.setattr(module, 'require_local_console', lambda _: 'test-console')
    monkeypatch.setattr('builtins.input', lambda _: 'WRONG')
    monkeypatch.setattr(module.getpass, 'getpass', lambda _: pytest.fail('PIN before confirmation'))
    with pytest.raises(Failure) as exc:
        await asyncio.to_thread(
            MoneyAdmin(app.settings.config_dir).execute_offer, 'disable', offer_id='legacy-offer'
        )
    assert exc.value.code == 'approval_cancelled'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT enabled FROM server_offers WHERE offer_id=?', ('legacy-offer',))[0]
    seed, _, _, _ = installation_seed
    shutil.copytree(
        root_envelope(seed / 'etc').parent, root_envelope(app.settings.config_dir).parent
    )
    monkeypatch.setattr(
        'builtins.input', lambda prompt: prompt.removeprefix('Type ').removesuffix(' to continue: ')
    )
    monkeypatch.setattr(module.getpass, 'getpass', lambda _: 'correct-horse-test-passphrase')
    result = await asyncio.to_thread(
        MoneyAdmin(app.settings.config_dir).execute_offer, 'disable', offer_id='legacy-offer'
    )
    assert result['offer']['enabled'] is False
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one('SELECT enabled FROM server_offers WHERE offer_id=?', ('legacy-offer',))[0]
            is False
        )
        assert (
            tx.one(
                'SELECT COUNT(*) FROM events WHERE body LIKE ?', ('%root.money.offer.disable%',)
            )[0]
            == 1
        )
        assert tx.one('SELECT COUNT(*) FROM audit')[0] >= 1
    with pytest.raises(Failure) as exc:
        await apply_offer(
            app, signer, action='disable', operator='test-console', offer_id='legacy-offer'
        )
    assert exc.value.code == 'offer_already_disabled'
