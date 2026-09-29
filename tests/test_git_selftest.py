"""The Git diagnostic executes real isolated protocols and refuses live apps."""
from types import SimpleNamespace

import pytest

from msg.admin.git_check import check_git
from msg.core.errors import Failure
from test_service import call, register


async def test_git_selftest_requires_disposable_namespace():
    with pytest.raises(Failure, match='selftest_namespace_required'):
        await check_git(SimpleNamespace(selftest_run_id=None), None, None)


async def test_git_selftest_real_publication_read_and_stale_ref(installed, monkeypatch):
    app, _ = installed
    # The installed fixture owns a disposable PostgreSQL database and storage.
    monkeypatch.setattr(app, 'selftest_run_id', 'isolated-git-fixture')

    async def invoke(operation, args, key=None, subject=None):
        return await call(app, operation, args, key=key, subject=subject)

    async def enroll(name):
        key, user, _ = await register(app, name)
        return key, user

    assert await check_git(app, invoke, enroll) is True
