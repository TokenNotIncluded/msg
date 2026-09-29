"""Legacy lease pages stay bounded while applying real current permissions."""
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from read_only_evidence import readonly_evidence
from test_service import NOW, call, register

from msg.core.codec import canonical, loads, wire


@pytest.mark.asyncio
@pytest.mark.parametrize('visible_count', [0, 3])
async def test_lease_pages_bound_sql_and_cross_hidden_batches(installed, monkeypatch, visible_count):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'lease-target-owner')
    reader_key, reader, _ = await register(app, 'lease-list-reader')
    targets, templates = [], []
    for name in ('hidden', 'visible'):
        post = await call(app, 'content.post_create',
            {'parent': '/main', 'name': name, 'body': name + '-lease-body'},
            key=owner_key, subject=owner)
        assert post.status == 'ok', wire(post)
        targets.append(post)
        acquired = await call(app, 'communication.lease_acquire',
            {'target': post.resources[0].id, 'purpose': name + '-lease-purpose',
             'expires_at': wire(NOW + timedelta(hours=1))},
            key=reader_key, subject=reader)
        assert acquired.status == 'ok', wire(acquired)
        async with app.metadata.transaction(write=False) as tx:
            templates.append(loads(tx.one('SELECT body FROM collaboration_leases WHERE id=?',
                (acquired.data['lease']['id'],))[0]))
    hidden = targets[0]
    changed = await call(app, 'content.chmod',
        {'id': hidden.resources[0].id, 'mode': '0700'}, key=owner_key, subject=owner,
        expected=((hidden.resources[0].id, hidden.data['generation']),))
    assert changed.status == 'ok', wire(changed)

    # Stress fixtures copy records originally created by real signed operations.
    # They are test-only authoritative rows, not a production migration claim.
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('DELETE FROM collaboration_leases WHERE holder=?', (reader,), write=True)
        for index in range(129 + visible_count):
            record = dict(templates[0 if index < 129 else 1])
            record['id'] = f'lease_page_{index:04d}'
            if index == 130:
                record.update(acquired_at=wire(NOW - timedelta(hours=2)),
                              expires_at=wire(NOW - timedelta(hours=1)))
            if index == 131:
                record.update(status='released', generation=2)
            tx.execute('INSERT INTO collaboration_leases VALUES (?,?,?,?,?,?,?)',
                (record['id'], reader, record['target'], record['status'],
                 record['generation'], record['expires_at'], canonical(record).decode()), write=True)
    transaction = app.metadata.transaction
    batches = []

    @asynccontextmanager
    async def bounded_transaction(*, write):
        assert write is False
        async with transaction(write=False) as tx:
            query_rows = tx.rows

            def bounded_rows(sql, parameters=()):
                lease_query = sql.startswith('SELECT id,target,status,expires_at,body FROM collaboration_leases ')
                if lease_query:
                    assert ' LIMIT ' in sql.upper(), 'lease_list issued an unbounded SELECT'
                rows = query_rows(sql, parameters)
                if lease_query:
                    assert len(rows) <= 128, 'lease_list materialized more than one bounded batch'
                    batches.append(len(rows))
                return rows

            with monkeypatch.context() as session_patch:
                session_patch.setattr(tx, 'rows', bounded_rows)
                yield tx

    async with readonly_evidence(app, monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(app.metadata, 'transaction', bounded_transaction)
            after = None
            seen, statuses = [], []
            for page in range(max(1, visible_count)):
                args = {'limit': 1}
                if after is not None:
                    args['after'] = after
                result = await call(app, 'communication.lease_list', args,
                                    key=reader_key, subject=reader)
                assert result.status == 'ok', wire(result)
                assert 'hidden-lease-purpose' not in str(wire(result))
                items = result.data['items']
                assert len(items) == (1 if visible_count else 0)
                seen.extend(item['id'] for item in items)
                statuses.extend(item['effective_status'] for item in items)
                if page == 0:
                    assert len(batches) >= 2, 'must scan past the first fully hidden batch'
                after = result.data['next_after']
                if page + 1 < visible_count:
                    assert after == items[-1]['id']
                else:
                    assert after is None
            assert seen == [f'lease_page_{i:04d}' for i in range(129, 129 + visible_count)]
            assert statuses == (['active', 'expired', 'released'] if visible_count else [])
