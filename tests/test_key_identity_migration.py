"""Retire login data atomically without removing native identity or API limits."""

import sqlite3

import pytest

from msg.storage.key_identity_migration import migrate_key_identity


@pytest.fixture(params=['sqlite', 'postgres'])
def database(request):
    postgres = request.param == 'postgres'
    if postgres:
        import psycopg

        conn = psycopg.connect(request.getfixturevalue('pg_dsn'), autocommit=True)
    else:
        conn = sqlite3.connect(':memory:', isolation_level=None)
    for table in ('credential_states', 'oauth_states'):
        conn.execute(
            f'CREATE TABLE {table} (id TEXT PRIMARY KEY, kind TEXT, expires TEXT, body TEXT)'
        )
    conn.execute('CREATE TABLE identities (id TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE content (id TEXT PRIMARY KEY, body TEXT)')
    conn.execute('CREATE TABLE login_bindings (id TEXT PRIMARY KEY)')
    conn.execute("INSERT INTO identities VALUES ('u_keep')")
    conn.execute("INSERT INTO content VALUES ('p_keep', 'retained content')")
    conn.execute("INSERT INTO login_bindings VALUES ('old-provider-binding')")
    conn.execute("INSERT INTO oauth_states VALUES ('api:keep','api','2030-01-01','native-source')")
    for kind in ('session', 'family', 'pending', 'code', 'login'):
        conn.execute(
            f"INSERT INTO oauth_states VALUES ('{kind}:old','{kind}','2030-01-01','retired')"
        )
    yield conn, postgres
    conn.close()


def test_migration_removes_only_retired_login_state_and_is_idempotent(database):
    conn, postgres = database
    for _ in range(2):
        conn.execute('BEGIN')
        migrate_key_identity(conn, postgres=postgres)
        conn.commit()
        assert conn.execute('SELECT id,body FROM credential_states').fetchall() == [
            ('api:keep', 'native-source')
        ]
        assert conn.execute('SELECT id FROM identities').fetchall() == [('u_keep',)]
        assert conn.execute('SELECT id,body FROM content').fetchall() == [
            ('p_keep', 'retained content')
        ]
    if postgres:
        assert conn.execute(
            "SELECT to_regclass('public.oauth_states'), to_regclass('public.login_bindings')"
        ).fetchone() == (None, None)
    else:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('oauth_states','login_bindings')"
            ).fetchall()
            == []
        )


def test_existing_native_source_is_not_overwritten_during_migration(database):
    conn, postgres = database
    conn.execute(
        "INSERT INTO credential_states VALUES ('api:keep','api','2030-01-01','current-source')"
    )
    conn.execute('BEGIN')
    migrate_key_identity(conn, postgres=postgres)
    conn.commit()
    assert conn.execute('SELECT body FROM credential_states').fetchall() == [('current-source',)]


def test_failed_upgrade_rolls_back_removed_tables_and_copied_source(database):
    conn, postgres = database
    conn.execute('BEGIN')
    migrate_key_identity(conn, postgres=postgres)
    conn.rollback()
    assert conn.execute('SELECT COUNT(*) FROM oauth_states').fetchone() == (6,)
    assert conn.execute('SELECT id FROM login_bindings').fetchone() == ('old-provider-binding',)
    assert conn.execute('SELECT COUNT(*) FROM credential_states').fetchone() == (0,)
