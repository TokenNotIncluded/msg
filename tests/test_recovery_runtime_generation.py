"""Promotion invalidates idle runtimes even if they never observed quarantine."""
import httpx
import pytest
from test_hosting_runtime import reader_settings, runtime_class, website  # noqa: F401
from test_service import NOW, call

from msg.application import Application
from msg.core.errors import Failure
from msg.transports.http import create_app
from msg.workers.effects import EffectWorker
from msg.workers.maintenance import run_maintenance


async def promote_generation(app):
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('recovery_quarantine', {'restore': 'test'})
    # Deliberately no calls on the already loaded peer during quarantine.
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('recovery_runtime_generation', 'new-verified-promotion')
        tx.execute("DELETE FROM settings WHERE key='recovery_quarantine'", write=True)


@pytest.mark.asyncio
async def test_idle_executor_and_http_require_restart_after_promotion(installed):
    app, _ = installed
    peer = await Application(app.settings, clock=lambda: NOW).load()
    try:
        await promote_generation(app)
        result = await call(peer, 'discovery.get', {'id': '/main'})
        assert result.status == 'error' and result.error.code == 'recovery_runtime_stale'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(peer)),
                                     base_url=app.settings.service_url) as http:
            for path in ('/', '/healthz', '/_read/t_main/json'):
                response = await http.get(path)
                assert response.status_code == 503
                assert response.json()['error']['code'] == 'recovery_runtime_stale'
        fresh = await Application(app.settings, clock=lambda: NOW).load()
        try:
            assert (await call(fresh, 'discovery.get', {'id': '/main'})).status == 'ok'
        finally:
            await fresh.close()
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_idle_workers_claim_and_timers_cannot_mutate_promoted_database(installed):
    app, _ = installed
    peer = await Application(app.settings, clock=lambda: NOW).load()
    worker = EffectWorker(peer)
    try:
        await promote_generation(app)
        from msg.market.arbitration import resolve_cases
        from msg.market.escrow import resolve_due
        from msg.workers.leases import current_attempt
        for timer in (resolve_due, resolve_cases):
            with pytest.raises(Failure, match='recovery_runtime_stale'):
                await timer(peer)
        async with peer.metadata.transaction(write=True) as tx:
            with pytest.raises(Failure, match='recovery_runtime_stale'):
                await current_attempt(peer, tx, None)
        for work in (worker.run_once, worker._claim):
            with pytest.raises(Failure, match='recovery_runtime_stale'):
                await work()
        with pytest.raises(Failure, match='recovery_runtime_stale'):
            await run_maintenance(peer, 'cleanup_expired', scheduled=True)
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_idle_readonly_hosting_requires_restart_without_expanding_role(installed, reader_settings):  # noqa: F811
    app, _ = installed
    await website(app)
    reader = await runtime_class()(reader_settings, clock=lambda: NOW).load()
    from msg.extensions.hosting import hosting_app
    try:
        await promote_generation(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(reader)),
                                     base_url=app.settings.service_url) as http:
            response = await http.get('/@readonly-host/web/')
            assert response.status_code != 200
            assert b'recovery_runtime_stale' in response.content
            assert b'<h1>published</h1>' not in response.content
        fresh = await runtime_class()(reader_settings, clock=lambda: NOW).load()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(fresh)),
                                         base_url=app.settings.service_url) as http:
                response = await http.get('/@readonly-host/web/')
                assert response.status_code == 200
                assert b'<h1>published</h1>' in response.content
        finally:
            await fresh.close()
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_generation_mismatch_remains_latched_even_if_setting_reverted(installed):
    app, _ = installed
    await promote_generation(app)
    with pytest.raises(Failure, match='recovery_runtime_stale'):
        await app.executor.require_current_runtime()
    async with app.metadata.transaction(write=True) as tx:
        tx.execute("DELETE FROM settings WHERE key='recovery_runtime_generation'", write=True)
    with pytest.raises(Failure, match='recovery_runtime_stale'):
        await app.executor.require_current_runtime()
    with pytest.raises(Failure, match='recovery_runtime_stale'):
        await app.load()


@pytest.mark.asyncio
async def test_promotion_during_load_cannot_pin_new_generation_to_old_runtime(installed, monkeypatch):
    from contextlib import asynccontextmanager

    app, _ = installed
    peer = Application(app.settings, clock=lambda: NOW)
    await peer.open_storage()
    original = peer.metadata.transaction
    entered = 0

    @asynccontextmanager
    async def promote_between_startup_transactions(*, write):
        nonlocal entered
        entered += 1
        if entered == 2:
            await promote_generation(app)
        async with original(write=write) as tx:
            yield tx

    monkeypatch.setattr(peer.metadata, 'transaction', promote_between_startup_transactions)
    try:
        with pytest.raises(Failure, match='recovery_runtime_stale'):
            await peer.load()
        assert not peer._loaded
    finally:
        await peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, False, '', {}])
async def test_present_invalid_generation_is_not_treated_as_an_old_installation(installed, value):
    app, _ = installed
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('recovery_runtime_generation', value)
    result = await call(app, 'discovery.get', {'id': '/main'})
    assert result.status == 'error' and result.error.code == 'recovery_runtime_stale'
    peer = Application(app.settings, clock=lambda: NOW)
    try:
        with pytest.raises(Failure, match='recovery_runtime_stale'):
            await peer.load()
    finally:
        await peer.close()
