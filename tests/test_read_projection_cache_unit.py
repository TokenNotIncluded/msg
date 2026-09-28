"""Cache validators match exact full or compact projections, never signed bytes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from msg.application import Application
from msg.config import write_example
from msg.core.codec import digest, wire
from msg.core.errors import Failure
from msg.plugins import discovery


@pytest.fixture
def get_handler(tmp_path):
    app = Application(write_example(tmp_path / 'etc', tmp_path / 'data',
                                    'https://cache.example',
                                    postgres_dsn='postgresql://localhost/unopened'))
    return app.registry.operation('discovery.get').handler


PAYLOADS = [
    {'id': 'r_cached', 'revision': None},
    {'id': 'r_cached', 'links': {'a': {'ref': {'id': 'u_author', 'revision': None}}}},
    {'id': 'r_cached', 'items': [{'id': 'r_child', 'revision': None}]},
    {'id': 'r_cached', 'content': [None, {'value': None}, 'null']},
]


@pytest.mark.asyncio
@pytest.mark.parametrize('data', PAYLOADS)
@pytest.mark.parametrize('compact', [False, True])
async def test_get_reuses_exact_full_and_compact_projection_digest(
        get_handler, monkeypatch, data, compact):
    project = AsyncMock(return_value=data)
    monkeypatch.setattr(discovery, 'read_projection', project)
    tx = SimpleNamespace(resource=AsyncMock(return_value=SimpleNamespace(id='r_cached')))
    known = digest(wire(data, compact=compact))
    request = SimpleNamespace(arguments={'id': 'r_cached', 'known_digest': known})
    result = await get_handler(None, request, tx)
    assert dict(result.data) == {'not_modified': True, 'digest': known}
    project.assert_awaited_once()  # The cache hint never replaces a current read.
    assert project.await_args.kwargs == {'revision': None, 'fields': ()}


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [
    {'id': 'r_cached', 'content': ['null']},
    {'id': 'r_other', 'content': [None, 'null']},
    {'id': 'r_cached'},
])
async def test_cache_hint_cannot_change_projection_values_or_array_nulls(
        get_handler, monkeypatch, changed):
    data = {'id': 'r_cached', 'revision': None, 'content': [None, 'null']}
    monkeypatch.setattr(discovery, 'read_projection', AsyncMock(return_value=data))
    tx = SimpleNamespace(resource=AsyncMock(return_value=SimpleNamespace(id='r_cached')))
    result = await get_handler(None, SimpleNamespace(arguments={
        'id': 'r_cached', 'known_digest': digest(changed)}), tx)
    assert wire(result.data) == data
    assert 'not_modified' not in result.data


@pytest.mark.asyncio
async def test_matching_cache_hint_never_suppresses_current_read_failure(get_handler, monkeypatch):
    data = {'id': 'r_cached', 'revision': None}
    project = AsyncMock(side_effect=Failure('permission_denied'))
    monkeypatch.setattr(discovery, 'read_projection', project)
    tx = SimpleNamespace(resource=AsyncMock(return_value=SimpleNamespace(id='r_cached')))
    request = SimpleNamespace(arguments={'id': 'r_cached',
        'known_digest': digest(wire(data, compact=True))})
    with pytest.raises(Failure) as raised:
        await get_handler(None, request, tx)
    assert raised.value.code == 'permission_denied'
    project.assert_awaited_once()
