"""Deterministic task scheduling does not call a model or use a database."""

import pytest

from msg.core.errors import Failure
from msg.core.task_graph import claim, create, finish, renew, settled, summary


def graph(concurrency=2):
    return create(
        [
            {'id': 'a', 'instruction': 'First'},
            {'id': 'b', 'instruction': 'Second'},
            {'id': 'join', 'instruction': 'Combine', 'dependencies': ['a', 'b']},
        ],
        concurrency,
    )


def take(value, worker='one', attempt='attempt-one', now=1):
    return claim(value, worker=worker, attempt=attempt, now=now, lease_seconds=10)


def test_fan_in_concurrency_results_and_idempotent_completion():
    original = graph()
    current, a = take(original)
    current, b = take(current, 'two', 'attempt-two')
    capped, nothing = take(current, 'three', 'attempt-three')
    assert nothing is None and capped == current
    assert summary(original)['running'] == 0
    args = {
        'task_id': a['id'],
        'worker': 'one',
        'attempt': 'attempt-one',
        'now': 2,
        'result_slot': 0,
    }
    current = finish(current, **args)
    assert finish(current, **args) == current
    assert summary(current)['ready'] == []
    current = finish(
        current, task_id=b['id'], worker='two', attempt='attempt-two', now=2, result_slot=1
    )
    assert summary(current)['ready'] == ['join']
    current, joined = take(current, 'three', 'attempt-three', now=3)
    assert joined['id'] == 'join'
    current = finish(
        current, task_id='join', worker='three', attempt='attempt-three', now=4, result_slot=2
    )
    assert summary(current)['completed'] == 3
    assert summary(current)['done']


def test_expired_work_is_not_automatically_executed_twice():
    current, _ = take(graph(1))
    expired = settled(current, 11)
    assert expired['tasks'][0]['status'] == 'failed'
    assert expired['tasks'][2]['status'] == 'blocked'
    next_graph, next_task = take(expired, 'two', 'attempt-two', now=12)
    assert next_task['id'] == 'b'
    with pytest.raises(Failure, match='task_result_conflict'):
        finish(next_graph, task_id='a', worker='one', attempt='attempt-one', now=12, result_slot=0)


def test_wrong_claim_or_conflicting_result_cannot_unlock_dependencies():
    current, task = take(graph())
    with pytest.raises(Failure, match='task_claim_mismatch'):
        finish(
            current,
            task_id=task['id'],
            worker='other',
            attempt=task['attempt'],
            now=2,
            result_slot=0,
        )
    current = finish(
        current, task_id='a', worker='one', attempt='attempt-one', now=2, result_slot=0
    )
    with pytest.raises(Failure, match='task_result_conflict'):
        finish(current, task_id='a', worker='one', attempt='attempt-one', now=2, result_slot=1)


def test_renew_and_failed_dependency_propagation():
    current, _ = take(graph())
    current = renew(
        current, task_id='a', worker='one', attempt='attempt-one', now=5, lease_seconds=20
    )
    assert settled(current, 11)['tasks'][0]['status'] == 'running'
    failed = finish(
        current, task_id='a', worker='one', attempt='attempt-one', now=12, error='test failed'
    )
    assert summary(failed)['blocked'] == 1
    assert summary(failed)['ready'] == ['b']


def test_dependency_results_must_be_available_to_the_claimant():
    current, task = claim(
        graph(), worker='one', attempt='a', now=1, lease_seconds=10, permitted={'b'}
    )
    assert task['id'] == 'b'
    assert summary(current)['ready'] == ['a']


@pytest.mark.parametrize(
    'tasks,code',
    [
        ([{'id': 'a', 'instruction': 'A', 'dependencies': ['absent']}], 'unknown_task_dependency'),
        ([{'id': 'a', 'instruction': 'A', 'dependencies': ['a']}], 'task_dependency_cycle'),
        ([{'id': 'a', 'instruction': 'A'}, {'id': 'a', 'instruction': 'B'}], 'duplicate_task_id'),
        (
            [
                {'id': 'a', 'instruction': 'A', 'dependencies': ['b', 'b']},
                {'id': 'b', 'instruction': 'B'},
            ],
            'duplicate_task_dependency',
        ),
    ],
)
def test_invalid_graphs(tasks, code):
    with pytest.raises(Failure, match=code):
        create(tasks, 1)


@pytest.mark.parametrize('concurrency', [True, 0, -1, 65, 1.5])
def test_invalid_concurrency(concurrency):
    with pytest.raises(Failure, match='invalid_task_concurrency'):
        graph(concurrency)
