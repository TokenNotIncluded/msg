"""HTTP ingress policy and the public adapter interface.

Route implementations remain in http_routes. The outer guard only rejects
passive execution attempts; it cannot normalize a URL or execute an operation.
All admitted requests still pass the router's URL, Host, proof and authorization
checks. Keeping this guard outside dispatch preserves denial precedence for
removed credential slots without weakening the shared secret classifier.
"""

from __future__ import annotations

from starlette.requests import Request as Request
from starlette.responses import JSONResponse as JSONResponse, Response as Response

from msg.core.errors import Failure as Failure
from msg.transports.http_routes import (
    BASE_HEADERS as BASE_HEADERS,
    GREP_V1_SEGMENTS as GREP_V1_SEGMENTS,
    HOME_FAVICON as HOME_FAVICON,
    HOME_HEADERS as HOME_HEADERS,
    HOME_HTML as HOME_HTML,
    HOME_LOGO as HOME_LOGO,
    HOME_MARKDOWN as HOME_MARKDOWN,
    SEARCH_V2_SEGMENTS as SEARCH_V2_SEGMENTS,
    SEARCH_V3_SEGMENTS as SEARCH_V3_SEGMENTS,
    SEARCH_V4_SEGMENTS as SEARCH_V4_SEGMENTS,
    SUBJECT_KEY_ALIASES as SUBJECT_KEY_ALIASES,
    SUBJECT_OPERATION_ALIASES as SUBJECT_OPERATION_ALIASES,
    SUBJECT_RESOURCE_ALIASES as SUBJECT_RESOURCE_ALIASES,
    TRANSFER_OPERATIONS as TRANSFER_OPERATIONS,
    RouteEffect as RouteEffect,
    RouteSpec as RouteSpec,
    body_bytes as body_bytes,
    classify_route as classify_route,
    compile_lexical_search as compile_lexical_search,
    compile_read_query as compile_read_query,
    compile_read_query_v2 as compile_read_query_v2,
    create_app as create_router,
    decode_query_path as decode_query_path,
    decode_read_query_path as decode_read_query_path,
    decode_search_query_path as decode_search_query_path,
    decode_search_v2_path as decode_search_v2_path,
    describe_resource as describe_resource,
    error_status as error_status,
    json_response as json_response,
    operation_route as operation_route,
    parse_stable_view as parse_stable_view,
    parse_view as parse_view,
    passive_client as passive_client,
    passive_client_response as passive_client_response,
    path_read_proof as path_read_proof,
    search_path_from_args as search_path_from_args,
)


class PassiveGetBoundary:
    """Deny known write GETs before parsing a credential-bearing representation."""

    def __init__(self, app, *, service):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        executor = getattr(self.service, 'executor', None)
        if scope['type'] == 'http' and executor is not None:
            try:
                await executor.require_current_runtime()
            except Failure as exc:
                if exc.code != 'recovery_runtime_stale':
                    raise
                response = JSONResponse(
                    {'error': {'code': exc.code, 'retryable': False}},
                    status_code=503,
                    headers=BASE_HEADERS,
                )
                await response(scope, receive, send)
                return
        if scope['type'] == 'http' and executor is not None and executor.recovery_drill_active():
            # Public ACLs in an old snapshot can also have been revoked. Do not
            # serve business content before authority replay and local promotion.
            health = (
                scope.get('raw_path', b'') == b'/healthz'
                and scope['method'] in {'GET', 'HEAD'}
                and not scope.get('query_string')
            )
            data = (
                {
                    'status': 'recovery_quarantined',
                    'ready': False,
                    'writes_enabled': False,
                    'outbound_enabled': False,
                }
                if health
                else {'error': {'code': 'recovery_quarantined', 'retryable': False}}
            )
            response = (
                Response(status_code=503, headers=BASE_HEADERS)
                if scope['method'] == 'HEAD'
                else JSONResponse(data, status_code=503, headers=BASE_HEADERS)
            )
            await response(scope, receive, send)
            return
        if scope['type'] == 'http' and scope['method'] == 'GET':
            raw = scope.get('raw_path') or scope['path'].encode('utf-8')
            maximum = self.service.settings.server.limits.max_path_bytes
            if raw.startswith(b'/-/g/') and len(raw) <= maximum:
                request = Request(scope)
                if passive_client(request):
                    try:
                        route = classify_route(raw.decode('ascii'), 'GET', self.service.registry)
                    except Failure, UnicodeDecodeError:
                        # Unknown and encoded routes are not interpreted here.
                        # Strict raw-target validation still applies downstream.
                        pass
                    else:
                        if route.effect in {
                            RouteEffect.BUSINESS_WRITE,
                            RouteEffect.EXTERNAL_EFFECT,
                        }:
                            await passive_client_response()(scope, receive, send)
                            return
        await self.app(scope, receive, send)


def create_app(service):
    app = create_router(service)
    from msg.transports.oauth_http import OAuthBoundary

    app.add_middleware(OAuthBoundary, service=service)
    app.add_middleware(PassiveGetBoundary, service=service)
    return app
