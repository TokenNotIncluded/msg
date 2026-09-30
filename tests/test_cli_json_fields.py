"""CLI field discovery and projection use the exact signed read contract."""

from types import SimpleNamespace

import pytest

from msg import cli
from msg.core.errors import Failure


def test_call_accepts_explicit_contract_and_optional_json_fields():
    args = cli.parser().parse_args([
        'call',
        'discovery.read_query',
        '--contract-version',
        '3',
        '--json',
        'id,name',
    ])
    assert args.contract_version == 3
    assert args.json_fields == 'id,name'
    assert cli.parser().parse_args(['call', 'discovery.read_query', '--json']).json_fields == ''


class SchemaClient:
    def __init__(self, *, effect='read', fields=('id', 'name'), status='ok'):
        self.calls = []
        self.result = SimpleNamespace(
            status=status,
            data={
                'operation': {'effect': effect},
                'input': {'properties': {'fields': {'items': {'enum': list(fields)}}}},
            },
        )

    async def call(self, operation, arguments):
        self.calls.append((operation, arguments))
        return self.result


@pytest.mark.asyncio
async def test_bare_json_discovers_exact_schema_without_query():
    from msg.client_output import json_fields

    client = SchemaClient()
    params, result = await json_fields(client, 'discovery.read_query', 3, {}, '')
    assert params is None
    assert result == {'status': 'ok', 'data': {'fields': ['id', 'name']}}
    assert client.calls == [('discovery.schema', {'operation': 'discovery.read_query@3'})]


@pytest.mark.asyncio
async def test_json_selection_becomes_server_arguments_without_mutating_input():
    from msg.client_output import json_fields

    client = SchemaClient()
    original = {'parent': '/', 'query_version': 3}
    params, result = await json_fields(client, 'discovery.read_query', 3, original, 'name,id')
    assert result is None
    assert params == {**original, 'fields': ['name', 'id']}
    assert 'fields' not in original


@pytest.mark.asyncio
@pytest.mark.parametrize('selection', ['id,id', ',id', 'id,', 'unknown', 'id, name'])
async def test_invalid_field_selection_fails_without_business_read(selection):
    from msg.client_output import json_fields

    client = SchemaClient()
    with pytest.raises(Failure, match='invalid_json_fields'):
        await json_fields(client, 'discovery.read_query', 3, {}, selection)
    assert all(op == 'discovery.schema' for op, _ in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [{'fields': ['id']}, {'cursor': 'opaque'}])
async def test_json_rejects_existing_projection_or_cursor_before_network(arguments):
    from msg.client_output import json_fields

    client = SchemaClient()
    with pytest.raises(Failure, match='json_query_conflict'):
        await json_fields(client, 'discovery.read_query', 3, arguments, 'id')
    assert not client.calls


@pytest.mark.asyncio
async def test_json_never_executes_mutating_contract():
    from msg.client_output import json_fields

    client = SchemaClient(effect='transaction')
    with pytest.raises(Failure, match='json_read_required'):
        await json_fields(client, 'content.post_create', 1, {}, 'id')
    assert client.calls == [('discovery.schema', {'operation': 'content.post_create@1'})]


@pytest.mark.asyncio
async def test_json_preserves_schema_error_and_missing_field_metadata_is_explicit():
    from msg.client_output import json_fields

    client = SchemaClient(status='error')
    assert await json_fields(client, 'discovery.read_query', 3, {}, '') == (None, client.result)
    client = SchemaClient(fields=())
    with pytest.raises(Failure, match='json_fields_unavailable'):
        await json_fields(client, 'discovery.get', 1, {}, '')


@pytest.mark.asyncio
async def test_real_cli_json_discovery_projection_cursor_and_readonly_state(
    installed, tmp_path, monkeypatch, capsys
):
    import httpx
    from read_only_evidence import readonly_evidence
    from test_service import NOW

    from msg.client import ClientState, MsgClient
    from msg.core.codec import canonical, loads
    from msg.core.read_query import ROOT_FIELDS
    from msg.transports.client import HTTPTransport
    from msg.transports.http import create_app

    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        directory = tmp_path / 'json-client'
        client = MsgClient(
            ClientState(directory, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('json-client')).status == 'ok'
        for body in ('first', 'second'):
            assert (
                await client.call('content.post_create', {'parent': '/main', 'body': body})
            ).status == 'ok'
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        monkeypatch.setattr(
            cli,
            'MsgClient',
            lambda state, transport: MsgClient(state, transport, clock=lambda: NOW),
        )

        async def invoke(operation, arguments, *flags):
            args = cli.parser().parse_args([
                '--config-dir',
                str(directory),
                '--server',
                app.settings.service_url,
                'call',
                operation,
                canonical(arguments).decode(),
                *flags,
            ])
            status = await cli.run(args)
            return status, loads(capsys.readouterr().out.encode())

        async with readonly_evidence(app, monkeypatch):
            status, listing = await invoke(
                'discovery.read_query', {}, '--contract-version', '3', '--json'
            )
            assert status == 0 and listing == {
                'status': 'ok',
                'data': {'fields': list(ROOT_FIELDS)},
            }
            status, page = await invoke(
                'discovery.read_query',
                {'parent': '/main', 'query_version': 3, 'limit': 1},
                '--contract-version',
                '3',
                '--json',
                'id,name',
            )
            assert status == 0 and page['status'] == 'ok'
            assert len(page['data']['items']) == 1 and page['data']['cursor']
            assert set(page['data']['items'][0]) == {'id', 'name'}
            for flag, expression in (
                ('--jq', '.data.items[0].name'),
                ('--template', '{{/data/items/0/name}}'),
            ):
                status, name = await invoke(
                    'discovery.read_query',
                    {'parent': '/main', 'query_version': 3, 'limit': 1},
                    '--contract-version',
                    '3',
                    '--json',
                    'id,name',
                    flag,
                    expression,
                )
                assert status == 0 and name == page['data']['items'][0]['name']
            status, next_page = await invoke(
                'discovery.read_query',
                {'cursor': page['data']['cursor']},
                '--contract-version',
                '3',
            )
            assert status == 0 and set(next_page['data']['items'][0]) == {'id', 'name'}
            assert next_page['data']['items'][0]['id'] != page['data']['items'][0]['id']
            with pytest.raises(Failure, match='json_query_conflict'):
                await invoke(
                    'discovery.read_query',
                    {'cursor': page['data']['cursor']},
                    '--contract-version',
                    '3',
                    '--json',
                    'id',
                )
            with pytest.raises(Failure, match='json_read_required'):
                await invoke(
                    'content.post_create', {'parent': '/main', 'body': 'never written'}, '--json'
                )
            with pytest.raises(Failure, match='json_fields_unavailable'):
                await invoke('discovery.get', {'id': '/main'}, '--json')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('selection', 'formatter'),
    [('', None), ('id,name', None), (None, None), ('id,name', '--jq'), ('id,name', '--template')],
)
async def test_real_cli_dispatch_keeps_version_and_machine_output(
    selection, formatter, tmp_path, monkeypatch, capsys
):
    from msg.core.codec import loads
    from msg.core.models import OperationResult

    calls = []
    closed = []

    class Transport:
        async def close(self):
            closed.append(True)

    class Client:
        def __init__(self, state, transport):
            pass

        async def call(self, operation, arguments, **kwargs):
            calls.append((operation, arguments, kwargs))
            data = SchemaClient().result.data if operation == 'discovery.schema' else {'items': []}
            return OperationResult(
                request_id='test',
                operation=operation,
                status='ok',
                actor=None,
                subject=None,
                resources=(),
                data=data,
            )

    monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: Transport())
    monkeypatch.setattr(cli, 'MsgClient', Client)
    argv = [
        '--config-dir',
        str(tmp_path),
        '--server',
        'https://msg.invalid',
        'call',
        'discovery.read_query',
        '{"parent":"/","query_version":3}',
        '--contract-version',
        '3',
    ]
    if selection is not None:
        argv += ['--json'] + ([selection] if selection else [])
    if formatter is not None:
        argv += [formatter, '.' if formatter == '--jq' else '{{}}']
    assert await cli.run(cli.parser().parse_args(argv)) == 0
    captured = capsys.readouterr()
    assert captured.err == ''
    assert loads(captured.out.encode())['status'] == 'ok'
    assert closed == [True]
    if selection == '':
        assert len(calls) == 1 and calls[0][0] == 'discovery.schema'
    else:
        operation, params, kwargs = calls[-1]
        assert operation == 'discovery.read_query' and kwargs['contract_version'] == 3
        assert ('fields' in params) == (selection is not None)
        if selection is not None:
            assert params['fields'] == ['id', 'name']
        else:
            assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'flags',
    [
        ['--json', 'id', '--return-field', 'status'],
        ['--jq', '.'],
        ['--template', '{{/data}}'],
        ['--json', '--jq', '.'],
        ['--json', '--template', '{{/data}}'],
    ],
)
async def test_cli_format_conflicts_refuse_before_network(flags, tmp_path, monkeypatch):
    class Transport:
        async def close(self):
            pass

    class Client:
        def __init__(self, state, transport):
            pass

        async def call(self, *args, **kwargs):
            raise AssertionError('invalid flags must not contact service')

    monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: Transport())
    monkeypatch.setattr(cli, 'MsgClient', Client)
    args = cli.parser().parse_args([
        '--config-dir',
        str(tmp_path),
        'call',
        'discovery.read_query',
        *flags,
    ])
    with pytest.raises(Failure, match='json_query_conflict|json_fields_required'):
        await cli.run(args)


@pytest.mark.asyncio
async def test_schema_error_preserves_json_and_nonzero_exit_without_formatter(
    tmp_path, monkeypatch, capsys
):
    import msg.client_output as output
    from msg.core.codec import loads
    from msg.core.models import OperationResult

    calls = []

    class Transport:
        async def close(self):
            pass

    class Client:
        def __init__(self, state, transport):
            pass

        async def call(self, operation, arguments, **kwargs):
            calls.append(operation)
            return OperationResult(
                request_id='error',
                operation=operation,
                status='error',
                actor=None,
                subject=None,
            )

    async def forbidden(*args, **kwargs):
        raise AssertionError('error response must not be filtered')

    monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: Transport())
    monkeypatch.setattr(cli, 'MsgClient', Client)
    monkeypatch.setattr(output, 'render_jq', forbidden)
    args = cli.parser().parse_args([
        '--config-dir',
        str(tmp_path),
        'call',
        'discovery.read_query',
        '--contract-version',
        '3',
        '--json',
        'id',
        '--jq',
        '.',
    ])
    assert await cli.run(args) == 1
    captured = capsys.readouterr()
    assert captured.err == '' and loads(captured.out.encode())['status'] == 'error'
    assert calls == ['discovery.schema']


def test_local_format_error_goes_only_to_stderr(monkeypatch, capsys):
    from msg.core.codec import loads

    async def fail(args):
        raise Failure('invalid_jq_query')

    monkeypatch.setattr(cli, 'run', fail)
    assert cli.main(['call', 'discovery.read_query', '--json', 'id', '--jq', 'bad']) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert loads(captured.err.encode())['error']['code'] == 'invalid_jq_query'
