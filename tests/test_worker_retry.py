"""Keep a busy database from restarting the worker or hiding other failures."""

import asyncio
import os
import signal
from types import SimpleNamespace

import pytest

from msg import daemon
from msg.core.errors import Failure
from msg.workers import effects, maintenance


class App:
    settings = SimpleNamespace(server=SimpleNamespace(valkey_url=None))

    def __init__(self):
        self.loads = self.closes = 0

    async def load(self):
        self.loads += 1

    async def close(self):
        self.closes += 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['maintenance', 'claim'])
async def test_busy_stage_recovers_in_same_worker(monkeypatch, capsys, stage):
    calls = []
    failures = 0
    workers = []

    async def maybe_busy(name):
        nonlocal failures
        calls.append(name)
        if stage == name and failures < 2:
            failures += 1
            raise Failure('server_busy', retryable=True, details={'secret': 'private detail'})

    class Worker:
        def __init__(self, app):
            workers.append(self)

        async def run_once(self):
            await maybe_busy('claim')
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.01)
            return True

    async def maintain(app, action, **kwargs):
        await maybe_busy('maintenance')

    monkeypatch.setattr(effects, 'EffectWorker', Worker)
    monkeypatch.setattr(maintenance, 'run_maintenance', maintain)
    monkeypatch.setattr(daemon, 'WORKER_RETRY_INITIAL_SECONDS', 0.001)
    app = App()
    assert await asyncio.wait_for(daemon.worker_loop(app), 2) is None
    assert (app.loads, app.closes, len(workers)) == (1, 1, 1)
    assert failures == 2
    assert calls.count('claim') == (3 if stage == 'claim' else 1)
    output = capsys.readouterr().out
    assert output.count('worker_retry') == 2
    assert 'private detail' not in output


@pytest.mark.asyncio
async def test_busy_backoff_caps_and_resets_after_success(monkeypatch):
    attempts = 0
    retries = []

    class Worker:
        def __init__(self, app):
            pass

        async def run_once(self):
            nonlocal attempts
            attempts += 1
            if attempts in (1, 2, 3, 4, 5, 7):
                raise Failure('server_busy', retryable=True)
            if attempts == 6:
                return True
            raise Failure('permission_denied', retryable=True)

    async def maintain(*args, **kwargs):
        pass

    monkeypatch.setattr(effects, 'EffectWorker', Worker)
    monkeypatch.setattr(maintenance, 'run_maintenance', maintain)
    monkeypatch.setattr(daemon, 'WORKER_RETRY_INITIAL_SECONDS', 0.001)
    monkeypatch.setattr(daemon, 'WORKER_RETRY_MAX_SECONDS', 0.008)
    monkeypatch.setattr(daemon, 'emit', lambda item: retries.append(item['retry_after_seconds']))
    app = App()
    with pytest.raises(Failure, match='permission_denied'):
        await asyncio.wait_for(daemon.worker_loop(app), 2)
    assert retries == [0.001, 0.002, 0.004, 0.008, 0.008, 0.001]
    assert attempts == 8
    assert app.closes == 1


@pytest.mark.asyncio
async def test_sigterm_interrupts_busy_backoff_without_another_claim(monkeypatch):
    retrying = asyncio.Event()
    claims = []

    class Worker:
        def __init__(self, app):
            pass

        async def run_once(self):
            claims.append('claim')
            raise Failure('server_busy', retryable=True)

    async def maintain(*args, **kwargs):
        pass

    monkeypatch.setattr(effects, 'EffectWorker', Worker)
    monkeypatch.setattr(maintenance, 'run_maintenance', maintain)
    monkeypatch.setattr(daemon, 'WORKER_RETRY_INITIAL_SECONDS', 30)
    monkeypatch.setattr(daemon, 'emit', lambda item: retrying.set())
    app = App()
    task = asyncio.create_task(daemon.worker_loop(app))
    try:
        await asyncio.wait_for(retrying.wait(), 2)
        assert not app.closes
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(task, 1) is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert claims == ['claim']
    assert app.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('once', 'code', 'retryable'),
    [(True, 'server_busy', True), (False, 'server_busy', False), (False, 'bad_request', True)],
)
async def test_once_or_other_failures_still_propagate(monkeypatch, once, code, retryable):
    class Worker:
        def __init__(self, app):
            pass

        async def run_once(self):
            raise Failure(code, retryable=retryable)

    async def maintain(*args, **kwargs):
        pass

    monkeypatch.setattr(effects, 'EffectWorker', Worker)
    monkeypatch.setattr(maintenance, 'run_maintenance', maintain)
    app = App()
    with pytest.raises(Failure, match=code):
        await asyncio.wait_for(daemon.worker_loop(app, once=once), 2)
    assert (app.loads, app.closes) == (1, 1)
