"""Exercise actual SIGTERM delivery while a worker stage is in flight."""

import asyncio
import os
import signal
from types import SimpleNamespace

import pytest

from msg import daemon
from msg.workers import effects, maintenance


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['load', 'maintenance', 'job'])
async def test_sigterm_drains_current_stage_without_new_claim(monkeypatch, stage):
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = []
    claims = []

    async def pause(name):
        if name == stage:
            entered.set()
            await release.wait()
        completed.append(name)

    class App:
        settings = SimpleNamespace(server=SimpleNamespace(valkey_url=None))
        closed = False

        async def load(self):
            await pause('load')

        async def close(self):
            self.closed = True

    class Worker:
        def __init__(self, app):
            pass

        async def run_once(self):
            claims.append('claim')
            await pause('job')
            return True

    async def maintain(app, action, **kwargs):
        await pause('maintenance')

    monkeypatch.setattr(effects, 'EffectWorker', Worker)
    monkeypatch.setattr(maintenance, 'run_maintenance', maintain)
    app = App()
    task = asyncio.create_task(daemon.worker_loop(app))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.02)
        assert not task.done()
        assert not app.closed
        release.set()
        await asyncio.wait_for(task, 2)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert app.closed
    assert completed.count(stage) == 1
    assert claims == (['claim'] if stage == 'job' else [])


@pytest.mark.asyncio
async def test_sigterm_budget_cancels_job_without_reporting_completion(monkeypatch):
    entered = asyncio.Event()
    states = []

    class App:
        settings = SimpleNamespace(server=SimpleNamespace(valkey_url=None))
        closed = False

        async def load(self):
            pass

        async def close(self):
            self.closed = True

    class Worker:
        def __init__(self, app):
            pass

        async def run_once(self):
            states.append('running')
            entered.set()
            try:
                await asyncio.Event().wait()
                states.append('done')
            except asyncio.CancelledError:
                states.append('cancelled')
                raise

    async def maintain(*args, **kwargs):
        pass

    monkeypatch.setattr(effects, 'EffectWorker', Worker)
    monkeypatch.setattr(maintenance, 'run_maintenance', maintain)
    monkeypatch.setattr(daemon, 'SHUTDOWN_GRACE_SECONDS', 0.02)
    app = App()
    task = asyncio.create_task(daemon.worker_loop(app))
    await asyncio.wait_for(entered.wait(), 2)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 2)
    assert states == ['running', 'cancelled']
    assert app.closed
