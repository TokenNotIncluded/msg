"""The hosting process reads current authority without owning a write service."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import importlib
import uuid

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.core.requests import request_for
from test_service import NOW, call, register

# Independent acceptance inventory: no ledger, token delivery, vault or job access.
READ_TABLES = (
    'schema_version', 'resources', 'revisions', 'identities', 'memberships',
    'credentials', 'certificates', 'settings', 'share_grants', 'share_grants_v2',
    'dm_conversations', 'system_sources',
)


def runtime_class():
    # Import inside tests so the pre-implementation baseline fails tests, not collection.
    return importlib.import_module('msg.hosting_runtime').HostingRuntime


@pytest.fixture
async def reader_settings(installed):
    app, _ = installed
    role = 'hosting_' + uuid.uuid4().hex
    password = uuid.uuid4().hex
    admin_dsn = app.settings.server.postgres_dsn
    connection_info = conninfo_to_dict(admin_dsn)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB '
                             'NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS').format(
                                 sql.Identifier(role), sql.Literal(password)))
        conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
            sql.Identifier(connection_info['dbname']), sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT SELECT ON {} TO {}').format(
            sql.SQL(', ').join(sql.Identifier('public', name) for name in READ_TABLES),
            sql.Identifier(role)))
    dsn = make_conninfo(admin_dsn, user=role, password=password)
    settings = replace(app.settings, server=replace(app.settings.server,
                       postgres_dsn=dsn, valkey_url=None, mail=None),
                       public_web_origin=app.settings.service_url)
    try:
        yield settings
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


async def website(app):
    key, subject, certificate = await register(app, 'readonly-host')
    site = await call(app, 'hosting.create', {'parent': '/@readonly-host', 'name': 'web'},
                      key=key, subject=subject)
    source = await call(app, 'content.file_put', {'parent': '/@readonly-host/files',
        'name': 'index.html', 'data': b64(b'<h1>published</h1>'), 'media_type': 'text/html'},
        key=key, subject=subject)
    assert site.status == source.status == 'ok'
    entries = [{'path': 'index.html', 'source': wire(source.resources[0])}]
    published = await call(app, 'hosting.deploy', {'id': site.resources[0].id, 'entries': entries},
        key=key, subject=subject, expected=((site.resources[0].id, site.data['generation']),))
    assert published.status == 'ok', wire(published)
    preview = await call(app, 'hosting.preview', {'id': site.resources[0].id, 'entries': entries},
        key=key, subject=subject, expected=((site.resources[0].id, published.data['generation']),))
    assert preview.status == 'ok', wire(preview)
    return key, subject, certificate, site, preview


def isolated(response):
    assert 'sandbox' in response.headers['content-security-policy']
    assert 'allow-same-origin' not in response.headers['content-security-policy']
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert 'set-cookie' not in response.headers


async def test_hosting_load_has_no_application_signers_or_startup_writes(installed, reader_settings, monkeypatch):
    app, _ = installed
    await website(app)
    cls = runtime_class()
    from msg.application import Application
    import msg.bootstrap
    from msg.security.crypto import Ed25519Signer

    def forbidden(*args, **kwargs):
        pytest.fail('read-only hosting assembled a signer, full Application or installer')

    monkeypatch.setattr(Application, '__init__', forbidden)
    monkeypatch.setattr(Ed25519Signer, 'from_bytes', forbidden)
    monkeypatch.setattr(msg.bootstrap, 'sync_system_sources', forbidden)
    before = sorted(str(p.relative_to(reader_settings.server.content_dir))
                    for p in reader_settings.server.content_dir.rglob('*'))
    keys = reader_settings.service_keys
    original_mode = keys.stat().st_mode & 0o777
    keys.chmod(0)
    reader = cls(reader_settings, clock=lambda: NOW)
    try:
        # This is an actual OS denial on the unprivileged CI runner, not a mock.
        with pytest.raises(PermissionError):
            (keys / 'online.key').read_bytes()
        await reader.load()
        for name in ('executor', 'online_signer', 'receipt_signer', '_token_secret',
                     '_vault_key', 'token_delivery', 'online_issuer', 'application', 'app'):
            assert not hasattr(reader, name), name
        assert not hasattr(reader.contents, 'put')
        assert not hasattr(reader.contents, 'pin')
        from msg.extensions.hosting import hosting_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(reader)),
                                     base_url=reader_settings.service_url) as http:
            response = await http.get('/@readonly-host/web/')
            assert response.status_code == 200 and response.content == b'<h1>published</h1>'
            isolated(response)
        assert before == sorted(str(p.relative_to(reader_settings.server.content_dir))
                                for p in reader_settings.server.content_dir.rglob('*'))
    finally:
        keys.chmod(original_mode)
        await reader.close()


async def test_hosting_reuses_exact_registry_contracts_but_no_business_callbacks(installed, reader_settings):
    app, _ = installed
    reader = runtime_class()(reader_settings, clock=lambda: NOW)
    await reader.load()
    try:
        assert reader.registry.catalog() == app.registry.catalog()
        assert reader.registry.capabilities() == app.registry.capabilities()
        assert reader.registry.resource_types() == app.registry.resource_types()
        for operation in reader.registry.operations():
            assert reader.registry.schema(operation.input_schema) == app.registry.schema(operation.input_schema)
            assert operation.handler.__closure__ is None
            assert operation.requirements.__closure__ is None
            with pytest.raises(Failure, match='readonly_registry'):
                await operation.handler(None, None, None)
            with pytest.raises(Failure, match='readonly_registry'):
                await operation.requirements(None, None)
        for manifest in reader.registry._plugins.values():
            assert all(spec is reader.registry.operation(spec.name, spec.version)
                       for spec in manifest.operations)
    finally:
        await reader.close()


async def test_hosting_database_has_both_transaction_and_role_write_denial(reader_settings):
    reader = runtime_class()(reader_settings, clock=lambda: NOW)
    await reader.load()
    try:
        with pytest.raises(Failure, match='read_only_transaction'):
            async with reader.metadata.transaction(write=True):
                pytest.fail('write transaction was entered')
        async with reader.metadata.transaction(write=False) as tx:
            assert tx.one('SHOW transaction_read_only') == ('on',)
            with pytest.raises(Failure, match='read_only_transaction'):
                tx.execute("UPDATE settings SET value=value", write=True)
        # Even bypassing the wrapper and READ ONLY transaction, DB grants deny it.
        with psycopg.connect(reader_settings.server.postgres_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute('UPDATE public.settings SET value=value')
        with psycopg.connect(reader_settings.server.postgres_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute('SELECT * FROM public.token_deliveries')
    finally:
        await reader.close()


async def test_hosting_rejects_writer_database_account(installed):
    app, _ = installed
    reader = runtime_class()(replace(app.settings, public_web_origin=app.settings.service_url),
                             clock=lambda: NOW)
    try:
        with pytest.raises(Failure, match='hosting_database_not_readonly'):
            await reader.load()
    finally:
        await reader.close()


@pytest.mark.parametrize('damage', ['trust', 'release', 'content'])
async def test_hosting_missing_or_stale_installation_fails_without_repair(installed, reader_settings, damage):
    app, _ = installed
    expected = {'trust': 'root_not_initialized', 'release': 'hosting_installation_stale',
                'content': 'content_store_not_initialized'}[damage]
    settings = reader_settings
    if damage == 'trust':
        settings.trust_file.unlink()
    elif damage == 'release':
        async with app.metadata.transaction(write=True) as tx:
            tx.execute("UPDATE system_sources SET source_digest='sha256:stale' WHERE resource_id='r_agents'",
                       write=True)
    else:
        settings = replace(settings, server=replace(settings.server,
                           content_dir=settings.server.content_dir.parent / 'missing-content'))
    reader = runtime_class()(settings, clock=lambda: NOW)
    try:
        with pytest.raises(Failure, match=expected):
            await reader.load()
        if damage == 'trust':
            assert not settings.trust_file.exists()
        elif damage == 'content':
            assert not settings.server.content_dir.exists()
        else:
            async with app.metadata.transaction(write=False) as tx:
                assert tx.one("SELECT source_digest FROM system_sources WHERE resource_id='r_agents'") == ('sha256:stale',)
    finally:
        await reader.close()


async def test_hosting_private_preview_head_range_revocation_and_zero_effects(installed, reader_settings):
    app, _ = installed
    key, subject, certificate, site, preview = await website(app)
    reader = runtime_class()(reader_settings, clock=lambda: NOW)
    await reader.load()
    from msg.extensions.hosting import hosting_app
    candidate = preview.resources[0].id
    path = f'/@readonly-host/web/_preview/{candidate}/index.html'
    packet = request_for('discovery.raw', {'id': candidate}, app.settings.service_url,
        signer=key, subject=subject, certificates=(certificate,), expires_at=NOW+timedelta(seconds=120))
    headers = {'X-Msg-Request': b64(canonical(wire(packet)))}
    async with app.metadata.transaction(write=False) as tx:
        before = {table: tuple(tx.rows(f'SELECT * FROM {table}')) for table in
                  ('resources', 'revisions', 'events', 'results', 'credentials', 'jobs')}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(reader)),
                                     base_url=app.settings.service_url) as http:
            assert (await http.get(path)).status_code == 403
            page = await http.get(path, headers=headers)
            assert page.status_code == 200 and page.content == b'<h1>published</h1>'
            isolated(page)
            assert (await http.head(path, headers=headers)).content == b''
            assert (await http.get(path, headers={**headers, 'Range': 'bytes=0-3'})).content == b'<h1>'
            assert (await http.get(path, headers={**headers, 'If-None-Match': page.headers['etag']})).status_code == 304
            assert (await http.get('/@readonly-host/web/missing.html')).status_code == 404
            assert (await http.post(path, headers=headers, content=b'write')).status_code == 405
            assert (await http.post('/-/p/content.post_create', content=b'{}')).status_code == 404
            assert (await http.get('/@readonly-host/web/', headers={'Host': 'other.invalid'})).status_code == 403
            async with app.metadata.transaction(write=False) as tx:
                assert before == {table: tuple(tx.rows(f'SELECT * FROM {table}')) for table in before}
            revoked = await call(app, 'content.chmod', {'id': candidate, 'mode': '0000'},
                key=key, subject=subject, expected=((candidate, preview.data['generation']),))
            assert revoked.status == 'ok'
            assert (await http.get(path, headers=headers)).status_code == 403
    finally:
        await reader.close()


@pytest.mark.parametrize('gate', ['database', 'marker'])
async def test_hosting_checks_quarantine_before_disclosing_bytes(installed, reader_settings, gate):
    app, _ = installed
    await website(app)
    reader = runtime_class()(reader_settings, clock=lambda: NOW)
    await reader.load()
    from msg.extensions.hosting import hosting_app
    try:
        if gate == 'database':
            async with app.metadata.transaction(write=True) as tx:
                tx.execute("INSERT INTO settings(key,value) VALUES ('recovery_quarantine','{}')", write=True)
        else:
            (app.settings.config_dir / 'recovery-drill.json').write_text('{}')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(reader)),
                                     base_url=app.settings.service_url) as http:
            response = await http.get('/@readonly-host/web/')
            assert response.status_code != 200
            assert b'<h1>published</h1>' not in response.content
            assert b'recovery_quarantined' in response.content
            isolated(response)
    finally:
        await reader.close()


def test_hosting_unit_has_distinct_identity_and_no_data_write_grant():
    service = (Path(__file__).resolve().parents[1] / 'deploy/msgd-hosting.service').read_text()
    assert 'User=msgd-hosting\n' in service
    assert 'Group=msgd-hosting\n' in service
    assert 'ReadWritePaths=' not in service
    assert '--config-dir /etc/msgd-hosting' in service
    assert 'ProtectSystem=strict' in service
    assert '/var/lib/msgd/service' in service


def test_hosting_subcommand_never_assembles_main_application(reader_settings, monkeypatch):
    from msg import daemon
    import msg.config
    import uvicorn
    called = []

    def forbidden(*args, **kwargs):
        pytest.fail('hosting constructed the normal write Application')

    monkeypatch.setattr(daemon, 'load_application', forbidden)
    monkeypatch.setattr(daemon, 'network_runtime', lambda settings: called.append(settings))
    monkeypatch.setattr(msg.config, 'load_settings', lambda directory: reader_settings)
    monkeypatch.setattr(uvicorn, 'run', lambda asgi, **options: called.append((asgi, options)))
    assert daemon.main(['--config-dir', str(reader_settings.config_dir), 'hosting']) == 0
    assert called[0] == reader_settings
    assert len(called) == 2 and called[1][1]['access_log'] is False
