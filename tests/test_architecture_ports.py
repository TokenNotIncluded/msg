"""Core/adapter boundaries plus real signed calls through the composed ports."""

import ast
import importlib
import importlib.util
import inspect
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from test_service import NOW, register

from msg.core.codec import digest, wire
from msg.core.executor import OperationExecutor
from msg.core.requests import request_for

ROOT = Path(__file__).resolve().parents[1] / 'src' / 'msg'


def imports(path):
    tree = ast.parse(path.read_text())
    return [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    ]


def test_executor_has_no_plugin_or_application_backdoor():
    source = (ROOT / 'core/executor.py').read_text()
    assert not any(
        name.startswith(('msg.plugins', 'msg.application'))
        for name in imports(ROOT / 'core/executor.py')
    )
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == 'application'
        for node in ast.walk(ast.parse(source))
    )


def test_batch_rules_and_event_identity_have_one_neutral_owner():
    assert importlib.util.find_spec('msg.core.batching') is not None
    assert importlib.util.find_spec('msg.core.events') is not None
    batching = importlib.import_module('msg.core.batching')
    events = importlib.import_module('msg.core.events')
    from msg.plugins import batch, communication

    assert batch.packets is batching.packets
    assert communication.event_id is events.event_id
    assert not any(name.startswith('msg.plugins') for name in imports(ROOT / 'core/batching.py'))
    request = request_for(
        'content.post_create',
        {},
        'https://example.invalid',
        subject='u_test',
        request_id='stable-request',
    )
    assert events.event_id(request, 'u_test') == 'e_' + digest(('u_test', 'stable-request'))[7:39]


def test_core_protocol_imports_without_loading_plugins_or_transports():
    code = """
import importlib.abc
import sys
class NoOuterLayer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('msg.plugins', 'msg.transports', 'msg.application')):
            raise AssertionError('outer layer imported: ' + fullname)
sys.meta_path.insert(0, NoOuterLayer())
from msg.core import executor, batching, packet
assert executor.result_wire is packet.result_wire
"""
    result = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_wire_contracts_and_primitive_schemas_are_single_source():
    assert importlib.util.find_spec('msg.core.packet') is not None
    assert importlib.util.find_spec('msg.core.schemas') is not None
    packet = importlib.import_module('msg.core.packet')
    schemas = importlib.import_module('msg.core.schemas')
    from msg.plugins import schemas as plugin_schemas
    from msg.transports import packet as transport

    for name in ('REQUEST_SCHEMA', 'RESULT_SCHEMA', 'decode_packet', 'decode_result'):
        assert getattr(packet, name) is getattr(transport, name)
    assert schemas.obj is plugin_schemas.obj
    assert schemas.SIGNATURE is plugin_schemas.SIGNATURE


@pytest.mark.asyncio
async def test_composition_reuses_ports_for_alternate_authenticator(installed):
    app, _ = installed
    assert hasattr(app, 'new_executor')
    executor = app.new_executor(app.authenticator)
    assert not hasattr(executor, 'application')
    assert executor.max_request_bytes == app.settings.server.limits.max_request_bytes
    assert executor.result_projection == app.executor.result_projection
    assert executor.event_notifications == app.executor.event_notifications
    assert executor.response_hook == app.executor.response_hook
    assert executor.recovery_drill_marker == app.executor.recovery_drill_marker
    app.executor.recovery_quarantined = True
    assert app.new_executor(app.authenticator).recovery_quarantined is True


@pytest.mark.asyncio
async def test_standalone_executor_projection_failure_rolls_back(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'port-agent')
    executor = OperationExecutor(
        app.registry,
        app.metadata,
        app.contents,
        app.authenticator,
        app.authorizer,
        app.clock,
        app.receipt_signer,
    )
    packet = request_for(
        'content.post_create',
        {'parent': '/main', 'body': 'not published'},
        app.settings.service_url,
        subject=uid,
        signer=key,
        request_id='missing-projection-port',
        return_fields=('id',),
        expires_at=NOW + timedelta(seconds=90),
    )
    result = await executor.execute(packet)
    assert result.status == 'error' and result.error.code == 'projection_unavailable', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0
        assert (
            tx.one('SELECT COUNT(*) FROM results WHERE request_id=?', (packet.request_id,))[0] == 0
        )


@pytest.mark.asyncio
async def test_explicit_projection_port_preserves_signed_execution_and_replay(installed):
    app, _ = installed
    assert 'result_projection' in inspect.signature(OperationExecutor).parameters
    key, uid, _ = await register(app, 'projected-agent')
    executor = OperationExecutor(
        app.registry,
        app.metadata,
        app.contents,
        app.authenticator,
        app.authorizer,
        app.clock,
        app.receipt_signer,
        max_request_bytes=app.settings.server.limits.max_request_bytes,
        result_projection=app.executor.result_projection,
        event_notifications=app.executor.event_notifications,
    )
    packet = request_for(
        'content.post_create',
        {'parent': '/main', 'body': 'projected'},
        app.settings.service_url,
        subject=uid,
        signer=key,
        request_id='explicit-projection-port',
        return_fields=('id',),
        expires_at=NOW + timedelta(seconds=90),
    )
    first = await executor.execute(packet)
    replay = await executor.execute(packet)
    assert first.status == replay.status == 'ok', wire(first)
    assert first.data['projection'][0]['id'] == first.resources[0].id
    assert (
        replay.replayed and replay.resources == first.resources and replay.receipt == first.receipt
    )
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 1
