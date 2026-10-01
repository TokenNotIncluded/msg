"""Live public network page. Each refresh goes through the anonymous executor."""

import base64
import hashlib
from importlib.resources import files

from starlette.responses import Response

from msg.core.codec import canonical, wire
from msg.core.errors import Failure, require
from msg.core.requests import request_for
from msg.transports.http_common import BASE_HEADERS


async def now_response(service, request):
    require(request.method in {'GET', 'HEAD'}, 'method_not_allowed')
    require(not request.query_params, 'unknown_query_parameter')
    require(
        not request.headers.get('x-msg-request') and not request.headers.get('authorization'),
        'ambiguous_credentials',
    )
    headers = {**BASE_HEADERS, 'Cache-Control': 'no-store'}
    if request.url.path == '/_now':
        require(service.registry.operation('discovery.now').effect == 'read', 'effect_mismatch')
        result = await service.executor.execute(
            request_for('discovery.now', {}, service.settings.service_url, source='manual')
        )
        if result.error:
            raise Failure(result.error.code)
        body = canonical(wire(result.data))
        media = 'application/json'
    else:
        body = files('msg.data').joinpath('live-space.html').read_bytes()
        script = body.split(b'<script>', 1)[1].split(b'</script>', 1)[0]
        hashed = base64.b64encode(hashlib.sha256(script).digest()).decode()
        headers['Content-Security-Policy'] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            f"script-src 'sha256-{hashed}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        media = 'text/html'
    require(len(body) <= service.settings.server.limits.max_response_bytes, 'response_too_large')
    headers['Content-Length'] = str(len(body))
    return Response(b'' if request.method == 'HEAD' else body, media_type=media, headers=headers)
