"""Initial pages have no synthetic endpoint; continuations use real SQL keys."""
import pytest
from read_only_evidence import readonly_evidence
from test_service import call, register

from msg.core.codec import wire


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,version', [
    ('discovery.list', 1), ('discovery.search', 1),
    ('discovery.read_query', 1), ('discovery.read_query', 2),
    ('discovery.read_query', 3),
])
async def test_all_sorts_page_over_actual_database_order(installed, monkeypatch, operation, version):
    app, _ = installed
    key, subject, _ = await register(app, 'page-boundaries')
    ids = set()
    # A supplementary-plane name sorts beyond U+FFFF under bytewise collation;
    # locale databases may place that same artificial endpoint below ASCII.
    for name in ('Alpha', 'zulu', '\u65e5\u672c\u8a9e', '\U00010000'):
        created = await call(app, 'content.post_create',
            {'parent': '/main', 'name': name, 'body': 'page-boundary-needle'},
            key=key, subject=subject)
        assert created.status == 'ok', wire(created)
        ids.add(created.resources[0].id)
    async with readonly_evidence(app, monkeypatch):
        for sort, column in (('id', 'id'), ('time', 'created_at'), ('name', 'name')):
            for direction in ('asc', 'desc'):
                order = 'ASC' if direction == 'asc' else 'DESC'
                async with app.metadata.transaction(write=False) as tx:
                    expected = [row[0] for row in tx.rows(
                        "SELECT id FROM resources WHERE parent='t_main' AND owner=? "
                        "AND type='post' AND state='active' "
                        f'ORDER BY {column} {order},id {order}', (subject,))]
                assert set(expected) == ids
                initial = {'parent': '/main', 'type': 'post', 'author': subject,
                           'query': 'page-boundary-needle', 'sort': sort,
                           'direction': direction, 'fields': ['id'], 'limit': 1}
                if version == 3:
                    initial['query_version'] = 3
                args = initial
                found, cursors = [], set()
                for index, expected_id in enumerate(expected):
                    result = await call(app, operation, args, key=key, subject=subject,
                                        contract_version=version)
                    assert result.status == 'ok', wire(result)
                    assert [item['id'] for item in result.data['items']] == [expected_id]
                    found.extend(item['id'] for item in result.data['items'])
                    more = index < len(expected) - 1
                    assert bool(result.data.get('next')) is more
                    if version == 3:
                        assert result.data['pageInfo']['hasNextPage'] is more
                    if more:
                        cursor = result.data['cursor']
                        assert cursor not in cursors
                        cursors.add(cursor)
                        args = ({'cursor': cursor} if operation == 'discovery.read_query'
                                else {**initial, 'cursor': cursor})
                assert found == expected and len(set(found)) == len(ids)
                empty = await call(app, operation, {**initial, 'query': 'absent-page-needle'},
                                   key=key, subject=subject, contract_version=version)
                assert empty.status == 'ok', wire(empty)
                assert empty.data['items'] == [] and not empty.data.get('next')
