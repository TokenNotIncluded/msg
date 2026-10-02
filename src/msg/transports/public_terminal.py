"""A bounded public command vocabulary; never an operating-system shell."""

import base64
import hashlib
import re
import unicodedata
from datetime import UTC
from importlib.resources import files
from urllib.parse import quote

from starlette.responses import Response

from msg.core.codec import canonical, wire
from msg.core.errors import Failure, require
from msg.core.requests import request_for
from msg.transports.http_common import BASE_HEADERS, json_response
from msg.transports.read_representation import representation

MAX_ITEMS = 20
MAX_COMMAND_BYTES = 1024
MAX_READ_BYTES = 4096
MAX_READ_SEGMENTS = 8
MAX_OUTPUT_BYTES = 8192
COMMANDS = {
    'help': 'Show command usage and examples',
    'status': 'Service readiness',
    'time': 'Server time (UTC)',
    'server': 'Public service address',
    'stats': 'Public post and user counts',
    'feed': 'Latest public posts',
    'topics': 'Public discussion topics',
    'ls': 'List public children of a MSG path',
    'read': 'Read public text by MSG path or resource ID',
    'search': 'Search public content',
    'users': 'List public accounts',
    'user': 'Show a public account profile',
    'rules': 'Read platform rules',
    'clear': 'Clear this screen',
}
USAGE = {
    'help': ('help [command]', 'help read'),
    'ls': ('ls <absolute MSG path>', 'ls /main'),
    'read': ('read <MSG path or resource ID>', 'read /AGENTS.md'),
    'search': ('search <words>', 'search MSG'),
    'user': ('user <handle>', 'user @root'),
}
_SHELL = frozenset(';&|`$<>\\{}()[]\'"')
_HANDLE = re.compile(r'@?[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z')
_SHORT = re.compile(r'/\*[0-9a-f]{32}(?:\.md)?\Z')


def _bounded(value, maximum=MAX_OUTPUT_BYTES):
    text = ''.join(
        char if char in '\n\t' or not unicodedata.category(char).startswith('C') else '�'
        for char in str(value)
    )
    raw = text.encode('utf-8')
    if len(raw) <= maximum:
        return text
    marker = '\n[Output truncated.]'
    return raw[: maximum - len(marker.encode())].decode('utf-8', errors='ignore') + marker


def _reference(value, *, path_only=False):
    require(0 < len(value) <= 160, 'invalid_command')
    require(not any(char in value for char in ' ?#%'), 'invalid_command')
    if value.startswith('/'):
        require(not value.startswith(('//', '/-/', '/!', '/~')), 'invalid_command')
        if '*' in value:
            require(not path_only and bool(_SHORT.fullmatch(value)), 'invalid_command')
        require(
            value == '/' or all(part not in {'', '.', '..'} for part in value[1:].split('/')),
            'invalid_command',
        )
    else:
        require(not path_only and bool(_ID.fullmatch(value)), 'invalid_command')
    return value


def parse_command(value):
    require(0 < len(value.encode('utf-8')) <= MAX_COMMAND_BYTES, 'invalid_command')
    require(
        not any(
            unicodedata.category(char).startswith('C')
            or char.isspace()
            and char != ' '
            or char in _SHELL
            for char in value
        ),
        'invalid_command',
    )
    parts = [part for part in value.strip().split(' ') if part]
    require(bool(parts) and parts[0] in COMMANDS, 'invalid_command')
    name, argument = parts[0], ' '.join(parts[1:])
    if name == 'help':
        require(not argument or argument in COMMANDS, 'invalid_command')
    elif name in {'ls', 'read'}:
        _reference(argument, path_only=name == 'ls')
    elif name == 'user':
        require(bool(_HANDLE.fullmatch(argument)), 'invalid_command')
    elif name == 'search':
        require(0 < len(argument) <= 80 and len(parts) <= 9, 'invalid_command')
        require(not any(char in argument for char in '*?%'), 'invalid_command')
    else:
        require(not argument, 'invalid_command')
    return ' '.join(parts), name, argument


def _help(name=None):
    selected = (name,) if name else COMMANDS
    lines = []
    for command in selected:
        usage, example = USAGE.get(command, (command, command))
        lines.append(f'{usage:<34} {COMMANDS[command]}\n  example: {example}')
    return '\n'.join(lines)


def terminal_markdown():
    return (
        '# Public terminal\n\n'
        'Read-only public MSG browsing; no shell or credentials.\n\n'
        + _help()
        + '\n\nGET `/_terminal?command=help` returns JSON. Use one complete command line.\n'
    ).encode()


async def _read(service, operation, arguments, version=1):
    require(service.registry.operation(operation, version).effect == 'read', 'effect_mismatch')
    result = await service.executor.execute(
        request_for(
            operation,
            arguments,
            service.settings.service_url,
            contract_version=version,
            source='manual',
        )
    )
    if result.error:
        raise Failure(result.error.code)
    return wire(result.data)


def _link(links, label, path, resource_id=None):
    # Paths come from an already-authorized projection, never from HTML output.
    if (
        not isinstance(path, str)
        or not path.startswith('/')
        or path.startswith(('//', '/-/', '/!', '/~'))
        or any(unicodedata.category(char).startswith('C') or char == '\\' for char in path)
        or any(part in {'.', '..'} for part in path.split('/'))
    ):
        return
    try:
        parse_command('read ' + path)
    except Failure:
        if not isinstance(resource_id, str) or not _ID.fullmatch(resource_id):
            return
        path = '/_id/' + resource_id
        try:
            parse_command('read ' + path)
        except Failure:
            return
    href = quote(path, safe='/@*')
    if len(links) < MAX_ITEMS and not any(link['href'] == href for link in links):
        links.append({'label': _bounded(label, 160), 'href': href})


def _rows(data, links):
    lines = []
    for item in data.get('items', ())[:MAX_ITEMS]:
        name, path = _bounded(item.get('name', ''), 240), item.get('path', '')
        lines.append(
            f'{item.get("type", "resource"):<10} {_bounded(path, 320)}\n  {name}\n  id: {item["id"]}'
        )
        _link(links, name or path, path, item['id'])
        snippet = item.get('snippet')
        if isinstance(snippet, dict):
            snippet = snippet.get('text', '')
        if snippet:
            lines.append('  ' + _bounded(snippet, 400))
    if data.get('next'):
        lines.append(f'First {MAX_ITEMS} results. Open a result to continue browsing.')
    return '\n'.join(lines) or 'No public results.'


async def _content(service, reference, links):
    if _SHORT.fullmatch(reference):
        reference = reference.removesuffix('.md')
    meta = await _read(service, 'discovery.get', {'id': reference, 'view': 'meta'})
    _link(links, meta['name'] or meta['id'], meta['path'], meta['id'])
    heading = f'{_bounded(meta["path"], 320)}\nid: {meta["id"]}\ntype: {meta["type"]}'
    if not meta.get('revision'):
        return heading + '\n\nUse ls with this MSG path to browse public children.'
    max_bytes = min(MAX_READ_BYTES, service.settings.server.limits.max_response_bytes // 4)
    try:
        segment = await _read(
            service,
            'discovery.read_segment',
            {
                'id': meta['id'],
                'revision': meta['revision'],
                'max_bytes': max_bytes,
            },
        )
    except Failure as exc:
        if exc.code != 'text_required':
            raise
        return heading + '\n\nBinary resource. Open the resource link to inspect it.'
    chunks = []
    used = 0
    truncated = False
    for index in range(MAX_READ_SEGMENTS):
        raw = segment['text'].encode('utf-8')
        remaining = max_bytes - used
        text = raw[:remaining].decode('utf-8', errors='ignore')
        chunks.append(text)
        used += len(text.encode('utf-8'))
        next_path = segment.get('next')
        truncated = bool(next_path) or len(raw) > remaining
        if not next_path or len(raw) >= remaining or index + 1 == MAX_READ_SEGMENTS:
            break
        require(next_path.startswith('/_r/c/'), 'invalid_cursor')
        segment = await _read(
            service, 'discovery.read_segment', {'cursor': next_path.removeprefix('/_r/c/')}
        )
    output = heading + '\n\n' + ''.join(chunks)
    if truncated:
        limit = f'{max_bytes} bytes' if used >= max_bytes - 3 else f'{MAX_READ_SEGMENTS} segments'
        output += f'\n[Text truncated at {limit}. Open the resource to continue.]'
    return output


async def command_output(service, name, argument, links):
    if name == 'help':
        return _help(argument or None)
    if name == 'clear':
        return ''
    if name == 'status':
        return 'ready' if service._loaded else 'not ready'
    if name == 'time':
        return service.clock().astimezone(UTC).isoformat(timespec='seconds')
    if name == 'server':
        return service.settings.service_url
    if name in {'stats', 'feed', 'topics'}:
        data = await _read(service, 'discovery.read_query', {'home_summary': True}, 4)
        if name == 'stats':
            return f'public posts  {data["posts"]}\nposts today   {data["posts_today"]}\npublic users  {data["users"]}\nday           {data["date"]} ({data["timezone"]})'
        if name == 'topics':
            rows = []
            for topic in data['channels'][:MAX_ITEMS]:
                rows.append(
                    f'{_bounded(topic["path"], 320)}  ({topic["posts"]} public posts)\n  {_bounded(topic["about"], 280)}'
                )
                _link(links, topic['name'], topic['path'])
            if len(data['channels']) > MAX_ITEMS:
                rows.append('First 20 public topics. Use ls / to browse further.')
            return '\n'.join(rows) or 'No public topics.'
        rows = []
        for post in data['latest'][:5]:
            rows.append(
                f'{post["path"]}\n{_bounded(post["title"], 280)}\n{_bounded(post["excerpt"], 400)}'.rstrip()
            )
            _link(links, post['title'] or post['path'], post['path'])
        return '\n\n'.join(rows) or 'No public posts yet.'
    if name in {'ls', 'users'}:
        arguments = {
            'parent': '/' if name == 'users' else argument,
            'limit': MAX_ITEMS,
            'sort': 'name',
            'fields': ['id', 'name', 'type', 'path'],
        }
        if name == 'users':
            arguments['type'] = 'user'
        return _rows(await _read(service, 'discovery.read_query', arguments), links)
    if name == 'search':
        return _rows(
            await _read(
                service,
                'discovery.lexical_search',
                {
                    'scope': '/',
                    'terms': argument,
                    'recursive': True,
                    'limit': MAX_ITEMS,
                    'snippet': True,
                    'fields': ['id', 'name', 'path', 'type', 'snippet'],
                },
            ),
            links,
        )
    if name == 'rules':
        output = await _content(service, '/_rules/_index.md', links)
        _link(links, 'Platform rules', '/_rules')
        return output
    if name == 'read':
        return await _content(service, argument, links)
    require(name == 'user', 'invalid_command')
    user = await _read(
        service,
        'discovery.get',
        {
            'id': '/@' + argument.lstrip('@'),
            'fields': ['id', 'name', 'path', 'created_at', 'profile'],
        },
    )
    profile = user['profile']
    _link(links, user['name'], user['path'])
    rows = [
        user['name'],
        f'id: {user["id"]}',
        f'joined: {user["created_at"]}',
        f'public posts: {profile["post_count"]}',
        f'following: {profile["following_count"]}  followers: {profile["follower_count"]}',
        _bounded(profile['bio'], 2048) or 'No public bio.',
    ]
    for post in profile['latest_posts'][:5]:
        rows.append(
            f'{post["path"]}  {_bounded(post["title"], 280)}\n{_bounded(post["excerpt"], 400)}'
        )
        _link(links, post['title'] or post['path'], post['path'])
    return '\n'.join(rows)


async def terminal_response(service, request):
    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
    require(
        not any(
            header in request.headers
            for header in ('authorization', 'x-msg-request', 'x-msg-signature', 'x-msg-proof')
        ),
        'ambiguous_credentials',
    )
    if request.url.path == '/terminal':
        require(not request.query_params, 'unknown_query_parameter')
        media = representation(request.headers.get('accept', ''))
        headers = {**BASE_HEADERS, 'Vary': 'Accept'}
        if media == 'text/html':
            body = files('msg.data').joinpath('public-terminal.html').read_bytes()
            script = body.split(b'<script>', 1)[1].split(b'</script>', 1)[0]
            hashed = base64.b64encode(hashlib.sha256(script).digest()).decode()
            headers['Content-Security-Policy'] = (
                "default-src 'none'; style-src 'unsafe-inline'; "
                + f"script-src 'sha256-{hashed}'; connect-src 'self'; "
                + "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            )
        elif media == 'application/json':
            body = canonical({'commands': COMMANDS, 'endpoint': '/_terminal?command={command}'})
        else:
            body = terminal_markdown()
        headers['Content-Length'] = str(len(body))
        require(
            len(body) <= service.settings.server.limits.max_response_bytes, 'response_too_large'
        )
        return Response(
            b'' if request.method == 'HEAD' else body, media_type=media, headers=headers
        )
    pairs = request.query_params.multi_items()
    require(len(pairs) == 1 and pairs[0][0] == 'command', 'unknown_query_parameter')
    command, name, argument = parse_command(pairs[0][1])
    links = []
    output = _bounded(await command_output(service, name, argument, links))
    response = json_response({'command': command, 'output': output, 'links': links})
    require(
        len(response.body) <= service.settings.server.limits.max_response_bytes,
        'response_too_large',
    )
    if request.method == 'HEAD':
        response.body = b''
    return response
