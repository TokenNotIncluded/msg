"""Local output formatting must survive a server-bound cursor continuation."""

import pytest

from msg import cli
from msg.core.codec import loads
from msg.core.errors import Failure
from msg.core.models import OperationResult


@pytest.fixture
def cursor_cli(tmp_path, monkeypatch):
    calls = []

    class Transport:
        async def close(self):
            pass

    class Client:
        def __init__(self, state, transport):
            pass

        async def call(self, operation, arguments, **options):
            calls.append((operation, arguments, options))
            data = (
                {'operation': {'effect': 'read', 'name': 'discovery.read_query', 'version': 3}}
                if operation == 'discovery.schema'
                else {'items': [{'name': 'next-page'}], 'cursor': 'next'}
            )
            return OperationResult(
                request_id='read',
                operation=operation,
                status='ok',
                actor=None,
                subject=None,
                data=data,
            )

    monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: Transport())
    monkeypatch.setattr(cli, 'MsgClient', Client)

    async def invoke(operation, arguments, *options):
        return await cli.run(
            cli.parser().parse_args([
                '--server',
                'https://unit.invalid',
                '--config-dir',
                str(tmp_path),
                'call',
                operation,
                arguments,
                '--contract-version',
                '3',
                *options,
            ])
        )

    return invoke, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('flag', 'expression'),
    [
        ('--jq', '.data.items[0].name'),
        ('--template', '{{/data/items/0/name}}'),
    ],
)
async def test_cursor_format_keeps_exact_signed_cursor_and_version(
    cursor_cli, capsys, flag, expression
):
    invoke, calls = cursor_cli
    assert await invoke('discovery.read_query', '{"cursor":"opaque"}', flag, expression) == 0
    captured = capsys.readouterr()
    assert captured.err == '' and loads(captured.out.encode()) == 'next-page'
    assert calls[0][:2] == ('discovery.schema', {'operation': 'discovery.read_query@3'})
    assert calls[1][0:2] == ('discovery.read_query', {'cursor': 'opaque'})
    assert calls[1][2]['contract_version'] == 3
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('operation', 'arguments', 'options'),
    [
        ('content.post_create', '{"cursor":"opaque"}', []),
        ('discovery.get', '{"cursor":"opaque"}', []),
        ('discovery.read_query', '{"cursor":"opaque","fields":["id"]}', []),
        ('discovery.read_query', '{"cursor":"opaque","limit":10}', []),
        ('discovery.read_query', '{"cursor":""}', []),
        ('discovery.read_query', '{"cursor":null}', []),
        ('discovery.read_query', '{"cursor":0}', []),
        ('discovery.read_query', '{"cursor":"opaque"}', ['--return-field', 'status']),
    ],
)
async def test_cursor_format_rejects_other_operations_or_conflicts_before_network(
    cursor_cli, operation, arguments, options
):
    invoke, calls = cursor_cli
    with pytest.raises(
        Failure, match='cursor_output_read_required|cursor_query_mismatch|json_query_conflict'
    ):
        await invoke(operation, arguments, '--template', '{{/data}}', *options)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'spec',
    [
        {'name': 'discovery.get', 'version': 3, 'effect': 'read'},
        {'name': 'discovery.read_query', 'version': 1, 'effect': 'read'},
        {'name': 'discovery.read_query', 'version': 3, 'effect': 'transaction'},
    ],
)
async def test_cursor_output_refuses_changed_contract_identity_or_effect(spec):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from msg.client_output import cursor_output

    client = SimpleNamespace(
        call=AsyncMock(
            return_value=SimpleNamespace(
                status='ok',
                data={'operation': spec},
            )
        )
    )
    with pytest.raises(Failure, match='invalid_read_response|json_read_required'):
        await cursor_output(client, 'discovery.read_query', 3, {'cursor': 'opaque'})
    client.call.assert_awaited_once_with(
        'discovery.schema', {'operation': 'discovery.read_query@3'}
    )


@pytest.mark.asyncio
async def test_cursor_output_preserves_unsuccessful_schema_result():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from msg.client_output import cursor_output

    error = SimpleNamespace(status='error')
    client = SimpleNamespace(call=AsyncMock(return_value=error))
    assert await cursor_output(client, 'discovery.read_query', 3, {'cursor': 'opaque'}) is error
    client.call.assert_awaited_once_with(
        'discovery.schema', {'operation': 'discovery.read_query@3'}
    )
