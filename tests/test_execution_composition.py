"""Retain PR #169's useful assembly/rollback cases on the one executor port.

Source cases: f7f8a890d91e4eda5d3da5f3c2ac4d8988e70848; complete frozen-JSON
comparison correction: 3db973ab1784926eb4e545cabdf832e7faa0a4bc.
No alternative BatchPolicy/decoder/factory is installed for a competing API.
"""
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

from msg.core.codec import wire
from msg.core.errors import Failure
from msg.core.executor import OperationExecutor
from msg.core.models import HandlerOutput, Principal, ResourceRef
from msg.core.requests import request_for
from test_service import NOW, call, register
from test_batch import packet


async def test_core_executor_runs_with_explicit_services_and_no_application():
    active = []
    transaction = object()
    principal = Principal(actor='u_test', subject='u_test', credential_id='k_test',
        method='signature', certificates=(), ceiling=())

    @asynccontextmanager
    async def transaction_scope(*, write):
        assert write is False
        active.append(transaction)
        try:
            yield transaction
        finally:
            active.pop()

    async def authenticate(request, tx, *, entry):
        assert tx is transaction and entry == 'network'
        return principal

    async def requirements(request, tx):
        return ()

    async def authorize(context, request, checks, tx):
        assert context.principal is principal and tx is transaction

    async def handler(context, request, tx):
        return HandlerOutput(resources=(ResourceRef(id='r_test'),), data={'original': True})

    async def project(context, request, tx, ref, *, fields):
        assert active == [transaction] and tx is transaction
        assert request.return_fields == fields == ('id',)
        return {'id': ref.id}

    spec = SimpleNamespace(name='testing.read', version=1, entries={'network'}, effect='read',
        input_schema=None, output_schema=None, requirements=requirements, handler=handler)
    registry = SimpleNamespace(operation=lambda *args: spec, validate=lambda *args: None)
    executor = OperationExecutor(registry, SimpleNamespace(transaction=transaction_scope), None,
        SimpleNamespace(authenticate=authenticate), SimpleNamespace(require=authorize),
        lambda: NOW, None, result_projection=project)
    assert not hasattr(executor, 'application')
    result = await executor.execute(request_for('testing.read', {}, 'https://unit.invalid',
        return_fields=('id',)))
    assert result.status == 'ok', result
    assert wire(result.data) == {'original': True, 'projection': [{'id': 'r_test'}]}
    assert active == []


async def test_factory_batch_and_projection_work_without_application_backreference(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'executor-factory')
    app.executor = app.new_executor()
    child = packet(app, key, subject, 'content.post_create',
        {'parent': '/main', 'body': 'boundaries'}, 'factory-child')
    result = await call(app, 'batch.atomic', {'requests': [child]},
        key=key, subject=subject, rid='factory-parent')
    assert result.status == 'ok', wire(result)
    assert result.data['results'][0]['status'] == 'ok'
    repeated = await call(app, 'batch.atomic', {'requests': [child]},
        key=key, subject=subject, rid='factory-parent')
    assert repeated.replayed
    projected = await app.executor.execute(request_for('content.post_create',
        {'parent': '/main', 'body': 'project'}, app.settings.service_url,
        subject=subject, signer=key, expires_at=NOW + timedelta(seconds=60),
        return_fields=('id', 'size')))
    assert projected.status == 'ok', wire(projected)
    assert projected.data['projection'][0]['size'] == 7


async def test_event_projection_failure_rolls_back_the_existing_transaction(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'event-rollback')
    app.executor = app.new_executor()
    seen = []

    async def reject(tx, event):
        assert tx.one('SELECT id FROM events WHERE id=?', (event.id,)) is not None
        seen.append(event.id)
        raise Failure('test_event_projection_abort')

    app.executor.event_notifications = reject
    result = await call(app, 'content.post_create', {'parent': '/main', 'body': 'not committed'},
        key=key, subject=subject, rid='event-rollback-request')
    assert result.error.code == 'test_event_projection_abort', wire(result)
    assert len(seen) == 1
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0
        assert tx.one('SELECT id FROM events WHERE id=?', (seen[0],)) is None
        assert tx.one('SELECT request_id FROM results WHERE request_id=?', ('event-rollback-request',)) is None
