"""Agent Link onboards a helper into two private mailboxes and nothing else."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from test_service import NOW

from msg import cli, client_delegated, client_link
from msg.client import ClientState, MsgClient
from msg.client_agent_cli import stream
from msg.client_link import decode_code, encode_code, grant_path, load_record, run_command
from msg.client_subagents_remote import RemoteAgents
from msg.core.codec import b64, canonical, loads, unb64
from msg.core.errors import Failure
from msg.core.models import OperationError
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


def args(action, **kwargs):
    return SimpleNamespace(action=action, **kwargs)


async def prepared_link(app, tmp_path, http, task='Review the parser patch and report.'):
    transport = HTTPTransport(app.settings.service_url, http=http)
    owner_state = ClientState(tmp_path / 'owner', server=app.settings.service_url)
    owner = MsgClient(owner_state, transport, clock=lambda: NOW)
    assert (await owner.register('alice')).status == 'ok'
    helper_state = ClientState(tmp_path / 'helper', server=app.settings.service_url)
    helper = MsgClient(helper_state, transport, clock=lambda: NOW)
    invited = await run_command(
        owner, args('invite', name='reviewer', task=task, task_file=None, minutes=30)
    )
    prompt = invited['prompt']
    assert task in prompt and invited['data']['invite_code'] in prompt
    assert 'prompt' not in invited['data']
    assert "--link '@alice#reviewer' link join" in prompt and '--config-dir' not in prompt
    joined = await run_command(helper, args('join', code=invited['data']['invite_code']))
    # Re-running join reuses the same local keys and prints the same public request.
    again = await run_command(helper, args('join', code=invited['data']['invite_code']))
    assert again['data']['join_code'] == joined['data']['join_code']
    return owner, helper, invited, joined


async def linked(app, tmp_path, http, task='Review the parser patch and report.'):
    owner, helper, _, joined = await prepared_link(app, tmp_path, http, task)
    approved = await run_command(
        owner, args('approve', code=joined['data']['join_code'], minutes=None)
    )
    assert approved['data']['address'].startswith('@alice~')
    assert 'prompt' not in approved['data']
    assert approved['prompt'].startswith('@alice approved your Agent Link access')
    assert approved['data']['commands']['listen'] == 'msg --agent reviewer-lead listen --remote'
    replay = await run_command(
        owner, args('approve', code=joined['data']['join_code'], minutes=None)
    )
    assert replay['data']['access_code'] == approved['data']['access_code']
    accepted = await run_command(helper, args('accept', code=approved['data']['access_code']))
    return owner, helper, accepted, joined['data']['join_code']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field,value',
    [
        ('certificate_id', 'r_wrong_certificate'),
        ('address', None),
        ('grantor_address', None),
        ('expires_at', '2026-01-01T00:00:00Z'),
        ('target_service', 'http://another-service'),
        ('key_id', 'k_other_helper'),
        ('grantor', 'u_another_owner'),
    ],
)
async def test_wrong_access_cannot_poison_prepared_or_owner_identity(
    installed, tmp_path, field, value
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, invited, joined = await prepared_link(app, tmp_path, http)
        owner_profile = owner.state.path.read_bytes()
        with pytest.raises(Failure, match='delegated_profile_must_be_empty'):
            await run_command(owner, args('join', code=invited['data']['invite_code']))
        assert owner.state.path.read_bytes() == owner_profile
        approved = await run_command(
            owner, args('approve', code=joined['data']['join_code'], minutes=None)
        )
        access = decode_code(approved['data']['access_code'], 'access')
        before = helper.state.path.read_bytes()
        original_key = helper.state.signer.key_id
        bad = {**access, 'grant': {**access['grant'], field: value}}
        with pytest.raises(Failure):
            await run_command(helper, args('accept', code=encode_code('access', bad)))
        assert helper.state.path.read_bytes() == before
        assert helper.state.subject is None and helper.state.signer.key_id == original_key
        accepted = await run_command(helper, args('accept', code=approved['data']['access_code']))
        assert accepted['data']['task']['from'] == '@alice#reviewer-lead'
        read = await helper.call('discovery.get', {'id': '/@alice'})
        assert read.status == 'ok'
        assert read.actor == access['grant']['subject_id'] and read.subject == owner.state.subject


@pytest.mark.asyncio
async def test_rejected_join_uncertain_result_and_bad_cursor_recover(
    installed, tmp_path, monkeypatch, capsys
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, _, joined = await prepared_link(app, tmp_path, http)
        code = joined['data']['join_code']
        request = decode_code(code, 'join')
        bad = {
            **request,
            'request': {
                **request['request'],
                'possession_proof': {
                    **request['request']['possession_proof'],
                    'value': b64(bytes(64)),
                },
            },
        }
        with pytest.raises(Failure, match='invalid_signature'):
            await run_command(owner, args('approve', code=encode_code('join', bad), minutes=None))
        record = load_record(owner.state, 'reviewer')
        assert 'approval' not in record
        journal = client_link.issuance_journal(owner.state, record)
        assert not journal.exists() and not grant_path(owner.state, record).exists()
        original_call = owner.call
        attempts = []
        interrupted = False

        async def uncertain(operation, arguments=None, **kwargs):
            nonlocal interrupted
            result = await original_call(operation, arguments, **kwargs)
            if operation == 'identity.delegated_create':
                attempts.append((kwargs['request_id'], canonical(arguments)))
                if not interrupted:
                    interrupted = True
                    return replace(result, status='uncertain', data=None)
                assert result.replayed
            return result

        monkeypatch.setattr(owner, 'call', uncertain)
        with pytest.raises(Failure, match='transport_uncertain'):
            await run_command(owner, args('approve', code=code, minutes=None))
        assert journal.exists() and not grant_path(owner.state, record).exists()
        approved = await run_command(owner, args('approve', code=code, minutes=None))
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        access = decode_code(approved['data']['access_code'], 'access')
        cursor = loads(unb64(access['cursor']))
        before = helper.state.path.read_bytes()
        for wrong in (
            'not-a-cursor',
            b64(canonical({**cursor, 'scope': {**cursor['scope'], 'agent': 'reviewer-lead'}})),
            b64(canonical({**cursor, 'sync_cursor': cursor['sync_cursor'] + 'X'})),
        ):
            with pytest.raises(Failure):
                await run_command(
                    helper, args('accept', code=encode_code('access', {**access, 'cursor': wrong}))
                )
            assert helper.state.path.read_bytes() == before and helper.state.subject is None
        await RemoteAgents(owner).send(
            'reviewer-lead', 'reviewer', 'Before acceptance.', message_id='early'
        )
        await run_command(helper, args('accept', code=approved['data']['access_code']))
        assert await drain(helper, 'reviewer', capsys) == [
            'Review the parser patch and report.',
            'Before acceptance.',
        ]
        # Acceptance retries keep the existing listener checkpoint, without replaying its task.
        await run_command(helper, args('accept', code=approved['data']['access_code']))
        assert await drain(helper, 'reviewer', capsys) == []
        await run_command(owner, args('revoke', name='reviewer'))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'interruption',
    [
        'response',
        'retryable_error',
        'grant_file',
        'issued_record',
        'task_send',
        'approved_record',
        'seed',
    ],
)
async def test_approval_recovers_without_changing_issuance_or_task(
    installed, tmp_path, monkeypatch, interruption
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, _, joined = await prepared_link(app, tmp_path, http)
        code = joined['data']['join_code']
        record = load_record(owner.state, 'reviewer')
        journal = client_link.issuance_journal(owner.state, record)
        issued = []
        failed = False

        def track(client):
            original = client.call

            async def call(operation, arguments=None, **kwargs):
                nonlocal failed
                if operation == 'identity.delegated_create':
                    saved = loads(journal.read_bytes())
                    assert saved['request_id'] == kwargs['request_id']
                    assert saved['intention']['arguments'] == arguments
                    issued.append((kwargs['request_id'], canonical(arguments)))
                result = await original(operation, arguments, **kwargs)
                if operation == 'identity.delegated_create' and not failed:
                    if interruption == 'response':
                        failed = True
                        raise Failure('transport_uncertain', retryable=True)
                    if interruption == 'retryable_error':
                        failed = True
                        return replace(
                            result,
                            status='error',
                            error=OperationError(code='transport_uncertain', retryable=True),
                        )
                return result

            monkeypatch.setattr(client, 'call', call)

        original_write = client_delegated.durable_write

        def write(path, value, **kwargs):
            nonlocal failed
            if (
                interruption == 'grant_file'
                and not failed
                and path == grant_path(owner.state, record)
            ):
                failed = True
                raise Failure('test_interrupted')
            return original_write(path, value, **kwargs)

        monkeypatch.setattr(client_delegated, 'durable_write', write)
        original_save = client_link.save_record

        def save(state, value):
            nonlocal failed
            phase = {'issued_record': 'issued', 'approved_record': 'approved'}.get(interruption)
            if phase and not failed and value['state'] == phase:
                failed = True
                raise Failure('test_interrupted')
            return original_save(state, value)

        monkeypatch.setattr(client_link, 'save_record', save)
        original_send = RemoteAgents.send

        async def send(mailboxes, sender, recipient, message, message_id=None, **kwargs):
            nonlocal failed
            result = await original_send(
                mailboxes, sender, recipient, message, message_id, **kwargs
            )
            if interruption == 'task_send' and not failed:
                failed = True
                raise Failure('test_interrupted')
            return result

        monkeypatch.setattr(RemoteAgents, 'send', send)
        original_seed = client_link.seed_listener

        def seed(client, label, cursor):
            nonlocal failed
            if interruption == 'seed' and not failed:
                failed = True
                raise Failure('test_interrupted')
            return original_seed(client, label, cursor)

        monkeypatch.setattr(client_link, 'seed_listener', seed)
        track(owner)
        with pytest.raises(Failure, match='transport_uncertain|test_interrupted'):
            await run_command(owner, args('approve', code=code, minutes=15))
        assert failed
        if interruption in {'response', 'retryable_error', 'grant_file'}:
            assert journal.exists()
        # Reopen the profile: recovery depends on disk state, not the live objects.
        resumed = MsgClient(
            ClientState(tmp_path / 'owner', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        track(resumed)
        approved = await run_command(resumed, args('approve', code=code, minutes=None))
        assert len(issued) == (
            2 if interruption in {'response', 'retryable_error', 'grant_file'} else 1
        )
        assert len(set(issued)) == 1
        assert not journal.exists()
        again = await run_command(resumed, args('approve', code=code, minutes=None))
        assert again['data']['access_code'] == approved['data']['access_code']
        accepted = await run_command(helper, args('accept', code=approved['data']['access_code']))
        assert accepted['data']['task']['message'] == 'Review the parser patch and report.'
        assert load_record(resumed.state, 'reviewer')['minutes'] == 15
        await run_command(resumed, args('revoke', name='reviewer'))
        denied = await helper.call('discovery.get', {'id': '/@alice'})
        assert denied.error.code == 'authority_source_inactive'


@pytest.mark.asyncio
async def test_approval_binds_helper_and_ignores_old_label_grant(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, invited, joined = await prepared_link(app, tmp_path, http)
        # A stale file from a former implementation cannot stand in for this invitation.
        old = client_link.records_directory(owner.state) / 'reviewer.grant.json'
        old.write_bytes(canonical({'delegation_id': 'old', 'key_id': 'wrong'}))
        approved = await run_command(
            owner, args('approve', code=joined['data']['join_code'], minutes=None)
        )
        grant = decode_code(approved['data']['access_code'], 'access')['grant']
        assert grant['delegation_id'] != 'old'
        assert grant['key_id'] == helper.state.signer.key_id
        stranger = MsgClient(
            ClientState(tmp_path / 'stranger', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        with pytest.raises(Failure, match='link_claim_taken'):
            await run_command(stranger, args('join', code=invited['data']['invite_code']))
        with pytest.raises(Failure, match='link_approval_pending'):
            await run_command(owner, args('approve', code=joined['data']['join_code'], minutes=60))
        record = load_record(owner.state, 'reviewer')
        grant['key_id'] = stranger.state.signer.key_id
        grant_path(owner.state, record).write_bytes(canonical(grant))
        with pytest.raises(Failure, match='link_grant_mismatch'):
            await run_command(
                owner, args('approve', code=joined['data']['join_code'], minutes=None)
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('interruption', ['response', 'issued_record'])
async def test_revoke_does_not_hide_an_interrupted_issuance(
    installed, tmp_path, monkeypatch, interruption
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, _, _, joined = await prepared_link(app, tmp_path, http)
        original_call = owner.call
        original_save = client_link.save_record
        failed = False

        async def call(operation, arguments=None, **kwargs):
            nonlocal failed
            result = await original_call(operation, arguments, **kwargs)
            if (
                operation == 'identity.delegated_create'
                and interruption == 'response'
                and not failed
            ):
                failed = True
                raise Failure('test_interrupted')
            return result

        def save(state, value):
            nonlocal failed
            if value['state'] == 'issued' and interruption == 'issued_record' and not failed:
                failed = True
                raise Failure('test_interrupted')
            return original_save(state, value)

        monkeypatch.setattr(owner, 'call', call)
        monkeypatch.setattr(client_link, 'save_record', save)
        with pytest.raises(Failure, match='test_interrupted'):
            await run_command(
                owner, args('approve', code=joined['data']['join_code'], minutes=None)
            )
        if interruption == 'response':
            with pytest.raises(Failure, match='link_issuance_pending'):
                await run_command(owner, args('revoke', name='reviewer'))
            assert load_record(owner.state, 'reviewer')['state'] == 'invited'
            await run_command(
                owner, args('approve', code=joined['data']['join_code'], minutes=None)
            )
        revoked = await run_command(owner, args('revoke', name='reviewer'))
        assert revoked['data']['state'] == 'revoked'
        status = await run_command(owner, args('status', name='reviewer'))
        assert status.status == 'ok' and status.data['active'] is False


async def drain(client, label, capsys):
    """Run the same default-checkpoint listener as `msg --agent LABEL listen --remote --once`."""
    store = RemoteAgents(client)
    options = SimpleNamespace(
        cursor_file=None,
        cursor=None,
        agent=label,
        event=[],
        interval=0.01,
        once=True,
        max_events=None,
        max_pages=None,
        from_now=False,
    )

    async def fetch(cursor, tail=False):
        return await store.inbox(label, cursor=cursor, tail=tail)

    capsys.readouterr()
    await asyncio.wait_for(stream(client.state, options, fetch, 'remote'), timeout=60)
    return [loads(line)['message'] for line in capsys.readouterr().out.splitlines()]


@pytest.mark.asyncio
async def test_invite_join_approve_accept_exchange_and_revoke(installed, tmp_path, capsys):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, accepted, joined_code = await linked(app, tmp_path, http)
        task = accepted['data']['task']
        assert task['message'] == 'Review the parser patch and report.'
        assert task['from'] == '@alice#reviewer-lead'
        assert helper.state.subject == owner.state.subject
        assert helper.state.signer.key_id != owner.state.signer.key_id

        helper_box, owner_box = RemoteAgents(helper), RemoteAgents(owner)
        await helper_box.send('reviewer', 'reviewer-lead', 'Done: two fixes.', message_id='r1')
        # Both default listeners were seeded at approval: no history scan, no gaps.
        assert await drain(owner, 'reviewer-lead', capsys) == [
            'Accepted the task as ' + accepted['data']['address'] + '.',
            'Done: two fixes.',
        ]
        await owner_box.send('reviewer-lead', 'reviewer', 'Thanks, also check docs.')
        assert await drain(helper, 'reviewer', capsys) == [
            'Review the parser patch and report.',
            'Thanks, also check docs.',
        ]
        assert await drain(helper, 'reviewer', capsys) == []

        listed = await run_command(owner, args('list'))
        assert listed['data']['items'][0]['state'] == 'approved'
        status = await run_command(owner, args('status', name='reviewer'))
        assert status.status == 'ok', status

        revoked = await run_command(owner, args('revoke', name='reviewer'))
        assert revoked['data']['state'] == 'revoked'
        denied = await helper.call('discovery.get', {'id': '/@alice'})
        assert denied.error.code == 'authority_source_inactive'
        with pytest.raises(Failure, match='subagent_archived'):
            await owner_box.send('reviewer-lead', 'reviewer', 'late message')
        # Revocation is idempotent; the name stays reserved for its archived history.
        await run_command(owner, args('revoke', name='reviewer'))
        with pytest.raises(Failure, match='link_revoked'):
            await run_command(owner, args('approve', code=joined_code, minutes=None))
        with pytest.raises(Failure, match='link_exists'):
            await run_command(
                owner, args('invite', name='reviewer', task='again', task_file=None, minutes=5)
            )


@pytest.mark.asyncio
async def test_manual_join_recovers_when_helper_claim_is_unreachable(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, invited, _ = await prepared_link(app, tmp_path, http)
        invited = await run_command(
            owner, args('invite', name='offline-review', task='Review.', task_file=None, minutes=30)
        )
        helper = MsgClient(
            ClientState(tmp_path / 'offline-helper', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        original = helper.call

        async def disconnected(operation, arguments=None, **kwargs):
            if operation == 'identity.link_claim':
                raise Failure('transport_uncertain', retryable=True)
            return await original(operation, arguments, **kwargs)

        monkeypatch.setattr(helper, 'call', disconnected)
        joined = await run_command(
            helper, args('join', code=invited['data']['invite_code'], wait=False)
        )
        approved = await run_command(
            owner, args('approve', code=joined['data']['join_code'], minutes=None)
        )
        accepted = await run_command(helper, args('accept', code=approved['data']['access_code']))
        assert accepted['data']['task']['message'] == 'Review.'


@pytest.mark.asyncio
async def test_claim_is_approved_from_the_owner_listener(installed, tmp_path, capsys):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, invited, joined = await prepared_link(app, tmp_path, http)
        page = owner.checked(await owner.call('communication.changes', {'limit': 200}))
        assert any(
            item.get('type') == 'identity.link_claim'
            and item.get('data', {}).get('name') == 'reviewer'
            for item in page.data['items']
        )
        intruder = MsgClient(
            ClientState(tmp_path / 'intruder', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        with pytest.raises(Failure, match='link_claim_taken'):
            await run_command(intruder, args('join', code=invited['data']['invite_code']))
        watched = await run_command(owner, args('watch', once=True, interval=0.1))
        assert watched['data']['approved'] == ['reviewer']
        accepted = await run_command(
            helper, args('join', code=invited['data']['invite_code'], wait=True)
        )
        assert accepted['data']['task']['message'] == 'Review the parser patch and report.'
        await RemoteAgents(helper).send(
            'reviewer', 'reviewer-lead', 'Done via the claim.', message_id='claim-r1'
        )
        assert await drain(owner, 'reviewer-lead', capsys) == [
            'Accepted the task as ' + accepted['data']['address'] + '.',
            'Done via the claim.',
        ]
        assert joined['data']['join_code'].startswith('msglink1.')


@pytest.mark.asyncio
async def test_helper_ceiling_is_limited_to_the_link_mailboxes(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, _, _ = await linked(app, tmp_path, http)
        owner_box = RemoteAgents(owner)
        await owner_box.create('private')
        await owner_box.send('private', 'private', 'owner-only note', message_id='secret')
        private = '/@alice/files/agents/private'
        for operation, arguments in (
            ('discovery.get', {'id': private, 'view': 'meta'}),
            ('file.read', {'id': private + '/msg-secret.json'}),
            ('discovery.list', {'parent': '/@alice/files/agents'}),
            ('content.post_create', {'parent': '/main', 'body': 'not authorized'}),
        ):
            result = await helper.call(operation, arguments)
            assert result.error.code == 'credential_ceiling', (operation, result)
        with pytest.raises(Failure, match='credential_ceiling'):
            await RemoteAgents(helper).send('reviewer', 'private', 'escape attempt')
        # The helper cannot write into its own task inbox, only reply to the lead.
        with pytest.raises(Failure, match='credential_ceiling'):
            await RemoteAgents(helper).send('reviewer', 'reviewer', 'forged task')
        changes = helper.checked(await helper.call('communication.changes', {'limit': 200}))
        visible = {ref['id'] for item in changes.data['items'] for ref in item['resources']}
        secret = owner.checked(
            await owner.call('discovery.get', {'id': private + '/msg-secret.json', 'view': 'meta'})
        )
        assert secret.data['id'] not in visible


@pytest.mark.asyncio
async def test_codes_are_bound_to_kind_and_invitation(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        transport = HTTPTransport(app.settings.service_url, http=http)
        owner = MsgClient(
            ClientState(tmp_path / 'owner', server=app.settings.service_url),
            transport,
            clock=lambda: NOW,
        )
        assert (await owner.register('alice')).status == 'ok'
        invited = await run_command(
            owner, args('invite', name='reviewer', task='x', task_file=None, minutes=5)
        )
        code = invited['data']['invite_code']
        with pytest.raises(Failure, match='link_code_kind_mismatch'):
            await run_command(owner, args('approve', code=code, minutes=None))
        forged = decode_code(code, 'invite')
        forged.pop('version'), forged.pop('kind')
        forged['invite'] = '0' * 32
        helper = MsgClient(
            ClientState(tmp_path / 'helper', server=app.settings.service_url),
            transport,
            clock=lambda: NOW,
        )
        with pytest.raises(Failure, match='not_found'):
            await run_command(helper, args('join', code=encode_code('invite', forged)))
        from msg.client_delegated import prepare as prepare_delegation

        mismatched = encode_code(
            'join',
            {
                'name': 'reviewer',
                'invite': '0' * 32,
                'request': prepare_delegation(helper, '@alice'),
            },
        )
        with pytest.raises(Failure, match='link_invite_mismatch'):
            await run_command(owner, args('approve', code=mismatched, minutes=None))
        with pytest.raises(Failure, match='invalid_link_code'):
            decode_code('msglink1.%%%', 'invite')
        with pytest.raises(Failure, match='link_exists'):
            await run_command(
                owner, args('invite', name='reviewer', task='y', task_file=None, minutes=5)
            )


def test_cli_link_commands_are_reachable():
    parser = cli.parser()
    parsed = parser.parse_args([
        'link',
        'invite',
        'reviewer',
        '--task',
        'Check it',
        '--minutes',
        '45',
    ])
    assert parsed.command == 'link' and parsed.minutes == 45 and parsed.task == 'Check it'
    assert parser.parse_args(['link', 'accept', 'msglink1.x']).code == 'msglink1.x'


def test_link_profile_is_standard_private_and_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    server = 'https://msg.lmm.best'
    profile = client_link.link_directory(server, '@alice#reviewer')
    assert profile == tmp_path / 'data/msg/links/msg.lmm.best/alice/reviewer'
    assert client_link.link_directory(server, 'alice#reviewer') == profile
    assert client_link.link_directory('https://other.example', '@alice#reviewer') != profile
    assert client_link.link_directory(server, '@alice#tester') != profile
    for parent in (profile.parent, profile.parent.parent, profile.parent.parent.parent):
        assert parent.stat().st_mode & 0o077 == 0
    for bad in ('alice', '@alice#reviewer-lead', '@Alice#reviewer', '@alice#../x', '@alice#'):
        with pytest.raises(Failure):
            client_link.link_directory(server, bad)


def test_cli_link_option_selects_only_the_link_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    monkeypatch.delenv('MSG_SERVER', raising=False)
    parser = cli.parser()
    base = ['--link', '@alice#reviewer', 'link', 'join', 'msglink1.x']
    parsed = parser.parse_args(['--server', 'https://msg.lmm.best', *base])
    expected = tmp_path / 'msg/links/msg.lmm.best/alice/reviewer'
    assert client_link.select_link_profile(parsed) == expected == parsed.config_dir
    for extra in (['--account', 'me'], ['--config-dir', 'x'], ['--profile', 'p']):
        conflict = parser.parse_args(['--server', 'https://msg.lmm.best', *extra, *base])
        with pytest.raises(Failure, match='link_profile_conflict'):
            client_link.select_link_profile(conflict)
    with pytest.raises(Failure, match='server_required'):
        client_link.select_link_profile(parser.parse_args(base))


def test_terminal_output_is_the_copyable_prompt():
    from msg.client_display import render_text

    value = {'status': 'ok', 'data': {'invite_code': 'msglink1.x'}, 'prompt': 'Paste me\nverbatim'}
    assert render_text(value, context='link') == 'Paste me\nverbatim'
    assert decode_code(' msglink1.' + encode_code('join', {'a': 1})[9:] + '\n', 'join')['a'] == 1


@pytest.mark.asyncio
async def test_join_resumes_accepted_profile_and_checks_live_revocation(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, invited, joined = await prepared_link(app, tmp_path, http)
        approved = await run_command(
            owner, args('approve', code=joined['data']['join_code'], minutes=None)
        )
        accepted = await run_command(helper, args('accept', code=approved['data']['access_code']))
        key, recipient = helper.state.signer.key_id, helper.state.encryption_recipient
        repeated = await run_command(
            helper, args('join', code=invited['data']['invite_code'], wait=True)
        )
        assert repeated['data']['task'] == accepted['data']['task']
        assert helper.state.signer.key_id == key and helper.state.encryption_recipient == recipient
        commands = repeated['data']['commands']
        assert "--link '@alice#reviewer'" in commands['send']
        assert '--server ' in commands['send'] and '--agent reviewer' in commands['listen']
        # Profiles accepted by the old client have no saved access code.
        helper.state.data['agent_link'].pop('access_code')
        helper.state._save()
        legacy = await run_command(
            helper, args('join', code=invited['data']['invite_code'], wait=True)
        )
        assert legacy['data']['task'] == accepted['data']['task']
        assert helper.state.signer.key_id == key
        await run_command(owner, args('revoke', name='reviewer'))
        with pytest.raises(Failure, match='authority_source_inactive'):
            await run_command(helper, args('join', code=invited['data']['invite_code'], wait=True))


@pytest.mark.asyncio
async def test_join_resumes_interrupted_acceptance_without_new_keys(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, invited, joined = await prepared_link(app, tmp_path, http)
        approved = await run_command(
            owner, args('approve', code=joined['data']['join_code'], minutes=None)
        )
        key = helper.state.signer.key_id
        original = RemoteAgents.send

        async def interrupted(*args, **kwargs):
            raise Failure('transport_uncertain', retryable=True)

        monkeypatch.setattr(RemoteAgents, 'send', interrupted)
        with pytest.raises(Failure, match='transport_uncertain'):
            await run_command(helper, args('accept', code=approved['data']['access_code']))
        assert helper.state.subject is not None
        monkeypatch.setattr(RemoteAgents, 'send', original)
        result = await run_command(
            helper, args('join', code=invited['data']['invite_code'], wait=True)
        )
        assert result['data']['task']['message'] == 'Review the parser patch and report.'
        assert helper.state.signer.key_id == key


@pytest.mark.asyncio
async def test_join_timeout_preserves_pending_profile(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        _, helper, invited, _ = await prepared_link(app, tmp_path, http)
        key, before = helper.state.signer.key_id, helper.state.path.read_bytes()
        with pytest.raises(Failure, match='link_wait_timeout'):
            await run_command(
                helper, args('join', code=invited['data']['invite_code'], wait=True, timeout=0)
            )
        assert helper.state.signer.key_id == key and helper.state.path.read_bytes() == before
