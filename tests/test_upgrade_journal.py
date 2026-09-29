"""Real signed temporary upgrades remain recoverable across lost responses."""

import hashlib
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_service import NOW, call, register

from msg.application import Application
from msg.client import ClientState, MsgClient
from msg.core.codec import b64, canonical, loads, wire
from msg.core.errors import Failure
from msg.core.models import Credential
from msg.plugins import identity
from msg.security.crypto import Ed25519Signer
from msg.transports.client import (
    GraphQLTransport,
    HTTPTransport,
    MCPHTTPTransport,
    PathGETTransport,
)
from msg.transports.http import create_app


async def legacy_state(app, directory):
    subject = 'u_tmp_legacy_' + uuid4().hex[:20]
    token = os.urandom(32)
    credential = 't_' + subject[2:]
    async with app.metadata.transaction(write=True) as tx:
        await identity.make_user(
            app, tx, SimpleNamespace(now=NOW), subject, 'tmp-' + subject[-20:], 'temporary'
        )
        await tx.save_credential(
            Credential(
                id=credential,
                subject_id=subject,
                kind='token',
                verifier=hashlib.sha256(token).digest(),
                ceiling=app.temporary_ceiling(),
                not_before=NOW,
                expires_at=NOW + timedelta(hours=1),
                revoked_at=None,
            ),
            0,
        )
    state = ClientState(directory, server=app.settings.service_url)
    state.data.update(
        subject_id=subject,
        token={
            'credential_id': credential,
            'value': b64(token),
            'expires_at': wire(NOW + timedelta(hours=1)),
        },
    )
    state._save()
    post = await call(
        app,
        'content.post_create',
        {'parent': '/tmp', 'body': 'original legacy work'},
        subject=subject,
        token=(credential, token),
    )
    assert post.status == 'ok', wire(post)
    async with app.metadata.transaction(write=False) as tx:
        history = tx.one('SELECT body FROM revisions WHERE id=?', (post.resources[0].revision,))[0]
        memberships = tx.rows('SELECT * FROM memberships WHERE subject=? ORDER BY org', (subject,))
    return state, (credential, token), post.resources[0], history, memberships


@asynccontextmanager
async def connected(app, state, transport_type=HTTPTransport, clock=lambda: NOW):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=state.server
    ) as http:
        transport = transport_type(state.server, http=http)
        yield MsgClient(state, transport, clock=clock, retries=0)


@pytest.mark.parametrize('transport_type', [HTTPTransport, GraphQLTransport, MCPHTTPTransport])
async def test_upgrade_journal_exists_before_send_and_recovers_committed_loss(
    installed, tmp_path, monkeypatch, transport_type
):
    app, _ = installed
    state, token, post, history, memberships = await legacy_state(app, tmp_path / 'client')
    packets = []
    async with connected(app, state, transport_type) as client:
        original = client.transport.call

        async def lost(packet):
            packets.append(packet)
            if packet.operation == 'identity.upgrade':
                journal = state.directory / 'identity-upgrade.json'
                assert journal.is_file(), 'upgrade intention must be durable before sending'
                saved = loads(journal.read_bytes())
                assert saved['request_id'] == packet.request_id
                assert journal.stat().st_mode & 0o777 == 0o600
                assert state.key_path.is_file() and state.age_key_path.is_file()
                assert b64(token[1]) not in journal.read_text()
                result = await original(packet)
                assert result.status == 'ok', wire(result)
                raise httpx.ReadTimeout('response deliberately discarded')
            return await original(packet)

        monkeypatch.setattr(client.transport, 'call', lost)
        result = await client.upgrade('recovered-agent')
        assert result.status == 'ok', wire(result)
        assert any(packet.operation == 'identity.upgrade_result' for packet in packets)
        assert state.subject == result.subject == result.data['subject_id']
        assert state.token is None
        assert not (state.directory / 'identity-upgrade.json').exists()
        assert result.data['key_id'] == state.signer.key_id
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(post.id)).owner == state.subject
        assert tx.one('SELECT body FROM revisions WHERE id=?', (post.revision,))[0] == history
        assert (
            tx.rows('SELECT * FROM memberships WHERE subject=? ORDER BY org', (state.subject,))
            == memberships
        )
        assert (await tx.credential(token[0])).revoked_at is not None
        assert (
            tx.one('SELECT COUNT(*) FROM identity_keys WHERE subject=?', (state.subject,))[0] == 1
        )
        assert (
            tx.one('SELECT COUNT(*) FROM encryption_subkeys WHERE subject=?', (state.subject,))[0]
            == 1
        )
    denied = await call(app, 'discovery.get', {'id': post.id}, subject=state.subject, token=token)
    assert denied.error.code == 'credential_revoked'


async def test_second_lost_response_and_server_client_restart_use_new_key_only(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    state, token, post, history, _ = await legacy_state(app, tmp_path / 'client')
    seen = []
    async with connected(app, state) as client:
        original = client.transport.call

        async def lost(packet):
            seen.append(packet)
            result = await original(packet)
            if packet.operation in {'identity.upgrade', 'identity.upgrade_result'}:
                assert result.status == 'ok', wire(result)
                raise httpx.ReadTimeout('response deliberately discarded')
            return result

        monkeypatch.setattr(client.transport, 'call', lost)
        with pytest.raises(Failure, match='transport_uncertain'):
            await client.upgrade('restart-agent')
    journal = state.directory / 'identity-upgrade.json'
    pending = journal.read_bytes()
    signing_bytes, age_bytes = state.key_path.read_bytes(), state.age_key_path.read_bytes()
    await app.close()
    later = NOW + timedelta(hours=2)
    restarted = Application(app.settings, clock=lambda: later)
    await restarted.load()
    try:
        state = ClientState(state.directory)
        async with connected(restarted, state, clock=lambda: later) as resumed:
            sent = []
            original = resumed.transport.call

            async def observed(packet):
                sent.append(packet)
                return await original(packet)

            monkeypatch.setattr(resumed.transport, 'call', observed)
            result = await resumed.upgrade('restart-agent')
            assert result.status == 'ok', wire(result)
            assert all(packet.operation == 'identity.upgrade_result' for packet in sent)
            assert result.data['upgrade_request_id'] == loads(pending)['request_id']
            assert result.data['status'] == 'completed'
        assert state.token is None and not journal.exists()
        assert (
            state.key_path.read_bytes() == signing_bytes
            and state.age_key_path.read_bytes() == age_bytes
        )
        async with restarted.metadata.transaction(write=False) as tx:
            assert (await tx.credential(token[0])).revoked_at is not None
            assert tx.one('SELECT body FROM revisions WHERE id=?', (post.revision,))[0] == history
    finally:
        await restarted.close()


async def test_precommit_crash_retries_the_same_intent_not_a_new_subject(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    state, _, _, _, _ = await legacy_state(app, tmp_path / 'client')
    async with connected(app, state) as client:

        async def crash(packet):
            assert (state.directory / 'identity-upgrade.json').is_file()
            raise RuntimeError('simulated process death before network send')

        monkeypatch.setattr(client.transport, 'call', crash)
        with pytest.raises(RuntimeError, match='simulated process death'):
            await client.upgrade('before-send')
    pending = loads((state.directory / 'identity-upgrade.json').read_bytes())
    keys = state.key_path.read_bytes(), state.age_key_path.read_bytes()
    async with connected(app, ClientState(state.directory)) as client:
        result = await client.upgrade('before-send')
        assert result.status == 'ok', wire(result)
        assert result.request_id == pending['request_id']
        assert result.subject == pending['subject_id']
    assert keys == (state.key_path.read_bytes(), state.age_key_path.read_bytes())


async def completed_upgrade(app, directory, monkeypatch):
    state, token, post, history, groups = await legacy_state(app, directory)
    async with connected(app, state) as client:
        original = client.transport.call
        requests = []

        async def observed(packet):
            requests.append(packet)
            return await original(packet)

        monkeypatch.setattr(client.transport, 'call', observed)
        result = await client.upgrade('completed-agent')
        assert result.status == 'ok', wire(result)
    request_id = next(p.request_id for p in requests if p.operation == 'identity.upgrade')
    return state, token, request_id, result


async def facts(app):
    async with app.metadata.transaction(write=False) as tx:
        tables = (
            'identities',
            'credentials',
            'certificates',
            'identity_keys',
            'encryption_subkeys',
            'resources',
            'revisions',
            'memberships',
            'events',
            'results',
            'jobs',
        )
        return {table: sorted(tx.rows('SELECT * FROM ' + table), key=repr) for table in tables}


async def test_status_requires_exact_current_new_key_and_is_a_pure_read(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    state, old_token, request_id, completed = await completed_upgrade(
        app, tmp_path / 'client', monkeypatch
    )
    other, other_subject, _ = await register(app, 'other-agent')
    arguments = {'upgrade_request_id': request_id}
    before = await facts(app)
    async with connected(app, state) as client:
        for _ in range(3):
            status = await client.call('identity.upgrade_result', arguments, certificates=())
            assert status.status == 'ok', wire(status)
            assert status.data['certificate_id'] == completed.data['certificate_id']
            assert status.data['key_id'] == state.signer.key_id
            assert status.data['upgrade_committed_at'] == wire(completed.committed_at)
            assert set(status.data) == {
                'subject_id',
                'key_id',
                'encryption_key_id',
                'encryption_recipient',
                'certificate_id',
                'handle',
                'status',
                'upgrade_request_id',
                'upgrade_committed_at',
            }
        for packet in (
            client.prepare('identity.upgrade_result', arguments, anonymous=True),
            client.prepare(
                'identity.upgrade_result',
                arguments,
                signer=other,
                subject=state.subject,
                certificates=(),
            ),
            client.prepare(
                'identity.upgrade_result', arguments, expires_at=NOW - timedelta(seconds=1)
            ),
        ):
            denied = await client.send(packet)
            assert denied.status == 'error' and denied.data is None, wire(denied)
        unrelated = await call(
            app, 'identity.upgrade_result', arguments, key=other, subject=other_subject
        )
        assert unrelated.error.code == 'upgrade_not_completed' and unrelated.data is None
        missing = await client.call(
            'identity.upgrade_result', {'upgrade_request_id': 'unknown-request'}
        )
        assert missing.error.code == 'upgrade_not_completed' and missing.data is None
        revoked = await call(
            app, 'identity.upgrade_result', arguments, subject=state.subject, token=old_token
        )
        assert revoked.status == 'error' and revoked.data is None
    assert await facts(app) == before


async def test_another_valid_key_is_not_the_upgrade_key_and_revocation_is_current(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    state, _, request_id, _ = await completed_upgrade(app, tmp_path / 'client', monkeypatch)
    second = Ed25519Signer.generate()
    proof = second.sign(
        canonical({'subject_id': state.subject, 'public_key': b64(second.public_key)}),
        purpose='key-add',
    )
    added = await call(
        app,
        'identity.key_add',
        {
            'public_key': b64(second.public_key),
            'possession_proof': wire(proof),
            'ceiling': wire(app.primary_ceiling()),
        },
        key=state.signer,
        subject=state.subject,
    )
    assert added.status == 'ok', wire(added)
    args = {'upgrade_request_id': request_id}
    denied = await call(app, 'identity.upgrade_result', args, key=second, subject=state.subject)
    assert denied.error.code == 'upgrade_new_key_required'
    revoked = await call(
        app,
        'identity.key_revoke',
        {'key_id': state.signer.key_id},
        key=second,
        subject=state.subject,
    )
    assert revoked.status == 'ok', wire(revoked)
    before = await facts(app)
    denied = await call(
        app, 'identity.upgrade_result', args, key=state.signer, subject=state.subject
    )
    assert denied.error.code == 'credential_revoked'
    assert await facts(app) == before


async def test_status_does_not_reissue_or_reactivate_a_revoked_certificate(
    installed, tmp_path, monkeypatch
):
    from msg.core.models import AuditEvent, Event

    app, _ = installed
    state, _, request_id, result = await completed_upgrade(app, tmp_path / 'client', monkeypatch)
    async with app.metadata.transaction(write=True) as tx:
        await tx.revoke_certificate(
            result.data['certificate_id'],
            AuditEvent(
                event=Event(
                    id='revoked-upgrade-cert',
                    type='cert.revoke',
                    time=NOW,
                    request_id='revoke-test',
                    actor='u_root',
                    subject='u_root',
                    resources=(),
                    data={},
                ),
                authority=(),
                before_digest=None,
                after_digest=None,
                previous_digest=None,
                entry_digest='',
                result='revoked',
            ),
        )
    before = await facts(app)
    denied = await call(
        app,
        'identity.upgrade_result',
        {'upgrade_request_id': request_id},
        key=state.signer,
        subject=state.subject,
    )
    assert denied.error.code == 'certificate_revoked'
    assert await facts(app) == before


async def test_client_commit_then_local_confirmation_crash_resumes_without_old_token(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    state, _, _, _, _ = await legacy_state(app, tmp_path / 'client')
    async with connected(app, state) as client:
        original = state.accept_identity

        def fail_after_local_write(result):
            original(result)
            raise RuntimeError('process stopped after durable local switch')

        monkeypatch.setattr(state, 'accept_identity', fail_after_local_write)
        with pytest.raises(RuntimeError, match='after durable local switch'):
            await client.upgrade('local-crash')
    resumed_state = ClientState(state.directory)
    assert resumed_state.token is None
    assert (state.directory / 'identity-upgrade.json').is_file()
    async with connected(app, resumed_state) as resumed:
        result = await resumed.upgrade('local-crash')
        assert result.status == 'ok' and result.operation == 'identity.upgrade_result', wire(result)
    assert not (state.directory / 'identity-upgrade.json').exists()


async def test_server_rollback_keeps_fixed_request_and_valid_old_credential(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    state, token, post, history, _ = await legacy_state(app, tmp_path / 'client')
    async with connected(app, state) as client:

        async def fail_after_writes(*args, **kwargs):
            raise Failure('forced_certificate_failure')

        with monkeypatch.context() as patched:
            patched.setattr(identity, 'issue_online', fail_after_writes)
            failed = await client.upgrade('retry-same')
            assert failed.error.code == 'forced_certificate_failure'
        pending = loads((state.directory / 'identity-upgrade.json').read_bytes())
        async with app.metadata.transaction(write=False) as tx:
            assert (await tx.subject(state.subject)).kind == 'temporary'
            assert (await tx.credential(token[0])).revoked_at is None
            assert (
                tx.one('SELECT COUNT(*) FROM identity_keys WHERE subject=?', (state.subject,))[0]
                == 0
            )
            assert tx.one('SELECT body FROM revisions WHERE id=?', (post.revision,))[0] == history
        result = await client.upgrade('retry-same')
        assert result.status == 'ok' and result.request_id == pending['request_id'], wire(result)


@pytest.mark.parametrize(
    'damage',
    [
        'handle',
        'subject_id',
        'server',
        'public_key',
        'encryption_recipient',
        'permissions',
        'symlink',
        'missing_key',
        'too_large',
    ],
)
async def test_unsafe_or_changed_pending_intention_does_not_send_or_generate_keys(
    installed, tmp_path, monkeypatch, damage
):
    from msg.storage.git import durable_write

    app, _ = installed
    state, _, _, _, _ = await legacy_state(app, tmp_path / 'client')
    async with connected(app, state) as client:

        async def stop(packet):
            raise RuntimeError('stop after journal')

        monkeypatch.setattr(client.transport, 'call', stop)
        with pytest.raises(RuntimeError):
            await client.upgrade('pending-agent')
    path = state.directory / 'identity-upgrade.json'
    key_bytes, age_bytes = state.key_path.read_bytes(), state.age_key_path.read_bytes()
    if damage in {'handle', 'subject_id', 'server', 'public_key', 'encryption_recipient'}:
        pending = loads(path.read_bytes())
        pending[damage] = 'changed'
        durable_write(path, canonical(pending), mode=0o600)
    elif damage == 'permissions':
        path.chmod(0o644)
    elif damage == 'symlink':
        target = tmp_path / 'external-journal'
        path.rename(target)
        path.symlink_to(target)
    elif damage == 'missing_key':
        state.key_path.unlink()
    else:
        durable_write(path, b' ' * 8193, mode=0o600)
    reloaded = ClientState(state.directory)
    async with connected(app, reloaded) as client:
        sent = []

        async def unexpected(packet):
            sent.append(packet)
            raise AssertionError('unsafe journal reached the network')

        monkeypatch.setattr(client.transport, 'call', unexpected)
        with pytest.raises(Failure):
            await client.upgrade('pending-agent')
        assert not sent
    assert state.age_key_path.read_bytes() == age_bytes
    if damage == 'missing_key':
        assert not state.key_path.exists()
    else:
        assert state.key_path.read_bytes() == key_bytes


async def test_competing_local_upgrade_is_rejected_without_blocking_or_overwriting_keys(
    installed, tmp_path
):
    from msg.client_upgrade import upgrade_lock

    app, _ = installed
    state, _, _, _, _ = await legacy_state(app, tmp_path / 'client')
    with upgrade_lock(state.directory):
        async with connected(app, state) as client:
            with pytest.raises(Failure, match='identity_upgrade_busy'):
                await client.upgrade('concurrent-agent')
    assert not state.key_path.exists() and not state.age_key_path.exists()
    async with connected(app, state) as client:
        assert (await client.upgrade('concurrent-agent')).status == 'ok'


async def test_path_only_transport_cannot_send_the_legacy_token_in_a_url(installed, tmp_path):
    app, _ = installed
    state, _, _, _, _ = await legacy_state(app, tmp_path / 'client')
    async with connected(app, state, PathGETTransport) as client:
        with pytest.raises(Failure, match='secure_channel_required'):
            await client.upgrade('path-agent')
        assert client.transport.calls == 0
    assert not state.key_path.exists() and not state.age_key_path.exists()


async def test_concurrent_distinct_clients_cannot_bind_two_identities_or_keys(installed, tmp_path):
    import asyncio

    app, _ = installed
    first, token, post, history, _ = await legacy_state(app, tmp_path / 'first')
    second = ClientState(tmp_path / 'second', server=first.server)
    second.data.update(subject_id=first.subject, token=dict(first.data['token']))
    second._save()
    before_subject = first.subject
    async with connected(app, first) as a, connected(app, second) as b:
        results = await asyncio.gather(a.upgrade('race-first'), b.upgrade('race-second'))
    assert sum(result.status == 'ok' for result in results) == 1
    winner = first if results[0].status == 'ok' else second
    assert winner.subject == before_subject
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one('SELECT COUNT(*) FROM identity_keys WHERE subject=?', (before_subject,))[0] == 1
        )
        assert (
            tx.one('SELECT COUNT(*) FROM encryption_subkeys WHERE subject=?', (before_subject,))[0]
            == 1
        )
        assert (
            tx.one('SELECT key_id FROM identity_keys WHERE subject=?', (before_subject,))[0]
            == winner.signer.key_id
        )
        assert (await tx.credential(token[0])).revoked_at is not None
        assert (await tx.resource(post.id)).owner == before_subject
        assert tx.one('SELECT body FROM revisions WHERE id=?', (post.revision,))[0] == history


async def test_expired_old_credential_does_not_authorize_an_uncommitted_upgrade(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    state, token, _, _, _ = await legacy_state(app, tmp_path / 'client')
    async with connected(app, state) as client:

        async def stop(packet):
            raise RuntimeError('before commit')

        monkeypatch.setattr(client.transport, 'call', stop)
        with pytest.raises(RuntimeError):
            await client.upgrade('expired-agent')
    later = NOW + timedelta(hours=2)
    await app.close()
    restarted = Application(app.settings, clock=lambda: later)
    await restarted.load()
    try:
        before = await facts(restarted)
        async with connected(
            restarted, ClientState(state.directory), clock=lambda: later
        ) as client:
            result = await client.upgrade('expired-agent')
            assert result.error.code == 'credential_expired'
        assert await facts(restarted) == before
        async with restarted.metadata.transaction(write=False) as tx:
            assert (await tx.subject(state.subject)).kind == 'temporary'
            assert (
                tx.one('SELECT COUNT(*) FROM identity_keys WHERE subject=?', (state.subject,))[0]
                == 0
            )
    finally:
        await restarted.close()


async def test_cli_show_finds_pending_upgrade_without_exposing_credentials(
    installed, tmp_path, monkeypatch, capsys
):
    from msg.cli import parser, run

    app, _ = installed
    state, token, _, _, _ = await legacy_state(app, tmp_path / 'client')
    async with connected(app, state) as client:

        async def stop(packet):
            raise RuntimeError('before network')

        monkeypatch.setattr(client.transport, 'call', stop)
        with pytest.raises(RuntimeError):
            await client.upgrade('discoverable-upgrade')
    code = await run(
        parser().parse_args(['--config-dir', str(state.directory), 'identity', 'show'])
    )
    output = capsys.readouterr().out
    assert code == 0
    shown = loads(output.encode())
    assert shown['pending_upgrade']['status'] == 'pending'
    assert shown['pending_upgrade']['handle'] == 'discoverable-upgrade'
    assert shown['pending_upgrade']['resume'] == 'msg identity upgrade'
    assert b64(token[1]) not in output and state.age_key_path.read_text().strip() not in output
    async with connected(app, ClientState(state.directory)) as client:
        result = await client.upgrade()
        assert result.status == 'ok', wire(result)


async def test_no_pending_upgrade_still_requires_an_explicit_handle(installed, tmp_path):
    app, _ = installed
    state, _, _, _, _ = await legacy_state(app, tmp_path / 'client')
    async with connected(app, state) as client:
        with pytest.raises(Failure, match='upgrade_handle_required'):
            await client.upgrade()
    assert not state.key_path.exists() and not state.age_key_path.exists()


async def test_completed_upgrade_status_keeps_signed_path_get_equivalence(installed, tmp_path):
    from msg.bootstrap import feature_manifest

    app, _ = installed
    state, _, _, _, _ = await legacy_state(app, tmp_path / 'client')
    async with connected(app, state) as client:
        completed = await client.upgrade('path-status-agent')
        assert completed.status == 'ok', wire(completed)
    specification = app.registry.operation('identity.upgrade_result')
    assert specification.effect == 'read' and specification.require_signature
    feature = next(row for row in feature_manifest() if row['feature_id'] == 'identity_upgrade')
    assert feature['selftest_case'] == 'identity_upgrade_recovery'
    assert feature['doctor_check'] == 'authority_snapshot'
    async with connected(app, state, PathGETTransport) as client:
        result = await client.call(
            'identity.upgrade_result', {'upgrade_request_id': completed.request_id}
        )
    assert result.status == 'ok' and result.data['status'] == 'completed', wire(result)
    assert result.data['subject_id'] == state.subject
    assert result.data['upgrade_request_id'] == completed.request_id
    assert 'token' not in result.data and 'recovery_secret' not in result.data
