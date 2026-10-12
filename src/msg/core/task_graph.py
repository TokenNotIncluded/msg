"""Bounded task state transitions. This module never starts an agent or runs a tool."""

from copy import deepcopy
from graphlib import CycleError, TopologicalSorter

from msg.core.errors import require

TERMINAL = frozenset({'succeeded', 'failed', 'blocked'})
MAX_TASKS = 256


def create(tasks, concurrency):
    require(type(concurrency) is int and 1 <= concurrency <= 64, 'invalid_task_concurrency')
    require(isinstance(tasks, list) and 1 <= len(tasks) <= MAX_TASKS, 'invalid_task_graph')
    records = {}
    for task in tasks:
        name = task['id']
        require(name not in records, 'duplicate_task_id')
        dependencies = task.get('dependencies', [])
        require(len(set(dependencies)) == len(dependencies), 'duplicate_task_dependency')
        records[name] = {
            'id': name,
            'instruction': task['instruction'],
            'dependencies': list(dependencies),
            'status': 'pending',
        }
    require(
        all(dep in records for task in records.values() for dep in task['dependencies']),
        'unknown_task_dependency',
    )
    try:
        order = list(
            TopologicalSorter({k: t['dependencies'] for k, t in records.items()}).static_order()
        )
    except CycleError:
        require(False, 'task_dependency_cycle')
    return {'concurrency': concurrency, 'tasks': [records[name] for name in order]}


def settled(graph, now):
    """Expire leases and propagate failure without making unfinished work runnable again."""
    graph = deepcopy(graph)
    tasks = {task['id']: task for task in graph['tasks']}
    for task in tasks.values():
        if task['status'] == 'running' and task['lease_until'] <= now:
            task.update(status='failed', error='lease_expired', finished_at=now)
        if task['status'] == 'pending' and any(
            tasks[dep]['status'] in {'failed', 'blocked'} for dep in task['dependencies']
        ):
            task.update(status='blocked', error='dependency_failed', finished_at=now)
    return graph


def summary(graph):
    tasks = {task['id']: task for task in graph['tasks']}
    counts = dict.fromkeys(('pending', 'running', 'succeeded', 'failed', 'blocked'), 0)
    ready = []
    for task in tasks.values():
        counts[task['status']] += 1
        if task['status'] == 'pending' and all(
            tasks[dep]['status'] == 'succeeded' for dep in task['dependencies']
        ):
            ready.append(task['id'])
    completed = sum(counts[status] for status in TERMINAL)
    return {
        **counts,
        'total': len(tasks),
        'completed': completed,
        'done': completed == len(tasks),
        'ready': ready,
        'available_slots': max(0, graph['concurrency'] - counts['running']),
    }


def claim(graph, *, worker, attempt, now, lease_seconds, permitted=None):
    graph = settled(graph, now)
    state = summary(graph)
    if not state['available_slots']:
        return graph, None
    for task in graph['tasks']:
        if task['id'] in state['ready'] and (permitted is None or task['id'] in permitted):
            task.update(
                status='running',
                worker=worker,
                attempt=attempt,
                lease_until=now + lease_seconds,
                started_at=now,
            )
            return graph, deepcopy(task)
    return graph, None


def finish(graph, *, task_id, worker, attempt, now, result_slot=None, error=None):
    graph = settled(graph, now)
    task = next((task for task in graph['tasks'] if task['id'] == task_id), None)
    require(task is not None, 'task_not_found')
    require(task.get('worker') == worker and task.get('attempt') == attempt, 'task_claim_mismatch')
    require((result_slot is not None) != (error is not None), 'task_result_required')
    target = 'succeeded' if result_slot is not None else 'failed'
    if task['status'] in TERMINAL:
        require(
            task['status'] == target
            and task.get('result_slot') == result_slot
            and task.get('error') == error,
            'task_result_conflict',
        )
        return graph
    require(task['status'] == 'running', 'task_not_running')
    task.update(status=target, finished_at=now)
    if result_slot is not None:
        task['result_slot'] = result_slot
    else:
        task['error'] = error
    return settled(graph, now)


def renew(graph, *, task_id, worker, attempt, now, lease_seconds):
    graph = settled(graph, now)
    task = next((task for task in graph['tasks'] if task['id'] == task_id), None)
    require(task is not None, 'task_not_found')
    require(task.get('worker') == worker and task.get('attempt') == attempt, 'task_claim_mismatch')
    require(task['status'] == 'running', 'task_not_running')
    task['lease_until'] = now + lease_seconds
    return graph
