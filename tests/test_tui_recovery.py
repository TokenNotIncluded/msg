"""Terminal layout and reconnect failures cannot retain selectable stale data."""

import unicodedata
from io import StringIO
from types import SimpleNamespace

import pytest
from test_tui import FakeClient

from msg.core.errors import Failure
from msg.tui import TerminalUI


def cells(text):
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {'W', 'F'}
        else 1
        for char in text
    )


def test_terminal_preserves_document_lines_and_blank_paragraphs():
    output = StringIO()
    TerminalUI(FakeClient(), stdout=output, width=40)._write('first\n\nsecond\nthird')
    assert output.getvalue() == 'first\n\nsecond\nthird\n'


@pytest.mark.parametrize('width', [1, 3, 10, 24, 32])
def test_terminal_wraps_by_display_cells_and_respects_narrow_windows(width):
    output = StringIO()
    TerminalUI(FakeClient(), stdout=output, width=width)._write('中文阅读与测试 e\u0301 ' * 8)
    assert all(cells(line) <= width for line in output.getvalue().splitlines())
    assert '\u0301' in output.getvalue()


@pytest.mark.asyncio
async def test_disconnect_clears_page_and_explicit_retry_rechecks_current_authority():
    class Client(FakeClient):
        async def call(self, operation, arguments):
            self.calls.append((operation, arguments))
            if len(self.calls) == 1:
                return SimpleNamespace(
                    status='ok', data={'items': [{'id': 'private'}], 'cursor': 'cursor-one'}
                )
            if len(self.calls) == 2:
                raise Failure(
                    'transport_uncertain', details={'url': 'https://secret.example/token'}
                )
            return SimpleNamespace(status='error', error=SimpleNamespace(code='permission_denied'))

    client = Client(subject='u_me')
    output = StringIO()
    ui = TerminalUI(client, stdout=output)
    await ui.home()
    await ui.next_page()
    assert ui.page is None
    before = len(client.calls)
    await ui.command('1')
    assert len(client.calls) == before
    await ui.command('retry')
    assert client.calls[-1] == ('discovery.read_query', {'cursor': 'cursor-one'})
    assert ui.page is None
    assert 'transport_uncertain' in output.getvalue()
    assert 'permission_denied' in output.getvalue()
    assert 'secret.example' not in output.getvalue()


@pytest.mark.asyncio
async def test_retry_reloads_a_document_without_ack_or_implicit_mutation():
    class Client(FakeClient):
        async def call(self, operation, arguments):
            self.calls.append((operation, arguments))
            if len(self.calls) == 1:
                raise OSError('secret token must not be shown')
            return SimpleNamespace(status='ok', data={'content': 'reconnected\nsecond line'})

    client = Client()
    output = StringIO()
    ui = TerminalUI(client, stdout=output)
    await ui.read('r_document')
    await ui.command('retry')
    assert client.calls == [('discovery.get', {'id': 'r_document'})] * 2
    assert 'reconnected\nsecond line' in output.getvalue()
    assert 'secret token' not in output.getvalue()


@pytest.mark.asyncio
async def test_tui_additional_views_use_existing_authorized_read_contracts():
    client = FakeClient(subject='u_me')
    client.responses = {
        'identity.note_list': {'items': [{'id': 'r_note', 'name': 'remember.md'}]},
        'identity.note_get': {'content': 'private note'},
        'identity.todo_list': {
            'items': [{'id': 'r_todo', 'name': 'work'}],
            'next_after_name': 'work',
        },
        'identity.todo_get': {'title': 'finish', 'status': 'pending'},
        'discovery.get': {'path': '/@me'},
    }
    ui = TerminalUI(client, stdout=StringIO())
    for command in ('topics', 'outbox', 'groups', 'notes', '1', 'todos', '1', 'files'):
        await ui.command(command)
    assert ('communication.outbox', {'limit': 20}) in client.calls
    assert ('identity.note_get', {'name': 'remember.md'}) in client.calls
    assert ('identity.todo_get', {'name': 'work'}) in client.calls
    assert ('discovery.read_query', {'parent': '/@me/files', 'limit': 20}) in client.calls
    assert all(
        name not in {'discussion.ack', 'content.post_create', 'identity.todo_put'}
        for name, _ in client.calls
    )


@pytest.mark.asyncio
async def test_todo_next_page_uses_its_own_pagination_contract():
    client = FakeClient(subject='u_me')
    client.responses['identity.todo_list'] = [
        {'items': [{'id': 'r_one', 'name': 'one'}], 'next_after_name': 'one'},
        {'items': [{'id': 'r_two', 'name': 'two'}], 'next_after_name': None},
    ]
    ui = TerminalUI(client, stdout=StringIO())
    await ui.command('todos')
    await ui.command('n')
    assert client.calls[-1] == ('identity.todo_list', {'limit': 20, 'after_name': 'one'})
    assert ui.page['cursor'] is None


@pytest.mark.asyncio
async def test_real_tui_private_views_are_read_only_and_do_not_expose_other_subjects(
    installed, tmp_path
):
    import httpx
    from test_route_effect_matrix import database_snapshot
    from test_service import NOW

    from msg.client import ClientState, MsgClient
    from msg.transports.client import HTTPTransport
    from msg.transports.http import create_app

    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:

        def client(name):
            return MsgClient(
                ClientState(tmp_path / name, server=app.settings.service_url),
                HTTPTransport(app.settings.service_url, http=http),
                clock=lambda: NOW,
            )

        owner, other = client('owner'), client('other')
        assert (await owner.register('tui-private-owner')).status == 'ok'
        assert (await other.register('tui-private-other')).status == 'ok'
        for connection, marker in ((owner, 'owner-visible'), (other, 'other-hidden')):
            for op, args in (
                ('identity.note_put', {'name': 'daily', 'body': marker}),
                (
                    'identity.todo_put',
                    {'name': 'task', 'title': marker, 'description': 'next step'},
                ),
                (
                    'content.post_create',
                    {
                        'parent': '/@'
                        + ('tui-private-owner' if connection is owner else 'tui-private-other')
                        + '/files',
                        'name': marker,
                        'body': marker,
                    },
                ),
            ):
                result = await connection.call(op, args)
                assert result.status == 'ok', result
        before = await database_snapshot(app)
        output = StringIO()
        ui = TerminalUI(owner, stdout=output, width=40)
        for command in (
            'topics',
            'groups',
            'outbox',
            'notes',
            '1',
            'todos',
            '1',
            'files',
            'credentials',
        ):
            await ui.command(command)
        assert await database_snapshot(app) == before
        assert '读取失败' not in output.getvalue(), output.getvalue()
        assert output.getvalue().count('owner-visible') >= 3
        assert 'other-hidden' not in output.getvalue()


@pytest.mark.asyncio
async def test_anonymous_private_view_drops_stale_selection_and_retry():
    client = FakeClient(subject='u_me')
    client.responses['communication.inbox'] = {'items': [{'id': 'private'}], 'cursor': 'old-cursor'}
    ui = TerminalUI(client, stdout=StringIO())
    await ui.command('inbox')
    client.state.subject = None
    await ui.command('inbox')
    assert ui.page is None
    before = len(client.calls)
    await ui.command('retry')
    assert len(client.calls) == before


def test_credentials_view_never_prints_secret_state_or_url_credentials():
    client = FakeClient(subject='u_me')
    client.state.server = 'https://user:password@example.invalid/?token=secret#proof'
    client.state.certificates = ('cert_public_id',)
    client.state.data = {'token': 'private-token', 'private_key': 'private-key'}
    output = StringIO()
    TerminalUI(client, stdout=output).credentials()
    text = output.getvalue()
    assert 'cert_public_id' in text and 'https://example.invalid' in text
    for secret in ('password', 'secret', 'private-token', 'private-key', '#proof'):
        assert secret not in text
