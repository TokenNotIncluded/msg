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


@pytest.mark.asyncio
async def test_cancelled_effect_keeps_lease_and_expires_uncertain(installed, monkeypatch):
    from dataclasses import replace
    from datetime import timedelta

    from msg.core.models import EffectJob, Principal

    app, _ = installed
    entered = asyncio.Event()
    now = app.clock()
    job = EffectJob(
        id='job_shutdown_lease',
        event_id='e_shutdown_lease',
        kind='maintenance',
        dedupe_key='shutdown-lease',
        principal=Principal(
            actor=None,
            subject=None,
            credential_id=None,
            method='anonymous',
            certificates=(),
            ceiling=(),
        ),
        operation='system.maintenance',
        arguments={'action': 'rebuild_search'},
        state='pending',
        attempts=0,
        next_attempt_at=now,
        lease_until=None,
    )
    async with app.metadata.transaction(write=True) as tx:
        await tx.enqueue(job)

    async def maintain(app, action, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(maintenance, 'run_maintenance', maintain)
    worker = effects.EffectWorker(app)
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with app.metadata.transaction(write=True) as tx:
        current = await tx.job(job.id)
        assert current.state == 'running'
        assert current.attempts == 1 and current.lease_until > now
        assert tx.setting('job_status:' + job.id) is None
        await tx.save_job(replace(current, lease_until=now - timedelta(seconds=1)))
    claimed, execute = await worker._claim()
    assert claimed.id == job.id and not execute
    async with app.metadata.transaction(write=False) as tx:
        current = await tx.job(job.id)
        assert current.state == 'uncertain'
        assert current.attempts == 1
        assert tx.setting('job_status:' + job.id) == {'code': 'expired_execution_lease'}


@pytest.mark.asyncio
async def test_stop_prevents_pending_effect_claim(installed):
    app, _ = installed
    worker = effects.EffectWorker(app)
    worker.stopping = lambda: True
    assert await worker._claim() == (None, False)
    assert not await worker.run_once()


@pytest.mark.asyncio
async def test_http_sigterm_drains_response_before_storage_close(installed, monkeypatch):
    import socket
    from contextlib import nullcontext

    import httpx
    import uvicorn
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from msg.transports.http import create_app

    app, _ = installed
    entered = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    original_close = app.close

    async def close():
        await original_close()
        closed.set()

    async def slow(request):
        entered.set()
        await release.wait()
        assert not closed.is_set()
        return PlainTextResponse('finished')

    monkeypatch.setattr(app, 'close', close)
    asgi = create_app(app)
    asgi.routes.insert(0, Route('/shutdown-test', slow))
    server = uvicorn.Server(
        uvicorn.Config(
            asgi,
            access_log=False,
            ws='none',
            log_level='error',
            timeout_graceful_shutdown=daemon.SHUTDOWN_GRACE_SECONDS,
        )
    )
    # Use asyncio's OS signal dispatch rather than Uvicorn's post-run re-raise,
    # which would terminate the pytest process after a successful shutdown.
    monkeypatch.setattr(server, 'capture_signals', nullcontext)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, server.handle_exit, signal.SIGTERM, None)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            response = asyncio.create_task(client.get(f'http://127.0.0.1:{port}/shutdown-test'))
            await asyncio.wait_for(entered.wait(), 2)
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.25)
            assert not closed.is_set()
            with pytest.raises(httpx.ConnectError):
                await client.get(f'http://127.0.0.1:{port}/shutdown-test')
            release.set()
            assert (await response).text == 'finished'
            await asyncio.wait_for(serving, 2)
            assert closed.is_set()
    finally:
        release.set()
        server.should_exit = True
        await asyncio.wait_for(serving, 3)
        listener.close()
        loop.remove_signal_handler(signal.SIGTERM)
