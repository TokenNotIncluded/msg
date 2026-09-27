"""All issuers share one durable release, recovery, batching and URL contract."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import gzip
import os
from uuid import uuid4

import httpx
import pytest

from msg.application import Application
from msg.core.codec import b64, canonical, loads, unb64, wire
from msg.core.models import Scope
from msg.core.requests import SECRET_DELIVERY_MIN_VERSION, request_for, receipt_bytes
from msg.security.crypto import verify
from msg.storage.git import durable_write
from msg.transports.client import HTTPTransport, GraphQLTransport, MCPHTTPTransport, PathGETTransport
from msg.transports.http import create_app
from msg.transports.packet import decode_packet, decode_result
from msg.core.errors import Failure
from test_service import NOW, call, register, temporary_v3_args

TRANSPORTS = (HTTPTransport, GraphQLTransport, MCPHTTPTransport)
ISSUERS = ('identity.temporary', 'identity.custodial_create', 'identity.token_rotate', 'identity.token_create')


def secret():
    return b64(os.urandom(32))


async def issuance(app, operation):
    args = {'nonce': secret(), 'recovery_secret': secret()}
    auth = {}
    rid = uuid4().hex
    if operation == 'identity.temporary':
        args, rid, _, _ = temporary_v3_args(request_id=rid)
    elif operation == 'identity.custodial_create':
        args['handle'] = 'contract-custody'
    elif operation == 'identity.token_rotate':
        initial, initial_id, _, _ = temporary_v3_args()
        result = await call(app, 'identity.temporary', initial, rid=initial_id, contract_version=3)
        assert result.status == 'ok'
        auth = {'subject': result.subject, 'token': (result.data['credential_id'], unb64(result.data['token']))}
    else:
        signer, subject, _ = await register(app, 'contract-signed')
        grant = next(g for g in app.primary_ceiling() if 'content.post_create@1' in g.operations)
        grant = replace(grant, scope=Scope(resource_id='t_main', descendants=True),
                        operations=frozenset({'content.post_create@1'}))
        args.update(ttl=3600, ceiling=[wire(grant)])
        auth = {'subject': subject, 'signer': signer}
    return request_for(operation, args, app.settings.service_url, **auth,
                       request_id=rid, contract_version=SECRET_DELIVERY_MIN_VERSION[operation],
                       expires_at=NOW+timedelta(seconds=120))


async def persisted_tables(app):
    async with app.metadata.transaction(write=False) as tx:
        return '\n'.join(str(row[0]) for table in
            ('results', 'events', 'credentials', 'token_deliveries', 'identities')
            for row in tx.rows('SELECT row_to_json(t)::text FROM '+table+' t'))


@pytest.mark.parametrize('operation', ISSUERS)
@pytest.mark.parametrize('transport_type', TRANSPORTS)
async def test_every_issuer_durable_release_recovery_race_and_expiry(installed, tmp_path, operation, transport_type, caplog):
    app, _ = installed
    packet = await issuance(app, operation)
    intention = tmp_path/'issuance.json'
    durable_write(intention, canonical(packet), mode=0o600)
    assert intention.stat().st_mode & 0o777 == 0o600
    # Commit without entering secret release, then start a new application.
    hook = app.executor.response_hook
    app.executor.response_hook = None
    try:
        committed = await app.executor.execute(packet)
    finally:
        app.executor.response_hook = hook
    assert committed.status == 'ok' and 'token' not in committed.data, wire(committed)
    current = [NOW]
    restarted = Application(app.settings, clock=lambda: current[0])
    await restarted.load()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(restarted)))
    transport = transport_type(restarted.settings.service_url, http=http)
    tokens = []
    recovery_secrets = [packet.arguments['recovery_secret']]
    try:
        restored = decode_packet(loads(intention.read_bytes()))
        results = await asyncio.gather(transport.call(restored), transport.call(restored))
        assert sorted(r.status for r in results) == ['error', 'ok'], [wire(r) for r in results]
        result = next(r for r in results if r.status == 'ok')
        exhausted = next(r for r in results if r.status == 'error')
        assert exhausted.error.code == 'token_delivery_unavailable'
        proof = decode_result(exhausted.data['committed_result'])
        verify(restarted.receipt_signer.public_key, receipt_bytes(proof), proof.receipt, purpose='receipt')
        tokens.append(result.data['token'])
        async with restarted.metadata.transaction(write=False) as tx:
            original = await tx.credential(result.data['credential_id'])
            deadline = tx.one('SELECT recovery_expires_at FROM token_deliveries WHERE credential_id=?', (original.id,))[0]
        credential_ids = [original.id]
        for index in range(2):
            args = {'credential_id': result.data['credential_id'], 'original_request_id': result.request_id,
                    'recovery_secret': recovery_secrets[-1], 'nonce': secret(), 'new_recovery_secret': secret()}
            request = request_for('identity.token_recover', args, restarted.settings.service_url,
                subject=result.subject, expires_at=NOW+timedelta(seconds=120))
            durable_write(tmp_path/f'recover-{index}.json', canonical(request), mode=0o600)
            recovery_secrets.append(args['new_recovery_secret'])
            # Competing attempts can only consume this predecessor once.
            competitor = request_for('identity.token_recover', {**args, 'nonce': secret()},
                restarted.settings.service_url, subject=result.subject,
                expires_at=NOW+timedelta(seconds=120))
            result = await transport.call(request)
            assert result.status == 'ok', wire(result)
            tokens.append(result.data['token'])
            credential_ids.append(result.data['credential_id'])
            replay, rejected = await asyncio.gather(transport.call(request), transport.call(competitor))
            assert replay.error.code == 'token_delivery_unavailable'
            assert rejected.error.code == 'recovery_unavailable'
            async with restarted.metadata.transaction(write=False) as tx:
                successor = await tx.credential(result.data['credential_id'])
                assert successor.ceiling == original.ceiling and successor.expires_at == original.expires_at
                assert tx.one('SELECT recovery_expires_at FROM token_deliveries WHERE credential_id=?', (successor.id,))[0] == deadline
                assert (await tx.credential(credential_ids[-2])).revoked_at is not None
        # A new short request expiry must never renew the 15-minute lineage.
        current[0] = NOW+timedelta(minutes=15)
        denied = await transport.call(request_for('identity.token_recover', {
            'credential_id': result.data['credential_id'], 'original_request_id': result.request_id,
            'recovery_secret': recovery_secrets[-1], 'nonce': secret(), 'new_recovery_secret': secret()},
            restarted.settings.service_url, subject=result.subject,
            expires_at=current[0]+timedelta(seconds=120)))
        assert denied.error.code == 'recovery_unavailable'
        stored = await persisted_tables(restarted)
        for value in [*tokens, *recovery_secrets, packet.arguments['nonce']]:
            assert value not in stored and value not in caplog.text
        assert 'token' not in proof.data
    finally:
        await http.aclose()
        await restarted.close()


@pytest.mark.parametrize('operation', tuple(SECRET_DELIVERY_MIN_VERSION))
@pytest.mark.parametrize('encoding', ('j', 'gz'))
async def test_path_client_and_hand_built_server_packet_refuse_secret_issuance(installed, operation, encoding):
    app, _ = installed
    packet = request_for(operation, {'nonce': secret(), 'recovery_secret': secret()},
        app.settings.service_url, contract_version=SECRET_DELIVERY_MIN_VERSION[operation],
        expires_at=NOW+timedelta(seconds=120))
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)))
    transport = PathGETTransport(app.settings.service_url, http=http)
    before = await persisted_tables(app)
    try:
        with pytest.raises(Failure, match='secure_channel_required'):
            await transport.call(packet)
        assert transport.calls == 0
        raw = canonical(packet)
        encoded = b64(gzip.compress(raw, mtime=0) if encoding == 'gz' else raw)
        response = await http.get(app.settings.service_url+'/-/g/'+operation+'/'+encoding+'/'+encoded)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "secure_channel_required"
        assert packet.arguments['recovery_secret'] not in response.text
        assert await persisted_tables(app) == before
        public = await http.get(app.settings.service_url+'/main')
        assert public.status_code == 200
    finally:
        await http.aclose()


@pytest.mark.parametrize('operation', tuple(SECRET_DELIVERY_MIN_VERSION))
@pytest.mark.parametrize('transport_type', TRANSPORTS)
async def test_plain_http_refuses_secret_before_any_network_request(operation, transport_type):
    transport = transport_type('http://example.invalid')
    packet = request_for(operation, {}, transport.server,
        contract_version=SECRET_DELIVERY_MIN_VERSION[operation],
        expires_at=NOW+timedelta(seconds=120))
    try:
        with pytest.raises(Failure, match='secure_channel_required'):
            await transport.call(packet)
        assert transport.calls == 0
    finally:
        await transport.close()


@pytest.mark.parametrize('operation', tuple(SECRET_DELIVERY_MIN_VERSION))
@pytest.mark.parametrize('batch', ('batch.atomic', 'batch.independent'))
async def test_every_secret_operation_is_excluded_from_both_batches(installed, operation, batch):
    app, _ = installed
    signer, subject, _ = await register(app, 'batch-boundary')
    child = request_for(operation, {}, app.settings.service_url, subject=subject, signer=signer,
                        contract_version=SECRET_DELIVERY_MIN_VERSION[operation],
                        expires_at=NOW+timedelta(seconds=120))
    before = await persisted_tables(app)
    result = await call(app, batch, {'requests': [wire(child)]}, key=signer, subject=subject)
    assert result.error.code == 'operation_not_batchable', wire(result)
    assert await persisted_tables(app) == before


@pytest.mark.parametrize('operation', ISSUERS)
async def test_no_recovery_material_or_revoked_predecessor_cannot_recover(installed, operation):
    app, _ = installed
    packet = await issuance(app, operation)
    issued = await app.executor.execute(packet)
    assert issued.status == 'ok'
    args = {'credential_id': issued.data['credential_id'], 'original_request_id': issued.request_id,
            'recovery_secret': packet.arguments['recovery_secret'],
            'nonce': secret(), 'new_recovery_secret': secret()}
    before = await persisted_tables(app)
    for changes in ({'recovery_secret': secret()}, {'original_request_id': 'unknown'},
                    {'recovery_secret': packet.arguments['nonce']}):
        result = await call(app, 'identity.token_recover', {**args, **changes}, subject=issued.subject)
        assert result.error.code == 'recovery_unavailable', wire(result)
        assert await persisted_tables(app) == before
    async with app.metadata.transaction(write=True) as tx:
        old = await tx.credential(issued.data['credential_id'])
        subject = await tx.subject(issued.subject)
        await tx.save_credential(replace(old, revoked_at=NOW), subject.auth_version)
    before = await persisted_tables(app)
    result = await call(app, 'identity.token_recover', args, subject=issued.subject)
    assert result.error.code == 'recovery_unavailable'
    assert await persisted_tables(app) == before


async def test_recovered_scoped_token_still_obeys_current_resource_permissions(installed):
    app, _ = installed
    packet = await issuance(app, 'identity.token_create')
    issued = await app.executor.execute(packet)
    assert issued.status == 'ok'
    recovered = await call(app, 'identity.token_recover', {
        'credential_id': issued.data['credential_id'], 'original_request_id': issued.request_id,
        'recovery_secret': packet.arguments['recovery_secret'],
        'nonce': secret(), 'new_recovery_secret': secret()}, subject=issued.subject)
    assert recovered.status == 'ok'
    token = (recovered.data['credential_id'], unb64(recovered.data['token']))
    allowed = await call(app, 'content.post_create', {'parent': '/main', 'body': 'scoped'},
                         subject=issued.subject, token=token)
    assert allowed.status == 'ok', wire(allowed)
    outside = await call(app, 'content.post_create', {'parent': '/intro', 'body': 'outside'},
                         subject=issued.subject, token=token)
    assert outside.error.code == 'credential_ceiling'
    async with app.metadata.transaction(write=True) as tx:
        current = await tx.resource('t_main')
        await tx.replace(replace(current, mode=0o555, generation=current.generation+1), current.generation)
    revoked = await call(app, 'content.post_create', {'parent': '/main', 'body': 'revoked'},
                         subject=issued.subject, token=token)
    assert revoked.error.code == 'permission_denied', wire(revoked)
