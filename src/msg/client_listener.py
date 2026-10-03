"""Independent, resumable JSONL readers; reading never acknowledges an event."""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import stat
import sys
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

import httpx

from msg.atomic_file import durable_write
from msg.core.errors import Failure


def _safe_path(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise Failure('unsafe_listener_checkpoint')
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise Failure('unsafe_listener_checkpoint')
        if info.st_mode & 0o077:
            raise Failure('unsafe_listener_checkpoint')


@contextmanager
def _checkpoint_lock(path: Path | None):
    if path is None:
        yield
        return
    _safe_path(path)
    lock = path.with_name(path.name + '.lock')
    _safe_path(lock)
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _safe_path(lock)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise Failure('unsafe_listener_checkpoint') from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Failure('unsafe_listener_checkpoint')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('cursor_in_use') from None
        yield
    finally:
        os.close(descriptor)


def _exclusive_checkpoint(function):
    @wraps(function)
    async def exclusive(fetch, *, cursor_file=None, **options):
        with _checkpoint_lock(cursor_file):
            return await function(fetch, cursor_file=cursor_file, **options)

    return exclusive


def _load(path: Path | None, binding: dict, cursor: str | None) -> dict:
    state = {'version': 1, 'context': binding, 'cursor': cursor, 'pending': [], 'has_more': False}
    if path is None:
        return state
    _safe_path(path)
    if not path.exists():
        return state
    try:
        saved = json.loads(path.read_text())
        valid = (
            saved.get('version') == 1
            and saved.get('context') == binding
            and isinstance(saved.get('pending'), list)
            and isinstance(saved.get('has_more'), bool)
            and (saved.get('cursor') is None or isinstance(saved.get('cursor'), str))
            and all(isinstance(item, dict) for item in saved['pending'])
        )
    except OSError, ValueError, AttributeError, KeyError:
        raise Failure('invalid_listener_checkpoint') from None
    if not valid:
        raise Failure('listener_checkpoint_context_mismatch')
    if cursor is not None and cursor != saved['cursor']:
        raise Failure('listener_checkpoint_cursor_conflict')
    return saved


def _save(path: Path | None, state: dict) -> None:
    if path is not None:
        _safe_path(path)
        durable_write(path, json.dumps(state, ensure_ascii=False).encode())


def _page(value: dict) -> dict:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get('items'), list)
        or not all(isinstance(item, dict) for item in value['items'])
        or not isinstance(value.get('cursor'), str)
        or not isinstance(value.get('has_more'), bool)
    ):
        raise Failure('invalid_listener_page')
    return value


@_exclusive_checkpoint
async def listen(
    fetch,
    *,
    context: dict,
    cursor_file: Path | None = None,
    cursor: str | None = None,
    interval: float = 1.0,
    event_types: tuple | list = (),
    once: bool = False,
    max_events: int | None = None,
    max_pages: int | None = None,
    from_now: bool = False,
    output=None,
) -> int:
    """Poll until cancelled, emitting and flushing one JSON object per matching event.

    ``fetch(cursor, tail=False)`` must return items, cursor and has_more. Cursor
    checkpoints contain the unprinted remainder of a page, so an exact event
    limit or restart never skips messages. Delivery is at least once if a process
    dies between printing and saving. Use a different checkpoint for every reader.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise Failure('invalid_listener_interval')
    if max_events is not None and (isinstance(max_events, bool) or max_events < 1):
        raise Failure('invalid_listener_event_limit')
    if max_pages is not None:
        if type(max_pages) is not int or max_pages < 1:
            raise Failure('invalid_listener_page_limit')
        if not once:
            raise Failure('listener_page_limit_requires_once')
    if not all(isinstance(value, str) and value for value in event_types):
        raise Failure('invalid_listener_event_filter')
    types = sorted(set(event_types))
    binding = {'source': context, 'event_types': types}
    state = _load(cursor_file, binding, cursor)
    if from_now and (cursor is not None or (cursor_file is not None and cursor_file.exists())):
        raise Failure('listener_start_position_conflict')
    stream = sys.stdout if output is None else output
    emitted = 0
    pages = 0
    retry_delay = interval

    async def read(tail=False):
        nonlocal retry_delay
        while True:
            try:
                value = _page(await fetch(state['cursor'], tail=tail))
                retry_delay = interval
                return value
            except Failure as exc:
                if not exc.retryable:
                    raise
            except OSError, TimeoutError, httpx.TransportError:
                pass
            print('msg listen: connection interrupted; retrying.', file=sys.stderr, flush=True)
            await asyncio.sleep(min(retry_delay, 30.0))
            retry_delay = min(retry_delay * 2, 30.0)

    if from_now:
        page = await read(tail=True)
        state.update(cursor=page['cursor'], pending=[], has_more=False)
        _save(cursor_file, state)

    while True:
        if not state['pending']:
            page = await read()
            pages += 1
            if page['has_more'] and page['cursor'] == state['cursor']:
                raise Failure('invalid_listener_page')
            state.update(cursor=page['cursor'], pending=page['items'], has_more=page['has_more'])
        while state['pending']:
            item = state['pending'][0]
            event_type = item.get('event_type', item.get('type'))
            if not types or event_type in types:
                stream.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')
                stream.flush()
                emitted += 1
            state['pending'] = state['pending'][1:]
            _save(cursor_file, state)
            if max_events is not None and emitted >= max_events:
                return emitted
        _save(cursor_file, state)
        if max_pages is not None and pages >= max_pages:
            if state['has_more']:
                print(
                    'msg listen: page limit reached; cursor saved, more pages remain.',
                    file=sys.stderr,
                    flush=True,
                )
            return emitted
        if state['has_more']:
            continue
        if once:
            return emitted
        await asyncio.sleep(interval)
