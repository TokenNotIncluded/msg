"""Due reminders are explicit worker writes, private and idempotent."""

from datetime import timedelta

import pytest
from test_service import NOW, call, register

from msg.core.codec import wire
from msg.workers.maintenance import run_maintenance


@pytest.mark.asyncio
async def test_due_todo_is_only_in_owner_inbox_and_repeated_maintenance_is_noop(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'due-owner')
    other_key, other, _ = await register(app, 'due-other')
    todo = await call(
        app,
        'identity.todo_put',
        {'name': 'due', 'title': 'Private due title', 'due_at': wire(NOW - timedelta(hours=1))},
        key=owner_key,
        subject=owner,
    )
    assert todo.status == 'ok', wire(todo)
    rid = todo.resources[0].id
    async with app.metadata.transaction(write=False) as tx:
        before = tx.one('SELECT COUNT(*) FROM audit')[0]
        assert tx.one('SELECT COUNT(*) FROM messages WHERE resource=?', (rid,))[0] == 0
    # Reads neither materialize the due reminder nor add an audit record.
    assert (await call(app, 'communication.inbox', {}, key=owner_key, subject=owner)).status == 'ok'
    assert (
        await call(app, 'identity.todo_get', {'name': 'due'}, key=owner_key, subject=owner)
    ).status == 'ok'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM audit')[0] == before
        assert tx.one('SELECT COUNT(*) FROM messages WHERE resource=?', (rid,))[0] == 0

    assert (await run_maintenance(app, 'deliver_due_todos', scheduled=True)) == {
        'delivered_todo_reminders': 1
    }
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM audit')[0] == before + 1
    assert (await run_maintenance(app, 'deliver_due_todos', scheduled=True)) == {
        'delivered_todo_reminders': 0
    }
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM audit')[0] == before + 1
        assert tx.one('SELECT COUNT(*) FROM messages WHERE resource=?', (rid,))[0] == 1
    own = await call(app, 'communication.inbox', {}, key=owner_key, subject=owner)
    notices = [item for item in own.data['items'] if item['resource']['id'] == rid]
    assert len(notices) == 1 and notices[0]['source'] == 'todo_due'
    assert notices[0]['recipient'] == owner
    assert notices[0]['todo_revision_at_delivery'] == todo.resources[0].revision
    assert not [
        item
        for item in (await call(app, 'communication.inbox', {}, key=other_key, subject=other)).data[
            'items'
        ]
        if item['resource']['id'] == rid
    ]
    assert not (
        await call(
            app, 'discovery.search', {'query': 'Private due title'}, key=other_key, subject=other
        )
    ).data.get('results', [])


@pytest.mark.asyncio
async def test_only_current_pending_due_revision_can_be_delivered(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'due-revisions')
    past = wire(NOW - timedelta(hours=1))
    future = wire(NOW + timedelta(days=2))
    original = await call(
        app,
        'identity.todo_put',
        {'name': 'rescheduled', 'title': 'Rescheduled', 'due_at': past},
        key=key,
        subject=owner,
    )
    rid = original.resources[0].id
    revised = await call(
        app,
        'identity.todo_put',
        {
            'name': 'rescheduled',
            'title': 'Rescheduled',
            'due_at': future,
            'expected_revision': original.resources[0].revision,
        },
        key=key,
        subject=owner,
        expected=((rid, original.data['generation']),),
    )
    assert revised.status == 'ok', wire(revised)
    completed = await call(
        app,
        'identity.todo_put',
        {'name': 'done', 'title': 'Completed', 'due_at': past, 'status': 'done'},
        key=key,
        subject=owner,
    )
    archived = await call(
        app,
        'identity.todo_put',
        {'name': 'archived', 'title': 'Archived', 'due_at': past},
        key=key,
        subject=owner,
    )
    archived_id = archived.resources[0].id
    result = await call(
        app,
        'identity.todo_archive',
        {'name': 'archived', 'expected_revision': archived.resources[0].revision},
        key=key,
        subject=owner,
        expected=((archived_id, archived.data['generation']),),
    )
    assert result.status == 'ok', wire(result)
    assert (await run_maintenance(app, 'deliver_due_todos', scheduled=True)) == {
        'delivered_todo_reminders': 0
    }
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT COUNT(*) FROM messages WHERE resource IN (?,?,?)',
                (rid, completed.resources[0].id, archived_id),
            )[0]
            == 0
        )

    # A later due instant is a distinct reminder, delivered only once then.
    app.clock = lambda: NOW + timedelta(days=3)
    assert (await run_maintenance(app, 'deliver_due_todos', scheduled=True)) == {
        'delivered_todo_reminders': 1
    }
    assert (await run_maintenance(app, 'deliver_due_todos', scheduled=True)) == {
        'delivered_todo_reminders': 0
    }
    async with app.metadata.transaction(write=False) as tx:
        row = tx.one('SELECT body FROM messages WHERE resource=?', (rid,))
        assert row is not None and future in row[0] and past not in row[0]
        assert tx.one('SELECT COUNT(*) FROM messages WHERE resource=?', (archived_id,))[0] == 0
