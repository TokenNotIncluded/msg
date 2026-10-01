"""Two independent MSG databases exchange signed mail through their HTTP APIs."""

import uuid
from dataclasses import replace

import httpx
import pytest
from conftest import _create_database, _drop_database
from test_service import NOW, call, register

from msg.application import Application
from msg.client_internet import deliver, discover, make_envelope
from msg.config import write_example
from msg.core.codec import b64
from msg.core.internet_address import DELIVERY_REL, SUBJECT_PROPERTY, delivery_request_id
from msg.transports.http import create_app


@pytest.fixture
async def peer(tmp_path, installation_seed, pg_cluster):
    database = 'msg_peer_' + uuid.uuid4().hex
    _create_database(pg_cluster, database)
    try:
        dsn = pg_cluster.format(database=database)
        from msg.admin.root import _approve_csr, _provision

        root = tmp_path / 'peer'
        settings = write_example(root / 'etc', root / 'data', 'http://peer.test', postgres_dsn=dsn)
        app = Application(settings, clock=lambda: NOW)
        csr, root_key = await _provision(app, 'correct-horse-test-passphrase')
        await _approve_csr(app, csr, root_key, expected_digest=None, operator='test-fixture')
        try:
            yield app
        finally:
            await app.close()
    finally:
        _drop_database(pg_cluster, database)


@pytest.mark.asyncio
async def test_two_servers_discover_allow_send_reply_and_replay(installed, peer):
    home, _ = installed
    alice_key, alice_id, alice_cert = await register(home, 'alice')
    bob_key, bob_id, bob_cert = await register(peer, 'bob')
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(home)), base_url=home.settings.service_url
        ) as home_http,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(peer)), base_url=peer.settings.service_url
        ) as peer_http,
    ):
        alice = await discover(home_http, 'alice@testserver', allow_http=True)
        bob = await discover(peer_http, 'bob@peer.test', allow_http=True)
        assert alice['subject_id'] == alice_id and bob['subject_id'] == bob_id
        envelope = make_envelope(alice, bob, '你好，来自另一台 MSG。', alice_key, now=NOW)
        denied = await call(peer, 'communication.internet_receive', {'envelope': envelope})
        assert denied.error.code == 'internet_sender_not_allowed'
        for app, key, uid, cert, remote in (
            (peer, bob_key, bob_id, bob_cert, alice),
            (home, alice_key, alice_id, alice_cert, bob),
        ):
            allowed = await call(
                app,
                'communication.internet_allow',
                {
                    'address': remote['address'],
                    'subject_id': remote['subject_id'],
                    'public_key': remote['public_key'],
                },
                key=key,
                subject=uid,
                certs=(cert,),
            )
            assert allowed.status == 'ok', allowed.error
        received = await deliver(peer_http, bob, envelope, now=NOW)
        assert received.data['delivered'] and not received.data['duplicate']
        duplicate = await deliver(peer_http, bob, envelope, now=NOW)
        assert duplicate.replayed or duplicate.data['duplicate']
        inbox = await call(
            peer, 'communication.internet_inbox', {}, key=bob_key, subject=bob_id, certs=(bob_cert,)
        )
        assert len(inbox.data['messages']) == 1
        assert inbox.data['messages'][0]['envelope'] == envelope
        anonymous = await call(peer, 'communication.internet_inbox', {})
        assert anonymous.status == 'error'
        reply = make_envelope(bob, alice, '收到，回复也跨站。', bob_key, now=NOW)
        assert (await deliver(home_http, alice, reply, now=NOW)).data['delivered']
        home_inbox = await call(
            home,
            'communication.internet_inbox',
            {},
            key=alice_key,
            subject=alice_id,
            certs=(alice_cert,),
        )
        assert home_inbox.data['messages'][0]['envelope']['body'] == '收到，回复也跨站。'
        deleted = await call(
            peer,
            'communication.internet_delete',
            {'id': inbox.data['messages'][0]['id']},
            key=bob_key,
            subject=bob_id,
            certs=(bob_cert,),
        )
        assert deleted.status == 'ok'
        assert (await deliver(peer_http, bob, envelope, now=NOW)).replayed
        empty = await call(
            peer, 'communication.internet_inbox', {}, key=bob_key, subject=bob_id, certs=(bob_cert,)
        )
        assert not empty.data['messages']
        revoked = await call(
            peer,
            'communication.internet_revoke',
            {'address': alice['address']},
            key=bob_key,
            subject=bob_id,
            certs=(bob_cert,),
        )
        assert revoked.status == 'ok'
        rejected = await call(peer, 'communication.internet_receive', {'envelope': envelope})
        assert rejected.error.code == 'internet_sender_not_allowed'


@pytest.mark.asyncio
async def test_webfinger_public_visibility_methods_and_rel_filter(installed):
    app, _ = installed
    _, uid, _ = await register(app, 'public-agent')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        path = '/.well-known/webfinger'
        query = {'resource': 'acct:public-agent@testserver'}
        response = await http.get(path, params=query, headers={'Origin': 'https://other.example'})
        assert response.status_code == 200, response.text
        assert response.headers['content-type'] == 'application/jrd+json'
        assert response.headers['access-control-allow-origin'] == '*'
        assert response.json()['properties'][SUBJECT_PROPERTY] == uid
        filtered = await http.get(path, params={**query, 'rel': DELIVERY_REL})
        assert [link['rel'] for link in filtered.json()['links']] == [DELIVERY_REL]
        assert (await http.head(path, params=query)).content == b''
        assert (await http.post(path, params=query)).status_code == 405
        assert (await http.options(path)).status_code == 204
        assert (await http.get(path)).status_code == 400
        assert (
            await http.get(path, params={'resource': 'acct:public-agent@elsewhere.test'})
        ).status_code == 404
        assert (
            await http.get(path, params={'resource': 'acct:missing@testserver'})
        ).status_code == 404
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(uid)
            await tx.replace(
                replace(resource, mode=0o700, generation=resource.generation + 1),
                resource.generation,
            )
        assert (await http.get(path, params=query)).status_code == 404


@pytest.mark.asyncio
async def test_pinned_signatures_target_binding_expiry_and_payload_conflicts(installed):
    from datetime import timedelta

    from msg.client_internet import envelope_bytes
    from msg.core.codec import wire
    from msg.core.internet_address import PURPOSE
    from msg.security.crypto import Ed25519Signer

    app, _ = installed
    bob_key, bob_id, bob_cert = await register(app, 'bob')
    remote_key = Ed25519Signer.generate()
    remote = {'address': 'alice@remote.test', 'subject_id': 'u_remote', 'key_id': remote_key.key_id}
    bob = {'address': 'bob@testserver', 'subject_id': bob_id}
    granted = await call(
        app,
        'communication.internet_allow',
        {
            'address': remote['address'],
            'subject_id': remote['subject_id'],
            'public_key': b64(remote_key.public_key),
        },
        key=bob_key,
        subject=bob_id,
        certs=(bob_cert,),
    )
    assert granted.status == 'ok', granted.error
    original = make_envelope(remote, bob, 'hello', remote_key, now=NOW)

    async def receive(envelope):
        return await call(
            app,
            'communication.internet_receive',
            {'envelope': envelope},
            rid=delivery_request_id(envelope),
        )

    tampered = {**original, 'body': 'forged'}
    assert (await receive(tampered)).error.code == 'invalid_signature'
    stranger = Ed25519Signer.generate()
    forged = {
        **original,
        'signature': wire(stranger.sign(envelope_bytes(original), purpose=PURPOSE)),
    }
    assert (await receive(forged)).error.code == 'invalid_signature'
    wrong_recipient = make_envelope(
        remote, {**bob, 'subject_id': 'u_other'}, 'hello', remote_key, now=NOW
    )
    assert (await receive(wrong_recipient)).error.code == 'internet_wrong_recipient'
    wrong_server = make_envelope(
        remote, {**bob, 'address': 'bob@elsewhere.test'}, 'hello', remote_key, now=NOW
    )
    assert (await receive(wrong_server)).error.code == 'internet_wrong_recipient'
    expired = make_envelope(remote, bob, 'hello', remote_key, now=NOW - timedelta(minutes=11))
    assert (await receive(expired)).error.code == 'internet_message_expired'
    future = make_envelope(remote, bob, 'hello', remote_key, now=NOW + timedelta(minutes=1))
    assert (await receive(future)).error.code == 'internet_message_expired'
    assert (await receive(original)).status == 'ok'
    changed = make_envelope(
        remote, bob, 'changed', remote_key, now=NOW, message_id=original['message_id']
    )
    assert (await receive(changed)).error.code == 'internet_message_conflict'
    # A different local account cannot see or modify Bob's inbox.
    other_key, other_id, other_cert = await register(app, 'other')
    other = await call(
        app,
        'communication.internet_inbox',
        {},
        key=other_key,
        subject=other_id,
        certs=(other_cert,),
    )
    assert not other.data['contacts'] and not other.data['messages']
    assert other.data['total'] == 0 and other.data['next_offset'] is None
    authenticated_receive = await call(
        app,
        'communication.internet_receive',
        {'envelope': original},
        key=bob_key,
        subject=bob_id,
        certs=(bob_cert,),
    )
    assert authenticated_receive.status == 'error'
    assert authenticated_receive.error.code in {'anonymous_only', 'credential_ceiling'}


@pytest.mark.asyncio
async def test_internet_state_survives_real_backup_and_recovery_proof(installed, tmp_path, pg_dsn):
    from msg.admin.backups import backup, restore
    from msg.admin.recovery_proof import IndependentRecoveryPin, capture, open_for_proof, promote
    from msg.core.codec import digest
    from msg.plugins.internet import mail_state
    from msg.security.crypto import Ed25519Signer

    app, root = installed
    key, subject, cert = await register(app, 'mail-backup')
    remote_key = Ed25519Signer.generate()
    remote = {'address': 'alice@remote.test', 'subject_id': 'u_remote', 'key_id': remote_key.key_id}
    local = {'address': 'mail-backup@testserver', 'subject_id': subject}
    grant = await call(
        app,
        'communication.internet_allow',
        {
            'address': remote['address'],
            'subject_id': remote['subject_id'],
            'public_key': b64(remote_key.public_key),
        },
        key=key,
        subject=subject,
        certs=(cert,),
    )
    assert grant.status == 'ok'
    envelope = make_envelope(remote, local, 'persistent cross-server message', remote_key, now=NOW)
    received = await call(
        app,
        'communication.internet_receive',
        {'envelope': envelope},
        rid=delivery_request_id(envelope),
    )
    assert received.status == 'ok', received.error
    async with app.metadata.transaction(write=False) as tx:
        original = mail_state(tx, subject)
    archive = tmp_path / 'internet.zip'
    saved = await backup(app, archive)
    packet = await capture(app, root, source_backup_sha256=saved['sha256'], sequence=1)
    pin = IndependentRecoveryPin(
        service=app.settings.service_url,
        public_key=root.public_key,
        digest=digest(packet['state']),
        sequence=1,
        source_backup_sha256=saved['sha256'],
    )
    restore(archive, tmp_path / 'restored-etc', tmp_path / 'restored-data', postgres_dsn=pg_dsn)
    restored = await open_for_proof(tmp_path / 'restored-etc')
    restored.clock = app.clock
    try:
        async with restored.metadata.transaction(write=False) as tx:
            assert mail_state(tx, subject) == original
        promoted = await promote(
            restored, packet, pin=pin, signer=root, operator='isolated-fixture'
        )
        assert promoted['status'] == 'recovery_promoted'
    finally:
        await restored.close()


@pytest.mark.parametrize(
    'address',
    ['alice@example.test:99999', 'alice@a..test', '../alice@example.test', 'alice@host/path'],
)
def test_invalid_addresses_fail_cleanly(address):
    from msg.core.errors import Failure
    from msg.core.internet_address import address_parts

    with pytest.raises(Failure, match='invalid_internet_address'):
        address_parts(address)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'change', ['redirect', 'foreign_delivery', 'wrong_subject', 'bad_key_list']
)
async def test_discovery_rejects_untrusted_remote_responses(change):
    from msg.core.errors import Failure
    from msg.core.internet_address import KEYS_REL, PROFILE_REL

    def remote(request):
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        assert request.url.host == 'remote.test'
        if change == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://other.test/'})
        if request.url.path == '/.well-known/webfinger':
            return httpx.Response(
                200,
                json={
                    'subject': 'acct:alice@elsewhere.test'
                    if change == 'wrong_subject'
                    else 'acct:alice@remote.test',
                    'properties': {SUBJECT_PROPERTY: 'u_remote'},
                    'links': [
                        {'rel': PROFILE_REL, 'href': 'https://remote.test/@alice'},
                        {'rel': KEYS_REL, 'href': 'https://remote.test/@alice/k'},
                        {
                            'rel': DELIVERY_REL,
                            'href': 'https://other.test/steal'
                            if change == 'foreign_delivery'
                            else 'https://remote.test/-/p/communication.internet_receive',
                        },
                    ],
                },
            )
        return httpx.Response(200, json={'subject_id': 'u_remote', 'keys': [None]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        with pytest.raises(Failure):
            await discover(http, 'alice@remote.test')


def test_terminal_transport_failure_prints_saved_retry_command():
    from msg.client_internet import retry_hint

    message_id = 'a' * 32
    text = retry_hint({
        'code': 'internet_transport_uncertain',
        'details': {'message_id': message_id, 'allow_http': True},
    })
    assert 'msg internet retry ' + message_id + ' --allow-http' in text
    assert retry_hint({'details': {'message_id': '../bad'}}) is None


def test_cross_server_receiver_is_explicitly_anonymous_in_its_contract():
    from types import SimpleNamespace

    settings = SimpleNamespace(
        server=SimpleNamespace(
            plugins=('identity', 'content', 'discussion', 'communication', 'discovery'),
        )
    )
    spec = Application(settings).registry.operation('communication.internet_receive')
    assert spec.anonymous_only and spec.effect == 'transaction' and not spec.require_signature


@pytest.mark.asyncio
async def test_contact_mutations_use_signatures_even_with_a_saved_api_key(tmp_path):
    from types import SimpleNamespace

    from msg.client import ClientState, MsgClient
    from msg.client_internet import run_command
    from msg.core.errors import Failure
    from msg.core.models import OperationResult, SignatureProof
    from msg.core.requests import signing_bytes
    from msg.security.crypto import Ed25519Signer, subject_id, verify

    signer = Ed25519Signer.generate()
    state = ClientState(tmp_path / 'client', server='https://home.test')
    state.save_signer(signer)
    state.data['subject_id'] = subject_id(signer.public_key)
    state.data['api_key'] = {'credential_id': 't_saved', 'value': b64(b't' * 32)}
    calls = []

    class Transport:
        server = state.server

        async def call(self, packet):
            assert isinstance(packet.proof, SignatureProof)
            verify(
                signer.public_key, signing_bytes(packet), packet.proof.signature, purpose='request'
            )
            calls.append(packet)
            return OperationResult(
                request_id=packet.request_id,
                operation=packet.operation,
                status='ok',
                actor=packet.subject,
                subject=packet.subject,
            )

    client = MsgClient(state, Transport())
    await run_command(client, SimpleNamespace(action='revoke', address='alice@remote.test'))
    await run_command(client, SimpleNamespace(action='delete', id='message-entry'))
    assert len(calls) == 2
    state.signer = None
    with pytest.raises(Failure, match='internet_identity_key_required'):
        await run_command(client, SimpleNamespace(action='revoke', address='alice@remote.test'))
