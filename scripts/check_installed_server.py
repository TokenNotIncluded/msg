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
import math
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import psycopg
from psycopg import sql

import msg
from msg.admin.backups import backup, restore
from msg.admin.diagnostics import temporary_postgres
from msg.admin.root import _approve_csr, _provision
from msg.application import Application
from msg.client import ClientState, MsgClient
from msg.config import load_settings, write_example
from msg.core.codec import canonical, loads
from msg.transports.client import (
    GraphQLTransport,
    HTTPTransport,
    MCPHTTPTransport,
    PathGETTransport,
)


def run(*command):
    return subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)


def database_facts(dsn):
    """Read complete row/sequence values without advancing PostgreSQL sequences."""
    facts = {'tables': {}, 'sequences': {}}
    with psycopg.connect(dsn) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()
        for (table,) in tables:
            rows = connection.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table)))
            facts['tables'][table] = sorted(canonical(row) for row in rows)
        sequences = connection.execute(
            "SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename"
        ).fetchall()
        for (sequence,) in sequences:
            value = connection.execute(
                sql.SQL('SELECT last_value,is_called FROM {}').format(sql.Identifier(sequence))
            ).fetchone()
            facts['sequences'][sequence] = canonical(value)
    return facts


def database_digest(dsn):
    return hashlib.sha256(canonical(database_facts(dsn))).hexdigest()


def latency_report(samples, elapsed):
    ordered = sorted(samples)
    assert ordered and elapsed > 0
    return {
        'completed': len(ordered),
        'elapsed_seconds': elapsed,
        'operations_per_second': len(ordered) / elapsed,
        'latency_p50_seconds': ordered[math.ceil(len(ordered) * 0.5) - 1],
        'latency_p95_seconds': ordered[math.ceil(len(ordered) * 0.95) - 1],
        'latency_max_seconds': ordered[-1],
    }


async def concurrent_workload(folder, origin, http, dsn):
    """Bounded workload on independently signed clients, not production load."""
    writers = []
    for index in range(4):
        client = MsgClient(
            ClientState(folder / f'capacity-client-{index}', server=origin),
            HTTPTransport(origin, http=http),
        )
        assert (await client.register(f'capacity-client-{index}')).status == 'ok'
        writers.append(client)

    async def write(client, index):
        measured = []
        for offset in range(8):
            body = f'capacity-{index}-{offset}\n' + 'bounded content\n' * 64
            packet = client.prepare(
                'content.post_create',
                {'parent': '/main', 'body': body},
                request_id=f'capacity-{index}-{offset}',
            )
            started = time.perf_counter()
            posted = await client.send(packet)
            measured.append((time.perf_counter() - started, posted.resources, body))
            assert posted.status == 'ok', 'Concurrent signed write failed'
            assert len(posted.resources) == 1
        return measured

    started = time.perf_counter()
    written = await asyncio.gather(*(write(client, i) for i, client in enumerate(writers)))
    writes = latency_report(
        [row[0] for group in written for row in group], time.perf_counter() - started
    )
    assert len({row[1][0].id for group in written for row in group}) == 32
    before = database_digest(dsn)

    async def read(client, group):
        measured = []
        for _, references, body in group:
            started = time.perf_counter()
            result = await client.call('discovery.get', {'id': references[0].id})
            measured.append(time.perf_counter() - started)
            assert result.status == 'ok' and result.data['content'] == body
        return measured

    started = time.perf_counter()
    reads = await asyncio.gather(
        *(read(client, group) for client, group in zip(writers, written, strict=True))
    )
    reading = latency_report(
        [sample for group in reads for sample in group], time.perf_counter() - started
    )
    assert database_digest(dsn) == before, 'Concurrent reads changed authoritative facts'
    return {'concurrent_clients': 4, 'writes': writes, 'reads': reading}


async def restore_drill(settings, folder, client, packet, source_dsn):
    """Compare every row and sequence, allowing only exact quarantine changes."""
    app = Application(settings)
    try:
        await app.load()
        original = database_facts(source_dsn)
        started = time.perf_counter()
        archive = folder / 'recovery.zip'
        result = await backup(app, archive)
        backup_seconds = time.perf_counter() - started
        assert result['root_private_key_included'] is False
        assert database_facts(source_dsn) == original
    finally:
        await app.close()
    with temporary_postgres() as destination_dsn:
        started = time.perf_counter()
        restored = restore(
            archive, folder / 'restore-etc', folder / 'restore-data', postgres_dsn=destination_dsn
        )
        restore_seconds = time.perf_counter() - started
        assert restored['promotion'] == 'blocked'
        current = database_facts(destination_dsn)
        expected_settings = dict(loads(row) for row in original['tables']['settings'])
        current_settings = dict(loads(row) for row in current['tables']['settings'])
        quarantine = loads(current_settings['recovery_quarantine'])
        assert quarantine == {
            'format': 'msg-recovery-quarantine-v1',
            'outbound_enabled': False,
            'source_backup_sha256': result['sha256'],
            'revocation_replay': 'required',
            'authority': 'health_only',
        }
        runtime = loads(expected_settings.get('runtime_config', '{}'))
        expected_settings['runtime_config'] = canonical({
            **runtime,
            'accept_writes': False,
            'cleanup_enabled': False,
        }).decode()
        expected_settings['recovery_quarantine'] = current_settings['recovery_quarantine']
        original['tables']['settings'] = sorted(canonical(row) for row in expected_settings.items())
        assert current == original, 'Restore changed other table values or sequence state'
        for marker_present in (True, False):
            if not marker_present:
                (folder / 'restore-etc/recovery-drill.json').unlink()
            recovered = Application(load_settings(folder / 'restore-etc'))
            try:
                await recovered.load()
                replay = await recovered.executor.execute(packet)
                assert replay.error.code == 'writes_paused'
                read = client.prepare('discovery.get', {'id': client.state.subject})
                denied = await recovered.executor.execute(read)
                assert denied.error.code == 'recovery_quarantined'
                assert database_facts(destination_dsn) == current
            finally:
                await recovered.close()
    return {
        'backup_seconds': backup_seconds,
        'restore_seconds': restore_seconds,
        'archive_bytes': archive.stat().st_size,
        'all_table_values_and_sequences_preserved': True,
        'persistent_quarantine_without_marker': True,
        'promotion_performed': False,
    }


async def acceptance():
    assert os.geteuid() != 0
    assert 'site-packages' in Path(msg.__file__).parts, 'Use the installed wheel with -I'
    assert msg.__version__ == importlib.metadata.version('msgctl')
    checks = ['installed_wheel_and_version']
    measurements = {'platform': platform.platform(), 'cpu_count': os.cpu_count()}
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
                await _approve_csr(
                    app, csr, root, expected_digest=None, operator='isolated-rehearsal'
                )
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
                    stdout=log,
                    stderr=log,
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

            started = time.perf_counter()
            process = await start()
            measurements['startup_seconds'] = time.perf_counter() - started
            checks.append('real_daemon_startup')
            state = ClientState(folder / 'client', server=origin)
            async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
                client = MsgClient(state, HTTPTransport(origin, http=http))
                registered = await client.register('rehearsal-client')
                assert registered.status == 'ok'
                packet = client.prepare(
                    'content.post_create',
                    {'parent': '/main', 'body': 'installed release\r\n'},
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
                for transport in (
                    HTTPTransport,
                    PathGETTransport,
                    GraphQLTransport,
                    MCPHTTPTransport,
                ):
                    reader = MsgClient(state, transport(origin, http=http))
                    read = await reader.call('discovery.get', {'id': posted.resources[0].id})
                    assert read.status == 'ok' and read.data['content'] == 'installed release\r\n'
                assert database_digest(dsn) == before
                checks.append('four_real_transports_preserve_all_database_facts')
                measurements['workload'] = await concurrent_workload(folder, origin, http, dsn)
                checks.append('concurrent_signed_writes_and_read_conservation')
                status = (Path('/proc') / str(process.pid) / 'status').read_text()
                measurements['daemon_memory_kib'] = {
                    name: int(line.split()[1])
                    for line in status.splitlines()
                    for name in ('VmRSS:', 'VmHWM:')
                    if line.startswith(name)
                }
                before = database_digest(dsn)
                process.kill()
                process.wait(timeout=15)
                started = time.perf_counter()
                process = await start()
                measurements['sigkill_restart_seconds'] = time.perf_counter() - started
                assert database_digest(dsn) == before
                repeated = await client.send(packet)
                assert repeated.status == 'ok' and repeated.replayed
                assert repeated.resources == posted.resources
                assert database_digest(dsn) == before
                checks.append('sigkill_restart_preserves_all_facts_and_idempotency')
            before = database_digest(dsn)
            diagnosed = json.loads(
                run(str(executable), '--config-dir', str(settings.config_dir), 'doctor').stdout
            )
            assert diagnosed['ok'] is True
            assert database_digest(dsn) == before
            checks.append('installed_doctor_is_read_only')
            run(str(executable), '--config-dir', str(settings.config_dir), 'worker', '--once')
            checks.append('installed_worker_without_mail_or_valkey')
            process.terminate()
            process.wait(timeout=15)
            process = None
            checks.append('clean_daemon_shutdown')
            measurements['recovery'] = await restore_drill(settings, folder, client, packet, dsn)
            checks.append('installed_backup_restore_conservation_and_persistent_quarantine')
            measurements['content_file_bytes'] = sum(
                entry.stat().st_size
                for directory in (
                    settings.server.content_dir,
                    settings.server.repositories_dir,
                    settings.server.blob_dir,
                    settings.server.staging_dir,
                )
                for entry in directory.rglob('*')
                if entry.is_file()
            )
            measurements['disk_free_bytes'] = shutil.disk_usage(folder).free
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
        'checks': checks,
        'python': sys.version,
        'version': msg.__version__,
        'temporary_installation_removed': True,
        'external_recipients': 0,
        'measurements': measurements,
        'measurement_scope': 'bounded disposable CI workload; not target-host capacity',
    }


if __name__ == '__main__':
    print(json.dumps(asyncio.run(acceptance()), indent=2))
