"""Real signed Resource-backed work lists must never materialize an unbounded query."""
from contextlib import asynccontextmanager

import pytest
from read_only_evidence import readonly_evidence
from test_service import call, register

from msg.core.codec import wire
from msg.plugins.collaboration_resources import KINDS


async def create_records(app, key, subject, kind, count):
    arguments = {
        'request': {'title': 'Review', 'description': 'Bounded work', 'requirements': 'Read tests'},
        'offer': {'description': 'Review', 'scope': 'Python', 'availability': 'Today'},
        'checkpoint': {'summary': 'Resume', 'resource_refs': []},
    }
    if kind == 'proposal':
        posts = []
        for body in ('Before', 'After'):
            post = await call(app, 'content.post_create', {'parent': '/main', 'body': body},
                              key=key, subject=subject)
            assert post.status == 'ok', str(wire(post))
            posts.append(post.resources[0])
        arguments[kind] = {'target': posts[0].id, 'base_revision': posts[0].revision,
                           'content_ref': wire(posts[1]), 'message': 'Review bounded proposal'}
    identifiers = []
    for _ in range(count):
        made = await call(app, 'communication.' + kind + '_create', arguments[kind],
                          key=key, subject=subject)
        assert made.status == 'ok', str(wire(made))
        identifiers.append(made.data['id'])
    return identifiers


@asynccontextmanager
async def observe_batches(app, monkeypatch):
    transaction = app.metadata.transaction
    batches, violations = [], []

    @asynccontextmanager
    async def checked_transaction(*, write):
        assert write is False
        async with transaction(write=False) as tx:
            rows = tx.rows

            def checked_rows(sql, parameters=()):
                work_query = sql.startswith('SELECT id FROM resources WHERE owner=? AND type=? ')
                if work_query and ' LIMIT ' not in sql.upper():
                    violations.append(sql)
                    raise AssertionError('unbounded collaboration Resource SQL')
                result = rows(sql, parameters)
                if work_query:
                    batches.append(len(result))
                    if len(result) > 128:
                        violations.append('batch size=' + str(len(result)))
                        raise AssertionError('oversized collaboration Resource batch')
                return result

            with monkeypatch.context() as patch:
                patch.setattr(tx, 'rows', checked_rows)
                yield tx

    with monkeypatch.context() as patch:
        patch.setattr(app.metadata, 'transaction', checked_transaction)
        yield batches, violations


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,count,limit', [
    ('request', 3, 1), ('offer', 3, 1), ('checkpoint', 3, 1), ('proposal', 3, 1),
    ('checkpoint', 129, 64),
])
async def test_work_lists_use_bounded_sql_and_actual_continuations(
        installed, monkeypatch, kind, count, limit):
    app, _ = installed
    key, subject, _ = await register(app, 'work-list-bounds')
    ids = await create_records(app, key, subject, kind, count)
    other_key, other, _ = await register(app, 'other-work-owner')
    other_ids = await create_records(app, other_key, other, kind, 1)
    async with app.metadata.transaction(write=False) as tx:
        expected = [row[0] for row in tx.rows(
            "SELECT id FROM resources WHERE owner=? AND type=? AND state='active' ORDER BY id",
            (subject, KINDS[kind]))]
    assert set(expected) == set(ids)
    async with readonly_evidence(app, monkeypatch):
        async with observe_batches(app, monkeypatch) as (batches, violations):
            seen, after = [], None
            for offset in range(0, count, limit):
                args = {'limit': limit, **({'after': after} if after is not None else {})}
                page = await call(app, 'communication.' + kind + '_list', args,
                                  key=key, subject=subject)
                assert not violations, 'unbounded work-list SQL: ' + repr(violations)
                assert page.status == 'ok', str(wire(page))
                ids_on_page = [item['id'] for item in page.data['items']]
                assert ids_on_page == expected[offset:offset + limit]
                seen.extend(ids_on_page)
                more = offset + limit < count
                after = page.data['next_after']
                assert after == (ids_on_page[-1] if more else None)
                assert all(identifier not in str(wire(page)) for identifier in other_ids)
            assert seen == expected and len(set(seen)) == count
            assert batches and max(batches) <= 128
            empty = await call(app, 'communication.' + kind + '_list',
                                {'limit': limit, 'after': expected[-1]}, key=key, subject=subject)
            assert not violations, repr(violations)
            assert empty.status == 'ok', str(wire(empty))
            assert not empty.data['items'] and empty.data['next_after'] is None
