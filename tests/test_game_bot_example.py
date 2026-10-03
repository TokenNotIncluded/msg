"""Run the executable example's guest path against a real TCP game server."""

import asyncio
import importlib.util
import math
import socket
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import pytest
from test_flight_websocket import active_count, flight_server, oauth

__all__ = ['flight_server', 'oauth']


@pytest.mark.asyncio
async def test_example_guest_pilot_plays_without_loading_signed_identity(
    flight_server, monkeypatch
):
    path = Path(__file__).parents[1] / 'examples' / 'game_bot.py'
    spec = importlib.util.spec_from_file_location('game_bot_example', path)
    bot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot)
    args = bot.parser().parse_args([
        '--server',
        'http://testserver',
        'pilot',
        '--guest',
        '--seconds',
        '3',
        '--throttle',
        '0.5',
        '--yaw',
        '0.1',
    ])

    def forbidden_identity(*args, **kwargs):
        pytest.fail('Guest example attempted to load a signed client or identity')

    monkeypatch.setattr(bot, 'signed_client', forbidden_identity)
    port = urlsplit(flight_server.url).port

    async def fixture_dns(self, host, port=0, family=socket.AF_INET):
        assert host == 'testserver' and port == 80
        return [
            {
                'hostname': host,
                'host': '127.0.0.1',
                'port': urlsplit(flight_server.url).port,
                'family': socket.AF_INET,
                'proto': socket.IPPROTO_TCP,
                'flags': 0,
            }
        ]

    assert port is not None
    monkeypatch.setattr(aiohttp.resolver.DefaultResolver, 'resolve', fixture_dns)
    observations = []

    async def observe_authoritative_controls():
        async with asyncio.timeout(5):
            while not flight_server.hub.world.active:
                await asyncio.sleep(0.01)
            ship_id = next(iter(flight_server.hub.world.active))
            while ship_id in flight_server.hub.world.active:
                ship = flight_server.hub.world.ships[ship_id]
                observations.append((
                    ship.id,
                    ship.guest,
                    ship.subject_id,
                    ship.ack_seq,
                    tuple(ship.position),
                ))
                await asyncio.sleep(0.02)

    observer = asyncio.create_task(observe_authoritative_controls())
    try:
        await bot.pilot(args)
        await active_count(flight_server, 0)
        await observer
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
    assert len({item[0] for item in observations}) == 1
    assert all(guest and subject is None for _, guest, subject, _, _ in observations)
    assert max(item[3] for item in observations) >= 5
    first = observations[0][4]
    assert max(math.dist(first, item[4]) for item in observations) > 0.1
    assert not hasattr(flight_server.app, '_game_runtime')
