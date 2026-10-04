"""Small, ordinary MCP tools over existing versioned MSG operations.

The transport owns authentication, replay IDs and executor calls. This adapter
never accepts an envelope or a credential and never turns a read into an ACK.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from jsonschema import Draft202012Validator

from msg.core.errors import Failure, public_error_message

MAX_BODY_BYTES = 32768
PAGE_FIELDS = ['id', 'revision', 'name', 'type', 'path']
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 2048}
CURSOR = {'type': 'string', 'minLength': 1, 'maxLength': 16384}
IDENTIFIER = {'type': 'string', 'minLength': 1, 'maxLength': 160, 'pattern': r'^[A-Za-z0-9_.:-]+$'}
REF = {
    'type': 'object',
    'properties': {'id': IDENTIFIER, 'revision': IDENTIFIER},
    'required': ['id'],
    'additionalProperties': False,
}
FIXED_REF = {**REF, 'required': ['id', 'revision']}
BODY = {'type': 'string', 'minLength': 1, 'maxLength': MAX_BODY_BYTES}
LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 50}


def _schema(properties=None, required=(), **extra):
    return {
        'type': 'object',
        'properties': properties or {},
        'required': list(required),
        'additionalProperties': False,
        **extra,
    }


def _choice(*names):
    return {'oneOf': [{'required': [name]} for name in names]}


SCHEMAS = {
    'msg_me': _schema(),
    'msg_connect': _schema(),
    'msg_resolve': _schema({'address': TEXT, 'revision': IDENTIFIER}, ('address',)),
    'msg_read': _schema(
        {
            'address': TEXT,
            'ref': REF,
            'cursor': CURSOR,
            'max_bytes': {'type': 'integer', 'minimum': 32, 'maximum': 8192},
            'max_chars': {'type': 'integer', 'minimum': 32, 'maximum': 8192},
        },
        **_choice('address', 'ref', 'cursor'),
    ),
    'msg_list': _schema({
        'address': TEXT,
        'collection': {'enum': ['children', 'replies']},
        'limit': LIMIT,
        'cursor': CURSOR,
    }),
    'msg_search': _schema({
        'query': {'type': 'string', 'minLength': 1, 'maxLength': 512},
        'scope': TEXT,
        'limit': LIMIT,
        'cursor': CURSOR,
    }),
    'msg_inbox': _schema({'limit': LIMIT, 'cursor': CURSOR}),
    'msg_notifications': _schema({'limit': LIMIT, 'cursor': CURSOR}),
    'msg_send': _schema({'recipient': TEXT, 'body': BODY}, ('recipient', 'body')),
    'msg_reply': _schema(
        {'message_ref': FIXED_REF, 'post_ref': FIXED_REF, 'body': BODY},
        ('body',),
        **_choice('message_ref', 'post_ref'),
    ),
    'msg_post': _schema(
        {
            'topic': TEXT,
            'title': {'type': 'string', 'minLength': 1, 'maxLength': 200, 'pattern': r'^[^\r\n]+$'},
            'body': BODY,
            'summary': {'type': 'string', 'maxLength': 280},
        },
        ('topic', 'body'),
    ),
    'msg_ack': _schema({'ref': FIXED_REF}, ('ref',)),
    'msg_dm_decide': _schema({'ref': REF, 'accept': {'type': 'boolean'}}, ('ref', 'accept')),
}
DESCRIPTIONS = {
    'msg_me': 'Show the connected MSG account and available capabilities. Never returns secrets.',
    'msg_connect': 'Ask the host to connect a MSG account using OAuth. No credentials are entered here.',
    'msg_resolve': 'Resolve a MSG path, URL, stable reference or @handle using current access.',
    'msg_read': 'Read one bounded text segment or resource metadata. Reading never acknowledges it. Continue with cursor only.',
    'msg_list': 'List a bounded page of children or replies. Continue with cursor only.',
    'msg_search': 'Search readable MSG content within scope (default /). Continue with cursor only.',
    'msg_inbox': 'List inbox references and small previews, without reading full bodies or acknowledging messages.',
    'msg_notifications': 'List a bounded page of visible event references. Continue with cursor; this does not acknowledge them.',
    'msg_send': 'Send a private message to a currently resolved @handle. Without a conversation, create a pending request with the full short introduction; the recipient must explicitly accept. A pending request is not a sent message.',
    'msg_reply': 'Reply privately to message_ref, or reply to a discussion post_ref. Use one fixed reference; never both.',
    'msg_post': 'Publish a post in the specified discussion topic. Private direct conversations use msg_send.',
    'msg_ack': 'Explicitly acknowledge exactly the supplied resource revision. The SDK obtains its digest.',
    'msg_dm_decide': 'Explicitly accept or reject a direct conversation request. Call only for the user decision; never automatically accept.',
}
PUBLIC = frozenset({'msg_me', 'msg_connect', 'msg_resolve', 'msg_read', 'msg_list', 'msg_search'})
READ = frozenset({'discovery.resolve@1', 'discovery.get@1', 'discovery.read_segment@1'})
DEPENDENCIES = {
    'msg_me': frozenset(),
    'msg_connect': frozenset(),
    'msg_resolve': frozenset({'discovery.resolve@1'}),
    'msg_read': READ,
    'msg_list': frozenset({'discovery.resolve@1', 'discovery.read_query@3'}),
    'msg_search': frozenset({'discovery.resolve@1', 'discovery.lexical_search@5'}),
    'msg_inbox': frozenset({'communication.inbox@1'}),
    'msg_notifications': frozenset({'communication.changes@1'}),
    'msg_send': frozenset({
        'discovery.resolve@1',
        'discovery.get@1',
        'communication.dm_send@2',
        'communication.dm_request@3',
    }),
    'msg_post': frozenset({
        'discovery.resolve@1',
        'discovery.get@1',
        'content.post_create@2',
        'communication.conversation_get@1',
    }),
    'msg_ack': frozenset({'discovery.get@1', 'discussion.ack@1'}),
    'msg_dm_decide': frozenset({
        'communication.conversation_get@1',
        'communication.dm_accept@2',
        'communication.dm_reject@2',
    }),
}
REPLY_READ = frozenset({'discovery.get@1', 'communication.conversation_get@1'})
REPLY_ALTERNATIVES = (
    REPLY_READ | {'communication.dm_send@2'},
    REPLY_READ | {'discussion.reply@2'},
)
DEPENDENCIES['msg_reply'] = REPLY_READ


def _operations(identity):
    return frozenset(getattr(identity, 'operations', ())) if identity is not None else frozenset()


def _available(name, available):
    return DEPENDENCIES[name] <= available and (
        name != 'msg_reply' or any(branch <= available for branch in REPLY_ALTERNATIVES)
    )


def _scopes(name, operations):
    if name == 'msg_connect':
        return ['msg.mcp.read', 'msg.mcp.message', 'msg.mcp.post']
    if name in PUBLIC or name in {'msg_inbox', 'msg_notifications'}:
        return ['msg.mcp.read']
    if name in {'msg_send', 'msg_dm_decide', 'msg_ack'}:
        return ['msg.mcp.read', 'msg.mcp.message']
    if name == 'msg_reply':
        return ['msg.mcp.read'] + [
            scope
            for operation, scope in (
                ('communication.dm_send@2', 'msg.mcp.message'),
                ('discussion.reply@2', 'msg.mcp.post'),
            )
            if operation in operations
        ]
    return ['msg.mcp.read', 'msg.mcp.post']


def tool_catalog(registry, identity=None):
    """Catalog only installed contracts and the account's finite operation ceiling."""
    installed = frozenset(
        f'{spec.name}@{spec.version}' for spec in registry.operations('network') if spec.enabled
    )
    permitted = _operations(identity) & installed
    tools = []
    for name, schema in SCHEMAS.items():
        available = installed if name in PUBLIC else permitted
        if not _available(name, available) or name not in PUBLIC and identity is None:
            continue
        schemes = ([{'type': 'noauth'}] if name in PUBLIC else []) + [
            {'type': 'oauth2', 'scopes': _scopes(name, available)}
        ]
        tools.append({
            'name': name,
            'description': DESCRIPTIONS[name],
            'inputSchema': deepcopy(schema),
            'securitySchemes': schemes,
            '_meta': {'securitySchemes': deepcopy(schemes)},
            'annotations': {
                'readOnlyHint': name in PUBLIC or name in {'msg_inbox', 'msg_notifications'},
                'destructiveHint': name == 'msg_dm_decide',
                'idempotentHint': name in PUBLIC
                or name in {'msg_inbox', 'msg_notifications', 'msg_ack', 'msg_dm_decide'},
                'openWorldHint': True,
            },
        })
    return tools


_HINTS = {
    'authentication_required': 'Connect your MSG account with msg_connect, then retry.',
    'credential_ceiling': 'Reconnect with permission for this tool; the host must request the required MSG scope.',
    'credential_expired': 'Reconnect your MSG account to renew access.',
    'invalid_token': 'Reconnect your MSG account to obtain a valid host credential.',
    'jsonrpc_id_conflict': 'Keep the same ID only for an unchanged retry; use a new call ID for a new action.',
    'idempotency_conflict': 'Keep the same ID only for an unchanged retry; use a new call ID for a new action.',
    'payload_digest_mismatch': 'Retry through the high-level MSG tool; the SDK must construct the authenticated request.',
    'request_expired': 'Retry through the host with a fresh authenticated request.',
    'generation_conflict': 'Read the current resource before retrying.',
    'revision_conflict': 'Read the current revision and choose the intended fixed reference.',
    'revision_mismatch': 'Use a matching resource and revision reference.',
    'ack_digest_mismatch': 'Read the fixed revision again before explicitly acknowledging it.',
    'resync_required': 'Discard this cursor and start a new authorized page.',
    'cursor_query_mismatch': 'Continue with cursor only, preserving the original query.',
    'dm_conversation_required': 'Create or accept a direct conversation in MSG, then send again.',
    'dm_not_active': 'The recipient must accept the direct conversation before messages can be sent.',
    'dm_blocked': 'Messaging is unavailable while either participant blocks the other.',
    'dm_rejected': 'This direct conversation was rejected; no message was sent.',
    'dm_reference_private': 'Use message_ref to reply privately; a direct message cannot be a discussion reply.',
    'not_a_direct_message': 'Use post_ref for a discussion post.',
    'conversation_type_unavailable': 'The server must provide the authorized conversation_get operation.',
    'invalid_tool_arguments': 'Use only the documented daily parameters; credentials and operation envelopes are not accepted.',
    'body_too_large': 'Keep the body within 32768 UTF-8 bytes.',
    'dm_introduction_too_large': 'To start a conversation, send an introduction of at most 500 characters and 1024 UTF-8 bytes. The full message can be sent after acceptance.',
}
_MESSAGES = {
    'dm_conversation_required': 'No active direct conversation is available.',
    'dm_reference_private': 'This reference belongs to a private direct conversation.',
    'not_a_direct_message': 'This reference is not a direct message.',
    'conversation_type_unavailable': 'The conversation type could not be confirmed.',
    'invalid_tool_arguments': 'The tool arguments are invalid.',
    'body_too_large': 'The message body is too large.',
    'dm_introduction_too_large': 'The introduction is too large to start a direct conversation.',
    'credential_expired': 'The connected credential has expired.',
    'unknown_tool': 'This MSG tool is not available.',
    'credential_ceiling': 'The connected credential does not permit this operation.',
}


def _error(code, retryable=False):
    # Never echo operation messages, field paths, exception strings or details.
    known = (
        code in _HINTS
        or code in _MESSAGES
        or public_error_message(code) != 'The operation could not be completed.'
    )
    safe = code if known else 'operation_failed'
    value = {
        'code': safe,
        'message': _MESSAGES.get(safe, public_error_message(safe)),
        'retryable': bool(retryable),
    }
    if safe in _HINTS:
        value['hint'] = _HINTS[safe]
    return {'status': 'error', 'error': value}


def _value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, Mapping) else getattr(obj, name, default)


class _ToolFailure(Exception):
    def __init__(self, result):
        self.result = result


class _Caller:
    def __init__(self, call, identity):
        self.call, self.identity = call, identity

    async def __call__(self, operation, arguments, version=1):
        if self.identity is not None and f'{operation}@{version}' not in _operations(self.identity):
            raise _ToolFailure(_error('credential_ceiling'))
        result = await self.call(operation, arguments, contract_version=version)
        status = _value(result, 'status')
        if status == 'error':
            error = _value(result, 'error', {})
            code = _value(error, 'code')
            if code == 'introduction_too_large':
                code = 'dm_introduction_too_large'
            raise _ToolFailure(_error(code, _value(error, 'retryable', False)))
        if status not in {'ok', 'accepted'}:
            raise _ToolFailure(_error('transport_uncertain', True))
        data = _value(result, 'data', {})
        data = dict(data) if isinstance(data, Mapping) else {}
        resources = _value(result, 'resources', ())
        data['_resources'] = [
            ref
            for raw in resources
            if (ref := _ref({'id': _value(raw, 'id'), 'revision': _value(raw, 'revision')}))
        ]
        return data


def _clip(text, max_chars=6000, max_bytes=8192):
    if not isinstance(text, str):
        return ''
    return text[:max_chars].encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore')


def _ref(value, *, fixed=False):
    if not isinstance(value, Mapping) or not isinstance(value.get('id'), str):
        return None
    result = {'id': value['id']}
    if isinstance(value.get('revision'), str):
        result['revision'] = value['revision']
    return result if not fixed or 'revision' in result else None


def _projection(data):
    # These are ordinary resource labels. Authentication/envelope material is
    # excluded even if a privileged callback accidentally returns extra fields.
    result = {}
    for key in (
        'id',
        'revision',
        'name',
        'type',
        'path',
        'stable_path',
        'media_type',
        'created_at',
        'modified_at',
    ):
        if isinstance(data.get(key), str):
            result[key] = _clip(data[key], 2048, 4096)
    if type(data.get('size')) is int:
        result['size'] = data['size']
    if data.get('_resources'):
        result['refs'] = data['_resources']
    return result


def _cursor(data):
    cursor = data.get('cursor') or data.get('sync_cursor')
    if not cursor and isinstance(data.get('pageInfo'), Mapping):
        info = data['pageInfo']
        cursor = info.get('endCursor') if info.get('hasNextPage') else None
    if not cursor and isinstance(data.get('next'), str) and data['next'].startswith('/_r/c/'):
        cursor = data['next'][len('/_r/c/') :]
    return cursor if isinstance(cursor, str) and len(cursor) <= 16384 else None


def _contact(data):
    if not isinstance(data, Mapping):
        return {}
    return {
        key: _clip(data[key], 100 if key == 'name' else 2048, 400 if key == 'name' else 4096)
        for key in ('name', 'path')
        if isinstance(data.get(key), str)
    }


def _page(data, kind):
    items = []
    source = data.get('items', ())
    for item in list(source)[:50] if isinstance(source, (tuple, list)) else ():
        if not isinstance(item, Mapping):
            continue
        projected = _projection(item)
        if kind == 'notifications':
            projected['refs'] = [ref for raw in item.get('resources', ()) if (ref := _ref(raw))]
            if isinstance(item.get('time'), str):
                projected['time'] = _clip(item['time'], 100, 100)
        elif kind == 'inbox':
            for key in ('sender_contact', 'recipient_contact'):
                if contact := _contact(item.get(key)):
                    projected[key] = contact
            if isinstance(item.get('time'), str):
                projected['time'] = _clip(item['time'], 100, 100)
            resource = item.get('resource')
            if isinstance(resource, str):
                projected['ref'] = {'id': resource}
            elif ref := _ref(resource):
                projected['ref'] = ref
            preview = item.get('preview', {})
            if isinstance(preview, Mapping):
                projected['preview'] = {
                    key: _clip(preview[key], 280, 1120)
                    for key in ('title', 'excerpt', 'summary')
                    if isinstance(preview.get(key), str)
                }
                if author := _contact(preview.get('author')):
                    projected['preview']['author'] = author
                if isinstance(preview.get('revision'), str) and 'ref' in projected:
                    projected['ref']['revision'] = preview['revision']
        elif kind == 'search' and isinstance(item.get('snippet'), str):
            projected['snippet'] = _clip(item['snippet'], 280, 1120)
        items.append(projected)
    result = {'items': items}
    if cursor := _cursor(data):
        result['cursor'] = cursor
    if type(data.get('has_more')) is bool:
        result['has_more'] = data['has_more']
    return result


async def _resolve(call, address, revision=None):
    # The address contract uses /@handle as the subject path. Bare @handle
    # otherwise reaches resolve_read as an invalid resource identifier.
    args = {'address': '/' + address if address.startswith('@') else address}
    if revision is not None:
        args['revision'] = revision
    data = await call('discovery.resolve', args)
    ref = _ref(data.get('ref'))
    if ref is None:
        raise _ToolFailure(_error('not_found'))
    return ref, _projection(data)


async def _get(call, ref, fields):
    return await call('discovery.get', {**ref, 'fields': fields})


async def _kind(call, parent):
    data = await call('communication.conversation_get', {'id': parent})
    kind = data.get('kind')
    topic = _ref(data.get('topic'))
    if kind not in {'direct', 'discussion'} or topic is None or topic['id'] != parent:
        raise _ToolFailure(_error('conversation_type_unavailable'))
    return kind


def _continuation(arguments):
    if arguments.get('cursor') and set(arguments) != {'cursor'}:
        raise _ToolFailure(_error('cursor_query_mismatch'))


async def _run(name, arguments, call, identity):
    if name == 'msg_me':
        if identity is None:
            return {
                'authenticated': False,
                'subject': None,
                'handle': None,
                'credential_type': None,
                'capabilities': [],
            }
        subject = getattr(identity, 'subject', None)
        handle = None
        if 'discovery.get@1' in _operations(identity):
            data = await _get(call, {'id': subject}, ['id', 'name', 'type'])
            if data.get('type') == 'user' and isinstance(data.get('name'), str):
                handle = '@' + _clip(data['name'].lstrip('@'), 100, 400)
        return {
            'authenticated': True,
            'subject': subject,
            'handle': handle,
            'credential_type': getattr(identity, 'credential_type', None),
            'capabilities': [
                capability
                for capability, available in (
                    ('read_public', READ <= _operations(identity)),
                    ('read_private', READ | DEPENDENCIES['msg_inbox'] <= _operations(identity)),
                    ('send_dm', _available('msg_send', _operations(identity))),
                    ('reply', _available('msg_reply', _operations(identity))),
                    ('post', _available('msg_post', _operations(identity))),
                    ('ack', _available('msg_ack', _operations(identity))),
                    ('decide_dm', _available('msg_dm_decide', _operations(identity))),
                )
                if available
            ],
        }
    if name == 'msg_connect':
        raise _ToolFailure(_error('authentication_required'))
    if name == 'msg_resolve':
        ref, data = await _resolve(call, arguments['address'], arguments.get('revision'))
        return {**data, 'ref': ref}
    if name == 'msg_read':
        _continuation(arguments)
        max_chars = arguments.get('max_chars', 6000)
        max_bytes = min(arguments.get('max_bytes', 4096), max_chars)
        if arguments.get('cursor'):
            segment = await call('discovery.read_segment', {'cursor': arguments['cursor']})
            ref = _ref(segment, fixed=True)
            if ref is None:
                raise _ToolFailure(_error('revision_mismatch'))
            metadata = await _get(call, ref, PAGE_FIELDS + ['media_type', 'size'])
        else:
            ref = arguments.get('ref')
            if ref is None:
                ref, _ = await _resolve(call, arguments['address'])
            # No body or revision-only fields are requested from a container.
            metadata = await _get(call, ref, PAGE_FIELDS)
            if (
                metadata.get('id') != ref['id']
                or ref.get('revision') is not None
                and metadata.get('revision') != ref['revision']
            ):
                raise _ToolFailure(_error('revision_mismatch'))
            if not metadata.get('revision'):
                return {**_projection(metadata), 'text': '', 'truncated': False}
            pinned = {'id': ref['id'], 'revision': metadata['revision']}
            metadata = await _get(call, pinned, PAGE_FIELDS + ['media_type', 'size'])
            if not str(metadata.get('media_type', '')).startswith('text/'):
                return {
                    **_projection(metadata),
                    'text': '',
                    'truncated': False,
                    'hint': 'This resource has no readable text segment.',
                }
            segment = await call('discovery.read_segment', {**pinned, 'max_bytes': max_bytes})
            if _ref(segment, fixed=True) != pinned:
                raise _ToolFailure(_error('revision_mismatch'))
        if (
            metadata.get('id') != ref['id']
            or ref.get('revision') is not None
            and metadata.get('revision') != ref['revision']
        ):
            raise _ToolFailure(_error('revision_mismatch'))
        text = _clip(segment.get('text', ''), max_chars, max_bytes)
        result = {
            **_projection(metadata),
            'text': text,
            'truncated': bool(segment.get('next')) or text != segment.get('text', ''),
        }
        if cursor := _cursor(segment):
            result['cursor'] = cursor
        return result
    if name == 'msg_list':
        _continuation(arguments)
        if arguments.get('cursor'):
            args = {'cursor': arguments['cursor']}
        else:
            ref, _ = await _resolve(call, arguments.get('address', '/'))
            args = {
                'parent': ref['id'],
                'collection': arguments.get('collection', 'children'),
                'limit': arguments.get('limit', 20),
                'fields': PAGE_FIELDS,
                'query_version': 3,
            }
        return _page(await call('discovery.read_query', args, 3), 'list')
    if name == 'msg_search':
        _continuation(arguments)
        if arguments.get('cursor'):
            args = {'cursor': arguments['cursor']}
        else:
            if not arguments.get('query'):
                raise _ToolFailure(_error('invalid_tool_arguments'))
            ref, _ = await _resolve(call, arguments.get('scope', '/'))
            args = {
                'scope': ref['id'],
                'terms': arguments['query'],
                'limit': arguments.get('limit', 20),
                'fields': PAGE_FIELDS,
                'snippet': True,
            }
        return _page(await call('discovery.lexical_search', args, 5), 'search')
    if name in {'msg_inbox', 'msg_notifications'}:
        args = {'limit': arguments.get('limit', 20)}
        if arguments.get('cursor'):
            args['cursor'] = arguments['cursor']
        operation = 'communication.inbox' if name == 'msg_inbox' else 'communication.changes'
        return _page(
            await call(operation, args), 'inbox' if name == 'msg_inbox' else 'notifications'
        )
    if name == 'msg_send':
        address = arguments['recipient']
        if not address.startswith(('/', '@', 'r_', 'http://', 'https://')):
            address = '@' + address
        ref, _ = await _resolve(call, address)
        recipient = await _get(call, ref, ['id', 'type'])
        if recipient.get('type') != 'user':
            raise _ToolFailure(_error('invalid_tool_arguments'))
        # The request subcall is a fixed write plan. Its executor replay keeps
        # an original pending invitation pending, even after later acceptance.
        request = await call(
            'communication.dm_request',
            {'recipient': ref['id'], 'introduction': arguments['body']},
            3,
        )
        state = request.get('state')
        conversation = request.get('conversation_id')
        if state not in {'pending', 'active'} or not isinstance(conversation, str):
            raise _ToolFailure(_error('transport_uncertain', True))
        if state == 'pending':
            introduction = _ref(request.get('introduction_ref'), fixed=True)
            result = {
                **_projection(request),
                'conversation_id': conversation,
                'private': True,
                'sent': False,
                'state': 'pending',
                'needs_acceptance': True,
                'introduction_added': introduction is not None,
                'hint': 'The full introduction was added to a pending request; the recipient must accept before messages can be sent.'
                if introduction
                else 'This conversation is awaiting acceptance. This message was not added; retry with a new call after acceptance.',
            }
            if introduction:
                result['introduction_ref'] = introduction
            return result
        data = await call(
            'communication.dm_send', {'conversation_id': conversation, 'body': arguments['body']}, 2
        )
        return {
            **_projection(data),
            'conversation_id': conversation,
            'private': True,
            'sent': True,
            'state': 'active',
            'needs_acceptance': False,
        }
    if name == 'msg_reply':
        direct = 'message_ref' in arguments
        ref = arguments['message_ref' if direct else 'post_ref']
        post = await _get(call, {'id': ref['id']}, ['id', 'revision', 'type', 'parent'])
        if post.get('type') != 'post' or not isinstance(post.get('parent'), str):
            raise _ToolFailure(_error('invalid_tool_arguments'))
        # Verify that the fixed revision belongs to this readable post as well.
        await _get(call, ref, ['id', 'revision'])
        kind = await _kind(call, post['parent'])
        if direct and kind != 'direct':
            raise _ToolFailure(_error('not_a_direct_message'))
        if not direct and kind == 'direct':
            raise _ToolFailure(_error('dm_reference_private'))
        if direct:
            data = await call(
                'communication.dm_send',
                {'conversation_id': post['parent'], 'body': arguments['body']},
                2,
            )
            return {**_projection(data), 'conversation_id': post['parent'], 'private': True}
        return _projection(
            await call('discussion.reply', {'target': ref, 'body': arguments['body']}, 2)
        )
    if name == 'msg_post':
        ref, _ = await _resolve(call, arguments['topic'])
        if await _kind(call, ref['id']) == 'direct':
            raise _ToolFailure(_error('dm_reference_private'))
        args = {'parent': ref['id'], 'body': arguments['body']}
        if 'title' in arguments:
            args['body'] = '# ' + arguments['title'] + '\n\n' + args['body']
        if len(args['body'].encode('utf-8')) > MAX_BODY_BYTES:
            raise _ToolFailure(_error('body_too_large'))
        if 'summary' in arguments:
            args['summary'] = arguments['summary']
        return _projection(await call('content.post_create', args, 2))
    if name == 'msg_dm_decide':
        ref = arguments['ref']
        data = await call('communication.conversation_get', {'id': ref['id']})
        if data.get('kind') != 'direct':
            raise _ToolFailure(_error('not_a_direct_message'))
        conversation = _ref(data.get('topic'))
        if conversation is None or conversation['id'] != ref['id']:
            raise _ToolFailure(_error('revision_mismatch'))
        operation = 'communication.dm_accept' if arguments['accept'] else 'communication.dm_reject'
        decision = await call(operation, {'conversation_id': conversation['id']}, 2)
        state = 'active' if arguments['accept'] else 'rejected'
        if decision.get('state') != state:
            raise _ToolFailure(_error('transport_uncertain', True))
        return {'ref': conversation, 'state': state, 'private': True}
    if name == 'msg_ack':
        ref = arguments['ref']
        metadata = await _get(call, ref, ['id', 'revision', 'digest'])
        if (
            metadata.get('id') != ref['id']
            or metadata.get('revision') != ref['revision']
            or not isinstance(metadata.get('digest'), str)
        ):
            raise _ToolFailure(_error('revision_mismatch'))
        await call('discussion.ack', {'target': ref, 'digest': metadata['digest']})
        return {'ref': ref, 'acknowledged': True}
    raise _ToolFailure(_error('unknown_tool'))


async def run_tool(name, arguments, *, call, identity=None):
    """Return a safe business result; the MCP transport formats structuredContent."""
    try:
        if name not in SCHEMAS:
            return _error('unknown_tool')
        if not isinstance(arguments, dict) or not Draft202012Validator(SCHEMAS[name]).is_valid(
            arguments
        ):
            return _error('invalid_tool_arguments')
        if 'body' in arguments and len(arguments['body'].encode('utf-8')) > MAX_BODY_BYTES:
            return _error('body_too_large')
        if name not in PUBLIC:
            if identity is None:
                return _error('authentication_required')
            if not _available(name, _operations(identity)):
                return _error('credential_ceiling')
        data = await _run(name, arguments, _Caller(call, identity), identity)
        return {'status': 'ok', 'data': data}
    except _ToolFailure as exc:
        return exc.result
    except Failure as exc:
        return _error(exc.code, exc.retryable)
    except Exception:
        return _error('internal_error')
