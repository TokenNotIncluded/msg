"""A transient batch sibling cannot bypass current mailbox authority checks."""

from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from msg.client import MsgClient
from msg.client_subagents_remote import RemoteAgents
from msg.core.errors import Failure
from msg.core.models import OperationError, OperationResult

OWNER = 'u_owner'
ROOT = '/@owner/files/agents'


def projections(*, archived=False):
    values = []
    for index in range(4):
        data = {
            'owner': OWNER,
            'mode': '0700' if index < 3 else '0600',
            'state': 'active',
            'type': 'topic' if index < 3 else 'file',
            'parent': 'r_mailbox',
        }
        if index == 3:
            data['content'] = {
                'version': 2,
                'owner': OWNER,
                'name': 'receiver',
                'archived': archived,
            }
        values.append(
            OperationResult(
                request_id=str(index),
                operation='discovery.get' if index < 3 else 'file.read',
                status='ok',
                actor=OWNER,
                subject=OWNER,
                data=data,
            )
        )
    return values


def failed(value, code, *, retryable=False):
    return replace(value, status='error', error=OperationError(code=code, retryable=retryable))


def adapter(values, *, fallback=False):
    calls = []
    batch = []
    serial = projections()
    paths = [ROOT.rpartition('/')[0], ROOT, ROOT + '/receiver', ROOT + '/receiver/agent.json']

    async def read_many(requests):
        batch.extend(requests)
        if isinstance(values, Exception):
            raise values
        return values

    async def call(operation, args):
        assert fallback, 'Permanent rejection must not enter the serial fallback'
        calls.append((operation, args['id']))
        return serial[paths.index(args['id'])]

    client = SimpleNamespace(
        state=SimpleNamespace(subject=OWNER),
        transport=SimpleNamespace(call_reads=read_many),
        prepare=lambda op, args: (op, args),
        checked=MsgClient.checked,
        call=call,
    )
    return RemoteAgents(client), calls, batch


@pytest.mark.parametrize('transient_index', (0, 3))
async def test_permanent_sibling_wins_in_either_order(transient_index):
    values = projections()
    permanent_index = 3 if transient_index == 0 else 0
    values[transient_index] = failed(values[transient_index], 'server_busy', retryable=True)
    values[permanent_index] = failed(values[permanent_index], 'credential_ceiling')
    agents, calls, _ = adapter(values)
    with pytest.raises(Failure, match='credential_ceiling'):
        await agents._receive_preflight(ROOT, 'receiver')
    assert calls == []


@pytest.mark.parametrize(
    'problem,expected',
    [
        ('public_directory', 'subagent_private_namespace_conflict'),
        ('foreign_config_owner', 'subagent_private_namespace_conflict'),
        ('archived_directory', 'subagent_archived'),
        ('misbound_config', 'invalid_subagent_message'),
    ],
)
async def test_invalid_success_projection_wins_over_transient_sibling(problem, expected):
    values = projections()
    index = 1 if problem.endswith('directory') else 3
    data = dict(values[index].data)
    if problem == 'public_directory':
        data['mode'] = '0711'
    elif problem == 'foreign_config_owner':
        data['owner'] = 'u_other'
    elif problem == 'archived_directory':
        data['state'] = 'archived'
    else:
        data['content'] = dict(data['content'], name='another-label')
    values[index] = replace(values[index], data=data)
    sibling = 3 if index < 3 else 0
    values[sibling] = failed(values[sibling], 'server_busy', retryable=True)
    agents, calls, _ = adapter(values)
    with pytest.raises(Failure, match=expected):
        await agents._receive_preflight(ROOT, 'receiver')
    assert calls == []


@pytest.mark.parametrize('index,expected', [(1, 'subagent_not_found'), (3, 'not_found')])
async def test_missing_directory_and_config_keep_distinct_errors(index, expected):
    values = projections()
    values[index] = failed(values[index], 'not_found')
    agents, calls, _ = adapter(values)
    with pytest.raises(Failure, match=expected):
        await agents._receive_preflight(ROOT, 'receiver')
    assert calls == []


@pytest.mark.parametrize(
    'code', ['credential_ceiling', 'permission_denied', 'invalid_graphql_result']
)
async def test_permanent_batch_failure_does_not_fall_back(code):
    agents, calls, _ = adapter(Failure(code))
    with pytest.raises(Failure, match=code):
        await agents._receive_preflight(ROOT, 'receiver')
    assert calls == []


@pytest.mark.parametrize(
    'failure',
    [
        None,
        httpx.ReadTimeout('synthetic read timeout'),
        httpx.ConnectError('synthetic connection failure'),
        Failure('server_busy', retryable=True),
        Failure('not_found'),
        Failure('dependency_unavailable'),
    ],
)
async def test_unsupported_or_transient_batch_rechecks_serially(failure):
    agents, calls, _ = adapter(failure, fallback=True)
    assert (await agents._receive_preflight(ROOT, 'receiver'))['parent'] == 'r_mailbox'
    assert [operation for operation, _ in calls] == [
        'discovery.get',
        'discovery.get',
        'discovery.get',
        'file.read',
    ]


async def test_retryable_sibling_alone_rechecks_all_projections_serially():
    values = projections()
    values[1] = failed(values[1], 'server_busy', retryable=True)
    agents, calls, _ = adapter(values, fallback=True)
    await agents._receive_preflight(ROOT, 'receiver')
    assert len(calls) == 4


@pytest.mark.parametrize('archived', [False, True])
async def test_receive_batch_keeps_private_binding_and_archived_label_history(archived):
    agents, calls, batch = adapter(projections(archived=archived))
    assert (await agents._receive_preflight(ROOT, 'receiver'))['parent'] == 'r_mailbox'
    assert calls == []
    assert [operation for operation, _ in batch] == [
        'discovery.get',
        'discovery.get',
        'discovery.get',
        'file.read',
    ]
    assert [args['id'] for _, args in batch] == [
        ROOT.rpartition('/')[0],
        ROOT,
        ROOT + '/receiver',
        ROOT + '/receiver/agent.json',
    ]
