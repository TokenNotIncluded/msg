"""Serve the real app on loopback with disposable PostgreSQL and TestRoot.

Example (use the candidate's src on PYTHONPATH):
  TMPDIR=/home/lightjunction/.cache/msg-details-tmp-20261002 \
  PYTHONPATH=src .venv/bin/python scripts/serve_flight_fixture.py \
    --ready-file /tmp/msg-flight-ready.json --lifetime 900

The JSON readiness file contains origin and the ordinary /@root/web/ URL.
SIGTERM or the finite lifetime closes the app, stops PG, and removes test data
and files. No production settings, keys, database, recipients, or WS mocks.
Optional --browser-accounts-file writes two ordinary OAuth session cookies to
a new mode-0600 file for local browser contexts; it is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn

import msg
from msg.admin.diagnostics import temporary_postgres
from msg.admin.root import _approve_csr, _provision
from msg.application import Application
from msg.bootstrap import ROOT_WEB_SAMPLE
from msg.config import write_example
from msg.constants import ROOT_SUBJECT
from msg.core.codec import b64
from msg.core.requests import request_for
from msg.oauth_config import OAuthConfig
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer, subject_id
from msg.transports.http import create_app
from msg.transports.oauth_http import csrf


class FixtureServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # This script's handler also covers provisioning before Uvicorn starts.
        yield


def private_json(path, value):
    """Only create task-owned files, never overwrite an existing artifact."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(value, output, indent=2)
        output.write('\n')


async def call(app, operation, arguments, *, key=None, subject=None, version=1):
    result = await app.executor.execute(
        request_for(
            operation,
            arguments,
            app.settings.service_url,
            signer=key,
            subject=subject,
            contract_version=version,
            expires_at=app.clock() + timedelta(seconds=120),
        ),
        entry='network',
    )
    if result.status != 'ok':
        code = result.error.code if result.error else result.status
        raise RuntimeError(f'Fixture operation {operation} failed: {code}')
    return result


async def seed(app, number):
    """Public topology and rings come from real signed registrations/posts/follows."""
    accounts = []
    posts = follows = 0
    for index in range(number):
        key = Ed25519Signer.generate()
        subject = subject_id(key.public_key)
        handle = f'fixture-{index:02}'
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
        for post_index in range(index % 4):
            await call(
                app,
                'content.post_create',
                {
                    'parent': '/main',
                    'body': f'Local flight fixture · @{handle} · post {post_index + 1}',
                },
                key=key,
                subject=subject,
            )
            posts += 1
    # Pair two accounts, a three-account component, and a Root-linked group.
    edges = [(0, 1), (1, 0), (2, 3), (3, 4), (4, 2), (5, None)]
    for index in range(6, number):
        if index % 3:
            edges.append((index, 5 if number > 5 else 0))
    for source, target in edges:
        if source >= number or (target is not None and target >= number):
            continue
        account = accounts[source]
        await call(
            app,
            'communication.follow',
            {'id': accounts[target]['subject_id'] if target is not None else ROOT_SUBJECT},
            key=account['key'],
            subject=account['subject_id'],
        )
        follows += 1
    return accounts, {
        'registered_users': len(accounts),
        'public_posts': posts,
        'public_follows': follows,
    }


async def browser_accounts(app, router, accounts):
    """Run the ordinary login/code approval/CSRF polling flow, not a cookie bypass."""
    result = []
    for account in accounts[:2]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router), base_url=app.settings.service_url
        ) as http:
            login = await http.get('/oauth/login')
            if login.status_code != 200:
                raise RuntimeError('Fixture OAuth login unavailable')
            code = login.text.split('msg auth approve ', 1)[1].split('</code>', 1)[0]
            await call(
                app,
                'identity.oauth_approve',
                {'user_code': code, 'decision': 'approve'},
                key=account['key'],
                subject=account['subject_id'],
            )
            response = await http.post(
                '/oauth/login/poll',
                json={'csrf': csrf(http.cookies.get('msg_login'))},
                headers={'Origin': app.settings.service_url},
            )
            if response.status_code != 200 or response.json() != {'logged_in': True}:
                raise RuntimeError('Fixture OAuth approval failed')
            cookie = http.cookies.get('msg_session')
            if not cookie:
                raise RuntimeError('Fixture OAuth session missing')
            result.append({
                'handle': account['handle'],
                'subject_id': account['subject_id'],
                'cookies': [
                    {
                        'name': 'msg_session',
                        'value': cookie,
                        'url': app.settings.service_url + '/',
                        'httpOnly': True,
                        'secure': False,
                        'sameSite': 'Lax',
                    }
                ],
            })
    return {'scope': 'disposable loopback TestRoot only', 'accounts': result}


async def serve(options):
    if os.geteuid() == 0:
        raise RuntimeError('Run as a non-root OS account')
    if 'MSG_TEST_POSTGRES_URL_TEMPLATE' in os.environ:
        raise RuntimeError('Remove MSG_TEST_POSTGRES_URL_TEMPLATE: fixture requires a new cluster')
    for path in (options.ready_file, options.browser_accounts_file):
        if path and (path.exists() or path.is_symlink()):
            raise RuntimeError(f'Refusing to overwrite fixture output: {path}')
    if options.browser_accounts_file and options.users < 2:
        raise RuntimeError('Two browser accounts require --users 2 or more')
    if options.ready_file and options.ready_file == options.browser_accounts_file:
        raise RuntimeError('Readiness and browser account files must differ')
    created = []
    server = None
    app = None
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()

    def stop():
        if server is None:
            task.cancel()
        else:
            server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    deadline = loop.call_later(options.lifetime, stop)
    folder = None
    postgres_directory = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', options.port))
            listener.setblocking(False)
            port = listener.getsockname()[1]
            origin = f'http://127.0.0.1:{port}'
            with (
                tempfile.TemporaryDirectory(prefix='msg-flight-fixture-') as temporary,
                temporary_postgres() as dsn,
            ):
                folder = Path(temporary)
                postgres_directory = Path(parse_qs(urlsplit(dsn).query)['host'][0]).parent
                settings = write_example(folder / 'etc', folder / 'data', origin, postgres_dsn=dsn)
                if settings.server.mail is not None or settings.server.valkey_url:
                    raise RuntimeError('Fixture must have no mail or Valkey')
                if options.browser_accounts_file:
                    settings = replace(settings, oauth=OAuthConfig(enabled=True))
                app = Application(settings)
                try:
                    csr, root = await _provision(app, 'disposable-flight-' + os.urandom(24).hex())
                    await _approve_csr(
                        app, csr, root, expected_digest=None, operator='isolated-flight-fixture'
                    )
                    accounts, seeded = await seed(app, options.users)
                    router = create_app(app)
                    # Serve the ordinary installed website with its actual pinned CSP.
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=router), base_url=origin
                    ) as http:
                        response = await http.get('/@root/web/')
                        if response.status_code != 200 or response.content != ROOT_WEB_SAMPLE:
                            raise RuntimeError('Installed release-owned Root website unavailable')
                        csp = response.headers.get('content-security-policy', '')
                        if "script-src 'sha256-" not in csp or "connect-src 'self'" not in csp:
                            raise RuntimeError('Root website lacks the normal pinned CSP')
                    if options.browser_accounts_file:
                        private_json(
                            options.browser_accounts_file,
                            await browser_accounts(app, router, accounts),
                        )
                        created.append(options.browser_accounts_file)
                    server = FixtureServer(
                        uvicorn.Config(
                            router,
                            host='127.0.0.1',
                            port=port,
                            access_log=False,
                            log_level='warning',
                            timeout_graceful_shutdown=5,
                        )
                    )
                    running = asyncio.create_task(server.serve(sockets=[listener]))
                    try:
                        while not server.started and not running.done():
                            await asyncio.sleep(0.05)
                        if running.done():
                            await running
                            raise RuntimeError('Fixture server failed before readiness')
                        readiness = {
                            'ready': True,
                            'pid': os.getpid(),
                            'origin': origin,
                            'url': origin + '/@root/web/',
                            'scope': 'real disposable PG/TestRoot/app; synthetic signed public content; no WS mocks',
                            'world_position_override': False,
                            'version': msg.__version__,
                            'module': str(Path(msg.__file__).resolve()),
                            'lifetime_seconds': options.lifetime,
                            'temporary_directory': str(folder),
                            'postgres_directory': str(postgres_directory),
                            'browser_accounts_file': str(options.browser_accounts_file)
                            if options.browser_accounts_file
                            else None,
                            **seeded,
                        }
                        if options.ready_file:
                            private_json(options.ready_file, readiness)
                            created.append(options.ready_file)
                        print(json.dumps(readiness), flush=True)
                        await running
                    finally:
                        server.should_exit = True
                        if not running.done():
                            await running
                finally:
                    await app.close()
    except asyncio.CancelledError:
        pass
    finally:
        deadline.cancel()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        for path in created:
            path.unlink(missing_ok=True)
        print(
            json.dumps({
                'stopped': True,
                'temporary_data_removed': folder is None or not folder.exists(),
                'postgres_data_removed': postgres_directory is None
                or not postgres_directory.exists(),
                'output_files_removed': all(not path.exists() for path in created),
            }),
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0, help='loopback port; 0 reserves a free port')
    parser.add_argument(
        '--users', type=int, default=6, help='signed synthetic public identities, 0..128'
    )
    parser.add_argument(
        '--lifetime', type=int, default=900, help='maximum process lifetime, 10..3600 seconds'
    )
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--browser-accounts-file', type=Path)
    options = parser.parse_args()
    if (
        not 0 <= options.port <= 65535
        or not 0 <= options.users <= 128
        or not 10 <= options.lifetime <= 3600
    ):
        parser.error('port must be 0..65535, users 0..128, lifetime 10..3600')
    asyncio.run(serve(options))


if __name__ == '__main__':
    main()
