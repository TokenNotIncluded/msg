"""A disposable, Python-created PostgreSQL database for native conformance tests.

Requires explicit MSG_PG_TEST_ADMIN_DSN. Only a freshly created, unpredictable
msg_rust_test_* database is populated and removed. Never truncates an existing DB.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from msg.storage.postgres import PostgresMetadataStore, _postgres_sql


class Connection:
    def __init__(self, dsn):
        self.connection = psycopg.connect(dsn, autocommit=True)

    def execute(self, statement, parameters=()):
        return self.connection.execute(
            _postgres_sql(statement, has_parameters=bool(parameters)), parameters or None
        )

    def close(self):
        self.connection.close()


@contextmanager
def database():
    admin = os.environ.get('MSG_PG_TEST_ADMIN_DSN')
    if not admin:
        raise RuntimeError('MSG_PG_TEST_ADMIN_DSN is required for isolated PostgreSQL tests')
    config = conninfo_to_dict(admin)
    host = config.get('host', '')
    if host and not (host.startswith('/') or host in {'127.0.0.1', 'localhost', '::1'}):
        raise RuntimeError('PostgreSQL conformance only connects to a local test instance')
    name = 'msg_rust_test_' + uuid.uuid4().hex
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    dsn = make_conninfo(admin, dbname=name)
    old = os.environ.get('MSG_PARITY_POSTGRES_DSN')
    try:
        os.environ['MSG_PARITY_POSTGRES_DSN'] = dsn
        yield dsn
    finally:
        if old is None:
            os.environ.pop('MSG_PARITY_POSTGRES_DSN', None)
        else:
            os.environ['MSG_PARITY_POSTGRES_DSN'] = old
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


async def replace_fixture_storage(fixture, dsn):
    """Copy only deterministic public test records, then use real PG for both sides."""
    metadata = PostgresMetadataStore(dsn)
    with psycopg.connect(dsn) as connection:
        for table in (
            'resources',
            'identities',
            'credentials',
            'certificates',
            'settings',
            'memberships',
            'dm_conversations',
        ):
            records = fixture.conn.execute('SELECT * FROM ' + table)
            columns = [entry[0] for entry in records.description]
            query = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                sql.Identifier(table),
                sql.SQL(',').join(map(sql.Identifier, columns)),
                sql.SQL(',').join(sql.Placeholder() for _ in columns),
            )
            if table == 'settings':
                query += sql.SQL(' ON CONFLICT(key) DO UPDATE SET value=excluded.value')
            for row in records:
                connection.execute(query, row)
    fixture.conn.close()
    await fixture.metadata.close()
    fixture.conn = Connection(dsn)
    fixture.metadata = metadata
