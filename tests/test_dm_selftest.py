"""The production diagnostic uses real signed requests only in an isolated root."""

from types import SimpleNamespace

import pytest
from test_service import NOW

from msg.admin.dm_check import check_private_dm
from msg.admin.root import _approve_csr, _provision
from msg.application import Application
from msg.config import write_example
from msg.core.errors import Failure


@pytest.mark.asyncio
async def test_private_dm_selftest_refuses_live_installation():
    with pytest.raises(Failure, match='isolated_selftest_required'):
        await check_private_dm(SimpleNamespace(selftest_run_id=None), NOW)


@pytest.mark.asyncio
async def test_private_dm_selftest_uses_real_isolated_clients(tmp_path, pg_dsn):
    app = Application(
        write_example(
            tmp_path / 'etc', tmp_path / 'data', 'https://selftest.invalid', postgres_dsn=pg_dsn
        ),
        clock=lambda: NOW,
        selftest_run_id='a' * 32,
    )
    try:
        csr, root = await _provision(app, 'isolated-dm-test-passphrase')
        await _approve_csr(app, csr, root, expected_digest=None, operator='isolated-test')
        assert await check_private_dm(app, NOW)
    finally:
        await app.close()
