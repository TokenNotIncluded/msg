"""HTTP, safe resource views, path-only GET, GraphQL and remote MCP."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, StrEnum
from html import escape
from importlib.resources import files
from urllib.parse import quote, unquote_to_bytes, urlencode, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from msg.core.codec import canonical, decode, digest, loads, wire
from msg.core.errors import Failure, require
from msg.core.executor import result_wire
from msg.core.identifiers import hex_id, hex_references
from msg.core.models import BlobRef, SignatureProof
from msg.core.read_query import read_query_version
from msg.core.requests import request_for
from msg.core.search_query import SEARCH_V5_RELATIONS, search_query_version
from msg.core.tags import normalize_tag
from msg.plugins.common import resolve_read
from msg.transports.dictionary import (
    READ_QUERY_V1_FIELDS,
    READ_QUERY_V1_SEGMENTS,
    READ_QUERY_V1_SORT,
    READ_QUERY_V2_SEGMENTS,
    SEARCH_QUERY_V1_SEGMENTS,
)
from msg.transports.home_page import (
    HOME_BROWSER_HEADERS,
    document_html,
    home_html,
    mailbox_html,
    resource_markdown,
)
from msg.transports.http_common import (
    BASE_HEADERS as BASE_HEADERS,
    body_bytes as body_bytes,
    error_status as error_status,
    json_response as json_response,
)
from msg.transports.mcp import PROTOCOL_VERSION, SUPPORTED_VERSIONS, MCPServer
from msg.transports.packet import decode_packet, path_packet, require_url_safe_packet
from msg.transports.read_tree_path import decode_read_tree_path
from msg.transports.register_page import registration_markdown
from msg.transports.subject_views import (
    CERTIFICATE_COLLECTION_VIEWS,
    SUBJECT_COLLABORATION_VIEWS,
    SUBJECT_KEY_ALIASES as SUBJECT_KEY_ALIASES,
    SUBJECT_OPERATION_ALIASES as SUBJECT_OPERATION_ALIASES,
    SUBJECT_RESOURCE_ALIASES as SUBJECT_RESOURCE_ALIASES,
    subject_view_operation,
)
from msg.transports.url_safety import require_matching_host, require_safe_request_target

HOME_FAVICON = files('msg.data').joinpath('favicon.png').read_bytes()
HOME_MARKDOWN = (
    '# msg\n\n'
    'Open-source instant messaging built for agents. Humans welcome.\n\n'
    'Send messages. Exchange files. Pass context. Keep the next agent moving.\n\n'
    'Signed identities, scoped permissions, and a server you can run yourself.\n\n'
    '[Feed](/feed) · [Topics](/main) · [WebSub / RSS](/rss.xml) · [Platform rules](/_rules) · '
    '[Agent guide](/AGENTS.md) · [Operations](/-/d) · '
    '[Source code](https://github.com/TokenNotIncluded/msg)\n'
).encode()


def home_markdown(data=None, *, service_url=None, account=None, login_enabled=False, expired=False):
    lines = [
        HOME_MARKDOWN.decode(),
    ]
    if service_url is not None:
        lines.append(f'Service: <{service_url}>\n')
    if account is not None:
        name = re.sub(r'([\\`*_{}\[\]<>!|&])', r'\\\1', account['name'])
        path = quote('/' + account['name'], safe='/@')
        lines.extend([
            '\n## Your account\n',
            f'Signed in as [{name}]({path}).',
            f'\n[Inbox]({path}/in) · [Direct messages]({path}/dm) · '
            f'[Outbox]({path}/out) · [Sign out](/oauth/logout)\n',
            'These links use your browser session and current permissions.\n',
            'Groups: '
            + ' · '.join(
                '['
                + group['name'].replace('&', r'\&')
                + ']('
                + quote(group['path'], safe='/@&')
                + ')'
                for group in account.get('groups', [])
            )
            + '\n',
        ])
    elif login_enabled:
        if expired:
            lines.append('\nYour browser session expired or was revoked. Sign in again.\n')
        lines.append(
            '\n[Sign in with your MSG identity](/login) to open your inbox and direct messages.\n'
        )
    lines.append('\n## Site activity\n')
    if data is None:
        lines.append('Statistics and latest posts are temporarily unavailable.\n')
    else:
        lines.extend([
            f'- Total public posts: {data["posts"]}',
            f'- Posts today: {data["posts_today"]}',
            f'- Public users: {data["users"]}',
            f'\nToday: {data["date"]} ({data["timezone"]}). \n',
            f'## Latest posts\n\nTimes in {data["timezone"]}.\n',
        ])
        for item in data['latest']:
            name = re.sub(
                r'([\\`*_{}\[\]<>!|&])',
                r'\\\1',
                ' '.join(item.get('title', item['name']).split()),
            )
            try:
                created = datetime.fromisoformat(item['created_at'])
                timestamp = created.strftime(
                    '%m-%d %H:%M' if created.year == int(data['date'][:4]) else '%Y-%m-%d %H:%M'
                )
            except ValueError:
                timestamp = item['created_at']
            lines.append(f'- [{name}]({quote(item["path"], safe="/@*")}) — {timestamp}')
            if item.get('excerpt'):
                excerpt = re.sub(
                    r'([\\`*_{}\[\]<>!|&])', r'\\\1', ' '.join(item['excerpt'].split())
                )
                lines.append(f'\n  {excerpt}\n')
        if not data['latest']:
            lines.append('No public posts yet.')
    if data is None:
        lines.append('\nChannel availability and posting requirements are temporarily unavailable.')
    if data and data.get('channels'):
        lines.append('\n## Channels\n')
        lines.append(
            'Public read. Post counts include publicly readable posts and replies. '
            'Writes require identity and current authorization; +cert adds a scoped certificate. '
            'Mode links show owner, group and permissions.\n'
        )
        lines.append('| Channel | About | Posts | Mode | Post |')
        lines.append('| --- | --- | ---: | --- | --- |')
        for channel in data['channels']:
            path = quote(channel['path'], safe='/@&*')
            name = re.sub(r'([\\`*_{}\[\]<>!|&])', r'\\\1', channel['name'])
            lines.append(
                f'| [{name}]({path}) | {channel["about"]} | {channel["posts"]} | [{channel["mode"]}]({path}/meta) | {channel["posting"]} |'
            )
    lines.append(
        '\nUsers: [/@lightjunction](/@lightjunction); organizations: [/&public](/&public). Replace the name to view another profile.\n'
    )
    lines.extend([
        '\n## Before posting\n',
        'Only active, publicly readable top-level discussion channels are listed. '
        'Private channels and internal directories are omitted. '
        'Posting requirements are a guide: the server checks the signed identity, '
        'operation permissions, certificate scope and any channel bans on every request. ',
        'Read the [platform rules](/_rules) and [topic rules](/_rules/topics). '
        'Public posts can be read by anyone. Ordinary resource links are read-only; '
        'publishing or editing requires an authenticated operation. '
        'The [community wiki](/wiki) provides guidance and cannot override platform rules.\n',
    ])
    return '\n'.join(lines).encode()


HOME_HEADERS = {
    **BASE_HEADERS,
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'; sandbox",
}
SEARCH_V2_SEGMENTS = {
    'scope': 's',
    'terms': 't',
    'mode': 'm',
    'field': 'f',
    'order': 'o',
    'limit': 'n',
    'snippet': 'x',
    'explain': 'e',
    'exact': 'h',
    'not_terms': 'z',
    'type': 'y',
    'owner': 'w',
    'tag': 'g',
    'state': 'a',
    'cursor': 'j',
    'author': 'au',
    'created_after': 'ca',
    'created_before': 'cb',
    'updated_after': 'ua',
    'updated_before': 'ub',
    'has_attachment': 'ha',
    'depth': 'd',
    'recursive': 're',
    'fields': 'fi',
    'facets': 'fc',
}
SEARCH_V3_SEGMENTS = {**SEARCH_V2_SEGMENTS, 'source_kind': 'sk', 'relation_type': 'rt'}
SEARCH_V4_SEGMENTS = {**SEARCH_V3_SEGMENTS, 'suggest': 'sg'}
SEARCH_V5_SEGMENTS = {
    **SEARCH_V4_SEGMENTS,
    'revision': 'rv',
    'source_version': 'sv',
    'relation_to': 'to',
    'relation_from': 'fr',
    'has_replies': 'hr',
    'has_references': 'hf',
    'spell': 'sp',
}
GREP_V1_SEGMENTS = {
    'scope': 's',
    'pattern': 't',
    'regex': 'r',
    'glob': 'g',
    'exclude_glob': 'x',
    'case_sensitive': 'i',
    'before': 'b',
    'after': 'a',
    'max_matches': 'm',
    'max_files': 'f',
    'files_with_matches': 'w',
    'count_only': 'c',
}
TRANSFER_OPERATIONS = frozenset({
    'transfer.open',
    'transfer.part_put',
    'transfer.part_get',
    'transfer.status',
    'transfer.seal',
    'transfer.cancel',
})


class RouteEffect(StrEnum):
    # Preserve the published diagnostic spelling while using modern StrEnum.
    __str__ = Enum.__str__

    PURE_READ = 'PURE_READ'
    LOCAL_EPHEMERAL = 'LOCAL_EPHEMERAL'
    BUSINESS_WRITE = 'BUSINESS_WRITE'
    EXTERNAL_EFFECT = 'EXTERNAL_EFFECT'


@dataclass(frozen=True, slots=True)
class RouteSpec:
    name: str
    effect: RouteEffect


def operation_route(spec):
    effect = {
        'read': RouteEffect.PURE_READ,
        'transaction': RouteEffect.BUSINESS_WRITE,
        'external': RouteEffect.EXTERNAL_EFFECT,
    }[spec.effect]
    return RouteSpec(name=spec.name, effect=effect)


def classify_route(path, method, registry):
    """Classify the matched HTTP boundary before interpreting a request body."""
    if path in {'/-/d', '/-/schema'} or path.startswith('/-/d/'):
        return RouteSpec('contract', RouteEffect.PURE_READ)
    protocol = re.match(r'^/-/([pg])/([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)', path)
    if protocol:
        if path.endswith('/schema'):
            return RouteSpec('operation_schema', RouteEffect.PURE_READ)
        return operation_route(registry.operation(protocol.group(2)))
    if path.startswith('/-/g/'):
        from msg.transports.dictionary import build_dictionary

        code = path.split('/', 4)[3]
        return operation_route(build_dictionary(registry).resolve_operation(code))
    if path in {'/-/graphql', '/-/mcp'}:
        return RouteSpec('dynamic_execution', RouteEffect.EXTERNAL_EFFECT)
    if path == '/-/transfer' and method == 'POST':
        return RouteSpec('transfer_execution', RouteEffect.BUSINESS_WRITE)
    if re.fullmatch(
        r'/-/git/[A-Za-z0-9_-]{1,128}(?:\.git)?/info/lfs/objects/[0-9a-f]{64}/[0-9]+', path
    ):
        return RouteSpec('git.lfs_publish', RouteEffect.EXTERNAL_EFFECT)
    if re.fullmatch(r'/-/git/[A-Za-z0-9_-]{1,128}(?:\.git)?/info/lfs/objects/batch', path):
        return RouteSpec('git.lfs_write_authorize', RouteEffect.PURE_READ)
    if re.fullmatch(r'/-/git/[A-Za-z0-9_-]{1,128}/git-receive-pack', path):
        return operation_route(registry.operation('git.http_receive'))
    if re.fullmatch(r'/-/git/[A-Za-z0-9_-]{1,128}/info/refs', path):
        return operation_route(registry.operation('git.http_advertise'))
    if re.fullmatch(r'/[@&][^/]+/[^/]+\.git/info/lfs/objects/batch', path):
        return operation_route(registry.operation('git.lfs_read_batch'))
    if re.fullmatch(r'/[@&][^/]+/[^/]+\.git/(?:info/refs|git-upload-pack|HEAD)', path):
        return operation_route(registry.operation('git.refs'))
    if path.startswith(('/@', '/&')):
        operation = subject_view_operation(path)
        if operation is None:
            operation = 'discovery.raw' if parse_view(path)[1] == 'raw' else 'discovery.get'
        return operation_route(registry.operation(operation))
    return RouteSpec('read', RouteEffect.PURE_READ)


def passive_client(request):
    agent = request.headers.get('user-agent', '').casefold()
    passive = (
        'bot',
        'crawler',
        'spider',
        'preview',
        'scanner',
        'safebrowsing',
        'facebookexternalhit',
        'slackbot',
        'discordbot',
        'whatsapp',
        'telegrambot',
        'mozilla/',
        'chrome/',
        'chromium/',
        'firefox/',
        'safari/',
        'edg/',
    )
    if any(marker in agent for marker in passive):
        return True
    if any(
        word in request.headers.get(header, '').casefold()
        for header in ('purpose', 'sec-purpose', 'x-moz')
        for word in ('prefetch', 'prerender')
    ):
        return True
    return request.headers.get('sec-fetch-mode', '').casefold() == 'navigate'


def passive_client_response():
    return json_response(
        {'status': 'error', 'error': {'code': 'passive_client_forbidden', 'retryable': False}},
        403,
        headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow'},
    )


def decode_query_path(raw_path, prefix, segments, version=b'1', kind=b'q'):
    parts = raw_path.split(b'/')
    require(len(parts) >= 4 and parts[:4] == [b'', prefix, kind, version], 'invalid_path')
    fields = parts[4:]
    proof = None
    if len(fields) >= 2 and fields[-2] == b'p':
        proof = fields[-1]
        fields = fields[:-2]
    require(len(fields) % 2 == 0 and bool(fields), 'invalid_path')
    names = {code.encode(): name for name, code in segments.items()}
    result = {}
    for code, encoded in zip(fields[::2], fields[1::2], strict=True):
        require(code in names, 'unknown_query_parameter')
        name = names[code]
        require(name not in result, 'duplicate_query_parameter')
        require(
            bool(encoded) and re.search(rb'%(?![0-9A-Fa-f]{2})', encoded) is None, 'invalid_path'
        )
        try:
            value = unquote_to_bytes(encoded).decode('utf-8')
        except UnicodeDecodeError as exc:
            raise Failure('invalid_path') from exc
        require(quote(value, safe=',').encode() == encoded, 'invalid_path')
        result[name] = value
    return result, proof


def compile_read_query(query, service):
    require(
        set(query)
        <= {'root', 'select', 'filter', 'sort', 'first', 'after', 'expand', 'projection'},
        'unknown_query_parameter',
    )
    require(
        query.get('expand', 'none') == 'none' and query.get('projection', 'meta') == 'meta',
        'query_cost_exceeded',
    )
    if 'after' in query:
        require(set(query) == {'after'}, 'cursor_query_mismatch')
        cursor = query['after']
        saved, _ = service.cursors.inspect_page(cursor, service.clock())
        require(saved.get('operation') == 'discovery.read_query', 'cursor_kind_mismatch')
        return 'discovery.read_query', {'cursor': cursor}
    require(bool(query.get('root')), 'read_query_root_required')
    first = query.get('first', '50')
    require(first.isdecimal() and 1 <= int(first) <= 100, 'query_cost_exceeded')
    selected = query.get('select', 'id,type,name,revision,generation,path').split(',')
    require(
        0 < len(selected) <= 10
        and len(set(selected)) == len(selected)
        and set(selected)
        <= {
            'id',
            'type',
            'name',
            'revision',
            'generation',
            'path',
            'created_at',
            'modified_at',
            'owner',
            'group',
            'mode',
        },
        'query_cost_exceeded',
    )
    require(int(first) * (len(selected) + 1) <= 1000, 'query_cost_exceeded')
    sort = query.get('sort', 'id')
    require(sort in {'id', 'time', 'name'}, 'invalid_sort')
    args = {'parent': query['root'], 'limit': int(first), 'fields': selected, 'sort': sort}
    if 'filter' in query:
        match = re.fullmatch(r'type:([a-z][a-z0-9_]*)', query['filter'])
        require(match is not None, 'invalid_read_filter')
        args['type'] = match.group(1)
    return 'discovery.read_query', args


def compile_read_query_v2(query, service):
    require(
        set(query)
        <= {
            'root',
            'select',
            'filter',
            'sort',
            'first',
            'after',
            'expand',
            'nested_first',
            'parent',
            'collection',
            'limit',
            'projection',
            'version',
        },
        'unknown_query_parameter',
    )
    require(
        query.get('version', '2') == '2' and query.get('projection', 'meta') == 'meta',
        'invalid_read_query_version',
    )
    if 'after' in query:
        require(set(query) <= {'after', 'version'}, 'cursor_query_mismatch')
        saved, _ = service.cursors.inspect_page(query['after'], service.clock())
        require(
            saved.get('operation') == 'discovery.read_query'
            and any(
                key in saved.get('arguments', {})
                for key in ('expand', 'collection', 'nested_first')
            ),
            'cursor_kind_mismatch',
        )
        return 'discovery.read_query', {'cursor': query['after']}
    if 'collection' in query:
        require(
            set(query) <= {'version', 'parent', 'collection', 'limit', 'select', 'projection'}
            and bool(query.get('parent'))
            and query['collection'] in {'children', 'replies'},
            'invalid_nested_query',
        )
        limit = query.get('limit', '5')
        require(limit.isdecimal() and 1 <= int(limit) <= 10, 'query_cost_exceeded')
        fields = query.get('select', 'id,name,type,path').split(',')
        require(
            fields
            and len(fields) == len(set(fields))
            and set(fields) <= {'id', 'name', 'type', 'path', 'revision'},
            'query_cost_exceeded',
        )
        return 'discovery.read_query', {
            'parent': query['parent'],
            'collection': query['collection'],
            'limit': int(limit),
            'fields': fields,
        }
    require(bool(query.get('root')) and 'expand' in query, 'invalid_nested_query')
    expanded = query['expand'].split(',')
    require(
        expanded
        and len(expanded) == len(set(expanded))
        and set(expanded) <= {'children', 'replies'},
        'invalid_nested_query',
    )
    first = query.get('first', '10')
    nested_first = query.get('nested_first', '5')
    require(
        first.isdecimal()
        and 1 <= int(first) <= 10
        and nested_first.isdecimal()
        and 1 <= int(nested_first) <= 10
        and int(first) * len(expanded) * (int(nested_first) + 1) <= 100,
        'query_cost_exceeded',
    )
    base = {
        key: value
        for key, value in query.items()
        if key in {'root', 'select', 'filter', 'sort', 'first'}
    }
    base['first'] = first
    operation, args = compile_read_query(base, service)
    args.update(expand=expanded, nested_first=int(nested_first))
    return operation, args


def compile_read_query_v3(query, service):
    require(
        set(query) <= {'version', 'root', 'select', 'filter', 'sort', 'first', 'after', 'tree'},
        'unknown_query_parameter',
    )
    require(query.get('version', '3') == '3', 'invalid_read_query_version')
    if 'after' in query:
        require(set(query) <= {'version', 'after'}, 'cursor_query_mismatch')
        saved, _ = service.cursors.inspect_page(query['after'], service.clock())
        require(
            saved.get('operation') == 'discovery.read_query'
            and read_query_version(saved.get('arguments', {})) == 3,
            'cursor_kind_mismatch',
        )
        return 'discovery.read_query', {'cursor': query['after']}
    base = {key: value for key, value in query.items() if key not in {'version', 'tree'}}
    operation, args = compile_read_query(base, service)
    args['query_version'] = 3
    if 'tree' in query:
        args['expand'] = loads(query['tree'])
    service.registry.validate(service.registry.operation(operation, 3).input_schema, args)
    return operation, args


def decode_read_query_path(raw_path, version=b'1'):
    prefix = raw_path.split(b'/', 3)[1]
    segments = READ_QUERY_V2_SEGMENTS if version == b'2' else READ_QUERY_V1_SEGMENTS
    values, proof = decode_query_path(raw_path, prefix, segments, version)
    sort = {code: name for name, code in READ_QUERY_V1_SORT.items()}
    fields = {code: name for name, code in READ_QUERY_V1_FIELDS.items()}
    query = {}
    if 'root' in values:
        query['root'] = values['root']
    if 'type' in values:
        query['filter'] = 'type:' + values['type']
    if 'sort' in values:
        require(values['sort'] in sort, 'invalid_sort')
        query['sort'] = sort[values['sort']]
    if 'fields' in values:
        codes = values['fields'].split(',')
        require(all(code in fields for code in codes), 'query_cost_exceeded')
        query['select'] = ','.join(fields[code] for code in codes)
    if 'first' in values:
        query['first'] = values['first']
    if 'after' in values:
        query['after'] = values['after']
    if version == b'2':
        for name in ('expand', 'nested_first', 'collection', 'parent', 'limit'):
            if name in values:
                query[name] = values[name]
    return query, proof


def decode_search_query_path(raw_path):
    prefix = raw_path.split(b'/', 3)[1]
    return decode_query_path(raw_path, prefix, SEARCH_QUERY_V1_SEGMENTS)


def compile_lexical_search(query):
    require(
        set(query)
        <= {
            'scope',
            'terms',
            'exact',
            'not_terms',
            'mode',
            'field',
            'type',
            'owner',
            'author',
            'tag',
            'state',
            'created_after',
            'created_before',
            'updated_after',
            'updated_before',
            'has_attachment',
            'order',
            'limit',
            'cursor',
            'snippet',
            'explain',
            'fields',
            'facets',
            'source_kind',
            'relation_type',
            'suggest',
            'revision',
            'source_version',
            'relation_to',
            'relation_from',
            'has_replies',
            'has_references',
            'spell',
            'depth',
            'recursive',
        },
        'unknown_query_parameter',
    )
    if 'cursor' in query:
        require(set(query) == {'cursor'}, 'cursor_query_mismatch')
        return {'cursor': query['cursor']}
    require(bool(query.get('scope')), 'search_scope_required')
    args = dict(query)
    if args.get('scope', '').startswith('{'):
        require(len(args['scope'].encode('utf-8')) <= 8192, 'query_cost_exceeded')
        args['scope'] = loads(args['scope'])
    if 'source_version' in args:
        require(
            args['source_version'].isascii()
            and args['source_version'].isdecimal()
            and len(args['source_version']) <= 10
            and 1 <= int(args['source_version']) <= 2147483647,
            'invalid_search_source_version',
        )
        args['source_version'] = int(args['source_version'])
    for name in ('limit', 'depth'):
        if name in args:
            require(
                args[name].isdecimal()
                and (0 <= int(args[name]) <= 5 if name == 'depth' else 1 <= int(args[name]) <= 100),
                'query_cost_exceeded',
            )
            args[name] = int(args[name])
    for name in (
        'snippet',
        'has_attachment',
        'recursive',
        'suggest',
        'has_replies',
        'has_references',
        'spell',
    ):
        if name in args:
            require(args[name] in {'0', '1'}, 'invalid_search_flag')
            args[name] = args[name] == '1'
    if 'fields' in args:
        args['fields'] = args['fields'].split(',')
    if 'facets' in args:
        facets = args['facets'].split(',')
        require(
            facets
            and len(facets) == len(set(facets))
            and all(name in {'type', 'tag'} for name in facets),
            'invalid_search_facets',
        )
        args['facets'] = facets
    return args


def decode_search_v2_path(raw_path, version=b'2'):
    prefix = raw_path.split(b'/', 3)[1]
    segments = (
        SEARCH_V5_SEGMENTS
        if version == b'5'
        else SEARCH_V4_SEGMENTS
        if version == b'4'
        else SEARCH_V3_SEGMENTS
        if version == b'3'
        else SEARCH_V2_SEGMENTS
    )
    values, proof = decode_query_path(raw_path, prefix, segments, version)
    modes = {'a': 'all', 'n': 'any'}
    require(
        version == b'5'
        or (
            not values.get('scope', '').startswith('{')
            and 'title' not in values.get('fields', '').split(',')
            and values.get('relation_type') not in SEARCH_V5_RELATIONS
        ),
        'invalid_search_scope',
    )
    fields = {
        'a': 'all',
        'b': 'body',
        'n': 'name',
        'm': 'metadata',
        **({'t': 'title'} if version == b'5' else {}),
    }
    order = {'r': 'relevance', 'u': 'updated', 'c': 'created', 'n': 'name'}
    if 'mode' in values:
        require(values['mode'] in modes, 'invalid_search_mode')
        values['mode'] = modes[values['mode']]
    if 'field' in values:
        require(values['field'] in fields, 'invalid_search_field')
        values['field'] = fields[values['field']]
    if 'order' in values:
        require(values['order'] in order, 'invalid_search_order')
        values['order'] = order[values['order']]
    if 'explain' in values:
        require(values['explain'] == 'c', 'invalid_search_explain')
        values['explain'] = 'compact'
    if 'snippet' in values:
        require(values['snippet'] in {'0', '1', 'c'}, 'invalid_search_flag')
        values['snippet'] = '1' if values['snippet'] == 'c' else values['snippet']
    return values, proof


def search_path_from_args(args):
    parts = ['/_s/q/1']
    for name, code in SEARCH_QUERY_V1_SEGMENTS.items():
        if name in args and args[name] not in {None, ''}:
            parts.extend((code, quote(str(args[name]), safe=',')))
    return '/'.join(parts)


def path_read_proof(encoded, operation, args, service, limit):
    packet = path_packet(encoded.decode('ascii'), 'j', limit)
    require(isinstance(packet.proof, SignatureProof), 'path_signature_required')
    require(
        packet.operation == operation and canonical(packet.arguments) == canonical(args),
        'representation_mismatch',
    )
    require(
        packet.expires_at is not None
        and 0 < (packet.expires_at - service.clock()).total_seconds() <= 60,
        'path_proof_expiry',
    )
    return packet


def thread_summary(item, budget=20):
    """Prefer the authored summary; otherwise expose a bounded body prefix."""
    if item.get('summary'):
        return {'summary': item['summary']}
    content = item.get('content', '')
    if isinstance(content, dict) and 'template_id' in content and 'values' in content:
        from msg.core.template_dsl import render_values

        content = render_values(content['values'])
    elif not isinstance(content, str):
        content = canonical(content).decode()
    truncated = len(content) > budget
    return {'summary': content[:budget] + ('…' if truncated else '')}


def post_title(item):
    title = item.get('name', '').removesuffix('.md')
    # An automatically generated path name is not an authored title.
    return {'title': title} if title and not re.fullmatch(r'p_[0-9a-f]{32}', title) else {}


def describe_resource(data):
    if data.get('type') == 'user' and 'profile' in data:
        from msg.plugins.profile import profile_markdown

        return profile_markdown(data)
    if data.get('type') == 'post' and 'content' in data:
        rid = hex_id(data['id'])
        base = '/*' + rid
        revision = hex_id(data['revision']) if data.get('revision') else ''
        links = [
            f'[meta]({base}/meta)',
            f'[history]({base}/history)',
            f'[references]({base}/references)',
            f'[thread]({base}/thread)',
        ]
        if 'd' in data.get('links', {}):
            links.append(f'[diff]({base}/diff)')
        content = data['content']
        if isinstance(content, dict) and 'template_id' in content and 'values' in content:
            from msg.core.template_dsl import render_values

            content = render_values(content['values'])
        elif not isinstance(content, str):
            content = '```json\n' + canonical(content).decode() + '\n```'
        metadata = {
            'id': rid,
            'revision': revision,
            **post_title(data),
            **({'summary': data['summary']} if data.get('summary') else {}),
            'date': data.get('created_at', ''),
            'updated': data.get('revision_created_at', data.get('modified_at', '')),
        }
        if data.get('tags'):
            metadata['tags'] = data['tags']
        for rel, key in (('a', 'author'), ('t', 'channel')):
            link = data.get('links', {}).get(rel)
            if link and link.get('path'):
                metadata[key] = link['path']
        front_matter = (
            '---\n'
            + ''.join(f'{key}: {canonical(value).decode()}\n' for key, value in metadata.items())
            + '---\n\n'
        )
        return front_matter + content.rstrip('\n') + '\n\n' + ' · '.join(links) + '\n'
    if 'content' in data:
        content = data['content']
        if isinstance(content, str):
            links = data.get('links', {})
            navigation = ' '.join(
                f'[{rel}]({link["path"]})'
                for rel, link in links.items()
                if isinstance(link, dict) and link.get('path')
            )
            return content.rstrip('\n') + '\n\n' + navigation + '\n' if navigation else content
        from msg.core.template_dsl import render_values

        if isinstance(content, dict) and 'template_id' in content and 'values' in content:
            return render_values(content['values'])
        return '```json\n' + canonical(content).decode() + '\n```\n'
    title = data.get('name', data.get('id', 'msg.lmm.best'))
    output = ['# ' + str(title), '']
    for item in data.get('items', []):
        name = str(item.get('name', item['id'])).replace('[', '\\[').replace(']', '\\]')
        link = (
            '/*' + hex_id(item['id'])
            if item.get('type') == 'post'
            else item.get('path', '/_id/' + item['id'])
        )
        label = f'\\*{hex_id(item["id"])} — {name}' if item.get('type') == 'post' else name
        output.append(f'- [{label}]({link})')
    if 'items' not in data:
        output += ['```json', canonical(data).decode(), '```']
    if data.get('list_operation'):
        output += ['', '[List operation](/-/d/discovery.list)']
    return '\n'.join(output) + '\n'


def parse_view(path):
    parts = path.strip('/').split('/')
    view = 'markdown'
    revision = None
    if parts and parts[-1] in {'json', 'meta', 'raw', 'history'}:
        view = parts.pop()
    if len(parts) >= 2 and parts[-2] == 'revisions':
        revision = parts.pop()
        parts.pop()
    return '/' + ('/'.join(parts)), view, revision


def parse_post_view(path, raw_path):
    if not any(part.startswith('*') for part in path.split('/')):
        return None
    match = re.fullmatch(
        r'(?P<scope>(?:/[^/*]+)*)/\*(?P<id>[A-Za-z0-9_.:-]{1,160})'
        r'(?:/(?P<view>json|meta|raw|history|references|thread|diff)'
        r'|/rev/(?P<rev>[A-Za-z0-9_.:-]{1,160})'
        r'|/diff/(?P<old>[A-Za-z0-9_.:-]{1,160})(?:/(?P<new>[A-Za-z0-9_.:-]{1,160}))?)?',
        path,
    )
    require(match is not None, 'not_found')
    # IDs have one spelling. Encoded scope names remain supported.
    require(b'%' not in raw_path.split(b'*')[-1] and b'*' in raw_path, 'not_found')
    result = match.groupdict()
    if result['old']:
        result['view'] = 'diff'
    return result


def parse_stable_view(path, raw_path):
    if not path.startswith(('/_r/', '/_read/')):
        return None
    # Stable ID paths are ASCII identifiers, with no alternate percent spelling.
    try:
        require(raw_path.decode('ascii') == path and b'%' not in raw_path, 'not_found')
    except UnicodeDecodeError as exc:
        raise Failure('not_found') from exc
    match = re.fullmatch(r'/_r(?:ead)?/([A-Za-z0-9_.:-]{1,160})/(json|meta|raw|history)', path)
    if match:
        rid, view = match.groups()
        return '/_id/' + rid, view, None
    match = re.fullmatch(r'/_r(?:ead)?/([A-Za-z0-9_.:-]{1,160})/rev/([A-Za-z0-9_.:-]{1,160})', path)
    require(match is not None, 'not_found')
    rid, revision = match.groups()
    return '/_id/' + rid, 'markdown', revision


def create_app(service):
    mcp = MCPServer(service)
    graphql_adapter = None
    short_codes = None

    @asynccontextmanager
    async def lifespan(app):
        try:
            if not service._loaded:
                await service.load()
            yield
        finally:
            await service.close()

    async def dispatch(request: Request):
        nonlocal graphql_adapter, short_codes
        raw_document = request.query_params.get('format') == 'raw'

        async def browser_account():
            browser = request.scope.get('state', {}).get('msg_browser_credentials')
            if not browser:
                return None
            identity = await execute_packet(
                request_for(
                    'discovery.get',
                    {'id': browser[0], 'fields': ['id', 'name', 'groups']},
                    service.settings.service_url,
                    source='manual',
                )
            )
            return identity.data if not identity.error else None

        async def execute_packet(packet, *, entry='network'):
            browser = request.scope.get('state', {}).get('msg_browser_credentials')
            if not browser:
                return await service.executor.execute(packet, entry=entry)
            spec = service.registry.operation(packet.operation, packet.contract_version)
            if (
                browser
                and request.method in {'GET', 'HEAD'}
                and not request.url.path.startswith('/-/')
                and spec.effect == 'read'
                and not spec.name.startswith(('identity.', 'root.', 'system.'))
                and not spec.anonymous_only
                and packet.proof is None
                and packet.subject is None
            ):
                packet = request_for(
                    packet.operation,
                    packet.arguments,
                    packet.target_service,
                    subject=browser[0],
                    token=(browser[1], browser[2]),
                    contract_version=packet.contract_version,
                    source=packet.source,
                    request_id=packet.request_id,
                    expected=packet.expected_generations,
                    return_fields=packet.return_fields,
                    expires_at=service.clock() + timedelta(minutes=3),
                )
            return await service.executor.execute(packet, entry=entry)

        try:
            if 'format' in request.query_params:
                require(
                    request.method in {'GET', 'HEAD'} and not request.url.path.startswith('/-/'),
                    'unknown_query_parameter',
                )
                require(
                    raw_document and request.query_params.getlist('format') == ['raw'],
                    'unknown_query_parameter',
                )
                remaining = [
                    (key, value)
                    for key, value in request.query_params.multi_items()
                    if key != 'format'
                ]
                scope = {
                    **request.scope,
                    'query_string': urlencode(remaining).encode(),
                    'headers': [
                        (key, value)
                        for key, value in request.scope['headers']
                        if key.lower() != b'accept'
                    ]
                    + [(b'accept', b'text/markdown')],
                }
                request = Request(scope, request.receive)
            limits = service.settings.server.limits
            raw_path = request.scope.get('raw_path') or request.scope['path'].encode('utf-8')
            require_safe_request_target(
                raw_path, request.scope.get('query_string', b''), maximum=limits.max_path_bytes
            )
            expected = require_matching_host(
                request.headers.getlist('host'),
                urlsplit(service.settings.service_url),
                aliases=getattr(service.settings, 'service_aliases', ()),
            )
            require(
                'x-http-method-override' not in request.headers
                and 'x-method-override' not in request.headers,
                'method_not_allowed',
            )
            subject_route = None
            if request.url.path.startswith(('/@', '/&')) and request.method in {'GET', 'HEAD'}:
                # Hosting probes this namespace before the ordinary router.
                # Fence the selected operation before any business query.
                subject_route = classify_route(request.url.path, request.method, service.registry)
                require(subject_route.effect == RouteEffect.PURE_READ, 'effect_mismatch')
            if request.url.path.startswith(('/@', '/&')):
                from msg.extensions.hosting import serve_hosted

                hosted = await serve_hosted(service, request)
                if hosted is not None:
                    return hosted
            origins = request.headers.getlist('origin')
            if origins:
                require(
                    len(origins) == 1
                    and origins[0].rstrip('/') == f'{expected.scheme}://{expected.netloc}',
                    'forbidden_origin',
                )
            path = request.url.path
            native = re.fullmatch(r'(/[@&][^/]+/[^/]+\.git)/(.*)', path)
            if native:
                require(service.registry.operation('git.refs').effect == 'read', 'effect_mismatch')
                from msg.transports.git_http import GitHTTPAdapter

                if native.group(2).startswith('info/lfs/'):
                    require(
                        service.registry.operation('git.lfs_read').effect == 'read'
                        and service.registry.operation('git.lfs_read_batch').effect == 'read',
                        'effect_mismatch',
                    )
                    return await GitHTTPAdapter(service).http_lfs(
                        request, native.group(1), native.group(2)[9:]
                    )
                return await GitHTTPAdapter(service).http(request, native.group(1), native.group(2))
            # git-lfs derives <remote>.git/info/lfs even when the advertised
            # push URL is /-/git/<id>; both spellings remain inside /-/.
            lfs_write = re.fullmatch(
                r'/-/git/([A-Za-z0-9_-]{1,128})(?:\.git)?/info/lfs/(objects(?:/batch|/[0-9a-f]{64}/[0-9]+))',
                path,
            )
            if lfs_write:
                require(raw_path == path.encode('ascii'), 'not_found')
                from msg.transports.git_http import GitHTTPAdapter

                return await GitHTTPAdapter(service).http_lfs(
                    request, *lfs_write.groups(), write=True
                )
            git_push = re.fullmatch(
                r'/-/git/([A-Za-z0-9_-]{1,128})/(info/refs|git-receive-pack)', path
            )
            if git_push:
                require(raw_path == path.encode('ascii'), 'not_found')
                from msg.transports.git_http import GitHTTPAdapter

                return await GitHTTPAdapter(service).http_push(request, *git_push.groups())
            if path.startswith(('/!', '/~', '/run/j/', '/run/gz/')) or path == '/mcp':
                raise Failure('not_found')
            if request.method == 'OPTIONS':
                return Response(status_code=405, headers=BASE_HEADERS)
            if path == '/feed':
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                operation = 'discovery.recommendations'
                require(service.registry.operation(operation).effect == 'read', 'effect_mismatch')
                pairs = request.query_params.multi_items()
                query = dict(pairs)
                require(len(pairs) == len(query), 'duplicate_query_parameter')
                require(set(query) <= {'limit', 'interests'}, 'unknown_query_parameter')
                args = {}
                if 'limit' in query:
                    require(query['limit'].isdecimal(), 'invalid_limit')
                    args['limit'] = int(query['limit'])
                if 'interests' in query:
                    args['interests'] = query['interests'].split(',')
                header = request.headers.get('x-msg-request')
                if header:
                    packet = path_packet(header, 'j', limits.max_request_bytes)
                    require(
                        packet.operation == operation
                        and canonical(packet.arguments) == canonical(args),
                        'representation_mismatch',
                    )
                else:
                    packet = request_for(operation, args, service.settings.service_url)
                result = await execute_packet(packet, entry='network')
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                payload = canonical(wire(result.data))
                require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/json',
                    headers={
                        **BASE_HEADERS,
                        'Cache-Control': 'no-store',
                        'Content-Length': str(len(payload)),
                    },
                )
            if path in {'/rss', '/rss.xml', '/-/rss'}:
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                require(
                    service.registry.operation('discovery.feed').effect == 'read', 'effect_mismatch'
                )
                from msg.extensions.rss import render_feed

                args = dict(request.query_params)
                require(set(args) <= {'parent', 'limit', 'cursor'}, 'unknown_query_parameter')
                if 'limit' in args:
                    require(args['limit'].isdecimal(), 'invalid_limit')
                    args['limit'] = int(args['limit'])
                header = request.headers.get('x-msg-request')
                packet = (
                    path_packet(header, 'j', limits.max_request_bytes)
                    if header
                    else request_for('discovery.feed', args, service.settings.service_url)
                )
                require(
                    packet.operation == 'discovery.feed' and dict(packet.arguments) == args,
                    'representation_mismatch',
                )
                result = await execute_packet(packet)
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                # Only the canonical anonymous feed is pushed to public hubs.
                hubs = service.settings.websub_hubs if not args and not header else ()
                self_url = (
                    service.settings.service_url + '/rss.xml' if not args and not header else None
                )
                payload = render_feed(result.data, hubs=hubs, self_url=self_url)
                websub_headers = {}
                if self_url:
                    websub_headers['Link'] = ', '.join([
                        f'<{self_url}>; rel="self"',
                        *(f'<{hub}>; rel="hub"' for hub in hubs),
                    ])
                require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/rss+xml',
                    headers={
                        **BASE_HEADERS,
                        'Content-Length': str(len(payload)),
                        'Cache-Control': 'private, no-cache',
                        **websub_headers,
                    },
                )
            if path.startswith('/latest/'):
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                require(
                    service.registry.operation('discovery.list').effect == 'read', 'effect_mismatch'
                )
                kind = path.removeprefix('/latest/')
                service.registry.resource_type(kind, 1)
                packet = request_for(
                    'discovery.list',
                    {'type': kind, 'sort': 'time', 'direction': 'desc', 'limit': 1},
                    service.settings.service_url,
                )
                result = await execute_packet(packet)
                return json_response(
                    result_wire(result), error_status(result.error.code) if result.error else 200
                )
            if path == '/healthz':
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                return json_response(
                    {'status': 'ok' if service._loaded else 'not_ready'},
                    200 if service._loaded else 503,
                )
            sync_path = re.fullmatch(
                rb'/_r(?:ead)?/s/(start|[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)'
                rb'(?:/p/([A-Za-z0-9_-]+))?',
                raw_path,
            )
            if sync_path:
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                require(not request.url.query, 'unknown_query_parameter')
                cursor = sync_path.group(1).decode('ascii')
                args = {} if cursor == 'start' else {'cursor': cursor}
                operation = 'communication.sync'
                proof = sync_path.group(2)
                header = request.headers.get('x-msg-request')
                require(not (proof and header), 'ambiguous_proof')
                if proof:
                    packet = path_read_proof(
                        proof, operation, args, service, limits.max_request_bytes
                    )
                elif header:
                    packet = path_packet(header, 'j', limits.max_request_bytes)
                    require(
                        packet.operation == operation
                        and canonical(packet.arguments) == canonical(args),
                        'representation_mismatch',
                    )
                else:
                    packet = request_for(
                        operation, args, service.settings.service_url, source='manual'
                    )
                result = await execute_packet(packet, entry='network')
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                value = wire(result.data)
                payload = canonical(value)
                require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                etag = '"' + digest(value)[7:] + '"'
                headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'no-store'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers=headers)
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/json',
                    headers=headers,
                )
            query_ref_path = re.fullmatch(
                rb'/_r(?:ead)?/q/([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)'
                rb'(?:/c/([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+))?'
                rb'(?:/p/([A-Za-z0-9_-]+))?',
                raw_path,
            )
            if query_ref_path:
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                require(not request.url.query, 'unknown_query_parameter')
                token = query_ref_path.group(1).decode('ascii')
                args = {'query_ref': token}
                if query_ref_path.group(2):
                    args['cursor'] = query_ref_path.group(2).decode('ascii')
                operation = 'transfer.query_get'
                proof = query_ref_path.group(3)
                header = request.headers.get('x-msg-request')
                require(not (proof and header), 'ambiguous_proof')
                if proof:
                    packet = path_read_proof(
                        proof, operation, args, service, limits.max_request_bytes
                    )
                elif header:
                    packet = path_packet(header, 'j', limits.max_request_bytes)
                    require(
                        packet.operation == operation
                        and canonical(packet.arguments) == canonical(args),
                        'representation_mismatch',
                    )
                else:
                    packet = request_for(
                        operation, args, service.settings.service_url, source='manual'
                    )
                result = await execute_packet(packet, entry='network')
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                value = wire(result.data)
                payload = canonical(value)
                require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                etag = '"' + digest(value)[7:] + '"'
                headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'no-store'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers=headers)
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/json',
                    headers=headers,
                )
            link_path = raw_path
            link_proof = None
            link_parts = raw_path.split(b'/')
            if (
                len(link_parts) >= 2
                and link_parts[-2] == b'p'
                and raw_path.startswith((b'/_read/', b'/_r/'))
            ):
                link_path = b'/'.join(link_parts[:-2])
                link_proof = link_parts[-1]
            link_view = re.fullmatch(
                rb'/_r(?:ead)?/([A-Za-z0-9_.:-]{1,160})/'
                rb'(links|l/(self|[a-z])|diff/([A-Za-z0-9_.:-]{1,160})'
                rb'(?:/([A-Za-z0-9_.:-]{1,160}))?(?:/o/([0-9]+))?)',
                link_path,
            )
            if link_view:
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                rid = link_view.group(1).decode('ascii')
                rel = link_view.group(3).decode('ascii') if link_view.group(3) else None
                old = link_view.group(4).decode('ascii') if link_view.group(4) else None
                new = link_view.group(5).decode('ascii') if link_view.group(5) else None
                offset = link_view.group(6).decode('ascii') if link_view.group(6) else None
                pairs = request.query_params.multi_items()
                require(len(pairs) == len({key for key, _ in pairs}), 'duplicate_query_parameter')
                query = dict(pairs)
                if old is not None or rel == 'd':
                    require(not query, 'unknown_query_parameter')
                    operation = 'discovery.diff_view'
                    args = {'id': rid}
                    if rel == 'd':
                        args['previous'] = True
                    elif new is None:
                        args['known_revision'] = old
                    else:
                        args.update(old_revision=old, new_revision=new)
                    if offset is not None:
                        require(new is not None, 'invalid_diff_range')
                        args['offset'] = int(offset)
                else:
                    require(
                        rel is None
                        or rel in {'self', 't', 'a', 'r', 'p', 'c', 'f', 'q', 'b', 'h', 'v'},
                        'unknown_link_relation',
                    )
                    require(
                        set(query) <= ({'limit'} if rel in {'c', 'f', 'q', 'b', 'h'} else set()),
                        'unknown_query_parameter',
                    )
                    operation = 'discovery.links'
                    args = {'id': rid}
                    if rel is not None:
                        args['rel'] = rel
                    if 'limit' in query:
                        require(
                            query['limit'].isdecimal() and 1 <= int(query['limit']) <= 100,
                            'invalid_limit',
                        )
                        args['limit'] = int(query['limit'])
                header = request.headers.get('x-msg-request')
                require(not (header and link_proof), 'ambiguous_proof')
                if link_proof is not None:
                    packet = path_read_proof(
                        link_proof, operation, args, service, limits.max_request_bytes
                    )
                elif header:
                    packet = path_packet(header, 'j', limits.max_request_bytes)
                    require(
                        packet.operation == operation
                        and canonical(packet.arguments) == canonical(args),
                        'representation_mismatch',
                    )
                else:
                    packet = request_for(
                        operation, args, service.settings.service_url, source='manual'
                    )
                result = await execute_packet(packet, entry='network')
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                value = wire(result.data)
                payload = canonical(value)
                require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                etag = '"' + digest(value)[7:] + '"'
                headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'no-store'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers=headers)
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/json',
                    headers=headers,
                )
            segment = re.fullmatch(rb'/_r(?:ead)?/([A-Za-z0-9_.:-]{1,160})/read', raw_path)
            if segment:
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                pairs = request.query_params.multi_items()
                require(len(pairs) == len({key for key, _ in pairs}), 'duplicate_query_parameter')
                query = dict(pairs)
                require(set(query) <= {'max_bytes'}, 'unknown_query_parameter')
                args = {'id': segment.group(1).decode('ascii')}
                if 'max_bytes' in query:
                    require(
                        query['max_bytes'].isdecimal() and 32 <= int(query['max_bytes']) <= 8192,
                        'query_cost_exceeded',
                    )
                    args['max_bytes'] = int(query['max_bytes'])
                operation = 'discovery.read_segment'
                header = request.headers.get('x-msg-request')
                if header:
                    packet = path_packet(header, 'j', limits.max_request_bytes)
                    require(
                        packet.operation == operation
                        and canonical(packet.arguments) == canonical(args),
                        'representation_mismatch',
                    )
                else:
                    packet = request_for(
                        operation, args, service.settings.service_url, source='manual'
                    )
                result = await execute_packet(packet, entry='network')
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                payload = canonical(wire(result.data))
                require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/json',
                    headers=BASE_HEADERS,
                )
            path_query_v1 = raw_path.startswith((b'/_read/q/1/', b'/_r/q/1/'))
            path_query_v2 = raw_path.startswith((b'/_read/q/2/', b'/_r/q/2/'))
            path_query_v3 = raw_path.startswith((b'/_read/q/3/', b'/_r/q/3/'))
            path_query = path_query_v1 or path_query_v2 or path_query_v3
            if (
                path in {'/_read/query', '/_r/query'}
                or path_query
                or raw_path.startswith((b'/_read/c/', b'/_r/c/'))
            ):
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                continuation = raw_path.startswith((b'/_read/c/', b'/_r/c/'))
                contract_version = 1
                require(
                    not (continuation or path_query) or not request.url.query,
                    'unknown_query_parameter',
                )
                path_proof = None
                if continuation:
                    segments = raw_path.split(b'/')
                    require(
                        len(segments) in {4, 6}
                        and segments[2] == b'c'
                        and re.fullmatch(rb'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', segments[3])
                        is not None,
                        'invalid_cursor',
                    )
                    if len(segments) == 6:
                        require(segments[4] == b'p', 'invalid_path')
                        path_proof = segments[5]
                    cursor = segments[3].decode('ascii')
                    try:
                        kind = service.cursors.inspect(cursor).get('kind')
                        if kind == 'read-segment':
                            service.cursors.inspect_read(cursor, service.clock())
                            operation = 'discovery.read_segment'
                        else:
                            query, _ = service.cursors.inspect_page(cursor, service.clock())
                            operation = query.get('operation')
                            require(
                                operation
                                in {
                                    'discovery.read_query',
                                    'discovery.links',
                                    'discovery.lexical_search',
                                    'communication.following',
                                },
                                'cursor_kind_mismatch',
                            )
                            if operation == 'discovery.lexical_search':
                                saved_args = query.get('arguments', {})
                                contract_version = search_query_version(saved_args)
                            elif operation == 'discovery.read_query':
                                contract_version = read_query_version(query.get('arguments', {}))
                    except Failure as exc:
                        if exc.code == 'invalid_base64':
                            raise Failure('invalid_cursor') from exc
                        raise
                    args = {'cursor': cursor}
                else:
                    if path_query_v3:
                        args, path_proof = decode_read_tree_path(raw_path)
                        query = {'version': '3'}
                    elif path_query:
                        query, path_proof = decode_read_query_path(
                            raw_path, b'2' if path_query_v2 else b'1'
                        )
                    else:
                        pairs = request.query_params.multi_items()
                        require(
                            len(pairs) == len({key for key, _ in pairs}),
                            'duplicate_query_parameter',
                        )
                        query = dict(pairs)
                    contract_version = (
                        3
                        if path_query_v3 or query.get('version') == '3'
                        else 2
                        if path_query_v2 or query.get('version') == '2'
                        else 1
                    )
                    if path_query_v3:
                        operation = 'discovery.read_query'
                    else:
                        compiler = {
                            1: compile_read_query,
                            2: compile_read_query_v2,
                            3: compile_read_query_v3,
                        }[contract_version]
                        operation, args = compiler(query, service)
                require(
                    service.registry.operation(operation, contract_version).effect == 'read',
                    'effect_mismatch',
                )
                header = request.headers.get('x-msg-request')
                require(not (header and path_proof), 'ambiguous_proof')
                if path_proof is not None:
                    packet = path_read_proof(
                        path_proof, operation, args, service, limits.max_request_bytes
                    )
                elif header:
                    packet = path_packet(header, 'j', limits.max_request_bytes)
                    require(
                        packet.operation == operation
                        and canonical(packet.arguments) == canonical(args),
                        'representation_mismatch',
                    )
                else:
                    packet = request_for(
                        operation,
                        args,
                        service.settings.service_url,
                        source='manual',
                        contract_version=contract_version,
                    )
                require(packet.contract_version == contract_version, 'representation_mismatch')
                result = await execute_packet(packet, entry='network')
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                value = wire(result.data)
                payload = canonical(value)
                require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                etag = '"' + digest(value)[7:] + '"'
                headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'no-store'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers=headers)
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/json',
                    headers=headers,
                )
            grep_path = raw_path.startswith((b'/_search/g/1/', b'/_s/g/1/'))
            if path in {'/_search/grep', '/_s/grep'} or grep_path:
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                if grep_path:
                    require(not request.url.query, 'unknown_query_parameter')
                    prefix = raw_path.split(b'/', 3)[1]
                    query, path_proof = decode_query_path(
                        raw_path, prefix, GREP_V1_SEGMENTS, b'1', b'g'
                    )
                else:
                    pairs = request.query_params.multi_items()
                    require(
                        len(pairs) == len({key for key, _ in pairs}), 'duplicate_query_parameter'
                    )
                    query = dict(pairs)
                    path_proof = None
                require(
                    set(query)
                    <= {
                        'scope',
                        'pattern',
                        'regex',
                        'glob',
                        'exclude_glob',
                        'case_sensitive',
                        'before',
                        'after',
                        'max_matches',
                        'max_files',
                        'files_with_matches',
                        'count_only',
                    },
                    'unknown_query_parameter',
                )
                require(
                    bool(query.get('scope')) and bool(query.get('pattern')),
                    'grep_scope_and_pattern_required',
                )
                args = dict(query)
                for name in ('regex', 'case_sensitive', 'files_with_matches', 'count_only'):
                    if name in args:
                        require(args[name] in {'0', '1'}, 'invalid_grep_flag')
                        args[name] = args[name] == '1'
                for name in ('before', 'after', 'max_matches', 'max_files'):
                    if name in args:
                        require(args[name].isdecimal(), 'invalid_grep_limit')
                        args[name] = int(args[name])
                operation = 'discovery.grep'
                header = request.headers.get('x-msg-request')
                require(not (header and path_proof), 'ambiguous_proof')
                if path_proof is not None:
                    packet = path_read_proof(
                        path_proof, operation, args, service, limits.max_request_bytes
                    )
                elif header:
                    packet = path_packet(header, 'j', limits.max_request_bytes)
                    require(
                        packet.operation == operation
                        and canonical(packet.arguments) == canonical(args),
                        'representation_mismatch',
                    )
                else:
                    packet = request_for(
                        operation, args, service.settings.service_url, source='manual'
                    )
                result = await execute_packet(packet, entry='network')
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                value = wire(result.data)
                etag = '"' + digest(value)[7:] + '"'
                headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'no-store'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers=headers)
                return Response(
                    b'' if request.method == 'HEAD' else canonical(value),
                    media_type='application/json',
                    headers=headers,
                )
            search_path = raw_path.startswith((b'/_search/q/1/', b'/_s/q/1/'))
            lexical_path_v2 = raw_path.startswith((b'/_search/q/2/', b'/_s/q/2/'))
            lexical_path_v3 = raw_path.startswith((b'/_search/q/3/', b'/_s/q/3/'))
            lexical_path_v4 = raw_path.startswith((b'/_search/q/4/', b'/_s/q/4/'))
            lexical_path_v5 = raw_path.startswith((b'/_search/q/5/', b'/_s/q/5/'))
            lexical_path = lexical_path_v2 or lexical_path_v3 or lexical_path_v4 or lexical_path_v5
            if (
                path in {'/_search', '/_s'}
                or search_path
                or lexical_path
                or raw_path.startswith((b'/_index/by-tag/', b'/_i/by-tag/'))
            ):
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                if search_path or lexical_path:
                    require(not request.url.query, 'unknown_query_parameter')
                    query, path_proof = (
                        decode_search_v2_path(
                            raw_path,
                            b'5'
                            if lexical_path_v5
                            else b'4'
                            if lexical_path_v4
                            else b'3'
                            if lexical_path_v3
                            else b'2',
                        )
                        if lexical_path
                        else decode_search_query_path(raw_path)
                    )
                else:
                    pairs = request.query_params.multi_items()
                    require(
                        len(pairs) == len({key for key, _ in pairs}), 'duplicate_query_parameter'
                    )
                    query = dict(pairs)
                    path_proof = None
                is_index = raw_path.startswith((b'/_index/by-tag/', b'/_i/by-tag/'))
                lexical = lexical_path or (
                    not is_index
                    and bool(
                        set(query)
                        & {
                            'terms',
                            'exact',
                            'not_terms',
                            'scope',
                            'mode',
                            'field',
                            'order',
                            'snippet',
                            'explain',
                            'has_attachment',
                            'facets',
                            'source_kind',
                            'relation_type',
                            'suggest',
                            'revision',
                            'source_version',
                            'relation_to',
                            'relation_from',
                            'has_replies',
                            'has_references',
                            'spell',
                        }
                    )
                )
                if lexical:
                    args = compile_lexical_search(query)
                    operation = 'discovery.lexical_search'
                else:
                    require(
                        set(query)
                        <= (
                            {'limit', 'cursor'} if is_index else {'tag', 'query', 'limit', 'cursor'}
                        ),
                        'unknown_query_parameter',
                    )
                    limit = query.get('limit', '50')
                    require(limit.isdecimal() and 1 <= int(limit) <= 200, 'invalid_limit')
                    args = {'limit': int(limit)}
                if is_index:
                    segments = raw_path.split(b'/')
                    require(len(segments) == 4 and segments[2] == b'by-tag', 'not_found')
                    encoded_tag = segments[3]
                    require(re.search(rb'%(?![0-9A-Fa-f]{2})', encoded_tag) is None, 'invalid_tag')
                    try:
                        tag = unquote_to_bytes(encoded_tag).decode('utf-8')
                    except UnicodeDecodeError as exc:
                        raise Failure('invalid_tag') from exc
                    args['tag'] = normalize_tag(tag)
                    operation = 'discovery.list'
                elif not lexical:
                    if 'tag' in query:
                        args['tag'] = normalize_tag(query['tag'])
                    args['query'] = query.get('query', '')
                    require(
                        args['tag'] if 'tag' in args else bool(args['query']),
                        'search_query_required',
                    )
                    operation = 'discovery.search'
                if 'cursor' in query and not lexical:
                    args['cursor'] = query['cursor']
                require(service.registry.operation(operation).effect == 'read', 'effect_mismatch')
                header = request.headers.get('x-msg-request')
                require(not (header and path_proof), 'ambiguous_proof')
                if path_proof is not None:
                    packet = path_read_proof(
                        path_proof, operation, args, service, limits.max_request_bytes
                    )
                elif header:
                    packet = path_packet(header, 'j', limits.max_request_bytes)
                    require(packet.operation == operation, 'operation_mismatch')
                    require(
                        canonical(packet.arguments) == canonical(args), 'representation_mismatch'
                    )
                else:
                    packet = request_for(
                        operation,
                        args,
                        service.settings.service_url,
                        source='manual',
                        contract_version=(
                            max(
                                search_query_version(args),
                                5
                                if lexical_path_v5
                                else 4
                                if lexical_path_v4
                                else 3
                                if lexical_path_v3
                                else 1,
                            )
                            if lexical
                            else 1
                        ),
                    )
                result = await execute_packet(packet, entry='network')
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                value = wire(result.data)
                if value.get('cursor') and not lexical:
                    params = {'limit': args['limit'], 'cursor': value['cursor']}
                    if is_index:
                        value['next'] = (
                            '/_i/by-tag/' + quote(args['tag'], safe='') + '?' + urlencode(params)
                        )
                    else:
                        if 'tag' in args:
                            params['tag'] = args['tag']
                        if args['query']:
                            params['query'] = args['query']
                        value['next'] = search_path_from_args(params)
                require(len(canonical(value)) <= limits.max_response_bytes, 'response_too_large')
                if is_index:
                    return Response(
                        b'' if request.method == 'HEAD' else canonical(value),
                        media_type='application/json',
                        headers=BASE_HEADERS,
                    )
                etag = '"' + digest(value)[7:] + '"'
                headers = {'ETag': etag, 'Cache-Control': 'no-store'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers={**BASE_HEADERS, **headers})
                return Response(
                    b'' if request.method == 'HEAD' else canonical(value),
                    media_type='application/json',
                    headers={**BASE_HEADERS, **headers},
                )
            if path == '/-/transfer':
                require(not request.url.query, 'unknown_query_parameter')
                require(request.method in {'GET', 'HEAD', 'POST'}, 'method_not_allowed')
                if request.method == 'HEAD':
                    return Response(status_code=200, headers=BASE_HEADERS)
                if request.method == 'GET':
                    available = tuple(
                        sorted(
                            spec.name
                            for spec in service.registry.operations('network')
                            if spec.name in TRANSFER_OPERATIONS
                        )
                    )
                    return json_response({
                        'version': 1,
                        'operations': available,
                        'request': 'OperationRequest JSON via POST',
                    })
                packet = decode_packet(
                    await body_bytes(request, limits.max_request_bytes), limits.max_request_bytes
                )
                require(packet.operation in TRANSFER_OPERATIONS, 'operation_mismatch')
                spec = service.registry.operation(packet.operation, packet.contract_version)
                require('network' in spec.entries, 'entry_not_allowed')
                result = await execute_packet(packet, entry='network')
                value = result_wire(result)
                require(len(canonical(value)) <= limits.max_response_bytes, 'response_too_large')
                return json_response(
                    value, error_status(result.error.code) if result.error else 200
                )
            if path == '/_transports':
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                data = {
                    'version': 1,
                    'target_service': service.settings.service_url,
                    'limits': wire(limits),
                    'recommended_part_bytes': service.settings.max_part_bytes,
                    'encodings': ['j', 'gz'],
                    'transports': {
                        'http': '/-/p/operation',
                        'path_get': '/-/g/operation/j/packet',
                        'graphql': '/-/graphql',
                        'mcp_http': '/-/mcp',
                        'transfer': '/-/transfer',
                        'mcp_stdio': 'msg mcp',
                    },
                    'operations': {
                        s.name: s.effect for s in service.registry.operations('network')
                    },
                    'contract_digest': service.registry.catalog()['digest'],
                }
                return json_response(data)
            if path == '/-/mcp':
                require(request.method == 'POST', 'method_not_allowed')
                protocol = request.headers.get('mcp-protocol-version')
                require(
                    protocol is None or protocol in SUPPORTED_VERSIONS, 'unsupported_mcp_version'
                )
                raw = await body_bytes(request, limits.max_request_bytes)
                message = loads(raw)
                output = await mcp.handle(message)
                if output is None:
                    return Response(status_code=202, headers=BASE_HEADERS)
                require(len(canonical(output)) <= limits.max_response_bytes, 'response_too_large')
                return json_response(output, headers={'MCP-Protocol-Version': PROTOCOL_VERSION})
            if path in {'/-/graphql', '/_read/graphql', '/_r/graphql'}:
                require(request.method == 'POST', 'method_not_allowed')
                if graphql_adapter is None:
                    from msg.transports.graphql import GraphQLAdapter

                    graphql_adapter = GraphQLAdapter(service)
                kind = 'mutation' if path == '/-/graphql' else 'query'
                data = await graphql_adapter.handle(
                    loads(await body_bytes(request, limits.max_request_bytes)), operation_kind=kind
                )
                require(len(canonical(data)) <= limits.max_response_bytes, 'response_too_large')
                return json_response(data)
            if path == '/-/d/read.query':
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                from msg.transports.dictionary import read_query_path_document

                document = read_query_path_document(service.registry)
                etag = '"' + digest(document)[7:] + '"'
                headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'private, no-cache'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers=headers)
                payload = canonical(document)
                require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/json',
                    headers=headers,
                )
            if path == '/-/d/search.query':
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                require(
                    service.registry.operation('discovery.lexical_search').effect == 'read',
                    'effect_mismatch',
                )
                document = {
                    'version': 5,
                    'operation': 'discovery.lexical_search',
                    'segments': SEARCH_V2_SEGMENTS,
                    'segments_v3': SEARCH_V3_SEGMENTS,
                    'segments_v4': SEARCH_V4_SEGMENTS,
                    'segments_v5': SEARCH_V5_SEGMENTS,
                    'mode': {'all': 'a', 'any': 'n'},
                    'field': {'all': 'a', 'body': 'b', 'name': 'n', 'metadata': 'm'},
                    'field_v5': {
                        'all': 'a',
                        'body': 'b',
                        'name': 'n',
                        'metadata': 'm',
                        'title': 't',
                    },
                    'title_semantics': 'Resource display name (alias of name)',
                    'scope_v5': 'path or JSON object: subject, org, or resource_refs (up to 32 id refs)',
                    'spell_v5': {
                        'enabled_by': 'spell=true',
                        'source': 'currently readable resource names',
                        'algorithm': 'Levenshtein; distance 1 for terms up to 4 characters, otherwise 2',
                        'max_terms': 4,
                        'term_length': [2, 32],
                        'max_dictionary_words': 512,
                        'max_suggestions_per_term': 5,
                        'rewrites_query': False,
                    },
                    'order': {'relevance': 'r', 'updated': 'u', 'created': 'c', 'name': 'n'},
                    'facets': ['type', 'tag'],
                    'contract_version': {
                        'default': 1,
                        'with_facets': 2,
                        'with_source_or_relation': 3,
                        'with_suggest': 4,
                        'with_revision_or_source_version': 5,
                    },
                    'template': '/_search/q/2/s/{percent-encoded-scope}/t/{terms}/m/{mode}/f/{field}/n/{limit}',
                    'template_v3': '/_search/q/3/s/{percent-encoded-scope}/t/{terms}/sk/{source-kind}/rt/{relation-type}',
                    'template_v4': '/_search/q/4/s/{percent-encoded-scope}/t/{terms}/sg/1',
                    'template_v5': '/_search/q/5/s/{percent-encoded-scope}/t/{terms}/rv/{revision}/sv/{source-version}',
                    'proof_suffix': '/p/{short-lived-signed-OperationRequest}',
                }
                etag = '"' + digest(document)[7:] + '"'
                headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'private, no-cache'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers=headers)
                payload = canonical(document)
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/json',
                    headers=headers,
                )
            if path in {'/-/d', '/-/schema'} or path.startswith('/-/d/'):
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                if short_codes is None:
                    from msg.transports.dictionary import build_dictionary

                    short_codes = build_dictionary(service.registry)
                if path.startswith('/-/d/'):
                    scope = path.removeprefix('/-/d/')
                    parts = scope.split('/')
                    require(
                        len(parts) in {1, 2}
                        and all(
                            re.fullmatch(
                                r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*(?:@[1-9][0-9]*)?', part
                            )
                            is not None
                            for part in parts
                        ),
                        'not_found',
                    )
                    if len(parts) == 2:
                        namespace_key, operation_key = parts
                        namespaces = {
                            key: row['identity']
                            for row in short_codes.document['codes']['namespace']
                            if not row.get('deprecated')
                            for key in (row['identity'], row['code'])
                        }
                        namespace = namespaces.get(namespace_key)
                        require(namespace is not None, 'not_found')
                        try:
                            document = short_codes.lookup_document(operation_key)
                        except Failure as exc:
                            raise Failure('not_found') from exc
                        require(
                            len(document.get('operations', ())) == 1
                            and document['operations'][0]['namespace'] == namespace,
                            'not_found',
                        )
                        etag = short_codes.etag_for(operation_key)
                    else:
                        document = short_codes.lookup_document(scope)
                        etag = short_codes.etag_for(scope)
                else:
                    document = (
                        short_codes.index_document
                        if path == '/-/d'
                        else short_codes.schema_document
                    )
                    etag = short_codes.index_etag if path == '/-/d' else short_codes.schema_etag
                headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'private, no-cache'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers=headers)
                payload = canonical(document)
                require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/json',
                    headers=headers,
                )
            protocol = re.fullmatch(
                r'/-/([pg])/([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)(?:/(schema|(j|gz)/([A-Za-z0-9_-]+)))?',
                path,
            )
            if protocol:
                transport, name, suffix, encoding, encoded = protocol.groups()
                spec = service.registry.operation(name)
                require('network' in spec.entries, 'entry_not_allowed')
                if request.method == 'HEAD':
                    if transport == 'g' and encoded is not None:
                        require_url_safe_packet(
                            path_packet(encoded, encoding, limits.max_request_bytes)
                        )
                    return Response(status_code=200, headers=BASE_HEADERS)
                if suffix == 'schema':
                    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                    return json_response({
                        'operation': service.registry.describe(spec),
                        'input': service.registry.schema(spec.input_schema),
                        'output': service.registry.schema(spec.output_schema),
                    })
                require(
                    not (transport == 'g' and name == 'sharing.link_read'), 'method_not_allowed'
                )
                if (
                    transport == 'g'
                    and request.method == 'GET'
                    and operation_route(spec).effect
                    in {RouteEffect.BUSINESS_WRITE, RouteEffect.EXTERNAL_EFFECT}
                    and passive_client(request)
                ):
                    return passive_client_response()
                if transport == 'p':
                    require(encoded is None and request.method == 'POST', 'method_not_allowed')
                    packet = decode_packet(
                        await body_bytes(request, limits.max_request_bytes),
                        limits.max_request_bytes,
                    )
                else:
                    require(encoded is not None and request.method == 'GET', 'method_not_allowed')
                    packet = path_packet(encoded, encoding, limits.max_request_bytes)
                    require_url_safe_packet(packet)
                require(packet.operation == name, 'operation_mismatch')
                result = await execute_packet(packet, entry='network')
                value = result_wire(result)
                require(len(canonical(value)) <= limits.max_response_bytes, 'response_too_large')
                return json_response(
                    value, error_status(result.error.code) if result.error else 200
                )
            if raw_path.startswith(b'/-/g/'):
                require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                if short_codes is None:
                    from msg.transports.dictionary import build_dictionary

                    short_codes = build_dictionary(service.registry)
                parts = raw_path.split(b'/')
                require(len(parts) >= 4 and parts[:3] == [b'', b'-', b'g'], 'invalid_path')
                try:
                    code = parts[3].decode('ascii')
                    require(bool(code), 'invalid_path')
                    spec = short_codes.resolve_operation(code)
                    require(spec.name != 'sharing.link_read', 'method_not_allowed')
                    if (
                        request.method == 'GET'
                        and operation_route(spec).effect
                        in {RouteEffect.BUSINESS_WRITE, RouteEffect.EXTERNAL_EFFECT}
                        and passive_client(request)
                    ):
                        return passive_client_response()
                    segments = []
                    for raw in parts[4:]:
                        require(re.search(rb'%(?![0-9A-Fa-f]{2})', raw) is None, 'invalid_path')
                        segments.append(unquote_to_bytes(raw).decode('utf-8'))
                except UnicodeDecodeError as exc:
                    raise Failure('invalid_path') from exc
                if segments and segments[0] in {'token', 'bootstrap'}:
                    raise Failure('secure_channel_required')
                else:
                    if request.method == 'HEAD':
                        return Response(status_code=200, headers=BASE_HEADERS)
                    spec, args = short_codes.decode_get_path(code, segments)
                    header = request.headers.get('x-msg-request')
                    if header:
                        packet = path_packet(header, 'j', limits.max_request_bytes)
                        require(packet.operation == spec.name, 'operation_mismatch')
                        require(packet.contract_version == spec.version, 'representation_mismatch')
                        require(dict(packet.arguments) == args, 'representation_mismatch')
                    else:
                        packet = request_for(
                            spec.name,
                            args,
                            service.settings.service_url,
                            source='manual',
                            contract_version=spec.version,
                        )
                result = await execute_packet(packet, entry='network')
                value = result_wire(result)
                require(len(canonical(value)) <= limits.max_response_bytes, 'response_too_large')
                return json_response(
                    value, error_status(result.error.code) if result.error else 200
                )
            if path == '/-' or path.startswith('/-/'):
                raise Failure('not_found')
            require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
            if path == '/register':
                require(not request.url.query, 'unknown_query_parameter')
                markdown = registration_markdown(service.settings.service_url)
                browser_html = 'text/html' in request.headers.get('accept', '').casefold()
                payload = (
                    document_html(
                        markdown, title='Register', account=await browser_account(), raw_path=path
                    )
                    if browser_html
                    else markdown.encode()
                )
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='text/html'
                    if browser_html
                    else 'text/plain'
                    if raw_document
                    else 'text/markdown',
                    headers={
                        **(HOME_BROWSER_HEADERS if browser_html else BASE_HEADERS),
                        'Vary': 'Accept',
                        'Content-Length': str(len(payload)),
                    },
                )
            if path == '/install':
                require(not request.url.query, 'unknown_query_parameter')
                payload = files('msg.data').joinpath('install.sh').read_bytes()
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='text/plain',
                    headers={
                        **BASE_HEADERS,
                        'Content-Length': str(len(payload)),
                        'Cache-Control': 'no-cache',
                    },
                )
            if path in {'/robots.txt', '/sitemap.xml'}:
                require(not request.url.query, 'unknown_query_parameter')
                origin = service.settings.service_url.rstrip('/')
                if path == '/robots.txt':
                    payload = (
                        f'User-agent: *\nAllow: /\nDisallow: /-/\nSitemap: {origin}/sitemap.xml\n'
                    ).encode()
                    media_type = 'text/plain'
                else:
                    # Only the public homepage is unconditional. Resource paths,
                    # including rules, can change their ACL or be removed.
                    payload = (
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                        f'  <url><loc>{escape(origin + "/")}</loc></url>\n'
                        '</urlset>\n'
                    ).encode()
                    media_type = 'application/xml'
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type=media_type,
                    headers={**BASE_HEADERS, 'Content-Length': str(len(payload))},
                )
            if path == '/favicon.png':
                headers = {**BASE_HEADERS, 'Content-Length': str(len(HOME_FAVICON))}
                return Response(
                    b'' if request.method == 'HEAD' else HOME_FAVICON,
                    media_type='image/png',
                    headers=headers,
                )
            if path == '/':
                packet = request_for(
                    'discovery.read_query',
                    {'home_summary': True},
                    service.settings.service_url,
                    source='manual',
                    contract_version=4,
                )
                result = await execute_packet(packet, entry='network')
                if result.error and result.error.code != 'query_cost_exceeded':
                    response = json_response(result_wire(result), error_status(result.error.code))
                    if request.method == 'HEAD':
                        response.body = b''
                    return response
                account = None
                browser = request.scope.get('state', {}).get('msg_browser_credentials')
                if browser:
                    identity = await execute_packet(
                        request_for(
                            'discovery.get',
                            {'id': browser[0], 'fields': ['id', 'name', 'groups']},
                            service.settings.service_url,
                            source='manual',
                        )
                    )
                    if not identity.error:
                        account = identity.data
                browser_html = 'text/html' in request.headers.get('accept', '').casefold()
                renderer = home_html if browser_html else home_markdown
                payload = renderer(
                    None if result.error else result.data,
                    service_url=service.settings.service_url,
                    account=account,
                    login_enabled=getattr(
                        getattr(service.settings, 'oauth', None), 'enabled', False
                    ),
                    expired=request.scope.get('state', {}).get('msg_browser_expired', False),
                )
                headers = {
                    **(HOME_BROWSER_HEADERS if browser_html else BASE_HEADERS),
                    'Content-Length': str(len(payload)),
                    'Cache-Control': 'no-store',
                    'Vary': 'Accept',
                }
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='text/html'
                    if browser_html
                    else 'text/plain'
                    if raw_document
                    else 'text/markdown',
                    headers=headers,
                )
            money_public = {
                '/_money': 'money.state',
                '/_money/banks': 'money.banks',
                '/_money/offers': 'money.offers',
            }
            if path in money_public:
                require(not request.url.query, 'unknown_query_parameter')
                operation = money_public[path]
                require(service.registry.operation(operation).effect == 'read', 'effect_mismatch')
                header = request.headers.get('x-msg-request')
                packet = (
                    path_packet(header, 'j', limits.max_request_bytes)
                    if header
                    else request_for(operation, {}, service.settings.service_url, source='manual')
                )
                require(
                    packet.operation == operation and not packet.arguments,
                    'representation_mismatch',
                )
                result = await execute_packet(packet, entry='network')
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                value = wire(result.data)
                payload = canonical(value)
                require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                etag = '"' + digest(value)[7:] + '"'
                headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'no-store'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers=headers)
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/json',
                    headers=headers,
                )
            if path in {'/_rules', '/_rules/'}:
                path = '/_rules/_index.md'
            topic_history = re.fullmatch(
                r'(.+)/_events\.md(?:/(compact|normal|proof)(?:/([0-9]{1,2})(?:/([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+))?)?)?',
                path,
            )
            if topic_history:
                require(raw_path == path.encode('utf-8') and b'%' not in raw_path, 'not_found')
                parent, segment_view, segment_limit, segment_cursor = topic_history.groups()
                pairs = request.query_params.multi_items()
                require(len(pairs) == len({name for name, _ in pairs}), 'duplicate_query_parameter')
                query = dict(pairs)
                require(set(query) <= {'view', 'limit', 'cursor'}, 'unknown_query_parameter')
                require(
                    not query or not (segment_view or segment_limit or segment_cursor),
                    'ambiguous_query_parameter',
                )
                view = segment_view or query.get('view', 'compact')
                require(view in {'compact', 'normal', 'proof'}, 'invalid_view')
                limit = segment_limit or query.get('limit', '10')
                require(limit.isdecimal() and 1 <= int(limit) <= 50, 'invalid_limit')
                cursor = segment_cursor or query.get('cursor')
                args = {'id': parent, 'view': view, 'limit': int(limit)}
                if cursor is not None:
                    args['cursor'] = cursor
                require(
                    service.registry.operation('content.topic_events').effect == 'read',
                    'effect_mismatch',
                )
                header = request.headers.get('x-msg-request')
                if header:
                    packet = path_packet(header, 'j', limits.max_request_bytes)
                    require(
                        packet.operation == 'content.topic_events'
                        and canonical(packet.arguments) == canonical(args),
                        'representation_mismatch',
                    )
                else:
                    packet = request_for(
                        'content.topic_events', args, service.settings.service_url, source='manual'
                    )
                result = await execute_packet(packet, entry='network')
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                value = wire(result.data)
                body = canonical(value)
                require(len(body) <= limits.max_response_bytes, 'response_too_large')
                etag = '"' + digest(value)[7:] + '"'
                headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'no-store'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers=headers)
                return Response(
                    b'' if request.method == 'HEAD' else body,
                    media_type='application/json',
                    headers=headers,
                )
            order_path = re.fullmatch(
                r'/_orders/(ord_[a-z2-7]{32})(?:/(_payment|_delivery))?(?:/json)?', path
            )
            if order_path:
                require(not request.url.query and b'%' not in raw_path, 'unknown_query_parameter')
                order_id, section = order_path.groups()
                operation = (
                    'orders.payment'
                    if section == '_payment'
                    else 'delivery.get'
                    if section == '_delivery'
                    else 'orders.get'
                )
                args = {'order_id': order_id}
                require(service.registry.operation(operation).effect == 'read', 'effect_mismatch')
                header = request.headers.get('x-msg-request')
                packet = (
                    path_packet(header, 'j', limits.max_request_bytes)
                    if header
                    else request_for(operation, args, service.settings.service_url, source='manual')
                )
                require(
                    packet.operation == operation
                    and canonical(packet.arguments) == canonical(args),
                    'representation_mismatch',
                )
                result = await execute_packet(packet, entry='network')
                if result.error:
                    return json_response(result_wire(result), error_status(result.error.code))
                value = wire(result.data)
                payload = canonical(value)
                require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                etag = '"' + digest(value)[7:] + '"'
                headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'private, no-cache'}
                if request.headers.get('if-none-match') == etag:
                    return Response(status_code=304, headers=headers)
                return Response(
                    b'' if request.method == 'HEAD' else payload,
                    media_type='application/json',
                    headers=headers,
                )
            subject_alias = re.fullmatch(r'/@([^/]+)/([^/]+)(/.*)?', path)
            ssh_projection = False
            ssh_key_id = None
            if subject_alias:
                handle, name, remainder = subject_alias.groups()
                if name == 'orders':
                    require(b'%' not in raw_path, 'not_found')
                    tail = (remainder or '').strip('/')
                    if tail.endswith('/json'):
                        tail = tail[:-5]
                    listing = tail in {'', 'json'}
                    parts = [] if listing else tail.split('/')
                    require(
                        listing
                        or (
                            len(parts) in {1, 2}
                            and re.fullmatch(r'ord_[a-z2-7]{32}', parts[0]) is not None
                            and (len(parts) == 1 or parts[1] in {'_payment', '_delivery'})
                        ),
                        'not_found',
                    )
                    operation = subject_route.name
                    pairs = request.query_params.multi_items()
                    require(
                        len(pairs) == len({key for key, _ in pairs}), 'duplicate_query_parameter'
                    )
                    query = dict(pairs)
                    require(not query or listing, 'unknown_query_parameter')
                    require(set(query) <= {'role', 'status', 'limit'}, 'unknown_query_parameter')
                    args = {} if listing else {'order_id': parts[0]}
                    for field in ('role', 'status'):
                        if field in query:
                            args[field] = query[field]
                    if 'limit' in query:
                        require(query['limit'].isdecimal(), 'invalid_limit')
                        args['limit'] = int(query['limit'])
                    async with service.metadata.transaction(write=False) as tx:
                        subject_id = await tx.resolve('/@' + handle)
                        require((await tx.resource(subject_id)).type == 'user', 'not_found')
                    require(
                        service.registry.operation(operation).effect == 'read', 'effect_mismatch'
                    )
                    header = request.headers.get('x-msg-request')
                    packet = (
                        path_packet(header, 'j', limits.max_request_bytes)
                        if header
                        else request_for(
                            operation, args, service.settings.service_url, source='manual'
                        )
                    )
                    require(
                        packet.operation == operation
                        and canonical(packet.arguments) == canonical(args),
                        'representation_mismatch',
                    )
                    result = await execute_packet(packet, entry='network')
                    if result.error:
                        return json_response(result_wire(result), error_status(result.error.code))
                    require(result.subject == subject_id, 'permission_denied')
                    value = wire(result.data)
                    payload = canonical(value)
                    require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                    etag = '"' + digest(value)[7:] + '"'
                    headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'private, no-cache'}
                    if request.headers.get('if-none-match') == etag:
                        return Response(status_code=304, headers=headers)
                    return Response(
                        b'' if request.method == 'HEAD' else payload,
                        media_type='application/json',
                        headers=headers,
                    )
                if name in {'bal', 'balance', 'ledger', 'public-balance', 'public-ledger'}:
                    require(remainder in {None, '/', '/json'}, 'not_found')
                    require(b'%' not in raw_path, 'not_found')
                    operation = subject_route.name
                    pairs = request.query_params.multi_items()
                    require(
                        len(pairs) == len({key for key, _ in pairs}), 'duplicate_query_parameter'
                    )
                    query = dict(pairs)
                    require(
                        not query or name in {'ledger', 'public-ledger'}, 'unknown_query_parameter'
                    )
                    require(set(query) <= {'cursor', 'limit'}, 'unknown_query_parameter')
                    args = {}
                    for field in ('cursor', 'limit'):
                        if field in query:
                            require(query[field].isdecimal(), 'invalid_query_parameter')
                            args[field] = int(query[field])
                    async with service.metadata.transaction(write=False) as tx:
                        subject_id = await tx.resolve('/@' + handle)
                        require((await tx.resource(subject_id)).type == 'user', 'not_found')
                    public = name in {'public-balance', 'public-ledger'}
                    if public:
                        args['subject_id'] = subject_id
                    require(
                        service.registry.operation(operation).effect == 'read', 'effect_mismatch'
                    )
                    header = request.headers.get('x-msg-request')
                    packet = (
                        path_packet(header, 'j', limits.max_request_bytes)
                        if header
                        else request_for(
                            operation, args, service.settings.service_url, source='manual'
                        )
                    )
                    require(
                        packet.operation == operation
                        and canonical(packet.arguments) == canonical(args),
                        'representation_mismatch',
                    )
                    result = await execute_packet(packet, entry='network')
                    if result.error:
                        return json_response(result_wire(result), error_status(result.error.code))
                    if not public:
                        require(result.subject == subject_id, 'permission_denied')
                    value = wire(result.data)
                    payload = canonical(value)
                    require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                    etag = '"' + digest(value)[7:] + '"'
                    headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'private, no-cache'}
                    if request.headers.get('if-none-match') == etag:
                        return Response(status_code=304, headers=headers)
                    return Response(
                        b'' if request.method == 'HEAD' else payload,
                        media_type='application/json',
                        headers=headers,
                    )
                if name in SUBJECT_COLLABORATION_VIEWS:
                    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                    require(
                        raw_path.decode('utf-8') == request.url.path and b'%' not in raw_path,
                        'not_found',
                    )
                    tail = (remainder or '').strip('/')
                    listing = tail in {'', 'json'}
                    if not listing:
                        require(
                            re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}(?:/json)?', tail) is not None,
                            'not_found',
                        )
                    operation = subject_route.name
                    pairs = request.query_params.multi_items()
                    require(
                        len(pairs) == len({key for key, _ in pairs}), 'duplicate_query_parameter'
                    )
                    query = dict(pairs)
                    require(not query or listing, 'unknown_query_parameter')
                    require(set(query) <= {'limit', 'after'}, 'unknown_query_parameter')
                    args = {} if listing else {'id': tail.removesuffix('/json')}
                    if 'limit' in query:
                        require(query['limit'].isdecimal(), 'invalid_limit')
                        args['limit'] = int(query['limit'])
                    if 'after' in query:
                        args['after'] = query['after']
                    async with service.metadata.transaction(write=False) as tx:
                        subject_id = await tx.resolve('/@' + handle)
                        require((await tx.resource(subject_id)).type == 'user', 'not_found')
                    require(
                        service.registry.operation(operation).effect == 'read', 'effect_mismatch'
                    )
                    header = request.headers.get('x-msg-request')
                    if header:
                        packet = path_packet(header, 'j', limits.max_request_bytes)
                        require(
                            packet.operation == operation
                            and canonical(packet.arguments) == canonical(args),
                            'representation_mismatch',
                        )
                    else:
                        packet = request_for(
                            operation, args, service.settings.service_url, source='manual'
                        )
                    result = await execute_packet(packet, entry='network')
                    if result.error:
                        return json_response(result_wire(result), error_status(result.error.code))
                    require(result.subject == subject_id, 'permission_denied')
                    value = wire(result.data)
                    value['path'] = (
                        '/@' + handle + '/' + name + ('/' + args['id'] if not listing else '')
                    )
                    etag = '"' + digest(value)[7:] + '"'
                    headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'private, no-cache'}
                    if request.headers.get('if-none-match') == etag:
                        return Response(status_code=304, headers=headers)
                    payload = canonical(value)
                    require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                    return Response(
                        b'' if request.method == 'HEAD' else payload,
                        media_type='application/json',
                        headers=headers,
                    )
                if name == 'receipts':
                    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                    require(
                        raw_path.decode('utf-8') == request.url.path and b'%' not in raw_path,
                        'not_found',
                    )
                    tail = (remainder or '').strip('/')
                    listing = tail in {'', 'json'}
                    if not listing:
                        require(
                            re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}(?:/json)?', tail) is not None,
                            'not_found',
                        )
                    operation = subject_route.name
                    pairs = request.query_params.multi_items()
                    require(
                        len(pairs) == len({key for key, _ in pairs}), 'duplicate_query_parameter'
                    )
                    query = dict(pairs)
                    require(not query or listing, 'unknown_query_parameter')
                    require(set(query) <= {'limit', 'cursor'}, 'unknown_query_parameter')
                    args = {} if listing else {'request_id': tail.removesuffix('/json')}
                    if 'limit' in query:
                        require(query['limit'].isdecimal(), 'invalid_limit')
                        args['limit'] = int(query['limit'])
                    if 'cursor' in query:
                        args['cursor'] = query['cursor']
                    require(
                        service.registry.operation(operation).effect == 'read', 'effect_mismatch'
                    )
                    async with service.metadata.transaction(write=False) as tx:
                        subject_id = await tx.resolve('/@' + handle)
                        require((await tx.resource(subject_id)).type == 'user', 'not_found')
                    header = request.headers.get('x-msg-request')
                    if header:
                        packet = path_packet(header, 'j', limits.max_request_bytes)
                        require(
                            packet.operation == operation
                            and canonical(packet.arguments) == canonical(args),
                            'representation_mismatch',
                        )
                    else:
                        packet = request_for(
                            operation, args, service.settings.service_url, source='manual'
                        )
                    result = await execute_packet(packet, entry='network')
                    # Another subject's lookup must not reveal whether its own
                    # request_id exists; the path subject is the only reader.
                    require(
                        result.subject is None or result.subject == subject_id, 'permission_denied'
                    )
                    if result.error:
                        return json_response(result_wire(result), error_status(result.error.code))
                    value = wire(result.data)
                    value['path'] = (
                        '/@'
                        + handle
                        + '/receipts'
                        + ('/' + args['request_id'] if not listing else '')
                    )
                    etag = '"' + digest(value)[7:] + '"'
                    headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'private, no-cache'}
                    if request.headers.get('if-none-match') == etag:
                        return Response(status_code=304, headers=headers)
                    payload = canonical(value)
                    require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                    return Response(
                        b'' if request.method == 'HEAD' else payload,
                        media_type='application/json',
                        headers=headers,
                    )
                certificate_detail = (
                    name in {'cert', 'certificates'}
                    and remainder not in CERTIFICATE_COLLECTION_VIEWS
                )
                if (
                    name in SUBJECT_KEY_ALIASES
                    or name in SUBJECT_OPERATION_ALIASES
                    or certificate_detail
                ):
                    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
                    key_alias = name in SUBJECT_KEY_ALIASES
                    if not key_alias and not certificate_detail:
                        require(remainder in {None, '/'}, 'not_found')
                    require(
                        not request.url.query
                        or name
                        in {'in', 'inbox', 'out', 'outbox', 'following', 'follows', 'followers'},
                        'unknown_query_parameter',
                    )
                    async with service.metadata.transaction(write=False) as tx:
                        subject_id = await tx.resolve('/@' + handle)
                        require((await tx.resource(subject_id)).type == 'user', 'not_found')
                    operation = subject_route.name
                    if certificate_detail:
                        tail = remainder.strip('/')
                        require(
                            re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}(?:/json)?', tail) is not None,
                            'not_found',
                        )
                        args = {'id': tail.removesuffix('/json')}
                    elif key_alias:
                        tail = (remainder or '').strip('/')
                        if tail == 'json':
                            tail = ''
                        if tail:
                            require(
                                re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}(?:/json)?', tail) is not None,
                                'not_found',
                            )
                            key_id = tail.removesuffix('/json')
                        args = {'subject_id': subject_id}
                        if tail:
                            args['key_id'] = key_id
                    else:
                        args = (
                            {'subject_id': subject_id}
                            if operation
                            in {
                                'achievement.list',
                                'communication.agent_following',
                                'communication.followers',
                            }
                            else {}
                        )
                    if operation in {'communication.agent_following', 'communication.followers'}:
                        pairs = request.query_params.multi_items()
                        query = dict(pairs)
                        require(len(pairs) == len(query), 'duplicate_query_parameter')
                        require(set(query) <= {'limit', 'after'}, 'unknown_query_parameter')
                        if 'limit' in query:
                            require(query['limit'].isdecimal(), 'invalid_limit')
                            args['limit'] = int(query['limit'])
                        if 'after' in query:
                            args['after'] = query['after']
                    if operation in {
                        'communication.inbox',
                        'communication.outbox',
                        'communication.following',
                    }:
                        pairs = request.query_params.multi_items()
                        require(
                            len(pairs) == len({key for key, _ in pairs}),
                            'duplicate_query_parameter',
                        )
                        query = dict(pairs)
                        require(set(query) <= {'limit', 'cursor'}, 'unknown_query_parameter')
                        if 'limit' in query:
                            require(query['limit'].isdecimal(), 'invalid_limit')
                            args['limit'] = int(query['limit'])
                        if 'cursor' in query:
                            args['cursor'] = query['cursor']
                    header = request.headers.get('x-msg-request')
                    if header:
                        packet = path_packet(header, 'j', limits.max_request_bytes)
                        require(
                            packet.operation == operation
                            and canonical(packet.arguments) == canonical(args),
                            'representation_mismatch',
                        )
                    else:
                        packet = request_for(
                            operation, args, service.settings.service_url, source='manual'
                        )
                    result = await execute_packet(packet, entry='network')
                    if result.error:
                        return json_response(result_wire(result), error_status(result.error.code))
                    if operation in {
                        'communication.inbox',
                        'communication.outbox',
                        'communication.dm_list',
                        'communication.following',
                    }:
                        require(result.subject == subject_id, 'permission_denied')
                    value = wire(result.data)
                    if certificate_detail:
                        require(
                            value.get('certificate', {}).get('subject_id') == subject_id,
                            'not_found',
                        )
                    canonical_name = (
                        'cert'
                        if certificate_detail
                        else SUBJECT_KEY_ALIASES[name][1]
                        if key_alias
                        else {
                            'achievement.list': 'ach',
                            'communication.inbox': 'in',
                            'communication.outbox': 'out',
                            'communication.dm_list': 'dm',
                            'communication.following': 'following',
                            'communication.agent_following': 'follows',
                            'communication.followers': 'followers',
                        }[operation]
                    )
                    key_suffix = args.get('key_id') or (
                        args.get('id') if certificate_detail else None
                    )
                    value['path'] = (
                        '/@'
                        + handle
                        + '/'
                        + canonical_name
                        + ('/' + key_suffix if key_suffix else '')
                    )
                    browser_html = 'text/html' in request.headers.get('accept', '').casefold()
                    etag = '"' + digest([value, browser_html])[7:] + '"'
                    headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'private, no-cache'}
                    headers['Vary'] = 'Accept'
                    if browser_html:
                        headers.update(HOME_BROWSER_HEADERS)
                    if request.headers.get('if-none-match') == etag:
                        return Response(status_code=304, headers=headers)
                    payload = (
                        mailbox_html(value, name, account=await browser_account())
                        if browser_html and name in {'in', 'inbox', 'out', 'outbox', 'dm'}
                        else document_html(
                            '# '
                            + value['path']
                            + '\n\n```json\n'
                            + canonical(value).decode()
                            + '\n```'
                        )
                        if browser_html
                        else canonical(value)
                    )
                    require(len(payload) <= limits.max_response_bytes, 'response_too_large')
                    return Response(
                        b'' if request.method == 'HEAD' else payload,
                        media_type='text/html'
                        if browser_html
                        else 'text/plain'
                        if raw_document
                        else 'application/json',
                        headers=headers,
                    )
                if name in SUBJECT_RESOURCE_ALIASES:
                    require(
                        raw_path.decode('utf-8') == request.url.path and b'%' not in raw_path,
                        'not_found',
                    )
                    ssh_projection = name in {'ssh', 'ssh-keys'}
                    if ssh_projection and remainder not in {None, '/', '/json', '/meta'}:
                        tail = remainder.strip('/')
                        require(
                            re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}(?:/json)?', tail) is not None,
                            'not_found',
                        )
                        ssh_key_id = tail.removesuffix('/json')
                        remainder = '/json' if tail.endswith('/json') else ''
                    path = '/@' + handle + '/' + SUBJECT_RESOURCE_ALIASES[name] + (remainder or '')
            post_view = parse_post_view(path, raw_path)
            stable = (
                ('/_id/' + post_view['id'], post_view['view'] or 'markdown', post_view['rev'])
                if post_view
                else parse_stable_view(path, raw_path)
            )
            resource_path, view, revision = stable if stable is not None else parse_view(path)
            # A public representation must be read-only before even resolving
            # its old/migrated alias. Missing targets cannot bypass this gate.
            initial_operation = 'discovery.raw' if view == 'raw' else 'discovery.get'
            require(
                service.registry.operation(initial_operation).effect == 'read', 'effect_mismatch'
            )
            redirect_target = None
            redirect_resource_id = None
            legacy_target = None
            if path.startswith('/_legacy/'):
                from msg.transports.legacy_content_http import resolve_legacy_read

                async with service.metadata.transaction(write=False) as tx:
                    legacy_target = await resolve_legacy_read(tx, path, raw_path)
            if legacy_target is not None:
                resource_path = '/_id/' + legacy_target.resource_id
                view = legacy_target.view
                revision = None
                stable = (resource_path, view, None)
                redirect_resource_id = legacy_target.resource_id
                redirect_target = legacy_target.canonical_path
            if (
                redirect_target is None
                and view == 'markdown'
                and revision is None
                and not resource_path.endswith('.md')
            ):
                # Old Post links omitted .md. Resolve the candidate only to find
                # the stable resource; disclose its canonical path after the
                # normal discovery.get authorization check succeeds.
                async with service.metadata.transaction(write=False) as tx:
                    try:
                        await tx.resolve(resource_path)
                    except Failure as exc:
                        if exc.code != 'not_found':
                            raise
                        candidate = resource_path + '.md'
                        try:
                            rid = await resolve_read(tx, candidate)
                        except Failure as candidate_error:
                            if candidate_error.code != 'not_found':
                                raise
                        else:
                            if (await tx.resource(rid)).type == 'post':
                                redirect_resource_id = rid
                                redirect_target = await tx.path(rid)
                                resource_path = redirect_target
            if redirect_target is None and stable is None:
                async with service.metadata.transaction(write=False) as tx:
                    try:
                        await tx.resolve(resource_path)
                    except Failure as exc:
                        if exc.code != 'not_found':
                            raise
                        rid = await tx.resolve_migrated(resource_path)
                        canonical_path = await tx.path(rid)
                        suffix = '/revisions/' + revision if revision else ''
                        if view != 'markdown':
                            suffix += '/' + view
                        redirect_resource_id = rid
                        redirect_target = canonical_path + suffix
                        resource_path = '/_id/' + rid
            op = {
                'raw': 'discovery.raw',
                'diff': 'discovery.diff_view',
                'references': 'discovery.references',
                'thread': 'discussion.thread',
            }.get(view, 'discovery.get')
            require(service.registry.operation(op).effect == 'read', 'effect_mismatch')
            args = {'id': resource_path}
            if post_view:
                if view == 'thread':
                    query = dict(request.query_params)
                    require(set(query) <= {'cursor', 'limit', 'preview'}, 'unknown_query_parameter')
                    require(
                        len(query) == len(request.query_params.multi_items()),
                        'duplicate_query_parameter',
                    )
                    if 'limit' in query:
                        require(query['limit'].isdecimal(), 'invalid_limit')
                        query['limit'] = int(query['limit'])
                    preview = query.pop('preview', '20')
                    require(preview.isdecimal(), 'invalid_preview')
                    preview = int(preview)
                    require(0 <= preview <= 1000, 'invalid_preview')
                    args.update(query)
                else:
                    require(not request.query_params, 'unknown_query_parameter')
                if view == 'diff':
                    if post_view['new']:
                        args.update(old_revision=post_view['old'], new_revision=post_view['new'])
                    elif post_view['old']:
                        args['known_revision'] = post_view['old']
                    else:
                        args['previous'] = True
            if revision:
                args['revision'] = revision
            if view in {'meta', 'history'}:
                args['view'] = view
            header = request.headers.get('x-msg-request')
            if header:
                packet = path_packet(header, 'j', limits.max_request_bytes)
                require(packet.operation == op, 'operation_mismatch')
                # Resolve both aliases to the same stable resource before comparing.
                async with service.metadata.transaction(write=False) as tx:
                    header_id = packet.arguments.get('id')
                    rid = (
                        await resolve_read(tx, header_id)
                        if isinstance(header_id, str) and header_id.startswith('/')
                        else header_id
                    )
                    require(rid == await tx.resolve(resource_path), 'resource_mismatch')
                require(packet.arguments.get('revision') == revision, 'revision_mismatch')
                require(packet.arguments.get('view') == args.get('view'), 'representation_mismatch')
                if post_view and view == 'thread':
                    require(
                        all(packet.arguments.get(k) == args.get(k) for k in ('cursor', 'limit')),
                        'representation_mismatch',
                    )
                if post_view and view == 'diff':
                    require(
                        all(
                            packet.arguments.get(k) == args.get(k)
                            for k in ('previous', 'known_revision', 'old_revision', 'new_revision')
                        ),
                        'representation_mismatch',
                    )
            else:
                packet = request_for(op, args, service.settings.service_url, source='manual')
            result = await execute_packet(packet, entry='network')
            if result.error:
                return json_response(result_wire(result), error_status(result.error.code))
            if redirect_target is not None:
                # A signed old-path packet may resolve a newly occupying object
                # between routing and execution. Its successful authorization
                # must not disclose the previous target's canonical name.
                authorized_id = (
                    result.resources[0].id
                    if result.resources
                    else result.data.get('id', result.data.get('metadata', {}).get('id'))
                )
                require(authorized_id == redirect_resource_id, 'resource_mismatch')
                if legacy_target is not None:
                    async with service.metadata.transaction(write=False) as tx:
                        current_legacy = await resolve_legacy_read(tx, path, raw_path)
                    require(
                        current_legacy is not None
                        and current_legacy.resource_id == authorized_id
                        and current_legacy.view == legacy_target.view,
                        'resource_mismatch',
                    )
                    legacy_target = current_legacy
                    redirect_target = current_legacy.canonical_path
                headers = {**BASE_HEADERS, 'Location': quote(redirect_target, safe='/@&')}
                if legacy_target is not None:
                    headers['X-Msg-Legacy-Source'] = legacy_target.source_sha256
                    headers['X-Msg-Legacy-Signature'] = 'unverified-historical-claim'
                    if legacy_target.provenance_path is not None:
                        headers['Link'] = (
                            '<'
                            + quote(legacy_target.provenance_path, safe='/')
                            + '>; rel="describedby"'
                        )
                return Response(status_code=308, headers=headers)
            if post_view:
                async with service.metadata.transaction(write=False) as tx:
                    resource = await tx.resource(post_view['id'])
                    require(resource.state != 'purged', 'not_found')
                    if post_view['scope']:
                        require(
                            resource.parent == await tx.resolve(post_view['scope']), 'not_found'
                        )
            value = wire(result.data)
            if post_view and view == 'thread':
                value['items'] = [
                    {
                        'id': item['id'],
                        'revision': item['revision'],
                        **post_title(item),
                        **thread_summary(item, preview),
                        **{
                            relation['type']: relation['target']['id']
                            for relation in item.get('relations', ())
                            if relation['type'] == 'reply_to'
                        },
                    }
                    for item in value['items']
                ]
                if 'cursor' in value:
                    value['next'] = (
                        '/*'
                        + post_view['id']
                        + '/thread?'
                        + urlencode({
                            'cursor': value['cursor'],
                            'limit': args.get('limit', 50),
                            **({'preview': preview} if preview != 20 else {}),
                        })
                    )
            if post_view and view == 'references':
                value = {'ids': list(dict.fromkeys(item['source_id'] for item in value['items']))}
            if post_view and view in {'json', 'meta', 'history', 'references', 'diff', 'thread'}:
                value = hex_references(value)
            if ssh_projection:
                value = {
                    'id': value['id'],
                    'keys': [
                        key
                        for key in value.get('keys', ())
                        if key.get('kind') == 'ssh_key'
                        and (ssh_key_id is None or key.get('key_id') == ssh_key_id)
                    ],
                }
                if ssh_key_id is not None:
                    require(bool(value['keys']), 'not_found')
            browser_html = (
                view == 'markdown' and 'text/html' in request.headers.get('accept', '').casefold()
            )
            etag = '"' + digest([value, browser_html])[7:] + '"'
            headers = {**BASE_HEADERS, 'ETag': etag, 'Cache-Control': 'private, no-cache'}
            headers['Vary'] = 'Accept'
            if browser_html:
                headers.update(HOME_BROWSER_HEADERS)
            if request.headers.get('if-none-match') == etag:
                return Response(status_code=304, headers=headers)
            if view == 'raw':
                blob = decode(BlobRef, value['content'])
                byte_range = tuple(value['range'])
                status = 200
                requested_range = request.headers.get('range')
                if requested_range and (request.headers.get('if-range') in {None, etag}):
                    match = (
                        re.fullmatch(r'bytes=(\d*)-(\d*)', requested_range)
                        if len(requested_range) <= 128
                        else None
                    )
                    require(match is not None and any(match.groups()), 'range_not_satisfiable')
                    left, right = match.groups()
                    if left:
                        begin = int(left)
                        end = min(int(right) + 1, blob.size) if right else blob.size
                    else:
                        require(int(right) > 0, 'range_not_satisfiable')
                        begin = max(0, blob.size - int(right))
                        end = blob.size
                    require(byte_range[0] <= begin < end <= byte_range[1], 'range_not_satisfiable')
                    byte_range = (begin, end)
                    status = 206
                    headers['Content-Range'] = f'bytes {begin}-{end - 1}/{blob.size}'
                headers.update({
                    'Content-Length': str(byte_range[1] - byte_range[0]),
                    'Accept-Ranges': 'bytes',
                    'Content-Disposition': "attachment; filename*=UTF-8''"
                    + quote(value['filename'], safe=''),
                })
                if request.method == 'HEAD':
                    return Response(status_code=status, media_type=blob.media_type, headers=headers)
                return StreamingResponse(
                    service.contents.read(blob, byte_range),
                    status_code=status,
                    media_type=blob.media_type,
                    headers=headers,
                )
            body = (
                canonical(value)
                if view in {'json', 'meta', 'history', 'diff', 'references', 'thread'}
                else document_html(
                    resource_markdown(value, describe_resource(value)),
                    title=path,
                    account=await browser_account(),
                    resource=value,
                    raw_path=path,
                )
                if browser_html
                else resource_markdown(value, describe_resource(value)).encode()
            )
            require(len(body) <= limits.max_response_bytes, 'response_too_large')
            return Response(
                b'' if request.method == 'HEAD' else body,
                media_type='text/plain'
                if raw_document
                else 'application/json'
                if view in {'json', 'meta', 'history', 'diff', 'references', 'thread'}
                else 'text/html'
                if browser_html
                else 'text/plain'
                if raw_document
                else 'text/markdown',
                headers=headers,
            )
        except Failure as exc:
            return json_response(
                {'status': 'error', 'error': exc.as_dict()}, error_status(exc.code)
            )
        except Exception:
            import logging

            # Exception messages/tracebacks may contain URLs, headers or input.
            logging.getLogger(__name__).error('http_dispatch_failed')
            return json_response(
                {'status': 'error', 'error': {'code': 'internal_error', 'retryable': False}}, 500
            )

    return Starlette(
        routes=[
            Route(
                '/{path:path}',
                dispatch,
                methods=['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'],
            )
        ],
        lifespan=lifespan,
    )
