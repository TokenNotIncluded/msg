"""The default client keeps one-use token recovery material out of URLs and output."""

import httpx
import pytest
from test_service import NOW

from msg.client import ClientState, MsgClient
from msg.core.codec import loads
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport, PathGETTransport
from msg.transports.http import create_app


def client_for(app, directory, http, *, retries=0):
    state = ClientState(directory, server=app.settings.service_url)
    transport = HTTPTransport(app.settings.service_url, http=http)
    return state, transport, MsgClient(state, transport, clock=lambda: NOW, retries=retries)


@pytest.mark.asyncio
async def test_bootstrap_lost_after_claim_recovers_after_restart(installed, tmp_path):
    app, _ = installed
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    )
    state, transport, client = client_for(app, tmp_path / 'client', http)
    original = transport.call
    seen = []

    async def lose_response(packet):
        seen.append(packet)
        result = await original(packet)
        if packet.operation == 'identity.temporary':
            raise httpx.ReadTimeout('claimed response lost')
        return result

    transport.call = lose_response
    try:
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.temporary()
        journal = state.directory / 'temporary.json'
        saved = loads(journal.read_bytes())
        assert journal.stat().st_mode & 0o777 == 0o600
        assert saved['contract_version'] == 3 and len(saved['recovery_secret']) >= 43
        assert state.signer is not None and state.encryption_recipient is not None
        assert state.token is None and state.subject is None
        restarted_state, restarted_transport, restarted = client_for(app, state.directory, http)
        replay = await restarted.temporary()
        assert replay.error.code == 'token_delivery_unavailable'
        restored = await restarted.recover_token()
        assert restored.status == 'ok' and restarted_state.token is not None
        assert restored.data['credential_id'] != saved['credential_id']
        assert not journal.exists()
        assert all(packet.contract_version == 3 for packet in seen)
        post = await restarted.call('content.post_create', {'parent': '/main', 'body': 'recovered'})
        assert post.status == 'ok'
        with pytest.raises(Failure, match='token_recovery_not_pending'):
            await restarted.recover_token()
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_rotate_lost_response_blocks_old_token_until_recovery(installed, tmp_path):
    app, _ = installed
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    )
    state, transport, client = client_for(app, tmp_path / 'client', http)
    try:
        created = await client.temporary()
        assert created.status == 'ok'
        old = state.token
        original = transport.call

        async def lose_rotate(packet):
            result = await original(packet)
            if packet.operation == 'identity.token_rotate':
                raise httpx.ReadTimeout('claim lost')
            return result

        transport.call = lose_rotate
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.rotate_token()
        assert state.token == old
        with pytest.raises(Failure, match='token_rotation_pending'):
            await client.call('content.post_create', {'parent': '/main', 'body': 'old'})
        restarted_state, _, restarted = client_for(app, state.directory, http)
        recovered = await restarted.recover_token()
        assert recovered.status == 'ok' and restarted_state.token[0] != old[0]
        assert not (state.directory / 'token-rotation.json').exists()
        posted = await restarted.call('content.post_create', {'parent': '/main', 'body': 'new'})
        assert posted.status == 'ok'
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_before_commit_retry_and_path_transport_fail_closed(installed, tmp_path):
    app, _ = installed
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    )
    state, transport, client = client_for(app, tmp_path / 'client', http)
    original = transport.call

    async def fail_before_send(packet):
        raise httpx.ReadTimeout('not sent')

    transport.call = fail_before_send
    try:
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.temporary()
        saved = loads((state.directory / 'temporary.json').read_bytes())
        transport.call = original
        resumed = await client.temporary()
        assert resumed.status == 'ok' and resumed.data['credential_id'] == saved['credential_id']
        assert not (state.directory / 'temporary.json').exists()
        other = ClientState(tmp_path / 'path', server=app.settings.service_url)
        path_client = MsgClient(
            other, PathGETTransport(app.settings.service_url, http=http), clock=lambda: NOW
        )
        with pytest.raises(Failure, match='secure_channel_required'):
            await path_client.temporary()
        assert not (other.directory / 'temporary.json').exists()
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_recovery_claim_lost_can_recover_again_without_version_one(installed, tmp_path):
    app, _ = installed
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    )
    state, transport, client = client_for(app, tmp_path / 'client', http)
    original = transport.call
    lost = {'identity.temporary', 'identity.token_recover'}
    packets = []

    async def lose_claim(packet):
        packets.append(packet)
        result = await original(packet)
        if packet.operation in lost:
            lost.remove(packet.operation)
            raise httpx.ReadTimeout('claimed response lost')
        return result

    transport.call = lose_claim
    try:
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.temporary()
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.recover_token()
        persisted = loads((state.directory / 'temporary.json').read_bytes())
        assert persisted['recovery']['new_recovery_secret'] != persisted['recovery_secret']
        first = await client.recover_token()
        assert first.error.code == 'token_delivery_unavailable'
        advanced = loads((state.directory / 'temporary.json').read_bytes())
        assert advanced['credential_id'] == persisted['recovery']['credential_id']
        second = await client.recover_token()
        assert second.status == 'ok' and state.token[0] != advanced['credential_id']
        assert not (state.directory / 'temporary.json').exists()
        assert [p.contract_version for p in packets if p.operation == 'identity.temporary'] == [3]
        assert all(p.proof is None for p in packets if p.operation == 'identity.token_recover')
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_non_loopback_http_never_sends_recovery_secret(tmp_path):
    server = 'http://light.local:18144'
    state = ClientState(tmp_path / 'remote', server=server)
    transport = HTTPTransport(server)
    client = MsgClient(state, transport)
    try:
        with pytest.raises(Failure, match='secure_channel_required'):
            await client.temporary()
        assert not (state.directory / 'temporary.json').exists() and transport.calls == 0
    finally:
        await transport.close()
