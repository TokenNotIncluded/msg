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


async def test_checkpoint_lock_excludes_same_reader_and_releases_on_cancel(tmp_path):
    started = asyncio.Event()
    checkpoint = tmp_path / 'cursor.json'

    async def idle(cursor, tail=False):
        started.set()
        return page()

    first = asyncio.create_task(listen(idle, context=CONTEXT, cursor_file=checkpoint, interval=30))
    await asyncio.wait_for(started.wait(), 1)
    with pytest.raises(Failure, match='cursor_in_use'):
        await listen(idle, context=CONTEXT, cursor_file=checkpoint, once=True)
    # Different readers may run concurrently without contending on the first lock.
    assert (
        await listen(
            idle,
            context={**CONTEXT, 'agent': 'bot2'},
            cursor_file=tmp_path / 'bot2.json',
            once=True,
        )
        == 0
    )
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await listen(idle, context=CONTEXT, cursor_file=checkpoint, once=True) == 0
    assert stat.S_IMODE(checkpoint.with_name(checkpoint.name + '.lock').stat().st_mode) == 0o600


async def test_checkpoint_lock_rejects_symlink_and_unsafe_permissions(tmp_path):
    checkpoint = tmp_path / 'cursor.json'
    lock = tmp_path / 'cursor.json.lock'
    target = tmp_path / 'target'
    target.write_text('')
    lock.symlink_to(target)

    async def fetch(cursor, tail=False):
        raise AssertionError('Unsafe checkpoint lock must fail before contacting source')

    with pytest.raises(Failure, match='unsafe_listener_checkpoint'):
        await listen(fetch, context=CONTEXT, cursor_file=checkpoint, once=True)
    lock.unlink()
    lock.write_text('')
    lock.chmod(0o644)
    with pytest.raises(Failure, match='unsafe_listener_checkpoint'):
        await listen(fetch, context=CONTEXT, cursor_file=checkpoint, once=True)


async def test_sigint_process_releases_checkpoint_lock(tmp_path):
    import signal
    import sys

    checkpoint = tmp_path / 'process.json'
    script = """
import asyncio
import sys
from pathlib import Path
from msg.client_listener import listen

async def fetch(cursor, tail=False):
    return {'items': [], 'cursor': '0', 'has_more': False}

async def main():
    checkpoint = Path(sys.argv[1])
    task = asyncio.create_task(listen(fetch, context={'agent': 'bot'}, cursor_file=checkpoint, interval=30))
    while not checkpoint.exists():
        await asyncio.sleep(0.01)
    # Signal readiness only after the first durable checkpoint and idle wait.
    print('READY', flush=True)
    await task

try:
    asyncio.run(main())
except KeyboardInterrupt:
    sys.exit(130)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        '-c',
        script,
        str(checkpoint),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b'READY\n'
        process.send_signal(signal.SIGINT)
        assert await asyncio.wait_for(process.wait(), 5) == 130
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    async def fetch(cursor, tail=False):
        return page()

    assert await listen(fetch, context={'agent': 'bot'}, cursor_file=checkpoint, once=True) == 0
