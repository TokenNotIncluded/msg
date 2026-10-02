"""Batched receives recheck private directories and config over signed HTTP."""

import pytest
from test_client_subagents_remote import client_for

from msg.client_subagents_remote import RemoteAgents
from msg.core.codec import b64, canonical
from msg.core.errors import Failure


@pytest.mark.asyncio
async def test_receive_fresh_namespace_and_config_checks_preserve_archived_history(
    installed, tmp_path
):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'receive-authority')
    agents = RemoteAgents(client)
    root = '/@receive-authority/files/agents'
    config_path = root + '/recipient/agent.json'
    paths = []

    async def record(request):
        paths.append(request.url.path)

    async def meta_for(path):
        return client.checked(await client.call('discovery.get', {'id': path, 'view': 'meta'})).data

    async def change_mode(meta, mode):
        result = client.checked(
            await client.call(
                'content.chmod',
                {'id': meta['id'], 'mode': mode},
                expected=((meta['id'], meta['generation']),),
            )
        )
        return {**meta, 'generation': result.data['generation']}

    async def write_config(value):
        meta = await meta_for(config_path)
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

    async def fresh_tail():
        return await agents.inbox('recipient', tail=True)

    async def denied(code):
        paths.clear()
        before = client.transport.calls
        with pytest.raises(Failure) as caught:
            await fresh_tail()
        assert caught.value.code == code
        # Real HTTP hooks confirm rejection did not fall back or fetch changes.
        assert client.transport.calls - before == len(paths) == 2
        assert '/-/p/communication.changes' not in paths

    try:
        await agents.create('sender')
        await agents.create('recipient')
        http.event_hooks['request'].append(record)
        assert (await fresh_tail())['items'] == []
        # Reuse the same reader after every real server mutation: no cached grant.
        for path in (root.rpartition('/')[0], root, root + '/recipient', config_path):
            meta = await meta_for(path)
            changed = await change_mode(meta, '0644' if meta['type'] == 'file' else '0711')
            await denied('subagent_private_namespace_conflict')
            await change_mode(changed, meta['mode'])
            assert (await fresh_tail())['items'] == []

        config = {
            'version': 2,
            'owner': client.state.subject,
            'name': 'recipient',
            'archived': False,
        }
        await write_config({**config, 'owner': 'r_another_account'})
        await denied('invalid_subagent_message')
        await write_config(config)
        tail = await fresh_tail()
        await agents.send('sender', 'recipient', 'readable history', message_id='history')
        await agents.archive('recipient')
        # A logically archived label remains readable in its active private namespace.
        page = await agents.inbox('recipient', cursor=tail['cursor'])
        assert [item['id'] for item in page['items']] == ['history']
        assert (await agents.inbox('recipient', cursor=page['cursor']))['items'] == []
    finally:
        await http.aclose()
