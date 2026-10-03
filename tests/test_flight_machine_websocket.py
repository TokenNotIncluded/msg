"""Machine tickets enter the real flight world with fresh signed authority."""

import json
import secrets
from dataclasses import replace
from datetime import timedelta

import pytest
from test_flight_websocket import (
    active_count,
    browser_login,
    closed,
    controls,
    flight_server,
    make_private,
    oauth,
    receive_until,
    snapshot,
)
from test_service import NOW

from msg.core.codec import wire
from msg.core.requests import request_for

__all__ = ['flight_server', 'oauth']


async def ticket(flight, oauth):
    app, key, subject, _ = oauth
    nonce = secrets.token_urlsafe(32)
    request = request_for(
        'communication.game_join_ticket',
        {'nonce': nonce},
        app.settings.service_url,
        signer=key,
        subject=subject,
        expires_at=app.clock() + timedelta(seconds=120),
    )
    url = flight.url.replace('ws://', 'http://').replace(
        '/_flight', '/-/p/communication.game_join_ticket'
    )
    async with flight.client.post(
        url, headers={'Host': 'testserver'}, json=wire(request)
    ) as response:
        result = await response.json()
        assert response.status == 200 and result['status'] == 'ok', result
    assert 'nonce' not in result['data'] and 'ticket' not in result['data']
    return {'id': result['data']['ticket_id'], 'nonce': nonce}


async def machine(flight, proof):
    peer = await flight.connect()
    await peer.send_json({'v': 1, 'type': 'join', 'ticket': proof})
    hello = await receive_until(peer, lambda body: body.get('type') == 'hello')
    return peer, hello


async def revoke(oauth):
    app, key, _, _ = oauth
    async with app.metadata.transaction(write=True) as tx:
        old = await tx.credential(key.key_id)
        owner = await tx.subject(old.subject_id)
        await tx.save_credential(replace(old, revoked_at=NOW), owner.auth_version)


async def test_signed_machine_ticket_drives_the_same_authoritative_world(flight_server, oauth):
    _, _, subject, _ = oauth
    proof = await ticket(flight_server, oauth)
    peer, hello = await machine(flight_server, proof)
    assert hello['self']['subject_id'] == subject and not hello['self']['guest']
    assert hello['self']['handle'].removeprefix('@') == 'oauth-owner'
    assert flight_server.hub.world.active_count == 1
    assert sum(p.machine_principal is not None for p in flight_server.hub.peers.values()) == 1
    await peer.send_json(controls(1, throttle=1, yaw=0.3, pitch=0.1))
    moved = await snapshot(peer, lambda body: body['players'][0]['ack_seq'] == 1)
    assert moved['players'][0]['id'] == hello['self']['id']
    assert moved['players'][0]['yaw'] == 0.3 and moved['players'][0]['pitch'] == 0.1

    reused = await flight_server.connect()
    await reused.send_json({'v': 1, 'type': 'join', 'ticket': proof})
    await closed(reused, 4013)
    assert flight_server.hub.world.active_count == 1


async def test_ticket_with_a_wrong_nonce_cannot_create_a_machine_player(flight_server, oauth):
    proof = await ticket(flight_server, oauth)
    peer = await flight_server.connect()
    await peer.send_json({
        'v': 1,
        'type': 'join',
        'ticket': {**proof, 'nonce': secrets.token_urlsafe(32)},
    })
    await closed(peer, 4013)
    await active_count(flight_server, 0)
    assert not flight_server.hub.world.ships
    valid, _ = await machine(flight_server, proof)
    assert (await snapshot(valid))['total_players'] == 1


async def test_machine_ticket_cannot_mix_cookies_resume_or_observer_mode(flight_server, oauth):
    proof = await ticket(flight_server, oauth)
    cookie = await browser_login(oauth)
    peer = await flight_server.connect(cookie=cookie)
    await peer.send_json({'v': 1, 'type': 'join', 'ticket': proof})
    await closed(peer, 1008)
    peer = await flight_server.connect()
    await peer.send_json({'v': 1, 'type': 'join', 'ticket': proof, 'resume': 'a' * 32})
    await closed(peer, 1008)
    peer = await flight_server.connect()
    await peer.send_json({'v': 1, 'type': 'observe', 'ticket': proof})
    await closed(peer, 1008)
    # Rejected credential combinations did not consume or silently downgrade
    # the ticket into an anonymous guest join.
    await active_count(flight_server, 0)
    assert not flight_server.hub.world.ships
    valid, _ = await machine(flight_server, proof)
    assert (await snapshot(valid))['total_players'] == 1


async def test_private_machine_reuses_existing_anonymous_public_projection(flight_server, oauth):
    app, key, subject, _ = oauth
    await make_private(app, key, subject)
    proof = await ticket(flight_server, oauth)
    signed, hello = await machine(flight_server, proof)
    assert hello['self']['subject_id'] is None and hello['self']['home_body']['private']
    guest, _ = await flight_server.join()
    public = await snapshot(guest, lambda body: body['total_players'] == 2)
    encoded = json.dumps(public)
    assert subject not in encoded and 'oauth-owner' not in encoded
    assert 'home_body' not in encoded and 'home_position' not in encoded
    assert hello['self']['home_body']['id'] not in encoded
    assert signed.close_code is None


@pytest.mark.parametrize('before_join', [False, True])
async def test_machine_revocation_is_enforced_before_join_and_while_connected(
    flight_server, oauth, before_join
):
    proof = await ticket(flight_server, oauth)
    if before_join:
        await revoke(oauth)
        peer = await flight_server.connect()
        await peer.send_json({'v': 1, 'type': 'join', 'ticket': proof})
    else:
        peer, _ = await machine(flight_server, proof)
        await snapshot(peer)
        await revoke(oauth)
    await closed(peer, 4013, timeout=2.5)
    await active_count(flight_server, 0)
