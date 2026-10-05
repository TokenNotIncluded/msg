"""Online coordination uses real signatures, PostgreSQL and HTTP ACL checks."""

import asyncio

import httpx
import pytest
from test_service import NOW

from msg.client import ClientState, MsgClient
from msg.client_subagents_remote import RemoteAgents
from msg.core.codec import b64, wire
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


async def client_for(app, directory, username):
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    )
    client = MsgClient(
        ClientState(directory, server=app.settings.service_url),
        HTTPTransport(app.settings.service_url, http=http),
        clock=lambda: NOW,
    )
    client.checked(await client.register(username))
    return client, http


async def all_messages(agents, name):
    items, cursor = [], None
    for _ in range(50):
        page = await agents.inbox(name, cursor=cursor)
        items.extend(page['items'])
        if not page['has_more']:
            return items
        assert page['cursor'] != cursor
        cursor = page['cursor']
    pytest.fail('mailbox history did not finish within bounded continuation calls')


@pytest.mark.asyncio
async def test_remote_private_delivery_tail_restart_and_archive(installed, tmp_path):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'online-bots')
    stranger, other_http = await client_for(app, tmp_path / 'other', 'online-other')
    agents = RemoteAgents(client)
    try:
        assert await agents.list() == []
        missing = await client.call('discovery.get', {'id': '/@online-bots/files/agents'})
        assert missing.status == 'error' and missing.error.code == 'not_found'
        with pytest.raises(Failure, match='subagent_not_found'):
            await agents.inbox('bot2')
        await agents.create('bot1')
        await agents.create('@online-bots#bot2')
        assert len(await agents.list()) == 2
        tail = await agents.inbox('bot2', tail=True)
        assert not tail['items']
        event = await agents.send('bot1', 'bot2', 'private handoff', message_id='stable-message')
        assert event['from'] == '@online-bots#bot1'
        assert (
            await agents.send('bot1', 'bot2', 'private handoff', message_id='stable-message')
            == event
        )
        receipt = await agents.send(
            'bot1', 'bot2', 'private handoff', message_id='stable-message', receipt=True
        )
        assert receipt['type'] == 'subagent.receipt'
        assert receipt['id'] == event['id']
        assert receipt['from'] == event['from'] and receipt['to'] == event['to']
        assert receipt['created_at'] == event['created_at']
        assert 'message' not in receipt
        assert receipt['body_bytes'] == len(b'private handoff')
        assert receipt['path'] == '/@online-bots/files/agents/bot2/msg-stable-message.json'
        saved_meta, saved_body = await agents._json(receipt['resource']['id'])
        assert saved_meta['revision'] == receipt['resource']['revision']
        assert saved_body['message'] == 'private handoff'
        assert (
            await agents.send(
                'bot1', 'bot2', 'private handoff', message_id='stable-message', receipt=True
            )
            == receipt
        )
        with pytest.raises(Failure, match='subagent_message_id_conflict'):
            await agents.send(
                'bot1', 'bot2', 'different', message_id='stable-message', receipt=True
            )
        restarted = RemoteAgents(client)
        page = await restarted.inbox('bot2', cursor=tail['cursor'])
        assert [x['id'] for x in page['items']] == ['stable-message']
        empty = await restarted.inbox('bot2', cursor=page['cursor'])
        assert empty['items'] == []
        with pytest.raises(Failure, match='invalid_subagent_cursor'):
            await agents.inbox('bot1', cursor=page['cursor'])
        with pytest.raises(Failure, match='subagent_account_mismatch'):
            await agents.send('@online-other#bot1', 'bot2', 'no')
        await agents.archive('bot2')
        with pytest.raises(Failure, match='subagent_archived'):
            await agents.send('bot1', 'bot2', 'after archive')
        history = await all_messages(agents, 'bot2')
        assert [x['message'] for x in history] == ['private handoff']
        public = client.checked(
            await client.call(
                'file.create',
                {'parent': '/main', 'name': 'public-control.txt', 'data': b64(b'Public control')},
            )
        )
        for anonymous in (True, False):
            visitor = client if anonymous else stranger
            for operation, args in [
                (
                    'discovery.get',
                    {'id': '/@online-bots/files/agents/bot2/msg-stable-message.json'},
                ),
                ('discovery.list', {'parent': '/@online-bots/files/agents'}),
            ]:
                result = await visitor.call(operation, args, anonymous=anonymous)
                assert result.status == 'error'
                assert 'private handoff' not in repr(result)
            # Exercise every page without exhausting one request's read budget.
            query = {'type': 'file', 'limit': 20}
            cursors, seen = set(), set()
            while True:
                visible = await visitor.call('discovery.list', query, anonymous=anonymous)
                assert visible.status == 'ok', wire(visible.error)
                for item in visible.data['items']:
                    assert 'msg-stable-message' not in item['name']
                    assert item['id'] not in seen
                    seen.add(item['id'])
                cursor = visible.data.get('cursor')
                if cursor is None:
                    break
                assert cursor not in cursors
                cursors.add(cursor)
                query['cursor'] = cursor
            assert public.resources[0].id in seen
            feed = await visitor.call('discovery.recommendations', {}, anonymous=anonymous)
            assert 'private handoff' not in repr(feed)
        users = client.checked(
            await client.call('discovery.list', {'type': 'user', 'parent': '/'})
        ).data['items']
        assert not any('#bot' in x['name'] for x in users)
    finally:
        await http.aclose()
        await other_http.aclose()


@pytest.mark.asyncio
async def test_remote_concurrent_messages_equal_timestamps_no_cursor_loss(installed, tmp_path):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'concurrent-bots')
    agents = RemoteAgents(client)
    try:
        await agents.create('bot1')
        await agents.create('bot2')
        start = await agents.inbox('bot2', tail=True)
        await asyncio.gather(
            *(
                agents.send('bot1', 'bot2', 'message ' + str(i), message_id='message-' + str(i))
                for i in range(7)
            )
        )
        cursor, found = start['cursor'], []
        for _ in range(30):
            page = await RemoteAgents(client).inbox('bot2', cursor=cursor, limit=2)
            cursor = page['cursor']
            found.extend(x['id'] for x in page['items'])
            if len(found) >= 7:
                break
        assert len(found) == len(set(found)) == 7
        await agents.send('bot1', 'bot2', 'insert after drained cursor', message_id='late-message')
        late = await agents.inbox('bot2', cursor=cursor, limit=2)
        assert [x['id'] for x in late['items']] == ['late-message']
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_remote_rejects_existing_public_namespace(installed, tmp_path):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'conflict-bots')
    try:
        client.checked(
            await client.call('file.mkdir', {'parent': '/@conflict-bots/files', 'name': 'agents'})
        )
        with pytest.raises(Failure, match='subagent_private_namespace_conflict'):
            await RemoteAgents(client).create('bot1')
        meta = client.checked(
            await client.call(
                'discovery.get', {'id': '/@conflict-bots/files/agents', 'view': 'meta'}
            )
        ).data
        assert meta['mode'] == '1777'
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_remote_labels_legacy_messages_and_cursor_survive_account_rename(installed, tmp_path):
    from msg.core.codec import b64, canonical

    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'bots-before')
    agents = RemoteAgents(client)
    try:
        await agents.create('bot1')
        await agents.create('bot2')
        root = '/@bots-before/files/agents'
        config_meta, _ = await agents._agent(root, 'bot1')
        client.checked(
            await client.call(
                'file.write',
                {
                    'id': config_meta['id'],
                    'base_revision': config_meta['revision'],
                    'data': b64(
                        canonical({
                            'version': 1,
                            'identity': '@bots-before#bot1',
                            'archived': False,
                        })
                    ),
                    'media_type': 'application/json',
                },
                expected=((config_meta['id'], config_meta['generation']),),
            )
        )
        await agents._put(
            root + '/bot2',
            'msg-legacy.json',
            {
                'version': 1,
                'id': 'legacy',
                'from': '@bots-before#bot1',
                'to': '@bots-before#bot2',
                'message': 'legacy private message',
            },
        )
        await agents.send('bot1', 'bot2', 'stable account record', message_id='stable')
        receipt_before = await agents.send(
            'bot1', 'bot2', 'stable account record', message_id='stable', receipt=True
        )
        before = await agents.inbox('bot2', tail=True)
        assert agents.username == 'bots-before'
        # The old persisted cursor also embedded a handle. Its stable owner,
        # server and mailbox suffix must survive a rename without crossing users.
        from msg.core.codec import loads, unb64

        legacy_cursor = loads(unb64(before['cursor']))
        legacy_cursor['scope'] = {
            'server': client.state.server,
            'owner': client.state.subject,
            'to': '@bots-before#bot2',
        }
        client.checked(await client.rename_identity('bots-after'))
        assert await agents.account_name() == 'bots-after'
        assert [x['identity'] for x in await agents.list()] == [
            '@bots-after#bot1',
            '@bots-after#bot2',
        ]
        assert (await agents.create('bot1'))['identity'] == '@bots-after#bot1'
        receipt_after = await agents.send(
            'bot1', 'bot2', 'stable account record', message_id='stable', receipt=True
        )
        assert receipt_before['resource'] == receipt_after['resource']
        assert receipt_after['path'] == '/@bots-after/files/agents/bot2/msg-stable.json'
        history = await all_messages(agents, 'bot2')
        assert {x['id'] for x in history} == {'legacy', 'stable'}
        assert all(
            x['from'] == '@bots-after#bot1' and x['to'] == '@bots-after#bot2' for x in history
        )
        # Both old v1 records and v2 records remain idempotent after rename.
        for identifier, message in [
            ('legacy', 'legacy private message'),
            ('stable', 'stable account record'),
        ]:
            sent = await agents.send('bot1', 'bot2', message, message_id=identifier)
            assert sent['from'] == '@bots-after#bot1'
        await agents.send('bot1', 'bot2', 'after rename', message_id='new')
        delta = await agents.inbox('bot2', cursor=before['cursor'])
        assert [x['id'] for x in delta['items']] == ['new']
        compatibility = await agents.inbox('bot2', cursor=b64(canonical(legacy_cursor)))
        assert [x['id'] for x in compatibility['items']] == ['new']
        with pytest.raises(Failure, match='connection_user_mismatch'):
            await RemoteAgents(client, username='bots-before').account_name()
        await agents.archive('bot1')
        _, migrated = await agents._json('/@bots-after/files/agents/bot1/agent.json')
        assert migrated == {
            'version': 2,
            'owner': client.state.subject,
            'name': 'bot1',
            'archived': True,
        }
        # A v2 payload cannot claim a different stable account, even when a
        # private file was edited by the owner.
        meta, value = await agents._json('/@bots-after/files/agents/bot2/msg-new.json')
        value['owner'] = 'r_another_account'
        client.checked(
            await client.call(
                'file.write',
                {
                    'id': meta['id'],
                    'base_revision': meta['revision'],
                    'data': b64(canonical(value)),
                    'media_type': 'application/json',
                },
                expected=((meta['id'], meta['generation']),),
            )
        )
        with pytest.raises(Failure, match='invalid_subagent_message'):
            await all_messages(agents, 'bot2')
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_remote_agents_list_call_reads_batch_and_fallback(installed, tmp_path):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'batch-bots')
    agents = RemoteAgents(client)
    original_call_reads = client.transport.call_reads
    try:
        for i in range(10):
            await agents.create(f'worker-{i}')

        # 1. Normal list with call_reads batching
        items = await agents.list()
        assert len(items) == 10
        assert [x['name'] for x in items] == [f'worker-{i}' for i in range(10)]

        # 2. Test fallback when call_reads raises an exception
        async def failing_call_reads(reqs):
            raise httpx.NetworkError('simulated network fault')

        client.transport.call_reads = failing_call_reads
        fallback_items = await agents.list()
        assert len(fallback_items) == 10
        assert [x['name'] for x in fallback_items] == [f'worker-{i}' for i in range(10)]

        # 3. Test fallback when call_reads is None (transport doesn't support it)
        client.transport.call_reads = None
        unsupported_items = await agents.list()
        assert len(unsupported_items) == 10
        assert [x['name'] for x in unsupported_items] == [f'worker-{i}' for i in range(10)]
    finally:
        client.transport.call_reads = original_call_reads
        await http.aclose()
