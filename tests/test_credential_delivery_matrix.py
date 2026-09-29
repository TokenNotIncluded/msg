"""Real adapters and durable state, including a second lost recovery response."""

import asyncio
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from test_service import NOW

from msg.application import Application
from msg.client import ClientState, MsgClient
from msg.core.codec import wire
from msg.core.errors import Failure
from msg.transports.client import GraphQLTransport, HTTPTransport, MCPHTTPTransport
from msg.transports.http import create_app

TRANSPORTS = (HTTPTransport, GraphQLTransport, MCPHTTPTransport)


def connect(app, directory, transport_type, clock):
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)))
    state = ClientState(directory, server=app.settings.service_url)
    transport = transport_type(state.server, http=http)
    return MsgClient(state, transport, clock=clock, retries=0), http


async def begin(client, kind):
    if kind == 'temporary':
        return await client.temporary()
    if kind == 'custodial':
        return await client.custodial('delivery-matrix')
    return await client.rotate_token()


@pytest.mark.parametrize('transport_type', TRANSPORTS)
@pytest.mark.parametrize('kind', ('temporary', 'custodial', 'rotate'))
async def test_two_lost_responses_restart_after_proof_expiry(
    installed, tmp_path, transport_type, kind
):
    app, _ = installed
    current = [NOW]

    def clock():
        return current[0]

    client, http = connect(app, tmp_path / 'client', transport_type, clock)
    if kind == 'rotate':
        assert (await client.temporary()).status == 'ok'
    original = client.transport.call
    issued = []

    async def discard(packet):
        result = await original(packet)
        assert result.status == 'ok', wire(result)
        issued.append(result.data['credential_id'])
        raise httpx.ReadTimeout('lost after release')

    client.transport.call = discard
    try:
        with pytest.raises(Failure, match='transport_uncertain'):
            await begin(client, kind)
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.recover_token()
        path, saved = client._token_journal()
        async with app.metadata.transaction(write=False) as tx:
            first = await tx.credential(issued[0])
            deadline = tx.one(
                'SELECT recovery_expires_at FROM token_deliveries WHERE credential_id=?',
                (first.id,),
            )[0]
        current[0] += timedelta(minutes=4)
        restarted = Application(app.settings, clock=clock)
        await restarted.load()
        recovered, other_http = connect(restarted, client.state.directory, transport_type, clock)
        try:
            replay = await recovered.recover_token()
            assert replay.error.code == 'token_delivery_unavailable', wire(replay)
            result = await recovered.recover_token()
            assert result.status == 'ok', wire(result)
            assert recovered.state.token[0] not in issued and not path.exists()
            async with restarted.metadata.transaction(write=False) as tx:
                latest = await tx.credential(recovered.state.token[0])
                assert latest.ceiling == first.ceiling and latest.expires_at == first.expires_at
                assert (
                    tx.one(
                        'SELECT recovery_expires_at FROM token_deliveries WHERE credential_id=?',
                        (latest.id,),
                    )[0]
                    == deadline
                )
                for cid in issued:
                    assert (await tx.credential(cid)).revoked_at is not None
        finally:
            await other_http.aclose()
            await restarted.close()
    finally:
        await http.aclose()


@pytest.mark.parametrize('transport_type', TRANSPORTS)
async def test_crash_after_recovered_token_saved_does_not_revoke_it(
    installed, tmp_path, monkeypatch, transport_type
):
    app, _ = installed
    client, http = connect(app, tmp_path / 'client', transport_type, lambda: NOW)
    original = client.transport.call

    async def discard(packet):
        await original(packet)
        raise httpx.ReadTimeout('lost')

    try:
        client.transport.call = discard
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.temporary()
        client.transport.call = original
        path, _ = client._token_journal()
        unlink = Path.unlink

        def crash(self, *args, **kwargs):
            if self == path:
                raise OSError('crash before unlink')
            return unlink(self, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, 'unlink', crash)
            with pytest.raises(OSError, match='crash before unlink'):
                await client.recover_token()
        accepted = client.state.token
        assert accepted and path.exists()
        restarted, other_http = connect(app, client.state.directory, transport_type, lambda: NOW)
        try:
            with pytest.raises(Failure, match='token_recovery_not_pending'):
                await restarted.recover_token()
            assert restarted.transport.calls == 0
            assert restarted.state.token == accepted and not path.exists()
            async with app.metadata.transaction(write=False) as tx:
                assert (await tx.credential(accepted[0])).revoked_at is None
        finally:
            await other_http.aclose()
    finally:
        await http.aclose()


async def test_missing_pending_key_does_not_generate_a_replacement(installed, tmp_path):
    app, _ = installed
    client, http = connect(app, tmp_path / 'client', HTTPTransport, lambda: NOW)

    async def discard(packet):
        raise httpx.ReadTimeout('before send')

    client.transport.call = discard
    try:
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.temporary()
        client.state.key_path.unlink()
        restarted, other_http = connect(app, client.state.directory, HTTPTransport, lambda: NOW)
        try:
            with pytest.raises(Failure, match='token_journal_key_missing'):
                await restarted.temporary()
            assert not restarted.state.key_path.exists() and restarted.transport.calls == 0
        finally:
            await other_http.aclose()
    finally:
        await http.aclose()


async def test_release_unavailable_still_returns_verifiable_commit(installed):
    from test_service import call, temporary_v3_args

    from msg.core.requests import receipt_bytes
    from msg.security.crypto import verify
    from msg.transports.packet import decode_result

    app, _ = installed
    args, rid, _, _ = temporary_v3_args()
    issued = await call(app, 'identity.temporary', args, rid=rid, contract_version=3)
    replay = await call(app, 'identity.temporary', args, rid=rid, contract_version=3)
    assert replay.error.code == 'token_delivery_unavailable'
    committed = decode_result(replay.data['committed_result'])
    assert (
        committed.request_id == issued.request_id and committed.committed_at == issued.committed_at
    )
    assert committed.subject == issued.subject and 'token' not in committed.data
    verify(
        app.receipt_signer.public_key,
        receipt_bytes(committed),
        committed.receipt,
        purpose='receipt',
    )


@pytest.mark.parametrize('transport_type', TRANSPORTS)
async def test_one_local_operation_and_stale_client_cannot_bootstrap_twice(
    installed, tmp_path, transport_type
):
    app, _ = installed
    client, http = connect(app, tmp_path / 'client', transport_type, lambda: NOW)
    stale, stale_http = connect(app, client.state.directory, transport_type, lambda: NOW)
    entered, release = asyncio.Event(), asyncio.Event()
    original = client.transport.call

    async def pause(packet):
        entered.set()
        await release.wait()
        return await original(packet)

    client.transport.call = pause
    task = asyncio.create_task(client.temporary())
    try:
        await entered.wait()
        with pytest.raises(Failure, match='identity_upgrade_busy'):
            await stale.temporary()
        assert stale.transport.calls == 0
        release.set()
        assert (await task).status == 'ok'
        with pytest.raises(Failure, match='identity_already_configured'):
            await stale.temporary()
        assert stale.transport.calls == 0 and stale.state.subject == client.state.subject
    finally:
        release.set()
        await task
        await http.aclose()
        await stale_http.aclose()


@pytest.mark.parametrize(
    'damage', ('symlink', 'hardlink', 'world_readable', 'oversize', 'server', 'key', 'malformed')
)
async def test_unsafe_journal_never_sends_recovery_material(installed, tmp_path, damage):
    from msg.core.codec import canonical
    from msg.security.crypto import Ed25519Signer
    from msg.storage.git import durable_write

    app, _ = installed
    client, http = connect(app, tmp_path / 'client', HTTPTransport, lambda: NOW)

    async def fail_before_send(packet):
        raise httpx.ReadTimeout('before send')

    client.transport.call = fail_before_send
    try:
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.temporary()
        path, saved = client._token_journal()
        if damage == 'symlink':
            backup = path.with_suffix('.old')
            path.rename(backup)
            path.symlink_to(backup)
        elif damage == 'hardlink':
            path.with_suffix('.link').hardlink_to(path)
        elif damage == 'world_readable':
            path.chmod(0o644)
        elif damage == 'oversize':
            path.write_bytes(b' ' * 8193)
        elif damage == 'server':
            saved['server'] = 'https://other.invalid'
            path.write_bytes(canonical(saved))
        elif damage == 'key':
            durable_write(client.state.key_path, Ed25519Signer.generate().private_bytes())
        else:
            path.write_bytes(b'[]')
        restarted, other_http = connect(app, client.state.directory, HTTPTransport, lambda: NOW)
        try:
            with pytest.raises(Failure):
                await restarted.recover_token()
            assert restarted.transport.calls == 0 and (path.exists() or path.is_symlink())
        finally:
            await other_http.aclose()
    finally:
        await http.aclose()


async def test_delivery_feature_doctor_and_isolated_selftest(installed):
    from msg.admin.diagnostics import doctor
    from msg.admin.token_delivery_check import check_token_delivery
    from msg.bootstrap import feature_manifest

    app, _ = installed
    feature = next(row for row in feature_manifest() if row['feature_id'] == 'credential_delivery')
    assert feature['doctor_check'] == 'credential_delivery'
    assert feature['selftest_case'] == 'credential_delivery_recovery'
    report = doctor(app.settings.config_dir, clock=lambda: NOW)
    assert report['checks']['credential_delivery']['ok']
    assert report['checks']['credential_delivery']['recovery_window_seconds'] == 900
    assert await check_token_delivery(app, NOW)


async def test_pending_upgrade_cannot_be_obstructed_by_a_new_rotation(installed, tmp_path):
    app, _ = installed
    client, http = connect(app, tmp_path / 'client', HTTPTransport, lambda: NOW)
    assert (await client.temporary()).status == 'ok'
    original = client.transport.call

    async def discard(packet):
        await original(packet)
        raise httpx.ReadTimeout('lost upgrade and status responses')

    client.transport.call = discard
    try:
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.upgrade('pending-upgrade')
        assert (client.state.directory / 'identity-upgrade.json').exists()
        client.transport.call = original
        before = client.transport.calls
        with pytest.raises(Failure, match='identity_recovery_pending'):
            await client.rotate_token()
        assert not (client.state.directory / 'token-rotation.json').exists()
        assert client.transport.calls == before
        assert (await client.upgrade()).status == 'ok'
    finally:
        await http.aclose()
