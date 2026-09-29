"""Authority stays current; shared content publication never changes secret defaults."""
import httpx
import psycopg
from psycopg import sql
import pytest

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.security.crypto import Ed25519Signer
from msg.storage.git import GitContentStore, durable_write
from test_hosting_readonly import reader_settings, runtime
from test_hosting_readonly_isolation import preview
from test_service import call


@pytest.mark.asyncio
async def test_revoked_signing_key_cannot_reuse_a_warm_private_preview(installed, reader_settings):
    app, _ = installed
    key, subject, _, payload, request = await preview(app, 'revocable-reader')
    replacement = Ed25519Signer.generate()
    public = b64(replacement.public_key)
    proof = replacement.sign(canonical({'subject_id': subject, 'public_key': public}), purpose='key-add')
    added = await call(app, 'identity.key_add', {'public_key': public,
        'possession_proof': wire(proof), 'ceiling': wire(app.primary_ceiling())}, key=key, subject=subject)
    assert added.status == 'ok', wire(added)
    service = runtime(reader_settings, app.clock)
    await service.load()
    try:
        from msg.extensions.hosting import hosting_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(service)),
                                     base_url='http://testserver') as http:
            headers = {'X-Msg-Request': request['header']}
            accepted = await http.get(request['path'], headers=headers)
            assert accepted.status_code == 200 and accepted.content == payload
            revoked = await call(app, 'identity.key_revoke', {'key_id': key.key_id},
                                 key=replacement, subject=subject)
            assert revoked.status == 'ok', wire(revoked)
            for method, extra in (('GET', {}), ('HEAD', {}), ('GET', {'Range': 'bytes=0-3'}),
                                  ('GET', {'If-None-Match': accepted.headers['etag']})):
                denied = await http.request(method, request['path'], headers={**headers, **extra})
                assert denied.status_code == 403
                assert payload not in denied.content
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('privilege', ['column', 'sequence', 'ownership'])
async def test_partial_or_restorable_database_write_rights_are_rejected(installed, reader_settings, privilege):
    app, _ = installed
    with psycopg.connect(reader_settings.server.postgres_dsn) as conn:
        reader = conn.info.user
    with psycopg.connect(app.settings.server.postgres_dsn, autocommit=True) as conn:
        if privilege == 'column':
            conn.execute(sql.SQL('GRANT UPDATE(value) ON settings TO {}').format(sql.Identifier(reader)))
        elif privilege == 'sequence':
            conn.execute('CREATE SEQUENCE hosting_test_sequence')
            conn.execute(sql.SQL('GRANT USAGE ON SEQUENCE hosting_test_sequence TO {}').format(sql.Identifier(reader)))
        else:
            conn.execute('CREATE TABLE hosting_test_owned (value integer)')
            conn.execute(sql.SQL('ALTER TABLE hosting_test_owned OWNER TO {}').format(sql.Identifier(reader)))
            conn.execute(sql.SQL('REVOKE ALL ON hosting_test_owned FROM {}').format(sql.Identifier(reader)))
    service = runtime(reader_settings, app.clock)
    try:
        with pytest.raises(Failure, match='hosting_database_role_not_readonly'):
            await service.load()
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(('directory_mode', 'file_mode'), [
    (0o700, 0o600), (0o750, 0o600), (0o2700, 0o600), (0o2750, 0o640)])
async def test_content_group_sharing_is_explicit_and_secret_defaults_stay_private(tmp_path, directory_mode, file_mode):
    store = GitContentStore(tmp_path/'content')
    for directory in (store.path, store.index, store.binary):
        directory.chmod(directory_mode)
    store.staging.chmod(0o700)
    text = await store.put_bytes(b'group content', 'text/plain')
    binary = await store.put_bytes(b'\0group binary', 'application/octet-stream')
    for ref in (text, binary):
        assert (store.index/store._key(ref)).stat().st_mode & 0o777 == file_mode
    target = store.binary/store._key(binary)
    assert target.stat().st_mode & 0o777 == file_mode
    if file_mode == 0o640:
        assert target.stat().st_gid == store.binary.stat().st_gid
    # durable_write is also used for real keys. This must not become group-readable.
    secret = store.path/'dummy-private-data'
    durable_write(secret, b'test-only private material')
    assert secret.stat().st_mode & 0o777 == 0o600
