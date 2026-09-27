"""Classify settlement parties without rewriting signed historical facts."""
import pytest

from msg.core.codec import canonical, wire
from msg.storage.postgres import PostgresMetadataStore
from test_ledger_accounts import _market_accounts
from test_ledger_migration_references import _restore_legacy_escrow_identities
from test_service import call


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['actor', 'recipient'])
async def test_migration_refuses_decision_identity_references_without_changing_journal(installed, field):
    app, root = installed
    _, _, _, buyer, bounty, order = await _market_accounts(app, root)
    escrows = (bounty.data['funding']['body']['to_subject'],
               order.data['payment']['body']['to_subject'])
    await _restore_legacy_escrow_identities(app, escrows)
    body = {'actor': buyer, 'recipient': buyer, field: escrows[0]}
    # Construct a corrupt restored row, not a successful public settlement.
    # Even an invalid signature is no excuse to retire a referenced identity.
    # No append-only trigger is disabled or existing journal row rewritten.
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('''INSERT INTO order_escrow_decisions
            (order_id,id,body,signature,source_proof,transaction_id)
            VALUES (?,?,?,?,?,?)''',
            (order.data['order']['id'], 'ed_legacy_reference', canonical(body).decode(),
             '{}', '{}', order.data['payment']['body']['transaction_id']), write=True)
        before = tx.rows('SELECT * FROM order_escrow_decisions ORDER BY id')
        ledger = tx.rows('SELECT * FROM money_ledger ORDER BY seq')
    with pytest.raises(RuntimeError, match='active reference'):
        PostgresMetadataStore(app.settings.server.postgres_dsn)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.rows('SELECT * FROM order_escrow_decisions ORDER BY id') == before
        assert tx.rows('SELECT * FROM money_ledger ORDER BY seq') == ledger
        assert tx.one('SELECT COUNT(*) FROM identities WHERE id IN (?,?)', escrows)[0] == 2
        assert tx.one('SELECT COUNT(*) FROM ledger_accounts WHERE id IN (?,?)', escrows)[0] == 0


@pytest.mark.asyncio
async def test_migration_preserves_real_signed_decision_and_ledger_on_repeated_startup(installed):
    app, root = installed
    _, _, key, buyer, bounty, order = await _market_accounts(app, root)
    result = await call(app, 'orders.cancel', {'order_id': order.data['order']['id']},
                        key=key, subject=buyer)
    assert result.status == 'ok', wire(result)
    escrows = (bounty.data['funding']['body']['to_subject'],
               order.data['payment']['body']['to_subject'])
    async with app.metadata.transaction(write=False) as tx:
        decisions = tx.rows('SELECT * FROM order_escrow_decisions ORDER BY id')
        ledger = tx.rows('SELECT * FROM money_ledger ORDER BY seq')
    assert len(decisions) == 1
    await _restore_legacy_escrow_identities(app, escrows)
    for _ in range(2):
        migrated = PostgresMetadataStore(app.settings.server.postgres_dsn)
        await migrated.close()
    async with app.metadata.transaction(write=False) as tx:
        assert tx.rows('SELECT * FROM order_escrow_decisions ORDER BY id') == decisions
        assert tx.rows('SELECT * FROM money_ledger ORDER BY seq') == ledger
        assert tx.one('SELECT COUNT(*) FROM identities WHERE id IN (?,?)', escrows)[0] == 0
        assert tx.one('SELECT COUNT(*) FROM ledger_accounts WHERE id IN (?,?)', escrows)[0] == 2
