"""The transfer selftest checks real storage and refuses an ordinary app."""

from types import SimpleNamespace

import pytest
from test_service import call, register

from msg.admin.transfer_check import check_transfer
from msg.core.errors import Failure


async def test_transfer_selftest_refuses_nonisolated_application():
    with pytest.raises(Failure, match='selftest_namespace_required'):
        await check_transfer(SimpleNamespace(selftest_run_id=None), None, None)


@pytest.mark.parametrize('read_mutation', [False, True])
async def test_transfer_selftest_real_upload_download_and_readonly_boundaries(
    installed, monkeypatch, read_mutation
):
    app, _ = installed
    # installed creates a disposable PostgreSQL database and content directory.
    monkeypatch.setattr(app, 'selftest_run_id', 'isolated-transfer-fixture')

    async def invoke(operation, arguments, key=None, subject=None):
        result = await call(app, operation, arguments, key=key, subject=subject)
        if read_mutation and operation == 'transfer.part_get' and result.status == 'ok':
            async with app.metadata.transaction(write=True) as tx:
                tx.set_setting('selftest-read-mutation', True)
        return result

    async def enroll(name):
        key, subject, _ = await register(app, name)
        return key, subject

    if read_mutation:
        with pytest.raises(Failure, match='selftest_transfer_read_mutated'):
            await check_transfer(app, invoke, enroll)
    else:
        assert await check_transfer(app, invoke, enroll) is True
