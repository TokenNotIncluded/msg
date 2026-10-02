"""Exercise mailbox request counts over signed HTTP and a real database."""

from dataclasses import replace
from time import perf_counter

import pytest
from test_client_subagents_remote import client_for

from msg.client_subagents_remote import RemoteAgents
from msg.core.codec import b64, wire
from msg.core.errors import Failure


@pytest.mark.asyncio
async def test_remote_mailbox_round_trips(installed, tmp_path):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'efficient-bots')
    agents = RemoteAgents(client)
    try:
        await agents.create('sender')
        await agents.create('recipient')
        for number in range(12):
            client.checked(
                await client.call(
                    'file.create',
                    {
                        'parent': '/@efficient-bots/files',
                        'name': 'unrelated-' + str(number) + '.txt',
                        'data': b64(b'unrelated account history'),
                    },
                )
            )
        calls = []
        original = client.transport.call

        async def counted(packet):
            calls.append(packet.operation)
            return await original(packet)

        client.transport.call = counted
        start = perf_counter()
        await agents.send('sender', 'recipient', 'bounded private message', message_id='once')
        send_calls, send_seconds = list(calls), perf_counter() - start
        calls.clear()
        start = perf_counter()
        page = await agents.inbox('recipient', limit=2)
        inbox_seconds = perf_counter() - start
        assert [item['id'] for item in page['items']] == ['once']
        assert len(send_calls) == 8
        assert len(calls) == 8
        inbox_calls = list(calls)
        calls.clear()
        tail = await agents.inbox('recipient', tail=True)
        assert tail['items'] == []
        assert len(calls) == 6
        await agents.send('sender', 'recipient', 'after tail', message_id='later')
        assert [
            item['id'] for item in (await agents.inbox('recipient', cursor=tail['cursor']))['items']
        ] == ['later']
        # A large internal page must leave later messages unread when the user
        # requests one. An unrelated first event forces that larger second page.
        start_page = await agents.inbox('recipient', tail=True)
        client.checked(
            await client.call(
                'file.create',
                {
                    'parent': '/@efficient-bots/files',
                    'name': 'between.txt',
                    'data': b64(b'unrelated'),
                },
            )
        )
        for number in range(3):
            await agents.send(
                'sender', 'recipient', 'page message', message_id='page-' + str(number)
            )
        cursor, found = start_page['cursor'], []
        for _ in range(4):
            bounded = await agents.inbox('recipient', cursor=cursor, limit=1)
            assert len(bounded['items']) <= 1
            found.extend(item['id'] for item in bounded['items'])
            cursor = bounded['cursor']
        assert found == ['page-0', 'page-1', 'page-2']
        print({
            'send_requests': len(send_calls),
            'send_seconds': round(send_seconds, 3),
            'inbox_requests': len(inbox_calls),
            'inbox_seconds': round(inbox_seconds, 3),
        })
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_changes_hints_current_acl_and_mailbox_cursor_scope(installed, tmp_path):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'bounded-bots')
    stranger, other_http = await client_for(app, tmp_path / 'other', 'bounded-other')
    agents = RemoteAgents(client)
    try:
        await agents.create('sender')
        await agents.create('recipient')
        parent = '/@bounded-bots/files/agents/recipient'
        legacy = client.checked(await client.call('communication.changes', {})).data
        tail = await agents.inbox('recipient', tail=True)
        with pytest.raises(Failure, match='invalid_subagent_cursor'):
            await agents.inbox('sender', cursor=tail['cursor'])
        event = await agents.send(
            'sender', 'recipient', 'bounded private text', message_id='protected'
        )
        assert event['id'] == 'protected'
        page = client.checked(
            await client.call(
                'communication.changes',
                {
                    'cursor': legacy['tail_cursor'],
                },
            )
        ).data
        assert len(page['items']) == 1
        meta, _ = await agents._json(parent + '/msg-protected.json')
        for event in page['items']:
            assert set(event['resource_parents']) == {ref['id'] for ref in event['resources']}
            assert event['resource_parents'][meta['id']]['name'] == 'msg-protected.json'
            assert isinstance(event['resume_cursor'], str)
        visible = client.checked(await stranger.call('communication.changes', {})).data
        assert meta['id'] not in repr(visible)
        assert 'msg-protected.json' not in repr(visible)
        client.checked(
            await client.call(
                'content.chmod',
                {'id': meta['id'], 'mode': '0644'},
                expected=((meta['id'], meta['generation']),),
            )
        )
        with pytest.raises(Failure, match='subagent_private_namespace_conflict'):
            await agents.inbox('recipient')
        # A change in read authority must invalidate previously minted cursors.
        stale = await client.call(
            'communication.changes',
            {
                'cursor': page['sync_cursor'],
            },
        )
        assert stale.error.code == 'resync_required'
    finally:
        await http.aclose()
        await other_http.aclose()


@pytest.mark.asyncio
async def test_remote_old_output_compatibility_and_replay_privacy(installed, tmp_path):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'legacy-efficient')
    agents = RemoteAgents(client)
    try:
        await agents.create('sender')
        await agents.create('recipient')
        await agents.send('sender', 'recipient', 'old deployment', message_id='legacy')
        original_transport = client.transport.call
        fresh_pages = 0
        requested_limits = []

        async def old_output(packet):
            nonlocal fresh_pages
            result = await original_transport(packet)
            if packet.operation == 'communication.changes' and result.status == 'ok':
                requested_limits.append(packet.arguments['limit'])
                if fresh_pages:
                    fresh_pages -= 1
                    return result
                data = wire(result.data)
                data.pop('tail_cursor', None)
                for event in data['items']:
                    event.pop('resource_parents', None)
                    event.pop('resume_cursor', None)
                return replace(result, data=data)
            return result

        client.transport.call = old_output
        assert [item['id'] for item in (await agents.inbox('recipient'))['items']] == ['legacy']
        tail = await agents.inbox('recipient', tail=True)
        await agents.send('sender', 'recipient', 'legacy delta', message_id='next')
        delta = await agents.inbox('recipient', cursor=tail['cursor'])
        assert [item['id'] for item in delta['items']] == ['next']
        mixed_tail = await agents.inbox('recipient', tail=True)
        client.checked(
            await client.call(
                'file.create',
                {
                    'parent': '/@legacy-efficient/files',
                    'name': 'unrelated.txt',
                    'data': b64(b'unrelated'),
                },
            )
        )
        for number in range(3):
            await agents.send(
                'sender', 'recipient', 'mixed worker message', message_id='mixed-' + str(number)
            )
        fresh_pages = 1
        requested_limits.clear()
        first = await agents.inbox('recipient', cursor=mixed_tail['cursor'], limit=1)
        assert [item['id'] for item in first['items']] == ['mixed-0']
        assert requested_limits == [1, 200, 1]
        cursor, found = first['cursor'], ['mixed-0']
        for _ in range(3):
            page = await agents.inbox('recipient', cursor=cursor, limit=1)
            assert len(page['items']) <= 1
            found.extend(item['id'] for item in page['items'])
            cursor = page['cursor']
        assert found == ['mixed-0', 'mixed-1', 'mixed-2']
        original = agents._call
        filters = []

        async def denied(operation, arguments, **kwargs):
            if operation == 'communication.changes':
                filters.append(arguments)
                raise Failure('permission_denied')
            return await original(operation, arguments, **kwargs)

        agents._call = denied
        with pytest.raises(Failure, match='permission_denied'):
            await agents.inbox('recipient')
        assert len(filters) == 1 and set(filters[0]) == {'limit'}
        # A lost response can be replayed after another operation changed the
        # new leaf. Its saved projection must not authorize current privacy.
        agents._call = original
        transport_call = client.transport.call

        async def replay_after_chmod(packet):
            result = await transport_call(packet)
            if packet.operation == 'file.create' and packet.arguments['name'] == 'msg-replay.json':
                client.checked(result)
                identifier = result.resources[0].id
                client.checked(
                    await client.call(
                        'content.chmod',
                        {'id': identifier, 'mode': '0644'},
                        expected=((identifier, result.data['generation']),),
                    )
                )
                replay = await transport_call(packet)
                assert replay.replayed
                return replay
            return result

        client.transport.call = replay_after_chmod
        with pytest.raises(Failure, match='subagent_private_namespace_conflict'):
            await agents.send('sender', 'recipient', 'must recheck replay', message_id='replay')
    finally:
        await http.aclose()
