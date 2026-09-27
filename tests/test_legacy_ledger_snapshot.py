"""Real ac083b3 database/backup -> current migration, with persistent failures."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import zipfile

import psycopg
from psycopg import sql
import pytest

from msg.admin.backups import restore
from msg.config import load_settings
from msg.storage import ledger_migration
from msg.storage.postgres import PostgresMetadataStore

FIXTURES = Path(__file__).parent/'fixtures'
HISTORY_TABLES = ('money_accounts', 'money_ledger', 'money_bank_roles', 'store_orders',
    'store_packages', 'store_deliveries', 'bounty_listings', 'bounty_challenges',
    'bounty_claims', 'resources', 'revisions', 'events', 'audit', 'results',
    'credentials', 'certificates', 'memberships', 'messages', 'jobs')


def snapshot(dsn):
    with psycopg.connect(dsn) as conn:
        facts = {table: conn.execute(sql.SQL('SELECT * FROM {} ORDER BY 1').format(
            sql.Identifier(table))).fetchall() for table in HISTORY_TABLES}
        facts['ledger_sequence'] = conn.execute('SELECT last_value,is_called FROM money_ledger_seq_seq').fetchone()
        facts['total_supply'] = conn.execute("""SELECT COALESCE(SUM(CASE kind
            WHEN 'mint' THEN amount_minor WHEN 'burn' THEN -amount_minor ELSE 0 END),0)
            FROM money_ledger""").fetchone()[0]
        facts['balances'] = conn.execute('''SELECT account_id,SUM(delta) FROM (
            SELECT credit_account account_id,amount_minor delta FROM money_ledger WHERE credit_account IS NOT NULL
            UNION ALL SELECT debit_account,-amount_minor FROM money_ledger WHERE debit_account IS NOT NULL
            ) e GROUP BY account_id ORDER BY account_id''').fetchall()
        return facts


@pytest.fixture(scope='session')
def legacy_market_archive(tmp_path_factory, pg_cluster):
    source = Path(os.environ.get('MSG_TEST_LEGACY_SOURCE', Path(__file__).parents[1]/'.legacy-ledger')).resolve()
    manifest = json.loads((FIXTURES/'legacy_market_source.json').read_text())
    assert source.is_dir(), ('set MSG_TEST_LEGACY_SOURCE to a checkout of ' + manifest['commit'])
    for name, expected in manifest['sha256'].items():
        assert hashlib.sha256((source/name).read_bytes()).hexdigest() == expected, name
    directory = tmp_path_factory.mktemp('real-legacy-market')
    database = 'msg_legacy_' + uuid4().hex
    dsn = pg_cluster.format(database=database)
    with psycopg.connect(pg_cluster.format(database='postgres'), autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    try:
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(source/'src'), str(source/'tests')))}
        process = subprocess.run([sys.executable, str(FIXTURES/'create_legacy_market.py'),
            str(source), str(directory), dsn], env=env, capture_output=True, timeout=120)
        assert process.returncode == 0, process.stderr.decode()
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT to_regclass('ledger_accounts')").fetchone() == (None,)
        original = snapshot(dsn)
        assert original['total_supply'] == 100_000_000
        archive = directory/'legacy.zip'
        info = json.loads((directory/'fixture.json').read_text())
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == info['backup_sha256']
        with zipfile.ZipFile(archive) as zipped:
            manifest = json.loads(zipped.read('manifest.json'))
            assert manifest['root_private_key_included'] is False
            assert not any('root.key' in name or 'root-private' in name for name in zipped.namelist())
        yield archive, original, info
    finally:
        with psycopg.connect(pg_cluster.format(database='postgres'), autocommit=True) as conn:
            conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database)))


@pytest.fixture
def restored_legacy(tmp_path, pg_dsn, legacy_market_archive):
    archive, before, info = legacy_market_archive
    result = restore(archive, tmp_path/'etc', tmp_path/'data', postgres_dsn=pg_dsn)
    assert result['status'] == 'restored'
    assert result['outbound'] == 'disabled_recovery_drill'
    assert snapshot(pg_dsn) == before
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT to_regclass('ledger_accounts')").fetchone() == (None,)
        escrows = {row[0] for row in conn.execute('''SELECT escrow_subject FROM store_orders
            UNION SELECT escrow_subject FROM bounty_listings''')}
        assert len(escrows) == 5
        identities = conn.execute('SELECT * FROM identities ORDER BY id').fetchall()
    return pg_dsn, before, info, escrows, identities, tmp_path/'etc'


def assert_migrated(dsn, before, escrows, identities):
    assert snapshot(dsn) == before
    with psycopg.connect(dsn) as conn:
        assert conn.execute('SELECT * FROM identities ORDER BY id').fetchall() == [row for row in identities if row[0] not in escrows]
        actual = conn.execute("""SELECT id,kind,subject_id,source_id FROM ledger_accounts
            WHERE kind<>'subject'""").fetchall()
        assert len(actual) == 5 and {row[0] for row in actual} == escrows
        assert all(row[2] is None and row[3] is not None for row in actual)
        for table, _, current, _ in ledger_migration._LEDGER_ACCOUNT_FKS:
            assert conn.execute('SELECT confrelid::regclass::text FROM pg_constraint WHERE conrelid=%s::regclass AND conname=%s',
                                (table, current)).fetchone() == ('ledger_accounts',)
    assert sum(balance for _, balance in before['balances']) == before['total_supply']


def test_real_old_backup_twice_and_two_concurrent_startups_keep_all_history(restored_legacy):
    dsn, before, _, escrows, identities, config = restored_legacy
    # Real threads, independent connections, and the actual schema advisory lock.
    with ThreadPoolExecutor(max_workers=2) as pool:
        instances = list(pool.map(PostgresMetadataStore, (dsn, dsn)))
    for instance in instances:
        asyncio.run(instance.close())
    assert_migrated(dsn, before, escrows, identities)
    instance = PostgresMetadataStore(dsn)
    asyncio.run(instance.close())
    assert_migrated(dsn, before, escrows, identities)
    assert (config/'recovery-drill.json').is_file()
    # Migration must not promote a restored installation or re-enable workers.
    with psycopg.connect(dsn) as conn:
        runtime = json.loads(conn.execute("SELECT value FROM settings WHERE key='runtime_config'").fetchone()[0])
        assert runtime['accept_writes'] is False
    for statement in ('UPDATE money_ledger SET amount_minor=amount_minor', 'DELETE FROM money_ledger'):
        with pytest.raises(psycopg.errors.CheckViolation, match='append_only_money_ledger'):
            with psycopg.connect(dsn) as conn:
                conn.execute(statement)
    assert snapshot(dsn) == before


@pytest.mark.parametrize('stage', ['_install_accounts', '_switch_account_foreign_keys',
                                   '_retire_legacy_identities', '_install_subject_account_trigger'])
def test_each_migration_stage_rolls_back_the_actual_old_database(restored_legacy, monkeypatch, stage):
    dsn, before, _, escrows, identities, _ = restored_legacy
    original = getattr(ledger_migration, stage)
    def interrupted(*args):
        original(*args)
        raise RuntimeError('injected migration interruption')
    with monkeypatch.context() as patched:
        patched.setattr(ledger_migration, stage, interrupted)
        with pytest.raises(RuntimeError, match='injected migration interruption'):
            PostgresMetadataStore(dsn)
    assert snapshot(dsn) == before
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT to_regclass('ledger_accounts')").fetchone() == (None,)
        assert conn.execute('SELECT * FROM identities ORDER BY id').fetchall() == identities
        for table, old, _, _ in ledger_migration._LEDGER_ACCOUNT_FKS:
            assert conn.execute('SELECT confrelid::regclass::text FROM pg_constraint WHERE conrelid=%s::regclass AND conname=%s',
                                (table, old)).fetchone() == ('identities',)
    # The same restored backup can be retried after interruption, without repair SQL.
    instance = PostgresMetadataStore(dsn)
    asyncio.run(instance.close())
    assert_migrated(dsn, before, escrows, identities)


@pytest.mark.parametrize('reference', ['body_owner', 'unknown_subject_column', 'credential', 'certificate_scope', 'unknown_json_table'])
def test_real_old_backup_rejects_active_semantic_references(restored_legacy, reference):
    dsn, _, info, escrows, identities, _ = restored_legacy
    escrow = sorted(escrows)[0]
    with psycopg.connect(dsn) as conn:
        if reference == 'body_owner':
            rid, raw = conn.execute('SELECT id,body FROM resources ORDER BY id LIMIT 1').fetchone()
            body = json.loads(raw); body['owner'] = escrow
            conn.execute('UPDATE resources SET body=%s WHERE id=%s', (json.dumps(body), rid))
        elif reference == 'unknown_subject_column':
            conn.execute('CREATE TABLE unknown_plugin_reference(subject_id TEXT)')
            conn.execute('INSERT INTO unknown_plugin_reference VALUES (%s)', (escrow,))
        elif reference == 'unknown_json_table':
            conn.execute('CREATE TABLE unknown_plugin_json(body TEXT)')
            conn.execute('INSERT INTO unknown_plugin_json VALUES (%s)', (json.dumps({'owner': escrow}),))
        elif reference == 'credential':
            conn.execute("INSERT INTO credentials VALUES ('unrelated-credential',%s,'{}')", (escrow,))
        else:
            rid, raw = conn.execute('SELECT id,body FROM certificates ORDER BY id LIMIT 1').fetchone()
            body = json.loads(raw)
            body['authority_sources'] = [{'id': escrow}]
            conn.execute('UPDATE certificates SET body=%s WHERE id=%s', (json.dumps(body), rid))
    before = snapshot(dsn)
    reason = 'unclassified legacy reference table' if reference == 'unknown_json_table' else 'active reference'
    with pytest.raises(RuntimeError, match=reason):
        PostgresMetadataStore(dsn)
    assert snapshot(dsn) == before
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT to_regclass('ledger_accounts')").fetchone() == (None,)
        assert conn.execute('SELECT * FROM identities ORDER BY id').fetchall() == identities


def test_plain_historical_text_is_not_an_identity_reference(restored_legacy):
    dsn, _, _, escrows, identities, _ = restored_legacy
    escrow = sorted(escrows)[0]
    with psycopg.connect(dsn) as conn:
        for table, field, value in [('resources', 'name', escrow),
                                   ('events', 'data', {'text': escrow, 'actor': escrow}),
                                   ('revisions', 'change_note', escrow)]:
            rid, raw = conn.execute(sql.SQL('SELECT id,body FROM {} ORDER BY id LIMIT 1').format(sql.Identifier(table))).fetchone()
            body = json.loads(raw); body[field] = value
            conn.execute(sql.SQL('UPDATE {} SET body=%s WHERE id=%s').format(sql.Identifier(table)), (json.dumps(body), rid))
    before = snapshot(dsn)
    instance = PostgresMetadataStore(dsn)
    asyncio.run(instance.close())
    assert_migrated(dsn, before, escrows, identities)
