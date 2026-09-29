"""Exercise a clean installed server on loopback with disposable data and Root.

Run with -I outside the checkout as a non-root user with sudo -n. The only
privileged path is this invocation's newly generated temporary Root directory.
No production configuration, listener, trust or external recipient is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import psycopg
from psycopg import sql

import msg
from msg.admin.diagnostics import temporary_postgres
from msg.admin.root import _approve_csr, _provision
from msg.application import Application
from msg.client import ClientState, MsgClient
from msg.config import write_example
from msg.core.codec import canonical
from msg.transports.client import GraphQLTransport, HTTPTransport, MCPHTTPTransport, PathGETTransport


def run(*command):
    return subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)


def database_digest(dsn):
    """Read complete row/sequence values without advancing PostgreSQL sequences."""
    hasher = hashlib.sha256()
    with psycopg.connect(dsn) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()
        for (table,) in tables:
            hasher.update(canonical(table))
            rows = connection.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table)))
            for row in sorted(canonical(row) for row in rows):
                hasher.update(len(row).to_bytes(8, 'big'))
                hasher.update(row)
        sequences = connection.execute(
            "SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename"
        ).fetchall()
        for (sequence,) in sequences:
            value = connection.execute(
                sql.SQL('SELECT last_value,is_called FROM {}').format(sql.Identifier(sequence))
            ).fetchone()
            hasher.update(canonical((sequence, value)))
    return hasher.hexdigest()


async def acceptance():
    assert os.geteuid() != 0
    assert 'site-packages' in Path(msg.__file__).parts, 'Use the installed wheel with -I'
    assert msg.__version__ == importlib.metadata.version('msgctl')
    checks = ['installed_wheel_and_version']
    with (
        tempfile.TemporaryDirectory(prefix='msg-installed-rehearsal-') as temporary,
        temporary_postgres() as dsn,
    ):
        folder = Path(temporary)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        settings = write_example(folder / 'etc', folder / 'data', origin, postgres_dsn=dsn)
        assert settings.root_private_dir == folder / 'etc-root'
        assert settings.server.mail is None and not settings.server.valkey_url
        config = folder / 'etc/msgd.toml'
        config.write_text(config.read_text().replace('port = 8042', f'port = {port}'))
        app = Application(settings)
        process = None
        protected = False
        executable = Path(sys.executable).with_name('msgd')
        log = None
        try:
            try:
                csr, root = await _provision(app, 'test-only-' + os.urandom(24).hex())
                await _approve_csr(app, csr, root, expected_digest=None, operator='isolated-rehearsal')
            finally:
                await app.close()
            run('sudo', '-n', 'chown', '-R', '0:0', str(settings.root_private_dir))
            protected = True
            assert not os.access(settings.root_private_dir, os.R_OK | os.X_OK)
            checks.append('root_inaccessible_to_network_user')
            log = (folder / 'server.log').open('w')

            async def start():
                child = subprocess.Popen(
                    [str(executable), '--config-dir', str(settings.config_dir), 'serve'],
                    stdout=log, stderr=log,
                )
                try:
                    async with httpx.AsyncClient(timeout=1, trust_env=False) as http:
                        for _ in range(100):
                            assert child.poll() is None, 'Installed daemon exited during startup'
                            try:
                                response = await http.get(origin + '/healthz')
                                if response.status_code == 200:
                                    assert response.json() == {'status': 'ok'}
                                    return child
                            except httpx.NetworkError:
                                pass
                            await asyncio.sleep(0.05)
                    raise AssertionError('Installed daemon never became ready')
                except BaseException:
                    child.terminate()
                    child.wait(timeout=15)
                    raise

            process = await start()
            checks.append('real_daemon_startup')
            state = ClientState(folder / 'client', server=origin)
            async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
                client = MsgClient(state, HTTPTransport(origin, http=http))
                registered = await client.register('rehearsal-client')
                assert registered.status == 'ok'
                packet = client.prepare(
                    'content.post_create', {'parent': '/main', 'body': 'installed release\r\n'},
                    request_id='restart-stable-write',
                )
                posted = await client.send(packet)
                assert posted.status == 'ok'
                checks.append('real_signed_registration_and_write')
                process.terminate()
                process.wait(timeout=15)
                process = await start()
                repeated = await client.send(packet)
                assert repeated.status == 'ok' and repeated.replayed
                assert repeated.resources == posted.resources
                checks.append('restart_preserves_identity_content_and_idempotency')
                before = database_digest(dsn)
                for transport in (HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport):
                    reader = MsgClient(state, transport(origin, http=http))
                    read = await reader.call('discovery.get', {'id': posted.resources[0].id})
                    assert read.status == 'ok' and read.data['content'] == 'installed release\r\n'
                assert database_digest(dsn) == before
                checks.append('four_real_transports_preserve_all_database_facts')
            before = database_digest(dsn)
            diagnosed = json.loads(run(str(executable), '--config-dir', str(settings.config_dir), 'doctor').stdout)
            assert diagnosed['ok'] is True
            assert database_digest(dsn) == before
            checks.append('installed_doctor_is_read_only')
            run(str(executable), '--config-dir', str(settings.config_dir), 'worker', '--once')
            checks.append('installed_worker_without_mail_or_valkey')
            process.terminate()
            process.wait(timeout=15)
            process = None
            checks.append('clean_daemon_shutdown')
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
            if log is not None:
                log.close()
            if protected:
                run('sudo', '-n', 'rm', '-rf', '--', str(settings.root_private_dir))
    assert not folder.exists()
    return {
        'checks': checks, 'python': sys.version, 'version': msg.__version__,
        'temporary_installation_removed': True, 'external_recipients': 0,
    }


if __name__ == '__main__':
    print(json.dumps(asyncio.run(acceptance()), indent=2))
