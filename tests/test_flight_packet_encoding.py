"""Unsigned flight encoding retains the existing bytes and transport bounds."""

import asyncio
from types import SimpleNamespace

import pytest

from msg.core.codec import canonical
from msg.core.errors import Failure
from msg.transports.flight_collectibles import CollectibleField
from msg.transports.flight_simulation import FlightWorld
from msg.transports.flight_space import MAX_SNAPSHOT_BYTES, FlightHub


async def send_packet(value):
    packets = []

    async def send_text(text):
        packets.append(text)

    hub = FlightHub.__new__(FlightHub)
    await hub._send(SimpleNamespace(send_text=send_text), value)
    return packets


async def test_full_unsigned_world_packet_matches_existing_canonical_bytes():
    world = FlightWorld(collectibles=CollectibleField(seed='0' * 32))
    world.set_gravity_wells([{'id': 'u_root', 'position': [0, 0, 0], 'radius': 5.4}])
    ship, _ = world.join({'subject_id': 'u_fixture', 'handle': '飞行员 🚀', 'guest': False})
    other, _ = world.join({'subject_id': 'u_other', 'handle': '第二位', 'guest': False})
    world._event('laser', other, world.clock(), end=[1.25, 2.5, 3.75])
    packet = world.snapshot(ship.id)
    packets = await send_packet(packet)
    assert len(packets) == 1
    assert packets[0].encode('utf-8') == canonical(packet)


async def test_encoded_utf8_limit_accepts_exact_boundary_and_rejects_one_byte_over():
    packet = {'message': '核' * ((MAX_SNAPSHOT_BYTES - 14) // 3)}
    remaining = MAX_SNAPSHOT_BYTES - len(canonical(packet))
    packet['message'] += 'a' * remaining
    assert len(canonical(packet)) == MAX_SNAPSHOT_BYTES
    assert len((await send_packet(packet))[0].encode('utf-8')) == MAX_SNAPSHOT_BYTES
    packet['message'] += 'a'
    with pytest.raises(Failure) as failure:
        await send_packet(packet)
    assert failure.value.code == 'response_too_large'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), b'unsupported', '\ud800'])
async def test_invalid_server_packet_fails_before_any_socket_write(value):
    with pytest.raises(Failure) as failure:
        await send_packet({'value': value})
    assert failure.value.code == 'invalid_json_value'


async def test_slow_socket_still_has_the_existing_send_deadline(monkeypatch):
    from msg.transports import flight_space

    entered = asyncio.Event()

    async def send_text(text):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(flight_space, 'SEND_TIMEOUT', 0.01)
    hub = FlightHub.__new__(FlightHub)
    with pytest.raises(TimeoutError):
        await hub._send(SimpleNamespace(send_text=send_text), {'v': 1, 'type': 'snapshot'})
    assert entered.is_set()
