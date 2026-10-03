"""Signed TCP admission, real WS controls and generated events reach verified TLS.

The only network substitution maps a public webhook DNS name to this test's
loopback TLS listener. No enqueue, physics result or captured authority is faked.
"""

import asyncio
import json
import secrets
import socket
from datetime import timedelta

import aiohttp
import pytest
from aiohttp import web
from test_flight_websocket import controls, flight_server, oauth, player, receive_until, snapshot
from test_game_webhook_delivery import tls_contexts

from msg.core.codec import b64, loads, wire
from msg.core.errors import Failure
from msg.core.requests import request_for
from msg.workers.effects import EffectWorker
from msg.workers.webhook import PublicResolver, WebhookSender, validate_endpoint, verify_delivery

__all__ = ['flight_server', 'oauth']


@pytest.mark.asyncio
async def test_signed_bot_ws_events_reach_hmac_verified_tls_receiver(
    flight_server, oauth, tmp_path, monkeypatch
):
    app, key, subject, _ = oauth
    flight = flight_server
    secret = secrets.token_bytes(32)
    server_tls, client_tls = tls_contexts(tmp_path)
    received, delivery_ids = [], set()

    async def receive(request):
        body = await request.read()
        try:
            delivery_id = verify_delivery(dict(request.headers), body, secret, now=app.clock())
        except Failure:
            return web.Response(status=400)
        if delivery_id not in delivery_ids:
            delivery_ids.add(delivery_id)
            received.append(loads(body))
        return web.Response(status=204)

    receiver = web.Application(client_max_size=4096)
    receiver.router.add_post('/hook', receive)
    runner = web.AppRunner(receiver, access_log=None)
    await runner.setup()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(16)
    listener.setblocking(False)
    receiver_port = listener.getsockname()[1]
    await web.SockSite(runner, listener, ssl_context=server_tls).start()

    async def fixture_dns(self, host, port=0, family=socket.AF_INET):
        assert host == 'hooks.example.org' and port == 443
        return [
            {
                'hostname': host,
                'host': '127.0.0.1',
                'port': receiver_port,
                'family': socket.AF_INET,
                'proto': socket.IPPROTO_TCP,
                'flags': 0,
            }
        ]

    real_connector = aiohttp.TCPConnector

    def fixture_connector(*args, **kwargs):
        assert isinstance(kwargs['resolver'], PublicResolver)
        return real_connector(*args, **kwargs, ssl=client_tls)

    monkeypatch.setattr(PublicResolver, 'resolve', fixture_dns)
    monkeypatch.setattr('msg.workers.webhook.aiohttp.TCPConnector', fixture_connector)
    assert validate_endpoint('https://hooks.example.org/hook') == ('hooks.example.org', 443)

    async def post(operation, arguments):
        packet = request_for(
            operation,
            arguments,
            app.settings.service_url,
            signer=key,
            subject=subject,
            expires_at=app.clock() + timedelta(seconds=120),
        )
        base = flight.url.replace('ws://', 'http://').removesuffix('/_flight')
        async with flight.client.post(
            base + '/-/p/' + operation, headers={'Host': 'testserver'}, json=wire(packet)
        ) as response:
            result = await response.json()
            assert response.status == 200 and result['status'] == 'ok', result
            return result['data']

    try:
        await post(
            'communication.webhook_set',
            {'url': 'https://hooks.example.org/hook', 'secret': b64(secret)},
        )
        await post(
            'communication.game_webhook_subscribe', {'events': ['game.joined', 'game.region']}
        )
        nonce = secrets.token_urlsafe(32)
        ticket = await post('communication.game_join_ticket', {'nonce': nonce})
        assert 'nonce' not in ticket
        peer = await flight.connect()
        await peer.send_json({
            'v': 1,
            'type': 'join',
            'ticket': {'id': ticket['ticket_id'], 'nonce': nonce},
        })
        hello = await receive_until(peer, lambda body: body.get('type') == 'hello')
        ship_id = hello['self']['id']
        assert hello['self']['subject_id'] == subject and not hello['self']['guest']
        initial = await snapshot(peer)
        before = player(initial, ship_id)['position']
        await peer.send_json(controls(1, throttle=1, yaw=0.25, pitch=0.1))
        moved = await snapshot(
            peer,
            lambda body: (
                player(body, ship_id)['ack_seq'] == 1
                and player(body, ship_id)['position'] != before
            ),
        )
        region = (player(moved, ship_id)['region'] + 1) % 19
        await peer.send_json({'v': 1, 'type': 'region', 'region': region})
        await snapshot(peer, lambda body: player(body, ship_id)['region'] == region)
        runtime = app._game_runtime
        async with asyncio.timeout(5):
            await runtime.queue.join()
        assert runtime.dropped == 0

        worker = EffectWorker(app, webhook_sender=WebhookSender())
        async with asyncio.timeout(10):
            while len(received) < 2:
                if not await worker.run_once():
                    await asyncio.sleep(0.02)
        assert {body['type'] for body in received} == {'game.joined', 'game.region'}
        for body in received:
            assert body['subject_id'] == subject
            assert body['event']['ship_id'] == ship_id
            assert set(body['event']) <= {
                'id',
                'type',
                'ship_id',
                'time_ms',
                'region',
                'score',
                'collected',
                'hp',
            }
            assert body['type'] == body['event']['type']
            async with app.metadata.transaction(write=False) as tx:
                job = await tx.job(body['delivery_id'])
                assert job.state == 'done' and job.attempts == 1
                assert job.arguments['event'] == body['event']
        changed = next(body for body in received if body['type'] == 'game.region')
        assert changed['event']['region'] == region
        assert changed['event']['hp'] == 100 and type(changed['event']['hp']) is int
        encoded = json.dumps(received)
        assert all(
            name not in encoded for name in ('position', 'home', 'target_id', 'credential_id')
        )
        assert len(delivery_ids) == 2
        await peer.close()
    finally:
        await runner.cleanup()
