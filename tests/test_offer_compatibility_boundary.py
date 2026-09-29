"""Legacy catalogue rows must satisfy the same issuer limits as new local offers."""
import pytest

from msg.plugins.hosting_capacity import MAX_CAPACITY_BYTES
from test_market_redemption import setup_offer
from test_service import call


@pytest.mark.asyncio
@pytest.mark.parametrize(('column', 'value'), [
    ('unit', 'megabyte'),
    ('max_quantity', MAX_CAPACITY_BYTES + 1),
    ('duration_seconds', 315360001),
])
async def test_invalid_legacy_offer_is_hidden_and_cannot_fund(installed, column, value):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(f'UPDATE server_offers SET {column}=? WHERE offer_id=?',
                   (value, args['offer_id']), write=True)
    tables = ('money_ledger', 'money_purchases', 'resource_entitlements', 'ledger_accounts')
    async with app.metadata.transaction(write=False) as tx:
        before = {table: tx.rows('SELECT * FROM ' + table + ' ORDER BY id') for table in tables}
        original = tx.rows('SELECT * FROM server_offers')
    catalog = await call(app, 'money.offers', {})
    assert catalog.status == 'ok' and len(catalog.data['offers']) == 0
    for version in (1, 2):
        result = await call(app, 'money.redeem', args, key=key, subject=owner,
                            contract_version=version)
        assert result.status == 'error' and result.error.code == 'offer_not_found'
    async with app.metadata.transaction(write=False) as tx:
        assert {table: tx.rows('SELECT * FROM ' + table + ' ORDER BY id') for table in tables} == before
        assert tx.rows('SELECT * FROM server_offers') == original


@pytest.mark.asyncio
async def test_catalog_filter_does_not_rewrite_or_invalidate_locked_purchase(installed):
    app, root = installed
    key, owner, args, _ = await setup_offer(app, root)
    result = await call(app, 'money.redeem', {**args, 'defer': True}, key=key,
                        subject=owner, contract_version=2, rid='legacy-compatible-purchase')
    assert result.status == 'ok'
    original = result.data['purchase']
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('UPDATE server_offers SET unit=? WHERE offer_id=?',
                   ('megabyte', args['offer_id']), write=True)
        signed_funding = tx.one('SELECT receipt FROM money_ledger WHERE id=?',
                               (original['funding_transaction_id'],))
    assert len((await call(app, 'money.offers', {})).data['offers']) == 0
    read = await call(app, 'money.purchase_get', {'purchase_id': original['id']},
                      key=key, subject=owner)
    assert read.data['purchase'] == original
    settled = await call(app, 'money.purchase_settle', {'purchase_id': original['id']},
                         key=key, subject=owner)
    assert settled.status == 'ok'
    assert settled.data['purchase']['state'] == 'settled'
    assert settled.data['purchase']['offer_snapshot'] == original['offer_snapshot']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT receipt FROM money_ledger WHERE id=?',
                      (original['funding_transaction_id'],)) == signed_funding
