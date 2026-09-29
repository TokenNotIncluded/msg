"""Current authorization, failure containment and real non-owner filesystem reads."""
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import httpx
import psycopg
from psycopg import sql
import pytest

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.core.requests import request_for
from test_hosting_readonly import reader_settings, runtime
from test_service import NOW, call, register


async def preview(app, name='readonly-preview', *, binary=False):
    key, subject, _ = await register(app, name)
    site = await call(app, 'hosting.create', {'parent': '/@'+name, 'name': 'web'},
                      key=key, subject=subject)
    assert site.status == 'ok', wire(site)
    payload = b'\x00readonly-live-binary\xff' if binary else b'<h1>private read-only preview</h1>'
    filename = 'asset.bin' if binary else 'index.html'
    source = await call(app, 'content.file_put', {'parent': '/@'+name+'/files',
        'name': filename, 'data': b64(payload),
        'media_type': 'application/octet-stream' if binary else 'text/html'}, key=key, subject=subject)
    assert source.status == 'ok', wire(source)
    candidate = await call(app, 'hosting.preview', {'id': site.resources[0].id,
        'entries': [{'path': filename, 'source': wire(source.resources[0])}]},
        key=key, subject=subject, expected=((site.resources[0].id, site.data['generation']),))
    assert candidate.status == 'ok', wire(candidate)
    packet = request_for('discovery.raw', {'id': candidate.resources[0].id}, app.settings.service_url,
        signer=key, subject=subject, expires_at=NOW+timedelta(seconds=120))
    return key, subject, candidate, payload, {
        'path': f'/@{name}/web/_preview/{candidate.resources[0].id}/{filename}',
        'header': b64(canonical(wire(packet))), 'payload': b64(payload)}


@pytest.mark.asyncio
async def test_preview_rechecks_acl_before_head_range_and_cached_response(installed, reader_settings):
    app, _ = installed
    key, subject, candidate, payload, request = await preview(app)
    service = runtime(reader_settings, app.clock)
    await service.load()
    try:
        from msg.extensions.hosting import hosting_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(service)),
                                     base_url='http://testserver') as http:
            path = request['path']
            headers = {'X-Msg-Request': request['header']}
            assert (await http.get(path)).status_code == 403
            accepted = await http.get(path, headers=headers)
            assert accepted.status_code == 200 and accepted.content == payload
            assert 'allow-same-origin' not in accepted.headers['content-security-policy']
            assert (await http.get('/-/schema')).status_code == 404
            assert (await http.get(path, headers={**headers, 'Host': 'other.example'})).status_code == 403
            denied = await call(app, 'content.chmod', {'id': candidate.resources[0].id, 'mode': '0000'},
                key=key, subject=subject, expected=((candidate.resources[0].id, candidate.data['generation']),))
            assert denied.status == 'ok', wire(denied)
            for method, extra in (('GET', {}), ('HEAD', {}), ('GET', {'Range': 'bytes=0-7'}),
                                  ('GET', {'If-None-Match': accepted.headers['etag']})):
                response = await http.request(method, path, headers={**headers, **extra})
                assert response.status_code == 403
                assert payload not in response.content
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('gate', ['database', 'marker', 'marker_symlink'])
async def test_quarantine_revokes_loaded_hosting_even_for_cached_requests(installed, reader_settings, gate):
    app, _ = installed
    service = runtime(reader_settings, app.clock)
    await service.load()
    marker = reader_settings.config_dir/'recovery-drill.json'
    try:
        from msg.extensions.hosting import hosting_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(service)),
                                     base_url='http://testserver') as http:
            original = await http.get('/@root/web/index.html')
            assert original.status_code == 200
            if gate == 'database':
                async with app.metadata.transaction(write=True) as tx:
                    tx.set_setting('recovery_quarantine', {'version': 1, 'test': True})
            elif gate == 'marker':
                marker.write_text('{}')
            else:
                marker.symlink_to(marker.with_name('missing-marker-target'))
            for method in ('GET', 'HEAD'):
                response = await http.request(method, '/@root/web/index.html',
                    headers={'If-None-Match': original.headers['etag']})
                assert response.status_code not in (200, 206, 304)
                assert original.content not in response.content
            fresh = runtime(reader_settings, app.clock)
            try:
                with pytest.raises(Failure, match='recovery_quarantined'):
                    await fresh.load()
            finally:
                await fresh.close()
    finally:
        marker.unlink(missing_ok=True)
        await service.close()


@pytest.mark.asyncio
async def test_missing_trust_and_schema_fail_without_initialization(installed, reader_settings):
    app, _ = installed
    trust = reader_settings.trust_file
    hidden = trust.with_name('root.test-hidden')
    trust.rename(hidden)
    service = runtime(reader_settings, app.clock)
    try:
        with pytest.raises(Failure, match='root_not_initialized'):
            await service.load()
        assert not trust.exists()
    finally:
        hidden.rename(trust)
        await service.close()
    with psycopg.connect(app.settings.server.postgres_dsn, autocommit=True) as conn:
        conn.execute('DROP TABLE schema_version')
    service = runtime(reader_settings, app.clock)
    try:
        with pytest.raises(Failure, match='hosting_installation_not_ready'):
            await service.load()
    finally:
        await service.close()
    with psycopg.connect(app.settings.server.postgres_dsn) as conn:
        assert conn.execute("SELECT to_regclass('public.schema_version')").fetchone() == (None,)


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', ['write', 'membership', 'definer'])
async def test_effective_database_privilege_escalations_fail_startup(installed, reader_settings, extra):
    app, _ = installed
    with psycopg.connect(reader_settings.server.postgres_dsn) as conn:
        reader = conn.info.user
    with psycopg.connect(app.settings.server.postgres_dsn, autocommit=True) as conn:
        if extra == 'write':
            conn.execute(sql.SQL('GRANT UPDATE ON settings TO {}').format(sql.Identifier(reader)))
        elif extra == 'membership':
            conn.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(conn.info.user), sql.Identifier(reader)))
        else:
            conn.execute('CREATE FUNCTION hosting_test_definer() RETURNS integer LANGUAGE sql '
                         "SECURITY DEFINER AS 'SELECT 1'")
    service = runtime(reader_settings, app.clock)
    try:
        with pytest.raises(Failure, match='hosting_database_role_not_readonly'):
            await service.load()
    finally:
        await service.close()


READ_PROCESS = r'''
import asyncio, json, os, sys
from datetime import UTC, datetime
from pathlib import Path
import httpx
from msg.config import load_settings
from msg.core.codec import unb64
from msg.hosting_runtime import HostingRuntime
from msg.extensions.hosting import hosting_app

async def main():
    assert os.geteuid() == 65534, os.geteuid()
    settings = load_settings(Path(sys.argv[1]))
    private = Path(sys.argv[2])
    try:
        private.read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError('reader can read service signing material')
    try:
        (settings.server.content_dir/'forbidden-write').write_bytes(b'no')
    except PermissionError:
        pass
    else:
        raise AssertionError('reader can write business files')
    service = HostingRuntime(settings, clock=lambda: datetime(2026, 9, 27, tzinfo=UTC))
    await service.load()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(service)),
                                     base_url='http://testserver') as http:
            assert (await http.get('/@root/web/index.html')).status_code == 200
            for request in json.loads(Path(sys.argv[3]).read_text()):
                assert (await http.get(request['path'])).status_code == 403
                response = await http.get(request['path'], headers={'X-Msg-Request': request['header']})
                assert response.status_code == 200, response.status_code
                assert response.content == unb64(request['payload'])
        print(json.dumps({'uid': os.geteuid(), 'private_key_denied': True,
                          'business_write_denied': True, 'live_previews': 2}))
    finally:
        await service.close()
asyncio.run(main())
'''


@pytest.mark.asyncio
async def test_real_non_owner_reads_new_text_and_binary_publications_without_keys(installed, reader_settings):
    # CI provides sudo/setfacl. This is a real UID boundary in a disposable install.
    assert shutil.which('sudo') and shutil.which('setfacl'), 'Linux isolation prerequisites missing'
    app, _ = installed
    base = Path(tempfile.mkdtemp(prefix='msg-hosting-reader-'))
    changed_ancestors = []
    try:
        package = Path(__file__).resolve().parents[1]/'src'/'msg'
        shutil.copytree(package, base/'src'/'msg')
        from msg.config import write_example
        fields = psycopg.conninfo.conninfo_to_dict(reader_settings.server.postgres_dsn)
        assert all('\n' not in value for value in fields.values())
        pgservice = base/'pg_service.conf'
        pgservice.write_text('[hosting_test]\n' + ''.join(f'{key}={value}\n' for key, value in fields.items()))
        config = write_example(base/'etc', base/'unused', 'http://testserver',
                               postgres_dsn='service=hosting_test')
        config_file = config.config_dir/'msgd.toml'
        text = config_file.read_text()
        for previous, actual in ((config.server.content_dir, app.contents.path),
                                 (config.server.blob_dir, app.contents.binary),
                                 (config.server.staging_dir, app.contents.staging)):
            text = text.replace(str(previous), str(actual))
        config_file.write_text(text)
        config.trust_file.parent.mkdir(parents=True)
        shutil.copyfile(reader_settings.trust_file, config.trust_file)
        # Provision existing content first; NEW publications get no ACL/chmod repair.
        for root in (app.contents.path, app.contents.binary):
            for parent in root.parents:
                if parent == Path('/tmp') or parent == Path('/'):
                    break
                old = parent.stat().st_mode & 0o7777
                if not old & 0o010:
                    changed_ancestors.append((parent, old))
                    parent.chmod(old | 0o010)
            for path in [root, *root.rglob('*')]:
                path.chmod(0o2750 if path.is_dir() else 0o640)
            for directory in [root, *(p for p in root.rglob('*') if p.is_dir())]:
                subprocess.run(['setfacl', '-m', 'd:u::rwx,d:g::r-x,d:o::---', str(directory)],
                               check=True, capture_output=True)
        subprocess.run(['git', '--git-dir', str(app.contents.repo), 'config', 'core.sharedRepository', '0640'],
                       check=True, capture_output=True)
        first = await preview(app, 'live-text-reader')
        second = await preview(app, 'live-binary-reader', binary=True)
        requests = base/'requests.json'
        requests.write_text(json.dumps([first[-1], second[-1]]))
        script = base/'reader.py'
        script.write_text(READ_PROCESS)
        for path in [base, *base.rglob('*')]:
            path.chmod(0o550 if path.is_dir() else 0o440)
        before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                  for root in (app.contents.path, app.contents.binary)
                  for path in root.rglob('*') if path.is_file()}
        result = subprocess.run(['sudo', '-n', '-u', '#65534', '-g', '#'+str(os.getgid()), '--',
            'env', 'PYTHONDONTWRITEBYTECODE=1', 'PYTHONPATH='+str(base/'src'),
            'PGSERVICEFILE='+str(pgservice), 'HOME='+str(base),
            sys.executable, str(script), str(config.config_dir),
            str(app.settings.service_keys/'online.key'), str(requests)],
            capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr[-6000:]
        report = json.loads(result.stdout)
        assert report == {'uid': 65534, 'private_key_denied': True,
                          'business_write_denied': True, 'live_previews': 2}
        assert before == {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                          for root in (app.contents.path, app.contents.binary)
                          for path in root.rglob('*') if path.is_file()}
        assert not (app.contents.path/'forbidden-write').exists()
    finally:
        for path in [base, *base.rglob('*')]:
            if path.is_dir():
                path.chmod(0o700)
        shutil.rmtree(base)
        for parent, mode in reversed(changed_ancestors):
            parent.chmod(mode)
