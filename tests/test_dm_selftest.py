"""The production diagnostic uses real signed requests only in an isolated root."""
from types import SimpleNamespace

import pytest
from test_service import NOW

from msg.admin.dm_check import check_private_dm
from msg.core.errors import Failure


@pytest.mark.asyncio
async def test_private_dm_selftest_refuses_live_installation():
    with pytest.raises(Failure, match='isolated_selftest_required'):
        await check_private_dm(SimpleNamespace(selftest_run_id=None), NOW)


@pytest.mark.asyncio
async def test_private_dm_selftest_uses_real_isolated_clients(installed, monkeypatch):
    app, _ = installed
    monkeypatch.setattr(app, 'selftest_run_id', 'isolated-dm-fixture')
    assert await check_private_dm(app, NOW)
