"""The terminal navigator uses only read contracts and never acknowledges."""

from io import StringIO
from types import SimpleNamespace

import httpx
import pytest
from test_service import NOW

from msg.client import ClientState, MsgClient
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from msg.tui import TerminalUI, run_tui, safe_text


class FakeClient:
    def __init__(self, *, subject=None):
        self.state = SimpleNamespace(subject=subject, server='https://msg.example')
        self.calls = []
        self.responses = {}

    async def call(self, operation, arguments):
        self.calls.append((operation, arguments))
        answer = self.responses.get(operation, {'items': []})
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, str):
            return SimpleNamespace(status='error', error=SimpleNamespace(code=answer), data={})
        return SimpleNamespace(status='ok', error=None, data=answer)


@pytest.mark.asyncio
async def test_tui_read_call_matrix_cursor_revocation_and_safe_rendering():
    client = FakeClient(subject='u_me')
    client.responses = {
        'discovery.read_query': [
            {'items': [{'id': 't_public', 'name': '\x1b[31mMain'}], 'cursor': 'next-secret'},
            'access_denied',
        ],
        'communication.inbox': {'items': [{'resource': {'id': 'p_1'}, 'source': 'dm'}]},
        'discovery.lexical_search': {'items': [{'id': 'p_1', 'name': 'found'}]},
        'discussion.thread': {'items': [{'id': 'p_1', 'name': 'reply'}]},
        'discovery.get': {'path': '/main/p_1.md', 'content': '\x1b]0;malicious\x07Hello'},
    }
    output = StringIO()
    ui = TerminalUI(client, stdout=output, width=24)
    for command in ('h', 'n'):
        assert await ui.command(command)
    assert ui.page is None  # expired/revoked cursor is no longer selectable
    for command in ('id', 'i', 's /main found', 't p_1', 'r p_1'):
        assert await ui.command(command)
    assert ('discovery.read_query', {'cursor': 'next-secret'}) in client.calls
    assert all(
        operation
        in {
            'discovery.read_query',
            'communication.inbox',
            'discovery.lexical_search',
            'discussion.thread',
            'discovery.get',
        }
        for operation, _ in client.calls
    )
    assert '\x1b' not in output.getvalue() and '\x07' not in output.getvalue()
    assert 'https://msg.example' in output.getvalue()
    assert 'access_denied' in output.getvalue()
    assert safe_text('\u202eabc\n') == ' abc '


@pytest.mark.asyncio
async def test_tui_anonymous_entry_and_eof_exits_without_mutation():
    client = FakeClient()
    client.responses['discovery.read_query'] = {'items': []}
    output = StringIO()
    await run_tui(client, stdin=StringIO('id\ni\nq\n'), stdout=output, width=24)
    assert client.calls == [('discovery.read_query', {'parent': '/', 'limit': 20})]
    assert '未登录' in output.getvalue() and '需要已登录' in output.getvalue()


@pytest.mark.asyncio
async def test_tui_real_pg_read_and_revocation_no_business_effect(installed, tmp_path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        author = MsgClient(
            ClientState(tmp_path / 'author', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await author.register('tui-author')).status == 'ok'
        post = await author.call(
            'content.post_create', {'parent': '/main', 'body': 'TUI public text'}
        )
        assert post.status == 'ok'
        rid = post.resources[0].id
        reader = MsgClient(
            ClientState(tmp_path / 'anonymous', server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        output = StringIO()
        ui = TerminalUI(reader, stdout=output, width=32)
        async with app.metadata.transaction(write=False) as tx:
            before = tuple(
                tx.one(f'SELECT COUNT(*) FROM {table}')[0]
                for table in ('resources', 'revisions', 'messages', 'events', 'reactions')
            )
        await ui.home()
        await ui.read(rid)
        output.seek(0)
        output.truncate(0)
        await ui.search('/main', 'TUI public')
        assert 'TUI public text' in output.getvalue()
        await ui.thread(rid)
        async with app.metadata.transaction(write=False) as tx:
            after = tuple(
                tx.one(f'SELECT COUNT(*) FROM {table}')[0]
                for table in ('resources', 'revisions', 'messages', 'events', 'reactions')
            )
        assert after == before
        assert 'TUI public text' in output.getvalue()
        assert (
            await author.call(
                'content.chmod',
                {'id': rid, 'mode': '0600'},
                expected=((rid, post.data['generation']),),
            )
        ).status == 'ok'
        ui.page = None
        hidden = StringIO()
        ui.stdout = hidden
        await ui.read(rid)
        assert 'TUI public text' not in hidden.getvalue()
        assert '读取失败' in hidden.getvalue()
