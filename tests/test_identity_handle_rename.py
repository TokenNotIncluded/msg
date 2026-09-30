"""Usernames change independently of identity, with a persisted seven-day interval."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_service import NOW

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import loads, wire
from msg.core.requests import request_for
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_own_handle_cli_history_aliases_persistent_limit_and_exact_boundary(
    installed, tmp_path, monkeypatch, capsys
):
    app, _ = installed
    monkeypatch.setattr(app.executor, 'clock', lambda: app.clock())
    monkeypatch.setattr(app.authenticator, 'clock', lambda: app.clock())
    monkeypatch.setattr(app.certificates, 'clock', lambda: app.clock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        directory = tmp_path / 'owner'
        client = MsgClient(
            ClientState(directory, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: app.clock(),
        )
        assert (await client.register('handle-original')).status == 'ok'
        uid = client.state.subject
        key = client.state.signer.public_key
        post = await client.call('content.post_create', {'parent': '/main', 'body': 'history'})
        assert post.status == 'ok'
        async with app.metadata.transaction(write=False) as tx:
            user_before = await tx.resource(uid)
            credential_before = tx.rows('SELECT id,body FROM credentials WHERE subject=?', (uid,))
            revision_before = tx.one(
                'SELECT body FROM revisions WHERE id=?', (post.resources[0].revision,)
            )
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        monkeypatch.setattr(
            cli,
            'MsgClient',
            lambda state, transport: MsgClient(
                state,
                transport,
                clock=lambda: app.clock(),
            ),
        )
        args = cli.parser().parse_args([
            '--config-dir',
            str(directory),
            '--server',
            app.settings.service_url,
            'identity',
            'rename',
            'handle-new',
        ])
        assert await cli.run(args) == 0
        changed = loads(capsys.readouterr().out.encode())
        assert changed['data']['subject_id'] == uid
        assert changed['data']['path'] == '/@handle-new'
        assert changed['data']['next_rename_at'].startswith('2026-10-04T00:00:00')
        reloaded = MsgClient(
            ClientState(directory),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: app.clock(),
        )
        assert reloaded.state.data['handle'] == 'handle-new'
        assert reloaded.state.subject == uid and reloaded.state.signer.public_key == key
        async with app.metadata.transaction(write=False) as tx:
            user = await tx.resource(uid)
            assert user.created_at == user_before.created_at
            assert user.generation == user_before.generation + 1
            assert (
                tx.rows('SELECT id,body FROM credentials WHERE subject=?', (uid,))
                == credential_before
            )
            assert (
                tx.one('SELECT body FROM revisions WHERE id=?', (post.resources[0].revision,))
                == revision_before
            )
            assert await tx.resolve_migrated('/@handle-original/files') == await tx.resolve(
                '/@handle-new/files'
            )
        assert (await http.get('/@handle-original')).status_code in {200, 307, 308}
        no_change = await reloaded.rename_identity('handle-new')
        assert no_change.status == 'ok' and no_change.data['generation'] == user.generation
        blocked = await reloaded.rename_identity('handle-third')
        assert blocked.error.code == 'handle_rename_cooldown'
        packet = reloaded.prepare(
            'identity.rename', {'handle': 'handle-third'}, expected=((uid, user.generation),)
        )
        assert (
            await http.post(
                '/-/p/identity.rename',
                json=wire(packet),
            )
        ).status_code == 429
        app.clock = lambda: NOW + timedelta(days=7) - timedelta(microseconds=1)
        assert (
            await reloaded.rename_identity('handle-third')
        ).error.code == 'handle_rename_cooldown'
        app.clock = lambda: NOW + timedelta(days=7)
        assert (await reloaded.rename_identity('handle-third')).status == 'ok'
        app.clock = lambda: NOW


@pytest.mark.asyncio
async def test_handle_validation_reservation_race_and_idempotent_retry(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:

        def client(name):
            return MsgClient(
                ClientState(tmp_path / name, server=app.settings.service_url),
                HTTPTransport(app.settings.service_url, http=http),
                clock=lambda: app.clock(),
            )

        owner, other = client('owner'), client('other')
        assert (await owner.register('rename-first')).status == 'ok'
        assert (await other.register('rename-taken')).status == 'ok'
        for invalid in ('root', 'online-ca', '../escape', '@new', 'X', ''):
            assert (await owner.rename_identity(invalid)).error.code == 'invalid_handle'
        assert (await owner.rename_identity('rename-taken')).error.code == 'handle_unavailable'
        uid = owner.state.subject
        async with app.metadata.transaction(write=False) as tx:
            generation = (await tx.resource(uid)).generation
        packet = owner.prepare(
            'identity.rename',
            {'handle': 'rename-second'},
            expected=((uid, generation),),
            request_id='rename-once',
        )
        first, retried = await asyncio.gather(owner.send(packet), owner.send(packet))
        assert first.status == retried.status == 'ok'
        assert first.resources == retried.resources
        assert first.replayed != retried.replayed
        async with app.metadata.transaction(write=False) as tx:
            user = await tx.resource(uid)
            saved = tx.setting('identity_handle_renamed_at:' + uid)
        assert user.name == '@rename-second' and user.generation == generation + 1
        assert saved is not None
        assert (await other.rename_identity('rename-first')).error.code == 'handle_unavailable'
        fresh = client('fresh')
        assert (await fresh.register('rename-first')).error.code == 'handle_unavailable'
        # A request cannot select another subject or execute anonymously.
        anonymous = request_for(
            'identity.rename', {'handle': 'anonymous'}, app.settings.service_url
        )
        assert (await app.executor.execute(anonymous)).status == 'error'
        attempted = await other.call(
            'identity.rename', {'handle': 'rename-steal'}, expected=((uid, user.generation),)
        )
        assert attempted.error.code == 'expected_generation_required'
        async with app.metadata.transaction(write=False) as tx:
            assert (await tx.resource(uid)).name == '@rename-second'


@pytest.mark.asyncio
async def test_custodial_handle_rename_obeys_same_account_cooldown(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        client = MsgClient(
            ClientState(tmp_path / 'custodial', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.custodial('custody-original')).status == 'ok'
        assert (await client.rename_identity('custody-new')).status == 'ok'
        assert (
            await client.rename_identity('custody-again')
        ).error.code == 'handle_rename_cooldown'


@pytest.mark.asyncio
async def test_rename_does_not_expand_old_credentials_or_name_temporary_accounts(
    installed, tmp_path
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner = MsgClient(
            ClientState(tmp_path / 'restricted', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await owner.register('unchanged-account')).status == 'ok'
        uid = owner.state.subject
        async with app.metadata.transaction(write=True) as tx:
            subject = await tx.subject(uid)
            credential = await tx.credential(owner.state.signer.key_id)
            await tx.save_credential(
                replace(
                    credential,
                    ceiling=tuple(
                        replace(grant, operations=grant.operations - {'identity.rename@1'})
                        for grant in credential.ceiling
                    ),
                ),
                subject.auth_version,
            )
        assert (await owner.rename_identity('disallowed')).error.code == 'credential_ceiling'
        async with app.metadata.transaction(write=False) as tx:
            assert (await tx.resource(uid)).name == '@unchanged-account'
            assert tx.setting('identity_handle_renamed_at:' + uid) is None
        temporary = MsgClient(
            ClientState(tmp_path / 'temporary', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await temporary.temporary()).status == 'ok'
        assert (
            await temporary.rename_identity('not-registered')
        ).error.code == 'formal_identity_required'
