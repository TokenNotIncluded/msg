#!/usr/bin/env python3
"""Finite signed game pilot and optional local webhook receiver. See docs/ai-game.md."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import secrets
import sqlite3
import stat
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

EVENTS = ('game.joined', 'game.left', 'game.region', 'game.collect', 'game.hit')


def seconds(value):
    number = float(value)
    if not math.isfinite(number) or not 1 <= number <= 300:
        raise argparse.ArgumentTypeError('seconds must be between 1 and 300')
    return number


def control(value):
    number = float(value)
    if not math.isfinite(number) or not -1 <= number <= 1:
        raise argparse.ArgumentTypeError('control must be between -1 and 1')
    return number


def angle(value):
    number = float(value)
    if not math.isfinite(number) or not -math.pi <= number <= math.pi:
        raise argparse.ArgumentTypeError('angle must be finite radians between -pi and pi')
    return number


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument('--server', help='Existing configured service origin')
    root.add_argument('--account', help='Existing local account, for example lightjunction')
    root.add_argument('--profile', help='Existing MSG client profile')
    root.add_argument('--config-dir', type=Path, help='Existing isolated client directory')
    root.add_argument(
        '--key', type=Path, help='Existing owned 0600 raw 32-byte Ed25519 private key'
    )
    root.add_argument('--certificate', action='append', help='Certificate matching the chosen key')
    commands = root.add_subparsers(dest='command', required=True)
    pilot = commands.add_parser('pilot', help='Sign a short-lived ticket and fly at most 10 Hz')
    pilot.add_argument(
        '--guest', action='store_true', help='Public guest; no identity or key reads'
    )
    pilot.add_argument(
        '--seconds', type=seconds, default=30, help='Total budget, including join, 1..300'
    )
    for name in ('throttle', 'strafe', 'lift'):
        pilot.add_argument('--' + name, type=control, default=0.25 if name == 'throttle' else 0)
    pilot.add_argument('--yaw', type=angle, default=0, help='Desired yaw in radians')
    pilot.add_argument(
        '--pitch', type=angle, default=0, help='Desired pitch in radians, -pi/2..pi/2'
    )
    setting = commands.add_parser(
        'webhook-set', help='Explicitly rotate the account shared endpoint'
    )
    setting.add_argument('--url', required=True, help='Public HTTPS endpoint on port 443')
    setting.add_argument(
        '--secret-file', type=Path, required=True, help='Owned 0600 raw 32..64 bytes'
    )
    subscribe = commands.add_parser(
        'subscribe', help='Enable owner game events on the current endpoint'
    )
    subscribe.add_argument('--events', nargs='+', choices=EVENTS, default=list(EVENTS))
    commands.add_parser('unsubscribe', help='Disable this account game subscription')
    commands.add_parser('status', help='Read game subscription status')
    receive = commands.add_parser('receive', help='Local HMAC receiver behind your HTTPS proxy')
    receive.add_argument(
        '--secret-file', type=Path, required=True, help='Owned 0600 raw 32..64 bytes'
    )
    receive.add_argument(
        '--replay-db', type=Path, required=True, help='Ledger in an owned 0700 directory'
    )
    receive.add_argument('--host', choices=('127.0.0.1', '::1'), default='127.0.0.1')
    receive.add_argument('--port', type=int, default=8787)
    receive.add_argument('--seconds', type=seconds, default=300)
    return root


def secret_file(path):
    from msg.core.errors import require

    descriptor = os.open(path.expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and info.st_mode & 0o077 == 0,
            'unsafe_game_secret_file',
        )
        value = os.read(descriptor, 65)
        require(32 <= len(value) <= 64 and info.st_size == len(value), 'invalid_webhook_secret')
        return value
    finally:
        os.close(descriptor)


@asynccontextmanager
async def signed_client(args):
    from msg.client import ClientState, MsgClient, private_identity_key
    from msg.core.errors import require
    from msg.transports.client import HTTPTransport

    state = ClientState(
        args.config_dir, server=args.server, account=args.account, profile=args.profile
    )
    signer = private_identity_key(args.key.expanduser()) if args.key else state.signer
    require(
        signer is not None and state.subject is not None, 'configured_signing_identity_required'
    )
    certificates = tuple(args.certificate) if args.certificate else state.certificates
    transport = HTTPTransport(state.server)
    client = MsgClient(state, transport)
    try:

        async def call(operation, arguments):
            # Explicit signer bypasses both saved tokens and automatic OAuth refresh.
            return client.checked(
                await client.call(
                    operation,
                    arguments,
                    signer=signer,
                    certificates=certificates,
                    contract_version=1,
                )
            )

        yield state, call
    finally:
        await transport.close()


async def pilot_controls(websocket, hello, args):
    """Use an existing joined aiohttp WS; args supplies seconds and the five controls."""
    import aiohttp

    from msg.core.codec import loads
    from msg.core.errors import Failure, require

    require(hello.get('type') == 'hello' and hello.get('v') == 1, 'invalid_game_hello')
    require(1 <= args.seconds <= 300, 'invalid_game_duration')
    print(json.dumps({'joined': hello['self']['id'], 'region': hello['region']}), flush=True)

    async def drain():
        async for frame in websocket:
            if frame.type != aiohttp.WSMsgType.TEXT:
                raise Failure('game_socket_closed')
            value = loads(frame.data)
            require(value.get('type') == 'snapshot', 'invalid_game_snapshot')
            # A policy can inspect this authorized snapshot, then choose controls.
            # Keep poses and resource IDs out of outgoing control packets.
        raise Failure('game_socket_closed')

    reader = asyncio.create_task(drain())
    try:
        async with asyncio.timeout(args.seconds):
            sequence = 0
            while True:
                if reader.done():
                    await reader
                await websocket.send_json({
                    'v': 1,
                    'type': 'input',
                    'seq': sequence,
                    'throttle': args.throttle,
                    'strafe': args.strafe,
                    'lift': args.lift,
                    'yaw': args.yaw,
                    'pitch': args.pitch,
                    'actions': [],
                })
                sequence += 1
                await asyncio.sleep(0.1)
    except TimeoutError:
        pass
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@asynccontextmanager
async def pilot_identity(args):
    if args.guest:
        yield args.server or 'https://msg.lmm.best', None
    else:
        async with signed_client(args) as (state, call):
            yield state.server, call


async def pilot(args):
    import aiohttp

    from msg.core.codec import b64, loads
    from msg.core.errors import Failure, require

    require(-math.pi / 2 <= args.pitch <= math.pi / 2, 'invalid_pitch')
    joined = False
    try:
        async with asyncio.timeout(args.seconds), pilot_identity(args) as (server, call):
            packet = {'v': 1, 'type': 'join'}
            if call is not None:
                # Only this process keeps the nonce; receipts contain the public ticket ID.
                nonce = b64(secrets.token_bytes(32))
                ticket = (await call('communication.game_join_ticket', {'nonce': nonce})).data
                require(
                    isinstance(ticket.get('ticket_id'), str)
                    and ticket.get('websocket') == '/_flight'
                    and ticket.get('protocol') == 1,
                    'invalid_game_ticket',
                )
                packet['ticket'] = {'id': ticket['ticket_id'], 'nonce': nonce}
            parts = urlsplit(server)
            require(
                parts.scheme in {'http', 'https'}
                and bool(parts.netloc)
                and parts.username is None
                and parts.password is None
                and not parts.query
                and not parts.fragment,
                'invalid_game_server',
            )
            url = urlunsplit((
                'wss' if parts.scheme == 'https' else 'ws',
                parts.netloc,
                '/_flight',
                '',
                '',
            ))
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as http:
                async with http.ws_connect(
                    url,
                    headers={'Origin': server},
                    max_msg_size=192 * 1024,
                ) as websocket:
                    await websocket.send_json(packet)
                    frame = await websocket.receive()
                    require(frame.type == aiohttp.WSMsgType.TEXT, 'game_socket_closed')
                    hello = loads(frame.data)
                    require(
                        hello.get('type') == 'hello' and hello.get('v') == 1, 'invalid_game_hello'
                    )
                    joined = True
                    await pilot_controls(websocket, hello, args)
    except TimeoutError:
        if not joined:
            raise Failure('game_join_timeout') from None
    print(json.dumps({'status': 'finished', 'reason': 'time_limit'}), flush=True)


def ledger(path):
    from msg.core.errors import require

    path = path.expanduser().absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
        'unsafe_game_ledger_directory',
    )
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_mode & 0o077 == 0
            and info.st_nlink == 1,
            'unsafe_game_ledger',
        )
    finally:
        os.close(descriptor)
    connection = sqlite3.connect(path)
    connection.execute(
        'CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, type TEXT NOT NULL)'
    )
    connection.commit()
    return connection


async def receive(args):
    from aiohttp import web

    from msg.core.codec import loads
    from msg.core.errors import Failure, require
    from msg.workers.game_webhook import bounded_game_event
    from msg.workers.webhook import verify_delivery

    require(1 <= args.port <= 65535, 'invalid_receiver_port')
    secret = secret_file(args.secret_file)
    database = ledger(args.replay_db)

    async def hook(request):
        body = await request.read()
        try:
            identifier = verify_delivery(request.headers, body, secret, now=datetime.now(UTC))
            envelope = loads(body)
            event = bounded_game_event(envelope.get('event'))
            require(
                event['id'] == envelope['event_id'] and event['type'] == envelope.get('type'),
                'invalid_game_event',
            )
        except Failure:
            return web.Response(status=400)
        with database:
            if database.execute('SELECT 1 FROM deliveries WHERE id=?', (identifier,)).fetchone():
                return web.Response(status=204)
            if database.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] >= 100_000:
                return web.Response(status=503)
            database.execute('INSERT INTO deliveries VALUES (?,?)', (identifier, event['type']))
        # The ledger is durable; stdout is only a demo. Real business effects need the same transaction.
        print(json.dumps({'delivery_id': identifier, 'type': event['type']}), flush=True)
        return web.Response(status=204)

    app = web.Application(client_max_size=4096)
    app.router.add_post('/hook', hook)
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner, args.host, args.port).start()
        print(
            json.dumps({
                'receiver': f'http://{args.host}:{args.port}/hook',
                'seconds': args.seconds,
            }),
            flush=True,
        )
        await asyncio.sleep(args.seconds)
    finally:
        await runner.cleanup()
        database.close()


async def run(args):
    if args.command == 'receive':
        return await receive(args)
    if args.command == 'pilot':
        return await pilot(args)
    from msg.core.codec import b64, wire
    from msg.workers.webhook import validate_endpoint

    async with signed_client(args) as (_, call):
        if args.command == 'webhook-set':
            validate_endpoint(args.url)
            operation = 'communication.webhook_set'
            arguments = {'url': args.url, 'secret': b64(secret_file(args.secret_file))}
        elif args.command == 'subscribe':
            operation = 'communication.game_webhook_subscribe'
            arguments = {'events': sorted(set(args.events))}
        else:
            operation = 'communication.game_webhook_' + args.command
            arguments = {}
        result = await call(operation, arguments)
        print(json.dumps(wire(result.data), ensure_ascii=False), flush=True)


def main():
    args = (
        parser().parse_args()
    )  # --help exits before any identity, secret or network initialization.
    from aiohttp import ClientError
    from httpx import HTTPError

    from msg.core.errors import Failure

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Failure as exc:
        print(json.dumps({'error': exc.code}))
        return 1
    except OSError, TimeoutError, sqlite3.Error, ClientError, HTTPError:
        print(json.dumps({'error': 'game_example_unavailable'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
