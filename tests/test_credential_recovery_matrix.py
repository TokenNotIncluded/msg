"""Persistent credential delivery boundaries across the actual protocol adapters."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_service import NOW

from msg.application import Application
from msg.client import ClientState, MsgClient
from msg.client_tokens import JOURNALS, pending_delivery
from msg.core.codec import canonical, decode, loads, wire
from msg.core.errors import Failure
from msg.core.models import OperationResult
from msg.core.requests import SECRET_DELIVERY_MIN_VERSION, receipt_bytes, request_for
from msg.security.crypto import verify
from msg.transports.client import (
    GraphQLTransport,
    HTTPTransport,
    MCPHTTPTransport,
    PathGETTransport,
)
from msg.transports.http import create_app

TRANSPORTS = (HTTPTransport, GraphQLTransport, MCPHTTPTransport)


@pytest.fixture
async def delivery_system(installed, tmp_path):
    initial, _ = installed
    settings = initial.settings
    await initial.close()
    system = SimpleNamespace(now=NOW, app=None, packets=[], dropped=[], directory=tmp_path/'client')

    async def restart():
        if system.app is not None:
            await system.app.close()
        system.app = await Application(settings, clock=lambda: system.now).load()
        system.asgi = create_app(system.app)

    await restart()

    async def dispatch(scope, receive, send):
        await system.asgi(scope, receive, send)

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=dispatch), base_url=settings.service_url)

    def client(transport_type=HTTPTransport):
        state = ClientState(system.directory, server=settings.service_url)
        transport = transport_type(settings.service_url, http=http)
        original = transport.call

        async def observe(packet):
            system.packets.append(packet)
            if system.dropped and system.dropped[0] == ('before', packet.operation):
                system.dropped.pop(0)
                raise httpx.ReadTimeout('before submission')
            result = await original(packet)
            if system.dropped and system.dropped[0] == ('after', packet.operation):
                system.dropped.pop(0)
                assert result.status == 'ok', wire(result)
                raise httpx.ReadTimeout('committed response discarded')
            return result

        transport.call = observe
        return MsgClient(state, transport, clock=lambda: system.now, retries=0)

    system.client = client
    system.restart = restart
    try:
        yield system
    finally:
        await http.aclose()
        await system.app.close()


async def begin(system, kind, transport_type=HTTPTransport):
    client = system.client(transport_type)
    if kind == 'rotate':
        assert (await client.temporary()).status == 'ok'
    elif kind == 'create':
        assert (await client.register('matrix-signer')).status == 'ok'
    operation = {'temporary': 'identity.temporary', 'custodial': 'identity.custodial_create',
                 'rotate': 'identity.token_rotate', 'create': 'identity.token_create'}[kind]
    return client, operation


async def issue(client, kind):
    if kind == 'temporary':
        return await client.temporary()
    if kind == 'custodial':
        return await client.custodial('matrix-custodial')
    if kind == 'rotate':
        return await client.rotate_token()
    return await client.create_token(ceiling=[], ttl=1200)


async def delivery_rows(app):
    async with app.metadata.transaction(write=False) as tx:
        return tx.execute('SELECT credential_id,subject,request_id,recovery_expires_at,claimed_at,consumed_at '
                      'FROM token_deliveries ORDER BY credential_id').fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize('transport_type', TRANSPORTS)
@pytest.mark.parametrize('kind', ('temporary', 'custodial', 'rotate', 'create'))
async def test_repeated_lost_response_recovers_after_proof_expiry(delivery_system, kind, transport_type):
    s = delivery_system
    client, operation = await begin(s, kind, transport_type)
    s.dropped = [('after', operation), ('after', 'identity.token_recover')]
    with pytest.raises(Failure, match='transport_uncertain'):
        await issue(client, kind)
    original_rows = await delivery_rows(s.app)
    pending = loads((s.directory/JOURNALS[operation]).read_bytes())
    async with s.app.metadata.transaction(write=False) as tx:
        original_credential = await tx.credential(pending['credential_id'])
    with pytest.raises(Failure, match='transport_uncertain'):
        await client.recover_token()
    first_recovery = s.packets[-1]
    s.now += timedelta(minutes=4)  # Request proof expires, but the 15-minute delivery window does not.
    await s.restart()
    restarted = s.client(transport_type)
    replay = await restarted.recover_token()
    assert replay.status == 'error' and replay.error.code == 'token_delivery_unavailable', wire(replay)
    assert s.packets[-1].request_id == first_recovery.request_id
    assert s.packets[-1].payload_digest == first_recovery.payload_digest
    assert s.packets[-1].expires_at > first_recovery.expires_at
    recovered = await restarted.recover_token()
    assert recovered.status == 'ok', wire(recovered)
    if kind == 'create':
        assert restarted.state.token is None  # Minting must not replace the signing identity.
        saved = s.directory/('credential-' + recovered.data['credential_id'] + '.json')
        assert loads(saved.read_bytes())['token'] == recovered.data['token']
        assert saved.stat().st_mode & 0o777 == 0o600
    else:
        assert restarted.state.token is not None
    rows = await delivery_rows(s.app)
    # Every descendant retains the original deadline; retry never restarts the window.
    assert {row[3] for row in rows} == {row[3] for row in original_rows}
    async with s.app.metadata.transaction(write=False) as tx:
        credentials = [await tx.credential(row[0]) for row in rows]
    active = [c for c in credentials if c.revoked_at is None]
    assert len(active) == 1
    assert active[0].ceiling == original_credential.ceiling
    assert active[0].expires_at == original_credential.expires_at
    assert all(c.revoked_at is not None for c in credentials if c.id != active[0].id)
    assert not any(s.directory.glob('*-rotation.json')) and not (s.directory/'temporary.json').exists()
    assert not (s.directory/'custodial-bootstrap.json').exists()
    assert not (s.directory/'token-create.json').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ('temporary', 'custodial', 'rotate', 'create'))
async def test_precommit_retry_refreshes_only_proof_expiry(delivery_system, kind):
    s = delivery_system
    client, operation = await begin(s, kind)
    s.dropped = [('before', operation)]
    with pytest.raises(Failure, match='transport_uncertain'):
        await issue(client, kind)
    original = s.packets[-1]
    s.now += timedelta(minutes=4)
    await s.restart()
    result = await issue(s.client(), kind)
    assert result.status == 'ok', wire(result)
    retried = s.packets[-1]
    assert (retried.request_id, retried.payload_digest) == (original.request_id, original.payload_digest)
    assert retried.expires_at > original.expires_at


@pytest.mark.asyncio
async def test_lost_release_returns_verifiable_nonsecret_commit_receipt(delivery_system):
    s = delivery_system
    client, operation = await begin(s, 'temporary')
    s.dropped = [('after', operation)]
    with pytest.raises(Failure, match='transport_uncertain'):
        await client.temporary()
    before = await delivery_rows(s.app)
    replay = await s.client().temporary()
    assert replay.error.code == 'token_delivery_unavailable'
    committed = decode(OperationResult, replay.data['committed_result'])
    assert committed.status == 'ok' and committed.request_id == replay.request_id
    assert committed.subject == replay.subject and committed.committed_at == NOW
    assert 'token' not in committed.data
    verify(s.app.receipt_signer.public_key, receipt_bytes(committed), committed.receipt, purpose='receipt')
    assert await delivery_rows(s.app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ('temporary', 'custodial', 'rotate', 'create'))
async def test_recovery_post_local_save_crash_does_not_revoke_accepted_successor(delivery_system, monkeypatch, kind):
    s = delivery_system
    client, operation = await begin(s, kind)
    s.dropped = [('after', operation)]
    with pytest.raises(Failure, match='transport_uncertain'):
        await issue(client, kind)
    unlink = Path.unlink
    def crash(path, *args, **kwargs):
        if path == s.directory/JOURNALS[operation]:
            raise SystemExit('crash after saving client identity')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', crash)
        with pytest.raises(SystemExit):
            await client.recover_token()
    accepted = client.state.token
    if kind == 'create':
        assert accepted is None
        saved = next(s.directory.glob('credential-*.json')).read_bytes()
    else:
        assert accepted is not None
    before = await delivery_rows(s.app)
    count = len(s.packets)
    restarted = s.client()
    with pytest.raises(Failure, match='token_recovery_not_pending'):
        await restarted.recover_token()
    assert restarted.state.token == accepted and len(s.packets) == count
    assert await delivery_rows(s.app) == before
    assert not (s.directory/JOURNALS[operation]).exists()

    if kind == 'create':
        assert next(s.directory.glob('credential-*.json')).read_bytes() == saved


@pytest.mark.asyncio
async def test_same_directory_concurrent_issue_has_one_local_owner(delivery_system):
    s = delivery_system
    first, second = s.client(), s.client()
    entered, release = asyncio.Event(), asyncio.Event()
    original = first.transport.call
    async def hold(packet):
        entered.set()
        await release.wait()
        return await original(packet)
    first.transport.call = hold
    task = asyncio.create_task(first.temporary())
    await entered.wait()
    try:
        with pytest.raises(Failure, match='token_operation_busy'):
            await second.temporary()
    finally:
        release.set()
    assert (await task).status == 'ok'
    assert len(await delivery_rows(s.app)) == 1


@pytest.mark.parametrize('transport_type', TRANSPORTS)
@pytest.mark.parametrize('kind', ('temporary', 'custodial', 'rotate', 'create'))
async def test_preclaim_commit_restart_then_concurrent_delivery_once(delivery_system, kind, transport_type):
    s = delivery_system
    client, operation = await begin(s, kind, transport_type)
    s.app.executor.response_hook = None  # Business committed, release has not happened.
    s.dropped = [('after', operation)]
    with pytest.raises(Failure, match='transport_uncertain'):
        await issue(client, kind)
    packet = s.packets[-1]
    await s.restart()
    transport = s.client(transport_type).transport
    outcomes = await asyncio.gather(*(transport.call(packet) for _ in range(3)))
    assert sorted(r.status for r in outcomes) == ['error', 'error', 'ok']
    winner = next(r for r in outcomes if r.status == 'ok')
    assert winner.data['token']
    for result in outcomes:
        if result.status == 'error':
            assert result.error.code == 'token_delivery_unavailable'
            assert winner.data['token'] not in canonical(result).decode()
            committed = decode(OperationResult, result.data['committed_result'])
            verify(s.app.receipt_signer.public_key, receipt_bytes(committed), committed.receipt,
                   purpose='receipt')
    async with s.app.metadata.transaction(write=False) as tx:
        row = tx.one('SELECT claimed_at FROM token_deliveries WHERE credential_id=?',
                     (winner.data['credential_id'],))
        assert row[0] is not None


@pytest.mark.parametrize('transport_type', TRANSPORTS)
@pytest.mark.parametrize('kind', ('temporary', 'custodial', 'rotate', 'create'))
@pytest.mark.parametrize('lose_recovery', (False, True))
async def test_original_deadline_expires_without_recovery_writes(delivery_system, kind, transport_type, lose_recovery):
    s = delivery_system
    client, operation = await begin(s, kind, transport_type)
    s.dropped = [('after', operation)]
    with pytest.raises(Failure, match='transport_uncertain'):
        await issue(client, kind)
    if lose_recovery:
        s.now += timedelta(minutes=14)
        s.dropped = [('after', 'identity.token_recover')]
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.recover_token()
    s.now = NOW + timedelta(minutes=15)  # Boundary is exclusive, even for a just-created descendant.
    await s.restart()
    before = await delivery_rows(s.app)
    client = s.client(transport_type)
    original_journal = loads((s.directory/JOURNALS[operation]).read_bytes())
    result = await client.recover_token()
    assert result.status == 'error' and result.error.code == 'recovery_unavailable', wire(result)
    assert await delivery_rows(s.app) == before
    assert (s.directory/JOURNALS[operation]).exists()
    assert original_journal['recovery_secret'] not in canonical(result).decode()
    assert pending_delivery(client.state)['request_id'] == original_journal['request_id']


@pytest.mark.parametrize('kind', ('temporary', 'custodial', 'rotate', 'create'))
async def test_live_ceiling_is_not_overridden_by_recovery(delivery_system, kind):
    s = delivery_system
    client, operation = await begin(s, kind)
    s.dropped = [('after', operation)]
    with pytest.raises(Failure, match='transport_uncertain'):
        await issue(client, kind)
    journal = loads((s.directory/JOURNALS[operation]).read_bytes())
    async with s.app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(journal['credential_id'])
        subject = await tx.subject(credential.subject_id)
        await tx.save_credential(replace(credential, ceiling=()), subject.auth_version)
    recovered = await client.recover_token()
    assert recovered.status == 'ok', wire(recovered)
    async with s.app.metadata.transaction(write=False) as tx:
        assert (await tx.credential(recovered.data['credential_id'])).ceiling == ()


@pytest.mark.parametrize('kind', ('temporary', 'custodial', 'rotate', 'create'))
async def test_revoked_issuance_cannot_be_recovered(delivery_system, kind):
    s = delivery_system
    client, operation = await begin(s, kind)
    s.dropped = [('after', operation)]
    with pytest.raises(Failure, match='transport_uncertain'):
        await issue(client, kind)
    pending = loads((s.directory/JOURNALS[operation]).read_bytes())
    async with s.app.metadata.transaction(write=True) as tx:
        current = await tx.credential(pending['credential_id'])
        subject = await tx.subject(current.subject_id)
        await tx.save_credential(replace(current, revoked_at=s.now), subject.auth_version)
    before = await delivery_rows(s.app)
    denied = await client.recover_token()
    assert denied.error.code == 'recovery_unavailable'
    assert await delivery_rows(s.app) == before


@pytest.mark.parametrize('kind', ('temporary', 'custodial', 'rotate', 'create'))
@pytest.mark.parametrize('transport_type', TRANSPORTS)
async def test_concurrent_recovery_request_releases_only_one_successor(delivery_system, kind, transport_type):
    s = delivery_system
    client, operation = await begin(s, kind, transport_type)
    s.dropped = [('after', operation), ('before', 'identity.token_recover')]
    with pytest.raises(Failure, match='transport_uncertain'):
        await issue(client, kind)
    with pytest.raises(Failure, match='transport_uncertain'):
        await client.recover_token()
    packet = s.packets[-1]
    transport = s.client(transport_type).transport
    outcomes = await asyncio.gather(*(transport.call(packet) for _ in range(3)))
    assert sorted(r.status for r in outcomes) == ['error', 'error', 'ok']
    winner = next(r for r in outcomes if r.status == 'ok')
    assert winner.data['previous_credential'] == packet.arguments['credential_id']
    for result in outcomes:
        if result.status == 'error':
            assert result.error.code == 'token_delivery_unavailable'
            assert winner.data['token'] not in canonical(result).decode()
    async with s.app.metadata.transaction(write=False) as tx:
        old = await tx.credential(packet.arguments['credential_id'])
        new = await tx.credential(winner.data['credential_id'])
        assert old.revoked_at is not None and new.revoked_at is None
        assert old.ceiling == new.ceiling and old.expires_at == new.expires_at


@pytest.mark.parametrize('kind', ('temporary', 'custodial', 'rotate', 'create'))
async def test_path_get_and_batch_never_dispatch_secret_issuance(delivery_system, kind):
    s = delivery_system
    client, operation = await begin(s, kind)
    s.dropped = [('before', operation)]
    with pytest.raises(Failure, match='transport_uncertain'):
        await issue(client, kind)
    packet = s.packets[-1]
    transport = PathGETTransport(s.app.settings.service_url)
    before = await delivery_rows(s.app)
    try:
        with pytest.raises(Failure, match='secure_channel_required'):
            await transport.call(packet)
        assert transport.calls == 0
    finally:
        await transport.close()
    from msg.plugins.batch import packets
    for name in ('batch.atomic', 'batch.independent'):
        parent = request_for(name, {'requests': [wire(packet)]}, client.state.server,
                             subject=packet.subject)
        with pytest.raises(Failure, match='operation_not_batchable'):
            packets(s.app.registry, parent, packet.subject)
    assert await delivery_rows(s.app) == before


@pytest.mark.parametrize('transport_type', TRANSPORTS)
@pytest.mark.parametrize('operation', tuple(SECRET_DELIVERY_MIN_VERSION))
async def test_plain_http_refuses_secret_before_any_network_request(tmp_path, transport_type, operation):
    transport = transport_type('http://example.invalid')
    packet = request_for(operation, {}, transport.server,
                         contract_version=SECRET_DELIVERY_MIN_VERSION[operation])
    try:
        with pytest.raises(Failure, match='secure_channel_required'):
            await transport.call(packet)
        assert transport.calls == 0
    finally:
        await transport.close()
