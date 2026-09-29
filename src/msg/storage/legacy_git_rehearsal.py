"""TEST ONLY: rehearse a protected Git bundle in an isolated private installation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pwd
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer, subject_id
from msg.storage.legacy_git_import import (
    PURPOSE,
    git_approval,
    import_git,
    sha256_file,
    verify_bundle,
)
from msg.storage.legacy_rehearsal import _write


async def _run(bundle, manifest_path, target, dsn):
    from msg.admin.root import _approve_csr, _provision
    from msg.application import Application
    from msg.config import write_example

    manifest_digest = sha256_file(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    verified = verify_bundle(
        bundle, manifest['bundle_sha256'], protected_work=target / 'verification'
    )
    if verified['refs'] != manifest['refs'] or not manifest['refs_stable']:
        raise RuntimeError('fixed-ref manifest mismatch')
    settings = write_example(
        target / 'etc', target / 'data', 'https://test-only-legacy-git.invalid', postgres_dsn=dsn
    )
    app = Application(settings)
    try:
        csr, root = await _provision(app, 'test-only-disposable-git-rehearsal-passphrase')
        await _approve_csr(app, csr, root, expected_digest=None, operator='test-only-git-rehearsal')
        signer = Ed25519Signer.generate()
        uid = subject_id(signer.public_key)
        _, recipient = generate_age_key()
        registered = await app.executor.execute(
            request_for(
                'identity.register',
                {
                    'handle': 'test-only-git-importer',
                    'public_key': b64(signer.public_key),
                    'encryption_recipient': recipient,
                },
                settings.service_url,
                signer=signer,
                subject=uid,
                contract_version=2,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        if registered.status != 'ok':
            raise RuntimeError('test-only registration failed')
        created = await app.executor.execute(
            request_for(
                'content.topic_create',
                {'parent': '/main', 'name': 'test-only-git-import'},
                settings.service_url,
                signer=signer,
                subject=uid,
                certificates=(registered.data['certificate_id'],),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        if created.status != 'ok':
            raise RuntimeError('test-only namespace failed')
        parent = created.resources[0].id
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(parent)
            await tx.replace(
                replace(resource, mode=0o700, generation=resource.generation + 1),
                resource.generation,
            )
            generation = resource.generation + 1
            baseline = {
                table: tx.one('SELECT count(*) FROM ' + table)[0]
                for table in ('credentials', 'certificates', 'jobs')
            }
        approval = git_approval(
            bundle_sha256=verified['bundle_sha256'],
            refs_digest=verified['refs_digest'],
            default_ref=manifest['default_ref'],
            service=settings.service_url,
            parent=parent,
            parent_generation=generation,
            operator=uid,
            name='test-only-retained.git',
            expires_at=wire(datetime.now(UTC) + timedelta(hours=2)),
        )
        report = await import_git(
            app, bundle, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
        )
        own = await app.executor.execute(
            request_for(
                'git.refs',
                {'id': report['resource_id'], 'limit': 128},
                settings.service_url,
                signer=signer,
                subject=uid,
                certificates=(registered.data['certificate_id'],),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        denied = await app.executor.execute(
            request_for('git.refs', {'id': report['resource_id']}, settings.service_url)
        )
        async with app.metadata.transaction(write=False) as tx:
            final = {table: tx.one('SELECT count(*) FROM ' + table)[0] for table in baseline}
        unchanged = (
            sha256_file(bundle) == manifest['bundle_sha256']
            and sha256_file(manifest_path) == manifest_digest
        )
        refs_match = (
            own.status == 'ok'
            and {row['name']: row['oid'] for row in own.data['refs']} == verified['refs']
        )
        summary = {
            'format': 'msg-test-only-legacy-git-rehearsal-v1',
            'test_only': True,
            'production_approval': False,
            'bundle_sha256': verified['bundle_sha256'],
            'bundle_bytes': verified['bundle_bytes'],
            'ref_count': verified['ref_count'],
            'reachable_object_count': verified['reachable_object_count'],
            'lfs_pointer_count': verified['lfs_pointer_count'],
            'gitlink_count': verified['gitlink_count'],
            'refs_digest': verified['refs_digest'],
            'object_ids_and_refs_preserved': refs_match,
            'source_unchanged': unchanged,
            'anonymous_denied': denied.status == 'error',
            'authority_and_jobs_unchanged': baseline == final,
            'visibility': 'private',
            'result': 'passed',
        }
        if not all((unchanged, refs_match, denied.status == 'error', baseline == final)):
            raise RuntimeError('git rehearsal invariant failed')
        return summary
    finally:
        await app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--protected-target', type=Path, required=True)
    args = parser.parse_args()
    source = args.bundle.resolve()
    manifest = args.manifest.resolve()
    target = args.protected_target.absolute()
    if target.is_relative_to('/tmp') or target.is_relative_to('/var/tmp'):
        parser.error('target must be protected persistent storage, not a temporary directory')
    if (
        args.bundle.is_symlink()
        or args.manifest.is_symlink()
        or manifest.stat().st_mode & 0o077
        or source.stat().st_mode & 0o077
    ):
        parser.error('source must be a non-symlink private snapshot (0600 or stricter)')
    if target.parent.resolve() != target.parent or target.parent.stat().st_mode & 0o077:
        parser.error('target parent must be a private real directory (0700 or stricter)')
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    # Socket contains no backup payload. Put it under the same protected directory.
    socket = target / 'sock'
    socket.mkdir(mode=0o700)
    if len(str(socket).encode()) > 85:
        parser.error('protected-target path is too long for a local PostgreSQL socket')
    cluster = target / 'pg'
    started = False
    summary = None
    try:
        subprocess.run(
            ['initdb', '-D', str(cluster), '-A', 'trust', '--no-instructions'],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                'pg_ctl',
                '-D',
                str(cluster),
                '-l',
                str(target / 'postgres.log'),
                '-o',
                f'-k {socket} -h "" -p 5432',
                '-w',
                'start',
            ],
            check=True,
            capture_output=True,
        )
        started = True
        import psycopg

        dsn = f'postgresql://{quote(pwd.getpwuid(os.getuid()).pw_name)}@localhost:5432/postgres?host={quote(str(socket), safe="")}'
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute('CREATE DATABASE legacy_rehearsal')
        dsn = dsn.replace('/postgres?', '/legacy_rehearsal?')
        summary = asyncio.run(_run(source, manifest, target, dsn))
    except BaseException as exc:
        # No raw exception messages: DB errors can include original row contents.
        from msg.core.errors import Failure

        summary = {
            'format': 'msg-test-only-legacy-git-rehearsal-v1',
            'test_only': True,
            'production_approval': False,
            'result': 'failed',
            'error_type': type(exc).__name__,
        }
        if isinstance(exc, Failure):
            summary['error_code'] = exc.code
    finally:
        if started:
            stopped = subprocess.run(
                ['pg_ctl', '-D', str(cluster), '-m', 'fast', '-w', 'stop'],
                capture_output=True,
                check=False,
            )
            if summary is not None:
                summary['cluster_stopped'] = stopped.returncode == 0
                if stopped.returncode != 0:
                    summary['result'] = 'failed'
        if summary is not None:
            _write(target / 'summary.json', summary)
            print(json.dumps(summary, sort_keys=True))
    if summary is None or summary['result'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
