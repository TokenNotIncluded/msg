import asyncio
import io
import json
import stat

import pytest

from msg.client_listener import listen
from msg.core.errors import Failure

CONTEXT = {'server': 'https://example.test', 'subject': 'alice', 'agent': 'bot1'}


def page(items=(), cursor='1', has_more=False):
    return {'items': list(items), 'cursor': cursor, 'has_more': has_more}


async def test_listener_drains_pages_without_sleep_and_filters(tmp_path, monkeypatch):
    calls = []
    pages = iter([page([{'type': 'other'}], '1', True), page([{'type': 'message'}], '2')])

    async def fetch(cursor, tail=False):
        calls.append(cursor)
        return next(pages)

    async def sleep(delay):
        raise AssertionError('Should not sleep while draining')

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    output = io.StringIO()
    checkpoint = tmp_path / 'cursor.json'
    assert (
        await listen(
            fetch,
            context=CONTEXT,
            cursor_file=checkpoint,
            event_types=['message'],
            once=True,
            output=output,
        )
        == 1
    )
    assert calls == [None, '1']
    assert json.loads(output.getvalue()) == {'type': 'message'}
    assert json.loads(checkpoint.read_text())['cursor'] == '2'
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600


async def test_exact_limit_retains_remainder_and_resume(tmp_path):
    checkpoint = tmp_path / 'cursor.json'
    output = io.StringIO()

    async def fetch(cursor, tail=False):
        return page([{'id': 1}, {'id': 2}, {'id': 3}], '3')

    assert (
        await listen(fetch, context=CONTEXT, cursor_file=checkpoint, max_events=1, output=output)
        == 1
    )
    assert json.loads(checkpoint.read_text())['pending'] == [{'id': 2}, {'id': 3}]
    assert (
        await listen(fetch, context=CONTEXT, cursor_file=checkpoint, max_events=2, output=output)
        == 2
    )
    assert [json.loads(line)['id'] for line in output.getvalue().splitlines()] == [1, 2, 3]


async def test_context_filter_and_symlink_rejected(tmp_path):
    checkpoint = tmp_path / 'cursor.json'

    async def fetch(cursor, tail=False):
        return page()

    await listen(fetch, context=CONTEXT, cursor_file=checkpoint, once=True)
    with pytest.raises(Failure, match='context_mismatch'):
        await listen(fetch, context={**CONTEXT, 'agent': 'bot2'}, cursor_file=checkpoint, once=True)
    with pytest.raises(Failure, match='context_mismatch'):
        await listen(
            fetch, context=CONTEXT, event_types=['message'], cursor_file=checkpoint, once=True
        )
    symlink = tmp_path / 'link'
    symlink.symlink_to(checkpoint)
    with pytest.raises(Failure, match='unsafe_listener_checkpoint'):
        await listen(fetch, context=CONTEXT, cursor_file=symlink, once=True)


async def test_no_checkpoint_before_successful_flush(tmp_path):
    checkpoint = tmp_path / 'cursor.json'

    async def fetch(cursor, tail=False):
        return page([{'id': 1}])

    class Broken(io.StringIO):
        def flush(self):
            raise OSError('broken pipe')

    with pytest.raises(OSError):
        await listen(fetch, context=CONTEXT, cursor_file=checkpoint, once=True, output=Broken())
    assert not checkpoint.exists()


async def test_idle_wakes_on_new_message_and_cancels_without_empty_output():
    output = io.StringIO()
    polls = 0

    async def fetch(cursor, tail=False):
        nonlocal polls
        polls += 1
        return page([{'id': 1}] if polls == 2 else [], str(polls))

    assert (
        await asyncio.wait_for(
            listen(fetch, context=CONTEXT, interval=0.001, max_events=1, output=output), 1
        )
        == 1
    )
    assert output.getvalue().splitlines() == ['{"id":1}']
    task = asyncio.create_task(listen(fetch, context=CONTEXT, interval=0.001, output=output))
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_from_now_tail_and_retry_are_read_only(tmp_path, monkeypatch, capsys):
    calls = []
    delays = []
    count = 0

    async def fetch(cursor, tail=False):
        nonlocal count
        calls.append((cursor, tail))
        count += 1
        if count == 1:
            raise OSError('SECRET must never be logged')
        return page([{'id': 'old' if tail else 'new'}], 'tail' if tail else 'next')

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    output = io.StringIO()
    assert await listen(fetch, context=CONTEXT, from_now=True, once=True, output=output) == 1
    assert calls == [(None, True), (None, True), ('tail', False)]
    assert json.loads(output.getvalue()) == {'id': 'new'}
    assert delays == [1.0]
    assert 'SECRET' not in capsys.readouterr().err


async def test_auth_failure_propagates_without_retry():
    async def fetch(cursor, tail=False):
        raise Failure('permission_denied')

    with pytest.raises(Failure, match='permission_denied'):
        await listen(fetch, context=CONTEXT)


async def test_retry_backoff_is_bounded_and_resets(monkeypatch, capsys):
    import httpx

    attempts = 0
    delays = []

    async def fetch(cursor, tail=False):
        nonlocal attempts
        attempts += 1
        if attempts <= 8:
            raise httpx.ConnectError('SECRET server credential')
        return page([{'type': 'ready'}])

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    assert await listen(fetch, context=CONTEXT, once=True, output=io.StringIO()) == 1
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30]
    assert 'SECRET' not in capsys.readouterr().err


async def test_nonadvancing_page_rejected():
    async def fetch(cursor, tail=False):
        return page(cursor=cursor, has_more=True)

    with pytest.raises(Failure, match='invalid_listener_page'):
        await listen(fetch, context=CONTEXT, cursor='same', once=True)
