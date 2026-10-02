"""Compare real loopback home requests using two sources and one disposable DB.

Run with --baseline-source pointing to an immutable extracted src/ directory.
No production settings, credentials or accounts are read. Both servers use
independent ephemeral ports, sequentially, and identical PostgreSQL/Git input.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

import httpx
import psycopg

from msg.admin.root import _approve_csr, _provision
from msg.application import Application
from msg.config import write_example
from msg.core.codec import b64
from msg.core.models import ResourceRef
from msg.core.requests import request_for
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer, subject_id

SERVER = r"""
import asyncio,json,socket,sys
from datetime import datetime
from pathlib import Path
import uvicorn
from msg.application import Application
from msg.config import load_settings
from msg.transports.http import create_app
async def main():
 app=Application(load_settings(Path(sys.argv[1])),clock=lambda:datetime.fromisoformat(sys.argv[2]))
 sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();sock.setblocking(False)
 server=uvicorn.Server(uvicorn.Config(create_app(app),log_level='error',access_log=False,ws='none'))
 task=asyncio.create_task(server.serve(sockets=[sock]))
 while not server.started:
  if task.done(): await task
  await asyncio.sleep(0.01)
 print(json.dumps({'port':sock.getsockname()[1]}),flush=True)
 await task
asyncio.run(main())
"""


def source_digest(source):
    value = sha256()
    for relative in (
        'msg/plugins/discovery.py',
        'msg/transports/http_routes.py',
        'msg/transports/home_cache.py',
    ):
        path = source / relative
        if path.is_file():
            value.update(relative.encode())
            value.update(path.read_bytes())
    return value.hexdigest()


async def call(app, operation, arguments, key, subject, *, version=1):
    result = await app.executor.execute(
        request_for(
            operation,
            arguments,
            app.settings.service_url,
            subject=subject,
            signer=key,
            contract_version=version,
            expires_at=app.clock() + timedelta(seconds=120),
        )
    )
    assert result.status == 'ok', result.error
    return result


async def probe(source, config, now):
    environment = {**os.environ, 'PYTHONPATH': str(source)}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        '-u',
        '-c',
        SERVER,
        str(config),
        now.isoformat(),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        ready = await asyncio.wait_for(process.stdout.readline(), 30)
        if not ready:
            raise RuntimeError((await process.stderr.read()).decode())
        origin = 'http://127.0.0.1:' + str(json.loads(ready)['port'])
        rows = []
        async with httpx.AsyncClient(timeout=30, headers={'Host': 'testserver'}) as http:
            for index in range(4):
                started = asyncio.get_running_loop().time()
                response = await http.get(origin + '/', headers={'Accept': 'text/html'})
                assert response.status_code == 200, response.text[:200]
                rows.append({
                    'request': 'cold' if index == 0 else f'warm-{index}',
                    'seconds': asyncio.get_running_loop().time() - started,
                    'bytes': len(response.content),
                    'snapshot': response.headers.get('x-msg-home-snapshot'),
                    'unavailable': '<p class="notice" role="status" data-i18n="unavailable">'
                    in response.text,
                })
                if index == 0:
                    await asyncio.sleep(0.5)
            etag = response.headers.get('etag')
            started = asyncio.get_running_loop().time()
            conditional = await http.get(
                origin + '/',
                headers={'Accept': 'text/html', 'If-None-Match': etag or '"missing"'},
            )
            rows.append({
                'request': 'conditional',
                'seconds': asyncio.get_running_loop().time() - started,
                'status': conditional.status_code,
                'bytes': len(conditional.content),
            })
        return {'source_digest': source_digest(source), 'requests': rows}
    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), 15)


async def main(args):
    directory = Path(tempfile.mkdtemp(prefix='msg-home-latency-'))
    data, socket = directory / 'postgres', directory / 'socket'
    socket.mkdir()
    now = datetime(2026, 9, 27, tzinfo=UTC)
    try:
        subprocess.run(
            ['initdb', '-D', str(data), '-U', 'msgbench', '-A', 'trust', '--no-instructions'],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                'pg_ctl',
                '-D',
                str(data),
                '-l',
                str(directory / 'postgres.log'),
                '-o',
                f"-k {socket} -h '' -p 5432",
                '-w',
                'start',
            ],
            check=True,
            capture_output=True,
        )
        dsn = 'postgresql://msgbench@localhost:5432/postgres?host=' + quote(str(socket), safe='')
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute('CREATE DATABASE msg_home_bench')
        settings = write_example(
            directory / 'etc',
            directory / 'store',
            'http://testserver',
            postgres_dsn=dsn.replace('/postgres?', '/msg_home_bench?'),
        )
        app = Application(settings, clock=lambda: now)
        try:
            csr, root = await _provision(app, 'disposable-benchmark-passphrase')
            await _approve_csr(app, csr, root, expected_digest=None, operator='loopback-benchmark')
            await app.load()
            key = Ed25519Signer.generate()
            subject = subject_id(key.public_key)
            _, recipient = generate_age_key()
            await call(
                app,
                'identity.register',
                {
                    'handle': 'loopback-fixture',
                    'public_key': b64(key.public_key),
                    'encryption_recipient': recipient,
                },
                key,
                subject,
                version=2,
            )
            created = await call(
                app,
                'content.post_create',
                {
                    'parent': '/main',
                    'body': 'Public benchmark fixture',
                },
                key,
                subject,
            )
            async with app.metadata.transaction(write=True) as tx:
                resource = await tx.resource(created.resources[0].id)
                revision = await tx.revision(ResourceRef(id=resource.id))
                for index in range(args.posts - 1):
                    rid, rev = f'p_bench_{index:032x}', f'v_bench_{index:032x}'
                    await tx.insert(
                        replace(resource, id=rid, name=f'fixture-{index}', revision=rev)
                    )
                    await tx.append_revision(
                        replace(revision, id=rev, resource_id=rid, signature=None)
                    )
        finally:
            await app.close()
        current = Path(__file__).resolve().parents[1] / 'src'
        before = await probe(args.baseline_source.resolve(), directory / 'etc', now)
        after = await probe(current, directory / 'etc', now)
        result = {
            'method': 'Sequential independent real loopback HTTP servers; identical disposable PostgreSQL/Git input; no production traffic or credentials.',
            'posts': args.posts,
            'baseline_reference': args.baseline_reference,
            'before': before,
            'after': after,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    finally:
        if data.exists():
            subprocess.run(
                ['pg_ctl', '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                check=False,
                capture_output=True,
            )
        shutil.rmtree(directory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-source', type=Path, required=True)
    parser.add_argument('--baseline-reference', required=True)
    parser.add_argument('--posts', type=int, default=70)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
