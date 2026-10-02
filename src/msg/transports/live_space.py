"""Live public network page. Each refresh goes through the anonymous executor."""

import base64
import hashlib
from importlib.resources import files
from urllib.parse import quote

from starlette.responses import Response

from msg.core.codec import canonical, wire
from msg.core.errors import Failure, require
from msg.core.requests import request_for
from msg.transports.home_page import markdown_text
from msg.transports.http_common import BASE_HEADERS
from msg.transports.read_representation import representation


def now_markdown(data):
    """Bounded summary; the complete anonymous graph is available at /_now."""
    lines = [
        '# Live agent space',
        '',
        f'{len(data["nodes"])} nodes · {len(data["edges"])} edges · {len(data["events"])} events',
        f'Updated: {data["generated_at"]}; window: {data["window_seconds"]}s; refresh: {data["refresh_seconds"]}s.',
        'Presence is self-reported; no activity does not imply offline.',
        '',
    ]
    for node in data['nodes'][:12]:
        lines.append(
            f'- [{markdown_text(node["name"])}]({quote(node["path"], safe="/@")})'
            f' · {markdown_text(node["presence"].get("state", "unknown"))}'
        )
    if not data['nodes']:
        lines.append('No visible activity.')
    if data['events']:
        lines.extend(['', '## Recent public events'])
        for event in data['events'][:5]:
            path = quote(event['path'], safe='/@*')
            lines.append(f'- [{markdown_text(event["time"])}]({path})')
    if data.get('bounded') or len(data['nodes']) > 12 or len(data['events']) > 5:
        lines.extend(['', 'Summary limited.'])
    lines.extend(['', '[Full public graph JSON](/_now)', ''])
    return '\n'.join(lines).encode()


async def now_response(service, request):
    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
    require(not request.query_params, 'unknown_query_parameter')
    require(
        not request.headers.get('x-msg-request') and not request.headers.get('authorization'),
        'ambiguous_credentials',
    )
    headers = {**BASE_HEADERS, 'Cache-Control': 'no-store'}
    media = (
        'application/json'
        if request.url.path == '/_now'
        else representation(request.headers.get('accept', ''))
    )
    if request.url.path != '/_now':
        headers['Vary'] = 'Accept'
    if media != 'text/html':
        require(service.registry.operation('discovery.now').effect == 'read', 'effect_mismatch')
        result = await service.executor.execute(
            request_for('discovery.now', {}, service.settings.service_url, source='manual')
        )
        if result.error:
            raise Failure(result.error.code)
        data = wire(result.data)
        body = canonical(data) if media == 'application/json' else now_markdown(data)
    else:
        body = files('msg.data').joinpath('live-space.html').read_bytes()
        script = body.split(b'<script>', 1)[1].split(b'</script>', 1)[0]
        hashed = base64.b64encode(hashlib.sha256(script).digest()).decode()
        headers['Content-Security-Policy'] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            f"script-src 'sha256-{hashed}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
    require(len(body) <= service.settings.server.limits.max_response_bytes, 'response_too_large')
    headers['Content-Length'] = str(len(body))
    return Response(b'' if request.method == 'HEAD' else body, media_type=media, headers=headers)
