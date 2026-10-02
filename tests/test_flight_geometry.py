"""A stalled public projection cannot starve snapshots or publish stale wells."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from msg.core.errors import Failure
from msg.transports import flight_space
from msg.transports.flight_collectibles import CollectibleField
from msg.transports.flight_simulation import FlightWorld
from msg.transports.flight_space import FlightHub, Peer


def slow_room(monkeypatch):
    monkeypatch.setattr(flight_space, 'GEOMETRY_READ_SECONDS', 0.02)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def execute(request):
        assert request.operation == 'discovery.get'
        assert request.subject is None and request.proof is None
        assert request.arguments['id'] == 'u_root'
        assert list(request.arguments['fields']) == ['star_topology']
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    hub = FlightHub.__new__(FlightHub)
    hub.service = SimpleNamespace(
        executor=SimpleNamespace(execute=execute),
        settings=SimpleNamespace(service_url='http://testserver'),
    )
    hub.world = FlightWorld(collectibles=CollectibleField(seed='0' * 32))
    hub.world.set_gravity_wells([{'id': 'u_root', 'position': [0, 0, 0], 'radius': 5.4}])
    ship, _ = hub.world.join({'subject_id': 'u_public', 'handle': 'public', 'guest': False})
    hub.geometry_lock = asyncio.Lock()
    hub.geometry_initialized = True
    hub.next_geometry_check = 0.0
    hub.closed = False
    hub.next_auth_check = 0.0
    hub.runner = None
    hub.addresses = {'127.0.0.1': 1}
    closed = []

    async def close(code):
        closed.append(code)

    peer = Peer('peer', SimpleNamespace(close=close), '127.0.0.1', ship_id=ship.id)
    hub.peers = {peer.id: peer}
    return hub, started, cancelled, closed


@pytest.mark.asyncio
@pytest.mark.parametrize('wait_for_lock', [False, True])
async def test_geometry_budget_includes_executor_and_lock_wait_and_clears_old_field(
    monkeypatch, wait_for_lock
):
    hub, started, cancelled, _ = slow_room(monkeypatch)
    if wait_for_lock:
        await hub.geometry_lock.acquire()
    began = time.monotonic()
    with pytest.raises(Failure) as failure:
        await hub._refresh_geometry()
    assert failure.value.code == 'server_busy'
    assert time.monotonic() - began < 0.2
    assert started.is_set() is not wait_for_lock
    assert cancelled.is_set() is not wait_for_lock
    assert hub.world.gravity_descriptor() == {'version': 1, 'wells': []}
    assert hub.world.collectibles.seed != '0' * 32 and hub.world.collectibles.revision == 0
    assert not hub.geometry_initialized and hub.next_geometry_check == 0
    if wait_for_lock:
        hub.geometry_lock.release()
    assert not hub.geometry_lock.locked()


@pytest.mark.asyncio
async def test_external_geometry_cancellation_releases_read_and_lock(monkeypatch):
    hub, started, cancelled, _ = slow_room(monkeypatch)
    task = asyncio.create_task(hub._refresh_geometry())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and not hub.geometry_lock.locked()


@pytest.mark.asyncio
async def test_runner_geometry_timeout_closes_room_before_any_stale_snapshot(monkeypatch):
    hub, _, cancelled, closed = slow_room(monkeypatch)
    sent = []
    all_closing = asyncio.Event()

    async def close(code):
        closed.append(code)
        if len(closed) == 2:
            all_closing.set()
        await all_closing.wait()

    first = next(iter(hub.peers.values()))
    first.websocket = SimpleNamespace(close=close)
    other, _ = hub.world.join({'subject_id': 'u_other', 'handle': 'other', 'guest': False})
    hub.peers['other'] = Peer('other', SimpleNamespace(close=close), '127.0.0.1', ship_id=other.id)
    hub.addresses['127.0.0.1'] = 2

    async def validate(*, identities):
        assert identities

    hub._validate = validate
    hub._enqueue = lambda peer, packet: sent.append(packet)
    await asyncio.wait_for(hub._run(), 0.2)
    assert cancelled.is_set() and closed == [1013, 1013]
    assert not sent and not hub.peers and not hub.addresses
    assert hub.world.active_count == 0 and hub.runner is None
    assert not hub.world.gravity_wells


@pytest.mark.asyncio
async def test_synchronous_layout_over_budget_is_rejected_before_publishing(monkeypatch):
    hub, _, _, _ = slow_room(monkeypatch)
    moment = [100.0]
    monkeypatch.setattr(flight_space, 'time', SimpleNamespace(monotonic=lambda: moment[0]))

    async def execute(request):
        return SimpleNamespace(
            error=None, data={'star_topology': {'nodes': ['u_root'], 'edges': []}}
        )

    def layout(nodes, edges):
        moment[0] += 1
        return {'u_root': {'position': [0, 0, 0]}}

    published = []
    setter = hub.world.set_gravity_wells

    def record_publish(records):
        published.append(records)
        setter(records)

    hub.service.executor.execute = execute
    hub.world.set_gravity_wells = record_publish
    monkeypatch.setattr(flight_space, 'planet_layout', layout)
    hub.geometry_initialized = False
    with pytest.raises(Failure) as failure:
        await hub._refresh_geometry()
    assert failure.value.code == 'server_busy'
    assert published == [[]], 'Only the fail-closed clear may publish after the deadline'
    assert not hub.world.gravity_wells and not hub.geometry_initialized
    assert hub.next_geometry_check == 0 and not hub.geometry_lock.locked()
