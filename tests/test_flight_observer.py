"""Observers use real TCP/OAuth, never a hidden guest player or test principal."""

import json

import aiohttp
import pytest
from test_flight_websocket import (
    active_count,
    browser_login,
    closed,
    controls,
    flight_server,
    make_private,
    oauth,
    player,
    receive_until,
    snapshot,
)

from msg.transports.oauth_http import csrf

# Imported fixtures are deliberate: this is the same real PostgreSQL-backed
# OAuth and TCP harness used by the driving acceptance suite.
__all__ = ['flight_server', 'oauth']


async def observe(flight, *, cookie=None):
    peer = await flight.connect(cookie=cookie)
    await peer.send_json({'v': 1, 'type': 'observe'})
    hello = await receive_until(peer, lambda body: body.get('type') == 'observer_hello')
    assert len(hello['regions']) == 19
    assert not {'self', 'self_id', 'resume', 'home_position', 'home_body'} & hello.keys()
    return peer, hello


async def observed(peer, predicate=lambda body: True):
    return await receive_until(
        peer, lambda body: body.get('type') == 'observer_snapshot' and predicate(body)
    )


async def test_observer_shares_two_real_players_without_using_a_player_slot(flight_server):
    flight = flight_server
    flight.hub.world.max_players = 2
    observer, hello = await observe(flight)
    empty = await observed(observer)
    assert empty['players'] == [] and empty['total_players'] == 0
    assert flight.hub.world.active_count == 0 and not flight.hub.world.ships

    first, first_hello = await flight.join()
    second, second_hello = await flight.join()
    ids = {first_hello['self']['id'], second_hello['self']['id']}
    initial = await observed(observer, lambda body: body['total_players'] == 2)
    assert {pilot['id'] for pilot in initial['players']} == ids
    assert flight.hub.world.active_count == len(flight.hub.world.ships) == 2
    assert len(flight.hub.peers) == 3
    assert sum(initial['region_counts'].values()) == 2
    assert hello['regions'] == first_hello['regions'] == second_hello['regions']
    assert initial['gravity'] == first_hello['gravity']
    assert initial['collectibles']['seed'] == first_hello['collectibles']['seed']

    ship_id = first_hello['self']['id']
    origin = player(initial, ship_id)['position']
    await first.send_json(controls(1, throttle=1, yaw=0.6, pitch=0.2))
    moved = await observed(
        observer,
        lambda body: (
            player(body, ship_id)['ack_seq'] == 1 and player(body, ship_id)['position'] != origin
        ),
    )
    assert moved['total_players'] == 2
    await first.send_json({'v': 1, 'type': 'region', 'region': 18})
    relocated = await observed(observer, lambda body: player(body, ship_id)['region'] == 18)
    assert relocated['region_counts']['18'] >= 1
    assert 'self_id' not in relocated and 'region' not in relocated
    await second.close()
    await active_count(flight, 1)
    survivor = await observed(observer, lambda body: body['total_players'] == 1)
    assert survivor['players'][0]['id'] == ship_id
    await first.close()
    await active_count(flight, 0)
    assert (await observed(observer, lambda body: body['total_players'] == 0))['players'] == []


@pytest.mark.parametrize(
    'packet',
    [
        controls(1, throttle=1, actions=['laser']),
        {'v': 1, 'type': 'region', 'region': 18},
        {'v': 1, 'type': 'join'},
    ],
)
async def test_observer_cannot_move_attack_relocate_or_join(flight_server, packet):
    observer, _ = await observe(flight_server)
    await observed(observer)
    await observer.send_json(packet)
    await closed(observer, 1008)
    await active_count(flight_server, 0)
    assert not flight_server.hub.world.ships
    assert not flight_server.hub.world._events


async def test_observer_private_pilot_has_only_existing_public_game_projection(
    flight_server, oauth
):
    app, key, subject, _ = oauth
    await make_private(app, key, subject)
    cookie = await browser_login(oauth)
    signed, secret = await flight_server.join(cookie=cookie)
    observer, hello = await observe(flight_server)
    public = await observed(observer, lambda body: body['total_players'] == 1)
    pilot = public['players'][0]
    assert pilot['id'] == secret['self']['id']
    assert pilot['subject_id'] is None and pilot['handle'].startswith('pilot-')
    assert pilot['position'] == secret['self']['position']
    encoded = json.dumps([hello, public])
    assert subject not in encoded and 'oauth-owner' not in encoded
    assert 'home_position' not in encoded and 'home_body' not in encoded
    assert secret['self']['home_body']['id'] not in encoded
    assert len(flight_server.hub.world.ships) == 1
    await signed.close()


@pytest.mark.parametrize('mutation', ['logout', 'runtime', 'quarantine'])
async def test_observer_current_authority_is_rechecked_without_ship(flight_server, oauth, mutation):
    app, _, _, http = oauth
    cookie = await browser_login(oauth)
    observer, _ = await observe(flight_server, cookie=cookie)
    await observed(observer)
    if mutation == 'logout':
        logout = await http.post(
            '/oauth/logout', json={'csrf': csrf(cookie)}, headers={'Origin': 'http://testserver'}
        )
        assert logout.status_code == 200
    else:
        async with app.metadata.transaction(write=True) as tx:
            tx.set_setting(
                'recovery_runtime_generation' if mutation == 'runtime' else 'recovery_quarantine',
                'changed',
            )
    await closed(observer, 4013 if mutation == 'logout' else 1013, timeout=2.5)
    assert not flight_server.hub.world.ships


async def test_observer_retains_origin_and_bounded_admission(flight_server, monkeypatch):
    import msg.transports.flight_space as flight_space

    with pytest.raises(aiohttp.WSServerHandshakeError) as rejected:
        await flight_server.connect(headers={'Origin': 'http://other.invalid'})
    assert rejected.value.status == 403
    monkeypatch.setattr(flight_space, 'MAX_OBSERVERS', 1)
    observer, _ = await observe(flight_server)
    await observed(observer)
    rejected_observer = await flight_server.connect()
    await rejected_observer.send_json({'v': 1, 'type': 'observe'})
    await closed(rejected_observer, 1013)
    pilot, _ = await flight_server.join()
    assert (await snapshot(pilot))['total_players'] == 1
    assert flight_server.hub.world.active_count == 1


async def test_observer_receives_current_public_geometry_after_acl_removal(flight_server, oauth):
    app, key, subject, _ = oauth
    observer, hello = await observe(flight_server)
    assert subject in {well['id'] for well in hello['gravity']['wells']}
    # An empty observed room stays on the same public geometry across its 2s
    # ACL revalidation, rather than randomizing all token routes on every read.
    unchanged = await observed(observer, lambda body: body['tick'] >= 35)
    assert unchanged['collectibles']['seed'] == hello['collectibles']['seed']
    await make_private(app, key, subject)
    state = await observed(
        observer, lambda body: subject not in {well['id'] for well in body['gravity']['wells']}
    )
    assert subject not in json.dumps(state)
    assert state['total_players'] == 0
