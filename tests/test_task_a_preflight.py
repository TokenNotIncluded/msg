"""Read-only means no schema initialization, counter advance or report-side writes."""

import psycopg
import pytest

from msg.admin.preflight import inspect_database, preflight
from msg.config import write_example
from msg.core.errors import Failure
from msg.storage.postgres import PostgresMetadataStore


def tree(path):
    return {
        str(file.relative_to(path)): file.read_bytes() for file in path.rglob('*') if file.is_file()
    }


def test_preflight_does_not_initialize_an_empty_database(pg_dsn, tmp_path):
    settings = write_example(
        tmp_path / 'etc', tmp_path / 'data', 'http://testserver', postgres_dsn=pg_dsn
    )
    before = tree(tmp_path)
    result = preflight(settings.config_dir)
    assert result['decision'] == 'blocked' and result['mutation_performed'] is False
    assert result['database']['state'] == 'empty'
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        ).fetchone() == (0,)
    assert tree(tmp_path) == before
    assert pg_dsn not in str(result)
    assert all(item['status'] == 'missing' for item in result['field_evidence'])


def test_existing_schema_is_checked_without_touching_sequences_or_files(pg_dsn, tmp_path):
    PostgresMetadataStore(pg_dsn)
    settings = write_example(
        tmp_path / 'etc', tmp_path / 'data', 'http://testserver', postgres_dsn=pg_dsn
    )
    before = tree(tmp_path)
    with psycopg.connect(pg_dsn) as conn:
        seq = conn.execute('SELECT last_value,is_called FROM money_ledger_seq_seq').fetchone()
    result = preflight(settings.config_dir)
    assert result['database']['read_only'] is True
    assert result['database']['ledger']['checks'] == 'passed'
    assert result['decision'] == 'blocked'  # A consistent test DB is not a field attestation.
    assert result['database']['ca_inventory'] == []
    with psycopg.connect(pg_dsn) as conn:
        assert (
            conn.execute('SELECT last_value,is_called FROM money_ledger_seq_seq').fetchone() == seq
        )
        assert conn.execute('SELECT COUNT(*) FROM audit').fetchone() == (0,)
        assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone() == (0,)
    assert tree(tmp_path) == before


def test_inspection_refuses_a_writable_transaction(pg_dsn):
    with (
        psycopg.connect(pg_dsn) as conn,
        pytest.raises(Failure, match='^preflight_read_only_required$'),
    ):
        inspect_database(conn)
