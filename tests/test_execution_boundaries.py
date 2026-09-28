"""Architecture contracts plus real PostgreSQL assembly, not source-only claims."""
from __future__ import annotations

import ast
from dataclasses import fields
from datetime import timedelta
import importlib
import inspect
from pathlib import Path
import subprocess
import sys
from typing import get_type_hints

import pytest

from msg.core.codec import digest, wire
from msg.core.errors import Failure
from msg.core.models import OperationResult, ResourceRef
from msg.core.requests import request_for, receipt_bytes
from msg.security.crypto import verify
from test_service import register, NOW


def source(module):
    return Path(importlib.import_module(module).__file__).read_text()


def imports(text):
    return [node.module or '' for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.ImportFrom)] + [name.name
            for node in ast.walk(ast.parse(text)) if isinstance(node, ast.Import)
            for name in node.names]


def test_executor_has_no_concrete_plugin_or_application_imports():
    dependencies = imports(source('msg.core.executor'))
    assert not [name for name in dependencies if name.startswith(
        ('msg.plugins', 'msg.application', 'msg.transports', 'msg.workers'))]


def test_execution_core_imports_without_loading_server_layers():
    code = '''
import importlib.abc, sys
class BlockServer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in
               ('msg.plugins', 'msg.application', 'msg.storage', 'msg.admin',
                'msg.workers', 'msg.transports')):
            raise AssertionError('unexpected server dependency: ' + fullname)
sys.meta_path.insert(0, BlockServer())
from msg.core.executor import OperationExecutor
from msg.core.batch import packets
from msg.core.envelopes import decode_packet, result_wire
from msg.core.tool_runner import ToolRunner, ToolResult
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_batch_and_adapter_share_the_canonical_decoder_and_schema():
    batch = importlib.import_module('msg.core.batch')
    envelopes = importlib.import_module('msg.core.envelopes')
    adapter = importlib.import_module('msg.transports.packet')
    plugin = importlib.import_module('msg.plugins.batch')
    old_schemas = importlib.import_module('msg.plugins.schemas')
    schemas = importlib.import_module('msg.core.schemas')
    assert plugin.packets is batch.packets
    assert batch.decode_packet is adapter.decode_packet is envelopes.decode_packet
    assert adapter.REQUEST_SCHEMA is envelopes.REQUEST_SCHEMA
    assert old_schemas.SIGNATURE is schemas.SIGNATURE
    assert old_schemas.obj is schemas.obj


def test_result_wire_is_one_compatibly_reexported_implementation():
    envelopes = importlib.import_module('msg.core.envelopes')
    executor = importlib.import_module('msg.core.executor')
    assert executor.result_wire is envelopes.result_wire
    result = OperationResult(request_id='wire-once', operation='discovery.get',
                             status='ok', actor=None, subject=None)
    expected = wire(result, compact=True)
    expected.pop('replayed', None)
    expected.pop('prefer_cli', None)
    assert envelopes.result_wire(result) == expected


def test_event_identity_preserves_existing_request_subject_bytes():
    requests = importlib.import_module('msg.core.requests')
    communication = importlib.import_module('msg.plugins.communication')
    packet = request_for('content.post_create', {}, 'https://test.invalid',
                         request_id='stable-event')
    assert communication.event_id is requests.event_id
    assert requests.event_id(packet, 'u_alice') == 'e_' + digest(('u_alice', 'stable-event'))[7:39]


@pytest.mark.asyncio
async def test_production_executor_receives_only_explicit_collaborators(installed):
    app, _ = installed
    executor = app.executor
    assert not hasattr(executor, 'application')
    assert executor.max_request_bytes == app.settings.server.limits.max_request_bytes
    assert callable(executor.project_result)
    assert callable(executor.enqueue_event)


@pytest.mark.asyncio
async def test_declared_executor_ports_keep_projection_events_receipts_and_replay(installed):
    from msg.core.executor import OperationExecutor
    app, _ = installed
    key, uid, cert = await register(app, 'declared-executor-ports')
    original = app.executor
    projected, emitted = [], []

    async def project(context, request, tx, ref, selected):
        projected.append(ref)
        return await original.project_result(context, request, tx, ref, selected)

    async def enqueue(tx, event):
        emitted.append(event)
        await original.enqueue_event(tx, event)

    executor = OperationExecutor(app.registry, app.metadata, app.contents,
        app.authenticator, app.authorizer, app.clock, app.receipt_signer,
        max_request_bytes=app.settings.server.limits.max_request_bytes,
        project_result=project, enqueue_event=enqueue)
    packet = request_for('content.post_create', {'parent':'/main','body':'port contract'},
        app.settings.service_url, subject=uid, signer=key, certificates=(cert,),
        request_id='declared-ports-once', expires_at=NOW+timedelta(seconds=90),
        return_fields=('id',))
    first = await executor.execute(packet)
    assert first.status == 'ok', wire(first)
    assert first.data['projection'][0]['id'] == first.resources[0].id
    assert projected == list(first.resources)
    assert [event.request_id for event in emitted] == [packet.request_id]
    verify(app.receipt_signer.public_key, receipt_bytes(first), first.receipt, purpose='receipt')
    replay = await executor.execute(packet)
    assert replay.replayed and replay.resources == first.resources
    assert len(projected) == len(emitted) == 1


@pytest.mark.asyncio
async def test_injected_event_failure_rolls_back_business_and_idempotency(installed):
    from msg.core.executor import OperationExecutor
    app, _ = installed
    key, uid, cert = await register(app, 'event-port-rollback')

    async def reject(tx, event):
        assert tx.one('SELECT id FROM events WHERE id=?', (event.id,))
        raise Failure('event_port_rejected')

    executor = OperationExecutor(app.registry, app.metadata, app.contents,
        app.authenticator, app.authorizer, app.clock, app.receipt_signer,
        max_request_bytes=app.settings.server.limits.max_request_bytes,
        project_result=app.executor.project_result, enqueue_event=reject)
    packet = request_for('content.post_create', {'parent':'/main','body':'not committed'},
        app.settings.service_url, subject=uid, signer=key, certificates=(cert,),
        request_id='event-port-failed', expires_at=NOW+timedelta(seconds=90))
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(tx.one(f'SELECT COUNT(*) FROM {table}')[0]
                       for table in ('events','resources','results','jobs'))
    result = await executor.execute(packet)
    assert result.error.code == 'event_port_rejected', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        after = tuple(tx.one(f'SELECT COUNT(*) FROM {table}')[0]
                      for table in ('events','resources','results','jobs'))
    assert after == before


def test_sandbox_does_not_import_the_worker_for_its_result_type():
    assert 'msg.workers.effects' not in imports(source('msg.workers.sandbox'))


def test_tool_runner_has_one_neutral_result_and_no_obsolete_publication_port():
    contract = importlib.import_module('msg.core.tool_runner')
    worker = importlib.import_module('msg.workers.effects')
    contracts = importlib.import_module('msg.core.contracts')
    sandbox = importlib.import_module('msg.workers.sandbox')
    assert worker.ToolResult is contracts.ToolResult is contract.ToolResult
    assert contracts.ToolRunner is contract.ToolRunner
    assert not hasattr(contracts, 'ToolExecutor')
    assert [field.name for field in fields(contract.ToolResult)] == ['path','media_type','metadata']
    assert get_type_hints(sandbox.BubblewrapRunner.__call__) == get_type_hints(contract.ToolRunner.__call__)
    assert get_type_hints(contract.ToolRunner.__call__)['return'] is not ResourceRef
    assert inspect.iscoroutinefunction(sandbox.BubblewrapRunner.__call__)
    assert get_type_hints(worker.EffectWorker.__init__)['tool_runner'] == contract.ToolRunner | None


@pytest.mark.asyncio
async def test_default_and_injected_runners_use_the_same_runtime_port(installed, tmp_path):
    from msg.core.tool_runner import ToolRunner, ToolResult
    from msg.workers.effects import EffectWorker
    from msg.workers.sandbox import BubblewrapRunner
    app, _ = installed
    default = EffectWorker(app)
    assert isinstance(default.tool_runner, (ToolRunner, BubblewrapRunner))
    calls = []

    async def runner(tool, arguments, policies, directory):
        calls.append((tool, arguments, policies, directory))
        return ToolResult(path=directory/'output.bin', media_type='text/plain', metadata={})

    injected = EffectWorker(app, tool_runner=runner)
    assert isinstance(injected.tool_runner, ToolRunner)
    result = await injected.tool_runner(None, {}, (), tmp_path)
    assert type(result) is ToolResult and result.path == tmp_path/'output.bin'
    assert calls == [(None, {}, (), tmp_path)]
