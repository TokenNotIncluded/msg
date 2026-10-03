"""Movement coalescing preserves bounded combat intents before a fenced pump."""

import asyncio
import math
from types import SimpleNamespace

import pytest

from msg.transports.flight_simulation import FlightWorld
from msg.transports.flight_space import MAX_PENDING_COMMANDS, FlightHub, Peer


def packet(seq, **changes):
    return {
        'v': 1,
        'type': 'input',
        'seq': seq,
        'throttle': 0,
        'strafe': 0,
        'lift': 0,
        'yaw': 0,
        'pitch': 0,
        'actions': [],
        **changes,
    }


def playground():
    hub = FlightHub.__new__(FlightHub)
    hub.world = FlightWorld(clock=lambda: 100.0)
    ship, _ = hub.world.join({
        'subject_id': 'u_pilot',
        'handle': 'pilot',
        'guest': False,
        'spawn_position': [0, 0, 0],
        'spawn_region': 0,
    })
    return hub, Peer('peer', None, '127.0.0.1', ship_id=ship.id), ship


def test_held_actions_keep_original_aim_and_latest_control_without_advancing_world():
    hub, peer, ship = playground()
    target, _ = hub.world.join({
        'subject_id': 'u_target',
        'handle': 'target',
        'guest': False,
        'spawn_position': [0, 0, -20],
        'spawn_region': 1,
    })
    hub._queue_controls(peer, packet(0, actions=['laser']))
    for sequence in range(1, 200):
        hub._queue_controls(peer, packet(sequence, yaw=math.pi / 2, actions=['laser']))

    assert len(peer.pending) == 1
    assert peer.latest_control[1]['seq'] == 199
    assert ship.ack_seq == -1 and ship.fuel == 100 and target.hp == 100
    hub._drain_inputs(peer)
    assert target.hp == 85  # The initial aim hits; the latest aim would miss.
    assert ship.ack_seq == 199 and ship.yaw == math.pi / 2
    assert ship.fuel == 99
    assert not peer.pending and peer.latest_control is None
    assert (
        len([event for event in hub.world.snapshot(ship.id)['events'] if event['type'] == 'laser'])
        == 1
    )


def test_discrete_skills_are_preserved_once_across_movement_coalescing():
    hub, peer, ship = playground()
    hub._queue_controls(peer, packet(0, actions=['laser']))
    hub._queue_controls(peer, packet(1, yaw=0.4, actions=['dash']))
    hub._queue_controls(peer, packet(2, yaw=0.6, actions=['laser', 'shield', 'dash']))
    hub._queue_controls(peer, packet(3, yaw=0.8))
    assert [command['actions'] for _, command in peer.pending] == [['laser'], ['dash'], ['shield']]
    hub._drain_inputs(peer)
    assert ship.ack_seq == 3 and ship.yaw == 0.8
    assert ship.fuel == 69
    events = hub.world.snapshot(ship.id)['events']
    assert all(
        sum(event['type'] == action for event in events) == 1
        for action in ('laser', 'dash', 'shield')
    )


def test_region_barrier_preserves_control_order_and_deduplicates_held_laser():
    hub, peer, ship = playground()
    hub._queue_controls(peer, packet(0, actions=['laser']))
    hub._queue_controls(peer, packet(1, throttle=1))
    hub._queue_controls(peer, {'v': 1, 'type': 'region', 'region': 1})
    hub._queue_controls(peer, packet(2, actions=['laser']))
    hub._drain_inputs(peer)
    events = hub.world.snapshot(ship.id)['events']
    laser = next(event for event in events if event['type'] == 'laser')
    assert laser['position'] == [0, 7, 0]
    assert sum(event['type'] == 'laser' for event in events) == 1
    assert ship.region == 1 and ship.ack_seq == 2


def test_full_discrete_queue_rejects_commands_but_retains_current_movement():
    hub, peer, ship = playground()
    for region in range(1, MAX_PENDING_COMMANDS + 1):
        hub._queue_controls(peer, {'v': 1, 'type': 'region', 'region': region})
    hub._queue_controls(peer, packet(0, actions=['dash']))
    hub._queue_controls(peer, packet(1, yaw=0.7))
    assert len(peer.pending) == MAX_PENDING_COMMANDS
    assert peer.queue.get_nowait()['code'] == 'input_busy'
    assert peer.latest_control[1]['seq'] == 1
    assert ship.ack_seq == -1
    hub._drain_inputs(peer)
    assert ship.ack_seq == 1 and ship.yaw == 0.7
    assert ship.dash_ready_ms == 0


def test_region_after_movement_does_not_restore_older_thrust():
    hub, peer, ship = playground()
    hub._queue_controls(peer, packet(0, throttle=1))
    hub._queue_controls(peer, {'v': 1, 'type': 'region', 'region': 1})
    hub._drain_inputs(peer)
    relocated = list(ship.position)
    hub.world.clock = lambda: 100.0 + 1 / 15
    hub.world.step()
    assert ship.ack_seq == 0 and ship.position == relocated
    assert ship.velocity == [0, 0, 0]


@pytest.mark.asyncio
async def test_a_fenced_tick_uses_waiting_input_before_advancing_physics():
    hub, peer, ship = playground()
    hub.peers = {peer.id: peer}
    hub.addresses = {peer.address: 1}
    hub.service = SimpleNamespace()
    hub.closed = False
    hub.next_auth_check = 0.0
    hub.next_geometry_check = 0.0
    hub.runner = None
    clock = [100.0]
    hub.world.clock = lambda: clock[0]
    initial = list(ship.position)
    snapshots = []

    async def gate(*, identities):
        assert ship.ack_seq == -1 and ship.position == initial
        clock[0] += 1 / 15

    async def geometry():
        pass

    def deliver(_, body):
        snapshots.append(body)
        hub.closed = True

    hub._validate = gate
    hub._refresh_geometry = geometry
    hub._enqueue = deliver
    hub._queue_controls(peer, packet(0, throttle=1))
    writer = asyncio.create_task(asyncio.Event().wait())
    peer.writer = writer
    try:
        await hub._run()
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)

    assert len(snapshots) == 1
    own = snapshots[0]['players'][0]
    assert own['ack_seq'] == 0
    assert own['position'][2] < initial[2]
    assert own['velocity'][2] < 0
