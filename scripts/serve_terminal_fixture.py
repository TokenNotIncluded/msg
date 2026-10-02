"""Real loopback terminal fixture with disposable PostgreSQL, TestRoot and users.

Run with the candidate's src on PYTHONPATH and an ordinary OS user. Supply new
--ready-file and --accounts-file paths. SIGTERM or --lifetime removes all test
data and private OAuth files. No production settings or credentials are used.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import socket
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import uvicorn
from serve_flight_fixture import FixtureServer, browser_accounts, call, private_json

import msg
from msg.admin.diagnostics import temporary_postgres
from msg.admin.root import _approve_csr, _provision
from msg.application import Application
from msg.config import write_example
from msg.core.codec import b64
from msg.core.requests import request_for
from msg.oauth_config import OAuthConfig
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer, subject_id
from msg.transports.http import create_app

ROOT = Path(__file__).resolve().parents[1]


async def seed(app):
    accounts = []
    for handle in ('terminal-reader', 'terminal-owner'):
        key = Ed25519Signer.generate()
        subject = subject_id(key.public_key)
        _, recipient = generate_age_key()
        await call(
            app,
            'identity.register',
            {
                'handle': handle,
                'public_key': b64(key.public_key),
                'encryption_recipient': recipient,
            },
            key=key,
            subject=subject,
            version=2,
        )
        accounts.append({'handle': handle, 'subject_id': subject, 'key': key})
    owner = accounts[1]
    posts = {}
    for visibility in ('public', 'private', 'long'):
        marker = 'TERMINAL_' + visibility.upper() + '_BROWSER'
        result = await call(
            app,
            'content.post_create',
            {
                'parent': '/main',
                'name': ('中文' * 32 + ' public.md')
                if visibility == 'long'
                else 'terminal-browser-' + visibility + '.md',
                'body': '# Terminal browser post\n\n'
                + marker
                + '\n<script>window.__TERMINAL_XSS=1</script>',
            },
            key=owner['key'],
            subject=owner['subject_id'],
        )
        ref = result.resources[0]
        if visibility == 'private':
            changed = await app.executor.execute(
                request_for(
                    'content.chmod',
                    {'id': ref.id, 'mode': '0600'},
                    app.settings.service_url,
                    signer=owner['key'],
                    subject=owner['subject_id'],
                    expected=((ref.id, result.data['generation']),),
                    expires_at=app.clock() + timedelta(seconds=120),
                ),
                entry='network',
            )
            if changed.status != 'ok':
                raise RuntimeError('Could not make the fixture post private')
        meta = await call(
            app,
            'discovery.get',
            {'id': ref.id, 'view': 'meta'},
            key=owner['key'],
            subject=owner['subject_id'],
        )
        posts[visibility + '_post'] = {
            'id': ref.id,
            'path': meta.data['path'],
            'marker': marker,
        }
    return accounts, posts


async def business_state(app):
    async with app.metadata.transaction(write=False) as tx:
        return {
            table: hashlib.sha256(
                repr(tx.rows(f'SELECT * FROM {table} ORDER BY 1,2')).encode()
            ).hexdigest()
            for table in ('resources', 'revisions', 'relations', 'reactions', 'agent_follows')
        }


async def serve(options):
    if os.geteuid() == 0 or 'MSG_TEST_POSTGRES_URL_TEMPLATE' in os.environ:
        raise RuntimeError('An ordinary OS user and a new private PostgreSQL cluster are required')
    if Path(msg.__file__).resolve() != ROOT / 'src/msg/__init__.py':
        raise RuntimeError('Use the candidate src on PYTHONPATH')
    for path in (options.ready_file, options.accounts_file):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f'Refusing to overwrite fixture output: {path}')
    task = asyncio.current_task()
    server = None
    created = []
    folder = postgres = None
    unchanged = None
    loop = asyncio.get_running_loop()

    def stop():
        if server is None:
            task.cancel()
        else:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    deadline = loop.call_later(options.lifetime, stop)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.setblocking(False)
            origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
            with tempfile.TemporaryDirectory(prefix='msg-terminal-') as temporary:
                folder = Path(temporary)
                with temporary_postgres() as dsn:
                    postgres = Path(parse_qs(urlsplit(dsn).query)['host'][0]).parent
                    settings = replace(
                        write_example(folder / 'etc', folder / 'data', origin, postgres_dsn=dsn),
                        oauth=OAuthConfig(enabled=True),
                    )
                    if settings.server.mail is not None or settings.server.valkey_url:
                        raise RuntimeError('Fixture must not use mail or Valkey')
                    app = Application(settings)
                    try:
                        csr, root = await _provision(
                            app, 'disposable-terminal-' + os.urandom(24).hex()
                        )
                        await _approve_csr(
                            app,
                            csr,
                            root,
                            expected_digest=None,
                            operator='terminal-browser-fixture',
                        )
                        accounts, posts = await seed(app)
                        router = create_app(app)
                        browser = await browser_accounts(app, router, accounts)
                        private_json(options.accounts_file, browser)
                        created.append(options.accounts_file)
                        before = await business_state(app)
                        server = FixtureServer(
                            uvicorn.Config(
                                router,
                                host='127.0.0.1',
                                access_log=False,
                                log_level='warning',
                                proxy_headers=False,
                                timeout_graceful_shutdown=5,
                            )
                        )
                        running = asyncio.create_task(server.serve(sockets=[listener]))
                        try:
                            while not server.started and not running.done():
                                await asyncio.sleep(0.05)
                            if running.done():
                                await running
                                raise RuntimeError('Fixture stopped before readiness')
                            private_json(
                                options.ready_file,
                                {
                                    'scope': 'disposable terminal loopback TestRoot only',
                                    'origin': origin,
                                    'pid': os.getpid(),
                                    'accounts_file': str(options.accounts_file),
                                    'temporary_directory': str(folder),
                                    'postgres_directory': str(postgres),
                                    'user_handle': accounts[1]['handle'],
                                    'topic': '/main',
                                    'source_sha256': {
                                        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                        for name in (
                                            'src/msg/data/public-terminal.html',
                                            'src/msg/transports/public_terminal.py',
                                        )
                                    },
                                    **posts,
                                },
                            )
                            created.append(options.ready_file)
                            print(json.dumps({'ready': True, 'origin': origin}), flush=True)
                            await running
                        finally:
                            server.should_exit = True
                            if not running.done():
                                await running
                            unchanged = await business_state(app) == before
                            print(json.dumps({'business_state_unchanged': unchanged}), flush=True)
                    finally:
                        await app.close()
    except asyncio.CancelledError:
        pass
    finally:
        deadline.cancel()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        for path in created:
            path.unlink(missing_ok=True)
        print(
            json.dumps({
                'stopped': True,
                'temporary_data_removed': folder is None or not folder.exists(),
                'postgres_data_removed': postgres is None or not postgres.exists(),
                'accountfiles_removed': not options.accounts_file.exists(),
                'metadata_removed': not options.ready_file.exists(),
                'business_state_unchanged': unchanged,
            }),
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ready-file', type=Path, required=True)
    parser.add_argument('--accounts-file', type=Path, required=True)
    parser.add_argument('--lifetime', type=int, default=1200)
    options = parser.parse_args()
    if not 10 <= options.lifetime <= 3600 or options.ready_file == options.accounts_file:
        parser.error('Lifetime must be 10..3600; output paths must differ')
    asyncio.run(serve(options))


if __name__ == '__main__':
    main()
