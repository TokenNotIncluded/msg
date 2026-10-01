"""Online coordination uses real signatures, PostgreSQL and HTTP ACL checks."""

import asyncio

import httpx
import pytest
from test_service import NOW

from msg.client import ClientState, MsgClient
from msg.client_subagents_remote import RemoteAgents
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
        with pytest.raises(Failure, match='subagent_message_id_conflict'):
            await agents.send('bot1', 'bot2', 'different', message_id='stable-message')
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
        history = await agents.inbox('bot2')
        assert [x['message'] for x in history['items']] == ['private handoff']
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
            visible = await visitor.call(
                'discovery.list', {'type': 'file', 'limit': 200}, anonymous=anonymous
            )
            assert visible.status == 'ok'
            assert not any('msg-stable-message' in item['name'] for item in visible.data['items'])
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
