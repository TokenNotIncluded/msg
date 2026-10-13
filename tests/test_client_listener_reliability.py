"""Measure concrete listener boundaries; never infer a harness speed ranking."""

import asyncio
import io
import json
import os
import signal
import sys

import pytest

from msg import client_listener
from msg.client_events import LocalEvents
from msg.client_listener import listen
from msg.core.errors import Failure

CONTEXT = {'server': 'https://example.test', 'subject': 'alice', 'agent': 'worker'}


def page(items=(), cursor='end', has_more=False):
    return {'items': list(items), 'cursor': cursor, 'has_more': has_more}


async def test_remote_checkpoint_contains_no_bodies_and_rereads_pending(tmp_path):
    checkpoint = tmp_path / 'cursor.json'
    calls = []
    body = 'private original'

    async def fetch(cursor, tail=False):
        calls.append(cursor)
        return page([{'id': str(i), 'message': body} for i in range(3)])

    output = io.StringIO()
    await listen(
        fetch,
        context=CONTEXT,
        cursor_only=True,
        cursor_file=checkpoint,
        max_events=1,
        output=output,
    )
    saved = json.loads(checkpoint.read_text())
    assert saved['version'] == 2 and saved['cursor'] is None
    assert saved['processed'] == ['0'] and saved['pending'] == []
    assert body not in checkpoint.read_text()
    body = 'fresh authorized result'
    await listen(
        fetch,
        context=CONTEXT,
        cursor_only=True,
        cursor_file=checkpoint,
        max_events=2,
        output=output,
    )
    results = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [item['id'] for item in results] == ['0', '1', '2']
    assert [item['message'] for item in results[1:]] == [body, body]
    assert calls == [None, None]
    assert json.loads(checkpoint.read_text())['cursor'] == 'end'
    assert body not in checkpoint.read_text()


async def test_remote_resume_denied_has_no_cached_output(tmp_path):
    checkpoint = tmp_path / 'cursor.json'

    async def fetch(cursor, tail=False):
        return page([{'id': '1', 'message': 'secret'}, {'id': '2', 'message': 'secret'}])

    await listen(
        fetch,
        context=CONTEXT,
        cursor_only=True,
        cursor_file=checkpoint,
        max_events=1,
        output=io.StringIO(),
    )
    saved = checkpoint.read_bytes()

    async def denied(cursor, tail=False):
        raise Failure('permission_denied')

    output = io.StringIO()
    with pytest.raises(Failure, match='permission_denied'):
        await listen(
            denied,
            context=CONTEXT,
            cursor_only=True,
            cursor_file=checkpoint,
            once=True,
            output=output,
        )
    assert output.getvalue() == ''
    assert checkpoint.read_bytes() == saved
    assert b'secret' not in saved
    with pytest.raises(Failure, match='context_mismatch'):
        await listen(
            denied,
            context={**CONTEXT, 'subject': 'bob'},
            cursor_only=True,
            cursor_file=checkpoint,
            once=True,
            output=output,
        )


async def test_legacy_remote_cache_is_scrubbed_before_authorization(tmp_path):
    checkpoint = tmp_path / 'cursor.json'

    async def fetch(cursor, tail=False):
        return page([{'id': '1', 'message': 'secret'}, {'id': '2', 'message': 'secret'}])

    await listen(fetch, context=CONTEXT, cursor_file=checkpoint, max_events=1, output=io.StringIO())
    assert 'secret' in checkpoint.read_text()
    output = io.StringIO()

    async def denied(message_id):
        assert message_id == '2' and 'secret' not in checkpoint.read_text()
        raise Failure('credential_ceiling')

    with pytest.raises(Failure, match='credential_ceiling'):
        await listen(
            fetch,
            context=CONTEXT,
            cursor_only=True,
            resolve_legacy=denied,
            cursor_file=checkpoint,
            once=True,
            output=output,
        )
    assert output.getvalue() == ''
    assert json.loads(checkpoint.read_text())['legacy_pending'] == [{'id': '2'}]

    async def authorized(message_id):
        return {'id': message_id, 'message': 'fresh'}

    await listen(
        fetch,
        context=CONTEXT,
        cursor_only=True,
        resolve_legacy=authorized,
        cursor_file=checkpoint,
        max_events=1,
        output=output,
    )
    assert json.loads(output.getvalue())['message'] == 'fresh'
    assert 'fresh' not in checkpoint.read_text()


async def test_legacy_account_events_require_safe_migration(tmp_path):
    checkpoint = tmp_path / 'cursor.json'

    async def fetch(cursor, tail=False):
        return page([{'id': '1'}, {'id': '2', 'message': 'private'}])

    await listen(fetch, context=CONTEXT, cursor_file=checkpoint, max_events=1, output=io.StringIO())
    output = io.StringIO()
    with pytest.raises(Failure, match='requires_revalidation'):
        await listen(
            fetch,
            context=CONTEXT,
            cursor_only=True,
            cursor_file=checkpoint,
            once=True,
            output=output,
        )
    assert output.getvalue() == ''


async def test_idle_backoff_is_bounded_without_repeated_checkpoint_writes(tmp_path, monkeypatch):
    delays, writes = [], []
    original = client_listener._save

    def save(path, state):
        writes.append(state['cursor'])
        original(path, state)

    async def fetch(cursor, tail=False):
        return page(cursor='unchanged')

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 6:
            raise asyncio.CancelledError

    monkeypatch.setattr(client_listener, '_save', save)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await listen(
            fetch,
            context=CONTEXT,
            cursor_file=tmp_path / 'cursor.json',
            max_idle_interval=8,
            output=io.StringIO(),
        )
    assert delays == [1, 2, 4, 8, 8, 8]
    assert writes == ['unchanged']


async def test_idle_backoff_resets_on_activity(monkeypatch):
    calls, delays = 0, []

    async def fetch(cursor, tail=False):
        nonlocal calls
        calls += 1
        return page([{'id': 'result'}] if calls == 3 else [], cursor=str(calls))

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await listen(fetch, context=CONTEXT, max_idle_interval=30, output=io.StringIO())
    assert delays == [1, 2, 1, 1, 2]


@pytest.mark.parametrize('limit', [0, -1, True, 1.5, '2'])
async def test_invalid_event_limit_fails_before_fetch(limit):
    async def fetch(cursor, tail=False):
        raise AssertionError('Invalid input must not contact a source.')

    with pytest.raises(Failure, match='invalid_listener_event_limit'):
        await listen(fetch, context=CONTEXT, max_events=limit)


@pytest.mark.parametrize('bad', [None, True, '', [], {}])
async def test_cursor_only_requires_stable_event_ids(bad):
    async def fetch(cursor, tail=False):
        return page([{'id': bad}])

    output = io.StringIO()
    with pytest.raises(Failure, match='invalid_listener_page'):
        await listen(fetch, context=CONTEXT, cursor_only=True, once=True, output=output)
    assert output.getvalue() == ''


@pytest.mark.skipif(sys.platform != 'linux', reason='Native inotify test')
async def test_native_events_block_idle_and_wake_all_readers(tmp_path):
    database = tmp_path / 'mailbox.sqlite3'
    database.write_bytes(b'')
    calls = [0, 0]
    ready = [asyncio.Event(), asyncio.Event()]
    outputs = [io.StringIO(), io.StringIO()]
    with LocalEvents(database) as first, LocalEvents(database) as second:

        async def reader(index, events):
            async def fetch(cursor, tail=False):
                calls[index] += 1
                ready[index].set()
                return page([{'id': '1'}] if database.read_bytes() else [])

            return await listen(
                fetch,
                context=CONTEXT,
                max_events=1,
                wait_for_change=events.wait,
                output=outputs[index],
            )

        tasks = [asyncio.create_task(reader(0, first)), asyncio.create_task(reader(1, second))]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), 2)
            (tmp_path / 'unrelated.txt').write_text('not a mailbox change')
            await asyncio.sleep(0.04)
            assert calls == [1, 1] and all(not task.done() for task in tasks)
            database.write_bytes(b'committed message')
            assert await asyncio.wait_for(asyncio.gather(*tasks), 2) == [1, 1]
            assert calls == [2, 2]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert first._descriptor == second._descriptor == -1
    assert [json.loads(output.getvalue())['id'] for output in outputs] == ['1', '1']


@pytest.mark.skipif(sys.platform != 'linux', reason='Native inotify test')
async def test_write_between_subscription_and_wait_is_not_lost(tmp_path):
    database = tmp_path / 'mailbox.sqlite3'
    database.touch()
    with LocalEvents(database) as events:
        database.write_text('already committed')
        await asyncio.wait_for(events.wait(), 2)


@pytest.mark.skipif(sys.platform != 'linux', reason='Native inotify test')
async def test_removed_native_watch_fails_instead_of_sleeping_forever(tmp_path):
    directory = tmp_path / 'mail'
    directory.mkdir()
    with LocalEvents(directory / 'mailbox.sqlite3') as events:
        directory.rmdir()
        with pytest.raises(OSError, match='subscription was lost'):
            await asyncio.wait_for(events.wait(), 2)


async def test_hardlinked_checkpoint_is_rejected(tmp_path):
    target = tmp_path / 'target'
    target.write_text('{}')
    target.chmod(0o600)
    checkpoint = tmp_path / 'cursor.json'
    os.link(target, checkpoint)

    async def fetch(cursor, tail=False):
        raise AssertionError('Unsafe checkpoint must not contact a source.')

    with pytest.raises(Failure, match='unsafe_listener_checkpoint'):
        await listen(fetch, context=CONTEXT, cursor_file=checkpoint, once=True)


@pytest.mark.skipif(sys.platform != 'linux', reason='SIGKILL fault injection')
@pytest.mark.parametrize('phase', ['before_flush', 'after_flush', 'after_checkpoint'])
async def test_sigkill_recovery_no_message_loss_and_explicit_replay(tmp_path, phase):
    checkpoint = tmp_path / 'cursor.json'
    script = r"""
import asyncio, os, signal, sys
from pathlib import Path
from msg import client_listener as module
checkpoint, phase = Path(sys.argv[1]), sys.argv[2]
original = module._save

def save(path, state):
    original(path, state)
    if phase == 'after_checkpoint' and state.get('processed') == ['1']:
        os.kill(os.getpid(), signal.SIGKILL)
module._save = save

class Output:
    def write(self, value):
        if phase == 'before_flush':
            os.kill(os.getpid(), signal.SIGKILL)
        sys.stdout.write(value)
    def flush(self):
        sys.stdout.flush()
        if phase == 'after_flush':
            os.kill(os.getpid(), signal.SIGKILL)

async def fetch(cursor, tail=False):
    return {'items': [] if cursor == 'end' else [{'id': str(i)} for i in range(1,4)],
            'cursor': 'end', 'has_more': False}
asyncio.run(module.listen(fetch, context={'agent': 'worker'}, cursor_only=True,
                          cursor_file=checkpoint, once=True, output=Output()))
"""

    async def run(mode):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            '-c',
            script,
            str(checkpoint),
            mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            assert not stderr, stderr.decode()
            return process.returncode, [json.loads(line)['id'] for line in stdout.splitlines()]
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    code, first = await run(phase)
    assert code == -signal.SIGKILL
    code, second = await run('resume')
    assert code == 0
    delivered = first + second
    assert set(delivered) == {'1', '2', '3'}
    assert len(delivered) == (4 if phase == 'after_flush' else 3)
    assert json.loads(checkpoint.read_text())['cursor'] == 'end'
