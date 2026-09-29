"""Real first pages must not depend on a fabricated Unicode maximum key."""
import pytest

from msg.core.codec import wire
from read_only_evidence import readonly_evidence
from test_service import call, register


@pytest.mark.parametrize('operation,version', [
    ('discovery.list', 1), ('discovery.search', 1),
    ('discovery.read_query', 1), ('discovery.read_query', 2),
    ('discovery.read_query', 3),
])
@pytest.mark.parametrize('sort', ['id', 'time', 'name'])
@pytest.mark.parametrize('direction', ['asc', 'desc'])
async def test_first_page_and_continuations_use_actual_database_keys(
        installed, monkeypatch, operation, version, sort, direction):
    app, _ = installed
    key, subject, _ = await register(app, 'keyset-owner')
    # The supplementary-plane name also exceeds U+FFFF in byte/codepoint
    # ordering. Equal creation times exercise the existing ID tie breaker.
    names = ['Alpha', 'zebra', 'éclair', '𐐀-tail', 'unmatched']
    expected_ids = []
    for index, name in enumerate(names):
        result = await call(app, 'content.post_create', {
            'parent': '/main', 'name': name,
            'body': 'keysetneedle' if index < 4 else 'not a query match',
        }, key=key, subject=subject)
        assert result.status == 'ok', wire(result)
        if index < 4:
            expected_ids.append(result.resources[0].id)
    column = {'id': 'id', 'time': 'created_at', 'name': 'name'}[sort]
    ordering = 'ASC' if direction == 'asc' else 'DESC'
    async with app.metadata.transaction(write=False) as tx:
        # Independent SQL oracle has no synthetic first-page key. Use the
        # database's existing collation, not Python's Unicode sort order.
        expected = [row[0] for row in tx.rows(
            f'SELECT id FROM resources WHERE id IN (?,?,?,?) '
            f'ORDER BY {column} {ordering},id {ordering}', tuple(expected_ids))]
    assert len(expected) == 4
    arguments = {'parent': '/main', 'type': 'post', 'author': subject,
                 'query': 'keysetneedle', 'sort': sort, 'direction': direction,
                 'fields': ['id'], 'limit': 1}
    if version == 3:
        arguments['query_version'] = 3

    async def read(args):
        result = await call(app, operation, args, key=key, subject=subject,
                            contract_version=version)
        assert result.status == 'ok', wire(result)
        return result.data

    def continuation(cursor):
        return ({'cursor': cursor} if operation == 'discovery.read_query'
                else {**arguments, 'cursor': cursor})

    async with readonly_evidence(app, monkeypatch):
        query = arguments
        found = []
        cursors = []
        for index, expected_id in enumerate(expected):
            page = await read(query)
            assert list(page['items']) == [{'id': expected_id}]
            found.append(page['items'][0]['id'])
            if index < len(expected) - 1:
                assert page.get('next')
                cursor = page['cursor']
                assert cursor not in cursors, 'cursor did not advance'
                cursors.append(cursor)
                query = continuation(cursor)
            else:
                assert not page.get('next')
                if version == 3:
                    exhausted = await read(continuation(page['cursor']))
                    assert list(exhausted['items']) == []
                    assert exhausted['pageInfo']['hasNextPage'] is False
        assert found == expected
        assert len(found) == len(set(found))
        # Replaying a cursor repeats the same page, not an offset-dependent
        # or newly invented boundary, and still goes through authorization.
        repeated = await read(continuation(cursors[0]))
        assert list(repeated['items']) == [{'id': expected[1]}]
