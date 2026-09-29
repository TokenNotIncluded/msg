"""Retained #174 authority cases on #173's single hosting implementation.

These tests use disposable PostgreSQL objects and real signed calls. No second
runtime, shared installer or alternative publication policy is introduced.
"""
import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
import pytest

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.security.crypto import Ed25519Signer
from test_hosting_runtime import reader_settings, runtime_class
from test_hosting_runtime_isolation import preview
from test_service import call


async def test_revoked_signing_key_cannot_reuse_a_warm_private_preview(installed, reader_settings):
    app, _ = installed
    key, subject, _, payload, request = await preview(app, 'revocable-reader')
    replacement = Ed25519Signer.generate()
    public = b64(replacement.public_key)
    proof = replacement.sign(canonical({'subject_id': subject, 'public_key': public}), purpose='key-add')
    added = await call(app, 'identity.key_add', {'public_key': public,
        'possession_proof': wire(proof), 'ceiling': wire(app.primary_ceiling())}, key=key, subject=subject)
    assert added.status == 'ok', wire(added)
    service = runtime_class()(reader_settings, clock=app.clock)
    await service.load()
    try:
        from msg.extensions.hosting import hosting_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(service)),
                                     base_url=app.settings.service_url) as http:
            headers = {'X-Msg-Request': request['header']}
            accepted = await http.get(request['path'], headers=headers)
            assert accepted.status_code == 200 and accepted.content == payload
            revoked = await call(app, 'identity.key_revoke', {'key_id': key.key_id},
                                 key=replacement, subject=subject)
            assert revoked.status == 'ok', wire(revoked)
            for method, extra in (('GET', {}), ('HEAD', {}), ('GET', {'Range': 'bytes=0-3'}),
                                  ('GET', {'If-None-Match': accepted.headers['etag']})):
                denied = await http.request(method, request['path'], headers={**headers, **extra})
                assert denied.status_code == 403 and payload not in denied.content
    finally:
        await service.close()


async def test_column_only_update_is_not_a_readonly_database_role(installed, reader_settings):
    app, _ = installed
    reader = conninfo_to_dict(reader_settings.server.postgres_dsn)['user']
    with psycopg.connect(app.settings.server.postgres_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('GRANT UPDATE(value) ON settings TO {}').format(sql.Identifier(reader)))
    service = runtime_class()(reader_settings, clock=app.clock)
    try:
        with pytest.raises(Failure, match='hosting_database_not_readonly'):
            await service.load()
    finally:
        await service.close()


@pytest.mark.parametrize('kind', ['table', 'schema', 'sequence', 'database'])
async def test_owner_can_restore_revoked_rights_and_must_not_be_a_reader(installed, reader_settings, kind):
    app, _ = installed
    reader = conninfo_to_dict(reader_settings.server.postgres_dsn)['user']
    name = 'hosting_owned_probe'
    owner_dsn = app.settings.server.postgres_dsn
    service = runtime_class()(reader_settings, clock=app.clock)
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        original_owner, database = conn.info.user, conn.info.dbname
        if kind == 'table':
            conn.execute('CREATE TABLE public.hosting_owned_probe (value integer)')
            conn.execute(sql.SQL('ALTER TABLE public.hosting_owned_probe OWNER TO {}').format(sql.Identifier(reader)))
            grant, privilege = 'TABLE public.hosting_owned_probe', 'UPDATE'
        elif kind == 'schema':
            conn.execute(sql.SQL('CREATE SCHEMA hosting_owned_probe AUTHORIZATION {}').format(sql.Identifier(reader)))
            grant, privilege = 'SCHEMA hosting_owned_probe', 'CREATE'
        elif kind == 'sequence':
            conn.execute('CREATE SEQUENCE public.hosting_owned_probe')
            conn.execute(sql.SQL('ALTER SEQUENCE public.hosting_owned_probe OWNER TO {}').format(sql.Identifier(reader)))
            grant, privilege = 'SEQUENCE public.hosting_owned_probe', 'USAGE'
        else:
            conn.execute(sql.SQL('ALTER DATABASE {} OWNER TO {}').format(sql.Identifier(database), sql.Identifier(reader)))
            grant = sql.SQL('DATABASE {}').format(sql.Identifier(database)).as_string(conn)
            privilege = 'CREATE, TEMPORARY'
        # Keep CONNECT for a database owner; revoke every object ACL otherwise.
        revoked = privilege if kind == 'database' else 'ALL'
        query = sql.SQL('REVOKE {} ON {} FROM {}').format(sql.SQL(revoked), sql.SQL(grant), sql.Identifier(reader))
        conn.execute(query)
    try:
        # Demonstrate why current ACL bits alone cannot establish a read-only
        # boundary: the owner can recover rights without an administrator.
        with psycopg.connect(reader_settings.server.postgres_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('GRANT {} ON {} TO {}').format(
                sql.SQL(privilege), sql.SQL(grant), sql.Identifier(reader)))
            conn.execute(query)
        with pytest.raises(Failure, match='hosting_database_not_readonly'):
            await service.load()
    finally:
        await service.close()
        if kind == 'database':
            # DROP OWNED does not restore database ownership; restore it before
            # the shared role fixture removes the disposable reader login.
            with psycopg.connect(owner_dsn, autocommit=True) as conn:
                conn.execute(sql.SQL('ALTER DATABASE {} OWNER TO {}').format(
                    sql.Identifier(database), sql.Identifier(original_owner)))
