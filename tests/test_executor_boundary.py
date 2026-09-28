"""Explicit executor assembly, with unchanged real transaction/security paths."""
from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from datetime import timedelta
import inspect
from types import SimpleNamespace

import pytest

from msg.core.codec import wire
from msg.core.errors import Failure
from msg.core.executor import OperationExecutor
from msg.core.models import HandlerOutput, Principal, ResourceRef
from msg.core.requests import request_for
from test_service import NOW, call, register
from test_batch import packet


def test_executor_has_no_business_plugin_imports_or_application_backreference():
    import msg.core.executor as executor

    tree = ast.parse(inspect.getsource(executor))
    forbidden = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or '').startswith(
                ('msg.plugins', 'msg.application', 'msg.transports')):
            forbidden.append(node.module)
        elif isinstance(node, ast.Import):
            forbidden.extend(alias.name for alias in node.names if alias.name.startswith(
                ('msg.plugins', 'msg.application', 'msg.transports')))
    assert forbidden == []
    assert not any(isinstance(node, ast.Attribute) and node.attr == 'application'
                   for node in ast.walk(tree))


def test_batch_structure_has_one_neutral_owner():
    import msg.core.batch as batch
    import msg.plugins.batch as plugin

    tree = ast.parse(inspect.getsource(batch))
    assert not any(isinstance(node, ast.ImportFrom) and (node.module or '').startswith(
        ('msg.plugins', 'msg.transports', 'msg.application')) for node in ast.walk(tree))
    # The plugin must not retain a second copy of the child admission algorithm.
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'packets'
                   for node in ast.walk(ast.parse(inspect.getsource(plugin))))
    assert batch.NO_BATCH >= {'identity.register', 'identity.upgrade', 'identity.token_recover'}


def test_event_identity_is_shared_without_importing_the_communication_plugin():
    from msg.core.events import event_id
    from msg.plugins.communication import event_id as compatibility_id

    request = request_for('testing.read', {}, 'https://unit.invalid', request_id='event-once')
    assert event_id is compatibility_id
    assert event_id(request, 'u_test') == event_id(request, 'u_test')
    assert event_id(request, 'u_test') != event_id(request, 'u_other')


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

    async def project(context, request, tx, ref):
        assert active == [transaction] and tx is transaction
        assert request.return_fields == ('id',)
        return {'id': ref.id}

    spec = SimpleNamespace(name='testing.read', version=1, entries={'network'}, effect='read',
        input_schema=None, output_schema=None, requirements=requirements, handler=handler)
    registry = SimpleNamespace(operation=lambda *args: spec, validate=lambda *args: None)
    executor = OperationExecutor(registry, SimpleNamespace(transaction=transaction_scope), None,
        SimpleNamespace(authenticate=authenticate), SimpleNamespace(require=authorize),
        lambda: NOW, None, projection_reader=project)
    assert not hasattr(executor, 'application')
    result = await executor.execute(request_for('testing.read', {}, 'https://unit.invalid',
        return_fields=('id',)))
    assert result.status == 'ok', result
    assert result.data == {'original': True, 'projection': [{'id': 'r_test'}]}
    assert active == []


async def test_application_factory_configures_normal_and_alternate_authentication(installed):
    app, _ = installed
    alternate = object()
    executor = app.make_executor(authenticator=alternate)
    assert executor.authenticator is alternate
    assert not hasattr(executor, 'application')
    assert executor.projection_reader is not None
    assert executor.event_projector is not None
    assert executor.batch_policy.max_request_bytes == app.settings.server.limits.max_request_bytes
    assert executor.response_hook == app._secrets_for_caller
    assert executor.recovery_drill_marker == app.executor.recovery_drill_marker
    assert executor.recovery_quarantined == app.executor.recovery_quarantined
    import msg.extensions.ssh as ssh
    source = inspect.getsource(ssh)
    assert 'app.make_executor(' in source
    assert 'executor.application' not in source


async def test_factory_batch_and_projection_work_without_application_backreference(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'executor-factory')
    app.executor = app.make_executor()
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
    app.executor = app.make_executor()
    seen = []

    async def reject(tx, event):
        assert tx.one('SELECT id FROM events WHERE id=?', (event.id,)) is not None
        seen.append(event.id)
        raise Failure('test_event_projection_abort')

    app.executor.event_projector = reject
    result = await call(app, 'content.post_create', {'parent': '/main', 'body': 'not committed'},
        key=key, subject=subject, rid='event-rollback-request')
    assert result.error.code == 'test_event_projection_abort', wire(result)
    assert len(seen) == 1
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0
        assert tx.one('SELECT id FROM events WHERE id=?', (seen[0],)) is None
        assert tx.one('SELECT request_id FROM results WHERE request_id=?', ('event-rollback-request',)) is None


@pytest.mark.parametrize('case', ['batch', 'projection'])
async def test_missing_optional_service_fails_closed_without_internal_error_or_commit(installed, case):
    app, _ = installed
    key, subject, _ = await register(app, 'unconfigured-executor')
    executor = OperationExecutor(app.registry, app.metadata, app.contents,
        app.authenticator, app.authorizer, app.clock, app.receipt_signer)
    if case == 'batch':
        child = packet(app, key, subject, 'content.post_create',
            {'parent': '/main', 'body': 'not committed'}, 'unconfigured-child')
        request = request_for('batch.atomic', {'requests': [child]}, app.settings.service_url,
            subject=subject, signer=key, expires_at=NOW + timedelta(seconds=60))
        expected = 'batch_decoder_not_configured'
    else:
        request = request_for('content.post_create', {'parent': '/main', 'body': 'not committed'},
            app.settings.service_url, subject=subject, signer=key,
            expires_at=NOW + timedelta(seconds=60), return_fields=('id',))
        expected = 'projection_unavailable'
    result = await executor.execute(request)
    assert result.status == 'error' and result.error.code == expected, wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0
