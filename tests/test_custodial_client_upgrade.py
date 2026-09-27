"""The client persists new keys and request IDs before custodial exit."""
import httpx
import pytest
from datetime import timedelta

from msg.core.errors import Failure

from msg.client import ClientState,MsgClient
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from test_service import NOW


@pytest.mark.asyncio
async def test_client_custodial_exit_persists_keys_and_clears_token(installed,tmp_path):
    app,_=installed
    http=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                           base_url=app.settings.service_url)
    state=ClientState(tmp_path/'client',server=app.settings.service_url)
    client=MsgClient(state,HTTPTransport(app.settings.service_url,http=http),clock=lambda:NOW)
    try:
        created=await client.custodial('client-custodial')
        assert created.status=='ok' and state.token is not None
        assert state.signer is None and state.encryption_recipient is None
        completed=await client.upgrade_custodial('client-self',external_ciphertexts_migrated=True)
        assert completed.status=='ok' and completed.data['status']=='completed'
        assert state.subject==created.data['subject_id'] and state.token is None
        assert state.key_path.is_file() and state.age_key_path.is_file()
        assert not (state.directory/'custodial-upgrade.json').exists()
        post=await client.call('content.post_create',{'parent':'/main','body':'after custody'})
        assert post.status=='ok'
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_lost_finish_response_replays_without_losing_new_keys(installed,tmp_path):
    app,_=installed
    http=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                           base_url=app.settings.service_url)
    transport=HTTPTransport(app.settings.service_url,http=http)
    state=ClientState(tmp_path/'client',server=app.settings.service_url)
    client=MsgClient(state,transport,clock=lambda:NOW,retries=1)
    try:
        await client.custodial('lost-custodial')
        original=transport.call
        dropped=False
        async def lost_once(packet):
            nonlocal dropped
            result=await original(packet)
            if packet.operation=='identity.custodial_upgrade_finish' and not dropped:
                dropped=True
                raise httpx.ReadTimeout('response lost after commit')
            return result
        transport.call=lost_once
        completed=await client.upgrade_custodial('lost-self',external_ciphertexts_migrated=True)
        assert dropped and completed.status=='ok' and completed.replayed
        assert state.token is None and state.signer is not None
        assert not (state.directory/'custodial-upgrade.json').exists()
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_lost_finish_response_recovers_after_old_token_expires(installed,tmp_path):
    app,_=installed
    http=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                           base_url=app.settings.service_url)
    transport=HTTPTransport(app.settings.service_url,http=http)
    state=ClientState(tmp_path/'client',server=app.settings.service_url)
    client=MsgClient(state,transport,clock=lambda:NOW,retries=0)
    try:
        await client.custodial('late-custodial')
        original=transport.call
        dropped=False
        async def lost_finish_and_lookup(packet):
            nonlocal dropped
            if packet.operation=='identity.custodial_upgrade_result':
                raise httpx.ReadTimeout('lookup also lost')
            result=await original(packet)
            if packet.operation=='identity.custodial_upgrade_finish' and not dropped:
                dropped=True
                raise httpx.ReadTimeout('finish response lost')
            return result
        transport.call=lost_finish_and_lookup
        with pytest.raises(Failure,match='transport_uncertain'):
            await client.upgrade_custodial('late-self',external_ciphertexts_migrated=True)
        assert dropped and state.token is not None
        assert (state.directory/'custodial-upgrade.json').exists()
        transport.call=original
        later=NOW+timedelta(seconds=3601)
        client.clock=lambda:later
        app.authenticator.clock=lambda:later
        recovered=await client.upgrade_custodial('late-self',external_ciphertexts_migrated=True)
        assert recovered.status=='ok' and recovered.data['status']=='completed'
        assert state.token is None and not (state.directory/'custodial-upgrade.json').exists()
    finally:
        await http.aclose()
