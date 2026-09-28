"""Topic history work is bounded by the requested page, not global activity."""
import pytest
from test_service import call, register

from msg.core.codec import wire
from msg.plugins import content


@pytest.mark.asyncio
async def test_topic_history_decodes_only_one_page(installed, monkeypatch):
    app, _ = installed
    key, subject, _ = await register(app, 'security-events')
    made = await call(app, 'content.topic_create', {'parent': '/main', 'name': 'indexed-events'},
                      key=key, subject=subject)
    assert made.status == 'ok', wire(made)
    topic = made.resources[0].id
    for policy in ('approval', 'open', 'invite', 'closed'):
        result = await call(app, 'content.topic_policy_set', {'id': topic, 'membership_policy': policy},
                            key=key, subject=subject)
        assert result.status == 'ok', wire(result)
    for i in range(12):
        posted = await call(app, 'content.post_create', {'parent': '/main', 'body': f'Unrelated {i}'},
                            key=key, subject=subject)
        assert posted.status == 'ok', wire(posted)
    decoded = 0
    original = content.loads
    def observed(raw):
        nonlocal decoded
        value = original(raw)
        if isinstance(value, dict) and 'request_id' in value and 'type' in value:
            decoded += 1
        return value
    monkeypatch.setattr(content, 'loads', observed)
    cursor = None
    seen = []
    while True:
        decoded = 0
        args = {'id': topic, 'limit': 2, **({'cursor': cursor} if cursor else {})}
        page = await call(app, 'content.topic_events', args, key=key, subject=subject)
        assert page.status == 'ok', wire(page)
        assert decoded <= 3
        seen.extend(item['s'] for item in page.data['items'])
        cursor = page.data.get('cursor')
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 5
    assert seen == sorted(seen, reverse=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['postgres', 'sqlite'])
async def test_backfill_is_atomic_and_not_repeated(installed, tmp_path, backend):
    from msg.core.codec import canonical
    from msg.storage.sqlite import SqliteMetadataStore
    from msg.storage.topic_event_migration import EVENT_TYPES, MIGRATION_KEY, migrate_topic_events
    app, _ = installed
    store = app.metadata if backend == 'postgres' else SqliteMetadataStore(tmp_path / 'events.db')
    assert set(EVENT_TYPES) == set(content.TOPIC_EVENT_CODES)
    async with store.transaction(write=True) as tx:
        tx.execute('DELETE FROM settings WHERE key=?', (MIGRATION_KEY,), write=True)
        for identifier, kind, topic in [('historical', 'topic.create', 't_index'),
                                        ('unrelated', 'content.post_create', 't_index'),
                                        ('missing-topic', 'topic.create', None)]:
            body = canonical({'type': kind, 'data': {'topic_id': topic}}).decode()
            tx.execute('INSERT INTO events (id,body) VALUES (?,?)', (identifier, body), write=True)
        # Constructor migrations run on the already-held physical schema lock.
        migrate_topic_events(tx._connection, postgres=backend == 'postgres')
        assert tx.one('SELECT COUNT(*) FROM topic_event_projection WHERE topic=?', ('t_index',))[0] == 1
        assert tx.setting(MIGRATION_KEY) is True
        tx.execute('INSERT INTO events (id,body) VALUES (?,?)', ('not-a-normal-write',
            canonical({'type': 'topic.create', 'data': {'topic_id': 't_index'}}).decode()), write=True)
        migrate_topic_events(tx._connection, postgres=backend == 'postgres')
        assert tx.one('SELECT COUNT(*) FROM topic_event_projection WHERE topic=?', ('t_index',))[0] == 1
        # Both range shapes used by the rotating todo worker have supporting indexes.
        if backend == 'sqlite':
            plan = tx.rows("EXPLAIN QUERY PLAN SELECT id,body FROM resources "
                           "WHERE type='todo' AND state='active' AND id>? ORDER BY id LIMIT ?", ('', 3))
            assert 'resources_type_state' in str(plan)
        else:
            tx.execute('SET LOCAL enable_seqscan=off')
            plan = tx.rows("EXPLAIN SELECT id,body FROM resources WHERE type='todo' "
                           "AND state='active' AND id>? ORDER BY id LIMIT ?", ('', 3))
            assert 'resources_type_state' in str(plan)
