"""工具发现去重不改契约、调用版本或执行入口。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from msg.application import Application
from msg.config import write_example
from msg.core.codec import digest, wire
from msg.core.cursors import CursorCodec
from msg.core.errors import Failure
from msg.core.models import OperationResult
from msg.core.requests import request_for
from msg.transports.mcp import MCPServer
from msg.transports.mcp_tools import DEPENDENCIES, PUBLIC, REPLY_ALTERNATIVES, tool_catalog


@pytest.fixture(scope='module')
def catalog_service(tmp_path_factory):
    root = tmp_path_factory.mktemp('mcp-catalog')
    app = Application(write_example(root / 'etc', root / 'data', 'http://testserver'))
    return SimpleNamespace(
        registry=app.registry,
        cursors=CursorCodec(b'catalog-test-key'),
        settings=app.settings,
    )


async def _tools(server):
    tools, cursor, cursors = [], None, set()
    while True:
        page = await server.tools(cursor)
        assert len(page['tools']) <= 8
        tools.extend(page['tools'])
        cursor = page.get('nextCursor')
        if cursor is None:
            return tools
        assert cursor not in cursors
        cursors.add(cursor)


def _registry(service, specs):
    return SimpleNamespace(
        operations=lambda entry: tuple(spec for spec in specs if entry in spec.entries),
        catalog=lambda: {
            'digest': digest([
                (spec.name, spec.version, spec.enabled, sorted(spec.entries)) for spec in specs
            ])
        },
        schema=service.registry.schema,
        load_schemas=AsyncMock(),
    )


async def test_real_catalog_lists_each_latest_contract_once_without_changing_registry(
    catalog_service,
):
    registry = catalog_service.registry
    before = registry.catalog()
    tools = await _tools(MCPServer(catalog_service))
    versions = {}
    for spec in registry.operations('network'):
        if spec.enabled:
            versions.setdefault(spec.name, []).append(spec.version)
    names = [tool['name'].partition('@')[0] for tool in tools]
    assert names == sorted(versions)
    assert len(names) == len(set(names))
    for tool in tools:
        name = tool['name'].partition('@')[0]
        version = max(versions[name])
        assert tool['name'] == (name if version == 1 else f'{name}@{version}')
        schema = tool['inputSchema']['properties']['packet']['properties']
        assert schema['operation'] == {'const': name}
        assert schema['contract_version'] == {'const': version}
        assert schema['arguments'] == registry.schema(
            registry.operation(name, version).input_schema
        )
    assert registry.catalog() == before
    assert registry.operation('content.post_create', 1).version == 1
    assert registry.operation('content.post_create', 2).version == 2
    assert registry.operation('discovery.read_query', 1).version == 1
    assert registry.operation('discovery.read_query', 5).version == 5
    assert not any(name.startswith('root.') for name in names)


async def test_unordered_versions_disabled_contracts_and_local_entries(catalog_service):
    spec = catalog_service.registry.operation('discovery.get')
    registry = _registry(
        catalog_service,
        (
            replace(spec, name='test.read', version=4),
            replace(spec, name='test.read', version=2),
            replace(spec, name='test.read', version=99, enabled=False),
            replace(spec, name='test.read', version=100, entries=frozenset({'local_admin'})),
            replace(spec, name='test.hidden', enabled=False),
            replace(spec, name='test.single'),
        ),
    )
    server = MCPServer(SimpleNamespace(registry=registry, cursors=catalog_service.cursors))
    assert [tool['name'] for tool in await _tools(server)] == ['test.read@4', 'test.single']


async def test_local_schemas_are_loaded_only_for_selected_page(catalog_service):
    spec = catalog_service.registry.operation('discovery.get')
    specs = tuple(
        replace(spec, name=f'test.read{i:02}', version=version)
        for i in reversed(range(17))
        for version in (2, 1, 3)
    )
    registry = _registry(catalog_service, specs)
    client = SimpleNamespace()
    server = MCPServer(
        SimpleNamespace(registry=registry, cursors=catalog_service.cursors), local_client=client
    )
    tools = await _tools(server)
    assert [tool['name'] for tool in tools] == [f'test.read{i:02}@3' for i in range(17)]
    pages = [call.args[0] for call in registry.load_schemas.await_args_list]
    assert [len(page) for page in pages] == [8, 8, 1]
    assert all(spec.version == 3 for page in pages for spec in page)
    assert all(tool['inputSchema'] == registry.schema(specs[0].input_schema) for tool in tools)
    assert all('packet' not in tool['inputSchema']['properties'] for tool in tools)


async def test_old_catalog_cursor_requires_restart(catalog_service):
    old_cursor = catalog_service.cursors.encode(
        'mcp-tools', catalog_service.registry.catalog()['digest'], 8
    )
    response = await MCPServer(catalog_service).handle({
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/list',
        'params': {'cursor': old_cursor},
    })
    assert response['error']['message'] == 'cursor_query_mismatch'


async def test_registry_change_invalidates_page_cursor(catalog_service):
    spec = catalog_service.registry.operation('discovery.get')
    specs = tuple(replace(spec, name=f'test.read{i:02}') for i in range(9))
    service = SimpleNamespace(
        registry=_registry(catalog_service, specs), cursors=catalog_service.cursors
    )
    server = MCPServer(service)
    first = await server.tools()
    service.registry = _registry(catalog_service, specs + (replace(spec, name='test.extra'),))
    with pytest.raises(Failure, match='cursor_query_mismatch'):
        await server.tools(first['nextCursor'])


@pytest.mark.parametrize('offset', [-1, True, '8', 999999])
async def test_cursor_offset_is_a_bounded_integer(catalog_service, offset):
    first = await MCPServer(catalog_service).tools()
    context = catalog_service.cursors.inspect(first['nextCursor'])['query']
    cursor = catalog_service.cursors.encode('mcp-tools', context, offset)
    with pytest.raises(Failure, match='invalid_cursor'):
        await MCPServer(catalog_service).tools(cursor)


@pytest.mark.parametrize(
    'name,version',
    [('content.post_create', 1), ('content.post_create@1', 1), ('content.post_create@2', 2)],
)
async def test_explicit_old_calls_keep_the_requested_version(catalog_service, name, version):
    packet = request_for(
        'content.post_create',
        {'parent': '/main', 'body': 'compatibility'},
        catalog_service.settings.service_url,
        contract_version=version,
    )
    output = OperationResult(
        request_id=packet.request_id,
        operation=packet.operation,
        status='ok',
        actor=None,
        subject=None,
    )
    executor = SimpleNamespace(execute=AsyncMock(return_value=output))
    service = SimpleNamespace(**vars(catalog_service), executor=executor)
    response = await MCPServer(service).handle({
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {'name': name, 'arguments': {'packet': wire(packet)}},
    })
    assert response['result']['structuredContent']['status'] == 'ok'
    forwarded = executor.execute.await_args
    assert forwarded.args[0] == packet
    assert forwarded.kwargs == {'entry': 'network'}


async def test_network_entry_stays_required_for_explicit_calls(catalog_service):
    spec = replace(
        catalog_service.registry.operation('discovery.get'),
        name='test.local',
        entries=frozenset({'local_admin'}),
    )
    service = SimpleNamespace(**{
        **vars(catalog_service),
        'registry': SimpleNamespace(operation=lambda name: spec),
    })
    response = await MCPServer(service).handle({
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {'name': 'test.local', 'arguments': {}},
    })
    assert response['error']['message'] == 'entry_not_allowed'


def test_default_catalog_remains_small_and_separate_from_raw_versions(catalog_service):
    registry = catalog_service.registry
    assert {tool['name'] for tool in tool_catalog(registry)} == PUBLIC
    identity = SimpleNamespace(
        operations=frozenset().union(*DEPENDENCIES.values(), *REPLY_ALTERNATIVES)
    )
    authorized = tool_catalog(registry, identity)
    assert len(authorized) == 13
    assert all(tool['name'].startswith('msg_') for tool in authorized)
    assert all('@' not in tool['name'] for tool in authorized)
