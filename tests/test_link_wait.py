"""Claim-bound waits use real PostgreSQL signals through the HTTP adapter."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_client_link import args, prepared_link
from test_service import NOW

from msg import client_link
from msg.client_link import LinkWait, decode_code, run_command
from msg.core.codec import b64, wire
from msg.core.errors import Failure
from msg.transports.http import create_app


def claim_args(joined):
    code = decode_code(joined['data']['join_code'], 'join')
    return {
        'invite_id': code['invite'],
        **{
            key: code['request'][key]
            for key in (
                'grantor',
                'public_key',
                'encryption_recipient',
                'possession_proof',
            )
        },
    }


async def waiting(signal):
    for _ in range(100):
        if signal.waiters:
            await asyncio.sleep(0.1)
            return
        await asyncio.sleep(0.01)
    pytest.fail('waiter did not register')


@pytest.mark.asyncio
async def test_collect_claim_bound_race_revocation_cancel_and_expiry(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, invited, joined = await prepared_link(app, tmp_path, http)
        claim = claim_args(joined)
        signal = app.metadata.event_signal
        bad = {**claim, 'possession_proof': {**claim['possession_proof'], 'value': b64(bytes(64))}}
        result = await helper.call(
            'identity.link_collect', {**bad, 'wait_ms': 200}, anonymous=True, contract_version=2
        )
        assert result.error.code == 'invalid_signature' and signal.waiters == 0
        wrong = {**claim, 'invite_id': '0' * 32, 'wait_ms': 200}
        assert (
            await helper.call('identity.link_collect', wrong, anonymous=True, contract_version=2)
        ).error.code == 'not_found'

        pending = asyncio.create_task(
            helper.call(
                'identity.link_collect',
                {**claim, 'wait_ms': 2000},
                anonymous=True,
                contract_version=2,
            )
        )
        await waiting(signal)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert signal.waiters == 0

        # No notification is required for expiry: the bounded deadline rechecks it.
        async with app.metadata.transaction(write=True) as tx:
            tx.execute(
                'UPDATE link_invitations SET expires_at=? WHERE invite_id=?',
                (wire(NOW + timedelta(milliseconds=150)), claim['invite_id']),
                write=True,
            )
        pending = asyncio.create_task(
            helper.call(
                'identity.link_collect',
                {**claim, 'wait_ms': 2000},
                anonymous=True,
                contract_version=2,
            )
        )
        await waiting(signal)
        app.executor.clock = lambda: NOW + timedelta(seconds=1)
        expired = await asyncio.wait_for(pending, 1)
        assert expired.data['state'] == 'expired' and signal.waiters == 0
        app.executor.clock = lambda: NOW
        async with app.metadata.transaction(write=True) as tx:
            tx.execute(
                'UPDATE link_invitations SET expires_at=? WHERE invite_id=?',
                (wire(NOW + timedelta(minutes=30)), claim['invite_id']),
                write=True,
            )
        pending = asyncio.create_task(
            helper.call(
                'identity.link_collect',
                {**claim, 'wait_ms': 2000},
                anonymous=True,
                contract_version=2,
            )
        )
        await waiting(signal)
        owner.checked(await owner.call('identity.link_close', {'invite_id': claim['invite_id']}))
        revoked = await asyncio.wait_for(pending, 2)
        assert revoked.error.code == 'link_revoked' and signal.waiters == 0


@pytest.mark.asyncio
async def test_owner_wait_claim_before_registration_privacy_and_join_exit(
    installed, tmp_path, monkeypatch
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, invited, joined = await prepared_link(app, tmp_path, http)
        claim = claim_args(joined)
        ident = claim['invite_id']
        signal = app.metadata.event_signal
        original = signal.start
        called = False

        # Put the durable claim in the registration gap, after the first read.
        async with app.metadata.transaction(write=True) as tx:
            tx.execute(
                'UPDATE link_invitations SET claim=NULL WHERE invite_id=?', (ident,), write=True
            )

        async def start(timeout=2):
            nonlocal called
            if not called:
                called = True
                helper.checked(await helper.call('identity.link_claim', claim, anonymous=True))
            return await original(timeout)

        monkeypatch.setattr(signal, 'start', start)
        pending = owner.checked(
            await owner.call(
                'identity.link_pending',
                {'invite_ids': [ident], 'wait_ms': 2000},
                contract_version=2,
            )
        )
        assert [item['invite_id'] for item in pending.data['items']] == [ident]
        assert called and signal.waiters == 0
        monkeypatch.setattr(signal, 'start', original)
        denied = await helper.call(
            'identity.link_pending',
            {'invite_ids': [ident], 'wait_ms': 2000},
            contract_version=2,
            anonymous=True,
        )
        assert denied.status == 'error' and signal.waiters == 0
        assert (
            owner.checked(
                await owner.call(
                    'identity.link_pending', {'invite_ids': [], 'wait_ms': 0}, contract_version=2
                )
            ).data['items']
            == ()
        )

        joined_wait = asyncio.create_task(
            run_command(
                helper, args('join', code=invited['data']['invite_code'], wait=True, timeout=10)
            )
        )
        await waiting(signal)
        watched = await run_command(owner, args('watch', once=True, interval=0.1))
        assert watched['data']['approved'] == ['reviewer']
        accepted = await asyncio.wait_for(joined_wait, 10)
        assert accepted['data']['task']['message'] == 'Review the parser patch and report.'
        assert signal.waiters == 0
        # v1 retains released items, v2 cannot busy-loop on already issued grants.
        assert owner.checked(await owner.call('identity.link_pending')).data['items']
        assert (
            owner.checked(
                await owner.call(
                    'identity.link_pending', {'invite_ids': [ident]}, contract_version=2
                )
            ).data['items']
            == ()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'reason', ['unknown_operation', 'credential_ceiling', 'server_wait_unsupported']
)
async def test_explicit_legacy_fallback_is_same_operation_and_claim(reason, capsys):
    calls = []

    class Client:
        def checked(self, result):
            return result

        async def call(self, operation, arguments, **kwargs):
            calls.append((operation, arguments, kwargs))
            if kwargs.get('contract_version') == 2:
                if reason != 'server_wait_unsupported':
                    raise Failure(reason)
                return SimpleNamespace(data={'state': 'claimed', 'wait_supported': False})
            return SimpleNamespace(data={'state': 'claimed'})

    waiter = LinkWait(Client(), 'identity.link_collect')
    claim = {'invite_id': 'fixed', 'possession_proof': {'value': 'fixed'}}
    await waiter.read(claim, wait_ms=20000, anonymous=True)
    await waiter.read(claim, wait_ms=20000, anonymous=True)
    assert not waiter.enabled
    assert all(op == 'identity.link_collect' and kw['anonymous'] for op, _, kw in calls)
    assert calls[-1][1] == claim
    assert 'same-scope polling' in capsys.readouterr().err


@pytest.mark.asyncio
async def test_authority_failure_never_falls_back():
    class Client:
        def checked(self, value):
            return value

        async def call(self, *args, **kwargs):
            raise Failure('link_revoked')

    waiter = LinkWait(Client(), 'identity.link_collect')
    with pytest.raises(Failure, match='link_revoked'):
        await waiter.read({}, wait_ms=20000, anonymous=True)
    assert waiter.enabled


@pytest.mark.asyncio
@pytest.mark.parametrize('once', [True, False])
async def test_owner_cli_once_never_waits_and_normal_wait_exits(monkeypatch, once):
    calls = []
    active = True

    class Client:
        state = object()

        def checked(self, value):
            return value

        async def call(self, operation, arguments=None, **kwargs):
            calls.append((operation, arguments, kwargs))
            items = (
                []
                if len(calls) == 1 and not once
                else [
                    {
                        'invite_id': 'fixed',
                        'name': 'worker',
                        'request': {},
                    }
                ]
            )
            return SimpleNamespace(data={'items': items, 'wait_supported': True})

    async def approve(*args, **kwargs):
        nonlocal active
        active = False

    monkeypatch.setattr(client_link, 'approve', approve)
    monkeypatch.setattr(
        client_link, 'load_record', lambda *args: {'invite_id': 'fixed', 'state': 'invited'}
    )
    monkeypatch.setattr(client_link, '_pending_local', lambda client: active)
    monkeypatch.setattr(client_link, '_pending_invites', lambda client: ['fixed'])
    result = await client_link.watch(Client(), SimpleNamespace(once=once, interval=0.1))
    assert result['data']['approved'] == ['worker']
    if once:
        assert len(calls) == 1 and calls[0][1] is None
    else:
        assert [a['wait_ms'] for _, a, _ in calls] == [0, 20000]
        assert all(k['contract_version'] == 2 and not k['anonymous'] for _, _, k in calls)


@pytest.mark.asyncio
async def test_owner_scope_budget_uses_complete_legacy_pending(capsys):
    calls = []

    class Client:
        def checked(self, value):
            return value

        async def call(self, operation, arguments, **kwargs):
            calls.append((operation, arguments, kwargs))
            return SimpleNamespace(data={'items': []})

    waiter = LinkWait(Client(), 'identity.link_pending')
    await waiter.read({'invite_ids': [str(i) for i in range(101)]}, wait_ms=20000)
    assert calls == [('identity.link_pending', {}, {'anonymous': False})]
    assert 'invite_scope_limit' in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', ['record', 'approval'])
async def test_owner_blocked_claim_backs_off_then_retries_and_cancels(monkeypatch, blocked):
    calls, delays = [], []
    active = True

    class Client:
        state = object()

        def checked(self, value):
            return value

        async def call(self, operation, arguments=None, **kwargs):
            calls.append(arguments)
            return SimpleNamespace(
                data={
                    'items': [
                        {
                            'invite_id': 'fixed',
                            'name': 'worker',
                            'request': {},
                        }
                    ],
                    'wait_supported': True,
                }
            )

    def record(*args):
        if blocked == 'record' and len(calls) < 3:
            raise Failure('not_found')
        return {'invite_id': 'fixed', 'state': 'invited'}

    async def approve(*args, **kwargs):
        nonlocal active
        if blocked == 'approval' and len(calls) < 3:
            raise Failure('link_approval_in_progress')
        active = False

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(client_link, 'load_record', record)
    monkeypatch.setattr(client_link, 'approve', approve)
    monkeypatch.setattr(client_link, '_pending_local', lambda client: active)
    monkeypatch.setattr(client_link, '_pending_invites', lambda client: ['fixed'])
    monkeypatch.setattr(client_link.asyncio, 'sleep', sleep)
    result = await client_link.watch(Client(), SimpleNamespace(once=False, interval=0.1))
    assert result['data']['approved'] == ['worker']
    assert len(calls) == 3 and delays == [0.1, 0.2]
    calls.clear()
    active = True

    async def cancelled(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(client_link.asyncio, 'sleep', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await client_link.watch(Client(), SimpleNamespace(once=False, interval=0.1))
    assert len(calls) == 1


def test_owner_invite_ids_are_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(client_link, 'records_directory', lambda state: tmp_path)
    for name in ['one.json', 'two.json']:
        (tmp_path / name).write_text('{"invite_id":"fixed","state":"invited"}')
    assert client_link._pending_invites(SimpleNamespace(state=None)) == ['fixed']
