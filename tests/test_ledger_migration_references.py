"""PostgreSQL regressions for unsafe legacy escrow references and ledger immutability."""

import pytest
from test_ledger_accounts import _market_accounts

from msg.core.errors import Failure
from msg.core.models import Subject
from msg.storage.postgres import PostgresMetadataStore


async def _restore_legacy_escrow_identities(app, escrows):
    """Recreate the old FK shape as in the existing migration fixture."""
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'reference',
    [
        'certificate_parent',
        'relation_target',
        'relation_source',
        'ledger_actor',
    ],
)
async def test_migration_refuses_escrow_references_and_preserves_legacy_rows(installed, reference):
    app, root = installed
    _, _, _, _, bounty, order = await _market_accounts(app, root)
    escrow = bounty.data['funding']['body']['to_subject']
    escrows = (escrow, order.data['payment']['body']['to_subject'])
    if reference == 'ledger_actor':
        # Recreate this historical actor reference before converting the escrow
        # to its legacy identity representation. Bypass the current append-only
        # trigger only while constructing that old database state.
        async with app.metadata.transaction(write=True) as tx:
            tx.execute(
                'ALTER TABLE money_ledger DISABLE TRIGGER money_ledger_no_update', write=True
            )
            tx.execute(
                'UPDATE money_ledger SET actor=? WHERE seq=(SELECT MIN(seq) FROM money_ledger)',
                (escrow,),
                write=True,
            )
            tx.execute('ALTER TABLE money_ledger ENABLE TRIGGER money_ledger_no_update', write=True)
    async with app.metadata.transaction(write=False) as tx:
        ledger_before = tx.rows("""SELECT seq,id,kind,amount_minor,debit_account,
            credit_account,actor,reference,committed_at,policy_version,policy_digest,receipt
            FROM money_ledger ORDER BY seq""")
    await _restore_legacy_escrow_identities(app, escrows)

    async with app.metadata.transaction(write=True) as tx:
        if reference == 'certificate_parent':
            tx.execute(
                """INSERT INTO certificates(id,subject,parent,revoked,body)
                VALUES (?,?,?,?,?)""",
                ('migration_ref_certificate', 'u_root', escrow, 0, '{}'),
                write=True,
            )
        elif reference in {'relation_target', 'relation_source'}:
            revision = tx.one('SELECT id FROM revisions LIMIT 1')[0]
            source_id = escrow if reference == 'relation_source' else 'unrelated-source'
            target_id = escrow if reference == 'relation_target' else 'unrelated-target'
            tx.execute(
                """INSERT INTO relations(revision_id,source_id,type,target_id,
                target_revision,body) VALUES (?,?,?,?,?,?)""",
                (revision, source_id, 'migration-regression', target_id, None, '{}'),
                write=True,
            )

    async with app.metadata.transaction(write=False) as tx:
        identities_before = tx.rows(
            """SELECT id,kind,generation,body FROM identities
            WHERE id IN (?,?) ORDER BY id""",
            escrows,
        )
        accounts_before = tx.rows(
            """SELECT id,kind,subject_id,source_id FROM ledger_accounts
            WHERE id IN (?,?) ORDER BY id""",
            escrows,
        )

    with pytest.raises(RuntimeError, match='active reference'):
        PostgresMetadataStore(app.settings.server.postgres_dsn)

    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.rows(
                """SELECT id,kind,generation,body FROM identities
            WHERE id IN (?,?) ORDER BY id""",
                escrows,
            )
            == identities_before
        )
        assert (
            tx.rows(
                """SELECT id,kind,subject_id,source_id FROM ledger_accounts
            WHERE id IN (?,?) ORDER BY id""",
                escrows,
            )
            == accounts_before
        )
        assert (
            tx.rows("""SELECT seq,id,kind,amount_minor,debit_account,
            credit_account,actor,reference,committed_at,policy_version,policy_digest,receipt
            FROM money_ledger ORDER BY seq""")
            == ledger_before
        )
        if reference == 'certificate_parent':
            assert tx.one(
                'SELECT parent FROM certificates WHERE id=?', ('migration_ref_certificate',)
            ) == (escrow,)
        elif reference in {'relation_target', 'relation_source'}:
            row = tx.one("""SELECT source_id,target_id FROM relations
                WHERE type='migration-regression' LIMIT 1""")
            assert row[0] == (escrow if reference == 'relation_source' else 'unrelated-source')
            assert row[1] == (escrow if reference == 'relation_target' else 'unrelated-target')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'operation',
    [
        'UPDATE',
        'DELETE',
    ],
)
async def test_money_ledger_rejects_update_and_delete(installed, operation):
    app, root = installed
    # Seed real ledger entries first: an empty-table UPDATE/DELETE would pass
    # without ever invoking the append-only trigger.
    await _market_accounts(app, root)
    async with app.metadata.transaction(write=False) as tx:
        ledger_before = tx.rows("""SELECT seq,id,kind,amount_minor,debit_account,
            credit_account,receipt FROM money_ledger ORDER BY seq""")
    assert ledger_before
    seq = ledger_before[0][0]
    statement = (
        f'UPDATE money_ledger SET amount_minor=amount_minor WHERE seq={seq}'
        if operation == 'UPDATE'
        else f'DELETE FROM money_ledger WHERE seq={seq}'
    )

    with pytest.raises(Failure, match='constraint_conflict') as raised:
        async with app.metadata.transaction(write=True) as tx:
            tx.execute(statement, write=True)
    # Confirm the database rejected the row through the dedicated trigger,
    # rather than another constraint or a statement that matched no rows.
    assert 'append_only_money_ledger' in str(raised.value.__cause__)

    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.rows("""SELECT seq,id,kind,amount_minor,debit_account,
            credit_account,receipt FROM money_ledger ORDER BY seq""")
            == ledger_before
        )
