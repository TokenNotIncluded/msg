"""Storage capabilities for a process which must never initialize or publish.

PostgreSQL grants are an independent boundary, not replaced by READ ONLY or a
Python wrapper. Content reads share the exact existing Git/CAS implementation,
without constructing a writer or exposing its publication methods.
"""
from contextlib import asynccontextmanager
from pathlib import Path
import os

from msg.core.errors import require
from msg.storage.git import GitContentStore
from msg.storage.postgres import PostgresMetadataStore


HOSTING_READ_TABLES = frozenset({
    'schema_version', 'resources', 'revisions', 'identities', 'memberships',
    'credentials', 'certificates', 'settings', 'share_grants', 'share_grants_v2',
    'dm_conversations', 'system_sources',
})


def require_readonly_role(tx):
    """Refuse owner/writer/SET ROLE/secret-table authority; never repair grants."""
    role = tx.one('''SELECT current_user, session_user, rolsuper, rolcreaterole,
        rolcreatedb, rolreplication, rolbypassrls FROM pg_catalog.pg_roles
        WHERE rolname=current_user''')
    require(role is not None and role[0] == role[1] and not any(role[2:]),
            'hosting_database_not_readonly')
    require(tx.one('''SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname<>current_user AND pg_has_role(oid,'MEMBER') LIMIT 1''') is None,
        'hosting_database_not_readonly')
    require(not tx.one("SELECT has_database_privilege(current_database(),'CREATE')")[0],
            'hosting_database_not_readonly')
    require(tx.one('''SELECT 1 FROM pg_catalog.pg_namespace
        WHERE left(nspname,3)<>'pg_' AND nspname<>'information_schema'
        AND has_schema_privilege(oid,'CREATE') LIMIT 1''') is None,
        'hosting_database_not_readonly')
    for schema, name, readable, writable in tx.rows('''
        SELECT n.nspname,c.relname,has_any_column_privilege(c.oid,'SELECT'),
            (has_table_privilege(c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
             OR has_any_column_privilege(c.oid,'INSERT,UPDATE,REFERENCES'))
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','f')
        AND left(n.nspname,3)<>'pg_' AND n.nspname<>'information_schema'
    '''):
        require(not writable and (not readable or
                (schema == 'public' and name in HOSTING_READ_TABLES)),
                'hosting_database_not_readonly')
    # Fetch sequence OIDs first: SQL predicates do not impose evaluation order,
    # and has_sequence_privilege must never receive an index/table OID.
    for (oid,) in tx.rows('''SELECT c.oid FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind='S' AND left(n.nspname,3)<>'pg_' '''):
        require(not tx.one("SELECT has_sequence_privilege(?::oid,'USAGE,UPDATE')", (oid,))[0],
                'hosting_database_not_readonly')
    # A callable SECURITY DEFINER routine can reintroduce its owner's privileges.
    require(tx.one('''SELECT 1 FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE p.prosecdef AND left(n.nspname,3)<>'pg_'
        AND n.nspname<>'information_schema'
        AND has_function_privilege(p.oid,'EXECUTE') LIMIT 1''') is None,
        'hosting_database_not_readonly')


class ReadOnlyPostgresStore:
    """Reuse canonical sessions/transactions, without migration or a write API."""
    def __init__(self, dsn):
        self._store = PostgresMetadataStore(dsn, initialize=False)

    @asynccontextmanager
    async def transaction(self, *, write=False):
        require(not write, 'read_only_transaction')
        async with self._store.transaction(write=False) as tx:
            # Explicit ordering prevents writable/temp schemas from shadowing
            # the installed public schema or PostgreSQL's privilege functions.
            tx.execute('SET LOCAL search_path = pg_catalog, public, pg_temp')
            require_readonly_role(tx)
            yield tx

    async def close(self):
        await self._store.close()


class GitContentReader:
    """Read existing content; no mkdir, init, staging, pin, write or writer pointer."""
    def __init__(self, path, *, binary_dir=None):
        self.path = Path(path)
        self.repo = self.path / 'private.git'
        self.index = self.path / 'index'
        self.binary = Path(binary_dir) if binary_dir is not None else self.path / 'binary'
        require(all(p.is_dir() for p in (self.path, self.repo, self.index, self.binary)),
                'content_store_not_initialized')
        self.env = {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8',
            'HOME': str(self.path), 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_OPTIONAL_LOCKS': '0',
            # The approved content repository is owned by the writer, not this UID.
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'safe.directory',
            'GIT_CONFIG_VALUE_0': str(self.repo),
        }

    # Unbound implementations, not a wrapped GitContentStore instance. Keep the
    # original limits, references, streaming, range and truncation behavior.
    _key = staticmethod(GitContentStore._key)
    _entry = GitContentStore._entry
    read = GitContentStore.read
    read_bytes = GitContentStore.read_bytes
