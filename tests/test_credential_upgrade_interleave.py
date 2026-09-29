"""Token transactions and upgrades must share one fresh local identity state."""

import asyncio

import pytest
from test_credential_delivery_matrix import TRANSPORTS, connect
from test_service import NOW

from msg.core.codec import wire
from msg.core.errors import Failure


@pytest.mark.parametrize('transport_type', TRANSPORTS)
async def test_stale_upgrade_uses_durably_rotated_token(installed, tmp_path, transport_type):
    app, _ = installed
    client, http = connect(app, tmp_path / 'client', transport_type, lambda: NOW)
    stale_http = None
    try:
        assert (await client.temporary()).status == 'ok'
        stale, stale_http = connect(app, client.state.directory, transport_type, lambda: NOW)
        assert (await client.rotate_token()).status == 'ok'
        result = await stale.upgrade('fresh-local-state')
        assert result.status == 'ok', wire(result)
        assert stale.state.subject == client.state.subject
        assert stale.state.token is None
        assert not (client.state.directory / 'identity-upgrade.json').exists()
    finally:
        if stale_http is not None:
            await stale_http.aclose()
        await http.aclose()


@pytest.mark.parametrize('transport_type', TRANSPORTS)
async def test_custodial_upgrade_cannot_interrupt_token_rotation(
    installed, tmp_path, transport_type
):
    app, _ = installed
    client, http = connect(app, tmp_path / 'client', transport_type, lambda: NOW)
    stale_http = None
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        assert (await client.custodial('custody-local-state')).status == 'ok'
        stale, stale_http = connect(app, client.state.directory, transport_type, lambda: NOW)
        original = client.transport.call

        async def paused(packet):
            entered.set()
            await release.wait()
            return await original(packet)

        client.transport.call = paused
        task = asyncio.create_task(client.rotate_token())
        await entered.wait()
        with pytest.raises(Failure, match='identity_upgrade_busy'):
            await stale.upgrade_custodial('self-local-state', external_ciphertexts_migrated=True)
        assert stale.transport.calls == 0
        assert not stale.state.key_path.exists()
        assert not (client.state.directory / 'custodial-upgrade.json').exists()
        release.set()
        assert (await task).status == 'ok'
    finally:
        release.set()
        if task is not None:
            await task
        if stale_http is not None:
            await stale_http.aclose()
        await http.aclose()
