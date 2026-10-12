import io
import json

import pytest

from msg.client_listener import listen
from msg.core.errors import Failure


async def test_resume_refreshes_pending_body_and_drops_revoked_item(tmp_path):
    path = tmp_path / 'cursor.json'
    output = io.StringIO()

    async def fetch(cursor, tail=False):
        return {
            'items': [{'id': 1}, {'id': 2, 'body': 'old secret'}, {'id': 3}],
            'cursor': 'end', 'has_more': False,
        }

    await listen(fetch, context={}, cursor_file=path, max_events=1, output=output)

    async def refresh(item):
        return {'id': 2, 'body': 'current allowed'} if item['id'] == 2 else None

    await listen(
        fetch, context={}, cursor_file=path, once=True, output=output, revalidate=refresh,
    )
    assert [json.loads(line) for line in output.getvalue().splitlines()] == [
        {'id': 1}, {'id': 2, 'body': 'current allowed'},
    ]
    assert json.loads(path.read_text())['pending'] == []


async def test_resume_permission_failure_leaves_checkpoint_and_stdout_unchanged(tmp_path):
    path = tmp_path / 'cursor.json'
    output = io.StringIO()

    async def fetch(cursor, tail=False):
        return {'items': [{'id': 1}, {'id': 2}], 'cursor': 'end', 'has_more': False}

    await listen(fetch, context={}, cursor_file=path, max_events=1, output=output)
    original = path.read_bytes()
    stdout = output.getvalue()

    async def refresh(item):
        raise Failure('permission_denied')

    with pytest.raises(Failure, match='permission_denied'):
        await listen(fetch, context={}, cursor_file=path, once=True, output=output, revalidate=refresh)
    assert path.read_bytes() == original
    assert output.getvalue() == stdout


async def test_resume_network_retry_reuses_pending_item(tmp_path, monkeypatch):
    path = tmp_path / 'cursor.json'
    output = io.StringIO()

    async def fetch(cursor, tail=False):
        return {'items': [{'id': 1}, {'id': 2}], 'cursor': 'end', 'has_more': False}

    await listen(fetch, context={}, cursor_file=path, max_events=1, output=output)
    calls = []

    async def refresh(item):
        calls.append(item['id'])
        if len(calls) == 1:
            raise OSError('offline')
        return item

    async def sleep(delay):
        pass

    monkeypatch.setattr('msg.client_listener.asyncio.sleep', sleep)
    await listen(fetch, context={}, cursor_file=path, once=True, output=output, revalidate=refresh)
    assert calls == [2, 2]
    assert len(output.getvalue().splitlines()) == 2
