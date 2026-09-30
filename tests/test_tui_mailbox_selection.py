"""Mailbox navigation resolves content references, never delivery-record IDs."""

from io import StringIO
from types import MappingProxyType, SimpleNamespace

import pytest

from msg.tui import TerminalUI


class MailboxClient:
    def __init__(self, item, *, denied=False):
        self.state = SimpleNamespace(subject='u_reader', server='https://msg.example')
        self.item, self.denied = item, denied
        self.calls = []

    async def call(self, operation, arguments):
        self.calls.append((operation, arguments))
        if operation == 'discovery.get':
            if self.denied:
                return SimpleNamespace(
                    status='error', error=SimpleNamespace(code='permission_denied')
                )
            return SimpleNamespace(status='ok', data={'content': 'Referenced body'})
        return SimpleNamespace(status='ok', data={'items': [self.item]})


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['inbox', 'outbox'])
@pytest.mark.parametrize('frozen', [False, True])
async def test_mailbox_selection_uses_resource_not_message_id(command, frozen):
    ref = {'id': 'p_content'}
    item = {'id': 'message_delivery', 'resource': ref, 'state': 'delivered'}
    if frozen:
        item = MappingProxyType({**item, 'resource': MappingProxyType(ref)})
    client = MailboxClient(item)
    output = StringIO()
    ui = TerminalUI(client, stdout=output)
    await ui.command(command)
    await ui.command('1')
    assert client.calls == [
        ('communication.' + command, {'limit': 20}),
        ('discovery.get', {'id': 'p_content'}),
    ]
    assert 'Referenced body' in output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['inbox', 'outbox'])
@pytest.mark.parametrize('ref', [None, {}, {'id': ''}, {'id': 7}, 'p_content'])
async def test_mailbox_missing_reference_cannot_open_delivery_record(command, ref):
    client = MailboxClient({'id': 'message_delivery', 'resource': ref})
    ui = TerminalUI(client, stdout=StringIO())
    await ui.command(command)
    await ui.command('1')
    assert client.calls == [('communication.' + command, {'limit': 20})]


@pytest.mark.asyncio
async def test_mailbox_selection_rechecks_access_and_retry_stays_read_only():
    client = MailboxClient({'id': 'message_delivery', 'resource': {'id': 'p_content'}}, denied=True)
    ui = TerminalUI(client, stdout=StringIO())
    await ui.command('inbox')
    await ui.command('1')
    assert ui.page is None
    await ui.command('1')
    await ui.command('retry')
    assert client.calls == [
        ('communication.inbox', {'limit': 20}),
        ('discovery.get', {'id': 'p_content'}),
        ('discovery.get', {'id': 'p_content'}),
    ]


@pytest.mark.asyncio
async def test_ordinary_resource_list_keeps_its_own_id():
    client = MailboxClient({'id': 'p_content', 'resource': {'id': 'p_other'}})
    ui = TerminalUI(client, stdout=StringIO())
    await ui.command('home')
    await ui.command('1')
    assert client.calls[-1] == ('discovery.get', {'id': 'p_content'})


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['inbox', 'outbox'])
async def test_real_signed_mailbox_selection_reads_without_ack(
    installed, tmp_path, monkeypatch, command
):
    import httpx
    from read_only_evidence import readonly_evidence
    from test_service import NOW

    from msg.client import ClientState, MsgClient
    from msg.transports.client import HTTPTransport
    from msg.transports.http import create_app

    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        clients = [
            MsgClient(
                ClientState(tmp_path / name, server=app.settings.service_url),
                HTTPTransport(app.settings.service_url, http=http),
                clock=lambda: NOW,
            )
            for name in ('mailbox-author', 'mailbox-recipient')
        ]
        author, recipient = clients
        for client, name in zip(clients, ('mailbox-author', 'mailbox-recipient'), strict=True):
            assert (await client.register(name)).status == 'ok'
        posted = await author.call(
            'content.post_create', {'parent': '/main', 'body': 'Mailbox referenced content'}
        )
        assert posted.status == 'ok'
        sent = await author.call(
            'communication.send',
            {'recipient': recipient.state.subject, 'resource': {'id': posted.resources[0].id}},
        )
        assert sent.status == 'ok'
        output = StringIO()
        ui = TerminalUI(recipient if command == 'inbox' else author, stdout=output)
        async with readonly_evidence(app, monkeypatch):
            await ui.command(command)
            item = next(
                item
                for item in ui.page['items']
                if item['resource']['id'] == posted.resources[0].id
            )
            assert item['id'] != item['resource']['id']
            await ui.command(str(ui.page['items'].index(item) + 1))
        assert 'Mailbox referenced content' in output.getvalue()
        assert '读取失败' not in output.getvalue()


@pytest.fixture(autouse=True)
def chinese_ui_locale(monkeypatch):
    """Keep existing Chinese rendering expectations independent of the runner locale."""
    monkeypatch.setenv('LC_ALL', 'zh_CN.UTF-8')
