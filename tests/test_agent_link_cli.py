"""Local Agent Link acceptance through real signed HTTP and disposable PostgreSQL."""

import argparse
import secrets
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_service import NOW

from msg.client import ClientState, MsgClient
from msg.client_agent_link import AgentLink, add_commands, run_command
from msg.core.codec import b64, canonical, loads
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app

TASK = 'PRIVATE_TASK_DO_NOT_PROJECT'
MESSAGE = 'PRIVATE_PROGRESS_DO_NOT_PROJECT'
READS = ('file.read@1',)
DISCOVERY = ('discovery.get@1', 'discovery.list@1')


def resource_ref(result):
    assert result.status == 'ok'
    return {'id': result.resources[0].id, 'revision': result.resources[0].revision}


async def make_topic(owner, name):
    made = owner.checked(
        await owner.call('file.mkdir', {'parent': '/@link-owner/files', 'name': name})
    )
    identifier = made.resources[0].id
    private = owner.checked(
        await owner.call(
            'content.chmod',
            {'id': identifier, 'mode': '0700'},
            expected=((identifier, made.data['generation']),),
        )
    )
    owner.checked(
        await owner.call(
            'content.topic_configure',
            {'id': identifier, 'policy': {'topic_mode': '0700', 'file_mode': '0600'}},
            expected=((identifier, private.data['generation']),),
        )
    )
    return identifier


def grants(incoming, outgoing):
    result = []
    for target, writes in ((incoming, ()), (outgoing, ('file.create@1',))):
        for capability, operations in (
            ('resource.basic', (*READS, *writes)),
            ('discovery.basic', DISCOVERY if writes else ('discovery.get@1',)),
        ):
            result.append({
                'capability': capability,
                'version': 1,
                'scope': {'resource_id': target, 'descendants': True},
                'operations': list(operations),
                'constraints': {},
            })
    return result


@pytest.fixture(params=('independent', 'shared-token'))
async def link_env(installed, tmp_path, request):
    app, _ = installed
    # All services retain a callable reading this mutable value, including certificates.
    clock = SimpleNamespace(now=NOW)
    app.clock = lambda: clock.now
    app.certificates.clock = app.clock
    app.authenticator.clock = app.clock
    app.executor.clock = app.clock
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner_state = ClientState(tmp_path / 'owner', server=app.settings.service_url)
        owner_transport = HTTPTransport(app.settings.service_url, http=http)
        owner = MsgClient(owner_state, owner_transport, clock=app.clock, retries=0)
        owner.checked(await owner.register('link-owner'))
        original_state = ClientState(tmp_path / 'worker-own', server=app.settings.service_url)
        original = MsgClient(
            original_state,
            HTTPTransport(app.settings.service_url, http=http),
            clock=app.clock,
            retries=0,
        )
        original.checked(await original.register('link-worker'))
        incoming = await make_topic(owner, 'incoming')
        outgoing = await make_topic(owner, 'outgoing')
        business = await make_topic(owner, 'business')
        secret_ref = resource_ref(
            await owner.call(
                'file.create',
                {
                    'parent': business,
                    'name': 'private.json',
                    'data': b64(canonical({'private': TASK})),
                },
            )
        )
        worker_state = ClientState(tmp_path / 'worker-link', server=app.settings.service_url)
        worker_state.data['subject_id'] = owner_state.subject
        credential_secret = None
        if request.param == 'independent':
            issued = owner.checked(
                await owner.call(
                    'identity.delegate',
                    {
                        'grantee': original_state.subject,
                        'key_id': original_state.signer.key_id,
                        'grants': grants(incoming, outgoing),
                        'ttl': 3600,
                        'depth': 0,
                    },
                )
            )
            worker_state.save_signer(original_state.signer)
            worker_state.data['certificates'] = [issued.data['certificate_id']]
            worker_actor = original_state.subject
            revoke_id = issued.resources[0].id
            async with app.metadata.transaction(write=False) as tx:
                certificate = await tx.certificate(issued.data['certificate_id'])
                assert certificate.delegation_depth == 0
                assert certificate.expires_at <= NOW + timedelta(hours=1)
                assert tx.setting('delegated_identity:' + original_state.subject) is None
        else:
            issued = owner.checked(
                await owner.call(
                    'identity.token_create',
                    {
                        'nonce': b64(secrets.token_bytes(32)),
                        'recovery_secret': b64(secrets.token_bytes(32)),
                        'ceiling': grants(incoming, outgoing),
                        'ttl': 3600,
                    },
                    contract_version=3,
                )
            )
            credential_secret = issued.data['token']
            worker_state.data['token'] = {
                'credential_id': issued.data['credential_id'],
                'value': credential_secret,
                'expires_at': issued.data['expires_at'],
            }
            worker_actor = owner_state.subject
            revoke_id = issued.data['credential_id']
            assert not worker_state.key_path.exists()
            async with app.metadata.transaction(write=False) as tx:
                credential = await tx.credential(revoke_id)
                assert credential.expires_at == NOW + timedelta(hours=1)
                assert credential.subject_id == owner_state.subject
        worker_state._save()
        worker_transport = HTTPTransport(app.settings.service_url, http=http)
        worker = MsgClient(worker_state, worker_transport, clock=app.clock, retries=0)
        yield SimpleNamespace(
            app=app,
            owner=owner,
            worker=worker,
            original=original,
            transport=worker_transport,
            incoming=incoming,
            outgoing=outgoing,
            business=business,
            secret_ref=secret_ref,
            actor=worker_actor,
            owner_uid=owner_state.subject,
            original_uid=original_state.subject,
            mode=request.param,
            revoke_id=revoke_id,
            clock=clock,
            path=tmp_path,
            credential_secret=credential_secret,
            owner_link=AgentLink(owner),
            worker_link=AgentLink(worker),
        )


async def invite(env, *, ttl=3600):
    return await env.owner_link.invite(
        incoming=env.incoming,
        outgoing=env.outgoing,
        worker_actor=env.actor,
        mode=env.mode,
        task=TASK,
        expires_at=NOW + timedelta(seconds=ttl),
        request_id='invite-fixed',
        journal=env.path / 'owner-journal.json',
    )


async def records(env, parent):
    result = env.owner.checked(
        await env.owner.call(
            'discovery.list',
            {'parent': parent, 'fields': ['id', 'revision'], 'limit': 200},
        )
    )
    assert not result.data.get('cursor')
    return [dict(item) for item in result.data['items']]


async def raw_event(env, invitation, *, actor=None, raw=None, **updates):
    value = {
        'version': 1,
        'kind': 'accept',
        'invitation': invitation,
        'previous': None,
        'message': '',
        'artifacts': [],
    }
    value.update(updates)
    result = await (actor or env.worker).call(
        'file.create',
        {
            'parent': env.outgoing,
            'name': 'raw-' + secrets.token_hex(8) + '.json',
            'data': b64(raw if raw is not None else canonical(value)),
            'media_type': 'application/json',
        },
    )
    return resource_ref(result)


def assert_no_credentials(env, captured, failures=()):
    text = captured.out + captured.err + ''.join(str(x) for x in failures)
    for journal in env.path.glob('*journal.json'):
        text += journal.read_text()
        assert journal.stat().st_mode & 0o077 == 0
    needles = [
        b64(env.owner.state.signer.private_bytes()),
        b64(env.original.state.signer.private_bytes()),
    ]
    if env.credential_secret:
        needles.append(env.credential_secret)
    exposed = any(value in text for value in needles)
    assert not exposed, 'credential material appeared in output, errors or retry journals'
    assert TASK not in text and MESSAGE not in text


@pytest.mark.asyncio
async def test_real_private_lifecycle_fixed_artifacts_and_preserved_actor(link_env, capsys):
    env = link_env
    invitation = (await invite(env))['resource']
    before = await records(env, env.outgoing)
    assert before == []
    view = await env.worker_link.get(invitation)
    assert view['state'] == 'invited' and view['events'] == []
    assert await records(env, env.outgoing) == before
    assert TASK not in str(view)
    assert await env.worker_link.task(invitation) == TASK
    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest='command', required=True))
    fixed_arguments = ['--invitation', invitation['id'], '--revision', invitation['revision']]
    private_task = env.path / 'private-task.txt'
    cli_view = await run_command(
        env.worker,
        parser.parse_args([
            'agent-link',
            'get',
            *fixed_arguments,
            '--task-output',
            str(private_task),
        ]),
    )
    assert cli_view['state'] == 'invited' and TASK not in str(cli_view)
    assert private_task.read_text() == TASK
    assert private_task.stat().st_mode & 0o777 == 0o600
    assert await records(env, env.outgoing) == before
    selected_profile = env.worker.state.path.read_bytes()
    with pytest.raises(Failure, match='agent_link_profile_output_forbidden'):
        await run_command(
            env.worker,
            parser.parse_args([
                'agent-link',
                'get',
                *fixed_arguments,
                '--task-output',
                str(env.worker.state.path),
            ]),
        )
    assert env.worker.state.path.read_bytes() == selected_profile
    assert await records(env, env.outgoing) == before
    accepted = await run_command(
        env.worker,
        parser.parse_args([
            'agent-link',
            'accept',
            *fixed_arguments,
            '--request-id',
            'accept-fixed',
            '--journal',
            str(env.path / 'worker-journal.json'),
        ]),
    )
    assert accepted['actor'] == env.actor and accepted['subject'] == env.owner_uid
    assert (env.actor == env.owner_uid) == (env.mode == 'shared-token')
    assert (
        await env.worker_link.accept(
            invitation,
            request_id='accept-fixed',
            journal=env.path / 'worker-journal.json',
        )
        == accepted
    )
    progress = await env.worker_link.progress(
        invitation,
        message=MESSAGE,
        request_id='progress-fixed',
        journal=env.path / 'worker-journal.json',
    )
    artifact = resource_ref(
        await env.worker.call(
            'file.create',
            {
                'parent': env.outgoing,
                'name': 'artifact.json',
                'data': b64(canonical({'artifact': 'result'})),
                'media_type': 'application/json',
            },
        )
    )
    delivered = await env.worker_link.deliver(
        invitation,
        message='done',
        artifacts=[artifact],
        request_id='deliver-fixed',
        journal=env.path / 'worker-journal.json',
    )
    final = await env.owner_link.get(invitation)
    assert final['state'] == 'delivered' and not final['ambiguous']
    assert final['artifacts'] == [artifact]
    assert [item['kind'] for item in final['events']] == ['accept', 'progress', 'deliver']
    assert [item['resource'] for item in final['events']] == [
        accepted['resource'],
        progress['resource'],
        delivered['resource'],
    ]
    assert TASK not in str(final) and MESSAGE not in str(final)
    for item in (accepted, progress, delivered):
        meta = env.owner.checked(
            await env.owner.call(
                'discovery.get',
                {'id': item['resource']['id'], 'view': 'meta'},
            )
        ).data
        assert meta['created_by'] == env.actor and meta['owner'] == env.owner_uid
        mode = int(meta['mode'], 8) if isinstance(meta['mode'], str) else meta['mode']
        assert mode == 0o600
    own = env.original.checked(await env.original.call('discovery.get', {'id': env.original_uid}))
    assert own.actor == own.subject == env.original_uid
    assert env.original.state.subject == env.original_uid
    assert_no_credentials(env, capsys.readouterr())


@pytest.mark.asyncio
async def test_ceiling_rejects_business_incoming_edits_posts_and_identity(link_env):
    env = link_env
    invitation = (await invite(env))['resource']
    outgoing = await raw_event(env, invitation)
    changes = [
        ('file.read', {'id': env.secret_ref['id']}),
        ('file.create', {'parent': env.business, 'name': 'escape.txt', 'data': b64(b'escape')}),
        ('file.create', {'parent': env.incoming, 'name': 'injected.txt', 'data': b64(b'escape')}),
        (
            'file.write',
            {'id': outgoing['id'], 'base_revision': outgoing['revision'], 'data': b64(b'edited')},
        ),
        (
            'content.text_patch',
            {
                'id': outgoing['id'],
                'base_revision': outgoing['revision'],
                'exact': 'accept',
                'replacement': 'deliver',
            },
        ),
        (
            'file.patch',
            {
                'id': outgoing['id'],
                'base_revision': outgoing['revision'],
                'base_generation': 1,
                'patch': {'kind': 'exact', 'exact': 'accept', 'replacement': 'deliver'},
            },
        ),
        ('content.post_create', {'parent': env.outgoing, 'body': 'post'}),
        ('content.archive', {'id': outgoing['id']}),
        ('content.chmod', {'id': outgoing['id'], 'mode': '0644'}),
        ('identity.rename', {'handle': 'escaped-identity'}),
        (
            'identity.delegate',
            {
                'grantee': env.original_uid,
                'key_id': env.original.state.signer.key_id,
                'grants': grants(env.incoming, env.outgoing),
                'ttl': 60,
                'depth': 0,
            },
        ),
    ]
    for operation, arguments in changes:
        # Each packet must have valid syntax: a schema failure is not evidence of a ceiling.
        expected = ((outgoing['id'], 1),) if operation == 'content.archive' else ()
        packet = env.worker.prepare(operation, arguments, expected=expected)
        env.app.registry.validate(
            env.app.registry.operation(operation).input_schema, packet.arguments
        )
        result = await env.worker.call(operation, arguments, expected=expected)
        assert result.status == 'error'
        allowed = {'credential_ceiling', 'delegation_scope'}
        if operation == 'identity.rename' and env.mode == 'independent':
            # The actor/subject owner check precedes this operation's scope check.
            allowed.add('identity_owner_required')
        if operation == 'identity.delegate' and env.mode == 'independent':
            # The delegation certificate explicitly forbids further delegation.
            allowed.add('redelegation_forbidden')
        assert result.error.code in allowed, (
            operation,
            result.error.code,
        )
        assert TASK not in str(result.error)
    assert await records(env, env.incoming) == [invitation]
    assert await records(env, env.business) == [env.secret_ref]
    restored = env.owner.checked(await env.owner.call('file.read', outgoing)).data
    assert restored['revision'] == outgoing['revision']


@pytest.mark.asyncio
async def test_unaccepted_delivery_wrong_revision_and_fake_actor_are_not_projected(link_env):
    env = link_env
    invitation = (await invite(env))['resource']
    with pytest.raises(Failure, match='agent_link_transition_required'):
        await env.worker_link.deliver(
            invitation,
            message='skip accept',
            artifacts=[],
            request_id='skip-accept',
            journal=env.path / 'worker-journal.json',
        )
    assert await records(env, env.outgoing) == []
    await raw_event(env, invitation, kind='deliver')
    await raw_event(env, {**invitation, 'revision': 'v_wrong'}, kind='accept')
    if env.mode == 'independent':
        await raw_event(env, invitation, actor=env.owner, kind='accept', message='claimed worker')
        # Creation attribution must not authenticate a later revision written by the owner.
        worker_created = await raw_event(env, invitation)
        rewritten = env.owner.checked(
            await env.owner.call(
                'file.write',
                {
                    'id': worker_created['id'],
                    'base_revision': worker_created['revision'],
                    'data': b64(
                        canonical({
                            'version': 1,
                            'kind': 'accept',
                            'invitation': invitation,
                            'message': 'owner rewrite',
                            'artifacts': [],
                        })
                    ),
                },
                expected=((worker_created['id'], 1),),
            )
        )
        assert rewritten.resources[0].revision != worker_created['revision']
    else:
        # A shared token cannot distinguish workers sharing the same actor UID.
        # A different, existing actor has no private-compartment ACL without a grant.
        denied = await env.original.call(
            'file.create',
            {
                'parent': env.outgoing,
                'name': 'foreign.json',
                'data': b64(canonical({'kind': 'accept'})),
            },
        )
        assert denied.status == 'error' and denied.error.code == 'permission_denied'
    state = await env.owner_link.get(invitation)
    assert state['state'] == 'invited' and state['events'] == [] and state['ignored'] >= 2
    with pytest.raises(Failure):
        await env.worker_link.get({**invitation, 'revision': 'v_wrong'})
    accepted = await env.worker_link.accept(
        invitation,
        request_id='accept-after-invalid',
        journal=env.path / 'worker-journal.json',
    )
    assert (await env.owner_link.get(invitation))['events'] == [
        {'resource': accepted['resource'], 'kind': 'accept'}
    ]


@pytest.mark.asyncio
async def test_malformed_json_and_forks_cannot_silently_choose_success(link_env):
    env = link_env
    invitation = (await invite(env))['resource']
    await raw_event(env, invitation, raw=b'{not-json')
    await raw_event(env, invitation, version=True)
    await raw_event(env, invitation, kind=['accept'])
    malformed = await env.owner_link.get(invitation)
    assert malformed['state'] == 'invited' and malformed['ignored'] == 3
    await raw_event(env, invitation)
    await raw_event(env, invitation)
    view = await env.owner_link.get(invitation)
    assert view['state'] == 'invited' and view['ambiguous'] and view['events'] == []
    with pytest.raises(Failure, match='agent_link_channel_ambiguous'):
        await env.worker_link.accept(
            invitation,
            request_id='ambiguous-accept',
            journal=env.path / 'worker-journal.json',
        )


@pytest.mark.asyncio
async def test_response_loss_reuses_request_body_and_never_allocates_new_id(
    link_env, monkeypatch, capsys
):
    env = link_env
    invitation = (await invite(env))['resource']
    transport_call = env.transport.call
    lost = True

    async def lose_committed_create(packet):
        nonlocal lost
        result = await transport_call(packet)
        if packet.operation == 'file.create' and lost:
            lost = False
            raise httpx.ReadTimeout('response lost after commit')
        return result

    monkeypatch.setattr(env.transport, 'call', lose_committed_create)
    journal = env.path / 'worker-journal.json'
    with pytest.raises(Failure, match='transport_uncertain') as failure:
        await env.worker_link.accept(invitation, request_id='accept-lost', journal=journal)
    saved = loads(journal.read_bytes())
    assert set(saved['entries']) == {'accept-lost'}
    first = await records(env, env.outgoing)
    assert len(first) == 1
    # Restart the adapter and transport; preserve the persisted explicit ID and previous ref.
    monkeypatch.setattr(env.transport, 'call', transport_call)
    accepted = await AgentLink(env.worker).accept(
        invitation, request_id='accept-lost', journal=journal
    )
    assert accepted['resource'] == first[0]
    assert await records(env, env.outgoing) == first
    unsent = True

    async def lose_before_create(packet):
        nonlocal unsent
        if packet.operation == 'file.create' and unsent:
            unsent = False
            raise httpx.ReadTimeout('response lost before send')
        return await transport_call(packet)

    monkeypatch.setattr(env.transport, 'call', lose_before_create)
    with pytest.raises(Failure, match='transport_uncertain') as before_send:
        await env.worker_link.progress(
            invitation, message=MESSAGE, request_id='progress-fixed', journal=journal
        )
    assert await records(env, env.outgoing) == first
    assert set(loads(journal.read_bytes())['entries']) == {'accept-lost', 'progress-fixed'}
    monkeypatch.setattr(env.transport, 'call', transport_call)
    progress = await AgentLink(env.worker).progress(
        invitation, message=MESSAGE, request_id='progress-fixed', journal=journal
    )
    before = await records(env, env.outgoing)
    with pytest.raises(Failure, match='agent_link_request_id_conflict') as conflict:
        await env.worker_link.progress(
            invitation, message='different', request_id='progress-fixed', journal=journal
        )
    assert await records(env, env.outgoing) == before
    assert progress['resource'] in before
    assert_no_credentials(
        env, capsys.readouterr(), (failure.value, before_send.value, conflict.value)
    )


@pytest.mark.asyncio
async def test_expired_invitation_and_revocation_block_writes_without_replacing_identity(link_env):
    env = link_env
    invitation = (await invite(env, ttl=30))['resource']
    env.clock.now = NOW + timedelta(seconds=31)
    with pytest.raises(Failure, match='agent_link_expired'):
        await env.worker_link.accept(
            invitation,
            request_id='expired-accept',
            journal=env.path / 'worker-journal.json',
        )
    assert await records(env, env.outgoing) == []
    assert (await env.owner_link.get(invitation))['expired']
    env.clock.now = NOW
    if env.mode == 'independent':
        env.owner.checked(await env.owner.call('identity.delegation_revoke', {'id': env.revoke_id}))
    else:
        env.owner.checked(await env.owner.call('identity.key_revoke', {'key_id': env.revoke_id}))
    blocked = await env.worker.call('file.read', invitation)
    assert blocked.status == 'error'
    assert blocked.error.code in {
        'authority_source_inactive',
        'credential_revoked',
        'invalid_api_key',
        'invalid_grant',
    }
    own = env.original.checked(await env.original.call('discovery.get', {'id': env.original_uid}))
    assert own.actor == own.subject == env.original_uid


@pytest.mark.asyncio
async def test_credential_ceiling_expires_at_one_hour_without_replacing_identity(link_env):
    env = link_env
    invitation = (await invite(env))['resource']
    env.clock.now = NOW + timedelta(seconds=3599)
    current = env.worker.checked(await env.worker.call('file.read', invitation))
    assert current.actor == env.actor and current.subject == env.owner_uid
    env.clock.now = NOW + timedelta(seconds=3600)
    expired = await env.worker.call('file.read', invitation)
    assert expired.status == 'error'
    assert expired.error.code == (
        'certificate_expired' if env.mode == 'independent' else 'credential_expired'
    )
    assert await records(env, env.outgoing) == []
    env.owner.checked(await env.owner.call('file.read', invitation))
    own = env.original.checked(await env.original.call('discovery.get', {'id': env.original_uid}))
    assert own.actor == own.subject == env.original_uid


def test_agent_link_parser_help_needs_no_state_or_network(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('help touched identity or network')

    monkeypatch.setattr(ClientState, '__init__', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, '__init__', forbidden)
    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest='command', required=True))
    for command in (
        [],
        ['agent-link'],
        *(['agent-link', kind] for kind in ('invite', 'get', 'accept', 'progress', 'deliver')),
    ):
        with pytest.raises(SystemExit) as done:
            parser.parse_args([*command, '--help'])
        assert done.value.code == 0
    assert 'agent-link' in capsys.readouterr().out
