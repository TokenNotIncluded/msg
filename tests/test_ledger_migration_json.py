"""Migration must inspect typed JSON references, never arbitrary text matches."""

import json

import pytest
from test_ledger_accounts import _market_accounts
from test_ledger_migration_references import _restore_legacy_escrow_identities

from msg.storage.postgres import PostgresMetadataStore


@pytest.mark.parametrize(
    'table,field',
    [
        ('resources', 'owner'),
        ('resources', 'modified_by'),
        ('revisions', 'author'),
        ('events', 'actor'),
        ('certificates', 'subject_id'),
    ],
)
async def test_migration_refuses_authoritative_json_identity_reference(installed, table, field):
    app, root = installed
    _, _, _, _, bounty, order = await _market_accounts(app, root)
    escrows = (
        bounty.data['funding']['body']['to_subject'],
        order.data['payment']['body']['to_subject'],
    )
    await _restore_legacy_escrow_identities(app, escrows)
    async with app.metadata.transaction(write=True) as tx:
        rid, raw = tx.one(f'SELECT id,body FROM {table} ORDER BY id LIMIT 1')
        body = json.loads(raw)
        body[field] = escrows[0]
        encoded = json.dumps(body)
        tx.execute(f'UPDATE {table} SET body=? WHERE id=?', (encoded, rid), write=True)
    with pytest.raises(RuntimeError, match='active reference'):
        PostgresMetadataStore(app.settings.server.postgres_dsn)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one(f'SELECT body FROM {table} WHERE id=?', (rid,))[0] == encoded
        assert tx.one('SELECT COUNT(*) FROM identities WHERE id IN (?,?)', escrows)[0] == 2
        assert tx.one('SELECT COUNT(*) FROM ledger_accounts WHERE id IN (?,?)', escrows)[0] == 0


async def test_json_reference_contract_covers_the_current_schema(installed):
    from msg.storage.ledger_migration import _JSON_IDENTITY_REFS

    app, _ = installed
    async with app.metadata.transaction(write=False) as tx:
        actual = {
            row[0]
            for row in tx.rows("""SELECT table_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND column_name='body'""")
        }
    assert actual == set(_JSON_IDENTITY_REFS)


@pytest.mark.parametrize('reference', ['subject', 'ceiling'])
async def test_migration_refuses_oauth_identity_references(installed, reference):
    app, root = installed
    _, _, _, _, bounty, order = await _market_accounts(app, root)
    escrows = (
        bounty.data['funding']['body']['to_subject'],
        order.data['payment']['body']['to_subject'],
    )
    await _restore_legacy_escrow_identities(app, escrows)
    body = (
        {'subject': escrows[0]}
        if reference == 'subject'
        else {'ceiling': [{'scope': {'resource_id': escrows[0]}}]}
    )
    encoded = json.dumps(body)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'INSERT INTO oauth_states VALUES (?,?,?,?)',
            ('migration-oauth', 'session', '2999-01-01T00:00:00Z', encoded),
            write=True,
        )
    with pytest.raises(RuntimeError, match='active reference'):
        PostgresMetadataStore(app.settings.server.postgres_dsn)
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one('SELECT body FROM oauth_states WHERE id=?', ('migration-oauth',))[0] == encoded
        )
        assert tx.one('SELECT COUNT(*) FROM identities WHERE id IN (?,?)', escrows)[0] == 2
        assert tx.one('SELECT COUNT(*) FROM ledger_accounts WHERE id IN (?,?)', escrows)[0] == 0


def test_reference_paths_ignore_untyped_dictionary_keys():
    from msg.storage.ledger_migration import _values_at

    body = {
        'grants': [{'scope': {'resource_id': 'known'}}],
        'data': {'actor': 'untrusted-text'},
        'actors': {'*': 'not-an-array'},
    }
    assert list(_values_at(body, 'grants.*.scope.resource_id'.split('.'))) == ['known']
    assert list(_values_at(body, 'actors.*'.split('.'))) == []
    assert list(_values_at(body, 'missing'.split('.'))) == []
