"""Pure adapter tests; real authorization/transactions remain executor tests."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from msg.application import Application
from msg.config import write_example
from msg.core.errors import Failure
from msg.core.models import OperationError, OperationResult, ResourceRef
from msg.transports.mcp_tools import (
    DEPENDENCIES,
    PAGE_FIELDS,
    PUBLIC,
    REPLY_ALTERNATIVES,
    SCHEMAS,
    run_tool,
    tool_catalog,
)

ALL = frozenset().union(*DEPENDENCIES.values(), *REPLY_ALTERNATIVES)
FIXED = {'id': 'r_post', 'revision': 'v_read'}
SECRET = 'do-not-show-token-or-exception'


@pytest.fixture(scope='module')
def registry(tmp_path_factory):
    root = tmp_path_factory.mktemp('mcp-contracts')
    settings = write_example(root / 'etc', root / 'data', 'http://testserver')
    return Application(settings).registry


class CatalogRegistry:
    def __init__(self, registry):
        self.registry = registry

    def operations(self, entry):
        specs = list(self.registry.operations(entry))
        # The new versions are implemented/tested separately by the DM worker;
        # their business input schema retains these registered parameters.
        specs.append(replace(self.registry.operation('communication.dm_send'), version=2))
        specs.append(
            replace(self.registry.operation('discovery.get'), name='communication.conversation_get')
        )
        specs.extend(
            replace(self.registry.operation(op, old), version=new)
            for op, old, new in (
                ('communication.dm_request', 2, 3),
                ('communication.dm_accept', 1, 2),
                ('communication.dm_reject', 1, 2),
            )
        )
        return specs


def identity(operations=ALL, **kwargs):
    return SimpleNamespace(
        subject='s_current',
        credential_id=SECRET,
        token=SECRET,
        operations=operations,
        credential_type='oauth_token',
        expires_at=kwargs.get('expires_at'),
        session_binding=SECRET,
    )


def okay(operation, data=None, resources=()):
    return OperationResult(
        request_id=SECRET,
        operation=operation,
        status='ok',
        actor='s_current',
        subject='s_current',
        data=data or {},
        resources=resources,
    )


class Call:
    def __init__(self, registry, handler):
        self.registry, self.handler, self.calls = registry, handler, []

    async def __call__(self, operation, arguments, *, contract_version=1):
        self.calls.append((operation, arguments, contract_version))
        if operation == 'communication.conversation_get':
            assert set(arguments) == {'id'}
        else:
            version = contract_version
            if (
                operation
                in {'communication.dm_send', 'communication.dm_accept', 'communication.dm_reject'}
                and version == 2
            ):
                version = 1
            if operation == 'communication.dm_request' and version == 3:
                version = 2
            spec = self.registry.operation(operation, version)
            if operation == 'communication.dm_request' and contract_version == 3:
                schema = self.registry.schema(spec.input_schema)
                schema['properties']['introduction']['maxLength'] = 32768
                Draft202012Validator(schema).validate(arguments)
            else:
                self.registry.validate(spec.input_schema, arguments)
        return self.handler(operation, arguments, contract_version)


def _property_names(schema):
    if isinstance(schema, dict):
        yield from schema.get('properties', {})
        for item in schema.values():
            yield from _property_names(item)
    elif isinstance(schema, list):
        for item in schema:
            yield from _property_names(item)


def test_default_schemas_hide_envelopes_and_credentials(registry):
    forbidden = {
        'packet',
        'proof',
        'signature',
        'credential_id',
        'token',
        'key_id',
        'digest',
        'request_id',
        'expiry',
        'expires_at',
        'subject',
        'subject_id',
        'actor',
        'contract_version',
        'expected_generations',
    }
    anonymous = tool_catalog(CatalogRegistry(registry))
    full = tool_catalog(CatalogRegistry(registry), identity())
    assert {tool['name'] for tool in anonymous} == PUBLIC
    assert len(full) == 13
    for tool in full:
        Draft202012Validator.check_schema(tool['inputSchema'])
        assert not forbidden.intersection(_property_names(tool['inputSchema']))
        assert tool['inputSchema']['additionalProperties'] is False
        assert tool['securitySchemes'] == tool['_meta']['securitySchemes']
        assert tool['securitySchemes'][0]['type'] == (
            'noauth' if tool['name'] in PUBLIC else 'oauth2'
        )
        assert all(
            scope in {'msg.mcp.read', 'msg.mcp.message', 'msg.mcp.post'}
            for scheme in tool['securitySchemes']
            for scope in scheme.get('scopes', ())
        )
    anonymous[0]['inputSchema']['properties']['token'] = {}
    assert 'token' not in tool_catalog(CatalogRegistry(registry))[0]['inputSchema']['properties']


def test_catalog_uses_exact_installed_versions_and_ceilings(registry):
    assert 'msg_send' not in {item['name'] for item in tool_catalog(registry, identity())}
    read_identity = identity(
        frozenset().union(
            *(DEPENDENCIES[name] for name in PUBLIC),
            DEPENDENCIES['msg_inbox'],
            DEPENDENCIES['msg_notifications'],
        )
    )
    read = {item['name'] for item in tool_catalog(CatalogRegistry(registry), read_identity)}
    assert read == PUBLIC | {'msg_inbox', 'msg_notifications'}
    message = tool_catalog(
        CatalogRegistry(registry), identity(DEPENDENCIES['msg_send'] | REPLY_ALTERNATIVES[0])
    )
    reply = next(tool for tool in message if tool['name'] == 'msg_reply')
    assert reply['securitySchemes'] == [
        {'type': 'oauth2', 'scopes': ['msg.mcp.read', 'msg.mcp.message']}
    ]
    assert 'msg_post' not in {tool['name'] for tool in message}


@pytest.mark.parametrize('name', SCHEMAS)
async def test_unknown_secret_fields_are_rejected_before_callback(name):
    calls = []

    async def call(*args, **kwargs):
        calls.append(args)
        raise AssertionError('must not be called')

    result = await run_tool(name, {'packet': SECRET}, call=call, identity=identity())
    assert result['error']['code'] == 'invalid_tool_arguments'
    assert not calls and SECRET not in json.dumps(result)


async def test_me_returns_no_identity_secret(registry):
    call = Call(
        registry,
        lambda op, args, version: okay(
            op, {'id': 's_current', 'type': 'user', 'name': 'lightjunction', 'token': SECRET}
        ),
    )
    result = await run_tool('msg_me', {}, call=call, identity=identity())
    assert result['data']['authenticated'] is True
    assert result['data']['subject'] == 's_current'
    assert result['data']['handle'] == '@lightjunction'
    assert result['data']['capabilities'] == [
        'read_public',
        'read_private',
        'send_dm',
        'reply',
        'post',
        'ack',
        'decide_dm',
    ]
    assert result['data']['credential_type'] == 'oauth_token'
    assert SECRET not in json.dumps(result)
    assert call.calls == [
        ('discovery.get', {'id': 's_current', 'fields': ['id', 'name', 'type']}, 1)
    ]
    anon = await run_tool('msg_me', {}, call=call)
    assert anon['data']['authenticated'] is False


async def test_authentication_and_ceiling_fail_before_writes(registry):
    call = Call(registry, lambda *args: pytest.fail('unexpected callback'))
    args = {'recipient': '@friend', 'body': 'hello'}
    assert (await run_tool('msg_send', args, call=call))['error'][
        'code'
    ] == 'authentication_required'
    assert (await run_tool('msg_send', args, call=call, identity=identity(frozenset())))['error'][
        'code'
    ] == 'credential_ceiling'
    assert not call.calls


async def test_resolve_and_read_are_bounded_and_never_ack(registry):
    def handler(op, args, version):
        if op == 'discovery.resolve':
            return okay(
                op,
                {
                    'ref': FIXED,
                    'path': '/topic/one.md',
                    'stable_path': '/_r/r_post',
                    'token': SECRET,
                },
            )
        if op == 'discovery.get':
            assert args['fields'] in (PAGE_FIELDS, PAGE_FIELDS + ['media_type', 'size'])
            return okay(
                op,
                {
                    **FIXED,
                    'name': 'One',
                    'type': 'post',
                    'path': '/topic/one.md',
                    'media_type': 'text/markdown',
                    'size': 999,
                    'content': SECRET,
                },
            )
        assert op == 'discovery.read_segment'
        assert args == {**FIXED, 'max_bytes': 32}
        return okay(op, {**FIXED, 'text': '你' * 40, 'next': '/_r/c/opaque-next'})

    call = Call(registry, handler)
    resolved = await run_tool('msg_resolve', {'address': '/topic/one.md'}, call=call)
    assert resolved['data']['ref'] == FIXED
    result = await run_tool(
        'msg_read', {'address': '/topic/one.md', 'max_chars': 32, 'max_bytes': 64}, call=call
    )
    assert result['data']['text'] == '你' * 10
    assert len(result['data']['text'].encode()) <= 32
    assert result['data']['truncated'] is True
    assert result['data']['cursor'] == 'opaque-next'
    assert SECRET not in json.dumps(result)
    assert all(not op.startswith('discussion.') for op, _, _ in call.calls)


async def test_read_cursor_only_and_metadata_only_resource(registry):
    def handler(op, args, version):
        assert op == 'discovery.get'
        assert args['fields'] == PAGE_FIELDS and 'view' not in args
        return okay(
            op,
            {
                'id': 'r_topic',
                'revision': None,
                'type': 'topic',
                'name': 'Main',
                'conversation': {'messages': [SECRET]},
            },
        )

    call = Call(registry, handler)
    result = await run_tool('msg_read', {'ref': {'id': 'r_topic'}}, call=call)
    assert result['data']['text'] == '' and result['data']['truncated'] is False
    assert SECRET not in json.dumps(result)
    invalid = await run_tool('msg_read', {'cursor': 'opaque', 'max_bytes': 100}, call=call)
    assert invalid['error']['code'] == 'cursor_query_mismatch'
    assert len(call.calls) == 1


@pytest.mark.parametrize(
    'name,operation,version',
    [('msg_list', 'discovery.read_query', 3), ('msg_search', 'discovery.lexical_search', 5)],
)
async def test_list_search_fixed_contracts_and_cursor_only(registry, name, operation, version):
    def handler(op, args, v):
        if op == 'discovery.resolve':
            return okay(op, {'ref': {'id': 'r_scope'}})
        assert op == operation and v == version
        assert args == {'cursor': 'opaque'} or args['fields'] == PAGE_FIELDS
        return okay(
            op,
            {
                'items': [
                    {**FIXED, 'name': 'One', 'snippet': 's' * 900, 'body': SECRET, 'proof': SECRET}
                ],
                'cursor': 'next',
            },
        )

    call = Call(registry, handler)
    args = (
        {'address': '/topic', 'limit': 2}
        if name == 'msg_list'
        else {'scope': '/topic', 'query': 'hello', 'limit': 2}
    )
    result = await run_tool(name, args, call=call)
    assert result['status'] == 'ok'
    assert result['data']['cursor'] == 'next'
    assert SECRET not in json.dumps(result)
    if name == 'msg_search':
        assert len(result['data']['items'][0]['snippet']) == 280
    continuation = await run_tool(name, {'cursor': 'opaque'}, call=call)
    assert continuation['status'] == 'ok'
    assert call.calls[-1] == (operation, {'cursor': 'opaque'}, version)
    conflict = await run_tool(name, {'cursor': 'opaque', 'limit': 2}, call=call)
    assert conflict['error']['code'] == 'cursor_query_mismatch'


async def test_inbox_notifications_only_refs_and_small_previews(registry):
    def handler(op, args, version):
        if op == 'communication.inbox':
            return okay(
                op,
                {
                    'items': [
                        {
                            'id': 'm_delivery',
                            'resource': FIXED,
                            'sender_contact': {
                                'name': 'Friend',
                                'path': '/@friend',
                                'token': SECRET,
                            },
                            'recipient_contact': {
                                'name': 'Current',
                                'path': '/@current',
                                'proof': SECRET,
                            },
                            'time': '2026-10-05T00:00:00Z',
                            'body': SECRET,
                            'preview': {
                                'title': 'Readable',
                                'excerpt': 'x' * 900,
                                'body': SECRET,
                                'author': {'name': 'Friend', 'path': '/@friend', 'token': SECRET},
                            },
                        }
                    ],
                    'cursor': 'next',
                },
            )
        assert op == 'communication.changes'
        return okay(
            op,
            {
                'items': [
                    {
                        'type': 'resource.created',
                        'resources': [FIXED],
                        'data': {'token': SECRET},
                        'signed_envelope': SECRET,
                    }
                ],
                'sync_cursor': 'next',
                'has_more': True,
            },
        )

    call = Call(registry, handler)
    inbox = await run_tool('msg_inbox', {}, call=call, identity=identity())
    item = inbox['data']['items'][0]
    assert item['ref'] == FIXED
    assert len(item['preview']['excerpt']) == 280
    assert item['sender_contact'] == {'name': 'Friend', 'path': '/@friend'}
    assert item['recipient_contact'] == {'name': 'Current', 'path': '/@current'}
    assert item['time'] == '2026-10-05T00:00:00Z'
    assert item['preview']['author'] == {'name': 'Friend', 'path': '/@friend'}
    notes = await run_tool('msg_notifications', {}, call=call, identity=identity())
    assert notes['data']['items'][0]['refs'] == [FIXED]
    assert notes['data']['has_more'] is True
    assert SECRET not in json.dumps([inbox, notes])
    assert all(op.startswith('communication.') for op, _, _ in call.calls)


async def test_send_resolves_current_handle_each_time_and_uses_dm_v2(registry):
    selected = ['s_first']

    def handler(op, args, version):
        if op == 'discovery.resolve':
            assert args == {'address': '/@friend'}
            return okay(op, {'ref': {'id': selected[0]}})
        if op == 'discovery.get':
            return okay(op, {'id': selected[0], 'type': 'user'})
        if op == 'communication.dm_request':
            assert version == 3
            assert args == {'recipient': selected[0], 'introduction': 'hello'}
            return okay(op, {'conversation_id': 'r_' + selected[0], 'state': 'active'})
        assert op == 'communication.dm_send' and version == 2
        assert args == {'conversation_id': 'r_' + selected[0], 'body': 'hello'}
        return okay(
            op,
            {'conversation_id': args['conversation_id'], 'token': SECRET},
            (ResourceRef(**FIXED),),
        )

    call = Call(registry, handler)
    first = await run_tool(
        'msg_send', {'recipient': 'friend', 'body': 'hello'}, call=call, identity=identity()
    )
    selected[0] = 's_second'
    second = await run_tool(
        'msg_send', {'recipient': 'friend', 'body': 'hello'}, call=call, identity=identity()
    )
    assert first['data']['conversation_id'] != second['data']['conversation_id']
    assert first['data']['refs'] == [FIXED] and first['data']['private'] is True
    assert SECRET not in json.dumps([first, second])
    assert len([c for c in call.calls if c[0] == 'discovery.resolve']) == 2


async def test_send_existing_pending_does_not_add_or_send_the_message(registry):
    def handler(op, args, version):
        if op == 'discovery.resolve':
            return okay(op, {'ref': {'id': 's_friend'}})
        if op == 'discovery.get':
            return okay(op, {'id': 's_friend', 'type': 'user'})
        assert op == 'communication.dm_request' and version == 3
        return okay(op, {'conversation_id': 'r_dm', 'state': 'pending'})

    call = Call(registry, handler)
    result = await run_tool(
        'msg_send', {'recipient': '@friend', 'body': 'hello'}, call=call, identity=identity()
    )
    assert result['data']['state'] == 'pending' and result['data']['sent'] is False
    assert (
        result['data']['introduction_added'] is False and 'introduction_ref' not in result['data']
    )
    assert 'message was not added' in result['data']['hint']
    assert all(
        op not in {'communication.dm_list', 'communication.dm_send', 'communication.dm_accept'}
        for op, _, _ in call.calls
    )


@pytest.mark.parametrize('code', ['dm_rejected', 'dm_blocked'])
async def test_send_rejection_and_block_preserve_fixed_plan_and_safe_errors(registry, code):
    def handler(op, args, version):
        if op == 'discovery.resolve':
            return okay(op, {'ref': {'id': 's_friend'}})
        if op == 'discovery.get':
            return okay(op, {'id': 's_friend', 'type': 'user'})
        assert op == 'communication.dm_request' and version == 3
        return OperationResult(
            request_id=SECRET,
            operation=op,
            status='error',
            actor=None,
            subject=None,
            error=OperationError(code=code, retryable=False, message=SECRET),
        )

    call = Call(registry, handler)
    result = await run_tool(
        'msg_send', {'recipient': '@friend', 'body': 'hello'}, call=call, identity=identity()
    )
    assert result['error']['code'] == code and SECRET not in json.dumps(result)
    assert [op for op, _, _ in call.calls] == [
        'discovery.resolve',
        'discovery.get',
        'communication.dm_request',
    ]


def reply_handler(kind, write=None, fail=None):
    def handler(op, args, version):
        if op == 'discovery.get':
            if fail:
                return OperationResult(
                    request_id=SECRET,
                    operation=op,
                    status='error',
                    actor=None,
                    subject=None,
                    error=OperationError(code=fail, retryable=False, message=SECRET),
                )
            return okay(op, {**FIXED, 'type': 'post', 'parent': 'r_topic'})
        if op == 'communication.conversation_get':
            return okay(
                op,
                {
                    'topic': {'id': 'r_topic'},
                    'kind': kind,
                    'state': 'active',
                    'other_subject': 's_friend',
                },
            )
        assert op == write
        if op == 'communication.dm_send':
            assert args == {'conversation_id': 'r_topic', 'body': 'hello'} and version == 2
        else:
            assert args == {'target': FIXED, 'body': 'hello'} and version == 2
        return okay(op, {'credential_id': SECRET}, (ResourceRef(**FIXED),))

    return handler


@pytest.mark.parametrize(
    'field,kind,write',
    [
        ('message_ref', 'direct', 'communication.dm_send'),
        ('post_ref', 'discussion', 'discussion.reply'),
    ],
)
async def test_reply_maps_private_and_discussion_by_current_kind(registry, field, kind, write):
    call = Call(registry, reply_handler(kind, write))
    result = await run_tool(
        'msg_reply', {field: FIXED, 'body': 'hello'}, call=call, identity=identity()
    )
    assert result['status'] == 'ok'
    assert result['data']['refs'] == [FIXED]
    assert SECRET not in json.dumps(result)
    assert call.calls[-1][0] == write
    assert bool(result['data'].get('private')) == (kind == 'direct')


@pytest.mark.parametrize(
    'field,kind,error',
    [
        ('post_ref', 'direct', 'dm_reference_private'),
        ('message_ref', 'discussion', 'not_a_direct_message'),
        ('post_ref', None, 'conversation_type_unavailable'),
    ],
)
async def test_reply_fail_closed_for_archived_dm_and_unknown_kind(registry, field, kind, error):
    call = Call(registry, reply_handler(kind))
    result = await run_tool(
        'msg_reply', {field: FIXED, 'body': 'hello'}, call=call, identity=identity()
    )
    assert result['error']['code'] == error
    assert not any(op in {'discussion.reply', 'communication.dm_send'} for op, _, _ in call.calls)


async def test_unauthorized_ref_and_wrong_reply_branch_cannot_write(registry):
    call = Call(registry, reply_handler('discussion', fail='permission_denied'))
    result = await run_tool(
        'msg_reply', {'post_ref': FIXED, 'body': 'hello'}, call=call, identity=identity()
    )
    assert result['error']['code'] == 'permission_denied' and SECRET not in json.dumps(result)
    wrong_branch = Call(registry, reply_handler('discussion', 'discussion.reply'))
    result = await run_tool(
        'msg_reply',
        {'post_ref': FIXED, 'body': 'hello'},
        call=wrong_branch,
        identity=identity(REPLY_ALTERNATIVES[0]),
    )
    assert result['error']['code'] == 'credential_ceiling'
    assert all(op != 'discussion.reply' for op, _, _ in wrong_branch.calls)


async def test_post_is_only_discussion_with_explicit_summary(registry):
    def handler(op, args, version):
        if op == 'discovery.resolve':
            return okay(op, {'ref': {'id': 'r_topic'}})
        if op == 'communication.conversation_get':
            return okay(op, {'topic': {'id': 'r_topic'}, 'kind': 'discussion'})
        assert op == 'content.post_create' and version == 2
        assert args == {'parent': 'r_topic', 'body': '# Title\n\nhello', 'summary': 'short'}
        return okay(op, {'body': SECRET}, (ResourceRef(**FIXED),))

    call = Call(registry, handler)
    result = await run_tool(
        'msg_post',
        {'topic': '/main/topic', 'title': 'Title', 'body': 'hello', 'summary': 'short'},
        call=call,
        identity=identity(),
    )
    assert result['data']['refs'] == [FIXED]
    assert SECRET not in json.dumps(result)


async def test_ack_fixed_revision_fetches_digest_only_then_explicit_ack(registry):
    def handler(op, args, version):
        if op == 'discovery.get':
            assert args == {**FIXED, 'fields': ['id', 'revision', 'digest']}
            return okay(op, {**FIXED, 'digest': 'sha256:fixed', 'signature': SECRET})
        assert op == 'discussion.ack'
        assert args == {'target': FIXED, 'digest': 'sha256:fixed'}
        return okay(
            op,
            {
                'auth': 'token',
                'acknowledged_revision': FIXED['revision'],
                'signed_envelope': SECRET,
            },
        )

    call = Call(registry, handler)
    result = await run_tool('msg_ack', {'ref': FIXED}, call=call, identity=identity())
    assert result == {'status': 'ok', 'data': {'ref': FIXED, 'acknowledged': True}}
    assert len(call.calls) == 2
    unsigned = await run_tool('msg_ack', {'ref': {'id': 'r_post'}}, call=call, identity=identity())
    assert unsigned['error']['code'] == 'invalid_tool_arguments' and len(call.calls) == 2


async def test_ack_rejects_wrong_revision_and_body_utf8_budget(registry):
    call = Call(
        registry,
        lambda op, args, v: okay(op, {'id': 'r_other', 'revision': 'v_other', 'digest': SECRET}),
    )
    result = await run_tool('msg_ack', {'ref': FIXED}, call=call, identity=identity())
    assert result['error']['code'] == 'revision_mismatch'
    assert len(call.calls) == 1
    oversized = await run_tool(
        'msg_send', {'recipient': '@friend', 'body': '你' * 12000}, call=call, identity=identity()
    )
    assert oversized['error']['code'] == 'body_too_large' and len(call.calls) == 1


@pytest.mark.parametrize(
    'failure',
    [
        RuntimeError(SECRET),
        Failure('credential_ceiling', details={'token': SECRET}),
        Failure(SECRET),
    ],
)
async def test_errors_never_echo_exception_or_private_details(registry, failure):
    async def call(*args, **kwargs):
        raise failure

    result = await run_tool('msg_resolve', {'address': '/main'}, call=call)
    assert result['status'] == 'error' and SECRET not in json.dumps(result)
    assert set(result['error']) <= {'code', 'message', 'hint', 'retryable'}


async def test_connect_requests_all_limited_scopes_and_returns_host_challenge(registry):
    catalog = tool_catalog(CatalogRegistry(registry))
    connect = next(tool for tool in catalog if tool['name'] == 'msg_connect')
    assert connect['securitySchemes'][1]['scopes'] == [
        'msg.mcp.read',
        'msg.mcp.message',
        'msg.mcp.post',
    ]

    async def call(*args, **kwargs):
        pytest.fail('connect is handled by the host, not a business operation')

    result = await run_tool('msg_connect', {}, call=call)
    assert result['error']['code'] == 'authentication_required'


async def test_send_new_conversation_keeps_full_intro_and_returns_pending(registry):
    def handler(op, args, version):
        if op == 'discovery.resolve':
            return okay(op, {'ref': {'id': 's_friend'}})
        if op == 'discovery.get':
            return okay(op, {'id': 's_friend', 'type': 'user'})
        assert op == 'communication.dm_request' and version == 3
        assert args == {'recipient': 's_friend', 'introduction': '完整介绍'}
        return okay(
            op,
            {
                'conversation_id': 'r_new',
                'state': 'pending',
                'introduction_ref': FIXED,
                'participant_pair': ['s_current', 's_friend'],
                'token': SECRET,
            },
            (ResourceRef(id='r_new'),),
        )

    call = Call(registry, handler)
    result = await run_tool(
        'msg_send', {'recipient': '@friend', 'body': '完整介绍'}, call=call, identity=identity()
    )
    assert result['data']['state'] == 'pending'
    assert result['data']['sent'] is False and result['data']['needs_acceptance'] is True
    assert result['data']['conversation_id'] == 'r_new'
    assert (
        result['data']['introduction_ref'] == FIXED and result['data']['introduction_added'] is True
    )
    assert SECRET not in json.dumps(result)
    assert all(
        op not in {'communication.dm_send', 'communication.dm_accept'} for op, _, _ in call.calls
    )


@pytest.mark.parametrize('body', ['x' * 501, '你' * 342])
async def test_new_dm_introduction_budget_never_truncates_or_sends(registry, body):
    def handler(op, args, version):
        if op == 'discovery.resolve':
            return okay(op, {'ref': {'id': 's_friend'}})
        if op == 'discovery.get':
            return okay(op, {'id': 's_friend', 'type': 'user'})
        assert op == 'communication.dm_request' and version == 3
        assert args == {'recipient': 's_friend', 'introduction': body}
        return OperationResult(
            request_id=SECRET,
            operation=op,
            status='error',
            actor=None,
            subject=None,
            error=OperationError(code='introduction_too_large', retryable=False, message=SECRET),
        )

    call = Call(registry, handler)
    result = await run_tool(
        'msg_send', {'recipient': '@friend', 'body': body}, call=call, identity=identity()
    )
    assert result['error']['code'] == 'dm_introduction_too_large'
    assert all(
        op not in {'communication.dm_list', 'communication.dm_send'} for op, _, _ in call.calls
    )


@pytest.mark.parametrize(
    'accept,operation,state',
    [(True, 'communication.dm_accept', 'active'), (False, 'communication.dm_reject', 'rejected')],
)
async def test_dm_decision_requires_explicit_choice_and_direct_topic(
    registry, accept, operation, state
):
    def handler(op, args, version):
        if op == 'communication.conversation_get':
            return okay(op, {'topic': {'id': 'r_topic'}, 'kind': 'direct', 'state': 'pending'})
        assert op == operation and version == 2
        assert args == {'conversation_id': 'r_topic'}
        return okay(op, {'conversation_id': 'r_topic', 'state': state, 'token': SECRET})

    call = Call(registry, handler)
    result = await run_tool(
        'msg_dm_decide',
        {'ref': {'id': 'r_topic'}, 'accept': accept},
        call=call,
        identity=identity(),
    )
    assert result == {
        'status': 'ok',
        'data': {'ref': {'id': 'r_topic'}, 'state': state, 'private': True},
    }
    assert SECRET not in json.dumps(result)
    missing = await run_tool(
        'msg_dm_decide', {'ref': {'id': 'r_topic'}}, call=call, identity=identity()
    )
    assert missing['error']['code'] == 'invalid_tool_arguments' and len(call.calls) == 2


async def test_dm_decision_cannot_accept_a_discussion_topic(registry):
    call = Call(
        registry,
        lambda op, args, version: okay(op, {'topic': {'id': 'r_topic'}, 'kind': 'discussion'}),
    )
    result = await run_tool(
        'msg_dm_decide', {'ref': {'id': 'r_topic'}, 'accept': True}, call=call, identity=identity()
    )
    assert result['error']['code'] == 'not_a_direct_message'
    assert len(call.calls) == 1


async def test_expired_executor_credential_has_safe_reconnect_hint(registry):
    call = Call(
        registry,
        lambda op, args, version: OperationResult(
            request_id=SECRET,
            operation=op,
            status='error',
            actor=None,
            subject=None,
            error=OperationError(code='credential_expired', retryable=False, message=SECRET),
        ),
    )
    result = await run_tool('msg_resolve', {'address': '/main'}, call=call, identity=identity())
    assert result['error']['code'] == 'credential_expired'
    assert 'Reconnect' in result['error']['hint'] and SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    'code', ['jsonrpc_id_conflict', 'idempotency_conflict', 'payload_digest_mismatch']
)
async def test_conflict_and_digest_errors_explain_safe_repair(registry, code):
    call = Call(
        registry,
        lambda op, args, version: OperationResult(
            request_id=SECRET,
            operation=op,
            status='error',
            actor=None,
            subject=None,
            error=OperationError(code=code, retryable=False, message=SECRET),
        ),
    )
    result = await run_tool('msg_resolve', {'address': '/main'}, call=call)
    assert result['error']['code'] == code and result['error']['hint']
    assert SECRET not in json.dumps(result)


async def test_historical_ref_and_unicode_segments_keep_fixed_revision(registry):
    full = '你喜欢读旧版。' * 8
    windows = [full[index : index + 10] for index in range(0, len(full), 10)]

    def handler(op, args, version):
        if op == 'discovery.get':
            assert args['revision'] == FIXED['revision'] and 'view' not in args
            return okay(
                op,
                {
                    **FIXED,
                    'type': 'post',
                    'name': 'Current label',
                    'media_type': 'text/markdown',
                    'size': len(full.encode()),
                },
            )
        assert op == 'discovery.read_segment'
        if args.get('cursor'):
            position = int(args['cursor'])
        else:
            assert args == {**FIXED, 'max_bytes': 32}
            position = 0
        data = {**FIXED, 'text': windows[position]}
        if position + 1 < len(windows):
            data['next'] = '/_r/c/' + str(position + 1)
        return okay(op, data)

    call = Call(registry, handler)
    args = {'ref': FIXED, 'max_bytes': 32, 'max_chars': 32}
    chunks = []
    while True:
        result = await run_tool('msg_read', args, call=call)
        assert result['status'] == 'ok' and result['data']['revision'] == FIXED['revision']
        chunks.append(result['data']['text'])
        if not result['data'].get('cursor'):
            break
        args = {'cursor': result['data']['cursor']}
    assert ''.join(chunks) == full
    assert all(op != 'discussion.ack' for op, _, _ in call.calls)


async def test_read_ref_never_silently_projects_a_current_revision(registry):
    call = Call(
        registry,
        lambda op, args, version: okay(
            op, {'id': FIXED['id'], 'revision': 'v_new', 'type': 'post'}
        ),
    )
    result = await run_tool('msg_read', {'ref': FIXED}, call=call)
    assert result['error']['code'] == 'revision_mismatch' and len(call.calls) == 1


@pytest.mark.parametrize('title', ['有空格 / 中文标题', 'A readable human title'])
async def test_human_title_is_markdown_not_resource_name(registry, title):
    def handler(op, args, version):
        if op == 'discovery.resolve':
            return okay(op, {'ref': {'id': 'r_topic'}})
        if op == 'communication.conversation_get':
            return okay(op, {'topic': {'id': 'r_topic'}, 'kind': 'discussion'})
        assert op == 'content.post_create' and 'name' not in args
        assert args == {'parent': 'r_topic', 'body': '# ' + title + '\n\nbody'}
        return okay(op, {}, (ResourceRef(**FIXED),))

    call = Call(registry, handler)
    result = await run_tool(
        'msg_post',
        {'topic': '/topic', 'title': title, 'body': 'body'},
        call=call,
        identity=identity(),
    )
    assert result['status'] == 'ok' and result['data']['refs'] == [FIXED]


async def test_send_pending_replay_keeps_same_subcalls_after_acceptance(registry):
    current = {'state': 'pending'}
    frozen_request = {'conversation_id': 'r_dm', 'state': 'pending', 'introduction_ref': FIXED}

    def handler(op, args, version):
        if op == 'discovery.resolve':
            return okay(op, {'ref': {'id': 's_friend'}})
        if op == 'discovery.get':
            return okay(op, {'id': 's_friend', 'type': 'user'})
        assert op == 'communication.dm_request' and version == 3
        assert current['state'] in {'pending', 'active'}
        # Model the executor returning the original committed request result.
        return okay(op, frozen_request)

    call = Call(registry, handler)
    args = {'recipient': '@friend', 'body': 'hello'}
    first = await run_tool('msg_send', args, call=call, identity=identity())
    initial_plan = list(call.calls)
    current['state'] = 'active'
    replay = await run_tool('msg_send', args, call=call, identity=identity())
    assert replay == first and replay['data']['sent'] is False
    assert call.calls[len(initial_plan) :] == initial_plan
    assert [op for op, _, _ in initial_plan] == [
        'discovery.resolve',
        'discovery.get',
        'communication.dm_request',
    ]


async def test_send_active_long_body_uses_request_then_send_without_truncation(registry):
    body = '长消息' * 3000

    def handler(op, args, version):
        if op == 'discovery.resolve':
            return okay(op, {'ref': {'id': 's_friend'}})
        if op == 'discovery.get':
            return okay(op, {'id': 's_friend', 'type': 'user'})
        if op == 'communication.dm_request':
            assert args == {'recipient': 's_friend', 'introduction': body} and version == 3
            return okay(op, {'conversation_id': 'r_dm', 'state': 'active'})
        assert op == 'communication.dm_send' and version == 2
        assert args == {'conversation_id': 'r_dm', 'body': body}
        return okay(op, {}, (ResourceRef(**FIXED),))

    call = Call(registry, handler)
    result = await run_tool(
        'msg_send', {'recipient': '@friend', 'body': body}, call=call, identity=identity()
    )
    assert result['data']['sent'] is True and result['data']['state'] == 'active'
    assert result['data']['needs_acceptance'] is False and result['data']['refs'] == [FIXED]
    assert [op for op, _, _ in call.calls] == [
        'discovery.resolve',
        'discovery.get',
        'communication.dm_request',
        'communication.dm_send',
    ]


@pytest.mark.parametrize('address', ['@friend', '/@friend'])
async def test_resolve_normalizes_handle_to_real_subject_path(registry, address):
    def handler(op, args, version):
        assert op == 'discovery.resolve' and version == 1
        assert args == {'address': '/@friend'}
        return okay(op, {'ref': {'id': 's_friend'}, 'path': '/@friend'})

    call = Call(registry, handler)
    result = await run_tool('msg_resolve', {'address': address}, call=call)
    assert result['data']['ref'] == {'id': 's_friend'}
    assert call.calls == [('discovery.resolve', {'address': '/@friend'}, 1)]
