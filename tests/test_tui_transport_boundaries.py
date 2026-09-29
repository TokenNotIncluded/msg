"""No implicit retries/writes or stale selections after a terminal read fails."""
from io import StringIO
from types import SimpleNamespace

import httpx
import pytest

from msg.tui import READ_OPERATIONS, TerminalUI


class Client:
    def __init__(self, replies):
        self.state = SimpleNamespace(subject='alice', server='http://testserver', certificates=())
        self.replies = list(replies)
        self.calls = []

    async def call(self, operation, arguments):
        assert operation in READ_OPERATIONS
        self.calls.append((operation, dict(arguments)))
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(status='ok', data=result)


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout,
    httpx.WriteError, httpx.PoolTimeout, httpx.RemoteProtocolError, httpx.DecodingError])
async def test_httpx_disconnect_is_sanitized_and_retry_is_explicit(error_type):
    secret = 'https://testserver/private?token=never-display-this'
    client = Client([{'items': [{'id': 'old', 'name': 'OLD'}], 'cursor': 'old-cursor'},
                     error_type(secret), {'items': [{'id': 'new', 'name': 'FRESH'}]}])
    output = StringIO()
    ui = TerminalUI(client, stdout=output, width=160)
    await ui.home()
    await ui.next_page()
    assert ui.page is None
    assert len(client.calls) == 2
    assert secret not in output.getvalue()
    assert 'transport_unavailable' in output.getvalue()
    await ui.command('1')
    assert len(client.calls) == 2
    await ui.command('retry')
    assert len(client.calls) == 3
    assert client.calls[1] == client.calls[2]
    assert ui.page['items'][0]['id'] == 'new'
    assert 'FRESH' in output.getvalue()


@pytest.mark.asyncio
async def test_successful_document_replaces_old_list_selection():
    client = Client([{'items': [{'id': 'old'}], 'cursor': 'old-cursor'},
                     {'path': '/main/current.md', 'content': 'CURRENT'}])
    ui = TerminalUI(client, stdout=StringIO())
    await ui.home()
    await ui.read('current')
    assert ui.page is None
    await ui.command('1')
    await ui.command('n')
    assert len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['subject', 'server', 'certificates'])
@pytest.mark.parametrize('command', ['1', 'n', 'retry'])
async def test_changed_identity_or_service_drops_old_selection(change, command):
    client = Client([{'items': [{'id': 'old-private'}], 'cursor': 'old-cursor'}])
    ui = TerminalUI(client, stdout=StringIO())
    await ui.home()
    setattr(client.state, change, {'subject': 'bob', 'server': 'http://other',
                                  'certificates': ('new-cert',)}[change])
    await ui.command(command)
    assert ui.page is None
    assert ui._last_read is None
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_oversized_selection_is_rejected_without_integer_conversion_crash():
    client = Client([{'items': [{'id': 'one'}]}])
    ui = TerminalUI(client, stdout=StringIO())
    await ui.home()
    await ui.command('9' * 5000)
    assert len(client.calls) == 1
    assert '序号不在本页' in ui.stdout.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    ['not', 'an', 'object'], 'text', None, 7,
    {'items': 'abc'}, {'items': ['x']}, {'items': [{'id': 'ok'}, None]},
    {'items': [], 'cursor': {'forged': 'cursor'}}, {'items': [], 'cursor': 5},
])
async def test_malformed_read_payload_is_a_stable_error_not_a_crash(payload):
    client = Client([{'items': [{'id': 'old', 'name': 'OLD'}], 'cursor': 'old-cursor'},
                     payload, {'items': [{'id': 'new', 'name': 'FRESH'}]}])
    output = StringIO()
    ui = TerminalUI(client, stdout=output, width=160)
    await ui.home()
    await ui.next_page()
    assert ui.page is None
    assert '读取失败：invalid_read_response' in output.getvalue()
    await ui.command('1')
    await ui.command('n')
    assert len(client.calls) == 2
    await ui.command('retry')
    assert client.calls[1] == client.calls[2]
    assert ui.page['items'][0]['id'] == 'new'


@pytest.mark.asyncio
async def test_frozen_client_payload_is_still_a_valid_page():
    from msg.core.codec import freeze_json
    client = Client([freeze_json({'items': [{'ref': {'id': 'r_frozen'}, 'name': 'FROZEN'}],
                                  'cursor': 'next'})])
    output = StringIO()
    ui = TerminalUI(client, stdout=output, width=160)
    await ui.home()
    assert ui.page['cursor'] == 'next'
    assert '1. FROZEN  r_frozen' in output.getvalue()


@pytest.mark.asyncio
async def test_items_without_readable_identifier_are_not_dereferenced():
    client = Client([{'items': [{'name': 'no id'}, {'ref': 'not-a-dict'},
                                {'resource': {'id': ['list']}}, {'id': 'r_ok', 'ref': 'x'}]}])
    output = StringIO()
    ui = TerminalUI(client, stdout=output, width=160)
    await ui.home()
    for command in ('1', '2', '3'):
        await ui.command(command)
    assert len(client.calls) == 1
    assert output.getvalue().count('该条目没有可读取的标识。') == 3
    client.replies.append({'path': '/main/r_ok.md', 'content': 'OK'})
    await ui.command('4')
    assert client.calls[-1] == ('discovery.get', {'id': 'r_ok'})


@pytest.mark.asyncio
@pytest.mark.parametrize('name', [None, '', 5, {'x': 1}])
async def test_note_or_todo_without_name_is_not_requested(name):
    item = {'id': 'r_note'} if name is None else {'id': 'r_note', 'name': name}
    client = Client([{'items': [item]}])
    output = StringIO()
    ui = TerminalUI(client, stdout=output)
    await ui.command('notes')
    await ui.command('1')
    assert client.calls == [('identity.note_list', {})]
    assert '该条目没有可读取的标识。' in output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize('profile', [{}, {'path': None}, {'path': 'relative'}, {'path': ['/x']}])
async def test_files_rejects_profile_without_absolute_path(profile):
    client = Client([profile, {'path': '/@alice'}, {'items': []}])
    output = StringIO()
    ui = TerminalUI(client, stdout=output)
    await ui.command('files')
    assert client.calls == [('discovery.get', {'id': 'alice', 'fields': ['path']})]
    assert 'invalid_read_response' in output.getvalue()
    await ui.command('retry')
    assert client.calls[-1] == ('discovery.read_query', {'parent': '/@alice/files', 'limit': 20})


@pytest.mark.asyncio
async def test_inflight_read_from_previous_identity_is_not_displayed():
    client = Client([])

    async def switch_during_read(operation, arguments):
        client.calls.append((operation, arguments))
        client.state.subject = 'bob'
        return SimpleNamespace(status='ok', data={'items': [{'id': 'alice-private',
                                                           'name': 'NEVER DISPLAY'}]})

    client.call = switch_during_read
    ui = TerminalUI(client, stdout=StringIO())
    await ui.home()
    assert ui.page is None
    assert ui._last_read is None
    assert 'NEVER DISPLAY' not in ui.stdout.getvalue()
    assert len(client.calls) == 1


@pytest.mark.parametrize('text', [7, {'nested': 'text'}, ['text'], None])
def test_malformed_snippet_text_does_not_crash_or_display(text):
    output = StringIO()
    ui = TerminalUI(Client([]), stdout=output)
    ui._show_items([{'id': 'r_item', 'snippet': {'text': text}}])
    assert output.getvalue().strip() == '1. r_item  r_item'
