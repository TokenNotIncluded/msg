from types import SimpleNamespace

import pytest

from msg.admin.tool_check import inspect_tool_sandbox
from msg.application import Application
from msg.config import write_example
from msg.core.errors import Failure


@pytest.mark.asyncio
async def test_doctor_exercises_real_bubblewrap_deny_path(tmp_path):
    app = Application(write_example(tmp_path/'etc', tmp_path/'data'))
    assert await inspect_tool_sandbox(app) == {
        'runner': 'bubblewrap', 'policy_denial': True, 'external_requests': 0}


@pytest.mark.asyncio
async def test_missing_sandbox_is_not_reported_ready(tmp_path, monkeypatch):
    app = Application(write_example(tmp_path/'etc', tmp_path/'data'))
    monkeypatch.setattr('msg.workers.sandbox.shutil.which', lambda _: None)
    with pytest.raises(Failure, match='tool_isolation_unavailable'):
        await inspect_tool_sandbox(app)


@pytest.mark.asyncio
async def test_tool_selftest_rejects_live_application():
    from msg.admin.tool_check import check_tool_sandbox
    with pytest.raises(Failure, match='selftest_namespace_required'):
        await check_tool_sandbox(SimpleNamespace(selftest_run_id=None), None, None, None, None)


@pytest.mark.asyncio
async def test_tool_selftest_distinguishes_runner_output_from_operation_denial(installed, monkeypatch):
    from test_authorization import approve
    from test_service import call, register

    from msg.admin.tool_check import check_tool_sandbox
    app, root = installed
    monkeypatch.setattr(app, 'selftest_run_id', 'isolated-tool-fixture')
    key, subject, _ = await register(app, 'tool-selftest')
    async def invoke(name, arguments, signer, uid, certificates):
        return await call(app, name, arguments, key=signer, subject=uid, certs=certificates)
    async def certify(signer, uid, grants):
        return await approve(app, root, uid, signer, grants)
    result = await check_tool_sandbox(app, invoke, certify, key, subject)
    assert result == {'operation': 'private_target_denied',
        'runner': 'loopback_output_and_policy_denial',
        'normal_operation_pipeline': 'not_exercised', 'external_requests': 0}
