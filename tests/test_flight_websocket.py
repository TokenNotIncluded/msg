"""Flight acceptance uses real TCP WebSockets and real OAuth credentials."""

import asyncio
import json
import math
import socket
import time
from dataclasses import dataclass, replace
from datetime import timedelta

import aiohttp
import pytest
import uvicorn
from test_oauth import browser_login, oauth as real_oauth
from test_service import NOW, call

from msg.core.codec import wire
from msg.transports.http import create_app
from msg.transports.oauth_http import csrf

oauth = real_oauth


@dataclass
class NetworkFlight:
    app: object
    hub: object
    client: aiohttp.ClientSession
    url: str
    peers: list
    server: object
    task: asyncio.Task

    async def connect(self, *, cookie=None, headers=None, query=''):
        values = {'Host': 'testserver', 'Origin': 'http://testserver'}
        if cookie:
            values['Cookie'] = 'msg_session=' + cookie
        values.update(headers or {})
        peer = await self.client.ws_connect(self.url + query, headers=values)
        self.peers.append(peer)
        return peer

    async def join(self, *, cookie=None, resume=None):
        peer = await self.connect(cookie=cookie)
        await peer.send_json({'v': 1, 'type': 'join', **({'resume': resume} if resume else {})})
        hello = await receive_until(peer, lambda body: body.get('type') == 'hello')
        assert hello['v'] == 1
        assert hello['resume']
        assert isinstance(hello['server_time_ms'], int)
        assert len(hello['regions']) == 19
        assert hello['limits']['max_players'] <= 96
        return peer, hello


@pytest.fixture
async def flight_server(oauth, request):
    service, _, _, _ = oauth
    service.settings = replace(service.settings, service_url='http://testserver')
    router = create_app(service)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            router,
            host='127.0.0.1',
            port=port,
            lifespan=getattr(request, 'param', 'off'),
            loop='asyncio',
            ws='websockets-sansio',
            access_log=False,
            log_level='error',
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]), name='flight-test-server')
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    pytest.fail('network server stopped during startup')
                await asyncio.sleep(0.01)
        async with aiohttp.ClientSession() as client:
            flight = NetworkFlight(
                service,
                router.state.flight_hub,
                client,
                f'ws://127.0.0.1:{port}/_flight',
                [],
                server,
                task,
            )
            try:
                yield flight
            finally:
                await flight.hub.close()
                await asyncio.gather(*(peer.close() for peer in flight.peers))
                assert flight.hub.world.active_count == 0
                assert not flight.hub.peers and not flight.hub.addresses
                assert flight.hub.runner is None
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


@pytest.mark.parametrize('flight_server', ['on'], indirect=True)
async def test_production_lifespan_cleans_connected_world(flight_server):
    """A real Uvicorn shutdown must run the application's flight cleanup."""
    flight = flight_server
    peer, hello = await flight.join()
    assert hello['self']['id'] in flight.hub.world.ships
    assert flight.hub.world.active_count == 1
    assert flight.hub.runner is not None

    flight.server.should_exit = True
    async with asyncio.timeout(5):
        await flight.task

    # Uvicorn owns the transport shutdown and may close it before the peer
    # processes the restart frame. Application cleanup must work either way.
    async with asyncio.timeout(3):
        while True:
            frame = await peer.receive()
            if frame.type != aiohttp.WSMsgType.TEXT:
                assert frame.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
                assert peer.closed
                break
    assert flight.hub.closed
    assert flight.hub.runner is None
    assert flight.hub.world.active_count == 0
    assert not flight.hub.world.ships
    assert not flight.hub.peers
    assert not flight.hub.addresses
    assert not flight.hub.admissions


def controls(seq, **changes):
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


async def receive_until(peer, predicate, *, timeout=4):
    async with asyncio.timeout(timeout):
        while True:
            frame = await peer.receive()
            assert frame.type == aiohttp.WSMsgType.TEXT, (
                frame.type,
                frame.data,
                peer.close_code,
            )
            body = json.loads(frame.data)
            if predicate(body):
                return body


async def snapshot(peer, predicate=lambda body: True, *, timeout=4):
    return await receive_until(
        peer, lambda body: body.get('type') == 'snapshot' and predicate(body), timeout=timeout
    )


def player(body, ship_id):
    return next((item for item in body['players'] if item['id'] == ship_id), None)


async def closed(peer, expected, *, timeout=3):
    async with asyncio.timeout(timeout):
        while True:
            frame = await peer.receive()
            if frame.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                assert peer.close_code == expected
                return
            assert frame.type == aiohttp.WSMsgType.TEXT, (frame.type, frame.data)


async def active_count(flight, count):
    async with asyncio.timeout(3):
        while flight.hub.world.active_count != count:
            await asyncio.sleep(0.02)


async def make_private(app, key, subject):
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(subject)).generation
    result = await call(
        app,
        'content.chmod',
        {'id': subject, 'mode': '0600'},
        key=key,
        subject=subject,
        expected=((subject, generation),),
    )
    assert result.status == 'ok', wire(result)


@pytest.mark.asyncio
async def test_two_network_guests_share_movement_across_regions_and_disconnect(flight_server):
    flight = flight_server
    first, hello1 = await flight.join()
    second, hello2 = await flight.join()
    ship1, ship2 = hello1['self']['id'], hello2['self']['id']
    assert ship1 != ship2
    assert hello1['self']['guest'] and hello1['self']['subject_id'] is None
    initial = await snapshot(second, lambda body: len(body['players']) == 2)
    origin = player(initial, ship1)['position']
    await first.send_json(controls(1, throttle=1, yaw=0.6, pitch=0.2))
    observed = await snapshot(
        second,
        lambda body: (
            player(body, ship1)
            and player(body, ship1)['ack_seq'] == 1
            and math.dist(player(body, ship1)['position'], origin) > 0.1
        ),
    )
    assert math.hypot(*player(observed, ship1)['velocity']) > 0
    destination = (player(observed, ship1)['region'] + 1) % 19
    await second.send_json({'v': 1, 'type': 'region', 'region': destination})
    relocated = await snapshot(
        first,
        lambda body: player(body, ship2) and player(body, ship2)['region'] == destination,
    )
    assert {ship['id'] for ship in relocated['players']} == {ship1, ship2}
    assert relocated['total_players'] == 2
    assert sum(relocated['region_counts'].values()) == 2
    await second.close()
    await active_count(flight, 1)
    survivor = await snapshot(first, lambda body: body['total_players'] == 1)
    assert {ship['id'] for ship in survivor['players']} == {ship1}
    await flight.hub.close()
    await active_count(flight, 0)
    await closed(first, 1001)


@pytest.mark.asyncio
async def test_network_browser_identity_and_spawn_reject_forged_subject(flight_server, oauth):
    flight = flight_server
    app, _, subject, _ = oauth
    cookie = await browser_login(oauth)
    signed, hello = await flight.join(cookie=cookie)
    assert not hello['self']['guest']
    assert hello['self']['subject_id'] == subject
    assert hello['self']['handle'].removeprefix('@') == 'oauth-owner'
    from msg.transports.universe import subject_position

    home_position = await subject_position(app, subject)
    assert hello['self']['home_position'] == pytest.approx(home_position)
    assert hello['self']['position'] == pytest.approx([
        home_position[0],
        home_position[1] + 7,
        home_position[2],
    ])
    guest, _ = await flight.join()
    signed_id = hello['self']['id']
    public = await snapshot(guest, lambda body: player(body, signed_id) is not None)
    assert player(public, signed_id)['subject_id'] == subject
    for sequence in range(3):
        await signed.send_json(controls(sequence, subject_id='u_root', position=[0, 0, 0]))
    await closed(signed, 1008)
    await active_count(flight, 1)


@pytest.mark.asyncio
async def test_network_private_identity_and_home_are_never_disclosed_to_peers(flight_server, oauth):
    flight = flight_server
    app, key, subject, _ = oauth
    await make_private(app, key, subject)
    cookie = await browser_login(oauth)
    with pytest.raises(aiohttp.WSServerHandshakeError) as rejected:
        await flight.connect(headers={'Cookie': f'msg_session={cookie}; msg_session={cookie}'})
    assert rejected.value.status == 403
    guest, _ = await flight.join()
    signed, secret = await flight.join(cookie=cookie)
    assert not secret['self']['guest']
    assert secret['self']['subject_id'] is None
    assert 'oauth-owner' not in json.dumps(secret)
    assert subject not in json.dumps(secret)
    home = secret['self']['home_body']
    assert home['private'] is True and home['radius'] == 3
    assert home['position'] == secret['self']['home_position']
    assert secret['self']['position'] == pytest.approx([
        home['position'][0],
        home['position'][1] + 7,
        home['position'][2],
    ])
    peer_view = await snapshot(guest, lambda body: player(body, secret['self']['id']) is not None)
    encoded = json.dumps(peer_view)
    assert subject not in encoded and 'oauth-owner' not in encoded
    assert 'home_position' not in encoded and 'home_body' not in encoded
    await signed.close()
    await active_count(flight, 1)
    _, resumed = await flight.join(cookie=cookie)
    assert resumed['self']['id'] == secret['self']['id']
    assert resumed['self']['home_position'] == secret['self']['home_position']
    assert resumed['self']['subject_id'] is None


@pytest.mark.asyncio
async def test_network_rejects_unsafe_handshakes_and_untrusted_frames(flight_server):
    flight = flight_server
    for headers, query in [
        ({'Origin': 'https://evil.example'}, ''),
        ({'Origin': ''}, ''),
        ({'Host': 'evil.example'}, ''),
        ({'Authorization': 'Bearer fake-credential'}, ''),
        ({'Authorization': ''}, ''),
        ({'Cookie': 'msg_session=fake-credential'}, ''),
        ({}, '?token=fake-credential'),
    ]:
        with pytest.raises(aiohttp.WSServerHandshakeError) as rejected:
            await flight.connect(headers=headers, query=query)
        assert rejected.value.status == 403
    binary, _ = await flight.join()
    await binary.send_bytes(b'{}')
    await closed(binary, 1003)
    oversized, _ = await flight.join()
    await oversized.send_str(' ' * 2049)
    await closed(oversized, 1009)
    forged, _ = await flight.join()
    for sequence in range(3):
        await forged.send_json(controls(sequence, yaw=math.pi + 0.01, hp=100000))
    await closed(forged, 1008)
    flooded, _ = await flight.join()
    for sequence in range(80):
        try:
            await flooded.send_json(controls(sequence))
        except aiohttp.ClientConnectionError:
            break
    await closed(flooded, 1008)
    await active_count(flight, 0)
    admitted = [await flight.join() for _ in range(8)]
    with pytest.raises(aiohttp.WSServerHandshakeError) as rejected:
        await flight.connect()
    assert rejected.value.status == 403
    assert flight.hub.world.active_count == 8
    assert len(flight.hub.peers) == 8
    await asyncio.gather(*(peer.close() for peer, _ in admitted))
    await active_count(flight, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['logout', 'parent', 'expiry', 'runtime', 'quarantine'])
async def test_network_current_authority_loss_closes_existing_socket(
    flight_server, oauth, mutation
):
    flight = flight_server
    app, key, subject, http = oauth
    await make_private(app, key, subject)
    cookie = await browser_login(oauth)
    peer, hello = await flight.join(cookie=cookie)
    guest, guest_hello = await flight.join()
    await snapshot(peer)
    if mutation == 'logout':
        logout = await http.post(
            '/oauth/logout', json={'csrf': csrf(cookie)}, headers={'Origin': 'http://testserver'}
        )
        assert logout.status_code == 200, logout.text
    elif mutation == 'expiry':
        app._oauth_clock[0] = NOW + timedelta(seconds=app.settings.oauth.session_ttl + 1)
    else:
        async with app.metadata.transaction(write=True) as tx:
            if mutation == 'parent':
                old = await tx.credential(key.key_id)
                owner = await tx.subject(old.subject_id)
                await tx.save_credential(replace(old, revoked_at=NOW), owner.auth_version)
            else:
                tx.set_setting(
                    'recovery_runtime_generation'
                    if mutation == 'runtime'
                    else 'recovery_quarantine',
                    'changed',
                )
    restored = mutation in {'runtime', 'quarantine'}
    await closed(peer, 1013 if restored else 4013, timeout=2.5)
    if restored:
        await closed(guest, 1013)
        await active_count(flight, 0)
    else:
        await active_count(flight, 1)
        remaining = await snapshot(guest, lambda body: body['total_players'] == 1)
        assert player(remaining, hello['self']['id']) is None
        assert player(remaining, guest_hello['self']['id']) is not None


async def aim_at(peer, current, target, sequence, *, action=None):
    offset = [target[axis] - current[axis] for axis in range(3)]
    length = math.hypot(*offset)
    await peer.send_json(
        controls(
            sequence,
            yaw=math.atan2(offset[0], -offset[2]),
            pitch=math.asin(offset[1] / length),
            actions=[action] if action else [],
        )
    )


async def fly_near(peer, state, source_id, target_id):
    sequence = 0
    async with asyncio.timeout(8):
        while (
            math.dist(player(state, source_id)['position'], player(state, target_id)['position'])
            > 30
        ):
            sequence += 1
            origin = player(state, source_id)['position']
            destination = player(state, target_id)['position']
            offset = [destination[axis] - origin[axis] for axis in range(3)]
            await peer.send_json(
                controls(
                    sequence,
                    throttle=1,
                    yaw=math.atan2(offset[0], -offset[2]),
                    pitch=math.asin(offset[1] / math.hypot(*offset)),
                )
            )
            state = await snapshot(
                peer,
                lambda body, expected=sequence: player(body, source_id)['ack_seq'] == expected,
            )
    return state, sequence


@pytest.mark.asyncio
async def test_network_laser_skills_cooldowns_regeneration_and_resume(flight_server, oauth):
    flight = flight_server
    cookie = await browser_login(oauth)
    attacker, hello1 = await flight.join(cookie=cookie)
    target, hello2 = await flight.join()
    source_id, target_id = hello1['self']['id'], hello2['self']['id']
    # Both relocations are server commands; clients never supply coordinates.
    sector = (hello1['self']['region'] + 1) % 19
    await attacker.send_json({'v': 1, 'type': 'region', 'region': sector})
    if hello2['self']['region'] != sector:
        await target.send_json({'v': 1, 'type': 'region', 'region': sector})
    state = await snapshot(
        attacker,
        lambda body: (
            player(body, source_id)
            and player(body, target_id)
            and player(body, source_id)['region'] == player(body, target_id)['region'] == sector
        ),
    )
    state, sequence = await fly_near(attacker, state, source_id, target_id)
    sequence += 1
    # Freeze game time across the paired shots and replay. Snapshot delivery and
    # scheduler pauses cannot accidentally expire the 400 ms cooldown.
    clock_state = [time.monotonic()]
    flight.hub.world.clock = lambda: clock_state[0]
    await aim_at(
        attacker,
        player(state, source_id)['position'],
        player(state, target_id)['position'],
        sequence,
        action='laser',
    )
    hit = await snapshot(target, lambda body: player(body, target_id)['hp'] < 100)
    damaged_hp = player(hit, target_id)['hp']
    assert damaged_hp == 85
    source = player(hit, source_id)
    laser_ready = source['laser_ready_ms']
    assert laser_ready > hello1['server_time_ms']
    sequence += 1
    await aim_at(
        attacker, source['position'], player(hit, target_id)['position'], sequence, action='laser'
    )
    cooling = await snapshot(target, lambda body: player(body, source_id)['ack_seq'] == sequence)
    assert player(cooling, target_id)['hp'] == damaged_hp
    assert player(cooling, source_id)['laser_ready_ms'] == laser_ready
    await attacker.send_json(controls(sequence, actions=['laser', 'dash']))
    sequence += 1
    await attacker.send_json(controls(sequence))
    replayed = await snapshot(target, lambda body: player(body, source_id)['ack_seq'] == sequence)
    assert player(replayed, source_id)['dash_ready_ms'] == 0
    assert player(replayed, source_id)['laser_ready_ms'] == laser_ready
    assert player(replayed, target_id)['hp'] == damaged_hp
    await target.send_json(controls(0, actions=['shield', 'dash']))
    protected = await snapshot(
        attacker, lambda body: player(body, target_id)['shield_ready_ms'] > 0
    )
    protected_ship = player(protected, target_id)
    assert protected_ship['shield_until_ms'] > 0 and protected_ship['dash_ready_ms'] > 0
    assert protected_ship['fuel'] < 100
    await target.close()
    await active_count(flight, 1)
    target, resumed = await flight.join(resume=hello2['resume'])
    assert resumed['self']['id'] == target_id
    assert resumed['self']['hp'] == damaged_hp
    assert resumed['self']['shield_ready_ms'] == protected_ship['shield_ready_ms']
    assert resumed['self']['dash_ready_ms'] == protected_ship['dash_ready_ms']
    await target.send_json(controls(1, brake=True))
    await snapshot(attacker, lambda body: player(body, target_id)['ack_seq'] == 1)
    # Advancing only the server clock expires cooldowns; incoming actions still
    # traverse the real socket and authoritative world.
    clock_state[0] += 0.5
    sequence += 1
    await attacker.send_json(controls(sequence))
    state = await snapshot(
        attacker,
        lambda body: (
            player(body, target_id) is not None and player(body, source_id)['ack_seq'] == sequence
        ),
    )
    sequence += 1
    await aim_at(
        attacker,
        player(state, source_id)['position'],
        player(state, target_id)['position'],
        sequence,
        action='laser',
    )
    shield_hit = await snapshot(target, lambda body: player(body, target_id)['hp'] < damaged_hp)
    assert player(shield_hit, target_id)['hp'] == damaged_hp - 6
    wounded = player(shield_hit, target_id)['hp']
    clock_state[0] += 3.5
    healed = await snapshot(target, lambda body: player(body, target_id)['hp'] > wounded)
    assert wounded < player(healed, target_id)['hp'] <= 100
    assert player(healed, target_id)['fuel'] <= 100
    await aim_at(
        target,
        player(healed, target_id)['position'],
        player(healed, source_id)['position'],
        2,
        action='laser',
    )
    retaliation = await snapshot(attacker, lambda body: player(body, source_id)['hp'] < 100)
    surviving = player(retaliation, source_id)
    await attacker.close()
    await active_count(flight, 1)
    attacker, signed_resume = await flight.join(cookie=cookie)
    assert signed_resume['self']['id'] == source_id
    assert signed_resume['self']['hp'] == surviving['hp']
    assert signed_resume['self']['laser_ready_ms'] == surviving['laser_ready_ms']
    assert signed_resume['self']['fuel'] < 100


@pytest.mark.asyncio
async def test_network_profile_visibility_changes_rekey_home_and_preserve_battle_state(
    flight_server, oauth
):
    flight = flight_server
    app, key, subject, _ = oauth
    async with app.metadata.transaction(write=False) as tx:
        original_mode = (await tx.resource(subject)).mode
    cookie = await browser_login(oauth)
    signed, public = await flight.join(cookie=cookie)
    guest, anonymous = await flight.join()
    signed_id, guest_id = public['self']['id'], anonymous['self']['id']
    sector = (public['self']['region'] + 1) % 19
    await signed.send_json({'v': 1, 'type': 'region', 'region': sector})
    if anonymous['self']['region'] != sector:
        await guest.send_json({'v': 1, 'type': 'region', 'region': sector})
    state = await snapshot(
        guest,
        lambda body: (
            player(body, signed_id)
            and player(body, guest_id)
            and player(body, signed_id)['region'] == player(body, guest_id)['region'] == sector
        ),
    )
    state, sequence = await fly_near(guest, state, guest_id, signed_id)
    sequence += 1
    await guest.send_json(controls(sequence, brake=True))
    now = time.monotonic()
    flight.hub.world.clock = lambda: now
    await signed.send_json(controls(0, actions=['shield', 'dash', 'laser'], brake=True))
    protected = await snapshot(guest, lambda body: player(body, signed_id)['ack_seq'] == 0)
    sequence += 1
    await aim_at(
        guest,
        player(protected, guest_id)['position'],
        player(protected, signed_id)['position'],
        sequence,
        action='laser',
    )
    battle = await snapshot(signed, lambda body: player(body, signed_id)['hp'] < 100)
    before = player(battle, signed_id)
    assert before['hp'] < 100 and before['fuel'] < 100
    deadlines = ('laser_ready_ms', 'shield_ready_ms', 'dash_ready_ms', 'region_ready_ms')
    assert all(before[name] > 0 for name in deadlines)
    await make_private(app, key, subject)
    await closed(signed, 4013, timeout=2.5)
    await active_count(flight, 1)
    signed, private = await flight.join(cookie=cookie)
    assert private['self']['id'] != signed_id
    assert private['resume'] != public['resume']
    assert private['self']['subject_id'] is None
    assert private['self']['home_position'] != public['self']['home_position']
    assert private['self']['hp'] == before['hp']
    assert private['self']['fuel'] == before['fuel']
    assert {name: private['self'][name] for name in deadlines} == {
        name: before[name] for name in deadlines
    }
    private_id = private['self']['id']
    hidden = await snapshot(guest, lambda body: player(body, private_id) is not None)
    assert subject not in json.dumps(hidden) and 'oauth-owner' not in json.dumps(hidden)
    assert player(hidden, signed_id) is None
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(subject)).generation
    restored = await call(
        app,
        'content.chmod',
        {'id': subject, 'mode': f'{original_mode:04o}'},
        key=key,
        subject=subject,
        expected=((subject, generation),),
    )
    assert restored.status == 'ok', wire(restored)
    await closed(signed, 4013, timeout=2.5)
    await active_count(flight, 1)
    _, restored_home = await flight.join(cookie=cookie)
    assert restored_home['self']['id'] != private_id
    assert restored_home['self']['subject_id'] == subject
    assert restored_home['self']['home_position'] == public['self']['home_position']
    assert restored_home['self']['hp'] == before['hp']
    assert restored_home['self']['fuel'] == before['fuel']
    assert {name: restored_home['self'][name] for name in deadlines} == {
        name: before[name] for name in deadlines
    }
