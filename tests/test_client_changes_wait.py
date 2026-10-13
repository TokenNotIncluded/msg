import asyncio
import io
from types import SimpleNamespace

import pytest

from msg.client_agent_cli import run_remote
from msg.client_changes_wait import ChangesWait
from msg.client_listener import listen
from msg.core.errors import Failure


class Client:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    async def call(self, operation, arguments, **options):
        self.calls.append((operation, arguments, options))
        page = next(self.pages)
        if isinstance(page, Failure):
            raise page
        return SimpleNamespace(data=page)

    @staticmethod
    def checked(result):
        return result


def page(items=(), more=False, cursor='next', supported=True):
    return {
        'items': list(items),
        'has_more': more,
        'sync_cursor': cursor,
        'wait_supported': supported,
    }


async def test_signed_version_keeps_sync_cursor():
    client = Client([page()])
    assert (await ChangesWait(client).read({'cursor': 'saved'}, wait=True))['sync_cursor'] == 'next'
    assert client.calls == [
        ('communication.changes', {'cursor': 'saved', 'wait_ms': 20000}, {'contract_version': 2})
    ]


@pytest.mark.parametrize('reason', ['unknown_operation', 'credential_ceiling'])
async def test_auto_scope_and_old_server_fallback_same_feed(reason, capsys):
    client = Client([Failure(reason), page(), page()])
    source = ChangesWait(client, interval=0.001)
    await source.read({'cursor': 'saved'})
    await source.read({'cursor': 'next'}, wait=True)
    assert [args for _, args, _ in client.calls] == [
        {'cursor': 'saved', 'wait_ms': 0},
        {'cursor': 'saved'},
        {'cursor': 'next'},
    ]
    assert 'using polling' in capsys.readouterr().err


@pytest.mark.parametrize('reason', ['unknown_operation', 'credential_ceiling'])
async def test_events_never_silently_falls_back(reason):
    client = Client([Failure(reason)])
    with pytest.raises(Failure, match='listener_events_unavailable'):
        await ChangesWait(client, mode='events').read({})
    assert len(client.calls) == 1


@pytest.mark.parametrize('mode', ['auto', 'events'])
async def test_auth_failure_is_not_compatibility(mode):
    client = Client([Failure('credential_expired')])
    with pytest.raises(Failure, match='credential_expired'):
        await ChangesWait(client, mode=mode).read({})
    assert len(client.calls) == 1


async def test_events_once_checks_support_without_waiting():
    client = Client([page()])
    await ChangesWait(client, mode='events', once=True).read({})
    assert client.calls[0][1]['wait_ms'] == 0
    with pytest.raises(Failure, match='listener_events_unavailable'):
        await ChangesWait(Client([Failure('unknown_operation')]), mode='events', once=True).read({})


async def test_retry_idle_read_retains_cursor():
    calls = []

    async def fetch(cursor, tail=False):
        return {'items': [], 'cursor': 'saved', 'has_more': False}

    async def wait_fetch(cursor, tail=False):
        calls.append(cursor)
        if len(calls) == 1:
            raise OSError('connection interrupted')
        return {'items': [{'id': 'done'}], 'cursor': 'end', 'has_more': False}

    assert (
        await listen(
            fetch,
            wait_fetch=wait_fetch,
            context={},
            interval=0.001,
            max_events=1,
            output=io.StringIO(),
        )
        == 1
    )
    assert calls == ['saved', 'saved']


async def test_saturated_events_fails_explicitly():
    with pytest.raises(Failure, match='listener_events_unavailable'):
        await ChangesWait(Client([page(supported=False)]), mode='events').read({}, wait=True)


@pytest.mark.parametrize('options', [{'mode': 'poll'}, {'once': True}])
async def test_poll_and_once_keep_legacy_nonwaiting_contract(options):
    client = Client([page()])
    await ChangesWait(client, **options).read({})
    assert client.calls == [('communication.changes', {}, {})]


async def test_filtered_has_more_drains_before_wait_and_max_one_exits(tmp_path):
    calls = []
    pages = iter([
        {'items': [], 'cursor': 'scan', 'has_more': True},
        {'items': [], 'cursor': 'drained', 'has_more': False},
        {'items': [{'id': 'mail', 'type': 'wanted'}], 'cursor': 'done', 'has_more': False},
    ])

    async def fetch(cursor, tail=False):
        calls.append(('read', cursor))
        return next(pages)

    async def wait_fetch(cursor, tail=False):
        calls.append(('wait', cursor))
        return next(pages)

    output = io.StringIO()
    checkpoint = tmp_path / 'reader.json'
    assert (
        await listen(
            fetch,
            wait_fetch=wait_fetch,
            context={},
            cursor_file=checkpoint,
            cursor_only=True,
            max_events=1,
            output=output,
        )
        == 1
    )
    assert calls == [('read', None), ('read', 'scan'), ('wait', 'drained')]
    assert 'mail' in output.getvalue() and 'mail' not in checkpoint.read_text()


async def test_once_drains_without_wait():
    calls = []

    async def fetch(cursor, tail=False):
        calls.append(cursor)
        return {'items': [], 'cursor': 'end', 'has_more': cursor is None}

    async def wait_fetch(*args, **kwargs):
        pytest.fail('once must never wait')

    assert await listen(fetch, wait_fetch=wait_fetch, context={}, once=True) == 0
    assert calls == [None, 'end']


async def test_normal_account_cli_from_tail_waits_once_then_exits(tmp_path):
    client = Client([
        {**page(), 'tail_cursor': 'tail'},
        page(cursor='drained'),
        page([{'id': 'done', 'type': 'wanted'}]),
    ])
    client.state = SimpleNamespace(server='https://msg.example', subject='owner')
    args = SimpleNamespace(
        command='listen',
        offline=False,
        user=None,
        subscribe=False,
        resource=None,
        agent=None,
        once=False,
        interval=0.001,
        wait_mode='events',
        cursor=None,
        cursor_file=tmp_path / 'account.json',
        event=['wanted'],
        max_events=1,
        max_pages=None,
        from_now=True,
    )
    assert await run_remote(client, args) == 0
    assert [arguments['wait_ms'] for _, arguments, _ in client.calls] == [0, 0, 20000]
    assert [arguments.get('cursor') for _, arguments, _ in client.calls] == [
        None,
        'tail',
        'drained',
    ]


async def test_cancel_wait_releases_independent_reader_lock(tmp_path):
    checkpoint = tmp_path / 'reader.json'
    waiting = asyncio.Event()

    async def fetch(cursor, tail=False):
        return {'items': [], 'cursor': 'end', 'has_more': False}

    async def wait_fetch(cursor, tail=False):
        waiting.set()
        await asyncio.Future()

    task = asyncio.create_task(
        listen(fetch, wait_fetch=wait_fetch, context={}, cursor_file=checkpoint, cursor_only=True)
    )
    await waiting.wait()
    with pytest.raises(Failure, match='cursor_in_use'):
        await listen(fetch, context={}, cursor_file=checkpoint, cursor_only=True, once=True)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await listen(fetch, context={}, cursor_file=checkpoint, cursor_only=True, once=True) == 0
