from datetime import timedelta

from test_service import NOW, call, register

from msg.core.codec import wire


async def plan(app, key, owner, **extra):
    result = await call(app, 'communication.task_plan_create', {
        'title': 'Two tasks', 'concurrency': 1,
        'tasks': [
            {'id': 'a', 'instruction': 'Produce result'},
            {'id': 'b', 'instruction': 'Use result', 'dependencies': ['a']},
        ], **extra,
    }, key=key, subject=owner)
    assert result.status == 'ok', wire(result)
    return result


async def change(app, operation, previous, key, owner, **arguments):
    result = await call(
        app, 'communication.task_' + operation, {'id': previous.data['id'], **arguments},
        key=key, subject=owner,
        expected=((previous.data['id'], previous.data['generation']),),
    )
    assert result.status == 'ok', wire(result)
    return result


async def test_plan_releases_dependencies_correlates_results_and_limits_concurrency(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'task-owner')
    made = await plan(app, key, owner)
    claimed = await change(app, 'claim', made, key, owner, worker='one')
    assert claimed.data['task_id'] == 'a'
    busy = await change(app, 'claim', claimed, key, owner, worker='two')
    assert busy.data['task_id'] is None and busy.data['summary']['running'] == 1
    note = await call(app, 'identity.note_put', {'name': 'answer', 'body': 'fixed result'},
                      key=key, subject=owner)
    assert note.status == 'ok', wire(note)
    completed = await change(
        app, 'complete', busy, key, owner, task_id='a', worker='one',
        attempt=claimed.data['attempt'], result=wire(note.resources[0]),
    )
    assert completed.data['summary']['ready'] == ['b']
    second = await change(app, 'claim', completed, key, owner, worker='two')
    assert second.data['task_id'] == 'b'
    final = await change(
        app, 'complete', second, key, owner, task_id='b', worker='two',
        attempt=second.data['attempt'], result=wire(note.resources[0]),
    )
    assert final.data['summary']['done'] and final.data['summary']['succeeded'] == 2
    state = await call(app, 'communication.task_status', {'id': made.data['id']}, key=key, subject=owner)
    assert state.data['plan']['graph']['tasks'][0]['result'] == wire(note.resources[0])
    assert state.data['plan']['status'] == 'fulfilled'
    old = await call(app, 'communication.request_claim', {'id': made.data['id']}, key=key,
                     subject=owner, expected=((made.data['id'], final.data['generation']),))
    assert old.error.code == 'task_plan_required'


async def test_read_sharing_does_not_grant_task_mutation_or_dependency_result(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'task-private')
    other_key, other, _ = await register(app, 'task-reader')
    made = await plan(app, key, owner)
    shared = await call(app, 'sharing.grant', {
        'resource': made.data['id'], 'grantee': other,
        'expires_at': wire(NOW + timedelta(days=1)),
    }, key=key, subject=owner)
    assert shared.status == 'ok', wire(shared)
    denied = await call(app, 'communication.task_claim', {'id': made.data['id'], 'worker': 'reader'},
                        key=other_key, subject=other,
                        expected=((made.data['id'], made.data['generation']),))
    assert denied.error.code == 'task_worker_forbidden'
    claimed = await change(app, 'claim', made, key, owner, worker='one')
    note = await call(app, 'identity.note_put', {'name': 'secret', 'body': 'private result'},
                      key=key, subject=owner)
    completed = await change(app, 'complete', claimed, key, owner, task_id='a', worker='one',
                             attempt=claimed.data['attempt'], result=wire(note.resources[0]))
    state = await call(app, 'communication.task_status', {'id': made.data['id']},
                       key=other_key, subject=other)
    assert state.status == 'ok', wire(state)
    assert note.resources[0].id not in str(wire(state.data))
    assert 'result' not in state.data['plan']['graph']['tasks'][0]
    assert completed.data['summary']['ready'] == ['b']


async def test_generation_fences_racing_claims_and_unpinned_results(installed):
    import asyncio

    app, _ = installed
    key, owner, _ = await register(app, 'task-race')
    made = await plan(app, key, owner)
    args = {'id': made.data['id'], 'worker': 'one'}
    results = await asyncio.gather(*[
        call(app, 'communication.task_claim', args, key=key, subject=owner,
             expected=((made.data['id'], made.data['generation']),))
        for _ in range(2)
    ])
    assert sorted(result.status for result in results) == ['error', 'ok']
    winner = next(result for result in results if result.status == 'ok')
    loser = next(result for result in results if result.status == 'error')
    assert loser.error.code == 'generation_conflict'
    bad = await call(app, 'communication.task_complete', {
        'id': made.data['id'], 'task_id': 'a', 'worker': 'one',
        'attempt': winner.data['attempt'], 'result': {'id': made.data['id']},
    }, key=key, subject=owner, expected=((made.data['id'], winner.data['generation']),))
    assert bad.error.code == 'task_result_revision_required'
