"""Escrow account migration and network isolation on real PostgreSQL."""

import pytest
from test_orders import _intent, _sale
from test_service import call, register

from msg.admin.money import apply_money
from msg.core.codec import wire
from msg.core.models import Subject
from msg.security.crypto import Ed25519Signer
from msg.storage.postgres import PostgresMetadataStore


async def _market_accounts(app, root):
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'ledger-buyer')
    await apply_money(app, root, action='mint', operator='test-console', amount_minor=20_000_000)
    await apply_money(
        app,
        root,
        action='transfer',
        operator='test-console',
        subject_id=seller,
        amount_minor=10_000_000,
    )
    await apply_money(
        app,
        root,
        action='transfer',
        operator='test-console',
        subject_id=buyer,
        amount_minor=10_000_000,
    )
    bounty = await call(
        app,
        'bounty.create',
        {
            'name': 'escrow-migration-bounty',
            'terms': 'one signature',
            'reward_minor': 5_000_000,
            'budget_minor': 5_000_000,
            'max_claims': 1,
        },
        key=seller_key,
        subject=seller,
    )
    assert bounty.status == 'ok', wire(bounty)
    listing, _ = await _sale(app, seller_key, seller)
    order = await call(app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer)
    assert order.status == 'ok', wire(order)
    return seller_key, seller, buyer_key, buyer, bounty, order


@pytest.mark.asyncio
async def test_escrow_accounts_are_not_subjects_and_reject_network_money(installed):
    app, root = installed
    seller_key, seller, buyer_key, buyer, bounty, order = await _market_accounts(app, root)
    bounty_id = bounty.data['funding']['body']['to_subject']
    order_id = order.data['payment']['body']['to_subject']
    async with app.metadata.transaction(write=False) as tx:
        for account_id, kind in ((bounty_id, 'bounty_escrow'), (order_id, 'order_escrow')):
            assert tx.one(
                'SELECT kind,subject_id FROM ledger_accounts WHERE id=?', (account_id,)
            ) == (kind, None)
            assert tx.one('SELECT 1 FROM identities WHERE id=?', (account_id,)) is None
            for table in ('credentials', 'identity_keys', 'certificates'):
                assert (
                    tx.one(f'SELECT COUNT(*) FROM {table} WHERE subject=?', (account_id,))[0] == 0
                )
    for escrow in (bounty_id, order_id):
        before = (await call(app, 'money.balance', {}, key=buyer_key, subject=buyer)).data[
            'balance_minor'
        ]
        recipient = await call(
            app,
            'money.transfer',
            {'to_subject': escrow, 'currency_id': 'primary', 'amount_minor': 1},
            key=buyer_key,
            subject=buyer,
        )
        assert recipient.status == 'error'
        sender = await call(
            app,
            'money.transfer',
            {
                'from_subject': escrow,
                'to_subject': seller,
                'currency_id': 'primary',
                'amount_minor': 1,
            },
            key=buyer_key,
            subject=buyer,
        )
        assert sender.status == 'error'
        impersonator = await call(
            app, 'money.balance', {}, key=Ed25519Signer.generate(), subject=escrow
        )
        assert impersonator.status == 'error'
        assert (await call(app, 'money.balance', {}, key=buyer_key, subject=buyer)).data[
            'balance_minor'
        ] == before
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM money_ledger')[0] == 5
        net = tx.one("""SELECT COALESCE((SELECT SUM(amount_minor) FROM money_ledger
            WHERE credit_account IS NOT NULL),0) -
            COALESCE((SELECT SUM(amount_minor) FROM money_ledger
            WHERE debit_account IS NOT NULL),0)""")[0]
        assert net == 20_000_000


@pytest.mark.asyncio
async def test_legacy_escrow_identity_migration_keeps_ledger_bytes(installed):
    app, root = installed
    seller_key, seller, buyer_key, buyer, bounty, order = await _market_accounts(app, root)
    escrows = (
        bounty.data['funding']['body']['to_subject'],
        order.data['payment']['body']['to_subject'],
    )
    async with app.metadata.transaction(write=False) as tx:
        original = tx.rows("""SELECT seq,id,kind,amount_minor,debit_account,
            credit_account,receipt FROM money_ledger ORDER BY seq""")
    # Recreate the pre-migration FK shape in this disposable database. The
    # account IDs, ledger amounts, sequence and signed receipt bytes stay put.
    async with app.metadata.transaction(write=True) as tx:
        for escrow in escrows:
            await tx.update_identity(
                Subject(
                    resource_id=escrow,
                    kind='system',
                    primary_group='g_public',
                    auth_version=0,
                    local_only=True,
                ),
                -1,
            )
        for table, new_name, old_name, column in (
            (
                'money_accounts',
                'money_accounts_ledger_account_fkey',
                'money_accounts_subject_id_fkey',
                'subject_id',
            ),
            (
                'money_ledger',
                'money_ledger_debit_ledger_account_fkey',
                'money_ledger_debit_account_fkey',
                'debit_account',
            ),
            (
                'money_ledger',
                'money_ledger_credit_ledger_account_fkey',
                'money_ledger_credit_account_fkey',
                'credit_account',
            ),
            (
                'bounty_listings',
                'bounty_listings_escrow_account_fkey',
                'bounty_listings_escrow_subject_fkey',
                'escrow_subject',
            ),
            (
                'store_orders',
                'store_orders_escrow_account_fkey',
                'store_orders_escrow_subject_fkey',
                'escrow_subject',
            ),
        ):
            tx.execute(f'ALTER TABLE {table} DROP CONSTRAINT {new_name}', write=True)
            tx.execute(
                f"""ALTER TABLE {table} ADD CONSTRAINT {old_name}
                FOREIGN KEY ({column}) REFERENCES identities(id)""",
                write=True,
            )
        for escrow in escrows:
            tx.execute('DELETE FROM ledger_accounts WHERE id=?', (escrow,), write=True)
    migrated = PostgresMetadataStore(app.settings.server.postgres_dsn)
    await migrated.close()
    repeated = PostgresMetadataStore(app.settings.server.postgres_dsn)
    await repeated.close()
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.rows("""SELECT seq,id,kind,amount_minor,debit_account,
            credit_account,receipt FROM money_ledger ORDER BY seq""")
            == original
        )
        assert tx.one('SELECT COUNT(*) FROM identities WHERE id IN (?,?)', escrows)[0] == 0
        assert tx.one('SELECT COUNT(*) FROM ledger_accounts WHERE id IN (?,?)', escrows)[0] == 2
        assert tx.one('SELECT COUNT(*) FROM money_accounts')[0] == 5
    assert (await call(app, 'money.balance', {}, key=seller_key, subject=seller)).data[
        'balance_minor'
    ] == 5_000_000
    assert (await call(app, 'money.balance', {}, key=buyer_key, subject=buyer)).data[
        'balance_minor'
    ] == 5_000_000
    assert (await call(app, 'money.state', {})).data['total_supply_minor'] == 20_000_000


@pytest.mark.asyncio
async def test_migration_refuses_escrow_with_credential_reference(installed):
    app, root = installed
    _, _, _, _, bounty, _ = await _market_accounts(app, root)
    escrow = bounty.data['funding']['body']['to_subject']
    async with app.metadata.transaction(write=True) as tx:
        await tx.update_identity(
            Subject(
                resource_id=escrow,
                kind='system',
                primary_group='g_public',
                auth_version=0,
                local_only=True,
            ),
            -1,
        )
        tx.execute(
            'INSERT INTO credentials(id,subject,body) VALUES (?,?,?)',
            ('legacy_escrow_credential', escrow, '{}'),
            write=True,
        )
    with pytest.raises(RuntimeError, match='active reference'):
        PostgresMetadataStore(app.settings.server.postgres_dsn)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT 1 FROM identities WHERE id=?', (escrow,)) is not None
        assert tx.one('SELECT 1 FROM credentials WHERE subject=?', (escrow,)) is not None
