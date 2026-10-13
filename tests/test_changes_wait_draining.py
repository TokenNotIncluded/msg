from test_client_subagents_remote import client_for

from msg.client_subagents_remote import RemoteAgents


async def test_mailbox_bounded_irrelevant_pages_never_wait_between_pages(installed, tmp_path):
    app, _ = installed
    client, http = await client_for(app, tmp_path / 'owner', 'changes-filter-bound')
    agents = RemoteAgents(client)
    try:
        await agents.create('receiver')
        calls = []

        async def read(arguments, *, wait=False):
            calls.append((arguments, wait))
            return {'items': [], 'sync_cursor': f'scan-{len(calls)}', 'has_more': True}

        page = await agents.inbox('receiver', changes_reader=read, wait=True)
        assert page['items'] == [] and page['has_more'] is True
        assert len(calls) == 8
        assert [wait for _, wait in calls] == [True] + [False] * 7
    finally:
        await http.aclose()
