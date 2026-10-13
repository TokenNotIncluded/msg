import asyncio
import io
import json

import pytest

from msg.cli import parser
from msg.client_changes_wait import ChangesWait
from msg.client_listener import ListenerStatus, listen
from msg.core.errors import Failure

CONTEXT = {'server': 'https://example.test', 'subject': 'alice', 'agent': 'bot'}


def page(items=(), cursor='opaque-secret', has_more=False):
    return {'items': list(items), 'cursor': cursor, 'has_more': has_more}


def records(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_status_flag_is_opt_in_and_once_unchanged():
    args = parser().parse_args(['listen', '--status-json', '--once', '--max-events', '1'])
    assert args.status_json and args.once and args.max_events == 1
    assert not parser().parse_args(['listen']).status_json


async def test_tail_page_budget_and_resume_status_without_private_data(tmp_path):
    checkpoint = tmp_path / 'private-path.json'
    status_output, output = io.StringIO(), io.StringIO()
    calls = []

    async def fetch(cursor, tail=False):
        calls.append((cursor, tail))
        if tail:
            return page(cursor='tail-secret')
        if cursor == 'tail-secret':
            return page(cursor='next-secret', has_more=True)
        return page([{'id': 'event1', 'message': 'private-body'}])

    await listen(
        fetch,
        context=CONTEXT,
        cursor_file=checkpoint,
        from_now=True,
        once=True,
        max_pages=1,
        output=output,
        status=ListenerStatus(status_output),
    )
    assert [r['state'] for r in records(status_output)] == [
        'tail_initialized',
        'backlog',
        'stopped',
    ]
    assert records(status_output)[-1]['has_more'] is True
    assert output.getvalue() == ''
    await listen(
        fetch,
        context=CONTEXT,
        cursor_file=checkpoint,
        once=True,
        output=output,
        status=ListenerStatus(status_output),
    )
    assert json.loads(output.getvalue())['message'] == 'private-body'
    assert records(status_output)[-1]['reason'] == 'once'
    for secret in ('tail-secret', 'next-secret', 'opaque-secret', 'private-body', 'private-path'):
        assert secret not in status_output.getvalue()
    assert calls == [(None, True), ('tail-secret', False), ('next-secret', False)]


async def test_wait_one_heartbeats_do_not_repeat_status_and_cancel_resumes(tmp_path):
    checkpoint = tmp_path / 'cursor.json'
    output, statuses = io.StringIO(), io.StringIO()
    waiting = asyncio.Event()
    count = 0

    async def fetch(cursor, tail=False):
        return page()

    async def wait_fetch(cursor, tail=False):
        nonlocal count
        count += 1
        if count == 5:
            waiting.set()
            await asyncio.Event().wait()
        return page()

    task = asyncio.create_task(
        listen(
            fetch,
            context=CONTEXT,
            cursor_file=checkpoint,
            cursor_only=True,
            max_events=1,
            output=output,
            wait_fetch=wait_fetch,
            status=ListenerStatus(statuses),
        )
    )
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [r['state'] for r in records(statuses)] == ['caught_up', 'waiting', 'stopped']
    assert records(statuses)[-1]['reason'] == 'cancelled'
    assert output.getvalue() == ''

    async def resumed(cursor, tail=False):
        assert cursor == 'opaque-secret'
        return page([{'id': 'one'}, {'id': 'two'}], cursor='end-secret')

    await listen(
        resumed,
        context=CONTEXT,
        cursor_file=checkpoint,
        cursor_only=True,
        max_events=1,
        output=output,
        status=ListenerStatus(statuses),
    )
    assert json.loads(output.getvalue()) == {'id': 'one'}
    assert records(statuses)[-1]['pending'] is True
    assert records(statuses)[-1]['reason'] == 'event_limit'


async def test_retry_status_is_machine_json_and_caps_delay(monkeypatch, capsys):
    attempts = 0
    delays = []
    statuses = io.StringIO()

    async def fetch(cursor, tail=False):
        nonlocal attempts
        attempts += 1
        if attempts < 8:
            raise Failure('temporary', retryable=True)
        return page()

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    await listen(fetch, context=CONTEXT, once=True, status=ListenerStatus(statuses))
    assert delays == [1, 2, 4, 8, 16, 30, 30]
    assert capsys.readouterr().err == ''
    assert [r['delay_seconds'] for r in records(statuses) if r['state'] == 'backoff'] == [
        1,
        2,
        4,
        8,
        16,
        30,
    ]


def test_changes_wait_status_does_not_echo_warning(capsys):
    statuses = io.StringIO()
    wait = ChangesWait(None, status=ListenerStatus(statuses))
    wait.unavailable('server_wait_unsupported')
    wait.unavailable('server_wait_unsupported')
    assert capsys.readouterr().err == ''
    assert records(statuses) == [
        {
            'type': 'msg.listen.status',
            'version': 1,
            'state': 'polling_fallback',
            'reason': 'server_wait_unsupported',
        }
    ]
