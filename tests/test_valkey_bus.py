"""Real Valkey Pub/Sub contract for the post-commit outbox wake-up."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import valkey

from msg.core.models import EffectJob, Event, Principal, ResourceId
from msg.storage.postgres import PostgresMetadataStore
from msg.storage.valkey_bus import ValkeyOutboxSignal


@pytest.fixture
def valkey_url(tmp_path: Path) -> Iterator[str]:
    configured = os.environ.get('MSG_TEST_VALKEY_URL')
    if configured:
        yield configured
        return
    socket = tmp_path / 'valkey.sock'
    process = subprocess.Popen(
        [
            'valkey-server',
            '--port',
            '0',
            '--unixsocket',
            str(socket),
            '--unixsocketperm',
            '700',
            '--save',
            '',
            '--appendonly',
            'no',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    client = valkey.Valkey(unix_socket_path=str(socket), socket_timeout=1)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('temporary Valkey exited during startup')
            try:
                if client.ping():
                    break
            except OSError, valkey.exceptions.ConnectionError:
                time.sleep(0.02)
        else:
            raise RuntimeError('temporary Valkey did not start')
        yield f'unix://{socket}'
    finally:
        client.close()
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.asyncio
async def test_publish_pending_sends_only_job_ids_to_active_subscribers(valkey_url: str) -> None:
    channel = 'msgd:test-effects'
    signal = ValkeyOutboxSignal(valkey_url, channel=channel)
    subscriber = valkey.Valkey.from_url(valkey_url).pubsub()
    try:
        subscriber.subscribe(channel)
        subscriber.get_message(timeout=1)  # subscription acknowledgement
        await signal.publish_pending(['job-1', 'job-2'])
        messages = [
            subscriber.get_message(ignore_subscribe_messages=True, timeout=1) for _ in range(2)
        ]
        assert [message['data'] for message in messages] == [b'job-1', b'job-2']
        assert all(message['channel'] == channel.encode() for message in messages)
    finally:
        subscriber.close()
        signal.client.close()


@pytest.mark.asyncio
async def test_valkey_failure_is_reported_to_post_commit_caller(tmp_path: Path) -> None:
    signal = ValkeyOutboxSignal(f'unix://{tmp_path / "missing.sock"}')
    try:
        with pytest.raises(valkey.exceptions.ConnectionError):
            await signal.publish_pending(['job-1'])
    finally:
        signal.client.close()


@pytest.mark.asyncio
async def test_worker_can_receive_wakeup_hint(valkey_url: str) -> None:
    channel = 'msgd:test-worker-wakeup'
    signal = ValkeyOutboxSignal(valkey_url, channel=channel)
    inspector = valkey.Valkey.from_url(valkey_url)
    try:
        waiting = asyncio.create_task(signal.wait_for_pending(1))
        deadline = time.monotonic() + 1
        while inspector.pubsub_numsub(channel)[0][1] != 1:
            assert time.monotonic() < deadline, 'worker did not subscribe'
            await asyncio.sleep(0.01)
        await signal.publish_pending(['job-3'])
        assert await waiting
        assert not await signal.wait_for_pending(0.05)
    finally:
        inspector.close()
        signal.client.close()


@pytest.mark.asyncio
async def test_postgres_commit_publishes_durable_job_id(pg_dsn: str, valkey_url: str) -> None:
    channel = 'msgd:test-postgres-commit'
    signal = ValkeyOutboxSignal(valkey_url, channel=channel)
    subscriber = valkey.Valkey.from_url(valkey_url).pubsub()
    store = PostgresMetadataStore(pg_dsn, signal=signal)
    now = datetime.now(UTC)
    principal = Principal(
        actor=ResourceId('actor'),
        subject=ResourceId('actor'),
        credential_id=None,
        method='local',
        certificates=(),
        ceiling=(),
    )
    try:
        subscriber.subscribe(channel)
        subscriber.get_message(timeout=1)
        async with store.transaction(write=True) as tx:
            await tx.append_event(
                Event(
                    id='event-1',
                    type='test',
                    time=now,
                    request_id='req-1',
                    actor=ResourceId('actor'),
                    subject=ResourceId('actor'),
                    resources=(),
                    data={},
                )
            )
            await tx.enqueue(
                EffectJob(
                    id='job-1',
                    event_id='event-1',
                    kind='mail',
                    dedupe_key='job-1',
                    principal=principal,
                    operation='mail.send',
                    arguments={},
                    state='pending',
                    attempts=0,
                    next_attempt_at=now,
                    lease_until=None,
                )
            )
        message = subscriber.get_message(ignore_subscribe_messages=True, timeout=1)
        assert message['data'] == b'job-1'
        async with store.transaction(write=False) as tx:
            assert (await tx.job('job-1')).state == 'pending'
    finally:
        subscriber.close()
        signal.client.close()
        await store.close()


@pytest.mark.asyncio
async def test_missed_hint_expires_and_worker_polls_again(valkey_url: str, monkeypatch) -> None:
    """A publish before subscription is lost, but the worker still polls PostgreSQL."""
    from msg import daemon
    from msg.workers import effects, maintenance

    url = valkey_url
    signal = ValkeyOutboxSignal(url, channel='msgd:test-missed-hint')
    await signal.publish_pending(['job-before-subscribe'])
    assert not await signal.wait_for_pending(0.05)
    signal.client.close()

    polled_twice = asyncio.Event()

    class FakeWorker:
        def __init__(self, app):
            self.polls = 0

        async def run_once(self):
            self.polls += 1
            if self.polls == 2:
                polled_twice.set()
            return 0

    async def no_maintenance(*args, **kwargs):
        return None

    class FakeApp:
        settings = SimpleNamespace(server=SimpleNamespace(valkey_url=url))
        closed = False

        async def load(self):
            return self

        async def close(self):
            self.closed = True

    monkeypatch.setattr(effects, 'EffectWorker', FakeWorker)
    monkeypatch.setattr(maintenance, 'run_maintenance', no_maintenance)
    app = FakeApp()
    task = asyncio.create_task(daemon.worker_loop(app))
    try:
        await asyncio.wait_for(polled_twice.wait(), 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert app.closed


@pytest.mark.asyncio
async def test_valkey_disconnect_falls_back_to_database_poll(tmp_path: Path, monkeypatch) -> None:
    """A failed subscription cannot strand durable work in PostgreSQL."""
    from msg import daemon
    from msg.workers import effects, maintenance

    polled_twice = asyncio.Event()

    class FakeWorker:
        def __init__(self, app):
            self.polls = 0

        async def run_once(self):
            self.polls += 1
            if self.polls == 2:
                polled_twice.set()
            return 0

    async def no_maintenance(*args, **kwargs):
        return None

    class FakeApp:
        settings = SimpleNamespace(
            server=SimpleNamespace(valkey_url=f'unix://{tmp_path / "missing.sock"}')
        )
        closed = False

        async def load(self):
            return self

        async def close(self):
            self.closed = True

    monkeypatch.setattr(effects, 'EffectWorker', FakeWorker)
    monkeypatch.setattr(maintenance, 'run_maintenance', no_maintenance)
    app = FakeApp()
    task = asyncio.create_task(daemon.worker_loop(app))
    try:
        await asyncio.wait_for(polled_twice.wait(), 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert app.closed
