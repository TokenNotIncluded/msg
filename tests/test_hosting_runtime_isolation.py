"""Independent-UID and failure-containment cases retained from PR #174.

Adapted from eafea15af0b8ff26c0208d5c0fcc039b4d32fb56 to the #173 reader API.
These are disposable CI installs, not production permission changes.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from test_hosting_runtime import isolated, runtime_class, reader_settings as reader_settings
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.core.requests import request_for


async def preview(app, name='readonly-preview', *, binary=False):
    key, subject, _ = await register(app, name)
    site = await call(
        app, 'hosting.create', {'parent': '/@' + name, 'name': 'web'}, key=key, subject=subject
    )
    assert site.status == 'ok', wire(site)
    payload = b'\x00readonly-live-binary\xff' if binary else b'<h1>private read-only preview</h1>'
    filename = 'asset.bin' if binary else 'index.html'
    source = await call(
        app,
        'content.file_put',
        {
            'parent': '/@' + name + '/files',
            'name': filename,
            'data': b64(payload),
            'media_type': 'application/octet-stream' if binary else 'text/html',
        },
        key=key,
        subject=subject,
    )
    assert source.status == 'ok', wire(source)
    candidate = await call(
        app,
        'hosting.preview',
        {
            'id': site.resources[0].id,
            'entries': [{'path': filename, 'source': wire(source.resources[0])}],
        },
        key=key,
        subject=subject,
        expected=((site.resources[0].id, site.data['generation']),),
    )
    assert candidate.status == 'ok', wire(candidate)
    packet = request_for(
        'discovery.raw',
        {'id': candidate.resources[0].id},
        app.settings.service_url,
        signer=key,
        subject=subject,
        expires_at=NOW + timedelta(seconds=120),
    )
    return (
        key,
        subject,
        candidate,
        payload,
        {
            'path': f'/@{name}/web/_preview/{candidate.resources[0].id}/{filename}',
            'header': b64(canonical(wire(packet))),
            'payload': b64(payload),
        },
    )


async def test_preview_rechecks_acl_before_head_range_and_cached_response(
    installed, reader_settings
):
    app, _ = installed
    key, subject, candidate, payload, request = await preview(app)
    service = runtime_class()(reader_settings, clock=app.clock)
    await service.load()
    try:
        from msg.extensions.hosting import hosting_app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=hosting_app(service)),
            base_url=app.settings.service_url,
        ) as http:
            path, headers = request['path'], {'X-Msg-Request': request['header']}
            assert (await http.get(path)).status_code == 403
            accepted = await http.get(path, headers=headers)
            assert accepted.status_code == 200 and accepted.content == payload
            isolated(accepted)
            assert (await http.get('/-/schema')).status_code == 404
            assert (
                await http.get(path, headers={**headers, 'Host': 'other.example'})
            ).status_code == 403
            denied = await call(
                app,
                'content.chmod',
                {'id': candidate.resources[0].id, 'mode': '0000'},
                key=key,
                subject=subject,
                expected=((candidate.resources[0].id, candidate.data['generation']),),
            )
            assert denied.status == 'ok', wire(denied)
            for method, extra in (
                ('GET', {}),
                ('HEAD', {}),
                ('GET', {'Range': 'bytes=0-7'}),
                ('GET', {'If-None-Match': accepted.headers['etag']}),
            ):
                response = await http.request(method, path, headers={**headers, **extra})
                assert response.status_code == 403 and payload not in response.content
    finally:
        await service.close()


@pytest.mark.parametrize('gate', ['database', 'marker', 'marker_symlink'])
async def test_quarantine_revokes_cached_reads_and_new_startup(installed, reader_settings, gate):
    app, _ = installed
    service = runtime_class()(reader_settings, clock=app.clock)
    await service.load()
    marker = reader_settings.config_dir / 'recovery-drill.json'
    try:
        from msg.extensions.hosting import hosting_app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=hosting_app(service)),
            base_url=app.settings.service_url,
        ) as http:
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
                response = await http.request(
                    method,
                    '/@root/web/index.html',
                    headers={'If-None-Match': original.headers['etag']},
                )
                assert response.status_code not in (200, 206, 304)
                assert original.content not in response.content
            fresh = runtime_class()(reader_settings, clock=app.clock)
            try:
                with pytest.raises(Failure, match='recovery_quarantined'):
                    await fresh.load()
            finally:
                await fresh.close()
            if gate != 'database':
                marker.unlink()
                assert (await http.get('/@root/web/index.html')).status_code not in (200, 206, 304)
    finally:
        marker.unlink(missing_ok=True)
        await service.close()


@pytest.mark.parametrize('damage', ['missing', 'wrong_version', 'missing_read_grant'])
async def test_required_schema_is_verified_without_repair(installed, reader_settings, damage):
    app, _ = installed
    reader = conninfo_to_dict(reader_settings.server.postgres_dsn)['user']
    with psycopg.connect(app.settings.server.postgres_dsn, autocommit=True) as conn:
        if damage == 'missing':
            conn.execute('DROP TABLE schema_version')
        elif damage == 'wrong_version':
            conn.execute('UPDATE schema_version SET version=999')
        else:
            conn.execute(
                sql.SQL('REVOKE SELECT ON share_grants_v2 FROM {}').format(sql.Identifier(reader))
            )
    service = runtime_class()(reader_settings, clock=app.clock)
    try:
        with pytest.raises(Failure, match='hosting_installation_stale'):
            await service.load()
    finally:
        await service.close()
    with psycopg.connect(app.settings.server.postgres_dsn) as conn:
        if damage == 'missing':
            assert conn.execute("SELECT to_regclass('public.schema_version')").fetchone() == (None,)
        elif damage == 'wrong_version':
            assert conn.execute('SELECT version FROM schema_version').fetchall() == [(999,)]
        else:
            assert not conn.execute(
                'SELECT has_table_privilege(%s,%s,%s)', (reader, 'public.share_grants_v2', 'SELECT')
            ).fetchone()[0]


@pytest.mark.parametrize('extra', ['write', 'membership', 'definer', 'sequence', 'secret_read'])
async def test_effective_privilege_escalations_fail_startup(installed, reader_settings, extra):
    app, _ = installed
    reader = conninfo_to_dict(reader_settings.server.postgres_dsn)['user']
    with psycopg.connect(app.settings.server.postgres_dsn, autocommit=True) as conn:
        if extra == 'write':
            conn.execute(sql.SQL('GRANT UPDATE ON settings TO {}').format(sql.Identifier(reader)))
        elif extra == 'membership':
            conn.execute(
                sql.SQL('GRANT {} TO {}').format(
                    sql.Identifier(conn.info.user), sql.Identifier(reader)
                )
            )
        elif extra == 'definer':
            conn.execute(
                'CREATE FUNCTION hosting_test_definer() RETURNS integer LANGUAGE sql '
                "SECURITY DEFINER AS 'SELECT 1'"
            )
        elif extra == 'sequence':
            conn.execute('CREATE SEQUENCE hosting_test_sequence')
            conn.execute(
                sql.SQL('GRANT USAGE ON SEQUENCE hosting_test_sequence TO {}').format(
                    sql.Identifier(reader)
                )
            )
        else:
            conn.execute(
                sql.SQL('GRANT SELECT ON token_deliveries TO {}').format(sql.Identifier(reader))
            )
    service = runtime_class()(reader_settings, clock=app.clock)
    try:
        with pytest.raises(Failure, match='hosting_database_not_readonly'):
            await service.load()
    finally:
        await service.close()


READ_PROCESS = r"""
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
    try:
        Path(sys.argv[2]).read_bytes()
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
                                     base_url=settings.service_url) as http:
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
"""


async def test_real_other_uid_reads_new_text_and_binary_without_keys(installed, reader_settings):
    assert shutil.which('sudo') and shutil.which('setpriv'), 'Linux isolation prerequisites missing'
    app, _ = installed
    base = Path(tempfile.mkdtemp(prefix='msg-hosting-reader-'))
    changed_ancestors = {}
    try:
        shutil.copytree(Path(__file__).resolve().parents[1] / 'src' / 'msg', base / 'src' / 'msg')
        from msg.config import write_example

        config = write_example(
            base / 'etc',
            base / 'unused',
            app.settings.service_url,
            postgres_dsn='service=hosting-test',
        )
        config_file = config.config_dir / 'msgd.toml'
        text = config_file.read_text().replace(
            '[server]', '[server]\npublic_web_origin = ' + json.dumps(app.settings.service_url)
        )
        text = text.replace(
            '[hosting]',
            '[hosting]\nrecovery_marker = '
            + json.dumps(str(app.settings.config_dir / 'recovery-drill.json')),
        )
        for previous, actual in (
            (config.server.content_dir, app.contents.path),
            (config.server.blob_dir, app.contents.binary),
            (config.server.staging_dir, app.contents.staging),
        ):
            text = text.replace(str(previous), str(actual))
        config_file.write_text(text)
        config.trust_file.parent.mkdir(parents=True)
        shutil.copyfile(reader_settings.trust_file, config.trust_file)
        service_file = base / 'pg_service.conf'
        service_file.write_text(
            '[hosting-test]\n'
            + ''.join(
                f'{key}={value}\n'
                for key, value in conninfo_to_dict(reader_settings.server.postgres_dsn).items()
            )
        )
        # Explicitly prepare existing content before opt-in. No post-publication
        # chmod/default ACL can hide a mode bug in newly published objects.
        for root in (app.contents.path, app.contents.binary):
            for parent in root.parents:
                if parent in (Path('/tmp'), Path('/')):
                    break
                if parent not in changed_ancestors:
                    changed_ancestors[parent] = parent.stat().st_mode & 0o7777
                    parent.chmod(changed_ancestors[parent] | 0o010)
            for path in [root, *root.rglob('*')]:
                path.chmod(0o2750 if path.is_dir() else 0o640)
        subprocess.run(
            ['git', '--git-dir', str(app.contents.repo), 'config', 'core.sharedRepository', '0640'],
            check=True,
            capture_output=True,
        )
        from msg.storage.git import GitContentStore

        old_umask = os.umask(0o077)
        try:
            app.contents = GitContentStore(
                app.contents.path,
                binary_dir=app.contents.binary,
                staging_dir=app.contents.staging,
                group_read=True,
            )
            first = await preview(app, 'live-text-reader')
            second = await preview(app, 'live-binary-reader', binary=True)
        finally:
            os.umask(old_umask)
        requests = base / 'requests.json'
        requests.write_text(json.dumps([first[-1], second[-1]]))
        script = base / 'reader.py'
        script.write_text(READ_PROCESS)
        for path in [base, *base.rglob('*')]:
            path.chmod(0o550 if path.is_dir() else 0o440)

        def snapshots():
            return {
                str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for root in (app.contents.path, app.contents.binary)
                for path in root.rglob('*')
                if path.is_file()
            }

        before = snapshots()
        result = subprocess.run(
            [
                'sudo',
                '-n',
                '--',
                'setpriv',
                '--reuid=65534',
                '--regid=' + str(os.getgid()),
                '--clear-groups',
                '--no-new-privs',
                '--',
                'env',
                'PYTHONDONTWRITEBYTECODE=1',
                'PYTHONPATH=' + str(base / 'src'),
                'PGSERVICEFILE=' + str(service_file),
                sys.executable,
                str(script),
                str(config.config_dir),
                str(app.settings.service_keys / 'online.key'),
                str(requests),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-6000:]
        assert json.loads(result.stdout) == {
            'uid': 65534,
            'private_key_denied': True,
            'business_write_denied': True,
            'live_previews': 2,
        }
        assert before == snapshots()
        assert not (app.contents.path / 'forbidden-write').exists()
    finally:
        for path in [base, *base.rglob('*')]:
            if path.is_dir():
                path.chmod(0o700)
        shutil.rmtree(base)
        for parent, mode in reversed(tuple(changed_ancestors.items())):
            parent.chmod(mode)
