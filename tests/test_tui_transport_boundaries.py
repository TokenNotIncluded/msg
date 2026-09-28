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
