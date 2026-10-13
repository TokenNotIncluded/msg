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
        if info.st_mode & 0o077 or info.st_nlink != 1:
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
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
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


def _event_id(item):
    value = item.get('id')
    if type(value) not in (str, int) or value == '':
        raise Failure('invalid_listener_page')
    return value


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
            saved.get('version') in (1, 2)
            and saved.get('context') == binding
            and isinstance(saved.get('pending'), list)
            and isinstance(saved.get('has_more'), bool)
            and (saved.get('cursor') is None or isinstance(saved.get('cursor'), str))
            and all(isinstance(item, dict) for item in saved['pending'])
        )
        if saved.get('version') == 2:
            valid = (
                valid
                and not saved['pending']
                and isinstance(saved.get('processed'), list)
                and all(type(value) in (str, int) and value != '' for value in saved['processed'])
                and isinstance(saved.get('legacy_pending'), list)
                and all(
                    isinstance(item, dict)
                    and set(item) == {'id'}
                    and type(item['id']) in (str, int)
                    and item['id'] != ''
                    for item in saved['legacy_pending']
                )
            )
    except OSError, ValueError, AttributeError, KeyError, TypeError:
        raise Failure('invalid_listener_checkpoint') from None
    if not valid:
        raise Failure('listener_checkpoint_context_mismatch')
    if cursor is not None and cursor != saved['cursor']:
        raise Failure('listener_checkpoint_cursor_conflict')
    return saved


def _snapshot(state):
    # Remote checkpoints contain positions and delivered IDs, never private
    # bodies. The uncommitted page is fetched again under current authority.
    return {**state, 'pending': []} if state['version'] == 2 else state


def _save(path: Path | None, state: dict) -> None:
    if path is not None:
        _safe_path(path)
        durable_write(path, json.dumps(_snapshot(state), ensure_ascii=False).encode())


def _binding(context: dict, event_types) -> dict:
    return {'source': context, 'event_types': sorted(set(event_types))}


def seed_checkpoint(path: Path, context: dict, cursor: str, event_types=()) -> bool:
    """Start a new listener at a known position; an existing checkpoint always wins."""
    binding = _binding(context, event_types)
    with _checkpoint_lock(path):
        if path.exists():
            _load(path, binding, None)
            return False
        _save(path, _load(None, binding, cursor))
        return True


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
    wait_for_change=None,
    max_idle_interval: float | None = None,
    cursor_only: bool = False,
    resolve_legacy=None,
    revalidate=None,
    server_wait: bool = False,
) -> int:
    """Drain a source, then wait for a native hint or a bounded polling delay.

    Subscribe to hints *before* calling this function. Hints carry no messages:
    every wake reads the durable source. With ``cursor_only``, stable event IDs
    and a page-start cursor replace cached bodies; restart rechecks source ACLs.
    An old body checkpoint requires ``resolve_legacy(id)`` to reread pending mail.
    Delivery is at least once across a crash between flush and checkpoint save.
    Output/flush is not a task ACK and cannot guarantee exactly-once execution.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise Failure('invalid_listener_interval')
    idle_limit = interval if max_idle_interval is None else max_idle_interval
    if not math.isfinite(idle_limit) or idle_limit < interval:
        raise Failure('invalid_listener_interval')
    if max_events is not None and (type(max_events) is not int or max_events < 1):
        raise Failure('invalid_listener_event_limit')
    if max_pages is not None:
        if type(max_pages) is not int or max_pages < 1:
            raise Failure('invalid_listener_page_limit')
        if not once:
            raise Failure('listener_page_limit_requires_once')
    if not all(isinstance(value, str) and value for value in event_types):
        raise Failure('invalid_listener_event_filter')
    binding = _binding(context, event_types)
    types = binding['event_types']
    state = _load(cursor_file, binding, cursor)
    restored = bool(state['pending'])
    if from_now and (cursor is not None or (cursor_file is not None and cursor_file.exists())):
        raise Failure('listener_start_position_conflict')
    if state['version'] == 2 and not cursor_only:
        raise Failure('listener_checkpoint_context_mismatch')
    if cursor_only and state['version'] == 1:
        if state['pending'] and resolve_legacy is None:
            raise Failure('listener_checkpoint_requires_revalidation')
        state = {
            **state,
            'version': 2,
            'pending': [],
            'processed': [],
            'legacy_pending': [{'id': _event_id(item)} for item in state['pending']],
        }
    if state.get('legacy_pending') and resolve_legacy is None:
        raise Failure('listener_checkpoint_requires_revalidation')
    stream = sys.stdout if output is None else output
    emitted = 0
    pages = 0
    retry_delay = interval
    idle_delay = interval
    saved_snapshot = None

    def persist():
        nonlocal saved_snapshot
        snapshot = json.dumps(_snapshot(state), ensure_ascii=False)
        if snapshot != saved_snapshot:
            _save(cursor_file, state)
            saved_snapshot = snapshot

    async def retry(call):
        nonlocal retry_delay
        while True:
            try:
                value = await call()
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

    async def read(tail=False):
        return _page(await retry(lambda: fetch(state['cursor'], tail=tail)))

    def emit(item):
        nonlocal emitted
        event_type = item.get('event_type', item.get('type'))
        if not types or event_type in types:
            stream.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')
            stream.flush()
            emitted += 1

    if cursor_only:
        # Remove legacy plaintext before contacting the server. Failure to
        # authorize a reread must never fall back to the cached private body.
        persist()
        while state['legacy_pending']:
            reference = state['legacy_pending'][0]
            item = await retry(lambda message_id=reference['id']: resolve_legacy(message_id))
            if item is None:
                # A formerly authorized item is no longer visible. Do not emit a
                # persisted body or discard unrelated pending references.
                state['legacy_pending'].pop(0)
                persist()
                continue
            if not isinstance(item, dict) or _event_id(item) != reference['id']:
                raise Failure('invalid_listener_page')
            emit(item)
            state['legacy_pending'].pop(0)
            persist()
            if max_events is not None and emitted >= max_events:
                return emitted

    if from_now:
        page = await read(tail=True)
        state.update(cursor=page['cursor'], pending=[], has_more=False)
        persist()

    while True:
        page_end = state['cursor']
        activity = bool(state['pending'])
        if not state['pending']:
            page = await read()
            pages += 1
            if page['has_more'] and page['cursor'] == state['cursor']:
                raise Failure('invalid_listener_page')
            if cursor_only:
                ids = [_event_id(item) for item in page['items']]
                if len(ids) != len(set(ids)):
                    raise Failure('invalid_listener_page')
            page_end = page['cursor']
            state.update(pending=page['items'], has_more=page['has_more'])
            if not cursor_only:
                state['cursor'] = page_end
            activity = bool(page['items'])
        processed = set(state.get('processed', ()))
        while state['pending']:
            item = state['pending'][0]
            if restored and revalidate is not None:
                # A legacy local checkpoint can contain a cached private body.
                # Re-fetch with today's authority before sending it to stdout.
                item = await retry(lambda: revalidate(state['pending'][0]))
                if item is None:
                    state['pending'] = state['pending'][1:]
                    persist()
                    continue
                if not isinstance(item, dict):
                    raise Failure('invalid_listener_page')
            if not cursor_only or _event_id(item) not in processed:
                emit(item)
                if cursor_only:
                    state['processed'].append(_event_id(item))
                    processed.add(_event_id(item))
            state['pending'] = state['pending'][1:]
            if cursor_only and not state['pending']:
                state.update(cursor=page_end, processed=[])
            persist()
            if max_events is not None and emitted >= max_events:
                return emitted
        restored = False
        if cursor_only:
            state.update(cursor=page_end, processed=[])
        persist()
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
        if activity:
            idle_delay = interval
        if wait_for_change is not None:
            await wait_for_change()
        elif not server_wait:
            await asyncio.sleep(idle_delay)
            if not activity:
                idle_delay = min(idle_delay * 2, idle_limit)
