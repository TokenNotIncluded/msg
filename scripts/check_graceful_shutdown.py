"""Exercise installed worker shutdown with disposable PostgreSQL and TestRoot.

Run as a non-root user: python -I check_graceful_shutdown.py --installed
Only this process receives SIGTERM. Its sandbox contacts a loopback fixture;
no production configuration, database, keys, queue, or recipients are used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import tempfile
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

import msg
from msg import daemon
from msg.admin.diagnostics import temporary_postgres
from msg.admin.root import _approve_csr, _provision
from msg.application import Application
from msg.config import write_example
from msg.core.models import EffectJob, NetworkPolicy, Principal
from msg.workers import maintenance, sandbox
from msg.workers.effects import EffectWorker


async def probe(app, mode):
    """Isolate the installed queue/runner lifecycle from authority projection."""
    assert mode in {'drain', 'cancel', 'timeout'}
    entered, release, disconnected, closed = (asyncio.Event() for _ in range(4))
    connections, processes = set(), []
    task = None
    original_spawn = asyncio.create_subprocess_exec
    original_maintain = maintenance.run_maintenance
    original_grace = daemon.SHUTDOWN_GRACE_SECONDS
    original_close = app.close

    async def spawn(*arguments, **kwargs):
        process = await original_spawn(*arguments, **kwargs)
        if arguments[0].endswith('/bwrap'):
            processes.append(process)
        return process

    async def close():
        await original_close()
        closed.set()

    async def receive(reader, writer):
        connection = asyncio.current_task()
        connections.add(connection)
        try:
            await reader.readuntil(b'\r\n\r\n')
            entered.set()
            if mode == 'drain':
                await release.wait()
                assert not closed.is_set()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
                await writer.drain()
            else:
                # An unresponsive peer makes the sandbox remain in flight.
                assert await reader.read() == b''
                disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()
            connections.discard(connection)

    server = await asyncio.start_server(receive, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    policy = NetworkPolicy(
        schemes=frozenset({'http'}),
        hosts=('127.0.0.1',),
        ports=frozenset({port}),
        methods=frozenset({'GET'}),
        allow_private=True,
        timeout_ms=1000 if mode == 'timeout' else 5000,
        max_response_bytes=1024,
        max_redirects=0,
    )

    async def maintain(app, action, *, scheduled=False, **kwargs):
        if scheduled:
            return {}
        directory = app.settings.server.staging_dir / ('shutdown-sandbox-' + mode)
        directory.mkdir(parents=True)
        result = await sandbox.BubblewrapRunner(app)(
            SimpleNamespace(executor_key='curl'),
            {'url': f'http://127.0.0.1:{port}/in-flight'},
            (policy,),
            directory,
        )
        assert result.path.read_bytes() == b'ok'
        return {}

    principal = Principal(
        actor=None,
        subject=None,
        credential_id=None,
        method='anonymous',
        certificates=(),
        ceiling=(),
    )
    now = app.clock()
    prefix = 'job_shutdown_sandbox_' + mode + '_'
    async with app.metadata.transaction(write=True) as tx:
        for name in ('000_in_flight', '999_pending'):
            await tx.enqueue(
                EffectJob(
                    id=prefix + name,
                    event_id='e_' + prefix + name,
                    kind='maintenance',
                    dedupe_key=prefix + name,
                    principal=principal,
                    operation='system.maintenance',
                    arguments={'action': 'rebuild_search'},
                    state='pending',
                    attempts=0,
                    next_attempt_at=now,
                    lease_until=None,
                )
            )
    maintenance.run_maintenance = maintain
    asyncio.create_subprocess_exec = spawn
    daemon.SHUTDOWN_GRACE_SECONDS = 0.05 if mode == 'cancel' else original_grace
    app.close = close
    try:
        task = asyncio.create_task(daemon.worker_loop(app))
        await asyncio.wait_for(entered.wait(), 15)
        started = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.02)
        assert not task.done() and not closed.is_set()
        if mode == 'drain':
            release.set()
        await asyncio.wait_for(task, 25)
        elapsed = time.monotonic() - started
        if mode != 'drain':
            await asyncio.wait_for(disconnected.wait(), 2)
        assert closed.is_set()
        assert len(processes) == 1 and processes[0].returncode is not None
        async with app.metadata.transaction(write=False) as tx:
            running = await tx.job(prefix + '000_in_flight')
            pending = await tx.job(prefix + '999_pending')
            assert (
                running.state
                == {'drain': 'done', 'cancel': 'running', 'timeout': 'uncertain'}[mode]
            )
            assert running.attempts == 1
            assert pending.state == 'pending'
            assert pending.attempts == 0 and pending.lease_until is None
            status = tx.setting('job_status:' + running.id)
            if mode == 'cancel':
                assert running.lease_until > now and status is None
            elif mode == 'timeout':
                assert running.lease_until is None and status == {'code': 'external_uncertain'}
            else:
                assert running.lease_until is None
        if mode == 'cancel':
            async with app.metadata.transaction(write=True) as tx:
                await tx.save_job(replace(running, lease_until=app.clock() - timedelta(seconds=1)))
            swept, execute = await EffectWorker(app)._claim()
            assert swept.id == running.id and not execute
            async with app.metadata.transaction(write=False) as tx:
                swept = await tx.job(running.id)
                assert swept.state == 'uncertain' and swept.attempts == 1
                assert tx.setting('job_status:' + swept.id) == {'code': 'expired_execution_lease'}
        return {
            'mode': mode,
            'shutdown_grace_seconds': daemon.SHUTDOWN_GRACE_SECONDS,
            'sigterm_to_closed_seconds': elapsed,
            'state_after_shutdown': running.state,
            'attempts': running.attempts,
            'next_job_unclaimed': True,
            'sandbox_reaped': True,
            'storage_closed': True,
            'lease_expiry_checked': mode == 'cancel',
            'expired_cancelled_attempt_replayed': False if mode == 'cancel' else None,
        }
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        server.close()
        await server.wait_closed()
        for connection in tuple(connections):
            connection.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        app.close = original_close
        daemon.SHUTDOWN_GRACE_SECONDS = original_grace
        maintenance.run_maintenance = original_maintain
        asyncio.create_subprocess_exec = original_spawn


async def acceptance(*, installed):
    assert os.geteuid() != 0, 'Use a non-root OS account'
    if installed:
        assert 'site-packages' in Path(msg.__file__).parts, 'Use the installed package with -I'
    assert 'MSG_TEST_POSTGRES_URL_TEMPLATE' not in os.environ, 'Use a new disposable cluster'
    outcomes = []
    with (
        tempfile.TemporaryDirectory(prefix='msg-shutdown-rehearsal-') as temporary,
        temporary_postgres() as dsn,
    ):
        folder = Path(temporary)
        for mode in ('drain', 'cancel', 'timeout'):
            database = 'msg_shutdown_' + mode
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
            settings = write_example(
                folder / mode / 'etc',
                folder / mode / 'data',
                'http://127.0.0.1',
                postgres_dsn=urlsplit(dsn)._replace(path='/' + database).geturl(),
            )
            assert settings.server.mail is None and not settings.server.valkey_url
            app = Application(settings)
            try:
                csr, root = await _provision(app, 'test-only-' + os.urandom(24).hex())
                await _approve_csr(
                    app, csr, root, expected_digest=None, operator='isolated-shutdown-rehearsal'
                )
                outcomes.append(await probe(app, mode))
            finally:
                await app.close()
    return {
        'version': msg.__version__,
        'module': str(Path(msg.__file__).resolve()),
        'scope': 'isolated installation; production in-flight queue not exercised',
        'temporary_installation_removed': not folder.exists(),
        'external_recipients': 0,
        'outcomes': outcomes,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true')
    options = parser.parse_args()
    print(json.dumps(asyncio.run(acceptance(installed=options.installed)), indent=2))
