"""A hosting process reads current authority without acquiring writer capabilities."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import uuid

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from msg.core.errors import Failure
from msg.core.models import ResourceRef
from msg.storage.git import GitContentStore
from msg.storage.postgres import PostgresMetadataStore


READ_TABLES = ('schema_version', 'resources', 'revisions', 'identities',
               'credentials', 'certificates', 'memberships', 'settings', 'csrs',
               'share_grants', 'share_grants_v2', 'topic_bans', 'dm_conversations',
               'dm_blocks', 'system_sources')


def runtime(settings, clock):
    assert importlib.util.find_spec('msg.hosting_runtime') is not None, \
        'hosting still has no independent read-only composition'
    from msg.hosting_runtime import HostingRuntime
    return HostingRuntime(settings, clock=clock)


@pytest.fixture
async def reader_settings(installed):
    app, _ = installed
    role = 'hosting_test_' + uuid.uuid4().hex
    password = uuid.uuid4().hex
    owner_dsn = app.settings.server.postgres_dsn
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        database = conn.info.dbname
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB '
                             'NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS').format(
                                 sql.Identifier(role), sql.Literal(password)))
        conn.execute(sql.SQL('REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC').format(
            sql.Identifier(database)))
        conn.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
            sql.Identifier(database), sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT SELECT ON {} TO {}').format(
            sql.SQL(', ').join(sql.Identifier('public', name) for name in READ_TABLES),
            sql.Identifier(role)))
    try:
        yield replace(app.settings, server=replace(app.settings.server,
            postgres_dsn=make_conninfo(owner_dsn, user=role, password=password)))
    finally:
        with psycopg.connect(owner_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@pytest.mark.asyncio
async def test_hosting_starts_without_service_secrets_or_release_writes(installed, reader_settings, monkeypatch):
    app, _ = installed
    keys = app.settings.service_keys
    moved = keys.with_name(keys.name + '-hidden')
    keys.rename(moved)
    async def forbidden_sync(*args, **kwargs):
        pytest.fail('read-only startup invoked release publication')
    monkeypatch.setattr('msg.bootstrap.sync_system_sources', forbidden_sync)
    try:
        service = runtime(reader_settings, app.clock)
        await service.load()
        try:
            for name in ('executor', 'online_signer', 'receipt_signer', '_token_secret',
                         '_vault_key', 'issued_token', 'record_token_delivery'):
                assert not hasattr(service, name), name
            from msg.extensions.hosting import hosting_app
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(service)),
                                         base_url='http://testserver') as http:
                page = await http.get('/@root/web/index.html')
                assert page.status_code == 200, page.text
                assert 'sandbox' in page.headers['content-security-policy']
                assert 'set-cookie' not in page.headers
                head = await http.head('/@root/web/index.html')
                assert head.status_code == 200 and head.content == b''
                part = await http.get('/@root/web/index.html', headers={'Range': 'bytes=0-7'})
                assert part.status_code == 206 and part.content == page.content[:8]
                cached = await http.get('/@root/web/index.html', headers={'If-None-Match': page.headers['etag']})
                assert cached.status_code == 304
                assert (await http.post('/@root/web/index.html', content=b'write')).status_code == 405
        finally:
            await service.close()
    finally:
        moved.rename(keys)


@pytest.mark.asyncio
async def test_hosting_catalog_keeps_contract_metadata_but_no_business_handlers(installed, reader_settings):
    app, _ = installed
    service = runtime(reader_settings, app.clock)
    assert [app.registry.describe(s) for s in app.registry.operations()] == [
        service.registry.describe(s) for s in service.registry.operations()]
    assert app.registry.capabilities() == service.registry.capabilities()
    for spec in service.registry.operations():
        with pytest.raises(Failure, match='read_only_role'):
            await spec.handler(None, None, None)
        with pytest.raises(Failure, match='read_only_role'):
            await spec.requirements(None, None)
    for manifest in service.registry._plugins.values():
        for spec in manifest.operations:
            assert spec.handler is service.registry.operation(spec.name, spec.version).handler


@pytest.mark.asyncio
async def test_postgres_reader_rejects_write_before_transaction(installed, reader_settings):
    store = PostgresMetadataStore(reader_settings.server.postgres_dsn,
                                  initialize=False, read_only=True)
    try:
        async with store.transaction(write=False) as tx:
            assert tx.one('SHOW transaction_read_only') == ('on',)
            assert tx.one('SELECT COUNT(*) FROM resources')[0] > 0
        with pytest.raises(Failure, match='read_only_role'):
            async with store.transaction(write=True):
                pytest.fail('write transaction was entered')
        with psycopg.connect(reader_settings.server.postgres_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE settings SET value=value WHERE key='active_root_certificate'")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_git_reader_preserves_read_and_rejects_all_mutations(installed):
    app, _ = installed
    async with app.metadata.transaction(write=False) as tx:
        rev = await tx.revision(ResourceRef(id='r_agents'))
    source = app.contents
    reader = GitContentStore(source.path, binary_dir=source.binary,
                             staging_dir=source.path/'must-not-create', read_only=True)
    assert not reader.staging.exists()
    assert await reader.read_bytes(rev.content) == await source.read_bytes(rev.content)
    for action in (lambda: reader.put_bytes(b'no'), lambda: reader.pin(rev.content, 'no'),
                   lambda: reader.unpin(rev.content, 'no'),
                   lambda: reader.commit_revision('r_rules', rev)):
        with pytest.raises(Failure, match='read_only_role'):
            await action()
    assert not reader.staging.exists()


@pytest.mark.asyncio
async def test_hosting_refuses_writer_database_credentials(installed):
    app, _ = installed
    service = runtime(app.settings, app.clock)
    try:
        with pytest.raises(Failure, match='hosting_database_role_not_readonly'):
            await service.load()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_missing_release_source_is_not_silently_repaired(installed, reader_settings):
    app, _ = installed
    async with app.metadata.transaction(write=True) as tx:
        tx.execute("DELETE FROM system_sources WHERE resource_id='r_agents'", write=True)
    service = runtime(reader_settings, app.clock)
    try:
        with pytest.raises(Failure, match='hosting_release_not_ready'):
            await service.load()
    finally:
        await service.close()
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT 1 FROM system_sources WHERE resource_id='r_agents'") is None


def test_hosting_unit_uses_dedicated_identity_and_read_only_mounts():
    unit = (Path(__file__).resolve().parents[1]/'deploy/msgd-hosting.service').read_text()
    assert 'User=msgd-hosting\n' in unit
    assert 'Group=msgd-hosting\n' in unit
    assert 'ReadWritePaths=' not in unit
    assert 'ReadOnlyPaths=' in unit
    assert '--config-dir /etc/msgd-hosting hosting' in unit
    assert '/var/lib/msgd/service' in unit
