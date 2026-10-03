"""Real signed HTTP/PG effects and a certificate-verified local HTTPS receiver.

Only the test's network resolver maps a syntactically public HTTPS:443 endpoint
to its isolated loopback port. Production endpoint, DNS and TLS policy is intact.
Game WS/control acceptance belongs to the runtime integration's separate test.
"""

import asyncio
import json
import socket
import ssl
import subprocess
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import aiohttp
import httpx
import pytest
import uvicorn
from aiohttp import web
from test_service import NOW, register

from msg.core.codec import b64, canonical, decode, loads
from msg.core.errors import Failure
from msg.core.models import JsonMap
from msg.core.requests import request_for
from msg.transports.http import create_app
from msg.workers.effects import EffectWorker
from msg.workers.game_webhook import EVENT_TYPES, OPERATION, bounded_game_event, enqueue_game_event
from msg.workers.webhook import PublicResolver, WebhookSender, validate_endpoint, verify_delivery


def event(kind='game.joined', **changes):
    return {
        'id': 'ge_' + uuid4().hex,
        'type': kind,
        'ship_id': 'ship_' + '1' * 24,
        'time_ms': int(NOW.timestamp() * 1000),
        'region': 2,
        **changes,
    }


@pytest.mark.parametrize(
    'changes',
    [
        {'position': [1, 2, 3]},
        {'home': 'private'},
        {'target_id': 'ship_' + '2' * 24},
        {'credential_id': 'private'},
        {'handle': 'other-player'},
        {'subject_id': 'u_other'},
        {'type': 'game.snapshot'},
        {'region': True},
        {'region': 19},
        {'score': -1},
        {'collected': 2**53},
        {'hp': float('nan')},
        {'hp': float('inf')},
        {'time_ms': True},
        {'ship_id': 'u_other'},
    ],
)
def test_game_event_closed_vocabulary_rejects_private_and_invalid_values(changes):
    with pytest.raises(Failure, match='invalid_game_event'):
        bounded_game_event(event(**changes))


def test_game_event_is_copied_and_keeps_only_small_discrete_owner_facts():
    original = event('game.hit', hp=40.5, score=4, collected=2)
    copied = bounded_game_event(original)
    original['hp'] = 0
    assert copied['hp'] == 40.5
    assert len(canonical(copied)) < 2048


def test_game_event_accepts_the_storage_codecs_frozen_json_map():
    original = event('game.collect', score=1, collected=1)
    stored = decode(JsonMap, original)
    assert bounded_game_event(stored) == original


@asynccontextmanager
async def signed_http_server(app):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    listener.setblocking(False)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(app),
            lifespan='off',
            loop='asyncio',
            ws='websockets-sansio',
            access_log=False,
            log_level='error',
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    pytest.fail('HTTP fixture stopped before startup')
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(
            base_url=f'http://127.0.0.1:{listener.getsockname()[1]}',
            headers={'Host': 'testserver'},
        ) as client:
            yield client
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(5):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            listener.close()


def tls_contexts(tmp_path):
    certificate, key = tmp_path / 'receiver-cert.pem', tmp_path / 'receiver-key.pem'
    subprocess.run(
        [
            'openssl',
            'req',
            '-x509',
            '-newkey',
            'rsa:2048',
            '-nodes',
            '-keyout',
            str(key),
            '-out',
            str(certificate),
            '-days',
            '1',
            '-subj',
            '/CN=hooks.example.org',
            '-addext',
            'subjectAltName=DNS:hooks.example.org,IP:127.0.0.1',
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate, key)
    client = ssl.create_default_context(cafile=str(certificate))
    assert client.check_hostname and client.verify_mode == ssl.CERT_REQUIRED
    return server, client


@pytest.mark.asyncio
async def test_game_webhook_signed_http_pg_tls_delivery_and_current_authority(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    key, subject, certificate = await register(app, 'game-hook-owner')
    other_key, other_subject, other_certificate = await register(app, 'game-hook-other')
    secret = b'x' * 32  # Disposable fixture secret, not a production credential.
    server_tls, client_tls = tls_contexts(tmp_path)
    received, attempts, seen = [], [], set()
    refuse_once = True

    async def receive(request):
        nonlocal refuse_once
        body = await request.read()
        headers = dict(request.headers)
        try:
            delivery = verify_delivery(headers, body, secret, now=app.clock())
        except Failure:
            return web.Response(status=400)
        attempts.append((headers, body))
        if refuse_once:
            refuse_once = False
            return web.Response(status=503)
        if delivery not in seen:
            seen.add(delivery)
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
    site = web.SockSite(runner, listener, ssl_context=server_tls)
    await site.start()

    async def local_fixture_dns(self, host, port=0, family=socket.AF_INET):
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

    def verified_fixture_connector(*args, **kwargs):
        assert isinstance(kwargs['resolver'], PublicResolver)
        return real_connector(*args, **kwargs, ssl=client_tls)

    monkeypatch.setattr(PublicResolver, 'resolve', local_fixture_dns)
    monkeypatch.setattr('msg.workers.webhook.aiohttp.TCPConnector', verified_fixture_connector)
    assert validate_endpoint('https://hooks.example.org/hook') == ('hooks.example.org', 443)

    async def job(job_id):
        async with app.metadata.transaction(write=False) as tx:
            return await tx.job(job_id)

    async def assert_job_state(job_id, expected):
        async with app.metadata.transaction(write=False) as tx:
            value = await tx.job(job_id)
            status = tx.setting('job_status:' + job_id)
        assert value.state == expected, {'state': value.state, 'status': status}

    try:
        async with signed_http_server(app) as http:

            async def post(operation, arguments, *, signer=key, owner=subject, cert=certificate):
                packet = request_for(
                    operation,
                    arguments,
                    app.settings.service_url,
                    signer=signer,
                    subject=owner,
                    certificates=(cert,),
                    expires_at=app.clock() + timedelta(seconds=120),
                )
                response = await http.post('/-/p/' + operation, content=canonical(packet))
                assert response.status_code == 200, response.status_code
                return response.json()

            endpoint = await post(
                'communication.webhook_set',
                {'url': 'https://hooks.example.org/hook', 'secret': b64(secret)},
            )
            assert endpoint['status'] == 'ok'
            subscribed = await post(OPERATION, {'events': sorted(EVENT_TYPES)})
            assert subscribed['status'] == 'ok', subscribed

            async def generations():
                async with app.metadata.transaction(write=False) as tx:
                    record = tx.setting('game_webhook:' + subject)
                    return record['generation'], record['endpoint_generation']

            first_event = event()
            first = await enqueue_game_event(app, subject, first_event, *await generations())
            assert (
                await enqueue_game_event(app, subject, first_event, *await generations()) == first
            )
            with pytest.raises(Failure, match='game_event_conflict'):
                await enqueue_game_event(
                    app, subject, {**first_event, 'region': 3}, *await generations()
                )
            first_event['region'] = 8
            assert (await job(first)).arguments['event']['region'] == 2
            worker = EffectWorker(app, webhook_sender=WebhookSender())
            assert await worker.run_once()
            await assert_job_state(first, 'pending')
            assert (await job(first)).attempts == 1
            app.clock = lambda: NOW + timedelta(seconds=31)
            assert await worker.run_once()
            await assert_job_state(first, 'done')
            assert len(attempts) == 2 and len(received) == 1
            assert attempts[0][0]['Msg-Delivery-Id'] == attempts[1][0]['Msg-Delivery-Id'] == first
            assert set(received[0]) == {
                'event_id',
                'delivery_id',
                'timestamp',
                'subject_id',
                'type',
                'event',
            }
            assert received[0]['subject_id'] == subject
            assert other_subject not in json.dumps(received)
            assert 'position' not in json.dumps(received)
            replay_headers, replay_body = attempts[-1]
            replay_headers = {
                name: value
                for name, value in replay_headers.items()
                if name.lower()
                in {'msg-event-id', 'msg-delivery-id', 'msg-timestamp', 'msg-signature'}
            }
            async with httpx.AsyncClient(verify=client_tls) as receiver_http:
                response = await receiver_http.post(
                    f'https://127.0.0.1:{receiver_port}/hook',
                    headers=replay_headers,
                    content=replay_body,
                )
                assert response.status_code == 204 and len(received) == 1
                tampered = await receiver_http.post(
                    f'https://127.0.0.1:{receiver_port}/hook',
                    headers=replay_headers,
                    content=replay_body + b' ',
                )
                assert tampered.status_code == 400 and len(received) == 1

            # Restore current endpoint/subscribe between each stop condition.
            async def enable():
                setting = await post(
                    'communication.webhook_set',
                    {'url': 'https://hooks.example.org/hook', 'secret': b64(secret)},
                )
                assert setting['status'] == 'ok'
                setting = await post(OPERATION, {'events': sorted(EVENT_TYPES)})
                assert setting['status'] == 'ok'

            pending = await enqueue_game_event(
                app, subject, event('game.left'), *await generations()
            )
            # Capacity is shared with the pre-existing webhook job kind.
            with monkeypatch.context() as limited:
                limited.setattr('msg.storage.capacity.MAX_QUEUED_WEBHOOKS', 1)
                with pytest.raises(Failure, match='storage_capacity_exceeded'):
                    await enqueue_game_event(
                        app, subject, event('game.collect'), *await generations()
                    )
            rotated = await post(
                'communication.webhook_set',
                {'url': 'https://hooks.example.org/hook', 'secret': b64(secret)},
            )
            assert rotated['status'] == 'ok'
            assert await worker.run_once()
            assert (await job(pending)).state == 'failed' and len(received) == 1
            async with app.metadata.transaction(write=False) as tx:
                assert tx.setting('job_status:' + pending)['code'] == 'webhook_disabled'
            subscribed = await post(OPERATION, {'events': sorted(EVENT_TYPES)})
            assert subscribed['status'] == 'ok'
            pending = await enqueue_game_event(
                app, subject, event('game.region'), *await generations()
            )
            unsubscribed = await post('communication.game_webhook_unsubscribe', {})
            assert unsubscribed['status'] == 'ok'
            assert await worker.run_once()
            assert (await job(pending)).state == 'failed' and len(received) == 1
            await enable()

            # A foreign signing principal cannot be substituted into an owner's record.
            await post(
                'communication.webhook_set',
                {'url': 'https://hooks.example.org/hook', 'secret': b64(secret)},
                signer=other_key,
                owner=other_subject,
                cert=other_certificate,
            )
            other_subscribed = await post(
                OPERATION,
                {'events': sorted(EVENT_TYPES)},
                signer=other_key,
                owner=other_subject,
                cert=other_certificate,
            )
            assert other_subscribed['status'] == 'ok'
            async with app.metadata.transaction(write=True) as tx:
                original = tx.setting('game_webhook:' + subject)
                foreign = tx.setting('game_webhook:' + other_subject)
                tx.set_setting(
                    'game_webhook:' + subject, {**original, 'principal': foreign['principal']}
                )
            with pytest.raises(Failure, match='game_webhook_owner'):
                await enqueue_game_event(app, subject, event('game.collect'), *await generations())
            async with app.metadata.transaction(write=True) as tx:
                tx.set_setting('game_webhook:' + subject, original)

            # Stored pre-upgrade operation snapshots remain denied over real HTTP.
            async with app.metadata.transaction(write=True) as tx:
                credential = await tx.credential(key.key_id)
                identity = await tx.subject(subject)
                ceiling = tuple(
                    replace(grant, operations=grant.operations - {OPERATION + '@1'})
                    for grant in credential.ceiling
                )
                await tx.save_credential(
                    replace(credential, ceiling=ceiling), identity.auth_version
                )
            denied = await post(OPERATION, {'events': ['game.joined']})
            assert denied['status'] == 'error' and denied['error']['code'] == 'credential_ceiling'
            with pytest.raises(Failure, match='credential_ceiling'):
                await enqueue_game_event(app, subject, event(), *await generations())
            async with app.metadata.transaction(write=True) as tx:
                await tx.save_credential(credential, identity.auth_version)

            pending = await enqueue_game_event(
                app, subject, event('game.hit'), *await generations()
            )
            async with app.metadata.transaction(write=True) as tx:
                await tx.save_credential(
                    replace(credential, revoked_at=app.clock()), identity.auth_version
                )
            assert await worker.run_once()
            assert (await job(pending)).state == 'failed' and len(received) == 1
            async with app.metadata.transaction(write=False) as tx:
                assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='webhook'")[0] == 4
                assert tx.one(
                    'SELECT ciphertext FROM webhook_endpoints WHERE subject=?', (subject,)
                )
            print({
                'signed_http': 'real TCP',
                'webhook': 'verified TLS TCP',
                'deliveries': 1,
                'retry_attempts': 2,
                'dedupe_jobs': 4,
                'game_websocket': 'separate integration',
            })
    finally:
        await runner.cleanup()
