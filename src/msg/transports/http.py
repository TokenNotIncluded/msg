"""HTTP ingress policy and the public adapter interface.

Route implementations remain in http_routes. The outer guard only rejects
passive execution attempts; it cannot normalize a URL or execute an operation.
All admitted requests still pass the router's URL, Host, proof and authorization
checks. Keeping this guard outside dispatch preserves denial precedence for
removed credential slots without weakening the shared secret classifier.
"""
from __future__ import annotations

from starlette.requests import Request

from msg.core.errors import Failure
from msg.transports.http_routes import (
    BASE_HEADERS, HOME_HEADERS, HOME_HTML, HOME_LOGO, HOME_FAVICON, HOME_MARKDOWN,
    SUBJECT_RESOURCE_ALIASES, SUBJECT_KEY_ALIASES, SUBJECT_OPERATION_ALIASES,
    SEARCH_V2_SEGMENTS, SEARCH_V3_SEGMENTS, SEARCH_V4_SEGMENTS, GREP_V1_SEGMENTS,
    TRANSFER_OPERATIONS, RouteEffect, RouteSpec, operation_route, classify_route,
    passive_client, passive_client_response, decode_query_path, compile_read_query,
    compile_read_query_v2, decode_read_query_path, decode_search_query_path,
    compile_lexical_search, decode_search_v2_path, search_path_from_args,
    path_read_proof, json_response, error_status, body_bytes, describe_resource,
    parse_view, parse_stable_view, create_app as create_router,
)


class PassiveGetBoundary:
    """Deny known write GETs before parsing a credential-bearing representation."""

    def __init__(self, app, *, service):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and scope['method'] == 'GET':
            raw = scope.get('raw_path') or scope['path'].encode('utf-8')
            maximum = self.service.settings.server.limits.max_path_bytes
            if raw.startswith(b'/-/g/') and len(raw) <= maximum:
                request = Request(scope)
                if passive_client(request):
                    try:
                        route = classify_route(raw.decode('ascii'), 'GET', self.service.registry)
                    except (Failure, UnicodeDecodeError):
                        # Unknown and encoded routes are not interpreted here.
                        # Strict raw-target validation still applies downstream.
                        pass
                    else:
                        if route.effect in {RouteEffect.BUSINESS_WRITE, RouteEffect.EXTERNAL_EFFECT}:
                            await passive_client_response()(scope, receive, send)
                            return
        await self.app(scope, receive, send)


def create_app(service):
    app = create_router(service)
    app.add_middleware(PassiveGetBoundary, service=service)
    return app
