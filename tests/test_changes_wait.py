import asyncio

from test_client_subagents_remote import client_for
from test_service import call, register

from msg.client_changes_wait import ChangesWait
from msg.client_subagents_remote import RemoteAgents
from msg.core.codec import wire


async def test_changes_v2_signature_version_cursor_and_authority(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'changes-contract')
    old = await call(app, 'communication.changes', {}, key=key, subject=subject)
    cursor = old.data['tail_cursor']
    resumed = await call(
        app,
        'communication.changes',
        {'cursor': cursor, 'wait_ms': 0},
        key=key,
        subject=subject,
        contract_version=2,
    )
    assert resumed.status == 'ok', wire(resumed)
    assert resumed.data['sync_cursor'] == cursor and not resumed.data['items']
    unsigned = await call(app, 'communication.changes', {}, subject=subject, contract_version=2)
    assert unsigned.status == 'error'
    invalid = await call(
        app,
        'communication.changes',
        {'wait_ms': 20001},
        key=key,
        subject=subject,
        contract_version=2,
    )
    assert invalid.error.code == 'schema_validation'
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('authorization_epoch', tx.setting('authorization_epoch', 0) + 1)
    revoked = await call(
        app,
        'communication.changes',
        {'cursor': cursor},
        key=key,
        subject=subject,
        contract_version=2,
    )
    assert revoked.error.code == 'resync_required'


async def test_real_http_mailbox_wait_commit_and_filter_draining(installed, tmp_path):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'changes-mailbox')
    agents = RemoteAgents(client)
    source = ChangesWait(client, mode='events')
    try:
        await agents.create('sender')
        await agents.create('receiver')
        await agents.create('unrelated')
        tail = await agents.inbox('receiver', tail=True)
        # Other mailbox traffic is relevant to the account but cannot be delivered
        # or cause a wait while the sync source still has more pages to scan.
        await agents.send('sender', 'unrelated', 'not receiver', message_id='unrelated')
        reads = []

        async def read(arguments, *, wait=False):
            reads.append(wait)
            return await source.read(arguments, wait=wait)

        drained = await agents.inbox('receiver', cursor=tail['cursor'], changes_reader=read)
        assert drained['items'] == [] and not drained['has_more']
        assert not any(reads)
        pending = asyncio.create_task(
            agents.inbox('receiver', cursor=drained['cursor'], changes_reader=read, wait=True)
        )
        try:
            await asyncio.wait_for(app.metadata.event_signal.ready.wait(), 1)
            await agents.send('sender', 'receiver', 'private payload', message_id='wanted')
            result = await asyncio.wait_for(pending, 2)
            assert [item['id'] for item in result['items']] == ['wanted']
            assert 'private payload' not in result['cursor']
            assert app.metadata.event_signal.waiters == 0
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        await http.aclose()
