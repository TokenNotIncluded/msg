"""A fixed public command vocabulary; never an operating-system shell."""

import base64
import hashlib
from datetime import UTC
from importlib.resources import files

from starlette.responses import Response

from msg.core.codec import wire
from msg.core.errors import Failure, require
from msg.core.requests import request_for
from msg.transports.http_common import BASE_HEADERS, json_response

COMMANDS = {
    'help': 'Show available commands',
    'status': 'Service readiness',
    'time': 'Server time (UTC)',
    'server': 'Public service address',
    'stats': 'Public post and user counts',
    'feed': 'Latest public posts',
    'clear': 'Clear this screen',
}


async def terminal_response(service, request):
    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
    require(
        not request.headers.get('authorization') and not request.headers.get('x-msg-request'),
        'ambiguous_credentials',
    )
    if request.url.path == '/terminal':
        require(not request.query_params, 'unknown_query_parameter')
        body = files('msg.data').joinpath('public-terminal.html').read_bytes()
        script = body.split(b'<script>', 1)[1].split(b'</script>', 1)[0]
        hashed = base64.b64encode(hashlib.sha256(script).digest()).decode()
        headers = {
            **BASE_HEADERS,
            'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; "
            f"script-src 'sha256-{hashed}'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            'Content-Length': str(len(body)),
        }
        require(
            len(body) <= service.settings.server.limits.max_response_bytes, 'response_too_large'
        )
        return Response(
            b'' if request.method == 'HEAD' else body, media_type='text/html', headers=headers
        )

    pairs = request.query_params.multi_items()
    require(len(pairs) == 1 and pairs[0][0] == 'command', 'unknown_query_parameter')
    command = pairs[0][1]
    require(command in COMMANDS, 'invalid_command')
    if command == 'help':
        output = '\n'.join(f'{name:<8} {description}' for name, description in COMMANDS.items())
    elif command == 'clear':
        output = ''
    elif command == 'status':
        output = 'ready' if service._loaded else 'not ready'
    elif command == 'time':
        output = service.clock().astimezone(UTC).isoformat(timespec='seconds')
    elif command == 'server':
        output = service.settings.service_url
    else:
        # Bypass browser session enrichment: even signed-in visitors see anonymous data.
        operation = 'discovery.read_query'
        require(service.registry.operation(operation, 4).effect == 'read', 'effect_mismatch')
        result = await service.executor.execute(
            request_for(
                operation,
                {'home_summary': True},
                service.settings.service_url,
                contract_version=4,
                source='manual',
            )
        )
        if result.error:
            raise Failure(result.error.code)
        data = wire(result.data)
        if command == 'stats':
            output = (
                f'public posts  {data["posts"]}\n'
                f'posts today   {data["posts_today"]}\n'
                f'public users  {data["users"]}\n'
                f'day           {data["date"]} ({data["timezone"]})'
            )
        else:
            output = (
                '\n\n'.join(
                    f'{post["path"]}\n{post["title"]}\n{post["excerpt"]}'.rstrip()
                    for post in data['latest'][:5]
                )
                or 'No public posts yet.'
            )
    response = json_response({'command': command, 'output': output})
    require(
        len(response.body) <= service.settings.server.limits.max_response_bytes,
        'response_too_large',
    )
    if request.method == 'HEAD':
        response.body = b''
    return response
