"""Bounded ingress diagnostics without URLs, headers, packets or message bodies."""

import json
import logging
import time
from uuid import uuid4

from msg.core.errors import Failure
from msg.transports.packet import safe_error_code

LOGGER = logging.getLogger('msg.safe_diagnostics')


def configure():
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


class SafeDiagnostics:
    def __init__(self, app, *, service):
        self.app, self.service = app, service
        configure()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        trace, started = uuid4().hex, time.monotonic()
        operation, code, status, body = None, None, 500, bytearray()
        parts = scope.get('path', '').split('/')
        registry = getattr(self.service, 'registry', None)
        if registry is not None and len(parts) >= 4 and parts[1:3] in (['-', 'p'], ['-', 'g']):
            # Only registry names are retained; never retain arbitrary path segments.
            try:
                spec = registry.operation(parts[3])
                operation = spec.name
            except Failure, KeyError, ValueError:
                pass

        async def traced(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                message = {
                    **message,
                    'headers': [*message.get('headers', []), (b'x-msg-trace-id', trace.encode())],
                }
            elif message['type'] == 'http.response.body':
                data = message.get('body', b'')
                if len(body) + len(data) <= 8192:
                    body.extend(data)
                else:
                    body.clear()
                    # An oversized response cannot provide an error to this diagnostic.
                    body.extend(b' ' * 8193)
            await send(message)

        try:
            await self.app(scope, receive, traced)
        finally:
            if len(body) <= 8192:
                try:
                    value = json.loads(body)
                    error = value.get('error') if isinstance(value, dict) else None
                    if isinstance(error, dict):
                        code = safe_error_code(error.get('code'), 'unspecified_error')
                except ValueError, TypeError:
                    pass
            if scope.get('path', '').startswith('/-/') or status >= 400:
                LOGGER.info(
                    json.dumps(
                        {
                            'stage': 'http_response',
                            'operation': operation,
                            'http_status': status,
                            'error_code': code,
                            'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                            'trace_id': trace,
                        },
                        separators=(',', ':'),
                    )
                )
