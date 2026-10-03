"""Process-local admission/queue bounds; signed authority uses TCP acceptance."""

import asyncio
import secrets
from types import SimpleNamespace

import pytest

from msg.core.codec import b64
from msg.core.errors import Failure
from msg.core.models import Principal
from msg.transports.game_api import (
    MAX_EVENT_QUEUE,
    MAX_OWNER_TICKETS,
    GameRuntime,
    capture_game_events,
    close_game_api,
    emit_game_event,
)


def principal():
    return Principal(
        actor='u_owner',
        subject='u_owner',
        credential_id='k_owner',
        method='signature',
        certificates=(),
        ceiling=(),
    )


def test_admission_nonce_is_not_a_persistent_result_and_consumption_is_once():
    now = [10.0]
    runtime = GameRuntime(SimpleNamespace(), clock=lambda: now[0])
    owner, nonce = principal(), b64(secrets.token_bytes(32))
    result = runtime.issue(owner, nonce)
    assert nonce not in str(result) and nonce not in str(runtime.tickets)
    packet = {'id': result['ticket_id'], 'nonce': nonce}
    with pytest.raises(Failure, match='invalid_grant'):
        runtime.take(dict(packet, nonce=b64(secrets.token_bytes(32))))
    assert runtime.take(packet) == owner
    with pytest.raises(Failure, match='invalid_grant'):
        runtime.take(packet)
    expired = runtime.issue(owner, nonce)
    now[0] += 30
    with pytest.raises(Failure, match='invalid_grant'):
        runtime.take({'id': expired['ticket_id'], 'nonce': nonce})


def test_live_admissions_are_bounded_per_owner_and_expired_records_reclaimed():
    now = [10.0]
    runtime = GameRuntime(SimpleNamespace(), clock=lambda: now[0])
    for _ in range(MAX_OWNER_TICKETS):
        runtime.issue(principal(), b64(secrets.token_bytes(32)))
    with pytest.raises(Failure, match='server_busy'):
        runtime.issue(principal(), b64(secrets.token_bytes(32)))
    now[0] += 30
    runtime.issue(principal(), b64(secrets.token_bytes(32)))
    assert len(runtime.tickets) == 1


async def test_event_ingress_projects_own_flat_fields_and_drops_when_full():
    service = SimpleNamespace()
    runtime = service._game_runtime = GameRuntime(service)
    runtime.configure(
        'u_owner',
        {'enabled': True, 'events': ['game.joined'], 'generation': 7, 'endpoint_generation': 2},
    )
    # Keep the consumer away from persistence while exercising producer bounds.
    runtime.consumer = asyncio.create_task(asyncio.Event().wait())
    payload = {
        'ship_id': 'ship_own',
        'region': 4,
        'position': [1, 2, 3],
        'home_position': [9, 8, 7],
        'target_id': 'ship_other',
        'handle': '@private',
    }
    assert not emit_game_event(service, 'u_other', 'game.joined', payload)
    for _ in range(MAX_EVENT_QUEUE):
        assert emit_game_event(service, 'u_owner', 'game.joined', payload)
    assert not emit_game_event(service, 'u_owner', 'game.joined', payload)
    assert runtime.queue.qsize() == MAX_EVENT_QUEUE and runtime.dropped == 1
    subject, event, generation, endpoint = runtime.queue.get_nowait()
    runtime.queue.task_done()
    assert (subject, generation, endpoint) == ('u_owner', 7, 2)
    assert set(event) == {'id', 'type', 'ship_id', 'time_ms', 'region'}
    assert '@private' not in str(event) and 'ship_other' not in str(event)
    await close_game_api(service)
    await close_game_api(service)
    assert runtime.closed and runtime.queue.empty() and runtime.consumer is None


def test_absent_runtime_and_guest_tick_hooks_are_noops():
    service = SimpleNamespace()
    hub = SimpleNamespace()
    assert not emit_game_event(service, None, 'game.joined', {})
    assert not emit_game_event(service, 'u_owner', 'game.joined', {})
    assert capture_game_events(service, hub) is None
    assert not hasattr(service, '_game_runtime')
