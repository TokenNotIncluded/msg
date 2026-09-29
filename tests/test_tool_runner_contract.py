"""The runner owns local output; only the worker may publish a ResourceRef."""

from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import get_type_hints

import pytest
from test_authorization import approve, scoped
from test_service import call, register

from msg.core.errors import Failure
from msg.core.models import JsonMap, NetworkPolicy, ResourceRef, ToolSpec
from msg.workers.effects import EffectWorker
from msg.workers.sandbox import BubblewrapRunner


def test_tool_result_has_one_neutral_owner_and_compatible_export(tmp_path):
    from msg.core.tool_execution import ToolResult
    from msg.workers.effects import ToolResult as CompatibilityResult

    assert CompatibilityResult is ToolResult
    assert ToolResult.__module__ == 'msg.core.tool_execution'
    assert [field.name for field in fields(ToolResult)] == ['path', 'media_type', 'metadata']
    result = ToolResult(tmp_path / 'output.bin', 'application/octet-stream', {'size': 1})
    assert not isinstance(result, ResourceRef)
    assert not hasattr(result, '__dict__')
    with pytest.raises(FrozenInstanceError):
        result.path = tmp_path / 'other.bin'


def test_actual_worker_dependency_is_the_declared_callable_port():
    from msg.core.tool_execution import ToolRunner

    assert get_type_hints(EffectWorker.__init__)['tool_runner'] == ToolRunner | None
    assert isinstance(BubblewrapRunner(None), ToolRunner)


def test_default_runner_and_port_have_the_same_input_and_local_output_types():
    from msg.core.tool_execution import ToolResult, ToolRunner

    expected = {
        'tool': ToolSpec,
        'arguments': JsonMap,
        'policies': tuple[NetworkPolicy, ...],
        'directory': Path,
        'return': ToolResult,
    }
    assert get_type_hints(ToolRunner.__call__) == expected
    assert get_type_hints(BubblewrapRunner.__call__) == expected
    assert inspect.iscoroutinefunction(BubblewrapRunner.__call__)
    assert inspect.iscoroutinefunction(ToolRunner.__call__)


def test_unused_publishing_port_is_retired_not_a_second_runner_contract():
    from msg.core.contracts import ToolExecutor
    from msg.core.tool_execution import ToolRunner

    # Keep the old import spelling, not the unused invoke -> ResourceRef interface.
    assert ToolExecutor is ToolRunner
    assert not hasattr(ToolExecutor, 'invoke')


def test_sandbox_does_not_import_its_worker_or_application_composition():
    import msg.workers.sandbox as sandbox

    tree = ast.parse(inspect.getsource(sandbox))
    dependencies = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            dependencies.append(node.module or '')
        elif isinstance(node, ast.Import):
            dependencies.extend(alias.name for alias in node.names)
    assert 'msg.core.tool_execution' in dependencies
    assert not any(
        name == 'msg.workers.effects'
        or name.startswith('msg.workers.effects.')
        or name == 'msg.application'
        for name in dependencies
    )


async def test_default_runner_still_refuses_missing_mandatory_sandbox(monkeypatch, tmp_path):
    import msg.workers.sandbox as sandbox

    monkeypatch.setattr(sandbox.shutil, 'which', lambda name: None)
    with pytest.raises(Failure, match='tool_isolation_unavailable'):
        await BubblewrapRunner(None)(None, {}, (), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('invalid_result', [False, True])
async def test_injected_port_cannot_publish_a_resource_itself(installed, invalid_result):
    from msg.core.tool_execution import ToolResult, ToolRunner

    app, root = installed
    key, subject, _ = await register(app, 'runner-contract')
    cap = scoped(app, 'tool.use', 'tool_dns', app.registry.capability('tool.use').operations)
    certificate = await approve(app, root, subject, key, (cap,))
    certs = (certificate.resource_id,)
    request = await call(
        app,
        'tool.run',
        {'id': '/tools/dns', 'arguments': {'name': 'example.org', 'type': 'A'}},
        key=key,
        subject=subject,
        certs=certs,
    )
    assert request.status == 'accepted'
    invocations = []

    async def runner(
        tool: ToolSpec, arguments: JsonMap, policies: tuple[NetworkPolicy, ...], directory: Path
    ) -> ToolResult:
        assert tool.executor_key == 'dns'
        assert arguments['name'] == 'example.org'
        assert isinstance(policies, tuple) and policies
        assert all(isinstance(policy, NetworkPolicy) for policy in policies)
        invocations.append(tool.resource)
        if invalid_result:
            # An untrusted/misconfigured runner must not publish an existing ref.
            return ResourceRef(id='tool_dns', revision=tool.resource.revision)
        output = directory / 'output.bin'
        output.write_bytes(b'[]')
        return ToolResult(output, 'application/json', {'kind': 'dns'})

    assert isinstance(runner, ToolRunner)
    worker = EffectWorker(app, tool_runner=runner)
    assert worker.tool_runner is runner
    assert await worker.run_once()
    assert not await worker.run_once()
    assert len(invocations) == 1
    async with app.metadata.transaction(write=False) as tx:
        job = await tx.job(request.data['job_id'])
        outputs = list(tx.execute('SELECT id FROM resources WHERE name=?', ('tool-' + job.id,)))
        if invalid_result:
            assert job.state == 'uncertain'
            assert job.result is None
            assert outputs == []
        else:
            assert job.state == 'done'
            assert isinstance(job.result, ResourceRef)
            assert job.result.id != 'tool_dns'
            assert [row[0] for row in outputs] == [job.result.id]
